"""新闻情绪回测脚本：利好做多 / 利空做空，持有至反向情绪或止损止盈。

数据源:
  - 新闻: Huntly SQLite (page.connected_at) + PostgreSQL (news_article_enrichment)
  - K线: QuantDB parquet (daily_forward)
  - 交易日历: QuantDB parquet (trading_days)
  - ST 列表: PostgreSQL (stocks)

策略规则:
  1. 利好文章 → 做多入场；利空文章 → 做空入场
  2. 出场条件（满足任一）:
     a. 反向情绪出现
     b. 持有满 20 个交易日
     c. 止损 10%
     d. 动态止盈: 盈利 > 15% 后从最高点回撤 5%（做多）/ 从最低点反弹 5%（做空）
  3. 消息时间对齐:
     - 盘中消息（9:30-15:00 交易日）→ 当天收盘价成交
     - 盘后/盘前/非交易日 → 下一交易日开盘价成交
  4. T+1: 当日买入的股票次日才可卖出
  5. 涨跌停约束: 涨停不能买，跌停不能卖
  6. 停牌: K线缺失 → 不交易，持仓顺延
  7. ST 股票排除
  8. 交易成本: 佣金 0.03% + 印花税 0.1%（卖出）+ 滑点 0.2%
"""
import sys
import os
import math
import json
import bisect
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from collections import defaultdict
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
INIT_CASH = 500_000.0         # 初始资金
MAX_HOLD_DAYS = 20            # 最大持仓天数
STOP_LOSS = 0.15              # 止损 15%
TRAILING_ACTIVATE = 0.15      # 动态止盈激活阈值
TRAILING_DROP = 0.05          # 从最高点回撤/反弹比例
SIGNAL_THRESHOLD = 0.35       # 情绪分值阈值
MAX_POSITION_PCT = 0.10       # 单只股票最大仓位
MAX_CONCURRENT = 10           # 最大同时持仓数
MA_WINDOW = 20                # 大盘 MA 过滤窗口
ENABLE_REVERSE_EXIT = False   # 反向情绪出场
ENABLE_SHORT = True           # 启用做空
STRATEGY_MODE = "fade"        # "follow"=利好做多利空做空 / "fade"=全部做空 / "long_only"=只做多
COMMISSION = 0.0003
STAMP_TAX = 0.001
SLIPPAGE = 0.002

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TRADING_AM_START = time(9, 30)
TRADING_AM_END = time(11, 30)
TRADING_PM_START = time(13, 0)
TRADING_PM_END = time(15, 0)

HUNTLY_DB = "/data/huntly/db.sqlite"
QUANTDB_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"))

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.services.trade.simulation.services.local_market_data import compute_limits
from sqlalchemy import text


# ── 工具函数 ──────────────────────────────────────────────

def _is_trading_time(dt: datetime) -> bool:
    """判断 Asia/Shanghai datetime 是否在 A 股交易时段内."""
    t = dt.time()
    return (TRADING_AM_START <= t <= TRADING_AM_END) or (TRADING_PM_START <= t <= TRADING_PM_END)


def _board_of(code: str) -> str:
    if code.startswith("688"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("00", "002", "003")):
        return "深主板"
    if code.startswith("60"):
        return "沪主板"
    if code.startswith(("83", "43", "87", "88", "92")):
        return "北交所"
    return "其他"


def _parse_huntly_time(s: str) -> datetime | None:
    """解析 Huntly connected_at (Asia/Shanghai 本地时间字符串)."""
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
    """从 QuantDB parquet 加载交易日列表（YYYY-MM-DD 格式）."""
    import pyarrow.parquet as pq
    cal_path = QUANTDB_DIR / "2_base_sector" / "trading_calendar" / "trading_days.parquet"
    df = pq.read_table(str(cal_path)).to_pandas()
    days = sorted(df["TradingDate"].astype(str).tolist())
    return [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in days]


