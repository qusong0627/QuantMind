"""tdx_signal_push_service 的 QuantDB 股票名表路径解析回归测试。

背景：便携包数据目录是 ``$STORAGE_ROOT/quantdb``，机器上没有 ``/data/quantdb``。
修复前 ``_QUANTDB_NAME_DIR`` 是模块级硬编码常量，兜底名表永远找不到。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.live_trading.services import tdx_signal_push_service as tdx


@pytest.mark.unit
def test_name_table_resolves_from_env_dir(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    detail = root / "2_base_sector" / "instrument_detail"
    detail.mkdir(parents=True)
    table = detail / "instrument_list.parquet"
    table.write_bytes(b"")
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    assert tdx._quantdb_name_table() == str(table)


@pytest.mark.unit
def test_name_table_none_when_absent(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    (root / "placeholder").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    assert tdx._quantdb_name_table() is None


@pytest.mark.unit
def test_no_module_level_hardcoded_dir():
    """防止回退成 import 时求值的硬编码常量。"""
    assert not isinstance(getattr(tdx, "_QUANTDB_NAME_DIR", None), str)
    assert isinstance(tdx._quantdb_name_dir(), Path)
