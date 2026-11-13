# ── 移植注（2026-09-30，arena 原生迁移 A4）─────────────────────────────
# 来源：quant-Trader `backend/api_server.py` L374–1731（/api/live/* 家族，上游已退役）。
# 逻辑与输出形状逐字节对照上游；数据源换成 QM 原生。改写点全记账：
#   ① 落点：arena 数据根 /logs，走既有唯一出处 `arena_config.logs_dir()`
#      （上游是 `Path(__file__).parents[1]/"logs"` 模块常量；A1 已迁 37 个 live_* 文件）。
#   ② real-account / real-accounts / real-ledger / l2-factors：上游用 psycopg2 直读
#      *QM 的 PG*（QM_PG_* env，见上游 L906 注释）；QM 内改原生 async SQLAlchemy
#      （database_manager_v2.get_session），SQL 照抄 + tenant/user 过滤。
#   ③ 账户/用户键：上游硬编码 `tdx-default-00000001`（QM 2026-09-18 用户 id 规范化
#      前的旧键，已停更）→ 现役规范：resolve_db_account_user + ledger_user_id_candidates
#      （跨别名）/ account_family（跨改名，见 real_account_ledger_service）。
#   ④ /live/account：桥查询改 QM `settings.TDX_BRIDGE_URL`（与实盘执行链同一座桥）
#      + Bearer；asset **拍平为 number**（上游把 asset dict 直接塞给前端、前端按
#      number 渲染——显示 bug 不留）；持仓补价 Redis `market:snapshot:*` 标准键
#      优先，缺价（盘外/午休键过期）回桥直查（2026-10-08，上游逐只 broker.get_quote
#      语义的等价恢复）→ 都失败才 0 降级。
#   ⑤ /live/trades 的「桥当日委托补价」段：QM 桥 orders/query 形状与上游桥不同，
#      **deferred**（上游该段异常本就 pass 降级，留注待映射；不迁桥价不假造）。
#   ⑥ /live/ledger：主源 = QM PG 活账（agent_ledger_store.load_ledger，形状与
#      live_ledger.json agents 段逐字一致）；文件仅作读败兜底（退役快照）。
#   ⑦ /live/news：进程内 HTTP 打本机 /api/v1/news/articles（上游即 HTTP 代理到 QM，
#      NEWS_API_BASE 默认指上游 Docker 网桥网关（172.17 网段）；同容器内收窄为 127.0.0.1:8000）。
#   ⑧ /live/indices cn：上游桥日K两根 → QM Redis `market:snapshot:*` 标准键
#      （decision_context_source 同款键与涨跌算法）；文件兜底同上游。
#   ⑨ real-ledger 复用 `list_real_account_daily_ledgers_by_family`——改名断头
#      （00000001→10000001）与用户别名的坑现役已有唯一解，不重复实现。
#   ⑩ 时间口径：快照表 `snapshot_at` 是 naive UTC（[[real-account-snapshots-naive-utc]]），
#      age_sec 用 utc_now() 对齐（上游 datetime.now() 本地墙钟会算错 8 小时）；
#      ts 一律 to_utc_iso（Z 后缀，QM 现行口径）。
#   ⑪ /live/trades 第二数据源（2026-10-09）：文件写入者随 quant-Trader 退役冻结于
#      09-29，迁移后的真单成交只落 PG `trades`——`load_real_fills_pg` 读 PG（trades
#      ⨝ orders 取 agent），`merge_live_trade_rows` 按桥委托号与文件行合并去重。
# 消费者：backend/services/api/routers/agent_arena/live_family.py（本包路由层）。
# ─────────────────────────────────────────────────────────────────────
"""实况家族（/api/live/*）服务层：账户/持仓/分账/成交/净值/指数/新闻。"""
from __future__ import annotations

import asyncio
import json
import statistics
import time as _time
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text

from backend.shared.trading_policy import load_trading_policy
from backend.shared.decision_context_source import day_change_pct, read_snapshots
from backend.shared.utc_datetime import as_utc, to_utc_iso, utc_now
from backend.services.agent_arena import arena_config, market_lab
from backend.services.agent_arena.text_io import read_text_lossy

#: 实盘两通道（照抄上游 REAL_ACCOUNTS 的 label/channel；「BayMax」字样不迁）。
REAL_ACCOUNTS: dict[str, dict[str, str]] = {
    "tdx": {"label": "通达信桥", "channel": "通达信（实盘执行通道）"},
    "qmt": {"label": "迅投 QMT", "channel": "迅投 QMT（账户只读观测）"},
}

#: 决策轮账户 user 的唯一 env 名（与 decision_round_core.ENV_ACCOUNT_USER 同源口径）。
ENV_ACCOUNT_USER = "QM_DECISION_ACCOUNT_USER_ID"

#: A 股主流指数（通达信代码；上证用 000001.SH，999999 该源不支持）——照抄上游。
INDEX_DEFS: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("000016.SH", "上证50"),
    ("000300.SH", "沪深300"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
)

#: 全球指数（腾讯 qt.gtimg.cn）——照抄上游。
TENCENT_INDICES: dict[str, tuple[tuple[str, str], ...]] = {
    "us": (("usDJI", "道琼斯"), ("usIXIC", "纳斯达克"), ("usINX", "标普500")),
    "hk": (("hkHSI", "恒生指数"), ("hkHSCEI", "国企指数"), ("hkHSTECH", "恒生科技")),
}


# ---------- 工具 ----------

def _snap_last(snap: Any) -> float:
    """快照 → 最新价（兼容 ``Now``/``now`` 两套字段名，与 day_change_pct 同宽容）。

    取不到 → 0（同上游 quote 失败语义）。
    """
    if not isinstance(snap, dict):
        return 0.0
    raw = snap.get("Now") if snap.get("Now") is not None else snap.get("now")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


# ---------- 落点 ----------

def _account_key(account: str, user_id: str) -> str:
    """通道 key → 现役账户键（`tdx-default-10000001` 形态）。未知 key 抛 ValueError。"""
    if account not in REAL_ACCOUNTS:
        raise ValueError(
            f"未知实盘账户通道 {account!r}，可选：{', '.join(REAL_ACCOUNTS)}"
        )
    return f"{account}-default-{user_id}"


def _resolve_account_user() -> str:
    """实盘账户 user_id（规范名 10000001；env 老口径经 normalize 收口）。"""
    from backend.shared.simulation_account_keys import resolve_db_account_user

    return resolve_db_account_user(ENV_ACCOUNT_USER)


def _account_family(account: str) -> str:
    """通道 key → 账户**家族**（跨 00000001→10000001 改名；口径唯一出处）。"""
    from backend.services.trade.services.real_account_ledger_service import (
        account_family,
    )

    return account_family(_account_key(account, _resolve_account_user()))


def _alias_users() -> list[str]:
    """快照/台账查询的 user 别名族（与档位生产者同源）。"""
    from backend.shared.simulation_account_keys import ledger_user_id_candidates

    return ledger_user_id_candidates(_resolve_account_user())


# ---------- 文件类（上游逻辑逐字节） ----------

def load_equity() -> dict:
    """实盘净值点（logs/live_equity.jsonl）：total + 每 agent 分账，按 ts 时间序。"""
    f = arena_config.logs_dir() / "live_equity.jsonl"
    total: list = []
    agents: dict = {}
    try:
        text = read_text_lossy(f)
    except OSError:
        return {"total": total, "agents": agents}
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        v = r.get("value")
        if v is None:
            continue
        # source 必须原样带出：merge_live_equity 只认 "source=pg" 的当日点作
        # 盘中尾巴；丢了它，当天曲线会退化成 PG 单点（2026-10-20 线上故障）。
        pt = {
            "date": r.get("date"),
            "ts": r.get("ts"),
            "value": float(v),
            "source": r.get("source"),
        }
        agent = r.get("agent")
        if agent:
            agents.setdefault(agent, []).append(pt)
        else:
            total.append(pt)
    total.sort(key=lambda p: str(p.get("ts", "")))
    for pts in agents.values():
        pts.sort(key=lambda p: str(p.get("ts", "")))
    return {"total": total, "agents": agents}


def load_trades(limit: int = 200) -> list[dict]:
    """实盘交易记录（logs/live_trade_*.jsonl 买卖行，最新在前）。

    改写点⑤：上游尾段用桥当日委托补 filled_price 与 side（异常 pass 降级）——
    QM 桥 orders/query 形状待映射，本版不迁补价；无价行按上游同款置 None。
    """
    if not isinstance(limit, int):  # 直接函数调用（绕过 FastAPI 参数解析）兜底
        limit = 200
    records: list[dict] = []
    for f in sorted(arena_config.logs_dir().glob("live_trade_*.jsonl")):
        try:
            text = read_text_lossy(f)
        except OSError:
            continue
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("mode") in ("execute", "execute_intraday", "sell",
                                   "fill_confirm", "fill_adjust") and (
                "result" in rec or "error" in rec or "fill" in rec
                or "pending" in rec or rec.get("mode") == "fill_confirm"
                or rec.get("note")  # 对账行（fill_adjust）只带 note，无成交/委托字段
            ):
                # fill_confirm（reconcile 兜底确认的成交）没有嵌套 result/fill，
                # 平铺 volume/price——补一层 fill 让前端 hasFill 判定放行
                #（2026-09-03：强平守护 13:00 的成交确认因此没进「成交」页）
                if rec.get("mode") == "fill_confirm" and not rec.get("fill"):
                    rec["fill"] = {"order_id": rec.get("order_id"),
                                   "filled_price": rec.get("price"),
                                   "filled_volume": rec.get("volume")}
                records.append(rec)
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    # 上游此处用桥当日委托 filled_price 回填（按 order_id / 回退 code）——QM 桥
    # orders/query 形状待映射（改写点⑤）；桥不可用时上游走 `except: pass` 后的
    # 同一循环：价格缺失行显式置 None。本实现即该分支的逐字对应。
    for rec in records:
        if rec.get("mode") == "fill_adjust":
            continue  # 对账行价格是台账口径（当时成交价），不是当日行情，不参与回填
        if not rec.get("price"):
            rec["price"] = None
    return records[:limit]