def load_news_signals() -> dict[str, list[tuple[datetime, str, float]]]:
    """从 Huntly SQLite + PG 联合查询，构建 stock → [(datetime_shanghai, sentiment_label, score)].

    返回的 datetime 带 Asia/Shanghai tzinfo，用于后续判断盘中/盘后.
    """
    import asyncio

    async def _load():
        async with get_session(read_only=True) as s:
            res = await s.execute(text("""
                SELECT huntly_page_id, tickers, sentiment_label, sentiment_score
                FROM news_article_enrichment
                WHERE cardinality(tickers) > 0
                  AND sentiment_label IN ('bullish', 'bearish')
                  AND ABS(sentiment_score) >= :threshold
            """), {"threshold": SIGNAL_THRESHOLD})
            enrich_rows = res.fetchall()

        # 构建 huntly_page_id → enrichment 映射
        enrich_map = {}
        for row in enrich_rows:
            pid, tickers, label, score = row
            enrich_map[int(pid)] = (list(tickers), label, float(score))

        # 从 Huntly SQLite 读取 connected_at
        conn = sqlite3.connect(f"file:{HUNTLY_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in enrich_map)
        cur.execute(
            f"SELECT id, connected_at FROM page WHERE id IN ({placeholders})",
            list(enrich_map.keys()),
        )
        page_times = {int(r["id"]): r["connected_at"] for r in cur.fetchall()}
        conn.close()

        # 组装: stock → [(datetime, label, score)]
        out: dict[str, list[tuple[datetime, str, float]]] = defaultdict(list)
        for pid, (tickers, label, score) in enrich_map.items():
            ts_str = page_times.get(pid)
            dt = _parse_huntly_time(ts_str)
            if dt is None:
                continue
            for ticker in tickers:
                ticker = ticker.strip()
                if not ticker:
                    continue
                out[ticker].append((dt, label, score))

        # 按时间排序
        for ticker in out:
            out[ticker].sort(key=lambda x: x[0])
        return dict(out)

    return asyncio.run(_load())


def load_klines(symbols: set[str]) -> dict[str, pd.DataFrame]:
    """加载指定股票的前复权日K线."""
    import pyarrow.parquet as pq

    daily_dir = QUANTDB_DIR / "1_kline_data" / "daily_forward"
    suffix_list = sorted(s for s in symbols)
    if not suffix_list:
        return {}

    partitions = []
    for p in sorted(daily_dir.glob("dt=*")):
        dt_str = p.name[3:]  # "dt=20260328" → "20260328"
        if "2026" <= dt_str[:4] <= "2026":
            partitions.append(p / "data.parquet")

    filters = [("symbol", "in", suffix_list)]
    all_dfs = []
    for f in partitions:
        try:
            t = pq.read_table(f, columns=["symbol", "time", "open", "high", "low", "close"],
                              filters=filters)
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
        klines[suffix] = grp.sort_values("trade_date")[
            ["trade_date", "open", "high", "low", "close"]
        ].reset_index(drop=True)
    return klines


def load_st_symbols() -> set[str]:
    """加载 ST/*ST 股票列表."""
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
    """加载股票名称映射."""
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
            return {StockCodeUtil.to_suffix(str(x).strip().upper()): str(y)
                    for x, y in cur.fetchall()}
    finally:
        conn.close()


def load_index_ma() -> dict[str, bool]:
    """上证指数每日是否 >= MA20（大盘多头过滤）."""
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


# ── 信号调度 ──────────────────────────────────────────────

def resolve_trade_date(
    news_dt: datetime,
    trading_day_set: set[str],
    all_dates_sorted: list[str],
) -> str | None:
    """根据消息时间确定实际成交日期.

    返回 YYYY-MM-DD 格式的交易日，或 None（消息日期超出回测范围）.
    """
    cal_date = news_dt.strftime("%Y-%m-%d")

    if _is_trading_time(news_dt) and cal_date in trading_day_set:
        return cal_date  # 盘中消息 → 当天收盘价成交

    # 盘后/盘前/非交易日 → 下一交易日
    idx = bisect.bisect_left(all_dates_sorted, cal_date)
    if idx < len(all_dates_sorted):
        return all_dates_sorted[idx]
    return None


def build_signal_schedule(
    signals: dict[str, list[tuple[datetime, str, float]]],
    trading_days: list[str],
) -> dict[str, dict[str, tuple[str, float]]]:
    """构建每日信号表: {date: {stock: (label, score)}}.

    同一股票同一天多条消息 → 取绝对分值最高的一条.
    """
    trading_day_set = set(trading_days)
    all_dates_sorted = sorted(trading_day_set)

    # 聚合: date → stock → [(label, score)]
    daily_raw: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for ticker, items in signals.items():
        for news_dt, label, score in items:
            trade_date = resolve_trade_date(news_dt, trading_day_set, all_dates_sorted)
            if trade_date is None:
                continue
            daily_raw[trade_date][ticker].append((label, score))

    # 每天每股票取绝对分值最高的一条
    schedule: dict[str, dict[str, tuple[str, float]]] = {}
    for d, stocks in daily_raw.items():
        schedule[d] = {}
        for ticker, items in stocks.items():
            best = max(items, key=lambda x: abs(x[1]))
            schedule[d][ticker] = best

    return schedule


# ── 回测引擎 ──────────────────────────────────────────────

def run_backtest(
    schedule: dict[str, dict[str, tuple[str, float]]],
    klines: dict[str, pd.DataFrame],
    trading_days: list[str],
    st_symbols: set[str],
    index_ma: dict[str, bool],
) -> dict:
    """主回测循环."""
    # 价格索引: (symbol, date) → {open, high, low, close}
    price: dict[tuple[str, str], dict[str, float]] = {}
    close_map: dict[str, dict[str, float]] = {}
    for suffix, df in klines.items():
        cm = {}
        for _, row in df.iterrows():
            d = str(row["trade_date"])[:10]
            price[(suffix, d)] = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
            cm[d] = float(row["close"])
        close_map[suffix] = cm

    # 涨跌停计算
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

    def _bar_state(suffix: str, day: str) -> tuple[float | None, float | None, bool, bool]:
        """返回 (limit_up, limit_down, 一字涨停不可买, 一字跌停不可卖)."""
        lim = _limits(suffix, day)
        px = price.get((suffix, day))
        if lim is None or px is None:
            return None, None, True, True
        lu, ld = lim
        o, h, l = px["open"], px["high"], px["low"]
        cannot_buy = o >= lu - 0.001 and h <= lu + 0.001
        cannot_sell = o <= ld + 0.001 and l >= ld - 0.001
        return lu, ld, cannot_buy, cannot_sell

    # 构建 next_trading_day 映射
    date_set = set(trading_days)
    next_day = {}
    for i in range(len(trading_days) - 1):
        next_day[trading_days[i]] = trading_days[i + 1]

    # 持仓状态
    # holdings[stock] = {side, shares, cost, entry_date, entry_px, highest, lowest, hold_days}
    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    daily_records: dict[str, dict] = {}
    cash = INIT_CASH

    # 只处理有信号的交易日（减少空转）
    signal_dates = sorted(set(schedule.keys()) & date_set)
    # 但需要遍历所有交易日来检查持仓的止损/止盈
    all_trading_days = [d for d in trading_days if d >= signal_dates[0]]

    for d in all_trading_days:
        day_signals = schedule.get(d, {})
        tgt_day = d  # 当天信号当天成交（时间对齐已在 resolve_trade_date 中处理）

        if tgt_day not in date_set:
            continue

        # ── 先处理出场（止损/止盈/到期/反向情绪） ──
        to_remove = []
        for suffix, h in holdings.items():
            px_today = price.get((suffix, tgt_day))
            if px_today is None:
                continue  # 停牌，跳过

            side = h["side"]
            cost = h["cost"]
            entry_date = h["entry_date"]
            h["hold_days"] = h.get("hold_days", 0) + 1

            exit_reason = None
            exit_px = None

            # 更新 highest/lowest
            if side == "LONG":
                h["highest"] = max(h.get("highest", -1e9), px_today["high"])
            else:
                h["lowest"] = min(h.get("lowest", 1e9), px_today["low"])

            # 检查 T+1: 当天买入的不能当天卖
            if h["entry_date"] == tgt_day:
                pass  # skip exit check for T+1
            else:
                # 止损检查
                if side == "LONG":
                    if px_today["low"] <= cost * (1 - STOP_LOSS):
                        exit_reason = "stop_loss"
                        exit_px = cost * (1 - STOP_LOSS)
                else:
                    if px_today["high"] >= cost * (1 + STOP_LOSS):
                        exit_reason = "stop_loss"
                        exit_px = cost * (1 + STOP_LOSS)

                # 动态止盈检查
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

                # 到期检查
                if exit_reason is None and h["hold_days"] >= MAX_HOLD_DAYS:
                    exit_reason = "max_hold"
                    exit_px = px_today["close"]

            # 反向情绪检查（可配置开关）
            if ENABLE_REVERSE_EXIT and exit_reason is None and suffix in day_signals:
                sig_label, _sig_score = day_signals[suffix]
                if (side == "LONG" and sig_label == "bearish") or \
                   (side == "SHORT" and sig_label == "bullish"):
                    _, _, cannot_sell, _ = _bar_state(suffix, tgt_day)
                    if not cannot_sell:
                        exit_reason = "reverse_sentiment"
                        exit_px = px_today["close"]

            if exit_reason and exit_px:
                # 执行出场
                _, _, _, cannot_sell = _bar_state(suffix, tgt_day)
                if cannot_sell and exit_reason != "max_hold":
                    continue  # 一字板无法出场（到期除外，用收盘价强制平仓）

                # 实际成交价（考虑滑点）和 PnL
                if side == "LONG":
                    sell_px = exit_px * (1 - SLIPPAGE)
                    gross_pnl = (sell_px - cost) * h["shares"]
                    fee = sell_px * h["shares"] * (COMMISSION + STAMP_TAX)
                    cash += h["shares"] * sell_px - fee
                else:
                    # 做空平仓：买入还券
                    buyback_px = exit_px * (1 + SLIPPAGE)
                    gross_pnl = (cost - buyback_px) * h["shares"]
                    fee = buyback_px * h["shares"] * (COMMISSION + STAMP_TAX)
                    cash -= h["shares"] * buyback_px + fee

                pnl = gross_pnl - fee

                trades.append({
                    "entry_date": h["entry_date"],
                    "exit_date": tgt_day,
                    "symbol": suffix,
                    "side": side,
                    "entry_px": round(h["entry_px"], 3),
                    "exit_px": round(exit_px, 3),
                    "shares": h["shares"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (cost * h["shares"]) * 100, 2),
                    "hold_days": h["hold_days"],
                    "exit_reason": exit_reason,
                })
                to_remove.append(suffix)

        for suffix in to_remove:
            del holdings[suffix]

        # ── 再处理入场 ──
        # 计算当前权益（做空头寸要减去市值）
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
                    # 做空: 权益 = 现金 + (开仓价 - 当前价) * 股数
                    val += (h["cost"] - close) * h["shares"]
            return val

        equity = _calc_equity(cash, holdings, tgt_day)
        max_per_stock = equity * MAX_POSITION_PCT

        # 大盘过滤
        market_up = index_ma.get(tgt_day, True)

        for suffix, (sig_label, sig_score) in day_signals.items():
            if suffix in holdings:
                continue
            if suffix in st_symbols:
                continue
            if len(holdings) >= MAX_CONCURRENT:
                continue

            # 策略模式：决定方向
            if STRATEGY_MODE == "fade":
                # 全部做空：利好和利空消息都做空
                side = "SHORT"
                if not ENABLE_SHORT:
                    continue
                # 大盘空头时做空（顺势）
                if market_up:
                    continue
            elif STRATEGY_MODE == "long_only":
                if sig_label == "bullish":
                    side = "LONG"
                    if not market_up:
                        continue
                else:
                    continue
            elif STRATEGY_MODE == "follow":
                if sig_label == "bullish":
                    side = "LONG"
                    if not market_up:
                        continue
                elif sig_label == "bearish":
                    side = "SHORT"
                    if not ENABLE_SHORT:
                        continue
                    if market_up:
                        continue
                else:
                    continue
            else:
                continue

            # 北交所不做空
            if side == "SHORT" and suffix.endswith(".BJ"):
                continue

            _, _, cannot_buy, _ = _bar_state(suffix, tgt_day)
            px_today = price.get((suffix, tgt_day))
            if px_today is None or cannot_buy:
                continue

            # 入场价: 盘中消息用收盘价，盘后消息用次日开盘（已在 resolve_trade_date 处理）
            # 这里统一用收盘价（如果是盘中消息）或开盘价（如果是盘后消息）
            # 简化处理：用收盘价
            entry_px = px_today["close"]
            if side == "LONG":
                entry_px = entry_px * (1 + SLIPPAGE)
            else:
                entry_px = entry_px * (1 - SLIPPAGE)

            pos_val = min(max_per_stock, cash * 0.95)  # 最多用 95% 现金
            shares = int(pos_val / entry_px / 100) * 100
            if shares < 100:
                continue

            cost_val = shares * entry_px
            fee = cost_val * COMMISSION

            if side == "LONG":
                # 做多：花费现金买入
                if cost_val + fee > cash:
                    shares = int((cash / (entry_px * (1 + COMMISSION))) / 100) * 100
                    if shares < 100:
                        continue
                    cost_val = shares * entry_px
                    fee = cost_val * COMMISSION
                cash -= cost_val + fee
            else:
                # 做空：卖出借来的股票，收到现金
                # 不需要检查现金，但需要确保有足够的"保证金"（简化：不需要额外保证金）
                cash += cost_val - fee

            holdings[suffix] = {
                "side": side,
                "shares": shares,
                "cost": entry_px,
                "entry_date": tgt_day,
                "entry_px": entry_px,
                "highest": px_today["high"] if side == "LONG" else None,
                "lowest": px_today["low"] if side == "SHORT" else None,
                "hold_days": 0,
            }

            trades.append({
                "entry_date": tgt_day,
                "exit_date": None,
                "symbol": suffix,
                "side": side,
                "entry_px": round(entry_px, 3),
                "exit_px": None,
                "shares": shares,
                "pnl": 0.0,
                "pnl_pct": 0.0,
                "hold_days": 0,
                "exit_reason": "entry",
            })

        # 记录当日净值
        total_val = _calc_equity(cash, holdings, tgt_day)
        daily_records[tgt_day] = {
            "value": total_val,
            "cash": cash,
            "n_holdings": len(holdings),
            "holdings": list(holdings.keys()),
        }

    return {
        "daily": daily_records,
        "trades": trades,
        "trading_days": trading_days,
    }


# ── 指标计算 ──────────────────────────────────────────────

def compute_metrics(result: dict) -> dict:
    """计算回测绩效指标."""
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

    # 最大回撤
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

    # 日收益率
    daily_rets = []
    for i in range(1, len(nets)):
        daily_rets.append(nets[i] / nets[i - 1] - 1)

    # 夏普比率（年化，假设无风险利率 2%）
    if daily_rets:
        avg_daily = np.mean(daily_rets)
        std_daily = np.std(daily_rets, ddof=1)
        if std_daily > 0:
            sharpe = (avg_daily - 0.02 / 252) / std_daily * np.sqrt(252)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Calmar 比率
    calmar = (annual / max_dd) if annual and max_dd > 0 else None

    # 交易统计
    completed = [t for t in trades if t["exit_reason"] != "entry"]
    long_trades = [t for t in completed if t["side"] == "LONG"]
    short_trades = [t for t in completed if t["side"] == "SHORT"]
    win_trades = [t for t in completed if t["pnl"] > 0]

    total_pnl = sum(t["pnl"] for t in completed)
    win_rate = len(win_trades) / len(completed) if completed else 0
    avg_hold = np.mean([t["hold_days"] for t in completed]) if completed else 0
    avg_win = np.mean([t["pnl"] for t in win_trades]) if win_trades else 0
    avg_loss = np.mean([t["pnl"] for t in completed if t["pnl"] <= 0]) if completed else 0

    # 盈亏比
    if avg_loss != 0 and avg_loss < 0:
        profit_factor = abs(avg_win / avg_loss) if avg_win > 0 else 0
    else:
        profit_factor = 0

    # 按出场原因统计
    reason_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0, "win": 0})
    for t in completed:
        r = t["exit_reason"]
        reason_stats[r]["count"] += 1
        reason_stats[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            reason_stats[r]["win"] += 1

    # 按板块统计
    board_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0, "win": 0})
    for t in completed:
        code = t["symbol"].split(".")[0]
        b = _board_of(code)
        board_stats[b]["count"] += 1
        board_stats[b]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            board_stats[b]["win"] += 1

    return {
        "total_ret": total_ret,
        "annual_ret": annual,
        "max_dd": max_dd,
        "dd_start": dd_start,
        "dd_end": dd_end,
        "sharpe": sharpe,
        "calmar": calmar,
        "total_pnl": total_pnl,
        "n_trades": len(completed),
        "n_long": len(long_trades),
        "n_short": len(short_trades),
        "win_rate": win_rate,
        "avg_hold_days": avg_hold,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "reason_stats": dict(reason_stats),
        "board_stats": dict(board_stats),
        "dates": dates,
        "nets": nets,
        "daily_rets": daily_rets,
    }


