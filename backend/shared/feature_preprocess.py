from __future__ import annotations

import pandas as pd


def cs_zscore_with_mad_series(series: pd.Series, mad_multiplier: float = 5.0) -> pd.Series:
    median = series.median()
    abs_dev = (series - median).abs()
    mad = abs_dev.median()
    
    # 优势 1：天然屏蔽稀疏低效噪音。当 MAD 为 0 时直接返回全 0.0。
    if mad == 0:
        return pd.Series(0.0, index=series.index)
        
    upper = median + mad_multiplier * mad
    lower = median - mad_multiplier * mad
    clipped = series.clip(lower=lower, upper=upper)
    
    # 优势 2：释放小尺度核心因子的真实波动。移除 1e-9 惩罚项。
    std = clipped.std()
    if pd.isna(std) or std == 0:
        std = 1e-8
        
    return (clipped - clipped.mean()) / std


def apply_cs_mad_zscore(
    df: pd.DataFrame,
    *,
    feature_columns: list[str],
    label_column: str | None = None,
    chunk_size: int = 50,
    mad_multiplier: float = 5.0,
) -> pd.DataFrame:
    out = df.copy()
    for i in range(0, len(feature_columns), max(1, int(chunk_size))):
        chunk_cols = feature_columns[i : i + max(1, int(chunk_size))]
        out[chunk_cols] = out.groupby("trade_date")[chunk_cols].transform(
            lambda s: cs_zscore_with_mad_series(s, mad_multiplier=mad_multiplier)
        )
    if label_column and label_column in out.columns:
        out[label_column] = out.groupby("trade_date")[label_column].transform(
            lambda s: cs_zscore_with_mad_series(s, mad_multiplier=mad_multiplier)
        )
    return out
