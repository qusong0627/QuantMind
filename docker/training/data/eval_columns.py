"""评估/闸门需要的附加列（训练端与 DL 适配器共用，纯 pandas）。

这些列不参与主训练目标，只随 pred_df 落盘，供：
- eval_report 的真实收益口径（label_return/open/close/label_vol）；
- 可交易性过滤（次日涨跌停）；
- residual IC 中性化控制变量（size/vol/momentum/turnover）；
- horizon curve（label_return_h1/h2/h3/h5/h10）。
"""

from __future__ import annotations

import pandas as pd

EVAL_EXTRA_CANDIDATES = (
    "label_return",
    # 可交易性过滤（次日开盘/收盘涨跌停）用的原始价，只进评估、不进特征
    "open",
    "close",
    "label_vol",
    "total_cap",
    "fun_total_mv",
    "fun_float_mv",
    "amt_log",
    "amt_z_20",
    "vol_20",
    "vol_std_20",
    "vol_amp_20",
    "mom_20",
    "mom_ret_20d",
    "mom_ret_60d",
    "turnover",
    "turnover_20",
    "turn_20",
    "turn_z_20",
)


def eval_extra_cols(df: pd.DataFrame) -> list[str]:
    """pred_df 要携带的评估列：真实收益/波动/多周期收益/中性化控制变量。"""
    cols = [c for c in EVAL_EXTRA_CANDIDATES if c in df.columns]
    cols.extend(c for c in df.columns if str(c).startswith("label_return_h"))
    return list(dict.fromkeys(cols))
