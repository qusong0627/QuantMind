"""Helpers for the admin auto-inference monitor page."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_SH_TZ = ZoneInfo("Asia/Shanghai")

ENSURE_DISPATCH_LOG_SQL = """
CREATE TABLE IF NOT EXISTS qm_model_inference_dispatch_logs (
  id BIGSERIAL PRIMARY KEY,
  trigger_source TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  strategy_id TEXT,
  model_id TEXT,
  data_trade_date DATE,
  prediction_trade_date DATE,
  status TEXT NOT NULL,
  reason_code TEXT,
  reason_detail TEXT,
  run_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

REASON_LABELS = {
    "ALREADY_DONE": "当日已完成",
    "LOCK_HELD": "任务锁冲突",
    "EXECUTION_FAILED": "执行失败",
    "EXCEPTION": "执行异常",
    "UP_TO_DATE": "已是最新",
    "DRY_RUN": "试运行跳过",
    "PARTIAL": "部分补全失败",
    "BACKFILL_FAILED": "缺口补全失败",
    "NO_TARGETS": "无调度目标",
    # 任务开始时的 running 标记：正常结束会被删掉，长期留着说明任务中途被杀（如 OOM）
    "STARTED": "已启动（未结束）",
}


def isoformat_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=_SH_TZ)
    return value.astimezone(_SH_TZ).isoformat(sep=" ", timespec="seconds")


def isoformat_date(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def auto_inference_enabled() -> bool:
    return os.getenv("AUTO_INFERENCE_ENABLED", "true").lower() == "true"


def next_weekday_auto_inference(now: datetime | None = None) -> datetime:
    """下一次默认模型补全时间：下一个工作日 06:30（Asia/Shanghai）。"""
    current = (now.astimezone(_SH_TZ) if now else datetime.now(_SH_TZ))
    candidate = current.replace(hour=6, minute=30, second=0, microsecond=0)
    if current >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def reason_label(code: str | None) -> str | None:
    if not code:
        return None
    return REASON_LABELS.get(code, code)
