#!/usr/bin/env python3
"""
QuantMind Real-time Trading Runner
核心执行器：负责消费信号、风控校验、执行下单及状态回写。
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import redis

try:
    from backend.shared.auth import get_internal_call_secret as _shared_internal_call_secret
except Exception:  # pragma: no cover - runner image keeps auth deps minimal
    _shared_internal_call_secret = None

try:
    from exchange_calendars import get_calendar
except ImportError:  # pragma: no cover - optional dependency in local/unit test env
    get_calendar = None


# --- 结构化日志配置 (RC2 增强) ---
class CloudJsonFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pod_name": os.getenv("HOSTNAME", "local-runner"),
        }
        # 提取业务元数据
        if hasattr(record, "extra_data"):
            log_entry.update(record.extra_data)
        return json.dumps(log_entry)


# 强制替换标准输出的格式
root_logger = logging.getLogger()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(CloudJsonFormatter())
root_logger.handlers = [handler]
root_logger.setLevel(logging.INFO)

logger = logging.getLogger("quantmind.realtime.runner")


def log_business(message, extra: dict, level=logging.INFO):
    """辅助函数：记录带元数据的业务日志"""
    logger.log(level, message, extra={"extra_data": extra})


# -------------------------------

start_time = time.time()
_RUNNER_TZ = ZoneInfo(os.getenv("RUNNER_TIMEZONE", "Asia/Shanghai"))

_DEFAULT_LIVE_TRADE_CONFIG = {
    "rebalance_days": 3,
    "schedule_type": "interval",
    "trade_weekdays": [],
    "enabled_sessions": ["PM"],
    "sell_time": "14:45",
    "buy_time": "14:50",
    "sell_first": True,
    "order_type": "MARKET",
    "max_price_deviation": 0.02,
    "max_orders_per_cycle": 20,
}


def _safe_json_loads(val: Any, default: Any) -> Any:
    if not val:
        return default
    try:
        return json.loads(val)
    except Exception:
        return default


def get_internal_call_secret() -> str:
    """Runner 优先直接读取环境变量，避免依赖完整认证模块。"""
    if _shared_internal_call_secret is not None:
        try:
            return str(_shared_internal_call_secret()).strip()
        except Exception:
            pass

    # 不再回落公开默认值，也不拿 SECRET_KEY（用户 JWT 签名密钥）当内部调用密钥使——
    # 两者是独立信任链，串用会让任一处的泄露扩散到另一处。
    return str(os.getenv("INTERNAL_CALL_SECRET") or "").strip()


def _headers(user_id: str, tenant_id: str) -> dict[str, str]:
    return {
        "X-User-Id": str(user_id),
        "X-Tenant-Id": str(tenant_id),
        "X-Internal-Call": get_internal_call_secret(),
        "Content-Type": "application/json",
    }


def _load_live_trade_config() -> dict[str, Any]:
    raw = os.getenv("LIVE_TRADE_CONFIG", "")
    cfg = _safe_json_loads(raw, {})
    merged = {**_DEFAULT_LIVE_TRADE_CONFIG, **(cfg or {})}
    merged["schedule_type"] = str(merged.get("schedule_type") or "interval").lower()
    merged["trade_weekdays"] = [str(item).upper() for item in (merged.get("trade_weekdays") or [])]
    merged["enabled_sessions"] = [str(item).upper() for item in (merged.get("enabled_sessions") or ["PM"])]
    merged["order_type"] = str(merged.get("order_type") or "MARKET").upper()
    merged["rebalance_days"] = int(merged.get("rebalance_days") or 3)
    merged["max_orders_per_cycle"] = int(merged.get("max_orders_per_cycle") or 20)
    merged["sell_first"] = bool(merged.get("sell_first", True))
    return merged


def _load_execution_config() -> dict[str, Any]:
    raw = os.getenv("EXECUTION_CONFIG", "")
    cfg = _safe_json_loads(raw, {})
    if not isinstance(cfg, dict):
        cfg = {}
    return dict(cfg)


def _current_local_ts():
    return time.time()


def _runner_local_dt(ts_value: float) -> datetime:
    return datetime.fromtimestamp(ts_value, tz=_RUNNER_TZ)


def _is_rebalance_day(ts_value: float, live_trade_config: dict[str, Any]) -> bool:
    local_dt = _runner_local_dt(ts_value)
    schedule_type = str(live_trade_config.get("schedule_type") or "interval").lower()
    if schedule_type == "weekly":
        weekday = local_dt.strftime("%a").upper()[:3]
        return weekday in set(live_trade_config.get("trade_weekdays") or [])

    rebalance_days = max(1, int(live_trade_config.get("rebalance_days") or 5))
    try:
        if get_calendar is None:
            raise RuntimeError("exchange_calendars unavailable")
        calendar = get_calendar("XSHG")
        session = calendar.date_to_session(pd.Timestamp(local_dt.date()), direction="previous")
        idx = int(calendar.sessions.get_loc(session))
        return idx % rebalance_days == 0
    except Exception:
        day_of_year = int(local_dt.strftime("%j"))
        return day_of_year % rebalance_days == 0


def _current_phase(now_hhmm: str, live_trade_config: dict[str, Any]) -> str:
    sell_time = str(live_trade_config.get("sell_time") or "14:45")
    buy_time = str(live_trade_config.get("buy_time") or "14:50")
    if now_hhmm < sell_time:
        return "IDLE"
    if sell_time <= now_hhmm < buy_time:
        return "SELL" if live_trade_config.get("sell_first", True) else "BUY"
    return "BUY"


def _is_within_enabled_session(now_hhmm: str, live_trade_config: dict[str, Any]) -> bool:
    enabled = set(live_trade_config.get("enabled_sessions") or [])
    if "AM" in enabled and "09:30" <= now_hhmm <= "11:30":
        return True
    if "PM" in enabled and "13:00" <= now_hhmm <= "15:00":
        return True
    return False


def _filter_signals_by_phase(signals: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    def _resolve_action(sig: dict[str, Any]) -> str:
        trade_action = str(sig.get("trade_action") or "").upper()
        if trade_action in {"SELL_TO_CLOSE", "SELL_TO_OPEN"}:
            return "SELL"
        if trade_action in {"BUY_TO_OPEN", "BUY_TO_CLOSE"}:
            return "BUY"
        return str(sig.get("action") or "").upper()

    if phase == "SELL":
        return [s for s in signals if _resolve_action(s) == "SELL"]
    if phase == "BUY":
        return [s for s in signals if _resolve_action(s) == "BUY"]
    if phase == "ALL":
        return signals
    return []


def _signal_stream_name(tenant_id: str) -> str:
    return f"qm:signal:stream:{tenant_id}"


def _latest_signal_run_key(tenant_id: str, user_id: str) -> str:
    return f"qm:signal:latest:{tenant_id}:{user_id}"


def _get_latest_signal_run_id(redis_client: redis.Redis, tenant_id: str, user_id: str) -> str | None:
    try:
        latest = str(redis_client.get(_latest_signal_run_key(tenant_id, user_id)) or "").strip()
        return latest or None
    except Exception as e:
        logger.warning("[SignalStream] 读取最新推理版本失败: %s", e)
        return None


def _signal_stream_group_name(tenant_id: str, user_id: str, strategy: str, exec_config: dict[str, Any]) -> str:
    return exec_config.get("signal_stream_group") or f"signal-runners-{tenant_id}-{user_id}"


def _signal_stream_consumer_name(tenant_id: str, user_id: str, strategy: str, exec_config: dict[str, Any]) -> str:
    pod_name = os.getenv("HOSTNAME", "local-runner")
    return f"runner-{pod_name}"


def _ensure_signal_stream_group(
    redis_client: redis.Redis, tenant_id: str, user_id: str, strategy: str, exec_config: dict[str, Any]
):
    stream = _signal_stream_name(tenant_id)
    group = _signal_stream_group_name(tenant_id, user_id, strategy, exec_config)
    try:
        redis_client.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("[SignalStream] 消费者组创建成功: %s", group)
    except redis.exceptions.ResponseError as e:
        if "already exists" in str(e):
            pass
        else:
            logger.warning("[SignalStream] xgroup_create 失败: %s", e)


def fetch_market_snapshot(redis_client: redis.Redis) -> dict[str, Any]:
    try:
        data = redis_client.get("market:snapshot")
        return _safe_json_loads(data, {})
    except Exception as e:
        logger.error("获取行情快照失败: %s", e)
        return {}


def _fetch_account_state(user_id: str, tenant_id: str) -> dict[str, Any]:
    base_url = os.getenv("TRADE_SERVICE_INTERNAL_URL", "http://quantmind-trade:8002/api/v1/internal/strategy").rstrip(
        "/"
    )
    url = f"{base_url}/sync-account"
    try:
        resp = requests.get(url, headers=_headers(user_id, tenant_id), timeout=3)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("[Risk] 获取账户状态失败，使用空账户降级: %s", e)
        return {"cash": 0, "total_value": 0, "positions": {}, "drawdown": 0}


from backend.services.trade.runner.risk_gate import RiskGate


def _to_float(value):
    """向后兼容保留，内部已迁移至 risk_gate.py。"""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _apply_portfolio_risk_gate(signals, account, exec_config, market_snapshot, live_trade_config=None):
    """委托 RiskGate.apply()，保留此名称以兼容已有调用点。"""
    return RiskGate.apply(signals, account, exec_config, market_snapshot, live_trade_config)


def _signal_fingerprint(signals):
    return RiskGate.fingerprint(signals)


def _acquire_idempotency_lock(redis_client, tenant_id, user_id, strategy, fingerprint, ttl_seconds):
    return RiskGate.acquire_lock(redis_client, tenant_id, user_id, strategy, fingerprint, ttl_seconds)


def _report_dispatch_item_status(
    batch_id: str,
    run_id: str,
    tenant_id: str,
    user_id: str,
    signal: dict[str, Any],
    status: str,
    order_id: str | None = None,
    error_msg: str | None = None,
) -> None:
    if not batch_id or not run_id:
        return
    base_url = os.getenv("ENGINE_SERVICE_INTERNAL_URL", "http://quantmind-engine:8001/api/v1").rstrip("/")
    url = f"{base_url}/dispatch/{batch_id}/items/upsert"
    payload = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "trade_date": time.strftime("%Y-%m-%d"),
        "items": [
            {
                "run_id": run_id,
                "signal_id": signal.get("signal_id"),
                "client_order_id": signal.get("client_order_id"),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "trade_date": time.strftime("%Y-%m-%d"),
                "symbol": signal.get("symbol"),
                "action": signal.get("action"),
                "quantity": float(signal.get("volume") or 0),
                "price": float(signal.get("price") or 0),
                "score": float(signal.get("score") or 0),
                "dispatch_status": status,
                "order_id": order_id,
                "exec_message": error_msg,
            }
        ],
    }
    try:
        requests.post(url, json=payload, headers=_headers(user_id, tenant_id), timeout=3)
    except Exception as e:
        logger.warning("[E2E] 状态机回写失败: %s", e)


def _consume_signal_events(
    user_id: str,
    tenant_id: str,
    strategy: str,
    redis_client: redis.Redis,
    exec_config: dict[str, Any],
    latest_run_id: str | None = None,
    last_id: str = ">",
) -> tuple[list[dict[str, Any]], list[str]]:
    stream = _signal_stream_name(tenant_id)
    group = _signal_stream_group_name(tenant_id, user_id, strategy, exec_config)
    consumer = _signal_stream_consumer_name(tenant_id, user_id, strategy, exec_config)
    batch_size = int(exec_config.get("signal_stream_batch_size", 100))
    block_ms = int(exec_config.get("signal_stream_block_ms", 1000)) if last_id == ">" else None

    records = redis_client.xreadgroup(group, consumer, {stream: last_id}, count=batch_size, block=block_ms)
    if not records:
        return [], []

    signals, ack_ids = [], []
    for _, messages in records:
        for mid, fields in messages:
            try:
                if str(fields.get("user_id")) != str(user_id):
                    ack_ids.append(mid)
                    continue
                run_id = str(fields.get("run_id") or "").strip()
                if latest_run_id and run_id and run_id != str(latest_run_id):
                    logger.info(
                        "[SignalStream] 丢弃过期信号: stream=%s msg=%s run_id=%s latest_run_id=%s",
                        stream,
                        mid,
                        run_id,
                        latest_run_id,
                    )
                    ack_ids.append(mid)
                    continue
                symbol = str(fields.get("symbol")).upper()
                side = str(fields.get("side")).upper()
                if side not in {"BUY", "SELL"}:
                    ack_ids.append(mid)
                    continue
                signals.append(
                    {
                        "signal_id": str(fields.get("signal_id") or ""),
                        "batch_id": str(fields.get("batch_id") or ""),
                        "run_id": run_id,
                        "client_order_id": str(fields.get("client_order_id") or ""),
                        "symbol": symbol,
                        "action": side,
                        "trade_action": str(fields.get("trade_action") or "") or None,
                        "position_side": str(fields.get("position_side") or "") or None,
                        "is_margin_trade": str(fields.get("is_margin_trade") or "").lower()
                        in {"1", "true", "yes", "on"},
                        "volume": int(float(fields.get("quantity") or 0)),
                        "price": float(fields.get("price") or 0),
                        "score": float(fields.get("score") or 0),
                        "_stream_message_id": mid,
                    }
                )
                ack_ids.append(mid)
            except Exception as e:
                logger.warning("[SignalStream] 无效消息 %s: %s", mid, e)
                ack_ids.append(mid)
    return signals, ack_ids


def create_hosted_execution_task(
    user_id: str,
    tenant_id: str,
    strategy: str,
    redis_client: redis.Redis,
    exec_config: dict[str, Any],
    live_trade_config: dict[str, Any],
    trigger_context: dict[str, Any],
    task_id: str,
):
    base_url = os.getenv("TRADE_SERVICE_INTERNAL_URL", "http://quantmind-trade:8002/api/v1/internal/strategy").rstrip(
        "/"
    )
    trade_api = f"{base_url}/hosted-executions"
    mode = str(exec_config.get("trading_mode", "REAL")).upper()
    payload = {
        "task_id": task_id,
        "strategy_id": strategy,
        "trading_mode": mode,
        "execution_config": exec_config,
        "live_trade_config": live_trade_config,
        "signals": [],
        "trigger_context": trigger_context,
        "parent_runtime_id": str(os.getenv("RUN_ID") or "").strip() or None,
    }
    try:
        log_business(
            f"[{mode}] 创建自动托管任务",
            {
                "task_id": task_id,
                "strategy_id": strategy,
                "trigger_context": trigger_context,
            },
        )
        resp = requests.post(trade_api, json=payload, headers=_headers(user_id, tenant_id), timeout=8)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log_business(
            f"创建自动托管任务失败: {str(e)}",
            {"task_id": task_id, "strategy_id": strategy, "error": str(e)},
            level=logging.ERROR,
        )
        return None


def _ack_signal_events(
    redis_client: redis.Redis,
    tenant_id: str,
    user_id: str,
    strategy: str,
    exec_config: dict[str, Any],
    ack_ids: list[str],
):
    if not ack_ids:
        return
    stream = _signal_stream_name(tenant_id)
    group = _signal_stream_group_name(tenant_id, user_id, strategy, exec_config)
    try:
        redis_client.xack(stream, group, *ack_ids)
    except Exception as e:
        logger.warning("[SignalStream] ACK 失败: %s", e)


def _build_hosted_runner_task_id(
    tenant_id: str,
    user_id: str,
    strategy: str,
    trade_date: str,
    phase: str,
) -> str:
    source = json.dumps(
        {
            "tenant_id": str(tenant_id or "default"),
            "user_id": str(user_id or ""),
            "strategy": str(strategy or ""),
            "trade_date": str(trade_date or ""),
            "phase": str(phase or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    return f"hosted_{digest}"


def _hosted_trigger_lock_key(
    tenant_id: str,
    user_id: str,
    strategy: str,
    trade_date: str,
    phase: str,
) -> str:
    return f"qm:hosted:runner:{tenant_id}:{user_id}:{strategy}:{trade_date}:{phase}"


def process_cycle(
    user_id: str,
    tenant_id: str,
    strategy: str,
    redis_client: redis.Redis,
    exec_config: dict[str, Any],
    live_trade_config: dict[str, Any],
):
    test_mode = str(os.getenv("RUNNER_TEST_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
    current_ts = _current_local_ts()
    now_hhmm = _runner_local_dt(current_ts).strftime("%H:%M")
    is_rebalance_day = _is_rebalance_day(current_ts, live_trade_config)
    phase = _current_phase(now_hhmm, live_trade_config)

    if test_mode:
        # 测试模式：绕过交易日/交易时段门禁，强制消费当前流消息。
        is_rebalance_day = True
        phase = "ALL"

    log_business(
        "执行窗口判定",
        {
            "phase": phase,
            "is_rebalance_day": is_rebalance_day,
            "time": now_hhmm,
            "live_trade_config": live_trade_config,
            "test_mode": test_mode,
        },
        level=logging.DEBUG,
    )

    if not test_mode and (
        not is_rebalance_day or not _is_within_enabled_session(now_hhmm, live_trade_config) or phase == "IDLE"
    ):
        return False

    trade_date = _runner_local_dt(current_ts).date().isoformat()
    lock_key = _hosted_trigger_lock_key(tenant_id, user_id, strategy, trade_date, phase)
    try:
        if redis_client.get(lock_key):
            logger.info(
                "[HostedRunner] 已存在触发锁，跳过重复调度: tenant=%s user=%s strategy=%s date=%s phase=%s",
                tenant_id,
                user_id,
                strategy,
                trade_date,
                phase,
            )
            return False
    except Exception as e:
        logger.warning("[HostedRunner] 读取调度锁失败: %s", e)

    task_id = _build_hosted_runner_task_id(tenant_id, user_id, strategy, trade_date, phase)
    trigger_context = {
        "schedule_type": str(live_trade_config.get("schedule_type") or "interval"),
        "phase": phase,
        "triggered_at": datetime.now(tz=_RUNNER_TZ).isoformat(),
        "rebalance_day": bool(is_rebalance_day),
        "runner_trade_date": trade_date,
        "task_id": task_id,
        "runner_mode": str(exec_config.get("trading_mode") or "REAL").upper(),
    }
    result = create_hosted_execution_task(
        user_id,
        tenant_id,
        strategy,
        redis_client,
        exec_config,
        live_trade_config,
        trigger_context,
        task_id=task_id,
    )
    if not result:
        return False  # 表示本周期没有处理任务

    try:
        redis_client.set(lock_key, task_id, ex=86400, nx=True)
    except Exception as e:
        logger.warning("[HostedRunner] 写入调度锁失败: %s", e)
    return True


def _build_redis_client() -> redis.Redis:
    host = os.getenv("REDIS_HOST", "quantmind-trade-redis")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD", "quantmind2026")
    db = int(os.getenv("REDIS_DB", "0"))
    return redis.Redis(
        host=host,
        port=port,
        password=password,
        db=db,
        decode_responses=True,
        socket_timeout=2,
    )


def _resolve_runner_identity(args: argparse.Namespace) -> tuple[str, str, str] | None:
    user_id = str(
        getattr(args, "user_id", None)
        or os.getenv("USER_ID")
        or os.getenv("RUNNER_USER_ID")
        or ""
    ).strip()
    strategy = str(
        getattr(args, "strategy", None)
        or os.getenv("STRATEGY_ID")
        or os.getenv("RUNNER_STRATEGY_ID")
        or ""
    ).strip()
    tenant_id = str(
        getattr(args, "tenant_id", None)
        or os.getenv("TENANT_ID")
        or os.getenv("RUNNER_TENANT_ID")
        or "default"
    ).strip() or "default"
    if not user_id or not strategy:
        logger.error(
            "缺少必要参数: user_id 或 strategy (可通过命令行或环境变量提供)"
        )
        return None
    return user_id, strategy, tenant_id


def _run_scheduler_once(
    *,
    user_id: str,
    tenant_id: str,
    strategy: str,
    redis_client: redis.Redis,
    exec_config: dict[str, Any],
    live_trade_config: dict[str, Any],
) -> bool:
    return process_cycle(
        user_id=user_id,
        tenant_id=tenant_id,
        strategy=strategy,
        redis_client=redis_client,
        exec_config=exec_config,
        live_trade_config=live_trade_config,
    )


def _run_scheduler_loop(
    *,
    user_id: str,
    tenant_id: str,
    strategy: str,
    redis_client: redis.Redis,
    exec_config: dict[str, Any],
    live_trade_config: dict[str, Any],
    poll_interval_seconds: int,
    once: bool = False,
) -> None:
    interval = max(5, int(poll_interval_seconds or 30))
    logger.info(
        "[HostedRunner] 调度器启动 user_id=%s tenant_id=%s strategy=%s poll_interval=%ss",
        user_id,
        tenant_id,
        strategy,
        interval,
    )
    while True:
        try:
            _run_scheduler_once(
                user_id=user_id,
                tenant_id=tenant_id,
                strategy=strategy,
                redis_client=redis_client,
                exec_config=exec_config,
                live_trade_config=live_trade_config,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error("[HostedRunner] 调度循环失败: %s", exc, exc_info=True)
        if once:
            break
        time.sleep(interval)


def _bootstrap_from_cli() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--user_id", type=str, default=None)
    parser.add_argument("--strategy", type=str, default=None)
    parser.add_argument("--tenant_id", type=str, default=None)
    parser.add_argument("--poll_interval_seconds", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    identity = _resolve_runner_identity(args)
    if identity is None:
        return 0

    user_id, strategy, tenant_id = identity
    exec_config = _load_execution_config()
    live_trade_config = _load_live_trade_config()
    poll_interval = int(
        getattr(args, "poll_interval_seconds", None)
        or os.getenv("RUNNER_POLL_INTERVAL_SECONDS")
        or 30
    )
    redis_client = _build_redis_client()
    _run_scheduler_loop(
        user_id=user_id,
        tenant_id=tenant_id,
        strategy=strategy,
        redis_client=redis_client,
        exec_config=exec_config,
        live_trade_config=live_trade_config,
        poll_interval_seconds=poll_interval,
        once=bool(args.once),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_bootstrap_from_cli())
