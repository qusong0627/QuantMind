"""统计派 · 逐日截面中性化（移植 P2 / Tier 2）。

两个口径，都在**逐日截面内**做（跨日不混合）：

- :func:`industry_demean` —— 行业内去均值（最直白，抗极端行业暴露）；
- :func:`industry_size_residual` —— 对「行业哑变量 + ln(流通市值)」做逐日截面
  回归取残差（行业与市值一次剥掉；市值取对数保证尺度线性可比）。

纪律：

- 元数据缺失（行业/市值 NaN）的样本中性化后为 **NaN**（由 IC 统计按缺样丢弃，
  不硬凑、不填 0）；
- 残差回归用 ``np.linalg.lstsq``（列满秩时即 OLS；行业只有单只票导致的共线由
  最小范数解兜底——残差仍良定义，不炸也不静默换口径）；
- 每日样本不足（参数个数 + 余量）→ 该日整天跳过（NaN），报告里计数。

报告（:class:`NeutralizeReport`）随结果进任务产物：中性化丢了多少样本、跳了
几天，是复核 IC 数字时必须看的东西。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: 每日回归的最少额外样本数（自由度余量，加在参数个数之上）
DEFAULT_MIN_EXTRA_NAMES = 10


@dataclass(frozen=True)
class NeutralizeReport:
    """中性化审计（行数口径 + 跳过天数）。"""

    n_input: int
    n_output: int
    n_missing_meta: int
    n_days_skipped: int


def _aligned(
    factor: pd.Series, industry: pd.Series, size_log: pd.Series
) -> pd.DataFrame:
    frame = pd.concat(
        {"factor": factor, "industry": industry, "size_log": size_log}, axis=1
    )
    if not frame.index.equals(factor.index):
        raise ValueError("中性化输入索引不一致（factor/industry/size 必须同一面板）")
    return frame


def _days(index: pd.Index) -> int:
    return int(pd.Index(index.get_level_values(0)).nunique()) if len(index) else 0


def industry_demean(
    factor: pd.Series, industry: pd.Series
) -> tuple[pd.Series, NeutralizeReport]:
    """行业内去均值：``factor - mean(factor | 当日同行业)``（逐日截面）。"""
    frame = _aligned(factor, industry, pd.Series(np.nan, index=factor.index))
    valid = frame[["factor", "industry"]].dropna()
    out = pd.Series(np.nan, index=factor.index, name=factor.name)
    if len(valid):
        day = valid.index.get_level_values(0)
        means = valid.groupby([day, valid["industry"]])["factor"].transform("mean")
        out.loc[valid.index] = (valid["factor"] - means).to_numpy()
    return out, NeutralizeReport(
        n_input=len(frame),
        n_output=int(out.notna().sum()),
        n_missing_meta=len(frame) - len(valid),
        n_days_skipped=0,
    )


def _day_residual(sub: pd.DataFrame, *, min_extra_names: int) -> pd.Series:
    """单日截面：截距 + ln 市值 + 行业哑变量（去首列）→ OLS 残差。"""
    design = pd.get_dummies(sub["industry"], prefix="ind", drop_first=True, dtype=float)
    x = np.column_stack(
        [
            np.ones(len(sub)),
            sub["size_log"].to_numpy(dtype=float),
            design.to_numpy(dtype=float),
        ]
    )
    if len(sub) < x.shape[1] + min_extra_names:
        return pd.Series(np.nan, index=sub.index)
    y = sub["factor"].to_numpy(dtype=float)
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    return pd.Series(y - x @ beta, index=sub.index)


def industry_size_residual(
    factor: pd.Series,
    industry: pd.Series,
    float_shares: pd.Series | None = None,
    *,
    close: pd.Series | None = None,
    size: pd.Series | None = None,
    min_extra_names: int = DEFAULT_MIN_EXTRA_NAMES,
) -> tuple[pd.Series, NeutralizeReport]:
    """行业 + 市值中性化：逐日截面回归残差。

    市值优先用显式 ``size``（如流通市值，单位任意——取对数后只比大小）；否则由
    ``close × float_shares`` 现算（QuantDB ``ActiveCapital`` 为流通股本）。
    市值 ≤ 0 或缺失 → 该样本 NaN。
    """
    if size is None:
        if close is None or float_shares is None:
            raise ValueError("需提供 size，或同时提供 close 与 float_shares")
        size = close.astype(float) * float_shares.astype(float)
    size_log = np.log(size.where(size > 0))
    frame = _aligned(factor, industry, size_log)
    valid = frame.dropna()
    results: list[pd.Series] = []
    for _day, sub in valid.groupby(level=0, sort=True):
        results.append(_day_residual(sub, min_extra_names=min_extra_names))
    out = (
        pd.concat(results).reindex(frame.index)
        if results
        else pd.Series(np.nan, index=frame.index)
    )
    return out.rename(factor.name), NeutralizeReport(
        n_input=len(frame),
        n_output=int(out.notna().sum()),
        n_missing_meta=len(frame) - len(valid),
        n_days_skipped=_days(valid.index) - _days(out.dropna().index),
    )
