"""因子研究 → 训练目录注册（把研究页多选的因子写进训练因子目录草稿）。

背景：因子研究页与训练页此前是两条互不相通的链——研究页能多选、能对比合成，
但没有任何出口把这些因子送进 ``qm_training_factor_mapping``；训练页只能从
「已发现字段」全量播种，再由管理员逐个开关。这个模块补的就是中间那一段。

职责切分：
- :func:`resolve_registrable` 纯解析（code → 候选映射行），可离线单测；
- :func:`register_research_factors` 落库（找/建草稿 + 逐库写入 + 审计 + 回报明细）。

机构级约束（这些是硬要求，不是风格偏好）：

1. **只写草稿，绝不碰已发布版本**。发布是管理员在训练页的显式动作
   （``publish_catalog_version``）；注册若顺手改线上口径，等于绕过发布闸门。
2. **绝不猜来源库**。目录项缺 ``l2`` 就跳过，不回退默认源——把 A 库的列名挂到
   B 库下，训练时读出来是 NaN，而没有任何一层会报错（静默错位是最贵的故障）。
3. **校验失败即拒绝（fail-closed）**。来源库未刷新字段时**不能**因为"没数据可
   比对"就放行——列存在性无法验证时写入即失控。
4. **审计与副作用同事务**。不使用 ``AuditLogService``：它在 ``log_action`` 内部
   自行 ``commit()``，会把这个事务提前提交，破坏原子性（见该服务 :97）。
5. **并发建草稿由事务级 advisory lock 串行化**。两个管理员同时注册不会产生两份
   同源草稿——不加唯一索引是为了不在存量库上因历史重复数据炸掉 ``_ensure_schema``。
6. ``enabled=True`` 但 ``default_selected=False``：用户表达的是「允许参与训练」，
   不是「以后每次训练都默认勾上」。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.services.engine.data_platform.quantdb_factor_dictionary import (
    definition_for,
)
from backend.services.engine.data_platform.quantdb_factor_reader import (
    EXCLUDED_FROM_TRAINING,
    KEY_COLUMNS,
    MARKET_FACTOR_SOURCES,
    REQUIRED_COLUMNS,
    normalize_market,
)

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 单次注册上限：一次请求的映射行逐条 INSERT，放开会让管理端点变成写放大器。
MAX_CODES = 500

# 列宽（对齐 qm_training_factor_mapping 的 DDL）。标识符列超宽不截断——截断会写出
# 一个既不对应真实列、也不报错的特征名；展示列超宽则截断（丢的只是文案）。
_COLUMN_MAX = 128
_DISPLAY_NAME_MAX = 256
_CATEGORY_ID_MAX = 64
_CATEGORY_NAME_MAX = 128


def _clamp(value: str, limit: int) -> str:
    """按目标列宽截断展示类文本（超宽会让 INSERT 抛 StringDataRightTruncation）。"""
    return value if len(value) <= limit else value[:limit]

# feature_key 不得占用的列：主键/OHLCV/标签契约列，占用会覆盖行情列。
_RESERVED_FEATURE_KEYS = frozenset(KEY_COLUMNS) | set(REQUIRED_COLUMNS) | {
    "trade_date",
    "symbol",
    "dt",
}

AUDIT_ACTION = "register_research_factors"
AUDIT_RESOURCE = "training_factor_catalog"


class RegistrationError(Exception):
    """注册不可继续（端点层翻译成 4xx）。"""

    status_code = 400


class VersionNotDraft(RegistrationError):
    """目标版本不存在或不是草稿。"""

    status_code = 409


class SourceMismatch(RegistrationError):
    """选中因子的来源库与目标草稿来源不符。"""

    status_code = 400


class TooManyCodes(RegistrationError):
    """一次请求的因子数超上限。"""

    status_code = 422


class UnknownMarket(RegistrationError):
    """市场标识无法识别（写路径不猜）。"""

    status_code = 422


# normalize_market 的市场别名（读路径兜底用；写路径只用来判定「是否认识」）。
_MARKET_ALIASES = frozenset({"A", "A_SHARE", "SSE", "CN"})


def _require_known_market(market: str) -> str:
    """写路径 fail-closed：不认识的市场直接拒，不能静默落成 CN。

    读路径用 normalize_market 兜底是合理的（少一个市场不该让页面崩），但写路径
    兜底意味着 ``market="XX"`` 会被静默写进 CN 草稿——改的是训练口径，不能猜。
    """
    raw = str(market or "").upper().strip()
    if raw in _MARKET_ALIASES or raw in MARKET_FACTOR_SOURCES:
        return normalize_market(raw)
    raise UnknownMarket(
        f"未知市场：{market}（可用：{'、'.join(sorted(MARKET_FACTOR_SOURCES))}）"
    )


@dataclass(frozen=True)
class RegistrationCandidate:
    """一条待写入的映射行（source_dataset + source_column → feature_key）。"""

    source_dataset: str
    source_column: str
    feature_key: str
    display_name: str
    category_id: str
    category_name: str
    sort_order: int
    enabled: bool = True
    default_selected: bool = False


@dataclass(frozen=True)
class SkippedFactor:
    """被跳过的因子及其可展示原因（前端逐条回报，不静默吞）。"""

    code: str
    reason: str


def _factor_index(factors: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for item in factors or ():
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        if code and code not in index:
            index[code] = item
    return index


def resolve_registrable(
    codes: Sequence[str],
    factors: Iterable[Mapping[str, Any]],
) -> tuple[list[RegistrationCandidate], list[SkippedFactor]]:
    """把研究页的因子 code 列表解析成待注册的映射行。

    保持传入顺序、按 code 去重；无法安全注册的逐条进 ``skipped`` 并带原因。
    """
    index = _factor_index(factors)
    candidates: list[RegistrationCandidate] = []
    skipped: list[SkippedFactor] = []
    seen: set[str] = set()

    for raw in codes or ():
        code = str(raw or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)

        entry = index.get(code)
        if entry is None:
            skipped.append(SkippedFactor(code, "该因子不在当前数据集中"))
            continue

        library = str(entry.get("l2") or "").strip()
        if not library:
            skipped.append(SkippedFactor(code, "目录项缺少来源库（l2），无法定位物理列"))
            continue
        if not _IDENTIFIER.fullmatch(library):
            skipped.append(SkippedFactor(code, f"来源库名不合法：{library}"))
            continue
        if library in EXCLUDED_FROM_TRAINING:
            skipped.append(
                SkippedFactor(code, f"{library} 是标签/泄漏库，不得作为训练特征源")
            )
            continue
        # 与 PUT /versions/{id}/mappings 同口径：两条写入口不能一个严一个松。
        # 标识符也能挡住投毒快照里带引号/换行的 code。
        if not _IDENTIFIER.fullmatch(code):
            skipped.append(SkippedFactor(code, "因子名不是合法标识符，无法作为特征名"))
            continue
        if code in _RESERVED_FEATURE_KEYS:
            skipped.append(SkippedFactor(code, f"{code} 是主键/行情列，不能作为特征"))
            continue
        # 超宽标识符截断后会变成一个既不对应真实列、也不报错的特征名，只能拒。
        if len(code) > _COLUMN_MAX:
            skipped.append(
                SkippedFactor(code[:64] + "…", f"因子名超过 {_COLUMN_MAX} 字符，无法作为特征名")
            )
            continue

        definition = definition_for(code)
        candidates.append(
            RegistrationCandidate(
                source_dataset=library,
                source_column=code,
                feature_key=code,
                display_name=_clamp(
                    str(entry.get("display_name") or definition["display_name"]),
                    _DISPLAY_NAME_MAX,
                ),
                category_id=_clamp(str(definition["category_id"]), _CATEGORY_ID_MAX),
                category_name=_clamp(str(definition["category_name"]), _CATEGORY_NAME_MAX),
                sort_order=int(definition["sort_order"]),
            )
        )

    return candidates, skipped


async def _lock_draft_scope(session, *, market: str, source_dataset: str) -> None:
    """串行化同一 (market, source_dataset) 的「找草稿/建草稿」。

    事务级 advisory lock，事务结束自动释放；不引入唯一索引，避免存量库因历史
    重复草稿导致 ``_ensure_schema`` 失败而拖垮整个训练数据集页。

    **调用方必须按 ``sorted()`` 顺序加锁**：多库请求会连加多把，锁又持有到事务
    结束，按传入顺序加锁时 A=[libA,libB] 与 B=[libB,libA] 构成 ABBA 环。
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
        {"key": f"qm-factor-draft:{market}:{source_dataset}"},
    )


