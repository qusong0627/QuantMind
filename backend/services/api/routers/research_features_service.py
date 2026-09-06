"""QuantDB parquet 投影特征服务。

为投研平台提供按需字段投影（一级：候选池列表选中日期后加载 50 维宽表
所需字段；二级：个股详情点击后动态加载）。全量分类特征路径已随
FactorPanel 下线移除。

所有读取都复用 QuantDBDataHub 单例的 DuckDB 视图（懒加载 + 线程本地连接），
不额外创建连接。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

# 按数据日投影的结果缓存（投研选中日期的 50 维宽表截面）：
# 宽表按日落盘、当天不变，TTL 内重复切日期/刷新页面直接命中
_PROJ_DAY_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_PROJ_DAY_CACHE_TTL = 600.0
_PROJ_DAY_CACHE_MAX_ENTRIES = 32
_PROJ_DAY_CACHE_LOCK = threading.Lock()

# 特征读取线程池：DuckDB 连接按线程持有（threading.local），并发过大会同时
# 打开多套多 GB parquet 视图扫描，故刻意限制为 2 个常驻线程。
_FEATURE_EXECUTOR: ThreadPoolExecutor | None = None
_FEATURE_EXECUTOR_LOCK = threading.Lock()


def _feature_executor() -> ThreadPoolExecutor:
    global _FEATURE_EXECUTOR
    if _FEATURE_EXECUTOR is None:
        with _FEATURE_EXECUTOR_LOCK:
            if _FEATURE_EXECUTOR is None:
                _FEATURE_EXECUTOR = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="qm-features"
                )
    return _FEATURE_EXECUTOR


async def _offload(coro_func, *args):
    """在受限线程池中执行同步特征读取，避免阻塞 API 事件循环。"""
    return await asyncio.get_running_loop().run_in_executor(
        _feature_executor(), coro_func, *args
    )


# 投影模式（只取表格/筛选所需字段）上限：响应体小得多，可覆盖整个候选池。
# 投研宇宙按数据日直读 pred.parquet 全市场截面（约 5400 只），上限需覆盖全市场
MAX_BATCH_SYMBOLS_PROJECTED = 6000

# 只接受规范 suffix 代码，杜绝 SQL 注入（hub.query 不支持参数绑定）
_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

# dt 为整数 YYYYMMDD；回看窗口用于裁剪分区扫描范围（约一个月）
_DT_LOOKBACK = 100

# 基础元数据列（不作为因子列参与任何处理）
_META_COLUMNS = frozenset(
    {
        "symbol",
        "wind_code",
        "time",
        "trade_date",
        "dt",
        "release_id",
        "published_at",
        "rn",
        "close",
    }
)


def _get_hub():
    """获取 QuantDBHub 单例（延迟导入，避免 API 服务启动强依赖 engine 模块）。"""
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    return QuantDBDataHub.get_instance()


def normalize_symbols(symbols: list[str]) -> list[str]:
    """将任意格式股票代码归一化为规范 suffix 格式并去重（保持入参顺序）。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in symbols:
        suffix = StockCodeUtil.to_suffix(str(raw or "").strip())
        if not _SYMBOL_RE.match(suffix) or suffix in seen:
            continue
        seen.add(suffix)
        result.append(suffix)
    return result


# ----------------------------------------------------------------------
# camelCase 投影
# ----------------------------------------------------------------------
# 与前端 featureMapper.ts 的 FIELD_ALIASES 保持一致：这些列所在的 QuantDB 前缀
# 与前端列分组不同，必须显式改名，否则投影会漏掉它们。
_CAMEL_ALIASES: dict[str, str] = {
    "fun_mv_rank": "styleMvRank",
    "fun_value_zscore": "styleValueZscore",
    "micro_liquidity_amihud_20": "liqAmihud20",
    # PG `stock_daily_latest` 自 2026-06-18 起不再回填 PE/ROE 等列（近期交易日 100% NULL，
    # 序列化后变成 0，前端显示 “PE 0.0 / ROE 0.0%”）。这些别名让 QuantDB 顶上同名 UI 字段。
    "pe_ttm": "pe",
    "fun_roe": "roe",
    "fun_np_growth": "profitGrowth",
    # features_daily 的原始列名与 UI 字段名不对齐：
    # 涨跌幅 = pct_change（当日涨跌），收盘价 = close。
    # 不加别名时 toCamel 产出 pctChange/close，前端列（latestChange/closePrice）取不到值。
    "pct_change": "latestChange",
    "close": "closePrice",
    # 量比：features_daily 列名是 vol_to_ma5/vol_to_ma20（toCamel → volToMa5），
    # 前端列（volRatio5/volRatio20）取不到值。
    "vol_to_ma5": "volRatio5",
    "vol_to_ma20": "volRatio20",
}

