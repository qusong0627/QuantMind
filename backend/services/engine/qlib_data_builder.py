"""
Qlib 数据构建器 — 从各市场 parquet 生成 Qlib 格式缓存。

Qlib binary 格式要求：
- calendars/day.txt: 每行一个日期 YYYY-MM-DD
- instruments/all.txt: tab 分隔 symbol\\tstart_date\\tend_date
- features/{symbol}/{field}.day.bin: 4-byte float32 start_idx + N*4-byte float32 values

各市场 parquet 是 single source of truth，Qlib 缓存是派生产物，可随时重建。
支持 CN / US / HK / CRYPTO / FUTURES 五市场，通过 Hub 注入实现通用化。
"""

from __future__ import annotations

import logging
import struct
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.shared.qlib_multiplicative_factor import (
    EventBook,
    board_limit,
    build_multiplicative_factor,
    covered_dates,
    unexplained_ex_dates,
)

logger = logging.getLogger(__name__)

# Qlib 期望的字段列表（A 股额外补 change，CnExchange 靠它判定涨跌停）
QLIB_FIELDS = ["open", "high", "low", "close", "volume", "amount", "factor"]

# QuantDB parquet 列名 -> Qlib 字段名映射
_KLINE_COL_MAP = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
}

# 各市场的 Qlib symbol 前缀规则。
# A 股是 Qlib 原生格式 sh600036（exchange+code）；其他市场用 {market}_ 前缀
# + 原始 symbol（保留大小写与点号），反向剥离前缀即可无损还原。
_MARKET_QLIB_PREFIX: dict[str, str] = {
    "CN": "",  # 特殊：A 股用原生 exchange 前缀（sh600036），不走统一前缀
    "US": "us_",
    "HK": "hk_",
    "CRYPTO": "bc_",
    "FUTURES": "fut_",
}

# A 股额外纳入 Qlib 的指数（index_daily 数据，原生代码格式）
# 沪深300 / 中证500 / 中证1000
CN_QLIB_INDEX_SYMBOLS: list[str] = [
    "000300.SH",
    "000905.SH",
    "000852.SH",
]

_MARKET_HUB_FACTORY = {}


def _register_market_hub(market: str, builder_cls: type):
    """注册市场对应的 Hub 工厂（避免循环 import）。"""
    _MARKET_HUB_FACTORY[market.upper()] = builder_cls


