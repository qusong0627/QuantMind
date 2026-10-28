"""资金流质量分（订单流结构）—— 研究口径的唯一实现。

实测结论（2022-01~2026-10，L2 日频，行业中性，剔除 ST/低价/低成交/小市值，
t 日收盘信号 → t+1 收盘建仓）：

- **主力净流入金额 / 净额比本身没有稳定预测力**：分位→之后收益近乎平坦；
  高净流入 + 已大涨反而在 10~20 日跑输（追高资金流是反向指标）。
- 真正有增量的是**订单流结构**：
  - 成交方向一致性（trade sign autocorr）越高 → 之后 5~20 日越强；
  - VPIN 波动比（知情交易/波动）、成交间隔（越慢）→ 正向；
  - 卖压、撤单深度分布、VPIN 冲击、大单占比越高 → 之后越弱。

分数 = 各分项**当日截面分位**（只在可交易宇宙内排名，0~1）按符号等权平均 ×100。
缺失分项不插补：任一必需列缺失，该股当日分数为 NaN（展示层显示 —，绝不回落成 0）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Component:
    column: str
    sign: int
    label: str
    note: str


#: 顺序即展示顺序；sign=+1 表示“分位越高越好”
COMPONENTS: tuple[Component, ...] = (
    Component(
        "flow_consistency",
        +1,
        "成交方向一致性",
        "日内主动买卖方向的一致性（≈成交方向自相关）",
    ),
    Component(
        "micro_vpin_vol_ratio",
        +1,
        "VPIN 波动比",
        "知情交易概率（VPIN）相对波动的比率",
    ),
    Component(
        "micro_trade_interval_mean",
        +1,
        "成交间隔",
        "平均成交间隔（越大＝成交越慢、越少高频拥挤）",
    ),
    Component(
        "micro_trade_sell_pressure",
        -1,
        "卖压",
        "主动卖出压力",
    ),
    Component(
        "flow_cancel_depth_dist",
        -1,
        "撤单深度分布",
        "撤单在订单簿深度上的分布（越高＝挂单撤退越明显）",
    ),
    Component(
        "micro_vpin_zscore_20",
        -1,
        "VPIN 冲击",
        "VPIN 相对自身 20 日的 z 值（越高＝毒性冲击越大）",
    ),
    Component(
        "flow_big_trade_ratio",
        -1,
        "大单占比",
        "大单成交占总成交的比例（越高＝大单拥挤/派发风险）",
    ),
)

COMPONENT_COLUMNS: tuple[str, ...] = tuple(c.column for c in COMPONENTS)


def rank_pct(frame: pd.DataFrame, mask: pd.DataFrame | None = None) -> pd.DataFrame:
    """逐日截面分位（0~1，当日最低=0、最高=1）；``mask`` 外不参与排名、自身 NaN。

    pandas 的 ``pct=True`` 下限是 1/n 而不是 0；这里做一次按日 min-max 归一，
    让「最差 0 分 / 最好 100 分」在展示面成立（同一交易日内各分项用同一变换，
    不改变复合分的排序）。
    """
    data = frame.where(mask) if mask is not None else frame
    r = data.rank(axis=1, pct=True, na_option="keep")
    n = data.notna().sum(axis=1)
    denom = (1.0 - 1.0 / n).where(n > 1)
    out = r.sub(1.0 / n, axis=0).div(denom, axis=0)
    if (n <= 1).any():
        out.loc[n <= 1, :] = 0.5
    return out


def _check(features: Mapping[str, pd.DataFrame]) -> None:
    missing = [c for c in COMPONENT_COLUMNS if c not in features]
    if missing:
        raise KeyError(f"资金流质量分缺分项: {missing}")


def component_percentiles(
    features: Mapping[str, pd.DataFrame], mask: pd.DataFrame | None = None
) -> dict[str, pd.DataFrame]:
    """各分项逐日截面分位（feature → 宽表 日期×标的）。缺项显式报错，不静默补 0。"""
    _check(features)
    return {c: rank_pct(features[c], mask) for c in COMPONENT_COLUMNS}


def quality_score(
    features: Mapping[str, pd.DataFrame], mask: pd.DataFrame | None = None
) -> pd.DataFrame:
    """资金流质量分（0~100）；任一必需分项缺失则该样本为 NaN。

    ``features`` 是 ``{列名: 宽表(日期×标的)}``；``mask`` 之外的样本不参与
    截面排名、自身为 NaN（ST/低价/低成交/小市值在调用方先剔除）。
    """
    pct = component_percentiles(features, mask)
    acc = None
    for comp in COMPONENTS:
        part = pct[comp.column] * comp.sign
        acc = part if acc is None else acc + part
    assert acc is not None
    # 有符号和的理论区间是 [-负向项数, +正向项数]：仿射映射到 0~100（50=中性）。
    # 只是展示变换，分位排序不变。
    lo = -sum(1 for c in COMPONENTS if c.sign < 0)
    hi = sum(1 for c in COMPONENTS if c.sign > 0)
    return (acc - lo) / (hi - lo) * 100.0
