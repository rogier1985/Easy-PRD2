import yaml
import json

from easy_prd2.models import CopyRequest
from easy_prd2.prd2_adapter import Prd2Adapter, artifact_manifest_is_create_only, assert_prd2_contract


def manifest(target_id=None, patch=False):
    return yaml.safe_dump(
        {
            "patch_target_org": patch,
            "workspaces": [
                {
                    "id": 10,
                    "name": "Source",
                    "targets": [{"id": target_id, "attribute_override": {"name": "Copy"}}],
                }
            ],
        }
    )


def test_create_only_manifest_accepts_empty_target_ids():
    assert artifact_manifest_is_create_only(manifest())


def test_create_only_manifest_rejects_existing_target_ids_and_org_patch():
    assert not artifact_manifest_is_create_only(manifest(999))
    assert not artifact_manifest_is_create_only(manifest(patch=True))


def test_pinned_prd2_contract():
    assert_prd2_contract()


def test_manifest_builder_selects_queue_and_applies_names(tmp_path):
    source = tmp_path / "source"
    queue_dir = source / "workspaces" / "AP_[10]" / "queues" / "Invoices_[20]"
    queue_dir.mkdir(parents=True)
    (queue_dir.parent.parent / "workspace.json").write_text(
        json.dumps({"id": 10, "name": "AP", "url": "https://source/api/v1/workspaces/10", "queues": []})
    )
    (queue_dir / "queue.json").write_text(
        json.dumps(
            {
                "id": 20,
                "name": "Invoices",
                "url": "https://source/api/v1/queues/20",
                "workspace": "https://source/api/v1/workspaces/10",
                "schema": "https://source/api/v1/schemas/30",
                "hooks": [],
                "workflows": ["https://source/api/v1/workflows/40"],
            }
        )
    )
    (queue_dir / "schema.json").write_text(
        json.dumps({"id": 30, "name": "Invoice schema", "url": "https://source/api/v1/schemas/30"})
    )
    request = CopyRequest(
        source_connection_id="source",
        target_connection_id="target",
        source_organization_id=1,
        target_organization_id=2,
        workspace_id=10,
        queue_ids=[20],
        target_workspace_name="Customer demo",
        target_queue_names={20: "Customer invoices"},
        target_hook_owner_id=99,
    )
    adapter = Prd2Adapter(gateway=None)  # type: ignore[arg-type]
    manifest_data, items, warnings, follow_ups = adapter._build_manifest(
        request, source, "https://source/api/v1", "https://target/api/v1", {}
    )
    text = yaml.safe_dump(manifest_data)
    assert artifact_manifest_is_create_only(text)
    assert manifest_data["patch_target_org"] is False
    assert manifest_data["workspaces"][0]["targets"][0]["attribute_override"]["name"] == "Customer demo"
    assert manifest_data["queues"][0]["targets"][0]["attribute_override"]["name"] == "Customer invoices"
    assert {item.type for item in items} == {"workspace", "queue"}
    assert any("workflows" in item for item in follow_ups)
    assert not warnings


def test_manifest_builder_skips_rule_with_action_queue_outside_selection(tmp_path):
    source = tmp_path / "source"
    queue_dir = source / "workspaces" / "AP_[10]" / "queues" / "Invoices_[20]"
    queue_dir.mkdir(parents=True)
    (queue_dir.parent.parent / "workspace.json").write_text(
        json.dumps({"id": 10, "name": "AP", "url": "https://source/api/v1/workspaces/10", "queues": []})
    )
    (queue_dir / "queue.json").write_text(
        json.dumps(
            {
                "id": 20,
                "name": "Invoices",
                "url": "https://source/api/v1/queues/20",
                "workspace": "https://source/api/v1/workspaces/10",
                "schema": "https://source/api/v1/schemas/30",
                "hooks": [],
            }
        )
    )
    (queue_dir / "schema.json").write_text(
        json.dumps({"id": 30, "name": "Invoice schema", "url": "https://source/api/v1/schemas/30"})
    )
    rules_dir = source / "rules"
    rules_dir.mkdir()
    (rules_dir / "safe.json").write_text(
        json.dumps(
            {
                "id": 40,
                "name": "Keep in selected queue",
                "queues": ["https://source/api/v1/queues/20"],
                "actions": [{"payload": {"queue": "https://source/api/v1/queues/20"}}],
            }
        )
    )
    (rules_dir / "unsafe.json").write_text(
        json.dumps(
            {
                "id": 41,
                "name": "Move to unselected queue",
                "queues": ["https://source/api/v1/queues/20"],
                "actions": [{"payload": {"queue": "https://source/api/v1/queues/21"}}],
            }
        )
    )
    request = CopyRequest(
        source_connection_id="source",
        target_connection_id="target",
        source_organization_id=1,
        target_organization_id=2,
        workspace_id=10,
        queue_ids=[20],
        target_workspace_name="Customer demo",
        target_queue_names={20: "Customer invoices"},
        target_hook_owner_id=99,
    )

    manifest_data, items, warnings, _ = Prd2Adapter(gateway=None)._build_manifest(  # type: ignore[arg-type]
        request, source, "https://source/api/v1", "https://target/api/v1", {}
    )

    assert [rule["id"] for rule in manifest_data["rules"]] == [40]
    assert ("rule", 40) in {(item.type, item.source_id) for item in items}
    assert ("rule", 41) not in {(item.type, item.source_id) for item in items}
    assert warnings == [
        "Rule ‘Move to unselected queue’ references queue(s) outside the selection in its actions (21) "
        "and will not be copied."
    ]
