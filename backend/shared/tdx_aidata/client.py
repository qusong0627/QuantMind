"""TdxAiData 父进程客户端：按需拉起 worker、JSONL 往返、错误映射。

并发模型：连接按需建立并复用；调用可并发（worker 侧串行+闸门）。
worker 死亡：本次调用报错（不假成功）→ 下次调用自动重拉（带 flock 防多服务竞拉）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from backend.shared.tdx_aidata import config, protocol

logger = logging.getLogger(__name__)

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])


class TdxAiDataError(RuntimeError):
    """统一错误：code ∈ rate_limited/worker_down/timeout/invalid_params/sdk_unavailable/call_failed/protocol。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        raw_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.raw_code = raw_code
        self.retry_after_s = retry_after_s


class TdxAiDataClient:
    def __init__(
        self,
        socket_path: str | None = None,
        *,
        spawn_fn=None,
        default_timeout: float = 60.0,
        ready_timeout: float = 15.0,
    ) -> None:
        self.socket_path = socket_path or config.socket_path()
        self._spawn_fn = spawn_fn
        self.default_timeout = float(default_timeout)
        self.ready_timeout = float(ready_timeout)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._conn_lock = asyncio.Lock()
        self._req_id = 0
        self._down_since: float | None = None

    # ── 连接管理 ────────────────────────────────────────────────────

    async def _close_conn(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _try_connect(self, timeout: float = 5.0) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path), timeout=timeout
            )
        except Exception:  # noqa: BLE001 - 未拉起/不可达
            return False
        self._reader, self._writer = reader, writer
        return True

    async def _spawn_worker(self) -> bool:
        """按需拉起 worker（flock 防竞拉；拉起后轮询 ping 就绪）。"""
        if self._spawn_fn is not None:
            try:
                ok = await self._spawn_fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tdx_aidata spawn_fn 失败: %s", exc)
                return False
            return bool(ok)
        return await asyncio.to_thread(self._spawn_worker_proc)

    def _spawn_worker_proc(self) -> bool:
        import fcntl

        lock_path = f"{self.socket_path}.spawn.lock"
        try:
            lock_file = open(lock_path, "a+")
        except OSError:
            lock_file = None
        try:
            if lock_file is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            # 双检：锁内再连一次（其他服务可能刚拉起）
            if os.path.exists(self.socket_path):
                return True
            log_dir = Path(_PROJECT_ROOT) / "logs"
            log_dir.mkdir(exist_ok=True)
            log_file = open(log_dir / "tdx_aidata_worker.log", "ab")
            env = dict(os.environ)
            env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backend.shared.tdx_aidata.worker",
                    "--socket-path",
                    self.socket_path,
                ],
                cwd=_PROJECT_ROOT,
                env=env,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("tdx_aidata worker 拉起失败: %s", exc)
            return False
        finally:
            if lock_file is not None:
                try:
                    lock_file.close()
                except OSError:
                    pass

    async def ensure_worker(self) -> bool:
        """确保 worker 可用（连接或拉起+就绪等待）。返回是否可用。"""
        async with self._conn_lock:
            if self._writer is not None and not self._writer.is_closing():
                return True
            if await self._try_connect():
                return True
            if not config.is_enabled():
                return False
            if not await self._spawn_worker():
                return False
            deadline = time.monotonic() + self.ready_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(0.3)
                if await self._try_connect(timeout=1.0):
                    self._down_since = None
                    return True
            self._down_since = self._down_since or time.monotonic()
            return False

    # ── 调用 ────────────────────────────────────────────────────────

    async def call(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> Any:
        timeout = float(timeout or self.default_timeout)
        if not await self.ensure_worker():
            raise TdxAiDataError("worker_down", "TdxAiData worker 不可用（未启动/拉起失败）")
        self._req_id += 1
        assert self._writer is not None and self._reader is not None
        try:
            self._writer.write(protocol.encode_request(self._req_id, method, params))
            await self._writer.drain()
            line = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._close_conn()
            raise TdxAiDataError("timeout", f"{method} 超时({timeout}s)") from None
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            await self._close_conn()
            self._down_since = self._down_since or time.monotonic()
            raise TdxAiDataError("worker_down", f"worker 连接异常: {exc}") from exc
        if not line:
            await self._close_conn()
            self._down_since = self._down_since or time.monotonic()
            raise TdxAiDataError("worker_down", "worker 提前关闭连接")
        resp = protocol.parse_response(line)
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise TdxAiDataError(
                str(err.get("code") or "call_failed"),
                str(err.get("message") or ""),
                raw_code=err.get("raw_code"),
                retry_after_s=err.get("retry_after_s"),
            )
        return resp.get("result")

    # ── 便捷方法 ────────────────────────────────────────────────────

    async def get_quote(self, symbol: str, *, timeout: float | None = None) -> dict:
        return await self.call("get_quote", {"symbol": symbol}, timeout=timeout)

    async def get_klines(
        self,
        symbol: str,
        *,
        interval: str = "daily",
        count: int = 0,
        start: str = "",
        end: str = "",
        timeout: float | None = None,
    ) -> list[dict]:
        return await self.call(
            "get_klines",
            {"symbol": symbol, "interval": interval, "count": count, "start": start, "end": end},
            timeout=timeout,
        )

    async def get_klines_batch(
        self,
        symbols: list[str],
        *,
        interval: str = "daily",
        count: int = 0,
        start: str = "",
        end: str = "",
        timeout: float | None = None,
    ) -> dict[str, list[dict]]:
        return await self.call(
            "get_klines_batch",
            {"symbols": symbols, "interval": interval, "count": count, "start": start, "end": end},
            timeout=timeout,
        )

    async def get_minute_data(
        self, symbol: str, date: str, *, timeout: float | None = None
    ) -> dict:
        return await self.call(
            "get_minute_data", {"symbol": symbol, "date": date}, timeout=timeout
        )

    async def get_tick_data(
        self,
        symbol: str,
        date: str,
        *,
        startxh: int = 0,
        wantnum: int = 100,
        timeout: float | None = None,
    ) -> dict:
        return await self.call(
            "get_tick_data",
            {"symbol": symbol, "date": date, "startxh": startxh, "wantnum": wantnum},
            timeout=timeout,
        )

    # ── 状态 ────────────────────────────────────────────────────────

    async def status(self, *, try_start: bool = False) -> dict[str, Any]:
        """worker 视角状态；不可达时如实标 down（保持可读，不抛错）。

        try_start=True 时允许按需拉起（自检端点用）；缺省无副作用。
        """
        info: dict[str, Any] = {
            "socket_path": self.socket_path,
            "enabled": config.is_enabled(),
        }
        if try_start and self._writer is None:
            await self.ensure_worker()
        if self._writer is None or self._writer.is_closing():
            if not await self._try_connect(timeout=1.0):
                info["worker"] = "down"
                return info
        try:
            payload = await self.call("status", timeout=5.0)
            info["worker"] = "up"
            info.update(payload or {})
        except TdxAiDataError:
            info["worker"] = "down"
        return info

    async def close(self) -> None:
        await self._close_conn()

    async def restart(self) -> bool:
        """重启 worker（配置变更后让 SDK 重读 ini/目录）。返回是否已发出重启。"""
        pid = None
        try:
            st = await self.status()
            pid = st.get("pid")
        except Exception:  # noqa: BLE001
            pass
        await self._close_conn()
        if isinstance(pid, int) and pid > 1:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            await asyncio.sleep(0.5)
            return True
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        return False


_default_client: TdxAiDataClient | None = None


def default_client() -> TdxAiDataClient:
    """进程内共享客户端（服务端使用；测试请自建实例）。"""
    global _default_client
    if _default_client is None:
        _default_client = TdxAiDataClient()
    return _default_client
