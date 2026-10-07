"""QuantDB l2_factors 资金流金额单位归一。

QuantDB ``l2_factors`` 的 ``flow_*`` 金额单位**按分区混存**，不能假定常量
（本机实测：2026-09-21 起为**万元**，``flow_net_amount / amount ≈
flow_net_ratio``；2023-01 ~ 2026-09-18 为**元**，``flow_net_amount /
(amount × 1e4) ≈ flow_net_ratio``；2018/2020 年早期分区亦为万元）。
同表 ``amount`` 恒为万元，因此「比率恒等式在哪一边成立」可逐分区判别单位。

下游市场分析 / 投研 / 终端约定仍按**元**消费（再 /1e8 转亿元、/1e6 转百万元）。
本模块在读入边界把金额列归一为元；按 ``dt`` 分组自动识别新旧分区，
跨单位分区的窗口（趋势序列）不会被单一常量化错，混存时也不会二次放大。
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

# 金额类列（需乘 1e4 才能从万元还原为元）；比率类列不动
FLOW_MONEY_COLS: tuple[str, ...] = (
    "flow_net_amount",
    "flow_buy_amount",
    "flow_sell_amount",
    "flow_super_net",
    "flow_large_net",
    "flow_medium_net",
    "flow_small_net",
)

_WAN_TO_YUAN = 1e4
# 无 amount 可对账时：全市场单日净流入中位数 < 该阈值 → 视为万元
# （实测万元日 |flow| 中位数 ~1e3，元日 ~1e7，1e5 可干净分离）
_WAN_MEDIAN_ABS_HINT = 1.0e5


def _median_abs_err(a: pd.Series, b: pd.Series) -> float:
    err = (a - b).abs()
    err = err[err.notna()]
    if err.empty:
        return float("inf")
    return float(err.median())


def detect_flow_money_scale_to_yuan(df: pd.DataFrame) -> float:
    """返回把当前金额列乘到「元」所需的系数：万元→1e4，已是元→1.0。"""
    if df is None or df.empty or "flow_net_amount" not in df.columns:
        return 1.0

    flow = pd.to_numeric(df["flow_net_amount"], errors="coerce")
    ratio = (
        pd.to_numeric(df["flow_net_ratio"], errors="coerce")
        if "flow_net_ratio" in df.columns
        else None
    )
    amount = (
        pd.to_numeric(df["amount"], errors="coerce") if "amount" in df.columns else None
    )

    if ratio is not None and amount is not None:
        mask = flow.notna() & ratio.notna() & amount.notna() & (amount > 0)
        if int(mask.sum()) >= 20:
            f, r, a = flow[mask], ratio[mask], amount[mask]
            err_wan = _median_abs_err(f / a, r)
            err_yuan = _median_abs_err(f / (a * _WAN_TO_YUAN), r)
            # 万元口径误差应显著更小
            if err_wan < err_yuan * 0.5:
                return _WAN_TO_YUAN
            if err_yuan < err_wan * 0.5:
                return 1.0

    med = float(flow.abs().median()) if flow.notna().any() else 0.0
    if med > 0 and med < _WAN_MEDIAN_ABS_HINT:
        return _WAN_TO_YUAN
    return 1.0


def normalize_l2_flow_money_to_yuan(
    df: pd.DataFrame,
    *,
    cols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """就地/拷贝：将 flow 金额列归一为元。按 ``dt`` 分组识别（兼容混日分区）。"""
    if df is None or df.empty:
        return df

    money_cols = [c for c in (cols or FLOW_MONEY_COLS) if c in df.columns]
    if not money_cols:
        return df

    out = df.copy()
    if "dt" in out.columns and out["dt"].nunique(dropna=True) > 1:
        pieces: list[pd.DataFrame] = []
        for _, grp in out.groupby("dt", sort=False):
            scale = detect_flow_money_scale_to_yuan(grp)
            if scale != 1.0:
                for c in money_cols:
                    out_col = pd.to_numeric(grp[c], errors="coerce") * scale
                    grp = grp.assign(**{c: out_col})
            pieces.append(grp)
        return pd.concat(pieces, ignore_index=True)

    scale = detect_flow_money_scale_to_yuan(out)
    if scale != 1.0:
        for c in money_cols:
            out[c] = pd.to_numeric(out[c], errors="coerce") * scale
    return out
