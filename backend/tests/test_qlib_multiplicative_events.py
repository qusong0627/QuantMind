"""qlib 乘法复权因子的事件表目录解析。

背景：便携包（免 Docker）的 QuantDB 数据目录是 ``$STORAGE_ROOT/quantdb``，
机器上不存在 ``/data/quantdb``。事件表目录若写死绝对路径，``EventBook`` 会读到空，
``_multiplicative_factor`` 返回 None，CN 的 qlib ``$factor`` 静默退回加法口径
（``real_shares = adjusted_amount * factor`` 下股数逐日漂移）。
"""
from __future__ import annotations

import pytest

from backend.shared.qlib_multiplicative_factor import EventBook, resolve_events_dir


@pytest.mark.unit
def test_events_dir_derives_from_quantdb_data_dir(tmp_path, monkeypatch):
    """env 指向 QuantDB 数据目录时，事件表目录应派生自它而非硬编码 /data/quantdb。"""
    root = tmp_path / "quantdb"
    (root / "3_financial_data" / "dividend_factors").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.delenv("QUANTDB_DIVIDEND_DIR", raising=False)

    assert resolve_events_dir() == root / "3_financial_data" / "dividend_factors"


@pytest.mark.unit
def test_quantdb_dividend_dir_env_wins(tmp_path, monkeypatch):
    """QUANTDB_DIVIDEND_DIR 显式覆盖优先于 QuantDB 数据目录派生。"""
    root = tmp_path / "quantdb"
    (root / "3_financial_data" / "dividend_factors").mkdir(parents=True)
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.setenv("QUANTDB_DIVIDEND_DIR", str(explicit))

    assert resolve_events_dir() == explicit


@pytest.mark.unit
def test_event_book_defaults_to_resolved_dir(tmp_path, monkeypatch):
    """EventBook 无参构造走解析结果（调用时求值，非 import 时）。"""
    root = tmp_path / "quantdb"
    events = root / "3_financial_data" / "dividend_factors"
    events.mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    monkeypatch.delenv("QUANTDB_DIVIDEND_DIR", raising=False)

    assert EventBook().events_dir == events
