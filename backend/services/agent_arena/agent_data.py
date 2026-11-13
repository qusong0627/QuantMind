# ── 移植注（2026-09-29，arena 原生迁移 A2）─────────────────────────────
# 逐字节副本：quant-Trader `backend/services/agent_data.py`（上游已退役）。
# 改写点仅两处导入（下）——`backend.config.get_data_root` → 本包 arena_config，
# `backend.services.text_io` → 本包 text_io。改逻辑前先 diff 源文件确认无漂移。
# ─────────────────────────────────────────────────────────────────────
"""agent_data 聚合服务：读取交易落盘数据，提供结构化视图。

position.jsonl 每行: {"date", "id", "this_action"?, "positions": {"CASH": ...}}
log 每行: {"signature", "new_messages": [{"role", "content"}]}
兼容日级（date 为 YYYY-MM-DD）与小时级（YYYY-MM-DD HH:MM:SS）两种格式。
"""

import json
import math
import os
import statistics
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.agent_arena.arena_config import get_data_root
from backend.services.agent_arena.text_io import read_text_lossy

# 价格行缓存（见 _cached_price_rows）：解析结果按文件版本记忆化，避免同一市场重复解析。
_PRICE_ROWS_CACHE: Dict[tuple, List[tuple]] = {}
_PRICE_ROWS_LOCK = threading.Lock()
_PRICE_ROWS_MAX = 12


def list_agents(config: dict, market: str = "us") -> List[Dict[str, Any]]:
    """扫描 data/{agent_data_dir}/ 下的模型目录，返回元信息列表。"""
    markets = config.get("markets", {})
    market_cfg = markets.get(market, {})
    agent_dir = get_data_root(config) / market_cfg.get("data_dir", "agent_data")
    agents = []
    if not agent_dir.exists():
        return agents
    for folder in sorted(agent_dir.iterdir()):
        if not folder.is_dir():
            continue
        # 服务型 agent（新闻管线/晚间复盘）不进模型列表——只在新闻 tab 展示
        if folder.name.startswith("news-"):
            continue
        position_file = folder / "position" / "position.jsonl"
        log_dir = folder / "log"
        info = {
            "name": folder.name,
            "has_position": position_file.exists(),
            "has_log": log_dir.exists(),
            "latest_date": None,
            "total_records": 0,
            "cash": None,
        }
        if position_file.exists():
            records = _read_jsonl(position_file)
            info["total_records"] = len(records)
            if records:
                info["latest_date"] = records[-1].get("date")
                info["cash"] = records[-1].get("positions", {}).get("CASH")
        agents.append(info)
    return agents


