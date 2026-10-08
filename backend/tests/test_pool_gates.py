"""物化门禁插件（P1）——软告警默认、可逐条升级硬拦。

用户裁决（写死在测试里）：**只有既有的 |ρ|≥0.9 值级查重保持硬拒**（不在本插件集，
原逻辑原样保留）；本插件的五个门禁默认全 **soft**——失败只记录不拦截。
升级路径：yaml 逐条 `mode: hard`，或 env `QM_MINING_GATES_MODE=strict` 全局升级，
`QM_MINING_GATES_DISABLED=key1,key2` 逐条关闭。

硬 ICIR 阈值的历史教训（memory: model-ic-is-size-regime-bet）在门外候着：
ICIR 的分母是族常数，默认硬拦会不可逆地批量拒绝存量因子。

第三条纪律：**指标缺失 = skipped，不判不拦**——缺失按 0 判等于把「没算过」
当「算出来很差」（显示面的同一纪律：缺失一律「—」，见 researchScore）。
"""

from __future__ import annotations

import pytest

from backend.services.engine.mining_plugins.base import GateContext
from backend.services.engine.mining_plugins.registry import get_registry

GOOD_METRICS = {
    "pfs": 0.95,
    "rre": 0.8,
    "ann_turnover": 12.0,
    "ann_return_net": 0.14,
    "ic": 0.03,
}


def _ctx(**overrides) -> GateContext:
    metrics = dict(GOOD_METRICS)
    metrics.update(overrides.pop("metrics", {}))
    return GateContext(
        factor_id="fid",
        market="a_share",
        universe="csi300",
        metrics=metrics,
        pool_ic_pct=overrides.pop("pool_ic_pct", 0.7),
        **overrides,
    )


def _run(ctx):
    return get_registry().run_gates(ctx)


def _by_key(outcomes):
    return {o.key: o for o in outcomes}


# ── 注册面 ────────────────────────────────────────────────────────
def test_builtin_gates_registered_with_soft_defaults():
    names = get_registry().gate_names()
    assert set(names) == {
        "pfs_floor",
        "rre_floor",
        "ic_pool_pct",
        "turnover_cap",
        "net_return_floor",
    }
    for name in names:
        desc = get_registry().get_gate(name).descriptor
        assert desc.default_mode == "soft", f"{name} 默认必须是软告警（用户裁决）"


# ── 放行 / 软告警 / 硬拒 ──────────────────────────────────────────
def test_all_good_metrics_pass():
    decision = _run(_ctx())
    assert decision.rejected is False
    assert {o.status for o in decision.outcomes} == {"pass"}


def test_soft_failure_records_warn_but_does_not_reject():
    decision = _run(_ctx(metrics={"pfs": 0.5}))
    pfs = _by_key(decision.outcomes)["pfs_floor"]
    assert pfs.status == "fail"
    assert pfs.mode == "soft"
    assert pfs.observed == pytest.approx(0.5)
    assert pfs.threshold == pytest.approx(0.9)
    assert "0.5000" in pfs.message and "0.9000" in pfs.message
    assert decision.rejected is False


def test_yaml_hard_mode_rejects():
    decision = _run_hard("pfs_floor")
    assert decision.rejected is True
    assert _by_key(decision.outcomes)["pfs_floor"].mode == "hard"


def test_env_strict_upgrades_everything(monkeypatch):
    monkeypatch.setenv("QM_MINING_GATES_MODE", "strict")
    decision = _run(_ctx(metrics={"pfs": 0.5, "rre": 0.1, "ann_turnover": 500.0}))
    assert decision.rejected is True
    assert all(o.mode == "hard" for o in decision.outcomes if o.status == "fail")


def test_env_disabled_drops_gate(monkeypatch):
    monkeypatch.setenv("QM_MINING_GATES_DISABLED", "pfs_floor,rre_floor")
    decision = _run(_ctx(metrics={"pfs": 0.1, "rre": 0.1}))
    keys = {o.key for o in decision.outcomes}
    assert "pfs_floor" not in keys and "rre_floor" not in keys
    assert decision.rejected is False


