#!/usr/bin/env python3
"""交易成本分析（TCA）— implementation shortfall 五分解 + 基准对标 + QuantDB(CN) 分钟基准。

来源：quantskills/skill-transaction-cost-analysis 与 quantskills/skill-transaction-cost-calibration
（均 GPL-3.0-only）。方法论保留（Perold 1988 IS 框架、square-root 冲击模型、VWAP/TWAP/arrival
基准、参与率-滑点校准），数据层由 PandaData SDK 改为本地 QuantDB 直读（2026-10 标定）：

  --demo     模式：纯标准库确定性合成明细，五分解数字可手算（内置分数精确自校验）。
  --input    模式：成交明细复核（纯标准库；可选 --bars 分钟线 CSV）。
                  columns: symbol,side,datetime,price,qty [,order_id,decision_price,arrival_price,
                  end_price,order_qty,adv,sigma_day,fees_bps,benchmark_price]
  --quantdb  模式：pandas/pyarrow，**在 quantmind 容器内运行**（CN 分钟数据为 per-symbol parquet）：
                  分钟线 ← <root>/quantdb/1_kline_data/min{1,5}_kline/<SYM>.parquet
                  对账   ← <root>/quantdb/1_kline_data/daily_unadjusted/dt=YYYYMMDD/data.parquet
                  盘口   ← <root>/quantdb/1_kline_data/tick_data/<SYM_>_YYYYMMDD.parquet（仅个别日期）

口径（2026-10 实测，详见 references/methodology.md）：
  - CN 分钟线 = **不复权原始价**（与 daily_unadjusted 同口径；与 daily_forward 混算会双重缩放）。
    volume 单位=股，amount 单位=**万元** → VWAP(元/股) = Σamount × 1e4 / Σvol。
  - 日总量对账窗 = 09:25 竞价 + 09:31-11:30 + 13:01-14:57 + 15:00 收盘竞价；14:58/14:59
    （深市个别日重复）与 15:01-15:30（盘后固定价格）不计入（实测剔除后与日线吻合到 1e-7）。
  - 费用常量：真单券商万 2.5 / 撮合回测保守万 3（刻意保守）；CN 卖出印花税 5 bps、过户费 0.1 bps。
  - 五分解统一为 **订单名义额@决策价** 的 bps，正值=对本方不利；各项和 ≡ 总 IS（残差对账）。

用法：
  python3 tca.py --demo --out /tmp/tca_demo.json
  python3 tca.py --input fills.csv [--bars bars.csv] --benchmark vwap --out report.json
  python3 tca.py --quantdb --symbol 600036.SH --date 2026-08-25 [--qty 200000] [--side buy] \
      [--tick-check] --out /data/reports/transaction-cost-analysis/cn_600036_20260825.json

输出：stdout 中文表格 + JSON 报告。仅限本地研究使用，不构成投资建议。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

# ---------------------------------------------------------------- 常量

COMMISSION_BPS_BROKER = 2.5      # 真单券商口径（本地实盘，万 2.5）
COMMISSION_BPS_MATCHING = 3.0    # 撮合/回测口径（万 3，刻意保守）
STAMP_DUTY_BPS = 5.0             # A 股卖出印花税 0.05%（2023-08-28 起单边）
TRANSFER_FEE_BPS = 0.1           # 过户费近似（万分之 0.1，双边小额）
DEFAULT_IMPACT_K = 0.1           # square-root 冲击系数 k 经验默认（源技能）
DEFAULT_WINDOW_MINUTES = 30      # 区间 VWAP/TWAP 默认窗口（源技能）
A_SHARE_MINUTES_PER_DAY = 240
TRADING_DAYS = 244
MIN_CALIB_FILLS = 5              # 参与率-滑点拟合最少样本
BUCKET_MINUTES = 30              # 分时段流动性画像桶宽

FEE_PROFILES = {
    "broker": COMMISSION_BPS_BROKER,
    "matching": COMMISSION_BPS_MATCHING,
    "none": 0.0,
}

DATA_ROOT_CANDIDATES = ["/data", "/quantmind/data", "/home/zbox/projects/quantmind/data"]

# 分钟线 canon：日总量对账窗（实测证据见 methodology.md §5）
CANON_TIMES = "09:25,09:31-11:30,13:01-14:57,15:00"
CN_SUFFIXES = (".SH", ".SZ", ".BJ")

SIDE_CN = {"buy": "买入", "sell": "卖出"}

BENCH_CN = {"vwap": "区间VWAP", "twap": "区间TWAP", "arrival": "到达价"}

# 已知局限（写入每份报告 caveats）
STATIC_CAVEATS = [
    "分钟级近似：本地无逐笔全量盘口（tick 快照仅个别日期），点差用分钟高低幅半幅代理，"
    "实测高估真实半价差约 5 倍（600036.SH 2026-07-20：代理 6.47 bps vs 盘口 1.29 bps），只作相对比较。",
    "冲击成本为 square-root 模型估计（k·σ_day·√(Q/ADV)），非盘口反演；k 亦可由成交数据校准。",
    "决策价缺省时用到达价代理（延迟成本=0）；到达价缺省时用成交前一根分钟线收盘价代理。",
    "结论用于研究与执行诊断，不用于清算结算；不构成投资建议。",
]


# ---------------------------------------------------------------- 基础工具

def parse_dt(s):
    """解析时间戳，兼容常见格式；失败返回 None。"""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M", "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def dir_of(side) -> int:
    """side=buy→+1，sell→−1（与源技能一致）。"""
    return 1 if str(side).strip().lower().startswith("b") else -1


def is_cn_symbol(symbol: str) -> bool:
    s = str(symbol).strip().upper()
    if s.endswith(CN_SUFFIXES):
        return True
    return len(s) == 6 and s.isdigit()


def classify_bar(hhmm: str, cn: bool = True) -> str:
    """分钟 bar 归属（标签=分钟结束时刻，TDX 约定）。cn=False 时全部视为连续。"""
    if not cn:
        return "continuous"
    if hhmm == "09:25":
        return "auction_open"
    if "09:31" <= hhmm <= "11:30" or "13:01" <= hhmm <= "14:57":
        return "continuous"
    if hhmm == "15:00":
        return "auction_close"
    if hhmm in ("14:58", "14:59"):
        return "close_redundant"   # 深市个别日重复 bar（实测剔除后与日线吻合）
    if "15:01" <= hhmm <= "15:30":
        return "afterhours"        # 盘后固定价格交易（不计入日线，实测）
    return "other"


def in_canon(hhmm: str) -> bool:
    """是否属于日总量对账窗（含两段集合竞价）。"""
    return classify_bar(hhmm) in ("auction_open", "continuous", "auction_close")


def fee_bps_for(side, symbol, profile: str, commission_override=None):
    """单边费用 bps（佣金 + 过户费 + CN 卖出印花税），返回 (总额, 分项 dict)。"""
    commission = (FEE_PROFILES.get(profile, COMMISSION_BPS_BROKER)
                  if commission_override is None else float(commission_override))
    stamp = STAMP_DUTY_BPS if (dir_of(side) < 0 and is_cn_symbol(symbol)) else 0.0
    transfer = TRANSFER_FEE_BPS if profile != "none" else 0.0
    parts = {"commission_bps": round(commission, 4), "stamp_duty_bps": round(stamp, 4),
             "transfer_fee_bps": round(transfer, 4)}
    return round(commission + stamp + transfer, 4), parts


def r4(x):
    """四舍五入到 4 位并消掉 -0.0（brown-bag：round(-0.0, 4) 仍是 -0.0）。"""
    return round(x, 4) or 0.0


def quantile(sorted_vals, q: float):
    """线性插值分位数（值须先排序）；空列表返回 None。"""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


# ---------------------------------------------------------------- 分钟线核心（纯标准库）

def interval_vwap(bars):
    """Σamount/Σvol；无 amount 时用 close×vol 近似。返回 None 表示不可算。"""
    amt = vol = 0.0
    for b in bars:
        v = b.get("v") or 0.0
        a = b.get("amt")
        amt += (a if a else (b.get("c") or 0.0) * v)
        vol += v
    return amt / vol if vol > 0 else None


def interval_twap(bars):
    """典型价 (H+L+C)/3 等权平均；缺高/低回退收盘。"""
    px = []
    for b in bars:
        hi, lo, cl = b.get("h"), b.get("l"), b.get("c")
        if hi and lo and cl:
            px.append((hi + lo + cl) / 3.0)
        elif cl:
            px.append(cl)
    return sum(px) / len(px) if px else None


def realized_sigma_day(bars):
    """分钟对数收益的标准差 × sqrt(240) = 日尺度 σ；不足返回 None。

    年化 = σ_day × sqrt(244)（源技能口径：分钟波动 × sqrt(240×244)）。
    """
    closes = [b.get("c") for b in bars if (b.get("c") or 0) > 0]
    if len(closes) < 5:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    if var <= 0:
        return None
    sd = math.sqrt(var) * math.sqrt(A_SHARE_MINUTES_PER_DAY)
    return sd


def spread_proxy_bps(bars):
    """分钟高低幅的半幅均值作半点差代理（bps，源技能口径）。"""
    vals = []
    for b in bars:
        hi, lo = b.get("h") or 0.0, b.get("l") or 0.0
        mid = (hi + lo) / 2.0
        if hi and lo and mid > 0:
            vals.append(0.5 * (hi - lo) / mid * 1e4)
    if not vals:
        return None
    vals.sort()
    return {"mean_bps": sum(vals) / len(vals), "median_bps": quantile(vals, 0.5)}


def adv_from_days(daily_vols):
    """ADV（股）= 给定各交易日总量均值。"""
    vols = [v for v in daily_vols if v and v > 0]
    return sum(vols) / len(vols) if vols else None


def slice_window(bars, dt: datetime, window_minutes: float):
    """成交时刻 ± window/2 的连续竞价 bar；空则取最近一根。"""
    half = timedelta(minutes=window_minutes / 2.0)
    lo, hi = dt - half, dt + half
    sel = [b for b in bars if b["_t"] is not None and lo <= b["_t"] <= hi]
    if sel:
        return sel
    if bars:
        return [min(bars, key=lambda b: abs(b["_t"] - dt))]
    return []


def arrival_from_bars(bars, dt: datetime):
    """到达价 = dt 之前最后一根 bar 的收盘（源技能口径）。"""
    prior = [b for b in bars if b["_t"] is not None and b["_t"] < dt]
    if prior:
        return prior[-1].get("c")
    if bars:
        return bars[0].get("o") or bars[0].get("c")
    return None


def bucket_profile(bars):
    """半小时桶量占比（0.5h × 8 连续桶 + 开盘/收盘竞价单列）。"""
    def bucket_of(b):
        s = b["session"]
        if s == "auction_open":
            return "09:25 开盘竞价"
        if s == "auction_close":
            return "15:00 收盘竞价"
        m = int(b["hhmm"][:2]) * 60 + int(b["hhmm"][3:])
        if m <= 11 * 60 + 30:
            start = max(9 * 60 + 30, (m - 1) // BUCKET_MINUTES * BUCKET_MINUTES)
        else:
            start = max(13 * 60, (m - 1) // BUCKET_MINUTES * BUCKET_MINUTES)
        hh, mm = divmod(start, 60)
        eh, em = divmod(start + BUCKET_MINUTES, 60)
        return f"{hh:02d}:{mm:02d}-{eh:02d}:{em:02d}"

    order, agg = [], {}
    for b in bars:
        k = bucket_of(b)
        if k not in agg:
            agg[k] = {"vol": 0.0, "amt": 0.0, "nbsp": 0}
            order.append(k)
        agg[k]["vol"] += b.get("v") or 0.0
        agg[k]["amt"] += b.get("amt") or 0.0
        agg[k]["nbsp"] += 1
    total = sum(a["vol"] for a in agg.values()) or 1.0
    out = []
    for k in order:
        a = agg[k]
        vwap = (a["amt"] / a["vol"]) if a["vol"] > 0 and a["amt"] > 0 else None
        out.append({"bucket": k, "n_bars": a["nbsp"], "volume": a["vol"],
                    "volume_share": a["vol"] / total,
                    "vwap": vwap if vwap is not None else None})
    return out


def ols_fit(xs, ys):
    """一元 OLS，返回 (slope, intercept, r2)；样本不足/退化返回 None。

    退化判据用相对跨度而非 sxx<=0：xs 全同时，Py≤3.11 的 sum() 左折叠会
    让 sum(xs)/n 与 x 差 1 ulp，sxx≈1e-35>0，可拟出虚高斜率（Py≥3.12 的
    补偿求和无此噪声，结果随解释器版本漂移）。
    """
    xs = list(xs)
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    if max(xs) - min(xs) <= 1e-9 * max(abs(mx), 1e-12):
        return None
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


# ---------------------------------------------------------------- 五分解（纯标准库）

def decompose_order(order: dict, *, k: float, fee_profile: str,
                    commission_bps=None) -> dict:
    """订单级 IS 五分解（全部折算为「订单名义额@决策价」bps，正值=不利）。

    符号：D=决策价, A=到达价, E=Σ(p·q)/Σq 成交均价, P=区间末价, Q=订单量, q=已成交量,
          f=q/Q, dir=buy:+1/sell:−1, κ=费用 bps(按成交名义额)。
    恒等式（元）：IS = q·dir·(E−D) + (Q−q)·dir·(P−D) + Σ费用
      延迟 delay = dir·q·(A−D)/(Q·D)×1e4
      执行 exec  = dir·q·(E−A)/(Q·D)×1e4 = 冲击 impact + 择时 timing
        冲击 impact = f·k·σ_day·√(Q/ADV)×1e4（square-root 模型，f 折算到订单名义额）
        择时 timing = exec − impact（残差项；无 σ/ADV 时 impact=0 并记 degraded）
      机会 opp   = dir·(Q−q)·(P−D)/(Q·D)×1e4（未成交部分 vs 期末价）
      费用 fees  = Σ(κ_i·p_i·q_i)/(Q·D)×1e4
    """
    symbol = order["symbol"]
    side = order["side"]
    d = dir_of(side)
    fills = order["fills"]
    degraded: list[str] = []

    q = sum(f["qty"] for f in fills)
    if q <= 0:
        raise ValueError(f"{symbol}: 无有效成交数量")
    e = sum(f["price"] * f["qty"] for f in fills) / q

    decision = order.get("decision_price")
    arrival = order.get("arrival_price")
    if arrival is None:
        arrival = decision
    if decision is None:
        decision = arrival
        degraded.append("无决策价，以到达价代理（延迟成本=0）")
    if decision is None:
        raise ValueError(f"{symbol}: 需要 decision_price 或 arrival_price 至少其一")
    if order.get("arrival_price") is None:
        degraded.append("无到达价，以决策价代理（延迟/执行分界退化）")

    q_order = order.get("order_qty") or q
    f_ratio = q / q_order
    if order.get("order_qty") is None:
        degraded.append("无订单量(order_qty)，视同全部成交（机会成本=0）")

    end_price = order.get("end_price")
    if q_order > q and end_price is None:
        degraded.append("有未成交部分但无期末价(end_price)，机会成本=0")
        end_price = decision

    # --- 逐笔费用（按成交名义额） ---
    fees_ccy = 0.0
    fee_parts = None
    for fl in fills:
        if fl.get("fees_bps") is not None:
            fb = float(fl["fees_bps"])
            if fee_parts is None and len(fills) == 1:
                fee_parts = {"explicit_fees_bps": round(fb, 4)}
        else:
            fb, fee_parts = fee_bps_for(side, symbol, fee_profile, commission_bps)
        fees_ccy += fb / 1e4 * fl["price"] * fl["qty"]

    notional_decision = q_order * decision
    s = 1e4 / notional_decision

    delay = d * q * (arrival - decision) * s
    exec_bps = d * q * (e - arrival) * s

    # 冲击（模型估计）：参与率 = Q/ADV，σ_day 折算
    adv = order.get("adv")
    sigma_day = order.get("sigma_day")
    if adv and sigma_day and float(adv) > 0:
        participation = q_order / float(adv)
        impact = f_ratio * k * float(sigma_day) * math.sqrt(participation) * 1e4
    else:
        participation = None
        impact = 0.0
        degraded.append("无 ADV 或 σ_day，冲击成本(timing/impact 拆分)不可算，执行漂移全部计入择时")

    timing = exec_bps - impact

    opp = d * (q_order - q) * ((end_price if end_price is not None else decision) - decision) * s
    fees = fees_ccy * s

    total = delay + exec_bps + opp + fees          # 恒等式一路
    direct = (q * d * (e - decision)
              + (q_order - q) * d * (end_price - decision)
              + fees_ccy) * s                     # 独立算术路径对账
    residual = direct - total
    five_sum = delay + impact + timing + opp + fees

    return {
        "symbol": symbol,
        "side": side,
        "date": order.get("date"),
        "fills_n": len(fills),
        "prices": {"decision": decision, "arrival": arrival,
                   "avg_fill": round(e, 6), "end": end_price},
        "qty": {"order": q_order, "filled": q, "fill_ratio": round(f_ratio, 6)},
        "market": {
            "adv": adv, "sigma_day": sigma_day,
            "participation": round(participation, 8) if participation else None,
            "spread_proxy_bps": order.get("spread_proxy_bps"),
        },
        "five_way_bps": {
            "delay": r4(delay),
            "impact": r4(impact),
            "timing": r4(timing),
            "opportunity": r4(opp),
            "fees": r4(fees),
            "total": r4(total),
            "residual_bps": round(residual, 8),
            "five_sum": r4(five_sum),
        },
        "fee_detail": fee_parts,
        "degraded": degraded,
    }


def calibrate_impact(rows: list[dict], sigma_day):
    """参与率-滑点校准：|滑点 bps| ~ a + b·√参与率（合并自 skill-transaction-cost-calibration）。

    implied k = b / (σ_day×1e4)（因 impact_bps = k·σ_day·√part·1e4）。
    """
    xs, ys = [], []
    for r in rows:
        if r.get("participation") and r.get("abs_slippage_bps") is not None:
            xs.append(math.sqrt(r["participation"]))
            ys.append(abs(r["abs_slippage_bps"]))
    if len(xs) < MIN_CALIB_FILLS:
        return {"status": "insufficient",
                "n_fills": len(xs),
                "note": f"参与率|滑点|配对样本 < {MIN_CALIB_FILLS}，不拟合"}
    fit = ols_fit(xs, ys)
    if fit is None:
        return {"status": "insufficient", "n_fills": len(xs), "note": "拟合退化（方差为零）"}
    slope, intercept, r2 = fit
    parts = [r["participation"] for r in rows if r.get("participation")]
    out = {
        "status": "ok",
        "n_fills": len(xs),
        "slope_bps_per_sqrt_part": round(slope, 4),
        "intercept_bps": round(intercept, 4),
        "r2": round(r2, 4),
        "participation_min": min(parts),
        "participation_max": max(parts),
        "note": "仅用 |成交价−基准价|（--benchmark 口径）拟合；勿外推到参与率观测区间之外（源校准技能纪律）",
    }
    if sigma_day and sigma_day > 0:
        out["implied_k"] = round(slope / (sigma_day * 1e4), 4)
        out["sigma_day_used"] = sigma_day
    return out


# ---------------------------------------------------------------- demo 模式

def demo_bars():
    """合成分钟线（DEMO.SH，2026-07-24）：amount ≡ close×vol，数字可手算。"""
    rows = [
        ("09:25", 10.01, 10.01, 10.01, 10.01, 500.0),
        ("09:31", 10.04, 10.06, 10.02, 10.04, 1000.0),
        ("09:35", 10.06, 10.09, 10.05, 10.08, 2000.0),
        ("09:45", 10.08, 10.12, 10.08, 10.10, 2000.0),
        ("10:00", 10.11, 10.15, 10.11, 10.13, 1000.0),
        ("15:00", 10.16, 10.16, 10.16, 10.16, 800.0),
    ]
    bars = []
    for hhmm, o, h, lo, c, v in rows:
        t = parse_dt(f"2026-07-24 {hhmm}:00")
        bars.append({"hhmm": hhmm, "o": o, "h": h, "l": lo, "c": c, "v": v,
                     "amt": round(c * v, 6), "session": classify_bar(hhmm),
                     "_t": t, "t": t.strftime("%Y-%m-%d %H:%M:%S")})
    return bars


def demo_orders():
    """两笔合成订单（买入 10000/8000 成交；卖出 5000 全成交），手算数字见 _demo_handcheck。"""
    return [
        {"symbol": "DEMO.SH", "side": "buy", "date": "2026-07-24",
         "decision_price": 10.00, "arrival_price": 10.05, "end_price": 10.20,
         "order_qty": 10000, "adv": 200000, "sigma_day": 0.02,
         "fills": [{"datetime": "2026-07-24 09:35:00", "price": 10.08, "qty": 3000},
                   {"datetime": "2026-07-24 09:45:00", "price": 10.10, "qty": 3000},
                   {"datetime": "2026-07-24 10:00:00", "price": 10.13, "qty": 2000}]},
        {"symbol": "DEMO.SH", "side": "sell", "date": "2026-07-24",
         "decision_price": 20.00, "arrival_price": 19.95, "end_price": 19.80,
         "order_qty": 5000, "adv": 100000, "sigma_day": 0.025,
         "fills": [{"datetime": "2026-07-24 09:35:00", "price": 19.90, "qty": 3000},
                   {"datetime": "2026-07-24 09:50:00", "price": 19.95, "qty": 2000}]},
    ]


def _demo_handcheck():
    """分数精确算术独立复算 demo 期望值（与浮点实现互为校验）。"""
    F = Fraction
    exp = {}

    # 订单 1（买入）：D=10, A=10.05, E=10.10(手算), P=10.20, Q=10000, q=8000, f=0.8
    e1 = (F(3000) * F("10.08") + F(3000) * F("10.10") + F(2000) * F("10.13")) / F(8000)
    assert e1 == F("10.10")                      # 手算：成交均价 = 10.10
    delay1 = F(8000) * (F("10.05") - F(10)) / (F(10000) * F(10)) * 10000
    exec1 = F(8000) * (e1 - F("10.05")) / (F(10000) * F(10)) * 10000
    opp1 = F(2000) * (F("10.20") - F(10)) / (F(10000) * F(10)) * 10000
    fees1 = F(80800) * F("2.6") / 10000 / F(100000) * 10000   # 成交额 80800×2.6bp
    # 冲击项含 √(Q/ADV) 无理数，手算用 float 闭式（其余各项为分数精确值）
    impact1 = 0.8 * 0.1 * 0.02 * math.sqrt(0.05) * 1e4
    exp["buy"] = {
        "delay_bps": delay1, "exec_bps": exec1, "impact_bps": impact1,
        "timing_bps": float(exec1) - impact1,
        "opportunity_bps": opp1, "fees_bps": fees1,
        "total_bps": delay1 + exec1 + opp1 + fees1,
    }
    # 订单 2（卖出）：D=20, A=19.95, E=(3000·19.90+2000·19.95)/5000=19.92, Q=q=5000
    e2 = (F(3000) * F("19.90") + F(2000) * F("19.95")) / F(5000)
    assert e2 == F("19.92")
    delay2 = F(-1) * F(5000) * (F("19.95") - F(20)) / (F(5000) * F(20)) * 10000
    exec2 = F(-1) * F(5000) * (e2 - F("19.95")) / (F(5000) * F(20)) * 10000
    impact2 = 0.1 * 0.025 * math.sqrt(0.05) * 1e4
    fees2 = F(3000) * F("19.90") * F("7.6") / 10000 + F(2000) * F("19.95") * F("7.6") / 10000
    exp["sell"] = {
        "delay_bps": delay2, "exec_bps": exec2, "impact_bps": impact2,
        "timing_bps": float(exec2) - impact2, "opportunity_bps": F(0),
        "fees_bps": fees2 / F(100000) * 10000,
        "total_bps": delay2 + exec2 + fees2 / F(100000) * 10000,
    }
    return exp


def run_demo(args) -> dict:
    bars = demo_bars()
    orders = demo_orders()
    order_costs = [decompose_order(o, k=DEFAULT_IMPACT_K, fee_profile="broker")
                   for o in orders]

    # 手算对账（分数精确）
    exp = _demo_handcheck()
    checks = []
    for oc, key in zip(order_costs, ("buy", "sell"), strict=True):
        fw = oc["five_way_bps"]
        for item, field in (("delay_bps", "delay"), ("impact_bps", "impact"),
                            ("timing_bps", "timing"), ("opportunity_bps", "opportunity"),
                            ("fees_bps", "fees"), ("total_bps", "total")):
            want = float(exp[key][item])
            got = float(fw[field])
            ok = abs(want - got) < 1e-4   # five_way_bps 已四舍五入到 4 位小数
            checks.append({"order": key, "item": field, "expected": round(want, 6),
                           "actual": round(got, 6), "ok": ok})
    if not all(c["ok"] for c in checks):
        raise SystemExit("demo 手算校验失败：" + json.dumps(checks, ensure_ascii=False))

    # 基准表（全日 / 连续 VWAP、TWAP、到达价、点差代理）
    cont = [b for b in bars if b["session"] == "continuous"]
    vwap_all = interval_vwap(bars)
    vwap_cont = interval_vwap(cont)
    twap_cont = interval_twap(cont)
    benchmarks = {
        "vwap_all": vwap_all, "vwap_continuous": vwap_cont,
        "twap_continuous": twap_cont,
        "arrival_open": bars[0]["c"],
        "spread_proxy_bps": spread_proxy_bps(cont),
    }

    # 逐笔对标（区间 ±15min VWAP→窗口默认 30 分钟）
    compare = _benchmark_compare(orders[0], bars, "vwap", DEFAULT_WINDOW_MINUTES, "broker")
    return {
        "mode": "demo",
        "bars_n": len(bars),
        "benchmarks": benchmarks,
        "orders": order_costs,
        "benchmark_compare": compare,
        "handcheck": {"all_ok": True, "items": checks},
    }


# ---------------------------------------------------------------- 基准对标（纯标准库）

def _benchmark_compare(order: dict, bars: list[dict], kind: str,
                       window_minutes: int, fee_profile: str) -> dict:
    """逐笔滑点 vs 所选基准 + 超越基准率（按成交额加权）。"""
    d = dir_of(order["side"])
    sym_bars = [b for b in bars if b["session"] == "continuous"]
    per_fill, tot_nt, w_slip, beat_nt = [], 0.0, 0.0, 0.0
    for fl in order["fills"]:
        dt = parse_dt(fl.get("datetime"))
        if fl.get("benchmark_price"):
            bench = float(fl["benchmark_price"])
        elif dt is not None and sym_bars:
            if kind == "twap":
                bench = interval_twap(slice_window(sym_bars, dt, window_minutes))
            elif kind == "arrival":
                bench = arrival_from_bars(sym_bars, dt)
            else:
                bench = interval_vwap(slice_window(sym_bars, dt, window_minutes))
        else:
            bench = None
        nt = fl["price"] * fl["qty"]
        row = {"datetime": fl.get("datetime"), "price": fl["price"], "qty": fl["qty"],
               "benchmark_price": round(bench, 6) if bench else None}
        if bench:
            slip = d * (fl["price"] - bench) / bench * 1e4
            row["slippage_bps"] = round(slip, 4)
            tot_nt += nt
            w_slip += slip * nt
            if slip <= 0:
                beat_nt += nt
        per_fill.append(row)
    return {
        "kind": kind,
        "window_minutes": window_minutes,
        "mean_slippage_bps": round(w_slip / tot_nt, 4) if tot_nt > 0 else None,
        "beat_rate": round(beat_nt / tot_nt, 4) if tot_nt > 0 else None,
        "beat_note": "超越基准率 = 滑点≤0 的成交额占比（正值=劣于基准）",
        "per_fill": per_fill,
    }


# ---------------------------------------------------------------- --input 模式

def _csv_num(row: dict, key: str, default=None):
    v = row.get(key)
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def read_fills_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            r = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v)
                 for k, v in raw.items()}
            if not r.get("symbol") or not r.get("price"):
                continue
            rows.append({
                "symbol": str(r["symbol"]).upper(),
                "side": str(r.get("side", "buy")).lower(),
                "datetime": r.get("datetime") or r.get("time") or "",
                "price": _csv_num(r, "price", 0.0) or 0.0,
                "qty": _csv_num(r, "qty", 0.0) or 0.0,
                "order_id": r.get("order_id") or None,
                "decision_price": _csv_num(r, "decision_price"),
                "arrival_price": _csv_num(r, "arrival_price"),
                "end_price": _csv_num(r, "end_price"),
                "order_qty": _csv_num(r, "order_qty"),
                "adv": _csv_num(r, "adv"),
                "sigma_day": _csv_num(r, "sigma_day"),
                "fees_bps": _csv_num(r, "fees_bps"),
                "benchmark_price": _csv_num(r, "benchmark_price"),
            })
    return rows


def read_bars_csv(path: str) -> tuple[list[dict], list[str]]:
    """分钟线 CSV → 标准 bar 列表 + 告警。amount 单位不做猜测，做量纲体检。"""
    bars, warns = [], []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            r = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v)
                 for k, v in raw.items()}
            t = parse_dt(r.get("datetime") or r.get("time") or "")
            if t is None:
                continue
            hhmm = t.strftime("%H:%M")
            cn = is_cn_symbol(str(r.get("symbol", "")))
            bars.append({"symbol": str(r.get("symbol", "")).upper(),
                         "hhmm": hhmm, "session": classify_bar(hhmm, cn),
                         "o": _csv_num(r, "open"), "h": _csv_num(r, "high"),
                         "l": _csv_num(r, "low"), "c": _csv_num(r, "close"),
                         "v": _csv_num(r, "volume") or 0.0,
                         "amt": _csv_num(r, "amount"), "_t": t,
                         "t": t.strftime("%Y-%m-%d %H:%M:%S")})
    bars.sort(key=lambda b: (b["symbol"], b["_t"]))
    # amount 量纲体检：vwap/收盘均值 应接近 1
    for sym in {b["symbol"] for b in bars}:
        sb = [b for b in bars if b["symbol"] == sym and b.get("amt") and b.get("v")]
        if not sb:
            continue
        vwap = interval_vwap(sb)
        closes = [b["c"] for b in sb if b.get("c")]
        ref = sum(closes) / len(closes) if closes else None
        if vwap and ref and not (0.5 <= vwap / ref <= 2.0):
            warns.append(f"{sym}: amount/volume 与收盘均价相差 {vwap / ref:.3g}×，"
                         f"amount 单位可能与价格不一致（须同量纲；QuantDB 万元需 ×1e4）")
    return bars, warns


def group_orders(fills: list[dict]) -> list[dict]:
    """按 order_id（若有）或 (symbol, side, 日期) 分组为订单。"""
    groups: dict = {}
    for fl in fills:
        dt = parse_dt(fl["datetime"])
        key = (fl["order_id"] or (fl["symbol"], fl["side"],
                                  dt.date().isoformat() if dt else "nodate"))
        groups.setdefault(key, []).append(fl)
    orders = []
    for _key, fls in groups.items():
        first = fls[0]
        dt0 = parse_dt(first["datetime"])
        o = {"symbol": first["symbol"], "side": first["side"],
             "date": dt0.date().isoformat() if dt0 else None}
        for field in ("decision_price", "arrival_price", "end_price", "order_qty",
                      "adv", "sigma_day"):
            for fl in fls:
                if o.get(field) is None and fl.get(field) is not None:
                    o[field] = fl[field]
        o["fills"] = [{"datetime": fl["datetime"], "price": fl["price"],
                       "qty": fl["qty"], "fees_bps": fl.get("fees_bps"),
                       "benchmark_price": fl.get("benchmark_price")} for fl in fls]
        orders.append(o)
    return orders


def run_input(args) -> dict:
    fills = read_fills_csv(args.input)
    if not fills:
        raise SystemExit("未从 fills 解析到有效成交记录（需 symbol,side,datetime,price,qty）")
    orders = group_orders(fills)

    bars_by_sym: dict[str, list[dict]] = {}
    warnings: list[str] = []
    if args.bars:
        all_bars, warns = read_bars_csv(args.bars)
        warnings.extend(warns)
        for b in all_bars:
            bars_by_sym.setdefault(b["symbol"], []).append(b)

    degraded: list[str] = []
    all_degraded: list[str] = []
    order_costs, calib_rows = [], []
    compare_all = {"kind": args.benchmark, "window_minutes": args.window_minutes,
                   "per_order": []}
    for o in orders:
        sym = o["symbol"]
        bars = bars_by_sym.get(sym, [])
        cont = [b for b in bars if b["session"] == "continuous"] or bars

        # 到达价/期末价缺省：由 bars 补（成交前最近一根收盘 / 当日末根收盘）
        if o.get("arrival_price") is None and cont:
            dt0 = parse_dt(o["fills"][0]["datetime"])
            if dt0 is not None:
                o["arrival_price"] = arrival_from_bars(cont, dt0)
                if o["arrival_price"] is not None:
                    degraded.append(f"{sym}: 到达价缺省，用成交前最近分钟线收盘代理")
        if o.get("end_price") is None and cont:
            o["end_price"] = cont[-1].get("c")
            degraded.append(f"{sym}: 期末价缺省，用当日末根分钟线收盘代理")

        # 市场统计补：σ_day、ADV、点差代理
        if cont and o.get("sigma_day") is None:
            sd = realized_sigma_day(cont)
            if sd:
                o["sigma_day"] = round(sd, 6)
                degraded.append(f"{sym}: σ_day 缺省，由分钟线收益估计（{sd:.4f}）")
        if cont and o.get("adv") is None:
            # ADV = 当日对账窗总量（含两段集合竞价，与 --quantdb 的日线 ADV 同口径）
            day_vol = sum(b["v"] for b in bars if in_canon(b["hhmm"]))
            minutes_n = max(1, len(cont))
            if minutes_n < A_SHARE_MINUTES_PER_DAY * 0.5:
                min_avg = day_vol / minutes_n
                day_vol = max(day_vol, min_avg * A_SHARE_MINUTES_PER_DAY)
                degraded.append(f"{sym}: 仅覆盖部分时段，ADV 由分钟均量外推为整日")
            o["adv"] = day_vol
        if cont:
            o["spread_proxy_bps"] = spread_proxy_bps(cont)

        oc = decompose_order(o, k=args.k, fee_profile=args.fee_profile,
                             commission_bps=args.commission_bps)
        oc["degraded"].extend(degraded)
        all_degraded.extend(f"{o['symbol']}: {g}" for g in degraded)
        degraded = []
        order_costs.append(oc)

        # 逐笔对标 + 校准行
        cmp = _benchmark_compare(o, bars, args.benchmark, args.window_minutes,
                                 args.fee_profile)
        for f in cmp["per_fill"]:
            if f.get("slippage_bps") is not None and abs(f["slippage_bps"]) > 500:
                warnings.append(
                    f"{sym} {f['datetime']}: 滑点 {f['slippage_bps']:.0f} bps 远超常规——"
                    f"核对成交价与本地行情口径（复权/单位/代码/是否为示例数据）")
        for row, fl in zip(cmp["per_fill"], o["fills"], strict=True):
            if (row.get("slippage_bps") is not None and o.get("adv")
                    and fl["qty"] and float(o["adv"]) > 0):
                calib_rows.append({
                    "participation": fl["qty"] / float(o["adv"]),
                    "abs_slippage_bps": abs(row["slippage_bps"]),
                })
        compare_all["per_order"].append({"symbol": sym, "side": o["side"],
                                         "mean_slippage_bps": cmp["mean_slippage_bps"],
                                         "beat_rate": cmp["beat_rate"],
                                         "per_fill": cmp["per_fill"]})

    calibration = None
    if args.calibrate_k and calib_rows:
        sigma_ref = next((oc["market"]["sigma_day"] for oc in order_costs
                          if oc["market"]["sigma_day"]), None)
        calibration = calibrate_impact(calib_rows, sigma_ref)

    # 汇总（按订单名义额加权）
    tot_nt = sum(oc["qty"]["order"] * oc["prices"]["decision"] for oc in order_costs)
    agg = {}
    if tot_nt > 0:
        for key in ("delay", "impact", "timing", "opportunity", "fees", "total"):
            agg[key] = round(sum(oc["five_way_bps"][key] * oc["qty"]["order"]
                                 * oc["prices"]["decision"] for oc in order_costs)
                             / tot_nt, 4)

    return {
        "mode": "input",
        "input": str(Path(args.input).resolve()),
        "benchmark": args.benchmark,
        "fee_profile": args.fee_profile,
        "params": {"k": args.k, "window_minutes": args.window_minutes,
                   "commission_bps": args.commission_bps},
        "orders": order_costs,
        "aggregate": agg,
        "benchmark_compare": compare_all,
        "calibration": calibration,
        "warnings": warnings,
        "degraded": sorted(set(all_degraded)),
        "caveats": STATIC_CAVEATS,
    }


# ---------------------------------------------------------------- --quantdb 模式（容器 pandas）

def resolve_data_root() -> Path:
    env = os.environ.get("QM_DATA_ROOT")
    cands = ([Path(env)] if env else []) + [Path(p) for p in DATA_ROOT_CANDIDATES]
    for c in cands:
        if (c / "quantdb").is_dir():
            return c
    raise SystemExit("找不到 QuantDB 数据根目录（需含 quantdb/ 子目录）："
                     f"检查 {[str(c) for c in cands]}，或设置 QM_DATA_ROOT")


def _market_of(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith(CN_SUFFIXES) or (len(s) == 6 and s.isdigit()):
        return "CN"
    if s.endswith(".HK") or (len(s) == 4 and s.isdigit()):
        return "HK"
    return "US"


def load_min1(symbol: str, freq: str, root: Path):
    """读单标的分钟 parquet → (rows, meta)；pandas/pyarrow 延迟导入。"""
    import pandas as pd  # noqa: PLC0415  （容器内依赖）

    fname = f"{symbol.upper()}.parquet"
    path = root / "quantdb" / "1_kline_data" / f"min{freq}_kline" / fname
    if not path.exists():
        raise SystemExit(f"分钟数据文件不存在：{path}（确认后缀式代码与 min{freq} 覆盖）")
    df = pd.read_parquet(path)
    df = df.dropna(subset=["time"]).sort_values("time")
    cn = True
    rows = []
    for rec in df.itertuples(index=False):
        t = rec.time.to_pydatetime() if hasattr(rec.time, "to_pydatetime") else rec.time
        hhmm = t.strftime("%H:%M")
        rows.append({"hhmm": hhmm, "session": classify_bar(hhmm, cn),
                     "o": float(rec.open), "h": float(rec.high), "l": float(rec.low),
                     "c": float(rec.close), "v": float(rec.volume),
                     # CN 分钟 amount 单位为万元（实测），装载即 ×1e4 归一到元，
                     # 与 price×volume 同量纲（interval_vwap = Σamt/Σvol 直接得元/股）
                     "amt": float(rec.amount) * 1e4, "_t": t,
                     "t": t.strftime("%Y-%m-%d %H:%M:%S"),
                     "d": t.date().isoformat()})
    return rows


def load_daily_unadj(symbol: str, day_compact: str, root: Path):
    import pandas as pd  # noqa: PLC0415

    p = (root / "quantdb" / "1_kline_data" / "daily_unadjusted"
         / f"dt={day_compact}" / "data.parquet")
    if not p.exists():
        return None
    df = pd.read_parquet(p, filters=[("symbol", "=", symbol.upper())])
    return df.iloc[0].to_dict() if len(df) else None


def load_tick_spread(symbol: str, day_compact: str, root: Path):
    """真实盘口点差（bps）：tick 快照 bid1/ask1 中位数。仅个别日期有数据。"""
    import pandas as pd  # noqa: PLC0415

    fname = f"{symbol.upper().replace('.', '_')}_{day_compact}.parquet"
    p = root / "quantdb" / "1_kline_data" / "tick_data" / fname
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=["time", "bidPrice", "askPrice"])
    ts = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
    hhmm = ts.dt.strftime("%H:%M")
    mask = (hhmm >= "09:31") & (hhmm <= "14:57")
    bid = df["bidPrice"].str[0][mask]
    ask = df["askPrice"].str[0][mask]
    mid = (bid + ask) / 2.0
    sp = ((ask - bid) / mid * 1e4).dropna()
    if sp.empty:
        return None
    return {"n_ticks": int(len(sp)), "median_bps": float(sp.median()),
            "mean_bps": float(sp.mean()),
            "p25_bps": float(sp.quantile(0.25)), "p75_bps": float(sp.quantile(0.75))}


def run_quantdb(args) -> dict:
    symbol = args.symbol.upper()
    market = _market_of(symbol)
    if market != "CN":
        raise SystemExit(
            f"{symbol} 判定为 {market} 市场：本地**无港美股分钟数据**（quanthk/quantus 仅有日线），"
            "--quantdb 分钟基准模式仅支持 CN（600036.SH / 000001.SZ / 430047.BJ）。\n"
            "港美股成交复核请用 --input 模式（自备成交明细与基准价）。")
    if not args.date:
        raise SystemExit("--quantdb 模式需要 --date YYYY-MM-DD")
    day = parse_dt(args.date)
    if day is None:
        raise SystemExit(f"无法解析日期：{args.date}")
    root = resolve_data_root()
    rows = load_min1(symbol, args.freq, root)
    days = sorted({r["d"] for r in rows})
    if day.date().isoformat() not in days:
        raise SystemExit(f"{symbol} 分钟数据无 {day.date()}（覆盖 {days[0]} ~ {days[-1]}）")

    day_rows = [r for r in rows if r["d"] == day.date().isoformat()]
    canon = [r for r in day_rows if in_canon(r["hhmm"])]
    cont = [r for r in day_rows if r["session"] == "continuous"]
    odd = [r for r in day_rows if r["session"] in ("close_redundant", "afterhours", "other")]

    # ---- 当日基准 ----
    vwap_all = interval_vwap(canon)
    vwap_cont = interval_vwap(cont)
    twap_cont = interval_twap(cont)
    arrival = canon[0]["c"] if canon and canon[0]["session"] == "auction_open" else (
        cont[0]["o"] if cont else None)
    spread = spread_proxy_bps(cont)
    sigma_today = realized_sigma_day(cont)

    # ---- 前 N 日日频统计（ADV / σ，避免用当日猜测） ----
    prior_days = [d for d in days if d < day.date().isoformat()][-args.adv_days:]
    daily_vols, daily_rets = [], []
    for d in prior_days:
        dr = [r for r in rows if r["d"] == d and in_canon(r["hhmm"])]
        if not dr:
            continue
        daily_vols.append(sum(r["v"] for r in dr))
        cr = [r for r in rows if r["d"] == d and r["session"] == "continuous"]
        v = interval_vwap(cr)
        if v:
            daily_rets.append(v)
    adv = adv_from_days(daily_vols)
    sigma_prev = None
    if len(daily_rets) >= 5:
        rets = [math.log(daily_rets[i] / daily_rets[i - 1])
                for i in range(1, len(daily_rets))]
        if len(rets) >= 4:
            m = sum(rets) / len(rets)
            var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
            sigma_prev = math.sqrt(var) if var > 0 else None

    # ---- 场景：到达后均匀执行 ----
    scenario = None
    if vwap_all and twap_cont:
        d = dir_of(args.side)
        drift = d * (twap_cont - vwap_all) / vwap_all * 1e4
        scenario = {"side": args.side, "exec_price_assumption": round(twap_cont, 6),
                    "drift_vs_vwap_all_bps": round(drift, 4)}
        fee, fee_parts = fee_bps_for(args.side, symbol, args.fee_profile,
                                     args.commission_bps)
        scenario["fees_bps"] = fee
        scenario["fee_detail"] = fee_parts
        if spread:
            scenario["spread_proxy_median_bps"] = round(spread["median_bps"], 4)
        if args.qty:
            adv_use = adv or sum(r["v"] for r in canon)
            part = args.qty / adv_use if adv_use else None
            sigma_use = sigma_prev or sigma_today
            impact = (args.k * sigma_use * math.sqrt(part) * 1e4
                      if (part and sigma_use) else None)
            scenario.update({
                "qty": args.qty, "adv_use": round(adv_use, 0) if adv_use else None,
                "participation": round(part, 8) if part else None,
                "sigma_use": (round(sigma_use, 6) if sigma_use else None),
                "sigma_source": "前20日分钟收益" if sigma_prev else "当日分钟收益",
                "impact_bps": round(impact, 4) if impact is not None else None,
            })
            tot = drift + fee + (impact or 0.0)
            scenario["total_est_bps_ex_spread"] = round(tot, 4)
            if spread:
                scenario["total_est_bps_incl_spread_proxy"] = round(
                    tot + spread["median_bps"], 4)

    # ---- 口径核对（vs 日线不复权） ----
    recon = {"day": day.date().isoformat(), "bars": {
        "total": len(day_rows), "canon": len(canon), "continuous": len(cont),
        "anomalous": [{"hhmm": r["hhmm"], "session": r["session"], "volume": r["v"]}
                      for r in odd]}}
    daily = load_daily_unadj(symbol, day.strftime("%Y%m%d"), root)
    if daily:
        mv = sum(r["v"] for r in canon)
        ma = sum(r["amt"] for r in canon)          # 已归一到元
        dv, da = float(daily["volume"]), float(daily["amount"])   # daily amount 为万元
        recon["vs_daily_unadjusted"] = {
            "minute_volume": mv, "daily_volume": dv,
            "volume_dev_pct": (mv - dv) / dv * 100 if dv else None,
            "minute_amount_wan": ma / 1e4, "daily_amount_wan": da,
            "amount_dev_pct": (ma / 1e4 - da) / da * 100 if da else None,
            "vwap_minute": round(ma / mv, 6) if mv else None,
            "vwap_daily": round(da * 1e4 / dv, 6) if dv else None,
            "vwap_in_low_high": (float(daily["low"]) <= (ma / mv) <= float(daily["high"]))
            if mv else None,
            "daily_low": float(daily["low"]), "daily_high": float(daily["high"]),
        }
    else:
        recon["vs_daily_unadjusted"] = None
        recon["degraded"] = f"daily_unadjusted/dt={day.strftime('%Y%m%d')} 无此标的，跳过对账"

    # ---- 可选：真实盘口点差 ----
    tick = load_tick_spread(symbol, day.strftime("%Y%m%d"), root) if args.tick_check else None
    tick_block = None
    if args.tick_check:
        if tick and spread:
            tick_block = {"tick": tick, "minute_proxy_median_bps": round(spread["median_bps"], 4),
                          "proxy_over_half_spread_x": round(
                              spread["median_bps"] / (tick["median_bps"] / 2), 3)}
        elif tick:
            tick_block = {"tick": tick, "note": "无连续分钟 bar，未比较代理"}
        else:
            tick_block = {"tick": None,
                          "note": "该日无 tick 快照（本地仅 2026-05-11 / 2026-07-20 两日）"}

    return {
        "mode": "quantdb",
        "symbol": symbol,
        "date": day.date().isoformat(),
        "freq": args.freq,
        "benchmark": args.benchmark,
        "fee_profile": args.fee_profile,
        "params": {"k": args.k, "side": args.side, "qty": args.qty,
                   "adv_days": args.adv_days},
        "day_profile": {
            "benchmarks": {
                "vwap_all": vwap_all, "vwap_continuous": vwap_cont,
                "twap_continuous": twap_cont, "arrival_open": arrival,
                "spread_proxy_bps": spread,
                "sigma_day_today": sigma_today,
                "sigma_day_prev20": sigma_prev,
                "adv_prev20": adv,
            },
            "buckets": bucket_profile(canon),
            "uniform_exec_scenario": scenario,
        },
        "reconciliation": recon,
        "tick_check": tick_block,
        "caveats": STATIC_CAVEATS,
    }


# ---------------------------------------------------------------- 渲染

def _fmt_num(x, nd=4):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    return str(x)


def render_order(oc: dict) -> list[str]:
    fw = oc["five_way_bps"]
    lines = []
    sym = oc["symbol"]
    lines.append(f"── {sym} {SIDE_CN.get(oc['side'], oc['side'])} {oc['date']} "
                 f"（{oc['fills_n']} 笔成交，成交率 {oc['qty']['fill_ratio'] * 100:.1f}%）")
    lines.append(f"   决策价 {_fmt_num(oc['prices']['decision'])} → 到达价 "
                 f"{_fmt_num(oc['prices']['arrival'])} → 成交均价 "
                 f"{_fmt_num(oc['prices']['avg_fill'])} → 期末价 {_fmt_num(oc['prices']['end'])}")
    lines.append("   IS 五分解（订单名义额@决策价 bps，正=对本方不利）：")
    for key, cn in (("delay", "延迟"), ("impact", "冲击"), ("timing", "择时"),
                    ("opportunity", "机会"), ("fees", "费用")):
        lines.append(f"     {cn:<4} {fw[key]:>10.4f}")
    lines.append(f"     {'合计':<4} {fw['total']:>10.4f}   "
                 f"（对账残差 {fw['residual_bps']:.2e} bps，五项和−合计 "
                 f"{fw['five_sum'] - fw['total']:.4f}）")
    if oc["degraded"]:
        for g in oc["degraded"]:
            lines.append(f"   ⚠ {g}")
    return lines


def render_input(report: dict) -> str:
    L = ["=" * 68,
         f"交易成本分析（TCA）— 成交明细复核  [{report['fee_profile']} 费用口径]",
         f"基准 {BENCH_CN.get(report['benchmark'], report['benchmark'])}，"
         f"k={report['params']['k']}，窗口 ±{report['params']['window_minutes'] / 2:.0f} 分钟",
         "=" * 68]
    for oc in report["orders"]:
        L.append("")
        L += render_order(oc)
    agg = report.get("aggregate") or {}
    if agg:
        L.append("")
        L.append("▶ 汇总（按订单名义额加权）: " + "  ".join(
            f"{cn} {agg.get(k, 0):.2f}" for k, cn in
            (("delay", "延迟"), ("impact", "冲击"), ("timing", "择时"),
             ("opportunity", "机会"), ("fees", "费用"))) + f"  合计 {agg.get('total', 0):.2f} bps")
    cmp = report.get("benchmark_compare")
    if cmp:
        L.append("")
        L.append(f"▶ 逐笔对标（{BENCH_CN.get(cmp['kind'])}）")
        for po in cmp["per_order"]:
            if po["beat_rate"] is not None:
                head = (f"   {po['symbol']} {SIDE_CN.get(po['side'], po['side'])}: "
                        f"平均滑点 {_fmt_num(po['mean_slippage_bps'])} bps，"
                        f"超越基准率 {po['beat_rate'] * 100:.1f}%")
            else:
                head = f"   {po['symbol']}: 无基准价，无法对标"
            L.append(head)
            for f in po["per_fill"]:
                L.append(f"     {f['datetime']}  {f['price']} × {f['qty']:g}  "
                         f"基准 {_fmt_num(f['benchmark_price'])}  "
                         f"滑点 {_fmt_num(f.get('slippage_bps'), 2)} bps")
    cal = report.get("calibration")
    if cal:
        L.append("")
        if cal["status"] == "ok":
            L.append(f"▶ 参与率-滑点校准: slope={cal['slope_bps_per_sqrt_part']} bps/√参与率，"
                     f"截距={cal['intercept_bps']} bps，R²={cal['r2']}，n={cal['n_fills']}，"
                     f"参与率区间 [{cal['participation_min']:.2e}, {cal['participation_max']:.2e}]")
            if "implied_k" in cal:
                L.append(f"   隐含 k = {cal['implied_k']}（σ_day={cal['sigma_day_used']}）——"
                         f"仅供本批样本参考，勿外推")
        else:
            L.append(f"▶ 参与率-滑点校准: {cal['note']}")
    for w in report.get("warnings", []):
        L.append(f"⚠ {w}")
    L.append("")
    L.append("仅限本地研究使用；分析结论不构成投资建议。")
    return "\n".join(L)


def render_quantdb(report: dict) -> str:
    dp = report["day_profile"]
    b = dp["benchmarks"]
    L = ["=" * 68,
         f"交易成本分析（TCA）— QuantDB 分钟基准  {report['symbol']}  {report['date']}"
         f"（min{report['freq']}）",
         "=" * 68]
    rec = report["reconciliation"]
    bars = rec["bars"]
    L.append(f"分钟线：{bars['total']} 根（连续 {bars['continuous']} + 两段集合竞价；"
             f"计入日总量对账窗 {bars['canon']} 根，非常规 {len(bars['anomalous'])} 根）")
    if bars["anomalous"]:
        for a in bars["anomalous"][:5]:
            L.append(f"   ⚠ 非常规分钟 {a['hhmm']}（{a['session']}，量 {a['volume']:g}）"
                     f"——不计入日总量对账窗")
    L.append("")
    L.append("▶ 当日基准")
    L.append(f"   全日 VWAP（含竞价）   {_fmt_num(b['vwap_all'], 6)}")
    L.append(f"   连续 VWAP             {_fmt_num(b['vwap_continuous'], 6)}")
    L.append(f"   连续 TWAP             {_fmt_num(b['twap_continuous'], 6)}")
    L.append(f"   到达价（09:25 竞价）  {_fmt_num(b['arrival_open'], 6)}")
    if b["spread_proxy_bps"]:
        L.append(f"   点差代理（半幅中位）  {b['spread_proxy_bps']['median_bps']:.2f} bps"
                 f"（均值 {b['spread_proxy_bps']['mean_bps']:.2f}）")
    L.append(f"   σ_day：当日 {_fmt_num(b['sigma_day_today'], 4)}，"
             f"前 20 日 {_fmt_num(b['sigma_day_prev20'], 4)}；ADV(前20日) {_fmt_num(b['adv_prev20'], 0)} 股")
    L.append("")
    L.append("▶ 分时段流动性画像（半小时桶）")
    L.append(f"   {'时段':<14}{'量占比':>8}{'桶VWAP':>12}")
    for bk in dp["buckets"]:
        vwap_str = f"{bk['vwap']:>12.4f}" if bk["vwap"] is not None else f"{'—':>12}"
        L.append(f"   {bk['bucket']:<14}{bk['volume_share'] * 100:>7.1f}%{vwap_str}")
    sc = dp["uniform_exec_scenario"]
    if sc:
        L.append("")
        L.append(f"▶ 到达后均匀执行成本估计（side={SIDE_CN.get(sc['side'])}, "
                 f"假定均价≈连续 TWAP {_fmt_num(sc['exec_price_assumption'], 6)}）")
        L.append(f"   漂移（TWAP−全日VWAP）  {sc['drift_vs_vwap_all_bps']:+.2f} bps")
        if "participation" in sc:
            L.append(f"   冲击（k={report['params']['k']}, 参与率 "
                     f"{sc['participation'] * 100:.3f}%, σ={sc['sigma_use']}【{sc['sigma_source']}】）"
                     f"  {sc['impact_bps']:+.2f} bps")
        L.append(f"   费用（{report['fee_profile']}）  {sc['fees_bps']:+.2f} bps"
                 f"（佣金 {sc['fee_detail']['commission_bps']}"
                 + (f" + 印花税 {sc['fee_detail']['stamp_duty_bps']}"
                    if sc['fee_detail']['stamp_duty_bps'] else "")
                 + (f" + 过户费 {sc['fee_detail']['transfer_fee_bps']}"
                    if sc['fee_detail']['transfer_fee_bps'] else "") + "）")
        if "total_est_bps_ex_spread" in sc:
            L.append(f"   合计（不含点差代理）  {sc['total_est_bps_ex_spread']:+.2f} bps"
                     + (f"；含点差代理 {sc['total_est_bps_incl_spread_proxy']:+.2f} bps"
                        if "total_est_bps_incl_spread_proxy" in sc else ""))
    L.append("")
    L.append("▶ 口径核对（分钟 vs 日线不复权）")
    v = rec.get("vs_daily_unadjusted")
    if v:
        L.append(f"   分钟 Σvol {v['minute_volume']:,.0f} vs 日线 {v['daily_volume']:,.0f}"
                 f"（偏差 {v['volume_dev_pct']:+.4f}%）")
        L.append(f"   分钟 Σamount/Σvol = {v['vwap_minute']:.6f} vs 日线 amount/volume = "
                 f"{v['vwap_daily']:.6f}（分钟 amount 装载时已由万元归一到元）")
        L.append(f"   VWAP ∈ [当日 low {v['daily_low']}, high {v['daily_high']}]："
                 f"{'✓' if v['vwap_in_low_high'] else '✗ 异常'}")
    else:
        L.append("   ⚠ " + rec.get("degraded", "日线对账数据缺失"))
    if report.get("tick_check"):
        tc = report["tick_check"]
        if tc.get("tick"):
            L.append(f"▶ 真实盘口点差（tick bid1/ask1，{tc['tick']['n_ticks']} 快照）："
                     f"中位 {tc['tick']['median_bps']:.3f} bps")
            if "proxy_over_half_spread_x" in tc:
                L.append(f"   分钟高低幅代理 / 真实半价差 = {tc['proxy_over_half_spread_x']}×"
                         f"（代理高估，只作相对比较）")
        else:
            L.append(f"▶ 盘口点差: {tc.get('note', '不可用')}")
    L.append("")
    L.append("仅限本地研究使用；分析结论不构成投资建议。")
    return "\n".join(L)


def render_demo(report: dict) -> str:
    L = ["=" * 68, "交易成本分析（TCA）— demo（合成明细，五分解可手算）", "=" * 68]
    b = report["benchmarks"]
    L.append(f"合成分钟线 {report['bars_n']} 根：全日VWAP {b['vwap_all']:.6f}，"
             f"连续VWAP {b['vwap_continuous']:.6f}，连续TWAP {b['twap_continuous']:.6f}，"
             f"到达价(09:25) {b['arrival_open']}")
    L.append("")
    for oc in report["orders"]:
        L += render_order(oc)
        L.append("")
    cmp = report["benchmark_compare"]
    L.append(f"▶ 逐笔对标（{BENCH_CN.get(cmp['kind'])}，±{cmp['window_minutes'] / 2:.0f} 分钟窗口）"
             f"：平均滑点 {cmp['mean_slippage_bps']} bps，超越基准率 {cmp['beat_rate'] * 100:.1f}%")
    for f in cmp["per_fill"]:
        L.append(f"   {f['datetime'][11:]}  {f['price']} × {f['qty']:g}  "
                 f"基准 {f['benchmark_price']}  滑点 {f.get('slippage_bps')} bps")
    L.append("")
    L.append(f"✓ 手算校验：{len(report['handcheck']['items'])} 项全部一致（分数精确算术独立复算）")
    L.append("仅限本地研究使用；分析结论不构成投资建议。")
    return "\n".join(L)


def json_safe(x):
    if isinstance(x, Path):
        return str(x)
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, dict):
        return {k: json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    return x


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="交易成本分析（TCA）：IS 五分解 + VWAP/TWAP/到达价基准 + QuantDB(CN) 分钟基准",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="内置合成明细演示（纯标准库）")
    mode.add_argument("--input", help="成交明细 CSV：symbol,side,datetime,price,qty[,可选列]")
    mode.add_argument("--quantdb", action="store_true", help="QuantDB 分钟基准模式（容器内，仅 CN）")

    p.add_argument("--bars", help="分钟线 CSV（--input 可选）：symbol,datetime,open,high,low,close,volume[,amount]")
    p.add_argument("--symbol", help="标的（--quantdb）：后缀式 600036.SH / 000001.SZ")
    p.add_argument("--date", help="日期 YYYY-MM-DD（--quantdb）")
    p.add_argument("--freq", default="1", choices=["1", "5"], help="分钟频率（默认 1；5 分钟数据本地仅 5 天）")
    p.add_argument("--benchmark", default="vwap", choices=["vwap", "twap", "arrival"], help="执行基准（默认 vwap）")
    p.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES, help="区间基准窗口（默认 30）")
    p.add_argument("--fee-profile", default="broker", choices=["broker", "matching", "none"],
                   help="费用口径：broker=真单万2.5（默认）/ matching=回测保守万3 / none=不含费用")
    p.add_argument("--commission-bps", type=float, default=None, help="覆盖佣金 bps（含规费）")
    p.add_argument("--k", type=float, default=DEFAULT_IMPACT_K, help="square-root 冲击系数（默认 0.1）")
    p.add_argument("--calibrate-k", action="store_true", help="--input 模式：由成交数据拟合参与率-滑点")
    p.add_argument("--side", default="buy", choices=["buy", "sell"], help="--quantdb 均匀执行场景方向（默认 buy）")
    p.add_argument("--qty", type=float, default=None, help="--quantdb 场景订单量（股；给定则含冲击）")
    p.add_argument("--adv-days", type=int, default=20, help="ADV/σ 回看交易日数（默认 20）")
    p.add_argument("--tick-check", action="store_true", help="--quantdb：对照真实盘口点差（仅个别日期有 tick）")
    p.add_argument("--out", default=None, help="JSON 报告输出路径（父目录自动创建）")
    args = p.parse_args(argv)

    if args.demo:
        report = run_demo(args)
        text = render_demo(report)
    elif args.input:
        report = run_input(args)
        text = render_input(report)
    else:
        report = run_quantdb(args)
        text = render_quantdb(report)

    print(text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report = dict(report)
        report["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report["schema"] = "quantmind-tca/1"
        out_path.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n已写入 JSON 报告：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
