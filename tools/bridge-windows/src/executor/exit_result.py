"""止损退出结果的三态判定（纯函数，可单测）。

被守护的缺陷
------------
旧 ``StopLossDaemon._tick`` 在触发后无条件 ``it["enabled"] = False``：
取价异常、无可用持仓、下单超时、券商拒单，全部被当成"已处理"永久停用。
一次失败就让这条规则失效，下一根 K 线不会再保护。

口径
----
* ``success``：券商明确受理（``order_stock`` Value == "2"），只有这一种停用；
* ``unknown``：超时/待确认，单可能已在柜台——保留规则 + 退避，绝不重发；
* ``rejected``：券商明确拒绝，保留规则 + 退避并告警（人工决定是否重配）；
* ``no_position`` / ``error``：无仓可卖 / 取数或查询异常，保留规则 + 退避。
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_SUCCESS = "success"
KIND_UNKNOWN = "unknown"
KIND_REJECTED = "rejected"
KIND_NO_POSITION = "no_position"
KIND_ERROR = "error"

_MAX_RETRY_S = 300.0
_BASE_RETRY_S = 5.0


@dataclass(frozen=True)
class ExitResult:
    kind: str
    detail: str = ""
    order_id: str = ""

    @property
    def is_success(self) -> bool:
        return self.kind == KIND_SUCCESS


def classify_order_result(value, msg="") -> ExitResult:
    """把 TDX ``order_stock`` 的 ``Value`` 翻译成三态。

    Value 语义（与 ``PlanExecutor._execute_one`` 同源）：
    ``2`` = 已提交；``1`` = 待客户端确认（结果未知）；其它/``0`` = 拒绝。
    """
    text = str(value or "").strip()
    if text == "2":
        return ExitResult(KIND_SUCCESS, str(msg or "order submitted"))
    if text == "1":
        return ExitResult(
            KIND_UNKNOWN,
            "needs client confirmation%s" % ((": " + str(msg)) if msg else ""),
        )
    return ExitResult(
        KIND_REJECTED,
        "order rejected value=%s%s" % (text or "0", (": " + str(msg)) if msg else ""),
    )


def retry_delay_s(attempts, base=_BASE_RETRY_S, cap=_MAX_RETRY_S) -> float:
    """指数退避：5s、10s、20s… 上限 300s；非法 attempts 按 1 次算。"""
    try:
        n = max(1, int(attempts))
    except (TypeError, ValueError):
        n = 1
    try:
        delay = float(base) * (2 ** (n - 1))
    except Exception:  # noqa: BLE001 - 极大 n 也不抛
        delay = float(cap)
    return min(float(cap), max(1.0, delay))


def apply_exit_outcome(item, result: ExitResult, now) -> dict:
    """把一次退出尝试写回规则条目（不原地修改）。

    只有 ``success`` 会 ``enabled=False``；其余全部保留并设置退避。
    """
    updated = dict(item or {})
    attempts = int(updated.get("attempts") or 0) + 1
    updated["attempts"] = attempts
    updated["last_attempt_at"] = float(now)
    updated["last_result"] = result.kind
    updated["last_error"] = result.detail
    if result.is_success:
        updated["enabled"] = False
        updated["retry_after_ts"] = 0.0
        if result.order_id:
            updated["order_id"] = result.order_id
        return updated
    updated["enabled"] = True
    # 未知/拒单给更长退避：避免 5s 一次地重复砸同一笔单；异常/无仓 5s 起。
    base = 30.0 if result.kind in (KIND_UNKNOWN, KIND_REJECTED) else _BASE_RETRY_S
    updated["retry_after_ts"] = float(now) + retry_delay_s(attempts, base=base)
    return updated
