"""P2 晋升闸门 G0-G7（**纯函数，无 IO**；设计 §5.2，三件套口径 §4.5）。

本模块只回答「证据摆出来，每个闸门的结论是什么」，不认识数据库、不发通知、
不做任何动作。**观察模式**（PASS/FAIL 只展示不拦）是 rollout 服务层的策略；
这里永远只算结论（``summary.enforced=False`` 即此意）。

证据契约（dict，rollout 服务组装；键缺 = 未评估）：

- ``admission``      G0：注册软闸门结果 / 登记状态 / **可复现三件套**
  （seed + config.yaml + data_fingerprint，§4.5「缺任何一件不允许进晋升流程」，
  此处即 G0 扩展检查）；
- ``independence``   G1：指标是否逐位相同 + 两侧 pred md5（相同 = 复制品）；
- ``delta_summary``  G2：``walkforward_rollup`` 的 ΔIC 汇总（mean/t/天数）；
- ``monthly``        G3：两侧月度统计（worst / month_std，近 12 个月）；
- ``regime``         G4：状态桶清单（P3 前留空 → 未评估）；
- ``observation``    G5：观察期（≥20 交易日）challenger/champion 同期 IC 与覆盖；
- ``turnover``       G6：两侧日均换手（``walkforward_rollup`` 产出）；
- ``trials``         G7：同 recipe 试次计数（仅展示，防 p-hacking）。

三态纪律：**pass / fail / skip 三态诚实**——证据缺失是 skip（未评估），不是
fail（判定不过），更不是静默 pass；G7 是展示项（``info``）。阈值全部可配
（``evaluate_rollout(..., thresholds={...})``），默认值出处 §5.2。

G0 与 G1 的「三件套」归属说明：§5.2 G1 行也列了三件套，但 §4.5 明说
「（G0 扩展检查）」且语义是「不允许进流程」→ 判定放在 G0；G1 只判独立性
（指标逐位相同 ∧ pred md5），不重复扣分。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

GATE_ORDER = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7")

GATE_LABELS = {
    "G0": "准入",
    "G1": "独立性",
    "G2": "回放非劣",
    "G3": "稳定性",
    "G4": "状态依赖",
    "G5": "观察期",
    "G6": "成本",
    "G7": "提示",
}

#: 默认阈值（设计 §5.2 建议值；观察期先展示不拦，积累 3-5 次真实决策后评审）
DEFAULT_THRESHOLDS: dict[str, float] = {
    "g2_non_inferior_mean": -0.002,
    "g2_non_inferior_t": -1.0,
    "g2_superior_t": 1.5,
    "g3_worst_slack": 0.02,
    "g3_std_ratio": 1.5,
    "g4_min_days": 15,
    "g4_challenger_ceiling": 0.0,
    "g4_champion_floor": 0.01,
    "g5_min_days": 20,
    "g5_ic_floor": 0.005,
    "g5_champion_ic_ratio": 0.9,
    "g5_coverage_ratio": 0.9,
    "g6_turnover_ratio": 1.3,
}

#: 优效路径的均值下界固定为「严格大于 0」（§5.2「或 mean ΔIC > 0 且 t ≥ 1.5」）
_SUPERIOR_MEAN_FLOOR = 0.0

#: 注册软闸门通过 ∨ 已有 ready —— 「已有 ready」的状态集
_READY_STATUSES = frozenset({"ready", "active"})


def _result(
    gate: str,
    status: str,
    reasons: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "gate": gate,
        "label": GATE_LABELS[gate],
        "status": status,
        "reasons": reasons or [],
        "detail": detail or {},
    }


# ── G0 准入（含可复现三件套扩展检查，§4.5）────────────────────────────


def gate_g0_admission(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """注册软闸门已过 ∨ 已有 ready；**且**可复现三件套齐（缺任一即拒）。"""
    admission = evidence.get("admission")
    if not isinstance(admission, Mapping):
        return _result("G0", "skip", ["无注册信息（admission 缺失）"])

    soft = admission.get("registration_soft_gate_passed")
    status = str(admission.get("status") or "")
    admitted = soft is True or status in _READY_STATUSES
    detail: dict[str, Any] = {
        "registration_soft_gate_passed": soft,
        "status": status or None,
        "admitted": admitted,
    }

    repro = admission.get("reproducibility")
    missing: list[str] = []
    if isinstance(repro, Mapping):
        seed = repro.get("seed")
        if seed is None:
            missing.append("seed")
        detail["seed"] = seed
        detail["seed_pinned"] = seed == 42  # 只报告；判据是「在场」（§4.5）
        if not repro.get("config_yaml"):
            missing.append("config_yaml")
        if not repro.get("data_fingerprint"):
            missing.append("data_fingerprint")
    else:
        missing = ["seed", "config_yaml", "data_fingerprint"]
    detail["missing"] = missing

    if missing:
        return _result(
            "G0", "fail", [f"可复现三件套缺 {missing}（§4.5：不允许进晋升流程）"], detail
        )
    if not admitted:
        return _result("G0", "fail", ["注册软闸门未过且非 ready/active"], detail)
    return _result("G0", "pass", detail=detail)


# ── G1 独立性 ──────────────────────────────────────────────────────────


def gate_g1_independence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """指标不逐位相同 ∧ pred md5 不同（HK 13 子模型复制事件教训）。"""
    indep = evidence.get("independence")
    if not isinstance(indep, Mapping):
        return _result("G1", "skip", ["无独立性证据（independence 缺失）"])

    metrics_identical = indep.get("metrics_identical")
    md5_c = str(indep.get("pred_md5_challenger") or "")
    md5_h = str(indep.get("pred_md5_champion") or "")
    if metrics_identical is None or not md5_c or not md5_h:
        return _result(
            "G1",
            "skip",
            ["独立性证据不全（指标比对结果或 pred md5 缺失）"],
            {
                "metrics_identical": metrics_identical,
                "pred_md5_challenger": md5_c or None,
                "pred_md5_champion": md5_h or None,
            },
        )

    detail = {
        "metrics_identical": bool(metrics_identical),
        "pred_md5_challenger": md5_c,
        "pred_md5_champion": md5_h,
    }
    reasons: list[str] = []
    if metrics_identical:
        reasons.append("指标逐位相同（复制品）")
    if md5_c == md5_h:
        reasons.append("pred md5 相同（同一份产物）")
    if reasons:
        return _result("G1", "fail", reasons, detail)
    return _result("G1", "pass", detail=detail)


# ── G2 回放非劣 / 优效 ─────────────────────────────────────────────────


def gate_g2_non_inferior(
    evidence: Mapping[str, Any], thresholds: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """mean ΔIC ≥ −0.002 且 t ≥ −1.0（非劣）；或 mean > 0 且 t ≥ 1.5（优效）。"""
    th = thresholds or DEFAULT_THRESHOLDS
    delta = evidence.get("delta_summary")
    if not isinstance(delta, Mapping) or delta.get("sufficient") is not True:
        reason = (
            str(delta.get("reason"))
            if isinstance(delta, Mapping) and delta.get("reason")
            else "ΔIC 汇总缺失或样本不足"
        )
        return _result("G2", "skip", [f"回放配对不可判：{reason}"])
    mean = delta.get("mean")
    t = delta.get("t")
    detail = {"mean": mean, "t": t, "n_days": delta.get("n_days")}
    if mean is None:
        return _result("G2", "skip", ["ΔIC 均值缺失"], detail)
    if t is None:
        return _result("G2", "skip", ["t 无定义（ΔIC 零方差）"], detail)

    non_inferior = (
        float(mean) >= th["g2_non_inferior_mean"] and float(t) >= th["g2_non_inferior_t"]
    )
    superior = float(mean) > _SUPERIOR_MEAN_FLOOR and float(t) >= th["g2_superior_t"]
    detail["non_inferior"] = non_inferior
    detail["superior"] = superior
    if non_inferior or superior:
        return _result("G2", "pass", detail=detail)
    if float(mean) < th["g2_non_inferior_mean"]:
        return _result(
            "G2", "fail", [f"mean ΔIC {mean} < {th['g2_non_inferior_mean']}"], detail
        )
    return _result("G2", "fail", [f"t {t} < {th['g2_non_inferior_t']}"], detail)


# ── G3 稳定性 ──────────────────────────────────────────────────────────


def gate_g3_stability(
    evidence: Mapping[str, Any], thresholds: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """近 12 个月：最差月 ≥ champion 最差月 − 0.02；月间 std ≤ 1.5×champion。"""
    th = thresholds or DEFAULT_THRESHOLDS
    monthly = evidence.get("monthly")
    if not isinstance(monthly, Mapping):
        return _result("G3", "skip", ["无月度统计（monthly 缺失）"])
    c = monthly.get("challenger") or {}
    h = monthly.get("champion") or {}
    c_worst, h_worst = c.get("worst"), h.get("worst")
    c_std, h_std = c.get("month_std"), h.get("month_std")
    if c_worst is None or h_worst is None:
        return _result("G3", "skip", ["月度最差值缺失"])
    if c_std is None or h_std is None:
        return _result("G3", "skip", ["月间 std 不可算（月份不足 2 个）"])
    detail = {
        "challenger_worst": c_worst,
        "champion_worst": h_worst,
        "challenger_month_std": c_std,
        "champion_month_std": h_std,
        "worst_floor": float(h_worst) - th["g3_worst_slack"],
        "std_ceiling": th["g3_std_ratio"] * float(h_std),
    }
    reasons: list[str] = []
    if float(c_worst) < detail["worst_floor"]:
        reasons.append(
            f"挑战者最差月 {c_worst} < 冠军最差月 − {th['g3_worst_slack']}"
            f"（{detail['worst_floor']:.4f}）"
        )
    if float(c_std) > detail["std_ceiling"]:
        reasons.append(
            f"月间 std {c_std} > {th['g3_std_ratio']}×冠军（{detail['std_ceiling']:.4f}）"
        )
    if reasons:
        return _result("G3", "fail", reasons, detail)
    return _result("G3", "pass", detail=detail)


# ── G4 状态依赖 ────────────────────────────────────────────────────────


def gate_g4_regime(
    evidence: Mapping[str, Any], thresholds: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """无「冠军达标而挑战者崩坏」桶（桶内 ≥15 天、challenger ≤0 且 champion >0.01）。

    P3 落地 regime 数据前，``regime`` 键缺失 → skip 并标「未评估」。
    """
    th = thresholds or DEFAULT_THRESHOLDS
    regime = evidence.get("regime")
    if not isinstance(regime, Mapping):
        return _result("G4", "skip", ["regime 未评估（P3 前无硬数据）"])
    buckets = regime.get("buckets")
    if buckets is None:
        return _result("G4", "skip", ["regime 未评估（桶清单为空缺）"])
    flagged: list[dict[str, Any]] = []
    for b in buckets:
        if not isinstance(b, Mapping):
            continue
        n_days = b.get("n_days")
        c_ic, h_ic = b.get("challenger_mean_ic"), b.get("champion_mean_ic")
        if n_days is None or c_ic is None or h_ic is None:
            continue
        if (
            int(n_days) >= th["g4_min_days"]
            and float(c_ic) <= th["g4_challenger_ceiling"]
            and float(h_ic) > th["g4_champion_floor"]
        ):
            flagged.append(dict(b))
    detail = {"n_buckets": len(list(buckets)), "flagged": flagged}
    if flagged:
        return _result(
            "G4",
            "fail",
            [f"{len(flagged)} 个桶内挑战者崩坏（冠军达标）"],
            detail,
        )
    return _result("G4", "pass", detail=detail)


# ── G5 观察期 ──────────────────────────────────────────────────────────


def gate_g5_observation(
    evidence: Mapping[str, Any], thresholds: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """≥20 交易日：mean rank_ic ≥ max(0.005, 0.9×冠军同期)；coverage ≥ 0.9×冠军。"""
    th = thresholds or DEFAULT_THRESHOLDS
    obs = evidence.get("observation")
    if not isinstance(obs, Mapping):
        return _result("G5", "skip", ["观察期未开始（observation 缺失）"])
    n_days = obs.get("n_days")
    if obs.get("sufficient") is False or n_days is None:
        return _result("G5", "skip", [str(obs.get("reason") or "观察期证据不足")])
    if int(n_days) < th["g5_min_days"]:
        return _result(
            "G5",
            "skip",
            [f"观察期 {int(n_days)}/{int(th['g5_min_days'])} 未满"],
            {"n_days": int(n_days)},
        )

    c_ic, h_ic = obs.get("challenger_mean_ic"), obs.get("champion_mean_ic")
    c_cov, h_cov = obs.get("challenger_coverage"), obs.get("champion_coverage")
    if c_ic is None or h_ic is None or c_cov is None or h_cov is None:
        return _result("G5", "skip", ["观察期 IC/覆盖值缺失"])
    ic_floor = max(th["g5_ic_floor"], th["g5_champion_ic_ratio"] * float(h_ic))
    cov_floor = th["g5_coverage_ratio"] * float(h_cov)
    detail = {
        "n_days": int(n_days),
        "challenger_mean_ic": c_ic,
        "champion_mean_ic": h_ic,
        "ic_floor": ic_floor,
        "challenger_coverage": c_cov,
        "champion_coverage": h_cov,
        "coverage_floor": cov_floor,
    }
    reasons: list[str] = []
    if float(c_ic) < ic_floor:
        reasons.append(f"观察期 mean rank_ic {c_ic} < 地板 {ic_floor:.4f}")
    if float(c_cov) < cov_floor:
        reasons.append(f"覆盖率 {c_cov} < {th['g5_coverage_ratio']}×冠军（{cov_floor:.4f}）")
    if reasons:
        return _result("G5", "fail", reasons, detail)
    return _result("G5", "pass", detail=detail)


# ── G6 成本 ────────────────────────────────────────────────────────────


def gate_g6_cost(
    evidence: Mapping[str, Any], thresholds: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """估算年化换手 ≤ 1.3×champion（换手均值比，成本口径不影响比值本身）。"""
    th = thresholds or DEFAULT_THRESHOLDS
    turnover = evidence.get("turnover")
    if not isinstance(turnover, Mapping):
        return _result("G6", "skip", ["无换手证据（turnover 缺失）"])
    c = turnover.get("challenger") or {}
    h = turnover.get("champion") or {}
    if c.get("sufficient") is not True or h.get("sufficient") is not True:
        return _result("G6", "skip", ["换手样本不足（不足两个有效交易日）"])
    c_turn, h_turn = c.get("turnover_mean"), h.get("turnover_mean")
    if c_turn is None or h_turn is None:
        return _result("G6", "skip", ["换手均值缺失"])
    if float(h_turn) <= 0:
        return _result("G6", "skip", ["冠军零换手，比值无定义"], {"champ_turnover": h_turn})
    ratio = float(c_turn) / float(h_turn)
    detail = {
        "challenger_turnover": c_turn,
        "champion_turnover": h_turn,
        "ratio": round(ratio, 6),
        "ratio_ceiling": th["g6_turnover_ratio"],
    }
    if ratio > th["g6_turnover_ratio"]:
        return _result(
            "G6",
            "fail",
            [f"换手比 {ratio:.4f} > {th['g6_turnover_ratio']}"],
            detail,
        )
    return _result("G6", "pass", detail=detail)


# ── G7 提示（仅展示） ──────────────────────────────────────────────────


def gate_g7_trials(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """同 recipe 试次计数——只展示，防 p-hacking 决策偏差（不参与判定）。"""
    trials = evidence.get("trials")
    count = None
    if isinstance(trials, Mapping):
        count = trials.get("trial_count")
    note = "试次越多，选中「碰巧好」的挑战者概率越高（多重比较）"
    return _result("G7", "info", [note], {"trial_count": count})


# ── 总评 ───────────────────────────────────────────────────────────────


def evaluate_rollout(
    evidence: Mapping[str, Any],
    *,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """全部闸门一次算完 → ``{gates, summary, thresholds, enforced: False}``。

    ``thresholds`` 覆盖默认值（未知键直接报错——静默忽略会让「调了阈值」的
    假象留在现场）。``summary.verdict``：任一 fail → ``flagged``；否则有
    skip → ``incomplete``；全 pass（G7 info 不计）→ ``all_pass``。
    """
    th: dict[str, float] = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        unknown = sorted(set(thresholds) - set(DEFAULT_THRESHOLDS))
        if unknown:
            raise ValueError(f"未知阈值键: {unknown}（可选: {sorted(DEFAULT_THRESHOLDS)}）")
        th.update({k: float(v) for k, v in thresholds.items()})

    gates = [
        gate_g0_admission(evidence),
        gate_g1_independence(evidence),
        gate_g2_non_inferior(evidence, th),
        gate_g3_stability(evidence, th),
        gate_g4_regime(evidence, th),
        gate_g5_observation(evidence, th),
        gate_g6_cost(evidence, th),
        gate_g7_trials(evidence),
    ]
    counts = {"pass": 0, "fail": 0, "skip": 0, "info": 0}
    for g in gates:
        counts[g["status"]] = counts.get(g["status"], 0) + 1
    if counts["fail"]:
        verdict = "flagged"
    elif counts["skip"]:
        verdict = "incomplete"
    else:
        verdict = "all_pass"
    return {
        "gates": gates,
        "summary": {"counts": counts, "verdict": verdict, "enforced": False},
        "thresholds": th,
    }
