"""data_fingerprint 契约测试（P1 · 设计文档 §4.5）。

覆盖两层：
1. ``docker/training/data/fingerprint.py`` 纯逻辑——实况区间/行数、双口径截止日
   （prices 源探测 / labels 实况）、manifest 摘要（文件数 + mtime + 清单 sha1）、
   全链路降级不抛（探测失败 / 无 factor_source / 垃圾输入）；
2. train.py 两处 metadata 构造点均落盘 ``data_fingerprint``（G0 晋升闸门读的是
   落盘字段，漏一处 = 单模型路径的 run 全部缺件）。

按 docker/training 既有测试模式（test_factor_quality 同款）经 importlib 按路径
加载模块：``data`` 包名与容器/仓库其他路径有撞名风险，且 fingerprint.py 顶部
零 backend 依赖，独立加载最贴近真实运行（训练容器内它就是顶层模块）。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FINGERPRINT_PY = _ROOT / "docker" / "training" / "data" / "fingerprint.py"


def _load_fingerprint_module():
    spec = importlib.util.spec_from_file_location(
        "data_fingerprint_under_test", _FINGERPRINT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fp = _load_fingerprint_module()

# bdate_range("2026-01-05", periods=10)：01-05(周一) … 01-16(周五)
_DATES = pd.bdate_range("2026-01-05", periods=10)


def _segments() -> dict[str, pd.DataFrame]:
    """三段切分帧：test 末行无标签 → labels_max_date 必须落在 01-15 而非 01-16。"""

    def seg(start: int, end: int, label_through: int | None = None) -> pd.DataFrame:
        rows = [
            {
                "trade_date": _DATES[i],
                "symbol": f"S{i:02d}",
                "label": 0.1 if label_through is None or i <= label_through else None,
            }
            for i in range(start, end + 1)
        ]
        return pd.DataFrame(rows)

    return {
        "train": seg(0, 3),
        "valid": seg(5, 6),
        "test": seg(8, 9, label_through=8),
    }


def _probe(root: Path, *, max_date: str = "2026-01-30", schema_hash: str = "srcsha"):
    calls: list[tuple] = []

    def probe_fn(factor_source, quantdb_dir, market):
        calls.append((factor_source, quantdb_dir, market))
        return {
            "path": str(root),
            "min_date": "2020-01-02",
            "max_date": max_date,
            "schema_hash": schema_hash,
            "files": 2,
        }

    probe_fn.calls = calls
    return probe_fn


@pytest.mark.unit
def test_builds_full_fingerprint_with_source_probe(tmp_path):
    probe = _probe(tmp_path)
    cfg = {
        "data": {
            "factor_source": "l1_factors",
            "quantdb_dir": "/data/quantdb",
            "factor_catalog_version": "v2026.10",
            "factor_schema_hash": "cfgsha",
        }
    }
    out = fp.build_data_fingerprint(
        cfg=cfg, split_frames=_segments(), market="cn", source_probe=probe
    )

    # 探测调用口径：market 归一化大写、quantdb_dir 透传
    assert probe.calls == [("l1_factors", "/data/quantdb", "CN")]

    assert out["splits"]["train"] == {
        "start": "2026-01-05",
        "end": "2026-01-08",
        "rows": 4,
        "label_rows": 4,
    }
    assert out["splits"]["valid"]["rows"] == 2
    assert out["splits"]["test"]["rows"] == 2

    # 双口径截止日：价格面来自源探测，标签面来自实况（test 末行无标签）
    assert out["prices_max_date"] == "2026-01-30"
    assert out["prices_max_date_basis"] == "quantdb_source"
    assert out["labels_max_date"] == "2026-01-15"

    # cfg 优先于源侧 schema_hash（与 metadata.factor_schema_hash 同源）
    assert out["schema_hash"] == "cfgsha"
    assert out["catalog_version"] == "v2026.10"
    assert out["factor_source"] == "l1_factors"

    assert out["source"]["min_date"] == "2020-01-02"
    assert out["source"]["max_date"] == "2026-01-30"
    assert out["manifest"]["root"] == str(tmp_path)
    assert out["error"] == ""
    assert out["computed_at"]


@pytest.mark.unit
def test_schema_hash_falls_back_to_source_when_cfg_missing(tmp_path):
    out = fp.build_data_fingerprint(
        cfg={"data": {"factor_source": "l1_factors"}},
        split_frames=_segments(),
        source_probe=_probe(tmp_path, schema_hash="onlysrc"),
    )
    assert out["schema_hash"] == "onlysrc"
    assert out["catalog_version"] is None


@pytest.mark.unit
def test_probe_failure_degrades_to_split_frames_basis(tmp_path):
    def boom(factor_source, quantdb_dir, market):
        raise RuntimeError("quantdb down")

    out = fp.build_data_fingerprint(
        cfg={"data": {"factor_source": "l1_factors"}},
        split_frames=_segments(),
        source_probe=boom,
    )
    assert out["error"].startswith("source_probe_failed")
    assert out["source"] is None
    assert out["manifest"]["root"] == ""
    # 降级口径：帧实况的最大交易日（含无标签行）
    assert out["prices_max_date"] == "2026-01-16"
    assert out["prices_max_date_basis"] == "split_frames"


@pytest.mark.unit
def test_legacy_mode_without_factor_source(tmp_path):
    out = fp.build_data_fingerprint(
        cfg={"data": {}}, split_frames=_segments(), source_probe=_probe(tmp_path)
    )
    assert out["factor_source"] is None
    assert out["source"] is None
    assert out["error"] == ""
    assert out["prices_max_date_basis"] == "split_frames"
    assert out["prices_max_date"] == "2026-01-16"


@pytest.mark.unit
def test_manifest_counts_parquet_and_digest_is_state_sensitive(tmp_path):
    (tmp_path / "a.parquet").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.parquet").write_bytes(b"yy")
    (tmp_path / "ignored.txt").write_text("nope")

    out1 = fp.build_data_fingerprint(
        cfg={"data": {"factor_source": "l1_factors"}},
        split_frames=_segments(),
        source_probe=_probe(tmp_path),
    )
    manifest = out1["manifest"]
    assert manifest["files"] == 2  # 非 parquet 不算
    assert len(manifest["sha1"]) == 40
    assert manifest["max_mtime"]

    # 同状态复算 → 摘要稳定
    out2 = fp.build_data_fingerprint(
        cfg={"data": {"factor_source": "l1_factors"}},
        split_frames=_segments(),
        source_probe=_probe(tmp_path),
    )
    assert out2["manifest"]["sha1"] == manifest["sha1"]

    # 文件 mtime 改动 → 摘要变化（增删改都会被察觉）
    os.utime(tmp_path / "a.parquet", (1_700_000_000, 1_700_000_000))
    out3 = fp.build_data_fingerprint(
        cfg={"data": {"factor_source": "l1_factors"}},
        split_frames=_segments(),
        source_probe=_probe(tmp_path),
    )
    assert out3["manifest"]["sha1"] != manifest["sha1"]


@pytest.mark.unit
def test_never_raises_on_garbage_inputs():
    for cfg, frames in [
        (None, None),
        ({}, {"train": "not-a-frame"}),
        ({"data": "not-a-dict"}, {}),
        ({"data": None}, {"train": pd.DataFrame()}),
    ]:
        out = fp.build_data_fingerprint(cfg=cfg, split_frames=frames)
        assert isinstance(out, dict)
        assert "computed_at" in out


@pytest.mark.unit
def test_train_py_wires_fingerprint_at_both_metadata_sites():
    src = (_ROOT / "docker" / "training" / "train.py").read_text(encoding="utf-8")
    assert "from data.fingerprint import build_data_fingerprint" in src
    assert src.count("build_data_fingerprint(") == 2, (
        "train.py 两处 metadata 构造点都要落盘 data_fingerprint"
        "（多模型 + 单模型路径，G0 晋升闸门按落盘字段检查）"
    )