# ── 缺失 = skipped，不判不拦（硬模式也不拦）───────────────────────
def test_missing_metric_is_skipped_even_in_hard_mode(monkeypatch):
    monkeypatch.setenv("QM_MINING_GATES_MODE", "strict")
    decision = _run(_ctx(metrics={"pfs": None}, pool_ic_pct=None))
    by_key = _by_key(decision.outcomes)
    assert by_key["pfs_floor"].status == "skipped"
    assert by_key["pfs_floor"].message  # 有理由说明
    assert by_key["ic_pool_pct"].status == "skipped"
    assert decision.rejected is False


# ── 五个门禁各自的判定面 ──────────────────────────────────────────
def test_rre_floor_fails_below_threshold():
    decision = _run(_ctx(metrics={"rre": 0.3}))
    assert _by_key(decision.outcomes)["rre_floor"].status == "fail"


def test_ic_pool_pct_uses_pool_percentile():
    decision = _run(_ctx(pool_ic_pct=0.1))
    assert _by_key(decision.outcomes)["ic_pool_pct"].status == "fail"
    decision2 = _run(_ctx(pool_ic_pct=0.5))
    assert _by_key(decision2.outcomes)["ic_pool_pct"].status == "pass"


def test_turnover_cap_fails_above_threshold():
    decision = _run(_ctx(metrics={"ann_turnover": 112.86}))
    assert _by_key(decision.outcomes)["turnover_cap"].status == "fail"
    assert _by_key(decision.outcomes)["turnover_cap"].observed == pytest.approx(112.86)


def test_net_return_floor_fails_when_negative():
    decision = _run(_ctx(metrics={"ann_return_net": -0.02}))
    assert _by_key(decision.outcomes)["net_return_floor"].status == "fail"
    ok = _run(_ctx(metrics={"ann_return_net": 0.0}))
    assert _by_key(ok.outcomes)["net_return_floor"].status == "pass"


# ── 阈值可配 ─────────────────────────────────────────────────────
def test_yaml_threshold_override():
    decision = _run_with_config(
        {"gates": {"pfs_floor": {"threshold": 0.99}}}, ctx=_ctx(metrics={"pfs": 0.95})
    )
    pfs = _by_key(decision.outcomes)["pfs_floor"]
    assert pfs.status == "fail"
    assert pfs.threshold == pytest.approx(0.99)


def test_yaml_disable_via_enabled_false():
    decision = _run_with_config({"gates": {"turnover_cap": {"enabled": False}}})
    assert "turnover_cap" not in {o.key for o in decision.outcomes}


# ── 输出形状（落 metadata / manifest 用）──────────────────────────
def test_decision_to_dict_shape():
    payload = _run(_ctx()).to_dict()
    assert set(payload) == {"rejected", "gates"}
    first = payload["gates"][0]
    assert set(first) == {
        "key",
        "label",
        "mode",
        "status",
        "message",
        "observed",
        "threshold",
    }


# ── helpers ──────────────────────────────────────────────────────
def _run_with_config(cfg: dict, ctx: GateContext | None = None):
    """写临时 yaml → QM_MINING_PLUGINS_CONFIG 指过去 → 跑完还原。"""
    import os
    import tempfile
    from pathlib import Path

    import yaml

    old = os.environ.get("QM_MINING_PLUGINS_CONFIG")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plugins.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        os.environ["QM_MINING_PLUGINS_CONFIG"] = str(path)
        try:
            return _run(ctx if ctx is not None else _ctx())
        finally:
            if old is None:
                os.environ.pop("QM_MINING_PLUGINS_CONFIG", None)
            else:
                os.environ["QM_MINING_PLUGINS_CONFIG"] = old


def _run_hard(key: str):
    """硬模式 + 必失败指标（pfs=0.5 < 0.9），验证 reject 只因 hard 而真。"""
    return _run_with_config(
        {"gates": {key: {"mode": "hard"}}}, ctx=_ctx(metrics={"pfs": 0.5})
    )
