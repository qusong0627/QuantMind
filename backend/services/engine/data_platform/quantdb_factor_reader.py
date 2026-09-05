"""Direct reader for the three QuantDB model-factor datasets.

This module deliberately never writes a derived feature parquet.  It provides
one canonical, in-memory frame per source (L1, L2, or the L1+L2 wide table)
for training, inference, and backtesting.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from collections.abc import Iterable

import pandas as pd

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

FactorSource = Literal[
    "l1_factors", "l2_factors", "l1_l2_factors", "ccass_factors", "south_factors"
]

FACTOR_SOURCE_DIRS: dict[FactorSource, str] = {
    "l1_factors": "6_ml_datasets/l1_factors",
    "l2_factors": "6_ml_datasets/l2_factors",
    "l1_l2_factors": "6_ml_datasets/l1_l2_factors",
    "ccass_factors": "6_ml_datasets/ccass_factors",
    "south_factors": "6_ml_datasets/south_factors",
}
DAILY_BACKWARD_DIR = "1_kline_data/daily_backward"
DEFAULT_FACTOR_SOURCE: FactorSource = "l1_factors"

# ── 市场 → 可用因子源映射（后台「模型训练数据集」与训练页数据源选择共用）───────
# 各市场 6_ml_datasets/ 下实际存在的训练直读数据集。
MARKET_FACTOR_SOURCES: dict[str, tuple[FactorSource, ...]] = {
    "CN": ("l1_factors", "l2_factors", "l1_l2_factors"),
    "HK": ("l1_factors", "ccass_factors", "south_factors"),
    "US": ("l1_factors",),
    "CRYPTO": ("l1_factors",),
    "FUTURES": ("l1_factors",),
}

# ── 6_ml_datasets 下不参与训练直读的目录（“刷新字段”自动发现时排除）─────────────
# - features_daily：含未来收益标签列（return_Nd）与 OHLCV 重复列，作为特征会泄漏；
# - alpha_library_labels：纯标签库；
# - alpha_library：历史策略预计算 Alpha 库（如需纳入训练直读，从本集合移除即可）。
# 其余新增因子目录（如未来上线的 xxx_factors）无需改代码，刷新字段即自动注册。
EXCLUDED_TRAIN_DATASETS: frozenset[str] = frozenset({
    "features_daily",
    "alpha_library_labels",
    "alpha_library",
})
DEFAULT_FACTOR_SOURCE_BY_MARKET: dict[str, FactorSource] = {
    "CN": "l1_factors",
    "HK": "l1_factors",
    "US": "l1_factors",
    "CRYPTO": "l1_factors",
    "FUTURES": "l1_factors",
}
# 各市场数据根目录环境变量（容器内路径，本地编排器挂载后亦可见）
MARKET_DATA_DIR_ENV: dict[str, str] = {
    "CN": "QM_QUANTDB_DATA_DIR",
    "HK": "QM_QUANTHK_DATA_DIR",
    "US": "QM_QUANTUS_DATA_DIR",
    "CRYPTO": "QM_QUANTBC_DATA_DIR",
    "FUTURES": "QM_QUANTFUTURES_DATA_DIR",
}
MARKET_DATA_DIR_DEFAULT: dict[str, str] = {
    "CN": "/data/quantdb",
    "HK": "/data/quanthk",
    "US": "/data/quantus",
    "CRYPTO": "/data/quantbc",
    "FUTURES": "/data/quantfutures",
}
# 次要因子源（ccass/south 等）不含 OHLCV，标签构建所需的行情列由同目录
# l1_factors 补给（各市场 l1_factors 均带 OHLCV）。
OHLCV_DONOR_SOURCE: FactorSource = "l1_factors"
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
REQUIRED_COLUMNS = (
    "symbol",
    "date",
    *OHLCV_COLUMNS,
)


def normalize_market(market: str | None) -> str:
    market_upper = str(market or "CN").upper().strip()
    if market_upper in {"A", "A_SHARE", "SSE", "CN"}:
        return "CN"
    return market_upper if market_upper in MARKET_FACTOR_SOURCES else "CN"


def sources_for_market(market: str | None = None) -> list[FactorSource]:
    """该市场在训练页/后台可选的因子源列表（按映射定义顺序）。"""
    return list(MARKET_FACTOR_SOURCES.get(normalize_market(market), ("l1_factors",)))


def default_source_for(market: str | None = None) -> FactorSource:
    return DEFAULT_FACTOR_SOURCE_BY_MARKET.get(normalize_market(market), "l1_factors")


def market_data_dir(market: str | None = None) -> Path:
    """解析某市场数据根目录（api 容器内视角）。缺省回退市场默认路径。

    Ubuntu 容器设计：仅识别容器内路径 /data/quantdb 及环境变量，
    不探测 Windows 盘符，避免本地盘符污染服务端判断。
    """
    market_upper = normalize_market(market)
    env_val = os.getenv(MARKET_DATA_DIR_ENV[market_upper], "").strip()
    if env_val:
        return Path(env_val)
    # CN 市场优先通过 hub 统一解析（hub 按 /data/quantdb -> /app/data/quantdb 探测）
    if market_upper == "CN":
        try:
            from backend.services.engine.data_platform.quantdb_hub import (
                _resolve_data_dir,
            )

            hub_dir = _resolve_data_dir()
            if hub_dir.is_dir():
                return hub_dir
        except Exception:
            pass
    return Path(MARKET_DATA_DIR_DEFAULT[market_upper])


KEY_COLUMNS = {"symbol", "date", "dt", "time", "release_id", "published_at"}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class QuantDBFactorError(RuntimeError):
    """The configured QuantDB factor source cannot safely serve a model."""


@dataclass(frozen=True)
class FactorSourceStatus:
    dataset_id: FactorSource
    path: str
    files: int
    columns: list[str]
    column_types: dict[str, str]
    schema_hash: str
    min_date: str | None
    max_date: str | None
    ready: bool
    missing_required: list[str]
    reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise QuantDBFactorError(f"Invalid QuantDB column name: {identifier!r}")
    return f'"{identifier}"'


class QuantDBFactorReader:
    """Read one raw QuantDB factor source without materialising a snapshot."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        market: str | None = None,
    ) -> None:
        if data_dir is not None:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = market_data_dir(market)

    def validate_source(self, source: str) -> str:
        """校验并放行因子数据集名。

        - 静态注册目录（FACTOR_SOURCE_DIRS）直接放行；
        - 未注册目录只要真实存在于该市场 6_ml_datasets/ 且命名合规即放行
          —— 未来新增因子数据集（如 xxx_factors）无需改代码；
        - 排除清单（EXCLUDED_TRAIN_DATASETS）与非法命名给出明确拒绝原因。
        """
        if source not in FACTOR_SOURCE_DIRS:
            if source in EXCLUDED_TRAIN_DATASETS:
                raise QuantDBFactorError(
                    f"Factor dataset {source!r} is excluded from direct training "
                    "(see EXCLUDED_TRAIN_DATASETS in quantdb_factor_reader)"
                )
            if not _IDENTIFIER.fullmatch(source):
                raise QuantDBFactorError(f"Invalid factor dataset name: {source!r}")
            root = self.data_dir / "6_ml_datasets" / source
            if not root.is_dir():
                raise QuantDBFactorError(
                    f"Unsupported factor source {source!r}; expected one of "
                    f"{', '.join(FACTOR_SOURCE_DIRS)} or a dataset directory under "
                    f"{self.data_dir / '6_ml_datasets'}"
                )
        return source

    def source_path(self, source: str) -> Path:
        mapped = FACTOR_SOURCE_DIRS.get(source)
        if mapped is not None:
            return self.data_dir / mapped
        # 动态目录：注册表外的 6_ml_datasets 子目录（validate 校验命名/排除清单）
        self.validate_source(source)
        return self.data_dir / "6_ml_datasets" / source

    def _files(self, source: str) -> list[Path]:
        root = self.source_path(source)
        # 只统计已发布的 dt= 分区文件，排除 _stage 等非分区暂存目录，
        # 否则暂存 parquet 会被计入分区文件数，与实际可读数据不一致。
        return sorted(root.glob("dt=*/*.parquet")) if root.is_dir() else []

    @staticmethod
    def _duckdb():
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise QuantDBFactorError(
                "duckdb is required to read QuantDB factor datasets"
            ) from exc
        return duckdb

    def _relation(self, source: str) -> str:
        root = self.source_path(source)
        if not root.is_dir():
            raise QuantDBFactorError(f"QuantDB factor directory does not exist: {root}")
        # 只读取已发布分区 dt=YYYYMMDD/*.parquet（hive 分区）。若用 **/*.parquet 把
        # _stage 等暂存目录一并 glob，暂存文件 schema 与分区不一致会抛
        # "Hive partition mismatch"，导致整个因子源无法直读训练。
        parquet_glob = str(root / "dt=*" / "*.parquet").replace("'", "''")
        return f"read_parquet('{parquet_glob}', hive_partitioning=true, union_by_name=true)"

    def _daily_backward_relation(self) -> str | None:
        """返回后复权日线关系；数据未部署时保持因子表原有行为。

        严格按 dt=YYYYMMDD/ 的 Hive 分区布局读取，只匹配已发布分区文件。
        不使用 **/*.parquet 递归：数据目录里若同时存在旧版按股票单文件
        （如 000001.SZ.parquet）与新版 dt= 分区文件，递归 glob 会把两种
        Hive 结构一并读入，抛出 "Hive partition mismatch" 导致训练失败。
        """
        root = self.data_dir / DAILY_BACKWARD_DIR
        if not root.is_dir() or not any(root.glob("dt=*/*.parquet")):
            return None
        parquet_glob = str(root / "dt=*" / "*.parquet").replace("'", "''")
        return f"read_parquet('{parquet_glob}', hive_partitioning=true, union_by_name=true)"

    def _ohlcv_donor_relation(self) -> str | None:
        """返回同目录 l1_factors 关系，作为无 OHLCV 次要源（ccass/south）的行情补给。

        与全库统一，严格读取已发布的 dt=YYYYMMDD/ 分区布局，避免误把顶层
        残留单文件 glob 进来造成 "Hive partition mismatch"。
        """
        root = self.data_dir / FACTOR_SOURCE_DIRS[OHLCV_DONOR_SOURCE]
        if not root.is_dir() or not any(root.glob("dt=*/*.parquet")):
            return None
        parquet_glob = str(root / "dt=*" / "*.parquet").replace("'", "''")
        return f"read_parquet('{parquet_glob}', hive_partitioning=true, union_by_name=true)"

    @staticmethod
    def _relation_columns(relation: str) -> set[str]:
        duckdb = QuantDBFactorReader._duckdb()
        con = duckdb.connect(config={"memory_limit": "2GB", "threads": "2"})
        try:
            rows = con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
            return {str(row[0]) for row in rows}
        except Exception:  # noqa: BLE001
            return set()
        finally:
            con.close()

    def describe(self, source: str) -> FactorSourceStatus:
        source = self.validate_source(source)
        files = self._files(source)
        root = self.source_path(source)
        if not files:
            return FactorSourceStatus(
                dataset_id=source,
                path=str(root),
                files=0,
                columns=[],
                column_types={},
                schema_hash="",
                min_date=None,
                max_date=None,
                ready=False,
                missing_required=list(REQUIRED_COLUMNS),
                reason="No parquet files found",
            )

        duckdb = self._duckdb()
        con = duckdb.connect(config={"memory_limit": "2GB", "threads": "2"})
        try:
            relation = self._relation(source)
            described = con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
            columns = [str(row[0]) for row in described]
            column_types = {str(row[0]): str(row[1]) for row in described}
            date_expr = self._date_expression(columns)
            date_row = con.execute(
                f"SELECT min({date_expr}), max({date_expr}) FROM {relation}"
            ).fetchone()
        except Exception as exc:
            return FactorSourceStatus(
                dataset_id=source,
                path=str(root),
                files=len(files),
                columns=[],
                column_types={},
                schema_hash="",
                min_date=None,
                max_date=None,
                ready=False,
                missing_required=list(REQUIRED_COLUMNS),
                reason=str(exc),
            )
        finally:
            con.close()

        schema_hash = hashlib.sha256("\n".join(columns).encode()).hexdigest()
        missing = [column for column in REQUIRED_COLUMNS if column not in columns]
        if "date" in missing and "dt" in columns:
            missing.remove("date")  # dt 分区列即日期（HK l1_factors 无 date 列）
        reason = None
        if missing and set(missing) <= set(OHLCV_COLUMNS):
            # 次要源（ccass/south）：OHLCV 由同目录 l1_factors 补给，标签可构建。
            donor = self._ohlcv_donor_relation()
            if donor is not None and set(OHLCV_COLUMNS) <= self._relation_columns(
                donor
            ):
                missing = []
            else:
                reason = "Missing OHLCV columns (l1_factors donor unavailable)"
        return FactorSourceStatus(
            dataset_id=source,
            path=str(root),
            files=len(files),
            columns=columns,
            column_types=column_types,
            schema_hash=schema_hash,
            min_date=str(date_row[0])[:10] if date_row and date_row[0] else None,
            max_date=str(date_row[1])[:10] if date_row and date_row[1] else None,
            ready=not missing,
            missing_required=missing,
            reason=None
            if not missing
            else (reason or "Missing required common columns"),
        )

    def discover(self, market: str | None = None) -> dict[str, dict]:
        """扫描因子数据集的 schema（后台“刷新字段”调用）。

        - market=None：扫描全部静态注册目录（跨市场工具场景）；
        - market 给定：静态注册目录 + 该市场 6_ml_datasets 下自动发现的新目录
          （排除 EXCLUDED_TRAIN_DATASETS），未来新增因子数据集无需改代码。
        """
        if market is None:
            sources = list(FACTOR_SOURCE_DIRS)
        else:
            sources = self.discover_market_sources(market)
        return {source: self.describe(source).to_dict() for source in sources}

    def discover_market_sources(self, market: str | None = None) -> list[str]:
        """静态注册 + 该市场数据根 6_ml_datasets 下自动发现的数据集列表。"""
        known = list(sources_for_market(market))
        root = self.data_dir / "6_ml_datasets"
        if not root.is_dir():
            return known
        dynamic: list[str] = []
        for child in sorted(root.iterdir()):
            name = child.name
            if not child.is_dir() or name.startswith("_") or not _IDENTIFIER.fullmatch(name):
                continue
            if name in FACTOR_SOURCE_DIRS or name in EXCLUDED_TRAIN_DATASETS or name in known:
                continue
            dynamic.append(name)
        return known + dynamic

    @staticmethod
    def _date_expression(columns: Iterable[str]) -> str:
        cols = set(columns)
        if "date" in cols:
            return 'CAST("date" AS DATE)'
        # Compatibility only.  New factor sources must publish the date column.
        if "dt" in cols:
            return "strptime(CAST(\"dt\" AS VARCHAR), '%Y%m%d')::DATE"
        raise QuantDBFactorError("Factor source has neither date nor dt")

    @staticmethod
    def _qualified_date_expression(columns: Iterable[str], alias: str) -> str:
        """与 _date_expression 相同，但返回带表别名的安全 SQL 表达式。"""
        cols = set(columns)
        if "date" in cols:
            return f'CAST({alias}."date" AS DATE)'
        if "dt" in cols:
            return f"strptime(CAST({alias}.\"dt\" AS VARCHAR), '%Y%m%d')::DATE"
        raise QuantDBFactorError("Factor source has neither date nor dt")

    def assert_ready(
        self,
        source: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> FactorSourceStatus:
        status = self.describe(source)
        if not status.ready:
            detail = (
                ", ".join(status.missing_required) or status.reason or "unknown reason"
            )
            raise QuantDBFactorError(
                f"{source} is not ready for direct training: {detail}"
            )
        if start and status.min_date and str(start)[:10] < status.min_date:
            raise QuantDBFactorError(
                f"{source} starts at {status.min_date}; requested {start}"
            )
        if end and status.max_date and str(end)[:10] > status.max_date:
            raise QuantDBFactorError(
                f"{source} ends at {status.max_date}; requested {end}"
            )
        return status

    def factor_columns(self, source: str) -> list[str]:
        return [
            column
            for column in self.describe(source).columns
            if column not in KEY_COLUMNS and column not in REQUIRED_COLUMNS
        ]

    def read_range(
        self,
        source: str,
        *,
        features: list[str],
        feature_sources: dict[str, str] | None = None,
        start: str | date,
        end: str | date,
        include_ohlcv: bool = True,
    ) -> pd.DataFrame:
        """Project raw source columns for a date range into an in-memory DataFrame."""
        status = self.assert_ready(source, start=start, end=end)
        available = set(status.columns)
        requested = list(dict.fromkeys(features))
        reserved = set(REQUIRED_COLUMNS) | {"trade_date", "dt"}
        if any(feature in reserved for feature in requested):
            raise QuantDBFactorError(
                "Mapped factor names cannot overwrite key or OHLCV columns"
            )
        if any(not _IDENTIFIER.fullmatch(feature) for feature in requested):
            raise QuantDBFactorError("Mapped factor names must be SQL identifiers")
        feature_sources = feature_sources or {}
        source_columns = {
            feature: feature_sources.get(feature, feature) for feature in requested
        }
        missing = [
            column for column in source_columns.values() if column not in available
        ]
        if missing:
            raise QuantDBFactorError(
                f"{source} is missing mapped fields: {', '.join(missing[:10])}"
            )

        factor_relation = self._relation(source)
        factor_date = self._qualified_date_expression(status.columns, "f")
        selected = [
            'f."symbol"',
            f"{factor_date} AS trade_date",
        ]
        daily_relation = self._daily_backward_relation()
        # 次要源（ccass/south 等）无 OHLCV 列：从同目录 l1_factors 补给行情，
        # 用于构建无泄漏的未来收益标签；含 OHLCV 的源不触发。
        ohlcv_donor = (
            self._ohlcv_donor_relation()
            if include_ohlcv and not set(OHLCV_COLUMNS) <= set(status.columns)
            else None
        )
        ohlcv_join = ohlcv_donor or daily_relation
        if include_ohlcv:
            for column in REQUIRED_COLUMNS[2:]:
                if column in status.columns:
                    factor_column = f"f.{_quote(column)}"
                    if ohlcv_join:
                        # 源内行情列优先，缺失部分由补给表补齐
                        # （CN: daily_backward 后复权日线；HK: l1_factors）。
                        selected.append(
                            f"COALESCE({factor_column}, k.{_quote(column)}) AS {_quote(column)}"
                        )
                    else:
                        selected.append(f"{factor_column} AS {_quote(column)}")
                elif ohlcv_join:
                    # 源无此行情列：直接取补给表（ccass/south 场景）
                    selected.append(f"k.{_quote(column)} AS {_quote(column)}")
        selected.extend(
            f"f.{_quote(source_column)} AS {_quote(feature)}"
            if source_column != feature
            else f"f.{_quote(feature)}"
            for feature, source_column in source_columns.items()
        )
        start_s, end_s = str(start)[:10], str(end)[:10]

        duckdb = self._duckdb()
        con = duckdb.connect(config={"memory_limit": "8GB", "threads": "4"})
        try:
            date_expr = factor_date
            from_clause = f"{factor_relation} AS f"
            if ohlcv_join:
                # factors.date 为实际交易日，补给表 dt 为 hive 分区整数。
                # 用日期格式化连接可同时兼容 int/string 两种 dt 物理类型。
                from_clause += (
                    f" LEFT JOIN {ohlcv_join} AS k"
                    " ON k.symbol = f.symbol"
                    f" AND CAST(k.dt AS VARCHAR) = strftime({date_expr}, '%Y%m%d')"
                )
            sql = (
                f"SELECT {', '.join(selected)} FROM {from_clause} "
                f"WHERE {date_expr} BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)"
            )
            frame = con.execute(sql, [start_s, end_s]).fetchdf()
        finally:
            con.close()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        # 先剔除空 symbol/日期，再归一化代码格式：str(None) 会把缺失值变成
        # "None" 字符串绕过 dropna（HK l1 坏分区曾缺 symbol 列，整日静默残留）。
        frame = frame.dropna(subset=["symbol", "trade_date"]).drop_duplicates(
            subset=["symbol", "trade_date"], keep="last"
        )
        # QuantDB may publish either suffix or prefix codes.  QuantMind's
        # canonical internal representation is the prefix form (SH600036),
        # including model inputs, prediction outputs, and persistence keys.
        frame["symbol"] = frame["symbol"].map(
            lambda value: StockCodeUtil.to_prefix(str(value))
        )
        return frame

    def read_day(
        self,
        source: str,
        *,
        features: list[str],
        trade_date: str | date,
        feature_sources: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        return self.read_range(
            source,
            features=features,
            feature_sources=feature_sources,
            start=trade_date,
            end=trade_date,
        )

    def available_dates(
        self, source: str, *, start: str | None = None, end: str | None = None
    ) -> list[str]:
        # 快速路径：直接读 hive 分区目录名（dt=YYYYMMDD），
        # 避免对全量 parquet 做 SELECT DISTINCT 扫描（440万行，30s+ 且阻塞事件循环）。
        root = self.source_path(source)
        dates: set[str] = set()
        if root.is_dir():
            for entry in root.iterdir():
                if entry.is_dir() and entry.name.startswith("dt="):
                    v = entry.name.split("=", 1)[1]
                    if v.isdigit() and len(v) == 8:
                        dates.add(f"{v[:4]}-{v[4:6]}-{v[6:]}")
        if dates:
            sorted_dates = sorted(dates)
            if start:
                sorted_dates = [d for d in sorted_dates if d >= start]
            if end:
                sorted_dates = [d for d in sorted_dates if d <= end]
            return sorted_dates

        # 兜底：非分区存储时退回 DuckDB 全表 DISTINCT 扫描
        status = self.assert_ready(source)
        duckdb = self._duckdb()
        con = duckdb.connect(config={"memory_limit": "2GB", "threads": "2"})
        try:
            date_expr = self._date_expression(status.columns)
            relation = self._relation(source)
            conditions, params = [], []
            if start:
                conditions.append(f"{date_expr} >= CAST(? AS DATE)")
                params.append(start)
            if end:
                conditions.append(f"{date_expr} <= CAST(? AS DATE)")
                params.append(end)
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = con.execute(
                f"SELECT DISTINCT {date_expr} AS d FROM {relation}{where} ORDER BY d",
                params,
            ).fetchall()
            return [str(row[0])[:10] for row in rows]
        finally:
            con.close()

    @staticmethod
    def forward_labels(
        frame: pd.DataFrame, *, horizon: int, signal_lag_days: int = 1
    ) -> pd.DataFrame:
        """Build labels from the source close column without persisting a derived dataset."""
        if "close" not in frame.columns:
            raise QuantDBFactorError(
                "close is required to construct direct-training labels"
            )
        data = frame[["symbol", "trade_date", "close"]].copy()
        data["close"] = pd.to_numeric(data["close"], errors="coerce")
        data = data[data["close"] > 0].sort_values(["symbol", "trade_date"])
        lag = max(0, int(signal_lag_days))
        horizon = max(1, int(horizon))
        execution_close = data.groupby("symbol")["close"].shift(-lag)
        future_close = data.groupby("symbol")["close"].shift(-(lag + horizon))
        data["label"] = future_close / execution_close - 1.0
        return data[["symbol", "trade_date", "label"]]
