"""P2 晋升流程服务层（设计 §3.1 状态机 / §5.3 观察期 / §5.4 晋升回滚审计）。

职责边界：

- **证据组装**：``admission``（三件套，§4.5）来自挑战者模型记录与产物目录；
  ``delta_summary / monthly / turnover / pred md5`` 来自 ``walkforward_rollup``
  的 vintage 回放（与 CLI 同一条 IO 路径，不是第二份实现）；``observation``
  来自 ``qm_model_inference_quality`` 前向 IC；``trials`` 数同 recipe campaign。
  ``regime`` 键留空（P3 前 G4=未评估）。
- **阶段推进**（每跳走台账条件迁移，并发安全）：
  - ``create`` → replay_eval；
  - ``start_observation``：**硬闸门 G0/G1 必须 pass**（三件套缺不许进流程 §4.5；
    复制品不许进观察期），随后写 ``qm_model_inference_settings(enabled=TRUE)``
    交给既有日更管线（§5.3 Plan A）→ observing；
  - ``evaluate``：重算 G0-G7 存台账；observing 阶段观察窗满 ``g5_min_days``
    自动 → gate_passed（**flagged 不拦**——观察模式下推不推是人工决定，
    证据卡如实展示；硬拦只有 G0/G1 与阶段守卫）；
  - ``promote``：**单事务**「市场级默认切换 + prior_default 落账 + stage=promoted
    + 审计」（谁/何时/理由）；市场级切换保证 CN/HK/US 冠军互不顶掉（§5.5）；
  - ``reject``：关 settings 行 → rejected；
  - ``rollback``：单事务「默认切回 prior_default + stage=rolled_back」，**理由必填**；
    备任链缺失/备任不可用 → 拒绝执行（诚实拦截，不猜）。
- **不动** ``qm_user_models.status`` 语义（候选/就绪/归档照旧，§5.4 说明）；
  ``model_registry.set_default_model`` 也保持原样（它仍是手动路径；rollout 层
  叠加的是按市场的晋升切换）。

异常语义（router 据此映射状态码）：``RolloutNotFound`` → 404，
``RolloutConflict`` → 409（阶段不符/并发变化），``RolloutInvalid`` → 400。
"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.scripts.eval import walkforward_rollup as walkforward
from backend.scripts.eval.model_card import _user_meta_path
from backend.scripts.eval.paired_stats import DEFAULT_TOP_K
from backend.shared import model_rollout_store as store
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import (
    _canonical_market,
    _model_market_of,
    model_registry_service,
)
from backend.shared.model_rollout import evaluate_rollout
from backend.shared.utc_datetime import utc_now
from backend.services.engine.services.model_inference_persistence import (
    model_inference_persistence,
)

#: 「样本外质量门禁」留痕前缀——注册软闸门未过的标记（model_registry 注册链路写入）
_SOFT_GATE_WARNING_MARK = "样本外质量门禁"

#: 进入观察期后允许复评的最少前向天数口径（G5 默认阈值；可被 thresholds 覆盖）
_READY_STATUSES = frozenset({"ready", "active"})


class RolloutNotFound(LookupError):
    """rollout 不存在。"""


class RolloutConflict(ValueError):
    """阶段不符 / 并发变化 / 活跃重复。"""


class RolloutInvalid(ValueError):
    """参数或前置条件不满足。"""


def _metrics_fingerprint(record: dict[str, Any]) -> str | None:
    """指标 JSON 的逐位指纹（两侧都非空才可判「复制品」）。"""
    metrics = record.get("metrics_json")
    if not isinstance(metrics, dict) or not metrics:
        return None
    return json.dumps(metrics, sort_keys=True, ensure_ascii=False, default=str)


class ModelRolloutService:
    # ── 查询 ────────────────────────────────────────────────────────────

    async def get_rollout(
        self, rollout_id: str, *, owner: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        row = await store.get_rollout(rollout_id)
        if row is None:
            raise RolloutNotFound(f"rollout 不存在: {rollout_id}")
        if owner is not None and (row["tenant_id"], row["user_id"]) != owner:
            # 他人台账按不存在处理（不泄露存在性）
            raise RolloutNotFound(f"rollout 不存在: {rollout_id}")
        return row

    async def get_detail(
        self, rollout_id: str, *, owner: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        """详情 + 两侧模型记录（治理页证据卡用）。"""
        row = await self.get_rollout(rollout_id, owner=owner)
        row["champion"] = await model_registry_service.get_model(
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            model_id=row["champion_model_id"],
        )
        row["challenger"] = await model_registry_service.get_model(
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            model_id=row["challenger_model_id"],
        )
        return row

    async def list_rollouts(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str | None = None,
        stage: str | None = None,
        limit: int = store.DEFAULT_LIST_LIMIT,
    ) -> list[dict[str, Any]]:
        return await store.list_rollouts(
            tenant_id=tenant_id, user_id=user_id, market=market, stage=stage, limit=limit
        )

    # ── 创建 ────────────────────────────────────────────────────────────

    async def create_rollout(
        self,
        *,
        tenant_id: str,
        user_id: str,
        market: str,
        challenger_model_id: str,
        campaign_id: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        market_key = _canonical_market(market)
        # 与 registry 查询同一 owner 口径（tenant 缺省 "default"）——否则台账行
        # 与模型行可能落在两个租户空间，后续 SQL 静默查空
        tenant_id, user_id = model_registry_service._normalize_owner(
            tenant_id=tenant_id, user_id=user_id
        )
        challenger = await model_registry_service.get_model(
            tenant_id=tenant_id, user_id=user_id, model_id=challenger_model_id
        )
        if challenger is None:
            raise RolloutInvalid(f"挑战者模型不存在: {challenger_model_id}")
        chal_status = str(challenger.get("status") or "")
        if chal_status not in _READY_STATUSES:
            raise RolloutInvalid(f"挑战者状态 {chal_status} 不可晋升（需 ready/active）")
        chal_market = _model_market_of(challenger)
        if chal_market != market_key:
            raise RolloutInvalid(f"挑战者市场 {chal_market} ≠ 台账市场 {market_key}")

        champion = await model_registry_service.get_default_model(
            tenant_id=tenant_id, user_id=user_id, market=market_key
        )
        if champion is None:
            raise RolloutInvalid(f"市场 {market_key} 没有默认模型（champion），先设置默认")
        champion_id = str(champion.get("model_id") or "")
        if champion_id == challenger_model_id:
            raise RolloutInvalid("挑战者就是当前默认模型（champion），无需晋升")

        if campaign_id:
            from backend.shared.rolling_campaigns import get_campaign

            campaign = await get_campaign(campaign_id)
            if campaign is None:
                raise RolloutInvalid(f"campaign 不存在: {campaign_id}")

        created = await store.insert_rollout(
            tenant_id=tenant_id,
            user_id=user_id,
            market=market_key,
            champion_model_id=champion_id,
            challenger_model_id=challenger_model_id,
            campaign_id=campaign_id,
            notes=notes,
        )
        if created is None:
            existing = await store.get_active_rollout(
                tenant_id=tenant_id,
                user_id=user_id,
                market=market_key,
                challenger_model_id=challenger_model_id,
            )
            raise RolloutConflict(
                f"该挑战者已有活跃 rollout: {(existing or {}).get('rollout_id')}"
            )
        return created

    # ── 证据组装 ────────────────────────────────────────────────────────

    async def assemble_evidence(
        self, rollout: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """G0-G7 证据（键缺 = 未评估；任何一部分失败只降级该部分并记 warning）。"""
        warnings: list[str] = []
        tenant_id, user_id = rollout["tenant_id"], rollout["user_id"]
        challenger = await model_registry_service.get_model(
            tenant_id=tenant_id, user_id=user_id, model_id=rollout["challenger_model_id"]
        )
        champion = await model_registry_service.get_model(
            tenant_id=tenant_id, user_id=user_id, model_id=rollout["champion_model_id"]
        )
        if challenger is None:
            raise RolloutInvalid(f"挑战者模型不存在: {rollout['challenger_model_id']}")

        evidence: dict[str, Any] = {}
        old_evidence = rollout.get("evidence") or {}
        if isinstance(old_evidence, dict) and old_evidence.get("observation_since"):
            evidence["observation_since"] = old_evidence["observation_since"]

        # G0 准入：注册软闸门 + 可复现三件套（§4.5）
        evidence["admission"] = self._admission_of(challenger)

        # 回放（G1 md5 / G2 delta / G3 monthly / G6 turnover 同源一次取）
        ns = Namespace(
            champion_model_id=rollout["champion_model_id"],
            challenger_model_id=[] if rollout.get("campaign_id") else [rollout["challenger_model_id"]],
            campaign_id=[rollout["campaign_id"]] if rollout.get("campaign_id") else [],
            champion_dir=None,
            challenger_dir=None,
            k=DEFAULT_TOP_K,
            out=None,
        )
        report, code = await walkforward.build_report(ns)
        if code == walkforward.EXIT_OK:
            evidence["delta_summary"] = report["delta_summary"]
            evidence["monthly"] = report["monthly"]
            evidence["turnover"] = report["turnover"]
            warnings.extend(report.get("warnings") or [])
            evidence["independence"] = self._independence_of(
                challenger, champion, report
            )
        else:
            warnings.append(
                f"回放不可用（{report.get('error') or code}）——G1/G2/G3/G6 将按未评估处理"
            )
            evidence["independence"] = self._independence_of(challenger, champion, None)

        # G5 观察期（未开始观察 → 键缺 = 未评估）
        observation = await self._observation_evidence(rollout)
        if observation is not None:
            evidence["observation"] = observation

        # G7 提示：同 recipe 试次计数
        trials = await self._trial_count(rollout.get("campaign_id"))
        if trials is not None:
            evidence["trials"] = {"trial_count": trials}

        return evidence, warnings

    @staticmethod
    def _admission_of(challenger: dict[str, Any]) -> dict[str, Any]:
        metadata = challenger.get("metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = str(challenger.get("status") or "")
        warnings = metadata.get("quality_warnings") or []
        if status in _READY_STATUSES:
            soft: bool | None = True
        elif any(_SOFT_GATE_WARNING_MARK in str(w) for w in warnings):
            soft = False
        else:
            soft = None

        meta_path = _user_meta_path(
            str(challenger.get("model_id") or ""), str(challenger.get("storage_path") or "")
        )
        model_dir = meta_path.parent if meta_path else None
        reproducibility = {
            "seed": metadata.get("seed"),
            "config_yaml": bool(
                model_dir is not None and (model_dir / "config.yaml").is_file()
            ),
            "data_fingerprint": metadata.get("data_fingerprint"),
        }
        return {
            "registration_soft_gate_passed": soft,
            "status": status,
            "reproducibility": reproducibility,
        }

    @staticmethod
    def _independence_of(
        challenger: dict[str, Any],
        champion: dict[str, Any] | None,
        report: dict[str, Any] | None,
    ) -> dict[str, Any]:
        c_fp = _metrics_fingerprint(challenger)
        h_fp = _metrics_fingerprint(champion or {})
        metrics_identical: bool | None = None
        if c_fp is not None and h_fp is not None:
            metrics_identical = c_fp == h_fp

        champion_md5: str | None = None
        challenger_md5: str | None = None
        if report is not None:
            champion_md5 = (report.get("champion") or {}).get("pred_md5")
            segment_md5s = [
                str(seg.get("pred_md5") or "")
                for seg in (report.get("challenger") or {}).get("segments") or []
            ]
            segment_md5s = [m for m in segment_md5s if m]
            if segment_md5s:
                if champion_md5 and champion_md5 in segment_md5s:
                    # 任一段 = 冠军产物 → 强制等值，让 G1 判 fail（多 vintage 拼接
                    # 时单靠 join 字符串比不出「段级复制」）
                    challenger_md5 = str(champion_md5)
                else:
                    challenger_md5 = ",".join(segment_md5s)
        return {
            "metrics_identical": metrics_identical,
            "pred_md5_challenger": challenger_md5,
            "pred_md5_champion": champion_md5,
        }

    async def _observation_evidence(
        self, rollout: dict[str, Any]
    ) -> dict[str, Any] | None:
        """观察期前向证据：窗口 = [observation_since, 今]（未开始观察 → None）。"""
        evidence = rollout.get("evidence") or {}
        since = evidence.get("observation_since") if isinstance(evidence, dict) else None
        if not since:
            return None

        async def _side(model_id: str) -> dict[str, Any]:
            async with get_session(read_only=True) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT
                                    COUNT(*) FILTER (WHERE rank_ic IS NOT NULL) AS n_days,
                                    AVG(rank_ic)  FILTER (WHERE rank_ic IS NOT NULL) AS mean_ic,
                                    AVG(coverage) FILTER (WHERE coverage IS NOT NULL) AS mean_cov
                                FROM qm_model_inference_quality
                                WHERE model_id = :model_id
                                  AND trade_date >= CAST(:since AS DATE)
                                """
                            ),
                            {"model_id": model_id, "since": str(since)[:10]},
                        )
                    )
                    .mappings()
                    .first()
                )
            return dict(row or {})

        challenger = await _side(rollout["challenger_model_id"])
        champion = await _side(rollout["champion_model_id"])
        n_days = int(challenger.get("n_days") or 0)
        return {
            "since": str(since)[:10],
            "sufficient": True,
            "n_days": n_days,
            "challenger_mean_ic": _round_or_none(challenger.get("mean_ic")),
            "champion_mean_ic": _round_or_none(champion.get("mean_ic")),
            "challenger_coverage": _round_or_none(challenger.get("mean_cov")),
            "champion_coverage": _round_or_none(champion.get("mean_cov")),
        }

    @staticmethod
    async def _trial_count(campaign_id: str | None) -> int | None:
        if not campaign_id:
            return None
        from backend.shared.rolling_campaigns import get_campaign

        campaign = await get_campaign(campaign_id)
        if campaign is None or not campaign.get("recipe_id"):
            return None
        async with get_session(read_only=True) as session:
            count = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM qm_rolling_campaigns WHERE recipe_id = :recipe_id"
                    ),
                    {"recipe_id": campaign["recipe_id"]},
                )
            ).scalar()
        return int(count or 0)

    # ── 评估（G0-G7 落台账；观察窗满自动 → gate_passed）────────────────

    async def evaluate(
        self,
        rollout_id: str,
        *,
        thresholds: dict[str, float] | None = None,
        owner: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        rollout = await self.get_rollout(rollout_id, owner=owner)
        stage = rollout["stage"]
        if stage not in store.ACTIVE_STAGES:
            raise RolloutConflict(f"阶段 {stage} 不可评估（已终态）")

        evidence, warnings = await self.assemble_evidence(rollout)
        result = evaluate_rollout(evidence, thresholds=thresholds)

        next_stage = stage
        if stage == store.STAGE_OBSERVING:
            obs = evidence.get("observation") or {}
            min_days = int(result["thresholds"]["g5_min_days"])
            if int(obs.get("n_days") or 0) >= min_days:
                next_stage = store.STAGE_GATE_PASSED

        updated = await store.transition(
            rollout_id,
            to_stage=next_stage,
            from_stages=[stage],
            gate_result=result,
            evidence=evidence,
        )
        if updated is None:
            raise RolloutConflict("台账阶段已变化，请刷新后重试")
        return {"rollout": updated, "evaluation": result, "warnings": warnings}

    # ── 开始观察（硬闸门 G0/G1 + 开 settings 行）────────────────────────

    async def start_observation(
        self,
        rollout_id: str,
        *,
        schedule_time: str | None = None,
        owner: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        rollout = await self.get_rollout(rollout_id, owner=owner)
        stage = rollout["stage"]
        if stage not in (store.STAGE_REPLAY_EVAL, store.STAGE_OBSERVING):
            raise RolloutConflict(f"阶段 {stage} 不能进入观察期")

        gate_result = rollout.get("gate_result") or {}
        gates = {
            g.get("gate"): g for g in gate_result.get("gates") or [] if isinstance(g, dict)
        }
        if not gates:
            raise RolloutInvalid("尚未评估——先运行 evaluate（G0/G1 是进观察期的硬闸门）")
        for gate_id in ("G0", "G1"):
            gate = gates.get(gate_id) or {}
            if gate.get("status") != "pass":
                reasons = "；".join(gate.get("reasons") or []) or gate.get("status")
                raise RolloutConflict(f"{gate_id} 未过，不能进观察期：{reasons}")

        challenger = await model_registry_service.get_model(
            tenant_id=rollout["tenant_id"],
            user_id=rollout["user_id"],
            model_id=rollout["challenger_model_id"],
        )
        if challenger is None or str(challenger.get("status") or "") not in _READY_STATUSES:
            raise RolloutInvalid("挑战者不在 ready/active，不能进观察期")

        # 排班取冠军同款（冠军没有 settings 行时回默认 00:00）
        if not schedule_time:
            champion_settings = await model_inference_persistence.get_settings(
                tenant_id=rollout["tenant_id"],
                user_id=rollout["user_id"],
                model_id=rollout["champion_model_id"],
            )
            schedule_time = str(champion_settings.get("schedule_time") or "00:00")

        settings = await model_inference_persistence.update_settings(
            tenant_id=rollout["tenant_id"],
            user_id=rollout["user_id"],
            model_id=rollout["challenger_model_id"],
            enabled=True,
            schedule_time=schedule_time,
        )

        evidence = dict(rollout.get("evidence") or {})
        evidence.setdefault("observation_since", utc_now().isoformat())
        updated = await store.transition(
            rollout_id,
            to_stage=store.STAGE_OBSERVING,
            from_stages=[store.STAGE_REPLAY_EVAL, store.STAGE_OBSERVING],
            evidence=evidence,
        )
        if updated is None:
            # 台账没改到 → 关掉刚开的 settings，别让孤儿推理继续跑
            await self._close_settings(rollout)
            raise RolloutConflict("台账阶段已变化，请刷新后重试")
        return {"rollout": updated, "settings": settings}

    # ── 晋升（单事务：市场级默认切换 + 备任链 + 审计）──────────────────

    async def promote(
        self,
        rollout_id: str,
        *,
        decided_by: str,
        notes: str,
        owner: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        if not str(notes or "").strip():
            raise RolloutInvalid("晋升必须填写理由（审计）")
        rollout = await self.get_rollout(rollout_id, owner=owner)
        if rollout["stage"] != store.STAGE_GATE_PASSED:
            raise RolloutConflict(
                f"阶段 {rollout['stage']} 不可晋升（需 gate_passed：观察期满且已评估）"
            )
        market = rollout["market"]
        warnings: list[str] = []
        now = datetime.now(timezone.utc)

        async with get_session() as session:
            current = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT model_id FROM qm_user_models
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND is_default = TRUE
                              AND qm_market_of(metadata_json) = :market
                            FOR UPDATE
                            """
                        ),
                        {
                            "tenant_id": rollout["tenant_id"],
                            "user_id": rollout["user_id"],
                            "market": market,
                        },
                    )
                )
                .mappings()
                .first()
            )
            prior_default = str(current["model_id"]) if current else None
            if prior_default and prior_default != rollout["champion_model_id"]:
                warnings.append(
                    f"当前默认（{prior_default}）≠ 台账 champion（{rollout['champion_model_id']}），"
                    "prior_default 按现场记录"
                )

            # 市场级切换：只清同市场默认——CN/HK/US 冠军互不顶掉（§5.5）
            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET is_default = FALSE, updated_at = :now
                    WHERE tenant_id = :tenant_id AND user_id = :user_id
                      AND is_default = TRUE
                      AND qm_market_of(metadata_json) = :market
                    """
                ),
                {
                    "tenant_id": rollout["tenant_id"],
                    "user_id": rollout["user_id"],
                    "market": market,
                    "now": now,
                },
            )
            activated = await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET is_default = TRUE, activated_at = :now, updated_at = :now
                    WHERE tenant_id = :tenant_id AND user_id = :user_id
                      AND model_id = :model_id AND status IN ('ready', 'active')
                    """
                ),
                {
                    "tenant_id": rollout["tenant_id"],
                    "user_id": rollout["user_id"],
                    "model_id": rollout["challenger_model_id"],
                    "now": now,
                },
            )
            if activated.rowcount != 1:
                raise RolloutInvalid("挑战者不在 ready/active，晋升中止（已整体回滚）")

            promoted = await store.transition(
                rollout_id,
                to_stage=store.STAGE_PROMOTED,
                from_stages=[store.STAGE_GATE_PASSED],
                prior_default_model_id=prior_default,
                decided_by=decided_by,
                notes=notes,
                decided=True,
                session=session,
            )
            if promoted is None:
                raise RolloutConflict("台账阶段已变化，晋升中止（已整体回滚）")

        return {
            "rollout": promoted,
            "prior_default_model_id": prior_default,
            "warnings": warnings,
        }

    # ── 拒绝（关 settings 行 + 审计）───────────────────────────────────

    async def reject(
        self,
        rollout_id: str,
        *,
        decided_by: str,
        notes: str,
        owner: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        if not str(notes or "").strip():
            raise RolloutInvalid("拒绝必须填写理由（审计）")
        rollout = await self.get_rollout(rollout_id, owner=owner)
        updated = await store.transition(
            rollout_id,
            to_stage=store.STAGE_REJECTED,
            from_stages=sorted(store.ACTIVE_STAGES),
            decided_by=decided_by,
            notes=notes,
            decided=True,
            require_notes=True,
        )
        if updated is None:
            raise RolloutConflict(f"阶段 {rollout['stage']} 不可拒绝（已终态）")
        settings_warning = await self._close_settings(rollout)
        result: dict[str, Any] = {"rollout": updated}
        if settings_warning:
            result["warning"] = settings_warning
        return result

    # ── 回滚（单事务：默认切回备任 + 理由必填）─────────────────────────

    async def rollback(
        self,
        rollout_id: str,
        *,
        decided_by: str,
        notes: str,
        owner: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        if not str(notes or "").strip():
            raise RolloutInvalid("回滚必须填写理由（审计）")
        rollout = await self.get_rollout(rollout_id, owner=owner)
        if rollout["stage"] != store.STAGE_PROMOTED:
            raise RolloutConflict(f"阶段 {rollout['stage']} 不可回滚（仅 promoted 可回滚）")
        prior_default = str(rollout.get("prior_default_model_id") or "")
        if not prior_default:
            raise RolloutInvalid("台账无备任链（prior_default_model_id 空），请人工设置默认模型")

        market = rollout["market"]
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            prior_row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT status FROM qm_user_models
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND model_id = :model_id
                            FOR UPDATE
                            """
                        ),
                        {
                            "tenant_id": rollout["tenant_id"],
                            "user_id": rollout["user_id"],
                            "model_id": prior_default,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if prior_row is None or str(prior_row["status"]) not in _READY_STATUSES:
                raise RolloutInvalid(f"备任模型 {prior_default} 不存在或不在 ready/active，回滚中止")

            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET is_default = FALSE, updated_at = :now
                    WHERE tenant_id = :tenant_id AND user_id = :user_id
                      AND is_default = TRUE
                      AND qm_market_of(metadata_json) = :market
                    """
                ),
                {
                    "tenant_id": rollout["tenant_id"],
                    "user_id": rollout["user_id"],
                    "market": market,
                    "now": now,
                },
            )
            restored = await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET is_default = TRUE, activated_at = :now, updated_at = :now
                    WHERE tenant_id = :tenant_id AND user_id = :user_id
                      AND model_id = :model_id
                    """
                ),
                {
                    "tenant_id": rollout["tenant_id"],
                    "user_id": rollout["user_id"],
                    "model_id": prior_default,
                    "now": now,
                },
            )
            if restored.rowcount != 1:
                raise RolloutInvalid("备任模型回切失败，回滚中止（已整体回滚）")

            rolled = await store.transition(
                rollout_id,
                to_stage=store.STAGE_ROLLED_BACK,
                from_stages=[store.STAGE_PROMOTED],
                decided_by=decided_by,
                notes=notes,
                decided=True,
                require_notes=True,
                session=session,
            )
            if rolled is None:
                raise RolloutConflict("台账阶段已变化，回滚中止（已整体回滚）")

        result: dict[str, Any] = {"rollout": rolled, "restored_model_id": prior_default}
        settings_warning = await self._close_settings(rollout)
        if settings_warning:
            result["warning"] = settings_warning
        return result

    # ── 内部：关观察推理 settings（best-effort，失败只留 warning）───────

    async def _close_settings(self, rollout: dict[str, Any]) -> str | None:
        try:
            await model_inference_persistence.update_settings(
                tenant_id=rollout["tenant_id"],
                user_id=rollout["user_id"],
                model_id=rollout["challenger_model_id"],
                enabled=False,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - 关行失败不推翻已完成的台账动作
            return f"关闭挑战者自动推理设置失败（请手工检查）: {exc}"


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


model_rollout_service = ModelRolloutService()