class QlibDataBuilder:
    """从各市场 parquet 构建 Qlib binary 缓存。

    用法：
        builder = QlibDataBuilder(hub, qlib_dir="data/quantdb/.qlib_cache/cn_data")
        builder.build_all()
    """

    def __init__(
        self,
        hub: QuantDBDataHub,
        qlib_dir: str | Path,
        market: str = "CN",
    ) -> None:
        self._hub = hub
        self._qlib_dir = Path(qlib_dir)
        self._market = market.upper()
        self._qlib_prefix = _MARKET_QLIB_PREFIX.get(self._market, "")
        self._events: EventBook | None = None  # 除权事件表（仅 CN 需要）

    @classmethod
    def for_market(
        cls,
        market: str,
        data_dir: str | Path | None = None,
        qlib_dir: str | Path | None = None,
    ) -> "QlibDataBuilder":
        """根据市场创建对应数据中枢的构建器。

        market: CN / US / HK / CRYPTO / FUTURES
        data_dir: 数据目录（默认按市场解析）
        qlib_dir: Qlib 输出目录（默认 {data_dir}/.qlib_cache/{market}_data）
        """
        from backend.services.engine.data_platform import quantus_hub, quanthk_hub, quantbc_hub, quantfutures_hub

        market_upper = market.upper()
        _qlib_sub = {
            "CN": "cn_data",
            "US": "us_data",
            "HK": "hk_data",
            "CRYPTO": "bc_data",
            "FUTURES": "futures_data",
        }[market_upper]
        if market_upper == "CN":
            if data_dir is None:
                data_dir = _resolve_cn_data_dir()
            hub = QuantDBDataHub(data_dir)
        elif market_upper == "US":
            if data_dir is None:
                data_dir = quantus_hub._resolve_quantus_data_dir()
            hub = quantus_hub.QuantUSDataHub(data_dir)
        elif market_upper == "HK":
            if data_dir is None:
                data_dir = quanthk_hub._resolve_quanthk_data_dir()
            hub = quanthk_hub.QuantHKDataHub(data_dir)
        elif market_upper == "CRYPTO":
            if data_dir is None:
                data_dir = quantbc_hub._resolve_quantbc_data_dir()
            hub = quantbc_hub.QuantBCDataHub(data_dir)
        elif market_upper == "FUTURES":
            if data_dir is None:
                data_dir = quantfutures_hub._resolve_quantfutures_data_dir()
            hub = quantfutures_hub.QuantFuturesDataHub(data_dir)
        else:
            raise ValueError(f"未知市场: {market_upper}")

        # 默认输出目录：容器布局(/data/qlib)存在时沿用；否则写入
        # {data_dir}/.qlib_cache/（裸机/便携部署无 /data 写权限，
        # 且 resolve_qlib_provider_uri 识别 .qlib_cache 路径，读写一致）
        if Path("/data/qlib").is_dir():
            default_qlib = Path(f"/data/qlib/{_qlib_sub}")
        else:
            default_qlib = Path(data_dir) / ".qlib_cache" / _qlib_sub

        return cls(hub, qlib_dir or default_qlib, market=market_upper)

    @property
    def qlib_dir(self) -> Path:
        return self._qlib_dir

    @property
    def hub(self) -> QuantDBDataHub:
        return self._hub

    def build_all(
        self,
        *,
        incremental: bool = True,
        symbols: list[str] | None = None,
        progress_cb=None,
    ) -> dict:
        """构建全部 Qlib 数据。

        Args:
            progress_cb: 可选 `progress_cb(progress:int)`（0-100），按阶段与特征批次回调，
                         供上层展示进度与维持任务心跳。回调异常被吞掉，不影响构建。

        Returns:
            {"calendar": int, "instruments": int, "features": int, "skipped": int}
        """
        def _notify(p: int) -> None:
            if callable(progress_cb):
                try:
                    progress_cb(max(0, min(100, p)))
                except Exception:  # noqa: BLE001 - 进度回调非关键路径
                    pass

        if not self._hub.available:
            raise RuntimeError(f"数据目录不可用: {self._hub.data_dir}")

        result: dict = {}

        # 1. 构建交易日历
        _notify(2)
        result["calendar"] = self.build_calendar()

        # 2. 构建标的列表
        _notify(5)
        result["instruments"] = self.build_instruments()

        # 3. 构建特征 binary
        _notify(8)
        feat_result = self.build_features(
            symbols=symbols, incremental=incremental, progress_cb=progress_cb
        )
        result["features"] = feat_result["updated"]
        result["skipped"] = feat_result["skipped"]
        _notify(95)

        logger.info(
            "QlibDataBuilder[%s] 完成: calendar=%d, instruments=%d, features=%d, skipped=%d",
            self._market, result["calendar"], result["instruments"],
            result["features"], result["skipped"],
        )
        return result

    # ------------------------------------------------------------------
    # 日历与标的（通用化：优先 hub 专用方法，缺失则从 parquet 推导）
    # ------------------------------------------------------------------
    def build_calendar(self) -> int:
        """从交易日历或 parquet 日期列生成 calendars/day.txt。"""
        cal_dir = self._qlib_dir / "calendars"
        cal_dir.mkdir(parents=True, exist_ok=True)
        cal_file = cal_dir / "day.txt"

        df = self._hub.fetch_calendar()
        dates = self._extract_dates(df) if not df.empty else None

        # 上游日历发布可能滞后于行情分区（如 QuantDB trading_calendar 落后 daily_forward 1-2 天）。
        # 取日历与行情日期的并集，保证日历覆盖最新行情，避免回测区间截断。
        parquet_dates = self._dates_from_recent_parquet()
        if dates and parquet_dates:
            merged = set(dates) | set(parquet_dates)
            if merged != set(dates):
                logger.info(
                    "Qlib[%s] 日历补齐行情日期: %d -> %d",
                    self._market, len(dates), len(merged),
                )
            dates = sorted(merged)
        elif not dates:
            # hub 日历缺失时（US/HK/BC/FUTURES 首次构建），全量行情推导
            dates = self._dates_from_parquet()

        if not dates:
            logger.warning("%s 交易日历为空", self._market)
            return 0

        with open(cal_file, "w") as f:
            f.write("\n".join(dates) + "\n")

        logger.info("Qlib[%s] calendar: %d trading days -> %s", self._market, len(dates), cal_file)
        return len(dates)

    def _extract_dates(self, df: pd.DataFrame) -> list[str] | None:
        """从 hub fetch_calendar 返回的 DataFrame 提取日期列表。"""
        date_col = None
        for col in ("trade_date", "date", "time", "cal_date", "TradingDate", "trading_date"):
            if col in df.columns:
                date_col = col
                break
        if date_col is None:
            return None
        dates = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d").unique()
        return sorted(dates.tolist()) if len(dates) else None

    def _dates_from_recent_parquet(self, recent_days: int = 14) -> list[str]:
        """从 daily_forward 最近分区推导交易日（只扫最新分区，避免全表扫描）。"""
        fwd = self._hub.data_dir / "1_kline_data" / "daily_forward"
        if not fwd.is_dir():
            return []
        import duckdb

        partitions = sorted(p.name for p in fwd.glob("dt=*"))[-recent_days:]
        if not partitions:
            return []
        files = ",".join(f"'{fwd / p / 'data.parquet'}'" for p in partitions)
        try:
            con = duckdb.connect(config={"memory_limit": "4GB", "threads": "2"})
            try:
                df = con.execute(
                    f"SELECT DISTINCT CAST(time AS DATE) d FROM read_parquet([{files}])"
                ).fetchdf()
            finally:
                con.close()
            if df.empty:
                return []
            return sorted(df["d"].astype(str).tolist())
        except Exception as exc:  # noqa: BLE001
            logger.warning("从最近分区推导日历失败: %s", exc)
            return []

    def _dates_from_parquet(self) -> list[str]:
        """从 daily_forward 分区的 time 列推导全部交易日。"""
        fwd = self._hub.data_dir / "1_kline_data" / "daily_forward"
        if not fwd.is_dir():
            return []
        import duckdb

        try:
            con = duckdb.connect(config={"memory_limit": "4GB", "threads": "2"})
            try:
                df = con.execute(
                    f"SELECT DISTINCT CAST(time AS DATE) d FROM read_parquet('{fwd / 'dt=*' / 'data.parquet'}', hive_partitioning=1)"
                ).fetchdf()
            finally:
                con.close()
            if df.empty:
                return []
            return sorted(df["d"].astype(str).tolist())
        except Exception as exc:  # noqa: BLE001
            logger.warning("从 parquet 推导日历失败: %s", exc)
            return []

    def build_instruments(self) -> int:
        """从标的列表或 parquet symbol 列生成 instruments/all.txt。"""
        inst_dir = self._qlib_dir / "instruments"
        inst_dir.mkdir(parents=True, exist_ok=True)
        inst_file = inst_dir / "all.txt"

        raw_symbols = self._collect_raw_symbols()

        cal_dates = self._load_calendar()
        if not cal_dates:
            logger.warning("请先构建日历 (build_calendar)")
            return 0

        start_date = cal_dates[0]
        end_date = cal_dates[-1]

        qlib_symbols = sorted({s for s in raw_symbols if s})

        with open(inst_file, "w") as f:
            for sym in qlib_symbols:
                f.write(f"{sym}\t{start_date}\t{end_date}\n")

        logger.info("Qlib[%s] instruments: %d symbols -> %s", self._market, len(qlib_symbols), inst_file)

        # 各市场股票池成分文件（hub 定义 UNIVERSE_MAP 即生成；CN 行为不变，其他市场新增 map 即生效）
        universes = getattr(self._hub, "UNIVERSE_MAP", {}) or {}
        if universes:
            self._build_universe_instruments(inst_dir, start_date, end_date, set(qlib_symbols))
        return len(qlib_symbols)

    def _collect_raw_symbols(self) -> set[str]:
        """收集原生 symbol 列表：优先 hub.fetch_stock_list，其次从行情 parquet 全量推导。

        额外补充 A 股指数（CN_QLIB_INDEX_SYMBOLS），让指数进入 instruments 与 features。
        """
        symbols: set[str] = set()

        # 1) 优先基础股票列表（如 2_base_sector/instrument_detail），记录是否取到真正的股票
        stock_list_loaded = False
        try:
            df = self._hub.fetch_stock_list()
            if df is not None and not df.empty:
                symbol_col = None
                for col in ("Symbol", "symbol", "instrument"):
                    if col in df.columns:
                        symbol_col = col
                        break
                if symbol_col:
                    for sym in df[symbol_col].dropna().unique():
                        qs = self._to_qlib_symbol(str(sym))
                        if qs:
                            symbols.add(qs)
                    stock_list_loaded = len(symbols) > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s fetch_stock_list 失败，改从行情 parquet 推导: %s", self._market, exc)

        # 2) 基础列表缺失/为空时，从 daily_forward 行情分区全量推导（依赖 K 线，不受基础表缺失影响）
        if not stock_list_loaded:
            fwd = self._hub.data_dir / "1_kline_data" / "daily_forward"
            if fwd.is_dir():
                import duckdb

                try:
                    con = duckdb.connect(config={"memory_limit": "4GB", "threads": "2"})
                    try:
                        df = con.execute(
                            f"SELECT DISTINCT symbol FROM read_parquet('{fwd / 'dt=*' / 'data.parquet'}', hive_partitioning=1)"
                        ).fetchdf()
                    finally:
                        con.close()
                    for sym in df["symbol"].dropna().unique():
                        qs = self._to_qlib_symbol(str(sym))
                        if qs:
                            symbols.add(qs)
                    logger.info(
                        "%s 基础股票列表缺失，已从行情 parquet 推导 %d 个标的",
                        self._market, len(symbols),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("从 parquet 推导标的失败: %s", exc)

        # 3) A 股额外纳入指定指数（无论走哪条路径都补充）
        if self._market == "CN":
            for idx_code in CN_QLIB_INDEX_SYMBOLS:
                qs = self._to_qlib_symbol(idx_code)
                if qs:
                    symbols.add(qs)

        return symbols

    def _build_universe_instruments(
        self, inst_dir: Path, start_date: str, end_date: str, known: set[str]
    ) -> None:
        """为 csi300/csi500/... 生成 Qlib instruments 文件。"""
        universes = getattr(self._hub, "UNIVERSE_MAP", {}) or {}
        for universe in universes:
            try:
                df = self._hub.fetch_universe_stocks(universe)
                if df is None or df.empty or "symbol" not in df.columns:
                    logger.warning("股票池 %s 无成分数据，跳过", universe)
                    continue
                syms = set()
                for sym in df["symbol"].dropna().unique():
                    qs = self._to_qlib_symbol(str(sym))
                    # 只保留有行情数据的标的，否则 Qlib 读 features 时会报缺文件
                    if qs and qs in known:
                        syms.add(qs)
                if not syms:
                    logger.warning("股票池 %s 成分与行情数据无交集，跳过", universe)
                    continue
                out = inst_dir / f"{universe}.txt"
                with open(out, "w") as f:
                    for s in sorted(syms):
                        f.write(f"{s}\t{start_date}\t{end_date}\n")
                logger.info("Qlib universe %s: %d symbols -> %s", universe, len(syms), out)
            except Exception as e:
                logger.warning("生成股票池 %s 成分文件失败: %s", universe, e)

    # ------------------------------------------------------------------
    # 特征构建
    # ------------------------------------------------------------------
    def build_features(
        self,
        symbols: list[str] | None = None,
        *,
        incremental: bool = True,
        batch_size: int = 100,
        progress_cb=None,
    ) -> dict:
        """从 parquet 后复权 K 线生成 features/*.day.bin。

        CN 与非 CN 一律走批量构建（一次读入全市场再按标的分组写 bin），
        比逐标的串行快 1~2 个量级。`incremental` 参数保留以兼容调用方，
        批量构建是整库重写，天然与上游 FULL_REWRITE（分红复权）保持一致。
        """
        if symbols is None:
            symbols = self._get_all_symbols()

        if not symbols:
            return {"updated": 0, "skipped": 0}

        result = self.build_features_bulk(symbols=symbols, progress_cb=progress_cb)

        # A 股指数不在 daily_* 行情分区中，bulk 读不到，单独补建（仅 3 只，开销极小）
        if self._market == "CN":
            cal_dates = self._load_calendar()
            if cal_dates:
                cal_index = {d: i for i, d in enumerate(cal_dates)}
                for qlib_sym in (self._to_qlib_symbol(s) for s in CN_QLIB_INDEX_SYMBOLS):
                    feat_dir = self._qlib_dir / "features" / self._feat_dir_name(qlib_sym)
                    feat_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        if self._build_index_features(qlib_sym, self._to_qdb_symbol(qlib_sym), feat_dir, cal_dates, cal_index):
                            result["updated"] += 1
                        else:
                            result["skipped"] += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("构建指数 %s features 失败: %s", qlib_sym, exc)
                        result["skipped"] += 1

        return result

    def _factor_book(self) -> EventBook:
        if self._events is None:
            self._events = EventBook()
        return self._events

    def _multiplicative_factor(
        self, qlib_sym: str, dates, raw_close, hfq_close
    ) -> np.ndarray | None:
        """A 股的 ``$factor`` 必须是阶梯型乘法因子：qlib 拿它换算真实股数
        （``real_shares = adjusted_amount * factor``）。上游的
        ``hfq_close/unadjusted_close`` 是加法口径、逐日漂移，会让整仓卖出的
        股数偏离买入股数（实测买 3300 卖 3305）。

        返回 None 表示不采信乘法因子（事件表缺失 / 可用行太少 / 事件表漏行），
        调用方回退到上游比值：与离线重建工具对这些标的「拒写、保持原样」的
        口径一致，总比写进一个缺阶跃的因子、把复权价砸出一个假跳空要好。
        """
        events = self._factor_book().for_symbol(qlib_sym)
        if events is None or events.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(np.asarray(dates)))
        if bool(idx.has_duplicates):
            logger.warning("Qlib[%s] %s 日期重复，跳过乘法因子", self._market, qlib_sym)
            return None
        raw = pd.Series(np.asarray(raw_close, dtype=np.float64), index=idx)
        raw = raw.where(np.isfinite(raw) & (raw > 0)).ffill()
        if int(raw.notna().sum()) < 30:
            return None
        limit = board_limit(qlib_sym)
        f, _applied, problems, _notes, steps = build_multiplicative_factor(
            raw, events, limit
        )
        # 反向闸门：裸价跌穿涨停、后复权价却明显抗跌的日子，事件表里查无此日。
        # 说明除权日期错位或漏行，此时整标的不改写（与离线工具拒写一致）。
        hfq = pd.Series(np.asarray(hfq_close, dtype=np.float64), index=idx)
        hfq = hfq.where(np.isfinite(hfq) & (hfq > 0)).ffill()
        missing = unexplained_ex_dates(raw, hfq, covered_dates(events, steps), limit)
        if missing:
            logger.warning(
                "Qlib[%s] %s 事件表缺行(%d)，$factor 保持上游口径: %s",
                self._market,
                qlib_sym,
                len(missing),
                "; ".join(missing[:2]),
            )
            return None
        for msg in problems[:5]:
            logger.warning("Qlib[%s] %s 除权事件异常: %s", self._market, qlib_sym, msg)
        return f.to_numpy(dtype=np.float64)

    def build_features_bulk(
        self,
        symbols: list[str] | None = None,
        progress_cb=None,
    ) -> dict:
        """一次扫描全市场 parquet，按标的分组写 bin。

        - CN: ``close_bin = 未复权价 x 乘法因子``，因子由 dividend_factors 除权
          事件累乘 ``PROD(p0/ref)`` 得到（阶梯型、单调不降）。daily_backward 只
          提供 volume/amount 与事件表缺失时的回退因子。
        - 非 CN（US/HK/CRYPTO/FUTURES）: 只有 daily_forward，factor 恒为 1.0。

        整体读入再 groupby，避免逐标的全库扫描导致 OOM / 数小时耗时。

        symbols: 可选子集（qlib 格式）；None 则覆盖 parquet 中全部标的。
        """
        import duckdb

        cal_dates = self._load_calendar()
        if not cal_dates:
            logger.warning("请先构建日历 (build_calendar)")
            return {"updated": 0, "skipped": 0}
        cal_index = {d: i for i, d in enumerate(cal_dates)}

        is_cn = self._market == "CN"
        kline_sub = "daily_backward" if is_cn else "daily_forward"
        kline_glob = str(self._hub.data_dir / f"1_kline_data/{kline_sub}/dt=*/data.parquet")

        con = duckdb.connect(config={"memory_limit": "8GB", "threads": "4"})
        try:
            if is_cn:
                # 未复权价单独取回：close_bin 要写成 raw x 乘法因子，不能再拿 hfq 当价格
                unadj_glob = str(self._hub.data_dir / "1_kline_data/daily_unadjusted/dt=*/data.parquet")
                df = con.execute(
                    f"""
                    SELECT k.symbol,
                           CAST(k.time AS DATE) AS d,
                           k.open, k.high, k.low, k.close, k.volume, k.amount,
                           u.open AS raw_open, u.high AS raw_high, u.low AS raw_low,
                           u.close AS raw_close,
                           k.close / NULLIF(u.close, 0.0) AS factor
                    FROM read_parquet('{kline_glob}', hive_partitioning=1) k
                    LEFT JOIN read_parquet('{unadj_glob}', hive_partitioning=1) u
                      ON u.symbol = k.symbol AND CAST(u.time AS DATE) = CAST(k.time AS DATE)
                    WHERE k.close > 0 AND u.close > 0
                    ORDER BY k.symbol, d
                    """
                ).fetchdf()
            else:
                df = con.execute(
                    f"""
                    SELECT symbol, CAST(time AS DATE) d,
                           open, high, low, close, volume, amount
                    FROM read_parquet('{kline_glob}', hive_partitioning=1)
                    ORDER BY symbol, d
                    """
                ).fetchdf()
                if not df.empty:
                    df["factor"] = np.ones(len(df), dtype=np.float64)
        finally:
            con.close()

        if df.empty:
            return {"updated": 0, "skipped": 0}

        df["ci"] = df["d"].astype(str).map(cal_index)
        df = df[df["ci"].notna()]
        df["ci"] = df["ci"].astype(np.int64)

        if symbols is not None:
            # all.txt 中的 symbol 已小写（_feat_dir_name 规则），而 parquet 原生
            # symbol 保留大小写，非 CN 市场过滤需两侧都小写比较
            if not is_cn:
                qlib_wanted = {s.lower() for s in symbols}
                df = df[df["symbol"].map(self._to_qlib_symbol).str.lower().isin(qlib_wanted)]
            else:
                qlib_wanted = set(symbols)
                df = df[df["symbol"].map(self._to_qlib_symbol).isin(qlib_wanted)]

        updated = skipped = 0
        factor_fallback = 0
        total_syms = int(df["symbol"].nunique()) if not df.empty else 0
        processed = 0
        for qdb_sym, group in df.groupby("symbol", sort=False):
            processed += 1
            # 特征阶段占据总耗时主体（8~95%）：按批次上报进度，让上层看到"在动"并续心跳
            if callable(progress_cb) and total_syms and (processed % 20 == 0 or processed == total_syms):
                try:
                    progress_cb(int(8 + 87 * processed / total_syms))
                except Exception:  # noqa: BLE001 - 进度回调非关键路径
                    pass
            qlib_sym = self._to_qlib_symbol(qdb_sym)
            positions = group["ci"].values
            start_idx = int(positions.min())
            span = int(positions.max()) - start_idx + 1
            offsets = positions - start_idx

            # qlib 的 FileFeatureStorage 强制 instrument.lower() 拼路径，
            # 目录必须小写否则读取静默为空
            feat_dir = self._qlib_dir / "features" / self._feat_dir_name(qlib_sym)
            feat_dir.mkdir(parents=True, exist_ok=True)

            try:
                cols = {
                    field: group[field].values
                    for field in ("open", "high", "low", "close", "volume", "amount")
                }
                factor = group["factor"].values if is_cn else np.ones(len(group))
                if is_cn:
                    f_mult = self._multiplicative_factor(
                        qlib_sym, group["d"].values, group["raw_close"].values,
                        group["close"].values,
                    )
                    if f_mult is not None:
                        # 价格序列整体换成 未复权价 x 乘法因子，保证 close/factor == raw
                        for field in ("open", "high", "low", "close"):
                            raw = group[f"raw_{field}"].values.astype(np.float64)
                            cols[field] = np.where(
                                np.isfinite(raw) & (raw > 0), raw * f_mult, np.nan
                            )
                        factor = f_mult
                    else:
                        factor_fallback += 1
                aligned: dict[str, np.ndarray] = {}
                for field, values in cols.items():
                    arr = np.full(span, np.nan, dtype=np.float32)
                    arr[offsets] = np.asarray(values, dtype=np.float32)
                    aligned[field] = arr
                f_aligned = np.full(span, 1.0, dtype=np.float32)
                f_aligned[offsets] = np.asarray(factor, dtype=np.float32)
                aligned["factor"] = f_aligned
                for field, arr in aligned.items():
                    self._write_bin_file(feat_dir / f"{field}.day.bin", start_idx, arr)
                if is_cn:
                    # $change = pct_change(close)：除权日分母自然换成参考价，
                    # CnExchange.check_stock_limit 读的就是这一列
                    close_arr = aligned["close"]
                    chg = np.full(span, np.nan, dtype=np.float32)
                    with np.errstate(invalid="ignore", divide="ignore"):
                        chg[1:] = close_arr[1:] / close_arr[:-1] - 1.0
                    self._write_bin_file(feat_dir / "change.day.bin", start_idx, chg)
                updated += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("构建 %s features 失败: %s", qlib_sym, exc)
                skipped += 1

        if is_cn and factor_fallback:
            logger.warning(
                "Qlib[CN] %d 个标的查无除权事件表，$factor 回退为 hfq/raw 比值"
                "（该口径会逐日漂移）",
                factor_fallback,
            )
        logger.info("Qlib[%s] features (bulk): updated=%d, skipped=%d", self._market, updated, skipped)
        return {"updated": updated, "skipped": skipped}

    def _load_calendar(self) -> list[str]:
        """加载已构建的 Qlib 日历。"""
        cal_file = self._qlib_dir / "calendars" / "day.txt"
        if not cal_file.exists():
            return []
        with open(cal_file) as f:
            return [line.strip() for line in f if line.strip()]

    def _get_all_symbols(self) -> list[str]:
        """获取所有 Qlib 格式的 symbol。"""
        inst_file = self._qlib_dir / "instruments" / "all.txt"
        if not inst_file.exists():
            return []
        symbols = []
        with open(inst_file) as f:
            for line in f:
                parts = line.strip().split("\t")
                if parts:
                    symbols.append(parts[0])
        return symbols

    def _build_index_features(
        self,
        qlib_sym: str,
        qdb_sym: str,
        feat_dir: Path,
        cal_dates: list[str],
        cal_index: dict[str, int],
    ) -> bool:
        """从 index_daily 构建指数 features（仅 A 股有指数）。"""
        df = self._hub.fetch_index_kline(qdb_sym, date(2016, 1, 4), date(2026, 12, 31))
        if df.empty:
            return False

        first_date = str(df.iloc[0].get("trade_date", ""))[:10]
        start_idx = cal_index.get(first_date, 0)

        field_data = {
            "open": df["open"].values if "open" in df.columns else None,
            "high": df["high"].values if "high" in df.columns else None,
            "low": df["low"].values if "low" in df.columns else None,
            "close": df["close"].values if "close" in df.columns else None,
            "volume": df["volume"].values if "volume" in df.columns else None,
            "amount": df["amount"].values if "amount" in df.columns else None,
            "factor": np.ones(len(df)),
        }

        for field_name, values in field_data.items():
            if values is None:
                continue
            bin_path = feat_dir / f"{field_name}.day.bin"
            self._write_bin_file(bin_path, start_idx, values.astype(np.float32))

        return True

    # ------------------------------------------------------------------
    # Symbol 转换（多市场）
    # ------------------------------------------------------------------
    def _to_qlib_symbol(self, symbol: str) -> str:
        """原生 symbol -> Qlib 格式（保留原始大小写，保证无损往返）。

        - A 股: 600519.SH -> sh600519（原生 exchange 前缀）
        - 美股: AAPL -> us_AAPL
        - 港股: 00700.HK -> hk_00700.HK
        - 加密货币: BTCUSDT -> bc_BTCUSDT
        - 期货: CL.FUT -> fut_CL.FUT, Au99.99 -> fut_Au99.99
        """
        s = symbol.strip()
        if self._market == "CN":
            if "." in s:
                code, exchange = s.split(".", 1)
                if exchange.upper() in ("SH", "SZ", "BJ"):
                    return f"{exchange.lower()}{code}"
            return s.lower() if not s.lower().startswith(("sh", "sz", "bj")) else s.lower()
        # 非 A 股：统一前缀 + 原始 symbol（含点号，保留大小写）
        prefix = _MARKET_QLIB_PREFIX.get(self._market, "mkt_")
        # 幂等：已是该市场前缀则直接返回
        if s.startswith(prefix):
            return s
        return f"{prefix}{s}"

    def _feat_dir_name(self, qlib_symbol: str) -> str:
        """feature 目录名。qlib 的 FileFeatureStorage 强制 instrument.lower()
        拼路径，非 A 股市场 symbol 含大写（fut_CL.FUT / hk_00700.HK /
        us_AAPL），目录必须小写，否则 feature 读取静默返回空。"""
        return qlib_symbol if self._market == "CN" else qlib_symbol.lower()

    def _to_qdb_symbol(self, qlib_symbol: str) -> str:
        """Qlib 格式 -> 原生 symbol。必须与 _to_qlib_symbol 完全对称。"""
        s = qlib_symbol.strip()
        # A 股：exchange 前缀还原
        if self._market == "CN":
            if s.startswith("sh"):
                return f"{s[2:]}.SH"
            if s.startswith("sz"):
                return f"{s[2:]}.SZ"
            if s.startswith("bj"):
                return f"{s[2:]}.BJ"
            if "." in s:
                return s
            return s
        # 非 A 股：剥离市场前缀
        prefix = _MARKET_QLIB_PREFIX.get(self._market, "mkt_")
        if s.startswith(prefix):
            return s[len(prefix):]
        # 可能已经是原生格式
        return s

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------
    def is_built(self) -> bool:
        """检查 Qlib 缓存是否已构建。"""
        cal_file = self._qlib_dir / "calendars" / "day.txt"
        inst_file = self._qlib_dir / "instruments" / "all.txt"
        feat_dir = self._qlib_dir / "features"
        return (
            cal_file.exists()
            and inst_file.exists()
            and feat_dir.exists()
            and any(feat_dir.iterdir())
        )

    def get_status(self) -> dict:
        """返回 Qlib 缓存状态。"""
        cal_file = self._qlib_dir / "calendars" / "day.txt"
        inst_file = self._qlib_dir / "instruments" / "all.txt"
        feat_dir = self._qlib_dir / "features"

        status: dict = {
            "qlib_dir": str(self._qlib_dir),
            "market": self._market,
            "calendar_built": cal_file.exists(),
            "instruments_built": inst_file.exists(),
            "features_built": feat_dir.exists(),
        }

        if cal_file.exists():
            with open(cal_file) as f:
                dates = [l.strip() for l in f if l.strip()]
            status["calendar_count"] = len(dates)
            if dates:
                status["calendar_range"] = f"{dates[0]} ~ {dates[-1]}"

        if inst_file.exists():
            with open(inst_file) as f:
                status["instrument_count"] = sum(1 for _ in f)

        if feat_dir.exists():
            status["feature_symbol_count"] = sum(
                1 for d in feat_dir.iterdir() if d.is_dir()
            )

        return status

    @staticmethod
    def _write_bin_file(path: Path, start_idx: int, values: np.ndarray) -> None:
        """写入 Qlib binary 文件。

        格式: 4-byte float32 start_idx + N * 4-byte float32 values
        """
        with open(path, "wb") as f:
            f.write(struct.pack("f", float(start_idx)))
            f.write(values.astype(np.float32).tobytes())

    @staticmethod
    def _read_bin_file(path: Path) -> tuple[int, np.ndarray]:
        """读取 Qlib binary 文件。返回 (start_idx, values)。"""
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) < 4:
            return 0, np.array([], dtype=np.float32)
        start_idx = int(struct.unpack("f", raw[:4])[0])
        values = np.frombuffer(raw[4:], dtype=np.float32)
        return start_idx, values


