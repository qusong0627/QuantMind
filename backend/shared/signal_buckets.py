"""信号桶（模型隔离）口径唯一实现（P2-0 ·《滚动训练与模型生命周期》§5.3）。

写侧早已按模型桶隔离：``engine_signal_scores.feature_version`` 携带
``script_v1_<模型桶>``（唯一写入口 = ``InferenceScriptRunner._persist_and_publish``），
同 tenant/user/trade_date 下多模型按桶并存。但读侧旧口径不看桶（自选池 / 持仓
哨兵 / 模型信号扫描器）——任何候选模型（观察期挑战者）的批量推理行都会混进
生产读取。2026-10-08 实测本机默认租户：同日 2 个 script_v1 桶（生产 3274 行 +
挑战者 3222 行）+ admin 的实时 ext 桶同时在库，`DISTINCT ON (symbol)` 按
``created_at`` 谁后写谁赢。

本模块承担两件事：

1. **桶名计算**（``normalize_model_bucket`` / ``resolve_feature_version``）：
   原实现私有在 ``InferenceScriptRunner``，上收为 shared——读写两侧共用同一份
   折叠/截断规则；读侧手抄一份正则的下场就是「读不到自己写的行」。
2. **生效桶解析**（``resolve_effective_bucket``）：复用 model_registry 的生效模型
   优先级（显式 → 策略绑定 → 用户默认 → system 兜底）。调用方没有 user 上下文时
   （哨兵/自选视图），按「租户内持有 is_default 模型的用户」解析；0 个或多持有者
   = 无法确定 → ``bucket=None``，**不猜**。

读路径模式（``get_scoping_mode``）：``off``（默认，旧口径）/ ``shadow``（旧口径
出数 + 影子比对留痕，enforce 前的证据积累期）/ ``enforce``（只读生效模型桶；解析
失败 → 告警 + 空结果 + reason，绝不退回全桶混读）。env
``SIGNAL_BUCKET_SCOPING`` 为进程默认，Redis 键 ``quantmind:signal_bucket_scoping``
可运行期覆盖——回退不必重建容器（进程内缓存 30s 生效）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any

from backend.shared.logging_config import get_logger

logger = get_logger(__name__)

SCOPING_ENV = "SIGNAL_BUCKET_SCOPING"
SCOPING_REDIS_KEY = "quantmind:signal_bucket_scoping"
VALID_SCOPING_MODES = ("off", "shadow", "enforce")
DEFAULT_SCOPING_MODE = "off"

#: 模式进程内缓存 TTL：读路径高频调用（哨兵每轮），Redis 覆盖的感知延迟上限。
_MODE_CACHE_TTL_SECONDS = 30.0
#: 影子比对留痕键（计数 JSON 读改写；RedisSentinelClient 无 HINCRBY）。
SHADOW_STATS_KEY = "qm:signal_bucket_shadow:{kind}:stats"
SHADOW_LAST_KEY = "qm:signal_bucket_shadow:{kind}:last"
SHADOW_EVIDENCE_TTL_SECONDS = 7 * 24 * 3600
#: 影子比对数值容差（与增量特征金样同款 ε）。
_SHADOW_VALUE_EPS = 1e-9

_mode_cache: tuple[float, str] | None = None
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning("[signal_buckets] %s", message)


# ─────────────────────────────────────────────────────────────────────────────
# 桶名计算（写侧契约——逐位复刻 InferenceScriptRunner 原实现）
# ─────────────────────────────────────────────────────────────────────────────


def normalize_model_bucket(model_id: str | None) -> str:
    """模型 id → 桶 slug（**写侧契约，改一位就是数据契约变更**）。

    空/None → ``inference_script``；非 [a-z0-9_] 折叠为下划线；截断 48 字符。
    修改任何一位都会让读侧对不上存量行（按旧桶名落库的 3000+ 行/日）。
    """
    raw = str(model_id or "").strip().lower()
    if not raw:
        return "inference_script"
    slug = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return slug[:48] if slug else "inference_script"


def resolve_feature_version(model_id: str | None) -> str:
    """模型 id → ``engine_signal_scores.feature_version`` 桶名。"""
    return f"script_v1_{normalize_model_bucket(model_id)}"


# ─────────────────────────────────────────────────────────────────────────────
# 读路径模式：env 默认 + Redis 运行期覆盖（回退不必重建容器）
# ─────────────────────────────────────────────────────────────────────────────


def reset_scoping_mode_cache() -> None:
    """清模式缓存（测试用；运维切模式后最多 30s 自然生效，无需调用）。"""
    global _mode_cache
    _mode_cache = None


def _default_redis_client() -> Any:
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        return get_redis_sentinel_client()
    except Exception:  # noqa: BLE001 - Redis 不可达时按 env 判模式即可
        return None


def _kv_get(client: Any, key: str) -> Any:
    """同步 KV 读。sentinel 客户端支持 use_slave；裸 redis 客户端不支持。

    一律读主库：模式开关的回退必须立即权威（等副本秒级滞后可能让「关上」
    多生效一轮）；证据计数是读改写，同样不能读副本。
    """
    try:
        return client.get(key, use_slave=False)
    except TypeError:
        return client.get(key)


def _kv_set(client: Any, key: str, value: bytes, *, ex: int | None = None) -> None:
    try:
        client.set(key, value, ex=ex)
    except TypeError:
        client.set(key, value)


def get_scoping_mode(
    *,
    redis_client: Any = None,
    now: float | None = None,
    use_cache: bool = True,
) -> str:
    """读路径模式：Redis 运行期键 > env > 默认 ``off``；非法值回退默认（告警一次）。

    30s 进程内缓存：读路径每请求探 Redis 没有意义（哨兵是分钟级循环），
    覆盖/回退的生效延迟上限 30s。
    """
    global _mode_cache
    t = time.monotonic() if now is None else float(now)
    if (
        use_cache
        and _mode_cache is not None
        and t - _mode_cache[0] < _MODE_CACHE_TTL_SECONDS
    ):
        return _mode_cache[1]
    raw: Any = None
    client = redis_client if redis_client is not None else _default_redis_client()
    if client is not None:
        try:
            raw = _kv_get(client, SCOPING_REDIS_KEY)
        except Exception:  # noqa: BLE001 - Redis 异常不改变读路径语义
            raw = None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if raw is None or str(raw).strip() == "":
        raw = os.getenv(SCOPING_ENV, DEFAULT_SCOPING_MODE)
    mode = str(raw or "").strip().lower()
    if mode not in VALID_SCOPING_MODES:
        _warn_once(
            f"invalid-mode:{mode}",
            f"非法 scoping 模式 {mode!r}（env={SCOPING_ENV} / redis="
            f"{SCOPING_REDIS_KEY}），回退 {DEFAULT_SCOPING_MODE}",
        )
        mode = DEFAULT_SCOPING_MODE
    _mode_cache = (t, mode)
    return mode


# ─────────────────────────────────────────────────────────────────────────────
# 生效桶解析（复用 model_registry 优先级；无 user 上下文时按租户默认持有者）
# ─────────────────────────────────────────────────────────────────────────────

_OWNER_SQL = (
    "SELECT DISTINCT user_id FROM qm_user_models "
    "WHERE tenant_id = :tenant_id AND is_default = TRUE "
    "AND status IN ('ready', 'active') "
    "AND qm_market_of(metadata_json) = :market"
)


async def _tenant_default_owner(tenant_id: str, market: str) -> tuple[str | None, str]:
    """租户内 is_default 模型持有者；0 个或 >=2 个用户 → (None, 原因)（不猜）。

    与 ``model_registry._get_default_model_sync`` 同一过滤口径（status
    ready/active、market 默认 CN），只是不限定 user——多用户各有默认=歧义，
    此时任何单桶选择都是替用户猜，宁可让读侧显式降级（enforce 空+原因 /
    shadow 留痕）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(_OWNER_SQL), {"tenant_id": tenant_id, "market": market}
            )
        ).fetchall()
    owners = sorted({str(r[0]) for r in rows if r[0]})
    if len(owners) == 1:
        return owners[0], ""
    if not owners:
        return None, "no_default_model_owner"
    return None, f"ambiguous_default_owners:{','.join(owners)}"


