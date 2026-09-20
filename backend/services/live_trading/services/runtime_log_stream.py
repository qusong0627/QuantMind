"""运行维度日志流（T-RC-14）：回答「策略正在做什么」，与任务维度日志互补。

与 :mod:`manual_execution_log_stream` 的分工：

- **任务维度**（``ManualExecutionLogStream``）：一次手动/托管执行任务跑完即止，
  键含 ``task_id``，前端按任务查（``GET /manual-executions/{task_id}/logs``）。
- **运行维度**（本模块）：只要策略还活着就持续写，键是 ``{tenant}:{user}``——
  纯模拟托管链路**不落** ``trade_manual_execution_tasks``，没有 task_id 可挂，
  此前运行日志面板因此恒为空。本模块补上这条链路。

两个写入来源：

1. **镜像**：容器/远程 runner（REAL/SHADOW）与手动单的日志全部经任务流落盘，
   ``ManualExecutionLogStream._mirror_to_runtime`` 在写入时 fan-out 一份到这里，
   无需改动约 30 处业务调用点（``source`` 由 task_id 前缀推断）。
2. **直写**：进程内模拟托管（``SimulationHostedScheduler`` + ``SimulationEngine``）
   没有任务行，在各阶段直接调 :func:`log_runtime`。

字段结构、maxlen/TTL 口径与任务流一致（继承父类），前端可复用同一套游标轮询逻辑。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from backend.services.live_trading.services.manual_execution_log_stream import (
    ManualExecutionLogStream,
    _int_env,
)

logger = logging.getLogger(__name__)

#: 日志来源：前端据此区分「托管模拟」「容器 runner」「手动单」「引导/风控/系统」
SOURCE_HOSTED_SIM = "hosted_sim"
SOURCE_HOSTED_RUNNER = "hosted_runner"
SOURCE_MANUAL = "manual"
SOURCE_BOOTSTRAP = "bootstrap"
SOURCE_SYSTEM = "system"


def infer_source(task_id: Any) -> str:
    """从任务 ID 前缀推断来源（镜像路径用）。

    任务 ID 契约见 ``manual_execution_service.create_hosted_task``：
    ``hosted_`` → 容器/远程 runner 托管；``hosted_sim_`` 前缀更长的先判，
    否则会误判成 runner。其余（含空）→ 手动单。
    """
    text = str(task_id or "").strip()
    if text.startswith("hosted_sim_"):
        return SOURCE_HOSTED_SIM
    if text.startswith("hosted_"):
        return SOURCE_HOSTED_RUNNER
    return SOURCE_MANUAL


def runtime_scope(tenant_id: str, user_id: str) -> str:
    """运行维度键后缀：一个用户在一个租户下同时只有一个活跃策略。"""
    tenant = str(tenant_id or "").strip() or "default"
    return f"{tenant}:{str(user_id or '').strip()}"


class RuntimeLogStream(ManualExecutionLogStream):
    """按 ``{tenant}:{user}`` 归档的运行日志流。

    继承任务流的全部读写实现，只换键前缀与键构造，**不新增字段词汇**。
    """

    def __init__(self) -> None:
        super().__init__()
        self.stream_prefix = (
            str(os.getenv("RUNTIME_LOG_STREAM_PREFIX", "qm:real-trading:runtime")).strip()
            or "qm:real-trading:runtime"
        )
        # 运行流是长期滚动窗口，条目量大于单次任务流，沿用父类默认值但允许独立调参。
        self.stream_maxlen = max(
            500, _int_env("RUNTIME_LOG_STREAM_MAXLEN", self.stream_maxlen)
        )
        self.state_ttl_sec = max(
            600,
            _int_env(
                "RUNTIME_LOG_STATE_TTL_SECONDS",
                _int_env("RUNTIME_LOG_STREAM_TTL_SECONDS", self.state_ttl_sec),
            ),
        )

    def _stream_key(self, scope: str) -> str:  # type: ignore[override]
        return f"{self.stream_prefix}:logs:{scope}"

    def _state_key(self, scope: str) -> str:  # type: ignore[override]
        return f"{self.stream_prefix}:state:{scope}"

    def _mirror_to_runtime(self, **_: Any) -> None:
        """运行流**不镜像自己**——父类 append_log 会回调本方法，不覆写即无限递归。"""
        return None

    def log(
        self,
        *,
        tenant_id: str,
        user_id: str,
        line: str,
        level: str = "info",
        source: str = SOURCE_SYSTEM,
        stage: str | None = None,
        status: str | None = None,
        progress: int | None = None,
        signal_index: int | None = None,
        order_index: int | None = None,
        summary: dict[str, Any] | None = None,
        task_id: str | None = None,
        strategy_id: str | None = None,
        run_id: str | None = None,
        phase: str | None = None,
    ) -> bool:
        """写一条运行日志，返回是否落盘。**绝不抛异常**——日志失败不能拖垮交易循环。"""
        if not str(user_id or "").strip():
            return False
        try:
            return bool(
                self.append_log(
                    task_id=runtime_scope(tenant_id, user_id),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    line=line,
                    level=level,
                    stage=stage,
                    status=status,
                    progress=progress,
                    signal_index=signal_index,
                    order_index=order_index,
                    summary=summary,
                    extra_fields={
                        "source": source,
                        "task_id": task_id or "",
                        "strategy_id": strategy_id or "",
                        "run_id": run_id or "",
                        "phase": phase or "",
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - 防御性
            logger.debug("runtime log append failed: %s", exc)
            return False

    def _last_skip_key(self, scope: str) -> str:
        return f"{self.stream_prefix}:lastskip:{scope}"

    def log_skip_once(
        self,
        *,
        tenant_id: str,
        user_id: str,
        reason: str,
        line: str,
        **kwargs: Any,
    ) -> bool:
        """只在**跳过原因变化**时写一条，返回是否真的写了。

        托管调度器每 30s 一跳，若每跳都写「非交易日/窗口外」，4000 条的流会被
        无信息量的重复行刷满，真正的事件（开轮/下单/报错）反而被挤掉。这里用
        一个短 TTL 的标记键做去重：原因不变就不写。

        返回「**确实写进去了**」而非「决定要写」——先写日志再落标记：标记先落的
        话，写失败会让该原因被永久静默（下次读到标记就不写了），这比重复一条更糟。
        """
        if not str(user_id or "").strip():
            return False
        scope = runtime_scope(tenant_id, user_id)
        client = self._get_client()
        if client is None:
            return False
        try:
            previous = self._decode(client.get(self._last_skip_key(scope)))
        except Exception:
            previous = ""
        if previous == str(reason):
            return False
        written = self.log(tenant_id=tenant_id, user_id=user_id, line=line, **kwargs)
        if not written:
            return False
        try:
            client.set(
                self._last_skip_key(scope),
                str(reason),
                ex=self.state_ttl_sec,
            )
        except Exception:
            pass
        return True

    def clear_skip_marker(self, *, tenant_id: str, user_id: str) -> None:
        """原因变化后（例如离开跳过态）复位标记，下次跳过能照常记一条。"""
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(self._last_skip_key(runtime_scope(tenant_id, user_id)))
        except Exception:
            pass

    def read_state(self, *, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        """只读状态快照（不读日志条目）。

        ``/status`` 每 10s 轮询一次，走 ``fetch_snapshot`` 会连带 xrevrange 拉 200 条
        日志——那是纯浪费。这里只取 state 键。
        """
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._state_key(runtime_scope(tenant_id, user_id)))
        except Exception:
            return None
        if not raw:
            return None
        try:
            state = json.loads(self._decode(raw))
        except Exception:
            return None
        return state if isinstance(state, dict) else None

    def fetch_scope_entries(
        self,
        *,
        tenant_id: str,
        user_id: str,
        after_id: str = "0-0",
        limit: int = 200,
        level: str | None = None,
        stage: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """游标增量读运行流，附带 level/stage/source 过滤。

        过滤在**读取后**做（Redis Stream 不支持字段过滤）。被滤掉的条目仍会推进
        ``next_id``（取原始 ``next_id``），否则游标会卡在最后一条不匹配记录上。
        """
        raw = self.fetch_entries(
            runtime_scope(tenant_id, user_id),
            after_id=after_id,
            limit=limit,
            # 首屏取最近 N 条；游标轮询取游标之后的前 N 条
            latest=not after_id or after_id == "0-0",
        )
        entries = raw.get("entries") or []

        want_level = str(level or "").strip().lower()
        want_stage = str(stage or "").strip()
        want_source = str(source or "").strip()
        if want_level or want_stage or want_source:
            entries = [
                item
                for item in entries
                if (not want_level or str(item.get("level") or "").lower() == want_level)
                and (not want_stage or str(item.get("stage") or "") == want_stage)
                and (not want_source or str(item.get("source") or "") == want_source)
            ]

        return {
            "entries": entries,
            "next_id": raw.get("next_id") or after_id,
            "snapshot": raw.get("snapshot"),
        }


runtime_log_stream = RuntimeLogStream()


def log_runtime(
    *,
    tenant_id: str,
    user_id: str,
    line: str,
    **kwargs: Any,
) -> None:
    """模块级便捷入口：调用方不需要关心流对象与异常。"""
    runtime_log_stream.log(tenant_id=tenant_id, user_id=user_id, line=line, **kwargs)