def load_position_records(config: dict, agent: str, market: str = "us",
                          limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读取某 agent 的 position.jsonl 全量记录（按时间正序）。"""
    position_file = _agent_position_file(config, agent, market)
    if not position_file.exists():
        return []
    records = _read_jsonl(position_file)
    return records[-limit:] if limit and limit > 0 else records


def load_trades(config: dict, agent: str, market: str = "us",
                limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """从 position 记录中提取有实际交易动作的行。"""
    records = load_position_records(config, agent, market)
    trades = [r for r in records if r.get("this_action")]
    return trades[-limit:] if limit and limit > 0 else trades


def enrich_trades_with_prices(config: dict, market: str,
                              trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为原始成交补 price/notional：按成交日从价格文件重算（滑点模型同 rebuild_closed_trades）。

    返回新列表（不可变）：每条 {..., price, notional}；查不到价格时 price/notional 为 None。
    """
    if not trades:
        return trades
    fees = config.get("trading", {}).get("fees", {})
    slippage = float(fees.get("slippage", 0.0005))
    prices = _load_price_lookup(config, market)
    enriched = []
    for t in trades:
        # sqlite 缓存为顶层字段（action/symbol/amount），JSONL 直读为 this_action 嵌套，两者兼容
        ta = t.get("this_action") or t
        date = t.get("date", "")[:10]
        sym = ta.get("symbol", "")
        action = ta.get("action")
        raw = prices.get(date, {}).get(sym)
        price = notional = None
        if raw:
            price = float(raw) * (1 + slippage) if action == "buy" else float(raw) * (1 - slippage)
            qty = ta.get("amount", 0) or 0
            notional = price * qty
        enriched.append({**t, "price": price, "notional": notional})
    return enriched


def load_agent_logs(config: dict, agent: str, market: str = "us",
                    date: Optional[str] = None,
                    limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读取某 agent 的日志记录；date 指定时只读该日期目录（精确匹配前缀）。

    limit：只取最近 N 条（按日期目录正序取尾部）。日志单条 = 一整个分析回合
    （提示词+回复，约 6KB），全量累积可达 1MB+，前端每 30s 轮询一遍是纯浪费
    （2026-09-08 卡顿治理）。
    """
    log_dir = _agent_log_dir(config, agent, market)
    if not log_dir.exists():
        return []
    lines = []
    for folder in sorted(log_dir.iterdir()):
        if not folder.is_dir():
            continue
        if date and not folder.name.startswith(date):
            continue
        log_file = folder / "log.jsonl"
        if log_file.exists():
            lines.extend(_read_jsonl(log_file))
    return lines[-limit:] if limit and limit > 0 else lines


def compute_equity_series(config: dict, agent: str, market: str = "us") -> List[Dict[str, Any]]:
    """净值序列：用 merged.jsonl 当日价格估值持仓，equity = CASH + Σ(股数×价格)。

    找不到当日价格时用记录中上次已知价格；全部缺失时以 CASH 兜底。
    """
    records = load_position_records(config, agent, market)
    if not records:
        return []
    prices = _load_price_lookup(config, market)
    series = []
    last_known_price: Dict[str, float] = {}
    for record in records:
        positions = record.get("positions", {})
        cash = positions.get("CASH", 0.0)
        date_key = record.get("date", "")[:10]
        market_value = 0.0
        for symbol, shares in positions.items():
            if symbol == "CASH" or not shares:
                continue
            price = None
            day_prices = prices.get(date_key, {})
            if symbol in day_prices:
                price = day_prices[symbol]
                last_known_price[symbol] = price
            elif symbol in last_known_price:
                price = last_known_price[symbol]
            if price is not None:
                market_value += float(shares) * float(price)
        series.append({
            "date": record.get("date"),
            "cash": round(float(cash), 2),
            "market_value": round(market_value, 2),
            "equity": round(float(cash) + market_value, 2),
            "action": record.get("this_action"),
        })
    return series


def _agent_position_file(config: dict, agent: str, market: str) -> Path:
    market_cfg = config.get("markets", {}).get(market, {})
    return get_data_root(config) / market_cfg.get("data_dir", "agent_data") / agent / "position" / "position.jsonl"


def _agent_log_dir(config: dict, agent: str, market: str) -> Path:
    market_cfg = config.get("markets", {}).get(market, {})
    return get_data_root(config) / market_cfg.get("data_dir", "agent_data") / agent / "log"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    lines = []
    try:
        # read_text_lossy：日志可能被截断在多字节字符中间，裸 read_text 会抛
        # UnicodeDecodeError（非 OSError，下面接不住）→ 整个端点 500、好记录陪葬。
        # 详见 backend/services/text_io.py。
        for line in read_text_lossy(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return lines


def _merged_file(config: dict, market: str) -> Path:
    """各市场合并价格文件（merged.jsonl，Alpha Vantage 格式，每行一只股票）。"""
    if market == "cn":
        return get_data_root(config) / "A_stock" / "merged.jsonl"
    if market == "hk":
        return get_data_root(config) / "HK_stock" / "merged.jsonl"
    return get_data_root(config) / "merged.jsonl"


def _duckdb_price_rows(path: Path, field_chain: tuple[str, ...]) -> List[tuple]:
    """DuckDB 展开 merged.jsonl → [(symbol, date_key, price)]。

    时间序列的日期是列名（"Time Series (Daily)" 的 struct key），取值须走
    json 路径 $."1. open"；field_chain 为 bar 内价格字段优先级（coalesce）。
    文件缺失 / duckdb 不可用 / 解析失败一律返回 []（调用方走逐行兜底）。
    """
    if not path.exists():
        return []
    try:
        import duckdb
    except ImportError:
        return []
    coalesce = ",\n".join(
        f"try_cast(json_extract_string(ts_json, '$.' || k.key || '.\"{f}\"') AS DOUBLE)"
        for f in field_chain
    )
    sql = f"""
    WITH raw AS (
        SELECT "Meta Data"."2. Symbol" AS symbol,
               to_json("Time Series (Daily)") AS ts_json
        FROM read_json_auto(?, format='newline_delimited')
    ),
    flat AS (
        SELECT symbol, k.key AS date_key, coalesce({coalesce}) AS price
        FROM raw, LATERAL unnest(json_keys(ts_json)) AS k(key)
    )
    SELECT symbol, date_key, price FROM flat WHERE price IS NOT NULL
    """
    try:
        con = duckdb.connect()
        try:
            return con.execute(sql, [str(path)]).fetchall()
        finally:
            con.close()
    except Exception:
        return []


def _fallback_price_rows(path: Path, field_chain: tuple[str, ...]) -> List[tuple]:
    """逐行 json.loads 兜底（duckdb 不可用或解析失败时），口径同 duckdb 路径。"""
    rows: List[tuple] = []
    for line in _read_raw_lines(path):
        doc = json.loads(line) if line else None
        if not doc:
            continue
        symbol = (doc.get("Meta Data") or {}).get("2. Symbol")
        if not symbol:
            continue
        for key, value in doc.items():
            if not key.startswith("Time Series") or not isinstance(value, dict):
                continue
            for ts_key, bar in value.items():
                price = next((bar.get(f) for f in field_chain if bar.get(f) is not None), None)
                if price is None:
                    continue
                rows.append((symbol, ts_key[:10], float(price)))
    return rows


def _cached_price_rows(path: Path, field_chain: tuple[str, ...]) -> List[tuple]:
    """merged.jsonl → [(symbol, date_key, price)]，按 (路径, 字段链, mtime, size) 记忆化。

    为什么需要（2026-09-08）：解析整份 merged.jsonl（duckdb 展开全表）是
    /api/overview 与 /api/prices 的主要成本，而 overview 对每个 agent 各调一次
    compute_equity_series → 同一市场重复解析 3 次。冷启动实测 5.8s，正是前端
    「刷新后左侧几秒才显示」的根因。文件 mtime/size 变化（每日价格同步）即失效。
    """
    try:
        st = path.stat()
        key = (str(path), field_chain, st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    with _PRICE_ROWS_LOCK:
        hit = _PRICE_ROWS_CACHE.get(key)
    if hit is not None:
        return hit
    rows = _duckdb_price_rows(path, field_chain)
    if not rows:
        rows = _fallback_price_rows(path, field_chain)
    with _PRICE_ROWS_LOCK:
        _PRICE_ROWS_CACHE[key] = rows
        while len(_PRICE_ROWS_CACHE) > _PRICE_ROWS_MAX:  # 无界增长防护（3 市场 × 2 字段链）
            _PRICE_ROWS_CACHE.pop(next(iter(_PRICE_ROWS_CACHE)))
    return rows


def _load_price_lookup(config: dict, market: str) -> Dict[str, Dict[str, float]]:
    """构建 {date: {symbol: price}} 查询表（优先 DuckDB，逐行兜底）。

    价格取每日首条记录（"1. buy price" → "1. open" 优先级，口径同旧实现）。
    """
    rows = _cached_price_rows(_merged_file(config, market), ("1. buy price", "1. open"))
    lookup: Dict[str, Dict[str, float]] = {}
    for symbol, date_key, price in rows:
        lookup.setdefault(date_key, {}).setdefault(symbol, price)
    return lookup


def load_latest_prices(config: dict, market: str) -> Dict[str, Dict[str, Any]]:
    """每只股票最新收盘价 + 昨收 + 涨跌幅（Live 滚动价格条用）。

    - 收盘价优先 `4. sell price`（US/HK）/`4. close`（CN），回退开盘价
    - 返回 {symbol: {price, date, prev_close, change_pct}}，无昨收时 change_pct 为 None
    """
    merged = _merged_file(config, market)
    rows = _cached_price_rows(merged, ("4. sell price", "4. close", "1. buy price", "1. open"))
    per_symbol: Dict[str, List[tuple]] = {}
    for symbol, date_key, price in rows:
        per_symbol.setdefault(symbol, []).append((date_key, price))
    out: Dict[str, Dict[str, Any]] = {}
    for symbol, rows_ in per_symbol.items():
        rows_.sort()
        latest = rows_[-1]
        prev = rows_[-2] if len(rows_) >= 2 else None
        date, price = latest  # rows_ 元素为 (date_str, price_float)
        out[symbol] = {
            "price": round(price, 4),
            "date": date,
            "prev_close": round(prev[1], 4) if prev else None,
            "change_pct": round((price - prev[1]) / prev[1], 6) if prev and prev[1] else None,
        }
    return out


def _read_raw_lines(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield line
    except OSError:
        return


def rebuild_closed_trades(
    config: dict, market: str, records: List[Dict[str, Any]]
) -> tuple:
    """FIFO 重建已平仓逐笔（LAST 25 TRADES 表 + 汇总指标共用）。

    返回 (closed, total_fee, open_lots)：
    - closed 每笔: {symbol, exit_date, qty, entry_price, exit_price, notional, fee, pnl, hold_days}
    - total_fee 含未平仓买单手续费（口径同历史 summary）
    - open_lots: {symbol: {qty, entry_price}} 当前剩余持仓（加权成本）
    - 成交价/手续费模型同 tool_trade：滑点 ±0.05% + 双边费率 0.03%
    - pnl = 卖出名义×(1-卖费率) − 买入成本（含滑点）
    - 每笔 fee = 卖出手续费 + 所消耗买单的摊分手续费
    """
    fees = config.get("trading", {}).get("fees", {})
    buy_rate = float(fees.get("buy_rate", 0.0003))
    sell_rate = float(fees.get("sell_rate", 0.0003))
    slippage = float(fees.get("slippage", 0.0005))
    prices = _load_price_lookup(config, market)

    lots: Dict[str, List[list]] = {}  # symbol -> [[qty, cost, buy_fee, buy_date]]
    closed: List[Dict[str, Any]] = []
    total_fee = 0.0
    for rec in records:
        date = rec.get("date", "")[:10]
        action = rec.get("this_action") or {}
        sym = action.get("symbol", "")
        qty = action.get("amount", 0) or 0
        kind = action.get("action")
        price = prices.get(date, {}).get(sym)
        if not price:
            continue
        if kind == "buy" and qty > 0 and sym:
            ex = float(price) * (1 + slippage)
            total_fee += ex * qty * buy_rate
            lots.setdefault(sym, []).append([qty, ex, ex * qty * buy_rate, date])
        elif kind == "sell" and qty > 0 and sym:
            ex = float(price) * (1 - slippage)
            total_fee += ex * qty * sell_rate
            remain, cost, buy_fees, buy_date = qty, 0.0, 0.0, None
            stack = lots.get(sym, [])
            while remain > 0 and stack:
                lot = stack[0]
                lq, lp, lf, ld = lot
                take = min(lq, remain)
                cost += take * lp
                if lq:
                    buy_fees += lf * (take / lq)
                if buy_date is None:
                    buy_date = ld
                remain -= take
                if take >= lq:
                    stack.pop(0)
                else:
                    lot[0] = lq - take
            sold = qty - remain
            if sold > 0:
                notional = ex * qty
                closed.append({
                    "symbol": sym,
                    "exit_date": date,
                    "qty": qty,
                    "entry_price": round(cost / qty, 4),
                    "exit_price": round(ex, 4),
                    "notional": round(notional, 2),
                    "fee": round(buy_fees + ex * qty * sell_rate, 2),
                    "pnl": round(notional * (1 - sell_rate) - cost, 2),
                    "hold_days": (
                        (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(buy_date, "%Y-%m-%d")).days
                        if buy_date else None
                    ),
                })
    # 剩余持仓（FIFO 队列未消耗部分 = 当前持仓，加权成本）
    open_lots: Dict[str, Dict[str, float]] = {}
    for sym, stack in lots.items():
        qty = sum(lot[0] for lot in stack)
        if qty > 0:
            cost = sum(lot[0] * lot[1] for lot in stack) / qty
            open_lots[sym] = {"qty": round(qty, 4), "entry_price": round(cost, 4)}
    return closed, total_fee, open_lots


def compute_holdings(
    config: dict, market: str, records: List[Dict[str, Any]], quotes: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """当前持仓明细（ModelDetail 持仓 tab）：数量/成本/最新价/市值/浮动盈亏/占比。

    - 成本来自 FIFO 剩余 lot（加权成本，含滑点）
    - quotes 来自 load_latest_prices（最新收盘）
    - 返回 {holdings: [...], cash, total_equity}
    """
    _, _, open_lots = rebuild_closed_trades(config, market, records)
    holdings: List[Dict[str, Any]] = []
    cash = 0.0
    if records:
        cash = float(records[-1].get("positions", {}).get("CASH", 0.0) or 0.0)
    total_mv = 0.0
    for sym, lot in open_lots.items():
        qty = lot["qty"]
        cost = lot["entry_price"]
        quote = quotes.get(sym, {})
        price = quote.get("price")
        if price is None:
            continue
        mv = qty * price
        cost_total = qty * cost
        total_mv += mv
        holdings.append({
            "symbol": sym,
            "qty": round(qty, 4),
            "entry_price": round(cost, 4),
            "price": round(price, 4),
            "market_value": round(mv, 2),
            "pnl": round(mv - cost_total, 2),
            "pnl_pct": round((price - cost) / cost, 6) if cost else None,
            "change_pct": quote.get("change_pct"),
        })
    holdings.sort(key=lambda h: h["market_value"], reverse=True)
    total_equity = cash + total_mv
    for h in holdings:
        h["weight_pct"] = round(h["market_value"] / total_equity, 4) if total_equity else None
    return {
        "holdings": holdings,
        "cash": round(cash, 2),
        "total_market_value": round(total_mv, 2),
        "total_equity": round(total_equity, 2),
    }


def compute_extended_summary(
    series: List[Dict[str, Any]], records: List[Dict[str, Any]], config: dict, market: str
) -> Dict[str, Any]:
    """NOF1 风格扩展指标：Sharpe / 胜率 / 盈亏比 / 费用占比 / 平均持仓时长 / 多头时间占比。

    - Sharpe：按天聚合权益（同日取最后一条）→ 日收益 → mean/std × sqrt(252)
    - 胜率/盈亏比：从 position 记录重建逐笔买卖（价格文件重算成交价与手续费），
      FIFO 平仓计每笔 PnL
    - 费用占比：累计手续费 / 期初权益（手续费模型同 tool_trade：slippage + 双边费率）
    """
    out: Dict[str, Any] = {}

    # ---- 1. Sharpe（日收益） ----
    daily: Dict[str, float] = {}
    for p in series:
        daily[p.get("date", "")[:10]] = p["equity"]  # 同日后写入即当日最后一条
    days = sorted(daily)
    rets = []
    for i in range(1, len(days)):
        prev, cur = daily[days[i - 1]], daily[days[i]]
        if prev:
            rets.append(cur / prev - 1)
    if len(rets) >= 2:
        mu, sd = statistics.mean(rets), statistics.stdev(rets)
        out["sharpe"] = round(mu / sd * math.sqrt(252), 3) if sd else 0.0
    else:
        out["sharpe"] = 0.0

    # ---- 2. 逐笔买卖重建（FIFO 平仓 + 价格文件重算成交价/费用） ----
    closed, total_fee, _ = rebuild_closed_trades(config, market, records)

    first_buy: Dict[str, str] = {}      # symbol -> 首买日期
    last_hold: Dict[str, str] = {}      # symbol -> 最后持有日期
    held_days = set()                   # 有持仓的日期
    for rec in records:
        date = rec.get("date", "")[:10]
        action = rec.get("this_action") or {}
        sym = action.get("symbol", "")
        if action.get("action") == "buy" and sym and sym not in first_buy:
            first_buy[sym] = date
        pos = rec.get("positions", {})
        if any(k != "CASH" and v for k, v in pos.items()):
            held_days.add(date)
            for sym in first_buy:
                if pos.get(sym, 0) > 0:
                    last_hold[sym] = date

    # ---- 3. 胜率 / 盈亏比 / 极值 / 期望 / 规模 ----
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    if closed:
        win_rate = len(wins) / len(closed)
        out["win_rate"] = round(win_rate, 4)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
        # 盈亏比 = 盈利合计 / 亏损合计（总 PnL 之比，非均值之比）；
        # 均价比(avg_win/avg_loss)是 payoff ratio，口径不同，前端文案按合计展示
        sum_win = sum(t["pnl"] for t in wins)
        sum_loss = abs(sum(t["pnl"] for t in losses))
        out["profit_factor"] = (
            round(sum_win / sum_loss, 3) if sum_loss > 0 else (None if not wins else 999.0)
        )
        out["closed_trades"] = len(closed)
        out["biggest_win"] = round(max(t["pnl"] for t in wins), 2) if wins else None
        out["biggest_loss"] = round(min(t["pnl"] for t in losses), 2) if losses else None
        out["avg_trade_pnl"] = round(sum(t["pnl"] for t in closed) / len(closed), 2)
        out["expectancy"] = round(avg_win * win_rate - avg_loss * (1 - win_rate), 2)
        sizes = sorted(t["notional"] for t in closed)
        out["avg_trade_size"] = round(sum(sizes) / len(sizes), 2)
        out["median_trade_size"] = round(sizes[len(sizes) // 2], 2)
        holds = sorted(t["hold_days"] for t in closed if t["hold_days"] is not None)
        out["median_hold_days"] = round(holds[len(holds) // 2], 1) if holds else None
    else:
        out.update({
            "win_rate": None, "profit_factor": None, "closed_trades": 0,
            "biggest_win": None, "biggest_loss": None, "avg_trade_pnl": None,
            "expectancy": None, "avg_trade_size": None, "median_trade_size": None,
            "median_hold_days": None,
        })

    # ---- 4. 费用 ----
    initial = series[0]["equity"] if series else 0.0
    out["total_fee"] = round(total_fee, 2)
    out["fee_ratio"] = round(total_fee / initial, 4) if initial else 0.0

    # ---- 5. 平均持仓时长（天） + 多头时间占比 ----
    hold_spans = []
    for sym, start in first_buy.items():
        end = last_hold.get(sym, days[-1] if days else start)
        hold_spans.append((datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1)
    out["avg_hold_days"] = round(sum(hold_spans) / len(hold_spans), 1) if hold_spans else None
    out["position_time_ratio"] = round(len(held_days) / len(days), 4) if days else None

    return out
