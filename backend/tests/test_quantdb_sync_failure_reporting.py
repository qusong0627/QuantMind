import sqlite3

from backend.scripts import quantdb_daily_sync as sync


def test_state_writes_do_not_hold_lock_between_operations(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(sync, "_state_path", lambda: tmp_path / "state.sqlite")
    first = sync._open_state()
    second = sqlite3.connect(tmp_path / "state.sqlite", timeout=0.1)
    try:
        first.execute("INSERT INTO releases VALUES (?, ?)", ("one", "1"))
        second.execute("INSERT INTO releases VALUES (?, ?)", ("two", "2"))
        second.commit()
    finally:
        first.close()
        second.close()


def test_dataset_error_is_reported_and_next_dataset_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(sync, "_state_path", lambda: tmp_path / "state.sqlite")
    monkeypatch.setattr(sync, "_make_client", lambda: object())
    datasets = [{"sub_category": "broken", "category_id": "1"}, {"sub_category": "ok", "category_id": "1"}]
    def run(client, state, cat, name, **kwargs):
        if name == "broken":
            raise sqlite3.OperationalError("database is locked")
        return 0, 0
    monkeypatch.setattr(sync, "_sync_v1_dataset", run)
    events = []
    result = sync.sync_parquet(datasets, progress_cb=lambda event, **kw: events.append((event, kw)))
    assert result["errors"] == ["broken: database is locked"]
    assert result["up_to_date"] == 1
    assert [r["status"] for r in result["datasets"]] == ["failed", "up_to_date"]
    assert [event for event, _ in events] == ["dataset_start", "dataset_done", "dataset_start", "dataset_done"]
    assert events[1][1]["error"] == "database is locked"