# ── 报告生成 ──────────────────────────────────────────────

def generate_report(result: dict, metrics: dict, names: dict[str, str], signals_count: dict):
    """打印 Markdown 格式的回测报告."""
    trades = result["trades"]
    daily = result["daily"]
    completed = [t for t in trades if t["exit_reason"] != "entry"]

    def P(s: str = ""):
        print(s)

    P("# 新闻情绪回测报告")
    P()
    P("## 数据概览")
    P()
    P(f"- 回测区间: {metrics['dates'][0]} ~ {metrics['dates'][-1]} ({len(metrics['dates'])} 个交易日)")
    P(f"- 初始资金: {INIT_CASH:,.0f} 元")
    P(f"- 总文章数: {signals_count.get('total_articles', 0):,}")
    P(f"- 信号文章数: {signals_count.get('signal_articles', 0):,}")
    P(f"- 涉及股票数: {signals_count.get('unique_stocks', 0)}")
    P()
    P("## 策略参数")
    P()
    P(f"- 策略模式: {STRATEGY_MODE}")
    P(f"- 止损: {STOP_LOSS*100:.0f}%")
    P(f"- 最大持仓: {MAX_HOLD_DAYS} 个交易日")
    P(f"- 动态止盈: 盈利 > {TRAILING_ACTIVATE*100:.0f}% 激活，回撤 {TRAILING_DROP*100:.0f}% 出场")
    P(f"- 信号阈值: |分值| >= {SIGNAL_THRESHOLD}")
    P(f"- 费用: 佣金 {COMMISSION*10000:.0f}‱ + 印花税 {STAMP_TAX*100:.1f}% + 滑点 {SLIPPAGE*100:.1f}%")
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
    P("## 极端案例")
    P()
    top_win = sorted(completed, key=lambda x: -x["pnl"])[:10]
    top_loss = sorted(completed, key=lambda x: x["pnl"])[:10]
    P("### 最大盈利 Top 10")
    P()
    P("| 股票 | 方向 | 入场日 | 出场日 | 持仓天 | 盈亏 | 原因 |")
    P("|------|------|--------|--------|--------|------|------|")
    for t in top_win:
        name = names.get(t["symbol"], t["symbol"])
        P(f"| {name} {t['symbol']} | {t['side']} | {t['entry_date']} | {t['exit_date']} | {t['hold_days']} | {t['pnl']:+,.0f} | {t['exit_reason']} |")
    P()
    P("### 最大亏损 Top 10")
    P()
    P("| 股票 | 方向 | 入场日 | 出场日 | 持仓天 | 盈亏 | 原因 |")
    P("|------|------|--------|--------|--------|------|------|")
    for t in top_loss:
        name = names.get(t["symbol"], t["symbol"])
        P(f"| {name} {t['symbol']} | {t['side']} | {t['entry_date']} | {t['exit_date']} | {t['hold_days']} | {t['pnl']:+,.0f} | {t['exit_reason']} |")
    P()

    # 交易明细（前 50 笔）
    P("## 交易明细（前 50 笔）")
    P()
    P("| 入场日 | 出场日 | 股票 | 方向 | 入场价 | 出场价 | 盈亏 | 盈亏% | 原因 |")
    P("|--------|--------|------|------|--------|--------|------|-------|------|")
    for t in completed[:50]:
        name = names.get(t["symbol"], t["symbol"])
        P(f"| {t['entry_date']} | {t['exit_date']} | {name} | {t['side']} | {t['entry_px']:.2f} | {t['exit_px']:.2f} | {t['pnl']:+,.0f} | {t['pnl_pct']:+.1f}% | {t['exit_reason']} |")