# market_sentiment 视图的列没有前缀，前端统一加 sentiment 前缀避免与基础字段冲突。
_SENTIMENT_CAMEL_ALIASES: dict[str, str] = {
    "liquidity_score": "sentimentLiquidityScore",
    "buy_pressure": "sentimentBuyPressure",
    "sell_pressure": "sentimentSellPressure",
    "body_ratio": "sentimentBodyRatio",
    "intraday_vol": "sentimentIntradayVol",
    "gap_up_down": "sentimentGapUpDown",
    "am_pm_trend": "sentimentAmPmTrend",
    "volume_concentration": "sentimentVolumeConcentration",
}


def _to_camel(column: str) -> str:
    """mom_ret_1d → momRet1d（与前端 toCamel 同规则）。"""
    parts = [p for p in column.split("_") if p]
    if not parts:
        return column
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _camel_name(column: str, view: str) -> str:
    if view == "qdb_market_sentiment" and column in _SENTIMENT_CAMEL_ALIASES:
        return _SENTIMENT_CAMEL_ALIASES[column]
    return _CAMEL_ALIASES.get(column) or _to_camel(column)


# 投影兜底：目标字段缺失时，用替代列换算填充。
# 注意：return1d/3d/5d 不再兜底到 l1_factors.mom_ret_*d（过去收益）——
# 投研平台的 return 系列是“推理日后 N 日真实收益”，语义上必须来自
# features_daily 按推理日读取的 return_* 标签；mom_ret_*d 是历史动量（过去收益），
# 用它会污染“未来收益”展示。features_daily 的 return_* 在推理日后未满 N 个交易日
# 时为 NaN，投影路径返回空由前端显示“-”，待未来行情生成后自然回填。
_DERIVED_FALLBACKS: dict[str, tuple[str, float]] = {
    # UI 的 rsi / atr 字段在 PG 里分别来自 rsi_6 与 vol_atr_14，
    # QuantDB 同名列是 rsi_6 / vol_atr_14，这里补上映射避免这两列取不到值。
    "rsi": ("rsi6", 1.0),
    "atr": ("volAtr14", 1.0),
}

# 单位对齐：QuantDB parquet 存原始单位（元），而 `/research/universe` 已把同名字段
# 换算过（市值 → 亿元，资金流 → 百万元）。前端在 universe 缺值或填 0 占位时才采用
# QuantDB 值，所以这里必须把会被采用的字段换算到同一量纲。
# 注意：qdb_valuation.total_mv / float_mv 是真正的元，可以线性换算；
# 但 l1_factors 的 fun_mv / liq_amount 是对数值，任何缩放都是错的——
# 它们保留原名（funMv / liqAmount），不参与 UI 的市值字段。
_UNIT_SCALES: dict[str, float] = {
    "totalMv": 1e-8,
    "floatMv": 1e-8,
    # 成交额：qdb_daily_unadjusted.amount 单位是「万元」（如 11754.99 万 ≈ 1.18 亿），
    # universe 已按 to_yi 口径给「亿元」，这里 ×1e-4 对齐（万元 → 亿元）。
    "amount": 1e-4,
    "mainFlow": 1e-6,
    "flowNetAmount": 1e-6,
    "flowBuyAmount": 1e-6,
    "flowSellAmount": 1e-6,
    "flowLargeNet": 1e-6,
    "flowMediumNet": 1e-6,
    "flowSmallNet": 1e-6,
    # l2_factors.flow_super_net 单位是元，与其他 flow* 一致（同类别统一 → 百万元）
    "flowSuperNet": 1e-6,
}


