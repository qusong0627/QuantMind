"""quantdb_daily_sync V2 指纹对账测试。

背景：2026-10 发现上游重算并重发了同一 key 的分区（l2_factors 补 17 列、
l1_factors 补 2 列），但同步脚本只要「key 已登记 + 文件存在」就跳过，
导致本地 2025 年 119 个交易日持续缺 19 列。

本文件锁定新的跳过规则：release 带 sha256 时，必须状态库指纹与最新
release 指纹一致才跳过；不一致必须重下并覆盖。
"""

import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.scripts import quantdb_daily_sync as sync


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]


class FakeClient:
    """最小 QuantDBClient 替身：releases / manifest / 对象下载。"""

    def __init__(self, objects=None, contents=None, manifest=None):
        self.objects = objects or {}
        self.contents = contents or {}
        self.manifest = manifest or []
        self.downloads = []

    def _get(self, path, params):
        return {
            "releases": [{"release_id": "r1", "objects": list(self.objects.values())}]
        }

    def query_manifest(self, category_id=None, sub_category=None):
        return self.manifest

    def _normalise_release_key(self, key):
        return key

    def _download_stream(self, params):
        key = params["object_key"]
        self.downloads.append(key)
        return FakeResponse(self.contents[key])


def _state(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.execute(
        "CREATE TABLE objects (key TEXT PRIMARY KEY, etag TEXT, sha256 TEXT,"
        " size INTEGER, path TEXT, layout TEXT, dataset TEXT)"
    )
    conn.execute(
        "CREATE TABLE releases (dataset TEXT PRIMARY KEY, release_id TEXT NOT NULL)"
    )
    return conn


def _release_obj(key: str, content: bytes) -> dict:
    return {
        "key": key,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "etag": "",
    }


def _seed_local(tmp_path: Path, key: str, content: bytes):
    target = tmp_path / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _insert_state(state, key, content, target, dataset="l2_factors"):
    state.execute(
        "INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
        (
            key,
            "",
            hashlib.sha256(content).hexdigest(),
            len(content),
            str(target),
            "v2_daily_partition",
            dataset,
        ),
    )


def test_released_partition_refreshed_when_sha_changes(tmp_path, monkeypatch):
    """同尺寸、不同内容也必须重下（19 列事故的最小复现）。"""
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/l2_factors/dt=20250102/data.parquet"
    target = _seed_local(tmp_path, key, b"old")
    state = _state(tmp_path)
    _insert_state(state, key, b"old", target)

    client = FakeClient({key: _release_obj(key, b"new")}, {key: b"new"})
    done, errors = sync._sync_v2_dataset(client, state, "6", "l2_factors")

    assert (done, errors) == (1, 0)
    assert target.read_bytes() == b"new"
    row = state.execute(
        "SELECT sha256, size FROM objects WHERE key=?", (key,)
    ).fetchone()
    assert row == (hashlib.sha256(b"new").hexdigest(), 3)


def test_unchanged_partition_skipped_without_download(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/l2_factors/dt=20250102/data.parquet"
    target = _seed_local(tmp_path, key, b"new")
    state = _state(tmp_path)
    _insert_state(state, key, b"new", target)

    client = FakeClient({key: _release_obj(key, b"new")}, {key: b"new"})
    done, errors = sync._sync_v2_dataset(client, state, "6", "l2_factors")

    assert (done, errors) == (0, 0)
    assert client.downloads == []


def test_missing_state_row_but_matching_file_is_registered(tmp_path, monkeypatch):
    """状态库丢失时不能整库重下：哈希对上就只补登记。"""
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/l2_factors/dt=20250102/data.parquet"
    _seed_local(tmp_path, key, b"new")
    state = _state(tmp_path)

    client = FakeClient({key: _release_obj(key, b"new")}, {key: b"new"})
    done, errors = sync._sync_v2_dataset(client, state, "6", "l2_factors")

    assert (done, errors) == (0, 0)
    assert client.downloads == []
    row = state.execute("SELECT sha256 FROM objects WHERE key=?", (key,)).fetchone()
    assert row[0] == hashlib.sha256(b"new").hexdigest()


def test_download_size_mismatch_keeps_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/l2_factors/dt=20250102/data.parquet"
    target = _seed_local(tmp_path, key, b"old")
    state = _state(tmp_path)
    _insert_state(state, key, b"old", target)

    obj = {**_release_obj(key, b"new"), "size": 99}
    client = FakeClient({key: obj}, {key: b"new"})
    done, errors = sync._sync_v2_dataset(client, state, "6", "l2_factors")

    assert (done, errors) == (0, 1)
    assert target.read_bytes() == b"old"


def test_manifest_only_partition_skipped_when_size_matches(tmp_path, monkeypatch):
    """manifest-only 对象没有 sha256，尺寸一致视为未变化。"""
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/l2_factors/dt=20250102/data.parquet"
    target = _seed_local(tmp_path, key, b"new")
    state = _state(tmp_path)
    _insert_state(state, key, b"new", target)

    client = FakeClient(
        objects={},
        contents={key: b"new"},
        manifest=[{"key": key, "size": 3, "etag": '"abc"'}],
    )
    done, errors = sync._sync_v2_dataset(client, state, "6", "l2_factors")

    assert (done, errors) == (0, 0)
    assert client.downloads == []


def test_manifest_only_size_drift_triggers_download(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/l2_factors/dt=20250102/data.parquet"
    target = _seed_local(tmp_path, key, b"old")
    state = _state(tmp_path)
    _insert_state(state, key, b"old", target)

    client = FakeClient(
        objects={},
        contents={key: b"new!"},
        manifest=[{"key": key, "size": 4, "etag": '"abc"'}],
    )
    done, errors = sync._sync_v2_dataset(client, state, "6", "l2_factors")

    assert (done, errors) == (1, 0)
    assert target.read_bytes() == b"new!"


def test_stale_release_sha_overridden_by_manifest_size(tmp_path, monkeypatch):
    """上游覆盖对象但未发 release：release sha 指向旧文件，以 manifest 尺寸为准。"""
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/l1_factors/dt=20250102/data.parquet"
    target = _seed_local(tmp_path, key, b"old")
    state = _state(tmp_path)
    _insert_state(state, key, b"old", target, dataset="l1_factors")

    release_obj = _release_obj(key, b"old")  # release 仍指向本地旧内容
    client = FakeClient(
        objects={key: release_obj},
        contents={key: b"new!"},
        manifest=[{"key": key, "size": 4, "etag": '"m"'}],
    )
    done, errors = sync._sync_v2_dataset(client, state, "6", "l1_factors")

    assert (done, errors) == (1, 0)
    assert target.read_bytes() == b"new!"
    row = state.execute(
        "SELECT sha256, size FROM objects WHERE key=?", (key,)
    ).fetchone()
    assert row == (hashlib.sha256(b"new!").hexdigest(), 4)


def test_release_control_object_skipped(tmp_path, monkeypatch):
    """kind=control 的控制对象（如 v5_factor_dictionary.json）不是分区，不能下载。

    网关 /api/v1/data/download 只认 manifest 内的数据对象，控制对象会 400，
    曾导致 factor_dictionary 每次同步都记为「1 个对象下载失败」。
    """
    monkeypatch.setattr(sync, "QUANTDB_DATA_DIR", tmp_path)
    key = "6_ml_datasets/factor_dictionary/dt=20261030/data.parquet"
    control_key = "6_ml_datasets/factor_dictionary/v5_factor_dictionary.json"
    control_obj = {**_release_obj(control_key, b"{}"), "kind": "control"}
    client = FakeClient(
        objects={key: _release_obj(key, b"data"), control_key: control_obj},
        contents={key: b"data"},
    )
    state = _state(tmp_path)

    done, errors = sync._sync_v2_dataset(client, state, "6", "factor_dictionary")

    assert (done, errors) == (1, 0)
    assert control_key not in client.downloads
    assert (tmp_path / key).read_bytes() == b"data"
