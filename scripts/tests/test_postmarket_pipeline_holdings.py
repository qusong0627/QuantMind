"""盘后链持仓源的单测（**宿主侧**运行）——审计 H1「复盘读僵尸账本」的回归闸。

跑法（``scripts/`` 不在容器挂载里，容器里跑不了）::

    python3 -m pytest scripts/tests/ -q

旧实现读 BayMax-Trader 的 live_ledger.json（2026-09-29 起停更），复盘却每晚照它编
持仓段；现在源 = PG ``real_account_snapshots``（决策轮同源）且带新鲜度门。盯三件事：

* 快照停更/缺失/读失败 → **空表 + 显式注记**（绝不按旧名单编复盘）；
* 新鲜快照 → 决策轮同源的并集（merge 口径），输出后缀式代码表给 QuantDB 用；
* 时间换算走北京墙钟（``snapshot_at`` 是 naive **UTC**）——午夜边界的快照不许误判停更。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根（backend.*）

import postmarket_pipeline as pp  # noqa: E402
from backend.shared import real_positions as rp  # noqa: E402
from backend.shared.real_positions import merge_real_sources  # noqa: E402

TRADE_DATE = "20261009"  # 复盘日（北京口径）


def _pos(symbol: str, volume: float = 100) -> dict:
    return {"symbol": symbol, "volume": volume}


class TestHoldingsForReview:
    def test_stale_snapshot_annotates_and_empties(self):
        """验收①：陈旧 → 显式标注「持仓源 X 日停更」并空段，旧名单一个字都不出现。"""
        positions, meta = merge_real_sources(
            [
                ("qmt_exec", datetime(2026, 9, 29, 6, 0), {"positions": [_pos("603213.SH")]}),
                ("tdx_bridge", datetime(2026, 9, 29, 5, 30), {"positions": [_pos("002074.SZ")]}),
            ]
        )

        codes, note = pp.holdings_for_review(positions, meta, TRADE_DATE)

        assert codes == []
        assert "持仓源 2026-09-29 停更" in note
        assert "2026-10-09" in note  # 与复盘日的对照写进注记
        assert "绝不按旧名单编复盘" in note
        assert "603213" not in note and "002074" not in note  # 停更名单不许被枚举出来

    def test_missing_snapshot_source_empties_with_hint(self):
        """验收②：新源无任何记录 → 空段 + 提示。"""
        positions, meta = merge_real_sources([])

        codes, note = pp.holdings_for_review(positions, meta, TRADE_DATE)

        assert codes == []
        assert "缺失" in note and "留空" in note

    def test_fresh_snapshot_keeps_the_union_as_suffix_codes(self):
        positions, meta = merge_real_sources(
            [
                (
                    "qmt_exec",  # 实测是前缀式（SH600036/BJ920950）
                    datetime(2026, 10, 9, 6, 0),
                    {"positions": [_pos("SH600036"), _pos("BJ920950")]},
                ),
                (
                    "tdx_bridge",  # 实测是后缀式（600036.SH）；同票两源要并成一只
                    datetime(2026, 10, 9, 6, 5),
                    {"positions": [_pos("600036.SH", 900), _pos("002074.SZ")]},
                ),
            ]
        )

        codes, note = pp.holdings_for_review(positions, meta, TRADE_DATE)

        assert codes == ["002074.SZ", "600036.SH", "920950.BJ"]  # QuantDB 要后缀式
        assert "qmt_exec/tdx_bridge 并集" in note and "3 只" in note
        assert "2026-10-09 14:05" in note  # 最新一条 06:05 UTC → 北京 14:05，写清快照时刻

    def test_after_midnight_beijing_snapshot_still_covers_the_day(self):
        """UTC 日期停在 D-1、北京已是复盘日 D 凌晨的快照 = 新鲜（换算守卫）。"""
        positions, meta = merge_real_sources(
            [("tdx_bridge", datetime(2026, 10, 8, 17, 0), {"positions": [_pos("600036.SH")]})]
            # 10-08 17:00 UTC = 10-09 01:00 北京
        )

        codes, note = pp.holdings_for_review(positions, meta, TRADE_DATE)

        assert codes == ["600036.SH"]
        assert "停更" not in note

    def test_relatively_stale_source_is_dropped_and_named(self):
        """相对停更（60 分钟口径）被 merge 剔除的源必须点名，不静默蒸发一个账户。"""
        positions, meta = merge_real_sources(
            [
                ("qmt_exec", datetime(2026, 10, 9, 6, 0), {"positions": [_pos("600036.SH")]}),
                ("tdx_bridge", datetime(2026, 9, 28, 6, 0), {"positions": [_pos("002074.SZ")]}),
            ]
        )

        codes, note = pp.holdings_for_review(positions, meta, TRADE_DATE)

        assert codes == ["600036.SH"]
        assert "tdx_bridge 相对停更未并入" in note
        assert "002074" not in note


class TestLoadHoldings:
    def test_read_failure_is_annotated_not_fatal(self):
        def _boom():
            raise RuntimeError("connection refused")

        codes, note = pp.load_holdings(TRADE_DATE, read_positions=_boom)

        assert codes == []
        assert "读取失败" in note and "connection refused" in note and "留空" in note

    def test_uses_the_injected_reader(self):
        calls: list[int] = []

        def _read():
            calls.append(1)
            return merge_real_sources(
                [("qmt_exec", datetime(2026, 10, 9, 6, 0), {"positions": [_pos("600036.SH")]})]
            )

        codes, note = pp.load_holdings(TRADE_DATE, read_positions=_read)

        assert calls == [1]
        assert codes == ["600036.SH"]
        assert "并集" in note

    def test_default_reader_uses_the_decision_round_identity(self, monkeypatch):
        """默认读取器必须落在决策轮的账户坐标上（default / 10000001）——同源纪律。"""
        seen: dict = {}

        def _fake_sync(tenant_id, user_id, *, connect, active_source=None):
            seen.update(tenant=tenant_id, user=user_id)
            return {}, {"sources": {}, "snapshot_at": None}

        monkeypatch.setattr(rp, "load_real_positions_sync", _fake_sync)
        monkeypatch.delenv("QM_DECISION_ACCOUNT_USER_ID", raising=False)

        codes, note = pp.load_holdings(TRADE_DATE)

        assert seen == {"tenant": "default", "user": "10000001"}
        assert codes == [] and "缺失" in note  # 空快照 → 仍走空段+提示
