"""
QuantDB 数据中枢 — A 股所有数据读取的单一入口。

基于本地 parquet 文件（data/quantdb/）+ DuckDB 提供高效查询：
- 分区数据（日K、估值、技术指标等）使用 DuckDB 谓词下推
- 单文件数据（财务报表、分钟K线）使用 pd.read_parquet
- 懒加载 DuckDB 视图，线程安全

数据目录结构（与 QuantDB SDK sync_dataset 输出一致）：
  data/quantdb/
    1_kline_data/daily_{forward,backward,unadjusted}/dt=YYYYMMDD/data.parquet
    1_kline_data/index_daily/dt=YYYYMMDD/data.parquet
    1_kline_data/min1_kline/{symbol}.parquet
    1_kline_data/min5_kline/{symbol}.parquet
    2_base_sector/instrument_detail/instrument_detail.parquet
    2_base_sector/sector_concept/sector_members.parquet
    2_base_sector/trading_calendar/
    2_base_sector/index_weights/
    2_base_sector/margin_trading/dt=YYYYMMDD/data.parquet
    3_financial_data/{balance,income,cashflow,...}/{symbol}.parquet
    5_technical_derived/valuation/dt=YYYYMMDD/data.parquet
    5_technical_derived/technical_indicators/dt=YYYYMMDD/data.parquet
    5_technical_derived/market_sentiment/dt=YYYYMMDD/data.parquet
    6_ml_datasets/features_daily/dt=YYYYMMDD/data.parquet
    6_ml_datasets/l1_factors/l1_factors_YYYYMMDD.parquet or dt=YYYYMMDD/
    6_ml_datasets/l2_factors/dt=YYYYMMDD/data.parquet
    6_ml_datasets/l1_l2_factors/dt=YYYYMMDD/data.parquet
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 环境变量：QuantDB 数据目录
_QUANTDB_DATA_DIR_ENV = "QM_QUANTDB_DATA_DIR"

# 默认数据目录（项目根相对 / 容器内绝对路径 / 本地盘符）
_DEFAULT_DATA_DIRS = [
    "/data/quantdb",  # Docker 容器内（挂载点）
    "/app/data/quantdb",  # Docker 容器内
    "D:/quant_data",  # Windows 本地开发常用盘符
    str(Path(__file__).resolve().parents[4] / "data" / "quantdb"),  # 项目根/data/quantdb
]


def _resolve_data_dir() -> Path:
    """解析 QuantDB 数据目录路径。"""
    env_val = os.getenv(_QUANTDB_DATA_DIR_ENV, "").strip()
    if env_val:
        p = Path(env_val)
        if p.is_dir() and any(p.iterdir()):
            return p
        logger.warning("QM_QUANTDB_DATA_DIR=%s 不存在或为空，尝试默认路径", env_val)

    for d in _DEFAULT_DATA_DIRS:
        p = Path(d)
        if p.is_dir() and any(p.iterdir()):
            return p

    # 从 __file__ 向上推算项目根
    project_root = Path(__file__).resolve().parents[4]
    fallback = project_root / "data" / "quantdb"
    if fallback.is_dir() and any(fallback.iterdir()):
        return fallback

    # 最后返回默认路径（让后续方法报错更清晰）
    return Path(_DEFAULT_DATA_DIRS[-1])


# ---------------------------------------------------------------------------
# 列名映射：QuantDB parquet → QuantMind 规范
# ---------------------------------------------------------------------------
_COLUMN_RENAMES: dict[str, str] = {
    "time": "trade_date",
    "wind_code": "symbol",
}

# volinstock / vol_in_stock → volume，仅在 volume 列不存在时才重命名
_VOLUME_ALIASES = {"volinstock", "vol_in_stock"}


def _dt_conditions(start: date | None, end: date | None, col: str = "dt") -> list[str]:
    """生成 dt 列的过滤条件（dt 在 Hive partitioning 中为整数 YYYYMMDD）。"""
    conditions = []
    if start:
        conditions.append(f"{col} >= {start.strftime('%Y%m%d')}")
    if end:
        conditions.append(f"{col} <= {end.strftime('%Y%m%d')}")
    return conditions


class QuantDBDataHub:
    """A 股数据中枢 — 所有数据读取的单一入口。"""

    _instance: Optional[QuantDBDataHub] = None
    _instance_lock = threading.Lock()

    def __init__(self, data_dir: str | Path | None = None) -> None:
        if data_dir is not None:
            self._data_dir = Path(data_dir)
        else:
            self._data_dir = _resolve_data_dir()
        self._local = threading.local()
        self._views_mounted_per_conn: set[int] = set()  # track which conn ids have views mounted

    @classmethod
    def get_instance(cls) -> QuantDBDataHub:
        """获取全局单例（懒初始化，线程安全）。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def available(self) -> bool:
        """数据目录是否存在且包含数据。"""
        return self._data_dir.is_dir() and any(self._data_dir.iterdir())

    def warm_up(self) -> None:
        """预初始化 DuckDB 连接和视图，消除首次查询延迟。

        应在服务启动时调用（在主线程中），这样后续请求无需等待视图注册。
        """
        if not self.available:
            logger.info("QuantDB data not available, skipping warm-up")
            return
        try:
            conn = self._get_duck_conn()
            conn.execute("SELECT 1")
            logger.info("QuantDB DuckDB warm-up complete")
        except Exception as exc:
            logger.warning("QuantDB warm-up failed (non-fatal): %s", exc)