# ── 主入口 ──────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("新闻情绪回测")
    print("=" * 60)

    print("\n[1/6] 加载交易日历...")
    trading_days = load_trading_days()
    # 限制到新闻数据范围内
    trading_days = [d for d in trading_days if "2026-03-28" <= d <= "2026-08-20"]
    print(f"  交易日: {len(trading_days)} ({trading_days[0]} ~ {trading_days[-1]})")

    print("\n[2/6] 加载新闻情绪信号...")
    signals = load_news_signals()
    total_articles = sum(len(v) for v in signals.values())
    print(f"  信号股票: {len(signals)} 只")
    print(f"  信号文章: {total_articles} 篇")

    print("\n[3/6] 构建每日信号调度...")
    schedule = build_signal_schedule(signals, trading_days)
    signal_dates = len(schedule)
    print(f"  信号交易日: {signal_dates} 天")

    print("\n[4/6] 加载K线数据...")
    all_syms = set(signals.keys())
    klines = load_klines(all_syms)
    print(f"  加载K线: {len(klines)} 只股票")

    print("\n[5/6] 加载ST列表和大盘MA...")
    st_symbols = load_st_symbols()
    index_ma = load_index_ma()
    print(f"  ST股票: {len(st_symbols)} 只, MA数据: {len(index_ma)} 天")

    print("\n[6/6] 运行回测...")
    result = run_backtest(schedule, klines, trading_days, st_symbols, index_ma)
    metrics = compute_metrics(result)
    print(f"  完成: {metrics['n_trades']} 笔交易")

    print("\n加载股票名称...")
    names = load_stock_names()
    signals_count = {
        "total_articles": total_articles,
        "signal_articles": total_articles,
        "unique_stocks": len(signals),
    }

    print("\n")
    generate_report(result, metrics, names, signals_count)

    # 保存结果到 JSON
    output_path = (_find_repo_root(Path(__file__).resolve()) / "data") / "backtest_news_sentiment.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        "metrics": {k: v for k, v in metrics.items()
                    if k not in ("nets", "daily_rets", "dates")},
        "nets": metrics["nets"],
        "dates": metrics["dates"],
    }
    with open(output_path, "w") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存到: {output_path}")
