"""QuantDB 就绪判据的回归测试。

锁的是 2026-09-20 改动的判据边界：

- **缺列 = 硬失败**（模型要用的列按名取不到，脚本必然崩）
- **整库列名漂移 = 放行 + 标记**（列都在，只是库里多了别的列）

曾经两者都走硬失败，代价是一个模型只因因子库新增了 9 列 OHLCV 基础列
就被永久锁死——注册表写着 ready，点下去必然失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.services.engine.inference import script_runner


class _Status:
    def __init__(self, columns: list[str], schema_hash: str = "livehash", ready: bool = True) -> None:
        self.columns = columns
        self.schema_hash = schema_hash
        self.ready = ready
        self.reason = None
        self.missing_required: list[str] = []
        self.min_date = "2016-01-04"
        self.max_date = "2026-09-18"


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """造一个只依赖 metadata 的 runner，把数据目录解析钉在临时目录。"""
    (tmp_path / "inference.py").write_text("")
    monkeypatch.setattr(
        script_runner, "_resolve_market_factor_data_dir", lambda _meta: str(tmp_path)
    )
    monkeypatch.setattr(script_runner, "_resolve_model_market", lambda _meta: "CN")
    return script_runner.InferenceScriptRunner(
        primary_model_dir=str(tmp_path), primary_data_dir=str(tmp_path), primary_model_id="mdl"
    )


def _arm(runner: Any, monkeypatch: pytest.MonkeyPatch, *, meta: dict, status: _Status) -> None:
    monkeypatch.setattr(runner, "_read_primary_metadata", lambda: meta)
    monkeypatch.setattr(script_runner, "_cached_describe", lambda _reader, _src: status)


_BASE_META = {"factor_source": "l1_factors", "factor_field_sources": {"f1": "vol_20"}}


def test_missing_mapped_field_fails_hard(runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(runner, monkeypatch, meta=_BASE_META, status=_Status(columns=["close", "amount"]))

    result = runner._query_quantdb_readiness(trade_date="")

    assert result["ready"] is False
    assert "vol_20" in result["detail"], "缺列必须指名道姓，否则无从修"


def test_hash_drift_with_all_columns_present_passes_with_marker(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = {**_BASE_META, "factor_schema_hash": "recordedhash"}
    _arm(runner, monkeypatch, meta=meta, status=_Status(columns=["close", "amount", "vol_20"]))

    result = runner._query_quantdb_readiness(trade_date="")

    assert result["ready"] is True
    assert "schema_drift" in result, "漂移必须可见——放行不等于无事发生"
    assert result["schema_drift"]["columns"] == 3


def test_matching_hash_reports_no_drift(runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    meta = {**_BASE_META, "factor_schema_hash": "livehash"}
    _arm(runner, monkeypatch, meta=meta, status=_Status(columns=["close", "vol_20"]))

    result = runner._query_quantdb_readiness(trade_date="")

    assert result["ready"] is True
    assert "schema_drift" not in result


def test_out_of_range_trade_date_still_blocks(runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """时效闸门不受本次改动影响：请求日期晚于数据覆盖范围仍须硬失败。"""
    meta = {**_BASE_META, "factor_schema_hash": "livehash"}
    _arm(runner, monkeypatch, meta=meta, status=_Status(columns=["close", "vol_20"]))

    result = runner._query_quantdb_readiness(trade_date="2026-09-25")

    assert result["ready"] is False
    assert result["latest_available_date"] == "2026-09-18"
