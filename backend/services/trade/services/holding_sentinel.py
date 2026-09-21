"""持仓哨兵：把「分数下滑 / 盘中重大利空 / 名单新增」变成 per-user 持仓预警。

**分工（本模块最重要的设计决定）**：市场级「什么算大事」已经有人做了——
`sentinel_alert_service` 消费 `intel:events`，把新闻/异动/regime 归并进
`sentinel_alerts`（带 direction/severity/dedupe/冷却/每小时上限）。本哨兵
**不重复消费总线**，只读 `sentinel_alerts` 的增量并按「这只票在不在这个用户的
池子里」扇出。这样「重大利空」只有一处定义，不会出现「总台判了利空、持仓哨兵
没反应」；也不会同一条新闻报两遍。

三类信号：

1. **分数下滑**（T1）：同一张分数表（`backend.shared.signal_scores`，与自选池
   统一视图同一个装载函数）——下穿 0 / 跌破自定阈值。基线存 Redis，逐轮对比。
2. **盘中重大利空 / 异动**（T2）：`sentinel_alerts` 增量里**标的级、方向向下**的
   行（新闻利空 / 大幅下行），标的落在监控集内才提醒。
3. **名单新增命中**（T3）：排除名单快照换版时，**新进**阻断集的持仓才提醒
   （名单里有几千只，把「在名单上」当信号等于每天提醒一次全部持仓）。

监控集 = 模拟持仓 ∪ 实盘持仓 ∪ 手工自选（各源开关由用户配置决定）。**只提醒，
不下单**——面板上的动作按钮由前端把用户点的那一下交给既有推送链路，本模块没有下单权限。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from backend.shared.holding_alert_contract import (
    BASELINE_KEY_PREFIX,
    COOLDOWN_SECONDS,
    CONFIG_KEY_PREFIX,
    KIND_RISK_ANOMALY,
    KIND_RISK_LIST,
    KIND_RISK_NEWS,
    MAX_ALERTS_PER_SCAN,
    RISK_CURSOR_KEY_PREFIX,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATUS_ACTIVE,
    alert_action_url,
    baseline_is_comparable,
    build_alert_content,
    build_alert_title,
    cooldown_bucket,
    dedupe_alert_rows,
    evaluate_score_transition,
    make_holding_dedupe_key,
    meets_min_severity,
    notification_level,
    parse_alert_config,
)
from backend.shared.real_positions import load_real_positions
from backend.shared.signal_scores import load_score_snapshot, normalize_position_symbol

logger = logging.getLogger(__name__)

#: 扫描节拍（秒）。盘中实时分每 ~1.7min 一圈，60s 足够跟上且不至于空转。
SCAN_INTERVAL_SECONDS = 60

#: 状态与计数器（供读端点与管理页如实显示「哨兵在跑」）
STATUS_KEY = "qm:holding:sentinel:status"
SCHEDULER_NAME = "holding_sentinel"

#: 市场级告警只吃这个窗口内的行（哨兵停摆后回放几天前的新闻没有意义）
_INTEL_LOOKBACK_SECONDS = 48 * 3600

#: 一条市场告警最多扇出多少只标的（全市场级新闻的 targets 可能很长）
_MAX_TARGETS_PER_ALERT = 50

#: 每轮最多处理多少条市场告警增量（正常每分钟几条；积压时先追平再谈别的）
_MAX_MARKET_ROWS_PER_SCAN = 500

#: 标的级的「该卖」信号白名单：alert_type → (kind, 兜底 severity)。
#: **必须是白名单**：`sentinel_alerts` 里还有账户级（撤单率）、模型级（IC 掉）、
#: 数据级（跳变）的告警，它们 direction 也可能是 down，但那是「系统有事」不是
#: 「该卖股票」。把它们当利空推给持仓用户，是在教用户忽略提醒。
_MARKET_ALERT_KINDS: dict[str, tuple[str, str]] = {
    "news:risk_event": (KIND_RISK_NEWS, SEVERITY_CRITICAL),
    "news:negative": (KIND_RISK_NEWS, SEVERITY_WARNING),
    "anomaly:price_drop": (KIND_RISK_ANOMALY, SEVERITY_WARNING),
    # 名字叫 surge 但方向判成 down 时，说明源侧看到的其实是下行
    "anomaly:price_surge": (KIND_RISK_ANOMALY, SEVERITY_WARNING),
}


# ── 纯函数（可单测）──────────────────────────────────────────────────


def positions_from_payload(payload: Any) -> dict[str, dict[str, Any]]:
    """模拟账户 JSON → ``{prefix 代码: 持仓行}``（只留 volume>0 的票）。

    ``volume<=0`` 的行是「平掉了但没清」的残影；把它们当持仓会让哨兵对着空仓
    发卖出提醒——库里留着它是因为对账要用，不是因为还持有。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    positions = (payload or {}).get("positions")
    if not isinstance(positions, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, row in positions.items():
        if not isinstance(row, Mapping):
            continue
        try:
            if float(row.get("volume") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        sym = normalize_position_symbol(row.get("symbol") or key)
        if not sym:
            continue
        out[sym] = dict(row)
    return out


def build_monitor_set(
    *,
    sim: Mapping[str, Any] | None = None,
    real: Mapping[str, Any] | None = None,
    manual: Mapping[str, Any] | None = None,
    name_resolver: Callable[[str], str] | None = None,
) -> dict[str, dict[str, Any]]:
    """三路持仓/自选 → ``{prefix: {sources, position, stockName}}``（纯函数）。"""
    out: dict[str, dict[str, Any]] = {}

    def _touch(sym: str) -> dict[str, Any]:
        entry = out.get(sym)
        if entry is None:
            entry = {"sources": [], "position": None, "stockName": ""}
            out[sym] = entry
        return entry

    for source, book in (("sim", sim or {}), ("real", real or {})):
        for sym, row in book.items():
            entry = _touch(sym)
            if source not in entry["sources"]:
                entry["sources"].append(source)
            name = str((row or {}).get("name") or "").strip()
            if name and not entry["stockName"]:
                entry["stockName"] = name
            # 实盘优先展示（真金白银的那一份）；模拟腿仍记进 sources
            if source == "real" or entry["position"] is None:
                entry["position"] = dict(row or {})

    for sym, row in (manual or {}).items():
        entry = _touch(sym)
        if "manual" not in entry["sources"]:
            entry["sources"].append("manual")
        name = str((row or {}).get("stockName") or "").strip()
        if name and not entry["stockName"]:
            entry["stockName"] = name

    for sym, entry in out.items():
        if not entry["stockName"] and name_resolver is not None:
            entry["stockName"] = str(name_resolver(sym) or "")
    return out


def classify_market_alert(
    alert_type: str, direction: str, severity: str
) -> tuple[str, str] | None:
    """`sentinel_alerts` 行 → ``(kind, severity)``；不该进持仓预警的返回 ``None``。

    两道闸：**方向必须向下**（利好/中性/regime 都不是「该卖」的理由），
    **类型必须在白名单**（账户级/模型级/数据级告警不进持仓预警，见常量注释）。
    """
    if str(direction or "").strip().lower() != "down":
        return None
    mapped = _MARKET_ALERT_KINDS.get(str(alert_type or "").strip().lower())
    if mapped is None:
        return None
    kind, fallback = mapped
    sev = str(severity or "").strip().lower()
    if sev == SEVERITY_CRITICAL:
        return kind, SEVERITY_CRITICAL
    if sev in {"warn", SEVERITY_WARNING}:
        return kind, SEVERITY_WARNING
    return kind, fallback


def target_symbols(symbol: Any, targets: Any) -> list[str]:
    """告警行 → 涉及的 prefix 标的列表（去重、保序、有上限）。

    ``symbol`` 可能是 ``'*'``（全市场），此时只能看 ``targets``；两者都归一化，
    认不出来的（账户号 ``9000ba63``、模型名 ``itest-xxxx``）自然落空。
    """
    raw: list[Any] = [symbol]
    if isinstance(targets, (list, tuple, set)):
        raw.extend(targets)
    elif isinstance(targets, str):
        try:
            parsed = json.loads(targets)
        except (TypeError, ValueError):
            parsed = [targets]
        raw.extend(parsed if isinstance(parsed, list) else [parsed])

    out: list[str] = []
    for item in raw:
        sym = normalize_position_symbol(item)
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= _MAX_TARGETS_PER_ALERT:
            break
    return out


def new_symbols(previous: set[str], current: set[str]) -> set[str]:
    """名单换版时的**新增**命中（首次无基线返回空集：不在第一轮就报全部持仓）。"""
    if not previous:
        return set()
    return {s for s in current if s not in previous}


# ── 服务 ─────────────────────────────────────────────────────────────


class HoldingSentinel:
    """持仓预警扫描器（依赖可注入，便于单测）。"""

    def __init__(
        self,
        *,
        redis: Any = None,
        session_factory: Callable[[], Any] | None = None,
        score_loader: Callable[[str], Any] = load_score_snapshot,
        real_loader: Callable[[str, str], Any] = load_real_positions,
        name_resolver: Callable[[str], str] | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._score_loader = score_loader
        self._real_loader = real_loader
        self._now_fn = now_fn
        if name_resolver is None:
            from backend.shared.stock_name_mapper import resolve_name as _resolve

            name_resolver = _resolve
        self._name_resolver = name_resolver

    # ── 依赖 ──

    def _client(self) -> Any:
        if self._redis is None:
            from backend.services.trade_shared.redis_client import redis_client

            if redis_client.client is None:
                redis_client.connect()
            self._redis = redis_client.client
        return self._redis

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from backend.shared.database_manager_v2 import get_session

        return get_session()

    # ── 主流程 ──

    async def run_once(self) -> dict[str, Any]:
        client = self._client()
        if client is None:
            return {"ok": False, "reason": "redis 不可用"}

        started = self._now_fn()
        users = await self._load_users()
        if not users:
            await self._write_status(client, {"users": 0, "alerts": 0})
            return {"ok": True, "users": 0, "alerts": 0}

        manual_by_user = await self._load_manual_watchlist()
        sim_by_user, orphan_accounts = self._load_sim_positions(
            client, self._user_index(users)
        )

        alerts: list[dict[str, Any]] = []
        per_user_stats: dict[str, dict[str, Any]] = {}
        for user in users:
            tenant, user_id = user["tenant_id"], user["user_id"]
            cfg = parse_alert_config(
                client.hget(f"{CONFIG_KEY_PREFIX}{tenant}:{user_id}", "settings")
            )
            stats = {"enabled": cfg["enabled"], "symbols": 0, "score": 0, "risk": 0}
            per_user_stats[f"{tenant}:{user_id}"] = stats
            if not cfg["enabled"]:
                continue

            real: dict[str, dict[str, Any]] = {}
            if cfg["watch_real"]:
                try:
                    real, _real_meta = await self._real_loader(tenant, user_id)
                except Exception as exc:  # noqa: BLE001 - 单用户实盘取不到不影响其余
                    logger.warning(
                        "[holding_sentinel] 实盘持仓取数失败 user=%s: %s", user_id, exc
                    )
                    real = {}
            monitor = build_monitor_set(
                sim=sim_by_user.get((tenant, user_id), {}) if cfg["watch_sim"] else {},
                real=real,
                manual=manual_by_user.get((tenant, user_id), {})
                if cfg["watch_manual"]
                else {},
                name_resolver=self._name_resolver,
            )
            stats["symbols"] = len(monitor)
            if not monitor:
                continue

            score_alerts = await self._scan_scores(client, user, monitor, cfg, started)
            risk_alerts = await self._scan_market_alerts(
                client, tenant, user_id, monitor, started
            )
            list_alerts = await self._scan_exclusion(
                client, tenant, user_id, monitor, started
            )
            stats["score"] = len(score_alerts)
            stats["risk"] = len(risk_alerts) + len(list_alerts)
            alerts.extend(score_alerts + risk_alerts + list_alerts)

        written = await asyncio.to_thread(self._persist, alerts)
        status = {
            "users": len(users),
            "monitored": sum(s["symbols"] for s in per_user_stats.values()),
            "alerts": written["recorded"],
            "notified": written["notified"],
            "orphan_accounts": orphan_accounts,
            "per_user": per_user_stats,
        }
        await self._write_status(client, status)
        if written["recorded"]:
            logger.info(
                "[holding_sentinel] 新增预警 %s 条（推送 %s 条）",
                written["recorded"],
                written["notified"],
            )
        return {"ok": True, **status}

    # ── 监控集 ──

    async def _load_users(self) -> list[dict[str, str]]:
        from sqlalchemy import text

        try:
            async with self._session() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, user_id, COALESCE(tenant_id, 'default') AS tid "
                            "FROM users "
                            "WHERE COALESCE(is_deleted, false) = false "
                            "AND COALESCE(is_active, true) = true"
                        )
                    )
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[holding_sentinel] 用户列表读取失败: %s", exc)
            return []
        return [
            {"id": str(r[0]), "user_id": str(r[1]), "tenant_id": str(r[2])}
            for r in rows
        ]

    @staticmethod
    def _user_index(users: list[dict[str, str]]) -> dict[tuple[str, str], str]:
        """(tenant, 模拟账户键里的 user 后缀) → 认证空间的 user_id。

        模拟账户键的后缀可能是 ``users.id``、历史 admin 别名
        （``00000001``/``1``/``admin``）或 ``user_id`` 本身——三套写法指向同一个人。
        映射不出来的是**孤儿账户**：没有用户行就没有通知收件人，跳过并计数
        （不静默：状态里能看见有几户在空转）。
        """
        from backend.shared.simulation_account_keys import ledger_user_id_candidates

        index: dict[tuple[str, str], str] = {}
        for user in users:
            tenant = user["tenant_id"]
            tokens = [
                user["user_id"],
                user["id"],
                *ledger_user_id_candidates(user["user_id"]),
            ]
            for token in tokens:
                text = str(token or "").strip()
                if text:
                    index.setdefault((tenant, text), user["user_id"])
        return index

    def _load_sim_positions(
        self, client: Any, users_by_token: Mapping[tuple[str, str], str]
    ) -> tuple[dict[tuple[str, str], dict[str, dict[str, Any]]], int]:
        """扫描全部模拟账户 → ``{(tenant, user_id): {symbol: row}}`` + 孤儿账户数。"""
        from backend.shared.simulation_account_keys import parse_account_key

        out: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        orphans = 0
        try:
            keys = list(client.scan_iter(match="simulation:account:*", count=500))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[holding_sentinel] 模拟账户扫描失败: %s", exc)
            return {}, 0
        for raw_key in keys:
            parsed = parse_account_key(str(raw_key))
            if parsed is None:
                continue
            tenant, token, market = parsed
            if market != "CN":  # 本哨兵服务 A 股分数/名单口径，其它市场另开
                continue
            user_id = users_by_token.get((tenant, token))
            if user_id is None:
                orphans += 1
                continue
            try:
                payload = client.get(str(raw_key))
            except Exception:  # noqa: BLE001 - 单账户读失败不影响其余
                continue
            positions = positions_from_payload(payload)
            if not positions:
                continue
            out.setdefault((tenant, user_id), {}).update(positions)
        return out, orphans

    async def _load_manual_watchlist(
        self,
    ) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
        from sqlalchemy import text

        try:
            async with self._session() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT COALESCE(tenant_id, 'default') AS tid, user_id, "
                            "symbol, stock_name FROM qm_user_watchlist"
                        )
                    )
                ).fetchall()
        except Exception as exc:  # noqa: BLE001 - 自选表取不到只影响该源
            logger.warning("[holding_sentinel] 手工自选读取失败: %s", exc)
            return {}
        out: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for tid, uid, symbol, name in rows:
            sym = normalize_position_symbol(symbol)
            if not sym:
                continue
            out.setdefault((str(tid), str(uid)), {})[sym] = {"stockName": name}
        return out

    # ── 三类信号 ──

    async def _scan_scores(
        self,
        client: Any,
        user: dict[str, str],
        monitor: Mapping[str, dict[str, Any]],
        cfg: Mapping[str, Any],
        now_ts: float,
    ) -> list[dict[str, Any]]:
        tenant, user_id = user["tenant_id"], user["user_id"]
        score_map, meta = await self._score_loader(tenant)
        if not score_map:
            # 取不到分**不动基线**：否则下一轮会把「重新有分」当成变化，凭空报一轮
            logger.debug(
                "[holding_sentinel] 分数快照空 user=%s reason=%s",
                user_id,
                meta.get("reason"),
            )
            return []

        baseline_key = f"{BASELINE_KEY_PREFIX}{tenant}:{user_id}"
        try:
            stored = client.hgetall(baseline_key) or {}
        except Exception:  # noqa: BLE001 - 基线读不到 → 本轮只播种
            stored = {}

        current_as_of = str(meta.get("signal_date") or "")
        alerts: list[dict[str, Any]] = []
        new_baseline: dict[str, str] = {}
        for sym, entry in score_map.items():
            if sym not in monitor:
                continue
            now_score = entry.get("value")
            new_baseline[sym] = json.dumps(
                {"v": now_score, "d": entry.get("asOf") or current_as_of},
                ensure_ascii=False,
            )
            raw = stored.get(sym)
            if raw is None:
                continue  # 首次见到：只播种，不报警
            try:
                prev = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(prev, Mapping):
                continue
            if not baseline_is_comparable(prev.get("d"), entry.get("asOf")):
                continue  # 基线过期（哨兵停摆/换期）：重新播种，不把陈年下跌当今天
            verdict = evaluate_score_transition(
                prev.get("v"), now_score, cfg.get("score_threshold", 0.0)
            )
            if verdict is None:
                continue
            kind, severity = verdict
            freq = str(entry.get("freq") or "daily")
            watch = monitor.get(sym) or {}
            alerts.append(
                self._make_alert(
                    tenant=tenant,
                    user_id=user_id,
                    symbol=sym,
                    stock_name=watch.get("stockName"),
                    kind=kind,
                    severity=severity,
                    title=build_alert_title(
                        kind, watch.get("stockName"), sym, freq=freq
                    ),
                    content=build_alert_content(
                        kind=kind,
                        symbol=sym,
                        score_prev=prev.get("v"),
                        score_now=now_score,
                    ),
                    detail={
                        "freq": freq,
                        "side": entry.get("side"),
                        "score_as_of": entry.get("asOf"),
                        "baseline_as_of": prev.get("d"),
                        "sources": watch.get("sources") or [],
                    },
                    score_prev=prev.get("v"),
                    score_now=now_score,
                    score_as_of=entry.get("asOf") or current_as_of,
                    now_ts=now_ts,
                )
            )

        self._update_baseline(client, baseline_key, stored, new_baseline)
        return alerts

    def _update_baseline(
        self,
        client: Any,
        key: str,
        stored: Mapping[str, Any],
        new_baseline: Mapping[str, str],
    ) -> None:
        """写回基线：先补新值再删退出的标的（不留删除-写入之间的空窗）。"""
        try:
            if new_baseline:
                client.hset(key, mapping=dict(new_baseline))
            gone = [sym for sym in stored if sym not in new_baseline]
            if gone:
                client.hdel(key, *gone)
        except Exception as exc:  # noqa: BLE001 - 基线写失败只影响下一轮比较
            logger.warning("[holding_sentinel] 基线写回失败 %s: %s", key, exc)

    async def _scan_market_alerts(
        self,
        client: Any,
        tenant: str,
        user_id: str,
        monitor: Mapping[str, dict[str, Any]],
        now_ts: float,
    ) -> list[dict[str, Any]]:
        """`sentinel_alerts` 增量 → 监控集内标的的持仓预警。"""
        from sqlalchemy import text

        cursor_key = f"{RISK_CURSOR_KEY_PREFIX}{tenant}:intel"
        try:
            raw_cursor = client.get(cursor_key)
            cursor = int(raw_cursor) if raw_cursor else None
        except Exception:  # noqa: BLE001
            cursor = None

        try:
            async with self._session() as session:
                if cursor is None:
                    # 首次运行只播种游标：把历史告警一次性回放给用户等于开一场告警风暴
                    top = (
                        await session.execute(
                            text("SELECT COALESCE(MAX(id), 0) FROM sentinel_alerts")
                        )
                    ).scalar()
                    cursor = int(top or 0)
                    client.set(cursor_key, str(cursor))
                    return []
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, ts, symbol, targets, alert_type, severity, "
                            "title, direction FROM sentinel_alerts WHERE id > :cursor "
                            "ORDER BY id ASC LIMIT " + str(_MAX_MARKET_ROWS_PER_SCAN)
                        ),
                        {"cursor": cursor},
                    )
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[holding_sentinel] 市场告警读取失败: %s", exc)
            return []

        alerts: list[dict[str, Any]] = []
        max_id = cursor
        cutoff = now_ts - _INTEL_LOOKBACK_SECONDS
        for row in rows:
            max_id = max(max_id, int(row[0]))
            ts_val = row[1].timestamp() if hasattr(row[1], "timestamp") else 0.0
            if ts_val and ts_val < cutoff:
                continue  # 停摆太久：回放两天前的新闻没有意义
            verdict = classify_market_alert(row[4], row[7], row[5])
            if verdict is None:
                continue
            kind, severity = verdict
            hit = [s for s in target_symbols(row[2], row[3]) if s in monitor]
            if not hit:
                continue
            headline = str(row[6] or "").strip()
            for sym in hit:
                watch = monitor.get(sym) or {}
                alerts.append(
                    self._make_alert(
                        tenant=tenant,
                        user_id=user_id,
                        symbol=sym,
                        stock_name=watch.get("stockName"),
                        kind=kind,
                        severity=severity,
                        title=build_alert_title(kind, watch.get("stockName"), sym),
                        content=build_alert_content(
                            kind=kind, symbol=sym, extra=headline
                        ),
                        detail={
                            "market_alert_id": int(row[0]),
                            "market_alert_type": str(row[4]),
                            "headline": headline,
                            "sources": watch.get("sources") or [],
                        },
                        now_ts=now_ts,
                    )
                )
        if max_id > cursor:
            try:
                client.set(cursor_key, str(max_id))
            except Exception:  # noqa: BLE001 - 水位数不到 → 下轮重读（有 dedupe 兜底）
                pass
        return alerts

    async def _scan_exclusion(
        self,
        client: Any,
        tenant: str,
        user_id: str,
        monitor: Mapping[str, dict[str, Any]],
        now_ts: float,
    ) -> list[dict[str, Any]]:
        """排除名单**新增**命中（换版才比较，避免每天把在册持仓报一遍）。"""
        from backend.shared.exclusion_list import load_exclusion_list

        lst = load_exclusion_list("CN")
        if lst is None:
            return []  # 名单未导入 ≠ 空名单：不报，也不把「没名单」当「已检查」
        today = time.strftime("%Y-%m-%d", time.localtime(now_ts))
        blocking = set(lst.symbols(today=today))
        key = f"{RISK_CURSOR_KEY_PREFIX}{tenant}:exclusion"
        try:
            stored_raw = client.get(key)
            stored = json.loads(stored_raw) if stored_raw else None
        except Exception:  # noqa: BLE001 - 读不到水位 → 视为首轮，只播种
            stored = None

        prev_set = (
            set(stored.get("symbols") or []) if isinstance(stored, dict) else set()
        )
        alerts: list[dict[str, Any]] = []
        for suffix_code in new_symbols(prev_set, blocking):
            sym = normalize_position_symbol(suffix_code)
            if not sym or sym not in monitor:
                continue
            watch = monitor.get(sym) or {}
            alerts.append(
                self._make_alert(
                    tenant=tenant,
                    user_id=user_id,
                    symbol=sym,
                    stock_name=watch.get("stockName"),
                    kind=KIND_RISK_LIST,
                    severity=SEVERITY_WARNING,
                    title=build_alert_title(
                        KIND_RISK_LIST, watch.get("stockName"), sym
                    ),
                    content=build_alert_content(
                        kind=KIND_RISK_LIST,
                        symbol=sym,
                        extra=f"已进入排除名单（基准日 {lst.asof}）",
                    ),
                    detail={
                        "exclusion_asof": lst.asof,
                        "sources": watch.get("sources") or [],
                    },
                    now_ts=now_ts,
                )
            )
        try:
            client.set(key, json.dumps({"asof": lst.asof, "symbols": sorted(blocking)}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[holding_sentinel] 名单水位写回失败: %s", exc)
        return alerts

    # ── 报警装配与投递 ──

    def _make_alert(
        self,
        *,
        tenant: str,
        user_id: str,
        symbol: str,
        stock_name: str | None,
        kind: str,
        severity: str,
        title: str,
        content: str,
        detail: dict[str, Any],
        now_ts: float,
        score_prev: float | None = None,
        score_now: float | None = None,
        score_as_of: str | None = None,
    ) -> dict[str, Any]:
        return {
            "tenant_id": tenant,
            "user_id": user_id,
            "symbol": symbol,
            "stock_name": (str(stock_name or "").strip() or None),
            "kind": kind,
            "severity": severity,
            "title": title[:256],
            "content": content,
            "detail": detail,
            "score_prev": score_prev,
            "score_now": score_now,
            "score_as_of": (str(score_as_of)[:10] if score_as_of else None),
            "dedupe_key": make_holding_dedupe_key(
                tenant_id=tenant,
                user_id=user_id,
                symbol=symbol,
                kind=kind,
                bucket=cooldown_bucket(now_ts, COOLDOWN_SECONDS),
            ),
            "action_url": alert_action_url(symbol),
        }

    def _persist(self, alerts: list[dict[str, Any]]) -> dict[str, int]:
        """落库 + 投递站内通知（同步 I/O，调用方放进线程池）。

        每用户每轮封顶 ``MAX_ALERTS_PER_SCAN``：全市场异动叠加几十只持仓时，
        一次刷 200 条提醒等于没有提醒（用户会直接关掉开关）。
        """
        if not alerts:
            return {"recorded": 0, "notified": 0, "duplicates": 0}

        by_user: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for alert in alerts:
            by_user.setdefault((alert["tenant_id"], alert["user_id"]), []).append(alert)

        from sqlalchemy import text

        from backend.shared.holding_alert_contract import TABLE
        from backend.shared.sync_db import sync_session

        insert_sql = text(
            f"INSERT INTO {TABLE} (tenant_id, user_id, symbol, stock_name, kind, "
            "severity, source, title, content, detail, score_prev, score_now, "
            "score_as_of, dedupe_key, status, action_url) "
            "VALUES (:tenant_id, :user_id, :symbol, :stock_name, :kind, :severity, "
            "'holding_sentinel', :title, :content, CAST(:detail AS jsonb), "
            ":score_prev, :score_now, :score_as_of, :dedupe_key, :status, :action_url) "
            "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id"
        )

        recorded = notified = duplicates = 0
        for (tenant, user_id), group in by_user.items():
            cfg = parse_alert_config(self._config_raw(tenant, user_id))
            for alert in dedupe_alert_rows(group)[:MAX_ALERTS_PER_SCAN]:
                try:
                    with sync_session() as session:
                        row = session.execute(
                            insert_sql, self._insert_params(alert)
                        ).first()
                        session.commit()
                except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整轮
                    logger.warning(
                        "[holding_sentinel] 预警落库失败 %s/%s: %s",
                        user_id,
                        alert["symbol"],
                        exc,
                    )
                    continue
                if row is None:
                    duplicates += 1  # 冷却期内已报过：留痕已有，不重复推送
                    continue
                recorded += 1
                if meets_min_severity(
                    str(alert["severity"]), str(cfg["min_severity"])
                ) and self._notify(alert, cfg):
                    notified += 1
        return {"recorded": recorded, "notified": notified, "duplicates": duplicates}

    @staticmethod
    def _insert_params(alert: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **alert,
            "detail": json.dumps(alert["detail"], ensure_ascii=False),
            "status": STATUS_ACTIVE,
        }

    def _config_raw(self, tenant: str, user_id: str) -> Any:
        try:
            return self._client().hget(
                f"{CONFIG_KEY_PREFIX}{tenant}:{user_id}", "settings"
            )
        except Exception:  # noqa: BLE001 - 读不到配置就用默认（默认是「全开」）
            return None

    def _notify(self, alert: Mapping[str, Any], cfg: Mapping[str, Any]) -> bool:
        """站内通知（三通道里的「站内」；桌面/声音由前端按同一开关播）。"""
        if not cfg.get("notify_inapp", True):
            return False
        from backend.shared.notification_publisher import publish_notification

        try:
            return bool(
                publish_notification(
                    user_id=str(alert["user_id"]),
                    tenant_id=str(alert["tenant_id"]),
                    title=str(alert["title"]),
                    content=str(alert["content"]),
                    type="holding_alert",
                    level=notification_level(str(alert["severity"])),
                    action_url=str(alert["action_url"]),
                    expire_days=7,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响留痕
            logger.warning("[holding_sentinel] 通知投递失败: %s", exc)
            return False

    async def _write_status(self, client: Any, status: Mapping[str, Any]) -> None:
        try:
            payload = {
                "updated_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z", time.localtime(self._now_fn())
                ),
                "last_scan_epoch": int(self._now_fn()),
                **{k: v for k, v in status.items() if k != "per_user"},
            }
            client.hset(STATUS_KEY, mapping={k: str(v) for k, v in payload.items()})
            client.expire(STATUS_KEY, 86400)
            if status.get("per_user"):
                client.set(
                    f"{STATUS_KEY}:users",
                    json.dumps(status["per_user"], ensure_ascii=False),
                    ex=86400,
                )
        except Exception as exc:  # noqa: BLE001 - 状态只是可观测性
            logger.debug("[holding_sentinel] 状态写回失败: %s", exc)


async def run_holding_sentinel_worker() -> None:
    """常驻扫描循环（trade 服务注册）。循环永不退出，单轮失败只告警。"""
    sentinel = HoldingSentinel()
    logger.info("[holding_sentinel] 持仓哨兵启动（节拍 %ss）", SCAN_INTERVAL_SECONDS)
    while True:
        try:
            from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

            _sched_heartbeat(SCHEDULER_NAME)
        except Exception:  # noqa: BLE001 - 心跳失败不影响扫描
            pass
        try:
            await sentinel.run_once()
        except Exception as exc:  # noqa: BLE001 - 循环永不退出
            logger.warning("[holding_sentinel] 扫描失败: %s", exc, exc_info=True)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
