"""中性化控制变量快照（市值 / beta / 动量），只读 QuantDB 日分区。

数据源：``6_ml_datasets/l1_factors/dt=YYYYMMDD/data.parquet``

- ``fun_float_mv`` → ``log_mv = log(max(v, 1))``
- ``style_beta_60`` → ``beta60``
- ``mom_ret_20d`` → ``mom20``

口径约束：

- 只允许读取 **<= 信号数据日** 的分区（防前视由调用方与 ``load_controls`` 双重把关）；
- 代码统一转规范**前缀式**（``SH600036``），与 ``engine_signal_scores.symbol`` 对齐；
- 分区缺失抛 ``ControlsUnavailableError``，由服务层记为 ``degraded``，不得静默跳过。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.quantdb_paths import resolve_quantdb_subdir
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = ("symbol", "fun_float_mv", "style_beta_60", "mom_ret_20d")


class ControlsUnavailableError(RuntimeError):
    """控制变量分区缺失或不可读。"""


@dataclass(frozen=True)
class ControlSnapshot:
    trade_date: date
    frame: pd.DataFrame  # index=symbol(prefix), columns=log_mv/beta60/mom20
    source_path: str


def partition_path(trade_date: date) -> Path:
    """某交易日的控制变量分区路径（不检查存在性）。"""
    return resolve_quantdb_subdir(
        "6_ml_datasets", "l1_factors", f"dt={trade_date:%Y%m%d}", "data.parquet"
    )


# 兼容旧名（内部使用）
_partition_path = partition_path


def _read_partition(path: Path) -> pd.DataFrame:
    try:
        raw = pd.read_parquet(path, columns=list(_REQUIRED_COLUMNS))
    except (KeyError, ValueError) as exc:
        raise ControlsUnavailableError(f"控制变量列缺失: {path} ({exc})") from exc
    if raw.empty:
        raise ControlsUnavailableError(f"控制变量分区为空: {path}")

    symbols = raw["symbol"].astype(str).map(StockCodeUtil.to_prefix)
    frame = pd.DataFrame(
        {
            "log_mv": np.log(
                pd.to_numeric(raw["fun_float_mv"], errors="coerce").clip(lower=1.0)
            ),
            "beta60": pd.to_numeric(raw["style_beta_60"], errors="coerce"),
            "mom20": pd.to_numeric(raw["mom_ret_20d"], errors="coerce"),
        }
    )
    # 按位置对齐：symbols 是 Series（index=0..n-1），直接传 index= 会按标签对齐成 NaN
    frame.index = symbols.to_numpy()
    frame.index.name = "symbol"
    # 同一标的重复行（数据源偶发）保留首行，避免索引重复导致回归静默出错
    frame = frame[~frame.index.duplicated(keep="first")]
    return frame


@lru_cache(maxsize=8)
def _load_cached(trade_date_iso: str, root_key: str) -> tuple[pd.DataFrame, str]:
    trade_date = date.fromisoformat(trade_date_iso)
    path = _partition_path(trade_date)
    if not path.exists():
        raise ControlsUnavailableError(f"控制变量分区不存在: {path}")
    frame = _read_partition(path)
    return frame, str(path)


def load_controls(
    trade_date: date,
    *,
    signal_data_date: date | None = None,
) -> ControlSnapshot:
    """读取某交易日的控制变量快照。

    ``signal_data_date`` 用于前视硬校验：控制变量日期不得晚于信号数据日。
    """
    if signal_data_date is not None and trade_date > signal_data_date:
        raise ControlsUnavailableError(
            f"控制变量日期 {trade_date} 晚于信号数据日 {signal_data_date}（前视）"
        )
    try:
        frame, source = _load_cached(trade_date.isoformat(), "default")
    except ControlsUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可降级的领域错误
        raise ControlsUnavailableError(f"控制变量读取失败 {trade_date}: {exc}") from exc
    return ControlSnapshot(
        trade_date=trade_date, frame=frame.copy(), source_path=source
    )


def clear_cache() -> None:
    """测试/回填用：清空进程内缓存。"""
    _load_cached.cache_clear()
