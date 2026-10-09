"""T-FB-16 单测：机构报告标量块装配（纯函数；无 DB / 无 HTTP）。

钉死的边：

- **同源锁**：装配出的每个统计量逐值等于 ``factor_report.metrics`` 直调结果
  （在报告模块里重写任何公式都会红）；
- **金样**：固定输入的输出与 ``tests/fixtures/factorReportGolden.json`` 一致
  （回归跳闸 + 数字可审阅；输入在 :func:`_golden_inputs`，改输入必须重生成）；
- **台账交叉**：``significance.nw_t`` 与台账 ``metrics.ic_nw_t`` 逐位相等
  （报告与评估期不许出现两套 NW 口径）;
- **族校正**：q 取 BY 校正后的**自身位**；自身位越界回落 n=1，不按位错配；
- **降级不出数字**：非完成终态/序列缺失 → ``available=False``，除
  status/reason/note 文本外块内无任何数字；
- 不变量：q ≥ p、bootstrap lo ≤ point ≤ hi、成本网格 net_return 随 bps 单调
  不增、盈亏平衡 = mean(ls)/mean(turnover)×1e4（手算锚）。
"""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import numpy as np
import pytest

rp = pytest.importorskip("backend.services.engine.factor_backtest.report")
from backend.services.engine.factor_report import metrics as M

_FIXTURE = Path(__file__).parent / "fixtures" / "factorReportGolden.json"


def _golden_inputs() -> tuple[dict, dict]:
    """确定性合成序列（金样生成器与用例共用；改这里必须重生成 fixture）。"""
    n = 120
    d0 = datetime.date(2024, 1, 2)
    dates = [str(d0 + datetime.timedelta(days=i)) for i in range(n)]
    ic = [
        0.04 + 0.15 * math.sin(i * 0.37) + 0.03 * math.cos(i * 0.11) for i in range(n)
    ]
    ls_ret = [0.0005 + 0.002 * math.sin(i * 0.23 + 1.0) for i in range(n)]
    long_ret = [0.0008 + 0.0025 * math.sin(i * 0.19 + 0.7) for i in range(n)]
    turnover = [0.3 + 0.15 * math.sin(i * 0.17) for i in range(n)]

    def _cum(rs: list[float]) -> list[float]:
        out, acc = [], 1.0
        for r in rs:
            acc *= 1.0 + r
            out.append(acc)
        return out

    series = {
        "dates": dates,
        "ic": ic,
        "ic_cum": _cum(ic),
        "nav_long": _cum(long_ret),
        "nav_ls": _cum(ls_ret),
        "nav_bench": _cum([0.0002] * n),
        "q_curves": {},
        "turnover": turnover,
        "coverage": [300] * n,
        "bench": "equal_weight",
        "meta": {
            "cost_bps": 10,
            "top_pct": 0.3,
            "n_buckets": 5,
            "turnover_convention": "daily_two_sided",
        },
    }
    run = {
        "run_id": "fb-golden",
        "factor_id": "f-golden",
        "status": "completed",
        "metrics": {"benchmark": "csi300"},
    }
    return run, series