async def _existing_mappings(
    session, *, version_id: str, source_dataset: str
) -> tuple[dict[str, str], dict[str, str]]:
    """该草稿现有的映射索引：(source_column→feature_key, feature_key→source_column)。

    ``qm_training_factor_mapping`` 上有**两条**唯一约束（``feature_key`` 一条、
    ``source_column`` 一条），而 ``PUT /versions/{id}/mappings`` 允许二者不等
    （人工起别名）。于是存在「只命中其中一条」的半匹配行：此时 ``ON CONFLICT``
    声明的目标不命中，INSERT 撞上另一条约束抛 ``IntegrityError`` —— 未捕获即
    整个批次 500 回滚。注册前先查出来逐条跳过，既能给出可读原因，也符合本模块
    「部分成功不吞」的口径。
    """
    rows = (
        await session.execute(
            text(
                "SELECT source_column, feature_key FROM qm_training_factor_mapping "
                "WHERE version_id = :version_id AND source_dataset = :source_dataset"
            ),
            {"version_id": version_id, "source_dataset": source_dataset},
        )
    ).mappings().all()
    by_column = {str(r["source_column"]): str(r["feature_key"]) for r in rows}
    by_key = {str(r["feature_key"]): str(r["source_column"]) for r in rows}
    return by_column, by_key


