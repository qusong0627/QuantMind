"""因子概率板：个股四象限 + 板块三层共振判定（纯函数，含真值表）。

判定枚举（机读，前端映射位置词）：
- ``resonant_long``        共振看多（均分≥q67 ∧ 个股广度≥0.5 ∧ 模型广度≥0.55）
- ``resonant_short``       共振看空（均分≤q33 ∧ 个股广度≤0.5 ∧ 模型广度≤0.45）
- ``diverge_model_weak``   因子多·模型弱（观察）
- ``diverge_model_resilient`` 因子空·模型抗（关注反转）
- ``neutral``              其余 / 模型广度缺失不判定
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.shared.factor_board import config as C

VERDICT_LONG = "resonant_long"
VERDICT_SHORT = "resonant_short"
VERDICT_WEAK = "diverge_model_weak"
VERDICT_RESILIENT = "diverge_model_resilient"
VERDICT_NEUTRAL = "neutral"


def sector_table(base: pd.DataFrame, min_n: int = C.SECTOR_MIN_N) -> pd.DataFrame:
    """按板块聚合：均分/中位/个股广度/模型广度/模型覆盖。

    ``base``：index=symbol，列含 composite/mdl_rank 及 SectorType/SectorName。
    """
    g = base.groupby("SectorName").agg(
        n=("composite", "size"),
        # 有评分成分数（composite 非空）；min_n 必须按它卡，否则「8 只成分里
        # 只有 2 只有分」也会产出板块 verdict，广度被少数样本主导。
        n_scored=("composite", "count"),
        fac=("composite", "mean"),
        med=("composite", "median"),
        # 广度分母只算有评分成分：NaN 在 (s>0) 里是 False，会把分母放大、
        # 系统性低估广度（旧实现 bug）。
        fbreadth=(
            "composite",
            lambda s: float((s.dropna() > 0).mean()) if s.notna().any() else np.nan,
        ),
        mbreadth=(
            "mdl_rank",
            lambda s: float((s.dropna() >= 0.5).mean()) if s.notna().any() else np.nan,
        ),
        model_cov=("mdl_rank", lambda s: float(s.notna().mean())),
    )
    return g[(g["n"] >= min_n) & (g["n_scored"] >= min_n)]


def classify_sector(
    fac: float | None,
    fbreadth: float | None,
    mbreadth: float | None,
    q67: float,
    q33: float,
) -> str:
    """三层判定真值表（逐行纯函数，金样锁定）：

    - 均分缺失 → neutral；个股广度缺失 → neutral
    - 模型广度缺失（无模型覆盖）→ neutral（**不判定**，绝不猜）
    - 各阈值比较含边界（≥/≤，与 v2 原型一致）
    """
    if fac is None or fbreadth is None or not np.isfinite(fac):
        return VERDICT_NEUTRAL
    if not np.isfinite(fbreadth):
        return VERDICT_NEUTRAL
    mb_known = mbreadth is not None and np.isfinite(mbreadth)
    hi = fac >= q67
    lo = fac <= q33
    fb_hi = fbreadth >= C.FB_HI
    fb_lo = fbreadth <= C.FB_LO
    if hi and fb_hi:
        if not mb_known:
            return VERDICT_NEUTRAL
        return VERDICT_LONG if mbreadth >= C.MB_HI else VERDICT_WEAK
    if lo and fb_lo:
        if not mb_known:
            return VERDICT_NEUTRAL
        return VERDICT_SHORT if mbreadth <= C.MB_LO else VERDICT_RESILIENT
    return VERDICT_NEUTRAL


def apply_verdicts(
    g: pd.DataFrame,
    *,
    q_hi: float = C.VERDICT_Q_HI,
    q_lo: float = C.VERDICT_Q_LO,
) -> tuple[pd.DataFrame, float, float]:
    """对板块聚合表逐行套真值表（quantile 由本表自身分位决定）。"""
    q67 = float(g["fac"].quantile(q_hi))
    q33 = float(g["fac"].quantile(q_lo))
    out = g.copy()
    out["verdict"] = [
        classify_sector(_f(r["fac"]), _f(r["fbreadth"]), _f(r["mbreadth"]), q67, q33)
        for _, r in out.iterrows()
    ]
    return out, q67, q33


def _f(x) -> float | None:
    """NaN/None → None（喂给 classify_sector 的缺失语义）。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def quadrant_frame(
    stock: pd.DataFrame,
    *,
    hi: float = C.QUADRANT_HI,
    lo: float = C.QUADRANT_LO,
) -> pd.DataFrame:
    """个股四象限（因子分位 × 模型分位），仅限 composite 与 mdl_rank 均非空。"""
    ok = stock["composite"].notna() & stock["mdl_rank"].notna()
    q = stock[ok].copy()
    th_f = stock["composite"].quantile(hi)
    th_m = stock["composite"].quantile(lo)
    q["fq"] = np.where(
        q["composite"] >= th_f, "H", np.where(q["composite"] <= th_m, "L", "M")
    )
    q["mq"] = np.where(
        q["mdl_rank"] >= hi, "H", np.where(q["mdl_rank"] <= lo, "L", "M")
    )
    q["quadrant"] = q["fq"] + q["mq"]
    return q


