# -*- coding: utf-8 -*-
"""minibt 模拟盘侧车（运行在 quantmind-minibt-runner 镜像内，Python 3.12）。

职责只有一件：**收到请求 → 跑一次 minibt 策略 → 交出目标持仓股数**。
不直接下单、不写台账、不碰账户资金——撮合与账本全部留在交易进程。

为什么是侧车而不是进程内：主容器是 Python 3.10，minibt 1.3.x 要求 3.12；
把它放进 runner 镜像既满足版本，也把用户策略代码关进独立容器（backend/data
只读挂载），比沙箱在交易进程内 exec 更小的爆炸半径。

协议（键由 ``backend.shared.minibt_sim_keys`` 唯一构造）：

- 轮询请求键；取到后计算目标，写目标键，删除请求键；
- 心跳键每 30s 续期；容器停止由 docker stop（SIGTERM）触发；
- 计算在 **spawn 子进程** 里跑，超时即杀——用户代码死循环不能拖死心跳。

单独调试（不接调度器）：

    QM_MINIBT_SIM_ONCE=1 QM_MINIBT_SIM_TRADE_DATE=2026-10-26 \
    python -m backend.shared.minibt_sim_agent
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import redis

from backend.shared.minibt_sim_keys import (
    heartbeat_key,
    parse_symbol_constant,
    request_key,
    target_key,
    trade_date_from_iso,
)

DEFAULT_POLL_SECONDS = 2.0
DEFAULT_COMPUTE_TIMEOUT_SECONDS = 180.0
DEFAULT_LIVE_MAX_AGE_SECONDS = 180.0
DEFAULT_TARGET_TTL_SECONDS = 3600
HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_TTL_SECONDS = 120


def _log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[minibt-sim {stamp}] {message}", flush=True)


class AgentError(RuntimeError):
    """侧车内部可读错误（原因串会原样写进目标键）。"""


@dataclass(frozen=True)
class AgentConfig:
    code_path: str
    tenant_id: str
    user_id: str
    strategy_id: str
    run_id: str
    poll_seconds: float
    compute_timeout_seconds: float
    live_max_age_seconds: float
    target_ttl_seconds: int

    @property
    def request_key(self) -> str:
        return request_key(self.tenant_id, self.user_id, self.strategy_id)

    @property
    def target_key(self) -> str:
        return target_key(self.tenant_id, self.user_id, self.strategy_id)

    @property
    def heartbeat_key(self) -> str:
        return heartbeat_key(self.tenant_id, self.user_id, self.strategy_id)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


def load_config() -> AgentConfig:
    tenant_id = str(os.getenv("QM_MINIBT_SIM_TENANT", "")).strip()
    user_id = str(os.getenv("QM_MINIBT_SIM_USER", "")).strip()
    strategy_id = str(os.getenv("QM_MINIBT_SIM_STRATEGY", "")).strip()
    run_id = str(os.getenv("QM_MINIBT_SIM_RUN_ID", "")).strip()
    code_path = (
        str(os.getenv("QM_MINIBT_SIM_CODE", "/app/strategy.py")).strip()
        or "/app/strategy.py"
    )
    missing = [
        name
        for name, value in (
            ("QM_MINIBT_SIM_TENANT", tenant_id),
            ("QM_MINIBT_SIM_USER", user_id),
            ("QM_MINIBT_SIM_STRATEGY", strategy_id),
            ("QM_MINIBT_SIM_RUN_ID", run_id),
        )
        if not value
    ]
    if missing:
        raise AgentError(f"missing_env:{','.join(missing)}")
    return AgentConfig(
        code_path=code_path,
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
        run_id=run_id,
        poll_seconds=max(
            0.5, _env_float("QM_MINIBT_SIM_POLL_SECONDS", DEFAULT_POLL_SECONDS)
        ),
        compute_timeout_seconds=max(
            5.0,
            _env_float(
                "QM_MINIBT_SIM_COMPUTE_TIMEOUT", DEFAULT_COMPUTE_TIMEOUT_SECONDS
            ),
        ),
        live_max_age_seconds=max(
            10.0,
            _env_float("QM_MINIBT_SIM_LIVE_MAX_AGE", DEFAULT_LIVE_MAX_AGE_SECONDS),
        ),
        target_ttl_seconds=max(
            60, _env_int("QM_MINIBT_SIM_TARGET_TTL", DEFAULT_TARGET_TTL_SECONDS)
        ),
    )


def _trade_redis() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", "6379") or 6379),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_TRADE", "2") or 2),
        decode_responses=True,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )


# ---------------------------------------------------------------------------
# 子进程：执行策略并提取最后一根 K 线的持仓
# ---------------------------------------------------------------------------
def _extract_last_position(bt: Any) -> tuple[float, int]:
    """bt → (最后持仓股数, 结果行数)。未跑完返回 (nan, 0)。"""
    results = None
    try:
        results = bt.get_results()
    except Exception:  # noqa: BLE001 - 未跑完时 minibt 内部会打印并返回 None
        results = None
    if not results:
        return float("nan"), 0
    frame = results[0][0] if results and results[0] else None
    if frame is None or len(frame) == 0 or "positions" not in frame.columns:
        return float("nan"), 0
    positions = frame["positions"].astype(float)
    return float(positions.iloc[-1]), int(len(frame))


def _find_strategy_class(namespace: dict[str, Any]) -> Any:
    from minibt import Strategy

    for value in namespace.values():
        if (
            isinstance(value, type)
            and issubclass(value, Strategy)
            and value is not Strategy
        ):
            return value
    return None


def _run_strategy(
    code_path: str, symbol: str, trade_date: date, max_age: float
) -> dict[str, Any]:
    """在当前进程执行策略（由 spawn 子进程调用）。"""
    import backend.shared.minibt_qdb as qdb
    from backend.shared.minibt_sim_data import (
        LiveBarUnavailable,
        append_live_bar,
        read_live_quote,
    )
    from backend.shared.remote_quote_config import make_sync_client
    from backend.shared.stock_utils import StockCodeUtil

    suffix = StockCodeUtil.to_suffix(symbol) or symbol
    original_load_daily = qdb.load_daily

    def patched_load_daily(symbol_arg, start=None, end=None, **kwargs):
        # END 常量在模板里是写死的（回测结束日）；模拟盘必须延伸到今天，
        # 由实时快照补出今天这根 K 线。START 仍尊重策略自己的设置。
        frame = original_load_daily(symbol_arg, start, None, **kwargs)
        # 收盘后 QuantDB 已含今天时直接返回：不读行情服，也不受快照断流影响。
        import pandas as pd

        if (
            frame is not None
            and not frame.empty
            and "datetime" in frame.columns
            and pd.Timestamp(frame["datetime"].iloc[-1]).date() >= trade_date
        ):
            frame.attrs["bar_source"] = "history_has_today"
            return frame
        client = make_sync_client()
        if client is None:
            raise LiveBarUnavailable("remote_quote_disabled")
        snapshot = read_live_quote(client, symbol_arg)
        merged, source = append_live_bar(
            frame,
            snapshot=snapshot,
            symbol=str(symbol_arg),
            trade_date=trade_date,
            now_ts=time.time(),
            max_age_seconds=max_age,
        )
        merged.attrs["bar_source"] = source
        return merged

    qdb.load_daily = patched_load_daily
    with open(code_path, encoding="utf-8") as handle:
        code = handle.read()

    namespace: dict[str, Any] = {"__name__": "__main__", "__file__": code_path}
    exec(compile(code, code_path, "exec"), namespace)  # noqa: S102 - 用户策略，容器隔离

    bt = namespace.get("bt")
    if bt is None:
        from backend.shared.minibt_result import run_and_report
        from minibt import Bt

        strategy_class = _find_strategy_class(namespace)
        if strategy_class is None:
            raise AgentError("no_strategy_class")
        source_df = namespace.get("DF")
        if source_df is None:
            source_df = patched_load_daily(suffix)
        bt = Bt(auto=False)
        bt.addstrategy(strategy_class)
        run_and_report(bt, source_df)

    position, bars = _extract_last_position(bt)
    if bars == 0 or position != position:  # NaN
        # 策略没有走 run_and_report（例如只定义类、无 __main__ 块）时补跑一次
        bt.run(isplot=False, isreport=False)
        position, bars = _extract_last_position(bt)
    if bars == 0 or position != position:
        raise AgentError("backtest_no_result")

    source_df = namespace.get("DF")
    if source_df is None:
        source_df = patched_load_daily(suffix)
    if source_df is None or len(source_df) == 0:
        raise AgentError("data_empty")
    last_bar = source_df["datetime"].iloc[-1]
    last_close = float(source_df["close"].iloc[-1])
    bar_date = last_bar.date() if hasattr(last_bar, "date") else trade_date
    if bar_date != trade_date:
        raise AgentError(f"bar_date_mismatch:{bar_date}!=trade_date:{trade_date}")
    notes: list[str] = []
    target_shares = int(round(position))
    if target_shares < 0:
        notes.append("short_unsupported")
        target_shares = 0
    return {
        "symbol": suffix,
        "target_shares": target_shares,
        "position_raw": float(position),
        "price": last_close,
        "bar_date": bar_date.isoformat(),
        "bar_source": str(source_df.attrs.get("bar_source") or "unknown"),
        "bars": bars,
        "notes": notes,
    }


def _child_entry(job: dict[str, Any], conn: Any) -> None:
    try:
        result = _run_strategy(
            code_path=str(job["code_path"]),
            symbol=str(job["symbol"]),
            trade_date=date.fromisoformat(str(job["trade_date"])),
            max_age=float(job["max_age"]),
        )
        conn.send({"ok": True, "result": result})
    except Exception as exc:  # noqa: BLE001 - 子进程必须把失败原样送回
        conn.send(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "traceback": traceback.format_exc(limit=8)[-2000:],
            }
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _compute_with_timeout(
    cfg: AgentConfig, symbol: str, trade_date: date
) -> dict[str, Any]:
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    job = {
        "code_path": cfg.code_path,
        "symbol": symbol,
        "trade_date": trade_date.isoformat(),
        "max_age": cfg.live_max_age_seconds,
    }
    process = ctx.Process(target=_child_entry, args=(job, child_conn), daemon=True)
    process.start()
    child_conn.close()
    process.join(cfg.compute_timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
        raise AgentError(f"compute_timeout:{cfg.compute_timeout_seconds:.0f}s")
    if not parent_conn.poll(2.0):
        raise AgentError(f"worker_crashed:exit={process.exitcode}")
    message = parent_conn.recv()
    if not message.get("ok"):
        raise AgentError(str(message.get("error") or "worker_error"))
    return dict(message.get("result") or {})


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def _handle_request(
    cfg: AgentConfig, client: redis.Redis, request: dict[str, Any]
) -> None:
    request_id = str(request.get("request_id") or "")
    trade_date = trade_date_from_iso(request.get("trade_date"))
    if trade_date is None:
        raise AgentError("bad_request_trade_date")
    if str(request.get("run_id") or "") != cfg.run_id:
        raise AgentError("run_id_mismatch")

    with open(cfg.code_path, encoding="utf-8") as handle:
        symbol = parse_symbol_constant(handle.read())
    if not symbol:
        raise AgentError("symbol_missing")

    started = time.time()
    result = _compute_with_timeout(cfg, symbol, trade_date)
    payload = {
        "status": "ok",
        "tenant_id": cfg.tenant_id,
        "user_id": cfg.user_id,
        "strategy_id": cfg.strategy_id,
        "run_id": cfg.run_id,
        "request_id": request_id,
        "trade_date": trade_date.isoformat(),
        "computed_at": time.time(),
        "compute_seconds": round(time.time() - started, 3),
        **result,
    }
    client.set(
        cfg.target_key,
        json.dumps(payload, ensure_ascii=False),
        ex=cfg.target_ttl_seconds,
    )
    _log(
        "target ready "
        f"symbol={payload['symbol']} shares={payload['target_shares']} "
        f"bar={payload['bar_date']}({payload['bar_source']}) "
        f"elapsed={payload['compute_seconds']}s"
    )


def _write_error(
    cfg: AgentConfig, client: redis.Redis, request: dict[str, Any], error: str
) -> None:
    payload = {
        "status": "error",
        "error": str(error)[:500],
        "tenant_id": cfg.tenant_id,
        "user_id": cfg.user_id,
        "strategy_id": cfg.strategy_id,
        "run_id": cfg.run_id,
        "request_id": str(request.get("request_id") or ""),
        "trade_date": str(request.get("trade_date") or ""),
        "computed_at": time.time(),
    }
    client.set(
        cfg.target_key,
        json.dumps(payload, ensure_ascii=False),
        ex=cfg.target_ttl_seconds,
    )
    _log(f"target error: {error}")


def _heartbeat_loop(cfg: AgentConfig, stop: threading.Event) -> None:
    client = _trade_redis()
    while not stop.is_set():
        try:
            client.set(cfg.heartbeat_key, str(time.time()), ex=HEARTBEAT_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - 心跳失败不阻断计算
            _log(f"heartbeat failed: {exc}")
        stop.wait(HEARTBEAT_INTERVAL_SECONDS)


def run_once(cfg: AgentConfig, trade_date: date) -> dict[str, Any]:
    """单次计算（调试/对拍用）：不经过请求键，结果直接返回并打印。"""
    with open(cfg.code_path, encoding="utf-8") as handle:
        symbol = parse_symbol_constant(handle.read())
    if not symbol:
        raise AgentError("symbol_missing")
    result = _compute_with_timeout(cfg, symbol, trade_date)
    payload = {
        "status": "ok",
        "trade_date": trade_date.isoformat(),
        "computed_at": time.time(),
        **result,
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return payload


def main() -> int:
    cfg = load_config()
    stop = threading.Event()

    def _stop(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if str(os.getenv("QM_MINIBT_SIM_ONCE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        trade_date = trade_date_from_iso(os.getenv("QM_MINIBT_SIM_TRADE_DATE"))
        if trade_date is None:
            trade_date = date.today()
        run_once(cfg, trade_date)
        return 0

    client = _trade_redis()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(cfg, stop),
        name="minibt-sim-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    _log(
        "agent started "
        f"tenant={cfg.tenant_id} user={cfg.user_id} strategy={cfg.strategy_id} "
        f"run={cfg.run_id} code={cfg.code_path}"
    )

    while not stop.is_set():
        try:
            raw = client.get(cfg.request_key)
            if not raw:
                stop.wait(cfg.poll_seconds)
                continue
            try:
                request = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                raise AgentError(f"bad_request_json:{exc}") from exc
            _log(
                f"request received: {request.get('request_id')} date={request.get('trade_date')}"
            )
            try:
                _handle_request(cfg, client, request)
            except Exception as exc:  # noqa: BLE001 - 失败必须写回目标，调度器才能显式跳过
                _write_error(cfg, client, request, f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    client.delete(cfg.request_key)
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 - Redis 抖动不退出常驻进程
            _log(f"loop error: {exc}")
            stop.wait(cfg.poll_seconds)

    _log("agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