def _alias_conflict(
    cand: RegistrationCandidate,
    by_column: Mapping[str, str],
    by_key: Mapping[str, str],
) -> str | None:
    """半匹配即冲突；返回可展示原因，无冲突返回 None（两条都命中=正常更新）。"""
    col_owner = by_column.get(cand.source_column)
    if col_owner is not None and col_owner != cand.feature_key:
        return (
            f"该列在此草稿中已命名为 {col_owner}，注册会覆盖人工别名——"
            "请先在训练数据集页处理该映射"
        )
    key_owner = by_key.get(cand.feature_key)
    if key_owner is not None and key_owner != cand.source_column:
        return (
            f"特征名 {cand.feature_key} 已被该草稿的 {key_owner} 占用，"
            "请先在训练数据集页处理该映射"
        )
    return None


async def _draft_version_id(
    session, *, market: str, source_dataset: str, version_name: str, created_by: str
) -> str:
    """该 (market, source_dataset) 下的草稿；没有就建一个。调用方须先持锁。"""
    row = (
        await session.execute(
            text(
                "SELECT version_id FROM qm_training_factor_catalog_version "
                "WHERE market = :market AND source_dataset = :source_dataset "
                "AND status = 'draft' ORDER BY created_at DESC LIMIT 1"
            ),
            {"market": market, "source_dataset": source_dataset},
        )
    ).mappings().first()
    if row:
        return str(row["version_id"])

    from backend.services.api.routers.admin.quantdb_factor_catalog import (
        create_catalog_draft,
        seed_draft_from_published,
    )

    draft_id = await create_catalog_draft(
        session, source_dataset, version_name, market, created_by=created_by
    )
    # 空草稿 + 「发布 = 替换」（publish_catalog_version 归档旧版、扶正草稿，不合并）
    # = 一点发布线上口径就只剩本次注册的几个。先抄一份线上那份再往上加。
    await seed_draft_from_published(
        session, draft_id=draft_id, market=market, source_dataset=source_dataset
    )
    return draft_id


async def _discovered_fields(session, *, market: str, source_dataset: str) -> set[str]:
    rows = (
        await session.execute(
            text(
                "SELECT column_name FROM qm_quantdb_factor_field "
                "WHERE market = :market AND dataset_id = :dataset_id AND is_present"
            ),
            {"market": market, "dataset_id": source_dataset},
        )
    ).scalars().all()
    return {str(r) for r in rows}


