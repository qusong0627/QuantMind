"""回归与单测：用户级资金快照多市场合并 + 初始资金按市场求和（P0-05）+ 调度日历兜底。

1. account_key 解析不再跳过非 CN 市场账户（跨市场合并入用户级快照）
2. resolve_account_seed：显式 initial_cash > 未交易启发式 > CN settings > 未知
   （2026-09-15 诊断事故：CN+FUTURES 双账户用户快照 total_pnl 曾虚增 +100 万）
3. SimulationScheduler._is_trading_day：日历不可用时回退周判断
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from backend.services.simulation.scheduler import SimulationScheduler
from backend.services.simulation.services.fund_snapshot_service import (
    resolve_account_seed,
)
from backend.shared.simulation_account_keys import parse_account_key


def test_parse_account_key_accepts_all_markets():
    # CN（无后缀）与带市场后缀的键都应解析成功，交给 capture_all 合并
    assert parse_account_key("simulation:account:default:1") == ("default", "1", "CN")
    assert parse_account_key("simulation:account:default:1:CN") == ("default", "1", "CN")
    assert parse_account_key("simulation:account:default:1:HK") == ("default", "1", "HK")
    assert parse_account_key("simulation:account:default:1:US") == ("default", "1", "US")
    assert parse_account_key("simulation:account:default:1:FUTURES") == (
        "default",
        "1",
        "FUTURES",
    )
    # 非账户键返回 None
    assert parse_account_key("simulation:settings:default:1") is None
    assert parse_account_key("") is None


def _traded_account(cash="488965.58", total="1008379.58"):
    return {
        "cash": cash,
        "total_asset": total,
        "positions": {"300649.SZ": {"volume": 700, "market_value": 18186}},
    }


def test_seed_explicit_initial_cash_wins():
    acc = _traded_account()
    acc["initial_cash"] = 1000000
    assert resolve_account_seed(acc, "CN", Decimal("500000")) == Decimal("1000000")


def test_seed_untraded_account_uses_total_asset():
    # 未交易（无持仓、现金==总资产）：无盈亏，种子=当前总资产
    # —— 这是修"多市场种子虚增"的关键分支（期货账户从未成交）
    acc = {"cash": 1000000.0, "total_asset": 1000000.0, "positions": {}}
    assert resolve_account_seed(acc, "FUTURES", None) == Decimal("1000000.0")


def test_seed_traded_cn_falls_back_to_settings():
    acc = _traded_account()
    assert resolve_account_seed(acc, "CN", Decimal("1000000")) == Decimal("1000000")


def test_seed_traded_non_cn_unknown_returns_none():
    acc = _traded_account()
    assert resolve_account_seed(acc, "HK", Decimal("1000000")) is None


def test_seed_zero_volume_positions_count_as_untraded():
    acc = {
        "cash": 1000000.0,
        "total_asset": 1000000.0,
        "positions": {"600036.SH": {"volume": 0, "market_value": 0}},
    }
    assert resolve_account_seed(acc, "US", None) == Decimal("1000000.0")


def test_multi_market_seed_sum_fixes_phantom_profit():
    """复现 2026-09-15 事故：CN(已交易, settings 100 万) + FUTURES(未交易 100 万)。

    合并后 initial_capital 应为 200 万 → total_pnl ≈ +8379.58，
    而不是拿单份 settings 当初始导致的 +1008379.58 假收益。
    """
    cn = _traded_account()
    fut = {
        "cash": 1000000.0,
        "total_asset": 1000000.0,
        "positions": {},
        "market": "FUTURES",
    }
    seed_cn = resolve_account_seed(cn, "CN", Decimal("1000000"))
    seed_fut = resolve_account_seed(fut, "FUTURES", None)
    initial = seed_cn + seed_fut
    total_asset = Decimal("1008379.58") + Decimal("1000000.0")
    assert initial == Decimal("2000000")
    assert total_asset - initial == Decimal("8379.58")


def test_scheduler_trading_day_fallback():
    scheduler = SimulationScheduler.__new__(SimulationScheduler)  # 不走 __init__（避免连 Redis）
    sh = ZoneInfo("Asia/Shanghai")
    # 周六无论日历与否必非交易日（日历可用时同样返回 False）
    saturday = datetime(2026, 9, 12, 9, 35, tzinfo=sh)
    assert scheduler._is_trading_day(saturday) is False
    # 周一：日历可用时按 XSHG 判断（True）；不可用回退周判断（True）。均为 True。
    monday = datetime(2026, 9, 14, 9, 35, tzinfo=sh)
    assert scheduler._is_trading_day(monday) is True


def test_snapshots_daily_uses_normalized_user_id():
    """回归 #2（T-P0-04）：/snapshots/daily 必须用 require_sim_user_id 归一后的 ID。

    曾用原始 JWT sub（00000001）直读，而快照行由账户键解析而来（int 归一 → "1"），
    导致 admin 资金曲线读到另一个空账户的平线。
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "simulation"
        / "routers"
        / "simulation.py"
    ).read_text(encoding="utf-8")
    anchor = src.index("async def list_simulation_fund_snapshots")
    block = src[anchor : anchor + 900]
    assert "_require_user_id" in block, "snapshots/daily 未使用归一身份"
    assert "user_id=str(auth.user_id)" not in block, "snapshots/daily 仍在直读原始 sub"