async def resolve_effective_bucket(
    *,
    tenant_id: str,
    user_id: str | None = None,
    market: str = "CN",
    registry: Any = None,
) -> dict[str, Any]:
    """生效模型 → 桶。返回 ``{"bucket","model_id","model_source","fallback_used",
    "reason","owner_user"}``；解析不到时 ``bucket=None`` 且 ``reason`` 非空。

    优先级复用 ``ModelRegistryService.resolve_effective_model_sync``（显式 →
    策略绑定 → 用户默认 → system 兜底）。``user_id`` 缺省时按租户 is_default
    持有者解析。注册表是同步 DB，调用包 ``to_thread``。
    """
    tenant = str(tenant_id or "default").strip() or "default"
    # 市场口径唯一实现：与 model_registry 同一份 _canonical_market（SQL 侧
    # qm_market_of 同源）。旧实现 .upper().strip() 会把 'CUSTOM' 这类未知值
    # 原样传给谓词，与注册表读侧（未知→CN）分叉——桶解析与默认模型读的不是同一行。
    from backend.shared.model_registry import _canonical_market

    wanted = _canonical_market(market)
    owner = str(user_id or "").strip()
    if not owner:
        owner, reason = await _tenant_default_owner(tenant, wanted)
        if not owner:
            _warn_once(
                f"owner:{tenant}:{reason}",
                f"生效模型持有者解析失败 tenant={tenant} market={wanted}: "
                f"{reason}；不混读",
            )
            return {
                "bucket": None,
                "model_id": None,
                "model_source": None,
                "fallback_used": False,
                "reason": reason,
                "owner_user": None,
            }
    if registry is None:
        from backend.shared.model_registry import model_registry_service

        registry = model_registry_service
    try:
        resolved = await asyncio.to_thread(
            registry.resolve_effective_model_sync,
            tenant_id=tenant,
            user_id=owner,
            market=wanted,
        )
    except Exception as exc:  # noqa: BLE001 - 解析失败按「解析不到」处置（不混读）
        _warn_once(
            f"resolve:{tenant}:{owner}",
            f"生效模型解析异常 tenant={tenant} user={owner}: {exc}",
        )
        return {
            "bucket": None,
            "model_id": None,
            "model_source": None,
            "fallback_used": False,
            "reason": f"resolve_failed:{exc}",
            "owner_user": owner,
        }
    resolved = resolved or {}
    model_id = str(resolved.get("effective_model_id") or "").strip()
    if not model_id:
        return {
            "bucket": None,
            "model_id": None,
            "model_source": resolved.get("model_source"),
            "fallback_used": bool(resolved.get("fallback_used")),
            "reason": str(resolved.get("fallback_reason") or "no_effective_model"),
            "owner_user": owner,
        }
    return {
        "bucket": resolve_feature_version(model_id),
        "model_id": model_id,
        "model_source": resolved.get("model_source"),
        "fallback_used": bool(resolved.get("fallback_used")),
        "reason": "",
        "owner_user": owner,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 影子比对（纯函数）+ 证据留痕（best-effort）
# ─────────────────────────────────────────────────────────────────────────────


def shadow_diff(
    old_values: Mapping[str, float | None],
    new_values: Mapping[str, float | None],
    *,
    old_date: Any = None,
    new_date: Any = None,
) -> dict[str, Any]:
    """旧口径 vs 按桶口径的读结果差异（模式 shadow 的证据核，纯函数）。

    ``equal=True`` 当且仅当：信号日相同、两侧标的集合相同、共有标的值差 ≤1e-9
    （None↔数值切换也计差异——「没有分数」与「分数是 0」不是一回事）。
    """
    old_syms, new_syms = set(old_values), set(new_values)
    only_old = sorted(old_syms - new_syms)
    only_new = sorted(new_syms - old_syms)
    value_diff: list[str] = []
    for sym in sorted(old_syms & new_syms):
        ov, nv = old_values[sym], new_values[sym]
        if (ov is None) != (nv is None):
            value_diff.append(sym)
        elif ov is not None and nv is not None and abs(float(ov) - float(nv)) > _SHADOW_VALUE_EPS:
            value_diff.append(sym)
    equal = (
        not only_old
        and not only_new
        and not value_diff
        and str(old_date) == str(new_date)
    )
    return {
        "equal": equal,
        "old_date": str(old_date) if old_date is not None else None,
        "new_date": str(new_date) if new_date is not None else None,
        "old_rows": len(old_values),
        "new_rows": len(new_values),
        "only_old": len(only_old),
        "only_new": len(only_new),
        "value_diff": len(value_diff),
        "sample_only_old": only_old[:5],
        "sample_only_new": only_new[:5],
        "sample_value_diff": value_diff[:5],
    }


def record_shadow_evidence(
    kind: str, diff: Mapping[str, Any], *, redis_client: Any = None
) -> None:
    """影子比对留痕（best-effort）：计数 JSON + 最近一次 diff（7 天）。

    计数走读改写（RedisSentinelClient 无 HINCRBY）；证据场景下极小概率的并发
    丢计数可接受。Redis 不可达只告警一次，绝不拖垮读路径。差异详情进程内只
    详告警一次（哨兵是分钟级循环，每轮全量刷日志会淹没现场）。
    """
    client = redis_client if redis_client is not None else _default_redis_client()
    if client is None:
        return
    stats_key = SHADOW_STATS_KEY.format(kind=kind)
    try:
        raw = _kv_get(client, stats_key)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        stats: dict[str, Any] = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        stats = {}
    stats["total"] = int(stats.get("total") or 0) + 1
    if not diff.get("equal"):
        stats["diff"] = int(stats.get("diff") or 0) + 1
    if diff.get("bucket_missing"):
        stats["missing"] = int(stats.get("missing") or 0) + 1
    stats["last_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        payload = json.dumps(dict(diff), ensure_ascii=False, default=str)
        _kv_set(
            client,
            stats_key,
            json.dumps(stats, ensure_ascii=False).encode("utf-8"),
            ex=SHADOW_EVIDENCE_TTL_SECONDS,
        )
        _kv_set(
            client,
            SHADOW_LAST_KEY.format(kind=kind),
            payload.encode("utf-8"),
            ex=SHADOW_EVIDENCE_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        _warn_once("shadow-evidence", f"影子比对留痕失败（忽略）: {exc}")
        return
    if not diff.get("equal"):
        _warn_once(
            f"shadow-diff:{kind}",
            f"影子读与旧口径存在差异 kind={kind} 首次样本={payload}"
            f"（计数与最近样本见 {stats_key} / last，进程内只详告警一次）",
        )
