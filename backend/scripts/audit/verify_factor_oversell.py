#!/usr/bin/env python3
"""验证 qlib 回测「卖出数量 > 买入数量」的根因假设：

假设：卖出股数 = 持仓原始股数 × factor(卖出日) / factor(买入日)
（qlib 以后复权浮点份额记账，买入按买入日因子取整到整手，
 整仓卖出跳过取整、按卖出日因子还原 → 除权除息导致系统性多卖）

判决标准：
1. 每笔超卖 sell 的 qty_ratio 与 factor_ratio 相对误差 < 1e-3
2. 超卖笔数 / 虚增现金 与人工审计（246 笔 / ~47867 元）量级一致
"""

import glob
import json
import sys
from collections import defaultdict

import numpy as np

QLIB = "/app/db/qlib_data"
RESULT_GLOBS = [
    "/data/backtest_results/**/395ab422d6024433a347e332cf47addd.json",
    "/data/backtest_results/**/*.json",
]

calendar = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
date_idx = {d: i for i, d in enumerate(calendar)}

_bin_cache: dict[str, tuple[int, np.ndarray]] = {}


def get_field(sym: str, field: str, date: str) -> float:
    key = f"{sym}/{field}"
    if key not in _bin_cache:
        arr = np.fromfile(f"{QLIB}/features/{sym}/{field}.day.bin", dtype="<f4")
        _bin_cache[key] = (int(arr[0]), arr[1:])
    start, vals = _bin_cache[key]
    i = date_idx.get(date)
    if i is None:
        return float("nan")
    j = i - start
    if 0 <= j < len(vals):
        return float(vals[j])
    return float("nan")


def get_factor(sym: str, date: str) -> float:
    """模拟 CnExchange._get_recent_valid_quote：当日 NaN 向前回查 10 个自然日。"""
    v = get_field(sym, "factor", date)
    if np.isfinite(v) and v > 0:
        return v
    i = date_idx.get(date)
    if i is None:
        return float("nan")
    for back in range(1, 15):
        if i - back < 0:
            break
        v = get_field(sym, "factor", calendar[i - back])
        if np.isfinite(v) and v > 0:
            return v
    return float("nan")


def main() -> int:
    paths: list[str] = []
    for g in RESULT_GLOBS:
        paths = glob.glob(g, recursive=True)
        if paths:
            break
    if not paths:
        print("未找到回测结果文件")
        return 1
    path = max(paths, key=lambda p: "395ab422" in p)
    print(f"结果文件: {path}")
    payload = json.load(open(path))
    trades = payload.get("trades") or []
    buys = [t for t in trades if str(t.get("action", "")).lower() == "buy"]
    sells = [t for t in trades if str(t.get("action", "")).lower() == "sell"]
    print(f"总成交 {len(trades)} 笔（买 {len(buys)} / 卖 {len(sells)}）")

    odd_sells = [t for t in sells if int(round(float(t["quantity"]))) % 100]
    odd_buys = [t for t in buys if int(round(float(t["quantity"]))) % 100]
    print(f"零头卖出 {len(odd_sells)} 笔；零头买入 {len(odd_buys)} 笔")

    # ── 逐标的累计审计：卖出超过累计买入即超卖 ──
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        sym = str(t.get("symbol", "")).lower()
        by_sym[sym].append(t)

    oversell_rows = []
    phantom_cash = 0.0
    match_ok = match_bad = 0
    for sym, items in by_sym.items():
        items.sort(key=lambda t: (str(t["date"]), 0 if str(t["action"]).lower() == "sell" else 1))
        cum_buy = cum_sell = 0.0
        open_adj = 0.0  # 复权份额台账：Σ lot_i / f_buy_i（qlib 内部持仓口径）
        last_buy_date = None
        for t in items:
            qty = float(t["quantity"])
            d = str(t["date"])[:10]
            if str(t["action"]).lower() == "buy":
                f_b = get_factor(sym, d)
                cum_buy += qty
                last_buy_date = d
                if f_b and np.isfinite(f_b) and f_b > 0:
                    open_adj += qty / f_b
            else:
                open_raw = cum_buy - cum_sell
                f_sell = get_factor(sym, d)
                # 期望卖出量 = 复权份额台账 × 卖出日因子（多笔加仓的精确形式）
                expected_raw = open_adj * f_sell if np.isfinite(f_sell) and f_sell > 0 else open_raw
                if qty > open_raw + 0.5 and open_raw > 0:
                    excess = qty - open_raw
                    price = float(t.get("price") or 0)
                    phantom_cash += excess * price
                    qty_ratio = qty / open_raw
                    fac_ratio = expected_raw / open_raw
                    rel_err = abs(qty_ratio - fac_ratio) / fac_ratio if fac_ratio > 0 else 9.9  # fidelity: allow-limit-threshold — 非阈值：除零哨兵值
                    if rel_err < 1e-3:
                        match_ok += 1
                    else:
                        match_bad += 1
                    oversell_rows.append(
                        (sym, last_buy_date or "", d, open_raw, qty,
                         qty_ratio, fac_ratio, rel_err)
                    )
                # 消耗台账：按卖出的复权份额扣减
                if np.isfinite(f_sell) and f_sell > 0:
                    open_adj = max(0.0, open_adj - qty / f_sell)
                cum_sell += qty

    print(f"\n超卖笔数: {len(oversell_rows)}（因子比吻合 <0.1%: {match_ok}，不吻合: {match_bad}）")
    print(f"按卖价估算虚增现金: {phantom_cash:,.0f} 元")

    oversell_rows.sort(key=lambda r: -abs(r[4] - r[3]) / r[3])
    print("\n超卖比例 Top10 及因子比对（qty_ratio 应≈ factor_ratio）:")
    print(f"{'symbol':10} {'买入日':10} {'卖出日':10} {'持仓':>7} {'卖出':>7} "
          f"{'qty_ratio':>10} {'fac_ratio':>10} {'rel_err':>8}")
    for r in oversell_rows[:10]:
        print(f"{r[0]:10} {r[1]:10} {r[2]:10} {r[3]:7.0f} {r[4]:7.0f} "
              f"{r[5]:10.5f} {r[6]:10.5f} {r[7]:8.2e}")

    # ── 报告中三个典型例子 ──
    print("\n典型样本核对:")
    for sym, expect in [("sz300536", 3305 / 3300), ("sh603359", 10961 / 10400),
                        ("sh603612", 1482 / 1400)]:
        hit = [r for r in oversell_rows if r[0] == sym]
        for r in hit[:2]:
            print(f"  {sym}: 买{r[1]}→卖{r[2]} 持仓{r[3]:.0f} 卖出{r[4]:.0f} "
                  f"qty_ratio={r[5]:.5f} factor_ratio={r[6]:.5f} "
                  f"(报告值 {expect:.5f})")
        if not hit:
            print(f"  {sym}: 本轮累计审计未判为超卖（可能中途有加仓）")

    # 因子日间变化统计：确认除权事件确实是超卖的来源
    changed = sum(1 for r in oversell_rows if abs(r[6] - 1.0) > 1e-9)
    print(f"\n超卖中因子比率>1（买卖日间发生除权）: {changed}/{len(oversell_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
