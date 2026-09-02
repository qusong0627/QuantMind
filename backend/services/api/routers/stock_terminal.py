"""个股终端（Stock Terminal）后端接口

P1 范围：
1. GET /list      股票列表（SH/SZ/BJ 分类 + 行业过滤 + 检索 + 分页）
2. GET /industries 行业列表（过滤下拉用）
3. GET /profile   个股概况聚合（详情 + 估值 + 宽基归属 + 概念板块）

数据全部来自本地 QuantDB parquet（instrument_detail / technical_indicators /
valuation / index_weights / sector_concept），无外部依赖。

K 线数据复用既有 /api/v1/market/kline 与 /api/v1/market/index-kline，
本模块不重复实现。
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/stock-terminal", tags=["StockTerminal"])

# 数据目录（与 quantdb_hub 同源，直接复用其解析逻辑避免双配置）
_DATA_DIR: Path | None = None


def _quantdb_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        _DATA_DIR = Path(QuantDBDataHub.get_instance().data_dir)
    return _DATA_DIR


# ---------------------------------------------------------------------------
# 内部数据层（进程内缓存，TTL 5 分钟；数据日频更新，缓存按交易日粒度足够）
# ---------------------------------------------------------------------------

_UNIVERSE_TTL = 300.0
_universe_cache: dict[str, Any] = {"df": None, "ts": 0.0, "trade_date": ""}
# universe 重建锁：to_thread 并发请求同时过期时只重建一次（读 2 parquet + merge）
_universe_lock = threading.Lock()
# 推理模型下拉选项缓存：model JOIN+GROUP BY 2.3s/次，按日频更新，TTL 与 universe 一致
_model_options_cache: dict[str, Any] = {"v": None, "ts": 0.0}
_concept_cache: dict[str, Any] = {"ts": 0.0, "symbol_map": {}}
# 宽基指数归属缓存：全量 7 个权重文件构建 symbol→归属映射，按 TTL 刷新
_index_membership_cache: dict[str, Any] = {"ts": 0.0, "map": {}}

# 概念板块展示上限：单只股票概念过多时截断（板块成员表全市场概念归属）
_MAX_CONCEPTS = 24

# 默认信号日覆盖阈值（distinct symbol 数）：低于此值视为「部分推理」，
# 默认列表回退到最近一个覆盖充分的推理日，避免最新日只推理了几十只
# 导致列表第 1 页之后得分/仓位/信号全部为空
_MIN_SIGNAL_COVERAGE = 1000


def _classify_board(symbol: str) -> str:
    """按代码归类市场板块：SH 主板/科创板、SZ 主板/创业板、BJ 北交所。"""
    code = symbol.split(".")[0]
    if symbol.endswith(".SH"):
        if code.startswith("68"):
            return "科创板"
        return "沪市主板"
    if symbol.endswith(".SZ"):
        if code.startswith("30"):
            return "创业板"
        return "深市主板"
    if symbol.endswith(".BJ"):
        return "北交所"
    return "其他"


def _exchange_of(symbol: str) -> str:
    if symbol.endswith(".SH"):
        return "SH"
    if symbol.endswith(".SZ"):
        return "SZ"
    if symbol.endswith(".BJ"):
        return "BJ"
    return ""


def _latest_partition(base: Path) -> Path | None:
    """取 Hive 分区数据集的最新 dt 分区文件。"""
    if not base.exists():
        return None
    parts = sorted(p for p in base.glob("dt=*") if (p / "data.parquet").exists())
    if not parts:
        return None
    return parts[-1] / "data.parquet"


def _partition_on(base: Path, asof: str) -> Path | None:
    """取 asof 当日或之前最近的分区文件（历史日快照，dt=YYYYMMDD）。

    asof 为 YYYY-MM-DD 字符串；超出范围时回退最新分区。
    """
    if not base.exists():
        return None
    day = asof.replace("-", "")
    parts = sorted(p for p in base.glob("dt=*") if (p / "data.parquet").exists())
    if not parts:
        return None
    for p in reversed(parts):
        if p.name.split("=")[1] <= day:
            return p / "data.parquet"
    return parts[0] / "data.parquet"


def _safe_f(v: Any) -> float | None:
    """NaN/inf -> None，保证 JSON 可序列化。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _load_universe(asof: str | None = None) -> tuple[pd.DataFrame, str]:
    """全市场快照：instrument_detail + technical_indicators(close/pct_change)。

    asof=YYYY-MM-DD 时读该日或之前最近的分区（历史日联动，日历点选日）；
    缺省读最新分区。static 列（名称/行业/市值等）取自 instrument_detail 最新快照。
    """
    now = time.time()
    cached = _universe_cache["df"]
    if cached is not None and now - _universe_cache["ts"] < _UNIVERSE_TTL and not asof:
        return cached, _universe_cache["trade_date"]

    with _universe_lock:
        # 双重检查：并发请求同时过期时只重建一次
        now = time.time()
        cached = _universe_cache["df"]
        if cached is not None and now - _universe_cache["ts"] < _UNIVERSE_TTL and not asof:
            return cached, _universe_cache["trade_date"]
        df, trade_date = _rebuild_universe(asof)
        if not asof:
            _universe_cache.update({"df": df, "ts": now, "trade_date": trade_date})
        return df, trade_date


_UNIVERSE_HK_CACHE: dict[str, Any] = {"df": None, "ts": 0.0, "trade_date": ""}
_UNIVERSE_HK_TTL = 300.0


def _load_universe_hk(asof: str | None = None) -> tuple[pd.DataFrame, str]:
    """港股全市场快照：quanthk = security_master(名称) + sdl_hk(OHLCV/涨跌) +
    akshare_profile(行业) + l1_factors(总市值)。列与 CN universe 对齐子集。"""
    now = time.time()
    cached = _UNIVERSE_HK_CACHE["df"]
    if cached is not None and now - _UNIVERSE_HK_CACHE["ts"] < _UNIVERSE_HK_TTL and not asof:
        return cached, _UNIVERSE_HK_CACHE["trade_date"]
    from backend.services.engine.data_platform.quanthk_hub import _resolve_quanthk_data_dir

    qdir = _resolve_quanthk_data_dir()
    import duckdb

    con = duckdb.connect()
    try:
        sdl = con.execute(
            "SELECT symbol, trade_date, close, pct_change FROM read_parquet("
            f"'{qdir}/2_base_sector/../2_base_sector/daily_placeholder', hive_partitioning=1)"
        ) if False else None
        # sdl_hk 是 PG 表；这里直接读 quanthk 日线/因子拼装（与 CN parquet 同构思路）
        parts = sorted(p.name for p in (qdir / "1_kline_data" / "daily_forward").glob("dt=*"))
        latest = parts[-1]
        df_day = latest[3:]
        k = con.execute(
            "SELECT symbol, close FROM read_parquet("
            f"'{qdir}/1_kline_data/daily_forward/dt={df_day}/data.parquet')"
        ).fetchdf()
        # 涨跌幅用前一交易日（无官方 pct 表时 close 环比，与行情口径一致）
        prev = parts[-2][3:]
        kp = con.execute(
            "SELECT symbol, close AS prev_close FROM read_parquet("
            f"'{qdir}/1_kline_data/daily_forward/dt={prev}/data.parquet')"
        ).fetchdf()
        k = k.merge(kp, on="symbol", how="left")
        k["pct_change"] = (k["close"] / k["prev_close"] - 1) * 100
        names = con.execute(
            "SELECT symbol, cn_name FROM read_parquet("
            f"'{qdir}/2_base_sector/security_master/data.parquet')"
        ).fetchdf()
        prof = con.execute(
            "SELECT symbol, 所属行业 FROM read_parquet("
            f"'{qdir}/2_base_sector/akshare_profile/*.parquet', union_by_name=true)"
        ).fetchdf().rename(columns={"所属行业": "rs_hyname"})
        mv = con.execute(
            "SELECT symbol, total_mv FROM read_parquet("
            f"'{qdir}/6_ml_datasets/l1_factors/dt={df_day}/data.parquet')"
        ).fetchdf()
        df = k.merge(names, on="symbol", how="left")
        df = df.merge(prof, on="symbol", how="left")
        df = df.merge(mv, on="symbol", how="left")
        df = df.rename(columns={
            "cn_name": "Name", "total_mv": "Zsz",
        })
        df["Symbol"] = df["symbol"]
        df["exchange"] = "HK"
        df["trade_date"] = str(df_day)
        df = df[df["close"].fillna(0) > 0].copy()
        # 与 CN universe 对齐的兼容列（港股无板块/静态估值，空值兜底）
        df["board"] = ""
        for _c in ("Ltsz", "DynaPE", "PB_MRQ"):
            df[_c] = None
        cols = ["Symbol", "Name", "close", "pct_change", "Zsz", "rs_hyname",
                "exchange", "trade_date", "board", "Ltsz", "DynaPE", "PB_MRQ"]
        df = df[[c for c in cols if c in df.columns]]
    finally:
        con.close()
    _UNIVERSE_HK_CACHE.update({"df": df, "ts": time.time(), "trade_date": str(df_day)})
    return df, str(df_day)


def _rebuild_universe(asof: str | None) -> tuple[pd.DataFrame, str]:
    """universe 重建（读 instrument_detail + technical_indicators 两 parquet + merge）。"""
    d = _quantdb_dir()
    # QuantDB SDK 新版落盘为 instrument_list.parquet，旧版为 instrument_detail.parquet
    detail_dir = d / "2_base_sector" / "instrument_detail"
    detail_file = detail_dir / "instrument_list.parquet"
    if not detail_file.exists():
        detail_file = detail_dir / "instrument_detail.parquet"
    if not detail_file.exists():
        raise HTTPException(status_code=503, detail="本地 instrument_detail 数据缺失")

    detail_cols = [
        "Symbol", "Name", "rs_hyname", "Zsz", "Ltsz", "DynaPE", "PB_MRQ",
        "StaffNum", "MainBusiness", "IPO_Price", "ZTPrice", "DTPrice",
        "RZRQ", "HSGT", "STGP", "IsSTGP", "IsHKGP", "J_zgb", "FreeLtgb", "BetaValue",
        "BelongHS300",
    ]
    raw = pd.read_parquet(detail_file)
    keep = [c for c in detail_cols if c in raw.columns]
    df = raw[keep].copy()

    # 收盘/涨跌幅（daily_unadjusted 不复权价，与 TDX 现价口径一致）：
    # technical_indicators 的 close 实为后复权价，直接用作“市价”会与前端默认
    # 前复权 K 线及实时行情相差数倍，故改用不复权日线。pct_change 由当日 vs
    # 前一个交易日的复权前 close 计算（两分区各读一次）。
    def _partition_files(base: Path, asof: str | None):
        if asof:
            cur = _partition_on(base, asof)
        else:
            cur = _latest_partition(base)
        if cur is None:
            return None, None
        cur_dir = cur.parent
        prev = None
        parts = sorted(p for p in base.glob("dt=*") if (p / "data.parquet").exists())
        idx = parts.index(cur_dir) if cur_dir in parts else -1
        if idx > 0:
            prev = parts[idx - 1] / "data.parquet"
        return cur, prev

    trade_date = ""
    for sub in ("daily_unadjusted",):
        k_cur, k_prev = _partition_files(
            d / "1_kline_data" / sub, asof
        )
        if k_cur is not None:
            trade_date = k_cur.parent.name.replace("dt=", "")
            close_df = pd.read_parquet(k_cur, columns=["symbol", "close"])
            df = df.merge(
                close_df, left_on="Symbol", right_on="symbol", how="left"
            ).drop(columns=["symbol"], errors="ignore")
            # 前复权/不复权口径下 pct_change = 当日/前日复权前 close 涨跌幅
            if k_prev is not None:
                prev_df = pd.read_parquet(k_prev, columns=["symbol", "close"])
                prev_df = prev_df.rename(columns={"close": "prev_close"})
                df = df.merge(
                    prev_df, left_on="Symbol", right_on="symbol", how="left"
                ).drop(columns=["symbol"], errors="ignore")
                df["pct_change"] = (df["close"] / df["prev_close"] - 1.0) * 100.0
            else:
                df["pct_change"] = float("nan")
            break
    for col in ("close", "pct_change"):
        if col not in df.columns:
            df[col] = float("nan")

    df["board"] = df["Symbol"].map(_classify_board)
    df["exchange"] = df["Symbol"].map(_exchange_of)

    return df, trade_date