async def _write_audit(
    session,
    *,
    user_id: str,
    tenant_id: str,
    market: str,
    dataset: str,
    codes: Sequence[str],
    versions: Mapping[str, str],
    registered: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, str]],
) -> None:
    """审计行与副作用同事务提交（不调 AuditLogService——它会自行 commit）。

    必须连同**因子清单**一起落库：只记「注册 3 个」的话，事后无从回答「是哪三个」，
    而映射表本身没有 created_by/created_at。对一个以「防标签穿越」为职责的治理
    链路，这条恰恰是关键的取证字段。
    """
    await session.execute(
        text(
            """
            INSERT INTO user_audit_logs
             (user_id, tenant_id, action, resource, resource_id, description,
              request_data, response_data, status_code, success)
            VALUES (:user_id, :tenant_id, :action, :resource, :resource_id, :description,
                    :request_data, :response_data, 200, :success)
            """
        ),
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "action": AUDIT_ACTION,
            "resource": AUDIT_RESOURCE,
            "resource_id": ",".join(dict.fromkeys(versions.values()))[:128] or None,
            "description": (
                f"因子研究注册到训练目录：数据集 {dataset} / 市场 {market} / "
                f"注册 {len(registered)} 个，跳过 {len(skipped)} 个"
            ),
            "request_data": json.dumps(
                {"dataset": dataset, "market": market, "codes": list(codes)[:MAX_CODES]},
                ensure_ascii=False,
            ),
            "response_data": json.dumps(
                {
                    "registered": [r["code"] for r in registered],
                    "skipped": [dict(s) for s in skipped],
                    "versions": dict(versions),
                },
                ensure_ascii=False,
            ),
            "success": len(registered) > 0,
        },
    )


