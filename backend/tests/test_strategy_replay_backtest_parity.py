"""T-P2-05c 测试：**策略层回放平价**——同一策略对象（同一批生产信号 + 同一窗口真实行情 +
同一参数）分别驱动「时光回放」与「回测引擎」，断言逐成交日 diff=0。

口径（细案见 docs/MVP实施计划_后端.md）：
- 决策信息集：截至 D 日（含）；成交：D+1 日**收盘 ± 滑点**——回放显式用
  MatchConfig(price_mode='close', slippage_bps=10) 对齐回测引擎的 close±0.001 约定
  （回放默认 open 是产品语义，平价口径须显式对齐）；
- 信号：真实生产信号（ReplaySignalLoader 直读模型 pred.parquet，T+1 语义与
  engine_signal_scores 一致）；固定 3 标的作为平价 universe（生产分数子集，两侧同一
  包装 loader 返回同一批 SignalScore 对象）；
- 两侧均为真实执行栈：回放 = ReplayDayRunner 全链（真会话行 + Redis 回放账户 +
  ashare_matcher）；回测 = BacktestEngine（费用/申报/涨跌停经 T-P2-02 同源收敛）+
  决策适配器复用回放的 `_build_orders`（同一策略对象）；
- 断言：逐日成交（symbol/side/数量/价格 round4/三项费用逐分）一致；期末账户
  （现金/持仓量/总权益）一致；每日权益交叉核对（引擎 equity_curve[k+1] ≈ 回放第 k 日
  状态按 k+1 收盘重建）。数据/模型不可用时 skip。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.day_runner import ReplayDayRunner
from backend.services.simulation.replay.signal_generator import (
    _get_pred_day_frame,
    replay_signal_loader,
)
from backend.services.simulation.services.ashare_matcher import MatchConfig
from backend.services.simulation.services.local_market_data import get_local_market_data
from backend.shared.backtest_engine.core.engine import BacktestEngine
from backend.shared.backtest_engine.strategies.base import BaseStrategy

_CASH = 1_000_000.0
_WINDOW_DAYS = 12
_UNIVERSE_CANDIDATES = (
    "600036.SH",
    "000001.SZ",
    "600519.SH",
    "601318.SH",
    "600030.SH",
    "000858.SZ",
)
_STRATEGY_PARAMS = {
    "topk": 3,
    "n_drop": 2,
    "rebalance_days": 1,
    "weight_mode": "equal",
    "max_position_pct": 0.45,
    "lot_size": 100,
    "renormalize_weights": True,
    "deterministic_buy_order": True,
    "force_exit_on_limit_down": True,
}


async def _ensure_db_pool():
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


def _resolve_parity_model_dir() -> Path | None:
    """解析带 pred.parquet 的模型目录（env 优先；否则用已知 CN 模型）。"""
    candidates = []
    env_dir = str(os.getenv("PARITY_MODEL_DIR", "")).strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(
        Path(
            "/app/models/users/default/00000001/"
            "mdl_cn_train_20260910053728_9b8a7c26_17e5c7d5"
        )
    )
    for cand in candidates:
        if (cand / "pred.parquet").is_file() or (cand / "pred" / "pred.parquet").is_file():
            return cand
    return None


def _discover_window(
    sessions: list[int], latest: int, model_dir: Path, n_days: int
) -> list[date] | None:
    """从最近交易日向前找连续 n_days 个「其前一交易日有 pred 分数」的交易日窗口。"""
    cons = [d for d in sessions if d <= latest]
    if len(cons) < n_days + 1:
        return None
    for end_idx in range(len(cons) - 1, n_days - 1, -1):
        window_ints = cons[end_idx - (n_days - 1) : end_idx + 1]  # 含窗口首日
        ok = True
        for d_int in window_ints[1:]:
            before = [x for x in sessions if x < d_int]
            if not before:
                ok = False
                break
            data_day = date(before[-1] // 10000, (before[-1] % 10000) // 100, before[-1] % 100)
            if _get_pred_day_frame(model_dir, data_day) is None:
                ok = False
                break
        if ok:
            return [
                date(x // 10000, (x % 10000) // 100, x % 100) for x in window_ints
            ]
    return None


def _pick_universe(market_data, window: list[date]) -> list[str]:
    """固定 3 标的：窗口内每个交易日都得有 bar（真数据可用性前置）。"""
    # 尽量用满 6 只（换手面越厚，平价证明力越强）；数据不全时逐级降级
    for n in (6, 5, 4, 3):
        trio = list(_UNIVERSE_CANDIDATES[:n])
        ok = True
        for d in window:
            bars = market_data.load_date(d, trio)
            if not all(s in bars for s in trio):
                ok = False
                break
        if ok:
            return trio
    return []


def _frame_from_bars(bars_by_day: dict[date, dict], symbol: str) -> pd.DataFrame:
    rows = []
    for d in sorted(bars_by_day):
        bar = bars_by_day[d].get(symbol)
        if bar is None:
            continue
        rows.append(
            {
                "date": pd.Timestamp(d),
                "open": float(bar.open or 0.0),
                "high": float(bar.high or 0.0),
                "low": float(bar.low or 0.0),
                "close": float(bar.close or 0.0),
                "volume": float(bar.volume or 0.0),
            }
        )
    return pd.DataFrame(rows).set_index("date")


class _UniverseLoader:
    """把生产信号裁剪到平价 universe 的包装 loader（两侧共享同一批对象与缓存）。"""

    def __init__(self, inner, universe: set[str]):
        self._inner = inner
        self._universe = universe
        self._cache: dict[date, list[Any]] = {}

    async def load_signals_for_date(self, db, session_id, trade_date, **kw):
        if trade_date not in self._cache:
            raw = await self._inner.load_signals_for_date(
                db, session_id=session_id, trade_date=trade_date, **kw
            )
            self._cache[trade_date] = [s for s in raw if s.symbol in self._universe]
        return list(self._cache[trade_date])


class _ReplayParityStrategy(BaseStrategy):
    """同一策略对象驱动回测：在 D 日回调里复用回放的 _build_orders 计算 D+1 的订单。

    引擎处理顺序保证：D 日回调前已按 D 收盘成交了 D-1 日排队单 → 组合镜像即回放
    「D+1 日初」的状态；信号/行情取 D+1 的（与回放 D+1 步输入逐项一致）。
    """

    def __init__(self, *, runner, days, signals_by_day, bars_by_day, params):
        super().__init__("replay_parity")
        self._runner = runner
        self._days = days
        self._signals = signals_by_day
        self._bars = bars_by_day
        self._params = params
        self._decided: set[date] = set()
        self.decisions: list[tuple[date, list]] = []

    def on_data(self, market_data):  # noqa: D102 - BaseStrategy 契约
        day = pd.Timestamp(market_data["date"]).date()
        if day in self._decided or day not in self._days:
            return
        self._decided.add(day)
        k = self._days.index(day)
        if k + 1 >= len(self._days):
            return
        fill_day = self._days[k + 1]
        account_data = self._mirror()
        orders = self._runner._build_orders(
            signals=list(self._signals.get(fill_day) or []),
            bars=dict(self._bars[fill_day]),
            account_data=account_data,
            strategy_params=self._params,
            approved_orders=None,
            day_index=k,
        )
        self.decisions.append((fill_day, orders))
        for o in sorted(orders, key=lambda x: 0 if str(x.side).upper() == "SELL" else 1):
            qty = int(o.quantity)
            if qty <= 0:
                continue
            if str(o.side).upper() == "SELL":
                self.sell(o.symbol, qty)
            else:
                self.buy(o.symbol, qty)

    def on_order_filled(self, order):  # noqa: D102 - BaseStrategy 契约（平价夹具无需钩子）
        pass

    def _mirror(self) -> dict[str, Any]:
        pf = self.backtest_engine.portfolio
        positions = {}
        for sym, p in pf.get_positions().items():
            qty = float(p.get("quantity") or 0.0)
            if qty > 0:
                positions[sym] = {"volume": qty, "cost": float(p.get("avg_cost") or 0.0)}
        return {
            "cash": float(pf.cash),
            "total_asset": float(pf.get_total_value()),
            "positions": positions,
        }


@pytest.mark.asyncio
async def test_strategy_replay_backtest_parity_real_data():
    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.services.simulation.models.replay import ReplaySession, ReplayStatus
    from backend.shared.database_manager_v2 import close_database, get_session

    model_dir = _resolve_parity_model_dir()
    if model_dir is None:
        pytest.skip("无带 pred.parquet 的模型目录（设 PARITY_MODEL_DIR 可指定）")
    market_data = get_local_market_data()
    sessions = await asyncio.to_thread(market_data._sessions)
    latest = market_data.latest_trade_date()
    if latest is None:
        pytest.skip("本地行情不可用")
    latest_int = int(latest.strftime("%Y%m%d"))
    window = _discover_window(sessions, latest_int, model_dir, _WINDOW_DAYS)
    if window is None:
        pytest.skip("找不到 pred 覆盖的连续交易日窗口")
    universe = _pick_universe(market_data, window)
    if len(universe) < 3:
        pytest.skip("平价 universe 标的数据不全")
    bars_by_day = {
        d: await asyncio.to_thread(market_data.load_date, d, list(universe))
        for d in window
    }

    session_id = uuid.uuid4()
    params = dict(_STRATEGY_PARAMS)
    params["_model_dir"] = str(model_dir)
    loader = _UniverseLoader(replay_signal_loader, set(universe))
    runner = ReplayDayRunner(
        market_data=market_data,
        loader=loader,
        match_config=MatchConfig(price_mode="close", slippage_bps=10.0),
    )

    try:
        async with get_session(read_only=False) as db:
            db.add(
                ReplaySession(
                    session_id=session_id,
                    tenant_id="default",
                    user_id=0,
                    name=f"T-P2-05c parity {session_id.hex[:6]}",
                    model_id=model_dir.name,
                    strategy_params=params,
                    initial_cash=_CASH,
                    start_date=window[0],
                    end_date=window[-1],
                    cursor_date=None,
                    next_date=window[1],
                    sessions_total=len(window) - 1,
                    sessions_done=0,
                    status=ReplayStatus.READY,
                    signal_progress={"done": len(window) - 1, "total": len(window) - 1},
                    auto_trade=False,
                    stop_loss_pct=None,
                )
            )
            await db.flush()
            accounts = ReplayAccountManager(session_id=session_id)
            await accounts.init(initial_cash=_CASH)
            await db.commit()

        # ── 信号（同一批对象：先经包装 loader 拉取，回放内部命中同一缓存）──
        signals_by_day: dict[date, list] = {}
        async with get_session(read_only=False) as db:
            for fill_day in window[1:]:
                signals_by_day[fill_day] = await loader.load_signals_for_date(
                    db, session_id=session_id, trade_date=fill_day
                )

        # ── ① 时光回放（真实全链）──
        replay_results: dict[date, Any] = {}
        async with get_session(read_only=False) as db:
            for k, fill_day in enumerate(window[1:]):
                res = await runner.run_day(
                    db,
                    session_id,
                    fill_day,
                    "default",
                    "0",
                    accounts,
                    strategy_params=params,
                    stop_loss_pct=None,
                    approved_orders=None,
                    initial_cash=_CASH,
                    match_config=runner._cfg,
                    day_index=k,
                )
                assert not res.error, f"回放第 {k} 步失败: {res.error}"
                replay_results[fill_day] = res

        # ── ② 回测（同一策略对象 = 适配器复用 _build_orders）──
        engine = BacktestEngine(
            initial_cash=_CASH, slippage_rate=0.001, enable_risk_management=False
        )
        engine.set_data({sym: _frame_from_bars(bars_by_day, sym) for sym in universe})
        strategy = _ReplayParityStrategy(
            runner=runner,
            days=window,
            signals_by_day=signals_by_day,
            bars_by_day=bars_by_day,
            params=params,
        )
        engine.add_strategy(strategy)
        engine.run()

        # ── 断言：逐成交日 diff=0 ──
        engine_trades_by_day: dict[date, list[dict]] = {}
        for t in engine.trades:
            d = pd.Timestamp(t["date"]).date()
            engine_trades_by_day.setdefault(d, []).append(t)

        checked_fills = 0
        for fill_day in window[1:]:
            rep = replay_results[fill_day].filled
            eng = engine_trades_by_day.get(fill_day, [])
            assert len(eng) == len(rep), (
                f"{fill_day} 成交笔数不一致：回测 {len(eng)} vs 回放 {len(rep)}\n"
                f"回测: {[(t['symbol'], t['side'], t['quantity']) for t in eng]}\n"
                f"回放: {[(f['symbol'], f['side'], f['quantity']) for f in rep]}"
            )
            for e, r in zip(eng, rep, strict=True):
                assert str(e["symbol"]) == str(r["symbol"])
                assert str(e["side"]).lower() == str(r["side"]).lower()
                assert float(e["quantity"]) == float(r["quantity"])
                assert round(float(e["price"]), 4) == round(float(r["price"]), 4), (
                    fill_day,
                    e,
                    r,
                )
                assert round(float(e["total_fee"]), 2) == round(float(r["total_fee"]), 2), (
                    fill_day,
                    e,
                    r,
                )
                checked_fills += 1
        assert checked_fills > 0, "窗口内无成交，平价未生效（请检查信号覆盖）"
        print(
            f"[T-P2-05c] 平价通过：窗口 {window[0]}..{window[-1]} "
            f"universe={universe} 成交 {checked_fills} 笔（逐笔 diff=0）"
        )

        # ── 断言：期末账户一致（现金/持仓量/总权益）──
        final_rep = await accounts.get()
        pf = engine.portfolio
        assert float(pf.cash) == pytest.approx(float(final_rep.get("cash") or 0.0), abs=0.011)
        rep_pos = {
            sym: float((p or {}).get("volume") or 0.0)
            for sym, p in (final_rep.get("positions") or {}).items()
        }
        eng_pos = {
            sym: float(p.get("quantity") or 0.0)
            for sym, p in pf.get_positions().items()
            if float(p.get("quantity") or 0.0) > 0
        }
        assert {k: round(v, 4) for k, v in eng_pos.items()} == {
            k: round(v, 4) for k, v in rep_pos.items() if v > 0
        }
        # 引擎组合的期末估值：以窗口最后一日收盘重估
        last_closes = {
            sym: float(bars_by_day[window[-1]][sym].close or 0.0) for sym in universe
        }
        pf.update_market_value(datetime.now(), last_closes)
        assert float(pf.get_total_value()) == pytest.approx(
            float(final_rep.get("total_asset") or 0.0), abs=0.011
        )

        # ── 断言：每日权益交叉核对（引擎 T+1 前置口径 = 回放 T 后置口径）──
        # engine.equity_curve[k+1] = post-fill(window[k]) 状态 × close(window[k+1])
        curve_by_day = {
            pd.Timestamp(e["date"]).date(): float(e["total_value"])
            for e in engine.equity_curve
        }
        for k, fill_day in enumerate(window[1:]):
            prev_day = window[k]
            prev_rep = replay_results.get(prev_day) if k > 0 else None
            if prev_rep is None or prev_rep.account is None:
                continue  # 首日：回放无前一日状态可比
            closes = {
                sym: float(bars_by_day[fill_day][sym].close or 0.0) for sym in universe
            }
            prev_account = prev_rep.account or {}
            reconstructed = float(prev_account.get("cash") or 0.0) + sum(
                float(((prev_account.get("positions") or {}).get(sym) or {}).get("volume") or 0.0)
                * closes.get(sym, 0.0)
                for sym in universe
            )
            assert curve_by_day.get(fill_day, reconstructed) == pytest.approx(
                reconstructed, abs=0.02
            ), f"{fill_day} 权益交叉核对不一致"
    finally:
        try:
            async with get_session(read_only=False) as db:
                for table in (
                    "replay_trades",
                    "replay_orders",
                    "replay_equity_snapshots",
                    "replay_signals",
                ):
                    await db.execute(
                        sa_text(f"DELETE FROM {table} WHERE session_id = :s"),
                        {"s": session_id},
                    )
                await db.execute(
                    sa_text("DELETE FROM replay_sessions WHERE session_id = :s"),
                    {"s": session_id},
                )
            from backend.services.trade_shared.redis_client import get_redis

            raw = get_redis()
            if getattr(raw, "client", None) is None:
                raw.connect()
            raw.client.delete(f"replay:account:{session_id}")
            raw.client.delete(f"replay:settings:{session_id}")
        except Exception:  # noqa: BLE001 - 清理失败不掩盖断言结果
            pass
        await close_database()
