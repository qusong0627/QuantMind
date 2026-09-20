"""L2 CatBoost T+5 策略全年严格回测（最严厉模拟 A股）。

模型: mdl_cn_train_20260819100559_9163cb84_ac5c5b2e (L2 CatBoost T+5 2023-2025)
周期: 2026-01-06 ~ 2026-08-19（151 交易日）

策略规则（最严格）:
1. 每日取模型分数最高 Top20（仅正分，剔除 ST/*ST）为目标持仓
2. 每只等权金额（资金用满，至少 60%，目标 90%）
3. 滑点：买入=开盘×(1+SLIPPAGE)，卖出=开盘×(1-SLIPPAGE)，SLIPPAGE=0.2%
4. T+1：当日买入次日才可卖出
5. 涨跌停约束（compute_limits 全板块：主板10%/创业板科创20%/北交所30%/ST5%）：
   - 一字涨停：开盘封板买不进→挂单等开板
   - 一字跌停：开盘封板卖不出→挂单等开板
   - 停牌/无行情：不成交，等待
6. 换股：跌出 Top20 或分数为负→卖出；新入选补足 20 只
7. 交易成本：佣金万三 + 印花税 0.1%（卖）

输出：月/周/日收益、top20 盈亏股、完整交易明细。
"""
import sys
import math
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODEL_ID = "mdl_cn_train_20260819100559_9163cb84_ac5c5b2e"
TOP_N = 20
START_DATE = date(2026, 1, 6)
END_DATE = date(2026, 8, 19)
INIT_CASH = 500000.0
COMMISSION = 0.0003
STAMP_TAX = 0.001
SLIPPAGE = 0.002        # 0.2% 滑点
MIN_USAGE = 0.6
TARGET_USAGE = 0.9

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.services.trade.simulation.services.local_market_data import compute_limits
from sqlalchemy import text

logger = None  # 不需要


def load_signals() -> dict[str, list[tuple[str, float]]]:
    import asyncio

    async def _load():
        async with get_session(read_only=True) as s:
            res = await s.execute(text("""
                SELECT trade_date, symbol, fusion_score
                FROM engine_signal_scores
                WHERE run_id IN (SELECT run_id FROM qm_model_inference_runs
                                 WHERE model_id=:mid AND status='completed')
                  AND trade_date >= :start AND trade_date <= :end
                ORDER BY trade_date, fusion_score DESC
            """), {"mid": MODEL_ID, "start": START_DATE, "end": END_DATE})
            rows = res.fetchall()
        df = pd.DataFrame(rows, columns=["trade_date", "symbol", "score"])
        df["symbol"] = df["symbol"].apply(lambda x: StockCodeUtil.to_suffix(str(x).strip().upper()))
        out = {}
        for d, grp in df.groupby("trade_date"):
            grp = grp.reset_index(drop=True).sort_values("score", ascending=False)
            out[str(d)] = list(zip(grp["symbol"], grp["score"]))
        return out
    return asyncio.run(_load())


def load_st_symbols() -> set[str]:
    import os
    import psycopg2
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )
    out = set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM stocks WHERE name LIKE '%ST%' OR name LIKE '%*ST%'")
            for (sym,) in cur.fetchall():
                try:
                    out.add(StockCodeUtil.to_suffix(str(sym).strip().upper()))
                except Exception:
                    continue
    finally:
        conn.close()
    return out


