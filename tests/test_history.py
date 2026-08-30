from easy_prd2.history import HistoryStore
from easy_prd2.models import RunRecord


def make_run() -> RunRecord:
    return RunRecord(
        id="run-1",
        status="planned",
        source_api_base="https://source.example/api/v1",
        target_api_base="https://target.example/api/v1",
        source_organization_id=1,
        source_organization_name="Source",
        target_organization_id=2,
        target_organization_name="Target",
        workspace_id=3,
        workspace_name="Demo copy",
        queue_ids=[4, 5],
    )


def test_history_round_trip_and_delete(tmp_path):
    store = HistoryStore(tmp_path)
    run = make_run()
    store.save(run)
    loaded = store.get(run.id)
    assert loaded is not None
    assert loaded.workspace_name == "Demo copy"
    assert len(store.list()) == 1
    store.write_artifact(run.id, "deploy.yaml", "workspaces: []")
    store.delete(run.id)
    assert store.get(run.id) is None
    assert not (store.artifacts_dir / run.id).exists()


def test_history_schema_has_no_credential_columns(tmp_path):
    store = HistoryStore(tmp_path)
    columns = {row[1] for row in store._connect().execute("PRAGMA table_info(runs)").fetchall()}
    assert "token" not in columns
    assert "credentials" not in columns

