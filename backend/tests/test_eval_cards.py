"""T-P4-05b-2 测试：四张评分卡（因子/模型/策略/账户）+ EOD 汇总 + worker 接线。

覆盖：
- 因子卡：半衰期（缺失视界不得当 0）、|IC| 方向口径、弱信号红线、高相关红线封顶、
  correlation 矩阵解析守卫；
- 模型卡：两代元数据归一（metrics / performance_metrics.test）+ OOS 红线 + 缺省归一；
- 策略卡：收益/回撤/月度数学 + MDD 红线 + 连续三月负红线；
- 账户卡：集中度红线、资金效率阶梯、风控事件罚分、基准回看日期回归（YYYYMMDD 减法的历史 bug）、
  双 ID 键形守卫（00000001 ≢ 1 台账）；
- run_all：单卡异常隔离 + 汇总计数；worker：时间解析/开关；
- 真库 E2E：因子卡/账户卡/五卡全链路（数据不可用则 skip，不假过）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

_BACKEND = Path(__file__).resolve().parents[1]


# ── 因子卡 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_half_life_days_decay_and_gaps():
    from backend.scripts.eval.factor_card import half_life_days

    # 恰好衰减到一半 → H=2
    assert half_life_days({1: 0.10, 2: 0.05}) == pytest.approx(2.0)
    # 线性插值：1→0.10、5→0.02（日衰减 0.02），穿过 0.05 于 h=1+(0.05/0.02)=3.5
    hl = half_life_days({1: 0.10, 5: 0.02})
    assert hl == pytest.approx(3.5, abs=0.01)
    # 未衰减到一半 → None
    assert half_life_days({1: 0.10, 2: 0.09, 5: 0.08, 10: 0.07, 20: 0.06}) is None
    # 缺失/NaN 视界不得当 0 处理（曾把「未测」误判成「已衰减」）
    assert half_life_days({1: 0.10, 2: np.nan, 5: 0.09}) is None
    assert half_life_days({1: 0.0}) is None


def _ic_series(mean: float, std: float, n: int = 250) -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.normal(mean, std, n)


@pytest.mark.unit
def test_factor_predictive_magnitude_and_direction():
    from backend.scripts.eval.factor_card import score_factor

    strong = _ic_series(0.06, 0.03)  # ICIR=2.0
    out = score_factor("f", {"ic": strong, "coverage": np.full(250, 0.9)}, None, None)
    pred = out["dimensions"]["predictive"]
    assert pred["score"] > 75 and pred["red_line_failed"] is False
    assert pred["detail"]["direction"] == "normal"

    # 负 IC 强因子：按强度取绝对值计分、标 direction=inverted（反转即可用，不判 0）
    inverted = score_factor(
        "f", {"ic": -strong, "coverage": np.full(250, 0.9)}, None, None
    )
    ipred = inverted["dimensions"]["predictive"]
    assert ipred["score"] == pytest.approx(pred["score"], abs=0.5)
    assert ipred["detail"]["direction"] == "inverted"
    assert ipred["red_line_failed"] is False

    # 弱信号：|ICIR|<0.2 → 红线
    weak = score_factor("f", {"ic": _ic_series(0.004, 0.05)}, None, None)
    wpred = weak["dimensions"]["predictive"]
    assert wpred["red_line_failed"] is True and wpred["score"] < 30


@pytest.mark.unit
def test_factor_independence_red_line_caps_score():
    from backend.scripts.eval.factor_card import score_factor

    series = {"ic": _ic_series(0.06, 0.03), "coverage": np.full(250, 0.9)}
    out = score_factor("dup", series, None, 0.95)
    assert out["dimensions"]["independence"]["red_line_failed"] is True
    assert out["capped"] is True and out["score"] == 59.0
    assert "independence" in out["red_line_failed"]


@pytest.mark.unit
def test_factor_insufficient_and_correlation_parse():
    from backend.scripts.eval.factor_card import _parse_correlation, score_factor

    short = score_factor("f", {"ic": np.array([0.01, 0.02])}, None, None)
    assert short["dimensions"]["predictive"]["score"] is None
    assert short["dimensions"]["predictive"]["detail"]["insufficient"] is True

    names, matrix = _parse_correlation(
        {"factors": ["a", "b"], "matrix": [[1.0, 0.5], [0.5, 1.0]]}
    )
    assert names == ["a", "b"] and list(matrix[0]) == [1.0, 0.5]
    # 长度不齐/缺失 → 空（宁缺勿错）
    assert _parse_correlation({"factors": ["a"], "matrix": [[1.0], [0.1]]}) == ([], [])
    assert _parse_correlation(None) == ([], [])


# ── 模型卡 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_model_metrics_two_generation_schema():
    from backend.scripts.eval.model_card import extract_oos_metrics

    new_meta = {"metrics": {"test_rank_ic": 0.05, "test_rank_icir": 0.8}}
    metrics, source = extract_oos_metrics(new_meta)
    assert source == "metrics" and metrics["test_rank_ic"] == 0.05

    legacy_meta = {"performance_metrics": {"test": {"mean_ic": 0.0609, "icir": 0.443}}}
    metrics, source = extract_oos_metrics(legacy_meta)
    assert source == "performance_metrics.test"
    assert metrics["test_rank_ic"] == pytest.approx(0.0609)
    assert metrics["test_rank_icir"] == pytest.approx(0.443)

    metrics, source = extract_oos_metrics({})
    assert source == "none" and metrics == {}


@pytest.mark.unit
def test_model_oos_red_line_and_insufficient():
    from backend.scripts.eval.model_card import score_oos

    good = score_oos({"test_rank_ic": 0.08, "test_rank_icir": 1.0})
    assert good.score is not None and good.score > 80 and not good.red_line_failed

    weak = score_oos({"test_rank_ic": 0.01, "test_rank_icir": 0.2})
    assert weak.red_line_failed is True  # IC<0.02 或 ICIR<0.3

    none = score_oos({})
    assert none.score is None and none.detail["insufficient"] is True


@pytest.mark.unit
def test_model_card_renormalizes_missing_dims(tmp_path, monkeypatch):
    import json as _json

    from backend.scripts.eval import model_card

    monkeypatch.setattr(model_card, "PRODUCTION_DIR", tmp_path)
    (tmp_path / "m1").mkdir()
    (tmp_path / "m1" / "metadata.json").write_text(
        _json.dumps({"metrics": {"test_rank_ic": 0.08, "test_rank_icir": 1.0}}),
        encoding="utf-8",
    )
    result = model_card.score_model("m1")
    assert result["score"] == pytest.approx(
        result["dimensions"]["oos_predictive"]["score"], abs=0.1
    )
    assert set(result["missing_dims"]) == {
        "stratification",
        "robustness",
        "rolling_health",
        "turnover_cost",
    }
    assert model_card.score_model("missing")["error"]

    # .bak 备份目录不纳入清单
    (tmp_path / "m1.bak-20260901").mkdir()
    (tmp_path / "m1.bak-20260901" / "metadata.json").write_text("{}", encoding="utf-8")
    assert model_card.list_production_models() == ["m1"]


@pytest.mark.unit
def test_user_model_meta_path_resolution(tmp_path, monkeypatch):
    """用户模型 metadata 定位（2026-09-17 纳入评分卡）：

    storage_path 直读优先 → ``/app/models`` 前缀映射 PROJECT_ROOT（宿主直跑）→
    users 树按 model_id 兜底（CN 两层 / 非 CN 三层布局均可）→ 缺失 None。
    """
    import json as _json

    from backend.scripts.eval import model_card

    monkeypatch.setattr(model_card, "PROJECT_ROOT", tmp_path)
    d1 = tmp_path / "models" / "users" / "default" / "00000001" / "mdl_a"
    d1.mkdir(parents=True)
    (d1 / "metadata.json").write_text(
        _json.dumps({"metrics": {"test_rank_ic": 0.05, "test_rank_icir": 0.8}}),
        encoding="utf-8",
    )
    d2 = tmp_path / "models" / "users" / "default" / "00000001" / "us" / "mdl_b"
    d2.mkdir(parents=True)
    (d2 / "metadata.json").write_text("{}", encoding="utf-8")

    assert model_card._user_meta_path("mdl_a") == d1 / "metadata.json"
    assert model_card._user_meta_path("mdl_b") == d2 / "metadata.json"
    assert model_card._user_meta_path("x", str(d1)) == d1 / "metadata.json"
    assert (
        model_card._user_meta_path("x", "/app/models/users/default/00000001/mdl_a")
        == d1 / "metadata.json"
    )
    assert model_card._user_meta_path("nope") is None

    # 带 meta_path 评分与系统模型同口径（OOS 维度可算出分）
    result = model_card.score_model("mdl_a", meta_path=d1 / "metadata.json")
    assert result["score"] is not None
    assert result["inputs_version"]["meta_path"].endswith("mdl_a/metadata.json")


# ── 策略卡 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_strategy_curve_math():
    from backend.scripts.eval.strategy_card import (
        annualized_return,
        curve_to_returns,
        monthly_returns,
    )

    assert curve_to_returns([100.0, 110.0, 121.0]).tolist() == pytest.approx([0.1, 0.1])
    assert len(curve_to_returns([100.0])) == 0
    # 每日 +0.1% 一年 → 年化 ≈ 28.6%
    ann = annualized_return(np.full(252, 0.001))
    assert ann == pytest.approx(1.001**252 - 1, abs=1e-6)
    assert annualized_return(np.full(5, 0.01)) is None  # 样本不足

    monthly = monthly_returns(
        ["2026-01-05", "2026-02-10", "2026-03-10"], [100.0, 110.0, 121.0]
    )
    assert [m for m, _ in monthly] == ["2026-01", "2026-02", "2026-03"]
    assert [r for _, r in monthly] == pytest.approx([0.0, 0.1, 0.1])


@pytest.mark.unit
def test_strategy_risk_and_stability_red_lines():
    from backend.scripts.eval.strategy_card import (
        score_risk_dim,
        score_return_dim,
        score_stability_dim,
    )

    # 深回撤曲线：20 日 +1%、15 日 -6% → MDD ≈ -60% → 红线
    returns = np.concatenate(
        [np.full(20, 0.01), np.full(15, -0.06), np.full(25, 0.005)]
    )
    risk = score_risk_dim(returns, None, ann=0.05)
    assert risk.red_line_failed is True
    assert risk.detail["max_drawdown"] <= -0.5

    stab = score_stability_dim(
        [
            ("m", 0.01),
            ("m", 0.02),
            ("m", 0.01),
            ("m", -0.01),
            ("m", -0.02),
            ("m", -0.03),
        ]
    )
    assert stab.red_line_failed is True and stab.detail["tail_3m_negative"] is True
    assert score_stability_dim([("m", 0.01)] * 5).score is None  # 月度样本 <6

    good = score_return_dim(0.15, 0.05)
    assert good.score is not None and good.detail["excess_annual"] == pytest.approx(
        0.10
    )
    assert score_return_dim(None, 0.05).score is None


# ── 账户卡 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_account_exposure_concentration_and_red_line():
    from backend.scripts.eval.account_card import (
        industry_breakdown,
        position_stats,
        score_exposure,
    )

    concentrated = {
        "600036.SH": {"market_value": 800.0},
        "000001.SZ": {"market_value": 200.0},
    }
    stats = position_stats(concentrated)
    assert stats["n"] == 2 and stats["max_share"] == pytest.approx(0.8)
    dim = score_exposure(concentrated, 1000.0, None)
    assert dim.red_line_failed is True and dim.detail["red_line"] == "单票 >50%"
    assert dim.detail["net_exposure"] == pytest.approx(1.0)

    diversified = {f"{i:06d}.SZ": {"market_value": 50.0} for i in range(20)}
    dim2 = score_exposure(diversified, 2000.0, None)
    assert dim2.red_line_failed is False and dim2.score > 90

    assert score_exposure({}, 1000.0, None).score is None  # 空仓不评暴露

    ind = industry_breakdown(
        {"A.SH": {"market_value": 60.0}, "B.SZ": {"market_value": 40.0}},
        {"A.SH": "银行"},
    )
    assert ind is not None and ind["unmapped_ratio"] == pytest.approx(0.4)
    assert ind["max_industry_share"] == pytest.approx(0.6)


@pytest.mark.unit
def test_account_attribution_and_efficiency():
    from backend.scripts.eval.account_card import (
        contribution_share,
        score_attribution,
        score_capital_efficiency,
        window_return,
    )

    assert window_return([("d1", 100.0), ("d2", 103.0)]) == pytest.approx(0.03)
    assert window_return([("d1", 100.0)]) is None
    assert contribution_share([("a", 9.0), ("b", 1.0)]) == pytest.approx(0.9)
    assert contribution_share([("a", -1.0)]) is None

    dim = score_attribution(
        [("2026-09-01", 100.0), ("2026-09-02", 101.0), ("2026-09-03", 103.0)],
        [("2026-09-01", 100.0), ("2026-09-03", 101.0)],
        [("a", 9.0), ("b", 1.0)],
    )
    assert dim.score is not None and 30 < dim.score < 60
    assert dim.detail["excess"] == pytest.approx(0.02)
    assert "简化口径" in dim.detail["note"]

    assert score_attribution([("d", 100.0)], [], None).score is None

    assert score_capital_efficiency(1000.0, 30.0, 970.0).score == pytest.approx(100.0)
    assert score_capital_efficiency(1000.0, 350.0, 650.0).score == pytest.approx(65.0)
    assert score_capital_efficiency(1000.0, 1000.0, 0.0).score == pytest.approx(0.0)
    assert score_capital_efficiency(1000.0, None, 970.0).detail[
        "cash"
    ] == pytest.approx(30.0)
    assert score_capital_efficiency(0.0, 0.0, 0.0).score is None


@pytest.mark.unit
def test_account_risk_events_and_keys():
    from backend.scripts.eval.account_card import (
        benchmark_probe_start,
        score_risk_events,
        uid_forms,
    )

    assert score_risk_events({"available": False}).score is None
    assert score_risk_events(None).score is None
    # 窗口内零事件 → 缺省：「无记录」不等于「无事件」（实测 user=42 曾是静默满分）
    assert score_risk_events({"available": True, "n_events": 0}).score is None
    clean = score_risk_events(
        {
            "available": True,
            "n_events": 12,
            "filled": 12,
            "failed": 0,
            "rejected_orders": 0,
        }
    )
    assert clean.score == pytest.approx(100.0) and not clean.red_line_failed
    bad = score_risk_events(
        {
            "available": True,
            "n_events": 6,
            "failed": 3,
            "rejected_orders": 2,
            "skipped": 1,
            "alert": 0,
        }
    )
    assert bad.red_line_failed is True and bad.score < 50
    assert bad.detail["penalty"] == pytest.approx(9.0)  # fidelity: allow-limit-threshold — 非阈值：风险事件罚分（9.0 分；skipped 已移入盲区覆盖率）

    # 基准回看日期：YYYYMMDD 整数的历史 bug（20260911-12=20260899 非法日期）
    assert benchmark_probe_start("2026-09-11") == 20260830
    assert benchmark_probe_start("2026-09-11", lookback_days=0) == 20260911

    assert uid_forms("1") == ["1", "00000001"]
    assert uid_forms("00000001") == ["00000001", "1"]
    assert uid_forms("admin") == ["admin"]
    assert uid_forms("") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_dual_id_history_guard():
    """非规范键形（00000001）不得借位到台账 user 1 的委托历史（双 ID 串号守卫）。"""
    from backend.scripts.eval.account_card import _has_trade_history

    assert await _has_trade_history("default", "00000001") is False
    assert await _has_trade_history("default", "admin") is False


# ── run_all + worker ────────────────────────────────────────────────


@pytest.mark.unit
def test_run_all_summary_pure():
    from backend.scripts.eval.run_all import _score_summary

    summary = _score_summary(
        [
            {"object_type": "a", "object_id": "1", "score": 80.0},
            {"object_type": "a", "object_id": "2", "error": "x"},
            {"object_type": "a", "object_id": "3", "score": None},
        ]
    )
    assert summary["n"] == 3 and summary["scored"] == 1
    assert summary["errors"] == ["a:2: x"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_all_isolates_card_failures(monkeypatch):
    from backend.scripts.eval import run_all as run_all_mod

    async def ok(*args, **kwargs):
        return {"n": 1, "scored": 1, "errors": [], "skipped": 0}

    async def boom(*args, **kwargs):
        raise RuntimeError("卡崩了")

    monkeypatch.setattr(run_all_mod, "_run_factor_card", boom)
    monkeypatch.setattr(run_all_mod, "_run_model_card", ok)
    monkeypatch.setattr(run_all_mod, "_run_strategy_card", ok)
    monkeypatch.setattr(run_all_mod, "_run_account_card", ok)
    monkeypatch.setattr(run_all_mod, "_run_daily_selection", ok)

    summary = await run_all_mod.run_all(save=False)
    assert set(summary["cards"]) == {
        "factor",
        "model",
        "strategy",
        "account",
        "daily_selection",
    }
    assert summary["cards"]["factor"]["scored"] == 0
    assert any("factor 卡异常" in e for e in summary["errors"])
    assert summary["total_scored"] == 4  # 其余四卡不受影响


@pytest.mark.unit
def test_eval_worker_config_and_time(monkeypatch):
    from backend.services.trade.services import eval_scores_service as svc

    assert svc.parse_eval_time("16:30") == (16, 30)
    assert svc.parse_eval_time("23:59") == (23, 59)
    assert svc.parse_eval_time("bad") == (16, 0)
    assert svc.parse_eval_time("25:00") == (16, 0)

    monkeypatch.setenv("EVAL_SCORES_WORKER_ENABLED", "0")
    monkeypatch.setenv("EVAL_SCORES_TIME", "17:05")
    monkeypatch.setenv("EVAL_SCORES_CHECK_INTERVAL_SEC", "5")
    cfg = svc._config()
    assert cfg["enabled"] is False
    assert cfg["time"] == "17:05"
    assert cfg["interval"] == 10  # 最小间隔钳制

    assert svc._raw_client(None) is None

    class _RawClient:  # 原生 client 形态（无 .client 包装层）
        def set(self, *args, **kwargs):  # pragma: no cover - 仅形态判定
            return None

    assert svc._raw_client(_RawClient()) is not None


@pytest.mark.unit
def test_eval_worker_wired_in_trade_main():
    src = (_BACKEND / "services/trade/main.py").read_text(encoding="utf-8")
    assert "run_eval_scores_worker" in src
    assert "eval-scores-worker" in src
    assert "eval_scores_task" in src


# ── 真库 E2E（数据不可用则 skip，不假过）────────────────────────────


@pytest.mark.asyncio
async def test_factor_card_real_dataset():
    """真库：alpha_library 报告在盘 → top3 全有分；相关矩阵路径真实生效。"""
    try:
        from backend.scripts.eval.factor_card import score_factors
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        results = score_factors("alpha_library", None, 3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据不可用: {exc}")
    if results and results[0].get("error"):
        pytest.skip(f"数据集缺失: {results[0]['error']}")
    assert len(results) == 3
    for r in results:
        assert r["score"] is not None and r["grade"]
        assert set(r["dimensions"]) == {
            "predictive",
            "stability",
            "independence",
            "quality_gate",
            "coverage",
        }
    # 相关矩阵可解析时独立性应有分（此前矩阵格式读错导致永远缺省）
    assert any(r["dimensions"]["independence"]["score"] is not None for r in results)


@pytest.mark.asyncio
async def test_account_card_real_scan():
    """真库+Redis：扫描在册账户；有持仓的真评、空账户 skipped、双 ID 键不重复计分。"""
    try:
        import redis  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        from backend.scripts.eval.account_card import score_all_accounts

        results = await score_all_accounts(tenant="default", save=False)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"环境不可用: {exc}")
    finally:
        try:
            from backend.shared.database_manager_v2 import close_database

            await close_database()
        except Exception:  # noqa: BLE001
            pass
    if not results or (len(results) == 1 and results[0].get("error")):
        pytest.skip("无在册账户或 Redis 不可用")
    assert all(r["object_type"] == "account" for r in results)
    # 每个账户要么 scored，要么 skipped（空账户/未创建）——不允许静默 None
    for r in results:
        assert r.get("score") is not None or r.get("skipped") or r.get("error")
    # 双 ID 守卫：00000001 与 1 不是同一账户，不得同时计分
    by_id = {r["object_id"]: r for r in results}
    if "00000001:CN" in by_id and "1:CN" in by_id:
        assert not (by_id["00000001:CN"].get("score") and by_id["1:CN"].get("score"))


@pytest.mark.asyncio
async def test_run_all_e2e_no_save():
    """五卡全链路（不落表）：所有卡都产出计数，真分数 >0；环境缺数据即 skip。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    from backend.scripts.eval.run_all import run_all

    try:
        summary = await run_all(save=False)
    finally:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
    assert set(summary["cards"]) == {
        "factor",
        "model",
        "strategy",
        "account",
        "daily_selection",
    }
    if summary["total_scored"] == 0:
        pytest.skip("环境无可用评分数据")
    assert summary["elapsed_sec"] > 0