def _series_arrays(series: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ic = np.asarray(series["ic"], dtype=np.float64)
    turnover = np.asarray(series["turnover"], dtype=np.float64)
    ls_daily = rp._nav_daily(series["nav_ls"])
    return ic, turnover, ls_daily


def _leaves(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def test_装配值逐位等于同源函数直调():
    run, series = _golden_inputs()
    block = rp.build_report_block(run, series)
    ic, turnover, ls_daily = _series_arrays(series)

    assert block["available"] is True
    assert block["headline"] == M.brain_headline(ls_daily, turnover)
    sig = block["significance"]
    assert sig["nw_t"] == M.nw_tstat(ic)
    assert sig["plain_t"] == M.plain_tstat(ic)
    assert sig["p_value"] == M.normal_pvalue(M.nw_tstat(ic))
    assert sig["bootstrap"] == M.bootstrap_ci(ic, stat="mean")
    assert sig["crowding"] == M.crowding_score(ic, turnover)
    assert block["cost_grid"] == M.cost_sensitivity(ls_daily, turnover)


def test_台账交叉_nw_t逐位一致():
    """评估期写进台账的 ic_nw_t 与报告层重算值必须逐位相等（同一函数同一序列）。"""
    run, series = _golden_inputs()
    ic, _, _ = _series_arrays(series)
    owed = M.nw_tstat(ic)
    run = {**run, "metrics": {**run["metrics"], "ic_nw_t": owed}}
    block = rp.build_report_block(run, series)
    assert block["significance"]["nw_t"] == run["metrics"]["ic_nw_t"] == owed


def test_族校正_自身位与越界回落():
    run, series = _golden_inputs()
    fam = [0.5, 2.5, 1.2, -0.3]
    block = rp.build_report_block(run, series, family_nw_t=fam, self_index=1)
    expected_q = float(M.bhy_qvalues([M.normal_pvalue(t) for t in fam])[1])
    sig = block["significance"]
    assert sig["q_value_bhy"] == expected_q
    assert sig["family_n"] == 4
    assert sig["n_trials"] == 4 and sig["n_trials_source"] == "batch_completed_units"
    assert "BY" in sig["family_note"]

    # 自身位越界 → 回落 n=1（绝不按位拿别的单元的 q）
    fallback = rp.build_report_block(run, series, family_nw_t=fam, self_index=9)
    fsig = fallback["significance"]
    assert fsig["family_n"] == 1
    assert fsig["q_value_bhy"] == fsig["p_value"]
    assert "未做多重校正" in fsig["family_note"]


def test_参数覆盖试次数与DSR同源():
    run, series = _golden_inputs()
    block = rp.build_report_block(run, series, n_trials=8, n_trials_source="param")
    sig = block["significance"]
    assert sig["n_trials"] == 8 and sig["n_trials_source"] == "param"
    _, _, ls_daily = _series_arrays(series)
    skew, kurt = rp._skew_kurt_of(ls_daily)
    h = block["headline"]
    assert sig["dsr"] == M.deflated_sharpe(h["ir"], 8, h["n_days"], skew, kurt)
    # 无族无覆盖：单 run 默认 1 / default_single
    solo = rp.build_report_block(run, series)
    assert solo["significance"]["n_trials"] == 1
    assert solo["significance"]["n_trials_source"] == "default_single"


def test_降级终态不出数字():
    for status in (
        "failed",
        "data_unsupported",
        "insufficient",
        "unavailable",
        "cancelled",
        "running",
    ):
        run = {"run_id": "r", "status": status, "error": f"{status}_reason"}
        block = rp.build_report_block(run, None)
        assert block["available"] is False
        assert block["status"] == status
        assert block["reason"] == f"{status}_reason"
        assert (
            "headline" not in block
            and "cost_grid" not in block
            and "n_days" not in block
        )
        for leaf in _leaves({k: v for k, v in block.items() if k != "available"}):
            assert isinstance(leaf, str) or leaf is None, f"降级块不得含数字: {leaf!r}"
    # error 为空 → reason 兜底非空（不许 None 空白出门）
    block = rp.build_report_block({"run_id": "r", "status": "insufficient"}, None)
    assert block["reason"]


def test_完成但序列缺失():
    block = rp.build_report_block(
        {"run_id": "r", "status": "completed", "metrics": {}}, None
    )
    assert block["available"] is False
    assert block["reason"] == "series_not_stored"
    empty = rp.build_report_block(
        {"run_id": "r", "status": "completed", "metrics": {}}, {"dates": []}
    )
    assert empty["available"] is False


def test_不变量_单调与手算锚():
    run, series = _golden_inputs()
    block = rp.build_report_block(run, series)
    boot = block["significance"]["bootstrap"]
    assert boot["lo"] <= boot["point"] <= boot["hi"]

    rows = block["cost_grid"]["rows"]
    nets = [r["net_return"] for r in rows]
    assert all(nets[i] >= nets[i + 1] for i in range(len(nets) - 1))

    _, turnover, ls_daily = _series_arrays(series)
    assert block["cost_grid"]["break_even_bps"] == pytest.approx(
        float(ls_daily.mean() / turnover.mean() * 1e4), rel=1e-12
    )

    # q ≥ p（BY 构造逐点不小于原始 p）
    sig = rp.build_report_block(run, series, family_nw_t=[0.5, 2.5, 1.2], self_index=1)[
        "significance"
    ]
    assert sig["q_value_bhy"] >= sig["p_value"] - 1e-12


def test_超额标注_等权兜底不冒充指数():
    run, series = _golden_inputs()
    ex = rp.build_report_block(run, series)["excess"]
    assert ex["kind"] == "equal_weight"
    assert ex["benchmark_ref"] == "csi300"
    assert "等权" in ex["note"] and "指数" in ex["note"]
    # T-FB-19：请求了真实指数但读数失败——措辞必须写明「已回落」与请求的是什么
    assert "沪深300 指数" in ex["note"]
    assert "回落" in ex["note"]


@pytest.mark.parametrize(
    ("bench", "ref", "label"),
    [
        ("csi300", "000300.SH", "沪深300 指数"),
        ("hsi", "HSI.HK", "恒生指数"),
        ("spx", "SPX.US", "标普500 指数"),
    ],
)
def test_超额标注_真实指数点名(bench, ref, label):
    """T-FB-19：series.bench 是真实指数时如实点名（中文名 + 指数代码 + 数据出处）。"""
    run, series = _golden_inputs()
    run = {**run, "metrics": {**run["metrics"], "benchmark": bench}}
    series = {**series, "bench": bench}
    ex = rp.build_report_block(run, series)["excess"]
    assert ex["kind"] == bench
    assert ex["benchmark_ref"] == ref
    assert ex["label"] == label
    assert label in ex["note"] and ref in ex["note"]
    assert "QuantDB" in ex["note"]


def test_超额标注_无指数市场等权不称回落():
    """crypto/futures：档案声明即等权——是「未接入」，不是「回落」，措辞不许混。"""
    run, series = _golden_inputs()
    run = {**run, "metrics": {**run["metrics"], "benchmark": "equal_weight"}}
    ex = rp.build_report_block(run, series)["excess"]
    assert ex["kind"] == "equal_weight"
    assert ex["benchmark_ref"] == "equal_weight"
    assert "该市场未接入指数序列" in ex["note"]
    assert "回落" not in ex["note"]


def test_unavailable清单写明缺什么():
    run, series = _golden_inputs()
    blocks = rp.build_report_block(run, series)["unavailable"]
    assert {b["block"] for b in blocks} == {
        "capacity",
        "holding_period",
        "ic_half_life",
        "style_attribution",
    }
    for b in blocks:
        assert b["reason"], "每条暂缺都必须写明缺什么输入"


def test_确定性_两次装配一致():
    run, series = _golden_inputs()
    assert rp.build_report_block(run, series) == rp.build_report_block(run, series)


# ── 金样数值比对 ─────────────────────────────────────────────────────


def _load_golden() -> dict:
    if not _FIXTURE.exists():
        pytest.fail(f"金样缺失: {_FIXTURE}（从 _golden_inputs 重新生成）")
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _assert_close(got, want, path: str = "<root>"):
    if isinstance(want, dict):
        assert isinstance(got, dict), f"{path}: 类型不符"
        for k, v in want.items():
            assert k in got, f"{path}.{k} 缺失"
            _assert_close(got[k], v, f"{path}.{k}")
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), f"{path}: 列表不符"
        for i, v in enumerate(want):
            _assert_close(got[i], v, f"{path}[{i}]")
    elif isinstance(want, bool) or want is None or isinstance(want, str):
        assert got == want, f"{path}: {got!r} != {want!r}"
    else:
        assert got == pytest.approx(want, rel=1e-12, abs=1e-15), (
            f"{path}: {got!r} != {want!r}"
        )


def test_金样数值比对():
    golden = _load_golden()
    run = golden["run"]
    series = golden["input"]
    block = rp.build_report_block(run, series)
    _assert_close(block, golden["expected"], "block")

    fam_block = rp.build_report_block(
        run,
        series,
        family_nw_t=golden["family_nw_t"],
        self_index=golden["self_index"],
        n_trials=golden["n_trials"],
        n_trials_source="param",
    )
    _assert_close(fam_block, golden["expected_family"], "family_block")


@pytest.mark.xfail(
    reason=(
        "已知缺陷（2026-10-10 发现，metrics_eval.deflated_sharpe 多重校正项"
        "多乘了一次 √(T-1)）：z 应为 SR·√(T-1)/√denom − e_max，现实现为"
        "(SR − e_max)·√(T-1)/√denom。n_trials>1 时现实现把 e_max 放大约 "
        "√(T-1)≈16..27 倍，任何现实输入 DSR 恒塌到 ≈0（实测 TB 数据 "
        "1273 个标的 crypto 强信号 z 从 -0.71 变 -24.43）。修复 metrics_eval "
        "后本用例转 xpass，届时重生成金样并移除本标记。"
    ),
    strict=False,
)
def test_DSR规范式对照_已知缺陷():
    """独立实现 Bailey-LdP 规范式（不调 metrics_eval），与装配值对照。

    规范式：z = SR·√(T-1)/√(1 − γ₃·SR + (γ₄−1)/4·SR²) − e_max，
    e_max = (1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))。
    """
    from statistics import NormalDist

    run, series = _golden_inputs()
    block = rp.build_report_block(run, series, n_trials=8, n_trials_source="param")
    h = block["headline"]
    _, _, ls_daily = _series_arrays(series)
    skew, kurt = rp._skew_kurt_of(ls_daily)

    sr = h["ir"] / math.sqrt(252.0)
    nd = NormalDist()
    g = 0.5772156649015329
    e_max = (1 - g) * nd.inv_cdf(1 - 1 / 8) + g * nd.inv_cdf(1 - 1 / (8 * math.e))
    denom = math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr * sr)
    z = sr * math.sqrt(h["n_days"] - 1) / denom - e_max
    assert block["significance"]["dsr"] == pytest.approx(nd.cdf(z), abs=1e-9)
