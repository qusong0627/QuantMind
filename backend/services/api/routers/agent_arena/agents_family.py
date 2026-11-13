"""arena 数据面 · 族 1：agent 台账 / 总控聚合 / 行情杂项（移植自隔壁 api_server.py）。

逐端点移植上游同名 handler（`/api/*` → `/api/v1/agent-arena/*`），响应形状与
上游逐字段一致——前端 `arena/api/client.ts` 的类型定义即契约，改形状先改契约。

与上游的三处**有意偏离**（各有注释）：
1. `/overview` 的 `tdx` 段：arena 的实盘分析管线（live_hourly_analysis）未迁移，
   恒报「关」；A4 接 QM 桥健康后填真实值；
2. `/metrics` 的服务探活：上游探 mcp_us/mcp_cn/mcp_hk/dsh（全随退役消失），
   改为探 QM 数据面四服务端口（UI 动态渲染键名，未知键原样显示）；
3. 缓存装饰器包在**无鉴权参数的私有实现**上（带上游原样会把 `Depends` 注入的
   用户 dict 塞进缓存 key——dict 不可哈希且没必要按用户分区）。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.shared.database_manager_v2 import get_session
from backend.services.agent_arena import agent_data, trade_store
from backend.services.agent_arena.arena_config import (
    data_root,
    get_data_root,
    load_arena_config,
)
from backend.services.agent_arena.ttl_cache import ttl_cache
from backend.services.api.user_app.middleware.auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

#: 热接口 TTL（照抄上游：overview 最重 30s、prices 轮询最频 10s）
_TTL_OVERVIEW_S = 30
_TTL_PRICES_S = 10

#: /metrics 服务探活目标：QM 数据面自己的四个服务端口（all 模式同进程监听）
_SERVICE_PROBES = {"api": 8000, "engine": 8001, "trade": 8002, "stream": 8003}

#: 北京时区（瞬时列一律 aware UTC 入库，展示按北京墙钟）
_BJ_TZ = timezone(timedelta(hours=8))


def _lf():
    from backend.services.agent_arena import live_family as live_family_module

    return live_family_module


def _quote_redis():
    """行情源 Redis 客户端（与实况族共用同一进程级缓存；不可用 → None）。"""
    from backend.services.api.routers.agent_arena.live_family import _redis_or_none

    return _redis_or_none()


def _to_bj(value: Any) -> datetime | None:
    """任意时间戳 → 北京时区 aware datetime（naive 按 UTC——PG 瞬时列恒 UTC）。"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_BJ_TZ)


# ---------- agent 台账 ----------

@router.get("/agents")
def list_agents(
    market: str = Query("us"),
    _current_user: dict = Depends(get_current_user),
):
    cfg = load_arena_config()
    return {"success": True, "data": agent_data.list_agents(cfg, market)}


@router.get("/agents/{agent}/positions")
async def get_positions(
    agent: str,
    market: str = Query("us"),
    limit: int = Query(200, ge=1, le=5000),
    _current_user: dict = Depends(get_current_user),
):
    """逐日持仓快照。cn = PG 实盘分账重建（与净值曲线同源）。"""
    if market == "cn":
        async with get_session(read_only=True) as session:
            records = await _lf().load_position_records_pg(
                session, agent, redis_client=_quote_redis()
            )
        if not records:
            raise HTTPException(status_code=404, detail=f"Agent 无持仓记录: {agent}")
        return {"success": True, "data": records[-limit:]}
    cfg = load_arena_config()
    records = agent_data.load_position_records(cfg, agent, market, limit)
    if not records:
        raise HTTPException(status_code=404, detail=f"Agent 无持仓记录: {agent}")
    return {"success": True, "data": records}


