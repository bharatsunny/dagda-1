#
# Licensed to Dagda under one or more contributor
# license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright
# ownership. Dagda licenses this file to you under
# the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import io
import os
import time
import json
import subprocess
import datetime
from shutil import copyfile
from exception.dagda_error import DagdaError
from log.dagda_logger import DagdaLogger
from api.internal.internal_server import InternalServer


# Sysdig Falco monitor class

class SysdigFalcoMonitor:

    # -- Private attributes

    _tmp_directory = "/tmp"
    _falco_output_filename = _tmp_directory + '/falco_output.json'
    _falco_custom_rules_filename = _tmp_directory + '/custom_falco_rules.yaml'

    # -- Public methods

    # SysdigFalcoMonitor Constructor
    def __init__(self, docker_driver, mongodb_driver, falco_rules_filename, external_falco_output_filename):
        super(SysdigFalcoMonitor, self).__init__()
        self.mongodb_driver = mongodb_driver
        self.docker_driver = docker_driver
        self.running_container_id = ''
        if falco_rules_filename is None:
            self.falco_rules = ''
        else:
            copyfile(falco_rules_filename, SysdigFalcoMonitor._falco_custom_rules_filename)
            self.falco_rules = ' -o rules_file=/host' + SysdigFalcoMonitor._falco_custom_rules_filename
        if external_falco_output_filename is not None:
            InternalServer.set_external_falco(True)
            SysdigFalcoMonitor._falco_output_filename = external_falco_output_filename

    # Pre check for Sysdig falco container
    def pre_check(self):
        if not InternalServer.is_external_falco():
            # Init
            linux_distro = SysdigFalcoMonitor._get_linux_distro()
            uname_r = os.uname().release

            # Check requirements
            if not os.path.isfile('/.dockerenv'):  # I'm living in real world!
                if 'Red Hat' in linux_distro or 'CentOS' in linux_distro or 'Fedora' in linux_distro \
                        or 'openSUSE' in linux_distro:
                    # Red Hat/CentOS/Fedora/openSUSE
                    return_code = subprocess.call(["rpm", "-q", "kernel-devel-" + uname_r],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif 'Debian' in linux_distro or 'Ubuntu' in linux_distro:
                    # Debian/Ubuntu
                    return_code = subprocess.call(["dpkg", "-l", "linux-headers-" + uname_r],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    raise DagdaError('Linux distribution not supported yet.')

                if return_code != 0:
                    raise DagdaError('The kernel headers are not installed in the host operating system.')
            else:  # I'm running inside a docker container
                DagdaLogger.get_logger().warning("I'm running inside a docker container, so I can't check if the kernel "
                                                 "headers are installed in the host operating system. Please, review it!!")

            # Check Docker driver
            if self.docker_driver.get_docker_client() is None:
                raise DagdaError('Error while fetching Docker server API version.')

            # Docker pull for ensuring the falcosecurity/falco image
            self.docker_driver.docker_pull('falcosecurity/falco', tag='0.29.0')

            # Stops sysdig/falco containers if there are any
            container_ids = self.docker_driver.get_docker_container_ids_by_image_name('falcosecurity/falco:0.29.0')
            if len(container_ids) > 0:
                for container_id in container_ids:
                    self.docker_driver.docker_stop(container_id)
                    self.docker_driver.docker_remove_container(container_id)

            # Cleans mongodb falco_events collection
            self.mongodb_driver.delete_falco_events_collection()

            # Starts sysdig running container without custom entrypoint for avoiding:
            # --> Runtime error: error opening device /host/dev/sysdig0
            self.running_container_id = self._start_container()
            time.sleep(30)
            logs = self.docker_driver.docker_logs(self.running_container_id, True, True, False)
            if "Runtime error: error opening device /host/dev/sysdig0" not in logs:
                self.docker_driver.docker_stop(self.running_container_id)
            else:
                raise DagdaError('Runtime error opening device /host/dev/sysdig0.')
            # Clean up
            self.docker_driver.docker_remove_container(self.running_container_id)

    # Runs SysdigFalcoMonitor
    def run(self):
        if not InternalServer.is_external_falco():
            self.running_container_id = self._start_container('falco -pc -o json_output=true -o file_output.enabled=true ' +
                                                              '-o file_output.filename=/host' +
                                                              SysdigFalcoMonitor._falco_output_filename +
                                                              self.falco_rules)

            # Wait 3 seconds for sysdig/falco start up and creates the output file
            time.sleep(3)

        # Check output file and running docker container
        if not os.path.isfile(SysdigFalcoMonitor._falco_output_filename) or \
            (not InternalServer.is_external_falco() and \
            len(self.docker_driver.get_docker_container_ids_by_image_name('falcosecurity/falco:0.29.0')) == 0):
            raise DagdaError('Falcosecurity/falco output file not found.')

        # Review sysdig/falco logs after rules parser
        if not InternalServer.is_external_falco():
            sysdig_falco_logs = self.docker_driver.docker_logs(self.running_container_id, True, True, False)
            if "Rule " in sysdig_falco_logs:
                SysdigFalcoMonitor._parse_log_and_show_dagda_warnings(sysdig_falco_logs)

        # Read file
        with open(SysdigFalcoMonitor._falco_output_filename, 'rb') as f:
            last_file_position = 0
            fbuf = io.BufferedReader(f)
            while True:
                fbuf.seek(last_file_position)
                content = fbuf.readlines()
                sysdig_falco_events = []
                for line in content:
                    falco_event = {}
                    line = line.decode('utf-8').replace("\n", "")
                    json_data = json.loads(line)
                    container_id = json_data['output_fields']['container.id']
                    if container_id != 'host':
                        try:
                            falco_event['container_id'] = container_id
                            falco_event['image_name'] = json_data['output_fields']['container.image.repository']
                            if 'container.image.tag' in json_data['output_fields']:
                                falco_event['image_name'] += ":" + json_data['output_fields']['container.image.tag']
                            falco_event['output'] = json_data['output']
                            falco_event['priority'] = json_data['priority']
                            falco_event['rule'] = json_data['rule']
                            falco_event['time'] = json_data['time']
                            sysdig_falco_events.append(falco_event)
                        except IndexError:
                            # The /tmp/falco_output.json file had information about ancient events, so nothing to do
                            pass
                        except KeyError:
                            # The /tmp/falco_output.json file had information about ancient events, so nothing to do
                            pass
                last_file_position = fbuf.tell()
                if len(sysdig_falco_events) > 0:
                    self.mongodb_driver.bulk_insert_sysdig_falco_events(sysdig_falco_events)
                time.sleep(2)

    # Gets running container id
    def get_running_container_id(self):
        return self.running_container_id

    # -- Private methods

    # Starts Sysdig falco container
    def _start_container(self, entrypoint=None):
        # Start container
        container_id = self.docker_driver.create_container('falcosecurity/falco:0.29.0',
                                                           entrypoint,
                                                           [
                                                              '/host/var/run/docker.sock',
                                                              '/host/dev',
                                                              '/host/proc',
                                                              '/host/boot',
                                                              '/host/lib/modules',
                                                              '/host/usr',
                                                              '/host/etc',
                                                              '/host' + SysdigFalcoMonitor._tmp_directory
                                                           ],
                                                           self.docker_driver.get_docker_client().create_host_config(
                                                              binds=[
                                                                  '/var/run/docker.sock:/host/var/run/docker.sock',
                                                                  '/dev:/host/dev',
                                                                  '/proc:/host/proc:ro',
                                                                  '/boot:/host/boot:ro',
                                                                  '/lib/modules:/host/lib/modules:ro',
                                                                  '/usr:/host/usr:ro',
                                                                  '/etc:/host/etc:ro',
                                                                  SysdigFalcoMonitor._tmp_directory + ':/host' +
                                                                            SysdigFalcoMonitor._tmp_directory + ':rw'
                                                              ],
                                                              privileged=True))
        self.docker_driver.docker_start(container_id)
        return container_id

    # -- Private & static methods

    # Parse sysdig/falco logs after rules parser
    @staticmethod
    def _parse_log_and_show_dagda_warnings(sysdig_falco_logs):
        date_prefix = datetime.datetime.now().strftime("%A")[:3] + ' '
        lines = sysdig_falco_logs.split("\n")
        warning = ''
        for line in lines:
            if line.startswith(date_prefix) is not True:
                line = line.strip()
                if line.startswith('Rule '):
                    if warning:
                        DagdaLogger.get_logger().warning(warning.strip())
                    warning = ''
                warning+=' ' + line

    # Avoids the "platform.linux_distribution()" method which is deprecated in Python 3.5
    @staticmethod
    def _get_linux_distro():
        with open('/etc/os-release', 'r') as f:
            lines = f.readlines()
        for line in lines:
            if line.startswith('NAME='):
                name = line.replace('NAME=', '').replace("\n", '').replace("'", '').replace('"', '')
                return name
