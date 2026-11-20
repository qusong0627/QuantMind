"""训练样本权重（WP3）：可交易 mask / ST PIT / 时间衰减。

设计要点：
- 截面 rank 标签仍在**全市场**计算，本模块只给样本加权（不可交易 → 0），
  不改变目标分布；
- 可交易判定复用 `trade_mask.parquet`（与回测/严格评估同一份），
  ST 用 PIT 缓存（`st_pit_cache`），缺文件即 fail-closed；
- 时间衰减只作用于 train（val 不做衰减，避免「未来样本权重 >1」）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_PREFIXES = ("SH", "SZ", "BJ")


def to_suffix(symbols: pd.Series) -> pd.Series:
    s = symbols.astype(str).str.upper()
    is_prefixed = s.str.len().gt(2) & s.str[:2].isin(_PREFIXES)
    return pd.Series(np.where(is_prefixed, s.str[2:] + "." + s.str[:2], s), index=s.index)


def _key(symbols: pd.Series, dates: pd.Series) -> pd.Series:
    return to_suffix(symbols) + "|" + pd.to_datetime(dates).dt.strftime("%Y%m%d")


def build_weights(
    frame: pd.DataFrame,
    *,
    quantdb_dir: str | Path | None,
    config: dict | None,
    market: str = "CN",
    with_decay: bool = True,
) -> np.ndarray:
    """返回与 frame 行对齐的 float32 权重（1.0 默认，0.0 不可交易）。"""
    n = len(frame)
    weights = np.ones(n, dtype=np.float32)
    cfg = config or {}
    if n == 0:
        return weights

    half_life = float(cfg.get("time_decay_half_life_days") or 0)
    if with_decay and half_life > 0 and "trade_date" in frame.columns:
        dates = pd.to_datetime(frame["trade_date"])
        age_days = (dates.max() - dates).dt.days.to_numpy(dtype=float)
        weights *= np.power(0.5, age_days / half_life).astype(np.float32)

    if not bool(cfg.get("tradeable_mask")) and not bool(cfg.get("st")):
        return weights

    if market.upper() != "CN":
        raise RuntimeError(
            f"sample_weight.tradeable_mask/st 仅支持 CN（当前 market={market}）"
        )
    root = Path(quantdb_dir or "")
    if not str(root) or not root.exists():
        raise RuntimeError(f"sample_weight 需要 quantdb_dir，当前为 {quantdb_dir!r}")

    keys = _key(frame["symbol"], frame["trade_date"])

    if bool(cfg.get("tradeable_mask")):
        mask_path = root / "5_technical_derived" / "trade_mask" / "trade_mask.parquet"
        if not mask_path.exists():
            raise RuntimeError(f"trade_mask.parquet 不存在：{mask_path}")
        mask = pd.read_parquet(mask_path, columns=["dt", "symbol", "blocked_long"])
        blocked = {
            f"{sym}|{day}"
            for day, sym in zip(
                mask.loc[mask["blocked_long"], "dt"].astype(str),
                mask.loc[mask["blocked_long"], "symbol"].astype(str),
            )
        }
        if blocked:
            hit = keys.isin(blocked).to_numpy()
            weights[hit] = 0.0
            logger.info(
                "sample_weight: tradeable_mask zeroed %d/%d rows (%.2f%%)",
                int(hit.sum()), n, 100.0 * hit.sum() / max(1, n),
            )

    if bool(cfg.get("st")):
        cache = root / "factor_research" / "st_pit_cache"
        if not cache.exists():
            raise RuntimeError(f"st_pit_cache 不存在：{cache}")
        suffixes = pd.unique(to_suffix(frame["symbol"]))
        st_keys: set[str] = set()
        for sym in suffixes:
            path = cache / f"{sym}.parquet"
            if not path.exists():
                continue
            try:
                cached = pd.read_parquet(path, columns=["time", "is_st"])
            except Exception as exc:  # noqa: BLE001 — 单票缓存损坏不阻断全市场
                logger.warning("sample_weight: st cache unreadable %s: %s", sym, exc)
                continue
            for day in pd.to_datetime(
                cached.loc[cached["is_st"], "time"], errors="coerce"
            ).dt.strftime("%Y%m%d").dropna():
                st_keys.add(f"{sym}|{day}")
        if st_keys:
            hit = keys.isin(st_keys).to_numpy()
            weights[hit] = 0.0
            logger.info(
                "sample_weight: ST zeroed %d/%d rows (%.2f%%)",
                int(hit.sum()), n, 100.0 * hit.sum() / max(1, n),
            )

    return weights
