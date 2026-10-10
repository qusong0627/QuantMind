"""因子池服务（P1）——池状态 / 谱系边 / 检索注入 / 批量刷新（唯一 DB 出口）。

职责与调用方
------------
* 回测完成钩子 ``record_backtested_factor``（engine ``_backtest_via_qlib``
  成功路径）：写面板缓存 + 池行 upsert + 公式/task 边。**绝不抛**——
  池是增益层，不能把回测拖挂。
* 批量重算 ``refresh_pool``（``mining_pool_rebuild.py`` / ``POST /pool/refresh``）：
  重算 novelty（面板两两秩相关）、``correlated_with`` 边（|ρ|≥0.8）、
  公式边、task 链边、多样性熵 + 留一贡献、pool_score（``pool_scoring``）。
* 注入 ``prepare_injection`` / ``mark_retrieved``（launcher 在 spawn 前调用，
  spawn 成功后计数）：摘要写 ``<task_log_dir>/pool_context.md``，路径经
  ``QMF_POOL_CONTEXT_PATH`` 传给挖掘子进程，由 ``rd_loop_wrapper`` 读入
  提示词——**只走提示词通道，零运行时副作用**（不碰 base_factors.json，
  否则 LLM 会把摘要文本当可用基础特征，见 P1 计划的关键约束）。
* 种子摘要 ``build_seed_digest``（T-MV-01 父本定向演化的注入源）：与
  ``build_injection_digest`` 同 scope，但不做池评分排序（父本由用户点名，
  保序 = 请求顺序），scope 外 id 进 dropped 如实上报。
* 读接口 ``pool_overview`` / ``list_pool_factors`` / ``pool_graph``
  （alpha_agent 路由的面板数据源）。

口径纪律
--------
* ``user_id`` 是隔离硬约束：池行/边/注入一律按用户过滤，跨用户泄漏
  等于把甲的挖掘成果注进乙的 prompt。
* 边表无 market/universe 列，scope 标记存 ``extra``（``market`` /
  ``universe`` 键）；refresh 按 ``extra->>'market'`` 清旧边再重插——
  没有这一步，阈值调整后旧边残留、谱系图出现幽灵连接。
* 无面板的因子只参与公式/task 边（novelty/pool_score 里的冗余项按「未知
  不惩罚」处理），UI 标「无面板」，不静默冒充算过。
* v1 三条关系全部无向化（``src < dst`` 字典序定向）；真派生关系不做假溯源。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.shared.factor_pool_contract import EDGES_TABLE, POOL_TABLE

from . import pool_panels
from .pool_cleanup import CleanupCandidate, CleanupCriteria, evaluate_cleanup
from .pool_edges import DEFAULT_TOP_K as _FORMULA_TOP_K
from .pool_edges import formula_tokens, similar_pairs
from .pool_scoring import (
    MISSING_TEXT,
    DigestEntry,
    PoolCandidate,
    PoolSota,
    ScoringParams,
    rank_candidates,
    render_digest,
    select_topk,
)

logger = logging.getLogger(__name__)

_ENV_INJECT_K = "QM_FACTOR_POOL_INJECT_K"
_ENV_INJECT_DISABLED = "QM_FACTOR_POOL_INJECT_DISABLED"
DEFAULT_INJECT_K = 5

#: |ρ|≥0.8 连 correlated_with 边（与物化器 _CORR_WARN 同值口径）
CORR_EDGE_THRESHOLD = 0.8

_INJECT_FILENAME = "pool_context.md"


def injection_enabled() -> bool:
    """注入开关（默认开；``QM_FACTOR_POOL_INJECT_DISABLED=1`` 关闭）。"""
    return os.environ.get(_ENV_INJECT_DISABLED, "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    )


def inject_k() -> int:
    raw = os.environ.get(_ENV_INJECT_K, "").strip()
    if not raw:
        return DEFAULT_INJECT_K
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "QM_FACTOR_POOL_INJECT_K=%r 非法，用默认 %d", raw, DEFAULT_INJECT_K
        )
        return DEFAULT_INJECT_K
    return max(0, value)


def _scoring_params() -> ScoringParams:
    """打分系数（yaml ``scoring:`` 段 > 代码默认，缺省见 ScoringParams）。"""
    from .config import load_plugin_config

    raw = load_plugin_config().get("scoring") or {}
    defaults = ScoringParams()

    def _f(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            logger.warning("scoring.%s=%r 非法，用默认 %s", key, raw.get(key), default)
            return default

    return ScoringParams(
        fatigue_weight=_f("fatigue_weight", defaults.fatigue_weight),
        redundancy_lambda=_f("redundancy_lambda", defaults.redundancy_lambda),
        freshness_halflife_days=_f(
            "freshness_halflife_days", defaults.freshness_halflife_days
        ),
    )


def _cleanup_criteria() -> CleanupCriteria:
    """清理判据阈值（yaml ``cleanup:`` 段 > 代码默认，缺省见 CleanupCriteria）。"""
    from .config import load_plugin_config

    raw = load_plugin_config().get("cleanup") or {}
    defaults = CleanupCriteria()

    def _f(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            logger.warning("cleanup.%s=%r 非法，用默认 %s", key, raw.get(key), default)
            return default

    def _i(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            logger.warning("cleanup.%s=%r 非法，用默认 %s", key, raw.get(key), default)
            return default

    return CleanupCriteria(
        corr_dup=_f("corr_dup", defaults.corr_dup),
        weak_icir_quantile=_f("weak_icir_quantile", defaults.weak_icir_quantile),
        min_icir_sample=_i("min_icir_sample", defaults.min_icir_sample),
    )


def _scope_conds(
    user_id: str | None,
    market: str,
    universe: str | None,
    *,
    alias: str = "p",
) -> tuple[list[str], dict[str, Any]]:
    """scope 过滤条件（NULL 安全）：market 走 COALESCE（老因子 market 可能为
    NULL，严格等号会把它们静默漏掉，见 memory: signal-scores-market-nullable）；
    universe=None 表示不过滤（全 universe 汇总）。"""
    conds = [f"COALESCE({alias}.market, :market) = :market"]
    params: dict[str, Any] = {"market": market}
    if user_id is not None:
        conds.append(f"{alias}.user_id = :user_id")
        params["user_id"] = str(user_id)
    if universe is not None:
        conds.append(f"COALESCE({alias}.universe, '') = :universe")
        params["universe"] = universe
    return conds, params


def _as_float(value: Any) -> float | None:
    """元数据 JSON 里的数值字符串 → float；任何解析失败 → None（缺失语义）。"""
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _age_days(created_at: Any) -> float | None:
    if not isinstance(created_at, datetime):
        return None
    ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)


def _json_or_empty(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False)


def _canonical_edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


# ── 写入：池行 / 边（session 级原语，事务由调用方管）────────────────────


async def _upsert_pool_row(
    session,
    *,
    factor_id: str,
    user_id: str,
    market: str,
    universe: str,
    panel_ref: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    from sqlalchemy import text

    await session.execute(
        text(f"""
            INSERT INTO {POOL_TABLE}
                (factor_id, user_id, market, universe, panel_ref, extra, updated_at)
            VALUES
                (:factor_id, :user_id, :market, :universe, :panel_ref,
                 CAST(:extra AS JSONB), NOW())
            ON CONFLICT (factor_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                market = EXCLUDED.market,
                universe = EXCLUDED.universe,
                panel_ref = COALESCE(EXCLUDED.panel_ref, {POOL_TABLE}.panel_ref),
                extra = COALESCE({POOL_TABLE}.extra, '{{}}'::jsonb)
                        || COALESCE(EXCLUDED.extra, '{{}}'::jsonb),
                updated_at = NOW()
        """),
        {
            "factor_id": factor_id,
            "user_id": user_id,
            "market": market,
            "universe": universe,
            "panel_ref": panel_ref,
            "extra": _json_or_empty(extra),
        },
    )


async def _upsert_edge(
    session,
    *,
    user_id: str,
    src: str,
    dst: str,
    relation: str,
    method: str,
    weight: float | None,
    extra: dict[str, Any] | None,
) -> None:
    from sqlalchemy import text

    await session.execute(
        text(f"""
            INSERT INTO {EDGES_TABLE}
                (user_id, src_factor_id, dst_factor_id, relation, method, weight, extra)
            VALUES
                (:user_id, :src, :dst, :relation, :method, :weight, CAST(:extra AS JSONB))
            ON CONFLICT ON CONSTRAINT uq_rd_agent_factor_edges DO UPDATE SET
                weight = EXCLUDED.weight,
                user_id = EXCLUDED.user_id,
                extra = EXCLUDED.extra
        """),
        {
            "user_id": user_id,
            "src": src,
            "dst": dst,
            "relation": relation,
            "method": method,
            "weight": weight,
            "extra": _json_or_empty(extra),
        },
    )


# ── 回测完成钩子 ─────────────────────────────────────────────────────


async def record_backtested_factor(
    factor_id: str,
    *,
    market: str,
    universe: str = "",
    values=None,
    forward_return=None,
) -> bool:
    """回测成功后的池登记（面板 + 池行 + 本因子相关边）。**绝不抛**。

    ``values`` 给定时写价值级面板（qlib 路径在进程内有因子值）；H5 路径
    无进程内值传 None → 面板留空（refresh/rebuild --panels 时补）。
    ``forward_return``（进程内的 r_clean）给定时面板附带 ``fret`` 列——
    组合实验室的 rank-IC 目标从这里取；缺列的历史面板由 rebuild 补。
    novelty / pool_score / 多样性属于**池级重算**，由 refresh_pool 落
    （钩子里算会让批量回测变成 O(N²) 卡顿）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text("""
                        SELECT user_id,
                               COALESCE(market, :market) AS market,
                               COALESCE(universe, '') AS universe,
                               factor_formulation,
                               metadata_json->>'task_id' AS task_id,
                               created_at
                        FROM rd_agent_factors
                        WHERE factor_id = :factor_id
                    """),
                        {"factor_id": factor_id, "market": market},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            logger.warning("[factor-pool] 因子不存在，跳过池登记: %s", factor_id)
            return False
        user_id = str(row["user_id"] or "").strip()
        if not user_id:
            logger.warning(
                "[factor-pool] 因子无 user_id（隔离硬约束），跳过池登记: %s", factor_id
            )
            return False
        scope_market = str(row["market"] or market)
        scope_universe = str(row["universe"] or universe or "")

        panel_ref: str | None = None
        if values is not None:
            try:
                panel_ref = pool_panels.write_panel(
                    scope_market, factor_id, values, forward_return=forward_return
                )
            except Exception as exc:  # noqa: BLE001 — 面板失败不拦池登记/回测
                logger.warning("[factor-pool] 面板写入失败 %s: %s", factor_id, exc)

        async with get_session() as session:
            await _upsert_pool_row(
                session,
                factor_id=factor_id,
                user_id=user_id,
                market=scope_market,
                universe=scope_universe,
                panel_ref=panel_ref,
            )
            edge_extra = {"market": scope_market, "universe": scope_universe}

            # task 链边：同任务内按 created_at 链到上一个兄弟（骨架，不做假溯源）；
            # user_id 过滤是隔离硬约束的一部分（task_id 理论上同任务同户，仍显式收口）
            task_id = row["task_id"]
            if task_id:
                prev = (
                    await session.execute(
                        text("""
                            SELECT factor_id FROM rd_agent_factors
                            WHERE metadata_json->>'task_id' = :task_id
                              AND user_id = :user_id
                              AND factor_id <> :factor_id
                              AND (created_at, factor_id) < (:created_at, :factor_id)
                            ORDER BY created_at DESC, factor_id DESC
                            LIMIT 1
                        """),
                        {
                            "task_id": str(task_id),
                            "user_id": user_id,
                            "factor_id": factor_id,
                            "created_at": row["created_at"],
                        },
                    )
                ).scalar()
                if prev:
                    src, dst = _canonical_edge(str(prev), factor_id)
                    await _upsert_edge(
                        session,
                        user_id=user_id,
                        src=src,
                        dst=dst,
                        relation="same_task",
                        method="task_round",
                        weight=1.0,
                        extra=edge_extra,
                    )

            # 公式边：与本 scope 池内已有因子的 token Jaccard
            mine = formula_tokens(str(row["factor_formulation"] or ""))
            if mine:
                others = (
                    await session.execute(
                        text(f"""
                            SELECT f.factor_id, f.factor_formulation
                            FROM rd_agent_factors f
                            JOIN {POOL_TABLE} p ON p.factor_id = f.factor_id
                            WHERE p.user_id = :user_id AND p.market = :market
                              AND p.universe = :universe
                              AND f.factor_id <> :factor_id
                              AND COALESCE(f.factor_formulation, '') <> ''
                        """),
                        {
                            "user_id": user_id,
                            "market": scope_market,
                            "universe": scope_universe,
                            "factor_id": factor_id,
                        },
                    )
                ).all()
                items = [(factor_id, mine)] + [
                    (str(r[0]), formula_tokens(str(r[1]))) for r in others
                ]
                for src, dst, weight in similar_pairs(items, top_k=_FORMULA_TOP_K):
                    await _upsert_edge(
                        session,
                        user_id=user_id,
                        src=src,
                        dst=dst,
                        relation="similar_to",
                        method="formula",
                        weight=weight,
                        extra=edge_extra,
                    )
        return True
    except Exception as exc:  # noqa: BLE001 — 池是增益层，绝不拖挂回测
        logger.warning(
            "[factor-pool] 回测完成钩子失败（不拦回测）%s: %s", factor_id, exc
        )
        return False


# ── 批量刷新 ─────────────────────────────────────────────────────────


async def refresh_pool(
    *,
    user_id: str | None = None,
    market: str = "a_share",
    universe: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """批量重算池状态与谱系边（幂等；dry_run 只统计不动库）。

    边界：面板两两相关按 60 日采样；多样性用 zscore 相关矩阵（缺测按
    成对完整观测），无 ``factor_quality``（精简部署未挂 docker/training）
    时多样性整体跳过并记 warning。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    stats: dict[str, Any] = {
        "market": market,
        "universe": universe,
        "user_id": user_id,
        "factors": 0,
        "panels": 0,
        "pairs": 0,
        "corr_edges": 0,
        "formula_edges": 0,
        "task_edges": 0,
        "scored": 0,
        "icir_missing": 0,
        "diversity": None,
        "dry_run": dry_run,
    }

    async with get_session(read_only=True) as session:
        conds, params = _scope_conds(user_id, market, universe, alias="f")
        conds.append("f.status = 'completed'")
        where = " AND ".join(conds)
        rows = (
            (
                await session.execute(
                    text(f"""
                    SELECT f.factor_id, f.user_id, COALESCE(f.market, :market) AS market,
                           COALESCE(f.universe, '') AS universe, f.factor_name,
                           f.factor_formulation,
                           f.metadata_json->>'task_id' AS task_id,
                           f.metadata_json->>'icir' AS icir,
                           f.created_at
                    FROM rd_agent_factors f
                    WHERE {where}
                    ORDER BY f.created_at ASC, f.factor_id ASC
                """),
                    params,
                )
            )
            .mappings()
            .all()
        )
        pool_conds, pool_params = _scope_conds(user_id, market, universe)
        pool_rows = (
            await session.execute(
                text(f"""
                    SELECT factor_id, times_retrieved FROM {POOL_TABLE} p
                    WHERE {" AND ".join(pool_conds)}
                """),
                pool_params,
            )
        ).all()

    factors = [dict(r) for r in rows if str(r["user_id"] or "").strip()]
    stats["factors"] = len(factors)
    skipped_no_user = len(rows) - len(factors)
    if skipped_no_user:
        logger.warning(
            "[factor-pool] %d 个因子无 user_id，跳过（隔离硬约束）", skipped_no_user
        )
    times_retrieved = {str(r[0]): int(r[1] or 0) for r in pool_rows}

    # 1) 面板装载（只读有面板的；顺带收集 panel_ref 供 upsert）
    frames: dict[str, Any] = {}
    panel_refs: dict[str, str] = {}
    for f in factors:
        fid = str(f["factor_id"])
        market_f = str(f["market"])
        panel = pool_panels.read_panel(market_f, fid)
        if panel is not None and not panel.empty:
            frames[fid] = panel
            panel_refs[fid] = pool_panels.panel_ref(market_f, fid)
    stats["panels"] = len(frames)

    # 2) 两两秩相关（60 日采样）→ novelty + correlated_with 边
    all_days: set[str] = set()
    for panel in frames.values():
        all_days.update(str(d) for d in panel["trade_date"].unique())
    sample = set(pool_panels.sample_days(all_days)) if all_days else set()
    pairs = pool_panels.pairwise_corr(frames, days=sample)
    stats["pairs"] = len(pairs)
    max_corr: dict[str, tuple[float, str]] = {}
    corr_edges: list[tuple[str, str, float]] = []
    for (a, b), rho in pairs.items():
        for fid, other in ((a, b), (b, a)):
            current = max_corr.get(fid)
            if current is None or abs(rho) > abs(current[0]):
                max_corr[fid] = (rho, other)
        if abs(rho) >= CORR_EDGE_THRESHOLD:
            corr_edges.append((a, b, rho))
    stats["corr_edges"] = len(corr_edges)

    # 3) 公式边 + task 链边（整 scope 重算）
    items = [
        (str(f["factor_id"]), formula_tokens(str(f["factor_formulation"] or "")))
        for f in factors
        if str(f["factor_formulation"] or "").strip()
    ]
    formula_edges = similar_pairs(items, top_k=_FORMULA_TOP_K)
    stats["formula_edges"] = len(formula_edges)
    task_edges: list[tuple[str, str, str]] = []  # (src, dst, user_id)
    by_task: dict[str, list[dict]] = {}
    for f in factors:
        task_id = f["task_id"]
        if task_id:
            by_task.setdefault(str(task_id), []).append(f)
    for members in by_task.values():
        ordered = sorted(members, key=lambda r: (r["created_at"], str(r["factor_id"])))
        for prev, cur in zip(ordered, ordered[1:], strict=False):
            src, dst = _canonical_edge(str(prev["factor_id"]), str(cur["factor_id"]))
            task_edges.append((src, dst, str(cur["user_id"])))
    stats["task_edges"] = len(task_edges)

    # 4) 多样性（zscore 相关矩阵 → 熵 + 有效因子数 + 留一贡献）
    diversity: float | None = None
    n_eff: float | None = None
    contrib: dict[str, float] = {}
    quality = None
    if len(frames) >= 2:
        try:
            from backend.shared.factor_quality import load_factor_quality

            quality = load_factor_quality()
        except Exception as exc:  # noqa: BLE001 — 质量模块不可用只降级
            logger.warning("[factor-pool] factor_quality 加载失败: %s", exc)
    matrix = None
    if quality is not None and len(frames) >= 2:
        matrix = pool_panels.corr_matrix(
            {
                fid: panel[panel["trade_date"].isin(sample)]
                for fid, panel in frames.items()
            }
        )
        if not matrix.empty:
            finite = matrix.dropna(how="any", axis=0).dropna(how="any", axis=1)
            dropped = len(matrix) - len(finite)
            if dropped:
                logger.warning("[factor-pool] 多样性矩阵剔除 %d 个缺重叠因子", dropped)
            if len(finite) >= 2:
                import numpy as np

                diversity = quality.diversity_entropy(np.asarray(finite))
                n_eff = quality.effective_factors(np.asarray(finite))
                if diversity is not None:
                    full_eff = n_eff
                    for fid in finite.index:
                        sub = finite.drop(index=fid, columns=fid)
                        if len(sub) < 2:
                            continue
                        sub_eff = quality.effective_factors(np.asarray(sub))
                        if full_eff is not None and sub_eff is not None:
                            contrib[str(fid)] = float(full_eff - sub_eff)
    stats["diversity"] = diversity
    stats["n_eff"] = n_eff

    # 5) pool_score（q_norm × 疲劳 × 冗余 × 新鲜度）
    params_scoring = _scoring_params()
    candidates = [
        PoolCandidate(
            factor_id=str(f["factor_id"]),
            icir=_as_float(f["icir"]),
            times_retrieved=times_retrieved.get(str(f["factor_id"]), 0),
            max_pool_corr=abs(max_corr[str(f["factor_id"])][0])
            if str(f["factor_id"]) in max_corr
            else None,
            age_days=_age_days(f["created_at"]),
        )
        for f in factors
    ]
    scores = {
        s.candidate.factor_id: s.score
        for s in rank_candidates(candidates, params_scoring)
    }
    stats["scored"] = len(scores)
    stats["icir_missing"] = sum(1 for c in candidates if c.icir is None)

    if dry_run:
        return stats

    # 6) 落库：清旧边（本 scope）→ upsert 池行 + 重插边 + 打分
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with get_session() as session:
        # 先清旧边再重插（没有这一步，阈值/池变化后旧边残留成幽灵连接）；
        # universe=None 时清本 market 全部 universe 的边——全量重算必须能覆盖
        # 之前按单 universe 写下的边，否则同一条边在两种 scope 间来回漂。
        del_conds = [
            "method IN ('formula', 'value', 'task_round')",
            "extra->>'market' = :market",
        ]
        del_params: dict[str, Any] = {"market": market}
        if universe is not None:
            del_conds.append("extra->>'universe' = :universe")
            del_params["universe"] = universe
        if user_id is not None:
            del_conds.append("user_id = :user_id")
            del_params["user_id"] = str(user_id)
        await session.execute(
            text(f"DELETE FROM {EDGES_TABLE} WHERE {' AND '.join(del_conds)}"),
            del_params,
        )
        for f in factors:
            fid = str(f["factor_id"])
            await _upsert_pool_row(
                session,
                factor_id=fid,
                user_id=str(f["user_id"]),
                market=str(f["market"]),
                universe=str(f["universe"]),
                panel_ref=panel_refs.get(fid),
                extra={
                    "pool_diversity": diversity,
                    "n_eff": n_eff,
                    "refreshed_at": now_iso,
                },
            )
            rho_other = max_corr.get(fid)
            await session.execute(
                text(f"""
                    UPDATE {POOL_TABLE} SET
                        pool_score = :pool_score,
                        novelty = :novelty,
                        max_pool_corr = :max_pool_corr,
                        max_pool_corr_with = :max_pool_corr_with,
                        diversity_contrib = :diversity_contrib,
                        updated_at = NOW()
                    WHERE factor_id = :factor_id
                """),
                {
                    "factor_id": fid,
                    "pool_score": scores.get(fid),
                    "novelty": (1.0 - min(1.0, abs(rho_other[0])))
                    if rho_other
                    else None,
                    "max_pool_corr": rho_other[0] if rho_other else None,
                    "max_pool_corr_with": rho_other[1] if rho_other else None,
                    "diversity_contrib": contrib.get(fid),
                },
            )
        user_of = {str(f["factor_id"]): str(f["user_id"]) for f in factors}
        universe_of = {str(f["factor_id"]): str(f["universe"]) for f in factors}

        def _pair_extra(a: str, b: str) -> dict[str, Any]:
            # 边 scope 取两端因子共同的 universe；跨 universe 的边记 ''（只在
            # universe=None 的全量刷新里出现，单 universe 刷新看不到它们）
            ua, ub = universe_of.get(a, ""), universe_of.get(b, "")
            return {"market": market, "universe": ua if ua == ub else ""}

        for a, b, rho in corr_edges:
            await _upsert_edge(
                session,
                user_id=user_of.get(a, user_of.get(b, "")),
                src=a,
                dst=b,
                relation="correlated_with",
                method="value",
                weight=rho,
                extra=_pair_extra(a, b),
            )
        for src, dst, weight in formula_edges:
            await _upsert_edge(
                session,
                user_id=user_of.get(src, user_of.get(dst, "")),
                src=src,
                dst=dst,
                relation="similar_to",
                method="formula",
                weight=weight,
                extra=_pair_extra(src, dst),
            )
        for src, dst, owner in task_edges:
            await _upsert_edge(
                session,
                user_id=owner,
                src=src,
                dst=dst,
                relation="same_task",
                method="task_round",
                weight=1.0,
                extra=_pair_extra(src, dst),
            )
    logger.info(
        "[factor-pool] refresh 完成 market=%s universe=%s: %d 因子 / %d 面板 / %d 对 / "
        "%d 相关边 / %d 公式边 / %d task 边 / 多样性 %s",
        market,
        universe,
        stats["factors"],
        stats["panels"],
        stats["pairs"],
        stats["corr_edges"],
        stats["formula_edges"],
        stats["task_edges"],
        stats["diversity"],
    )
    return stats


# ── 检索注入（提示词单通道）──────────────────────────────────────────


@dataclass(frozen=True)
class PoolInjection:
    """一次注入准备的结果：文件路径（无注入为 None）+ 真进 prompt 的因子 id。"""

    path: Path | None
    factor_ids: tuple[str, ...] = ()


def _pool_sota(rows) -> PoolSota:
    """池整体水平线：条数 + 各口径最大值（|IC| 取绝对——方向可翻转；
    icir/pfs 取原始值）。缺失(None)不进 max；全缺 → None → 渲染成「—」。"""
    ics: list[float] = []
    icirs: list[float] = []
    pfss: list[float] = []
    for r in rows:
        if (v := _as_float(r["ic_value"])) is not None:
            ics.append(abs(v))
        if (v := _as_float(r["icir"])) is not None:
            icirs.append(v)
        if (v := _as_float(r["pfs"])) is not None:
            pfss.append(v)
    return PoolSota(
        count=len(rows),
        best_ic=max(ics) if ics else None,
        best_icir=max(icirs) if icirs else None,
        best_pfs=max(pfss) if pfss else None,
    )


async def build_injection_digest(
    *,
    user_id: str,
    market: str,
    universe: str,
    k: int | None = None,
    exclude_task_id: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """池 → 注入摘要（markdown）+ 真注入的因子 id 列表。

    排除本任务自己的因子（刚挖出来的还没回测，注回去是噪声）；
    排序 = ``pool_scoring``（ICIR 分位 × 疲劳 × 冗余 × 新鲜度）。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    k = inject_k() if k is None else k
    if k <= 0:
        return "", ()
    conds = [
        "p.user_id = :user_id",
        "p.market = :market",
        "p.universe = :universe",
        "f.status = 'completed'",
        # 归档因子退出注入与 SOTA 标杆（用户主动判定「不值得再参考」）
        "p.archived_at IS NULL",
    ]
    params: dict[str, Any] = {
        "user_id": str(user_id),
        "market": market,
        "universe": universe,
    }
    if exclude_task_id:
        # 本任务自己的因子还没回测/刚回测，注回去是噪声；NULL task_id 的
        # 存量因子不该被这条排除（IS DISTINCT FROM 会把 NULL 也排除，故
        # 只在给定 task_id 时才加，且显式放行 NULL）
        conds.append(
            "(f.metadata_json->>'task_id' IS NULL"
            " OR f.metadata_json->>'task_id' <> :task_id)"
        )
        params["task_id"] = str(exclude_task_id)
    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(f"""
                    SELECT p.factor_id, f.factor_name, f.factor_formulation,
                           f.ic_value,
                           f.metadata_json->>'icir' AS icir,
                           f.metadata_json->'quality'->>'pfs' AS pfs,
                           p.times_retrieved, p.max_pool_corr, f.created_at
                    FROM {POOL_TABLE} p
                    JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                    WHERE {" AND ".join(conds)}
                """),
                    params,
                )
            )
            .mappings()
            .all()
        )

    params_scoring = _scoring_params()
    candidates = [
        PoolCandidate(
            factor_id=str(r["factor_id"]),
            icir=_as_float(r["icir"]),
            times_retrieved=int(r["times_retrieved"] or 0),
            max_pool_corr=(
                abs(corr)
                if (corr := _as_float(r["max_pool_corr"])) is not None
                else None
            ),
            age_days=_age_days(r["created_at"]),
        )
        for r in rows
    ]
    picked = select_topk(candidates, k, params=params_scoring)
    if not picked:
        return "", ()
    by_id = {str(r["factor_id"]): r for r in rows}
    entries = [
        DigestEntry(
            factor_name=str(
                by_id[s.candidate.factor_id]["factor_name"] or s.candidate.factor_id
            ),
            formula=str(by_id[s.candidate.factor_id]["factor_formulation"] or ""),
            ic=_as_float(by_id[s.candidate.factor_id]["ic_value"]),
            icir=_as_float(by_id[s.candidate.factor_id]["icir"]),
            pfs=_as_float(by_id[s.candidate.factor_id]["pfs"]),
            # 已取绝对值入 candidate（打分口径），展示同一口径，不另算
            max_pool_corr=s.candidate.max_pool_corr,
        )
        for s in picked
    ]
    sota = _pool_sota(rows)
    included: list[DigestEntry] = []
    text_out = render_digest(entries, include=included, sota=sota)
    fid_by_entry = {
        id(e): s.candidate.factor_id for e, s in zip(entries, picked, strict=True)
    }
    injected = tuple(fid_by_entry[id(e)] for e in included)
    return text_out, injected


