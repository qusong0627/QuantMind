from __future__ import annotations

import asyncio
import logging
import math
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.engine.services.model_inference_batch_persistence import (
    model_inference_batch_persistence,
)
from backend.shared.qlib_paths import resolve_qlib_calendar_path

logger = logging.getLogger(__name__)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

MAX_WINDOW_DAYS = 60

# range 模式单批交易日数上限。批量单日推理是逐个交易日串行跑推理（约 10 秒/天），
# 60 天约 10 分钟、120 天约 20 分钟。上限设为 300 天，超过后建议分批，但不再硬拒。
# 长区间由前端做智能提示（交易日数 + 预计耗时 + 确认）。
MAX_RANGE_TRADING_DAYS = 300

# 批量推理并发执行上限。每个交易日是独立子进程，可安全并行。
# 并发 5 时 60 天约 2 分钟（vs 串行 10 分钟）。过高会占满 CPU 影响同容器其他服务。
MAX_CONCURRENCY = 5

# script_runner 按 prediction_day - INFERENCE_PREDICTION_RETENTION_DAYS 清理
# engine_signal_scores 历史行（默认 365 天，与 script_runner 同一 env，不再各写一套）。
# 窗口跨度超过它时，最早几天的信号会在后续单日推理运行时被删掉，聚合结果会静默缺日
# —— 必须提前警告。
PREDICTION_RETENTION_DAYS = int(os.getenv("INFERENCE_PREDICTION_RETENTION_DAYS", "365"))


