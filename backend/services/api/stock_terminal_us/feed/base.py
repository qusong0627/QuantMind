"""美股个股终端 —— 数据层基座（薄封装，不复制 market_analysis_us 的实现）。

复用 `market_analysis_us.feed.base` 的全部基础设施（数据目录解析 / 显式分区读取 /
名称·行业映射 / TTL 缓存），本文件只补三样终端专属能力：

1. **单标的取数**：显式分区文件列表 + 列裁剪 + `symbol` 谓词下推，比全市场读
   再 pandas 过滤快 2-3 倍（实测 500 日 0.20s vs 0.57s），且不物化 24 万行无关标的。
2. **终端口径常量**：未复权原始价 / 美元成交额 / 年报财务 / 13F 滞后 —— 随响应体
   一起下发，前端无需硬编码口径说明。
3. **标的判定与每股小表**：`_symbol_exists`（标的池 ∪ 最新日线分区）、
   `_symbol_table`（4_analyst / 3_financial_data 下每股一文件的小表，TTL 缓存）。

口径（实测复核，2026-09-12）：
- 价格 = yfinance `auto_adjust=False` 的**原始未复权价**。AAPL 2020-08-31 拆股日
  499.23 → 129.04 是文件真实内容，不是脏数据；终端**只做拆股标记，不自算复权**。
  库内**没有现成复权因子**（`l1_factors.adj_factor` 恒 1.0），不要指望。
- `amount` = **美元原始成交额**（≈ close×volume 量级），无换算系数（与 A 股「股/万元」口径不同）。
- 最新日线分区 2026-09-10（2001-01-02 起）；最新分区有行情的标的约 484 只
  （security_master 池 516 只，其余 32 只为退市/并购残留 ATVI/ANSS/SBNY… 或
  代码口径不一致 —— BRK.B/BF.B 在 yahoo 侧是 BRK-B/BF-B，按点号查无行情）。
- 财务三表**仅年报**（各 488 文件、每股 5 个财年），列全部是 yfinance 英文长名
  （income 42 列 / balance 72 列 / cashflow 56 列），无季报。
- `f10` 共 516 个文件：484 只在交易标的全覆盖，约 424 只有 PE、464 只有 52w_high。
  **没有 PS / EV / forward PE 落盘**；`5_technical_derived/valuation` 分区自
  2026-08-28 起全 null（历史坏分区未回填）—— 估值一律以 f10 为唯一主路径。
- `splits` 357 文件 / 1509 行（最新 2026-09-03）；`dividend` 404 文件（最新 2026-09-09）。
- 内部人最近披露 2026-09-08；`mutual_fund_holders` 的 Date Reported 滞后一季（13F）。
- `en_name` 在 security_master 里**全为空串**（516/516 实测），保留字段但不要指望有值。
- QuantUS **没有 index_weights / 指数成分**，所以没有「宽基归属」可返回。

⚠️ 终端取数一律走**单标的**路径。不要为一只股票调用 `_load_analyst_table`
（那是全市场截面分析的口径，`upgrades_downgrades` 单表约 18 万行）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from collections.abc import Sequence

import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.display import safe_float
from backend.services.api.market_analysis_us.feed.base import (
    KLINE_REL,
    PCT_CLIP,
    _dedupe_bars,
    _f10_snapshot,
    _market_pct_snapshot,
    _name_map,
    _partition_file,
    _q,
    _sector_cn,
    _sector_map,
    _trading_days,
    _universes,
    clear_cache_us,
    to_iso,
)
from backend.services.api.market_analysis_us.feed.base import DATA_DIR  # noqa: F401
from backend.services.api.market_analysis_us.feed.base import SECURITY_MASTER_REL  # noqa: F401

logger = logging.getLogger(__name__)

# ---- 终端口径常量 ----

# 复权方式：库内就是未复权原始价，终端不提供 qfq/hfq（与市场分析模块立场一致）
ADJUST = "none"

TERMINAL_NOTES: dict[str, str] = {
    "universe": (
        "标的池为标普500 + 纳指补充（security_master 共 516 只）；列表只含最新交易日"
        "有行情的约 484 只，其余 32 只（退市/并购残留 ATVI/ANSS 等，或 BRK.B/BF.B 这类"
        "yahoo 侧代码口径不一致的）仍可按代码直查，但报价为空"
    ),
    "price_adjust": "日线为原始未复权价，拆股日价格会真实跳变（以 splits 事件标记提示）",
    "amount_unit": "amount 为美元原始成交额（≈ close×volume），非 A 股「股/万元」口径",
    "financials": "财务三表仅年报（yfinance 年度口径，每股 5 个财年），无季报；单位美元",
    "valuation": (
        "估值以 f10 快照为唯一主路径（5_technical_derived/valuation 分区自 2026-08-28 起全 null）；"
        "快照仅含 PE/PB/股息率/52 周区间，库内没有 PS/EV/forward PE"
    ),
    "institutional": "机构持仓为 13F 口径，披露滞后约一个季度",
    "news": (
        "资讯优先取 news_article_enrichment（tickers 精确匹配 + 标题中文名，带 FinBERT 情绪与"
        "事件标签），Huntly 标题 LIKE 仅作兜底；长度 ≤1 的代码不参与匹配"
    ),
}

# 单次 K 线请求的硬上限（约 12 年交易日）：显式分区列表是线性成本，
# 3000 个分区约 1.5s；图表用不到更长的窗口，超出时保留最近区间并置 truncated
MAX_RANGE_DAYS = 3000

FIN_DIR = "3_financial_data"
DIVIDEND_DIR = f"{FIN_DIR}/dividend"
SPLITS_DIR = f"{FIN_DIR}/splits"

# 代码白名单：security_master 实际取值 = 纯字母（A / AAPL）+ BRK.B 式双段
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z]{1,2})?$")
_CJK_RE = re.compile(r"[一-鿿]")

_BAR_COLUMNS = "symbol, dt, open, high, low, close, volume, amount, published_at"


# ---- 代码与路径 ----


def normalize_symbol(symbol: Any) -> str:
    """统一代码口径（strip + upper）；含白名单外的字符返回空串（调用方转 400）。"""
    sym = str(symbol or "").strip().upper()
    return sym if _SYMBOL_RE.match(sym) else ""


def _symbol_file(rel_dir: str, symbol: str) -> Path | None:
    """每股一文件的绝对路径（不存在返回 None）。"""
    path = DATA_DIR / rel_dir / f"{symbol}.parquet"
    return path if path.exists() else None


def _symbol_table(rel_dir: str, symbol: str) -> pd.DataFrame:
    """读取每股一文件的小表（分红 / 拆股 / 财务 / 分析师）。

    TTL 缓存：这些文件是快照或低频事件表，按请求重读纯属浪费。
    读取失败返回空表（面板按「无数据」降级，不冒泡成 500）。
    """

    def _load() -> pd.DataFrame:
        path = _symbol_file(rel_dir, symbol)
        if path is None:
            return pd.DataFrame()
        try:
            return pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - 单文件损坏不应打挂面板
            logger.warning(
                "[stock-terminal-us] 读取 %s/%s 失败: %s", rel_dir, symbol, exc
            )
            return pd.DataFrame()

    return cached(f"us_term:tbl:{rel_dir}:{symbol}", _load, ttl=1800.0)


# ---- 日线（单标的） ----


def _read_symbol_bars(symbol: str, ymd_days: Sequence[str]) -> pd.DataFrame:
    """单标的日线：显式分区文件列表 + 列裁剪 + symbol 谓词下推（升序返回）。

    走文件列表而不是 `**/*.parquet` + WHERE：文件内 dt 列会遮蔽 hive 分区列，
    DuckDB 无法裁剪，且耗时会随库内总分区数增长（详见 market_analysis_us/README）。

    `symbol` 必须已过 `_SYMBOL_RE` 白名单后插值 —— 无引号/括号等注入面。
    """
    files = [f for f in (_partition_file(KLINE_REL, d) for d in ymd_days) if f]
    if not files:
        return pd.DataFrame()
    literal = ",".join(repr(f) for f in files)
    df = _q(
        f"SELECT {_BAR_COLUMNS} FROM read_parquet([{literal}]) "
        f"WHERE symbol = {symbol!r}"
    )
    if df.empty:
        return df
    df = _dedupe_bars(df, ["symbol", "dt"])
    df = df.drop(columns=["published_at"], errors="ignore")
    return df.sort_values("dt").reset_index(drop=True)


def latest_bars(symbol: str, n: int = 2) -> pd.DataFrame:
    """最近 n 个交易日的原始价日线（升序）；n>=2 时可用于算前收与涨跌幅。"""
    return cached(
        f"us_term:bars:{symbol}:{n}",
        lambda: _read_symbol_bars(symbol, _trading_days(None, n)),
        ttl=300.0,
    )


def change_pct(close: float, prev_close: float) -> float:
    """单日涨跌幅（%）；±100% 之外视为拆股/公司行动造成的跳变并裁剪。

    与 `_market_pct_snapshot` 同口径（PCT_CLIP），保证列表与详情页数值一致。
    """
    cur, prev = float(close or 0), float(prev_close or 0)
    if prev <= 0:
        return 0.0
    raw = (cur / prev - 1) * 100
    return round(max(-PCT_CLIP, min(PCT_CLIP, raw)), 2)


def cap_display(market_cap: Any) -> str | None:
    """市值展示串（美元口径）：$4.80万亿 / $4.78亿 / $3.2万。

    档位与前端 `fmtUsd` 一致 —— 市值不像 A 股那样固定「亿元」单位，
    展示串由后端给死，避免前端各自换算（列表 / 概要 / 详情三处都得一致）。
    """
    v = safe_float(market_cap)
    if v <= 0:
        return None
    if v >= 1e12:
        return f"${v / 1e12:,.2f}万亿"
    if v >= 1e8:
        return f"${v / 1e8:,.2f}亿"
    if v >= 1e4:
        return f"${v / 1e4:,.2f}万"
    return f"${v:,.0f}"


# ---- 截面 / 标的池 ----


def _latest_snapshot() -> tuple[str | None, pd.DataFrame]:
    """最新交易日全市场截面 (trade_date, DataFrame[symbol, close, amount, volume, pct_change])。

    TTL 缓存：`_market_pct_snapshot` 每次都要重读 2 个分区 + 透视，列表页高频调用。
    """
    return cached("us_term:snapshot", _market_pct_snapshot, ttl=300.0)


def _symbol_meta() -> pd.DataFrame:
    """终端标的池：symbol / cn_name / en_name / sector / sector_cn / industry（缓存 30 分钟）。

    与共享 `_universes()` 的差异：security_master 里 SBNY 有两行（空名行在前、
    退市静态中文名行在后），`_universes()` 保留首行 → 中文名为空；这里用
    `_name_map()`（同代码取末次）回填，标的池中文名覆盖 100%。
    """

    def _load() -> pd.DataFrame:
        uni = _universes().copy()
        uni["cn_name"] = uni["cn_name"].fillna("").astype(str).str.strip()
        uni["en_name"] = uni["en_name"].fillna("").astype(str).str.strip()
        names = _name_map()
        blank = uni["cn_name"] == ""
        if blank.any():
            uni.loc[blank, "cn_name"] = (
                uni.loc[blank, "symbol"].map(names).fillna("").astype(str)
            )
        # 同代码多行时 _universes() 只留首行；若 _name_map（取末次）拿到的是中文名
        # 而保留行是拉丁名，以中文名为准（实测仅 SBNY：Signature Bank -> 签名银行）
        better = uni["symbol"].map(names).fillna("").astype(str)
        prefer = better.str.contains(_CJK_RE) & ~uni["cn_name"].str.contains(_CJK_RE)
        if prefer.any():
            uni.loc[prefer, "cn_name"] = better[prefer]
        sec = _sector_map()
        df = uni.merge(sec, on="symbol", how="left")
        df["sector"] = df["sector"].fillna("Unknown")
        df["sector_cn"] = df["sector"].map(_sector_cn)
        df["industry"] = df["industry"].fillna("")
        return df.sort_values("symbol").reset_index(drop=True)

    return cached("us_term:meta", _load, ttl=1800.0)


def _symbol_exists(symbol: str) -> bool:
    """标的池 ∪ 最新日线分区：池外但当日有成交的代码也允许取数。"""
    if not symbol:
        return False
    if (_symbol_meta()["symbol"] == symbol).any():
        return True
    _, snap = _latest_snapshot()
    return not snap.empty and bool((snap["symbol"] == symbol).any())


def _name_of(symbol: str) -> str:
    """中文展示名（缺失回退代码本身，不产生空单元格）。"""
    return _name_map().get(symbol, symbol)


def clear_cache_terminal() -> None:
    """清空终端缓存（共享缓存 + 名称映射一并清，数据同步后调用）。"""
    clear_cache_us()


__all__ = [
    "ADJUST",
    "TERMINAL_NOTES",
    "MAX_RANGE_DAYS",
    "DIVIDEND_DIR",
    "SPLITS_DIR",
    "DATA_DIR",
    "SECURITY_MASTER_REL",
    "normalize_symbol",
    "change_pct",
    "cap_display",
    "latest_bars",
    "_symbol_file",
    "_symbol_table",
    "_read_symbol_bars",
    "_latest_snapshot",
    "_symbol_meta",
    "_symbol_exists",
    "_name_of",
    "_f10_snapshot",
    "_trading_days",
    "to_iso",
    "clear_cache_terminal",
]
