"""TdxAiData worker 子进程——全仓唯一 SDK 引入口（源守卫约束）。

为什么必须是独立子进程：
1. **CWD 约束**：原生 .so 启动与后续取数都要求进程 CWD=安装目录（NewTc.dat 同目录），
   持久服务内全局 chdir 会伤及其他线程的相对路径——隔离到专用进程=零污染；
2. **崩溃隔离**：原生库异常/段错误只死 worker，父服务不受影响；
3. **单实例**：SDK 连接是进程级单例，多服务经同一 unix socket 共享。

**配额闸门（2026-09-16 实测）**：该 token 每个冷却窗口仅放行 3 次请求（所有接口共享），
第 4 次起服务端返回 Token Insufficient。因此 worker 自带预算门：
- 窗口内请求数 >= ``max_requests_per_window``（默认 3）→ 直接快速失败（不触 SDK）；
- 服务端限流（可检出时）→ 提前进入冷却，冷却时长翻倍（180s 起、600s 封顶），成功即复位；
- 窗口过期自动开新窗口。调用方据 ``retry_after_s`` 排队，绝不盲打 token。

协议：unix socket + JSONL（见 protocol.py）。启动：``python -m backend.shared.tdx_aidata.worker``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from typing import Any

from backend.shared.tdx_aidata import config, protocol

logger = logging.getLogger("tdx_aidata.worker")

_SDK_ERROR_RE = re.compile(r"错误码\s*(\d+)|code=(\d+)|Token\s*Insufficient", re.IGNORECASE)


class _BudgetGate:
    """冷却窗口 + 请求预算闸门（防盲打 token）。"""

    def __init__(
        self,
        max_requests_per_window: int = 3,
        initial_cooldown_s: float = 180.0,
        max_cooldown_s: float = 600.0,
    ) -> None:
        self.max_requests = max(1, int(max_requests_per_window))
        self.initial_cooldown_s = float(initial_cooldown_s)
        self.max_cooldown_s = float(max_cooldown_s)

        self._window_started = 0.0
        self._window_requests = 0
        self._cooldown_until = 0.0
        self._cooldown_s = self.initial_cooldown_s
        self._consecutive_limits = 0

    def _now(self) -> float:
        return time.monotonic()

    def check(self) -> float | None:
        """可发请求返回 None；否则返回建议重试等待秒数。"""
        now = self._now()
        if now < self._cooldown_until:
            return round(self._cooldown_until - now, 1)
        if now - self._window_started >= self._cooldown_s:
            # 窗口过期：开新窗口
            self._window_started = now
            self._window_requests = 0
            return None
        if self._window_requests >= self.max_requests:
            # 预算耗尽：视同冷却（估计值=窗口剩余）
            remaining = max(1.0, self._cooldown_s - (now - self._window_started))
            return round(remaining, 1)
        return None

    def consume(self) -> None:
        now = self._now()
        if now - self._window_started >= self._cooldown_s:
            self._window_started = now
            self._window_requests = 0
        self._window_requests += 1

    def note_rate_limited(self) -> float:
        """服务端限流：进入冷却并翻倍。返回 retry_after_s。"""
        self._consecutive_limits += 1
        self._cooldown_until = self._now() + self._cooldown_s
        # 本次冷却 = 当前档；下档翻倍
        current = self._cooldown_s
        self._cooldown_s = min(self.max_cooldown_s, self._cooldown_s * 2)
        return round(current, 1)

    def note_success(self) -> None:
        self._consecutive_limits = 0
        self._cooldown_s = self.initial_cooldown_s
        self._cooldown_until = 0.0

    def snapshot(self) -> dict[str, Any]:
        now = self._now()
        return {
            "cooldown_active": now < self._cooldown_until,
            "cooldown_remaining_s": round(max(0.0, self._cooldown_until - now), 1),
            "window_requests": self._window_requests,
            "max_requests_per_window": self.max_requests,
            "window_age_s": round(max(0.0, now - self._window_started), 1),
            "cooldown_step_s": round(self._cooldown_s, 1),
        }


def _capture_fd_call(fn, *args, **kwargs):
    """fd 级捕获 C 层 stdout/stderr（诊断用），返回 (result, captured_text)。

    原生库错误信息经 C printf 打到 fd1/fd2（Python 层 redirect 捕获不到）。
    fd 重定向期间调用线程持全局锁（worker 串行执行），无并发串扰。
    """
    import tempfile

    captured = ""
    try:
        with tempfile.TemporaryFile() as tmp:
            old1, old2 = os.dup(1), os.dup(2)
            try:
                os.dup2(tmp.fileno(), 1)
                os.dup2(tmp.fileno(), 2)
                try:
                    result = fn(*args, **kwargs)
                finally:
                    os.dup2(old1, 1)
                    os.dup2(old2, 2)
            finally:
                os.close(old1)
                os.close(old2)
            tmp.seek(0)
            captured = tmp.read().decode("gb18030", errors="replace")
    except Exception:  # noqa: BLE001 - 捕获失败不阻断主调用
        result = fn(*args, **kwargs)
    return result, captured


class AidataWorker:
    def __init__(
        self,
        socket_path: str | None = None,
        directory: str | None = None,
        *,
        max_requests_per_window: int = 3,
    ) -> None:
        self.socket_path = socket_path or config.socket_path()
        self.directory = directory or config.resolve_dir()
        self.gate = _BudgetGate(max_requests_per_window=max_requests_per_window)
        self.started_at = time.time()
        self.tqs = None
        self.sdk_error: str | None = None
        self.counters: dict[str, Any] = {
            "requests": 0,
            "ok": 0,
            "errors": {},
            "rate_limited": 0,
            "last_ok_ts": None,
            "last_error": None,
        }
        self._lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()
        self.subscription = None  # collector.SubscriptionEngine | None
        self._sub_task: asyncio.Task | None = None
        self.archiver = None  # l05_store.SnapshotArchiver | None

    # ── 订阅引擎（T-P6-02）─────────────────────────────────────────

    def _redis_factory(self):
        """订阅写侧 Redis（远端行情实例——与消费方同一事实源）。"""
        import redis as _redis

        from backend.shared.remote_quote_config import resolve_remote_quote_redis

        resolved = resolve_remote_quote_redis()
        if resolved is None:
            raise RuntimeError("远端行情 Redis 未配置/已禁用（REMOTE_QUOTE_DISABLED）")
        host, port, password, db = resolved
        return _redis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            socket_connect_timeout=3,
            socket_timeout=5,
            decode_responses=True,
        )

    def _start_subscription(self) -> None:
        from backend.shared.tdx_aidata import config as _cfg
        from backend.shared.tdx_aidata.collector import SubscriptionEngine

        archiver = None
        if str(os.getenv("QM_L05_ENABLED", "true")).strip().lower() not in {
            "0", "false", "no", "off",
        }:
            from backend.shared.l05_store import DEFAULT_BASE_DIR, SnapshotArchiver

            archiver = SnapshotArchiver(
                base_dir=os.getenv("QM_L05_DIR") or DEFAULT_BASE_DIR,
                flush_rows=int(os.getenv("QM_L05_FLUSH_ROWS", "50000")),
                flush_seconds=float(os.getenv("QM_L05_FLUSH_S", "30")),
                keep_days=int(os.getenv("QM_L05_KEEP_DAYS", "90")),
            )
            logger.info("l05 archiver enabled dir=%s", archiver.base_dir)
        self.archiver = archiver

        engine = SubscriptionEngine(
            sdk_subscribe=lambda codes, cb: self.tqs.subscribe(
                stock_list=codes, callback=cb
            ),
            sdk_unsubscribe=lambda: self.tqs._tdx().unsubscribe(),
            budget_gate=self.gate,
            redis_factory=self._redis_factory,
            archiver=archiver,
            hot_set_key=_cfg.hot_set_key(),
            cap=int(os.getenv("QM_HOT_SET_CAP", "1000")),
            silence_s=float(os.getenv("QM_HOT_SET_SILENCE_S", "120")),
            sync_interval_s=float(os.getenv("QM_HOT_SET_SYNC_S", "15")),
        )
        self.subscription = engine
        self._sub_task = asyncio.get_running_loop().create_task(
            engine.run(), name="tdx-aidata-subscription"
        )
        logger.info("subscription engine enabled hot_set=%s", _cfg.hot_set_key())

    # ── SDK 引入（唯一处） ──────────────────────────────────────────

    def _load_sdk(self) -> None:
        if not config.dir_ready(self.directory):
            self.sdk_error = f"安装目录不完整: {self.directory}"
            return
        os.chdir(self.directory)  # .so 依赖同目录 NewTc.dat；本进程专用于此，无副作用顾虑
        if self.directory not in sys.path:
            sys.path.insert(0, self.directory)
        try:
            from tqServer import tqs  # type: ignore  # 唯一合规引入点（源守卫）

            self.tqs = tqs
        except Exception as exc:  # noqa: BLE001
            self.sdk_error = f"SDK 加载失败: {exc}"

    # ── 请求分发 ────────────────────────────────────────────────────

    def _err(
        self,
        code: str,
        message: str,
        *,
        raw_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> dict[str, Any]:
        self.counters["errors"][code] = self.counters["errors"].get(code, 0) + 1
        self.counters["last_error"] = {"code": code, "message": message, "ts": time.time()}
        return {
            "code": code,
            "message": message,
            "raw_code": raw_code,
            "retry_after_s": retry_after_s,
        }

    async def dispatch(self, method: str, params: dict) -> tuple[Any, dict | None, dict]:
        """返回 (result, error, meta)。"""
        t0 = time.monotonic()
        meta: dict[str, Any] = {"duration_ms": 0.0, "attempts": 1}

        if method == "ping":
            return (
                {
                    "pong": True,
                    "pid": os.getpid(),
                    "started_at": self.started_at,
                    "dir": self.directory,
                    "sdk_ready": self.tqs is not None,
                    "sdk_error": self.sdk_error,
                },
                None,
                meta,
            )
        if method == "status":
            payload = {
                "pid": os.getpid(),
                "started_at": self.started_at,
                "dir": self.directory,
                "sdk_ready": self.tqs is not None,
                "sdk_error": self.sdk_error,
                "counters": self.counters,
                "gate": self.gate.snapshot(),
            }
            if self.subscription is not None:
                payload["subscription"] = self.subscription.snapshot()
            return payload, None, meta

        if method == "subscription_status":
            if self.subscription is None:
                return {"enabled": False}, None, meta
            return self.subscription.snapshot(), None, meta

        if method == "hot_set_sync":
            if self.subscription is None:
                return {"enabled": False}, None, meta
            result = await asyncio.to_thread(self.subscription.sync_hot_set_once)
            result["enabled"] = True
            result["subscription"] = self.subscription.snapshot()
            return result, None, meta

        # 数据类：预算闸门 + SDK 就绪检查
        wait_s = self.gate.check()
        if wait_s is not None:
            self.counters["rate_limited"] += 1
            meta["duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
            meta["budget_left"] = 0
            return (
                None,
                self._err(
                    "rate_limited",
                    "配额窗口已用尽（预算闸门）",
                    raw_code=13,
                    retry_after_s=wait_s,
                ),
                meta,
            )
        if self.tqs is None:
            meta["duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
            return (
                None,
                self._err("sdk_unavailable", self.sdk_error or "SDK 未加载"),
                meta,
            )

        try:
            async with self._lock:  # 原生调用串行（SDK 非线程安全 + fd 捕获需互斥）
                self.gate.consume()
                result = await asyncio.to_thread(self._invoke, method, params)
        except ValueError as exc:
            meta["duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
            return None, self._err("invalid_params", str(exc)), meta
        except Exception as exc:  # noqa: BLE001
            code, msg = protocol.map_sdk_error(exc)
            extra: dict[str, Any] = {}
            if code == "rate_limited":
                self.counters["rate_limited"] += 1
                extra["retry_after_s"] = self.gate.note_rate_limited()
            meta["duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
            return None, self._err(code, msg, **extra), meta

        if isinstance(result, dict) and result.get("__sdk_error__"):
            code, msg = protocol.map_sdk_error(result["__sdk_error__"])
            extra = {}
            if code == "rate_limited":
                self.counters["rate_limited"] += 1
                self.gate.note_rate_limited()
                extra["retry_after_s"] = self.gate.snapshot()["cooldown_remaining_s"]
            meta["duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
            return None, self._err(code, msg, **extra), meta

        self.counters["ok"] += 1
        self.counters["last_ok_ts"] = time.time()
        self.gate.note_success()
        meta["duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
        meta["budget_left"] = max(0, self.gate.max_requests - self.gate.snapshot()["window_requests"])
        return result, None, meta

    def _invoke(self, method: str, params: dict) -> Any:
        """同步执行原生调用（限流/参数错误以 __sdk_error__ 形式返回）。"""
        tqs = self.tqs
        if method == "get_quote":
            symbol = str(params.get("symbol") or "").strip()
            if not symbol:
                raise ValueError("symbol 必填")
            snap, out = _capture_fd_call(tqs.get_market_snapshot, stock_code=symbol)
            if not snap:
                return {"__sdk_error__": out or "空快照(未知原因，疑似限流)"}
            return snap

        if method in {"get_klines", "get_klines_batch"}:
            interval = str(params.get("interval") or "daily")
            period = protocol.period_of(interval)
            count = int(params.get("count") or 0)
            end = str(params.get("end") or "") or protocol.now_str()
            start = str(params.get("start") or "")
            if count and not start:
                start = protocol.start_for_count(period, count, end)
            if method == "get_klines":
                symbols = [str(params.get("symbol") or "").strip()]
                if not symbols[0]:
                    raise ValueError("symbol 必填")
            else:
                symbols = [str(s).strip() for s in (params.get("symbols") or []) if str(s).strip()]
                if not symbols:
                    raise ValueError("symbols 必填")
            data, out = _capture_fd_call(
                tqs.get_market_data,
                stock_list=symbols,
                period=period,
                start_time=start,
                end_time=end,
                dividend_type="front",
            )
            if not data:
                return {"__sdk_error__": out or "空K线(未知原因，疑似限流)"}
            if method == "get_klines":
                bars = protocol.bars_from_market_data(data, symbols[0])
                if count and len(bars) > count:
                    bars = bars[-count:]
                if not bars:
                    return {"__sdk_error__": out or "K线全为残缺行"}
                return bars
            out_map: dict[str, list[dict[str, Any]]] = {}
            for sym in symbols:
                bars = protocol.bars_from_market_data(data, sym)
                if count and len(bars) > count:
                    bars = bars[-count:]
                out_map[sym] = bars
            if not any(out_map.values()):
                return {"__sdk_error__": out or "批量K线全空"}
            return out_map

        if method == "get_minute_data":
            symbol = str(params.get("symbol") or "").strip()
            date = str(params.get("date") or "").strip()
            if not symbol or not date:
                raise ValueError("symbol/date 必填")
            data, out = _capture_fd_call(tqs._tdx().get_minute_data, symbol, date)
            if not data and out.strip():
                return {"__sdk_error__": out}
            return data or {}

        if method == "get_tick_data":
            symbol = str(params.get("symbol") or "").strip()
            date = str(params.get("date") or "").strip()
            if not symbol or not date:
                raise ValueError("symbol/date 必填")
            wantnum = int(params.get("wantnum") or 100)
            data, out = _capture_fd_call(
                tqs.get_tick_data,
                stock_code=symbol,
                date=date,
                startxh=int(params.get("startxh") or 0),
                wantnum=wantnum,
            )
            if not data and out.strip():
                return {"__sdk_error__": out}
            return data or {}

        raise ValueError(f"未知方法: {method}")

    # ── socket 服务 ────────────────────────────────────────────────

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    req = protocol.parse_request(line)
                except protocol.ProtocolError as exc:
                    resp = {
                        "id": -1,
                        "ok": False,
                        "result": None,
                        "error": {"code": "protocol", "message": str(exc), "raw_code": None, "retry_after_s": None},
                        "meta": {},
                    }
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    await writer.drain()
                    continue
                self.counters["requests"] += 1
                result, err, meta = await self.dispatch(req["method"], req.get("params") or {})
                resp = {"id": req["id"], "ok": err is None, "result": result, "error": err, "meta": meta}
                writer.write((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def serve(self) -> None:
        self._load_sdk()
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path
        )
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        logger.info(
            "tdx_aidata worker started pid=%s socket=%s dir=%s sdk_ready=%s",
            os.getpid(),
            self.socket_path,
            self.directory,
            self.tqs is not None,
        )
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # pragma: no cover
                pass
        if config.subscription_enabled() and self.tqs is not None:
            self._start_subscription()
        async with self._server:
            await self._stop.wait()
        if self.subscription is not None:
            self.subscription.stop()
        if self._sub_task is not None:
            try:
                await asyncio.wait_for(self._sub_task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sub_task.cancel()
        if self.archiver is not None:
            # 停机终刷：缓冲区数据不许丢
            try:
                flushed = await asyncio.to_thread(self.archiver.flush)
                logger.info("l05 archiver final flush rows=%s", flushed.get("rows"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("l05 停机终刷失败: %s", exc)
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="TdxAiData worker（唯一 SDK 引入口）")
    ap.add_argument("--socket-path", default=None)
    ap.add_argument("--dir", default=None)
    ap.add_argument(
        "--max-requests-per-window",
        type=int,
        default=int(os.getenv("TDX_AIDATA_MAX_REQ_PER_WINDOW", "3")),
        help="每个冷却窗口最大请求数（实测 token 为 3）",
    )
    args = ap.parse_args(argv)
    worker = AidataWorker(
        socket_path=args.socket_path,
        directory=args.dir,
        max_requests_per_window=args.max_requests_per_window,
    )
    asyncio.run(worker.serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
