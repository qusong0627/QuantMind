from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.services.trade_shared.redis_client import get_redis

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except Exception:
        return default


class ManualExecutionLogStream:
    """手动执行任务级日志流，使用 Redis Stream + 状态快照。"""

    def __init__(self) -> None:
        self.enabled = str(os.getenv("MANUAL_EXECUTION_LOG_STREAM_ENABLED", "true")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.stream_prefix = str(os.getenv("MANUAL_EXECUTION_LOG_STREAM_PREFIX", "qm:real-trading:manual-execution")).strip() or "qm:real-trading:manual-execution"
        self.stream_maxlen = max(500, _int_env("MANUAL_EXECUTION_LOG_STREAM_MAXLEN", 4000))
        self.state_ttl_sec = max(600, _int_env("MANUAL_EXECUTION_LOG_STATE_TTL_SECONDS", 172800))
        # T-RC-14：任务日志镜像进运行维度流（默认开）。整体开关便于线上降噪/回退。
        self.mirror_to_runtime = str(
            os.getenv("MANUAL_EXECUTION_LOG_MIRROR_RUNTIME", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._client = None
        self._client_init_failed = False

    def _get_client(self):
        if not self.enabled:
            return None
        if self._client is not None:
            return self._client
        if self._client_init_failed:
            return None
        try:
            redis_wrapper = get_redis()
            client = getattr(redis_wrapper, "client", None)
            if client is None:
                redis_wrapper.connect()
                client = getattr(redis_wrapper, "client", None)
            if client is None:
                self._client_init_failed = True
                return None
            self._client = client
            return self._client
        except Exception as exc:
            self._client_init_failed = True
            logger.warning("manual execution log redis unavailable: %s", exc)
            return None

    def _stream_key(self, task_id: str) -> str:
        return f"{self.stream_prefix}:logs:{task_id}"

    def _state_key(self, task_id: str) -> str:
        return f"{self.stream_prefix}:state:{task_id}"

    def _mirror_to_runtime(self, *, tenant_id: str, user_id: str, line: str, **fields: Any) -> None:
        """把任务日志镜像进运行维度流（T-RC-14）。

        容器/远程 runner（REAL/SHADOW）与手动单的日志全部经本类落盘，调用点约 30 处；
        在**此处**做一次 fan-out 即可让运行日志面板看到它们，无需改任何业务调用点。

        子类 ``RuntimeLogStream`` 必须覆写为 no-op，否则运行流写自己会无限递归。
        """
        if not self.mirror_to_runtime:
            return
        try:
            from backend.services.live_trading.services.runtime_log_stream import (
                infer_source,
                runtime_log_stream,
            )

            runtime_log_stream.log(
                tenant_id=tenant_id,
                user_id=user_id,
                line=line,
                source=infer_source(fields.get("task_id")),
                task_id=fields.get("task_id"),
                **{k: v for k, v in fields.items() if k != "task_id"},
            )
        except Exception as exc:  # noqa: BLE001 - 镜像失败绝不能影响任务日志
            logger.debug("mirror to runtime stream failed: %s", exc)

    @staticmethod
    def _decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def append_log(
        self,
        *,
        task_id: str,
        tenant_id: str,
        user_id: str,
        line: str,
        level: str = "info",
        stage: str | None = None,
        status: str | None = None,
        progress: int | None = None,
        signal_index: int | None = None,
        order_index: int | None = None,
        summary: dict[str, Any] | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> bool:
        """追加一条日志，返回**是否真的写进 Redis**。

        返回值不是装饰：写失败此前一律静默 ``return``，调用方无从区分「写了」与
        「Redis 挂了什么也没写」。运行流的 ``log_skip_once`` 依赖它决定要不要落
        去重标记——标记先落而写失败，会让该原因被永久静默。
        既有约 30 处调用点忽略返回值，语义不变。
        """
        text = str(line or "").rstrip("\n")
        if not text:
            return False

        # 先镜像到运行流（T-RC-14）：即便任务流 Redis 句柄拿不到，运行面板也该看到这条
        self._mirror_to_runtime(
            tenant_id=tenant_id,
            user_id=user_id,
            line=text,
            level=level,
            stage=stage,
            status=status,
            progress=progress,
            signal_index=signal_index,
            order_index=order_index,
            summary=summary,
            task_id=task_id,
        )

        client = self._get_client()
        if client is None:
            return False

        now_iso = datetime.now(timezone.utc).isoformat()
        fields: dict[str, str] = {
            "task_id": str(task_id),
            "tenant_id": str(tenant_id or "default"),
            "user_id": str(user_id or ""),
            "line": text,
            "ts": now_iso,
            "level": str(level or "info"),
        }
        if stage:
            fields["stage"] = str(stage)
        if status:
            fields["status"] = str(status)
        if progress is not None:
            fields["progress"] = str(int(progress))
        if signal_index is not None:
            fields["signal_index"] = str(int(signal_index))
        if order_index is not None:
            fields["order_index"] = str(int(order_index))
        if summary is not None:
            fields["summary"] = json.dumps(summary, ensure_ascii=False)
        if extra_fields:
            # 运行流用（source/strategy_id/run_id/phase）：任务流不传即为空，口径不变
            for extra_key, extra_value in extra_fields.items():
                if extra_value is not None and str(extra_value) != "":
                    fields[str(extra_key)] = str(extra_value)

        try:
            client.xadd(
                self._stream_key(task_id),
                fields,
                maxlen=self.stream_maxlen,
                approximate=True,
            )
        except Exception as exc:  # noqa: BLE001 - 日志失败不抛出，但如实回传 False
            logger.debug("log stream xadd failed task=%s: %s", task_id, exc)
            return False

        self.update_state(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            stage=stage,
            status=status,
            progress=progress,
            last_line=text,
            signal_index=signal_index,
            order_index=order_index,
            summary=summary,
        )
        return True

    def update_state(
        self,
        *,
        task_id: str,
        tenant_id: str,
        user_id: str,
        stage: str | None = None,
        status: str | None = None,
        progress: int | None = None,
        signal_count: int | None = None,
        order_count: int | None = None,
        success_count: int | None = None,
        failed_count: int | None = None,
        error_stage: str | None = None,
        error_message: str | None = None,
        last_line: str = "",
        signal_index: int | None = None,
        order_index: int | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        client = self._get_client()
        if client is None:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        state: dict[str, Any] = {
            "task_id": str(task_id),
            "tenant_id": str(tenant_id or "default"),
            "user_id": str(user_id or ""),
            "updated_at": now_iso,
        }
        if stage:
            state["stage"] = str(stage)
        if status:
            state["status"] = str(status)
        if progress is not None:
            state["progress"] = int(progress)
        if signal_count is not None:
            state["signal_count"] = int(signal_count)
        if order_count is not None:
            state["order_count"] = int(order_count)
        if success_count is not None:
            state["success_count"] = int(success_count)
        if failed_count is not None:
            state["failed_count"] = int(failed_count)
        if error_stage:
            state["error_stage"] = str(error_stage)
        if error_message:
            state["error_message"] = str(error_message)
        if last_line:
            state["last_line"] = str(last_line)
        if signal_index is not None:
            state["signal_index"] = int(signal_index)
        if order_index is not None:
            state["order_index"] = int(order_index)
        if summary is not None:
            state["summary"] = summary

        try:
            client.setex(self._state_key(task_id), self.state_ttl_sec, json.dumps(state, ensure_ascii=False))
        except Exception:
            return

    def fetch_snapshot(self, task_id: str, *, line_limit: int = 200) -> dict[str, Any] | None:
        client = self._get_client()
        if client is None:
            return None

        state: dict[str, Any] = {}
        try:
            raw_state = client.get(self._state_key(task_id))
            if raw_state:
                state = json.loads(self._decode(raw_state))
        except Exception:
            state = {}

        count = max(1, min(int(line_limit), 1000))
        lines: list[str] = []
        try:
            records = client.xrevrange(self._stream_key(task_id), count=count)
        except Exception:
            records = []

        for _, payload in reversed(records):
            if not isinstance(payload, dict):
                continue
            line = self._decode(payload.get("line"))
            if line:
                lines.append(line)

        return {
            "task_id": str(task_id),
            "status": str(state.get("status") or ""),
            "stage": str(state.get("stage") or ""),
            "progress": state.get("progress"),
            "signal_count": state.get("signal_count"),
            "order_count": state.get("order_count"),
            "success_count": state.get("success_count"),
            "failed_count": state.get("failed_count"),
            "error_stage": str(state.get("error_stage") or ""),
            "error_message": str(state.get("error_message") or ""),
            "updated_at": str(state.get("updated_at") or ""),
            "last_line": str(state.get("last_line") or ""),
            "summary": state.get("summary") or {},
            "logs_tail": "\n".join(lines).strip(),
        }

    def fetch_entries(
        self,
        task_id: str,
        *,
        after_id: str = "0-0",
        limit: int = 200,
        latest: bool = False,
    ) -> dict[str, Any]:
        """读条目。

        ``latest=True`` 取**最近** N 条（首屏用），否则取 ``after_id`` 之后的
        前 N 条（游标轮询用）。首屏若用 after_id 语义会拿到流里最老的 N 条——
        运行流 maxlen 4000，用户看到的将是很久以前的日志。
        """
        client = self._get_client()
        if client is None:
            return {"entries": [], "next_id": after_id, "snapshot": None}

        min_id = "-" if not after_id or after_id == "0-0" else f"({after_id}"
        max_count = max(1, min(int(limit), 500))
        try:
            if latest:
                records = list(
                    reversed(client.xrevrange(self._stream_key(task_id), count=max_count))
                )
            else:
                records = client.xrange(self._stream_key(task_id), min=min_id, max="+", count=max_count)
        except Exception:
            records = []

        entries: list[dict[str, Any]] = []
        for entry_id, payload in records:
            if not isinstance(payload, dict):
                continue
            item = {
                "id": str(entry_id),
                "task_id": self._decode(payload.get("task_id")),
                "tenant_id": self._decode(payload.get("tenant_id")),
                "user_id": self._decode(payload.get("user_id")),
                "line": self._decode(payload.get("line")),
                "ts": self._decode(payload.get("ts")),
                "level": self._decode(payload.get("level")) or "info",
                "stage": self._decode(payload.get("stage")),
                "status": self._decode(payload.get("status")),
            }
            # 运行流附加维度（T-RC-14）：任务流里这些键为空，返回空串不影响旧解析
            for extra_key in ("source", "strategy_id", "run_id", "phase"):
                value = self._decode(payload.get(extra_key))
                if value:
                    item[extra_key] = value
            if payload.get("progress") is not None:
                try:
                    item["progress"] = int(float(self._decode(payload.get("progress"))))
                except Exception:
                    pass
            if payload.get("signal_index") is not None:
                try:
                    item["signal_index"] = int(float(self._decode(payload.get("signal_index"))))
                except Exception:
                    pass
            if payload.get("order_index") is not None:
                try:
                    item["order_index"] = int(float(self._decode(payload.get("order_index"))))
                except Exception:
                    pass
            if payload.get("summary"):
                raw_summary = self._decode(payload.get("summary"))
                if raw_summary:
                    try:
                        item["summary"] = json.loads(raw_summary)
                    except Exception:
                        item["summary"] = raw_summary
            entries.append(item)

        next_id = entries[-1]["id"] if entries else after_id
        return {
            "entries": entries,
            "next_id": next_id,
            "snapshot": self.fetch_snapshot(task_id),
        }


manual_execution_log_stream = ManualExecutionLogStream()