_INDEX_NAMES = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000016.SH": "上证50",
    "000688.SH": "科创50",
    "399006.SZ": "创业板指",
    "000906.SH": "中证800",
}


def _index_membership(symbol: str) -> list[dict[str, Any]]:
    """查询个股归属的宽基指数（7 个指数权重文件逐一匹配）。

    权重文件为全市场快照，读取成本固定（与 symbol 无关），按 symbol 缓存结果；
    切换股票时命中缓存避免重复扫描 7 个 parquet。
    """
    now = time.time()
    if now - _index_membership_cache["ts"] < _SERIES_TTL and _index_membership_cache["map"]:
        return _index_membership_cache["map"].get(symbol, [])
    out: dict[str, list[dict[str, Any]]] = {}
    d = _quantdb_dir()
    weights_dir = d / "2_base_sector" / "index_weights"
    if weights_dir.exists():
        for file in sorted(weights_dir.glob("*.parquet")):
            code = file.stem
            if code == "index_weights" or code not in _INDEX_NAMES:
                continue
            try:
                w = pd.read_parquet(file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("read index weights %s failed: %s", file.name, exc)
                continue
            sym_col = "Symbol" if "Symbol" in w.columns else "symbol"
            for sym in w[sym_col].astype(str).dropna().unique():
                weight = _safe_f(w.loc[w[sym_col].astype(str) == sym, "Weight"].iloc[0]) if "Weight" in w.columns else None
                out.setdefault(sym, []).append({
                    "index_code": code,
                    "index_name": _INDEX_NAMES[code],
                    "weight": weight,
                })
    _index_membership_cache.update({"ts": now, "map": out})
    return out.get(symbol, [])


def _concepts_of(symbol: str) -> list[str]:
    """个股概念板块列表（sector_members 按 Symbol 反查，缓存 symbol->concepts 全表）。"""
    now = time.time()
    if now - _concept_cache["ts"] < _UNIVERSE_TTL and _concept_cache["symbol_map"]:
        return _concept_cache["symbol_map"].get(symbol, [])[:_MAX_CONCEPTS]

    d = _quantdb_dir()
    f = d / "2_base_sector" / "sector_concept" / "sector_members.parquet"
    if not f.exists():
        return []
    try:
        sm = pd.read_parquet(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("read sector_members failed: %s", exc)
        return []
    sym_col = "Symbol" if "Symbol" in sm.columns else "symbol"
    name_col = "SectorName" if "SectorName" in sm.columns else "sector_name"
    sm = sm[[sym_col, name_col]].dropna()
    symbol_map: dict[str, list[str]] = {}
    for sym, name in zip(sm[sym_col], sm[name_col]):
        symbol_map.setdefault(sym, []).append(str(name))
    _concept_cache.update({"ts": now, "symbol_map": symbol_map})
    return symbol_map.get(symbol, [])[:_MAX_CONCEPTS]


def _norm_dividend(v: Any) -> float | None:
    """valuation.dividend_rate 口径归一为百分数（<=1 视为小数）。"""
    f = _safe_f(v)
    if f is None:
        return None
    return round(f * 100, 2) if 0 < f <= 1 else round(f, 2)


def _flag(v: Any) -> bool:
    """标量 '1'/'0'/1/0/None -> bool（profile 中 r.get() 返回标量，不能用 Series.fillna）。"""
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def _st_mask(df: pd.DataFrame) -> pd.Series:
    """ST 布尔掩码：优先 IsSTGP（新列），兼容旧 STGP；列缺失时全 False。"""
    col = "IsSTGP" if "IsSTGP" in df.columns else ("STGP" if "STGP" in df.columns else None)
    if col is None:
        return pd.Series(False, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0) > 0


# ── L2 微观结构因子（14 个推荐，来自 l2_recommended_factors.csv 去冗余）──
# ICIR 越大 alpha 越强；全部为正向信号（值越高越强）。desc 供前端 hover 解释。
L2_RECOMMENDED_FACTORS: list[dict[str, Any]] = [
    {"name": "micro_vpin_vol_ratio", "category": "VPIN", "icir": 0.562, "label": "VPIN/量比",
     "desc": "按成交量口径的知情交易概率。越高=知情资金越活跃、毒性流动性越强（报告最强因子）"},
    {"name": "micro_vpin_amount_ratio", "category": "VPIN", "icir": 0.483, "label": "VPIN/额比",
     "desc": "按成交额口径的知情交易概率。越高=大单主导、知情资金进场"},
    {"name": "micro_pin", "category": "VPIN", "icir": 0.159, "label": "Pin 知情概率",
     "desc": "订单流中携带信息成分的占比估计。越高=知情投资者参与度越高"},
    {"name": "micro_zone_distribution", "category": "时段", "icir": 0.417, "label": "时段量分布",
     "desc": "日内成交量在各时段分布的集中度。越高=成交越集中在某几个时段"},
    {"name": "micro_zone_vol_ratio_T4", "category": "时段", "icir": 0.345, "label": "T4 时段量比",
     "desc": "第4时段（约10:30-11:00）成交量与日均量的比值。>1=该时段放量"},
    {"name": "micro_zone_vol_ratio_T6", "category": "时段", "icir": 0.338, "label": "T6 时段量比",
     "desc": "第6时段（约13:30-14:00）成交量与日均量的比值"},
    {"name": "micro_zone_vol_ratio_T5", "category": "时段", "icir": 0.316, "label": "T5 时段量比",
     "desc": "第5时段（约11:00-11:30）成交量与日均量的比值"},
    {"name": "micro_zone_vol_ratio_T3", "category": "时段", "icir": 0.198, "label": "T3 时段量比",
     "desc": "第3时段（约10:00-10:30）成交量与日均量的比值"},
    {"name": "micro_zone_rv_ratio_close", "category": "时段", "icir": 0.156, "label": "尾盘实现波动",
     "desc": "收盘时段已实现波动相对日均的比值。越高=尾盘波动放大，多空博弈加剧"},
    {"name": "vol_price_divergence", "category": "量价背离", "icir": 0.332, "label": "量价背离度",
     "desc": "成交量与价格走势的背离程度。越高=放量不涨/缩量不跌，主力行为异常"},
    {"name": "micro_open_gap", "category": "竞价", "icir": 0.273, "label": "跳空幅度",
     "desc": "开盘价相对昨收的跳空幅度（集合竞价定价偏移）。正=高开"},
    {"name": "micro_impact_decay_half_life", "category": "冲击", "icir": 0.271, "label": "冲击衰减半衰期",
     "desc": "大单冲击后价格恢复到均衡一半所需时间。越高=价格越有韧性/恢复越慢"},
    {"name": "micro_liquidity_daily_pattern", "category": "流动性", "icir": 0.237, "label": "流动性日模式",
     "desc": "流动性在日内时段的规律性形态强度。越高=日内流动性结构越稳定"},
    {"name": "flow_imbalance_revert_speed", "category": "资金流", "icir": 0.161, "label": "失衡回补速度",
     "desc": "主动买卖失衡后资金回补的速度。越高=失衡修复越快、趋势越易延续"},
]


@lru_cache(maxsize=1)
def _l2_partitions() -> tuple[str, ...]:
    """L2 因子分区目录（dt=YYYYMMDD）升序。"""
    d = _quantdb_dir() / "6_ml_datasets" / "l2_factors"
    parts = sorted((p.name for p in d.glob("dt=*")))
    return tuple(parts)


def _l2_feature_date(signal_date: str | None) -> str | None:
    """预测日的前一个交易日（L2 分区 dt 中 < signal_date 的最近一天）。"""
    dt = (signal_date or "").replace("-", "")
    for p in reversed(_l2_partitions()):
        d = p.removeprefix("dt=")
        if d < dt:
            return d
    return None


# L2 全市场因子表缓存：同一天切多只股票时避免重复读全市场 parquet（实测每次约 0.5~1s）
_l2_frame_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_l2_frame_cache_lock = threading.Lock()
_L2_FRAME_TTL = 300.0


def _l2_frame_for(feature_date: str) -> pd.DataFrame | None:
    """读某特征日的全市场 L2 因子表（按日期缓存）。"""
    now = time.time()
    with _l2_frame_cache_lock:
        hit = _l2_frame_cache.get(feature_date)
        if hit is not None:
            if now - hit[0] > _L2_FRAME_TTL:
                _l2_frame_cache.pop(feature_date, None)
            else:
                return hit[1]
    import duckdb

    d = _quantdb_dir() / "6_ml_datasets" / "l2_factors" / f"dt={feature_date}" / "data.parquet"
    if not d.exists():
        return None
    names = [f["name"] for f in L2_RECOMMENDED_FACTORS]
    col_list = ", ".join(names)
    try:
        con = duckdb.connect()
        df = con.execute(
            f"SELECT symbol, {col_list} FROM read_parquet('{str(d)}')"
        ).fetchdf()
        con.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("read l2 factors %s failed: %s", feature_date, exc)
        return None
    if df.empty or "symbol" not in df.columns:
        return None
    df = df.replace([float("inf"), float("-inf")], float("nan"))
    with _l2_frame_cache_lock:
        _l2_frame_cache[feature_date] = (time.time(), df)
    return df


def _l2_features_for(symbol: str, feature_date: str) -> dict[str, Any] | None:
    """读某股在特征日（YYYYMMDD）的 14 个推荐 L2 因子 + 当日全市场截面百分位。

    返回 {feature_date, factors: [{name, value, pct_rank, category, icir}]}；
    分区缺失/股票缺失时返回 None。百分位用当日全市场排名，0~1，越高越强。
    """
    df = _l2_frame_for(feature_date)
    if df is None:
        return None
    row = df[df["symbol"] == symbol]
    factors = []
    for f in L2_RECOMMENDED_FACTORS:
        name = f["name"]
        if name not in df.columns:
            continue
        val = row[name].iloc[0] if not row.empty else None
        if val is None or not np.isfinite(val):
            factors.append({**f, "value": None, "pct_rank": None})
            continue
        val = float(val)
        s = df[name].dropna()
        pct_rank = None
        if len(s) > 2:
            pct_rank = round(float((s <= val).mean()), 4)  # 全市场低于该值的占比
        factors.append({**f, "value": round(val, 6), "pct_rank": pct_rank})
    return {"feature_date": f"{feature_date[:4]}-{feature_date[4:6]}-{feature_date[6:]}", "factors": factors}


async def _latest_signal_date_for(symbol: str) -> str | None:
    """该股最近的推理信号日（YYY-MM-DD），无则 None。"""
    code = symbol.split(".")[0]
    from sqlalchemy import text as _txt

    try:
        async with get_session() as s:
            r = (
                await s.execute(
                    _txt(
                        "SELECT MAX(trade_date) FROM engine_signal_scores "
                        "WHERE tenant_id='default' AND symbol=:c"
                    ),
                    {"c": code},
                )
            ).scalar_one_or_none()
            return str(r)[:10] if r else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("latest signal date for %s failed: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# P2: 财务报表 + 通用时序（估值/筹码资金/两融/情绪/股东）
# ---------------------------------------------------------------------------

from datetime import date as _date, timedelta as _timedelta  # noqa: E402

import re as _re

_SYMBOL_RE = _re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

# 财务三表 + 每股指标 关键字段（单位: 元；输出统一转亿元）
_INCOME_COLS = {
    "revenue": "营业收入", "total_operating_cost": "营业成本",
    "oper_profit": "营业利润", "net_profit_excl_min_int_inc": "归母净利润",
    "research_expenses": "研发费用", "sale_expense": "销售费用",
    "inc_tax": "所得税",
}
_BALANCE_COLS = {
    "tot_assets": "总资产", "tot_liab": "总负债", "total_equity": "股东权益",
    "cash_equivalents": "货币资金", "inventories": "存货",
    "total_current_assets": "流动资产", "accounts_payable": "应付账款",
    "shortterm_loan": "短期借款",
}
_CASHFLOW_COLS = {
    "net_cash_flows_oper_act": "经营现金流净额",
    "net_cash_flows_inv_act": "投资现金流净额",
    "net_cash_flows_fnc_act": "筹资现金流净额",
}
_PERSHARE_COLS = {
    "equity_roe": "ROE(%)", "gross_profit": "毛利率(%)", "net_profit": "净利率(%)",
    "inc_revenue_rate": "营收增速(%)", "inc_net_profit_rate": "净利增速(%)",
    "s_fa_eps_basic": "EPS(元)", "s_fa_bps": "每股净资产(元)",
    "sales_cash_flow": "销售现金流比",
}

# /series 时序组: (DuckDB 视图, 输出列)
_SERIES_GROUPS: dict[str, tuple[str, list[str]]] = {
    "valuation": ("qdb_valuation", [
        "pe_ttm", "pb", "ps_ttm", "dividend_rate", "total_mv", "float_mv",
    ]),
    "margin": ("qdb_margin_trading", [
        "finance_balance", "finance_net", "finance_buy", "finance_repay",
    ]),
    "chip": ("qdb_l1_factors", [
        "chip_profit_ratio_20", "chip_profit_ratio_60", "chip_concentration_20",
        "chip_cost_90_width",
    ]),
    "flow": ("qdb_l2_factors", [
        "flow_net_amount", "flow_super_net", "flow_large_net", "flow_net_ratio",
    ]),
    "sentiment": ("qdb_market_sentiment", [
        "buy_pressure", "sell_pressure", "liquidity_score", "am_pm_trend",
        "volume_concentration",
    ]),
    "technical": ("qdb_technical_indicators", [
        "rsi_6", "rsi_14", "macd_dif", "macd_dea", "macd_hist",
        "vol_std_20", "vol_atr_14", "beta_20",
    ]),
}

# /series 进程级缓存：DuckDB 视图基于 read_parquet，每次查询冷扫全分区（technical 组实测
# 2s/次）。切换股票时各 Tab 高频触发同一 (symbol, group) 查询，加 LRU 命中后毫秒级返回。
# 数据日频更新，TTL 5 分钟足够；end_date 按日粒度归一，避免同一交易日多 key 击穿。
_SERIES_TTL = 300.0
_SERIES_CACHE_MAX = 256
_series_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_series_cache_lock = threading.Lock()


def _series_cache_key(symbol: str, group: str, years: int, end_date: str | None) -> str:
    return f"{symbol}|{group}|{years}|{end_date or ''}"


def _series_cache_get(key: str) -> pd.DataFrame | None:
    now = time.time()
    with _series_cache_lock:
        hit = _series_cache.get(key)
        if hit is None:
            return None
        if now - hit[0] > _SERIES_TTL:
            _series_cache.pop(key, None)
            return None
        return hit[1]


def _series_cache_set(key: str, df: pd.DataFrame) -> None:
    with _series_cache_lock:
        _series_cache[key] = (time.time(), df)
        # LRU 淘汰：超出上限时移除最早写入的条目
        if len(_series_cache) > _SERIES_CACHE_MAX:
            oldest = min(_series_cache.items(), key=lambda kv: kv[1][0])[0]
            _series_cache.pop(oldest, None)


def _read_symbol_parquet(ds: str, symbol: str) -> pd.DataFrame:
    """读 3_financial_data 下单标的平铺 parquet（小文件，直接读）。"""
    f = _quantdb_dir() / "3_financial_data" / ds / f"{symbol}.parquet"
    if not f.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("read %s/%s failed: %s", ds, symbol, exc)
        return pd.DataFrame()


def _fin_records(df: pd.DataFrame, cols: dict[str, str], limit: int, yi: bool) -> list[dict]:
    """财务表 -> {period, items:{中文名: 亿元/原值}} 按报告期倒序。"""
    if df.empty:
        return []
    df = df.sort_values("m_timetag", ascending=False).head(limit)
    out: list[dict] = []
    for _, r in df.iterrows():
        items: dict[str, float | None] = {}
        for col, label in cols.items():
            v = _safe_f(r.get(col))
            if v is not None and yi:
                v = round(v / 1e8, 2)  # 元 -> 亿元
            items[label] = v
        out.append({"period": str(r.get("m_timetag") or "")[:8], "items": items})
    return out


@router.get("/dividends")
async def stock_dividends(
    symbol: str = Query(...),
    date: str | None = Query(None, description="只看该日及之前的分红 YYYY-MM-DD（缺省=全部）"),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")
    df = _read_symbol_parquet("dividend_factors", sym)
    if df.empty:
        return {"success": True, "data": {"items": []}}
    df = df.sort_values("time", ascending=False)
    if date:
        df = df[df["time"].astype(str).str[:10] <= date]
    df = df.head(40)
    items = [
        {
            "date": str(r.get("time"))[:10],
            "interest": _safe_f(r.get("interest")),       # 每股派息(元)
            "stock_bonus": _safe_f(r.get("stockBonus")),  # 送股比例
            "stock_gift": _safe_f(r.get("stockGift")),    # 转增比例
            "gugai": _safe_f(r.get("gugai")),             # 股改? / 除权
            "dr": _safe_f(r.get("dr")),                   # 除权系数
        }
        for _, r in df.iterrows()
    ]
    return {"success": True, "data": {"items": items}}


@router.get("/tags")
async def stock_tags(
    symbol: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """个股命中标签 + 命中的组合预设。"""
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")

    import asyncio

    def _run() -> tuple[list[dict], list[dict]]:
        from backend.services.engine.data_platform import tag_rules

        return tag_rules.match_tags_for_symbol(sym), tag_rules.preset_matched(sym)

    try:
        tags, presets = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tag match %s failed: %s", sym, exc)
        tags, presets = [], []
    return {"success": True, "data": {"tags": tags, "presets": presets}}


@router.get("/tags/{tag_id}/stocks")
async def tag_stocks(
    tag_id: str,
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """标签同类股票（按 sort_key 排序 TopN）。"""
    _ = current_user

    import asyncio

    def _run() -> dict:
        from backend.services.engine.data_platform import tag_rules

        return tag_rules.stocks_for_tag(tag_id, limit=limit)

    try:
        result = await asyncio.to_thread(_run)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("tag stocks %s failed: %s", tag_id, exc)
        result = {"items": [], "score_min": None, "score_max": None}
    return {"success": True, "data": result}


@router.get("/presets")
async def list_presets(current_user: dict = Depends(get_current_user)):
    _ = current_user
    return {"success": True, "data": {"presets": [
        {"id": p["id"], "name": p["name"], "logic": p["logic"], "tags": p["tags"]}
        for p in __import__("backend.services.engine.data_platform.tag_rules", fromlist=["PRESETS"]).PRESETS
    ]}}


@router.get("/signal-overlay")
async def stock_signal_overlay(
    symbol: str = Query(...),
    days: int = Query(250, ge=30, le=1000),
    current_user: dict = Depends(get_current_user),
):
    """推理分数叠加：engine_signal_scores 按 model_version 分组返回。

    同表同口径与推理中心一致。返回 {dates, series:{model_version: [{fusion, side}]}}
    """
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")
    prefix = f"{sym.split('.')[1]}{sym.split('.')[0]}"  # 600519.SH -> SH600519

    from datetime import timedelta as _td

    async with get_session() as session:
        from sqlalchemy import text as _text

        start = _date.today() - _timedelta(days=days * 2)
        rows = (
            await session.execute(
                _text(
                    "SELECT trade_date, fusion_score, signal_side, model_version "
                    "FROM engine_signal_scores "
                    "WHERE tenant_id = :tid AND symbol = :s AND trade_date >= :start "
                    "ORDER BY trade_date"
                ),
                {"tid": "default", "s": prefix, "start": start},
            )
        ).fetchall()

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        mv = str(r[3] or "default")
        grouped.setdefault(mv, []).append({
            "date": str(r[0])[:10],
            "fusion": float(r[1]) if r[1] is not None else None,
            "side": str(r[2] or "HOLD"),
        })
    # 只保留最近 days 个交易日
    for mv in grouped:
        grouped[mv] = grouped[mv][-days:]
    return {"success": True, "data": {"series": grouped}}


@router.get("/chart-backtest")
async def chart_backtest(
    symbol: str = Query(...),
    buy_expr: str = Query(..., description="买入条件，如 CROSSUP(MA(CLOSE,5),MA(CLOSE,20))"),
    sell_expr: str = Query("", description="卖出条件；空=持有到结束"),
    days: int = Query(500, ge=50, le=2000),
    current_user: dict = Depends(get_current_user),
):
    """图表内简单策略回测：表达式条件 -> 次日开盘撮合（防未来函数）。

    返回: 交易点列表 {date, side, price, pnl} + 净值/胜率/回撤/年化。
    """
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")

    from datetime import timedelta as _td

    def _load() -> pd.DataFrame:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        end = _date.today()
        start = end - _td(days=int(days * 1.6))
        df = hub.fetch_daily_kline(sym, start, end)
        if df is None or df.empty:
            return pd.DataFrame()
        return df.sort_values("trade_date").tail(days).reset_index(drop=True)

    def _run() -> dict:
        from backend.services.engine.data_platform import expr_engine as ee

        df = _load()
        if df.empty:
            raise ValueError("无 K 线数据")
        ohlcv = df.rename(columns={"trade_date": "date"})[["date", "open", "high", "low", "close", "volume"]]
        ctx = ee.build_context(ohlcv)
        buy_sig = ee.eval_bool_expr(ee.compile_expr(buy_expr), ctx)
        sell_sig = ee.eval_bool_expr(ee.compile_expr(sell_expr), ctx) if sell_expr.strip() else None

        n = len(ohlcv)
        trades: list[dict] = []
        position = 0.0          # 持仓股数（满仓=资金/价，用1股基线）
        cash = 100000.0
        entry_price = 0.0
        entry_date = ""
        buy_signaled = False

        # 次日开盘成交，防未来函数
        for i in range(1, n):
            date = ohlcv["date"].iloc[i]
            open_p = float(ohlcv["open"].iloc[i])
            if position == 0:
                if buy_sig.iloc[i - 1]:
                    shares = cash / open_p
                    cash -= shares * open_p * 1.00025
                    position = shares
                    entry_price = open_p
                    entry_date = date
                    trades.append({"date": str(date)[:10], "side": "BUY", "price": round(open_p, 2),
                                   "pnl": None, "signal_date": str(ohlcv["date"].iloc[i - 1])[:10]})
                    buy_signaled = True
            else:
                sell_now = sell_sig is not None and sell_sig.iloc[i - 1]
                # 若持有中且已无买入信号且超过20根，强制止盈/止损为下一个卖出信号
                if sell_now:
                    cash += position * open_p * (1 - 0.0013)  # 卖出费+印花税
                    pnl = (open_p - entry_price) / entry_price * 100
                    trades.append({"date": str(date)[:10], "side": "SELL", "price": round(open_p, 2),
                                   "pnl": round(pnl, 2), "signal_date": str(ohlcv["date"].iloc[i - 1])[:10]})
                    position = 0.0
                    entry_price = 0.0

        # 期末市值
        final_close = float(ohlcv["close"].iloc[-1])
        if position > 0:
            cash += position * final_close
            trades.append({"date": str(ohlcv["date"].iloc[-1])[:10], "side": "CLOSE",
                           "price": round(final_close, 2), "pnl": round((final_close - entry_price) / entry_price * 100, 2),
                           "signal_date": str(ohlcv["date"].iloc[-1])[:10]})

        total_ret = (cash - 100000.0) / 100000.0 * 100
        # 基准：买入持有
        base_ret = (float(ohlcv["close"].iloc[-1]) - float(ohlcv["open"].iloc[0])) / float(ohlcv["open"].iloc[0]) * 100

        sells = [t for t in trades if t["side"] == "SELL"]
        wins = [t for t in sells if (t["pnl"] or 0) > 0]
        # 净值曲线：按日模拟（重建持仓历史）
        hist_pos = 0.0
        hist_cash = 100000.0
        hist_entry = 0.0
        equity = []
        for i in range(n):
            if i > 0:
                if hist_pos == 0 and buy_sig.iloc[i - 1]:
                    hist_pos = hist_cash / float(ohlcv["open"].iloc[i])
                    hist_cash -= hist_pos * float(ohlcv["open"].iloc[i]) * 1.00025
                    hist_entry = float(ohlcv["open"].iloc[i])
                elif hist_pos > 0 and sell_sig is not None and sell_sig.iloc[i - 1]:
                    hist_cash += hist_pos * float(ohlcv["open"].iloc[i]) * (1 - 0.0013)
                    hist_pos = 0.0
            equity.append(hist_cash + hist_pos * float(ohlcv["close"].iloc[i]))
        peak = -1e18
        max_dd = 0.0
        for e in equity:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak * 100)

        return {
            "trades": trades,
            "total_return": round(total_ret, 2),
            "buy_hold_return": round(base_ret, 2),
            "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else None,
            "trade_count": len(sells),
            "max_drawdown": round(max_dd, 2),
            "points": [
                {"date": str(ohlcv["date"].iloc[i]), "close": round(float(ohlcv["close"].iloc[i]), 2),
                 "equity": round(eq, 2)}
                for i, eq in enumerate(equity)
            ],
        }

    import asyncio

    try:
        result = await asyncio.to_thread(_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("chart-backtest %s failed: %s", sym, exc)
        raise HTTPException(status_code=500, detail=f"回测失败: {exc}")
    return {"success": True, "data": result}


@router.get("/news")
async def stock_news(
    symbol: str = Query(...),
    limit: int = Query(15, ge=1, le=50),
    current_user: dict = Depends(get_current_user),
):
    """个股 RSS 资讯：Huntly SQLite immutable 只读快照 + 标题关键词检索。

    不用 news.py 的共享 _list_articles_from_sqlite（mode=ro 会被 Huntly Java
    写锁阻塞，PRAGMA 都拿不到读锁）；这里用 immutable=1 跳过锁协商直接读。
    匹配字段限 title（全文 LIKE 太慢，1.4GB 库 2.9s/标题，正文会分钟级）。
    """
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")

    import os as _os
    import sqlite3 as _sq

    huntly_db = _os.getenv("HUNTLY_SQLITE_PATH", "/data/huntly/db.sqlite")
    if not _os.path.exists(huntly_db):
        return {"success": True, "data": {"items": [], "total": 0, "available": False}}

    code = sym.split(".")[0]
    name = ""
    try:
        detail_dir = _quantdb_dir() / "2_base_sector" / "instrument_detail"
        detail_file = detail_dir / "instrument_list.parquet"
        if not detail_file.exists():
            detail_file = detail_dir / "instrument_detail.parquet"
        detail = pd.read_parquet(detail_file, columns=["Symbol", "Name"])
        hit = detail[detail["Symbol"] == sym]
        if not hit.empty:
            name = str(hit.iloc[0]["Name"] or "").strip()
    except Exception:  # noqa: BLE001
        pass

    keywords = [k for k in {code, name, name.replace(" ", "")} if k]
    items: list[dict] = []
    seen: set[int] = set()
    try:
        conn = _sq.connect(f"file:{huntly_db}?immutable=1", uri=True, timeout=3)
        conn.row_factory = _sq.Row
        for kw in keywords:
            rows = conn.execute(
                "SELECT id, title, url, updated_at, connector_id FROM page "
                "WHERE title LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{kw}%", limit),
            ).fetchall()
            for r in rows:
                rid = r["id"]
                if rid in seen:
                    continue
                seen.add(rid)
                items.append({
                    "id": rid,
                    "title": r["title"],
                    "link": r["url"],
                    "published_at": str(r["updated_at"] or "")[:19],
                    "source": str(r["connector_id"] or ""),
                })
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("stock_news %s failed: %s", sym, exc)
        return {"success": True, "data": {"items": items, "total": len(items), "available": False}}
    items = items[:limit]

    # 按 huntly_page_id 关联 enrichment 情绪标签（FinBERT/字典法，可能为空）
    if items:
        try:
            import asyncpg  # noqa: F401

            from backend.shared.database_manager_v2 import get_session

            page_ids = [it["id"] for it in items]
            async with get_session() as session:
                from sqlalchemy import text as _txt
                from sqlalchemy.dialects.postgresql import asyncpg as _apg

                rows = (
                    await session.execute(
                        _txt(
                            "SELECT huntly_page_id, sentiment_label, sentiment_score, tickers "
                            "FROM news_article_enrichment WHERE huntly_page_id = ANY(:pids)"
                        ),
                        {"pids": page_ids},
                    )
                ).fetchall()
            enrich_map = {
                int(r[0]): {
                    "sentiment_label": str(r[1]) if r[1] else None,
                    "sentiment_score": float(r[2]) if r[2] is not None else None,
                    "tickers": (r[3] or []) if isinstance(r[3], (list, tuple)) else [],
                }
                for r in rows
            }
            for it in items:
                e = enrich_map.get(int(it["id"]))
                if e:
                    it["sentiment_label"] = e["sentiment_label"]
                    it["sentiment_score"] = e["sentiment_score"]
                    it["tickers"] = e["tickers"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("stock_news enrichment join %s failed: %s", sym, exc)

    # 对照 新闻情绪深度报告（docs/news_sentiment_deep_report.md）打分级标记：
    # 来源可信度 × 时段质量 × 事件标签，追加以便资讯列表直观判断
    try:
        from backend.services.api.news.report_match import annotate_news_item
    except Exception:  # noqa: BLE001
        annotate_news_item = None
    if annotate_news_item:
        items = [annotate_news_item(it) for it in items]

    return {"success": True, "data": {"items": items, "total": len(items), "available": True}}


@router.get("/ai-backtest")
async def ai_backtest(
    symbol: str = Query(...),
    hint: str = Query("", description="用户提示词，如 '底部放量突破'"),
    current_user: dict = Depends(get_current_user),
):
    """AI 生成策略表达式（利用命中标签+技术形态）-> 建议 buy/sell DSL 表达式。"""
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")

    from backend.services.engine.ai_strategy.provider_registry import get_provider

    # 收集上下文：命中标签 + 最新技术指标
    context_lines = []
    try:
        import asyncio as _aio

        def _tags():
            from backend.services.engine.data_platform import tag_rules

            return tag_rules.match_tags_for_symbol(sym)

        tags = await asyncio.to_thread(_tags)
        context_lines.append("命中标签: " + ", ".join(t["name"] for t in tags[:10]))
    except Exception:  # noqa: BLE001
        pass
    try:
        ti = pd.read_parquet(_latest_partition(_quantdb_dir() / "5_technical_derived" / "technical_indicators"),
                             columns=["symbol", "close", "ma5", "ma20", "rsi_14", "vol_to_ma5", "macd_hist"])
        r = ti[ti["symbol"] == sym]
        if not r.empty:
            x = r.iloc[0]
            context_lines.append(
                f"最新收盘 {x.get('close'):.2f}, MA5 {x.get('ma5'):.2f}, MA20 {x.get('ma20'):.2f}, "
                f"RSI14 {x.get('rsi_14'):.1f}, 量比MA5 {x.get('vol_to_ma5'):.2f}, MACD柱 {x.get('macd_hist'):.4f}"
            )
    except Exception:  # noqa: BLE001
        pass

    prompt = (
        "你是 A 股量化策略专家。给定个股 {sym} 的状态，生成一套简单均线/指标策略的买卖条件表达式。\n"
        "可用函数: MA(CLOSE,N), EMA(CLOSE,N), RSI(CLOSE,14), HHV(HIGH,N), LLV(LOW,N), "
        "REF(X,N), CROSSUP(A,B), CROSSDOWN(A,B), CROSS(A,B), AND(A,B), OR(A,B), NOT(A)\n"
        "变量: CLOSE, OPEN, HIGH, LOW, VOLUME\n"
        "上下文:\n{ctx}\n用户意图: {hint}\n\n"
        "只输出 JSON: {{\"buy\": \"...\", \"sell\": \"...\", \"name\": \"策略名\"}}，不要其他文字。"
    ).format(sym=sym, ctx="\n".join(context_lines) or "无", hint=hint or "通用趋势策略")

    try:
        # 系统配置了 DEEPSEEK_API_KEY 时优先用 deepseek（qwen 无 key 会 401）
        import os as _os2

        provider_name = "deepseek" if _os2.getenv("DEEPSEEK_API_KEY") else None
        provider = get_provider(provider_name)
        import json as _json

        resp = await provider.chat([
            {"role": "system", "content": "你是严谨的量化策略专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])
        text = resp if isinstance(resp, str) else (resp.get("content") or str(resp))
        # 提取 JSON
        start = text.find("{")
        end = text.rfind("}") + 1
        parsed = _json.loads(text[start:end]) if start >= 0 and end > start else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai-backtest llm failed: %s", exc)
        return {"success": True, "data": {
            "buy": "CROSSUP(MA(CLOSE,5),MA(CLOSE,20))",
            "sell": "CROSSDOWN(MA(CLOSE,5),MA(CLOSE,20))",
            "name": f"AI默认-{sym}",
            "llm_error": str(exc),
        }}

    # 校验表达式能编译
    from backend.services.engine.data_platform import expr_engine as _ee

    buy_expr = str(parsed.get("buy") or "").strip()
    sell_expr = str(parsed.get("sell") or "").strip()
    try:
        _ee.compile_expr(buy_expr)
    except Exception as exc:  # noqa: BLE001
        return {"success": True, "data": {
            "buy": "CROSSUP(MA(CLOSE,5),MA(CLOSE,20))", "sell": sell_expr,
            "name": str(parsed.get("name") or "AI策略"),
            "llm_error": f"买入表达式无法编译: {exc}",
        }}
    return {"success": True, "data": {
        "buy": buy_expr,
        "sell": sell_expr or "",
        "name": str(parsed.get("name") or "AI策略"),
        "llm_error": None,
    }}


@router.get("/minute")
async def stock_minute_kline(
    symbol: str = Query(...),
    freq: str = Query("min5", description="min5 / min1"),
    days: int = Query(10, ge=1, le=30),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")

    def _run() -> pd.DataFrame:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        subdir = "min1_kline" if freq == "min1" else "min5_kline"
        f = _quantdb_dir() / "1_kline_data" / subdir / f"{sym}.parquet"
        if not f.exists():
            return pd.DataFrame()
        try:
            return pd.read_parquet(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("read %s/%s failed: %s", subdir, sym, exc)
            return pd.DataFrame()

    import asyncio

    df = await asyncio.to_thread(_run)
    if df.empty:
        return {"success": True, "data": {"items": [], "available": False}}
    df = df.sort_values("time").tail(days * 48)
    items = [
        {
            "date": str(r.get("time"))[:16].replace(" ", " "),
            "open": _safe_f(r.get("open")),
            "high": _safe_f(r.get("high")),
            "low": _safe_f(r.get("low")),
            "close": _safe_f(r.get("close")),
            "volume": _safe_f(r.get("volume")),
            "amount": _safe_f(r.get("amount")),
        }
        for _, r in df.iterrows()
    ]
    return {"success": True, "data": {"items": items, "available": True}}


@router.get("/financials")
async def stock_financials(
    symbol: str = Query(..., description="600519.SH"),
    limit: int = Query(8, ge=2, le=20),
    date: str | None = Query(None, description="只看该日之前披露的报告期 YYYY-MM-DD（缺省=全部）"),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")

    income = _read_symbol_parquet("income", sym)
    balance = _read_symbol_parquet("balance", sym)
    cashflow = _read_symbol_parquet("cashflow", sym)
    pershare = _read_symbol_parquet("pershare_index", sym)

    if date:
        # 只看该日之前披露的报告期（日历点选历史日时财报不出现未来数据）
        cutoff = date.replace("-", "")

        def _until_disclosed(df: pd.DataFrame) -> pd.DataFrame:
            return df if df.empty else df[df["m_timetag"].astype(str).str[:8] <= cutoff]

        income = _until_disclosed(income)
        balance = _until_disclosed(balance)
        cashflow = _until_disclosed(cashflow)
        pershare = _until_disclosed(pershare)

    periods = (
        sorted(
            {str(v)[:8] for v in pershare.get("m_timetag", [])}
            | {str(v)[:8] for v in income.get("m_timetag", [])},
            reverse=True,
        )[:limit]
        if (not pershare.empty or not income.empty)
        else []
    )
    return {
        "success": True,
        "data": {
            "symbol": sym,
            "periods": periods,
            "income": _fin_records(income, _INCOME_COLS, limit, yi=True),
            "balance": _fin_records(balance, _BALANCE_COLS, limit, yi=True),
            "cashflow": _fin_records(cashflow, _CASHFLOW_COLS, limit, yi=True),
            "per_share": _fin_records(pershare, _PERSHARE_COLS, limit, yi=False),
        },
    }


@router.get("/series")
async def stock_series(
    symbol: str = Query(...),
    group: str = Query(..., description="valuation/margin/chip/flow/sentiment/technical/holders"),
    years: int = Query(3, ge=1, le=10),
    end_date: str | None = Query(None, description="截止日 YYYY-MM-DD，只看该日及之前（日历点选日联动）"),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    sym = symbol.upper().strip()
    if not _SYMBOL_RE.match(sym):
        raise HTTPException(status_code=400, detail=f"非法代码 {sym}")

    # 股东户数: 平铺小文件, endDate 为报告期
    if group == "holders":
        hn = _read_symbol_parquet("holder_num", sym)
        if hn.empty:
            return {"success": True, "data": {"dates": [], "columns": {}}}
        hn = hn.sort_values("endDate")
        if end_date:
            hn = hn[hn["endDate"].astype(str).str[:10] <= end_date]
        return {
            "success": True,
            "data": {
                "dates": [str(v)[:10] for v in hn["endDate"]],
                "columns": {"holder_num": [_safe_f(v) for v in hn["shareholder"]]},
            },
        }

    spec = _SERIES_GROUPS.get(group)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"未知时序组 {group}")
    view, cols = spec
    # dt 为整数 YYYYMMDD
    start_dt = (_date.today() - _timedelta(days=years * 366)).strftime("%Y%m%d")
    end_dt = end_date.replace("-", "") if end_date else ""
    cache_key = _series_cache_key(sym, group, years, end_dt)
    cached_df = _series_cache_get(cache_key)
    if cached_df is not None:
        df = cached_df
    else:
        col_list = ", ".join(cols)
        sql = (
            f"SELECT dt, {col_list} FROM {view} "
            f"WHERE symbol = '{sym}' AND dt >= {start_dt}"
            + (f" AND dt <= {end_dt}" if end_dt else "")
            + " ORDER BY dt"
        )

        def _run() -> pd.DataFrame:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            try:
                return QuantDBDataHub.get_instance().query(sql)
            except Exception as exc:  # noqa: BLE001
                logger.warning("series query %s %s failed: %s", group, sym, exc)
                return pd.DataFrame()

        import asyncio

        df = await asyncio.to_thread(_run)
        _series_cache_set(cache_key, df)
    if df.empty:
        return {"success": True, "data": {"dates": [], "columns": {}}}
    dates = [str(v)[:10] for v in df["dt"]]
    columns = {c: [_safe_f(v) for v in df[c]] for c in cols if c in df.columns}
    return {"success": True, "data": {"dates": dates, "columns": columns}}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _index_members(index_code: str) -> set[str]:
    """宽基指数成分 symbol 集合（index_weights parquet）。"""
    d = _quantdb_dir()
    f = d / "2_base_sector" / "index_weights" / f"{index_code}.parquet"
    if not f.exists():
        return set()
    try:
        w = pd.read_parquet(f)
        sym_col = "Symbol" if "Symbol" in w.columns else "symbol"
        return set(w[sym_col].astype(str))
    except Exception as exc:  # noqa: BLE001
        logger.warning("read index members %s failed: %s", index_code, exc)
        return set()


# 筛选面板选项（与前端 StockFilterPanel 保持一致）
BOARD_OPTIONS = ["沪市主板", "深市主板", "科创板", "创业板", "北交所"]
CAP_TIER_OPTIONS = [
    {"value": "微盘", "label": "微盘 <30亿"},
    {"value": "小盘", "label": "小盘 30-100亿"},
    {"value": "中盘", "label": "中盘 100-300亿"},
    {"value": "大盘", "label": "大盘 300-1000亿"},
    {"value": "超大盘", "label": "超大盘 >1000亿"},
]
TREND_OPTIONS = [
    {"value": "连续上升", "label": "连续上升"},
    {"value": "连续下降", "label": "连续下降"},
    {"value": "先升后降", "label": "先升后降 · 最佳买点"},
    {"value": "上升", "label": "单日上升"},
    {"value": "下降", "label": "单日下降"},
    {"value": "持平", "label": "持平"},
]


def _cap_mask(mv: pd.Series, tier: str) -> pd.Series:
    """市值档布尔掩码（与 _cap_tier_of 同阈值）。"""
    if tier == "微盘":
        return mv < 30
    if tier == "小盘":
        return (mv >= 30) & (mv < 100)
    if tier == "中盘":
        return (mv >= 100) & (mv < 300)
    if tier == "大盘":
        return (mv >= 300) & (mv < 1000)
    return mv >= 1000


def _cap_tier_of(mv_yi) -> str:
    """市值分档（亿元）：同推理研究阈值。"""
    mv = _safe_f(mv_yi)
    if mv is None:
        return ""
    if mv < 30:
        return "微盘"
    if mv < 100:
        return "小盘"
    if mv < 300:
        return "中盘"
    if mv < 1000:
        return "大盘"
    return "超大盘"


async def _trend_map(model: str | None, before=None) -> dict[str, str]:
    """每股最近 3 个信号日的分数趋势（symbol 纯数字 -> 趋势标签）。

    before: date 对象时，取 before 及之前的最近 3 个信号日（日历点选历史日联动）。
    """
    from sqlalchemy import text as _txt

    async with get_session() as session:
        mwhere = "AND run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE model_id = :m)" if model else ""
        params: dict = {"m": model} if model else {}
        bwhere = ""
        if before is not None:
            bwhere = "AND trade_date <= :b"
            params["b"] = before
        dates = [
            r[0]  # date 对象（asyncpg 需 date 类型绑定；显示用 str）
            for r in (
                await session.execute(
                    _txt(
                        "SELECT DISTINCT trade_date FROM engine_signal_scores "
                        f"WHERE tenant_id='default' {mwhere} {bwhere} "
                        "ORDER BY trade_date DESC LIMIT 3"
                    ),
                    params,
                )
            ).fetchall()
        ]
        if len(dates) < 2:
            return {}
        from sqlalchemy import bindparam as _bindparam

        stmt = _txt(
            "SELECT symbol, trade_date, fusion_score, created_at FROM engine_signal_scores "
            "WHERE tenant_id='default' AND trade_date IN :ds "
            f"{mwhere} {bwhere} ORDER BY created_at"
        ).bindparams(_bindparam("ds", expanding=True))
        rows = (
            await session.execute(stmt, {**params, "ds": tuple(dates)})
        ).fetchall()
    # 每 (symbol, date) 取 created_at 最新一条
    per: dict[tuple[str, str], float] = {}
    for sym, d, fusion, _created in rows:
        if fusion is not None:
            per[(str(sym), str(d))] = float(fusion)
    out: dict[str, str] = {}
    for (sym, d), fusion in per.items():
        idx = next((i for i, dt in enumerate(dates) if str(dt) == d), -1)
        if idx == 0:  # 最新日
            s2 = fusion
            s1 = per.get((sym, str(dates[1]))) if len(dates) > 1 else None
            s0 = per.get((sym, str(dates[2]))) if len(dates) > 2 else None
            if s1 is None:
                continue
            if s0 is not None:
                if s2 > s1 > s0:
                    t = "连续上升"
                elif s2 < s1 < s0:
                    t = "连续下降"
                elif s1 > s0 and s2 < s1:
                    t = "先升后降"
                elif s2 > s1:
                    t = "上升"
                elif s2 < s1:
                    t = "下降"
                else:
                    t = "持平"
            else:
                t = "上升" if s2 > s1 else ("下降" if s2 < s1 else "持平")
            out[sym] = t
    return out


@router.get("/list")
async def list_stocks(
    market: str = Query("ALL", description="SH / SZ / BJ / ALL"),
    industry: str | None = Query(None, description="行业名称（rs_hyname）"),
    q: str | None = Query(None, description="代码/名称模糊检索"),
    only_st: bool = Query(False, description="仅 ST 股"),
    exclude_st: bool = Query(False, description="排除 ST 股"),
    date: str | None = Query(None, description="推理分数基准日 YYYY-MM-DD，缺省=最近有分数日"),
    model: str | None = Query(None, description="推理模型（qm_model_inference_runs.model_id），缺省=全部模型融合"),
    score_min: float | None = Query(None, description="推理分数下限（fusion_score）"),
    score_max: float | None = Query(None, description="推理分数上限"),
    only_signaled: bool = Query(False, description="仅 BUY/SELL（排除 HOLD）"),
    side: str | None = Query(None, description="信号方向：BUY / SELL / HOLD"),
    concept: str | None = Query(None, description="概念板块（sector_members 板块名）"),
    board: str | None = Query(None, description="板块：沪市主板/深市主板/科创板/创业板/北交所"),
    cap_tier: str | None = Query(None, description="市值档：微盘/小盘/中盘/大盘/超大盘"),
    trend: str | None = Query(None, description="分数趋势：连续上升/连续下降/先升后降/上升/下降/持平"),
    tag: str | None = Query(None, description="智能标签 id（tag_rules）"),
    index_code: str | None = Query(None, description="宽基指数成分过滤（index_weights parquet 名）"),
    with_counts: bool = Query(False, description="附带筛选下拉的选项命中数（option_counts）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=6000),
    find_symbol: str | None = Query(None, description="定位股票（600519.SH 或纯代码），返回当前排序中的名次与页数"),
    symbols: str | None = Query(None, description="自选股列表（逗号分隔，prefix/suffix/纯代码均可），按当前排序保留分数降序"),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    # 日历点选历史日时 close/pct_change 也读该日快照（左右整页随日期联动）
    # _load_universe 同步读 parquet+merge，跑在 event loop 上会阻塞全部并发请求
    # （单 worker uvicorn），挪到线程池执行，list 接口并发不再互相排队
    m = market.upper()
    if m == "HK":
        # 港股：数据源 quanthk（security_master/sdl/因子），信号来自 HK 推理 run
        df, trade_date = await asyncio.to_thread(_load_universe_hk, asof=date)
    else:
        df, trade_date = await asyncio.to_thread(_load_universe, asof=date)
        if m in ("SH", "SZ", "BJ"):
            df = df[df["exchange"] == m]
    if industry:
        df = df[df["rs_hyname"] == industry]
    if q and q.strip():
        kw = q.strip()
        if m == "HK":
            # 港股代码 5 位（00700）与 4 位（0700.HK）等价：数字部分去前导零互配
            digits = "".join(ch for ch in kw if ch.isdigit())
            norm = digits.lstrip("0") if digits else ""
            sym_part = df["Symbol"].str.replace(".HK", "", regex=False).str.lstrip("0")
            hit_sym = df["Symbol"].str.contains(kw, case=False, regex=False, na=False) | sym_part.str.contains(norm, regex=False, na=False) if norm else False
            df = df[hit_sym | df["Name"].astype(str).str.contains(kw, case=False, regex=False, na=False)]
        else:
            df = df[df["Symbol"].str.contains(kw, case=False, regex=False, na=False) | df["Name"].astype(str).str.contains(kw, case=False, regex=False, na=False)]
    if only_st:
        df = df[_st_mask(df)]
    if exclude_st:
        df = df[~_st_mask(df)]
    if concept:
        members = await asyncio.to_thread(_concept_members, concept)
        if members:
            df = df[df["Symbol"].isin(members)]
    if index_code:
        members = _index_members(index_code)
        if members:
            df = df[df["Symbol"].isin(members)]
        else:
            df = df.iloc[0:0]
    if tag:
        try:
            from backend.services.engine.data_platform.tag_rules import stocks_for_tag

            # stocks_for_tag 内部 df.apply 全市场逐行 + 可能重建特征缓存（读多 parquet），
            # 同步执行会阻塞 event loop，挪线程池；结果已带 TTL 缓存，重复筛选秒回
            tag_syms = {
                str(it.get("symbol") or it.get("code") or "")
                for it in await asyncio.to_thread(stocks_for_tag, tag, 6000)
            }
            tag_codes = {x.split(".")[0] for x in tag_syms if x}
            if tag_codes:
                df = df[df["Symbol"].str.split(".").str[0].isin(tag_codes)]
            else:
                df = df.iloc[0:0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("tag filter %s failed: %s", tag, exc)

    # 维度筛选（board/cap_tier/trend/model/分数档）前的快照，供 option_counts 统计：
    # 其余已选条件（市场/行业/概念/检索/ST/宽基/标签）保持生效
    base_df = df.copy()
    if board:
        df = df[df["board"] == board]
    if cap_tier:
        mv = pd.to_numeric(df["Zsz"], errors="coerce")
        if cap_tier == "微盘":
            df = df[mv < 30]
        elif cap_tier == "小盘":
            df = df[(mv >= 30) & (mv < 100)]
        elif cap_tier == "中盘":
            df = df[(mv >= 100) & (mv < 300)]
        elif cap_tier == "大盘":
            df = df[(mv >= 300) & (mv < 1000)]
        elif cap_tier == "超大盘":
            df = df[mv >= 1000]

    # 推理分数叠加（engine_signal_scores）：默认最近有分数交易日单日，分数降序
    score_info: dict[str, dict] = {}
    _signal_date = None
    try:
        from datetime import date as _date2

        mwhere = "AND run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE model_id = :m)" if model else ""
        mparams: dict = {"m": model} if model else {}
        params: dict = {}
        if date:
            _signal_date = _date2.fromisoformat(date)
            latest = _signal_date
        else:
            async with get_session() as session:
                from sqlalchemy import text as _txt

                # 默认信号日：优先最近一个覆盖充分的推理日（COUNT(DISTINCT symbol)
                # >= _MIN_SIGNAL_COVERAGE），避免最新日只推理了少数股票导致列表
                # 第 1 页之后分数全空；无覆盖达标日时回退最近任意有分数日
                _d0 = (
                    await session.execute(
                        _txt(
                            "SELECT trade_date FROM engine_signal_scores e "
                            f"WHERE e.tenant_id='default' {mwhere} "
                            "GROUP BY trade_date "
                            "HAVING COUNT(DISTINCT symbol) >= :min_cov "
                            "ORDER BY trade_date DESC LIMIT 1"
                        ),
                        {**mparams, "min_cov": _MIN_SIGNAL_COVERAGE},
                    )
                ).scalar_one_or_none()
                if _d0 is None:
                    _d0 = (
                        await session.execute(
                            _txt(
                                "SELECT trade_date FROM engine_signal_scores e "
                                f"WHERE e.tenant_id='default' {mwhere} "
                                "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1"
                            ),
                            mparams,
                        )
                    ).scalar_one_or_none()
            latest = _d0
            _signal_date = str(_d0)[:10] if _d0 else None
        if latest is not None:
            where = "tenant_id = 'default' AND trade_date = :d"
            params: dict = {"d": latest}
            if model:
                # model_version 列恒为 'inference_script'（历史遗留），真实模型标识
                # 在 qm_model_inference_runs.model_id，按 run_id 关联过滤
                where += " AND run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE model_id = :m)"
                params["m"] = model
            if score_min is not None:
                where += " AND fusion_score >= :smin"
                params["smin"] = score_min
            if score_max is not None:
                where += " AND fusion_score <= :smax"
                params["smax"] = score_max
            if only_signaled:
                where += " AND signal_side IN ('BUY','SELL')"
            sql = (
                "SELECT symbol, fusion_score, signal_side, model_version, quality "
                f"FROM engine_signal_scores WHERE {where}"
            )
            async with get_session() as session:
                from sqlalchemy import text as _txt

                rows = (await session.execute(_txt(sql), params)).fetchall()
            for r in rows:
                sym = str(r[0])
                # engine_signal_scores.symbol 为纯数字 600519（不带市场后缀）
                sfx = sym if "." in sym else sym
                pos = None
                if isinstance(r[4], dict):
                    pos = r[4].get("position")
                score_info[sfx] = {
                    "fusion": float(r[1]) if r[1] is not None else None,
                    "side": str(r[2] or "HOLD"),
                    "date": str(latest)[:10],
                    "model": str(r[3] or ""),
                    "position_score": (float(pos.get("position_score")) if pos and pos.get("position_score") is not None else None),
                    "industry_top10_avg": (float(pos.get("industry_top10_avg")) if pos and pos.get("industry_top10_avg") is not None else None),
                    "board_top10_avg": (float(pos.get("board_top10_avg")) if pos and pos.get("board_top10_avg") is not None else None),
                    "cap_top10_avg": (float(pos.get("cap_top10_avg")) if pos and pos.get("cap_top10_avg") is not None else None),
                    "pct_industry": (float(pos.get("pct_industry")) if pos and pos.get("pct_industry") is not None else None),
                    "market_empty": (bool(pos.get("market_empty")) if pos else None),
                }
    except Exception as exc:  # noqa: BLE001
        logger.warning("signal scores for list failed: %s", exc)

    # 选了具体模型：只保留该模型有分数的股票（score_info 即该模型的当日分数）；
    # 模型在该信号日无分数则返回空集，不能静默回退成全市场
    if model:
        if score_info:
            df = df[df["Symbol"].str.split(".").str[0].isin(score_info.keys())]
        else:
            df = df.iloc[0:0]

    if score_info:
        df["_code"] = df["Symbol"].str.split(".").str[0]
        df["_fusion"] = df["_code"].map(lambda c: (score_info.get(c) or {}).get("fusion"))
        df["_side"] = df["_code"].map(lambda c: (score_info.get(c) or {}).get("side"))
        if score_min is not None:
            df = df[df["_fusion"].notna() & (df["_fusion"] >= score_min)]
        if score_max is not None:
            df = df[df["_fusion"].notna() & (df["_fusion"] <= score_max)]
        if only_signaled:
            df = df[df["_side"].isin(["BUY", "SELL"])]
        if side:
            df = df[df["_side"] == side]
        df = df.sort_values("_fusion", ascending=False, na_position="last")

    # 分数趋势：以当前基准信号日为 s2，往前比较最近 3 个信号日（每股 s0<-s1<-s2）。
    # 日历点选历史日时（date 参数），趋势随之按该日重算，整表联动。
    trend_map: dict[str, str] = {}
    if score_info:
        try:
            trend_map = await _trend_map(model, before=latest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trend map failed: %s", exc)
    if trend and trend_map:
        df = df[df["_code"].isin([sym for sym, t in trend_map.items() if t == trend])]
    elif trend:
        # 趋势筛选但该模型数据不足算趋势：返回空集（不能静默回退成全量）
        df = df.iloc[0:0]

    # 自选股列表（切「只看自选」时前端传全量自选）：过滤发生在排序后，自选股仍按分数降序展示；
    # 兼容 prefix(SH600519) / suffix(600519.SH) / 纯代码(600519) 三种写法
    if symbols:
        wanted = {s.strip().upper() for s in symbols.split(",") if s.strip()}
        if wanted:
            norm: set[str] = set()
            for s in wanted:
                if "." in s:
                    norm.add(s)
                elif s[:2] in ("SH", "SZ", "BJ"):
                    norm.add(f"{s[2:]}.{s[:2]}")
                else:
                    norm |= {f"{s}.{ex}" for ex in ("SH", "SZ", "BJ")}
            df = df[df["Symbol"].isin(norm)]

    total = len(df)

    # 定位选中股票在当前排序中的名次（列表自动跳转：切日期后名次可能掉到几千名）
    find_rank: int | None = None
    if find_symbol and total:
        code = find_symbol.split(".")[0]
        # df 过滤后 index 是非连续标签，必须用位置索引（0-based）而非 index 标签
        pos = (df["Symbol"].str.split(".").str[0] == code).to_numpy().nonzero()[0]
        if len(pos):
            find_rank = int(pos[0]) + 1  # 0-based 位置 -> 1-based 名次

    start = (page - 1) * page_size
    rows = df.iloc[start : start + page_size]

    # 筛选下拉的选项命中数（with_counts=true 时附带，供前端下拉后面显示数字）。
    # 统计口径：除该维度自身外，其余已选条件保持生效（base_df 已按其余条件过滤）。
    option_counts: dict[str, dict[str, int]] = {}
    if with_counts:
        try:
            for dim, col, opts in (
                ("board", "board", BOARD_OPTIONS),
                ("capTier", None, [c["value"] for c in CAP_TIER_OPTIONS]),
                ("trend", None, [t["value"] for t in TREND_OPTIONS]),
            ):
                counts: dict[str, int] = {}
                for optv in opts:
                    if dim == "board":
                        n = int((base_df[col] == optv).sum())
                    elif dim == "capTier":
                        mv = pd.to_numeric(base_df["Zsz"], errors="coerce")
                        n = int((_cap_mask(mv, optv)).sum())
                    else:
                        codes = base_df["Symbol"].str.split(".").str[0]
                        n = int((codes.map(trend_map) == optv).sum())
                    counts[optv] = n
                option_counts[dim] = counts
            if not model:
                # 推理模型命中数：各模型在最近信号日涉及的股票数
                model_counts: dict[str, int] = {}
                try:
                    async with get_session() as _s2:
                        from sqlalchemy import text as _txt

                        _d1 = (
                            await _s2.execute(
                                _txt("SELECT MAX(trade_date) FROM engine_signal_scores WHERE tenant_id='default'")
                            )
                        ).scalar_one_or_none()
                        if _d1 is not None:
                            _mr = (
                                await _s2.execute(
                                    _txt(
                                        "SELECT r.model_id, COUNT(*) c FROM engine_signal_scores e "
                                        "JOIN qm_model_inference_runs r ON r.run_id = e.run_id "
                                        "WHERE e.tenant_id='default' AND e.trade_date = :d "
                                        "GROUP BY r.model_id"
                                    ),
                                    {"d": _d1},
                                )
                            ).fetchall()
                            model_counts = {str(m): int(c) for m, c in _mr}
                except Exception as exc:  # noqa: BLE001
                    logger.warning("model counts failed: %s", exc)
                option_counts["model"] = model_counts
        except Exception as exc:  # noqa: BLE001
            logger.warning("option counts failed: %s", exc)

    # 列表表头筛选的取值集合（facets）：基于 base_df（其余已选条件已生效），始终附带
    facets: dict[str, list[str]] = {}
    try:
        _codes = base_df["Symbol"].str.split(".").str[0]
        facets["board"] = sorted(x for x in base_df["board"].dropna().unique().tolist() if x)
        facets["industry"] = sorted(x for x in base_df["rs_hyname"].dropna().unique().tolist() if x)
        facets["cap_tier"] = [
            t for t in ("微盘", "小盘", "中盘", "大盘", "超大盘")
            if int((_cap_mask(pd.to_numeric(base_df["Zsz"], errors="coerce"), t)).sum()) > 0
        ]
        facets["trend"] = sorted({t for t in trend_map.values() if t})
        # 信号方向固定三个（近 90 天可能只有 HOLD 有数据，但选项要完整）
        facets["side"] = ["BUY", "SELL", "HOLD"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("facets failed: %s", exc)

    # 推理模型选项（供筛选下拉）：最近 90 天内有信号的全部模型（真实 model_id + display_name）。
    # engine_signal_scores.model_version 恒为 'inference_script'（历史遗留无意义），
    # 真实模型标识在 qm_model_inference_runs.model_id（同 model_training history 逻辑）。
    # JOIN+GROUP BY 全表 2.3s/次，结果按日频更新——缓存 _UNIVERSE_TTL 消除重复开销
    model_options: list[dict[str, Any]] = []
    _now = time.time()
    if not model and _model_options_cache["v"] is not None and _now - _model_options_cache["ts"] < _UNIVERSE_TTL:
        model_options = _model_options_cache["v"]
    elif not model:
        try:
            async with get_session() as _s:
                from sqlalchemy import text as _txt

                _date_sql = (
                    "SELECT DISTINCT trade_date FROM engine_signal_scores "
                    "WHERE tenant_id='default' ORDER BY trade_date DESC LIMIT 1"
                )
                _latest_date = (await _s.execute(_txt(_date_sql))).scalar_one_or_none()
                if _latest_date is not None:
                    _from = _latest_date - _timedelta(days=90)
                    _mrows = (
                        await _s.execute(
                            _txt(
                                "SELECT r.model_id, MAX(e.trade_date) AS latest "
                                "FROM engine_signal_scores e "
                                "JOIN qm_model_inference_runs r ON r.run_id = e.run_id "
                                "LEFT JOIN qm_user_models u ON u.model_id = r.model_id "
                                "WHERE e.tenant_id='default' AND e.trade_date >= :d_from "
                                "AND (u.status IS NULL OR u.status <> 'archived') "
                                "GROUP BY r.model_id "
                                "ORDER BY MAX(u.is_default::int) DESC NULLS LAST, "
                                "MAX(e.trade_date) DESC LIMIT 200"
                            ),
                            {"d_from": _from},
                        )
                    ).fetchall()
                    _mids = [str(_r[0]) for _r in _mrows]
                    _meta_map: dict[str, str] = {}
                    if _mids:
                        _meta_rows = (
                            await _s.execute(
                                _txt(
                                    "SELECT model_id, metadata_json FROM qm_user_models "
                                    "WHERE model_id = ANY(:mids)"
                                ),
                                {"mids": _mids},
                            )
                        ).fetchall()
                        for _mid2, _meta_json in _meta_rows:
                            _meta = _meta_json if isinstance(_meta_json, dict) else {}
                            _meta_map[str(_mid2)] = (
                                _meta.get("display_name") or _meta.get("model_name") or ""
                            )
                    for _mid, _latest in _mrows:
                        model_options.append({
                            "model_id": str(_mid),
                            "display_name": _meta_map.get(str(_mid), ""),
                        })
                _model_options_cache.update({"v": model_options, "ts": time.time()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("model options for list failed: %s", exc)

    # ST 掩码全市场一次性预计算：逐行 _st_mask(r.to_frame().T) 约 40ms/行，
    # 100 行 items 要 4s+，且在 event loop 主线程执行——并发请求全部被拖慢
    st_mask_series = _st_mask(df)

    def _item(r: pd.Series) -> dict[str, Any]:
        info = score_info.get(str(r.get("Symbol")).split(".")[0]) if score_info else {}
        return {
            "symbol": r.get("Symbol"),
            "name": r.get("Name"),
            "board": r.get("board"),
            "industry": r.get("rs_hyname") or None,
            "close": _safe_f(r.get("close")),
            "pct_change": _safe_f(r.get("pct_change")),
            "total_mv": _safe_f(r.get("Zsz")),      # 亿元
            "float_mv": _safe_f(r.get("Ltsz")),     # 亿元
            "pe": _safe_f(r.get("DynaPE")),
            "pb": _safe_f(r.get("PB_MRQ")),
            "is_st": bool(st_mask_series.loc[r.name]) if r.name in st_mask_series.index else False,
            "fusion": (info.get("fusion") if info else None),
            "side": (info.get("side") if info else None),
            "signal_date": (info.get("date") if info else None),
            "model": (info.get("model") if info else None),
            "position_score": (info.get("position_score") if info else None),
            "industry_top10_avg": (info.get("industry_top10_avg") if info else None),
            "board_top10_avg": (info.get("board_top10_avg") if info else None),
            "cap_top10_avg": (info.get("cap_top10_avg") if info else None),
            "pct_industry": (info.get("pct_industry") if info else None),
            "market_empty": (info.get("market_empty") if info else None),
            "cap_tier": _cap_tier_of(r.get("Zsz")),
            "trend": (
                trend_map.get(str(r.get("Symbol")).split(".")[0], "-")
                if trend_map else "-"
            ),
        }

    return {
        "success": True,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "trade_date": trade_date,
            "signal_date": _signal_date,
            "find_rank": find_rank,
            "items": [_item(r) for _, r in rows.iterrows()],
            "models": model_options,
            "option_counts": option_counts,
            "facets": facets,
        },
    }


# ---------------------------------------------------------------------------
# 大盘 MA20 日历：左侧筛选栏日期选择（上证收盘 vs MA20 偏离度 + 当日推理概况）
# ---------------------------------------------------------------------------

# 日历聚合缓存：指数与推理分数均为日频，60s TTL 足够；推理完成后前端带 refresh 跳过
_CAL_CACHE: dict[str, Any] = {"key": "", "ts": 0.0, "payload": None}
_CAL_TTL = 60.0


def _build_calendar_days(df: pd.DataFrame, cal_start: str) -> list[dict[str, Any]]:
    """指数日线 -> 日历日列表（收盘 / MA20 / 偏离度%），跳过 cal_start 之前与 MA20 未成型的日子。"""
    if df is None or df.empty:
        return []
    df = df.sort_values("trade_date").reset_index(drop=True)
    closes = df["close"].astype(float)
    ma20 = closes.rolling(20).mean()
    days: list[dict[str, Any]] = []
    for i, (dt, close) in enumerate(zip(df["trade_date"], closes)):
        d = str(dt)[:10]
        if d < cal_start:
            continue
        m = ma20.iloc[i]
        if pd.isna(m):
            continue
        days.append(
            {
                "date": d,
                "close": round(float(close), 2),
                "ma20": round(float(m), 2),
                "dev_pct": round((float(close) - float(m)) / float(m) * 100, 2),
            }
        )
    return days


def _merge_signal_stats(days: list[dict[str, Any]], rows: list[tuple]) -> None:
    """把每日推理统计（(trade_date, 有分数行数, top10均分) 元组）就地合入日历日。"""
    # asyncpg 返回 date 对象 -> 统一 YYYY-MM-DD 字符串
    stats = {str(r[0])[:10]: (int(r[1]), float(r[2]) if r[2] is not None else None) for r in rows}
    for day in days:
        n_scored, top10 = stats.get(day["date"], (0, None))
        day["signal_count"] = n_scored
        day["top10_avg"] = round(top10, 4) if top10 is not None else None
        day["has_inference"] = n_scored > 0


@router.get("/market-calendar")
async def market_calendar(
    months: int = Query(12, ge=1, le=24, description="回看月数"),
    model: str | None = Query(None, description="按模型过滤推理概况（缺省=全模型）"),
    refresh: bool = Query(False, description="跳过缓存（推理完成后立即刷新）"),
    current_user: dict = Depends(get_current_user),
):
    """大盘 MA20 日历：近 N 月每个交易日的上证收盘/MA20/偏离度，叠加当日推理概况。

    前端日历格按偏离度着色（高于 MA20 红、越偏越深；低于 MA20 绿），
    点击有推理的日期切列表基准信号日；无推理日期可触发补推理。
    """
    _ = current_user
    import asyncio
    from datetime import date as _date, timedelta as _td

    from sqlalchemy import text as _txt

    cache_key = f"{months}:{model or ''}"
    now = time.time()
    if (
        not refresh
        and _CAL_CACHE["payload"] is not None
        and _CAL_CACHE["key"] == cache_key
        and now - _CAL_CACHE["ts"] < _CAL_TTL
    ):
        return {"success": True, "data": _CAL_CACHE["payload"]}

    end = _date.today()
    cal_start = (end - _td(days=months * 31)).isoformat()
    fetch_start = end - _td(days=months * 31 + 90)  # 多取 90 天作 MA20 预热

    def _run() -> list[dict[str, Any]]:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        df = QuantDBDataHub.get_instance().fetch_index_kline("000001.SH", fetch_start, end)
        return _build_calendar_days(df, cal_start)

    try:
        days = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("market calendar kline failed: %s", exc)
        days = []

    # 推理概况：每日有分数行数 + Top10 平均分（row_number 每日按分数降序取前10）
    if days:
        try:
            # model_version 列恒为 'inference_script'（历史遗留），真实模型在
            # qm_model_inference_runs.model_id，按 run_id 关联过滤（与 /list 同口径）
            model_where = (
                "AND run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE model_id = :m)"
                if model
                else ""
            )
            sql = (
                "SELECT trade_date, COUNT(fusion_score) AS n_scored, "
                "AVG(fusion_score) FILTER (WHERE rn <= 10) AS top10_avg FROM ("
                "SELECT trade_date, fusion_score, "
                "ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY fusion_score DESC NULLS LAST) AS rn "
                "FROM engine_signal_scores "
                f"WHERE tenant_id='default' AND trade_date >= :start {model_where}"
                ") t GROUP BY trade_date ORDER BY trade_date"
            )
            params: dict[str, Any] = {"start": _date.fromisoformat(cal_start)}
            if model:
                params["m"] = model
            async with get_session() as session:
                rows = (await session.execute(_txt(sql), params)).fetchall()
            _merge_signal_stats(days, rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("market calendar signals failed: %s", exc)
            for day in days:
                day["signal_count"] = 0
                day["top10_avg"] = None
                day["has_inference"] = False

    payload = {"index_symbol": "000001.SH", "index_name": "上证指数", "days": days}
    _CAL_CACHE.update(key=cache_key, ts=now, payload=payload)
    return {"success": True, "data": payload}


@router.get("/concepts")
async def list_concepts(
    market: str = Query("ALL", description="按市场过滤 SH/SZ/BJ/ALL"),
    current_user: dict = Depends(get_current_user),
):
    """概念/行业板块列表（sector_members 全量）。"""
    _ = current_user
    d = _quantdb_dir()
    f = d / "2_base_sector" / "sector_concept" / "sector_members.parquet"
    if not f.exists():
        return {"success": True, "data": {"concepts": []}}
    try:
        sm = await asyncio.to_thread(pd.read_parquet, f)
        name_col = "SectorName" if "SectorName" in sm.columns else "sector_name"
        sym_col = "Symbol" if "Symbol" in sm.columns else "symbol"
        type_col = "SectorType" if "SectorType" in sm.columns else None
        names = sorted(sm[name_col].dropna().astype(str).unique().tolist())
    except Exception as exc:  # noqa: BLE001
        logger.warning("concepts list failed: %s", exc)
        names = []
    return {"success": True, "data": {"concepts": names}}


_concept_members_cache: dict[str, Any] = {"ts": 0.0, "by_name": {}}


def _concept_members(concept: str) -> set[str]:
    """概念 -> 成分 symbol 集合（suffix 格式）。"""
    now = time.time()
    if not _concept_members_cache["by_name"] or now - _concept_members_cache["ts"] > _UNIVERSE_TTL:
        d = _quantdb_dir()
        f = d / "2_base_sector" / "sector_concept" / "sector_members.parquet"
        by_name: dict[str, set[str]] = {}
        if f.exists():
            try:
                sm = pd.read_parquet(f)
                name_col = "SectorName" if "SectorName" in sm.columns else "sector_name"
                sym_col = "Symbol" if "Symbol" in sm.columns else "symbol"
                for n, s in zip(sm[name_col], sm[sym_col]):
                    by_name.setdefault(str(n), set()).add(str(s))
            except Exception as exc:  # noqa: BLE001
                logger.warning("concept members failed: %s", exc)
        _concept_members_cache.update({"ts": now, "by_name": by_name})
    return _concept_members_cache["by_name"].get(concept, set())


@router.get("/industries")
async def list_industries(current_user: dict = Depends(get_current_user)):
    _ = current_user
    df, _ = _load_universe()
    names = sorted(x for x in df["rs_hyname"].dropna().astype(str).unique() if x.strip())
    return {"success": True, "data": {"industries": names}}


@router.get("/profile")
async def stock_profile(
    symbol: str = Query(..., description="600519.SH"),
    date: str | None = Query(None, description="历史快照日 YYYY-MM-DD（缺省=最新）"),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    sym = symbol.upper().strip()
    df, trade_date = await asyncio.to_thread(_load_universe, asof=date)
    hits = df[df["Symbol"] == sym]
    if hits.empty:
        raise HTTPException(status_code=404, detail=f"未找到 {sym}")
    r = hits.iloc[0]

    def _g(col: str) -> Any:
        v = r.get(col)
        if pd.isna(v):
            return None
        return v

    # 估值快照（pe_ttm/pb/ps/dividend_rate/float_mv 口径与列表的 DynaPE 互补）；
    # 指定 date 时读该日或之前最近分区，随日历联动。
    # 与宽基归属/概念/L2 并行读取（各自独立 parquet，避免串行累积耗时）
    def _read_valuation() -> tuple[dict[str, Any], float | None]:
        valuation: dict[str, Any] = {}
        dividend_yield: float | None = None
        d = _quantdb_dir()
        v_file = _partition_on(d / "5_technical_derived" / "valuation", date) if date else _latest_partition(d / "5_technical_derived" / "valuation")
        if v_file is not None:
            try:
                vdf = pd.read_parquet(v_file)
                sym_col = "symbol" if "symbol" in vdf.columns else "Symbol"
                vrow = vdf[vdf[sym_col] == sym]
                if not vrow.empty:
                    vr = vrow.iloc[0]
                    for col in ("pe_ttm", "pe_static", "pb", "ps_ttm",
                                "dividend_rate", "total_mv", "float_mv", "net_profit_ttm",
                                "revenue_ttm", "equity"):
                        valuation[col] = _safe_f(vr.get(col))
                    dividend_yield = _norm_dividend(vr.get("dividend_rate"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("read valuation for %s failed: %s", sym, exc)
        return valuation, dividend_yield

    # L2 微观结构因子：预测日（信号日）前一个交易日的 14 个推荐因子
    # date 参数=日历点选的预测日；缺省取该股最近的推理信号日。
    # signal_date 查询与其余 parquet 读取并行，避免串行等待 PG。
    async def _resolve_l2() -> tuple[dict[str, Any] | None, str | None]:
        sig = date or await _latest_signal_date_for(sym)
        if not sig:
            return None, None
        feat_date = _l2_feature_date(sig)
        if feat_date:
            return await asyncio.to_thread(_l2_features_for, sym, feat_date), sig
        return None, sig

    (valuation, dividend_yield), _idx, _concepts, _l2_res = await asyncio.gather(
        asyncio.to_thread(_read_valuation),
        asyncio.to_thread(_index_membership, sym),
        asyncio.to_thread(_concepts_of, sym),
        _resolve_l2(),
    )

    l2_features, signal_date = _l2_res if _l2_res is not None else (None, None)

    profile = {
        "symbol": sym,
        "name": _g("Name"),
        "board": _g("board"),
        "industry": _g("rs_hyname"),
        "trade_date": trade_date,
        "close": _safe_f(r.get("close")),
        "pct_change": _safe_f(r.get("pct_change")),
        "total_mv": _safe_f(r.get("Zsz")),       # 亿元
        "float_mv": _safe_f(r.get("Ltsz")),      # 亿元
        "total_share": _safe_f(r.get("J_zgb")),  # 万股
        "free_float_share": _safe_f(r.get("FreeLtgb")),
        "pe_dynamic": _safe_f(r.get("DynaPE")),
        "pb": _safe_f(r.get("PB_MRQ")),
        "dividend_yield": dividend_yield,
        "beta": _safe_f(r.get("BetaValue")),
        "staff_num": _safe_f(r.get("StaffNum")),
        "main_business": _g("MainBusiness"),
        "ipo_price": _safe_f(r.get("IPO_Price")),
        "limit_up_price": _safe_f(r.get("ZTPrice")),
        "limit_down_price": _safe_f(r.get("DTPrice")),
        "flags": {
            "hs300": _flag(r.get("BelongHS300")),
            "marginable": _flag(r.get("RZRQ")),
            "sh_hk_connect": _flag(r.get("HSGT")),
            "is_st": _flag(r.get("IsSTGP" if "IsSTGP" in r.index else "STGP")),
            "is_hk_listed": _flag(r.get("IsHKGP")),
        },
        "valuation": valuation,
        "index_membership": _idx,
        "concepts": _concepts,
        "l2_features": l2_features,
        "signal_date": signal_date,
    }
    return {"success": True, "data": profile}
