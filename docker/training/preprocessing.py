"""训练数据预处理纯函数集。

供 train.py 训练端与推理模板共用，保证训练/推理输入分布一致。
所有函数均为纯函数（输入 DataFrame/np.ndarray，返回新对象，不修改入参）。

设计要点：
- 截面 Z-score / 缩尾：按 trade_date 分组，推理端可复现（推理时拿到整日横截面）。
- 中性化：特征快照不含行业列，推理端不可复现 → 本模块不做中性化（已知限制）。
- 缺失值：停牌 NaN 用截面中位数填充；整列缺失（非 NaN 行数过低）填 0 → z=0 中性。
- 内存：按日期分块 + 分组向量化。历史版本逐 (日期×特征) `.loc` 赋值 + 整表 float64
  上浮，在 5.5M 行 × 273 特征直读训练时把 48G 容器打爆（OOM 137）；分块后峰值 < 15G，
  数学口径不变（每个交易日独立求截面统计量），输出保持 float32（与加载器降位一致）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from backend.shared.logging_config import get_logger
    logger = get_logger("training.preprocessing")
except Exception:  # pragma: no cover - 训练容器内可能无后端 logging
    import logging
    logger = logging.getLogger("training.preprocessing")

# 该特征当日非 NaN 行数低于 min(总行数×MIN_COVER_RATIO, MIN_COVER_ROWS) 视为整列缺失
MIN_COVER_RATIO = 0.01
MIN_COVER_ROWS = 20

_WINSOR_QUANTILES = (0.01, 0.99)
# 每次处理的交易日数（内存与向量化效率的平衡；单块 ≈ chunk×股票数 行）
_CHUNK_DATES = 100


def binarize_labels(y: np.ndarray | pd.Series, threshold: float = 0.0) -> np.ndarray:
    """分类目标二值化：label > threshold → 1，否则 0。

    输入应为未来 N 日原始收益（尚未做截面 rank）。NaN 保持 NaN（下游 dropna）。
    """
    arr = np.asarray(y, dtype=np.float64)
    out = np.empty_like(arr, dtype=np.float32)
    nan_mask = np.isnan(arr)
    out[nan_mask] = np.nan
    out[~nan_mask] = (arr[~nan_mask] > threshold).astype(np.float32)
    return out


def winsorize(
    values: np.ndarray, quantiles: tuple[float, float] = _WINSOR_QUANTILES
) -> np.ndarray:
    """分位缩尾：低于 p1 / 高于 p99 的极端值截断到对应分位数。

    采用分位缩尾而非 σ 缩尾——极端值会抬高 std，σ 缩尾边界反而比极端值更宽，
    无法真正截尾。NaN 透传。样本过少时退化为原样返回。
    """
    arr = np.asarray(values, dtype=np.float64)
    out = arr.copy()
    valid = ~np.isnan(arr)
    n = int(valid.sum())
    if n == 0:
        return out.astype(np.float32)
    if n < 10:
        # 样本过少无法可靠估计分位数，不缩尾
        return out.astype(np.float32)
    lo = float(np.quantile(arr[valid], quantiles[0]))
    hi = float(np.quantile(arr[valid], quantiles[1]))
    if not np.isfinite(lo) or not np.isfinite(hi) or lo >= hi:
        return out.astype(np.float32)
    out[valid] = np.clip(arr[valid], lo, hi)
    return out.astype(np.float32)


def _date_chunks(dates: pd.Series, chunk: int = _CHUNK_DATES):
    """把唯一交易日切成若干块（用于分块处理，控制峰值内存）。"""
    uniq = pd.Index(dates.unique())
    for i in range(0, len(uniq), chunk):
        yield uniq[i : i + chunk]


def _chunk_broadcast(dates: pd.Series, per_date: pd.DataFrame) -> pd.DataFrame:
    """把 (日期 × 特征) 的聚合值按行广播回该块（输出与 dates 同索引）。"""
    base = pd.DataFrame({"trade_date": dates.to_numpy()}, index=dates.index)
    return base.join(per_date, on="trade_date")[per_date.columns]


def cross_sectional_median_fill(
    df: pd.DataFrame,
    features: list[str],
    min_ratio: float = MIN_COVER_RATIO,
    min_rows: int = MIN_COVER_ROWS,
) -> pd.DataFrame:
    """按 (trade_date, feature) 截面中位数填充缺失值。

    规则：
    - 该特征当日非 NaN 行数 ≥ max(总行数×min_ratio, min_rows)：用截面中位数填充停牌 NaN。
    - 否则视为整列缺失：填 0（后续 Z-score 后 std=0 → 全 0，中性）。

    分块向量化实现：每块对 (日期×特征) 求一次 count/median，再整块 fillna。
    返回填充后的新 DataFrame（不修改入参）。
    """
    out = df.copy()
    if not features:
        return out
    zero_filled = 0
    for chunk in _date_chunks(out["trade_date"]):
        m = out["trade_date"].isin(chunk)
        sub = out.loc[m, ["trade_date", *features]]
        g = sub.groupby("trade_date", sort=False)
        cnt = g[features].count()
        med = g[features].median()
        n = g[features].size()
        thresh = np.maximum((n.astype(float) * min_ratio).astype(int), min_rows)
        zero_mask = cnt.lt(thresh, axis=0)  # 整列缺失 → 填 0
        fill = med.mask(zero_mask, 0.0)
        zero_filled += int(zero_mask.to_numpy().sum())
        fill_full = _chunk_broadcast(sub["trade_date"], fill)
        out.loc[m, features] = sub[features].fillna(fill_full).astype(np.float32)
    if zero_filled:
        logger.warning(
            "cross_sectional_median_fill: %d feature-days treated as whole-column missing -> 0",
            zero_filled,
        )
    return out


def cross_sectional_zscore(
    df: pd.DataFrame,
    features: list[str],
    winsor: bool = True,
    quantiles: tuple[float, float] = _WINSOR_QUANTILES,
) -> pd.DataFrame:
    """按 (trade_date, feature) 截面 Z-score：每个交易日截面先分位缩尾，再 (x-mean)/std。

    std=0 的列（整列缺失填充后）全 0。NaN 透传（下游 dropna）。
    必须按 trade_date 分组——全局 Z-score 会混入不同交易日分布，截面排名失真。

    分块向量化实现；块内用 float64 计算（防溢出），输出回 float32
    （与加载器 "columns downcast to float32" 口径一致，避免整表上浮吃内存）。
    """
    out = df.copy()
    if not features:
        return out
    if winsor:
        q_lo, q_hi = quantiles
    for chunk in _date_chunks(out["trade_date"]):
        m = out["trade_date"].isin(chunk)
        sub = out.loc[m, ["trade_date", *features]]
        g = sub.groupby("trade_date", sort=False)
        X = sub[features].astype(np.float64)
        if winsor:
            lo = g[features].quantile(q_lo)
            hi = g[features].quantile(q_hi)
            # 样本过少（<10 有效值）不缩尾：边界放宽为 ±inf
            cnt = g[features].count()
            enough = cnt.ge(10)
            lo = lo.where(enough, -np.inf)
            hi = hi.where(enough, np.inf)
            lo_full = _chunk_broadcast(sub["trade_date"], lo)
            hi_full = _chunk_broadcast(sub["trade_date"], hi)
            X = X.clip(lower=lo_full, upper=hi_full)
        # 统计量必须取自**缩尾后**的 X（与旧实现「先裁后统」一致；用裁剪前数据
        # 会让极值抬高的 mean/std 把 Z 压成常数——曾致全部特征近乎恒定）。
        xg = X.copy()
        xg["trade_date"] = sub["trade_date"].to_numpy()
        g2 = xg.groupby("trade_date", sort=False)
        mu = g2[features].transform("mean")
        sd = g2[features].transform(lambda s: s.std(ddof=0))  # 对齐旧实现 np.nanstd(ddof=0)
        sd_ok = sd.gt(0) & np.isfinite(sd)
        Z = (X - mu).div(sd.where(sd_ok))
        valid = X.notna()
        Z = Z.where(valid)  # NaN 透传
        Z = Z.mask(valid & ~sd_ok, 0.0)  # std 无效的截面 → 有效行置 0
        out.loc[m, features] = Z.astype(np.float32)
    return out


def cross_sectional_preprocess(
    df: pd.DataFrame,
    features: list[str],
    *,
    enabled: bool = False,
    winsor: bool = True,
    quantiles: tuple[float, float] = _WINSOR_QUANTILES,
    exclude: set[str] | None = None,
) -> pd.DataFrame:
    """主入口：按配置做截面预处理。

    enabled=False 时原样返回（兼容旧链路）。exclude 指定不参与变换的特征列
    （如类别特征 ind_code_l1，保持原始编码）。返回新 DataFrame。
    """
    if not enabled or not features:
        return df
    excl = exclude or set()
    feats = [f for f in features if f not in excl]
    out = cross_sectional_median_fill(df, feats)
    out = cross_sectional_zscore(out, feats, winsor=winsor, quantiles=quantiles)
    return out