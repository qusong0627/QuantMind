"""T-MV-09：机构级验证流水线——报告装配纯函数（TDD 先红后绿）。

契约（框架 §4 T-MV-09）：
1. **六节装配**：IC/ICIR、衰减（多视界 IC + 半衰期）、换手、分组单调、
   正交增量（T-MV-08 留痕只读透传）、PIT（截断不变性探针判定）；
   另设 ``tfb_run_id`` 链回回测台账（T-MV-10 排名消费）。
2. **诚实降级**：缺 T-FB 运行 / 缺 h5 统计 / 缺序列 / 缺正交留痕 / 缺探针，
   每节显式 ``status`` + ``reason``，绝不以 0 冒充数字；顶层
   ``completed`` ↔ ``degraded`` 仅由「是否存在 unavailable 节」裁决。
3. **单源复用**：半衰期与分组单调直接调 ``factor_report.metrics``
   （``half_life_days`` / ``monotonicity``）——与 T-FB 报告同一纯函数层，
   不复制实现；缺视界**跳过而非当 0**（把「未测」当「已衰减」会出假半衰期）。
4. **JSON 安全**：一切 NaN/Inf → None（JSON 无 NaN），``json.dumps`` 必过。
5. **PIT 判定语义**：截断在探针日（去掉其后全部数据）重算，探针日截面值
   逐点差 ≤ 容差（1e-9 绝对）→ pass；任何点超容差 → fail（值依赖未来数据）；
   可用共同标的不足 → unknown——**不当 pass**（未知 ≠ 通过）。
6. **验证落盘**：专表 ``rd_agent_factor_validations`` 状态机
   running→completed/degraded/failed；``start`` 重置整行（旧报告清空）、
   收口只认 running（幂等）；``report_json`` 序列化 ``ensure_ascii=False``，
   读侧坏 JSON 降级 None 不炸调用方。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.mining_plugins import validation as V  # noqa: E402
from backend.services.engine.mining_plugins import validation_store as VS  # noqa: E402


# ── 合成工具 ─────────────────────────────────────────────────────────


def _stats(ic: float, *, n_days: int = 60, icir: float | None = None) -> dict:
    """daily_ic_stats 形状的最小合成（只用验证层消费的字段）。"""
    return {
        "ic": ic,
        "rank_ic": ic,
        "icir": icir if icir is not None else round(ic * 8, 6),
        "rank_icir": ic * 8,
        "n_obs": n_days * 100,
        "ic_std": 0.02,
        "n_days": n_days,
        "ic_positive_rate": 0.55,
        "ic_nw_t": 2.0,
    }


def _nav(rate: float, days: int = 25) -> list[float]:
    """按日收益率 rate 复利的累计净值列表（探针只需形状与单调性）。"""
    out = [1.0]
    for _ in range(days - 1):
        out.append(out[-1] * (1.0 + rate))
    return out


def _series(q_rates: dict[str, float], *, turnover: list | None = None) -> dict:
    """T-FB 序列载荷的最小合成：q_curves 为累计净值。"""
    return {
        "dates": [f"2026-01-{i:02d}" for i in range(1, 26)],
        "q_curves": {k: _nav(r) for k, r in q_rates.items()},
        "turnover": turnover if turnover is not None else [0.4, 0.5, 0.6],
        "meta": {"cost_bps": 20, "n_buckets": len(q_rates)},
    }


def _canon(days: list[str], syms: list[str], values: np.ndarray) -> pd.Series:
    """(trade_date, symbol) 两层索引的规范口径因子值。"""
    nd, ns = values.shape
    return pd.Series(
        values.reshape(-1),
        index=pd.MultiIndex.from_arrays(
            [np.repeat(days, ns), np.tile(syms, nd)],
            names=["trade_date", "symbol"],
        ),
    )


def _tfb_ok(**over) -> dict:
    base = {
        "run_id": "fb-abc123",
        "ok": True,
        "status": "ok",
        "metrics": {"ic": 0.041, "rank_ic": 0.038, "icir": 0.52},
        "window": {"start": "2025-10-10", "end": "2026-10-10"},
        "universe": "csi300",
        "reason": None,
    }
    base.update(over)
    return base


def _h5_ic(**over) -> dict:
    base = {
        "stats_by_horizon": {1: _stats(0.05), 2: _stats(0.04), 5: _stats(0.02)},
        "window": {"start": "2025-01-06", "end": "2026-10-10"},
        "universe": "csi300",
    }
    base.update(over)
    return base


# ── 衰减节 ───────────────────────────────────────────────────────────


class TestDecaySection:
    def test_curve_sorted_and_half_life_interpolated(self):
        """0.10/0.08/0.05/0.04：|ic|≤0.05 最早出现在 h=5 → 半衰期 5.0。"""
        section = V.decay_section(
            {
                1: _stats(0.10),
                2: _stats(0.08),
                5: _stats(0.05),
                10: _stats(0.04),
            }
        )

        assert section["status"] == "ok"
        assert list(section["curve"]) == ["1", "2", "5", "10"]
        assert section["curve"]["5"] == pytest.approx(0.05)
        assert section["half_life_days"] == pytest.approx(5.0)
        assert section["n_days_by_horizon"]["1"] == 60

    def test_half_life_linear_interpolation_between_horizons(self):
        """0.10/0.08/0.04：穿越 0.05 在 2~5 之间 → 2 + (0.08-0.05)/(0.08-0.04)*3。"""
        section = V.decay_section(
            {1: _stats(0.10), 2: _stats(0.08), 5: _stats(0.04), 10: _stats(0.02)}
        )

        assert section["half_life_days"] == pytest.approx(2 + 0.75 * 3)

    def test_missing_horizon_skipped_not_zero(self):
        """缺 2/5 视界（未测）跳过不算 0：若当 0 会在 h=2 报半衰期 1.5；

        单源口径在已测视界间线性插值 → 1 + (0.10−0.05)/(0.10−0.01)×9 = 6.0。
        """
        section = V.decay_section({1: _stats(0.10), 10: _stats(0.01)})

        assert section["status"] == "ok"
        assert list(section["curve"]) == ["1", "10"]
        assert section["half_life_days"] == pytest.approx(6.0)

    def test_no_decay_to_half_gives_none_with_note(self):
        section = V.decay_section({1: _stats(0.10), 5: _stats(0.09)})

        assert section["status"] == "ok"
        assert section["half_life_days"] is None
        assert "未衰减" in section["note"]

    def test_zero_sample_horizons_filtered(self):
        """n_days=0 的视界 = 无样本，不得当 IC=0 参与曲线。"""
        section = V.decay_section({1: _stats(0.10), 2: _stats(0.0, n_days=0)})

        assert list(section["curve"]) == ["1"]
        assert section["half_life_days"] is None, "样本为 0 的视界不得冒充衰减"

    def test_none_and_empty_degrade(self):
        assert V.decay_section(None)["status"] == "unavailable"
        assert V.decay_section(None)["reason"] == "no_h5_stats"
        empty = V.decay_section({1: _stats(0.0, n_days=0)})
        assert empty["status"] == "unavailable"
        assert empty["reason"] == "no_h5_samples"


# ── 换手节 ───────────────────────────────────────────────────────────


class TestTurnoverSection:
    def test_two_sided_mean_and_one_side_convention(self):
        section = V.turnover_section(_series({}, turnover=[0.4, 0.5, None, 0.6]))

        assert section["status"] == "ok"
        assert section["daily_two_sided_mean"] == pytest.approx(0.5)
        assert section["one_side_mean"] == pytest.approx(0.25)
        assert section["n_days"] == 3
        assert section["convention"] == "daily_two_sided"

    def test_missing_series_unavailable(self):
        assert V.turnover_section(None)["reason"] == "no_series"
        no_turn = V.turnover_section({"dates": ["2026-01-01"]})
        assert no_turn["status"] == "unavailable"
        assert no_turn["reason"] == "no_series"


# ── 分组单调节 ───────────────────────────────────────────────────────


class TestMonotonicitySection:
    def test_increasing_buckets_give_plus_one(self):
        section = V.monotonicity_section(
            _series({"q1": 0.0, "q2": 0.001, "q3": 0.002, "q4": 0.003, "q5": 0.004})
        )

        assert section["status"] == "ok"
        assert section["value"] == pytest.approx(1.0)
        assert section["n_buckets"] == 5
        assert section["bucket_means"][0] == pytest.approx(0.0)
        assert section["bucket_means"][-1] == pytest.approx(0.004)

    def test_inverted_buckets_give_minus_one(self):
        section = V.monotonicity_section(
            _series({"q1": 0.004, "q2": 0.003, "q3": 0.002, "q4": 0.001, "q5": 0.0})
        )

        assert section["value"] == pytest.approx(-1.0)
        assert "q1 最低" in section["note"]

    def test_bucket_keys_sorted_numerically_not_lexicographically(self):
        """q10 必须排在 q2 之后（字典序会把它排到 q2 前 → 静默错序）。"""
        section = V.monotonicity_section(
            _series({"q1": 0.0, "q2": 0.001, "q10": 0.002})
        )

        assert section["status"] == "ok"
        assert section["q_order"] == ["q1", "q2", "q10"]
        assert section["value"] == pytest.approx(1.0), "按数值序应为递增"

    def test_none_bucket_makes_monotonicity_unavailable(self):
        """某桶全 None（如停牌）→ 均值缺失：不准按剩余桶给单调性。"""
        series = _series(
            {"q1": 0.0, "q2": 0.001, "q3": 0.002, "q4": 0.003, "q5": 0.004}
        )
        series["q_curves"]["q3"] = [None] * 25

        section = V.monotonicity_section(series)

        assert section["status"] == "unavailable"
        assert section["reason"] == "degenerate_buckets"
        assert section["bucket_means"][2] is None

    def test_fewer_than_three_buckets_unavailable(self):
        section = V.monotonicity_section(_series({"q1": 0.0, "q5": 0.004}))

        assert section["status"] == "unavailable"
        assert section["reason"] == "too_few_buckets"

    def test_missing_series_unavailable(self):
        assert V.monotonicity_section(None)["reason"] == "no_series"


# ── IC 节 ────────────────────────────────────────────────────────────


class TestIcSection:
    def test_prefers_tfb_run_metrics(self):
        section = V.ic_section(_tfb_ok(), _h5_ic())

        assert section["status"] == "ok"
        assert section["source"] == "tfb_run"
        assert section["run_id"] == "fb-abc123"
        assert section["metrics"]["icir"] == pytest.approx(0.52)
        assert section["window"] == {"start": "2025-10-10", "end": "2026-10-10"}
        assert section["universe"] == "csi300"

    def test_falls_back_to_h5_h1_when_tfb_failed(self):
        tfb = _tfb_ok(ok=False, status="failed", metrics=None, reason="syntax_error")
        section = V.ic_section(tfb, _h5_ic())

        assert section["status"] == "ok"
        assert section["source"] == "h5_values"
        assert section["metrics"]["ic"] == pytest.approx(0.05)
        assert "syntax_error" in section["note"], "降级注必须写明 T-FB 失效原因"

    def test_neither_source_unavailable(self):
        section = V.ic_section(None, None)

        assert section["status"] == "unavailable"
        assert section["reason"] == "no_ic_source"

    def test_tfb_ok_but_metrics_empty_still_falls_back(self):
        tfb = _tfb_ok(metrics={})
        section = V.ic_section(tfb, _h5_ic())

        assert section["source"] == "h5_values"

    def test_non_numeric_metrics_pass_through(self):
        """溯源类字符串字段（bench_used/data_source）不得被 _num 抹成 None。

        引擎契约「metrics.bench_used 只记实际用上的口径，绝不冒充指数」——
        报告引用后再丢口径，审计面就断链了。
        """
        tfb = _tfb_ok(
            metrics={
                "ic": 0.041,
                "rank_ic": float("nan"),
                "benchmark": "csi300",
                "bench_used": "equal_weight",
                "data_source": "qlib_bin",
                "window": "2023-10-09~2026-10-09",
                "evaluators": {"gate": {"pass": True}},  # 容器照旧丢弃（可含 NaN）
            }
        )
        section = V.ic_section(tfb, None)

        assert section["metrics"]["ic"] == pytest.approx(0.041)
        assert section["metrics"]["rank_ic"] is None, "数值 NaN 仍归 None（JSON 铁律）"
        assert section["metrics"]["benchmark"] == "csi300"
        assert section["metrics"]["bench_used"] == "equal_weight"
        assert section["metrics"]["data_source"] == "qlib_bin"
        assert section["metrics"]["window"] == "2023-10-09~2026-10-09"
        assert section["metrics"]["evaluators"] is None


# ── 正交增量节 ───────────────────────────────────────────────────────


class TestOrthogonalitySection:
    def _trace(self) -> dict:
        return {
            "status": "ok",
            "residual_ic": 0.031,
            "candidate_ic": 0.044,
            "max_parent_abs_corr": 0.12,
            "threshold": 0.3,
            "orthogonal": True,
            "n_days": 80,
            "n_obs": 8000,
            "parents": [
                {"factor_id": "p1", "name": "父本一", "corr": 0.12},
            ],
            "evaluated_at": "2026-10-10T00:00:00Z",
        }

    def test_trace_passthrough(self):
        section = V.orthogonality_section(self._trace())

        assert section["status"] == "ok"
        assert section["residual_ic"] == pytest.approx(0.031)
        assert section["candidate_ic"] == pytest.approx(0.044)
        assert section["max_parent_abs_corr"] == pytest.approx(0.12)
        assert section["orthogonal"] is True
        assert section["parents"][0]["factor_id"] == "p1"
        assert section["evaluated_at"] == "2026-10-10T00:00:00Z"

    def test_absent_trace_unavailable_mentions_pool_refresh(self):
        section = V.orthogonality_section(None)

        assert section["status"] == "unavailable"
        assert section["reason"] == "no_trace"
        assert "池刷新" in section["note"]

    def test_no_parents_status_passthrough(self):
        section = V.orthogonality_section({"status": "no_parents"})

        assert section["status"] == "no_parents"
        assert section["orthogonal"] is None

    @pytest.mark.parametrize("bad_status", [None, "", "   "])
    def test_missing_status_not_fabricated_ok(self, bad_status):
        """缺 status 的留痕不得被 ``or "ok"`` 洗成「已评估」（未知 ≠ 通过）。"""
        trace = {
            "status": bad_status,
            "residual_ic": 0.03,
            "evaluated_at": "2026-10-10T00:00:00Z",
        }
        section = V.orthogonality_section(trace)

        assert section["status"] == "unavailable"
        assert section["reason"] == "missing_status"

    def test_nan_values_become_none(self):
        trace = self._trace()
        trace["residual_ic"] = float("nan")
        section = V.orthogonality_section(trace)

        assert section["residual_ic"] is None


# ── PIT 探针判定 ─────────────────────────────────────────────────────


class TestJudgeTruncation:
    DAYS = [f"2026-01-{i:02d}" for i in range(1, 11)]
    SYMS = [f"s{j:03d}" for j in range(25)]  # > PIT_MIN_COMMON(20)，默认门槛可生效
    PROBE = "2026-01-08"

    def _pair(self, *, tamper=None):
        rng = np.random.default_rng(7)
        vals = rng.normal(size=(len(self.DAYS), len(self.SYMS)))
        full = _canon(self.DAYS, self.SYMS, vals)
        truncated = _canon(self.DAYS, self.SYMS, vals.copy())
        if tamper is not None:
            truncated.loc[(self.PROBE, self.SYMS[tamper[0]])] += tamper[1]
        return full, truncated

    def test_identical_values_pass_with_zero_diff(self):
        full, truncated = self._pair()
        verdict = V.judge_truncation(full, truncated, probe_date=self.PROBE)

        assert verdict["status"] == "pass"
        assert verdict["n_diff"] == 0
        assert verdict["max_abs_diff"] == pytest.approx(0.0)
        assert verdict["n_common"] == len(self.SYMS)
        assert verdict["probe_date"] == self.PROBE

    def test_any_tolerated_diff_fails_with_counts(self):
        """1e-6 的探针日差 = 值依赖未来数据（截断重算变了）→ fail。"""
        full, truncated = self._pair(tamper=(3, 1e-6))
        verdict = V.judge_truncation(full, truncated, probe_date=self.PROBE)

        assert verdict["status"] == "fail"
        assert verdict["n_diff"] == 1
        assert verdict["max_abs_diff"] == pytest.approx(1e-6)
        assert verdict["reason"] is None

    def test_below_tolerance_does_not_fail(self):
        full, truncated = self._pair(tamper=(3, 1e-12))
        verdict = V.judge_truncation(full, truncated, probe_date=self.PROBE)

        assert verdict["status"] == "pass"

    def test_diff_at_other_dates_ignored(self):
        """只有探针日截面参与判定——其他日期截断本就应无值/无约束。"""
        full, truncated = self._pair()
        truncated.loc[("2026-01-01", self.SYMS[0])] += 5.0
        verdict = V.judge_truncation(full, truncated, probe_date=self.PROBE)

        assert verdict["status"] == "pass"

    def test_insufficient_common_unknown_not_pass(self):
        full, truncated = self._pair()
        keeper = self.SYMS[:2]
        truncated = truncated[truncated.index.get_level_values(1).isin(keeper)]
        verdict = V.judge_truncation(
            full, truncated, probe_date=self.PROBE, min_common=5
        )

        assert verdict["status"] == "unknown"
        assert verdict["reason"] == "insufficient_common_symbols"
        assert verdict["n_common"] == 2

    def test_no_probe_cross_section_unknown(self):
        full, truncated = self._pair()
        verdict = V.judge_truncation(full, truncated, probe_date="2026-02-01")

        assert verdict["status"] == "unknown"
        assert verdict["n_common"] == 0


class TestPitSection:
    def test_verdict_passthrough_with_mechanism_note(self):
        verdict = {
            "status": "fail",
            "probe_date": "2026-09-01",
            "max_abs_diff": 0.002,
            "n_common": 280,
            "n_diff": 12,
            "tol": 1e-9,
            "reason": None,
        }
        section = V.pit_section(verdict)

        assert section["status"] == "fail"
        assert section["n_diff"] == 12
        assert "依赖未来数据" in section["note"]
        assert "截断" in section["note"]

    def test_none_verdict_unavailable(self):
        section = V.pit_section(None)

        assert section["status"] == "unavailable"
        assert section["reason"] == "probe_not_run"


# ── 报告装配 ─────────────────────────────────────────────────────────


class TestBuildValidationReport:
    def _full_bundle(self) -> dict:
        return {
            "factor_id": "f" * 32,
            "market": "a_share",
            "generated_at": "2026-10-10T06:00:00Z",
            "tfb": _tfb_ok(),
            "h5_ic": _h5_ic(),
            "series": _series(
                {"q1": 0.0, "q2": 0.001, "q3": 0.002, "q4": 0.003, "q5": 0.004}
            ),
            "orthogonality": {
                "status": "ok",
                "residual_ic": 0.03,
                "orthogonal": True,
            },
            "pit": {
                "status": "pass",
                "probe_date": "2026-09-01",
                "max_abs_diff": 0.0,
                "n_common": 280,
                "n_diff": 0,
                "tol": 1e-9,
                "reason": None,
            },
        }

    def test_happy_path_all_sections_no_unavailable(self):
        report = V.build_validation_report(**self._full_bundle())

        assert report["status"] == "completed"
        assert report["tfb_run_id"] == "fb-abc123"
        assert set(report["sections"]) == {
            "ic",
            "decay",
            "turnover",
            "monotonicity",
            "orthogonality",
            "pit",
        }
        assert report["unavailable"] == []
        assert report["sections"]["ic"]["source"] == "tfb_run"
        assert report["sections"]["monotonicity"]["value"] == pytest.approx(1.0)

    def test_full_degradation_marks_every_section_and_top_status(self):
        report = V.build_validation_report(
            factor_id="f" * 32,
            market="a_share",
            generated_at="2026-10-10T06:00:00Z",
        )

        assert report["status"] == "degraded"
        assert report["tfb_run_id"] is None
        names = [u["section"] for u in report["unavailable"]]
        assert names == [
            "ic",
            "decay",
            "turnover",
            "monotonicity",
            "orthogonality",
            "pit",
        ]
        for section in report["sections"].values():
            assert section["status"] == "unavailable"
            assert section.get("reason"), "每节降级必须有 reason"

    def test_pit_unknown_counts_into_degraded(self):
        bundle = self._full_bundle()
        bundle["pit"] = {
            "status": "unknown",
            "probe_date": "2026-09-01",
            "max_abs_diff": None,
            "n_common": 3,
            "n_diff": None,
            "tol": 1e-9,
            "reason": "insufficient_common_symbols",
        }
        report = V.build_validation_report(**bundle)

        assert report["status"] == "degraded"
        assert {"section": "pit", "reason": "insufficient_common_symbols"} in report[
            "unavailable"
        ]

    def test_pit_fail_is_concluded_not_degraded(self):
        bundle = self._full_bundle()
        bundle["pit"]["status"] = "fail"
        report = V.build_validation_report(**bundle)

        assert report["status"] == "completed"
        assert report["sections"]["pit"]["status"] == "fail"

    @pytest.mark.parametrize(
        "degrade_status",
        [
            "no_parents",
            "no_qualified_parents",
            "insufficient_days",
            "bad_panel",
            "no_panel",
        ],
    )
    def test_orthogonality_non_conclusion_counts_into_degraded(self, degrade_status):
        """T-MV-08 的降级态 = 正交问题**没得出结论**：节内如实透传（可见性），
        顶层必须计入降级面（未知 ≠ 通过；与 pit unknown 同一处理纪律）。

        旧行为（no_parents 不计入 → 顶层 completed）会让未评估的正交节
        冒充「验证齐全」，T-MV-10 排名/UI 徽标随之失真。
        """
        bundle = self._full_bundle()
        bundle["orthogonality"] = {"status": degrade_status}
        report = V.build_validation_report(**bundle)

        assert report["status"] == "degraded"
        assert {"section": "orthogonality", "reason": degrade_status} in report[
            "unavailable"
        ]
        # 节内透传不变：降级原因本身对审计面可见
        assert report["sections"]["orthogonality"]["status"] == degrade_status

    def test_orthogonality_evaluated_stays_completed(self):
        bundle = self._full_bundle()
        report = V.build_validation_report(**bundle)

        assert report["status"] == "completed"
        assert report["sections"]["orthogonality"]["status"] == "ok"

    def test_json_safe_no_nan(self):
        bundle = self._full_bundle()
        bundle["h5_ic"]["stats_by_horizon"] = {
            1: _stats(float("nan"), n_days=10),
            5: _stats(0.02),
        }
        bundle["series"]["turnover"] = [float("nan"), 0.5]
        bundle["series"]["q_curves"]["q2"] = [float("nan")] * 25
        report = V.build_validation_report(**bundle)

        text = json.dumps(report)  # NaN 会产出非法 JSON 字面量，能过即安全
        assert "NaN" not in text

        def _walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
            elif isinstance(node, float):
                assert math.isfinite(node), f"报告含非有限浮点: {node}"

        _walk(report)


# ── 常量契约 ─────────────────────────────────────────────────────────


class TestConstants:
    def test_probe_and_tolerance_constants(self):
        assert V.PIT_TOL == 1e-9
        assert V.PIT_PROBE_OFFSET_DAYS == 30
        from backend.services.engine.factor_report import metrics as M

        assert V.PIT_MIN_COMMON == M.MIN_SAMPLES, (
            "对照门槛复用平台横截面常量，不另起口径"
        )


# ── 验证脚本纯逻辑（2026-10-10 集成实跑回归：物化口径字符串日期崩溃）──


class TestToDatetimeIndex:
    """物化口径（``trade_date`` 字符串）→ 对齐口径（真 Timestamp）适配。"""

    def test_string_dates_convert_values_keep_place(self):
        from backend.scripts import mining_factor_validate as SV

        values = _canon(
            ["2026-01-05", "2026-01-06"],
            ["SH600036", "SZ000001"],
            np.array([[1.0, 2.0], [3.0, 4.0]]),
        )
        out = SV._to_datetime_index(values)
        assert pd.api.types.is_datetime64_any_dtype(out.index.get_level_values(0))
        assert list(out.index.names) == ["trade_date", "symbol"]
        # 只动标签不动值：行序与数值逐位不变
        assert out.to_numpy().tolist() == values.to_numpy().tolist()
        assert [tuple(v) for v in out.index] == [
            (pd.Timestamp("2026-01-05"), "SH600036"),
            (pd.Timestamp("2026-01-05"), "SZ000001"),
            (pd.Timestamp("2026-01-06"), "SH600036"),
            (pd.Timestamp("2026-01-06"), "SZ000001"),
        ]

    def test_datetime_input_unchanged(self):
        from backend.scripts import mining_factor_validate as SV

        days = pd.to_datetime(["2026-01-05", "2026-01-06"])
        values = pd.Series(
            [1.0, 2.0],
            index=pd.MultiIndex.from_arrays(
                [days, ["SH600036", "SZ000001"]], names=["trade_date", "symbol"]
            ),
        )
        out = SV._to_datetime_index(values)
        assert out.equals(values)

    def test_non_multiindex_passthrough(self):
        from backend.scripts import mining_factor_validate as SV

        values = pd.Series([1.0, 2.0])
        assert SV._to_datetime_index(values) is values


class TestDecayStatsScript:
    """脚本侧多视界统计：物化口径（字符串日期）必须能进引擎对齐链。

    2026-10-10 真实因子实跑取证：``_compute_factor_values`` 输出
    ``_to_canonical`` 口径（date 为 '%Y-%m-%d' 字符串），而
    ``_detect_datetime_level`` 按值判层且拒绝猜字符串日期——不转换则
    衰减节对**所有**因子恒降级（bug 实证：idio_vol20_lvl_rev）。
    """

    @staticmethod
    def _panel(n_days: int = 40):
        syms = [f"SH6000{i:02d}" for i in range(25)]
        days = pd.bdate_range("2026-01-05", periods=n_days)
        rng = np.random.default_rng(9)
        closes = 100.0 * np.cumprod(
            1.0 + rng.normal(0, 0.01, (n_days, len(syms))), axis=0
        )
        close = pd.Series(
            closes.reshape(-1),
            index=pd.MultiIndex.from_arrays(
                [np.tile(syms, n_days), np.repeat(days, len(syms))],
                names=["instrument", "datetime"],
            ),
        )
        # 因子值 = 次日收益（构造上 h=1 截面 IC 应接近 1）
        fwd = close.groupby(level="instrument").shift(-1) / close - 1.0
        values = fwd.copy()
        values.index = values.index.reorder_levels(["datetime", "instrument"])
        values.index = values.index.set_levels(
            values.index.levels[0].strftime("%Y-%m-%d"), level=0
        )
        values.index = values.index.set_names(["trade_date", "symbol"])
        return values.dropna(), close

    def test_string_dated_values_produce_stats(self):
        from backend.scripts import mining_factor_validate as SV

        values, close = self._panel()
        stats = SV._decay_stats(values, close, (1, 2, 5, 10))
        assert set(stats) == {1, 2, 5, 10}
        for h, s in stats.items():
            assert s["n_days"] > 0, f"h={h} 无有效天数"
        # 构造上 h=1 因子=次日收益 → 每日 IC≈1
        assert stats[1]["ic"] > 0.99

    def test_already_datetime_values_still_work(self):
        from backend.scripts import mining_factor_validate as SV

        values, close = self._panel()
        values = SV._to_datetime_index(values)
        stats = SV._decay_stats(values, close, (1, 2))
        assert stats[1]["n_days"] > 0
        assert stats[1]["ic"] > 0.99


class TestTruncateH5Rows:
    def test_rows_after_probe_are_dropped_and_file_atomic(self, tmp_path):
        pytest.importorskip("tables")  # 宿主无 PyTables 时跳过
        from backend.scripts import mining_factor_validate as SV

        days = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
        frame = pd.DataFrame(
            {"$close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]},
            index=pd.MultiIndex.from_arrays(
                [np.repeat(days, 2), ["sh600036", "sz000001"] * 3],
                names=["datetime", "instrument"],
            ),
        )
        src = tmp_path / "full.h5"
        dst = tmp_path / "trunc.h5"
        frame.to_hdf(src, key="data", mode="w")

        SV._truncate_h5_rows(src, dst, "2026-01-06")

        out = pd.read_hdf(dst, key="data")
        assert out.index.get_level_values(0).max() <= pd.Timestamp("2026-01-06")
        assert len(out) == 4
        assert out["$close"].tolist() == [1.0, 2.0, 3.0, 4.0]
        assert not list(tmp_path.glob("*.tmp"))  # 临时文件不残留

    def test_probe_on_last_date_keeps_everything(self, tmp_path):
        pytest.importorskip("tables")  # 宿主无 PyTables 时跳过
        from backend.scripts import mining_factor_validate as SV

        day = pd.Timestamp("2026-01-05")
        frame = pd.DataFrame(
            {"$close": [1.0]},
            index=pd.MultiIndex.from_arrays(
                [pd.DatetimeIndex([day]), ["sh600036"]],
                names=["datetime", "instrument"],
            ),
        )
        src = tmp_path / "full.h5"
        dst = tmp_path / "trunc.h5"
        frame.to_hdf(src, key="data", mode="w")
        SV._truncate_h5_rows(src, dst, "2026-01-05")
        assert len(pd.read_hdf(dst, key="data")) == 1


class TestDecayWindow:
    def test_tfb_window_passthrough(self):
        from backend.scripts import mining_factor_validate as SV

        values = _canon(["2026-01-05"], ["SH600036"], np.array([[1.0]]))
        tfb = {"window": {"start": "2023-10-09", "end": "2026-10-09"}}
        assert SV._decay_window(tfb, values) == ("2023-10-09", "2026-10-09")

    def test_fallback_trims_to_last_window_days(self):
        from backend.scripts import mining_factor_validate as SV

        days = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(600)]
        values = pd.Series(
            1.0,
            index=pd.MultiIndex.from_arrays(
                [days, ["SH600036"] * len(days)], names=["trade_date", "symbol"]
            ),
        )
        start, end = SV._decay_window(None, values)
        assert end == days[-1]
        assert start == days[-SV.DECAY_WINDOW_DAYS]

    def test_empty_values_give_none(self):
        from backend.scripts import mining_factor_validate as SV

        empty = pd.Series(
            dtype=float,
            index=pd.MultiIndex.from_arrays([[], []], names=["trade_date", "symbol"]),
        )
        assert SV._decay_window(None, empty) is None


class TestAsDict:
    def test_dict_passthrough(self):
        from backend.scripts import mining_factor_validate as SV

        trace = {"status": "ok"}
        assert SV._as_dict(trace) is trace

    def test_json_string_decoded(self):
        from backend.scripts import mining_factor_validate as SV

        assert SV._as_dict('{"status": "ok"}') == {"status": "ok"}

    def test_none_and_junk_and_non_dict_json(self):
        from backend.scripts import mining_factor_validate as SV

        assert SV._as_dict(None) is None
        assert SV._as_dict("") is None
        assert SV._as_dict("{broken") is None
        assert SV._as_dict("[1, 2]") is None


class TestDecayErrorHonesty:
    """衰减节「尝试过但失败」必须区别于「未生成」——集成实跑曾被
    no_h5_stats 掩盖真实异常（报告里只有原因枚举，没有错误正文）。"""

    def test_error_overrides_generic_no_h5_stats(self):
        sec = V.decay_section(None, error="无法从索引层 ['trade_date'] 判定")
        assert sec["status"] == "unavailable"
        assert sec["reason"] == "h5_stage_error"
        assert "无法从索引层" in sec["note"]

    def test_none_without_error_still_no_h5_stats(self):
        sec = V.decay_section(None)
        assert sec["reason"] == "no_h5_stats"

    def test_build_report_threads_h5_error_into_decay(self):
        report = V.build_validation_report(
            factor_id="f-err",
            market="a_share",
            generated_at="2026-10-10T00:00:00Z",
            h5_ic=None,
            h5_error="Qlib 收盘价装载为空",
        )
        assert report["sections"]["decay"]["reason"] == "h5_stage_error"
        assert {"section": "decay", "reason": "h5_stage_error"} in report["unavailable"]
        assert report["status"] == "degraded"


# ── 验证落盘（rd_agent_factor_validations）────────────────────────────


class _StoreSpy:
    """替身 session 工厂：记录 ``read_only`` 标志与逐条 SQL/参数，不碰真库。"""

    def __init__(self, result: _FakeResult | None = None) -> None:
        self.log: list[tuple[str, dict | None]] = []
        self.sessions: list[dict] = []
        self.result = result or _FakeResult()

    def get_session(self, read_only: bool = False):
        self.sessions.append({"read_only": read_only})
        return _FakeSession(self.log, self.result)


class _FakeResult:
    def __init__(self, *, rowcount: int = 0, row: dict | None = None) -> None:
        self.rowcount = rowcount
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, log: list, result: _FakeResult) -> None:
        self.log = log
        self.result = result

    async def execute(self, stmt, params=None):
        self.log.append((str(stmt), params))
        return self.result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _row(**overrides) -> dict:
    row = {
        "factor_id": "rd-1",
        "market": "a_share",
        "status": "completed",
        "tfb_run_id": "tfb-9",
        "report_json": '{"status": "completed"}',
        "error": None,
        "created_at": None,
        "finished_at": None,
    }
    row.update(overrides)
    return row


class TestValidationStore:
    """专表纪律：状态机 running→completed/degraded/failed；start 重置整行
    （旧报告/旧错误随新尝试清空）；收口只认仍在 running 的行（幂等）。"""

    def test_ensure_table_creates_table_and_index(self, monkeypatch):
        spy = _StoreSpy()
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        asyncio.run(VS.ensure_table())
        assert len(spy.log) == 2
        assert "CREATE TABLE IF NOT EXISTS rd_agent_factor_validations" in spy.log[0][0]
        assert (
            "CREATE INDEX IF NOT EXISTS idx_rd_agent_factor_validations_status"
            in spy.log[1][0]
        )

    def test_start_validation_resets_row_to_running(self, monkeypatch):
        spy = _StoreSpy()
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        asyncio.run(VS.start_validation("rd-1", "a_share"))
        sql, params = spy.log[0]
        assert "ON CONFLICT (factor_id, market) DO UPDATE" in sql
        assert "status = 'running'" in sql
        # 旧报告/旧错误随新尝试清空（专表只保最新结论）
        assert "report_json = NULL" in sql
        assert "error = NULL" in sql
        assert params == {"factor_id": "rd-1", "market": "a_share"}

    def test_finish_validation_rejects_unknown_status(self, monkeypatch):
        spy = _StoreSpy()
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        with pytest.raises(ValueError):
            asyncio.run(VS.finish_validation("rd-1", "a_share", status="running"))
        assert spy.log == []  # 非法终态不触库

    def test_finish_validation_guards_running_and_false_when_lost(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(rowcount=0))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        ok = asyncio.run(VS.finish_validation("rd-1", "a_share", status="completed"))
        assert ok is False
        sql, _ = spy.log[0]
        assert "AND status = 'running'" in sql  # 收口守卫：只收口仍在跑的
        assert "finished_at = now()" in sql

    def test_finish_validation_true_on_update(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(rowcount=1))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        ok = asyncio.run(VS.finish_validation("rd-1", "a_share", status="degraded"))
        assert ok is True

    def test_finish_validation_serializes_report_and_optional_fields(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(rowcount=1))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        report = {"status": "degraded", "note": "缺 PIT 探针"}
        asyncio.run(
            VS.finish_validation(
                "rd-1",
                "a_share",
                status="degraded",
                report=report,
                tfb_run_id="tfb-9",
                error="pit unknown",
            )
        )
        sql, params = spy.log[0]
        assert "CAST(:report_json AS JSONB)" in sql
        assert params["report_json"] == json.dumps(report, ensure_ascii=False)
        assert "缺 PIT 探针" in params["report_json"]  # ensure_ascii=False 中文原样
        assert params["tfb_run_id"] == "tfb-9"
        assert params["error"] == "pit unknown"

    def test_finish_validation_omits_absent_optional_fields(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(rowcount=1))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        asyncio.run(VS.finish_validation("rd-1", "a_share", status="failed"))
        sql, params = spy.log[0]
        assert "report_json" not in sql
        assert set(params) == {"status", "factor_id", "market"}

    def test_get_validation_none_when_absent(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(row=None))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        assert asyncio.run(VS.get_validation("rd-1", "a_share")) is None
        assert spy.sessions == [{"read_only": True}]  # 读侧走只读会话

    def test_get_validation_parses_report_json_string(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(row=_row()))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        item = asyncio.run(VS.get_validation("rd-1", "a_share"))
        assert item["report"] == {"status": "completed"}
        assert "report_json" not in item  # 解包后不透出原始列
        assert item["tfb_run_id"] == "tfb-9"

    def test_get_validation_bad_json_degrades_to_none(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(row=_row(report_json="{broken")))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        item = asyncio.run(VS.get_validation("rd-1", "a_share"))
        assert item["report"] is None  # 坏 JSON 降级为 None，不炸读取方

    def test_get_validation_report_already_dict_passes_through(self, monkeypatch):
        spy = _StoreSpy(_FakeResult(row=_row(report_json={"a": 1})))
        monkeypatch.setattr(VS, "get_session", spy.get_session)
        item = asyncio.run(VS.get_validation("rd-1", "a_share"))
        assert item["report"] == {"a": 1}
