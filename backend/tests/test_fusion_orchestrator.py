"""融合编排层（fusion_orchestrator）契约测试 —— 全部离线（fake registry + fake 证据）。

锁定的行为（对应 docs/机构级模型融合_设计方案.md §5/§9）：
1. 成员硬校验：≥2、存在、ready/active、不重复、同一市场 —— 一律 ValueError。
2. 预览链路：证据 → 权重引擎 → OOS 回放；权重由服务器权威计算。
3. 失败姿态：证据不可得 fail-open（等权 + warning），手动权重类错误硬失败。
4. create 把权重/诊断/回放/周期原样交给 register_ensemble_model
   （「创建即日更」settings 行是 registry 的单点职责，不在此层重复）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from backend.services.engine.inference import fusion_orchestrator as orch
from backend.services.engine.inference.fusion_quality import (
    FusionEvidence,
    MemberEvidence,
)
from backend.shared.model_registry import model_registry_service

_TENANT = "default"
_USER = "10000001"
_BASE_DATE = date(2026, 1, 1)


def _row(mid: str, *, market: str = "CN", status: str = "ready", as_json_str: bool = False):
    meta = {
        "display_name": f"模型-{mid}",
        "model_type": "lightgbm",
        "market": market,
    }
    return {
        "model_id": mid,
        "status": status,
        "storage_path": f"/tmp/models/{mid}",
        "metadata_json": json.dumps(meta) if as_json_str else meta,
    }


def _ic_by_date(n: int, mean: float, alt: float = 0.05) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(n):
        day = (_BASE_DATE + timedelta(days=i)).isoformat()
        out[day] = mean + (alt if i % 2 == 0 else -alt)
    return out


def _panels_and_labels(days: int = 3, symbols: int = 40):
    """A 与标签同序、B 与标签同序但被扰动 —— 保证回放产出可用摘要。"""
    rows_a, rows_b, rows_lab = [], [], []
    for d in range(days):
        day = (date(2026, 2, 2) + timedelta(days=d)).isoformat()
        for i in range(symbols):
            sym = f"S{i:03d}"
            rows_lab.append((day, sym, float(i)))
            rows_a.append((day, sym, float(i) + (0.25 if i % 4 == 0 else 0.0)))
            rows_b.append((day, sym, float(i) - (0.25 if i % 5 == 0 else 0.0)))
    cols = ["trade_date", "symbol", "score"]
    panels = {
        "m_a": pd.DataFrame(rows_a, columns=cols),
        "m_b": pd.DataFrame(rows_b, columns=cols),
    }
    labels = pd.DataFrame(rows_lab, columns=["trade_date", "symbol", "label"])
    return panels, labels


def _evidence(
    ic_a: dict[str, float],
    ic_b: dict[str, float],
    *,
    corr: float | None = None,
    horizon: int = 5,
) -> FusionEvidence:
    panels, labels = _panels_and_labels()
    members = (
        MemberEvidence(
            model_id="m_a",
            horizon_days=horizon,
            market="CN",
            ic_by_date=ic_a,
            score_days=len(ic_a) + 2,
        ),
        MemberEvidence(
            model_id="m_b",
            horizon_days=horizon,
            market="CN",
            ic_by_date=ic_b,
            score_days=len(ic_b) + 2,
        ),
    )
    corr_matrix = {"m_a": {"m_b": corr}, "m_b": {"m_a": corr}} if corr is not None else {}
    return FusionEvidence(
        members=members,
        corr=corr_matrix,
        panels=panels,
        labels_by_horizon={horizon: labels},
        warnings=(),
    )


def _wire_registry(monkeypatch, rows: dict, horizons: dict | None = None, capture: dict | None = None):
    async def fake_get_model(*, tenant_id, user_id, model_id):
        return rows.get(model_id)

    monkeypatch.setattr(model_registry_service, "get_model", fake_get_model)
    hmap = horizons or {}
    monkeypatch.setattr(orch, "read_model_horizon", lambda d: hmap.get(str(d), 5))
    if capture is not None:

        async def fake_register(**kwargs):
            capture.update(kwargs)
            return {
                "model_id": "mdl_cn_ensemble_test_00000000",
                "status": "ready",
                "daily_inference": {"enabled": True, "error": None},
            }

        monkeypatch.setattr(model_registry_service, "register_ensemble_model", fake_register)


class TestResolveMembers:
    @pytest.mark.asyncio
    async def test_rejects_fewer_than_two_and_duplicates(self, monkeypatch):
        _wire_registry(monkeypatch, {"m_a": _row("m_a")})
        with pytest.raises(ValueError, match="至少需要 2"):
            await orch.resolve_fusion_members(model_ids=["m_a"], tenant_id=_TENANT, user_id=_USER)
        with pytest.raises(ValueError, match="不可重复"):
            await orch.resolve_fusion_members(
                model_ids=["m_a", "m_a"], tenant_id=_TENANT, user_id=_USER
            )

    @pytest.mark.asyncio
    async def test_rejects_missing_and_not_ready(self, monkeypatch):
        _wire_registry(
            monkeypatch,
            {"m_a": _row("m_a"), "m_b": _row("m_b", status="training")},
        )
        with pytest.raises(ValueError, match="不存在"):
            await orch.resolve_fusion_members(
                model_ids=["m_a", "m_x"], tenant_id=_TENANT, user_id=_USER
            )
        with pytest.raises(ValueError, match="ready/active"):
            await orch.resolve_fusion_members(
                model_ids=["m_a", "m_b"], tenant_id=_TENANT, user_id=_USER
            )

    @pytest.mark.asyncio
    async def test_rejects_cross_market(self, monkeypatch):
        _wire_registry(
            monkeypatch,
            {"m_a": _row("m_a", market="CN"), "m_b": _row("m_b", market="HK")},
        )
        with pytest.raises(ValueError, match="同一市场"):
            await orch.resolve_fusion_members(
                model_ids=["m_a", "m_b"], tenant_id=_TENANT, user_id=_USER
            )

    @pytest.mark.asyncio
    async def test_parses_string_metadata_and_reads_horizon(self, monkeypatch):
        _wire_registry(
            monkeypatch,
            {"m_a": _row("m_a", as_json_str=True), "m_b": _row("m_b")},
            horizons={"/tmp/models/m_a": 3, "/tmp/models/m_b": 5},
        )
        members = await orch.resolve_fusion_members(
            model_ids=["m_a", "m_b"], tenant_id=_TENANT, user_id=_USER
        )
        assert [m.horizon_days for m in members] == [3, 5]
        assert members[0].display_name == "模型-m_a"


class TestPreview:
    @pytest.mark.asyncio
    async def test_equal_strategy_weights_and_replay(self, monkeypatch):
        _wire_registry(monkeypatch, {"m_a": _row("m_a"), "m_b": _row("m_b")})

        async def fake_gather(specs, **kwargs):
            return _evidence(_ic_by_date(30, 0.10), _ic_by_date(30, 0.05))

        monkeypatch.setattr(orch, "gather_fusion_evidence", fake_gather)
        out = await orch.build_fusion_preview(
            tenant_id=_TENANT,
            user_id=_USER,
            model_ids=["m_a", "m_b"],
            weight_strategy="equal",
        )
        assert out["weights"] == pytest.approx({"m_a": 0.5, "m_b": 0.5})
        assert out["replay"]["dates"] == 3
        assert out["replay"]["summary"]["fused"]["n_days"] == 3
        # 只做结构性断言：3 天合成数据里成员 ICIR（近完美 IC、极小方差）高于融合值
        # 是正当行为；beats_* 的数学裁决由 TestReplayVerdict 用干净数字单独锁定。
        verdict = out["replay"]["verdict"]
        assert verdict is not None and verdict["fused_ic_days"] == 3
        assert isinstance(verdict["beats_median"], bool)
        assert out["warnings"] == []
        assert out["market"] == "CN" and out["horizon_days"] == 5

    @pytest.mark.asyncio
    async def test_icir_shrunk_tilts_toward_high_icir_member(self, monkeypatch):
        _wire_registry(
            monkeypatch,
            {
                "m_a": _row("m_a"),
                "m_b": _row("m_b"),
                "m_c": _row("m_c"),
                "m_d": _row("m_d"),
            },
        )

        async def fake_gather(specs, **kwargs):
            members = tuple(
                MemberEvidence(
                    model_id=mid,
                    horizon_days=5,
                    market="CN",
                    ic_by_date=_ic_by_date(60, mean),
                    score_days=62,
                )
                for mid, mean in (
                    ("m_a", 0.15),
                    ("m_b", 0.10),
                    ("m_c", 0.05),
                    ("m_d", 0.025),
                )
            )
            panels, labels = _panels_and_labels()
            return FusionEvidence(
                members=members,
                corr={},
                panels=panels,
                labels_by_horizon={5: labels},
                warnings=(),
            )

        monkeypatch.setattr(orch, "gather_fusion_evidence", fake_gather)
        out = await orch.build_fusion_preview(
            tenant_id=_TENANT, user_id=_USER, model_ids=["m_a", "m_b", "m_c", "m_d"]
        )
        w = out["weights"]
        assert w["m_a"] > w["m_b"] > w["m_c"] > w["m_d"] > 0
        assert max(w.values()) <= 0.40 + 1e-9
        assert sum(w.values()) == pytest.approx(1.0)
        diag = {d["member_id"]: d for d in out["diagnostics"]}
        assert diag["m_a"]["icir"] > diag["m_d"]["icir"]

    @pytest.mark.asyncio
    async def test_insufficient_days_preview_is_equal_with_warning(self, monkeypatch):
        _wire_registry(monkeypatch, {"m_a": _row("m_a"), "m_b": _row("m_b")})

        async def fake_gather(specs, **kwargs):
            return _evidence(_ic_by_date(5, 0.2), _ic_by_date(6, 0.1))

        monkeypatch.setattr(orch, "gather_fusion_evidence", fake_gather)
        out = await orch.build_fusion_preview(
            tenant_id=_TENANT, user_id=_USER, model_ids=["m_a", "m_b"]
        )
        assert out["weights"] == pytest.approx({"m_a": 0.5, "m_b": 0.5})
        assert "all_insufficient_days_fallback_equal" in out["warnings"]

    @pytest.mark.asyncio
    async def test_evidence_failure_fails_open_with_equal_weights(self, monkeypatch):
        _wire_registry(monkeypatch, {"m_a": _row("m_a"), "m_b": _row("m_b")})

        async def fake_gather(specs, **kwargs):
            raise RuntimeError("scores bucket empty")

        monkeypatch.setattr(orch, "gather_fusion_evidence", fake_gather)
        out = await orch.build_fusion_preview(
            tenant_id=_TENANT, user_id=_USER, model_ids=["m_a", "m_b"]
        )
        assert out["status"] == "success"
        assert out["weights"] == pytest.approx({"m_a": 0.5, "m_b": 0.5})
        assert any(w.startswith("evidence_unavailable:") for w in out["warnings"])
        assert out["replay"] is None
        assert all(row["ic_days"] == 0 for row in out["members"])

    @pytest.mark.asyncio
    async def test_horizon_mismatch_warns_and_mode_wins(self, monkeypatch):
        _wire_registry(
            monkeypatch,
            {"m_a": _row("m_a"), "m_b": _row("m_b"), "m_c": _row("m_c")},
            horizons={"/tmp/models/m_a": 5, "/tmp/models/m_b": 5, "/tmp/models/m_c": 3},
        )

        async def fake_gather(specs, **kwargs):
            members = tuple(
                MemberEvidence(
                    model_id=s.model_id,
                    horizon_days=int(s.horizon_days or 5),
                    market="CN",
                    ic_by_date=_ic_by_date(5, 0.05),
                    score_days=7,
                )
                for s in specs
            )
            panels, labels = _panels_and_labels()
            return FusionEvidence(
                members=members,
                corr={},
                panels=panels,
                labels_by_horizon={5: labels},
                warnings=(),
            )

        monkeypatch.setattr(orch, "gather_fusion_evidence", fake_gather)
        out = await orch.build_fusion_preview(
            tenant_id=_TENANT, user_id=_USER, model_ids=["m_a", "m_b", "m_c"]
        )
        assert out["horizon_days"] == 5
        assert any(w.startswith("horizon_mismatch") for w in out["warnings"])

    @pytest.mark.asyncio
    async def test_manual_weights_respected_and_missing_member_rejected(self, monkeypatch):
        _wire_registry(monkeypatch, {"m_a": _row("m_a"), "m_b": _row("m_b")})

        async def fake_gather(specs, **kwargs):
            return _evidence(_ic_by_date(30, 0.1), _ic_by_date(30, 0.05))

        monkeypatch.setattr(orch, "gather_fusion_evidence", fake_gather)
        out = await orch.build_fusion_preview(
            tenant_id=_TENANT,
            user_id=_USER,
            model_ids=["m_a", "m_b"],
            weight_strategy="manual",
            manual_weights={"m_a": 2.0, "m_b": 1.0},
        )
        assert out["weights"] == pytest.approx({"m_a": 2 / 3, "m_b": 1 / 3})
        with pytest.raises(ValueError):
            await orch.build_fusion_preview(
                tenant_id=_TENANT,
                user_id=_USER,
                model_ids=["m_a", "m_b"],
                weight_strategy="manual",
                manual_weights={"m_a": 1.0},
            )


class TestReplayVerdict:
    def test_flags_beat_best_and_median(self):
        summary = {
            "fused": {"ic_mean": 0.06, "icir": 0.8, "n_days": 10.0},
            "m_a": {"ic_mean": 0.04, "icir": 0.5, "n_days": 10.0},
            "m_b": {"ic_mean": 0.02, "icir": 0.3, "n_days": 10.0},
            "m_c": {"ic_mean": 0.05, "icir": 0.7, "n_days": 10.0},
        }
        verdict = orch._replay_verdict(summary)
        assert verdict["beats_best"] is True
        assert verdict["beats_median"] is True
        assert verdict["best_member_icir"] == pytest.approx(0.7)
        assert verdict["median_member_icir"] == pytest.approx(0.5)

    def test_returns_none_when_evidence_insufficient_or_nan(self):
        assert orch._replay_verdict({}) is None
        assert (
            orch._replay_verdict({"fused": {"icir": float("nan"), "n_days": 5.0}}) is None
        )
        assert (
            orch._replay_verdict(
                {
                    "fused": {"icir": 0.5, "n_days": 5.0},
                    "m_a": {"icir": 0.4, "n_days": 0.0},
                }
            )
            is None
        )


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_forwards_artifacts_to_registry(self, monkeypatch):
        rows = {"m_a": _row("m_a"), "m_b": _row("m_b")}
        capture: dict = {}
        _wire_registry(monkeypatch, rows, capture=capture)

        async def fake_gather(specs, **kwargs):
            return _evidence(_ic_by_date(30, 0.10), _ic_by_date(30, 0.05))

        monkeypatch.setattr(orch, "gather_fusion_evidence", fake_gather)
        out = await orch.create_fusion_model(
            tenant_id=_TENANT,
            user_id=_USER,
            model_ids=["m_a", "m_b"],
            display_name="双模融合",
        )
        assert capture["weights_override"] == pytest.approx({"m_a": 0.5, "m_b": 0.5})
        assert capture["weight_strategy"] == "icir_shrunk"
        assert len(capture["weight_diagnostics"]) == 2
        assert capture["fusion_eval"]["dates"] == 3
        assert capture["target_horizon_days"] == 5
        assert capture["source_model_ids"] == ["m_a", "m_b"]
        assert out["model_id"] == "mdl_cn_ensemble_test_00000000"
        assert out["daily_inference"]["enabled"] is True
        assert out["preview"]["weights"] == pytest.approx({"m_a": 0.5, "m_b": 0.5})
