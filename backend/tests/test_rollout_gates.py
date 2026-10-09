"""P2 晋升闸门 G0-G7（``backend/shared/model_rollout.py``）纯函数测试。

口径出处：设计文档 §5.2（闸门表）+ §4.5（可复现三件套→G0 扩展检查）。
纪律（AAA 结构）：

- 每个闸门三态诚实：**pass / fail / skip**——证据缺了是 skip（未评估）不是
  fail（判定不通过），也不是静默 pass；G7 是展示项（``info``）；
- 阈值全部可配（``thresholds`` 覆盖默认值）；观察模式（只算不拦）在服务层，
  纯函数只出结论；
- 边界按「≥/≤」设计措辞判定（如 mean ΔIC 恰为 −0.002、t 恰为 −1.0 → 非劣
  通过）。
"""

from __future__ import annotations

import pytest


def _admission(**over):
    base = {
        "registration_soft_gate_passed": True,
        "status": "candidate",
        "reproducibility": {
            "seed": 42,
            "config_yaml": True,
            "data_fingerprint": "abc123",
        },
    }
    base.update(over)
    return base


def _independence(**over):
    base = {
        "metrics_identical": False,
        "pred_md5_challenger": "aaa",
        "pred_md5_champion": "bbb",
    }
    base.update(over)
    return base


def _delta(mean: float, t: float | None, *, sufficient: bool = True):
    return {"sufficient": sufficient, "mean": mean, "t": t, "n_days": 100}


def _monthly(c_worst: float, c_std: float | None, h_worst: float, h_std: float | None):
    return {
        "challenger": {"worst": c_worst, "month_std": c_std},
        "champion": {"worst": h_worst, "month_std": h_std},
    }


def _gate(evidence: dict, gate_id: str, **kwargs):
    from backend.shared.model_rollout import evaluate_rollout

    result = evaluate_rollout(evidence, **kwargs)
    return next(g for g in result["gates"] if g["gate"] == gate_id)


# ── G0 准入（含 §4.5 可复现三件套扩展检查）────────────────────────────


@pytest.mark.unit
def test_g0_pass_via_soft_gate_or_ready():
    from backend.shared.model_rollout import gate_g0_admission

    via_soft = gate_g0_admission({"admission": _admission()})
    assert via_soft["status"] == "pass"

    via_ready = gate_g0_admission(
        {"admission": _admission(registration_soft_gate_passed=None, status="ready")}
    )
    assert via_ready["status"] == "pass"


@pytest.mark.unit
def test_g0_fail_without_admission_and_missing_trio():
    from backend.shared.model_rollout import gate_g0_admission

    denied = gate_g0_admission(
        {"admission": _admission(registration_soft_gate_passed=False, status="candidate")}
    )
    assert denied["status"] == "fail"

    no_fp = _admission()
    no_fp["reproducibility"] = dict(no_fp["reproducibility"], data_fingerprint=None)
    broken = gate_g0_admission({"admission": no_fp})
    assert broken["status"] == "fail"
    assert "data_fingerprint" in broken["detail"]["missing"]

    assert gate_g0_admission({})["status"] == "skip"


@pytest.mark.unit
def test_g0_reports_unpinned_seed_but_presence_is_the_criterion():
    from backend.shared.model_rollout import gate_g0_admission

    res = gate_g0_admission({"admission": _admission()})
    assert res["status"] == "pass"
    assert res["detail"]["seed_pinned"] is True

    odd = _admission()
    odd["reproducibility"] = dict(odd["reproducibility"], seed=7)
    res_odd = gate_g0_admission({"admission": odd})
    assert res_odd["status"] == "pass"
    assert res_odd["detail"]["seed_pinned"] is False
    assert res_odd["detail"]["seed"] == 7


# ── G1 独立性 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_g1_pass_and_copy_detection_fails():
    from backend.shared.model_rollout import gate_g1_independence

    assert gate_g1_independence({"independence": _independence()})["status"] == "pass"

    same_metrics = gate_g1_independence(
        {"independence": _independence(metrics_identical=True)}
    )
    assert same_metrics["status"] == "fail"

    same_md5 = gate_g1_independence(
        {"independence": _independence(pred_md5_challenger="x", pred_md5_champion="x")}
    )
    assert same_md5["status"] == "fail"


