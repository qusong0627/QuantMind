"""新闻情绪优化回测：融合深度分析发现的所有规律。

基于 backtest_news_deep_analysis.py 的发现:
  1. 来源过滤: 只用预测力 Top 5 来源，排除反向指标
  2. 时段过滤: 只用 9:00-11:30, 15:00-17:00, 19:00-22:00 的消息
  3. 多篇确认: 同股票同日 >= 2 篇同向文章才产生信号
  4. 首日动量: 利好+首日涨才入场，利空+首日跌才入场
  5. 情绪反转出场: 利好→利空或利空→利好立即平仓
  6. 连续信号加成: 连续 3 天同向信号 → 仓位翻倍
  7. 事件标签加成: 高预测力标签 → 仓位 1.5x
"""
import sys
import os
import math
import json
import bisect
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "main_oss.py").is_file():
            return p
    raise FileNotFoundError("未找到仓库根（含 backend/main_oss.py）")


sys.path.insert(0, str(_find_repo_root(Path(__file__).resolve())))

# ── 配置 ──────────────────────────────────────────────────
def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v is not None else default

def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

INIT_CASH = 500_000.0
MAX_HOLD_DAYS = _env_int("QM_MAX_HOLD_DAYS", 20)
STOP_LOSS = 0.99                  # 禁用止损（测试结论: 止损是最大亏损源）
TRAILING_ACTIVATE = 0.15          # 动态止盈激活阈值
TRAILING_DROP = 0.05              # 从最高点回撤/反弹比例
SIGNAL_THRESHOLD = 0.30          # 降低阈值（深度分析用了 0.25）
MAX_POSITION_PCT = 0.10
MAX_CONCURRENT = 10
MA_WINDOW = 20
ENABLE_MA5_EXIT = _env_bool("QM_MA5_EXIT", False)          # P0 证伪: 新闻行情先洗盘再拉升，MA5 提前踢仓巨亏
MA5_EXIT_MIN_HOLD = _env_int("QM_MA5_MIN_HOLD", 10)        # MA5 离场的最小持仓天数
HOLD_WINNERS = _env_bool("QM_HOLD_WINNERS", True)          # P0 结论: 到期浮盈续持（胜者奔跑，败者到期照离场）
MAX_EXTEND_DAYS = _env_int("QM_MAX_EXTEND_DAYS", 40)       # 续持上限: 40天（单调性实验甜点 +105.35%/Calmar 12.76）
RESULT_FILE = os.getenv("QM_RESULT_FILE", "")              # 结果 JSON 输出路径（默认按配置自动命名）
COMMISSION = 0.0003
STAMP_TAX = 0.001
SLIPPAGE = 0.002

# ── 优化开关（可单独控制每个优化项） ──────────────────────
ENABLE_SOURCE_FILTER = True      # 来源过滤
ENABLE_TIME_FILTER = True        # 时段过滤
ENABLE_MULTI_CONFIRM = True      # 多篇确认（False=单篇也可）
ENABLE_MOMENTUM_FILTER = True    # 首日动量过滤
ENABLE_REVERSAL_EXIT = True      # 情绪反转出场
ENABLE_CONSECUTIVE_BOOST = True  # 连续信号加成
ENABLE_TAG_BOOST = True          # 事件标签加成
MIN_ARTICLES_FOR_SIGNAL = 2      # 多篇确认阈值（1=不确认）

# ── 优化参数 ──────────────────────────────────────────────
# 来源白名单（预测力 Top 5，排除反向指标）
SOURCE_WHITELIST = {
    "财联社电报", "同花顺实时新闻", "瓦斯阅读", "界面rss订阅",
    "7*24小时全球财经直播_同花顺财经", "Google Alert - 汇率",
    "东方财富网-行业研报",
}
# 反向指标黑名单（预测力为负的来源）
SOURCE_BLACKLIST = {
    "南华早报", "创业邦", "彭博社最新报道", "华尔街见闻",
    "中国话题", "商业 - 最新新闻 - Google 新闻", "雅虎财经 Lite",
    "链捕手", "路透社最新报道", "人民日报",
}

# 最佳时段 (小时)
BEST_HOURS = {9, 10, 11, 15, 16, 17, 19, 20, 21, 22}

# 高预测力事件标签 → 仓位加成
BOOST_TAGS = {
    "警示函": 1.5, "可转债": 1.5, "业绩预告": 1.5,
    "战略合作": 1.5, "净利润增长": 1.5, "扭亏为盈": 1.3,
    "监管": 1.3, "立案调查": 1.3, "大涨": 1.3,
    "涨停": 1.3, "加密": 1.3,
}

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TRADING_AM_START = time(9, 30)
TRADING_AM_END = time(11, 30)
TRADING_PM_START = time(13, 0)
TRADING_PM_END = time(15, 0)

HUNTLY_DB = "/data/huntly/db.sqlite"
QUANTDB_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"))

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.services.simulation.services.local_market_data import compute_limits
from sqlalchemy import text


