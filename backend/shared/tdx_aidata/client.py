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
        shard_id: int = 0,
        shard_total: int = 1,
    ) -> None:
        self.socket_path = socket_path or config.socket_path()
        self._spawn_fn = spawn_fn
        self.default_timeout = float(default_timeout)
        self.ready_timeout = float(ready_timeout)
        self.shard_id = int(shard_id)
        self.shard_total = max(1, int(shard_total))
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
            if self.shard_total > 1:
                env["QM_SUB_SHARD_ID"] = str(self.shard_id)
                env["QM_SUB_SHARDS"] = str(self.shard_total)
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
            raise TdxAiDataError(
                "worker_down", "TdxAiData worker 不可用（未启动/拉起失败）"
            )
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
            {
                "symbol": symbol,
                "interval": interval,
                "count": count,
                "start": start,
                "end": end,
            },
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
            {
                "symbols": symbols,
                "interval": interval,
                "count": count,
                "start": start,
                "end": end,
            },
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

    async def subscription_status(self, *, timeout: float | None = None) -> dict:
        """订阅引擎状态（T-P6-02；未启用返回 {"enabled": False}）。"""
        return await self.call("subscription_status", {}, timeout=timeout or 15.0)

    async def sync_hot_set(self, *, timeout: float | None = None) -> dict:
        """手动触发一次热集差分同步（测试/运维用）。"""
        return await self.call("hot_set_sync", {}, timeout=timeout or 60.0)

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


# ── 分片集群（SDK 单进程订阅上限 100 只，2026-09-17 实测）────────────────


def shard_socket_path(base_socket: str, shard_id: int) -> str:
    """分片 socket 路径：0 号片=默认 socket（请求类方法走它），其余 .s{i}。"""
    i = int(shard_id)
    return base_socket if i <= 0 else f"{base_socket}.s{i}"


def _sum_counters(snaps: list[dict[str, Any]]) -> dict[str, Any]:
    """合并订阅 counters：数值求和、字符串取首个非空（last_error 等）。

    ``hot_set_size`` 例外：各分片视角同值（全量热集），聚合取 **max** 而非求和
    （否则 6 片会把 527 报成 3162——2026-09-17 首跑验收仪器实测踩到）。
    """
    merged: dict[str, Any] = {}
    for snap in snaps:
        for key, value in (snap.get("counters") or {}).items():
            if isinstance(value, bool):
                merged[key] = bool(merged.get(key, False)) or value
            elif isinstance(value, (int, float)):
                if key == "hot_set_size":
                    merged[key] = max(merged.get(key, 0), value)
                else:
                    merged[key] = merged.get(key, 0) + value
            elif value is not None and not merged.get(key):
                merged[key] = value
    return merged


def merge_subscriptions(snaps: list[dict[str, Any]]) -> dict[str, Any]:
    """多分片订阅快照 → 集群聚合视图（含逐片明细）。"""
    snaps = [s for s in snaps if isinstance(s, dict)]
    if not snaps:
        return {"enabled": False, "subscribed": 0, "counters": {}, "shards": []}
    ages = [
        float(s["last_frame_age_s"])
        for s in snaps
        if s.get("last_frame_age_s") is not None
    ]
    archivers = [
        s.get("archiver") for s in snaps if isinstance(s.get("archiver"), dict)
    ]
    latencies = [s.get("latency") for s in snaps if isinstance(s.get("latency"), dict)]
    merged: dict[str, Any] = {
        "enabled": any(bool(s.get("enabled")) for s in snaps),
        "hot_set_key": next(
            (s.get("hot_set_key") for s in snaps if s.get("hot_set_key")), None
        ),
        "subscribed": sum(int(s.get("subscribed") or 0) for s in snaps),
        "last_frame_age_s": min(ages) if ages else None,
        # 保守口径：任一分片静默即置位（伴随 silent_shards 点名，绝不静默）
        "silent": any(bool(s.get("silent")) for s in snaps),
        "silent_shards": [
            (s.get("shard") or {}).get("id") for s in snaps if s.get("silent")
        ],
        "counters": _sum_counters(snaps),
        "shards": snaps,
    }
    if archivers:
        merged["archiver"] = {
            "pending_rows": sum(int(a.get("pending_rows") or 0) for a in archivers),
            "rows": sum(int(a.get("rows") or 0) for a in archivers),
            "flushes": sum(int(a.get("flushes") or 0) for a in archivers),
            "flush_errors": sum(int(a.get("flush_errors") or 0) for a in archivers),
            "base_dir": archivers[0].get("base_dir"),
        }
    if latencies:
        nums: dict[str, Any] = {}
        for lat in latencies:
            for key, value in lat.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if key in ("p50_ms", "p95_ms", "max_ms"):
                        nums[key] = max(nums.get(key, 0), value)  # 集群口径：取最差分片
                    else:
                        nums[key] = nums.get(key, 0) + value
        merged["latency"] = nums
    return merged