@pytest.mark.unit
def test_g1_skips_when_artifacts_missing():
    from backend.shared.model_rollout import gate_g1_independence

    assert gate_g1_independence({})["status"] == "skip"
    assert (
        gate_g1_independence({"independence": _independence(pred_md5_challenger=None)})[
            "status"
        ]
        == "skip"
    )


# ── G2 回放非劣 / 优效 ─────────────────────────────────────────────────


@pytest.mark.unit
def test_g2_non_inferior_and_superior_paths():
    # 非劣：mean ≥ −0.002 且 t ≥ −1.0（边界值也通过）
    for mean, t in [(0.0, 0.1), (-0.001, -0.5), (-0.002, -1.0)]:
        res = _gate({"delta_summary": _delta(mean, t)}, "G2")
        assert res["status"] == "pass", (mean, t)
        assert res["detail"]["non_inferior"] is True

    # 优效：mean > 0 且 t ≥ 1.5
    sup = _gate({"delta_summary": _delta(0.01, 1.6)}, "G2")
    assert sup["status"] == "pass"
    assert sup["detail"]["superior"] is True


@pytest.mark.unit
def test_g2_fails_on_mean_or_t_and_skips_when_insufficient():
    assert _gate({"delta_summary": _delta(-0.003, 0.0)}, "G2")["status"] == "fail"
    assert _gate({"delta_summary": _delta(-0.001, -1.2)}, "G2")["status"] == "fail"
    assert (
        _gate({"delta_summary": _delta(0.0, None, sufficient=False)}, "G2")["status"]
        == "skip"
    )
    assert _gate({}, "G2")["status"] == "skip"


# ── G3 稳定性 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_g3_stability_pass_and_failures():
    ok = _gate({"monthly": _monthly(-0.031, 0.05, -0.02, 0.04)}, "G3")
    assert ok["status"] == "pass"

    worst_fail = _gate({"monthly": _monthly(-0.05, 0.05, -0.02, 0.04)}, "G3")
    assert worst_fail["status"] == "fail"

    std_fail = _gate({"monthly": _monthly(-0.031, 0.07, -0.02, 0.04)}, "G3")
    assert std_fail["status"] == "fail"


@pytest.mark.unit
def test_g3_skips_without_monthly_stats():
    assert _gate({}, "G3")["status"] == "skip"
    assert _gate({"monthly": _monthly(-0.03, None, -0.02, 0.04)}, "G3")["status"] == "skip"


# ── G4 状态依赖（P3 前未评估）──────────────────────────────────────────


@pytest.mark.unit
def test_g4_fails_on_collapse_bucket_and_ignores_thin_buckets():
    bucket = [{"bucket": "down/high_vol", "n_days": 15, "challenger_mean_ic": -0.01, "champion_mean_ic": 0.02}]
    assert _gate({"regime": {"buckets": bucket}}, "G4")["status"] == "fail"

    thin = [dict(bucket[0], n_days=14)]
    assert _gate({"regime": {"buckets": thin}}, "G4")["status"] == "pass"

    fine = [dict(bucket[0], challenger_mean_ic=0.02)]
    assert _gate({"regime": {"buckets": fine}}, "G4")["status"] == "pass"


@pytest.mark.unit
def test_g4_skips_before_p3():
    res = _gate({}, "G4")
    assert res["status"] == "skip"
    assert "未评估" in res["reasons"][0]


# ── G5 观察期 ──────────────────────────────────────────────────────────


def _observation(n_days: int, c_ic: float, h_ic: float, c_cov: float = 0.9, h_cov: float = 1.0):
    return {
        "sufficient": True,
        "n_days": n_days,
        "challenger_mean_ic": c_ic,
        "champion_mean_ic": h_ic,
        "challenger_coverage": c_cov,
        "champion_coverage": h_cov,
    }


