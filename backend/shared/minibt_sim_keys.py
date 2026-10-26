# -*- coding: utf-8 -*-
"""minibt 模拟盘桥：Redis 键与纯逻辑（仅标准库，可在 py3.10 交易进程与 py3.12 runner 共用）。

数据流（单向，两个进程只通过 Redis 通信）：

    hosted 调度器 ──请求键──▶ minibt 侧车（runner 镜像）
         ▲                         │
         └──────目标键◀────────────┘

- 请求键：调度器在托管调仓窗口写，侧车取走后删除；
- 目标键：侧车写入 ``{status, trade_date, symbol, target_shares, ...}``；
- 心跳键：侧车每 30s 续期，状态接口据此判活（不依赖 docker inspect）。

键名统一经本模块构造，禁止各处手写 f-string——跨进程键漂移会表现为
「侧车在算、调度器永远读不到」的全链静默空转。
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from collections.abc import Mapping
from typing import Any

_PREFIX = "qm:sim:minibt"
_SEG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_SEG_LEN = 64
_SYMBOL_CONST_RE = re.compile(
    r"^SYMBOL(?:\s*:\s*[^=\n]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)

# 目标超过这个年龄即视为过期（秒）。默认 180s：TDX 热集一轮约 95s，
# 留一轮余量；决策窗口 90s 内正常路径只有几秒。
DEFAULT_TARGET_MAX_AGE_SECONDS = 180.0
# 时钟偏差容差：侧车与交易进程同宿主，正常情况下不应出现负年龄。
_CLOCK_SKEW_TOLERANCE_SECONDS = 30.0


def _seg(value: Any) -> str:
    text = _SEG_RE.sub("_", str(value or "").strip())
    text = text.strip("._-") or "unknown"
    return text[:_MAX_SEG_LEN]


def safe_segment(value: Any) -> str:
    """Redis 键 / 目录名共用的一段安全字符（不改变调用方语义）。"""
    return _seg(value)


def target_key(tenant_id: str, user_id: str, strategy_id: str) -> str:
    return f"{_PREFIX}:target:{_seg(tenant_id)}:{_seg(user_id)}:{_seg(strategy_id)}"


def request_key(tenant_id: str, user_id: str, strategy_id: str) -> str:
    return f"{_PREFIX}:req:{_seg(tenant_id)}:{_seg(user_id)}:{_seg(strategy_id)}"


def heartbeat_key(tenant_id: str, user_id: str, strategy_id: str) -> str:
    return f"{_PREFIX}:hb:{_seg(tenant_id)}:{_seg(user_id)}:{_seg(strategy_id)}"


def attempts_key(
    tenant_id: str, user_id: str, strategy_id: str, trade_date: str
) -> str:
    return (
        f"{_PREFIX}:attempts:{_seg(tenant_id)}:{_seg(user_id)}:"
        f"{_seg(strategy_id)}:{_seg(trade_date)}"
    )


def container_name(tenant_id: str, user_id: str, strategy_id: str) -> str:
    """docker 容器名（仅字母数字/._-，最长 63）。"""
    return f"qm-minibt-sim-{_seg(tenant_id)}-{_seg(user_id)}-{_seg(strategy_id)}"[:63]


def trade_date_str(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def parse_symbol_constant(code: str | None) -> str | None:
    """从策略代码提取顶层 ``SYMBOL = '600036'`` 常量；没有返回 None。

    minibt A 股模板都以模块级 SYMBOL 声明标的（回测中心改日期也是同一口径：
    只认行首常量）。这里只认大写 ``SYMBOL``，避免把普通变量误当标的。
    """
    match = _SYMBOL_CONST_RE.search(str(code or ""))
    return match.group(1).strip() if match else None


def target_state(
    payload: Mapping[str, Any] | None,
    *,
    trade_date: str,
    now_ts: float | None = None,
    max_age_seconds: float = DEFAULT_TARGET_MAX_AGE_SECONDS,
) -> tuple[str, str]:
    """目标载荷 → (状态, 原因)。状态：``ok`` / ``missing`` / ``error`` /
    ``wrong_date`` / ``stale`` / ``invalid``。

    纯函数，调度器与测试共用；**任何不可用都返回原因串，绝不返回空原因**。
    """
    if not isinstance(payload, Mapping) or not payload:
        return "missing", "missing"
    status = str(payload.get("status") or "ok").strip().lower()
    if status != "ok":
        detail = str(payload.get("error") or "unknown")[:200]
        return "error", f"error:{detail}"
    if str(payload.get("trade_date") or "") != str(trade_date):
        return "wrong_date", "wrong_date"
    try:
        computed_at = float(payload.get("computed_at"))
    except (TypeError, ValueError):
        return "invalid", "bad_computed_at"
    now_value = time.time() if now_ts is None else float(now_ts)
    age = now_value - computed_at
    if age < -_CLOCK_SKEW_TOLERANCE_SECONDS:
        return "invalid", "clock_skew"
    if age > float(max_age_seconds):
        return "stale", f"stale:{age:.0f}s"
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        return "invalid", "missing_symbol"
    try:
        shares = int(float(payload.get("target_shares")))
    except (TypeError, ValueError):
        return "invalid", "bad_target_shares"
    if shares < 0:
        return "invalid", "negative_target_shares"
    return "ok", "ok"


def normalize_target_shares(shares: Any) -> int:
    """目标股数归一：负数（A 股不支持裸卖空）按 0 处理，非数抛 ValueError。"""
    value = int(float(shares))
    return max(0, value)


def build_request_payload(
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    run_id: str,
    trade_date: str,
    phase: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "strategy_id": str(strategy_id),
        "run_id": str(run_id),
        "trade_date": str(trade_date),
        "phase": str(phase),
        "request_id": str(request_id),
        "requested_at": time.time(),
    }


def heartbeat_is_fresh(
    raw: Any, *, now_ts: float | None = None, max_age_seconds: float = 90.0
) -> bool:
    """心跳值（epoch 秒字符串或 JSON）→ 是否新鲜。解析失败一律不新鲜。"""
    if raw is None:
        return False
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return False
    try:
        value = float(text)
    except ValueError:
        try:
            import json

            value = float(json.loads(text).get("ts"))
        except Exception:
            return False
    now_value = time.time() if now_ts is None else float(now_ts)
    age = now_value - value
    return -_CLOCK_SKEW_TOLERANCE_SECONDS <= age <= float(max_age_seconds)


def trade_date_from_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