def _resolve_cn_data_dir() -> Path:
    """解析 A 股数据目录（兼容容器/宿主机）。"""
    import os

    env_val = os.getenv("QM_QUANTDB_DATA_DIR", "").strip()
    if env_val:
        return Path(env_val)
    for candidate in (
        Path("/data/quantdb"),
        Path(__file__).resolve().parents[3] / "data" / "quantdb",
    ):
        if candidate.is_dir():
            return candidate
    return Path("/data/quantdb")


def ensure_qlib_cache(
    market: str = "CN",
    quantdb_dir: str | Path | None = None,
    qlib_dir: str | Path | None = None,
) -> str:
    """确保指定市场的 Qlib 缓存可用，返回 provider_uri。

    如果缓存不存在或过期，从该市场 parquet 构建。

    Args:
        market: 市场名 CN/US/HK/CRYPTO/FUTURES，或兼容旧调用的数据目录路径。
                当第一个位置参数是存在的目录时视为 quantdb_dir（A 股）。
        quantdb_dir: 数据目录（默认按市场解析）
        qlib_dir: Qlib 输出目录（默认按 resolve_qlib_provider_uri 解析）
    """
    # 向后兼容：旧调用 ensure_qlib_cache("/data/quantdb") 把路径当 market
    if isinstance(market, (str, Path)) and str(market).startswith(("/", "~", ".")):
        p = Path(str(market))
        if p.is_dir() or "/quantdb" in str(market):
            quantdb_dir = quantdb_dir or p
            market = "CN"

    if qlib_dir is None:
        # 写回系统实际读取的那份缓存：否则会在 for_market 默认目录再建一份
        # 影子缓存，而 resolve_qlib_provider_uri 优先返回它，静默遮蔽已有数据。
        from backend.shared.qlib_paths import resolve_qlib_provider_uri

        qlib_dir = resolve_qlib_provider_uri(market)

    builder = QlibDataBuilder.for_market(market, data_dir=quantdb_dir, qlib_dir=qlib_dir)

    if not builder.is_built():
        logger.info("Qlib[%s] 缓存不存在，开始构建...", market)
        builder.build_all(incremental=False)
    else:
        # 增量更新：先重建日历（数据可能已更新），再增量更新 instruments 和 features
        builder.build_calendar()
        builder.build_instruments()
        builder.build_features(incremental=True)

    return str(builder.qlib_dir)
