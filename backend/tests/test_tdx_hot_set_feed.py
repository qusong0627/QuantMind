"""TDX 桥热集行情轮询测试：五档映射（U）+ 桥真机写标准键（I）。

I 类对**真实桥（.13:8550）**取 2 只票快照 → 写真实远端行情服标准键 → 断言字段 →
**立即删除测试键**（不留痕；避免测试快照冒充实时数据）。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.unit
def test_map_snapshot_with_book_fields():
    from backend.services.live_trading.services.tdx_hot_set_feed import (
        map_snapshot_with_book,
    )

    raw = {
        "Now": "40.60", "Open": "40.90", "Max": "40.95", "Min": "40.45",
        "LastClose": "40.92", "Volume": "554798", "Amount": "225468.42",
        "Buyp": ["40.59", "40.58", "40.57", "0.00", "0.00"],
        "Buyv": ["40", "120", "300", "0", "0"],
        "Sellp": ["40.60", "40.61", "40.62", "0.00", "0.00"],
        "Sellv": ["69", "55", "88", "0", "0"],
    }
    snap = map_snapshot_with_book(raw)
    assert snap is not None
    assert snap["Now"] == pytest.approx(40.60) and snap["PreClose"] == pytest.approx(40.92)
    assert snap["bid1"] == pytest.approx(40.59) and snap["bid_vol1"] == 40
    assert snap["bid2"] == pytest.approx(40.58) and snap["bid_vol3"] == 300
    assert snap["ask1"] == pytest.approx(40.60) and snap["ask_vol1"] == 69
    assert snap["ask5"] == 0.0 and snap["ask_vol5"] == 0

    # 必填缺失 → None（沿用持仓馈送映射口径）
    assert map_snapshot_with_book({"Open": "1", "LastClose": "1"}) is None


@pytest.mark.integration
def test_hot_set_feed_writes_standard_keys_via_real_bridge():
    from backend.shared.stock_utils import StockCodeUtil
    from backend.services.live_trading.services.tdx_hot_set_feed import (
        map_snapshot_with_book,
    )
    from backend.services.live_trading.services.tdx_push_service import tdx_pusher
    from backend.services.live_trading.services.tdx_quote_feed import _write_snapshot

    async def _flow():
        from backend.shared.remote_quote_config import make_sync_client

        symbols = ["600036.SH", "000001.SZ"]
        written = []
        for sym in symbols:
            result = await tdx_pusher.tdx_call(
                "get_market_snapshot", {"stock_code": StockCodeUtil.to_suffix(sym)}
            )
            assert isinstance(result, dict) and float(result.get("Now") or 0) > 0, result
            snap = map_snapshot_with_book(result)
            assert snap is not None
            # 字段名对齐实证（桥侧改名会让五档静默归零，本断言专防该回潮）：
            # 桥原始 Buyp/Sellp/Buyv/Sellv 首档 = 映射 bid1/ask1/bid_vol1/ask_vol1
            buyp = [float(v) for v in (result.get("Buyp") or [])]
            buyv = [float(v) for v in (result.get("Buyv") or [])]
            sellp = [float(v) for v in (result.get("Sellp") or [])]
            sellv = [float(v) for v in (result.get("Sellv") or [])]
            if buyp and sellp:
                assert snap["bid1"] == pytest.approx(buyp[0])
                assert snap["bid_vol1"] == int(buyv[0])
                assert snap["ask1"] == pytest.approx(sellp[0])
                assert snap["ask_vol1"] == int(sellv[0])
            ok = await _write_snapshot(StockCodeUtil.to_prefix(sym) or sym, snap)
            assert ok, f"{sym} 写入标准键失败"
            written.append(sym)
        client = make_sync_client()
        try:
            for sym in written:
                code, mk = sym.split(".")
                key = f"market:snapshot:{mk.lower()}{code}"
                h = client.hgetall(key)
                assert h, f"{key} 未落键"
                assert h.get("source") == "tdx_bridge"
                assert float(h["Now"]) > 0 and "bid1" in h and "ask1" in h
                assert client.zcard(f"market:series:{mk}{code}") >= 1
        finally:
            # 立即清理（测试 EOD 快照不得冒充实时数据留存）
            for sym in written:
                code, mk = sym.split(".")
                client.delete(f"market:snapshot:{mk.lower()}{code}")
                client.delete(f"market:series:{mk}{code}")
            client.close()
        return written

    written = asyncio.run(_flow())
    assert len(written) == 2


def _sample_raw() -> dict:
    return {
        "Now": "40.60", "Open": "40.90", "Max": "40.95", "Min": "40.45",
        "LastClose": "40.92", "Volume": "554798", "Amount": "225468.42",
        "Buyp": ["40.59", "40.58", "40.57", "0.00", "0.00"],
        "Buyv": ["40", "120", "300", "0", "0"],
        "Sellp": ["40.60", "40.61", "40.62", "0.00", "0.00"],
        "Sellv": ["69", "55", "88", "0", "0"],
    }


@pytest.mark.unit
def test_snapshot_l05_record_fields():
    """桥快照 → L0.5 归档行：列名与订阅侧写侧契约一致（缺列如实 None 不假填）。"""
    from backend.services.live_trading.services.tdx_hot_set_feed import (
        map_snapshot_with_book,
        snapshot_l05_record,
    )

    snap = map_snapshot_with_book(_sample_raw())
    assert snap is not None
    rec = snapshot_l05_record("600036.SH", snap)
    assert rec["symbol"] == "600036.SH"          # 后缀式（l05 列口径）
    assert rec["ts"] == int(snap["timestamp"])   # epoch 秒
    assert rec["source"] == "tdx_bridge"
    assert len(rec["refresh_time"]) == 6 and rec["refresh_time"].isdigit()
    assert rec["price"] == pytest.approx(40.60) and rec["pre_close"] == pytest.approx(40.92)
    assert rec["open"] == pytest.approx(40.90) and rec["high"] == pytest.approx(40.95)
    assert rec["low"] == pytest.approx(40.45)
    assert rec["volume"] == 554798 and rec["amount"] == pytest.approx(225468.42)
    assert rec["bid1"] == pytest.approx(40.59) and rec["bid_vol1"] == 40
    assert rec["bid2"] == pytest.approx(40.58) and rec["ask_vol2"] == 55
    assert rec["ask5"] == 0.0 and rec["ask_vol5"] == 0
    # 桥 get_market_snapshot 不提供涨跌停/封单 → 如实 None
    assert rec["limit_up"] is None and rec["limit_down"] is None and rec["seal_amount"] is None


@pytest.mark.unit
def test_l05_archiver_roundtrip(tmp_path):
    """归档行 → l05_store 真实落盘 → read_day 读回（与订阅侧归档完全同构）。"""
    from datetime import datetime

    from backend.shared.l05_store import CST, SnapshotArchiver, read_day
    from backend.services.live_trading.services.tdx_hot_set_feed import (
        map_snapshot_with_book,
        snapshot_l05_record,
    )

    snap = map_snapshot_with_book(_sample_raw())
    assert snap is not None
    rec = snapshot_l05_record("600036.SH", snap)
    arch = SnapshotArchiver(
        base_dir=str(tmp_path), flush_rows=1, flush_seconds=0.01, tag="bridge"
    )
    arch.append(rec)
    arch.flush()
    assert arch.counters["rows"] == 1 and arch.counters["flush_errors"] == 0

    day = datetime.fromtimestamp(rec["ts"], tz=CST).date()
    df = read_day(day, base_dir=str(tmp_path))
    assert len(df) == 1
    row = df.iloc[0]
    assert row["symbol"] == "600036.SH"
    assert int(row["ts"]) == rec["ts"]
    assert float(row["price"]) == pytest.approx(40.60)
    assert float(row["bid1"]) == pytest.approx(40.59)
    assert int(row["bid_vol1"]) == 40
    assert str(row["source"]) == "tdx_bridge"