async def _cn_trades_payload(agent: str, limit: int) -> list[dict]:
    """CN 实盘成交流水：PG 分账 fill（仅 ``applied_volume>0``）+ 成交后现金。

    现金列与净值曲线同一套反推锚定算术（``annotate_fills_cash``），两条链
    不会各算各的。最新在前，形状对齐前端 ``TradeRecord``。
    """
    lf = _lf()
    async with get_session(read_only=True) as session:
        ledger = await lf.read_pg_ledger(session, agents=[agent])
        rec = (ledger.get("agents") or {}).get(agent) or {}
        fills = (await lf.load_agent_fills_pg(session, [agent])).get(agent, [])
        rows = lf.annotate_fills_cash(
            fills,
            end_cash=float(rec.get("virtual_cash") or 0.0),
            end_positions={
                code: float((pos or {}).get("volume") or 0.0)
                for code, pos in (rec.get("positions") or {}).items()
                if isinstance(pos, dict)
            },
        )
    out: list[dict] = []
    for fill in rows[-limit:][::-1]:
        ts = _to_bj(fill.get("ts"))
        price = float(fill["price"])
        volume = float(fill["volume"])
        out.append(
            {
                "date": fill["day"].isoformat(),
                "ts": ts.isoformat() if ts else None,
                "action": fill["side"],
                "symbol": fill["code"],
                "amount": int(volume),
                "price": round(price, 4),
                "notional": round(volume * price, 2),
                "cash_after": fill.get("cash_after"),
                "approx_price": bool(fill.get("approx_price")),
                "live": True,
            }
        )
    return out


@router.get("/agents/{agent}/trades")
async def get_trades(
    agent: str,
    market: str = Query("us"),
    limit: int = Query(200, ge=1, le=5000),
    _current_user: dict = Depends(get_current_user),
):
    if market == "cn":
        return {"success": True, "data": await _cn_trades_payload(agent, limit)}
    cfg = load_arena_config()
    trades = trade_store.query_trades(cfg, market, agent, limit)
    if not trades:
        # 缓存未命中时回退 JSONL 直读（保证一致性）
        trades = agent_data.load_trades(cfg, agent, market, limit)
    # 补 price/notional（价格文件重算 + 滑点模型，与 FIFO 平仓口径一致）
    return {
        "success": True,
        "data": agent_data.enrich_trades_with_prices(cfg, market, trades),
    }


@router.get("/agents/{agent}/performance")
async def get_performance(
    agent: str,
    market: str = Query("us"),
    _current_user: dict = Depends(get_current_user),
):
    """净值曲线 + 统计。cn = PG 实盘分账（scope=live，全部指标按实盘口径）。"""
    if market == "cn":
        async with get_session(read_only=True) as session:
            data = await _lf().build_agent_performance_pg(
                session, agent, redis_client=_quote_redis()
            )
        if data is None:
            raise HTTPException(status_code=404, detail=f"Agent 无净值数据: {agent}")
        return {"success": True, "data": data}
    cfg = load_arena_config()
    series = agent_data.compute_equity_series(cfg, agent, market)
    if not series:
        raise HTTPException(status_code=404, detail=f"Agent 无净值数据: {agent}")
    equity_values = [p["equity"] for p in series]
    peak = -float("inf")
    max_drawdown = 0.0
    for v in equity_values:
        peak = max(peak, v)
        if peak > 0:
            max_drawdown = min(max_drawdown, (v - peak) / peak)
    first, last = equity_values[0], equity_values[-1]
    total_return = (last - first) / first if first else 0.0
    records = agent_data.load_position_records(cfg, agent, market)
    extended = agent_data.compute_extended_summary(series, records, cfg, market)
    return {
        "success": True,
        "data": {
            "agent": agent,
            "points": series,
            "summary": {
                "start_equity": first,
                "end_equity": last,
                "total_return": round(total_return, 6),
                "max_drawdown": round(abs(max_drawdown), 6),
                "records": len(series),
                **extended,
            },
        },
    }


def _roundtrip_row(row: dict) -> dict:
    """回合台账行 → 前端 ``ClosedTradeDetail``（最新在前由查询排序保证）。

    ``fee`` 账本里不存在 → null（显示「—」）；``partial=true`` 表示减仓，
    不是清仓——前端据此区分「已平仓」与「部分卖出」。
    """
    qty = int(float(row.get("volume") or 0))
    exit_price = float(row.get("sell_price") or 0.0)
    pnl = row.get("realized_pnl")
    buy_ts = _to_bj(row.get("buy_ts"))
    sell_ts = _to_bj(row.get("sell_ts"))
    hold_days = row.get("holding_days")
    return {
        "symbol": row.get("code"),
        "exit_date": sell_ts.strftime("%Y-%m-%d") if sell_ts else "",
        "entry_date": buy_ts.strftime("%Y-%m-%d") if buy_ts else None,
        "qty": qty,
        "entry_price": round(float(row.get("cost_price") or 0.0), 4),
        "exit_price": round(exit_price, 4),
        "notional": round(qty * exit_price, 2),
        "fee": None,
        "pnl": round(float(pnl), 2) if pnl is not None else None,
        "hold_days": round(float(hold_days), 1) if hold_days is not None else None,
        "exit_reason": row.get("exit_reason"),
        "live": True,
        "partial": not bool(row.get("closed")),
    }