def load_klines(symbols: set[str]) -> dict[str, pd.DataFrame]:
    import pyarrow.parquet as pq
    import os
    data_root = Path(os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
    daily_dir = data_root / "1_kline_data" / "daily_unadjusted"
    suffix_list = sorted(s for s in symbols)
    if not suffix_list:
        return {}
    partitions = []
    for p in sorted(daily_dir.glob("dt=2026*")):
        partitions.append(p / "data.parquet")
    filters = [("symbol", "in", suffix_list)]
    all_dfs = []
    for f in partitions:
        try:
            t = pq.read_table(f, columns=["symbol", "time", "open", "high", "low", "close"], filters=filters)
            if t.num_rows:
                all_dfs.append(t.to_pandas())
        except Exception:
            continue
    if not all_dfs:
        return {}
    full = pd.concat(all_dfs, ignore_index=True)
    full = full.rename(columns={"time": "trade_date"})
    full["trade_date"] = full["trade_date"].astype(str).str[:10]
    klines = {}
    for suffix, grp in full.groupby("symbol"):
        klines[suffix] = grp.sort_values("trade_date")[["trade_date", "open", "high", "low", "close"]].reset_index(drop=True)
    return klines


def run_backtest(signals, klines, st_symbols):
    commission = COMMISSION
    stamp_tax = STAMP_TAX
    dates = sorted(signals.keys())
    date_set = set(dates)
    next_day = {dates[i]: dates[i + 1] for i in range(len(dates) - 1)}

    price = {}
    close_map = {}
    for suffix, df in klines.items():
        cm = {}
        for _, row in df.iterrows():
            d = str(row["trade_date"])[:10]
            price[(suffix, d)] = {"open": float(row["open"]), "high": float(row["high"]),
                                  "low": float(row["low"]), "close": float(row["close"])}
            cm[d] = float(row["close"])
        close_map[suffix] = cm

    def _limits(suffix, day):
        m = close_map.get(suffix)
        if not m:
            return None
        day_dates = sorted(d for d in m if d < day)
        if not day_dates:
            return None
        pre = m[day_dates[-1]]
        code = suffix.split(".")[0]
        try:
            # is_st 必须与**选股**口径一致（选股用 `sym not in st_symbols` 剔 ST）。
            # 写死 False 时 ST 主板 5% 板（<2026-07-06）的封板日被当成可成交 ——
            # 回测买进了买不进的票，方向是乐观的（本文件头部却声称 ST 5% 已处理）。
            # 静态集合有前视偏差，但它本来就已在决定选股；用它只是消除自相矛盾，
            # 残余误差方向转为保守（少交易），不是美化。
            lu, ld = compute_limits(
                code,
                pre,
                is_st=suffix in st_symbols,
                trade_date=date.fromisoformat(day),
            )
        except Exception:
            return None
        return float(lu), float(ld)

    def _bar_state(suffix, day):
        """返回 (limit_up, limit_down, 是否一字涨停不可买, 是否一字跌停不可卖)"""
        lim = _limits(suffix, day)
        px = price.get((suffix, day))
        if lim is None or px is None:
            return None, None, True, True  # 无行情=停牌，均不可交易
        lu, ld = lim
        o, h, l = px["open"], px["high"], px["low"]
        cannot_buy = o >= lu - 0.001 and h <= lu + 0.001
        cannot_sell = o <= ld + 0.001 and l >= ld - 0.001
        return lu, ld, cannot_buy, cannot_sell

    holdings = {}      # suffix -> {shares, cost, buy_date, buy_px}
    pending_buy = {}   # suffix -> True
    pending_sell = {}  # suffix -> {shares, reason}
    trades = []
    daily = {}
    cash = INIT_CASH

    for i, d in enumerate(dates):
        day_items = signals.get(d, [])
        score_map = {sym: sc for sym, sc in day_items}
        tgt_day = next_day.get(d)

        # 当前权益（用当日收盘估价）
        equity = cash + sum(
            (price.get((s, d), {}).get("close") or 0) * h["shares"]
            for s, h in holdings.items()
        )

        if not tgt_day or tgt_day not in date_set:
            total_val = sum(
                (price.get((s, d), {}).get("close") or h["buy_px"]) * h["shares"]
                for s, h in holdings.items()
            ) + cash
            daily[d] = {"holdings": list(holdings.keys()), "value": total_val, "cash": cash, "n": len(holdings)}
            continue

        # 目标持仓：剔除 ST，分数>0 top20
        target_list = [sym for sym, sc in sorted(day_items, key=lambda x: -x[1])
                       if sc > 0 and sym not in st_symbols][:TOP_N]
        target_set = set(target_list)

        # ── 卖出（T+1：勿当日前已持有；优先卖跌出/负分）──
        to_sell = [s for s in holdings if score_map.get(s, 0) <= 0 or s not in target_set]
        pending_sell_after = {}
        for suffix in to_sell:
            h = holdings.get(suffix)
            if not h:
                continue
            if h["buy_date"] == tgt_day:
                # T+1：当日买入不可卖
                continue
            _, _, _, cannot_sell = _bar_state(suffix, tgt_day)
            px = price.get((suffix, tgt_day))
            if not px or cannot_sell:
                pending_sell[suffix] = {"shares": h["shares"], "reason": "limit_down"}
                continue
            sell_px = px["open"] * (1 - SLIPPAGE)
            fee = sell_px * h["shares"] * (commission + stamp_tax)
            cash += h["shares"] * sell_px - fee
            trades.append({"day": tgt_day, "symbol": suffix, "action": "SELL",
                           "px": round(sell_px, 3), "shares": h["shares"],
                           "cost": round(h["buy_px"] * h["shares"], 2),
                           "pnl": round(sell_px * h["shares"] - h["buy_px"] * h["shares"] - fee, 2),
                           "reason": "out_of_top" if score_map.get(suffix, 0) <= 0 else "replaced",
                           "score": round(score_map.get(suffix, 0), 4)})
            del holdings[suffix]

        # 处理挂单卖出（等跌停开板）
        for suffix in list(pending_sell.keys()):
            h = holdings.get(suffix)
            if not h:
                pending_sell.pop(suffix, None)
                continue
            _, _, _, cannot_sell = _bar_state(suffix, tgt_day)
            px = price.get((suffix, tgt_day))
            if not px or cannot_sell:
                continue
            pending_sell.pop(suffix)
            sell_px = px["open"] * (1 - SLIPPAGE)
            fee = sell_px * h["shares"] * (commission + stamp_tax)
            cash += h["shares"] * sell_px - fee
            trades.append({"day": tgt_day, "symbol": suffix, "action": "SELL",
                           "px": round(sell_px, 3), "shares": h["shares"],
                           "cost": round(h["buy_px"] * h["shares"], 2),
                           "pnl": round(sell_px * h["shares"] - h["buy_px"] * h["shares"] - fee, 2),
                           "reason": "pending_sell_open", "score": round(score_map.get(suffix, 0), 4)})
            del holdings[suffix]

        # ── 买入（补足到 20，等权金额）──
        pos_val = max(equity * TARGET_USAGE / TOP_N, equity * MIN_USAGE / TOP_N)
        new_targets = [s for s in target_list if s not in holdings and s not in pending_buy]

        # 先处理挂单买入（涨停等开板）
        for suffix in list(pending_buy.keys()):
            if suffix not in target_set or suffix in holdings:
                pending_buy.pop(suffix, None)
                continue
            _, _, cannot_buy, _ = _bar_state(suffix, tgt_day)
            px = price.get((suffix, tgt_day))
            if not px or cannot_buy:
                continue
            pending_buy.pop(suffix)
            buy_px = px["open"] * (1 + SLIPPAGE)
            shares = int(pos_val / buy_px / 100) * 100
            cost = shares * buy_px
            if cost * (1 + commission) > cash:
                shares = int(cash / buy_px / (1 + commission) / 100) * 100
                cost = shares * buy_px
            if shares < 100 or cost <= 0:
                continue
            cash -= cost * (1 + commission)
            holdings[suffix] = {"shares": shares, "buy_px": buy_px, "buy_date": tgt_day}
            trades.append({"day": tgt_day, "symbol": suffix, "action": "BUY",
                           "px": round(buy_px, 3), "shares": shares,
                           "cost": round(cost, 2), "pnl": 0.0,
                           "reason": "pending_buy_open", "score": round(score_map.get(suffix, 0), 4)})

        for suffix in new_targets:
            if len(holdings) >= TOP_N or cash <= pos_val * 0.1:
                break
            _, _, cannot_buy, _ = _bar_state(suffix, tgt_day)
            px = price.get((suffix, tgt_day))
            if not px or cannot_buy:
                pending_buy[suffix] = True
                continue
            buy_px = px["open"] * (1 + SLIPPAGE)
            shares = int(pos_val / buy_px / 100) * 100
            cost = shares * buy_px
            if cost * (1 + commission) > cash:
                shares = int(cash / buy_px / (1 + commission) / 100) * 100
                cost = shares * buy_px
            if shares < 100 or cost <= 0:
                continue
            cash -= cost * (1 + commission)
            holdings[suffix] = {"shares": shares, "buy_px": buy_px, "buy_date": tgt_day}
            trades.append({"day": tgt_day, "symbol": suffix, "action": "BUY",
                           "px": round(buy_px, 3), "shares": shares,
                           "cost": round(cost, 2), "pnl": 0.0,
                           "reason": "new_entry", "score": round(score_map.get(suffix, 0), 4)})

        # 当日估值
        total_val = sum(
            (price.get((s, tgt_day), {}).get("close") or h["buy_px"]) * h["shares"]
            for s, h in holdings.items()
        ) + cash
        daily[tgt_day] = {"holdings": list(holdings.keys()), "value": total_val, "cash": cash,
                          "n": len(holdings), "pending_buy": len(pending_buy), "pending_sell": len(pending_sell)}

    return {"dates": sorted(daily.keys()), "daily": daily, "trades": trades}


def report(result, signals, klines, names=None):
    daily = result["daily"]
    trades = result["trades"]
    dates = result["dates"]
    names = names or {}

    # ===== 净值 =====
    nets = [daily[d]["value"] / INIT_CASH for d in dates]
    total_ret = nets[-1] - 1
    days = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
    years = max(days, 1) / 365
    annual = nets[-1] ** (1 / years) - 1 if nets[-1] > 0 else None

    # 最大回撤
    peak, max_dd = -1e9, 0
    for n in nets:
        peak = max(peak, n)
        dd = (peak - n) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # 日/周/月收益
    daily_rets = []
    for i in range(1, len(nets)):
        daily_rets.append(nets[i] / nets[i - 1] - 1)

    import time
    from datetime import datetime
    week_map = {}
    for d in dates:
        dt = datetime.fromisoformat(d)
        wk = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]}"
        week_map.setdefault(wk, []).append(d)
    month_map = {}
    for d in dates:
        month_map.setdefault(d[:7], []).append(d)

    # 买卖盈亏
    sells = [t for t in trades if t["action"] == "SELL"]
    realized_pnl = sum(t["pnl"] for t in sells)
    win_sells = [t for t in sells if t["pnl"] > 0]
    win_rate = len(win_sells) / len(sells) if sells else 0

    # top20 盈亏股
    sym_pnl = {}
    for t in sells:
        sym_pnl.setdefault(t["symbol"], {"pnl": 0.0, "name": names.get(t["symbol"], t["symbol"]), "n": 0})
        sym_pnl[t["symbol"]]["pnl"] += t["pnl"]
        sym_pnl[t["symbol"]]["n"] += 1
    top_win = sorted(sym_pnl.items(), key=lambda x: -x[1]["pnl"])[:20]
    top_loss = sorted(sym_pnl.items(), key=lambda x: x[1]["pnl"])[:20]

    # 打印
    def P(s=""): print(s)
    P("=" * 70)
    P("L2 CatBoost T+5 全年严格回测报告")
    P(f"模型: {MODEL_ID}")
    P(f"周期: {dates[0]} ~ {dates[-1]}  ({len(dates)} 交易日)")
    P(f"初始资金: {INIT_CASH:,} 元 | 滑点: {SLIPPAGE*100:.1f}% | 剔除ST | T+1")
    P("=" * 70)
    P(f"累计收益: {total_ret*100:+.2f}%")
    P(f"年化收益: {annual*100:+.2f}%" if annual else "年化: --")
    P(f"最大回撤: {max_dd*100:.2f}%")
    P(f"已实现盈亏: {realized_pnl:+,.0f} 元")
    P(f"卖出 {len(sells)} 笔, 胜率 {win_rate*100:.1f}%")
    P(f"末日净值: {nets[-1]:.4f}, 资金利用: {(daily[dates[-1]]['value']-daily[dates[-1]]['cash'])/daily[dates[-1]]['value']*100:.1f}%")

    P("\n📅 月度收益:")
    for m, ds in sorted(month_map.items()):
        if len(ds) < 2: continue
        m_ret = nets[dates.index(ds[-1])] / nets[dates.index(ds[0])] - 1
        P(f"  {m}: {m_ret*100:+.2f}%  ({len(ds)}天)")

    P("\n📅 周度收益:")
    for wk, ds in sorted(week_map.items()):
        if len(ds) < 2: continue
        w_ret = nets[dates.index(ds[-1])] / nets[dates.index(ds[0])] - 1
        P(f"  {wk}: {w_ret*100:+.2f}%  ({len(ds)}天)")

    P("\n🏆 Top20 盈利股票:")
    for sym, info in top_win:
        P(f"  {info['name']:10} {sym:12} +{info['pnl']:>10,.0f} 元  ({info['n']}笔)")

    P("\n📉 Top20 亏损股票:")
    for sym, info in top_loss:
        P(f"  {info['name']:10} {sym:12} {info['pnl']:>10,.0f} 元  ({info['n']}笔)")

    P("\n📄 交易明细（前60笔）:")
    P(f"{'日期':12} {'代码':12} {'动作':4} {'价格':>8} {'股数':>6} {'盈亏':>10}")
    for t in trades[:60]:
        nm = names.get(t["symbol"], t["symbol"])
        P(f"{t['day']:12} {nm[:8]:8} {t['action']:4} {t['px']:>8.2f} {t['shares']:>6} {t['pnl']:>10,.0f}")

    return {"dates": dates, "daily": daily, "trades": trades, "nets": nets,
            "monthly": {m: nets[dates.index(ds[-1])] / nets[dates.index(ds[0])] - 1 for m, ds in month_map.items() if len(ds) >= 2},
            "weekly": {wk: nets[dates.index(ds[-1])] / nets[dates.index(ds[0])] - 1 for wk, ds in week_map.items() if len(ds) >= 2},
            "top_win": [(info['name'], sym, info['pnl']) for sym, info in top_win],
            "top_loss": [(info['name'], sym, info['pnl']) for sym, info in top_loss],
            "realized_pnl": realized_pnl, "win_rate": win_rate, "total_ret": total_ret,
            "max_dd": max_dd, "annual": annual}


def load_names():
    import os
    import psycopg2
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, name FROM stocks")
            return {str(x): str(y) for x, y in cur.fetchall()}
    finally:
        conn.close()


if __name__ == "__main__":
    print("加载信号...")
    signals = load_signals()
    all_syms = set()
    for it in signals.values():
        for s, _ in it:
            all_syms.add(s)
    print(f"信号天数: {len(signals)}, 股票: {len(all_syms)}")

    print("加载K线...")
    klines = load_klines(all_syms)

    st = load_st_symbols()
    print(f"ST 剔除: {len(st)}")

    names = load_names()
    result = run_backtest(signals, klines, st)
    report(result, signals, klines, names)
