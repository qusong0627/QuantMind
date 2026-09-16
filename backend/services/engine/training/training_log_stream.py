from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import redis as redis_lib
except Exception:  # pragma: no cover
    redis_lib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _get_env_with_root_fallback(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()

    try:
        root_env = Path(__file__).resolve().parents[4] / ".env"
        if root_env.exists():
            for line in root_env.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                env_key, env_value = text.split("=", 1)
                if env_key.strip() == key:
                    return env_value.strip().strip("'").strip('"')
    except Exception:
        pass
    return default


class TrainingRunLogStream:
    """训练任务容器日志实时流（Redis Stream + 最新状态快照）。"""

    def __init__(self) -> None:
        self.enabled = str(
            os.getenv("TRAINING_LOG_STREAM_ENABLED", "true")
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.redis_host = _get_env_with_root_fallback("REDIS_HOST", "quantmind-redis")
        self.redis_port = int(
            _get_env_with_root_fallback("REDIS_PORT", "6379") or "6379"
        )
        self.redis_password = _get_env_with_root_fallback("REDIS_PASSWORD", "") or None
        self.redis_db = int(_get_env_with_root_fallback("REDIS_DB_ENGINE", "4"))
        self.stream_prefix = (
            str(os.getenv("TRAINING_LOG_STREAM_PREFIX", "qm:training:run")).strip()
            or "qm:training:run"
        )
        self.stream_maxlen = max(
            200, int(os.getenv("TRAINING_LOG_STREAM_MAXLEN", "4000"))
        )
        self.state_ttl_sec = max(
            600, int(os.getenv("TRAINING_LOG_STATE_TTL_SECONDS", "172800"))
        )

        self._client = None
        self._client_init_failed = False

    def _get_client(self):
        if not self.enabled:
            return None
        if redis_lib is None:
            return None
        if self._client is not None:
            return self._client
        if self._client_init_failed:
            return None
        try:
            self._client = redis_lib.Redis(
                host=self.redis_host,
                port=self.redis_port,
                db=self.redis_db,
                password=self.redis_password,
                decode_responses=False,
                socket_timeout=3.0,
                socket_connect_timeout=3.0,
                health_check_interval=30,
            )
            self._client.ping()
            return self._client
        except Exception as exc:
            self._client_init_failed = True
            logger.warning(
                "training log redis unavailable host=%s port=%s db=%s err=%s",
                self.redis_host,
                self.redis_port,
                self.redis_db,
                exc,
            )
            return None

    def _stream_key(self, run_id: str) -> str:
        return f"{self.stream_prefix}:logs:{run_id}"

    def _state_key(self, run_id: str) -> str:
        return f"{self.stream_prefix}:state:{run_id}"

    def _user_active_key(self, tenant_id: str, user_id: str) -> str:
        # 记录「该用户最近活跃的训练 run」，供前端切页后从 redis 恢复进度/状态。
        return f"{self.stream_prefix}:user:active:{tenant_id}:{user_id}"

    def _cancel_key(self, run_id: str) -> str:
        return f"{self.stream_prefix}:cancel:{run_id}"

    @staticmethod
    def _decode(raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    def append_log(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        line: str,
        status: str | None = None,
        progress: int | None = None,
        container_id: str | None = None,
    ) -> None:
        text = str(line or "").rstrip("\n")
        if not text:
            return

        client = self._get_client()
        if client is None:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        fields: dict[str, str] = {
            "run_id": str(run_id),
            "tenant_id": str(tenant_id or "default"),
            "user_id": str(user_id or ""),
            "line": text,
            "ts": now_iso,
        }
        if status:
            fields["status"] = str(status)
        if progress is not None:
            fields["progress"] = str(int(progress))
        if container_id:
            fields["container_id"] = str(container_id)

        try:
            client.xadd(
                self._stream_key(run_id),
                fields,
                maxlen=self.stream_maxlen,
                approximate=True,
            )
        except Exception:
            return

        self.update_state(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status=status,
            progress=progress,
            last_line=text,
            container_id=container_id,
        )

    def update_state(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        status: str | None = None,
        progress: int | None = None,
        last_line: str = "",
        container_id: str | None = None,
    ) -> None:
        client = self._get_client()
        if client is None:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        prev: dict[str, Any] = {}
        try:
            raw_prev = client.get(self._state_key(run_id))
            if raw_prev:
                parsed = json.loads(self._decode(raw_prev))
                if isinstance(parsed, dict):
                    prev = parsed
        except Exception:
            prev = {}
        state: dict[str, Any] = {
            **prev,
            "run_id": str(run_id),
            "tenant_id": str(tenant_id or "default"),
            "user_id": str(user_id or ""),
            "updated_at": now_iso,
        }
        if status:
            state["status"] = str(status)
        if progress is not None:
            state["progress"] = int(progress)
        if container_id:
            state["container_id"] = str(container_id)
        if last_line:
            state["last_line"] = str(last_line)

        try:
            client.setex(
                self._state_key(run_id),
                self.state_ttl_sec,
                json.dumps(state, ensure_ascii=False),
            )
        except Exception:
            return

        # 维护「该用户最近活跃 run」索引（TTL 与状态一致），供前端切页后恢复进度。
        try:
            client.setex(
                self._user_active_key(tenant_id, user_id),
                self.state_ttl_sec,
                json.dumps(
                    {"run_id": str(run_id), "status": state.get("status", ""), "updated_at": now_iso},
                    ensure_ascii=False,
                ),
            )
        except Exception:
            return

    def mark_cancel_requested(self, run_id: str) -> None:
        """置取消标记（训练编排器轮询循环据此 kill 容器/进程）。"""
        client = self._get_client()
        if client is None:
            return
        try:
            client.setex(self._cancel_key(run_id), self.state_ttl_sec, "1")
        except Exception:
            return

    def is_cancel_requested(self, run_id: str) -> bool:
        """查询取消标记是否存在。"""
        client = self._get_client()
        if client is None:
            return False
        try:
            return bool(client.get(self._cancel_key(run_id)))
        except Exception:
            return False

    def clear_cancel(self, run_id: str) -> None:
        """清理取消标记（任务终态后调用）。"""
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(self._cancel_key(run_id))
        except Exception:
            return

    def fetch_active_run_id(
        self, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """返回该用户最近活跃训练 run 的索引快照（redis 缓存，查不到返回 None）。"""
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._user_active_key(tenant_id, user_id))
        except Exception:
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(self._decode(raw))
        except Exception:
            return None
        if not isinstance(parsed, dict) or not parsed.get("run_id"):
            return None
        return parsed

    def fetch_snapshot(
        self, run_id: str, *, line_limit: int = 200
    ) -> dict[str, Any] | None:
        client = self._get_client()
        if client is None:
            return None

        state: dict[str, Any] = {}
        try:
            raw_state = client.get(self._state_key(run_id))
            if raw_state:
                state = json.loads(self._decode(raw_state))
        except Exception:
            state = {}

        lines: list[str] = []
        last_status = str(state.get("status") or "").strip()
        last_progress: int | None = None
        if state.get("progress") is not None:
            try:
                last_progress = int(state["progress"])
            except Exception:
                last_progress = None

        count = max(1, min(int(line_limit), 1000))
        try:
            records = client.xrevrange(self._stream_key(run_id), count=count)
        except Exception:
            records = []

        # 容器日志经多路消费（docker 轮询 + 内部回调）写入流，同一条日志可能被
        # 重复 append 多次。连续去重，避免重复行挤占 line_limit 限额、日志显得"空洞"。
        last_seen: str | None = None
        for _, payload in reversed(records):
            line = self._decode(
                payload.get(b"line") if isinstance(payload, dict) else ""
            )
            if not line:
                continue
            if line == last_seen:
                continue
            last_seen = line
            lines.append(line)
            if not last_status:
                status_val = self._decode(
                    payload.get(b"status") if isinstance(payload, dict) else ""
                )
                if status_val:
                    last_status = status_val
            if last_progress is None:
                progress_val = self._decode(
                    payload.get(b"progress") if isinstance(payload, dict) else ""
                )
                if progress_val:
                    try:
                        last_progress = int(progress_val)
                    except Exception:
                        pass

        return {
            "status": last_status,
            "progress": last_progress,
            "logs": "\n".join(lines).strip(),
            "updated_at": str(state.get("updated_at") or ""),
            "container_id": str(state.get("container_id") or ""),
            "last_line": str(state.get("last_line") or ""),
        }