async def register_research_factors(
    session,
    *,
    market: str,
    dataset: str,
    codes: Sequence[str],
    user_id: str = "admin",
    tenant_id: str = "default",
    version_id: str | None = None,
    version_name: str | None = None,
) -> dict[str, Any]:
    """把研究页选中的因子写入训练目录草稿。

    ``version_id`` 给定时只写该草稿（须为 draft 且来源库匹配，跨库注册直接拒绝）；
    未给定时按来源库分别找/建草稿。整体在一个事务内提交：要么全部映射行 + 审计
    行落地，要么全部回滚。返回逐条明细，部分成功不吞。

    重复注册是幂等的（``ON CONFLICT DO UPDATE``），但**会把该草稿里已被人手工
    关闭的同一因子重新 ``enabled=True``**——「注册」的语义就是「允许它参与训练」，
    这一点是刻意的，不是副作用。
    """
    from backend.services.engine.factor_research import store

    if len(codes or ()) > MAX_CODES:
        raise TooManyCodes(f"一次最多注册 {MAX_CODES} 个因子，收到 {len(codes)} 个")

    market = _require_known_market(market)
    meta = store.factors_meta(dataset) or {}
    candidates, skipped_all = resolve_registrable(codes, meta.get("factors") or [])
    skipped: list[dict[str, str]] = [
        {"code": s.code, "reason": s.reason} for s in skipped_all
    ]

    # 指定草稿时先校验可写性与来源唯一性——跨库多选不能塞进单一来源的草稿。
    pinned: Mapping[str, Any] | None = None
    if version_id:
        pinned = (
            await session.execute(
                text(
                    "SELECT version_id, status, source_dataset, market "
                    "FROM qm_training_factor_catalog_version WHERE version_id = :version_id"
                ),
                {"version_id": version_id},
            )
        ).mappings().first()
        if not pinned:
            raise VersionNotDraft("目录版本不存在")
        if str(pinned["status"]) != "draft":
            raise VersionNotDraft("只有草稿目录可以编辑")
        market = str(pinned["market"])

    by_library: dict[str, list[RegistrationCandidate]] = {}
    for cand in candidates:
        by_library.setdefault(cand.source_dataset, []).append(cand)

    if pinned is not None:
        if len(by_library) > 1:
            raise SourceMismatch(
                "选中的因子来自多个来源库，无法写入同一个目录草稿："
                + "、".join(sorted(by_library))
            )
        if by_library:
            only = next(iter(by_library))
            if only != str(pinned["source_dataset"]):
                raise SourceMismatch(
                    f"该草稿的来源库是 {pinned['source_dataset']}，"
                    f"与选中的因子（{only}）不一致"
                )

    stamp = version_name or (
        f"因子研究注册 {dataset} {datetime.now(timezone.utc):%Y-%m-%d}"
    )
    versions: dict[str, str] = {}
    registered: list[dict[str, Any]] = []

    # 固定全局加锁顺序（见 _lock_draft_scope 的 ABBA 说明）——不能按传入顺序。
    for library in sorted(by_library):
        group = by_library[library]
        await _lock_draft_scope(session, market=market, source_dataset=library)
        target = (
            str(pinned["version_id"])
            if pinned is not None
            else await _draft_version_id(
                session,
                market=market,
                source_dataset=library,
                version_name=stamp,
                created_by=user_id,
            )
        )
        versions[library] = target

        # 发布闸门复核（防 TOCTOU）：开头那次 status 检查与这里的 INSERT 之间，
        # 另一个管理员可能已把该版本 publish 掉。映射行的外键检查只取
        # FOR KEY SHARE，与 publish 的 UPDATE（FOR NO KEY UPDATE）**不冲突**，
        # 行锁挡不住这个窗口 —— 结果就是映射被静默写进已发布版本、绕过发布闸门，
        # 而响应仍然成功。这里重新取一次行锁再断言 draft；
        # FOR NO KEY UPDATE 与并发注册的外键 FOR KEY SHARE 兼容，不会把两个
        # 并发注册串死，只与 publish 互斥。
        locked = (
            await session.execute(
                text(
                    "SELECT status FROM qm_training_factor_catalog_version "
                    "WHERE version_id = :version_id FOR NO KEY UPDATE"
                ),
                {"version_id": target},
            )
        ).mappings().first()
        if not locked or str(locked["status"]) != "draft":
            raise VersionNotDraft("目标目录版本已不是草稿（可能刚被发布），请刷新页面后重试")

        known = await _discovered_fields(session, market=market, source_dataset=library)
        by_column, by_key = await _existing_mappings(
            session, version_id=target, source_dataset=library
        )
        for cand in group:
            # fail-closed：列存在性无法验证（来源库从未刷新字段）时一律拒绝。
            # 放行会让错误的映射在训练时才炸（reader 抛 missing mapped fields），
            # 或者更糟——静默读到 NaN。
            if cand.source_column not in known:
                skipped.append(
                    {
                        "code": cand.source_column,
                        "reason": (
                            f"{library} 中未发现该列，请先在训练数据集页「刷新字段」"
                            if known
                            else f"{library} 尚未刷新字段，无法校验列存在性"
                        ),
                    }
                )
                continue
            if (conflict := _alias_conflict(cand, by_column, by_key)) is not None:
                skipped.append({"code": cand.source_column, "reason": conflict})
                continue
            await session.execute(
                text(
                    """
                    INSERT INTO qm_training_factor_mapping
                     (mapping_id, version_id, source_dataset, source_column, feature_key,
                      display_name, category_id, category_name, enabled, default_selected,
                      required, sort_order)
                    VALUES (:mapping_id, :version_id, :source_dataset, :source_column,
                            :feature_key, :display_name, :category_id, :category_name,
                            :enabled, :default_selected, FALSE, :sort_order)
                    ON CONFLICT (version_id, source_dataset, feature_key) DO UPDATE SET
                      source_column = EXCLUDED.source_column,
                      display_name = EXCLUDED.display_name,
                      category_id = EXCLUDED.category_id,
                      category_name = EXCLUDED.category_name,
                      enabled = EXCLUDED.enabled
                    """
                ),
                {
                    "mapping_id": uuid.uuid4().hex,
                    "version_id": target,
                    "source_dataset": cand.source_dataset,
                    "source_column": cand.source_column,
                    "feature_key": cand.feature_key,
                    "display_name": cand.display_name,
                    "category_id": cand.category_id,
                    "category_name": cand.category_name,
                    "enabled": cand.enabled,
                    "default_selected": cand.default_selected,
                    "sort_order": cand.sort_order,
                },
            )
            registered.append(
                {
                    "code": cand.source_column,
                    "source_dataset": cand.source_dataset,
                    "version_id": target,
                    "feature_key": cand.feature_key,
                }
            )

    # 审计与副作用同事务：即便「一个都没注册」也留痕（管理员动作要可回溯）。
    await _write_audit(
        session,
        user_id=user_id,
        tenant_id=tenant_id,
        market=market,
        dataset=dataset,
        codes=codes,
        versions=versions,
        registered=registered,
        skipped=skipped,
    )

    logger.info(
        "因子研究注册：dataset=%s market=%s 注册 %d 跳过 %d user=%s",
        dataset, market, len(registered), len(skipped), user_id,
    )
    return {
        "dataset": dataset,
        "market": market,
        "registered": registered,
        "skipped": skipped,
        "versions": versions,
    }