class TdxAiDataCluster:
    """多分片 worker 集群：同一热集按 symbol 哈希切 N 片，每片独立进程/socket/SDK 连接。

    背景（2026-09-17 实测）：SDK 单次 subscribe >100 只整批拒绝（错误码 2 仅打印不抛、
    零帧到达）→ 大热集必须分片；各片写同一标准键（键级天然合并），请求类方法走主片。
    """

    def __init__(
        self, *, shard_count: int | None = None, base_socket: str | None = None
    ) -> None:
        base = base_socket or config.socket_path()
        n = max(1, int(shard_count or config.shard_count()))
        self.shard_count = n
        self.base_socket = base
        self.clients: list[TdxAiDataClient] = [
            TdxAiDataClient(
                socket_path=shard_socket_path(base, i),
                shard_id=i,
                shard_total=n,
            )
            for i in range(n)
        ]

    @property
    def primary(self) -> TdxAiDataClient:
        return self.clients[0]

    async def ensure_all(self) -> dict[str, bool]:
        """拉起全部缺失分片（互相独立，失败如实标注）。"""
        out: dict[str, bool] = {}
        for i, client in enumerate(self.clients):
            try:
                out[f"s{i}"] = bool(await client.ensure_worker())
            except Exception:  # noqa: BLE001
                out[f"s{i}"] = False
        return out

    async def status(self, *, try_start: bool = False) -> dict[str, Any]:
        """聚合状态：主片基座 + shards 明细 + 合并 subscription。"""
        shard_status: list[dict[str, Any]] = []
        subscriptions: list[dict[str, Any]] = []
        primary_full: dict[str, Any] = {}
        for i, client in enumerate(self.clients):
            try:
                st = await client.status(try_start=try_start)
            except Exception as exc:  # noqa: BLE001
                st = {"worker": "down", "error": str(exc)}
            if i == 0:
                primary_full = st
            shard_status.append(
                {
                    "shard_id": i,
                    "socket_path": client.socket_path,
                    "worker": st.get("worker"),
                    "pid": st.get("pid"),
                    "sdk_ready": st.get("sdk_ready"),
                    "sdk_error": st.get("sdk_error"),
                }
            )
            if isinstance(st.get("subscription"), dict):
                subscriptions.append(st["subscription"])
        base = shard_status[0] if shard_status else {}
        ups = [s for s in shard_status if s.get("worker") == "up"]
        out: dict[str, Any] = {
            "worker": (
                "up"
                if len(ups) == len(shard_status)
                else ("down" if not ups else "degraded")
            ),
            "shards_up": f"{len(ups)}/{len(shard_status)}",
            "shard_count": self.shard_count,
            "shards": shard_status,
            "socket_path": base.get("socket_path"),
            "pid": base.get("pid"),
            "sdk_ready": all(bool(s.get("sdk_ready")) for s in shard_status)
            if shard_status
            else False,
            "enabled": config.is_enabled(),
        }
        if subscriptions:
            out["subscription"] = merge_subscriptions(subscriptions)
        for key in ("gate", "budget", "sdk_error"):
            if key in primary_full:
                out[key] = primary_full[key]
        return out

    async def subscription_status(
        self, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """聚合订阅状态（逐片查询；分片不响应时如实标 down）。"""
        snaps: list[dict[str, Any]] = []
        for i, client in enumerate(self.clients):
            try:
                snap = await client.subscription_status(timeout=timeout)
                if isinstance(snap, dict):
                    snap.setdefault("shard", {"id": i, "count": self.shard_count})
                    snaps.append(snap)
            except Exception as exc:  # noqa: BLE001
                snaps.append(
                    {
                        "enabled": False,
                        "shard": {"id": i, "count": self.shard_count},
                        "error": str(exc),
                        "subscribed": 0,
                        "counters": {},
                    }
                )
        return merge_subscriptions(snaps)

    async def restart(self) -> dict[str, bool]:
        """重启全部分片（配置变更后让 SDK 重读 ini/目录）。"""
        out: dict[str, bool] = {}
        for i, client in enumerate(self.clients):
            try:
                out[f"s{i}"] = bool(await client.restart())
            except Exception:  # noqa: BLE001
                out[f"s{i}"] = False
        return out

    async def close(self) -> None:
        for client in self.clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


_default_cluster: TdxAiDataCluster | None = None


def default_cluster() -> TdxAiDataCluster:
    """进程内共享分片集群客户端（按 config.shard_count() 构建）。"""
    global _default_cluster
    if _default_cluster is None:
        _default_cluster = TdxAiDataCluster()
    return _default_cluster
