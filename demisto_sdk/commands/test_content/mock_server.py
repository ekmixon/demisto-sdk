from __future__ import print_function

import ast
import os
import string
import time
import unicodedata
from contextlib import contextmanager
from pprint import pformat
from subprocess import (STDOUT, CalledProcessError, call, check_call,
                        check_output)
from threading import Lock
from typing import Dict, Iterator

import demisto_client.demisto_api
import urllib3

from demisto_sdk.commands.test_content.constants import SSH_USER

VALID_FILENAME_CHARS = f'-_.() {string.ascii_letters}{string.digits}'
PROXY_PROCESS_INIT_TIMEOUT = 20
PROXY_PROCESS_INIT_INTERVAL = 1
RESULT = 'result'
# Disable insecure warnings
urllib3.disable_warnings()


def clean_filename(playbook_or_integration_id, whitelist=VALID_FILENAME_CHARS, replace=' ()'):
    filename = playbook_or_integration_id

    # replace spaces
    for r in replace:
        filename = filename.replace(r, '_')

    # keep only valid ascii chars
    cleaned_filename = unicodedata.normalize('NFKD', filename).encode('ASCII', 'ignore').decode()

    # keep only whitelisted chars
    cleaned_filename = ''.join(c for c in cleaned_filename if c in whitelist)
    return cleaned_filename


def silence_output(cmd_method, *args, **kwargs):
    """Redirect linux command output(s) to /dev/null
    To redirect to /dev/null: pass 'null' string in 'stdout' or 'stderr' keyword args

    Args:
        cmd_method (PyFunctionObject): the "subprocess" (or wrapper) method to run
        *args: additional parameters for cmd_method
        **kwargs: additional parameters for cmd_method

    Returns:
        string. output of cmd_method
    """
    with open(os.devnull, 'w') as fnull:
        for k in ('stdout', 'stderr'):
            if kwargs.get(k) == 'null':
                kwargs[k] = fnull

        return cmd_method(*args, **kwargs)


def get_mock_file_path(playbook_or_integration_id):
    clean = clean_filename(playbook_or_integration_id)
    return os.path.join(clean + '/', clean + '.mock')


def get_log_file_path(playbook_or_integration_id, record=False):
    clean = clean_filename(playbook_or_integration_id)
    suffix = '_record' if record else '_playback'
    return os.path.join(clean + '/', clean + suffix + '.log')


def get_folder_path(playbook_or_integration_id):
    return clean_filename(playbook_or_integration_id) + '/'


class AMIConnection:
    """Wrapper for AMI communication.

    Attributes:
        internal_ip (string): The  internal IP of the instance.
    """

    REMOTE_HOME = f'/home/{SSH_USER}/'
    LOCAL_SCRIPTS_DIR = '/home/circleci/project/Tests/scripts/'

    def __init__(self, internal_ip):
        self.internal_ip = internal_ip

    def add_ssh_prefix(self, command, ssh_options=""):
        """Add necessary text before a command in order to run it on the AMI instance via SSH.

        Args:
            command (list): Command to run on the AMI machine (according to "subprocess" interface).
            ssh_options (string): optional parameters for ssh connection to AMI.

        Returns:
            string: ssh command that will run the desired command on the AMI.
        """
        if ssh_options and not isinstance(ssh_options, str):
            raise TypeError("options must be string")
        if not isinstance(command, list):
            raise TypeError("command must be list")
        prefix = "ssh {} {}@{}".format(ssh_options, SSH_USER, self.internal_ip).split()
        return prefix + command

    def call(self, command, **kwargs):
        return call(self.add_ssh_prefix(command), **kwargs)

    def check_call(self, command, **kwargs):
        return check_call(self.add_ssh_prefix(command), **kwargs)

    def check_output(self, command, **kwargs):
        return check_output(self.add_ssh_prefix(command), **kwargs)

    def copy_file(self, src, dst=REMOTE_HOME, **kwargs):
        silence_output(check_call, ['scp', src, "{}@{}:{}".format(SSH_USER, self.internal_ip, dst)],
                       stdout='null', **kwargs)
        return os.path.join(dst, os.path.basename(src))

    def run_script(self, script, *args):
        """Copy a script to the AMI and run it.

        Args:
            script (string): Name of the script file in the LOCAL_SCRIPTS_DIR.
            *args: arguments to be passed to the script.
        """
        remote_script_path = self.copy_file(os.path.join(self.LOCAL_SCRIPTS_DIR, script))

        silence_output(self.check_call, ['chmod', '+x', remote_script_path], stdout='null')
        silence_output(self.check_call, [remote_script_path] + list(args), stdout='null')