class BatchInferenceOrchestrator:
    """批量多日推理编排：解析交易日窗口 → 逐日执行（幂等复用）→ 落进度。

    用 asyncio.create_task 而非 FastAPI BackgroundTasks：后者的 anyio cancel
    scope 会在同步阻塞调用（推理走 asyncio.to_thread 跑子进程）期间静默取消任务，
    admin_training_utils.py 已因此踩过坑。
    """

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task] = {}

    # ---------------------------------------------------------------- 交易日

    @staticmethod
    def load_trading_calendar(market: str = "CN") -> list[str]:
        """读 Qlib day.txt 交易日历。

        不用 calendar_service：它的 ensure_tables() 全仓库无调用点，
        qm_market_calendar_day 表可能不存在，届时会 fallback 成「周一到周五均为
        交易日」，把节假日算成交易日。day.txt 是 Qlib 数据构建的产物，可信。
        """
        path = resolve_qlib_calendar_path(market)
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except Exception as exc:
            raise RuntimeError(f"读取交易日历失败: {path}: {exc}") from exc
        days = sorted({line.strip()[:10] for line in raw.splitlines() if line.strip()})
        if not days:
            raise RuntimeError(f"交易日历为空: {path}")
        return days

    @classmethod
    def resolve_lookback_dates(
        cls,
        anchor_date: date,
        window_days: int,
        market: str = "CN",
    ) -> tuple[list[str], str, bool]:
        """返回 (窗口交易日升序, 实际锚定交易日, 锚定日是否被回退)。

        锚定日非交易日时回退到 <= anchor 的最近交易日。
        """
        n = max(1, min(int(window_days), MAX_WINDOW_DAYS))
        days = cls.load_trading_calendar(market)
        anchor_str = anchor_date.isoformat()
        eligible = [i for i, d in enumerate(days) if d <= anchor_str]
        if not eligible:
            raise ValueError(
                f"锚定日 {anchor_str} 早于交易日历起点 {days[0]}，无法回溯"
            )
        idx = eligible[-1]
        resolved_anchor = days[idx]
        window = days[max(0, idx - n + 1) : idx + 1]
        return window, resolved_anchor, resolved_anchor != anchor_str

    @classmethod
    def resolve_range_dates(
        cls,
        start_date: date,
        end_date: date,
        market: str = "CN",
    ) -> tuple[list[str], str, str]:
        """返回 (区间内交易日升序, 实际起始交易日, 实际结束交易日)。

        区间端点非交易日时，起始日回退到 >= start 的最近交易日，
        结束日回退到 <= end 的最近交易日；起始日不得晚于结束日。
        """
        if start_date > end_date:
            raise ValueError(
                f"起始日期 {start_date.isoformat()} 晚于结束日期 {end_date.isoformat()}"
            )
        days = cls.load_trading_calendar(market)
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        first_ge = next((d for d in days if d >= start_str), None)
        last_le = next((d for d in reversed(days) if d <= end_str), None)
        if first_ge is None or last_le is None or first_ge > end_str:
            raise ValueError(
                f"区间 [{start_str}, {end_str}] 内无交易日（日历范围 "
                f"{days[0]} ~ {days[-1]}）"
            )
        resolved = [d for d in days if first_ge <= d <= last_le]
        if not resolved:
            raise ValueError(f"区间 [{start_str}, {end_str}] 内无交易日")
        if len(resolved) > MAX_RANGE_TRADING_DAYS:
            raise ValueError(
                f"区间内交易日 {len(resolved)} 天超过上限 "
                f"{MAX_RANGE_TRADING_DAYS} 天，请缩小日期范围"
            )
        return resolved, first_ge, last_le

    @staticmethod
    def build_window_meta(
        *,
        trade_dates: list[str],
        horizon_days: int,
        requested_window: int,
    ) -> dict[str, Any]:
        """窗口口径元信息：跨度、有效独立信号数、告警。

        effective_independent_bets 是诚实性的关键：N=5/H=10 时窗口重叠 90%，
        5 天不是 5 个独立样本，不暴露这个数字会让用户误以为多日平均天然更可信。
        """
        n = len(trade_dates)
        h = max(1, int(horizon_days))
        warnings: list[str] = []

        if n < requested_window:
            warnings.append(
                f"交易日历只回溯到 {n} 天（请求 {requested_window} 天）"
            )
        if n > h:
            warnings.append(
                f"回溯窗口 N={n} 大于持有期 H={h}：最早的信号梯队在锚定日前已退出，"
                "聚合变成跨轮次比较，共识含义被削弱"
            )
        elif n < h:
            warnings.append(
                f"回溯窗口 N={n} 小于持有期 H={h}：窗口只覆盖持有期的一部分"
            )

        if trade_dates:
            first = date.fromisoformat(trade_dates[0])
            last = date.fromisoformat(trade_dates[-1])
            span_calendar_days = (last - first).days
            if span_calendar_days > PREDICTION_RETENTION_DAYS - 2:
                warnings.append(
                    f"窗口跨 {span_calendar_days} 个自然日，接近或超过信号保留期 "
                    f"{PREDICTION_RETENTION_DAYS} 天：最早几天的信号可能已被"
                    "自动清理，聚合会缺日（可调 INFERENCE_PREDICTION_RETENTION_DAYS）"
                )
        else:
            span_calendar_days = 0

        return {
            "window_days": n,
            "requested_window_days": requested_window,
            "horizon_days": h,
            "trade_dates": list(trade_dates),
            "span_calendar_days": span_calendar_days,
            "span_trading_days": n - 1 + h + 1 if n else 0,
            "effective_independent_bets": max(1, math.ceil(n / h)),
            "overlap_ratio": round(max(0.0, 1.0 - 1.0 / max(1, min(n, h))), 4),
            "warnings": warnings,
        }

    # ---------------------------------------------------------------- 执行

    @staticmethod
    def _batch_key(tenant_id: str, user_id: str, model_id: str) -> str:
        return f"{tenant_id}::{user_id}::{model_id}"

    def is_running(self, tenant_id: str, user_id: str, model_id: str) -> bool:
        key = self._batch_key(tenant_id, user_id, model_id)
        task = self._running.get(key)
        return bool(task and not task.done())

    async def cancel(self, tenant_id: str, user_id: str, model_id: str) -> bool:
        """取消正在运行的批量任务（停止推理）。返回是否找到并取消。"""
        key = self._batch_key(tenant_id, user_id, model_id)
        task = self._running.get(key)
        if not task or task.done():
            return False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._running.pop(key, None)
        return True

    async def submit(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        anchor_date: date | None,
        start_date: date | None,
        end_date: date | None,
        window_days: int | None,
        horizon_days: int,
        market: str,
        params: dict[str, Any],
        execute_day: Callable[..., Awaitable[dict[str, Any]]],
        reuse_existing: bool = True,
        concurrency: int = 1,
    ) -> dict[str, Any]:
        """创建批次并后台执行。立即返回 batch_id 与窗口口径信息。

        mode: lookback 用 anchor_date+window_days 回溯；
              range 用 start_date+end_date 显式枚举区间内交易日。
        concurrency: 并发执行的交易日数。每个交易日是独立子进程（并发安全），
                     并发 n 约 n 倍提速。默认 1（串行），上限见 MAX_CONCURRENCY。
        """
        key = self._batch_key(tenant_id, user_id, model_id)
        existing = self._running.get(key)
        if existing and not existing.done():
            raise RuntimeError("该模型已有批量推理任务在运行中，请等待完成或稍后重试")

        if start_date is not None and end_date is not None:
            trade_dates, start_resolved, end_resolved = self.resolve_range_dates(
                start_date, end_date, market
            )
            window_meta = {
                "mode": "range",
                "requested_start_date": start_date.isoformat(),
                "requested_end_date": end_date.isoformat(),
                "start_date": start_resolved,
                "end_date": end_resolved,
                "window_days": len(trade_dates),
                "horizon_days": int(horizon_days),
                "trade_dates": list(trade_dates),
                "warnings": [],
            }
            # 长区间提示：单日推理约 10 秒/天
            if len(trade_dates) > MAX_WINDOW_DAYS:
                est_minutes = round(len(trade_dates) * 10 / 60, 1)
                window_meta["warnings"].append(
                    f"区间内 {len(trade_dates)} 个交易日较多，串行预计约 {est_minutes} 分钟"
                    f"（并发 {concurrency} 约 {round(est_minutes / concurrency, 1)} 分钟）。"
                    "可在历史批次中查看实时进度；如需中途停止可删除该批次，"
                    "之后重新提交同区间可复用已完成交易日续跑。"
                )
            resolved_anchor = end_resolved
        else:
            window_days = int(window_days or horizon_days)
            trade_dates, resolved_anchor, anchor_adjusted = self.resolve_lookback_dates(
                anchor_date, window_days, market
            )
            window_meta = self.build_window_meta(
                trade_dates=trade_dates,
                horizon_days=horizon_days,
                requested_window=window_days,
            )
            window_meta["mode"] = "lookback"
            window_meta["anchor_date"] = resolved_anchor
            window_meta["anchor_adjusted"] = anchor_adjusted
            window_meta["requested_anchor_date"] = (
                anchor_date.isoformat() if anchor_date else ""
            )

        if not trade_dates:
            raise ValueError("交易日列表为空，无法提交批量推理")

        batch_id = (
            f"batch_{resolved_anchor.replace('-', '')}_{uuid.uuid4().hex[:8]}"
        )
        stored_params = dict(params)
        stored_params["reuse_existing"] = bool(reuse_existing)
        stored_params["concurrency"] = int(concurrency)
        stored_params["window_meta"] = window_meta

        await model_inference_batch_persistence.create_batch(
            batch_id=batch_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_id=model_id,
            anchor_date=date.fromisoformat(resolved_anchor),
            window_days=len(trade_dates),
            horizon_days=int(horizon_days),
            trade_dates=trade_dates,
            params=stored_params,
            created_at=datetime.now(_SHANGHAI_TZ),
        )

        task = asyncio.create_task(
            self._run_batch(
                batch_id=batch_id,
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                trade_dates=trade_dates,
                execute_day=execute_day,
                reuse_existing=reuse_existing,
                concurrency=int(concurrency),
            ),
            name=f"inference-batch-{batch_id}",
        )
        self._running[key] = task
        task.add_done_callback(lambda _t: self._running.pop(key, None))

        return {
            "batch_id": batch_id,
            "status": "pending",
            "trade_dates": trade_dates,
            "window_meta": window_meta,
        }

    async def _run_batch(
        self,
        *,
        batch_id: str,
        tenant_id: str,
        user_id: str,
        model_id: str,
        trade_dates: list[str],
        execute_day: Callable[..., Awaitable[dict[str, Any]]],
        reuse_existing: bool,
        concurrency: int = 1,
    ) -> None:
        members: list[dict[str, Any]] = [
            {
                "trade_date": d,
                "run_id": None,
                "status": "pending",
                "reused": False,
                "signals_count": 0,
            }
            for d in trade_dates
        ]
        done = 0
        concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY))
        try:
            # 并发执行：维护一个固定大小的 worker 池，从队列取交易日执行。
            # 每个交易日是独立子进程（run_id 各异，写不同 trade_date 行），并发安全。
            import asyncio as _asyncio

            queue: _asyncio.Queue[str] = _asyncio.Queue()
            for d in trade_dates:
                await queue.put(d)

            results: dict[str, dict[str, Any]] = {}

            async def _worker() -> None:
                while True:
                    try:
                        trade_date = queue.get_nowait()
                    except _asyncio.QueueEmpty:
                        return
                    try:
                        member = await self._run_one_day(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            model_id=model_id,
                            trade_date=trade_date,
                            batch_id=batch_id,
                            execute_day=execute_day,
                            reuse_existing=reuse_existing,
                        )
                    except Exception as exc:
                        # 单日失败不中断整批：其余日期仍有价值，终态标 partial
                        logger.exception(
                            "batch %s day %s failed", batch_id, trade_date
                        )
                        member = {
                            "trade_date": trade_date,
                            "run_id": None,
                            "status": "failed",
                            "reused": False,
                            "signals_count": 0,
                            "error_message": str(exc),
                        }
                    results[trade_date] = member
                    queue.task_done()

            n_workers = min(concurrency, len(trade_dates))
            workers = [_asyncio.create_task(_worker()) for _ in range(n_workers)]

            # 进度推进：等待任意 worker 完成即刷新一次进度，保持 UI 实时更新
            while not queue.empty() or any(not w.done() for w in workers):
                await _asyncio.sleep(1.0)
                done = len(results)
                running_dates = sorted(
                    d for d in trade_dates
                    if d not in results
                )
                current = running_dates[0] if running_dates else None
                # 更新 member_runs：合并已完成结果 + 进行中标记
                merged = list(members)
                for i, m in enumerate(merged):
                    d = m["trade_date"]
                    if d in results:
                        merged[i] = results[d]
                    elif d == current and not any(
                        r.get("trade_date") == d for r in results.values()
                    ):
                        merged[i] = {**m, "status": "running"}
                await model_inference_batch_persistence.update_progress(
                    batch_id=batch_id,
                    member_runs=merged,
                    progress_done=done,
                    current_trade_date=current,
                )

            await _asyncio.gather(*workers)
            done = len(results)

            # 按原顺序回填成员结果
            for i, m in enumerate(members):
                d = m["trade_date"]
                if d in results:
                    members[i] = results[d]

            ok = sum(1 for m in members if m.get("status") == "completed")
            if ok == len(members):
                status = "completed"
                error_message = None
            elif ok > 0:
                status = "partial"
                failed = [
                    m["trade_date"]
                    for m in members
                    if m.get("status") != "completed"
                ]
                error_message = f"{len(failed)}/{len(members)} 个交易日未成功: " + ", ".join(
                    failed[:8]
                )
            else:
                status = "failed"
                error_message = "所有交易日推理均失败"

            await model_inference_batch_persistence.finalize_batch(
                batch_id=batch_id,
                status=status,
                member_runs=members,
                progress_done=done,
                error_message=error_message,
            )
        except asyncio.CancelledError:
            await model_inference_batch_persistence.finalize_batch(
                batch_id=batch_id,
                status="failed",
                member_runs=members,
                progress_done=done,
                error_message="任务被取消",
            )
            raise
        except Exception as exc:
            logger.exception("batch %s aborted", batch_id)
            await model_inference_batch_persistence.finalize_batch(
                batch_id=batch_id,
                status="failed",
                member_runs=members,
                progress_done=done,
                error_message=str(exc),
            )

    @staticmethod
    async def _run_one_day(
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        trade_date: str,
        batch_id: str,
        execute_day: Callable[..., Awaitable[dict[str, Any]]],
        reuse_existing: bool,
    ) -> dict[str, Any]:
        day = date.fromisoformat(trade_date)

        if reuse_existing:
            reusable = await model_inference_batch_persistence.find_reusable_run(
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                trade_date=day,
            )
            if reusable:
                return {
                    "trade_date": trade_date,
                    "run_id": str(reusable.get("run_id") or ""),
                    "status": "completed",
                    "reused": True,
                    "signals_count": int(reusable.get("actual_rows") or 0),
                    "prediction_trade_date": reusable.get("prediction_trade_date"),
                }

        result = await execute_day(requested_date=day, batch_id=batch_id)
        status = "completed" if result.get("success") else "failed"
        member: dict[str, Any] = {
            "trade_date": trade_date,
            "run_id": str(result.get("run_id") or ""),
            "status": status,
            "reused": False,
            "signals_count": int(result.get("signals_count") or 0),
            "prediction_trade_date": result.get("prediction_trade_date"),
        }
        # 推理内部可能把请求日期回退到有数据的日期，聚合层必须知道真实数据日
        actual = str(result.get("data_trade_date") or "")
        if actual and actual != trade_date:
            member["requested_trade_date"] = trade_date
            member["trade_date"] = actual
            member["date_fallback"] = True
        if status == "failed":
            member["error_message"] = str(
                result.get("error_message") or result.get("failure_stage") or "推理失败"
            )
        return member


batch_inference_orchestrator = BatchInferenceOrchestrator()
