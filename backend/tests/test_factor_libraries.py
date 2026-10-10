"""因子值库目录（T-MV-06）——schema 金样 + 校验规则 + 磁盘事实夹具。

三条纪律：

1. **金样钉策展**：``factor_libraries_golden.json`` 是
   ``load_factor_libraries()`` 读仓库 ``config/factor_libraries.yaml`` 的逐位
   快照（机器无关）。改 YAML 不改金样 → 本文件红；金样里没有任何列数/日期
   ——那些是磁盘事实，进金样必漂移。
2. **校验响亮失败**：坏目录宁可炸不可静默半残（展示面据此渲染两组方向）。
   每个 ``pytest.raises`` 同时校验报错**点名了原因**（id/kind/leakage），
   防止「炸了但看不出为什么」。
3. **磁盘事实用 tmp 夹具测**：``market_data_dir`` 与 ``library_disk_facts``
   都可注入（monkeypatch），断言的是读取逻辑本身（分组/回退/降级），不依赖
   宿主机真实数据面。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.services.engine.mining_plugins import factor_libraries
from backend.services.engine.mining_plugins.factor_libraries import (
    FactorLibraryError,
    factor_libraries_payload,
    library_disk_facts,
    load_factor_libraries,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── 夹具 ──────────────────────────────────────────────────────────────


def _entry(lib_id: str = "lib_a", **over) -> dict:
    base = {
        "id": lib_id,
        "name": "库 A",
        "kind": "mined",
        "description": "夹具库",
        "markets": ["CN"],
    }
    base.update(over)
    return base


def _write_config(tmp_path: Path, libraries: list, version: int = 1) -> Path:
    import yaml

    path = tmp_path / "factor_libraries.yaml"
    payload = {"version": version, "checked_at": "2026-01-01", "libraries": libraries}
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _expect_error(config_path: Path, needle: str) -> None:
    with pytest.raises(FactorLibraryError) as ei:
        load_factor_libraries(config_path)
    assert needle in str(ei.value), f"报错应点名 {needle!r}，实际：{ei.value}"


def _patch_market_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "backend.services.engine.data_platform.quantdb_factor_reader.market_data_dir",
        lambda market: tmp_path / market,
    )


def _write_parquet(path: Path, columns: int = 3) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({f"c{i}": [1.0] for i in range(columns)})
    pq.write_table(table, path)


# ── 金样：策展 = 仓库 YAML 的逐位快照 ────────────────────────────────


def test_repo_catalog_matches_golden():
    golden = json.loads(
        (FIXTURES / "factor_libraries_golden.json").read_text(encoding="utf-8")
    )
    assert load_factor_libraries() == golden


def test_golden_has_no_disk_facts():
    """金样只钉策展：列数/日期这类磁盘事实一旦写进金样，明天就会漂。"""
    golden = json.loads(
        (FIXTURES / "factor_libraries_golden.json").read_text(encoding="utf-8")
    )
    for lib in golden["libraries"]:
        assert set(lib) == {"id", "name", "kind", "description", "excluded", "markets"}
        assert all(isinstance(m, str) for m in lib["markets"])


# ── 校验规则：响亮失败 + 报错点名 ────────────────────────────────────


def test_missing_file_raises(tmp_path):
    with pytest.raises(FactorLibraryError, match="not found"):
        load_factor_libraries(tmp_path / "nope.yaml")


def test_top_level_must_be_mapping(tmp_path):
    path = tmp_path / "factor_libraries.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    _expect_error(path, "must be a mapping")


def test_unsupported_version_rejected(tmp_path):
    _expect_error(_write_config(tmp_path, [_entry()], version=2), "unsupported version")


def test_empty_libraries_rejected(tmp_path):
    _expect_error(_write_config(tmp_path, []), "non-empty list")


def test_unknown_kind_rejected(tmp_path):
    _expect_error(_write_config(tmp_path, [_entry(kind="whatever")]), "unknown kind")


def test_duplicate_id_rejected(tmp_path):
    _expect_error(
        _write_config(tmp_path, [_entry(), _entry(name="库 A 二号")]), "duplicate id"
    )


def test_path_traversal_id_rejected(tmp_path):
    """id 会拼进磁盘路径——'../' 这类字符必须在校验层就拒。"""
    _expect_error(_write_config(tmp_path, [_entry(id="../evil")]), "id must match")


def test_missing_name_or_description_rejected(tmp_path):
    _expect_error(_write_config(tmp_path, [_entry(name=" ")]), "missing name")
    _expect_error(
        _write_config(tmp_path, [_entry(description="")]), "missing description"
    )


def test_labels_kind_requires_excluded(tmp_path):
    """安全不变量：标签库漏标 excluded 直接炸，不允许溜进可选方向。"""
    _expect_error(_write_config(tmp_path, [_entry(kind="labels")]), "leakage safety")


def test_non_bool_excluded_rejected(tmp_path):
    _expect_error(_write_config(tmp_path, [_entry(excluded=1)]), "must be a boolean")


def test_markets_validation(tmp_path):
    _expect_error(_write_config(tmp_path, [_entry(markets=[])]), "non-empty list")
    _expect_error(_write_config(tmp_path, [_entry(markets=["XX"])]), "unknown market")
    _expect_error(
        _write_config(tmp_path, [_entry(markets=["CN", "cn"])]), "duplicate market"
    )


def test_normalization(tmp_path):
    """市场码统一大写、字符串去空白；labels 带 excluded 合法通过。"""
    path = _write_config(
        tmp_path,
        [_entry(markets=["cn", " hk "]), _entry("lib_b", kind="labels", excluded=True)],
    )
    out = load_factor_libraries(path)
    assert out["version"] == 1
    assert out["checked_at"] == "2026-01-01"
    assert out["libraries"][0]["markets"] == ["CN", "HK"]
    assert out["libraries"][0]["excluded"] is False
    assert out["libraries"][1]["excluded"] is True


# ── 磁盘事实：tmp 夹具注入 ───────────────────────────────────────────


def test_disk_facts_reads_schema_and_range(monkeypatch, tmp_path):
    _patch_market_root(monkeypatch, tmp_path)
    _write_parquet(
        tmp_path / "CN" / "6_ml_datasets" / "lib_a" / "dt=20240102" / "data.parquet", 3
    )
    _write_parquet(
        tmp_path / "CN" / "6_ml_datasets" / "lib_a" / "dt=20240105" / "data.parquet", 3
    )
    facts = library_disk_facts("CN", "lib_a")
    assert facts == {"columns": 3, "start": "2024-01-02", "end": "2024-01-05"}


def test_disk_facts_finds_nonstandard_parquet_name(monkeypatch, tmp_path):
    _patch_market_root(monkeypatch, tmp_path)
    _write_parquet(
        tmp_path / "CN" / "6_ml_datasets" / "lib_a" / "dt=20240102" / "part-0.parquet",
        2,
    )
    facts = library_disk_facts("CN", "lib_a")
    assert facts["columns"] == 2
    assert facts["start"] == "2024-01-02"


def test_disk_facts_unpartitioned(monkeypatch, tmp_path):
    """无 dt= 分区（整库单文件）→ 列数仍读出，日期范围诚实为 None。"""
    _patch_market_root(monkeypatch, tmp_path)
    _write_parquet(tmp_path / "CN" / "6_ml_datasets" / "lib_a" / "data.parquet", 4)
    facts = library_disk_facts("CN", "lib_a")
    assert facts == {"columns": 4, "start": None, "end": None}


def test_disk_facts_missing_or_empty_dir_is_none(monkeypatch, tmp_path):
    _patch_market_root(monkeypatch, tmp_path)
    assert library_disk_facts("CN", "ghost") is None
    (tmp_path / "CN" / "6_ml_datasets" / "empty_lib").mkdir(parents=True)
    assert library_disk_facts("CN", "empty_lib") is None


def test_disk_facts_unreadable_schema_keeps_dates(monkeypatch, tmp_path):
    """schema 读不出 → columns=None，但日期范围仍是真的（部分事实胜过丢弃）。"""
    _patch_market_root(monkeypatch, tmp_path)
    bad = tmp_path / "CN" / "6_ml_datasets" / "lib_a" / "dt=20240102" / "data.parquet"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a parquet")
    facts = library_disk_facts("CN", "lib_a")
    assert facts["columns"] is None
    assert facts["start"] == "2024-01-02"


# ── 端点载荷：markets 展开 + 单库失败降级 ───────────────────────────


def test_payload_expands_markets(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path, [_entry(markets=["CN", "HK"]), _entry("lib_b", markets=["US"])]
    )
    monkeypatch.setattr(
        factor_libraries,
        "library_disk_facts",
        lambda market, lib_id: {"columns": 9, "start": "x", "end": "y"},
    )
    payload = factor_libraries_payload(path)
    assert payload["checked_at"] == "2026-01-01"
    lib_a = payload["libraries"][0]
    assert lib_a["markets"] == {
        "CN": {"columns": 9, "start": "x", "end": "y"},
        "HK": {"columns": 9, "start": "x", "end": "y"},
    }


def test_payload_single_fact_failure_degrades_to_none(monkeypatch, tmp_path):
    """单库事实读取抛错只降级该市场（None），不拖垮整份目录。"""
    path = _write_config(tmp_path, [_entry(markets=["CN", "HK"])])

    def fake(market: str, lib_id: str):
        if market == "HK":
            raise OSError("mount gone")
        return {"columns": 1, "start": None, "end": None}

    monkeypatch.setattr(factor_libraries, "library_disk_facts", fake)
    payload = factor_libraries_payload(path)
    markets = payload["libraries"][0]["markets"]
    assert markets["CN"] == {"columns": 1, "start": None, "end": None}
    assert markets["HK"] is None


# ── 端点合并（真目录，非 HTTP）──────────────────────────────────────


def test_endpoint_merges_categories_and_libraries():
    """路由把两段拼进同一 data；libraries 段来自仓库目录（15 库）。"""
    from backend.services.engine.routers.alpha_agent import get_factor_categories

    resp = asyncio.run(get_factor_categories())
    assert resp["code"] == 200
    data = resp["data"]
    assert isinstance(data["categories"], list)
    ids = [lib["id"] for lib in data["libraries"]]
    assert "l1_factors" in ids and "features_daily" in ids
    assert len(ids) == 15
    for lib in data["libraries"]:
        assert isinstance(lib["markets"], dict)
    assert data["libraries_checked_at"] == "2026-10-10"