def _file_bridge_order_id(rec: dict) -> str | None:
    """文件成交行的桥委托号（TDX 委托编号空间，与 PG orders.exchange_order_id 同源）。"""
    for cand in (
        rec.get("order_id"),
        (rec.get("fill") or {}).get("order_id"),
        (rec.get("result") or {}).get("order_id"),
    ):
        if cand not in (None, ""):
            return str(cand)
    return None


def merge_live_trade_rows(file_rows: list[dict], pg_rows: list[dict]) -> list[dict]:
    """上游文件行 + PG 真单成交行合并（最新在前）。

    去重判据 = **桥委托号**：文件行 `order_id`/`fill.order_id`/`result.order_id`
    与 PG 行 `fill.order_id`（= orders.exchange_order_id）同为 TDX 委托编号空间。
    桥单在文件里已记过（quant-Trader 时代 arena 自己的账）→ 丢 PG 行、文件行原样
    保留（两源重叠期 2026-09-10~09-24 不重不漏）；文件里没有的桥单（强平/守护
    直连链路的成交，以及 2026-09-29 迁移后的全部新成交）→ PG 行进入展示。
    """
    file_ids = {bid for r in file_rows if (bid := _file_bridge_order_id(r))}
    out = list(file_rows)
    for r in pg_rows:
        bid = (r.get("fill") or {}).get("order_id")
        if bid and str(bid) in file_ids:
            continue
        out.append(r)
    out.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return out


#: PG 真单成交并入展示的起始时刻（arena 文件冻结于 2026-09-29 迁移夜）。
#: naive 北京墙钟，与 trades.executed_at 同钟（直接入 SQL 参数比较）。
#: 切点**之前**的 74 行 PG 成交经逐单核对全是镜像/守护**测试残留**
#: （client_order_id `mir-qm-mirror-e2e*` / `mir-stress-20260910-*` 与 09-11
#: sltp「一定要卖」实测单，QMT 通道），不是模型交易——不入「成交」展示。
PG_FILLS_CUTOVER = datetime(2026, 9, 30)


async def load_real_fills_pg(session: Any, limit: int = 200) -> list[dict]:
    """本仓 PG 真单成交（trades ⨝ orders），输出与文件行同形的记录。

    2026-10-09 起 `/live/trades` 的第二数据源：arena 的文件写入者随 quant-Trader
    退役停摆（live_trade_*.jsonl 冻结于 2026-09-29），迁移后的真单成交（决策轮/
    守护/手动）只落 PG `trades`；本函数把 PG 行翻译成上游记录形状，由
    `merge_live_trade_rows` 与文件行合并去重。

    - 过滤：tenant=default、user ∈ 别名族（00000001→10000001 改名兼容）、
      A 股后缀码（.SH/.SZ/.BJ；上游文件本就是 A 股口径）、
      ``executed_at >= PG_FILLS_CUTOVER``（迁移切点，见上）。
    - 时间：``executed_at`` 存量与 execution_stream_consumer 落库同为 **naive
      北京时间**（Trade 模型 default=datetime.now 容器本地钟；实测填单小时分布
      与沪深盘口一致），输出直接附 ``+08:00``，与上游 ts 逐字同形。
    - ``fill.order_id`` = orders.exchange_order_id（TDX 委托编号），跨源去重键。
    """
    if not isinstance(limit, int):
        limit = 200
    stmt = text(
        """
        SELECT t.order_id, t.symbol, t.side, t.quantity, t.price, t.executed_at,
               o.agent, o.status, o.exchange_order_id
        FROM trades t
        LEFT JOIN orders o ON o.order_id = t.order_id
        WHERE t.tenant_id = :tenant
          AND t.user_id IN :users
          AND t.executed_at >= :cutover
          AND (t.symbol LIKE '%.SH' OR t.symbol LIKE '%.SZ' OR t.symbol LIKE '%.BJ')
        ORDER BY t.executed_at DESC
        LIMIT :limit
        """
    ).bindparams(bindparam("users", expanding=True))
    result = await session.execute(
        stmt,
        {
            "tenant": "default",
            "users": _alias_users(),
            "cutover": PG_FILLS_CUTOVER,
            "limit": limit,
        },
    )
    out: list[dict] = []
    for r in result.mappings():
        ts = r["executed_at"]
        qty = int(r["quantity"] or 0)
        px = None if r["price"] is None else float(r["price"])
        bid = None if r["exchange_order_id"] in (None, "") else str(r["exchange_order_id"])
        out.append({
            "ts": "" if ts is None else ts.isoformat() + "+08:00",
            "mode": "qm_fill",
            "source": "qm_pg",
            "agent": r["agent"],
            "code": r["symbol"],
            "side": str(r["side"] or "").lower(),
            "volume": qty,
            "price": px,
            "fill": {"order_id": bid, "filled_price": px, "filled_volume": qty},
            "result": {"order_id": bid, "status": str(r["status"] or "")},
        })
    return out


def _settle_closed_entry(row: dict, pend: dict) -> None:
    """把倒走途中归因到的买入日写回 close 行（entry_date/hold_days）。

    没找到买点（日志中途才开始、买入早于记录）→ 两字段留 None，
    前端显示「—」；绝不臆造日期。
    """
    entry_ts = pend.get("entry")
    if not entry_ts:
        return
    row["entry_date"] = str(entry_ts)[:10]
    try:
        t0 = datetime.fromisoformat(str(entry_ts))
        t1 = datetime.fromisoformat(str(row["ts"]))
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        if t1.tzinfo is None:
            t1 = t1.replace(tzinfo=timezone.utc)
        row["hold_days"] = round((t1 - t0).total_seconds() / 86400, 2)
    except ValueError:
        pass