@router.get("/agents/{agent}/trade-detail")
async def get_trade_detail(
    agent: str,
    market: str = Query("us"),
    limit: int = Query(25, ge=1, le=200),
    _current_user: dict = Depends(get_current_user),
):
    """已平仓逐笔明细（LAST 25 TRADES 表用），最新平仓在前。

    cn = PG 回合台账（``qm_agent_ledger_roundtrip``）：卖出即一行，含减仓
    （``partial``）；文件名下 FIFO 重建仅覆盖旧栈历史，迁移后成交不再入文件。
    """
    if market == "cn":
        async with get_session(read_only=True) as session:
            rows = await _lf().load_agent_roundtrips_pg(session, agent)
        return {"success": True, "data": [_roundtrip_row(r) for r in rows[:limit]]}
    cfg = load_arena_config()
    records = agent_data.load_position_records(cfg, agent, market, limit=5000)
    closed, _, _ = agent_data.rebuild_closed_trades(cfg, market, records)
    closed.sort(key=lambda t: t["exit_date"], reverse=True)
    return {"success": True, "data": closed[:limit]}


@router.get("/agents/{agent}/holdings")
async def get_holdings(
    agent: str,
    market: str = Query("us"),
    _current_user: dict = Depends(get_current_user),
):
    """当前持仓明细：数量/成本/最新价/市值/浮动盈亏/占比（含现金）。"""
    if market == "cn":
        async with get_session(read_only=True) as session:
            data = await _lf().agent_holdings_pg(
                session, agent, redis_client=_quote_redis()
            )
        if data is None:
            raise HTTPException(status_code=404, detail=f"Agent 无持仓记录: {agent}")
        return {"success": True, "data": data}
    cfg = load_arena_config()
    records = agent_data.load_position_records(cfg, agent, market, limit=5000)
    quotes = agent_data.load_latest_prices(cfg, market)
    return {
        "success": True,
        "data": agent_data.compute_holdings(cfg, market, records, quotes),
    }


@router.get("/agents/{agent}/logs")
def get_logs(
    agent: str,
    market: str = Query("us"),
    date: str | None = None,
    limit: int = Query(0, ge=0, le=2000),
    _current_user: dict = Depends(get_current_user),
):
    """agent 分析日志（每行 = 一个分析回合）。limit>0 只回最近 N 条——
    全量日志可累积到 1MB+，前端轮询用 limit 收窄（2026-09-08 卡顿治理）。"""
    cfg = load_arena_config()
    lines = agent_data.load_agent_logs(cfg, agent, market, date, limit or None)
    return {"success": True, "data": lines}


# ---------- 最新价格（Live 滚动价格条） ----------

@ttl_cache(_TTL_PRICES_S)
def _prices_payload(market: str) -> dict:
    cfg = load_arena_config()
    return {"success": True, "data": agent_data.load_latest_prices(cfg, market)}


@router.get("/prices")
def get_latest_prices(
    market: str = Query("us"),
    _current_user: dict = Depends(get_current_user),
):
    return _prices_payload(market)


# ---------- 股票中文名 ----------

_QUANTDB_NAMES: dict | None = None


def _quantdb_stock_names() -> dict:
    """quantdb instrument_detail 全市场 Symbol→Name（duckdb,一次性缓存）。

    上游版本探三个候选路径（/data/quantdb、QM_QUANTDB_DATA_DIR、隔壁宿主路径）；
    QM 落点把**真数据根**放第一位（容器 /data、宿主符号链接后的同一份），
    env 与 /data 兜底保留（换机与测试场景）。
    """
    global _QUANTDB_NAMES
    if _QUANTDB_NAMES is not None:
        return _QUANTDB_NAMES
    found: dict = {}
    try:
        import duckdb

        candidates = [data_root() / "quantdb"]
        env_dir = os.getenv("QM_QUANTDB_DATA_DIR", "")
        if env_dir:
            candidates.append(Path(env_dir))
        candidates.append(Path("/data/quantdb"))
        seen: set[Path] = set()
        for _root in candidates:
            if _root in seen:
                continue
            seen.add(_root)
            detail = (
                _root
                / "2_base_sector/instrument_detail/instrument_detail.parquet"
            )
            if not detail.is_file():
                continue
            _con = duckdb.connect()
            try:
                for sym, nm in _con.execute(
                    "SELECT Symbol, Name FROM read_parquet(?)", [str(detail)]
                ).fetchall():
                    if sym and nm:
                        found.setdefault(sym, nm)
            finally:
                _con.close()
            break
    except Exception:  # noqa: BLE001 - 名称表是增强项，取不到就空表（前端回退代码显示）
        pass
    _QUANTDB_NAMES = found
    return found


