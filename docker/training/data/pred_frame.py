"""全量预测 DataFrame 构造（树模型与 DL 共用，保证图表/评估口径一致）。

背景（2026-11-28）：DLAdapter 原先只用 ``symbol/trade_date/label`` 造 pred_df，
不带 ``label_return``/OHLC/中性化控制列。多模型训练若主模型是 DL，父
pred.parquet 只有 6 列，导致 eval_report 退化为 ``label_rank``、
``tradability=False``、``residual_ic=None``，前端缺图。
"""

from __future__ import annotations

import pandas as pd

from data.eval_columns import eval_extra_cols


def merge_full_pred(
    df: pd.DataFrame,
    y_full_pred: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> pd.DataFrame:
    """把全量预测按 (symbol, trade_date) 合回带评估列的全量帧。

    与树模型主路径 ``_fp_cols = ['symbol','trade_date','label'] +
    _eval_extra_cols(df)`` 同口径；DL 与树模型的 pred_{type}.parquet 列集合
    从此一致。
    """
    cols = list(
        dict.fromkeys(["symbol", "trade_date", "label", *eval_extra_cols(df)])
    )
    full = df[cols].copy()
    full = full.merge(
        y_full_pred[["symbol", "trade_date", "pred"]],
        on=["symbol", "trade_date"],
        how="left",
    )
    full["split"] = "train"
    full.loc[
        (full["trade_date"] >= val_df["trade_date"].min())
        & (full["trade_date"] <= val_df["trade_date"].max()),
        "split",
    ] = "valid"
    full.loc[
        (full["trade_date"] >= test_df["trade_date"].min())
        & (full["trade_date"] <= test_df["trade_date"].max()),
        "split",
    ] = "test"
    return full