def load_closed(limit: int = 60, ledger: dict | None = None) -> list[dict]:
    """实盘已平仓事件（卖出后该 agent 该股归零）。

    账本只存当前快照，用 fill 成交事件倒走重建：倒走穿越一笔卖出时该 agent
    该股数量已为 0，说明这笔卖出就是清仓。``ledger`` 传「文件形状」账本
    （`{"agents": {…}}`，PG 活账与文件逐字同形）；None 时读 live_ledger.json
    兜底。成本取事件日志自带 cost_price，无成本则 pnl 置 None。

    买入日（entry_date/hold_days，2026-10-15 加）：同一次倒走里对每笔清仓
    挂一条待归因记录——继续往回遇到的买入逐笔冲抵（每越过一笔就覆写
    entry，留下**最早贡献买入**），部分卖出则补记等量买入需求（它卖掉的是
    更早买进的份额）；遇到上一轮清仓（q0==0 的卖出）即到界不再回溯。
    语义 =「该轮持仓从哪天开始」，非 FIFO 逐笔配对。
    """
    if not isinstance(limit, int):
        limit = 60
    events: list = []
    for f in sorted(arena_config.logs_dir().glob("live_trade_*.jsonl")):
        if any(m in f.name for m in ("_us_", "_hk_")):
            continue
        try:
            text = read_text_lossy(f)
        except OSError:
            continue
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("error"):
                continue
            mode = rec.get("mode")
            if mode == "fill_confirm":
                fv = int(rec.get("volume") or 0)
                fp = rec.get("price")
            else:
                fill = rec.get("fill") or {}
                fv = int(fill.get("filled_volume") or 0)
                fp = fill.get("filled_price")
            if fv <= 0 or not rec.get("side"):
                continue
            try:
                price = float(fp or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                continue
            events.append({
                "ts": rec.get("ts") or "",
                "agent": rec.get("agent"),
                "code": rec.get("code"),
                "side": str(rec["side"]).lower(),
                "vol": fv,
                "price": price,
                "cost": rec.get("cost_price"),
            })
    events.sort(key=lambda r: r["ts"], reverse=True)

    # 当前账本 → (agent, code) 数量/成本起点（倒走基准）
    if ledger is None:
        try:
            ledger = json.loads(
                (arena_config.logs_dir() / "live_ledger.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = {"agents": {}}
    qty: dict = {}
    cost_now: dict = {}
    for agent, rec in (ledger.get("agents") or {}).items():
        qty.setdefault(agent, {})
        cost_now.setdefault(agent, {})
        for code, p in (rec.get("positions") or {}).items():
            qty[agent][code] = int(p.get("volume") or 0)
            cost_now[agent][code] = p.get("cost_price")

    closed: list = []
    seen: set = set()
    pending: dict = {}  # (agent, code) → 当前挂起的清仓归因 {need, entry, idx}
    for e in events:
        agent, code = e["agent"], e["code"]
        if not agent or not code:
            continue
        pair = (agent, code)
        aqty = qty.setdefault(agent, {})
        acost = cost_now.setdefault(agent, {})
        q0 = int(aqty.get(code) or 0)
        pend = pending.get(pair)
        if e["side"] == "sell":
            if q0 == 0 and e["price"] > 0:
                # 上一挂起轮次到此为界：更早的买入属于更早的轮次，不再回溯
                if pend:
                    _settle_closed_entry(closed[pend["idx"]], pend)
                    pending.pop(pair, None)
                key = (e["ts"], agent, code)
                if key not in seen:
                    seen.add(key)
                    entry = e["cost"] if e["cost"] is not None else acost.get(code)
                    closed.append({
                        "ts": e["ts"], "agent": agent, "symbol": code,
                        "exit_date": str(e["ts"])[:10], "qty": e["vol"],
                        "entry_price": float(entry or 0),
                        "exit_price": e["price"],
                        "notional": round(e["price"] * e["vol"], 2),
                        "fee": 0.0,
                        "pnl": (round((e["price"] - float(entry)) * e["vol"], 2)
                                if entry not in (None, 0) else None),
                        "entry_date": None,
                        "hold_days": None,
                    })
                    pending[pair] = {"need": e["vol"], "entry": None,
                                     "idx": len(closed) - 1}
            elif pend and pend["need"] > 0:
                # 部分卖出：卖掉的份额是更早买进的 → 倒走需补记等量买入
                pend["need"] += e["vol"]
        elif pend and pend["need"] > 0:
            pend["need"] -= e["vol"]
            pend["entry"] = e["ts"]  # 逐笔覆写 → 收尾留下最早贡献买入
        # 倒走复原：卖出的逆向是加回，买入的逆向是扣减
        if e["side"] == "sell":
            aqty[code] = q0 + e["vol"]
        else:
            aqty[code] = max(0, q0 - e["vol"])
    for pend in pending.values():
        _settle_closed_entry(closed[pend["idx"]], pend)
    closed.sort(key=lambda r: r["ts"], reverse=True)
    # 合并历史补录（旧日志格式无 fill 回报的全仓清仓，价格近似、带 approx 标记；
    # 与实时重建按 symbol+exit_date 去重，实时优先）
    backfill = []
    try:
        for line in read_text_lossy(
                arena_config.logs_dir() / "live_closed_backfill.jsonl").splitlines():
            if not line.strip():
                continue
            try:
                backfill.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    live_keys = {(r["symbol"], r["exit_date"]) for r in closed}
    closed.extend(r for r in backfill
                  if (r.get("symbol"), r.get("exit_date")) not in live_keys)
    closed.sort(key=lambda r: r["ts"], reverse=True)
    return closed[:limit]


# ---------- 已完成（真清仓）：PG 回合台账 + 文件重建 ----------

def roundtrip_to_closed_row(row: Any) -> dict | None:
    """回合台账行 → 「已完成」feed 行；**非清仓（closed=false）返回 None**。

    2026-10-20 用户口径：「已完成」只统计真正清仓的股票，减仓不计入。
    回合台账在每笔卖出时都写一条（``closed = 卖后归零``），减仓行也在表里——
    过滤必须在这里做，不能只靠 SQL（SQL 改了/换源时仍要守住同一条口径）。
    """
    if not row.get("closed"):
        return None
    sell_ts = row.get("sell_ts")
    if sell_ts is None:
        return None
    sell_dt = as_utc(sell_ts)
    buy_ts = row.get("buy_ts")
    buy_dt = as_utc(buy_ts) if buy_ts is not None else None
    qty = float(row.get("volume") or 0)
    entry = float(row.get("cost_price") or 0)
    exit_px = float(row.get("sell_price") or 0)
    pnl = row.get("realized_pnl")
    return {
        "ts": to_utc_iso(sell_dt),
        "agent": row.get("agent"),
        "symbol": row.get("code"),
        # 北京日期：UTC 01:37（北京 09:37）不得截成前一天
        "exit_date": sell_dt.astimezone(_BJ_TZ).strftime("%Y-%m-%d"),
        "qty": qty,
        "entry_price": entry,
        "exit_price": exit_px,
        "notional": round(exit_px * qty, 2),
        "fee": 0.0,
        "pnl": float(pnl) if pnl is not None else None,
        "entry_date": (buy_dt.astimezone(_BJ_TZ).strftime("%Y-%m-%d")
                       if buy_dt is not None else None),
        "hold_days": row.get("holding_days"),
    }


async def load_closed_pg(session: Any, limit: int = 60) -> list[dict]:
    """PG 回合台账 → 已完成 feed（**仅 closed=true 的真清仓**）。

    文件写入者（live_trade_*.jsonl）随 quant-Trader 退役冻结于 2026-09-29；
    迁移后的 agent 清仓只落 ``qm_agent_ledger_roundtrip``。不接这条源，
    「已完成」会永远停在旧快照（2026-10-20 用户指出）。
    """
    if not isinstance(limit, int):
        limit = 60
    stmt = text(
        """
        SELECT agent, code, volume, cost_price, sell_price, realized_pnl,
               buy_ts, sell_ts, holding_days, closed
        FROM qm_agent_ledger_roundtrip
        WHERE tenant_id = :tenant
          AND user_id IN :users
          AND market = 'CN'
          AND closed = TRUE
        ORDER BY sell_ts DESC
        LIMIT :limit
        """
    ).bindparams(bindparam("users", expanding=True))
    result = await session.execute(
        stmt,
        {"tenant": "default", "users": _alias_users(), "limit": limit},
    )
    out: list[dict] = []
    for r in result.mappings():
        row = roundtrip_to_closed_row(dict(r))
        if row is not None:
            out.append(row)
    return out


def _closed_ts_key(row: dict) -> datetime:
    raw = str(row.get("ts") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def merge_closed_rows(
    pg_rows: list[dict] | None,
    file_rows: list[dict] | None,
    limit: int = 60,
) -> list[dict]:
    """PG 回合台账（真清仓，权威）+ 文件重建/补录（历史）；同键去重后倒序。

    两源 ``ts`` 后缀不同（PG 是 ``Z``、文件是 ``+08:00``）：排序必须按解析后的
    时刻比，直接比字符串会把混合源的先后顺序排错。
    """
    if not isinstance(limit, int):
        limit = 60
    out: list[dict] = []
    seen: set = set()
    for r in [*(pg_rows or []), *(file_rows or [])]:
        key = (r.get("agent"), r.get("symbol"), r.get("exit_date"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    out.sort(key=_closed_ts_key, reverse=True)
    return out[:limit]


# ---------- 分账账本（PG 活账主源，文件形状兜底） ----------

async def read_pg_ledger(
    session: Any,
    *,
    tenant_id: str = "default",
    agents: list[str] | None = None,
) -> dict:
    """QM PG 活账（形状与 logs/live_ledger.json 的 agents 段逐字一致）。

    (tenant, user) 身份与决策轮同一处解析（``resolve_db_account_user``）——
    写错一个身份，读到的就是**另一本空账**（seed 导入脚本同款纪律）。
    """
    from backend.shared.agent_ledger_store import load_ledger

    return await load_ledger(
        session,
        tenant_id=tenant_id,
        user_id=_resolve_account_user(),
        agents=agents,
    )


async def read_file_ledger() -> dict:
    """文件账本兜底（退役时快照，A1 已迁 /data/logs/live_ledger.json）。"""
    try:
        return json.loads(
            (arena_config.logs_dir() / "live_ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "agents": {}}


def ledger_view(
    ledger: dict,
    names: dict[str, str],
    prices: dict[str, float] | None = None,
) -> dict:
    """账本文档 → 前端 AgentLedger 形状（quota/used/remaining/positions 数组）。

    2026-10-20 补行情：旧实现只有成本价，``position_value`` 被 Live 页当
    「桥实时价市值」用（注释如此、值却是成本）——持仓估值/占比因此失真。
    现在 ``price``/``market_value``/``pnl`` 用实时快照；缺价时
    ``market_value``/``pnl`` 明确为 ``None``（不拿成本冒充现价），
    ``position_value`` 仅作历史兼容回落到成本。``incomplete`` 标出该 agent
    至少一只持仓缺价。
    """
    policy = load_trading_policy()
    quotes = prices or {}
    out: dict = {}
    for agent, rec in (ledger.get("agents") or {}).items():
        quota = policy.for_agent(agent).quota
        positions = []
        used = 0.0
        market_value = 0.0
        incomplete = False
        for code, p in sorted((rec.get("positions") or {}).items()):
            volume = float(p.get("volume") or 0)
            cost = float(p.get("cost_price") or 0)
            cost_value = volume * cost
            used += cost_value
            px = quotes.get(code)
            px = float(px) if isinstance(px, (int, float)) and px > 0 else None
            mv = round(volume * px, 2) if px is not None else None
            if mv is None:
                incomplete = True
            else:
                market_value += mv
            positions.append({
                "code": code,
                "name": names.get(code, code),
                "volume": int(volume),
                "cost_price": round(cost, 4),
                # 兼容字段：有实时价用市值，缺价才回落成本（Live 页单价估算用）
                "position_value": mv if mv is not None else round(cost_value, 2),
                "price": px,
                "market_value": mv,
                "pnl": round(mv - cost_value, 2) if mv is not None else None,
                "pnl_pct": round(px / cost - 1, 6) if px is not None and cost > 0 else None,
                "buy_ts": _bj_wall(p.get("buy_ts") or ""),
            })
        cash = float(rec.get("virtual_cash") or 0.0)
        out[agent] = {
            "quota": quota,
            "used": round(used, 2),
            "remaining": round(max(quota - used, 0.0), 2),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(cash + market_value, 2) if not incomplete else None,
            "incomplete": incomplete,
            "positions": positions,
        }
    return {"agents": out}


# ---------- 实盘分账净值（PG 活账重建） ----------

#: 旧 ``live_ledger.json`` 的最后有效时刻：09-29 迁移夜后文件冻结。
#: 此前的分钟点仍是当时活账算出（保留）；此后一律用 PG 成交+日线重建。
FILE_EQUITY_CUTOVER = datetime(2026, 9, 30, tzinfo=timezone(timedelta(hours=8)))

#: 接缝一致性容差：旧线末点与新线首点的水平差超过该比例，判定为迁移口径
#: 断层而非行情波动（2026-10-20 实测 pro -17.7%、glm -11.7%，均为期初持仓
#: 未入 PG 账），旧线整段弃用——宁可少一段历史，也不画假断崖。
SEAM_TOLERANCE = 0.05

#: 浮点持仓归零阈值（与账本核心同量级；1e-6 股在任何市场都无意义）。
_EQUITY_EPS = 1e-6

#: 单次请求内重建序列的重算间隔（秒）。轮询 20-30s，取 20s。
_SERIES_CACHE_TTL_S = 20.0

#: 进程内序列缓存：key=(tenant, user, agents, day) → (monotonic_ts, series)
_SERIES_CACHE: dict[tuple, tuple[float, dict[str, list[dict]]]] = {}


def _bj_day(value: Any) -> date | None:
    """PG 时间/日期 → 北京交易日。``trade_date`` 直接是 date；timestamptz 转 UTC+8。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=8))).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_ledger_equity_series(
    fills: list[dict],
    *,
    end_cash: float,
    end_positions: dict[str, float],
    closes: dict[date, dict[str, float]],
    quotes: dict[str, float] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """沿成交历史反推起点、再正向重放，得到逐日分账净值。

    为什么反推：账本只存**当前**现金/持仓，成交流水是完整历史。以当前状态为
    锚点倒放全部成交，得到第一笔成交前的起点（现金 + 持仓），再正向逐日重放；
    曲线终点与账本逐分一致，不依赖任何「初始额度」假设。

    ``fills`` 每项：``day``(date)/``ts``(aware datetime)/``code``(后缀)/``side``
    (buy|sell)/``volume``/``price``。只有 ``applied_volume>0`` 的成交才该传进来。
    ``closes``: ``{交易日: {后缀码: 收盘价}}``；``quotes``: 当日实时价。
    缺价顺序 = 当日实时价（仅今天）→ 当日收盘 → 最近已知价（标
    ``incomplete``，**绝不**拿成本价冒充现价）→ 0。
    """
    tz_bj = timezone(timedelta(hours=8))
    today = today or datetime.now(tz_bj).date()
    now = now or datetime.now(tz_bj)

    clean: list[dict] = []
    for f in fills or []:
        side = str(f.get("side") or "").strip().lower()
        volume = _float(f.get("volume"))
        price = _float(f.get("price"))
        day = f.get("day") if isinstance(f.get("day"), date) else _bj_day(f.get("day"))
        if side not in ("buy", "sell") or volume <= _EQUITY_EPS or price <= 0 or day is None:
            continue
        clean.append({
            "day": day, "ts": f.get("ts"), "code": str(f.get("code") or ""),
            "side": side, "volume": volume, "price": price,
            "id": f.get("id") or 0,
        })
    clean.sort(key=lambda x: (x["day"], str(x["ts"] or ""), int(x["id"] or 0)))

    # ── 1. 反推起点（当前账本 → 第一笔成交前）──────────────────────
    cash = _float(end_cash)
    pos: dict[str, float] = {
        c: _float(v) for c, v in (end_positions or {}).items()
        if abs(_float(v)) > _EQUITY_EPS
    }
    for f in reversed(clean):
        amount = f["volume"] * f["price"]
        code = f["code"]
        if f["side"] == "sell":
            cash -= amount
            pos[code] = pos.get(code, 0.0) + f["volume"]
        else:
            cash += amount
            pos[code] = pos.get(code, 0.0) - f["volume"]
        if abs(pos[code]) <= _EQUITY_EPS:
            pos.pop(code, None)

    # ── 2. 交易日轴：成交日 ∪ 行情日（首笔成交 ~ 今天）────────────
    first_day = min((f["day"] for f in clean), default=today)
    days = {f["day"] for f in clean}
    days.update(d for d in (closes or {}) if first_day <= d <= today)
    days.add(today)
    days = sorted(d for d in days if first_day <= d <= today)

    by_day: dict[date, list[dict]] = defaultdict(list)
    for f in clean:
        by_day[f["day"]].append(f)

    # 建仓前无行情时的最后兜底：该码任意一笔成交价（不是成本价，是真实成交价）
    fill_px: dict[str, float] = {}
    for f in clean:
        fill_px.setdefault(f["code"], f["price"])

    live_quotes = {k: _float(v) for k, v in (quotes or {}).items() if _float(v) > 0}
    last_px: dict[str, float] = {}
    points: list[dict] = []
    for day in days:
        held_intraday = any(v > _EQUITY_EPS for v in pos.values())
        for f in by_day.get(day, ()):  # 同日按时间序（已排序）
            amount = f["volume"] * f["price"]
            if f["side"] == "sell":
                cash += amount
                pos[f["code"]] = pos.get(f["code"], 0.0) - f["volume"]
            else:
                cash -= amount
                pos[f["code"]] = pos.get(f["code"], 0.0) + f["volume"]
            if abs(pos[f["code"]]) <= _EQUITY_EPS:
                pos.pop(f["code"], None)
            else:
                held_intraday = True
            last_px[f["code"]] = f["price"]

        market_value = 0.0
        incomplete = False
        for code, qty in pos.items():
            if qty <= _EQUITY_EPS:
                continue
            close = _float((closes.get(day) or {}).get(code))
            quote = live_quotes.get(code, 0.0) if day == today else 0.0
            if quote > 0:
                px = quote
            elif close > 0:
                px = close
            elif last_px.get(code):
                px = last_px[code]
                incomplete = True
            else:
                px = fill_px.get(code, 0.0)
                incomplete = True
            if px > 0:
                last_px[code] = px
                market_value += qty * px
            else:
                incomplete = True

        if day == today:
            ts = now.astimezone(tz_bj).isoformat()
        else:
            ts = datetime.combine(day, dt_time(15, 0), tzinfo=tz_bj).isoformat()
        points.append({
            "date": day.isoformat(),
            "ts": ts,
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(cash + market_value, 2),
            # 收盘持仓（股数，不含现金）；positions 端点/回合持仓列用
            "positions": {
                code: int(qty) for code, qty in sorted(pos.items())
                if qty > _EQUITY_EPS
            },
            "has_position": bool(held_intraday),
            "incomplete": bool(incomplete),
        })
    return points


def annotate_fills_cash(
    fills: list[dict],
    *,
    end_cash: float,
    end_positions: dict[str, float],
) -> list[dict]:
    """给成交流水补 ``cash_after``（成交后现金）。

    反推锚点与 :func:`build_ledger_equity_series` 完全同一套算术：从当前
    现金/持仓倒放全部成交得到起点现金，再正向逐笔推进。这样流水上的现金列
    与净值曲线同源，不会出现「两条链各自算现金」的对账差。
    """
    clean: list[dict] = []
    for f in fills or []:
        side = str(f.get("side") or "").strip().lower()
        volume = _float(f.get("volume"))
        price = _float(f.get("price"))
        day = f.get("day") if isinstance(f.get("day"), date) else _bj_day(f.get("day"))
        if side not in ("buy", "sell") or volume <= _EQUITY_EPS or price <= 0 or day is None:
            continue
        clean.append({
            "day": day, "ts": f.get("ts"), "code": str(f.get("code") or ""),
            "side": side, "volume": volume, "price": price,
            "id": f.get("id") or 0, "approx_price": f.get("approx_price", False),
        })
    clean.sort(key=lambda x: (x["day"], str(x["ts"] or ""), int(x["id"] or 0)))

    cash = _float(end_cash)
    pos: dict[str, float] = {
        c: _float(v) for c, v in (end_positions or {}).items()
        if abs(_float(v)) > _EQUITY_EPS
    }
    for f in reversed(clean):
        amount = f["volume"] * f["price"]
        if f["side"] == "sell":
            cash -= amount
            pos[f["code"]] = pos.get(f["code"], 0.0) + f["volume"]
        else:
            cash += amount
            pos[f["code"]] = pos.get(f["code"], 0.0) - f["volume"]
        if abs(pos[f["code"]]) <= _EQUITY_EPS:
            pos.pop(f["code"], None)
    for f in clean:
        amount = f["volume"] * f["price"]
        if f["side"] == "sell":
            cash += amount
            pos[f["code"]] = pos.get(f["code"], 0.0) - f["volume"]
        else:
            cash -= amount
            pos[f["code"]] = pos.get(f["code"], 0.0) + f["volume"]
        if abs(pos[f["code"]]) <= _EQUITY_EPS:
            pos.pop(f["code"], None)
        f["cash_after"] = round(cash, 2)
        f["position_after"] = {
            code: int(qty) for code, qty in sorted(pos.items()) if qty > _EQUITY_EPS
        }
    return clean


def summarize_ledger_roundtrips(rows: list[dict] | None) -> dict:
    """回合台账 → 旧 ``summary`` 同口径的统计（胜率/盈亏比/期望/极值/持仓时长）。

    口径与 ``agent_data.compute_extended_summary`` 对齐（前端文案按同一套写）：
    亏损侧含 0；盈亏比 = 盈利合计 / 亏损合计；全胜时 999（不是无穷大）。
    **费用列在账本里不存在**：``total_fee``/``fee_ratio`` 一律 ``None``，
    显示为「—」，不编 0。
    """
    rows = list(rows or [])
    pnls = [_float(r.get("realized_pnl")) for r in rows if r.get("realized_pnl") is not None]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    holds = sorted(
        _float(r.get("holding_days"))
        for r in rows if r.get("holding_days") is not None
    )
    out: dict[str, Any] = {
        "closed_trades": len(rows),
        "full_liquidations": sum(1 for r in rows if r.get("closed")),
        "total_fee": None,
        "fee_ratio": None,
    }
    if pnls:
        win_rate = len(wins) / len(pnls)
        sum_win = sum(wins)
        sum_loss = abs(sum(losses))
        avg_win = sum_win / len(wins) if wins else 0.0
        avg_loss = sum_loss / len(losses) if losses else 0.0
        out.update({
            "win_rate": round(win_rate, 4),
            "profit_factor": (
                round(sum_win / sum_loss, 3) if sum_loss > 0
                else (None if not wins else 999.0)
            ),
            "biggest_win": round(max(wins), 2) if wins else None,
            "biggest_loss": round(min(losses), 2) if losses else None,
            "avg_trade_pnl": round(sum(pnls) / len(pnls), 2),
            "expectancy": round(avg_win * win_rate - avg_loss * (1 - win_rate), 2),
        })
    else:
        out.update({
            "win_rate": None, "profit_factor": None, "biggest_win": None,
            "biggest_loss": None, "avg_trade_pnl": None, "expectancy": None,
        })
    sizes = sorted(
        _float(r.get("volume")) * _float(r.get("sell_price"))
        for r in rows if r.get("sell_price") is not None
    )
    out["avg_trade_size"] = round(sum(sizes) / len(sizes), 2) if sizes else None
    out["median_trade_size"] = round(sizes[len(sizes) // 2], 2) if sizes else None
    out["avg_hold_days"] = round(sum(holds) / len(holds), 1) if holds else None
    out["median_hold_days"] = round(holds[len(holds) // 2], 1) if holds else None
    return out


def _ts_of_point(point: dict) -> datetime:
    raw = str(point.get("ts") or point.get("date") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _point_value(point: dict) -> float | None:
    """净值点取值：旧文件线用 ``value``，PG 线用 ``equity``；非数 → None。"""
    try:
        return float(point.get("value", point.get("equity")))
    except (TypeError, ValueError):
        return None


def _seam_disagrees(kept: list[dict], pg_pts: list[dict]) -> bool:
    """旧线末点与 PG 线首个不早于该日的点，水平差是否超过接缝容差。

    同一天两点都存在时是精确对账（pro/glm 就是这种情形）；PG 线晚几天才
    有值时按首个不早于该日的点比较（3% 跨国庆长假属正常波动，不误判）。
    """
    if not kept or not pg_pts:
        return False
    last = max(kept, key=_ts_of_point)
    last_date = str(last.get("date") or "")[:10]
    last_value = _point_value(last)
    if last_value is None:
        return False
    ordered = sorted(pg_pts, key=lambda p: str(p.get("date") or "")[:10])
    ref = next(
        (p for p in ordered if str(p.get("date") or "")[:10] >= last_date),
        ordered[-1],
    )
    ref_value = _point_value(ref)
    if ref_value is None or ref_value == 0:
        return False
    return abs(last_value - ref_value) / abs(ref_value) > SEAM_TOLERANCE


def merge_live_equity(file_data: dict, pg_series: dict[str, list[dict]]) -> dict:
    """把 PG 重建的实盘净值线拼到 ``live_equity.jsonl`` 的旧分钟线上。

    拼接规则（2026-10-20）：
    * ``< 2026-09-30`` 的旧点保留——那时文件账本仍是活的，分钟级历史真实；
    * ``>= 2026-09-30`` 的旧点**全部丢弃**——文件账本已冻结，这些值把
      09-29 的现金/持仓配上之后每天的实时价，越走越假；
    * 用 PG 日线（``equity``）补 09-30 起的历史，收盘时刻 15:00；
    * 当日若采样器已写出 ``source=pg`` 的真实分钟点，以它收尾；否则用
      PG 序列里今天的单点（实时快照）；
    * 接缝水平差 > ``SEAM_TOLERANCE`` 视为迁移口径断层（期初持仓未入 PG
      账）：PG 日线是从**今日账本**反推的重建值，断层正说明它的历史水平不可
      信；冻结前的旧线反而是唯一可信的历史采样。此时保留旧线、跳过 PG 重建
      日线，由旧线末点直接连到当日 PG 尾点（两端都是真值，中间不画假形状）。
    """
    out_agents: dict[str, list[dict]] = {}
    old_agents = (file_data.get("agents") or {}) if isinstance(file_data, dict) else {}
    today_iso = ""
    for pts in (pg_series or {}).values():
        for p in pts or []:
            d = str(p.get("date") or "")[:10]
            if d > today_iso:
                today_iso = d
    if not today_iso:
        today_iso = datetime.now(timezone(timedelta(hours=8))).date().isoformat()

    for agent, old_pts in old_agents.items():
        old_pts = list(old_pts or [])
        pg_pts = list((pg_series or {}).get(agent) or [])
        if not pg_pts:
            out_agents[agent] = old_pts
            continue
        kept = [
            p for p in old_pts
            if _ts_of_point(p) < FILE_EQUITY_CUTOVER
        ]
        # 接缝断层（迁移期初持仓缺失）：PG 日线是「从今日账本反推」的重建值，
        # 断层说明它的历史水平不可信，而 kept（冻结前的分钟级采样）才是真实
        # 历史。旧实现把 kept 整段弃用 → pro/glm 左侧历史凭空消失（2026-10-20
        # 用户实测「线条断了、显示不全」）。改为保留旧线、跳过 PG 重建日线，
        # 让旧线末点直接连到当日 PG 尾点。
        seam_broken = _seam_disagrees(kept, pg_pts)
        today_points = [
            p for p in old_pts
            if p.get("source") == "pg" and str(p.get("date") or "")[:10] == today_iso
        ]
        # 文件线已覆盖的交易日不再追加 PG 日线点（同一天两套口径会出锯齿）；
        # 文件线提前断档时，从断档次日继续用 PG 日线补齐。
        last_kept_date = max(
            (str(p.get("date") or "")[:10] for p in kept), default=""
        )
        daily = [] if seam_broken else [
            {
                "date": str(p.get("date") or "")[:10],
                "ts": p.get("ts"),
                "value": p.get("equity"),
                "source": "pg",
            }
            for p in pg_pts
            if last_kept_date < str(p.get("date") or "")[:10] < today_iso
        ]
        tail = today_points or [
            {
                "date": str(pg_pts[-1].get("date") or "")[:10],
                "ts": pg_pts[-1].get("ts"),
                "value": pg_pts[-1].get("equity"),
                "source": "pg",
            }
        ]
        merged = kept + daily + tail
        merged.sort(key=_ts_of_point)
        out_agents[agent] = merged

    # PG 有账、文件线没有的 agent：整条线都由 PG 日线 + 当日点构成
    for agent, pg_pts in (pg_series or {}).items():
        if agent in out_agents or not pg_pts:
            continue
        out_agents[agent] = [
            {
                "date": str(p.get("date") or "")[:10],
                "ts": p.get("ts"),
                "value": p.get("equity"),
                "source": "pg",
            }
            for p in pg_pts
        ]

    return {
        "total": (file_data.get("total") or []) if isinstance(file_data, dict) else [],
        "agents": out_agents,
    }


async def load_agent_fills_pg(
    session: Any,
    agents: list[str],
    *,
    tenant_id: str = "default",
) -> dict[str, list[dict]]:
    """读分账成交流水（只取真正入账的：``applied_volume > 0``）。

    返回 ``{agent: [fill...]}``，按 ``trade_date, filled_at, id`` 升序。
    ``trade_date`` 可能为 NULL（历史补录）→ 用 ``filled_at`` 的北京日期。
    被拒/夹取为 0 的成交是审计痕迹，不参与净值重建。
    """
    if not agents:
        return {}
    stmt = text(
        """
        SELECT id, agent, code, side, volume, price, applied_volume,
               trade_date, filled_at, approx_price
        FROM qm_agent_ledger_fill
        WHERE tenant_id = :tenant
          AND user_id IN :users
          AND agent IN :agents
          AND applied_volume > 0
        ORDER BY trade_date NULLS FIRST, filled_at NULLS FIRST, id
        """
    ).bindparams(
        bindparam("users", expanding=True),
        bindparam("agents", expanding=True),
    )
    result = await session.execute(
        stmt,
        {"tenant": tenant_id, "users": _alias_users(), "agents": list(agents)},
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for r in result.mappings():
        day = _bj_day(r["trade_date"]) or _bj_day(r["filled_at"])
        if day is None:
            continue
        out[str(r["agent"])].append({
            "id": int(r["id"] or 0),
            "day": day,
            "ts": r["filled_at"],
            "code": str(r["code"] or ""),
            "side": str(r["side"] or "").strip().lower(),
            "volume": _float(r["applied_volume"]),
            "price": _float(r["price"]),
            "approx_price": bool(r["approx_price"]),
        })
    return dict(out)


async def load_daily_closes(
    session: Any,
    codes: list[str],
    *,
    start: date,
    end: date,
) -> dict[date, dict[str, float]]:
    """月表日收盘 ``{交易日: {后缀码: close}}``；表不存在/无行按缺价处理。

    表名由年/月整数拼出（不是用户输入），仍只查 ``stock_daily_new_YYYY_MM``
    这个固定前缀；代码参数走 ``IN :syms`` 展开。
    """
    from backend.shared.stock_utils import StockCodeUtil

    suffix_by_prefix = {StockCodeUtil.to_prefix(c): c for c in codes if c}
    if not suffix_by_prefix or start > end:
        return {}
    out: dict[date, dict[str, float]] = defaultdict(dict)
    ym = (start.year, start.month)
    stop = (end.year, end.month)
    while ym <= stop:
        table = f"stock_daily_new_{ym[0]:04d}_{ym[1]:02d}"
        exists = (
            await session.execute(text("SELECT to_regclass(:t)"), {"t": table})
        ).scalar()
        if exists:
            stmt = text(
                f"SELECT trade_date, symbol, close FROM {table} "
                "WHERE symbol IN :syms AND trade_date BETWEEN :start AND :end"
            ).bindparams(bindparam("syms", expanding=True))
            rows = await session.execute(
                stmt,
                {"syms": list(suffix_by_prefix), "start": start, "end": end},
            )
            for r in rows.mappings():
                code = suffix_by_prefix.get(str(r["symbol"]))
                px = _float(r["close"])
                if code and px > 0:
                    out[r["trade_date"]][code] = px
        ym = (ym[0] + 1, 1) if ym[1] == 12 else (ym[0], ym[1] + 1)
    return dict(out)


async def load_agent_roundtrips_pg(
    session: Any,
    agent: str,
    *,
    tenant_id: str = "default",
) -> list[dict]:
    """该 agent 的回合台账（含减仓行；``closed`` 只标是否真清仓）。"""
    stmt = text(
        """
        SELECT agent, code, volume, cost_price, sell_price, realized_pnl,
               buy_ts, sell_ts, holding_days, closed, exit_reason
        FROM qm_agent_ledger_roundtrip
        WHERE tenant_id = :tenant
          AND user_id IN :users
          AND agent = :agent
          AND market = 'CN'
        ORDER BY sell_ts DESC, id DESC
        """
    ).bindparams(bindparam("users", expanding=True))
    rows = await session.execute(
        stmt,
        {"tenant": tenant_id, "users": _alias_users(), "agent": agent},
    )
    return [dict(r) for r in rows.mappings()]


def _series_summary_stats(points: list[dict]) -> dict:
    """日线口径的夏普/最大回撤/持仓时间占比。"""
    values = [_float(p.get("equity")) for p in points]
    max_dd = 0.0
    peak = -float("inf")
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1)
    rets = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            rets.append(values[i] / values[i - 1] - 1)
    sharpe = None
    if len(rets) >= 2:
        sd = statistics.stdev(rets)
        if sd > 0:
            sharpe = round(statistics.mean(rets) / sd * (252 ** 0.5), 3)
    held = sum(1 for p in points if p.get("has_position"))
    return {
        "sharpe": sharpe,
        "max_drawdown": round(abs(max_dd), 6),
        "position_time_ratio": round(held / len(points), 4) if points else None,
    }


async def live_prices_for(
    codes: list[str],
    *,
    redis_client: Any | None = None,
    bridge_fallback: bool = True,
) -> dict[str, float]:
    """批量实时价：Redis 标准键 → （缺价时）桥逐只直查兜底。

    与 ``account_live`` 的补价链同源；盘外/午休键过期时桥直查仍能拿到最近价。
    单只失败按缺价处理（调用方自行决定回落收盘/成本），整函数永不抛。
    """
    wanted = [c for c in dict.fromkeys(codes) if c]
    if not wanted:
        return {}
    quotes: dict[str, float] = {}
    if redis_client is not None:
        try:
            snaps = await asyncio.to_thread(read_snapshots, redis_client, wanted)
            quotes = {
                code: px for code, snap in snaps.items()
                if (px := _snap_last(snap)) > 0
            }
        except Exception:  # noqa: BLE001 - 行情读败只降级到桥/收盘
            quotes = {}
    missing = [c for c in wanted if c not in quotes]
    if bridge_fallback and missing:
        try:
            quotes.update(await _bridge_quick_prices(missing))
        except Exception:  # noqa: BLE001 - 桥不可达维持缺价语义
            pass
    return quotes


async def equity_series_pg(
    session: Any,
    agents: list[str],
    *,
    redis_client: Any | None = None,
    today: date | None = None,
    use_cache: bool = True,
) -> dict[str, list[dict]]:
    """批量重建 agent 的实盘净值序列（PG 成交 + 月表日线 + 当日快照）。"""
    tz_bj = timezone(timedelta(hours=8))
    today = today or datetime.now(tz_bj).date()
    key = ("pg", tuple(sorted(agents)), today.isoformat())
    now_mono = _time.monotonic()
    if use_cache and agents:
        hit = _SERIES_CACHE.get(key)
        if hit and now_mono - hit[0] < _SERIES_CACHE_TTL_S:
            return hit[1]

    ledger = await read_pg_ledger(session, agents=list(agents))
    recs = (ledger.get("agents") or {}) if isinstance(ledger, dict) else {}
    fills_by_agent = await load_agent_fills_pg(session, list(agents))
    codes = sorted({
        str(c)
        for rec in recs.values()
        for c in (rec.get("positions") or {})
    } | {f["code"] for fl in fills_by_agent.values() for f in fl})
    start = min(
        (f["day"] for fl in fills_by_agent.values() for f in fl),
        default=today,
    )
    closes = await load_daily_closes(session, codes, start=start, end=today)

    held_codes = [
        str(c)
        for rec in recs.values()
        for c in (rec.get("positions") or {})
    ]
    quotes = await live_prices_for(held_codes, redis_client=redis_client)

    out: dict[str, list[dict]] = {}
    for agent in agents:
        rec = recs.get(agent) or {}
        positions = {
            str(c): _float(p.get("volume"))
            for c, p in (rec.get("positions") or {}).items()
        }
        out[agent] = build_ledger_equity_series(
            fills_by_agent.get(agent, []),
            end_cash=_float(rec.get("virtual_cash")),
            end_positions=positions,
            closes=closes,
            quotes=quotes,
            today=today,
        )
    if use_cache and agents:
        _SERIES_CACHE[key] = (now_mono, out)
    return out


async def build_agent_performance_pg(
    session: Any,
    agent: str,
    *,
    redis_client: Any | None = None,
    today: date | None = None,
) -> dict | None:
    """单模型实盘 performance（形状与文件版 ``/performance`` 对齐 + scope=live）。

    无分账账户且无成交 → ``None``（调用方 404，与旧行为一致）。
    """
    series_map = await equity_series_pg(
        session, [agent], redis_client=redis_client, today=today
    )
    points = series_map.get(agent) or []
    ledger = await read_pg_ledger(session, agents=[agent])
    rec = (ledger.get("agents") or {}).get(agent)
    # 没有活账账户就没有可锚定的现金/持仓：空 fills + end_cash=0 会重放出
    # 一条「权益 0」的假净值线（2026-10-20 实测 fake-model/market-research
    # 被渲染成 -100%），必须返回 None 让调用方 404。
    if rec is None:
        return None

    roundtrips = await load_agent_roundtrips_pg(session, agent)
    stats = summarize_ledger_roundtrips(roundtrips)
    series_stats = _series_summary_stats(points)
    end_equity = _float(points[-1]["equity"]) if points else _float(
        (rec or {}).get("virtual_cash")
    )
    start_equity = load_trading_policy().for_agent(agent).quota
    first_equity = _float(points[0]["equity"]) if points else start_equity
    fills = await load_agent_fills_pg(session, [agent])
    agent_fills = fills.get(agent, [])
    notionals = sorted(abs(f["volume"] * f["price"]) for f in agent_fills)
    summary = {
        "start_equity": round(start_equity, 2),
        "end_equity": round(end_equity, 2),
        "total_return": round(end_equity / start_equity - 1, 6) if start_equity else 0.0,
        "period_return": (
            round(end_equity / first_equity - 1, 6) if first_equity else None
        ),
        "series_start_equity": round(first_equity, 2),
        "max_drawdown": series_stats["max_drawdown"],
        "records": len(points),
        "sharpe": series_stats["sharpe"],
        "position_time_ratio": series_stats["position_time_ratio"],
        **stats,
        "fills_count": len(agent_fills),
        "buy_count": sum(1 for f in agent_fills if f["side"] == "buy"),
        "sell_count": sum(1 for f in agent_fills if f["side"] == "sell"),
        "turnover": round(sum(notionals), 2),
        "avg_trade_size": (
            round(sum(notionals) / len(notionals), 2) if notionals else None
        ),
        "median_trade_size": (
            round(notionals[len(notionals) // 2], 2) if notionals else None
        ),
        "first_trade_date": agent_fills[0]["day"].isoformat() if agent_fills else None,
        "last_trade_date": agent_fills[-1]["day"].isoformat() if agent_fills else None,
        "fee_basis": "unavailable",
    }
    return {
        "agent": agent,
        "scope": "live",
        "points": points,
        "summary": summary,
    }


async def agent_holdings_pg(
    session: Any,
    agent: str,
    *,
    redis_client: Any | None = None,
    today: date | None = None,
) -> dict | None:
    """单模型实盘持仓（PG 活账 + 实时价），形状与 ``agent_data.compute_holdings`` 对齐。

    补价优先级：Redis 标准键 → 桥直查 → 最近 14 天日线收盘 → 成本价
    （最后一档标 ``stale=true``、``pnl=null``——成本价不是现价，不拿它算浮盈）。
    """
    tz_bj = timezone(timedelta(hours=8))
    today = today or datetime.now(tz_bj).date()
    ledger = await read_pg_ledger(session, agents=[agent])
    rec = (ledger.get("agents") or {}).get(agent)
    if rec is None:
        return None
    positions = rec.get("positions") or {}
    codes = list(positions)
    quotes = await live_prices_for(codes, redis_client=redis_client)
    missing = [c for c in codes if c not in quotes]
    if missing:
        closes = await load_daily_closes(
            session, missing, start=today - timedelta(days=14), end=today
        )
        last_close: dict[str, float] = {}
        for day in sorted(closes):
            last_close.update(closes[day])
        for code in missing:
            if code in last_close:
                quotes[code] = last_close[code]

    cash = _float(rec.get("virtual_cash"))
    rows: list[dict] = []
    market_value = 0.0
    incomplete = False
    for code, p in sorted(positions.items()):
        qty = _float(p.get("volume"))
        cost = _float(p.get("cost_price"))
        px = quotes.get(code)
        stale = px is None or px <= 0
        if stale:
            px = cost
            incomplete = True
        value = round(qty * px, 2)
        market_value += value
        rows.append({
            "symbol": code,
            "qty": int(qty),
            "entry_price": round(cost, 4),
            "price": round(px, 4),
            "market_value": value,
            # 成本兜底不算浮盈：缺失就是缺失，不写 0
            "pnl": None if stale else round((px - cost) * qty, 2),
            "pnl_pct": None if stale or cost <= 0 else round(px / cost - 1, 6),
            "change_pct": None,
            "weight_pct": None,  # 总权益出来后再填
            "stale": stale,
        })
    total = cash + market_value
    for row in rows:
        row["weight_pct"] = round(row["market_value"] / total, 6) if total > 0 else None
    return {
        "holdings": rows,
        "cash": round(cash, 2),
        "total_market_value": round(market_value, 2),
        "total_equity": round(total, 2),
        "incomplete": incomplete,
    }


async def load_position_records_pg(
    session: Any,
    agent: str,
    *,
    redis_client: Any | None = None,
    today: date | None = None,
) -> list[dict]:
    """PG 活账 → 逐日持仓快照（``{date,id,this_action,positions}``）。

    与 ``agent_data.load_position_records`` 同形：``positions`` 带 ``CASH`` 键，
    ``this_action`` = 当日最后一笔成交（无成交为 None）。数据来自净值重建的
    逐日收盘持仓，因此前端 ``qtyAt``（成交后持仓）与净值曲线同源。
    """
    tz_bj = timezone(timedelta(hours=8))
    today = today or datetime.now(tz_bj).date()
    series = (
        await equity_series_pg(
            session, [agent], redis_client=redis_client, today=today
        )
    ).get(agent) or []
    if not series:
        return []
    fills = (await load_agent_fills_pg(session, [agent])).get(agent) or []
    by_day: dict[date, list[dict]] = defaultdict(list)
    for f in fills:
        by_day[f["day"]].append(f)
    out: list[dict] = []
    for i, point in enumerate(series):
        day = date.fromisoformat(point["date"])
        day_fills = by_day.get(day) or []
        action = None
        if day_fills:
            last = day_fills[-1]
            action = {
                "action": last["side"],
                "symbol": last["code"],
                "amount": int(last["volume"]),
                "price": last["price"],
            }
        out.append({
            "date": point["date"],
            "id": i,
            "this_action": action,
            "positions": {**point.get("positions", {}), "CASH": point["cash"]},
        })
    return out


def clear_series_cache() -> None:
    """测试/排障用：清空进程内净值序列缓存。"""
    _SERIES_CACHE.clear()


# ---------- 实盘账户（桥实时，改写点④） ----------

_BJ_TZ = timezone(timedelta(hours=8))


def _bj_wall(ts: str) -> str:
    """账本 buy_ts → 北京墙钟 'YYYY-MM-DDTHH:MM'（时刻不变，只换显示时区）。

    来源三态并存：PG 活账是 ``to_utc_iso`` 的 Z 串 UTC、文件账本快照是
    ``+08:00`` aware 串、无时区的存量串按 UTC 解释（账本契约恒 UTC）——
    一律 ``astimezone`` 到 UTC+8；裸 ``[:16]`` 在 Z 串上会显成凌晨
    （09-14T03:07 实为 11:07 买入，2026-10-15 修），而「+8 小时」硬换算
    会把已是 +08:00 的串二次平移（09:31→17:31），都错。坏串原样截断。
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BJ_TZ).strftime("%Y-%m-%dT%H:%M")


async def _bridge_account_query() -> dict:
    """QM 桥 /api/v1/account/query（与实盘执行链同一座桥）。

    返回 ``{account_id, asset: {asset, cash, market_value, ...}, positions:
    [{stock_code, cost_price, total_volume, available_volume}], channel_used}``。
    """
    import httpx

    from backend.services.trade_shared.trade_config import settings

    url = str(getattr(settings, "TDX_BRIDGE_URL", "") or "").strip()
    token = str(getattr(settings, "TDX_BRIDGE_TOKEN", "") or "").strip()
    if not url:
        raise RuntimeError("TDX 桥未配置（TDX_BRIDGE_URL 为空）")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{url.rstrip('/')}/api/v1/account/query",
                json={"account_type": "stock"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
    except Exception as e:  # noqa: BLE001 桥不可达/超时/非 200 同归「查询失败」
        raise RuntimeError(f"桥查询失败: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError("桥查询失败: 响应不是 JSON 对象")
    return data


async def _bridge_quick_prices(codes: list[str]) -> dict[str, float]:
    """持仓缺价时的桥直查兜底：逐只 ``get_market_snapshot`` → ``Now``。

    单只失败静默跳过（该行维持 0 价降级语义）；整体永不抛。
    """
    from backend.shared.stock_utils import StockCodeUtil
    from backend.services.live_trading.services.tdx_push_service import tdx_pusher
    from backend.services.live_trading.services.tdx_quote_feed import map_snapshot

    out: dict[str, float] = {}

    async def _one(code: str) -> None:
        suffix = StockCodeUtil.to_suffix(code) or code
        try:
            result = await tdx_pusher.tdx_call(
                "get_market_snapshot", {"stock_code": suffix}
            )
        except Exception:  # noqa: BLE001 兜底失败维持缺价语义
            return
        price = _snap_last(map_snapshot(result if isinstance(result, dict) else {}) or {})
        if price > 0:
            out[code] = price

    await asyncio.gather(*(_one(c) for c in codes))
    return out


async def account_live(
    session: Any | None = None,
    *,
    tenant_id: str = "default",
    redis_client: Any | None = None,
) -> dict:
    """实盘账户（桥实时）+ 持仓补名/补实时价/补买时/浮盈。

    补价优先级：Redis ``market:snapshot:*`` 标准键 → 桥直查兜底（盘外/午休
    键过期时，同上游逐只 ``broker.get_quote`` 语义）→ 0。
    买时来自 PG 活账（读失败降级为空，同上游 ledger 读取失败降级）。
    """
    payload = await _bridge_account_query()
    asset = payload.get("asset") if isinstance(payload.get("asset"), dict) else {}
    # asset 拍平为 number：桥的 asset["asset"] 是总资产；缺则 cash+market_value。
    total = asset.get("asset")
    if not isinstance(total, (int, float)) or total <= 0:
        total = float(asset.get("cash") or 0) + float(asset.get("market_value") or 0)
    positions = payload.get("positions") or []

    names = market_lab._names()
    buy_ts: dict[str, str] = {}
    if session is not None:
        try:
            led = await read_pg_ledger(session, tenant_id=tenant_id)
            for rec in (led.get("agents") or {}).values():
                for code, p in (rec.get("positions") or {}).items():
                    ts = _bj_wall(p.get("buy_ts") or "")
                    if ts and code not in buy_ts:
                        buy_ts[code] = ts
        except Exception:  # noqa: BLE001 买时是装饰性字段，读败不拖垮账户主数据
            buy_ts = {}

    snaps: dict[str, Any] = {}
    if redis_client is not None and positions:
        codes = [str(p.get("stock_code") or "") for p in positions]
        try:
            snaps = await asyncio.to_thread(read_snapshots, redis_client, codes)
        except Exception:  # noqa: BLE001 行情读败一律 0 价降级
            snaps = {}

    # 缺价补刀（2026-10-08）：标准键 TTL 300s 且盘外/午休停写（is_trading_time
    # 门）→ 休市时段持仓价全 0，面板成「持仓都是 0」。持仓本身刚从桥取到
    # （account/query 已通）→ 逐只直查必可达；只补缺价的、只按持仓只数
    # （个位数）调用，与热集轮转/持仓馈送共享的 600/min 预算无压力。
    if positions:
        missing = [
            str(p.get("stock_code") or "")
            for p in positions
            if not _snap_last(snaps.get(str(p.get("stock_code") or "")) or {})
        ]
        if missing:
            for code, price in (await _bridge_quick_prices(missing)).items():
                snaps[code] = {"Now": price}

    out_positions = []
    for p in positions:
        code = str(p.get("stock_code") or "")
        cost = float(p.get("cost_price") or 0)
        snap = snaps.get(code) or {}
        price = _snap_last(snap)
        row = {
            "stock_code": code,
            "name": names.get(code, ""),
            "cost_price": cost,
            "total_volume": int(p.get("total_volume") or 0),
            "available_volume": int(p.get("available_volume") or 0),
            "last_price": price,
            "position_value": round(price * float(p.get("total_volume") or 0), 2),
            "buy_time": buy_ts.get(code, ""),
        }
        if cost and price:
            row["pnl_pct"] = round((price - cost) / cost * 100, 2)
            row["pnl"] = round((price - cost) * float(p.get("total_volume") or 0), 2)
        else:
            row["pnl_pct"], row["pnl"] = None, None
        out_positions.append(row)
    # 上游把桥 payload 原样返回（account_id 等额外键透传）；唯一偏离：asset
    # 由 dict 拍平成 number（移植版前端按 number 消费，见改写点④）。
    result = dict(payload)
    result["asset"] = float(total or 0)
    result["positions"] = out_positions
    return result


# ---------- 实盘快照/台账（QM PG 原生，改写点②③⑨） ----------

_LATEST_SNAPSHOT_SQL = text(
    """
    SELECT account_id, snapshot_at, total_asset, cash, market_value,
           today_pnl_raw, total_pnl_raw, source, payload_json
    FROM real_account_snapshots
    WHERE tenant_id = :t AND user_id = ANY(:users) AND account_id LIKE :pat
    ORDER BY snapshot_at DESC LIMIT 20
    """
)

_ACCOUNTS_SUMMARY_SQL = text(
    """
    SELECT account_id, snapshot_at, total_asset, cash, market_value, source,
           payload_json
    FROM real_account_snapshots
    WHERE tenant_id = :t AND user_id = ANY(:users) AND account_id LIKE :pat
    ORDER BY snapshot_at DESC LIMIT 20
    """
)


async def _latest_account_row(session: Any, account: str) -> dict | None:
    """通道 → 家族内最新可用快照行（Python 侧 account_family 精确判家族）。"""
    family = _account_family(account)
    rows = (await session.execute(_LATEST_SNAPSHOT_SQL, {
        "t": "default", "users": _alias_users(), "pat": f"{family}-%",
    })).mappings().all()
    from backend.services.trade.services.real_account_ledger_service import (
        account_family,
    )

    for r in rows:
        if account_family(r["account_id"]) == family:
            return dict(r)
    return None


def _positions_with_names(positions: list[dict]) -> list[dict]:
    names = market_lab._names()
    out = []
    for p in positions:
        q = dict(p)
        sym = str(q.get("symbol") or "")
        if not q.get("name"):
            q["name"] = names.get(sym, "")
        out.append(q)
    return out


async def real_account(session: Any, account: str = "tdx") -> dict | None:
    """通道最新实盘快照（总资产/现金/持仓），形状照抄上游 /live/real-account。"""
    if account not in REAL_ACCOUNTS:
        raise ValueError(
            f"未知实盘账户通道 {account!r}，可选：{', '.join(REAL_ACCOUNTS)}")
    row = await _latest_account_row(session, account)
    if not row:
        return None
    payload = row.get("payload_json") or {}
    positions = _positions_with_names(list(payload.get("positions") or []))
    return {
        "ts": to_utc_iso(as_utc(row["snapshot_at"])),
        "total_asset": row["total_asset"],
        "cash": row["cash"],
        "market_value": row["market_value"],
        "today_pnl": row["today_pnl_raw"],
        "total_pnl": row["total_pnl_raw"],
        "account": account,
        "account_id": row["account_id"],
        "account_label": REAL_ACCOUNTS[account]["label"],
        "positions": positions,
    }


async def real_accounts(session: Any) -> list[dict]:
    """两通道最新快照摘要（通道选择器与「交易所在线」区块）。

    age_sec 按 naive-UTC 快照对齐 utc_now（改写点⑩）。
    """
    out = []
    now = utc_now()
    for key, meta in REAL_ACCOUNTS.items():
        row = await _latest_account_row(session, key)
        ts = row["snapshot_at"] if row else None
        payload = (row.get("payload_json") or {}) if row else {}
        out.append({
            "account": key,
            "account_id": row["account_id"] if row else _account_key(
                key, _resolve_account_user()),
            "label": meta["label"],
            "channel": meta["channel"],
            "ts": to_utc_iso(as_utc(ts)) if ts else None,
            "age_sec": int((now - as_utc(ts)).total_seconds()) if ts else None,
            "total_asset": row["total_asset"] if row else None,
            "cash": row["cash"] if row else None,
            "market_value": row["market_value"] if row else None,
            "source": row["source"] if row else None,
            "position_count": len(payload.get("positions") or []) if row else 0,
        })
    return out


async def real_ledger(session: Any, account: str = "tdx", days: int = 3650) -> dict:
    """日终台账（净值曲线数据源）。

    复用现役 ``list_real_account_daily_ledgers_by_family``：跨改名
    （00000001→10000001）与用户别名族的断头问题现役已有唯一解（改写点⑨）。
    """
    if account not in REAL_ACCOUNTS:
        raise ValueError(
            f"未知实盘账户通道 {account!r}，可选：{', '.join(REAL_ACCOUNTS)}")
    from backend.services.trade.services.real_account_ledger_service import (
        list_real_account_daily_ledgers_by_family,
    )

    user = _resolve_account_user()
    rows = await list_real_account_daily_ledgers_by_family(
        session, tenant_id="default", user_id=user,
        account_id=_account_key(account, user), days=days,
    )
    # 同日多行（daily_settlement 与桥回填并存）取后写的那条 = 上游 SQL
    # `DISTINCT ON (snapshot_date) … ORDER BY snapshot_date, id DESC` 的
    # Python 侧对应；输出按日期**升序**（前端取 [length-1] 当最后一天、
    # 折线按序连线，顺序反了会静默画反/读错最近日收益）。
    latest: dict = {}
    for r in rows:
        d = r.snapshot_date
        cur = latest.get(d)
        if cur is None or (int(getattr(r, "id", 0) or 0)
                           > int(getattr(cur, "id", 0) or 0)):
            latest[d] = r
    ordered = [latest[d] for d in sorted(latest)]
    return {
        "rows": [
            {
                "date": r.snapshot_date.isoformat() if r.snapshot_date else None,
                "total_asset": r.total_asset,
                "cash": r.cash,
                "market_value": r.market_value,
                "daily_return_pct": r.daily_return_pct,
                "total_return_pct": r.total_return_pct,
                "position_count": r.position_count,
                "source": r.source,
            }
            for r in ordered
        ],
        "account": account,
        "account_id": _account_key(account, user),
    }


# JOIN stocks 取股票名称：stocks 主键 symbol 为后缀格式（600097.SH），与
# tdx_l2_snapshot 的 stock_code 一致；snapshot 的 symbol 是前缀格式（SH600097），
# 不能用于关联（上游注释照抄）。两条 SQL 分列 = 上游两条分支逐字对应。
_L2_JOIN_SQL = """
    SELECT s.ts, s.symbol, s.stock_code, s.now_price, s.factors,
           st.name
    FROM tdx_l2_snapshot s
    LEFT JOIN stocks st ON st.symbol = s.stock_code
"""
_L2_FACTORS_ALL_SQL = text(_L2_JOIN_SQL + " ORDER BY s.ts DESC LIMIT :lim")
_L2_FACTORS_SYM_SQL = text(
    _L2_JOIN_SQL + " WHERE s.symbol = :sym ORDER BY s.ts DESC LIMIT :lim")


async def l2_factors(session: Any, symbol: str | None = None,
                     limit: int = 60) -> list[dict]:
    """L2 因子快照（最近 limit 条，可选 symbol 过滤）。SQL 照抄上游 JOIN 口径。"""
    if symbol:
        stmt, params = _L2_FACTORS_SYM_SQL, {"sym": symbol, "lim": limit}
    else:
        stmt, params = _L2_FACTORS_ALL_SQL, {"lim": limit}
    rows = (await session.execute(stmt, params)).mappings().all()
    return [
        {
            "ts": to_utc_iso(as_utc(r["ts"])) if r["ts"] else None,
            "symbol": r["symbol"],
            "stock_code": r["stock_code"],
            "name": r["name"],
            "now_price": r["now_price"],
            "factors": r["factors"] or {},
        }
        for r in rows
    ]


# ---------- 当日实时指数（改写点⑧） ----------

def _bench_last_two(path: Path):
    """基准文件（AlphaVantage 格式）最后两根日线 → 单元素 indices 列表或 None。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        series = doc.get("Time Series (Daily)") or {}
        if len(series) < 2:
            return None
        days = sorted(series.items())
        c1 = float(days[-2][1].get("4. close") or 0)
        c2 = float(days[-1][1].get("4. close") or 0)
        if c1 <= 0 or c2 <= 0:
            return None
        name = (doc.get("Meta Data") or {}).get("2. Symbol", "INDEX")
        return [{"code": name, "name": name, "last": round(c2, 2),
                 "change_pct": round((c2 - c1) / c1 * 100, 2)}]
    except (OSError, ValueError, TypeError):
        return None


async def _tencent_indices(market: str) -> list[dict]:
    """腾讯实时快照 → indices 行（照抄上游字段口径 f[1]/f[2]/f[3]/f[32]）。"""
    import httpx

    codes = ",".join(c for c, _ in TENCENT_INDICES[market])
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"https://qt.gtimg.cn/q={codes}")
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="ignore")
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        payload = line.split("=", 1)[1].strip('";')
        f = payload.split("~")
        if len(f) < 35:
            continue
        try:
            last = float(f[3])
            chg = float(f[32])
        except (TypeError, ValueError):
            continue
        if last <= 0:
            continue
        name = next((n for c, n in TENCENT_INDICES[market] if c in line), f[1])
        rows.append({"code": f[2], "name": name, "last": round(last, 2),
                     "change_pct": round(chg, 2)})
    return rows


async def load_indices(market: str = "cn", redis_client: Any | None = None) -> list[dict]:
    """当日实时指数（Live 顶部行情条）。

    CN：Redis ``market:snapshot:*`` 标准键（现役行情总线）→ 基准文件兜底。
    US：等权 NDX100 基准文件（无实时源）；HK：腾讯 + 空兜底。
    """
    root = arena_config.data_root()
    if market == "cn":
        rows: list = []
        if redis_client is not None:
            try:
                codes = [c for c, _ in INDEX_DEFS]
                snaps = await asyncio.to_thread(read_snapshots, redis_client, codes)
                for code, name in INDEX_DEFS:
                    snap = snaps.get(code)
                    last = _snap_last(snap)
                    if last <= 0:
                        continue
                    chg = day_change_pct(snap)
                    rows.append({"code": code, "name": name, "last": round(last, 2),
                                 "change_pct": round(chg, 2) if chg is not None else None})
            except Exception:  # noqa: BLE001 行情源不可用 → 基准兜底
                rows = []
        if rows:
            return rows
        return _bench_last_two(root / "A_stock" / "index_daily_sse_50.json") or []
    if market == "us":
        rows = await _tencent_indices("us")
        if rows:
            return rows
        return _bench_last_two(root / "benchmark_nasdaq100.json") or []
    if market == "hk":
        return await _tencent_indices("hk")
    return []


# ---------- 盘中新闻（改写点⑦） ----------

async def load_news(tickers: str = "", hours: int = 24, limit: int = 10,
                    keyword: str = "") -> dict:
    """盘中新闻 → 本机 /api/v1/news/articles（与上游代理同语义同参数）。

    news 路由 2026-10-07 起按鉴权分层（业务端点要求登录态），本调用改走
    内部信任头（X-Internal-Call 密钥 + X-User-Id），与 ai_strategy 等内部
    调用方同口径；缺密钥时下游 401，本函数按既有约定降级为空结果。
    """
    import httpx

    from backend.shared.auth import get_internal_call_secret

    base = "http://127.0.0.1:8000"
    headers = {
        "X-Internal-Call": str(get_internal_call_secret() or ""),
        "X-User-Id": "0",
        "X-Internal-Service": "agent_arena",
    }
    hours = max(1, min(168, int(hours or 24)))
    limit = max(1, min(50, int(limit or 10)))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict[str, Any] = {"since": since, "page_size": limit,
                              "sort": "time_desc"}
    if tickers:
        params["tickers"] = tickers
    if keyword:
        params["keyword"] = keyword
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base}/api/v1/news/articles", params=params, headers=headers
            )
            resp.raise_for_status()
            return {"success": True, "data": resp.json()}
    except Exception as e:  # noqa: BLE001 同上游：错误装进 data.error，不 5xx
        return {"success": True,
                "data": {"articles": [], "error": f"{type(e).__name__}: {e}"}}