class MITMProxy:
    """Manager for MITM Proxy and the mock file structure.

    Attributes:
        logging_module: Logging module to use
        public_ip (string): The IP of the AMI instance.
        repo_folder (string): path to the local clone of the content-test-data git repo.
        tmp_folder (string): path to a temporary folder for log/mock files before pushing to git.
        current_folder (string): the current folder to use for mock/log files.
        ami (AMIConnection): Wrapper for AMI communication.
        empty_files (list): List of playbooks that have empty mock files (indicating no usage of mock mechanism).
        rerecorded_tests (list): List of playbook ids that failed on mock playback but succeeded on new recording.
        build_number (str): The number of the circleci build.
        branch_name (str): The name of the content branch in which the current job works on.
    """

    PROXY_PORT = '9997'
    MOCKS_TMP_PATH = '/tmp/Mocks/'
    MOCKS_GIT_PATH = f'{AMIConnection.REMOTE_HOME}content-test-data/'
    TIME_TO_WAIT_FOR_PROXY_SECONDS = 30
    content_data_lock = Lock()

    def __init__(self,
                 internal_ip,
                 logging_module,
                 build_number,
                 branch_name,
                 repo_folder=MOCKS_GIT_PATH,
                 tmp_folder=MOCKS_TMP_PATH,
                 ):
        is_branch_master = branch_name == 'master'
        self.internal_ip = internal_ip
        self.current_folder = self.repo_folder = repo_folder
        self.tmp_folder = tmp_folder
        self.logging_module = logging_module
        self.build_number = build_number
        self.ami = AMIConnection(self.internal_ip)
        self.should_update_mock_repo = is_branch_master
        self.should_validate_playback = not is_branch_master
        self.empty_files = []
        self.failed_tests_count = 0
        self.successful_tests_count = 0
        self.successful_rerecord_count = 0
        self.failed_rerecord_count = 0
        self.failed_rerecord_tests = []
        self.rerecorded_tests = []
        silence_output(self.ami.call, ['mkdir', '-p', tmp_folder], stderr='null')
        script_filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timestamp_replacer.py')
        self.ami.copy_file(script_filepath)

    def commit_mock_file(self, folder_name):
        self.logging_module.debug('Committing mock files')
        try:
            output = self.ami.check_output(
                'cd content-test-data && '
                'git add * -v && '
                f'git commit -m "Updated mock files for \'{folder_name}\' from build number - {self.build_number}" -v'.split(),
                stderr=STDOUT)
            self.logging_module.debug(f'Committing mock files output:\n{output.decode()}')
        except CalledProcessError as exc:
            self.logging_module.debug(f'Committing mock files output:\n{exc.output.decode()}')

    def reset_mock_files(self):
        self.logging_module.debug('Resetting mock files')
        try:
            output = self.ami.check_output(
                'cd content-test-data && git reset --hard'.split(),
                stderr=STDOUT)
            self.logging_module.debug(f'Resetting mock files output:\n{output.decode()}')
        except CalledProcessError as exc:
            self.logging_module.debug(f'Resetting mock files output:\n{exc.output.decode()}')

    def push_mock_files(self):
        self.logging_module.debug('Pushing new/updated mock files to mock git repo.', real_time=True)
        self.content_data_lock.acquire()
        try:
            output = self.ami.check_output(
                'cd content-test-data && git reset --hard && git pull -r -Xtheirs && git push -f'.split(),
                stderr=STDOUT)
            self.logging_module.debug(f'Pushing mock files output:\n{output.decode()}', real_time=True)
        except CalledProcessError as exc:
            self.logging_module.debug(f'Pushing mock files output:\n{exc.output.decode()}', real_time=True)
        except Exception as exc:
            self.logging_module.debug(f'Failed pushing mock files with error: {exc}', real_time=True)
        finally:
            self.content_data_lock.release()

    def configure_proxy_in_demisto(self, username, password, server, api_key=None, auth_id=None, proxy=''):
        client = demisto_client.configure(base_url=server, username=username,
                                          password=password, api_key=api_key, auth_id=auth_id, verify_ssl=False)
        self.logging_module.debug('Adding proxy server configurations')
        system_conf_response = demisto_client.generic_request_func(
            self=client,
            path='/system/config',
            method='GET'
        )
        system_conf = ast.literal_eval(system_conf_response[0]).get('sysConf', {})
        self.logging_module.debug(f'Server configurations before proxy server configurations:\n{pformat(system_conf)}')
        http_proxy = https_proxy = proxy
        if proxy:
            http_proxy = 'http://' + proxy
            https_proxy = 'http://' + proxy
        system_conf.update({
            'http_proxy': http_proxy,
            'https_proxy': https_proxy
        })
        data = {
            'data': system_conf,
            'version': -1
        }
        response = demisto_client.generic_request_func(self=client, path='/system/config',
                                                       method='POST', body=data)
        self.logging_module.debug(f'Server configurations response:\n{pformat(ast.literal_eval(response[0]))}')

        return response

    def get_mock_file_size(self, filepath):
        return self.ami.check_output(['stat', '-c', '%s', filepath]).strip()

    @staticmethod
    def get_script_mode(is_record: bool) -> str:
        """
        Returns the string describing script mode for the SCRIPT_MODE env variable needed for the mitmdump initialization
        Args:
            is_record: A boolean indicating this is record mode or not

        Returns:
            'record' if is_record is True else 'playback'
        """
        return 'record' if is_record else 'playback'

    def has_mock_file(self, playbook_or_integration_id):
        command = ["[", "-f", os.path.join(self.current_folder, get_mock_file_path(playbook_or_integration_id)), "]"]
        return self.ami.call(command) == 0

    def has_mock_folder(self, playbook_or_integration_id):
        command = ["[", "-d", os.path.join(self.current_folder, get_folder_path(playbook_or_integration_id)), "]"]
        return self.ami.call(command) == 0

    def set_repo_folder(self):
        """Set the repo folder as the current folder (the one used to store mock and log files)."""
        self.current_folder = self.repo_folder

    def set_tmp_folder(self):
        """Set the temp folder as the current folder (the one used to store mock and log files)."""
        self.current_folder = self.tmp_folder

    def move_mock_file_to_repo(self, playbook_or_integration_id):
        """Move the mock and log files of a (successful) test playbook run from the temp folder to the repo folder

        Args:
            playbook_or_integration_id (string): ID of the test playbook or integration of which the files should be moved.
        """
        src_filepath = os.path.join(self.tmp_folder, get_mock_file_path(playbook_or_integration_id))
        src_files = os.path.join(self.tmp_folder, get_folder_path(playbook_or_integration_id) + '*')
        dst_folder = os.path.join(self.repo_folder, get_folder_path(playbook_or_integration_id))

        if not self.has_mock_file(playbook_or_integration_id):
            self.logging_module.debug('Mock file not created!')
        elif self.get_mock_file_size(src_filepath) == '0':
            self.logging_module.debug('Mock file is empty, ignoring.')
            self.empty_files.append(playbook_or_integration_id)
        else:
            # Move to repo folder
            self.logging_module.debug(f'Moving "{src_files}" files to "{dst_folder}" directory')
            self.ami.call(['mkdir', '--parents', dst_folder])
            self.ami.call(['mv', src_files, dst_folder])

    def normalize_mock_file(self, playbook_or_integration_id: str, path: str = None):
        """Normalize the mock file of a test playbook

        Normalizes a mock file by doing the following:
        1. Replaces the timestamp/variable data in a mock file as identified by
        the keys provided in the mock file's associated 'problematic_keys.json'
        file, with constant values.
        2. Standardizes the query parameter data order for all requests.

        Args:
            playbook_or_integration_id: ID of the test playbook or
                integration for which the associated mock file will be
                normalized.
            path: Path to the directory in which to search
                for the required data (temp directory or repo). If not
                provided, method will use the current directory.
        """
        self.logging_module.debug(f'normalize_mock_file was called for test "{playbook_or_integration_id}"')
        path = path or self.current_folder
        problem_keys_filepath = os.path.join(path, get_folder_path(playbook_or_integration_id), 'problematic_keys.json')
        self.logging_module.debug(f'problem_keys_filepath="{problem_keys_filepath}"')
        problem_key_file_exists = ["[", "-f", problem_keys_filepath, "]"]
        if not self.ami.call(problem_key_file_exists) == 0:
            self.logging_module.debug('Error: The problematic_keys.json file was not written to the file path'
                                      f' "{problem_keys_filepath}" when recording '
                                      f'the "{playbook_or_integration_id}" test playbook')
            return

        mock_file_path = os.path.join(path, get_mock_file_path(playbook_or_integration_id))
        cleaned_mock_filepath = mock_file_path.strip('.mock') + '_cleaned.mock'
        log_file = os.path.join(path, get_log_file_path(playbook_or_integration_id, record=True))
        command = '/home/ec2-user/.local/bin/mitmdump -ns ~/timestamp_replacer.py ' \
                  f'--set script_mode=clean --set keys_filepath={problem_keys_filepath}' \
                  f' -r {mock_file_path} -w {cleaned_mock_filepath} | sudo tee -a {log_file}'
        self.logging_module.debug(f'command to normalize mockfile:\n\t{command}')
        self.logging_module.debug('Let\'s try and normalize the mockfile')
        try:
            check_output(self.ami.add_ssh_prefix(command.split(), ssh_options='-t'), stderr=STDOUT)
        except CalledProcessError as e:
            self.logging_module.debug(
                'There may have been a problem while normalizing the mock file.')
            err_msg = f'command `{command}` exited with return code [{e.returncode}]'
            err_msg = f'{err_msg} and the output of "{e.output}"' if e.output else err_msg
            if e.stderr:
                err_msg += f'STDERR: {e.stderr}'
            self.logging_module.debug(err_msg)
        else:
            self.logging_module.debug('Success!')

        # verify cleaned mock is different than original
        diff_cmd = f'diff -sq {cleaned_mock_filepath} {mock_file_path}'
        try:
            diff_cmd_output = self.ami.check_output(diff_cmd.split()).decode().strip()
            self.logging_module.debug(f'diff_cmd_output={diff_cmd_output}')
            if diff_cmd_output.endswith('are identical'):
                self.logging_module.debug('normalized mock file and original mock file are identical')
            else:
                self.logging_module.debug('the normalized mock file differs from the original')

        except CalledProcessError:
            self.logging_module.debug('the normalized mock file differs from the original')

        self.logging_module.debug('Replacing original mock file with the normalized one.')
        mv_cmd = f'mv {cleaned_mock_filepath} {mock_file_path}'
        self.ami.call(mv_cmd.split())

    def start(self, playbook_or_integration_id, path=None, record=False) -> None:
        """Start the proxy process and direct traffic through it.

        Args:
            playbook_or_integration_id (string): ID of the test playbook to run.
            path (string): path override for the mock/log files.
            record (bool): Select proxy mode (record/playback)
        """
        if self.is_proxy_listening():
            self.logging_module.debug('proxy service is already running, stopping it')
            self.ami.call(['sudo', 'systemctl', 'stop', 'mitmdump'])
        self.logging_module.debug(f'Attempting to start proxy in {self.get_script_mode(record)} mode')
        self.prepare_proxy_start(path, playbook_or_integration_id, record)
        # Start proxy server
        self._start_proxy_and_wait_until_its_up(is_record=record)

    def _start_proxy_and_wait_until_its_up(self, is_record: bool) -> None:
        """
        Starts mitmdump service and wait for it to listen to port 9997 with timeout of 5 seconds
        Args:
            is_record (bool):  Indicates whether this is a record run or not
        """
        self._start_mitmdump_service()
        was_proxy_up = self.wait_until_proxy_is_listening()
        if was_proxy_up:
            self.logging_module.debug(f'Proxy service started in {self.get_script_mode(is_record)} mode')
        else:
            self.logging_module.error(f'Proxy failed to start after {self.TIME_TO_WAIT_FOR_PROXY_SECONDS} seconds')

    def _start_mitmdump_service(self) -> None:
        """
        Starts mitmdump service on the remote service
        """
        self.ami.call(['sudo', 'systemctl', 'start', 'mitmdump'])

    def prepare_proxy_start(self,
                            path: str,
                            playbook_or_integration_id: str,
                            record: bool) -> bool:
        """
        Writes proxy server run configuration options to the remote host, the details of which include:
        - Creating new tmp directory on remote machine if in record mode and moves the problematic keys file in to it
        - Creating the mitmdump_rc file that includes the script mode, keys file path, mock file path and log file path
          for the mitmdump service and puts it in '/home/ec2-user/mitmdump_rc' in the remote machine.
        - starts the systemd mitmdump service

        Args:
            path: the path to the temp folder in which the record files should be created
            playbook_or_integration_id: The ID of the playbook or integration that is tested
            record: Indicates whether this is a record run or not
        """
        path = path or self.current_folder
        folder_path = get_folder_path(playbook_or_integration_id)

        repo_problem_keys_path = os.path.join(self.repo_folder, folder_path, 'problematic_keys.json')
        current_problem_keys_path = os.path.join(path, folder_path, 'problematic_keys.json')
        log_file_path = os.path.join(path, get_log_file_path(playbook_or_integration_id, record))
        mock_file_path = os.path.join(path, get_mock_file_path(playbook_or_integration_id))

        file_content = f'export KEYS_FILE_PATH="{current_problem_keys_path if record else repo_problem_keys_path}"\n'
        file_content += f'export SCRIPT_MODE={self.get_script_mode(record)}\n'
        file_content += f'export MOCK_FILE_PATH="{mock_file_path}"\n'
        file_content += f'export LOG_FILE_PATH="{log_file_path}"\n'

        # Create mock files directory
        silence_output(self.ami.call, ['mkdir', os.path.join(path, folder_path)], stderr='null')
        # when recording, copy the `problematic_keys.json` for the test to current temporary directory if it exists
        # that way previously recorded or manually added keys will only be added upon and not wiped with an overwrite
        if record:
            try:
                silence_output(self.ami.call,
                               ['mv', repo_problem_keys_path, current_problem_keys_path],
                               stdout='null',
                               stderr='null')
            except CalledProcessError as e:
                self.logging_module.debug(f'Failed to move problematic_keys.json with exit code {e.returncode}')

        return self._write_mitmdump_rc_file_to_host(file_content)

    def _write_mitmdump_rc_file_to_host(self,
                                        file_content: str) -> bool:
        """
        Does all needed preparation for starting the proxy service which include:
        - Creating the mitmdump_rc file that includes the script mode, keys file path, mock file path and log file path
          for the mitmdump service and puts it in '/home/ec2-user/mitmdump_rc' in the remote machine.

        Args:
            file_content: The content of the mitmdump_rc file that includes the script mode, keys file path,
            mock file path and log file path

        Returns:
            True if file was successfully copied to the server, else False
        """
        try:
            self.ami.call(['echo', f"'{file_content}'", '>', os.path.join(AMIConnection.REMOTE_HOME, 'mitmdump_rc')])
            return True
        except CalledProcessError:
            self.logging_module.exception(
                f'Could not copy arg file for mitmdump service to server {self.ami.internal_ip},')
        return False

    def wait_until_proxy_is_listening(self):
        """
        Checks if the mitmdump service is listening, and raises an exception if 30 seconds pass without positive answer
        """
        for i in range(self.TIME_TO_WAIT_FOR_PROXY_SECONDS):
            proxy_is_listening = self.is_proxy_listening()
            if proxy_is_listening:
                return True
            time.sleep(1)
        return False

    def is_proxy_listening(self) -> bool:
        """
        Runs 'sudo lsof -iTCP:9997 -sTCP:LISTEN' on the remote machine and returns answer according to the results
        Returns:
            True if the ssh command return exit code 0 and False otherwise
        """
        try:
            self.ami.check_output(['sudo', 'lsof', '-iTCP:9997', '-sTCP:LISTEN'])
            return True
        except CalledProcessError:
            return False

    def stop(self):
        self.logging_module.debug('Stopping mitmdump service')
        if not self.is_proxy_listening():
            self.logging_module.debug('proxy service was already down.')
        else:
            self.ami.call(['sudo', 'systemctl', 'stop', 'mitmdump'])
        self.ami.call(["rm", "-rf", "/tmp/_MEI*"])  # Clean up temp files