def _seed_fmt(value: float | None) -> str:
    """种子摘要的指标格式化（口径与 ``pool_scoring._fmt`` 一致：缺失=「—」）。"""
    return MISSING_TEXT if value is None else f"{float(value):.4f}"


async def build_seed_digest(
    seed_factor_ids: Iterable[str],
    *,
    user_id: str,
    market: str,
    universe: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """种子（父本）id → markdown 清单 + used ids + dropped ids（T-MV-01）。

    与 :func:`build_injection_digest` 同一 scope（本用户 × 本市场 × 本池、
    completed、未归档），差别有二：

    * **不做池评分排序**——父本由用户点名，used 保序 = 请求顺序（提示词
      清单与卡片血统 ``seed_factor_id`` 要能逐条对照）；
    * scope 外的 id **不硬失败**，进 ``dropped`` 如实上报（前端选取与提交
      之间存在归档/换池竞态；静默丢弃才是缺陷）。
    """
    ids: list[str] = []
    for raw in seed_factor_ids or ():
        fid = str(raw or "").strip()
        if fid and fid not in ids:
            ids.append(fid)
    if not ids:
        return "", (), ()

    from sqlalchemy import bindparam, text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(f"""
                    SELECT p.factor_id, f.factor_name, f.factor_formulation,
                           f.ic_value,
                           f.metadata_json->>'icir' AS icir,
                           f.metadata_json->'quality'->>'pfs' AS pfs,
                           p.max_pool_corr
                    FROM {POOL_TABLE} p
                    JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                    WHERE p.user_id = :user_id
                      AND p.market = :market
                      AND p.universe = :universe
                      AND f.status = 'completed'
                      AND p.archived_at IS NULL
                      AND p.factor_id IN :ids
                    """).bindparams(bindparam("ids", expanding=True)),
                    {
                        "user_id": str(user_id),
                        "market": market,
                        "universe": universe,
                        "ids": ids,
                    },
                )
            )
            .mappings()
            .all()
        )
    by_id = {str(r["factor_id"]): r for r in rows}
    used = tuple(fid for fid in ids if fid in by_id)
    dropped = tuple(fid for fid in ids if fid not in by_id)

    lines: list[str] = []
    for i, fid in enumerate(used, start=1):
        r = by_id[fid]
        corr = _as_float(r["max_pool_corr"])
        lines.append(
            f"{i}. `{r['factor_name'] or fid}`（因子 ID：{fid}） | "
            f"IC={_seed_fmt(_as_float(r['ic_value']))}"
            f" ICIR={_seed_fmt(_as_float(r['icir']))}"
            f" PFS={_seed_fmt(_as_float(r['pfs']))}"
            f" 池内max|ρ|={_seed_fmt(None if corr is None else abs(corr))}"
        )
        formula = str(r["factor_formulation"] or "").strip().replace("\n", " ")
        lines.append(
            f"   公式: {formula or MISSING_TEXT}（父本仅作变异起点，勿原样复述）"
        )
    return "\n".join(lines), used, dropped


