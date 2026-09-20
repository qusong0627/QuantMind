"""市场广度与涨跌停判定纯函数（单一事实源，被复盘脚本与市场分析共用）。

涨跌停规则复用 backend/services/trade/simulation/services/local_market_data.py
（compute_limits / limit_pct，与 instrument_detail ZTPrice/DTPrice 交叉验证 99.71% 一致）。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from backend.services.simulation.services.local_market_data import (
    LIMIT_TOLERANCE,
    LIMIT_TOLERANCE_BSE,
    compute_limits,
    limit_pct,
)

# 容差（百分点）：唯一事实源是 local_market_data 的比例形态，此处只做单位换算。
# 不在本模块另立数值 —— 曾有第三份副本（scripts/review_stats.py）就是这样漂移的。
TOL_SHSZ = LIMIT_TOLERANCE * 100
TOL_BJ = LIMIT_TOLERANCE_BSE * 100

CAT_LIMIT_UP = "limit_up"
CAT_LIMIT_DOWN = "limit_down"
CAT_BROKE_UP = "broke_up"
CAT_CORP_ACTION = "corp_action"
CAT_NORMAL = "normal"
CAT_UP = "up"
CAT_DOWN = "down"
CAT_FLAT = "flat"


def is_bse_symbol(symbol: str) -> bool:
    code = symbol.partition(".")[0]
    return symbol.endswith(".BJ") or code[:2] in ("43", "83", "87", "88", "92")


def price_tolerance(symbol: str) -> float:
    """价格比较容差（元）：比较涨停价时允许的分位浮点误差。"""
    return 0.004


def classify_price(
    close: float, high: float, up_price: float, down_price: float
) -> str:
    """按价格精确判定：收盘封板 / 炸板 / 跌停 / 普通（方向由调用方按 pct 符号归 up/down/flat）。"""
    if up_price > 0:
        if close >= up_price - price_tolerance("600000.SH"):
            return CAT_LIMIT_UP
        if high >= up_price - price_tolerance("600000.SH"):
            return CAT_BROKE_UP
    if down_price > 0 and close <= down_price + price_tolerance("600000.SH"):
        return CAT_LIMIT_DOWN
    return CAT_NORMAL


def is_corp_action_pct(pct: float, board_pct: float) -> bool:
    """涨跌幅显著超过板块限制 → 除权/拆并股等公司行为（非交易性波动）。"""
    return abs(pct) > board_pct * 100 + 1.0


def classify_by_pct(pct: float, symbol: str, is_st: bool, trade_date: date) -> str:
    """按涨跌幅 + 容差兜底判定（除权日昨收不可信时用）。"""
    board = float(limit_pct(symbol, is_st=is_st, trade_date=trade_date)) * 100
    tol = TOL_BJ if is_bse_symbol(symbol) else TOL_SHSZ
    if pct >= board - tol:
        return CAT_LIMIT_UP
    if pct <= -(board - tol):
        return CAT_LIMIT_DOWN
    if pct > 0:
        return CAT_UP
    if pct < 0:
        return CAT_DOWN
    return CAT_FLAT


def limit_up_down_counts(
    pct: pd.Series,
    symbols: pd.Series,
    *,
    trade_date: date,
    st_symbols: frozenset[str] | set[str] | None = None,
) -> tuple[int, int]:
    """全市场涨停/跌停家数（单一事实源，市场分析页与快照脚本共用）。

    与 :func:`classify_by_pct` 同口径，只是把「逐行」换成「按 unique symbol
    先算好带宽再查表」—— 全市场 5500 行逐行调 limit_pct 要走 5500 次
    Decimal 运算，而 unique 后同样多，真正省掉的是重复的 Series 索引开销。

    **为什么必须换掉 `pct >= 9.8`**：这是一条写死的主板线，两个方向都错 ——
    20%/30% 板上任何 +9.8% 以上的普通阳线都被计成涨停（家数虚高），
    而 ST 5% 板上真封死的票（+5.0%）永远漏计。市场分析页显示的「涨停 N 家」
    因此既偏高又偏低，取决于当日哪个板块活跃。

    容差走 TOL_SHSZ/TOL_BJ（沪深 0.5pp、北交所 1pp），与 daily_review 一致。
    """
    st = set(st_symbols or ())
    band = {
        s: float(limit_pct(str(s), is_st=str(s) in st, trade_date=trade_date)) * 100
        - (TOL_BJ if is_bse_symbol(str(s)) else TOL_SHSZ)
        for s in pd.unique(symbols)
    }
    up = down = 0
    # strict=True：长度不等说明调用点传错了（如 pct 已被过滤），宁可报错也
    # 不要静默截断 —— 截断会让家数偏小且看不出原因。
    for p, s in zip(pct.tolist(), symbols.tolist(), strict=True):
        b = band.get(s, 0.0)
        # NaN 与「无涨跌幅限制」（新股首日等，带宽 ≤ 0）都不计入
        if b <= 0 or p != p:
            continue
        if p >= b:
            up += 1
        elif p <= -b:
            down += 1
    return up, down


def is_ex_div(official_pct: float, close: float, prev_close: float) -> bool:
    """除权除息日检测：官方 pct_change 与 (close/prev_close-1) 自算值差 > 0.5%。"""
    if prev_close is None or prev_close <= 0 or close is None:
        return False
    self_pct = (close / prev_close - 1) * 100
    return abs(official_pct - self_pct) > 0.5


def streak_from_tail(days: list[float], min_pct: float) -> int:
    """从最近一日（列表尾部）往前数，连续 ≥ min_pct 的天数。"""
    n = 0
    for v in reversed(days):
        if v is not None and v >= min_pct:
            n += 1
        else:
            break
    return n


_LABELS = ["涨停", ">7", "5~7", "3~5", "1~3", "0~1", "平盘",
           "-1~0", "-3~-1", "-5~-3", "-7~-5", "<-7", "跌停"]


def breadth_distribution(pct: pd.Series, limit_thresh: float = 9.7) -> dict[str, int]:
    """涨跌幅分布直方图（±limit_thresh 视为涨停/跌停近似桶）。"""
    dist: dict[str, int] = dict.fromkeys(_LABELS, 0)
    for v in pct.dropna():
        if v >= limit_thresh:
            dist["涨停"] += 1
        elif 7.0 <= v < limit_thresh:
            dist[">7"] += 1
        elif 5.0 <= v < 7.0:
            dist["5~7"] += 1
        elif 3.0 <= v < 5.0:
            dist["3~5"] += 1
        elif 1.0 <= v < 3.0:
            dist["1~3"] += 1
        elif 0.0 < v < 1.0:
            dist["0~1"] += 1
        elif v == 0.0:
            dist["平盘"] += 1
        elif -1.0 < v < 0.0:
            dist["-1~0"] += 1
        elif -3.0 < v <= -1.0:
            dist["-3~-1"] += 1
        elif -5.0 < v <= -3.0:
            dist["-5~-3"] += 1
        elif -7.0 < v <= -5.0:
            dist["-7~-5"] += 1
        elif -limit_thresh < v <= -7.0:
            dist["<-7"] += 1
        else:
            dist["跌停"] += 1
    return dist


def market_breadth(pct: pd.Series) -> dict:
    """涨跌家数与涨跌比。"""
    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    ratio = round(up / down, 2) if down else None
    return {"up_count": up, "down_count": down, "flat_count": flat, "up_down_ratio": ratio}


def sector_aggregate(
    members: pd.DataFrame,
    pct: pd.Series,
    mv: pd.Series | None = None,
) -> pd.DataFrame:
    """板块表现聚合：成员 (SectorCode, SectorName, SectorType, Symbol) × 个股涨跌幅。"""
    cols = ["SectorCode", "SectorName", "SectorType", "Symbol"]
    m = (
        members[cols]
        .drop_duplicates(subset=["SectorCode", "Symbol"])
        .set_index("Symbol")
        .join(pct.rename("pct"), how="inner")
    )
    if m.empty:
        return pd.DataFrame(
            columns=["SectorCode", "SectorName", "SectorType", "n", "avg_pct",
                     "mv_weighted_pct", "ignored"]
        )
    if mv is not None:
        m = m.join(mv.rename("mv"), how="left")

    rows = []
    for (sec_code, sec_name, sec_type), g in m.groupby(["SectorCode", "SectorName", "SectorType"]):
        avg = float(g["pct"].mean())
        if mv is not None and g["mv"].notna().mean() >= 0.6:
            w = g["mv"].dropna()
            weighted = round(float((g.loc[w.index, "pct"] * w).sum() / w.sum()), 2)
        else:
            weighted = None
        rows.append(
            {
                "SectorCode": sec_code,
                "SectorName": sec_name,
                "SectorType": sec_type,
                "n": len(g),
                "avg_pct": round(avg, 2),
                "mv_weighted_pct": weighted,
                "ignored": 0,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(
            columns=["SectorCode", "SectorName", "SectorType", "n", "avg_pct",
                     "mv_weighted_pct", "ignored"]
        )
    return out.sort_values("avg_pct", ascending=False).reset_index(drop=True)


def wan_to_yi(value_wan: float | None) -> float | None:
    """万元 → 亿元。"""
    if value_wan is None:
        return None
    return value_wan / 1e4


def fmt_yi(value_wan: float | None) -> str:
    """万元 → 亿元格式化。"""
    yi = wan_to_yi(value_wan)
    if yi is None:
        return "[数据缺失]"
    return f"{yi:,.2f} 亿元"


def volume_ratio_5(current: float | None, prior_amounts: list[float]) -> float | None:
    """量比：当日 / 前 5 日成交额均值。"""
    usable = [a for a in prior_amounts if a is not None and a > 0]
    if current is None or current <= 0 or not usable:
        return None
    return round(current / (sum(usable) / len(usable)), 2)