def _to_jsonable(value: Any) -> Any:
    """转换为 JSON 安全值：NaN/Inf/NaT → None，numpy 标量 → python 原生类型。"""
    if value is None:
        return None
    # numpy / pandas 标量统一走 .item()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except (ValueError, TypeError):
            return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    # date / datetime / Timestamp（pd.NaT.isoformat() 返回字符串 "NaT"，需排除）
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            text = isoformat()
        except (ValueError, TypeError):
            return None
        return None if text == "NaT" else text
    return None


def _latest_rows(
    view: str, symbols: list[str], dt: int | None = None, columns: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """读取指定视图中每个 symbol 的最新一行。

    dt（YYYYMMDD 整数）传入时读取「不晚于该日的最新一行」——投研平台按选中
    数据日 T 查看历史截面：return_*（T 后 N 日真实收益）只在 T 所在行有值，
    读最新行会永远取到 NaN。
    columns 传入时做列裁剪（只 SELECT symbol + 指定列），用于 l2/l1 等宽表
    （100+ 列）按需取 2~3 列，避免整表扫描拖慢投研页面。
    返回 {symbol: row_dict}；视图不存在或无数据时返回空字典（优雅降级）。
    """
    df = _get_hub().fetch_latest_rows(
        view, symbols, dt=dt, lookback=_DT_LOOKBACK, columns=columns
    )
    if df.empty:
        return {}
    return {str(row["symbol"]): dict(row) for _, row in df.iterrows()}


def _latest_l1_from_files(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """L1 因子平铺格式兜底：读取最新一个 l1_factors_YYYYMMDD.parquet。

    l1_factors 目录混合了分区目录与平铺文件，hub 的 DuckDB 视图只挂载分区目录，
    分区目录缺失时用此路径读取最新平铺文件（单文件，成本可控）。
    """
    import pandas as pd

    try:
        l1_dir = _get_hub().data_dir / "6_ml_datasets" / "l1_factors"
        files = sorted(l1_dir.glob("l1_factors_*.parquet"))
        if not files:
            return {}
        df = pd.read_parquet(files[-1])
    except Exception as exc:
        logger.warning("读取 L1 平铺文件失败（跳过）: %s", exc)
        return {}

    symbol_col = "symbol" if "symbol" in df.columns else "wind_code"
    if symbol_col not in df.columns:
        return {}

    df = df[df[symbol_col].isin(symbols)]
    return {str(row[symbol_col]): dict(row) for _, row in df.iterrows()}


def _fetch_l1(
    symbols: list[str], dt: int | None = None, columns: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """L1 因子：优先分区视图，缺失时回落到平铺文件。"""
    rows = _latest_rows("qdb_l1_factors", symbols, dt, columns=columns)
    if rows:
        return rows
    return _latest_l1_from_files(symbols)


def _resolve_trade_date(rows: dict[str, dict[str, Any]]) -> str | None:
    """从任一数据源行中提取交易日期。"""
    for row in rows.values():
        for key in ("time", "trade_date"):
            value = _to_jsonable(row.get(key))
            if isinstance(value, str) and value:
                return value[:10]
    return None


def _apply_unit_scales(values: dict[str, Any]) -> None:
    """对需要单位换算的字段就地缩放（元→亿元 / 元→百万元）。

    与 `/research/universe` 已换算后的值对齐（universe 缺值或 0 占位时前端
    才采用 QuantDB 投影值，量纲必须一致）。
    """
    for camel_name, scale in _UNIT_SCALES.items():
        if camel_name in values:
            values[camel_name] = values[camel_name] * scale


def _build_projected_payload(
    symbol: str, sources: dict[str, dict[str, Any]], wanted: frozenset[str]
) -> dict[str, Any]:
    """只输出 wanted 中的 camelCase 字段，平铺在 values 下（不分类）。

    表格与筛选只消费数值，因此这里丢弃非数值字段，响应体比全量小两个数量级。
    """
    # 兜底源字段即使未被请求也要收集，用于补齐缺失的目标字段
    fallback_sources = {
        src: (target, scale)
        for target, (src, scale) in _DERIVED_FALLBACKS.items()
        if target in wanted
    }
    # 换手率需要现算，额外收集两个原料列
    extra_spares: set[str] = set()
    if "turnoverRate" in wanted:
        extra_spares |= {"volume", "circulatingCapital"}

    values: dict[str, Any] = {}
    spare: dict[str, float] = {}
    available: list[str] = []

    for view, rows_by_symbol in sources.items():
        row = rows_by_symbol.get(symbol)
        if not row:
            continue
        available.append(view.removeprefix("qdb_"))
        for column, raw in row.items():
            # close 列在 _META_COLUMNS（基础价格列），但投影路径经别名映射为
            # closePrice（UI 收盘价列）——有别名映射的列不能当元数据跳过
            if column in _META_COLUMNS and column not in _CAMEL_ALIASES:
                continue
            name = _camel_name(column, view)
            is_wanted = name in wanted and name not in values
            is_spare = (
                name in fallback_sources or name in extra_spares
            ) and name not in spare
            if not is_wanted and not is_spare:
                continue
            value = _to_jsonable(raw)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if is_wanted:
                values[name] = value
            if is_spare:
                spare[name] = float(value)

    # 目标字段缺失时用兜底源换算填充
    for src, (target, scale) in fallback_sources.items():
        if target not in values and src in spare:
            values[target] = spare[src] * scale

    # 换手率：PG 从 2026-06-26 起完全没有该列，现算兜底。
    # qdb_daily_unadjusted.volume 与 features_daily.circulating_capital 单位均为「股」，
    # 相除 ×100 即得百分比（无需再 ×100 手→股，否则会放大 100 倍）。
    if "turnoverRate" in wanted and "turnoverRate" not in values:
        volume = spare.get("volume")
        circulating = spare.get("circulatingCapital")
        if volume and circulating and circulating > 0:
            values["turnoverRate"] = volume * 100.0 / circulating

    # 与 universe 行的单位对齐（亿元 / 百万元）
    _apply_unit_scales(values)

    return {
        "symbol": symbol,
        "tradeDate": _resolve_trade_date(
            {v: r[symbol] for v, r in sources.items() if symbol in r}
        ),
        "sources": sorted(available),
        "values": values,
    }


def _query_sources(
    symbols: list[str], *, wanted: frozenset[str] = frozenset(), dt: int | None = None
) -> dict[str, dict[str, Any]] | None:
    """查询 QuantDB 视图（按 wanted 字段集做分段式加载）。数据目录不可用时返回 None。

    优先使用 52 维核心宽表 qdb_features_daily 替代旧的 qdb_valuation 与 qdb_technical_indicators，
    未落盘 features_daily 时自动回退至旧分散视图。
    dt（YYYYMMDD 整数）传入时各视图读「不晚于该日的最新一行」（投研历史截面）。

    页面减负 + IO 分段：
    - dt 模式（投研选中日期）以 features_daily 单视图为主源；
    - 仅在 wanted 需要宽表缺失字段时，才额外按需只读所需列：
        roe/profitGrowth → qdb_l1_factors（2 列）；amount/turnoverRate → qdb_daily_unadjusted（2 列）。
      这样既补齐空值字段，又避免恢复「5 视图全扫」的旧成本。
    - 宽表未覆盖的日期（历史早于宽表起始日）回退多视图。
    """
    hub = _get_hub()
    if not hub.available:
        logger.warning("QuantDB 数据目录不可用: %s", hub.data_dir)
        return None

    sources: dict[str, dict[str, Any]] = {}

    # 1. 50+ 维日频特征宽表（优先主源）
    features_daily_rows = _latest_rows("qdb_features_daily", symbols, dt)
    if features_daily_rows:
        sources["qdb_features_daily"] = features_daily_rows

        if dt is not None:
            # 减负模式：主源之后按需分段补 L1 与日线（列裁剪，只取必要列）
            l1_cols: list[str] = []
            if "roe" in wanted:
                l1_cols.append("fun_roe")
            if "profitGrowth" in wanted:
                l1_cols.append("fun_np_growth")
            if l1_cols:
                sources["qdb_l1_factors"] = _fetch_l1(symbols, dt, columns=l1_cols)

            kline_cols: list[str] = []
            if "amount" in wanted:
                kline_cols.append("amount")
            if "turnoverRate" in wanted:
                kline_cols.append("volume")
            if kline_cols:
                daily = _latest_rows("qdb_daily_unadjusted", symbols, dt, columns=kline_cols)
                if daily:
                    sources["qdb_daily_unadjusted"] = daily
            return sources
    else:
        # 回退至旧分散视图
        sources["qdb_valuation"] = _latest_rows("qdb_valuation", symbols, dt)
        sources["qdb_technical_indicators"] = _latest_rows(
            "qdb_technical_indicators", symbols, dt
        )

    # 2. 情绪面、L1 因子、L2 因子（非 dt 模式沿用全量）
    sources["qdb_market_sentiment"] = _latest_rows("qdb_market_sentiment", symbols, dt)
    sources["qdb_l1_factors"] = _fetch_l1(symbols, dt)
    sources["qdb_l2_factors"] = _latest_rows("qdb_l2_factors", symbols, dt)

    if "turnoverRate" in wanted:
        # 日线视图只用于取 volume（现算换手率的原料）。其余列必须丢弃：
        # amount/open/high/low 与 UI 字段同名但量纲不同（amount 是元，UI 期望亿元），
        # 一旦混入就会污染成交额筛选。
        daily = _latest_rows("qdb_daily_unadjusted", symbols, dt)
        sources["qdb_daily_unadjusted"] = {
            sym: {"volume": row.get("volume")} for sym, row in daily.items()
        }
    return sources


# instrument_list 上市日期缓存：静态数据，进程内读一次即可复用（跨请求、跨日期）。
_LISTING_DATE_CACHE: dict[str, str] | None = None
_LISTING_DATE_CACHE_LOCK = threading.Lock()


def _load_listing_dates() -> dict[str, str]:
    """读取 QuantDB instrument_list 的上市日期，返回 {suffix_symbol: "YYYYMMDD"}。"""
    global _LISTING_DATE_CACHE
    with _LISTING_DATE_CACHE_LOCK:
        if _LISTING_DATE_CACHE is not None:
            return _LISTING_DATE_CACHE
        result: dict[str, str] = {}
        try:
            df = _get_hub().fetch_stock_list()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 instrument_list 失败（listedDays 缺失）: %s", exc)
            _LISTING_DATE_CACHE = result
            return result
        if df is None or df.empty or "J_start" not in df.columns:
            _LISTING_DATE_CACHE = result
            return result
        symbol_col = "symbol" if "symbol" in df.columns else "Symbol"
        for _, row in df[[symbol_col, "J_start"]].iterrows():
            try:
                sym = StockCodeUtil.to_suffix(str(row[symbol_col]).strip())
                start = str(int(row["J_start"]))
            except (ValueError, TypeError):
                continue
            if sym and len(start) >= 8:
                result[sym] = start[:8]
        _LISTING_DATE_CACHE = result
        return result


def _apply_listed_days(
    result: dict[str, dict[str, Any]],
    symbols: list[str],
    wanted: frozenset[str],
    dt: int,
) -> None:
    """补上市天数：listedDays = 选中交易日 - 上市日期（静态，无需扫描行情）。"""
    if "listedDays" not in wanted:
        return
    listing = _load_listing_dates()
    if not listing:
        return
    from datetime import datetime

    try:
        base = datetime.strptime(str(dt), "%Y%m%d").date()
    except ValueError:
        return
    for s in symbols:
        start = listing.get(s)
        if not start:
            continue
        try:
            start_date = datetime.strptime(start, "%Y%m%d").date()
        except ValueError:
            continue
        item = result.get(s)
        if item is None:
            continue
        item.setdefault("values", {})["listedDays"] = (base - start_date).days


def _load_projected_features(
    symbols: list[str], wanted: frozenset[str], dt: int | None = None
) -> dict[str, dict[str, Any]]:
    """投影模式：不走全量缓存（键随字段集变化），直接查询后按需裁剪。

    dt（YYYYMMDD 整数）传入时读「不晚于该日的最新截面」——投研平台按选中
    数据日查看历史状态，return_*（未来 N 日真实收益）也只有按日读取才有值。
    dt 模式只查 50 维宽表单视图，且结果按 (日期, 字段集) 缓存（宽表按日落盘、
    当天不变），切回已看过的日期或刷新页面直接命中，不重复扫描。
    """
    if dt is not None:
        cache_key = (
            f"{dt}|{len(symbols)}|{hash(tuple(symbols))}|{','.join(sorted(wanted))}"
        )
        with _PROJ_DAY_CACHE_LOCK:
            hit = _PROJ_DAY_CACHE.get(cache_key)
            if hit and time.monotonic() - hit[0] < _PROJ_DAY_CACHE_TTL:
                return hit[1]

    sources = _query_sources(symbols, wanted=wanted, dt=dt)
    if sources is None:
        return {}
    result = {s: _build_projected_payload(s, sources, wanted) for s in symbols}
    if dt is not None:
        _apply_listed_days(result, symbols, wanted, dt)

    if dt is not None and len(symbols) > 1000:
        # 只缓存全池量级请求（投研宇宙）；小批量（详情面板等）不值得占缓存槽
        with _PROJ_DAY_CACHE_LOCK:
            if len(_PROJ_DAY_CACHE) > _PROJ_DAY_CACHE_MAX_ENTRIES:
                _PROJ_DAY_CACHE.clear()
            _PROJ_DAY_CACHE[cache_key] = (time.monotonic(), result)
    return result


def _normalize_dt(trade_date: str | None) -> int | None:
    """'2026-08-28' / '20260828' → 20260828；无效输入返回 None（读最新行）。"""
    if not trade_date:
        return None
    digits = str(trade_date).replace("-", "").replace("/", "")[:8]
    return int(digits) if digits.isdigit() and len(digits) == 8 else None


def get_batch_full_features_sync(
    symbols: list[str], fields: list[str] | None = None, trade_date: str | None = None
) -> dict[str, Any]:
    """批量股票投影特征的同步实现（唯一实现）。

    fields（camelCase）为投影字段集：响应只含这些字段且平铺在 values 下，
    上限 MAX_BATCH_SYMBOLS_PROJECTED 覆盖整个候选池以支持全池筛选。
    trade_date 传入时投影按「不晚于该日的最新截面」读取（投研历史日期回看）。
    """
    normalized = normalize_symbols(symbols or [])
    if not normalized:
        return {"code": 200, "data": {"items": [], "total": 0, "missing": []}}

    wanted = frozenset(f for f in (fields or []) if f)
    if not wanted:
        return {
            "code": 400,
            "message": "fields 不能为空：投影接口必须指定字段集",
            "data": None,
        }

    cap = MAX_BATCH_SYMBOLS_PROJECTED
    truncated = normalized[:cap]
    features = _load_projected_features(truncated, wanted, _normalize_dt(trade_date))

    items = [features[s] for s in truncated if features.get(s, {}).get("sources")]
    missing = [s for s in truncated if not features.get(s, {}).get("sources")]
    return {
        "code": 200,
        "data": {
            "items": items,
            "total": len(items),
            "missing": missing,
            "truncated": len(normalized) > cap,
            "projected": True,
        },
    }


async def get_batch_full_features(
    symbols: list[str], fields: list[str] | None = None, trade_date: str | None = None
) -> dict[str, Any]:
    """批量股票的 QuantDB 特征投影（线程池卸载版，用于表格增强）。"""
    return await _offload(get_batch_full_features_sync, symbols, fields, trade_date)