async def prepare_injection(
    *,
    user_id: str,
    market: str,
    universe: str,
    task_id: str | None,
    log_dir: Path | str,
) -> PoolInjection:
    """挖矿 spawn 前：生成摘要文件（caller 把路径传 env ``QMF_POOL_CONTEXT_PATH``）。

    任何失败都返回空注入——注入是增益层，绝不允许拦住一次挖矿。
    """
    if not injection_enabled():
        return PoolInjection(None)
    try:
        text_out, ids = await build_injection_digest(
            user_id=user_id,
            market=market,
            universe=universe,
            exclude_task_id=task_id,
        )
        if not text_out or not ids:
            return PoolInjection(None)
        path = Path(log_dir) / _INJECT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text_out + "\n", encoding="utf-8")
        logger.info("[factor-pool] 注入 %d 条历史因子记忆 → %s", len(ids), path)
        return PoolInjection(path=path, factor_ids=ids)
    except Exception as exc:  # noqa: BLE001 — 注入失败不拦挖掘
        logger.warning("[factor-pool] 注入准备失败（不拦挖掘）: %s", exc)
        return PoolInjection(None)


async def mark_retrieved(factor_ids: tuple[str, ...] | list[str]) -> int:
    """疲劳计数 +1（spawn 成功后调用；失败只告警——计数少一次无伤大雅）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    ids = [str(x) for x in factor_ids if x]
    if not ids:
        return 0
    try:
        async with get_session() as session:
            result = await session.execute(
                text(f"""
                    UPDATE {POOL_TABLE}
                    SET times_retrieved = times_retrieved + 1,
                        last_retrieved_at = NOW()
                    WHERE factor_id = ANY(:ids)
                """),
                {"ids": ids},
            )
            return int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[factor-pool] 疲劳计数失败: %s", exc)
        return 0


# ── 读接口（池页数据源）──────────────────────────────────────────────


async def ic_pool_percentile(
    *, user_id: str, market: str, universe: str, factor_id: str
) -> float | None:
    """因子 IC 在同池（同 user/market/universe、已完成、IC 非空）内的分位。

    分位 = 严格小于者数 / (N−1)，值域 [0,1]；并列取同分位。池内有效 IC
    少于 2 个、本因子不在池中或其 IC 缺失 → None（物化门禁判 skipped，
    **绝不按 0 判**——缺口径不是算出来很差）。异常同样降级为 None，由
    调用方告警，不让分位查询拖挂物化。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    conds, params = _scope_conds(user_id, market, universe)
    where = " AND ".join(conds)
    params["factor_id"] = str(factor_id)
    sql = f"""
        WITH scope AS (
            SELECT p.factor_id, f.ic_value
              FROM {POOL_TABLE} p
              JOIN rd_agent_factors f ON f.factor_id = p.factor_id
             WHERE {where}
               AND f.status = 'completed'
               AND f.ic_value IS NOT NULL
        )
        SELECT (
            (SELECT COUNT(*) FROM scope WHERE ic_value < me.ic_value)::float
            / NULLIF((SELECT COUNT(*) FROM scope) - 1, 0)
        ) AS pct
          FROM scope me
         WHERE me.factor_id = :factor_id
    """
    async with get_session(read_only=True) as session:
        row = (await session.execute(text(sql), params)).first()
    if row is None or row[0] is None:
        return None
    return max(0.0, min(1.0, float(row[0])))


