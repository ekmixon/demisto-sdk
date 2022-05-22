from typing import Dict, List, Optional


class DeprecationValidator:
    """
        DeprecationValidator is designed to validate that there's no use for deprecated content items inside other
        non-deprecated content items.
    """

    def __init__(self, id_set_file: Dict[str, List]):
        self.script_section = id_set_file.get("scripts", [])
        self.playbook_section = id_set_file.get("playbooks", [])
        self.test_playbook_section = id_set_file.get("TestPlaybooks", [])

    def validate_integartion(self, deprecated_commands_list: List[str], test_playbooks_ls: List[str]):
        """
        Manages the deprecation usage for integration commands
        Checks if the given deprecated integration commands are used in a none-deprecated scripts / playbooks / testplaybooks

        Args:
            deprecated_commands_list (list): A list of all the integration's deprecated commands.
            test_playbooks_ls (list): A list of all the tests that're related to the given integration.

        Return:
            dict: A dictionary where the keys are the integartion's deprecated commands that are used in none-deprecated  scripts / playbooks / testplaybooks.
            The values are the file names where they're being used
        """
        usage_dict: Dict[str, list] = {}

        self.filter_testplaybooks_for_integration_validation(deprecated_commands_list, test_playbooks_ls, usage_dict)
        self.filter_playbooks_for_integration_validation(deprecated_commands_list, usage_dict)
        self.find_scripts_using_given_integration_commands(deprecated_commands_list, usage_dict)

        return usage_dict

    def validate_playbook(self, playbook_name: str, test_playbooks_ls: List[str]):
        """
        Manages the deprecation usage validation for playbooks.
        Checks if the given deprecated playbook is used in a none-deprecated playbooks / testplaybooks.

        Args:
            playbook_name (str): The name of the playbook.
            test_playbooks_ls (list): A list of all the tests that're related to the given playbook.

        Return:
            list: A list of all the none-deprecated files that are using the given playbook.
        """
        usage_list: List[str] = []
        key_to_check = "implementing_playbooks"

        self.filter_testplaybooks_for_scripts_or_playbook_validation(test_playbooks_ls, playbook_name, usage_list, key_to_check)
        self.filter_playbooks_for_scripts_or_playbook_validation(playbook_name, usage_list, key_to_check)

        return usage_list

    def validate_script(self, script_name: str, test_playbooks_ls: List[str]):
        """
        Manages the deprecation usage validation for scripts.
        Checks if the given deprecated script is used in a none-deprecated playbooks / testplaybooks / scripts.

        Args:
            script_name (str): The name of the script.
            test_playbooks_ls (list): A list of all the tests that're related to the given playbook.

        Return:
            list: A list of all the none-deprecated files that are using the given script.
        """
        usage_list: List[str] = []
        key_to_check = "implementing_scripts"

        self.find_scripts_using_given_script(script_name, usage_list)
        self.filter_testplaybooks_for_scripts_or_playbook_validation(test_playbooks_ls, script_name, usage_list, key_to_check)
        self.filter_playbooks_for_scripts_or_playbook_validation(script_name, usage_list, key_to_check)

        return usage_list

    def filter_testplaybooks_for_integration_validation(self, deprecated_commands_list: List[str], test_playbooks_ls: List[str], usage_dict: Dict[str, list]):
        """
        Filter the relevant test_playbooks for the current integration validation from the test_playbook_section
        and check which of the integration commands are being used in this files using the validate_integration_not_in_playbook function.

        Args:
            deprecated_commands_list (list): A list of all the integration's deprecated commands.
            test_playbooks_ls (list): A list of all the tests that're related to the given integration.
            usage_dict (dict): A dictionary where the keys are the integartion's deprecated commands
            that are used in none-deprecated scripts / playbooks / testplaybooks.
            The values are the file names where they're being used.
        """
        if test_playbooks_ls:
            for test_playbook in self.test_playbook_section:
                for test_playbook_key in test_playbook.keys():
                    if test_playbook_key in test_playbooks_ls:
                        command_to_integration = test_playbook.get(test_playbook_key, {}).get("command_to_integration")
                        if command_to_integration:
                            self.validate_integration_commands_not_in_playbook(usage_dict, deprecated_commands_list, command_to_integration, test_playbook)
                        test_playbooks_ls.pop(test_playbook_key)
                        if len(test_playbooks_ls) == 0:
                            return

    def filter_playbooks_for_integration_validation(self, deprecated_commands_list, usage_dict: Dict[str, list]):
        """
        Filter the relevant playbooks for the current integration validation from the playbook_section
        and check which of the integration commands are being used in this files using the validate_integration_not_in_playbook function.

        Args:
            deprecated_commands_list (list): A list of all the integration's deprecated commands.
            usage_dict (dict): A dictionary where the keys are the integartion's deprecated commands
            that are used in none-deprecated scripts / playbooks / testplaybooks.
            The values are the file names where they're being used.
        """
        for playbook in self.playbook_section:
            for playbook_val in playbook.values():
                command_to_integration = playbook_val.get("command_to_integration")
                if command_to_integration:
                    self.validate_integration_commands_not_in_playbook(usage_dict, deprecated_commands_list, command_to_integration, playbook_val)

    def validate_integration_commands_not_in_playbook(self, usage_dict: Dict, deprecated_commands_list: List[str],
                                                      command_to_integration: Dict[str, list], playbook: Dict):
        """
        List all the integration commands of the current checked integration that are being used in the given playbook.
        and update them in the given usage_dict.

        Args:
            deprecated_commands_list (list): A list of all the integration's deprecated commands.
            usage_dict (dict): A dictionary where the keys are the integartion's deprecated commands
            that are used in none-deprecated scripts / playbooks / testplaybooks.
            The values are the file names where they're being used.
            command_to_integration(dict) A dict where the keys are the integartions commands that are being used
            and the value is the integration name.
            playbook (dict): The playbook currently being checked.
        """
        if playbook.get('deprecated'):
            return
        for command in command_to_integration.keys():
            if command in deprecated_commands_list:
                playbook_path: Optional[str] = playbook.get("file_path", "")
                if command in usage_dict and playbook_path not in usage_dict.get(command, []):
                    usage_dict.get(command, []).append(playbook_path)
                if command not in usage_dict:
                    usage_dict[command] = [playbook_path]

    def find_scripts_using_given_integration_commands(self, deprecated_commands_list: List[str], usage_dict: Dict):
        """
        List all the integration commands of the current checked integration that are being used in the none-deprecated scripts
        and update them in the given usage_dict.

        Args:
            deprecated_commands_list (list): A list of all the integration's deprecated commands.
            usage_dict (dict): A dictionary where the keys are the integartion's deprecated commands
            that are used in none-deprecated scripts / playbooks / testplaybooks.
            The values are the file names where they're being used.
        """
        for script in self.script_section:
            for script_val in script.values():
                if script_val.get("deprecated"):
                    return
                depends_commads_list = script_val.get("depends_on")
                if depends_commads_list:
                    for command in depends_commads_list:
                        if command in deprecated_commands_list:
                            script_path = script_val.get("file_path", "")
                            if command in usage_dict and script_path not in usage_dict.get(command, []):
                                usage_dict.get(command, []).append(script_path)
                            elif command not in usage_dict:
                                usage_dict[command] = [script_path]

    def find_scripts_using_given_script(self, script_name: str, usage_list: List[str]):
        """
        List all the pathes of scripts that are using the given script.

        Args:
            script_name (str): The name of the script that is currently being checked.
            usage_list (list): A list of all the files that use the given script to update the found scripts into.
        """
        for script in self.script_section:
            for script_val in script.values():
                if script_val.get("deprecated"):
                    return
                depends_commads_list = script_val.get("depends_on")
                if depends_commads_list and script_name in depends_commads_list:
                    usage_list.append(script_val.get("file_path"))

    def filter_testplaybooks_for_scripts_or_playbook_validation(self, test_playbooks_ls: List[str], curent_entity_name: str,
                                                                usage_list: List[str], key_to_check: str):
        """
        Filter the relevant testplaybooks for the current script / playbook validation from the test_playbook_section.

        Args:
            test_playbooks_ls (list): A list of all the tests that're related to the given integration.
            curent_entity_name (str): The name of the script / playbook currently being checked.
            usage_list (list): A list of all the file paths that are using the script / playbook currently being checked.
            key_to_check (str): The field in which the right entity to check is located at.
        """
        if test_playbooks_ls:
            for test_playbook in self.test_playbook_section:
                for test_playbook_key in test_playbook.keys():
                    if test_playbook_key in test_playbooks_ls:
                        implementing_entities = test_playbook.get(test_playbook_key, {}).get(key_to_check)
                        if implementing_entities:
                            self.validate_playbook_or_script_not_in_playbook(usage_list, curent_entity_name, implementing_entities, test_playbook)
                        test_playbooks_ls.pop(test_playbook_key)
                        if len(test_playbooks_ls) == 0:
                            return

    def filter_playbooks_for_scripts_or_playbook_validation(self, curent_entity_name: str, usage_list: List[str], key_to_check: str):
        """
        Filter the relevant playbooks for the current script / playbook validation from the playbook_section.

        Args:
            curent_entity_name (str): The name of the script / playbook currently being checked.
            usage_list (list): A list of all the file paths that are using the script / playbook currently being checked.
            key_to_check (str): The field in which the right entity to check is located at.
        """
        for playbook in self.playbook_section:
            for playbook_val in playbook.values():
                implementing_entities = playbook_val.get(key_to_check)
                if implementing_entities:
                    self.validate_playbook_or_script_not_in_playbook(usage_list, curent_entity_name, implementing_entities, playbook)

    def validate_playbook_or_script_not_in_playbook(self, usage_list: List[str], curent_entity_name: str, implementing_entities: List[str], playbook: Dict):
        """
        List all the playbooks paths of playbooks that are using the current checked playbook / script.

        Args:
            usage_list (list): A list of all the file paths that are using the script / playbook currently being checked.
            curent_entity_name (str): The name of the script / playbook currently being checked.
            implementing_entities(dict) A list of all the entites that this currently checked playbook is using.
            playbook (dict): The playbook currently being checked.
        """
        if playbook.get('deprecated'):
            return
        for implementing_entity in implementing_entities:
            if implementing_entity == curent_entity_name:
                usage_list.append(playbook.get("file_path", ""))