@router.get("/stock-names")
def get_stock_names(
    market: str = Query("us"),
    _current_user: dict = Depends(get_current_user),
):
    from backend.services.agent_arena.stock_names import (
        CN_STOCK_NAMES,
        HK_STOCK_NAMES,
        US_STOCK_NAMES,
    )

    table = {"cn": CN_STOCK_NAMES, "hk": HK_STOCK_NAMES, "us": US_STOCK_NAMES}.get(
        market, {}
    )
    if market == "cn":
        table = {**dict(table), **_quantdb_stock_names()}
    return {"success": True, "data": table}


# ---------- 每 agent LLM token 消耗 ----------

@router.get("/token-usage")
def token_usage(_current_user: dict = Depends(get_current_user)):
    """每 agent 实盘 LLM 分析累计 token 消耗。
    统计 data/agent_data_astock/*/log/**/log.jsonl 中带 usage 字段的条目
    （2026-10-14 起写者 = 决策轮 record_round_dialogue 与 15:30 复盘脚本
    live_model_analysis.py → 天然排除模拟盘回放日志）。"""
    from backend.services.agent_arena.text_io import read_text_lossy

    root = data_root() / "agent_data_astock"
    agents: dict = {}
    if root.is_dir():
        for logf in sorted(root.glob("*/log/**/log.jsonl")):
            agent = logf.parts[-4]  # .../agent_data_astock/{agent}/log/{date}/log.jsonl
            acc = agents.setdefault(
                agent,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "estimated": 0,
                    "last_ts": None,
                },
            )
            try:
                text = read_text_lossy(logf)
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = r.get("usage")
                if not isinstance(u, dict):
                    continue
                acc["calls"] += 1
                acc["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                acc["completion_tokens"] += int(u.get("completion_tokens") or 0)
                acc["total_tokens"] += int(u.get("total_tokens") or 0)
                if u.get("usage_est"):
                    acc["estimated"] += 1
                ts = r.get("timestamp") or r.get("ts")
                if ts and (not acc["last_ts"] or ts > acc["last_ts"]):
                    acc["last_ts"] = ts
    return {"success": True, "data": {"agents": agents}}


# ---------- 总控聚合 ----------

def _tdx_live_status() -> dict:
    """通达信桥实盘状态（`/overview` 的 `tdx` 段，前端据此显示「实盘 开/关」）。

    A2 阶段恒为「关」——它反映的是 **arena 自己的实盘分析管线**
    （live_hourly_analysis → 桥下单）是否在跑，该管线未迁移、随上游退役；
    A4 接 QM 桥客户端（/api/v1/tdx/*）后按真实健康回报填充。这不是占位假值：
    对「arena 的自动分析轮」而言「关」就是当下的事实。
    """
    return {"real_trading_enabled": False, "health": None, "enabled": False}


@ttl_cache(_TTL_OVERVIEW_S)
def _overview_payload() -> dict:
    cfg = load_arena_config()
    markets = {}
    for market in ("us", "cn", "hk"):
        agents = agent_data.list_agents(cfg, market)
        rows = []
        for a in agents:
            series = agent_data.compute_equity_series(cfg, a["name"], market)
            summary = None
            if series:
                vals = [p["equity"] for p in series]
                peak = -float("inf")
                dd = 0.0
                for v in vals:
                    peak = max(peak, v)
                    if peak > 0:
                        dd = min(dd, (v - peak) / peak)
                first, last = vals[0], vals[-1]
                records = agent_data.load_position_records(cfg, a["name"], market)
                summary = {
                    "start_equity": first,
                    "end_equity": last,
                    "total_return": round((last - first) / first, 6) if first else 0.0,
                    "max_drawdown": round(abs(dd), 6),
                }
                summary.update(
                    agent_data.compute_extended_summary(series, records, cfg, market)
                )
            rows.append(
                {
                    "name": a["name"],
                    "latest_date": a["latest_date"],
                    "records": a["total_records"],
                    "cash": a["cash"],
                    "summary": summary,
                }
            )
        markets[market] = rows
    return {"markets": markets, "tdx": _tdx_live_status()}


async def _cn_overview_rows() -> list[dict]:
    """A 股总控行：只认 PG 分账账本（与 /performance、模型详情同源）。

    ``/overview`` 的其余市场仍读文件回放；A 股若继续读文件，总控的收益/权益
    会和模型详情（PG）对不上——2026-10-20 用户口径「都是按实盘来」。
    """
    lf = _lf()
    # PG 只是 A 股这一段的数据源：读失败时降级为空表并告警，
    # 不能把 hk/us（文件回放）和整页总控一起 500 掉（2026-10-20 审查 HIGH）。
    try:
        async with get_session(read_only=True) as session:
            ledger = await lf.read_pg_ledger(session)
            # 活账账户 = A 股实盘名册；文件目录里的历史模型（fake-model 等）没有
            # 分账账户，不该出现在总控（否则会渲染成 -100% 的空账户卡片）
            names = sorted(ledger.get("agents") or {})
            rows: list[dict] = []
            for name in names:
                perf = await lf.build_agent_performance_pg(
                    session, name, redis_client=_quote_redis()
                )
                if perf is None:
                    rows.append(
                        {
                            "name": name,
                            "latest_date": None,
                            "records": 0,
                            "cash": None,
                            "summary": None,
                        }
                    )
                    continue
                points = perf.get("points") or []
                rows.append(
                    {
                        "name": name,
                        "latest_date": (
                            str(points[-1].get("date"))[:10] if points else None
                        ),
                        "records": perf["summary"].get("records", 0),
                        "cash": (
                            _float_or_none(points[-1].get("cash")) if points else None
                        ),
                        "summary": perf["summary"],
                    }
                )
    except Exception as exc:  # noqa: BLE001 — 见上：只降级 cn 段
        logger.warning("arena /overview: A股 PG 账本读取失败，cn 段降级为空: %s", exc)
        return []
    return rows


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("/overview")
async def get_overview(_current_user: dict = Depends(get_current_user)):
    """总控台聚合：三市场 × agent（净值/收益/风控/记忆/运行状态）。

    A 股段改为 PG 分账实盘口径（异步补算）；港美股沿用文件回放的 30s 缓存。
    """
    payload = _overview_payload()
    payload["markets"]["cn"] = await _cn_overview_rows()
    return {"success": True, "data": payload}


@router.get("/metrics")
def get_metrics(_current_user: dict = Depends(get_current_user)):
    """聚合指标：服务健康 / agent / 交易 / 记忆。（服务探活口径见模块 docstring）"""
    cfg = load_arena_config()
    root = get_data_root(cfg)
    now = time.time()
    metrics: dict[str, Any] = {"services": {}, "markets": {}}

    for name, port in _SERVICE_PROBES.items():
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.close()
            metrics["services"][name] = "up"
        except OSError:
            metrics["services"][name] = "down"

    market_dirs = {
        m: root / mcfg.get("data_dir", f"agent_data_{m}")
        for m, mcfg in cfg.get("markets", {}).items()
    }
    for market in ("us", "cn", "hk"):
        agents = agent_data.list_agents(cfg, market)
        data_dir = market_dirs.get(market, root / "agent_data")
        trades = 0
        for a in agents:
            trades += a.get("total_records", 0)
        memory_file = data_dir / "market_memory.md"
        memory_lines = 0
        if memory_file.exists():
            try:
                memory_lines = len(memory_file.read_text(encoding="utf-8").splitlines())
            except OSError:
                pass
        metrics["markets"][market] = {
            "agents": len(agents),
            "position_records": trades,
            "memory_lines": memory_lines,
        }

    # 最近交易时间（任一市场最新 position mtime）
    latest_trade_ts = 0
    for market in ("us", "cn", "hk"):
        d = market_dirs.get(market)
        if d and d.exists():
            for pf in d.glob("*/position/position.jsonl"):
                try:
                    latest_trade_ts = max(latest_trade_ts, pf.stat().st_mtime)
                except OSError:
                    pass
    metrics["latest_trade_age_sec"] = int(now - latest_trade_ts) if latest_trade_ts else None
    metrics["generated_at"] = int(now)
    return {"success": True, "data": metrics}
