import yaml

from app.agents.routing_agent import validate_changes


def test_release_config_on_master_and_image_tag_are_reported():
    with open("config/rules_catalog.yaml", encoding="utf-8") as file:
        rules = yaml.safe_load(file)
    changes = [{"new_path": "deployment/qa2/values.yaml", "diff": "+image:\n+  tag: v2\n"}]
    violations = validate_changes("master", changes, rules)
    assert len(violations) == 2


def test_release_branch_and_unrelated_paths():
    with open("config/rules_catalog.yaml", encoding="utf-8") as file:
        rules = yaml.safe_load(file)
    changes = [{"new_path": "src/example.yaml", "diff": "+tag: x\n"}]
    assert validate_changes("master", changes, rules) == []
    changes = [{"new_path": "deployment/qa2/values.yaml", "diff": "+replicas: 2\n"}]
    assert validate_changes("release/VN2609-1", changes, rules) == []
    assert validate_changes("release/not-valid", changes, rules)