# ── 工具函数 ──────────────────────────────────────────────

def _is_trading_time(dt: datetime) -> bool:
    t = dt.time()
    return (TRADING_AM_START <= t <= TRADING_AM_END) or (TRADING_PM_START <= t <= TRADING_PM_END)


def _board_of(code: str) -> str:
    if code.startswith("688"): return "科创板"
    if code.startswith("30"): return "创业板"
    if code.startswith(("00", "002", "003")): return "深主板"
    if code.startswith("60"): return "沪主板"
    if code.startswith(("83", "43", "87", "88", "92")): return "北交所"
    return "其他"


def _parse_huntly_time(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s2 = s.strip()
        if "." in s2:
            return datetime.strptime(s2, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=SHANGHAI_TZ)
        return datetime.strptime(s2, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI_TZ)
    except ValueError:
        return None


# ── 数据加载 ──────────────────────────────────────────────

def load_trading_days() -> list[str]:
    import pyarrow.parquet as pq
    cal_path = QUANTDB_DIR / "2_base_sector" / "trading_calendar" / "trading_days.parquet"
    df = pq.read_table(str(cal_path)).to_pandas()
    days = sorted(df["TradingDate"].astype(str).tolist())
    return [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in days]


def load_enriched_articles() -> list[dict]:
    """加载所有富集文章（含来源、事件标签、行业），返回文章列表."""
    import asyncio

    async def _load():
        async with get_session(read_only=True) as s:
            res = await s.execute(text("""
                SELECT huntly_page_id, tickers, sentiment_label, sentiment_score,
                       event_tags, industries
                FROM news_article_enrichment
                WHERE cardinality(tickers) > 0
                  AND sentiment_label IN ('bullish', 'bearish')
                  AND ABS(sentiment_score) >= :threshold
            """), {"threshold": SIGNAL_THRESHOLD})
            return res.fetchall()

    enrich_rows = asyncio.run(_load())

    # 加载 Huntly 时间 + 来源
    conn = sqlite3.connect(f"file:{HUNTLY_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    pids = [int(r[0]) for r in enrich_rows]
    page_data = {}
    batch_size = 5000
    for i in range(0, len(pids), batch_size):
        batch = pids[i:i + batch_size]
        ph = ",".join("?" for _ in batch)
        cur.execute(f"""
            SELECT p.id, p.connected_at, p.title, c.name as source_name
            FROM page p
            LEFT JOIN connector c ON c.id = p.connector_id
            WHERE p.id IN ({ph})
        """, batch)
        for r in cur.fetchall():
            page_data[int(r["id"])] = {
                "connected_at": r["connected_at"],
                "title": r["title"],
                "source": r["source_name"] or "unknown",
            }
    conn.close()

    articles = []
    for row in enrich_rows:
        pid, tickers, label, score, event_tags, industries = row
        pinfo = page_data.get(int(pid))
        if not pinfo:
            continue
        dt = _parse_huntly_time(pinfo["connected_at"])
        if dt is None:
            continue
        source = pinfo["source"]
        # 来源过滤
        if ENABLE_SOURCE_FILTER:
            if SOURCE_BLACKLIST and source in SOURCE_BLACKLIST:
                continue
            if SOURCE_WHITELIST and source not in SOURCE_WHITELIST:
                continue
        # 时段过滤
        if ENABLE_TIME_FILTER and dt.hour not in BEST_HOURS:
            continue
        for ticker in (tickers or []):
            ticker = ticker.strip()
            if not ticker:
                continue
            articles.append({
                "ticker": ticker,
                "label": label,
                "score": float(score),
                "datetime": dt,
                "source": source,
                "event_tags": list(event_tags or []),
                "industries": list(industries or []),
            })
    return articles


def load_klines(symbols: set[str]) -> dict[str, pd.DataFrame]:
    import pyarrow.parquet as pq
    daily_dir = QUANTDB_DIR / "1_kline_data" / "daily_forward"
    suffix_list = sorted(s for s in symbols)
    if not suffix_list:
        return {}
    partitions = []
    for p in sorted(daily_dir.glob("dt=*")):
        dt_str = p.name[3:]
        if "2026" <= dt_str[:4] <= "2026":
            partitions.append(p / "data.parquet")
    filters = [("symbol", "in", suffix_list)]
    all_dfs = []
    for f in partitions:
        try:
            t = pq.read_table(f, columns=["symbol", "time", "open", "high", "low", "close"], filters=filters)
            if t.num_rows:
                all_dfs.append(t.to_pandas())
        except Exception:
            continue
    if not all_dfs:
        return {}
    full = pd.concat(all_dfs, ignore_index=True)
    full = full.rename(columns={"time": "trade_date"})
    full["trade_date"] = full["trade_date"].astype(str).str[:10]
    klines = {}
    for suffix, grp in full.groupby("symbol"):
        klines[suffix] = grp.sort_values("trade_date")[["trade_date", "open", "high", "low", "close"]].reset_index(drop=True)
    return klines


def load_st_symbols() -> set[str]:
    import psycopg2
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )
    out = set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM stocks WHERE name LIKE '%ST%' OR name LIKE '%*ST%'")
            for (sym,) in cur.fetchall():
                try:
                    out.add(StockCodeUtil.to_suffix(str(sym).strip().upper()))
                except Exception:
                    continue
    finally:
        conn.close()
    return out


def load_stock_names() -> dict[str, str]:
    import psycopg2
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, name FROM stocks")
            return {StockCodeUtil.to_suffix(str(x).strip().upper()): str(y) for x, y in cur.fetchall()}
    finally:
        conn.close()


def load_index_ma() -> dict[str, bool]:
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    hub = QuantDBDataHub()
    df = hub.fetch_index_kline("000001.SH", date(2026, 2, 1), date(2026, 8, 31))
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["ma20"] = df["close"].rolling(MA_WINDOW).mean()
    out = {}
    for _, row in df.iterrows():
        d = str(row["trade_date"])[:10]
        c = float(row["close"])
        m = row["ma20"]
        if pd.notna(m) and m > 0:
            out[d] = bool(c >= m)
    return out


# ── 信号构建（含优化逻辑） ──────────────────────────────

def resolve_trade_date(news_dt: datetime, trading_day_set: set[str], all_dates_sorted: list[str]) -> str | None:
    cal_date = news_dt.strftime("%Y-%m-%d")
    if _is_trading_time(news_dt) and cal_date in trading_day_set:
        return cal_date
    idx = bisect.bisect_left(all_dates_sorted, cal_date)
    if idx < len(all_dates_sorted):
        return all_dates_sorted[idx]
    return None


def build_optimized_schedule(
    articles: list[dict],
    trading_days: list[str],
    price: dict[tuple[str, str], dict[str, float]],
    trading_day_set: set[str],
) -> dict[str, dict[str, dict]]:
    """构建优化后的每日信号表。

    返回: {date: {stock: {label, score, boost, day0_ret}}}
    - boost: 仓位加成系数（1.0 基准，含标签加成和连续信号加成）
    - day0_ret: 消息当天的收益率（用于动量过滤）
    """
    all_dates_sorted = sorted(trading_day_set)

    # Step 1: 按 (date, stock) 聚合文章
    daily_raw: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for art in articles:
        trade_date = resolve_trade_date(art["datetime"], trading_day_set, all_dates_sorted)
        if trade_date is None:
            continue
        daily_raw[trade_date][art["ticker"]].append(art)

    # Step 2: 多篇确认 + 计算信号
    schedule: dict[str, dict[str, dict]] = {}
    for d, stocks in daily_raw.items():
        schedule[d] = {}
        for ticker, arts in stocks.items():
            # 多篇确认
            if ENABLE_MULTI_CONFIRM and len(arts) < MIN_ARTICLES_FOR_SIGNAL:
                continue

            # 取主导情绪和最高分值
            labels = [a["label"] for a in arts]
            dominant_label = max(set(labels), key=labels.count)
            best_art = max(arts, key=lambda a: abs(a["score"]))

            # 计算 day0 收益率（首日动量）
            day0_ret = None
            px_today = price.get((ticker, d))
            if px_today:
                # 找前一交易日收盘价
                d_idx = all_dates_sorted.index(d) if d in all_dates_sorted else -1
                if d_idx > 0:
                    prev_d = all_dates_sorted[d_idx - 1]
                    prev_px = price.get((ticker, prev_d))
                    if prev_px and prev_px["close"] > 0:
                        day0_ret = (px_today["close"] - prev_px["close"]) / prev_px["close"]

            # 动量过滤: 利好必须首日涨，利空必须首日跌
            if ENABLE_MOMENTUM_FILTER and day0_ret is not None:
                if dominant_label == "bullish" and day0_ret <= 0:
                    continue
                if dominant_label == "bearish" and day0_ret >= 0:
                    continue

            # 计算仓位加成
            boost = 1.0
            # 事件标签加成
            if ENABLE_TAG_BOOST:
                all_tags = set()
                for a in arts:
                    all_tags.update(a["event_tags"])
                tag_boosts = [BOOST_TAGS.get(t, 1.0) for t in all_tags]
                if tag_boosts:
                    boost *= max(tag_boosts)  # 取最高加成
                # 多篇加成
                if len(arts) >= 5:
                    boost *= 1.3
                elif len(arts) >= 3:
                    boost *= 1.15
            else:
                all_tags = []

            schedule[d][ticker] = {
                "label": dominant_label,
                "score": best_art["score"],
                "boost": min(boost, 2.0),  # 上限 2x
                "day0_ret": day0_ret,
                "n_articles": len(arts),
                "tags": list(all_tags),
            }

    # Step 3: 连续信号加成
    # 对同一股票，如果在连续 3 个交易日内都出现同向信号，给第 3 天加成
    if ENABLE_CONSECUTIVE_BOOST:
        stock_signal_dates: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for d in sorted(schedule.keys()):
            for ticker, info in schedule[d].items():
                stock_signal_dates[ticker].append((d, info["label"]))

        for ticker, date_labels in stock_signal_dates.items():
            date_labels.sort(key=lambda x: x[0])
            consecutive = 1
            for i in range(1, len(date_labels)):
                prev_d, prev_label = date_labels[i - 1]
                curr_d, curr_label = date_labels[i]
                if curr_label != prev_label:
                    consecutive = 1
                    continue
                # 检查是否在 3 个自然日内
                days_diff = (date.fromisoformat(curr_d) - date.fromisoformat(prev_d)).days
                if days_diff <= 3:
                    consecutive += 1
                else:
                    consecutive = 1
                if consecutive >= 3 and curr_d in schedule and ticker in schedule[curr_d]:
                    schedule[curr_d][ticker]["boost"] *= 1.5
                    schedule[curr_d][ticker]["boost"] = min(schedule[curr_d][ticker]["boost"], 2.5)
                    schedule[curr_d][ticker]["consecutive"] = consecutive

    return schedule


# ── 回测引擎 ──────────────────────────────────────────────

def run_backtest(
    schedule: dict[str, dict[str, dict]],
    klines: dict[str, pd.DataFrame],
    trading_days: list[str],
    st_symbols: set[str],
    index_ma: dict[str, bool],
) -> dict:
    price: dict[tuple[str, str], dict[str, float]] = {}
    close_map: dict[str, dict[str, float]] = {}
    for suffix, df in klines.items():
        cm = {}
        for _, row in df.iterrows():
            d = str(row["trade_date"])[:10]
            price[(suffix, d)] = {
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
            }
            cm[d] = float(row["close"])
        close_map[suffix] = cm

    def _limits(suffix: str, day: str) -> tuple[float, float] | None:
        m = close_map.get(suffix)
        if not m:
            return None
        day_dates = sorted(d for d in m if d < day)
        if not day_dates:
            return None
        pre = m[day_dates[-1]]
        code = suffix.split(".")[0]
        try:
            # is_st 与选股口径（`st_symbols`）保持一致 —— 见 backtest_l2_year.py
            # 同处的说明：写死 False 会把 ST 5% 板的封板日判成可成交。
            lu, ld = compute_limits(
                code,
                pre,
                is_st=suffix in st_symbols,
                trade_date=date.fromisoformat(day),
            )
        except Exception:
            return None
        return float(lu), float(ld)

    def _bar_state(suffix: str, day: str) -> tuple:
        lim = _limits(suffix, day)
        px = price.get((suffix, day))
        if lim is None or px is None:
            return None, None, True, True
        lu, ld = lim
        o, h, l = px["open"], px["high"], px["low"]
        cannot_buy = o >= lu - 0.001 and h <= lu + 0.001
        cannot_sell = o <= ld + 0.001 and l >= ld - 0.001
        return lu, ld, cannot_buy, cannot_sell

    def _calc_equity(cash, holdings, day):
        val = cash
        for s, h in holdings.items():
            px = price.get((s, day), {})
            close = px.get("close") if px else None
            if close is None:
                close = h["cost"]
            if h["side"] == "LONG":
                val += close * h["shares"]
            else:
                val += (h["cost"] - close) * h["shares"]
        return val

    date_set = set(trading_days)
    next_day = {}
    for i in range(len(trading_days) - 1):
        next_day[trading_days[i]] = trading_days[i + 1]

    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    daily_records: dict[str, dict] = {}
    cash = INIT_CASH

    signal_dates = sorted(set(schedule.keys()) & date_set)
    if not signal_dates:
        return {"daily": {}, "trades": [], "trading_days": trading_days}
    all_trading_days = [d for d in trading_days if d >= signal_dates[0]]

    # 跟踪每只股票的上一个情绪，用于反转出场
    stock_last_sentiment: dict[str, str] = {}

    for d in all_trading_days:
        day_signals = schedule.get(d, {})
        tgt_day = d
        if tgt_day not in date_set:
            continue

        # ── 出场处理 ──
        to_remove = []
        for suffix, h in holdings.items():
            px_today = price.get((suffix, tgt_day))
            if px_today is None:
                continue

            side = h["side"]
            cost = h["cost"]
            h["hold_days"] = h.get("hold_days", 0) + 1

            # 追踪收盘价序列（用于 MA5 离场）
            if "closes" in h:
                h["closes"].append(px_today["close"])
            else:
                h["closes"] = deque([px_today["close"]], maxlen=5)

            exit_reason = None
            exit_px = None

            if side == "LONG":
                h["highest"] = max(h.get("highest", -1e9), px_today["high"])
            else:
                h["lowest"] = min(h.get("lowest", 1e9), px_today["low"])

            if h["entry_date"] == tgt_day:
                pass  # T+1
            else:
                # 止损
                if side == "LONG":
                    if px_today["low"] <= cost * (1 - STOP_LOSS):
                        exit_reason = "stop_loss"
                        exit_px = cost * (1 - STOP_LOSS)
                else:
                    if px_today["high"] >= cost * (1 + STOP_LOSS):
                        exit_reason = "stop_loss"
                        exit_px = cost * (1 + STOP_LOSS)

                # 动态止盈
                if exit_reason is None:
                    if side == "LONG":
                        pnl_pct = (h["highest"] - cost) / cost
                        if pnl_pct >= TRAILING_ACTIVATE:
                            if px_today["low"] <= h["highest"] * (1 - TRAILING_DROP):
                                exit_reason = "trailing_stop"
                                exit_px = h["highest"] * (1 - TRAILING_DROP)
                    else:
                        pnl_pct = (cost - h["lowest"]) / cost
                        if pnl_pct >= TRAILING_ACTIVATE:
                            if px_today["high"] >= h["lowest"] * (1 + TRAILING_DROP):
                                exit_reason = "trailing_stop"
                                exit_px = h["lowest"] * (1 + TRAILING_DROP)

                # MA5 离场（P0）: 持仓足够久且趋势恶化时提前离场
                if exit_reason is None and ENABLE_MA5_EXIT and h["hold_days"] >= MA5_EXIT_MIN_HOLD:
                    closes = h.get("closes")
                    if closes is not None and len(closes) == 5:
                        ma5 = sum(closes) / len(closes)
                        if side == "LONG" and px_today["close"] < ma5:
                            exit_reason = "ma5_exit"
                            exit_px = px_today["close"]
                        elif side == "SHORT" and px_today["close"] > ma5:
                            exit_reason = "ma5_exit"
                            exit_px = px_today["close"]

                # 到期（P0c: 浮盈仓位可选择续持）
                if exit_reason is None and h["hold_days"] >= MAX_HOLD_DAYS:
                    cur_pnl = (px_today["close"] - cost) / cost
                    if side == "SHORT":
                        cur_pnl = -cur_pnl
                    extended = h.get("extended", False)
                    hard_cap = MAX_HOLD_DAYS + (MAX_EXTEND_DAYS if HOLD_WINNERS else 0)
                    if HOLD_WINNERS and not extended and cur_pnl > 0:
                        h["extended"] = True
                        h["extended_from"] = tgt_day
                    elif not (HOLD_WINNERS and extended and h["hold_days"] < hard_cap and cur_pnl > 0):
                        exit_reason = "max_hold_ext" if h.get("extended") else "max_hold"
                        exit_px = px_today["close"]

            # 情绪反转出场（优化特性）
            if ENABLE_REVERSAL_EXIT and exit_reason is None and suffix in day_signals:
                sig_info = day_signals[suffix]
                sig_label = sig_info["label"]
                if (side == "LONG" and sig_label == "bearish") or \
                   (side == "SHORT" and sig_label == "bullish"):
                    _, _, _, cannot_sell = _bar_state(suffix, tgt_day)
                    if not cannot_sell:
                        exit_reason = "reverse_sentiment"
                        exit_px = px_today["close"]

            if exit_reason and exit_px:
                _, _, _, cannot_sell = _bar_state(suffix, tgt_day)
                if cannot_sell and exit_reason != "max_hold":
                    continue

                if side == "LONG":
                    sell_px = exit_px * (1 - SLIPPAGE)
                    gross_pnl = (sell_px - cost) * h["shares"]
                    fee = sell_px * h["shares"] * (COMMISSION + STAMP_TAX)
                    cash += h["shares"] * sell_px - fee
                else:
                    buyback_px = exit_px * (1 + SLIPPAGE)
                    gross_pnl = (cost - buyback_px) * h["shares"]
                    fee = buyback_px * h["shares"] * (COMMISSION + STAMP_TAX)
                    cash -= h["shares"] * buyback_px + fee

                pnl = gross_pnl - fee
                trades.append({
                    "entry_date": h["entry_date"], "exit_date": tgt_day,
                    "symbol": suffix, "side": side,
                    "entry_px": round(h["entry_px"], 3), "exit_px": round(exit_px, 3),
                    "shares": h["shares"], "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (cost * h["shares"]) * 100, 2),
                    "hold_days": h["hold_days"], "exit_reason": exit_reason,
                })
                to_remove.append(suffix)

        for suffix in to_remove:
            del holdings[suffix]

        # ── 入场处理 ──
        equity = _calc_equity(cash, holdings, tgt_day)
        max_per_stock = equity * MAX_POSITION_PCT
        market_up = index_ma.get(tgt_day, True)

        for suffix, sig_info in day_signals.items():
            if suffix in holdings:
                continue
            if suffix in st_symbols:
                continue
            if len(holdings) >= MAX_CONCURRENT:
                continue

            sig_label = sig_info["label"]
            boost = sig_info.get("boost", 1.0)

            # 策略模式: follow (利好做多，利空做空)
            if sig_label == "bullish":
                side = "LONG"
                if not market_up:
                    continue
            elif sig_label == "bearish":
                side = "SHORT"
                if market_up:
                    continue
            else:
                continue

            if side == "SHORT" and (suffix.endswith(".BJ") or suffix.split(".")[0].startswith(("4", "8"))):
                continue

            _, _, cannot_buy, _ = _bar_state(suffix, tgt_day)
            px_today = price.get((suffix, tgt_day))
            if px_today is None or cannot_buy:
                continue

            entry_px = px_today["close"]
            if side == "LONG":
                entry_px = entry_px * (1 + SLIPPAGE)
            else:
                entry_px = entry_px * (1 - SLIPPAGE)

            # 仓位加成
            pos_val = min(max_per_stock * boost, cash * 0.95)
            shares = int(pos_val / entry_px / 100) * 100
            if shares < 100:
                continue

            cost_val = shares * entry_px
            fee = cost_val * COMMISSION

            if side == "LONG":
                if cost_val + fee > cash:
                    shares = int((cash / (entry_px * (1 + COMMISSION))) / 100) * 100
                    if shares < 100:
                        continue
                    cost_val = shares * entry_px
                    fee = cost_val * COMMISSION
                cash -= cost_val + fee
            else:
                cash += cost_val - fee

            holdings[suffix] = {
                "side": side, "shares": shares, "cost": entry_px,
                "entry_date": tgt_day, "entry_px": entry_px,
                "highest": px_today["high"] if side == "LONG" else None,
                "lowest": px_today["low"] if side == "SHORT" else None,
                "hold_days": 0, "boost": boost,
                "closes": deque(maxlen=5),
            }

            trades.append({
                "entry_date": tgt_day, "exit_date": None,
                "symbol": suffix, "side": side,
                "entry_px": round(entry_px, 3), "exit_px": None,
                "shares": shares, "pnl": 0.0, "pnl_pct": 0.0,
                "hold_days": 0, "exit_reason": "entry",
                "boost": boost, "n_articles": sig_info.get("n_articles", 0),
            })

            stock_last_sentiment[suffix] = sig_label

        total_val = _calc_equity(cash, holdings, tgt_day)
        daily_records[tgt_day] = {
            "value": total_val, "cash": cash,
            "n_holdings": len(holdings), "holdings": list(holdings.keys()),
        }

    return {"daily": daily_records, "trades": trades, "trading_days": trading_days}


# ── 指标计算 ──────────────────────────────────────────────

def compute_metrics(result: dict) -> dict:
    daily = result["daily"]
    trades = result["trades"]
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return {"error": "Not enough data"}

    nets = [daily[d]["value"] / INIT_CASH for d in dates]
    total_ret = nets[-1] - 1
    days_count = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
    years = max(days_count, 1) / 365
    annual = (nets[-1] ** (1 / years) - 1) if nets[-1] > 0 else None

    peak = -1e9
    max_dd = 0.0
    dd_start = dd_end = dates[0]
    current_peak_idx = 0
    for i, n in enumerate(nets):
        if n > peak:
            peak = n
            current_peak_idx = i
        dd = (peak - n) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            dd_start = dates[current_peak_idx]
            dd_end = dates[i]

    daily_rets = []
    for i in range(1, len(nets)):
        daily_rets.append(nets[i] / nets[i - 1] - 1)

    if daily_rets:
        avg_daily = np.mean(daily_rets)
        std_daily = np.std(daily_rets, ddof=1)
        sharpe = (avg_daily - 0.02 / 252) / std_daily * np.sqrt(252) if std_daily > 0 else 0.0
    else:
        sharpe = 0.0

    calmar = (annual / max_dd) if annual and max_dd > 0 else None

    completed = [t for t in trades if t["exit_reason"] != "entry"]
    long_trades = [t for t in completed if t["side"] == "LONG"]
    short_trades = [t for t in completed if t["side"] == "SHORT"]
    win_trades = [t for t in completed if t["pnl"] > 0]

    total_pnl = sum(t["pnl"] for t in completed)
    win_rate = len(win_trades) / len(completed) if completed else 0
    avg_hold = np.mean([t["hold_days"] for t in completed]) if completed else 0
    avg_win = np.mean([t["pnl"] for t in win_trades]) if win_trades else 0
    avg_loss = np.mean([t["pnl"] for t in completed if t["pnl"] <= 0]) if completed else 0

    profit_factor = abs(avg_win / avg_loss) if avg_loss and avg_loss < 0 and avg_win > 0 else 0

    reason_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0, "win": 0})
    for t in completed:
        r = t["exit_reason"]
        reason_stats[r]["count"] += 1
        reason_stats[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            reason_stats[r]["win"] += 1

    board_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0, "win": 0})
    for t in completed:
        code = t["symbol"].split(".")[0]
        b = _board_of(code)
        board_stats[b]["count"] += 1
        board_stats[b]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            board_stats[b]["win"] += 1

    return {
        "total_ret": total_ret, "annual_ret": annual, "max_dd": max_dd,
        "dd_start": dd_start, "dd_end": dd_end,
        "sharpe": sharpe, "calmar": calmar,
        "total_pnl": total_pnl, "n_trades": len(completed),
        "n_long": len(long_trades), "n_short": len(short_trades),
        "win_rate": win_rate, "avg_hold_days": avg_hold,
        "avg_win": avg_win, "avg_loss": avg_loss, "profit_factor": profit_factor,
        "reason_stats": dict(reason_stats), "board_stats": dict(board_stats),
        "dates": dates, "nets": nets, "daily_rets": daily_rets,
    }


