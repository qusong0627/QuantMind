"""中性化控制变量快照（规模 / 风险 / 动量 / 流动性 / 估值），只读 QuantDB 日分区。

数据源：``6_ml_datasets/l1_factors/dt=YYYYMMDD/data.parquet``

默认三因子：``fun_float_mv`` → ``log_mv``、``style_beta_60`` → ``beta60``、
``mom_ret_20d`` → ``mom20``；另提供 8 个可选变量（见 ``CONTROL_SPECS``）。

口径约束：

- 只允许读取 **<= 信号数据日** 的分区（防前视由调用方与 ``load_controls`` 双重把关）；
- 代码统一转规范**前缀式**（``SH600036``），与 ``engine_signal_scores.symbol`` 对齐；
- 分区缺失抛 ``ControlsUnavailableError``；单列缺失填 NaN（由服务层按覆盖率降级）。
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


class ControlsUnavailableError(RuntimeError):
    """控制变量分区缺失或不可读。"""


#: 控制变量库：逻辑名 → (物理列, 变换)
#:
#: 默认只用 ``log_mv / beta60 / mom20``（验证过的三因子口径）；
#: 其余变量供按模型/按市场选择，避免同族变量同时勾选（市值×总市值、beta20×beta60
#: 高度共线，设计矩阵条件数会越界，服务层会判 degraded）。
CONTROL_SPECS: dict[str, tuple[str, str]] = {
    "log_mv": ("fun_float_mv", "log"),
    "log_total_mv": ("fun_total_mv", "log"),
    "beta20": ("style_beta_20", "raw"),
    "beta60": ("style_beta_60", "raw"),
    "mom20": ("mom_ret_20d", "raw"),
    "mom60": ("mom_ret_60d", "raw"),
    "vol20": ("vol_std_20", "raw"),
    "idio_vol20": ("style_idio_vol_20", "raw"),
    "turn20": ("turn_20", "raw"),
    "pb": ("fun_pb", "raw"),
    "ep": ("fun_ep", "raw"),
}

_SYMBOL_COLUMN = "symbol"


@dataclass(frozen=True)
class ControlSnapshot:
    trade_date: date
    frame: pd.DataFrame  # index=symbol(prefix), columns=CONTROL_SPECS 的逻辑名
    source_path: str


def partition_path(trade_date: date) -> Path:
    """某交易日的控制变量分区路径（不检查存在性）。"""
    return resolve_quantdb_subdir(
        "6_ml_datasets", "l1_factors", f"dt={trade_date:%Y%m%d}", "data.parquet"
    )


# 兼容旧名（内部使用）
_partition_path = partition_path


def _available_columns(path: Path) -> set[str]:
    import pyarrow.parquet as pq  # noqa: PLC0415

    return set(pq.read_schema(path).names)


def _read_partition(path: Path) -> pd.DataFrame:
    available = _available_columns(path)
    if _SYMBOL_COLUMN not in available:
        raise ControlsUnavailableError(f"控制变量分区缺 symbol 列: {path}")
    physical = [col for col, _ in CONTROL_SPECS.values() if col in available]
    if not physical:
        raise ControlsUnavailableError(
            f"控制变量列全部缺失: {path}（可用列 {sorted(available)[:8]}…）"
        )
    raw = pd.read_parquet(path, columns=[_SYMBOL_COLUMN, *physical])
    if raw.empty:
        raise ControlsUnavailableError(f"控制变量分区为空: {path}")

    symbols = raw[_SYMBOL_COLUMN].astype(str).map(StockCodeUtil.to_prefix)
    columns: dict[str, pd.Series] = {}
    for name, (physical_col, transform) in CONTROL_SPECS.items():
        if physical_col not in raw.columns:
            # 老分区/自定义数据集可能缺列：缺失变量填 NaN，由服务层按覆盖率降级，
            # 而不是让整批计算失败（用户只勾选了已有变量时仍可正常中性化）。
            columns[name] = pd.Series(np.nan, index=raw.index, dtype=float)
            continue
        values = pd.to_numeric(raw[physical_col], errors="coerce")
        if transform == "log":
            values = np.log(values.clip(lower=1.0))
        columns[name] = values.astype(float)

    frame = pd.DataFrame(columns)
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
