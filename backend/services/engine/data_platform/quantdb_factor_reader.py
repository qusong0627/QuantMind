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
from collections.abc import Callable, Iterable, Mapping

import numpy as np
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
    "alpha_library": "6_ml_datasets/alpha_library",
}
DAILY_BACKWARD_DIR = "1_kline_data/daily_backward"
DEFAULT_FACTOR_SOURCE: FactorSource = "l1_factors"

# ── 市场 → 可用因子源映射（后台「模型训练数据集」与训练页数据源选择共用）───────
# 各市场 6_ml_datasets/ 下实际存在的训练直读数据集。
# CUSTOM 为用户自传市场：仅做因子扫描，不强制 OHLCV 完备性（见 describe）。
MARKET_FACTOR_SOURCES: dict[str, tuple[FactorSource, ...]] = {
    "CN": ("l1_factors", "l2_factors", "l1_l2_factors", "alpha_library"),
    "HK": ("l1_factors", "ccass_factors", "south_factors"),
    "US": ("l1_factors",),
    "CRYPTO": ("l1_factors",),
    "FUTURES": ("l1_factors",),
    "CUSTOM": ("l1_factors",),
}

# ── 6_ml_datasets 下的排除清单（2026-09-23 拆为两套语义）────────────────────
# 拆因：跨库组合选择（读时拼接，feature_sources 值写 "库:列"）要求 factor_defs /
# alpha_library 这类「清单库」能做训练直读；但它们在「刷新字段」自动发现里仍会
# 造成超长字段列表，所以「发现面」与「训练面」的排除范围不再相同。
#
# EXCLUDED_FROM_DISCOVERY —— 后台“刷新字段”/数据源列表不自动登记：
# - features_daily：含未来收益标签列（return_Nd）与 OHLCV 重复列；
# - alpha_library_labels：纯标签库；
# - alpha_library：历史策略预计算 Alpha 库（1300+ 清单性质）；
# - factor_defs：1300+ 条研究/报告同源清单库。
# EXCLUDED_FROM_TRAINING —— 读取层硬拦（标签/泄漏库，任何路径都不得当特征源）。
EXCLUDED_FROM_DISCOVERY: frozenset[str] = frozenset({
    "features_daily",
    "alpha_library_labels",
    "alpha_library",
    "factor_defs",
})
EXCLUDED_FROM_TRAINING: frozenset[str] = frozenset({
    "features_daily",
    "alpha_library_labels",
})
DEFAULT_FACTOR_SOURCE_BY_MARKET: dict[str, FactorSource] = {
    "CN": "l1_factors",
    "HK": "l1_factors",
    "US": "l1_factors",
    "CRYPTO": "l1_factors",
    "FUTURES": "l1_factors",
    "CUSTOM": "l1_factors",
}
# 各市场数据根目录环境变量（容器内路径，本地编排器挂载后亦可见）
MARKET_DATA_DIR_ENV: dict[str, str] = {
    "CN": "QM_QUANTDB_DATA_DIR",
    "HK": "QM_QUANTHK_DATA_DIR",
    "US": "QM_QUANTUS_DATA_DIR",
    "CRYPTO": "QM_QUANTBC_DATA_DIR",
    "FUTURES": "QM_QUANTFUTURES_DATA_DIR",
    "CUSTOM": "QM_QUANTCUSTOM_DATA_DIR",
}
MARKET_DATA_DIR_DEFAULT: dict[str, str] = {
    "CN": "/data/quantdb",
    "HK": "/data/quanthk",
    "US": "/data/quantus",
    "CRYPTO": "/data/quantbc",
    "FUTURES": "/data/quantfutures",
    "CUSTOM": "/data/quantcustom",
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


def split_qualified_source(value: str) -> tuple[str | None, str]:
    """解析 feature_sources 的值：``"库:列"`` → ``(库, 列)``；纯列名 → ``(None, 列)``。

    跨库组合读（库=包，读时拼接）的唯一语法点：值不写库名即锚库（``read_range`` 的
    ``source``），载荷与旧模型元数据逐字兼容；写库名则从该库 LEFT JOIN 取列。
    库与列均为 SQL 标识符，不含冒号，因此冒号可安全作为分隔符。
    """
    text = str(value or "").strip()
    if ":" not in text:
        return None, text
    lib, _, column = text.partition(":")
    return (lib.strip() or None), column.strip()


def split_features_by_availability(
    reader: QuantDBFactorReader,
    features: Iterable[str],
    feature_sources: Mapping[str, str] | None = None,
    *,
    anchor: str,
    columns_of: Callable[[str], Iterable[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """按库解析限定名并逐库检查列存在性，返回 ``(可取特征, 缺失特征)``。

    编排器（训练前过滤）/ 推理前置检查（script_runner）/ 实时推理（realtime_core）
    三处共用：跨库组合下不能再用锚库列名对照全部特征，否则副库特征会被整体误判为
    缺失。missing 保留调用方原始写法（含 ``"库:列"`` 限定形式），便于报错定位。

    ``columns_of`` 允许调用方注入带缓存的列查询（script_runner 的 ``describe``
    TTL 缓存）；不给则每库直接 ``reader.describe`` 一次。
    """
    feature_sources = feature_sources or {}

    def _columns(lib: str) -> set[str]:
        if columns_of is not None:
            return {str(c) for c in columns_of(lib)}
        return {str(c) for c in reader.describe(lib).columns}

    cache: dict[str, set[str]] = {}
    valid: list[str] = []
    missing: list[str] = []
    for feature in features:
        lib, column = split_qualified_source(feature)
        if lib is None:
            lib, column = split_qualified_source(feature_sources.get(feature, feature))
        lib = lib or anchor
        if lib not in cache:
            try:
                cache[lib] = _columns(lib)
            except Exception:  # noqa: BLE001 — 描述失败/未知副库 → 该库特征判缺失
                cache[lib] = set()
        (valid if column in cache[lib] else missing).append(feature)
    return valid, missing


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
        # 仅扫描模式判定用：CUSTOM 市场只扫描因子列，不强制 OHLCV。
        # 显式传 data_dir 且未传 market 时保持 CN 口径（历史行为不变）。
        self.market = normalize_market(market) if market is not None else "CN"

    def validate_source(self, source: str) -> str:
        """校验并放行因子数据集名。

        - 静态注册目录（FACTOR_SOURCE_DIRS）直接放行；
        - 未注册目录只要真实存在于该市场 6_ml_datasets/ 且命名合规即放行
          —— 未来新增因子数据集（如 xxx_factors）无需改代码；
        - 硬拦清单（EXCLUDED_FROM_TRAINING，标签/泄漏库）与非法命名给出明确拒绝原因。
        """
        if source not in FACTOR_SOURCE_DIRS:
            if source in EXCLUDED_FROM_TRAINING:
                raise QuantDBFactorError(
                    f"Factor dataset {source!r} is excluded from direct training "
                    "(label/leakage dataset; see EXCLUDED_FROM_TRAINING in quantdb_factor_reader)"
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
    def _partition_date_range(root: Path) -> tuple[str | None, str | None]:
        """从 dt=YYYYMMDD 分区目录名直接推导 min/max 日期，避免全表扫描。

        DESCRIBE + SELECT min/max 会打开 2581+ 个 parquet 做 union 推导，
        在请求路径同步执行耗时 50s+（前端 30s 超时 → ECONNABORTED）。
        分区名即日期，ls 目录 <0.2s。
        """
        if not root.is_dir():
            return None, None
        dates: list[str] = []
        try:
            for entry in root.iterdir():
                if entry.is_dir() and entry.name.startswith("dt="):
                    v = entry.name.split("=", 1)[1]
                    if v.isdigit() and len(v) == 8:
                        dates.append(f"{v[:4]}-{v[4:6]}-{v[6:]}")
        except OSError:
            return None, None
        if not dates:
            return None, None
        return min(dates), max(dates)

    @staticmethod
    def _sample_schema_relation(files: list[Path]) -> str:
        """用单个文件做 schema 采样，避免打开全量 2581 文件。

        发布分区 schema 一致，单文件足以推导列名；全量 DESCRIBE 只读 footer
        也要逐个开文件，耗时数秒~数十秒。刻意只取 1 个文件：多文件 UNION
        需要子查询别名，容易写出无效 SQL，且无额外收益。
        """
        p = files[0].as_posix().replace("'", "''")
        return f"read_parquet('{p}', hive_partitioning=true, union_by_name=true)"

    def _donor_has_ohlcv(self) -> bool:
        """检查 l1 donor 是否含 OHLCV：同样只采样 1 个文件，避免全扫。"""
        root = self.data_dir / FACTOR_SOURCE_DIRS[OHLCV_DONOR_SOURCE]
        if not root.is_dir():
            return False
        files = sorted(root.glob("dt=*/*.parquet"))
        if not files:
            return False
        duckdb = self._duckdb()
        con = duckdb.connect(config={"memory_limit": "2GB", "threads": "2"})
        try:
            rel = self._sample_schema_relation(files[:1])
            rows = con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
            cols = {str(r[0]) for r in rows}
            return set(OHLCV_COLUMNS) <= cols
        except Exception:  # noqa: BLE001
            return False
        finally:
            con.close()

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
        # 2026-09-23 去掉 union_by_name（原为「分区 schema 漂移容错」）：跨库组合读
        # （锚库 + 5 副库 ≈ 15,600 个分区文件）下，它会在绑定期打开**每一个文件**读
        # footer 做 schema 归一，实测单次查询 16.85G / 17.5s，且**与日期区间无关**
        # （单日 5,529 行与单月 116,335 行同为 16.6G——裁剪根本没省下这项）；去掉后
        # 2.57G / 6.9s，输出按 (symbol, trade_date) 归一后**逐位一致**。
        # 安全性：六库分区 schema 抽样 24 个/库，不一致 0；真出现漂移时 DuckDB 直接报
        # "schema mismatch in glob"（响亮失败，不会静默少列）。反向理由见
        # scripts/generate_feature_snapshots.py：union 出来的「老分区 NULL / 新分区
        # 非 NULL」列正是年代指示器，一旦被选入模型就是静默泄露。
        return f"read_parquet('{parquet_glob}', hive_partitioning=true)"

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
        # union_by_name 同 _relation 去掉：全量 glob 下它按文件读 footer 归一 schema，
        # 是每次查询的固定开销（与区间无关），且 union 会造年代指示器列。
        return f"read_parquet('{parquet_glob}', hive_partitioning=true)"

    def _ohlcv_donor_relation(self) -> str | None:
        """返回同目录 l1_factors 关系，作为无 OHLCV 次要源（ccass/south）的行情补给。

        与全库统一，严格读取已发布的 dt=YYYYMMDD/ 分区布局，避免误把顶层
        残留单文件 glob 进来造成 "Hive partition mismatch"。
        """
        root = self.data_dir / FACTOR_SOURCE_DIRS[OHLCV_DONOR_SOURCE]
        if not root.is_dir() or not any(root.glob("dt=*/*.parquet")):
            return None
        parquet_glob = str(root / "dt=*" / "*.parquet").replace("'", "''")
        # union_by_name 同 _relation 去掉（全量 glob 的固定绑定开销 + 年代指示器）
        return f"read_parquet('{parquet_glob}', hive_partitioning=true)"

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

        # 快路径：min/max 先走分区目录名（<0.2s），避免 SELECT 全表扫描 50s+
        part_min, part_max = self._partition_date_range(root)

        duckdb = self._duckdb()
        con = duckdb.connect(config={"memory_limit": "2GB", "threads": "2"})
        try:
            # schema 只采样首/中/末 3 文件，避免 DESCRIBE 打开全量 2581 文件
            sampled = self._sample_schema_relation(files)
            described = con.execute(f"DESCRIBE SELECT * FROM {sampled}").fetchall()
            columns = [str(row[0]) for row in described]
            column_types = {str(row[0]): str(row[1]) for row in described}
            if part_min is not None and part_max is not None:
                date_row = (part_min, part_max)
            else:
                # 兜底：非分区存储才回退全表 min/max 扫描
                relation = self._relation(source)
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
        if self.market == "CUSTOM":
            # 自定义市场（用户自传数据）：仅扫描因子列，不强制 OHLCV 完备性。
            # 有分区文件即 ready；标签构建仍需数据源自带 close 列，否则训练时按缺列报错。
            missing = []
        elif "date" in missing and ("dt" in columns or "time" in columns):
            missing.remove("date")  # dt 分区列或 time 列即日期（HK l1_factors/alpha_library 无 date 列）
        reason = None
        if missing and set(missing) <= set(OHLCV_COLUMNS):
            # 次要源（ccass/south）：OHLCV 由同目录 l1_factors 补给，标签可构建。
            # 用采样检查代替全量 _relation_columns，避免又一次全扫。
            if self._donor_has_ohlcv():
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
          （排除 EXCLUDED_FROM_DISCOVERY），未来新增因子数据集无需改代码。
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
            if name in FACTOR_SOURCE_DIRS or name in EXCLUDED_FROM_DISCOVERY or name in known:
                continue
            dynamic.append(name)
        return known + dynamic

    @staticmethod
    def _date_expression(columns: Iterable[str]) -> str:
        cols = set(columns)
        if "date" in cols:
            return 'CAST("date" AS DATE)'
        if "dt" in cols:
            return "strptime(CAST(\"dt\" AS VARCHAR), '%Y%m%d')::DATE"
        if "time" in cols:
            return 'CAST("time" AS DATE)'
        raise QuantDBFactorError("Factor source has neither date nor dt")

    @staticmethod
    def _qualified_date_expression(columns: Iterable[str], alias: str) -> str:
        """与 _date_expression 相同，但返回带表别名的安全 SQL 表达式。"""
        cols = set(columns)
        if "date" in cols:
            return f'CAST({alias}."date" AS DATE)'
        if "dt" in cols:
            return f"strptime(CAST({alias}.\"dt\" AS VARCHAR), '%Y%m%d')::DATE"
        if "time" in cols:
            return f'CAST({alias}."time" AS DATE)'
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
        feature_sources = feature_sources or {}
        # 逻辑名/输出列名 → (库, 原始列)。两种等价写法：
        # ① features 里直接写限定名 "库:列"（输出列名取裸列名）；
        # ② features 写裸名 + feature_sources[名] = "库:列"（输出列名 = 该裸名）。
        # 值不写库名即锚库同名列 —— 旧载荷（恒等映射）逐字兼容。
        resolved: list[tuple[str, str, str]] = []
        extra_refs: dict[str, list[str]] = {}
        seen_aliases: set[str] = set()
        for feature in requested:
            explicit_lib, explicit_column = split_qualified_source(feature)
            if explicit_lib is not None:
                lib, column, alias = explicit_lib, explicit_column, explicit_column
            else:
                lib, column = split_qualified_source(
                    feature_sources.get(feature, feature)
                )
                alias = feature
            lib = lib or source
            if not _IDENTIFIER.fullmatch(alias):
                raise QuantDBFactorError(
                    f"Mapped factor names must be SQL identifiers: {alias!r}"
                )
            if not _IDENTIFIER.fullmatch(column):
                raise QuantDBFactorError(f"Invalid QuantDB column name: {column!r}")
            if alias in seen_aliases:
                raise QuantDBFactorError(f"Duplicate factor alias: {alias!r}")
            seen_aliases.add(alias)
            resolved.append((alias, lib, column))
            if lib != source:
                extra_refs.setdefault(lib, []).append(column)
        missing = [
            column
            for feature, lib, column in resolved
            if lib == source and column not in available
        ]
        if missing:
            raise QuantDBFactorError(
                f"{source} is missing mapped fields: {', '.join(missing[:10])}"
            )
        # 副库（非锚库）按库校验：库名合法、分区存在、列存在。
        extra_columns: dict[str, set[str]] = {}
        for lib in sorted(extra_refs):
            self.validate_source(lib)
            if not self._files(lib):
                raise QuantDBFactorError(
                    f"Secondary factor dataset {lib!r} has no published partitions"
                )
            extra_columns[lib] = set(self.describe(lib).columns)
        missing_extra = [
            f"{lib}:{column}"
            for lib in sorted(extra_refs)
            for column in dict.fromkeys(extra_refs[lib])
            if column not in extra_columns[lib]
        ]
        if missing_extra:
            raise QuantDBFactorError(
                "Secondary factor datasets are missing mapped fields: "
                f"{', '.join(missing_extra[:10])}"
            )
        lib_alias = {lib: f"s{i}" for i, lib in enumerate(sorted(extra_refs))}

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
        # 行情列**名单**与下面的 SELECT 必须由同一分支产出（单一事实源）：名单一旦与
        # SELECT 脱钩，预分配块里对应的列就永远不被写入，输出会带上 np.empty 的
        # 未初始化内存（`include_ohlcv=False` 且补给表存在时就会这样）。
        # 补给表存在时 6 列全出（源内没有的走 k 侧）；否则只出源内自有的——
        # 此前只统计锚库自有行情列，会把补给列读出来又丢掉，HK ccass/south 直读时
        # close 等列因此在返回帧里消失。
        ohlcv_names: list[str] = []
        if include_ohlcv:
            ohlcv_names = (
                list(OHLCV_COLUMNS)
                if ohlcv_join
                else [c for c in OHLCV_COLUMNS if c in status.columns]
            )
            for column in REQUIRED_COLUMNS[2:]:
                if column in status.columns:
                    factor_column = f"f.{_quote(column)}"
                    if ohlcv_join:
                        # 源内行情列优先，缺失部分由补给表补齐
                        # （CN: daily_backward 后复权日线；HK: l1_factors）。
                        selected.append(
                            f"CAST(COALESCE({factor_column}, k.{_quote(column)}) AS FLOAT) AS {_quote(column)}"
                        )
                    else:
                        selected.append(f"CAST({factor_column} AS FLOAT) AS {_quote(column)}")
                elif ohlcv_join:
                    # 源无此行情列：直接取补给表（ccass/south 场景）
                    selected.append(f"CAST(k.{_quote(column)} AS FLOAT) AS {_quote(column)}")
        # 数值因子列在 DuckDB 侧直接降为 FLOAT(float32)：
        # 429 因子 × 全历史长表在 float64 下 ≈ 42GB+，超出训练容器 48GB mem_limit
        # 会被 OOM(SIGKILL 137) 杀死（2026-08-29 实测 train_20260829065659_af1fcf16）。
        # 训练/推理/IC 计算全部接受 float32，精度损失可忽略；调用方无需再降精度。
        factor_names = [feature for feature, _lib, _column in resolved]
        selected.extend(
            f"CAST({'f' if lib == source else lib_alias[lib]}.{_quote(column)} AS FLOAT)"
            f" AS {_quote(feature)}"
            for feature, lib, column in resolved
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
            # 副库（跨库组合读）：同款 symbol+dt 左连接；行集仍由锚库决定，
            # 副库缺该标的日子留空 → NaN，走既有缺失处理。
            # 区间谓词必须挂在 ON 上而非 WHERE：WHERE 里引用右表列会把 LEFT JOIN
            # 退化成 INNER JOIN，副库覆盖不到的标的日会整行丢失（2026-09-23 实测
            # 182,312 vs 锚库 183,744）。
            join_aliases: list[tuple[str, str]] = []
            for lib, alias in lib_alias.items():
                from_clause += (
                    f" LEFT JOIN {self._relation(lib)} AS {alias}"
                    f" ON {alias}.symbol = f.symbol"
                    f" AND CAST({alias}.dt AS VARCHAR) = strftime({date_expr}, '%Y%m%d')"
                    # 按月分块显式限定副库 dt 区间：仅靠连接条件里的 strftime，
                    # 优化器无法把 f.date 的区间下推到副库扫描（每分块全量扫副库）。
                    f" AND CAST({alias}.dt AS VARCHAR) BETWEEN ? AND ?"
                )
                join_aliases.append((lib, alias))
            base_sql = (
                f"SELECT {', '.join(selected)} FROM {from_clause} "
                f"WHERE {date_expr} BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)"
            )
            # ── 预分配 + 分块读取（2026-08-29 修复）───────────────────────
            # 全历史 × 429 因子 float32 帧 ≈ 22GB。单次 fetchdf 峰值实测
            # 53.9GB；分块+pd.concat 峰值仍 50.2GB（concat 复制累积帧），
            # 均超出训练容器 48GB mem_limit 被 OOM(SIGKILL 137) 杀死。
            # 方案：先 count 总行数 → 预分配 float32 numpy → 按月查询、
            # 块内清洗后直接填入，零复制累积，峰值 ≈ 最终帧 + 单块。
            # 组装段（2026-09-23 二次修复）：pandas 2.3 下 pd.concat(axis=1,
            # copy=False) 仍要付「一份拷贝 + 一份瞬时」＝2 份整帧（2M×283 实测
            # HWM +4.30G/2.11G 帧），全量 10.72M×283 即 ~24G，是读取段 44G 尖峰
            # 的主因。改为「单块 2D 数组建帧 + insert 元数据列」：实测零拷贝。
            # 行数以锚库为准（不含 LEFT JOIN）：连接不会增行（副库/补给表按
            # symbol+dt 唯一），以锚库计数做预分配上界，块内去重后只会更少。
            count_sql = (
                f"SELECT count(*) FROM {factor_relation} AS f "
                f"WHERE {date_expr} BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)"
            )
            total_rows = int(con.execute(count_sql, [start_s, end_s]).fetchone()[0])
            sym_arr = np.empty(total_rows, dtype=object)
            date_arr = np.empty(total_rows, dtype="datetime64[ns]")
            # 因子列与行情列放**同一块** float32 矩阵：单块建帧才是零拷贝，也避免
            # 「273 因子 = 273 块碎片」那种布局（碎片会让后续取行先合并、内存翻倍）。
            n_factor = len(factor_names)
            block = np.empty((total_rows, n_factor + len(ohlcv_names)), dtype=np.float32)
            pos = 0
            date_list = self.available_dates(source, start=start_s, end=end_s)
            for month in sorted({d[:7] for d in date_list}):
                month_days = [d for d in date_list if d.startswith(month)]
                m_lo, m_hi = month_days[0], month_days[-1]  # 实际交易日边界
                # 占位符按 SQL 文本顺序绑定：FROM 里的副库区间在前，WHERE 日期在后
                chunk_args: list[str] = []
                if join_aliases:
                    lo_compact, hi_compact = m_lo.replace("-", ""), m_hi.replace("-", "")
                    for _lib, _alias in join_aliases:
                        chunk_args += [lo_compact, hi_compact]
                chunk_args += [m_lo, m_hi]
                chunk = con.execute(base_sql, chunk_args).fetchdf()
                chunk["trade_date"] = pd.to_datetime(chunk["trade_date"], errors="coerce")
                chunk = chunk.dropna(subset=["symbol", "trade_date"]).drop_duplicates(
                    subset=["symbol", "trade_date"], keep="last"
                )
                chunk["symbol"] = chunk["symbol"].map(
                    lambda value: StockCodeUtil.to_prefix(str(value))
                )
                n = len(chunk)
                if n == 0:
                    continue
                sym_arr[pos:pos + n] = chunk["symbol"].values
                date_arr[pos:pos + n] = chunk["trade_date"].values
                for i, name in enumerate(factor_names):
                    block[pos:pos + n, i] = chunk[name].values
                for i, name in enumerate(ohlcv_names):
                    # 不设 `if name in chunk.columns` 守卫：走到这里就说明 SELECT 与
                    # 名单同源，列必在；守卫反而是「静默跳过 + 留下未初始化内存」。
                    block[pos:pos + n, n_factor + i] = chunk[name].values
                pos += n
            # 组装成**单块**连续帧：pandas 会把「逐列 1D 切片」各建一个块（273 因子=
            # 273 块碎片），后续 sort/filter/groupby 每次都要先合并碎片，大表
            # （8.5M×279）直读时峰值内存翻倍、实测 OOM(SIGKILL 137)。2D 数组
            # 直接交给 DataFrame 则是单块，copy=False 让帧零复制共享预分配缓冲。
            # 元数据两列走 insert（各建一块，只拷自身），**不 concat**——concat
            # 在 pandas 2.3 下要额外付 2 份整帧的瞬时内存（见上方组装段注释）。
            frame = pd.DataFrame(
                block[:pos],
                columns=[*factor_names, *ohlcv_names],
                copy=False,
            )
            frame.insert(0, "symbol", pd.Series(sym_arr[:pos], dtype=object))
            frame.insert(1, "trade_date", pd.Series(date_arr[:pos]))
        finally:
            con.close()
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