@contextmanager
def run_with_mock(proxy_instance: MITMProxy,
                  playbook_or_integration_id: str,
                  record: bool = False) -> Iterator[dict]:
    """
    Runs proxy in a context
    If it's a record mode:
        - Setting the current folder of the proxy to the tmp folder before mitmdump starts
        - Starts the mitmdump service in record mode
        - Handles the newly created mock files
        - Setting the current folder of the proxy to the repo folder after mitmdump stops
    If it's a playback mode:
        - Starts the mitmdump service in playback mode
        - In case the playback failed - will show the status of the mitmdump service which will include the last 10
        lines of the service's log.
    Args:
        proxy_instance: The instance of the proxy to use
        playbook_or_integration_id: The ID of the playbook or integration that is tested
        record: A boolean indicating this is record mode or not
    Yields: A result holder dict in which the calling method can add the result of the proxy run under the key 'result'
    """
    if record:
        proxy_instance.set_tmp_folder()
        # If the record files should be committed - clean the content-test-data repo first
        if proxy_instance.should_update_mock_repo:
            proxy_instance.logging_module.debug('Cleaning content-test-data repo')
            proxy_instance.reset_mock_files()
    proxy_instance.start(playbook_or_integration_id, record=record)
    result_holder: Dict[str, bool] = {}
    try:
        yield result_holder
    except Exception:
        proxy_instance.logging_module.exception('Unexpected failure in proxy context manager')
    finally:
        proxy_instance.stop()
        if record:
            if result_holder.get(RESULT):
                proxy_instance.normalize_mock_file(playbook_or_integration_id)
                proxy_instance.move_mock_file_to_repo(playbook_or_integration_id)
                proxy_instance.successful_rerecord_count += 1
                proxy_instance.rerecorded_tests.append(playbook_or_integration_id)
                if proxy_instance.should_update_mock_repo:
                    proxy_instance.logging_module.debug("committing new/updated mock files to mock git repo.")
                    proxy_instance.commit_mock_file(playbook_or_integration_id)
            else:
                proxy_instance.failed_rerecord_count += 1
                proxy_instance.failed_rerecord_tests.append(playbook_or_integration_id)
            proxy_instance.set_repo_folder()

        else:
            if result_holder.get(RESULT):
                proxy_instance.successful_tests_count += 1
            else:
                proxy_instance.failed_tests_count += 1
