"""QuantDB 同步预算闸（C2）。

下载前用 manifest 的 size 汇总预估本次流量，和账户剩余额度/日预算比较：
- ``QUANTDB_DAILY_BUDGET_GB`` > 0 时日预算超限 → 拒绝；
- 账户 ``remaining_gb`` 不足 → 拒绝；
- 显式 ``force`` 可放行（管理台二次确认，理由由操作留痕）。

manifest 声明的 size 可能与实际文件有偏差（同步器里有同样注释），所以预估
乘一个安全系数（默认 1.10）——宁可多算，不把配额打穿。
"""

from __future__ import annotations

GB = 1024**3
DEFAULT_SAFETY_RATIO = 1.10


def evaluate_budget(
    *,
    new_bytes: int,
    remaining_gb: float | None = None,
    budget_gb: float = 0.0,
    force: bool = False,
    safety_ratio: float = DEFAULT_SAFETY_RATIO,
) -> dict:
    raw_gb = max(0, int(new_bytes or 0)) / GB
    ratio = max(1.0, float(safety_ratio or 1.0))
    estimated_gb = round(raw_gb * ratio, 3)

    reasons: list[str] = []
    if budget_gb and float(budget_gb) > 0 and estimated_gb > float(budget_gb):
        reasons.append(
            f"预计 {estimated_gb:.2f} GB > 日预算 {float(budget_gb):.2f} GB"
        )
    if (
        remaining_gb is not None
        and float(remaining_gb) >= 0
        and estimated_gb > float(remaining_gb)
    ):
        reasons.append(
            f"预计 {estimated_gb:.2f} GB > 账户剩余 {float(remaining_gb):.2f} GB"
        )

    return {
        "ok": not reasons or bool(force),
        "estimated_gb": estimated_gb,
        "raw_gb": round(raw_gb, 3),
        "remaining_gb": remaining_gb,
        "budget_gb": budget_gb,
        "safety_ratio": ratio,
        "reasons": reasons,
        "forced": bool(force and reasons),
    }
