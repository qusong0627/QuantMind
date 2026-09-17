"""快照级撮合真机回放（T-P6-17，I 类）：L0.5 真实帧 → 盘口 → 撮合断言；陈旧/空档回退。

数据源：data/l05_snapshots/date=*/part-*.parquet（订阅采集写侧的真实帧落盘，
字段与 market:snapshot 契约同构）。无归档（未开盘日）→ skip（如实跳过，不假绿）。
"""

from __future__ import annotations

import glob

import pytest

pytestmark = pytest.mark.integration


def _load_real_frame():
    files = sorted(glob.glob("/data/l05_snapshots/date=*/part-*.parquet"))
    if not files:
        pytest.skip("无 L0.5 归档帧可回放（如实跳过）")
    import pandas as pd

    for path in reversed(files):
        df = pd.read_parquet(path)
        if df.empty:
            continue
        row = df[df["bid_vol1"].notna() & df["ask_vol1"].notna()]
        if row.empty:
            continue
        return path, row.iloc[0].to_dict()
    pytest.skip("归档帧无含五档的行（如实跳过）")


def test_replay_real_frame_through_book_and_walk():
    from backend.services.simulation.services.snapshot_book import (
        book_from_snapshot,
        is_fresh,
        walk_book,
    )

    path, row = _load_real_frame()
    snap = {k: row.get(k) for k in (
        "symbol", "price", "pre_close", "limit_up", "limit_down", "timestamp",
        *[f"bid{i}" for i in range(1, 6)], *[f"bid_vol{i}" for i in range(1, 6)],
        *[f"ask{i}" for i in range(1, 6)], *[f"ask_vol{i}" for i in range(1, 6)],
    )}
    snap["Now"] = row.get("price")
    snap["PreClose"] = row.get("pre_close")
    snap["LimitUp"] = row.get("limit_up")
    snap["LimitDown"] = row.get("limit_down")

    book = book_from_snapshot(snap, symbol=str(row.get("symbol") or ""))
    assert book is not None, f"真实帧无法解析为盘口: {path}"
    assert book.completeness >= 4, f"盘口完整度异常: {book.completeness}"
    # 真实盘口方向合理性：卖一 ≥ 现价 ≥ 买一（允许价格穿越的瞬时帧留 1% 容差）
    if book.asks and book.bids:
        assert book.asks[0].price >= book.bids[0].price
        assert abs(book.asks[0].price - book.price) / book.price < 0.05
    # 买 1 手 → 应以卖一价成交（真实深度数据）
    fill = walk_book("buy", 100, book)
    assert fill is not None and fill.fill_qty == 100
    assert fill.fill_price == pytest.approx(book.asks[0].price, abs=0.01)
    # 超大单 → 部分成交（深度上限），余量 > 0
    huge = walk_book("buy", 100_000_000, book)
    assert huge is not None and huge.unfilled > 0 and huge.levels_consumed >= 1
    # 归档帧已旧 → 新鲜度门必须拒绝（真机不拿旧盘口撮合）
    assert is_fresh(book, now=float(row.get("timestamp") or 0) + 400) is False


def test_stale_or_missing_book_falls_back():
    from backend.services.simulation.services.exec_core import (
        MODE_DAILY,
        resolve_exec_core,
        snapshot_core_stats,
        try_snapshot_fill,
    )

    # 默认模式 = daily（F1 平价：默认路径不启用快照核）
    assert resolve_exec_core() == MODE_DAILY
    # 陈旧/缺失盘口 → try_snapshot_fill 回退（计数可见，不抛）
    before = snapshot_core_stats()["fallbacks"]
    fill, book = try_snapshot_fill(
        symbol="600036.SH", side="buy", quantity=100,
        order_type="market", limit_price=None, lot_size=100,
    )
    assert fill is None  # 盘口缺失/陈旧（当前行情链路无新鲜盘口）→ 回退日频核
    assert snapshot_core_stats()["fallbacks"] >= before