def quadrant_counts(q: pd.DataFrame) -> dict[str, int]:
    """九象限计数（HH/H M/…），键为两字母组合。"""
    counts = q.groupby(["fq", "mq"]).size()
    return {f"{f}{m}": int(counts.get((f, m), 0)) for f in "HLM" for m in "HLM"}


def spearman(a: pd.Series, b: pd.Series) -> float | None:
    """rank 相关（两序列需已对齐且非空）。"""
    if len(a) < 10:
        return None
    rho = a.rank().corr(b.rank())
    return None if pd.isna(rho) else float(rho)


def stock_lists(stock: pd.DataFrame, n: int = C.STOCK_LIST_N) -> dict[str, list[dict]]:
    """产物名单：top/bottom（按 composite，**不要求模型覆盖**）+ 共振多/空（HH/LL，剔 ST）。

    每项字段固定：symbol/name/composite/model_rank/quadrant/sector_l2。
    ``stock`` 须含 composite/mdl_rank/st/name/sector_l2/quadrant（quadrant 可以是
    NaN——无模型覆盖的票仍应进 top/bottom，只是象限显示 —）。旧实现只传
    ``quadrant_frame`` 的 ``q``（已按双轴非空过滤），导致「合成分位 Top50」把
    无模型覆盖的票整段丢掉；与页面「按合成分排序」口径不符。

    P2-d 掩码过滤（列缺失时恒不生效，旧调用方/测试零扰动）：多头侧名单
    （top/共振多）剔除 ``blocked_long``（当日涨停/停牌等不可买入），空头侧
    （bottom/共振空）剔除 ``blocked_short``；被剔数在
    ``excluded_blocked_long/short`` 显形（**不落 0 之外的口径**：无行即无剔除）。
    """
    g = stock[~stock["st"].fillna(False)]
    if "blocked_long" in g.columns:
        bl_l = g["blocked_long"].fillna(False).astype(bool)
    else:
        bl_l = pd.Series(False, index=g.index)
    if "blocked_short" in g.columns:
        bl_s = g["blocked_short"].fillna(False).astype(bool)
    else:
        bl_s = pd.Series(False, index=g.index)
    g_long, g_short = g[~bl_l], g[~bl_s]

    def _rows(frame: pd.DataFrame) -> list[dict]:
        out = []
        for sym, r in frame.iterrows():
            sector = r.get("sector_l2")
            out.append(
                {
                    "symbol": str(sym),
                    "name": None if pd.isna(r.get("name")) else str(r["name"]),
                    "composite": _r(r["composite"]),
                    "model_rank": _r(r["mdl_rank"]),
                    "quadrant": None if pd.isna(r.get("quadrant")) else str(r["quadrant"]),
                    "sector_l2": None
                    if sector is None or pd.isna(sector)
                    else str(sector),
                }
            )
        return out

    top = g_long.sort_values("composite", ascending=False).head(n)
    bottom = g_short.sort_values("composite").head(n)
    hh = (
        g_long[g_long["quadrant"] == "HH"]
        .sort_values("composite", ascending=False)
        .head(n)
    )
    ll = g_short[g_short["quadrant"] == "LL"].sort_values("composite").head(n)
    return {
        "top": _rows(top),
        "bottom": _rows(bottom),
        "resonant_long": _rows(hh),
        "resonant_short": _rows(ll),
        "excluded_blocked_long": int(bl_l.sum()),
        "excluded_blocked_short": int(bl_s.sum()),
    }


def _r(x, nd: int = 4):
    """缺省语义：NaN/None → None（绝不落 0）。"""
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return round(float(x), nd)


def members_index(
    stock: pd.DataFrame, members: pd.DataFrame, stype: str
) -> dict[str, list[dict]]:
    """板块 → 成分明细（钻取件，板块名原样作键）。

    ``stock``：index=symbol（后缀式），列含 composite/name/mdl_rank，``quadrant``
    列由调用方先合入（双轴缺一即 NaN → 落 null）。**含 ST（名单层才剔）与无模型
    覆盖标的**——钻取展示板块全量成员，不是名单。板块内按 composite 降序（缺失
    垫底）。多对多（概念）与 sector_payload 同口径：同一标的可属多个板块。
    """
    sm = members[members["SectorType"] == stype]
    if sm.empty:
        return {}
    base = stock.join(
        sm.rename(columns={"Symbol": "symbol"}).set_index("symbol")["SectorName"],
        how="inner",
    )
    out: dict[str, list[dict]] = {}
    for name, g in base.groupby("SectorName", sort=False):
        g = g.sort_values("composite", ascending=False, na_position="last")
        rows = []
        for sym, r in g.iterrows():
            rows.append(
                {
                    "symbol": str(sym),
                    "name": None if pd.isna(r["name"]) else str(r["name"]),
                    "composite": _r(r["composite"]),
                    "model_rank": _r(r["mdl_rank"]),
                    "quadrant": None
                    if pd.isna(r.get("quadrant"))
                    else str(r["quadrant"]),
                }
            )
        out[str(name)] = rows
    return out
