from demisto_sdk.commands.common.content.objects.pack_objects import \
    ModelingRule
from demisto_sdk.commands.common.content.objects_factory import \
    path_to_pack_object
from demisto_sdk.commands.common.tools import get_yaml


def get_modeling_rule(pack, name):
    return pack.create_modeling_rule(name)


class TestModelingRule:
    def test_objects_factory(self, pack):
        modeling_rule = get_modeling_rule(pack, 'modeling_rule_name')
        obj = path_to_pack_object(modeling_rule.yml._tmp_path)
        assert isinstance(obj, ModelingRule)

    def test_prefix(self, pack):
        modeling_rule = get_modeling_rule(pack, 'modelingrule-modeling_rule_name')
        obj = ModelingRule(modeling_rule._tmpdir_rule_path)
        assert obj.normalize_file_name() == modeling_rule.yml._tmp_path.name

        modeling_rule = get_modeling_rule(pack, 'modeling_rule_name')
        obj = ModelingRule(modeling_rule._tmpdir_rule_path)
        assert obj.normalize_file_name() == f"modelingrule-{modeling_rule.yml._tmp_path.name}"

    def test_files_detection(self, pack):
        modeling_rule = get_modeling_rule(pack, 'modeling_rule_name')
        obj = ModelingRule(modeling_rule._tmpdir_rule_path)
        # assert obj.yml._tmp_path == Path(datadir["README.md"])
        assert obj.rules_path == modeling_rule.rules._tmp_path

    def test_is_unify(self, pack):
        modeling_rule = get_modeling_rule(pack, 'modeling_rule_name')
        obj = ModelingRule(modeling_rule._tmpdir_rule_path)
        assert not obj.is_unify()

    def test_unify_schema(self, pack):
        """
        Given:
        modeling rule:
        - yml file
        - the rule xif file
        - the schema json file
        When:
         - we want to unify all files to one unified yml file.

         Then:
         - Ensure the schema was unified successfully.
        """
        modeling_rule = get_modeling_rule(pack, 'modeling_rule_name')
        obj = ModelingRule(modeling_rule._tmpdir_rule_path)
        unify_obj = get_yaml(obj._unify(modeling_rule._tmpdir_rule_path)[0])
        assert unify_obj['schema'] == '{\n    "test_audit_raw": {\n        "name": {\n            "type": "string",\n' \
                                      '            "is_array": false\n        }\n    }\n}'
