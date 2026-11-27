"""WFA 窗口切分（纯函数，供 walk-forward 诊断与测试共用）。

与主训练切分同一条纪律：train 段尾部必须裁掉 `target_horizon_days +
_EXECUTION_LAG_DAYS` 个交易日。不裁则 train 末尾样本的标签落在 val 窗口内，
WFA 的稳定性/参数漂移结论会被前视污染。

本模块只依赖 pandas/numpy；`diagnostics/wfa.py` 负责训练与指标汇总。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("quantmind.train")


def split_wfa_window(
    df: pd.DataFrame,
    wfa: dict,
    idx: int,
    *,
    embargo_days: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构造第 idx 个 WFA 窗口的 (train_df, val_df)。

    基于 df 中的实际交易日历（trade_date）推进，避免节假日/停牌导致窗口稀疏。
    rolling:  训练起点 = 验证起点 - train_years 年（按交易日回退），训练集长度固定。
    expanding:训练起点 = 数据最早日期，训练集随 idx 扩张。

    ``embargo_days``：train 尾部裁掉的交易日数（= target_horizon_days +
    _EXECUTION_LAG_DAYS）。段太短无法裁时跳过该窗口（诊断不产出模型，
    跳过比带泄漏跑更有意义）。
    """
    all_dates = sorted(df["trade_date"].unique())
    if not all_dates:
        return pd.DataFrame(), pd.DataFrame()

    step_days = wfa["step_months"] * 30
    train_years_days = wfa["train_years"] * 365
    val_days = wfa["val_months"] * 30

    # 首个验证起点：rolling 需先留出 train_years 训练段 + val_days 验证段，
    # 保证首窗训练集完整；expanding 首窗即可从数据起点开始。
    start_str = wfa.get("start")
    base = pd.Timestamp(start_str) if start_str else pd.Timestamp(all_dates[0])
    if wfa["strategy"] == "rolling":
        first_anchor_offset = pd.Timedelta(days=train_years_days + val_days)
    else:
        first_anchor_offset = pd.Timedelta(days=val_days)
    anchor = base + first_anchor_offset + pd.Timedelta(days=idx * step_days)

    # 取 anchor 之前最近的交易日作为验证起点
    anchor_ts = pd.Timestamp(anchor.date())
    val_start = max((d for d in all_dates if d <= anchor_ts), default=None)
    if val_start is None:
        return pd.DataFrame(), pd.DataFrame()
    val_end = max(
        (d for d in all_dates if d <= anchor_ts + pd.Timedelta(days=val_days)),
        default=val_start,
    )

    if wfa["strategy"] == "expanding":
        train_start = all_dates[0]
    else:
        # rolling：训练起点 = 验证起点往前推 train_years 年（取最近交易日），长度固定。
        # 数据不足（如首个窗口往前推超过数据起点）时回退到数据最早日，保证窗口可运行。
        train_start = max(
            (d for d in all_dates if d <= val_start - pd.Timedelta(days=train_years_days)),
            default=all_dates[0],
        )

    train_df = df[
        (df["trade_date"] >= train_start) & (df["trade_date"] < val_start)
    ].copy()
    if embargo_days > 0 and not train_df.empty:
        train_days = np.sort(train_df["trade_date"].unique())
        if len(train_days) <= embargo_days:
            logger.warning(
                "[WFA] window %d: train days %d <= embargo %d; skip "
                "(train tail labels would leak into val)",
                idx,
                len(train_days),
                embargo_days,
            )
            return pd.DataFrame(), pd.DataFrame()
        train_df = train_df[
            train_df["trade_date"] < train_days[-embargo_days]
        ].copy()
    val_df = df[
        (df["trade_date"] >= val_start) & (df["trade_date"] <= val_end)
    ].copy()

    if train_df.empty or val_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    return train_df, val_df