@pytest.mark.unit
def test_g5_observation_window_and_thresholds():
    # 满 20 天且 IC 地板 = max(0.005, 0.9×冠军)
    assert _gate({"observation": _observation(20, 0.006, 0.001)}, "G5")["status"] == "pass"
    # 冠军强时按比例抬地板：0.9×0.02=0.018 → 0.01 不过
    ic_fail = _gate({"observation": _observation(20, 0.01, 0.02)}, "G5")
    assert ic_fail["status"] == "fail"
    # 覆盖率 ≥ 0.9×冠军
    cov_fail = _gate({"observation": _observation(20, 0.05, 0.02, c_cov=0.8)}, "G5")
    assert cov_fail["status"] == "fail"


@pytest.mark.unit
def test_g5_skips_when_immature_or_absent():
    res = _gate({"observation": _observation(19, 0.05, 0.01)}, "G5")
    assert res["status"] == "skip"
    assert "19/20" in res["reasons"][0]
    assert _gate({}, "G5")["status"] == "skip"


# ── G6 成本（换手比）───────────────────────────────────────────────────


def _turnover(c: float | None, h: float | None, *, sufficient: bool = True):
    return {
        "challenger": {"turnover_mean": c, "sufficient": sufficient},
        "champion": {"turnover_mean": h, "sufficient": sufficient},
    }


@pytest.mark.unit
def test_g6_turnover_ratio_and_skip_cases():
    assert _gate({"turnover": _turnover(1.29, 1.0)}, "G6")["status"] == "pass"
    assert _gate({"turnover": _turnover(1.31, 1.0)}, "G6")["status"] == "fail"
    # 冠军零换手：比值无定义 → skip 不判
    assert _gate({"turnover": _turnover(0.5, 0.0)}, "G6")["status"] == "skip"
    assert (
        _gate({"turnover": _turnover(None, None, sufficient=False)}, "G6")["status"]
        == "skip"
    )
    assert _gate({}, "G6")["status"] == "skip"


# ── G7 提示（仅展示） ──────────────────────────────────────────────────


@pytest.mark.unit
def test_g7_is_info_only():
    res = _gate({"trials": {"trial_count": 5}}, "G7")
    assert res["status"] == "info"
    assert res["detail"]["trial_count"] == 5

    absent = _gate({}, "G7")
    assert absent["status"] == "info"
    assert absent["detail"]["trial_count"] is None


# ── 总评与阈值覆盖 ─────────────────────────────────────────────────────


def _full_evidence():
    return {
        "admission": _admission(),
        "independence": _independence(),
        "delta_summary": _delta(0.01, 1.6),
        "monthly": _monthly(-0.031, 0.05, -0.02, 0.04),
        "turnover": _turnover(1.29, 1.0),
        "observation": _observation(20, 0.006, 0.001),
        "trials": {"trial_count": 3},
    }


@pytest.mark.unit
def test_evaluate_rollout_summary_and_order():
    from backend.shared.model_rollout import GATE_ORDER, evaluate_rollout

    result = evaluate_rollout(_full_evidence())
    assert [g["gate"] for g in result["gates"]] == list(GATE_ORDER)
    summary = result["summary"]
    # G4 无 regime 证据 → skip；其余 pass；G7 info
    assert summary["counts"]["fail"] == 0
    assert summary["counts"]["skip"] == 1
    assert summary["verdict"] == "incomplete"

    no_skips = _full_evidence()
    no_skips["regime"] = {"buckets": []}
    summary2 = evaluate_rollout(no_skips)["summary"]
    assert summary2["verdict"] == "all_pass"


@pytest.mark.unit
def test_evaluate_rollout_flags_failures_and_respects_overrides():
    from backend.shared.model_rollout import evaluate_rollout

    evidence = _full_evidence()
    evidence["turnover"] = _turnover(1.35, 1.0)
    flagged = evaluate_rollout(evidence)
    assert flagged["summary"]["verdict"] == "flagged"
    assert flagged["summary"]["counts"]["fail"] == 1

    # 阈值覆盖：放宽换手比到 1.4 → 同证据转 pass
    relaxed = evaluate_rollout(evidence, thresholds={"g6_turnover_ratio": 1.4})
    assert relaxed["summary"]["counts"]["fail"] == 0
    assert relaxed["thresholds"]["g6_turnover_ratio"] == 1.4