# ── 报告 ──────────────────────────────────────────────────

def generate_report(result: dict, metrics: dict, names: dict[str, str]):
    trades = result["trades"]
    completed = [t for t in trades if t["exit_reason"] != "entry"]

    def P(s: str = ""): print(s)

    P("# 新闻情绪优化回测报告")
    P()
    P("## 优化项")
    P()
    P("- 来源过滤: 只用预测力 Top 7 来源，排除反向指标")
    P("- 时段过滤: 只用 9:00-11:30, 15:00-17:00, 19:00-22:00")
    P(f"- 多篇确认: 同股票同日 >= {MIN_ARTICLES_FOR_SIGNAL} 篇同向文章")
    P("- 首日动量: 利好+首日涨 / 利空+首日跌 才入场")
    P("- 情绪反转出场: 利好→利空或利空→利好 立即平仓")
    P("- 连续信号加成: 连续 3 天同向 → 仓位 1.5x")
    P("- 事件标签加成: 高预测力标签 → 仓位 1.3-1.5x")
    P()
    P("## 数据概览")
    P()
    P(f"- 回测区间: {metrics['dates'][0]} ~ {metrics['dates'][-1]} ({len(metrics['dates'])} 个交易日)")
    P(f"- 初始资金: {INIT_CASH:,.0f} 元")
    P()
    P("## 收益指标")
    P()
    P("| 指标 | 数值 |")
    P("|------|------|")
    P(f"| 累计收益率 | {metrics['total_ret']*100:+.2f}% |")
    if metrics["annual_ret"]:
        P(f"| 年化收益率 | {metrics['annual_ret']*100:+.2f}% |")
    P(f"| 最大回撤 | {metrics['max_dd']*100:.2f}% ({metrics['dd_start']} ~ {metrics['dd_end']}) |")
    P(f"| 夏普比率 | {metrics['sharpe']:.2f} |")
    if metrics["calmar"]:
        P(f"| Calmar 比率 | {metrics['calmar']:.2f} |")
    P(f"| 总盈亏 | {metrics['total_pnl']:+,.0f} 元 |")
    P()
    P("## 交易统计")
    P()
    P("| 指标 | 数值 |")
    P("|------|------|")
    P(f"| 总交易次数 | {metrics['n_trades']} (做多 {metrics['n_long']} / 做空 {metrics['n_short']}) |")
    P(f"| 胜率 | {metrics['win_rate']*100:.1f}% |")
    P(f"| 平均持仓天数 | {metrics['avg_hold_days']:.1f} 天 |")
    P(f"| 平均盈利 | {metrics['avg_win']:+,.0f} 元 |")
    P(f"| 平均亏损 | {metrics['avg_loss']:+,.0f} 元 |")
    P(f"| 盈亏比 | {metrics['profit_factor']:.2f} |")
    P()
    P("## 出场原因分析")
    P()
    P("| 原因 | 次数 | 盈亏 | 胜率 |")
    P("|------|------|------|------|")
    for reason, stats in sorted(metrics["reason_stats"].items()):
        wr = stats["win"] / stats["count"] * 100 if stats["count"] else 0
        P(f"| {reason} | {stats['count']} | {stats['pnl']:+,.0f} | {wr:.1f}% |")
    P()
    P("## 板块分析")
    P()
    P("| 板块 | 次数 | 盈亏 | 胜率 |")
    P("|------|------|------|------|")
    for board, stats in sorted(metrics["board_stats"].items(), key=lambda x: -x[1]["pnl"]):
        wr = stats["win"] / stats["count"] * 100 if stats["count"] else 0
        P(f"| {board} | {stats['count']} | {stats['pnl']:+,.0f} | {wr:.1f}% |")
    P()

    # 月度收益
    P("## 月度收益")
    P()
    month_map = defaultdict(list)
    for d in metrics["dates"]:
        month_map[d[:7]].append(d)
    P("| 月份 | 交易日 | 收益 |")
    P("|------|--------|------|")
    for m, ds in sorted(month_map.items()):
        if len(ds) < 2:
            continue
        m_ret = metrics["nets"][metrics["dates"].index(ds[-1])] / metrics["nets"][metrics["dates"].index(ds[0])] - 1
        P(f"| {m} | {len(ds)} | {m_ret*100:+.2f}% |")
    P()

    # 极端案例
    P("## 最大盈利 Top 10")
    P()
    top_win = sorted(completed, key=lambda x: -x["pnl"])[:10]
    P("| 股票 | 方向 | 入场日 | 出场日 | 持仓天 | 盈亏 | 原因 |")
    P("|------|------|--------|--------|--------|------|------|")
    for t in top_win:
        name = names.get(t["symbol"], t["symbol"])
        P(f"| {name} {t['symbol']} | {t['side']} | {t['entry_date']} | {t['exit_date']} | {t['hold_days']} | {t['pnl']:+,.0f} | {t['exit_reason']} |")
    P()
    P("## 最大亏损 Top 10")
    P()
    top_loss = sorted(completed, key=lambda x: x["pnl"])[:10]
    P("| 股票 | 方向 | 入场日 | 出场日 | 持仓天 | 盈亏 | 原因 |")
    P("|------|------|--------|--------|--------|------|------|")
    for t in top_loss:
        name = names.get(t["symbol"], t["symbol"])
        P(f"| {name} {t['symbol']} | {t['side']} | {t['entry_date']} | {t['exit_date']} | {t['hold_days']} | {t['pnl']:+,.0f} | {t['exit_reason']} |")
    P()


