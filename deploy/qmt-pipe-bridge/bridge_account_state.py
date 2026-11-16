"""账户快照的分组件新鲜度（纯函数，无 redis / 时间格式化依赖）。

背景
----
2026-11-16 审计：``positions_worker`` 在 ``get_asset`` 失败时复用旧
``_asset_cache``，但 ``build_position_payload`` 仍把 ``updated_at`` 写成当前
时间，后端与看门狗因此把旧资产当新快照。资产与持仓是两个独立的观测源：
资产每 ``positions.asset_refresh_s`` 刷新一次，持仓每轮刷新。把它们压成一个
时间戳，必然要么误报陈旧、要么掩盖陈旧。

本模块只做判定：各组件给 ``observed_at``（epoch 秒；``None``/``<=0`` =
从未观测），各给一个 stale 阈值，输出状态、年龄、原因。格式化留给调用方，
这样测试不需要冻结本地时区。
"""

from __future__ import annotations

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_UNAVAILABLE = "unavailable"


def _age(observed_at, now):
    try:
        value = float(observed_at)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        current = float(now)
    except (TypeError, ValueError):
        return None
    return max(0.0, current - value)


def component_status(observed_at, now, stale_after_s, *, error="") -> str:
    """单个组件的状态。

    * 从未观测 -> ``unavailable``（不是 ok）；
    * 年龄超过阈值 -> ``stale``；
    * 否则 ``ok``。

    ``error`` 不直接改变状态：缓存还在阈值内时，一次刷新失败不等于陈旧；
    它进入 ``degraded_reasons``，由调用方决定告警级别。只有年龄超限才影响
    ``stale``，这样不会因为一次抖动把整份快照打停。
    """
    age = _age(observed_at, now)
    if age is None:
        return STATUS_UNAVAILABLE
    try:
        threshold = float(stale_after_s or 0)
    except (TypeError, ValueError):
        threshold = 0.0
    if threshold > 0 and age > threshold:
        return STATUS_STALE
    return STATUS_OK


def merge_observation(
    *,
    asset_observed_at,
    positions_observed_at,
    now,
    asset_stale_after_s,
    positions_stale_after_s,
    asset_error="",
    positions_error="",
) -> dict:
    """合成一份带分组件状态的元数据。

    返回字段（全部可 JSON 序列化）：
    ``asset_age_s`` / ``positions_age_s`` / ``asset_status`` /
    ``positions_status`` / ``asset_error`` / ``positions_error`` /
    ``stale`` / ``stale_reasons`` / ``degraded_reasons``。
    """
    asset_age = _age(asset_observed_at, now)
    positions_age = _age(positions_observed_at, now)
    asset_status = component_status(
        asset_observed_at, now, asset_stale_after_s, error=asset_error
    )
    positions_status = component_status(
        positions_observed_at,
        now,
        positions_stale_after_s,
        error=positions_error,
    )

    stale_reasons: list[str] = []
    degraded_reasons: list[str] = []
    if asset_status != STATUS_OK:
        stale_reasons.append("asset:%s" % asset_status)
    if positions_status != STATUS_OK:
        stale_reasons.append("positions:%s" % positions_status)
    if asset_error and asset_status == STATUS_OK:
        degraded_reasons.append("asset:last_refresh_failed")
    if positions_error and positions_status == STATUS_OK:
        degraded_reasons.append("positions:last_refresh_failed")

    return {
        "asset_age_s": round(asset_age, 1) if asset_age is not None else None,
        "positions_age_s": (
            round(positions_age, 1) if positions_age is not None else None
        ),
        "asset_status": asset_status,
        "positions_status": positions_status,
        "asset_error": str(asset_error or ""),
        "positions_error": str(positions_error or ""),
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "degraded_reasons": degraded_reasons,
    }