class QuantDBDataHub:
    """A 股数据中枢 — 所有数据读取的单一入口。

    用法：
        hub = QuantDBDataHub()
        df = hub.fetch_daily_kline("600036.SH", date(2024,1,1), date(2024,12,31))
        df = hub.fetch_valuation(symbol="600036.SH", start=date(2024,1,1))
        df = hub.fetch_l1_factors(start=date(2024,1,1), end=date(2024,6,30))
    """

    _instance: Optional[QuantDBDataHub] = None
    _instance_lock = threading.Lock()

    def __init__(self, data_dir: str | Path | None = None) -> None:
        if data_dir is not None:
            self._data_dir = Path(data_dir)
        else:
            self._data_dir = _resolve_data_dir()
        self._local = threading.local()
        self._views_mounted_per_conn: set[int] = set()  # track which conn ids have views mounted

    @classmethod
    def get_instance(cls) -> QuantDBDataHub:
        """获取全局单例（懒初始化，线程安全）。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def available(self) -> bool:
        """数据目录是否存在且包含数据。"""
        return self._data_dir.is_dir() and any(self._data_dir.iterdir())

    def warm_up(self) -> None:
        """预初始化 DuckDB 连接和视图，消除首次查询延迟。

        应在服务启动时调用（在主线程中），这样后续请求无需等待视图注册。
        """
        if not self.available:
            logger.info("QuantDB data not available, skipping warm-up")
            return
        try:
            conn = self._get_duck_conn()
            conn.execute("SELECT 1")
            logger.info("QuantDB DuckDB warm-up complete")
        except Exception as exc:
            logger.warning("QuantDB warm-up failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # DuckDB 连接管理（线程安全）
    # ------------------------------------------------------------------
    def _get_duck_conn(self):
        """获取当前线程的 DuckDB 连接。"""
        if not hasattr(self._local, "duck_conn") or self._local.duck_conn is None:
            try:
                import duckdb
            except ImportError:
                raise RuntimeError("duckdb 未安装，请运行 pip install duckdb")
            self._local.duck_conn = duckdb.connect(":memory:")
            self._mount_views(self._local.duck_conn)
            self._views_mounted_per_conn.add(id(self._local.duck_conn))
        return self._local.duck_conn

    def _mount_views(self, conn, force: bool = False) -> None:
        """为分区数据创建 DuckDB 视图（懒加载，首次查询时才读文件）。

        force=True 时忽略已挂载标记强制重建，用于“连接创建后数据目录才补齐”
        导致视图未挂载的场景（否则该连接会一直报 Table does not exist）。
        """
        conn_id = id(conn)
        if not force and conn_id in self._views_mounted_per_conn:
            return
        dd = self._data_dir

        # 分区数据视图（dt=YYYYMMDD Hive partitioning）
        partitioned_views = {
            "qdb_daily_forward": "1_kline_data/daily_forward",
            "qdb_daily_backward": "1_kline_data/daily_backward",
            "qdb_daily_unadjusted": "1_kline_data/daily_unadjusted",
            "qdb_index_daily": "1_kline_data/index_daily",
            "qdb_valuation": "5_technical_derived/valuation",
            "qdb_technical_indicators": "5_technical_derived/technical_indicators",
            "qdb_market_sentiment": "5_technical_derived/market_sentiment",
            "qdb_features_daily": "6_ml_datasets/features_daily",
            "qdb_margin_trading": "2_base_sector/margin_trading",
            "qdb_l2_factors": "6_ml_datasets/l2_factors",
            "qdb_l1_l2_factors": "6_ml_datasets/l1_l2_factors",
        }
        # l1_factors has mixed format (flat files + partitioned dirs); only read partitioned dirs
        l1_dir = dd / "6_ml_datasets" / "l1_factors"
        if l1_dir.exists() and any(l1_dir.glob("dt=*")):
            partitioned_views["qdb_l1_factors"] = "6_ml_datasets/l1_factors"
        # alpha_library: Alpha101 + GTJA191 + Alpha158 三库因子（429 列，训练直读）
        alpha_dir = dd / "6_ml_datasets" / "alpha_library"
        if alpha_dir.exists() and any(alpha_dir.glob("dt=*")):
            partitioned_views["qdb_alpha_library"] = "6_ml_datasets/alpha_library"

        for view_name, rel_path in partitioned_views.items():
            full_path = dd / rel_path
            if not full_path.exists():
                continue
            # 分区数据集统一只读 dt=YYYYMMDD/ 目录，避免混入平铺的 per-symbol
            parquet_glob = str(full_path / "dt=*" / "*.parquet").replace("\\", "/")
            if next(full_path.glob("dt=*"), None) is None:
                continue  # 无分区目录则跳过
            try:
                conn.execute(
                    f"CREATE VIEW IF NOT EXISTS {view_name} AS "
                    f"SELECT * FROM read_parquet('{parquet_glob}', hive_partitioning=1, union_by_name=true)"
                )
            except Exception as exc:
                logger.warning("创建 DuckDB 视图 %s 失败: %s", view_name, exc)

        # 非分区 per-symbol parquet 视图（无 dt= Hive 分区）
        per_symbol_views = {
            "qdb_hsgt_north_daily": "2_base_sector/hsgt_north/daily_freq/*.parquet",
        }
        for view_name, parquet_rel in per_symbol_views.items():
            glob_path = dd / parquet_rel
            glob_dir = str(glob_path).replace("\\", "/")
            if next(glob_path.parent.glob("*.parquet"), None) is None:
                continue  # 目录为空则跳过
            try:
                conn.execute(
                    f"CREATE VIEW IF NOT EXISTS {view_name} AS "
                    f"SELECT * FROM read_parquet('{glob_dir}', union_by_name=true)"
                )
            except Exception as exc:
                logger.warning("创建 DuckDB 视图 %s 失败: %s", view_name, exc)

        # 北向资金季度快照视图（quarter=YYYYQN Hive 分区，2024-08 起季度披露）
        north_quarter_dir = str(dd / "2_base_sector" / "hsgt_north" / "quarter=*" / "data.parquet").replace("\\", "/")
        if list((dd / "2_base_sector" / "hsgt_north").glob("quarter=*")):
            try:
                conn.execute(
                    "CREATE VIEW IF NOT EXISTS qdb_hsgt_north AS "
                    f"SELECT * FROM read_parquet('{north_quarter_dir}', "
                    "hive_partitioning=1, union_by_name=true)"
                )
            except Exception as exc:
                logger.warning("创建 DuckDB 视图 qdb_hsgt_north 失败: %s", exc)

        self._views_mounted_per_conn.add(conn_id)
    # ------------------------------------------------------------------
    # 精确分区路径读取（避免 dt=* glob 全分区枚举）
    # ------------------------------------------------------------------
    # 视图名 → 分区数据相对路径（供精确路径读取复用）
    _VIEW_REL_MAP: dict[str, str] = {
        "qdb_daily_forward": "1_kline_data/daily_forward",
        "qdb_daily_backward": "1_kline_data/daily_backward",
        "qdb_daily_unadjusted": "1_kline_data/daily_unadjusted",
        "qdb_index_daily": "1_kline_data/index_daily",
        "qdb_valuation": "5_technical_derived/valuation",
        "qdb_technical_indicators": "5_technical_derived/technical_indicators",
        "qdb_market_sentiment": "5_technical_derived/market_sentiment",
        "qdb_features_daily": "6_ml_datasets/features_daily",
        "qdb_margin_trading": "2_base_sector/margin_trading",
        "qdb_l2_factors": "6_ml_datasets/l2_factors",
        "qdb_l1_l2_factors": "6_ml_datasets/l1_l2_factors",
        "qdb_l1_factors": "6_ml_datasets/l1_factors",
        "qdb_alpha_library": "6_ml_datasets/alpha_library",
    }

    def _exact_conn(self):
        """线程内复用的裸 DuckDB 连接（不挂载 dt=* glob 视图）。

        精确路径查询（read_parquet([...])）不需要视图；复用全局视图连接反而要
        为每个新线程先 CREATE 13 个 glob 视图（首查实测 35s）。这里只留一个
        空连接，复用 parquet 元数据缓存即可。
        """
        conn = getattr(self._local, "exact_conn", None)
        if conn is None:
            try:
                import duckdb
            except ImportError as exc:
                raise RuntimeError("duckdb 未安装，请运行 pip install duckdb") from exc
            conn = duckdb.connect()
            self._local.exact_conn = conn
        return conn

    def _partition_dates(
        self,
        rel_path: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[str]:
        """目录枚举分区日期（YYYYMMDD，升序），不读任何 parquet 内容。"""
        dd = self._data_dir / rel_path
        if not dd.is_dir():
            return []
        start_s = start.strftime("%Y%m%d") if start else ""
        end_s = end.strftime("%Y%m%d") if end else "99999999"
        dates: list[str] = []
        try:
            for entry in dd.iterdir():
                if not entry.is_dir() or not entry.name.startswith("dt="):
                    continue
                val = entry.name[3:]
                if val.isdigit() and start_s <= val <= end_s:
                    dates.append(val)
        except OSError as exc:
            logger.warning("读取分区日期列表失败 %s: %s", rel_path, exc)
            return []
        return sorted(dates)

    def _read_partitioned(
        self,
        rel_path: str,
        dates: list[str],
        cols: str = "*",
        where_sql: str = "",
        order_by: str = "",
        bind: list | None = None,
    ) -> pd.DataFrame:
        """按精确分区路径读取指定交易日。

        与 ``dt=*`` glob + ``WHERE dt IN (...)`` 等价，但 DuckDB 不必枚举全部分区：
        实测 daily_unadjusted 两天 0.78s → 0.007s，l2 全历史 22.5s → 单日 0.04s。
        """
        base = self._data_dir / rel_path
        existing = [d for d in dates if (base / f"dt={d}").is_dir()]
        if not existing:
            return pd.DataFrame()
        paths = ", ".join(
            f"'{str(base / f'dt={d}' / '*.parquet').replace(chr(92), '/')}'"
            for d in existing
        )
        sql = f"SELECT {cols}, dt FROM read_parquet([{paths}], hive_partitioning=true)"
        if where_sql:
            sql += f" WHERE {where_sql}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        try:
            return self._exact_conn().execute(sql, bind or []).fetchdf()
        except Exception as exc:
            logger.warning("精确分区查询失败 %s: %s", rel_path, exc)
            return pd.DataFrame()

    def fetch_latest_rows(
        self,
        view: str,
        symbols: list[str],
        dt: int | None = None,
        lookback: int = 100,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """按精确分区路径读取指定视图每个 symbol 的最新一行。

        dt（YYYYMMDD 整数）传入时读取「不晚于该日的最新行」；缺省读取全库最新
        分区。columns 传入时做列裁剪（只 SELECT symbol + 指定列），避免宽表
        （l2/l1 等 100+ 列）整表扫描。返回 DataFrame（含 rn 过滤后的最新行）。
        """
        rel = self._VIEW_REL_MAP.get(view)
        if rel is None:
            return pd.DataFrame()
        dates = self._partition_dates(rel)
        if not dates:
            return pd.DataFrame()
        if dt is not None:
            low = str(dt - lookback)
            high = str(dt)
            dates = [d for d in dates if low <= d <= high]
        else:
            dates = dates[-lookback:]
        if not dates or not symbols:
            return pd.DataFrame()
        base = self._data_dir / rel
        existing = [d for d in dates if (base / f"dt={d}").is_dir()]
        if not existing:
            return pd.DataFrame()
        paths = ", ".join(
            f"'{str(base / f'dt={d}' / '*.parquet').replace(chr(92), '/')}'"
            for d in existing
        )
        placeholders = ", ".join("?" for _ in symbols)
        if columns:
            # 内层必须带 dt 供 ROW_NUMBER 排序与外层输出；不做列裁剪时 SELECT * 已含 dt
            select_cols = ["symbol", "dt"] + [f'"{c}"' for c in columns if c not in ("symbol", "dt")]
            inner_sel = ", ".join(
                select_cols + ["ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY dt DESC) AS rn"]
            )
            outer_sel = ", ".join(select_cols)
            sql = f"""
                SELECT {outer_sel} FROM (
                    SELECT {inner_sel}
                    FROM read_parquet([{paths}], hive_partitioning=true)
                    WHERE symbol IN ({placeholders})
                ) WHERE rn = 1
            """
        else:
            sql = f"""
                SELECT * EXCLUDE (rn) FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY dt DESC) AS rn
                    FROM read_parquet([{paths}], hive_partitioning=true)
                    WHERE symbol IN ({placeholders})
                ) WHERE rn = 1
            """
        try:
            return self._exact_conn().execute(sql, list(symbols)).fetchdf()
        except Exception as exc:
            logger.warning("最新行查询失败 %s: %s", view, exc)
            return pd.DataFrame()

    def fetch_series(
        self,
        view: str,
        symbol: str,
        start_dt: int,
        end_dt: int | None = None,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """按精确分区路径读取单 symbol 在某日期范围的时序数据。

        view 为 qdb_* 视图名，映射到对应分区数据集；仅打开 start_dt..end_dt
        范围内的分区文件（替代原 ``FROM qdb_xxx WHERE dt BETWEEN ...`` 冷扫全分区）。
        columns 传入时做列裁剪。返回含 dt 列（YYYYMMDD 整数）的 DataFrame。
        """
        rel = self._VIEW_REL_MAP.get(view)
        if rel is None:
            return pd.DataFrame()
        start = date(int(str(start_dt)[:4]), int(str(start_dt)[4:6]), int(str(start_dt)[6:8]))
        end = None
        if end_dt:
            end = date(int(str(end_dt)[:4]), int(str(end_dt)[4:6]), int(str(end_dt)[6:8]))
        dates = self._partition_dates(rel, start, end)
        if not dates:
            return pd.DataFrame()
        where_sql = f"symbol = '{symbol}'"
        cols = "*"
        if columns:
            # _read_partitioned 会自动追加 dt 列，这里只选 symbol + 业务列
            select = ["symbol"] + [c for c in columns if c not in ("symbol", "dt")]
            cols = ", ".join(select)
        df = self._read_partitioned(rel, dates, cols=cols, where_sql=where_sql, order_by="dt")
        if df.empty:
            return df
        df["dt"] = df["dt"].astype(int)
        return df

    # ------------------------------------------------------------------
    # 通用查询
    # ------------------------------------------------------------------
    def query(self, sql: str) -> pd.DataFrame:
        """直接执行 DuckDB SQL 查询（高级用法）。

        若本线程连接创建时数据目录尚为空、视图未挂载（后续数据补齐），
        遇到 Catalog “Table/View ... does not exist” 时强制重建视图并重试一次。
        防止长驻进程在数据补齐后一直报不存在。
        """
        conn = self._get_duck_conn()
        try:
            return conn.execute(sql).fetchdf()
        except Exception as exc:
            msg = str(exc)
            if "does not exist" in msg and (
                "catalog" in msg.lower() or "table with name" in msg.lower()
            ):
                self._mount_views(conn, force=True)
                return conn.execute(sql).fetchdf()
            raise

    # ------------------------------------------------------------------
    # K线数据
    # ------------------------------------------------------------------
    def fetch_daily_kline(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """读取日线 K 线。

        Args:
            symbol: 股票代码 (suffix 格式 600036.SH)
            start: 开始日期
            end: 结束日期
            adjust: 复权方式 qfq=前复权, hfq=后复权, none=不复权

        Returns:
            DataFrame with columns: symbol, trade_date, open, high, low, close, volume, amount
        """
        view_map = {"qfq": "daily_forward", "hfq": "daily_backward", "none": "daily_unadjusted"}
        subdir = view_map.get(adjust, "daily_forward")

        dates = self._partition_dates(f"1_kline_data/{subdir}", start, end)
        if not dates:
            return pd.DataFrame()

        df = self._read_partitioned(
            f"1_kline_data/{subdir}",
            dates,
            where_sql=f"symbol = '{symbol}'",
            order_by="dt",
        )
        if df.empty:
            return df

        return self._normalize_kline(df)

    def fetch_daily_kline_batch(
        self,
        symbols: list[str],
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """批量读取多只股票的日线 K 线（单次查询，避免逐股票扫描分区）。

        Returns:
            DataFrame with columns: symbol, trade_date, open, high, low, close, volume, amount
        """
        view_map = {
            "qfq": "daily_forward",
            "hfq": "daily_backward",
            "none": "daily_unadjusted",
        }
        subdir = view_map.get(adjust, "daily_forward")

        if not symbols:
            return pd.DataFrame()

        dates = self._partition_dates(f"1_kline_data/{subdir}", start, end)
        if not dates:
            return pd.DataFrame()

        placeholders = ", ".join("?" for _ in symbols)
        df = self._read_partitioned(
            f"1_kline_data/{subdir}",
            dates,
            where_sql=f"symbol IN ({placeholders})",
            order_by="symbol, dt",
            bind=symbols,
        )
        if df.empty:
            return df

        return self._normalize_kline(df)

    def fetch_index_kline(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """读取指数日线 K 线。"""
        dates = self._partition_dates("1_kline_data/index_daily", start, end)
        if not dates:
            return pd.DataFrame()

        df = self._read_partitioned(
            "1_kline_data/index_daily",
            dates,
            where_sql=f"symbol = '{symbol}'",
            order_by="dt",
        )
        return self._normalize_kline(df)

    def fetch_minute_kline(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        freq: str = "1min",
    ) -> pd.DataFrame:
        """读取分钟 K 线（单文件 per-symbol parquet）。"""
        subdir = "min1_kline" if freq == "1min" else "min5_kline"
        file_path = self._data_dir / "1_kline_data" / subdir / f"{symbol}.parquet"
        if not file_path.exists():
            return pd.DataFrame()

        df = pd.read_parquet(file_path)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"])
            # 日期范围过滤
            mask = (df["time"].dt.date >= start) & (df["time"].dt.date <= end)
            df = df.loc[mask]

        return self._normalize_kline(df)

    # ------------------------------------------------------------------
    # 基础/板块数据
    # ------------------------------------------------------------------
    def fetch_stock_list(self) -> pd.DataFrame:
        """读取股票列表。"""
        base_dir = self._data_dir / "2_base_sector" / "instrument_detail"
        for fname in ("instrument_detail.parquet", "instrument_list.parquet"):
            file_path = base_dir / fname
            if file_path.exists():
                df = pd.read_parquet(file_path)
                # 统一列名：Symbol -> symbol
                if "Symbol" in df.columns and "symbol" not in df.columns:
                    df = df.rename(columns={"Symbol": "symbol"})
                return df
        return pd.DataFrame()

    def fetch_instrument_industry(self) -> pd.DataFrame:
        """读取股票行业分类映射（CSRC 一级行业）。

        返回 DataFrame 包含: symbol, ind_name_l1, ind_code_l1
        """
        file_path = self._data_dir / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet"
        if not file_path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(file_path)
        # QuantDB instrument_detail 包含 rs_hyname(行业名称) 和 rs_hycode_sim(行业代码)
        col_map = {}
        if "rs_hyname" in df.columns:
            col_map["rs_hyname"] = "ind_name_l1"
        if "rs_hycode_sim" in df.columns:
            col_map["rs_hycode_sim"] = "ind_code_l1"
        if not col_map:
            return pd.DataFrame()
        symbol_col = (
            "symbol" if "symbol" in df.columns
            else "wind_code" if "wind_code" in df.columns
            else "Symbol" if "Symbol" in df.columns  # QuantDB 实测为大写 Symbol
            else None
        )
        if symbol_col is None:
            return pd.DataFrame()
        result = df[[symbol_col] + list(col_map.keys())].rename(columns={**col_map, symbol_col: "symbol"})
        # 行业编码转为整数（CatBoost cat_features 需要）
        if "ind_code_l1" in result.columns:
            result["ind_code_l1"] = pd.Categorical(result["ind_code_l1"]).codes
        return result.dropna(subset=["ind_name_l1"])

    def fetch_sector_members(self, sector_name: str | None = None) -> pd.DataFrame:
        """读取板块成分。

        兼容两套列名（小写规范列 + 原始大写列）与两种文件名。
        """
        base_dir = self._data_dir / "2_base_sector" / "sector_concept"
        for fname in ("sector_members.parquet", "sector_member.parquet"):
            file_path = base_dir / fname
            if file_path.exists():
                df = pd.read_parquet(file_path)
                break
        else:
            return pd.DataFrame()
        if df.empty:
            return df
        # 统一为小写列名
        rename = {
            "SectorCode": "sector_code",
            "SectorName": "sector_name",
            "SectorType": "sector_type",
            "Symbol": "symbol",
        }
        df = df.rename(columns=rename)
        if sector_name and "sector_name" in df.columns:
            df = df[df["sector_name"].astype(str) == sector_name]
        return df

    def fetch_calendar(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取交易日历。"""
        cal_dir = self._data_dir / "2_base_sector" / "trading_calendar"
        if not cal_dir.exists():
            return pd.DataFrame()

        # 交易日历可能是单个 parquet 或分区格式
        parquet_files = list(cal_dir.glob("*.parquet"))
        if not parquet_files:
            # 分区格式
            parquet_files = list(cal_dir.glob("**/*.parquet"))

        if not parquet_files:
            return pd.DataFrame()

        df = pd.concat([pd.read_parquet(f) for f in parquet_files], ignore_index=True)

        # 日期过滤
        date_col = None
        for col in ("trade_date", "date", "time", "cal_date", "TradingDate"):
            if col in df.columns:
                date_col = col
                break

        if date_col and (start or end):
            df[date_col] = pd.to_datetime(df[date_col])
            if start:
                df = df[df[date_col] >= pd.Timestamp(start)]
            if end:
                df = df[df[date_col] <= pd.Timestamp(end)]

        return df

    def fetch_index_weights(self, index_symbol: str) -> pd.DataFrame:
        """读取指数权重。"""
        iw_dir = self._data_dir / "2_base_sector" / "index_weights"
        if not iw_dir.exists():
            return pd.DataFrame()

        # 优先读取单指数文件；回退到合并文件。二者内容重叠，读取其一避免重复行。
        per_index_file = iw_dir / f"{index_symbol}.parquet"
        if per_index_file.exists():
            parquet_files = [per_index_file]
        else:
            parquet_files = sorted(set(iw_dir.glob("**/*.parquet")))
        if not parquet_files:
            return pd.DataFrame()

        df = pd.concat([pd.read_parquet(f) for f in parquet_files], ignore_index=True)

        # 按指数过滤（列名随数据源不同，IndexCode 为 QuantDB 实际列名）
        for col in ("IndexCode", "index_symbol", "index_code"):
            if col in df.columns:
                df = df[df[col] == index_symbol]
                break

        # 同一成分可能在多份快照中重复出现，按成分代码去重
        symbol_col = next(
            (c for c in ("Symbol", "symbol", "wind_code", "ConstituentCode") if c in df.columns),
            None,
        )
        if symbol_col:
            df = df.drop_duplicates(subset=[symbol_col])

        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # 财务数据
    # ------------------------------------------------------------------
    def fetch_financial(
        self,
        symbol: str,
        statement_type: str = "income",
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取财务报表。

        Args:
            symbol: 股票代码 (600036.SH)
            statement_type: balance / income / cashflow / capital / pershare_index / dividend_factors / holder_num
            start: 报告期起始
            end: 报告期结束
        """
        file_path = self._data_dir / "3_financial_data" / statement_type / f"{symbol}.parquet"
        if not file_path.exists():
            return pd.DataFrame()

        df = pd.read_parquet(file_path)

        # 日期过滤（按公告日 m_anntime 或报告期 m_timetag；holder_num 用 declareDate）
        if start or end:
            date_col = next(
                (c for c in ("m_anntime", "m_timetag", "declareDate", "endDate")
                 if c in df.columns),
                None,
            )
            if date_col:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                if start:
                    df = df[df[date_col] >= pd.Timestamp(start)]
                if end:
                    df = df[df[date_col] <= pd.Timestamp(end)]

        # 重命名 Symbol 列
        if "Symbol" in df.columns:
            df = df.rename(columns={"Symbol": "symbol"})

        return df

    def fetch_dividend_factors(self, symbol: str) -> pd.DataFrame:
        """读取分红因子。"""
        return self.fetch_financial(symbol, statement_type="dividend_factors")

    # ------------------------------------------------------------------
    # 技术衍生数据
    # ------------------------------------------------------------------
    def fetch_valuation(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取估值指标。"""
        dates = self._partition_dates("5_technical_derived/valuation", start, end)
        if not dates:
            return pd.DataFrame()

        where_sql = f"symbol = '{symbol}'" if symbol else ""
        df = self._read_partitioned(
            "5_technical_derived/valuation",
            dates,
            where_sql=where_sql,
            order_by="dt",
        )
        return self._normalize_columns(df)

    def fetch_technical_indicators(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取技术指标。"""
        dates = self._partition_dates("5_technical_derived/technical_indicators", start, end)
        if not dates:
            return pd.DataFrame()

        where_sql = f"symbol = '{symbol}'" if symbol else ""
        df = self._read_partitioned(
            "5_technical_derived/technical_indicators",
            dates,
            where_sql=where_sql,
            order_by="dt",
        )
        return self._normalize_columns(df)

    def fetch_market_sentiment(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取市场情绪。"""
        dates = self._partition_dates("5_technical_derived/market_sentiment", start, end)
        if not dates:
            return pd.DataFrame()

        where_sql = f"symbol = '{symbol}'" if symbol else ""
        df = self._read_partitioned(
            "5_technical_derived/market_sentiment",
            dates,
            where_sql=where_sql,
            order_by="dt",
        )
        return self._normalize_columns(df)

    # ------------------------------------------------------------------
    # ML 数据集
    # ------------------------------------------------------------------
    def fetch_features_daily(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取日频特征（已合并技术指标+估值）。"""
        dates = self._partition_dates("6_ml_datasets/features_daily", start, end)
        if not dates:
            return pd.DataFrame()

        where_sql = f"symbol = '{symbol}'" if symbol else ""
        df = self._read_partitioned(
            "6_ml_datasets/features_daily",
            dates,
            where_sql=where_sql,
            order_by="dt",
        )
        return self._normalize_columns(df)

    def fetch_l1_factors(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取 L1 因子（101 列，因子挖掘核心数据）。

        L1 factors 有两种存储格式：
        1. 分区格式: l1_factors/dt=YYYYMMDD/data.parquet
        2. 平铺格式: l1_factors/l1_factors_YYYYMMDD.parquet
        """
        l1_dir = self._data_dir / "6_ml_datasets" / "l1_factors"
        if not l1_dir.exists():
            return pd.DataFrame()

        # 尝试分区格式
        dates = self._partition_dates("6_ml_datasets/l1_factors", start, end)
        if dates:
            df = self._read_partitioned(
                "6_ml_datasets/l1_factors",
                dates,
                order_by="dt",
            )
            if not df.empty:
                return self._normalize_columns(df)

        # 平铺格式: l1_factors_YYYYMMDD.parquet
        pattern = "l1_factors_*.parquet"
        files = sorted(l1_dir.glob(pattern))
        if not files:
            return pd.DataFrame()

        # 过滤文件名中的日期
        dfs = []
        for f in files:
            # 从文件名提取日期: l1_factors_20160104.parquet
            date_str = f.stem.replace("l1_factors_", "")
            try:
                file_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
            except (ValueError, IndexError):
                continue
            if start and file_date < start:
                continue
            if end and file_date > end:
                continue
            dfs.append(pd.read_parquet(f))

        if not dfs:
            return pd.DataFrame()

        df = pd.concat(dfs, ignore_index=True)
        return self._normalize_columns(df)

    def fetch_alpha_factors(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取 Alpha 库因子（a101_* / gtja_* / a158_*，429 列，训练直读）。

        分区格式: alpha_library/dt=YYYYMMDD/data.parquet（float32）
        """
        dates = self._partition_dates("6_ml_datasets/alpha_library", start, end)
        if not dates:
            return pd.DataFrame()
        df = self._read_partitioned(
            "6_ml_datasets/alpha_library",
            dates,
            order_by="dt",
        )
        return self._normalize_columns(df)

    def fetch_l2_factors(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取 L2 高频因子。"""
        dates = self._partition_dates("6_ml_datasets/l2_factors", start, end)
        if not dates:
            return pd.DataFrame()
        df = self._read_partitioned(
            "6_ml_datasets/l2_factors",
            dates,
            order_by="dt",
        )
        return self._normalize_columns(df)

    # ------------------------------------------------------------------
    # 融资融券
    # ------------------------------------------------------------------
    def fetch_margin_trading(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取融资融券数据。"""
        dates = self._partition_dates("2_base_sector/margin_trading", start, end)
        if not dates:
            return pd.DataFrame()

        where_sql = f"symbol = '{symbol}'" if symbol else ""
        df = self._read_partitioned(
            "2_base_sector/margin_trading",
            dates,
            where_sql=where_sql,
            order_by="dt",
        )
        return self._normalize_columns(df)

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _view_exists(self, view_name: str) -> bool:
        """检查 DuckDB 视图是否存在（尝试查询验证）。"""
        try:
            conn = self._get_duck_conn()
            conn.execute(f"SELECT 1 FROM {view_name} LIMIT 0")
            return True
        except Exception:
            return False

    def _normalize_kline(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化 K 线列名。"""
        if df.empty:
            return df
        df = df.rename(columns=_COLUMN_RENAMES)
        # 处理 volume 别名：仅在 volume 列不存在时才重命名
        for alias in _VOLUME_ALIASES:
            if alias in df.columns and "volume" not in df.columns:
                df = df.rename(columns={alias: "volume"})
            elif alias in df.columns and "volume" in df.columns:
                # volume 已存在，删除别名列（通常为 0）
                df = df.drop(columns=[alias])
        # 确保 trade_date 是日期类型
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        # 删除元数据列
        meta_cols = ["release_id", "published_at"]
        df = df.drop(columns=[c for c in meta_cols if c in df.columns], errors="ignore")
        return df

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """通用列名标准化。"""
        if df.empty:
            return df
        df = df.rename(columns=_COLUMN_RENAMES)
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        # 删除元数据列
        meta_cols = ["release_id", "published_at"]
        df = df.drop(columns=[c for c in meta_cols if c in df.columns], errors="ignore")
        return df

    def _read_daily_kline_from_files(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """直接从 parquet 文件读取日线（DuckDB 视图不可用时的 fallback）。"""
        subdir_map = {"qfq": "daily_forward", "hfq": "daily_backward", "none": "daily_unadjusted"}
        subdir = subdir_map.get(adjust, "daily_forward")
        base_dir = self._data_dir / "1_kline_data" / subdir

        if not base_dir.exists():
            return pd.DataFrame()

        # 按日期分区读取
        start_str = start.strftime("%Y%m%d")
        end_str = end.strftime("%Y%m%d")

        dfs = []
        for dt_dir in sorted(base_dir.iterdir()):
            if not dt_dir.is_dir() or not dt_dir.name.startswith("dt="):
                continue
            dt_val = dt_dir.name[3:]  # 去掉 dt= 前缀
            if dt_val < start_str or dt_val > end_str:
                continue
            parquet_files = list(dt_dir.glob("*.parquet"))
            for pf in parquet_files:
                try:
                    chunk = pd.read_parquet(pf)
                    if "symbol" in chunk.columns:
                        chunk = chunk[chunk["symbol"] == symbol]
                    if not chunk.empty:
                        dfs.append(chunk)
                except Exception as exc:
                    logger.warning("读取 %s 失败: %s", pf, exc)

        if not dfs:
            return pd.DataFrame()

        df = pd.concat(dfs, ignore_index=True)
        return self._normalize_kline(df)

    # ------------------------------------------------------------------
    # 宇宙/股票池
    # ------------------------------------------------------------------
    UNIVERSE_MAP: dict[str, str | None] = {
        "csi300": "000300.SH",
        "csi500": "000905.SH",
        "csi1000": "000852.SH",
        "sse50": "000016.SH",
        "gem": "399006.SZ",
        "star": "000688.SH",
        "csi800": "000906.SH",
        "all_a": None,
    }

    UNIVERSE_NAMES: dict[str, str] = {
        "csi300": "沪深300",
        "csi500": "中证500",
        "csi1000": "中证1000",
        "sse50": "上证50",
        "gem": "创业板",
        "star": "科创板",
        "csi800": "中证800",
        "all_a": "全部A股",
    }

    def fetch_universe_stocks(self, universe: str) -> pd.DataFrame:
        """返回指定股票池的股票代码列表。

        Args:
            universe: 股票池标识 (csi300, csi500, csi1000, sse50, gem, star, csi800, all_a)

        Returns:
            DataFrame with columns: [symbol, weight]，按 weight 降序排列。
        """
        index_symbol = self.UNIVERSE_MAP.get(universe)

        if index_symbol is None and universe == "all_a":
            # 从 instrument_detail 获取所有 A 股
            df = self.fetch_stock_list()
            if df.empty:
                return pd.DataFrame()
            symbol_col = "Symbol" if "Symbol" in df.columns else "symbol"
            if symbol_col not in df.columns:
                return pd.DataFrame()
            result = pd.DataFrame({
                "symbol": df[symbol_col].dropna().unique(),
                "weight": 1.0,
            })
            return result

        if index_symbol is None:
            logger.warning("Unknown universe: %s", universe)
            return pd.DataFrame()

        df = self.fetch_index_weights(index_symbol)
        if df.empty:
            return pd.DataFrame()

        # 找到 symbol 和 weight 列
        symbol_col = None
        for col in ("Symbol", "symbol", "wind_code", "ConstituentCode"):
            if col in df.columns:
                symbol_col = col
                break
        weight_col = None
        for col in ("Weight", "weight", "weight_ratio"):
            if col in df.columns:
                weight_col = col
                break

        if symbol_col is None:
            return pd.DataFrame()

        result = pd.DataFrame({"symbol": df[symbol_col]})
        if weight_col:
            result["weight"] = df[weight_col].astype(float)
        else:
            result["weight"] = 1.0

        return result.sort_values("weight", ascending=False).reset_index(drop=True)

    def fetch_l1_factor_categories(self) -> dict:
        """返回按类别分组的 L1 因子类别（从 feature catalog 加载）。

        Returns:
            {"categories": [{"id": str, "name": str, "feature_count": int, "sample_features": [str]}]}
        """
        catalog_path = (
            Path(__file__).resolve().parents[4]
            / "config" / "features" / "model_training_feature_catalog_v1.json"
        )
        if not catalog_path.exists():
            return self._fallback_l1_categories()

        try:
            import json as _json
            with open(catalog_path, encoding="utf-8") as f:
                catalog = _json.load(f)
        except Exception as exc:
            logger.warning("Failed to load feature catalog: %s", exc)
            return self._fallback_l1_categories()

        categories = catalog.get("categories", [])
        if not categories:
            return self._fallback_l1_categories()

        result = []
        for cat in categories:
            features = cat.get("features", [])
            sample = [f.get("key", "") for f in features[:5] if f.get("key")]
            result.append({
                "id": cat.get("id", ""),
                "name": cat.get("name", ""),
                "feature_count": len(features),
                "sample_features": sample,
            })
        return {"categories": result}

    @staticmethod
    def _fallback_l1_categories() -> dict:
        """L1 因子类别兜底（当 catalog 文件不可用时）。"""
        return {
            "categories": [
                {"id": "momentum", "name": "动量", "feature_count": 24,
                 "sample_features": ["mom_ret_1d", "mom_ret_5d", "mom_ret_20d", "mom_ma_gap_5", "mom_rsi_14"]},
                {"id": "volatility", "name": "波动率", "feature_count": 11,
                 "sample_features": ["vol_std_5", "vol_std_20", "vol_atr_14", "vol_parkinson_10", "vol_gk_20"]},
                {"id": "liquidity", "name": "流动性", "feature_count": 12,
                 "sample_features": ["liq_volume", "liq_amount", "liq_volume_ma_5", "liq_obv_20", "liq_mfi_14"]},
                {"id": "technical", "name": "技术指标", "feature_count": 6,
                 "sample_features": ["tech_bb_width", "tech_bb_pos", "tech_cci_20", "tech_adx_14", "tech_vol_price_corr_20"]},
                {"id": "fundamental", "name": "基本面", "feature_count": 12,
                 "sample_features": ["fun_turnover_1", "fun_mv", "fun_pe", "fun_pb", "fun_roe"]},
                {"id": "style", "name": "风格因子", "feature_count": 9,
                 "sample_features": ["style_beta_20", "style_idio_vol_20", "style_residual_ret_20", "style_size_20", "style_value_20"]},
                {"id": "industry", "name": "行业因子", "feature_count": 14,
                 "sample_features": ["ind_ret_5", "ind_strength_20", "ind_dispersion_20", "ind_breadth_up_20", "ind_crowding_20"]},
                {"id": "chip", "name": "筹码", "feature_count": 9,
                 "sample_features": ["chip_profit_ratio_20", "chip_concentration_20", "chip_peak_distance", "chip_floating_ratio", "chip_cost_90_width"]},
                {"id": "concept", "name": "概念", "feature_count": 11,
                 "sample_features": ["concept_hot_score", "concept_momentum_top3", "concept_exposure_top1", "concept_rotation_score", "concept_crowding_max"]},
            ]
        }

    def get_data_summary(self) -> dict:
        """返回 QuantDB 数据可用性摘要，供前端使用。

        Returns:
            {"date_range": {...}, "universes": {...}, "stock_count": int, "datasets": {...}}
        """
        summary: dict = {"available": self.available, "data_dir": str(self._data_dir)}
        if not self.available:
            return summary

        dd = self._data_dir

        # 日期范围（从 daily_forward 分区目录推断）
        daily_dir = dd / "1_kline_data" / "daily_forward"
        if daily_dir.exists():
            dates = sorted([d.name[3:] for d in daily_dir.iterdir() if d.name.startswith("dt=")])
            if dates:
                summary["date_range"] = {
                    "start": f"{dates[0][:4]}-{dates[0][4:6]}-{dates[0][6:8]}",
                    "end": f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:8]}",
                    "trading_days": len(dates),
                }

        # 股票池信息
        universes: dict = {}
        for universe_id, index_symbol in self.UNIVERSE_MAP.items():
            if universe_id == "all_a":
                universes[universe_id] = {
                    "name": self.UNIVERSE_NAMES.get(universe_id, universe_id),
                    "index_symbol": None,
                    "count": 0,
                }
                continue
            df = self.fetch_index_weights(index_symbol or "")
            count = len(df) if not df.empty else 0
            universes[universe_id] = {
                "name": self.UNIVERSE_NAMES.get(universe_id, universe_id),
                "index_symbol": index_symbol,
                "count": count,
            }
        # all_a 的股票数
        stock_list = self.fetch_stock_list()
        if not stock_list.empty:
            symbol_col = "Symbol" if "Symbol" in stock_list.columns else "symbol"
            if symbol_col in stock_list.columns:
                stock_count = stock_list[symbol_col].nunique()
                universes["all_a"]["count"] = stock_count
                summary["stock_count"] = stock_count
        summary["universes"] = universes

        # 数据集信息
        datasets: dict = {}
        l1_dir = dd / "6_ml_datasets" / "l1_factors"
        if l1_dir.exists():
            categories_info = self.fetch_l1_factor_categories()
            cat_names = [c["name"] for c in categories_info.get("categories", [])]
            datasets["l1_factors"] = {
                "columns": 101,
                "categories": cat_names,
                "category_count": len(cat_names),
            }
        l2_dir = dd / "6_ml_datasets" / "l2_factors"
        if l2_dir.exists():
            datasets["l2_factors"] = {"columns": 219}
        ti_dir = dd / "5_technical_derived" / "technical_indicators"
        if ti_dir.exists():
            datasets["technical_indicators"] = {"columns": 37}
        val_dir = dd / "5_technical_derived" / "valuation"
        if val_dir.exists():
            datasets["valuation"] = {"columns": 18}
        ms_dir = dd / "5_technical_derived" / "market_sentiment"
        if ms_dir.exists():
            datasets["market_sentiment"] = {"columns": 19}
        fd_dir = dd / "6_ml_datasets" / "features_daily"
        if fd_dir.exists():
            datasets["features_daily"] = {"columns": 52}
        summary["datasets"] = datasets

        return summary

    # ------------------------------------------------------------------
    # 数据摘要
    # ------------------------------------------------------------------
    def get_summary(self) -> dict:
        """返回数据目录摘要信息。"""
        summary: dict = {
            "data_dir": str(self._data_dir),
            "available": self.available,
        }
        if not self.available:
            return summary

        dd = self._data_dir
        categories = {
            "kline_data": "1_kline_data",
            "base_sector": "2_base_sector",
            "financial_data": "3_financial_data",
            "technical_derived": "5_technical_derived",
            "ml_datasets": "6_ml_datasets",
        }
        for key, rel_path in categories.items():
            cat_dir = dd / rel_path
            if cat_dir.exists():
                # 统计 parquet 文件数
                count = sum(1 for _ in cat_dir.rglob("*.parquet"))
                # 统计大小
                total_size = sum(f.stat().st_size for f in cat_dir.rglob("*.parquet") if f.is_file())
                summary[key] = {
                    "parquet_files": count,
                    "size_mb": round(total_size / 1024 / 1024, 1),
                }
            else:
                summary[key] = {"parquet_files": 0, "size_mb": 0}

        # 日期范围（从 daily_forward 推断）
        daily_dir = dd / "1_kline_data" / "daily_forward"
        if daily_dir.exists():
            dates = sorted([d.name[3:] for d in daily_dir.iterdir() if d.name.startswith("dt=")])
            if dates:
                summary["date_range"] = {"start": dates[0], "end": dates[-1], "trading_days": len(dates)}

        return summary