# ── 主入口 ──────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("新闻情绪优化回测（融合深度分析规律）")
    print("=" * 60)

    print("\n[1/7] 加载交易日历...")
    trading_days = load_trading_days()
    trading_days = [d for d in trading_days if "2026-03-28" <= d <= "2026-08-20"]
    trading_day_set = set(trading_days)
    print(f"  交易日: {len(trading_days)} ({trading_days[0]} ~ {trading_days[-1]})")

    print("\n[2/7] 加载新闻情绪文章（含来源/时段过滤）...")
    articles = load_enriched_articles()
    total_articles = len(articles)
    unique_stocks = len(set(a["ticker"] for a in articles))
    print(f"  过滤后文章: {total_articles} 篇, 涉及股票: {unique_stocks} 只")

    print("\n[3/7] 加载K线数据...")
    all_syms = set(a["ticker"] for a in articles)
    klines = load_klines(all_syms)
    print(f"  K线: {len(klines)} 只股票")

    # 构建价格索引（用于动量过滤）
    price: dict[tuple[str, str], dict[str, float]] = {}
    for suffix, df in klines.items():
        for _, row in df.iterrows():
            d = str(row["trade_date"])[:10]
            price[(suffix, d)] = {
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
            }

    print("\n[4/7] 构建优化信号调度...")
    schedule = build_optimized_schedule(articles, trading_days, price, trading_day_set)
    signal_dates = len(schedule)
    total_signals = sum(len(stocks) for stocks in schedule.values())
    print(f"  信号交易日: {signal_dates} 天, 总信号数: {total_signals}")

    # 统计加成
    boosted = sum(1 for d, stocks in schedule.items() for s, info in stocks.items() if info.get("boost", 1.0) > 1.0)
    consecutive_boosted = sum(1 for d, stocks in schedule.items() for s, info in stocks.items() if info.get("consecutive", 0) >= 3)
    print(f"  加成信号: {boosted} (含连续 {consecutive_boosted} 个)")

    print("\n[5/7] 加载ST列表和大盘MA...")
    st_symbols = load_st_symbols()
    index_ma = load_index_ma()
    print(f"  ST: {len(st_symbols)} 只, MA: {len(index_ma)} 天")

    print("\n[6/7] 运行回测...")
    result = run_backtest(schedule, klines, trading_days, st_symbols, index_ma)
    metrics = compute_metrics(result)
    print(f"  完成: {metrics['n_trades']} 笔交易")

    print("\n[7/7] 加载股票名称...")
    names = load_stock_names()

    print("\n")
    generate_report(result, metrics, names)

    # 保存
    root = _find_repo_root(Path(__file__).resolve())
    if RESULT_FILE:
        output_path = Path(RESULT_FILE)
        if not output_path.is_absolute():
            output_path = root / output_path
    else:
        suffix = ""
        if ENABLE_MA5_EXIT:
            suffix += f"_ma5h{MA5_EXIT_MIN_HOLD}"
        if HOLD_WINNERS:
            suffix += f"_hw{MAX_EXTEND_DAYS}"
        output_path = (root / "data") / f"backtest_news_optimized_mh{MAX_HOLD_DAYS}{suffix}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        "config": {
            "max_hold_days": MAX_HOLD_DAYS,
            "enable_ma5_exit": ENABLE_MA5_EXIT,
            "ma5_exit_min_hold": MA5_EXIT_MIN_HOLD,
            "hold_winners": HOLD_WINNERS,
            "max_extend_days": MAX_EXTEND_DAYS,
        },
        "optimizations": [
            "source_filtering", "time_filtering", "multi_article_confirm",
            "day0_momentum", "sentiment_reversal_exit", "consecutive_bonus",
            "event_tag_boost",
        ],
        "metrics": {k: v for k, v in metrics.items() if k not in ("nets", "daily_rets", "dates")},
        "nets": metrics["nets"],
        "dates": metrics["dates"],
        "trades": result["trades"],
    }
    with open(output_path, "w") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存到: {output_path}")
