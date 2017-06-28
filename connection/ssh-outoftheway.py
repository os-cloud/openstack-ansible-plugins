# Copyright 2016, Rackspace US, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# (c) 2016, Kevin Carter <kevin.carter@rackspace.com>

import imp
import os

# NOTICE(cloudnull): The connection plugin imported using the full path to the
#                    file because the ssh connection plugin is not importable.
import ansible.plugins.connection as conn
SSH = imp.load_source(
    'ssh',
    os.path.join(os.path.dirname(conn.__file__), 'ssh.py')
)

if not hasattr(SSH, 'shlex_quote'):
    # NOTE(cloudnull): Later versions of ansible has this attribute already
    #                  however this is not set in all versions. Because we use
    #                  this method the attribute will set within the plugin
    #                  if it's not found.
    from ansible.compat.six.moves import shlex_quote
    setattr(SSH, 'shlex_quote', shlex_quote)


class Connection(SSH.Connection):
    """Transport options for LXC containers.

    This transport option makes the assumption that the playbook context has
    vars within it that contain "physical_host" which is the machine running a
    given container and "container_name" which is the actual name of the
    container. These options can be added into the playbook via vars set as
    attributes or though the modification of the a given execution strategy to
    set the attributes accordingly.

    This plugin operates exactly the same way as the standard SSH plugin but
    will pad pathing or add command syntax for lxc containers when a container
    is detected at runtime.
    """

    transport = 'ssh'

    def __init__(self, *args, **kwargs):
        super(Connection, self).__init__(*args, **kwargs)
        self.args = args
        self.kwargs = kwargs
        if hasattr(self._play_context, 'chroot_path'):
            self.chroot_path = self._play_context.chroot_path
        else:
            self.chroot_path = None
        if hasattr(self._play_context, 'container_name'):
            self.container_name = self._play_context.container_name
        else:
            self.container_name = None
        if hasattr(self._play_context, 'physical_host'):
            self.physical_host = self._play_context.physical_host
        else:
            self.physical_host = None
        if hasattr(self._play_context, 'container_type'):
            self.container_type = self._play_context.container_type
        else:
            self.container_type = 'lxc'

    def set_host_overrides(self, host, hostvars=None):
        if self._container_check() or self._chroot_check():
            physical_host_addrs = hostvars.get('physical_host_addrs', {})
            physical_host_addr = physical_host_addrs.get(self.physical_host,
                                                         self.physical_host)
            self.host = self._play_context.remote_addr = physical_host_addr

    def exec_command(self, cmd, in_data=None, sudoable=True):
        """run a command on the remote host."""

        if self._container_check():
            # Remote user is normally set, but if it isn't, then default to 'root'
            container_user = 'root'
            if self._play_context.remote_user:
                container_user = SSH.to_bytes(self._play_context.remote_user,
                                              errors='surrogate_or_strict')
            # NOTE(hwoarang) It is important to connect to the container
            # without inheriting the host environment as that would interfere
            # with running commands and services inside the container. However,
            # it is also important to create a sensible environment within the
            # container because certain commands and services expect some
            # enviromental variables to be set properly. The best way to do
            # that would be to execute the commands in a login shell
            lxc_command = 'lxc-attach --clear-env --name %s' % self.container_name

            # NOTE(hwoarang): the shlex_quote method is necessary here because
            # we need to properly quote the cmd as it's being passed as argument
            # to the -c su option. The Ansible ssh class has already
            # quoted the command of the _executable_ (ie /bin/bash -c "$cmd").
            # However, we also need to quote the executable itself because the
            # entire command is being passed to the su process. This produces
            # a somewhat ugly output with too many quotes in a row but we can't
            # do much since we are effectively passing a command to a command
            # to a command etc... It's somewhat ugly but maybe it can be
            # improved somehow...
            cmd = '%s -- su - %s -c %s' % (lxc_command, container_user,
                                           SSH.shlex_quote(cmd))

        if self._chroot_check():
            chroot_command = 'chroot %s' % self.chroot_path
            cmd = '%s %s' % (chroot_command, cmd)

        return super(Connection, self).exec_command(cmd, in_data, sudoable)

    def _chroot_check(self):
        if self.chroot_path is not None:
            SSH.display.vvv(u'chroot_path: "%s"' % self.chroot_path)
            if self.physical_host is not None:
                SSH.display.vvv(
                    u'physical_host: "%s"' % self.physical_host
                )
                SSH.display.vvv(u'chroot confirmed')
                return True

        return False

    def _container_check(self):
        if self.container_type != 'lxc':
            return False

        if self.container_name is not None:
            SSH.display.vvv(u'container_name: "%s"' % self.container_name)
            if self.physical_host is not None:
                SSH.display.vvv(
                    u'physical_host: "%s"' % self.physical_host
                )
                if self.container_name != self.physical_host:
                    SSH.display.vvv(u'Container confirmed')
                    return True

        return False

    def _container_path_pad(self, path, fake_path=False):
        args = (
            'ssh',
            self.host,
            u"lxc-info --name %s --pid | awk '/PID:/ {print $2}'"
            % self.container_name
        )
        returncode, stdout, _ = self._run(
            self._build_command(*args),
            in_data=None,
            sudoable=False
        )
        if returncode == 0:
            pad = os.path.join(
                '/proc/%s/root' % stdout.strip(),
                path.lstrip(os.sep)
            )
            SSH.display.vvv(
                u'The path has been padded with the following to support a'
                u' container rootfs: [ %s ]' % pad
            )
            return pad
        else:
            raise SSH.AnsibleError(
                u'No valid container info was found for container "%s" Please'
                u' check the state of the container.' % self.container_name
            )

    def fetch_file(self, in_path, out_path):
        """fetch a file from remote to local."""
        if self._container_check():
            in_path = self._container_path_pad(path=in_path)

        return super(Connection, self).fetch_file(in_path, out_path)

    def put_file(self, in_path, out_path):
        """transfer a file from local to remote."""
        if self._container_check():
            out_path = self._container_path_pad(path=out_path)

        return super(Connection, self).put_file(in_path, out_path)

    def close(self):
        # If we have a persistent ssh connection (ControlPersist), we can ask it
        # to stop listening. Otherwise, there's nothing to do here.
        if self._connected and self._persistent:
            cmd = self._build_command('ssh', '-O', 'stop', self.host)
            cmd = map(SSH.to_bytes, cmd)
            p = SSH.subprocess.Popen(cmd, stdin=SSH.subprocess.PIPE, stdout=SSH.subprocess.PIPE, stderr=SSH.subprocess.PIPE)
            p.communicate()