def _category_breakdown(rows, *, total: int) -> list[dict[str, Any]]:
    """按因子大类聚合（归类单源 = ``factor_classify``）。

    ``rows`` 是 scope 内活跃池行（factor_name/description/ic_value/icir/
    pool_score/novelty）。聚合值是**有值样本的均值**（n_ic/n_icir 回传覆盖率，
    缺失不按 0 计——与全站「缺失显 —」纪律一致）。``other`` 永远垫底。
    """
    from backend.services.engine.mining_plugins.factor_classify import classify_factor

    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        cls = classify_factor(
            factor_name=str(r.get("factor_name") or ""),
            description=r.get("description"),
        )
        b = buckets.setdefault(
            cls.category_id,
            {
                "category": cls.category_id,
                "label": cls.category_label,
                "count": 0,
                "ic": [],
                "icir": [],
                "pool_score": [],
                "novelty": [],
                "names": [],
            },
        )
        b["count"] += 1
        if r.get("ic_value") is not None:
            b["ic"].append(float(r["ic_value"]))
        if r.get("icir") is not None:
            b["icir"].append(float(r["icir"]))
        if r.get("pool_score") is not None:
            b["pool_score"].append(float(r["pool_score"]))
        if r.get("novelty") is not None:
            b["novelty"].append(float(r["novelty"]))
        if r.get("factor_name"):
            b["names"].append((r.get("pool_score"), str(r["factor_name"])))

    def _avg(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    out: list[dict[str, Any]] = []
    for b in buckets.values():
        n = int(b["count"])
        ranked = sorted(
            b["names"],
            key=lambda t: (
                -(t[0] if t[0] is not None else float("-inf")),
                t[1],
            ),
        )
        out.append(
            {
                "category": b["category"],
                "label": b["label"],
                "count": n,
                "share": (n / total) if total else 0.0,
                "avg_ic": _avg(b["ic"]),
                "n_ic": len(b["ic"]),
                "avg_icir": _avg(b["icir"]),
                "n_icir": len(b["icir"]),
                "avg_pool_score": _avg(b["pool_score"]),
                "avg_novelty": _avg(b["novelty"]),
                "top_factors": [name for _, name in ranked[:3]],
            }
        )
    out.sort(key=lambda d: (d["category"] == "other", -d["count"], d["category"]))
    return out


async def pool_overview(
    *, user_id: str, market: str, universe: str | None = None
) -> dict[str, Any]:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    conds, params = _scope_conds(user_id, market, universe)
    where = " AND ".join([*conds, "p.archived_at IS NULL"])
    where_archived = " AND ".join([*conds, "p.archived_at IS NOT NULL"])
    numeric = "^-?[0-9]+(\\.[0-9]+)?$"
    async with get_session(read_only=True) as session:
        archived_count = (
            await session.execute(
                text(
                    f"SELECT COUNT(*)::int FROM {POOL_TABLE} p WHERE {where_archived}"
                ),
                params,
            )
        ).scalar()
        agg = (
            (
                await session.execute(
                    text(f"""
                    SELECT COUNT(*)::int AS total,
                           COUNT(p.panel_ref)::int AS with_panel,
                           COALESCE(SUM(p.times_retrieved), 0)::int AS retrieved_total,
                           (COUNT(*) FILTER (WHERE p.times_retrieved > 0))::int
                               AS retrieved_factors,
                           AVG(p.pool_score) AS avg_pool_score,
                           AVG(p.novelty) AS avg_novelty,
                           AVG(p.max_pool_corr) AS avg_max_corr,
                           AVG(f.ic_value) AS avg_ic,
                           AVG(CASE WHEN f.metadata_json->>'icir' ~ :numeric
                                    THEN (f.metadata_json->>'icir')::float8 END) AS avg_icir,
                           AVG(CASE WHEN f.metadata_json->'quality'->>'pfs' ~ :numeric
                                    THEN (f.metadata_json->'quality'->>'pfs')::float8 END) AS avg_pfs
                    FROM {POOL_TABLE} p
                    LEFT JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                    WHERE {where}
                """),
                    {**params, "numeric": numeric},
                )
            )
            .mappings()
            .first()
        )
        div = (
            (
                await session.execute(
                    text(f"""
                    SELECT extra->>'pool_diversity' AS d, extra->>'n_eff' AS n_eff
                    FROM {POOL_TABLE} p
                    WHERE {where} AND extra->>'pool_diversity' IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 1
                """),
                    params,
                )
            )
            .mappings()
            .first()
        )
        cat_rows = (
            (
                await session.execute(
                    text(f"""
                    SELECT f.factor_name,
                           f.metadata_json->>'description' AS description,
                           f.ic_value, p.pool_score, p.novelty,
                           CASE WHEN f.metadata_json->>'icir' ~ :numeric
                                THEN (f.metadata_json->>'icir')::float8 END AS icir
                    FROM {POOL_TABLE} p
                    LEFT JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                    WHERE {where}
                """),
                    {**params, "numeric": numeric},
                )
            )
            .mappings()
            .all()
        )
    out = dict(agg or {})
    out["pool_diversity"] = None
    out["n_eff"] = None
    # 归档计数照实回传（默认聚合只算活跃因子；UI 用这个数字给「已归档 N」）
    out["archived_count"] = int(archived_count or 0)
    if div and div["d"] not in (None, ""):
        try:
            out["pool_diversity"] = float(div["d"])
        except (TypeError, ValueError):
            pass
    if div and div["n_eff"] not in (None, ""):
        try:
            out["n_eff"] = float(div["n_eff"])
        except (TypeError, ValueError):
            pass
    # 因子分类分布（池总览「因子分类」区块；归类单源 = factor_classify）
    out["category_breakdown"] = _category_breakdown(
        cat_rows, total=int(out.get("total") or 0)
    )
    return out


_SORT_COLUMNS = {
    "pool_score": "p.pool_score",
    "novelty": "p.novelty",
    "ic": "f.ic_value",
    "times_retrieved": "p.times_retrieved",
    "created_at": "f.created_at",
    "updated_at": "p.updated_at",
}


async def list_pool_factors(
    *,
    user_id: str,
    market: str,
    universe: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "pool_score",
    include_archived: bool = False,
    category: str | None = None,
) -> dict[str, Any]:
    from sqlalchemy import bindparam, text

    from backend.shared.database_manager_v2 import get_session

    conds, params = _scope_conds(user_id, market, universe)
    if not include_archived:
        # 归档因子默认退出列表（UI 显式勾选「含已归档」才可见）
        conds.append("p.archived_at IS NULL")
    order = _SORT_COLUMNS.get(sort, _SORT_COLUMNS["pool_score"])
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    numeric = "^-?[0-9]+(\\.[0-9]+)?$"

    from backend.services.engine.mining_plugins.factor_classify import classify_factor

    async with get_session(read_only=True) as session:
        if category:
            # 分类是 Python 侧归类（factor_classify 单源）：先取 scope 内
            # (id, name, description) 归出该类因子 id 集，再让主查询按 id 过滤
            # ——分页语义（total/limit/offset）保持不变。
            scope_rows = (
                (
                    await session.execute(
                        text(f"""
                        SELECT p.factor_id, f.factor_name,
                               f.metadata_json->>'description' AS description
                        FROM {POOL_TABLE} p
                        LEFT JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                        WHERE {" AND ".join(conds)}
                    """),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            cat_ids = [
                str(r["factor_id"])
                for r in scope_rows
                if classify_factor(
                    factor_name=str(r.get("factor_name") or ""),
                    description=r.get("description"),
                ).category_id
                == category
            ]
            if not cat_ids:
                return {
                    "total": 0,
                    "items": [],
                    "limit": limit,
                    "offset": offset,
                    "category": category,
                }
            conds.append("p.factor_id IN :cat_ids")
            params = {**params, "cat_ids": cat_ids}
        where = " AND ".join(conds)
        use_expanding = bool(category)

        def _stmt(sql: str):
            stmt = text(sql)
            if use_expanding:
                stmt = stmt.bindparams(bindparam("cat_ids", expanding=True))
            return stmt

        total = (
            await session.execute(
                _stmt(f"SELECT COUNT(*)::int FROM {POOL_TABLE} p WHERE {where}"),
                params,
            )
        ).scalar()
        rows = (
            (
                await session.execute(
                    _stmt(f"""
                    SELECT p.factor_id, f.factor_name, f.factor_formulation,
                           f.metadata_json->>'description' AS description,
                           f.ic_value, f.rank_ic,
                           CASE WHEN f.metadata_json->>'icir' ~ :numeric
                                THEN (f.metadata_json->>'icir')::float8 END AS icir,
                           CASE WHEN f.metadata_json->'quality'->>'pfs' ~ :numeric
                                THEN (f.metadata_json->'quality'->>'pfs')::float8 END AS pfs,
                           p.pool_score, p.novelty, p.max_pool_corr,
                           p.max_pool_corr_with, p.diversity_contrib,
                           p.times_retrieved, p.last_retrieved_at, p.panel_ref,
                           p.archived_at,
                           f.created_at, p.updated_at,
                           f.metadata_json->'materialization'->'gates' AS gates
                    FROM {POOL_TABLE} p
                    LEFT JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                    WHERE {where}
                    ORDER BY {order} DESC NULLS LAST, p.factor_id ASC
                    LIMIT :limit OFFSET :offset
                """),
                    {**params, "numeric": numeric, "limit": limit, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
    items = []
    for r in rows:
        item = dict(r)
        item["has_panel"] = bool(item.pop("panel_ref", None))
        cls = classify_factor(
            factor_name=str(item.get("factor_name") or ""),
            description=item.get("description"),
        )
        item["category"] = cls.category_id
        item["category_label"] = cls.category_label
        item["raw_category_label"] = cls.raw_label
        items.append(item)
    return {
        "total": int(total or 0),
        "items": items,
        "limit": limit,
        "offset": offset,
        "category": category,
    }


async def pool_graph(
    *,
    user_id: str,
    market: str,
    universe: str | None = None,
    max_nodes: int = 200,
    include_archived: bool = False,
) -> dict[str, Any]:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    conds, params = _scope_conds(user_id, market, universe)
    if not include_archived:
        conds.append("p.archived_at IS NULL")
    where = " AND ".join(conds)
    max_nodes = max(2, min(int(max_nodes), 500))
    numeric = "^-?[0-9]+(\\.[0-9]+)?$"
    async with get_session(read_only=True) as session:
        nodes = (
            (
                await session.execute(
                    text(f"""
                    SELECT p.factor_id, f.factor_name,
                           p.pool_score, p.novelty, p.times_retrieved,
                           (p.panel_ref IS NOT NULL) AS has_panel,
                           f.metadata_json->>'task_id' AS task_id,
                           CASE WHEN f.metadata_json->>'icir' ~ :numeric
                                THEN (f.metadata_json->>'icir')::float8 END AS icir
                    FROM {POOL_TABLE} p
                    LEFT JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                    WHERE {where}
                    ORDER BY p.pool_score DESC NULLS LAST, p.factor_id ASC
                    LIMIT :max_nodes
                """),
                    {**params, "numeric": numeric, "max_nodes": max_nodes},
                )
            )
            .mappings()
            .all()
        )
        node_ids = [str(n["factor_id"]) for n in nodes]
        edges: list[dict[str, Any]] = []
        if node_ids:
            edge_conds = [
                "user_id = :user_id",
                "extra->>'market' = :market",
                "src_factor_id = ANY(:ids)",
                "dst_factor_id = ANY(:ids)",
            ]
            edge_params: dict[str, Any] = {
                "user_id": str(user_id),
                "market": market,
                "ids": node_ids,
            }
            if universe is not None:
                edge_conds.append("extra->>'universe' = :universe")
                edge_params["universe"] = universe
            rows = (
                (
                    await session.execute(
                        text(f"""
                        SELECT src_factor_id, dst_factor_id, relation, method, weight
                        FROM {EDGES_TABLE}
                        WHERE {" AND ".join(edge_conds)}
                    """),
                        edge_params,
                    )
                )
                .mappings()
                .all()
            )
            edges = [dict(r) for r in rows]
    return {"nodes": [dict(n) for n in nodes], "edges": edges, "max_nodes": max_nodes}


# ── 非 SOTA 清理建议与归档（P3）─────────────────────────────────────────
#
# 纪律：**只建议不自动删**。判据（pool_cleanup）算好给用户看，归档与否由
# 用户决定；归档只置 ``archived_at`` 时间戳——池行/边/面板全保留，默认
# 退出注入摘要、池列表、谱系图与总览聚合，随时可恢复（unarchive）。
# 归档不参与 refresh_pool 的重算过滤：数据层仍在，隐藏是**视图决策**。


async def cleanup_suggestions(
    *,
    user_id: str,
    market: str,
    universe: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """全池清理建议（判据见 ``pool_cleanup.evaluate_cleanup``）。

    在**全 scope** 上评估（分位/支配关系必须看全池），输出截断到 ``limit``；
    ``summary`` 计数覆盖全部建议（不是截断后的），``criteria`` 回传实际
    阈值——UI 要展示「判据是什么」。archived 因子不参与评估。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    conds, params = _scope_conds(user_id, market, universe)
    scope_conds = [*conds, "f.status = 'completed'"]
    active_where = " AND ".join([*scope_conds, "p.archived_at IS NULL"])
    archived_where = " AND ".join([*conds, "p.archived_at IS NOT NULL"])
    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(f"""
                    SELECT p.factor_id, f.factor_name, f.factor_formulation,
                           f.ic_value,
                           f.metadata_json->>'icir' AS icir,
                           f.metadata_json->'quality'->>'pfs' AS pfs,
                           p.pool_score, p.max_pool_corr, p.max_pool_corr_with,
                           p.diversity_contrib, p.times_retrieved
                    FROM {POOL_TABLE} p
                    JOIN rd_agent_factors f ON f.factor_id = p.factor_id
                    WHERE {active_where}
                """),
                    params,
                )
            )
            .mappings()
            .all()
        )
        archived_count = (
            await session.execute(
                text(
                    f"SELECT COUNT(*)::int FROM {POOL_TABLE} p WHERE {archived_where}"
                ),
                params,
            )
        ).scalar()

    criteria = _cleanup_criteria()
    candidates = [
        CleanupCandidate(
            factor_id=str(r["factor_id"]),
            factor_name=str(r["factor_name"] or r["factor_id"]),
            icir=_as_float(r["icir"]),
            pool_score=_as_float(r["pool_score"]),
            max_pool_corr=(
                abs(corr)
                if (corr := _as_float(r["max_pool_corr"])) is not None
                else None
            ),
            max_pool_corr_with=(
                str(r["max_pool_corr_with"]) if r["max_pool_corr_with"] else None
            ),
            diversity_contrib=_as_float(r["diversity_contrib"]),
        )
        for r in rows
    ]
    report = evaluate_cleanup(candidates, criteria)
    by_row = {str(r["factor_id"]): r for r in rows}

    summary: dict[str, int] = {}
    for suggestion in report.suggestions:
        for reason in suggestion.reasons:
            summary[reason.code] = summary.get(reason.code, 0) + 1

    items: list[dict[str, Any]] = []
    for suggestion in report.suggestions[: max(1, int(limit))]:
        row = by_row[suggestion.candidate.factor_id]
        items.append(
            {
                "factor_id": suggestion.candidate.factor_id,
                "factor_name": suggestion.candidate.factor_name,
                "factor_formulation": str(row["factor_formulation"] or ""),
                "icir": suggestion.candidate.icir,
                "pool_score": suggestion.candidate.pool_score,
                "max_pool_corr": suggestion.candidate.max_pool_corr,
                "max_pool_corr_with": suggestion.candidate.max_pool_corr_with,
                "diversity_contrib": suggestion.candidate.diversity_contrib,
                "times_retrieved": int(row["times_retrieved"] or 0),
                "severity": suggestion.severity,
                "reasons": [
                    {"code": r.code, "label": r.label, "detail": r.detail}
                    for r in suggestion.reasons
                ],
            }
        )

    sota = _pool_sota(rows)
    return {
        "items": items,
        "total": len(report.suggestions),
        "pool_size": len(rows),
        "archived_count": int(archived_count or 0),
        "summary": summary,
        "criteria": {
            "corr_dup": criteria.corr_dup,
            "weak_icir_quantile": criteria.weak_icir_quantile,
            "min_icir_sample": criteria.min_icir_sample,
            "weak_icir_threshold": report.weak_icir_threshold,
            "icir_sample_size": report.icir_sample_size,
        },
        "sota": {
            "count": sota.count,
            "best_ic": sota.best_ic,
            "best_icir": sota.best_icir,
            "best_pfs": sota.best_pfs,
        },
    }


def _norm_ids(factor_ids) -> list[str]:
    """去重 + 剔空 + 排序（确定性；上限由 router 层把关）。"""
    return sorted({str(x) for x in factor_ids if str(x or "").strip()})


async def archive_factors(*, user_id: str, factor_ids) -> dict[str, Any]:
    """批量归档（置 ``archived_at``）。**只认本人的行**——跨用户 id 只进 skipped。

    归档不是删除，也不检查因子是否「够差」：这是用户看过判据后的决定。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    ids = _norm_ids(factor_ids)
    if not ids:
        return {"archived": 0, "archived_ids": [], "skipped": []}
    async with get_session() as session:
        rows = (
            await session.execute(
                text(f"""
                    UPDATE {POOL_TABLE}
                       SET archived_at = NOW(), updated_at = NOW()
                     WHERE factor_id = ANY(:ids)
                       AND user_id = :user_id
                       AND archived_at IS NULL
                 RETURNING factor_id
                """),
                {"ids": ids, "user_id": str(user_id)},
            )
        ).all()
    done = {str(r[0]) for r in rows}
    return {
        "archived": len(done),
        "archived_ids": sorted(done),
        "skipped": sorted(set(ids) - done),
    }


async def unarchive_factors(*, user_id: str, factor_ids) -> dict[str, Any]:
    """恢复归档（清 ``archived_at``）；不在池/非本人/未归档的 id 进 skipped。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    ids = _norm_ids(factor_ids)
    if not ids:
        return {"restored": 0, "restored_ids": [], "skipped": []}
    async with get_session() as session:
        rows = (
            await session.execute(
                text(f"""
                    UPDATE {POOL_TABLE}
                       SET archived_at = NULL, updated_at = NOW()
                     WHERE factor_id = ANY(:ids)
                       AND user_id = :user_id
                       AND archived_at IS NOT NULL
                 RETURNING factor_id
                """),
                {"ids": ids, "user_id": str(user_id)},
            )
        ).all()
    done = {str(r[0]) for r in rows}
    return {
        "restored": len(done),
        "restored_ids": sorted(done),
        "skipped": sorted(set(ids) - done),
    }


__all__ = [
    "CORR_EDGE_THRESHOLD",
    "DEFAULT_INJECT_K",
    "PoolInjection",
    "archive_factors",
    "build_injection_digest",
    "build_seed_digest",
    "cleanup_suggestions",
    "inject_k",
    "injection_enabled",
    "list_pool_factors",
    "mark_retrieved",
    "pool_graph",
    "pool_overview",
    "prepare_injection",
    "record_backtested_factor",
    "refresh_pool",
    "unarchive_factors",
]
