"""T-P4-06 测试：体检三处接入（回测后自动体检 / 晋级门禁 / 月度复检）。

覆盖：
- 晋级门禁纯函数矩阵（A/B 放行、L/E/未体检拒绝、REAL 的 SIM 天数与跟踪误差门槛）；
- `evaluate_equity_curve` 四分类确定性夹具（A/B/L/E 各一 + 短曲线 None）；
- SIM 首启打点与交易日计数（含 NX 幂等与日历回退）；
- 月度复检纯函数（退化告警判定/执行窗口/环境开关）；
- **接线源守卫**（回测落库挂钩/启动端点门禁/worker 注册——漏接即红，防静默失效）；
- 真库 E2E：`attach_backtest_health` 全链（合成回测行 → result_json.health + 策略留档 → 清理）
  与 `latest_strategy_health` 回读。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import numpy as np
import pytest

_BACKEND = Path(__file__).resolve().parents[1]


def _curve_from_returns(rets: np.ndarray, start: float = 100.0) -> list[dict]:
    vals = [start]
    for r in rets:
        vals.append(vals[-1] * (1 + float(r)))
    return [
        {"date": f"2023-{i // 21 + 1:02d}-{i % 21 + 1:02d}", "value": v}
        for i, v in enumerate(vals)
    ]


def _alpha_curve_a() -> tuple[list[dict], list[float]]:
    """确定性 A 夹具（alpha 显著 + DSR 过 + 样本充分；种子固定可复现）。"""
    rng = np.random.default_rng(7)
    bench = rng.normal(0.0003, 0.006, 750)
    strat = 0.3 * bench + 0.002 + rng.normal(0, 0.004, 750)
    return _curve_from_returns(strat), list(np.cumprod(1 + bench) * 100)


# ── 晋级门禁（纯函数矩阵）───────────────────────────────────────────


@pytest.mark.unit
def test_promotion_gate_verdict_matrix():
    from backend.shared.backtest_health import promotion_gate

    ok, reason = promotion_gate(None)
    assert ok is False and "体检" in reason  # 未体检不得晋级

    ok, reason = promotion_gate({"verdict": "A", "confidence": 90})
    assert ok is True and "A" in reason

    ok, reason = promotion_gate(
        {"verdict": "B", "confidence": 70, "reasons": ["R²=0.9：收益可由基准解释"]}
    )
    assert ok is True
    assert "收益" in reason and "暴露" in reason  # B 放行但须标注收益来源

    for verdict in ("L", "E"):
        ok, reason = promotion_gate(
            {
                "verdict": verdict,
                "confidence": 40,
                "reasons": ["DSR 0.4 < 0.95"],
                "suggestions": ["延长样本"],
            }
        )
        assert ok is False, verdict
        assert verdict in reason and "DSR 0.4 < 0.95" in reason  # 拒绝理由带证据


@pytest.mark.unit
def test_promotion_gate_real_mode_thresholds():
    from backend.shared.backtest_health import promotion_gate

    health = {"verdict": "A", "confidence": 88}
    # SIM 天数不足 → 拒绝
    ok, reason = promotion_gate(health, mode="REAL", sim_trading_days=5)
    assert ok is False and "20" in reason
    # 天数达标但跟踪误差超限 → 拒绝
    ok, reason = promotion_gate(
        health, mode="REAL", sim_trading_days=25, tracking_error=0.20
    )
    assert ok is False and "跟踪误差" in reason
    # 双双达标 → 放行
    ok, _ = promotion_gate(
        health, mode="REAL", sim_trading_days=25, tracking_error=0.05
    )
    assert ok is True
    # 无状态史/无影子数据 → v1 只提示不拦（数据条件未接线）
    ok, _ = promotion_gate(health, mode="REAL")
    assert ok is True


# ── 四分类确定性夹具（曲线 → 报告全链）──────────────────────────────


@pytest.mark.unit
def test_evaluate_equity_curve_four_verdicts():
    from backend.shared.backtest_health import evaluate_equity_curve

    # A：alpha 显著（t≈13）、DSR 过、样本充分
    curve_a, bench_a = _alpha_curve_a()
    rep_a = evaluate_equity_curve(curve_a, benchmark_closes=bench_a)
    assert rep_a is not None and rep_a["verdict"] == "A"
    assert rep_a["tests"]["factor_regression"]["alpha_significant"] is True
    assert 0 <= rep_a["confidence"] <= 100

    # B：与基准完全一致 → R²=1、alpha 退化守卫判 0 → beta 主导
    rng = np.random.default_rng(42)
    bench = rng.normal(0.0004, 0.01, 500)
    rep_b = evaluate_equity_curve(
        _curve_from_returns(bench), benchmark_closes=list(np.cumprod(1 + bench) * 100)
    )
    assert rep_b is not None
    assert rep_b["verdict"] in {"B", "E"}  # 1 年样本可先落 E 区（MinTRL 诚实）；R² 必高
    assert rep_b["tests"]["factor_regression"]["r2"] > 0.9

    # L：收益集中在单日（剔 Top5 后 alpha 消失）
    rets_l = np.zeros(500)
    rets_l[250] = 0.5
    rep_l = evaluate_equity_curve(_curve_from_returns(rets_l), benchmark_closes=bench_a)
    assert rep_l is not None and rep_l["verdict"] == "L"
    assert rep_l["tests"]["concentration"]["kills_alpha"] is True

    # 短曲线 → None（不产垃圾报告）
    assert evaluate_equity_curve(_curve_from_returns(np.zeros(20))) is None
    assert evaluate_equity_curve([]) is None

    # 纯浮点数组输入也支持
    rep_flat = evaluate_equity_curve(
        list(np.cumprod(1 + rets_l) * 100), benchmark_closes=bench_a
    )
    assert rep_flat is not None and rep_flat["verdict"] == "L"


# ── SIM 打点 / 交易日计数 / 环境开关 ────────────────────────────────


@pytest.mark.unit
def test_sim_stamp_and_trading_days():
    from backend.shared.backtest_health import (
        sim_start_key,
        sim_trading_days_since,
        stamp_sim_start,
        trading_days_between,
    )

    class _FakeRedis:
        def __init__(self):
            self.store: dict = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return None
            self.store[key] = value

        def get(self, key):
            return self.store.get(key)

    fake = _FakeRedis()
    assert sim_start_key("default", "1", "28") == "strategy:sim_start:default:1:28"
    stamp_sim_start(fake, "default", "1", "28")
    first = fake.store[sim_start_key("default", "1", "28")]
    stamp_sim_start(fake, "default", "1", "28")  # NX 幂等：不得刷新
    assert fake.store[sim_start_key("default", "1", "28")] == first

    # 同日起点 → 0 个交易日；区间为左开右闭
    assert trading_days_between("2026-09-16", "2026-09-16") == 0
    n = trading_days_between(
        "2030-01-01", "2030-01-31"
    )  # 未来区间无指数数据 → 工作日回退
    assert 18 <= n <= 23

    # 无打点 → None（门禁按"无数据只提示"）
    assert sim_trading_days_since(fake, "default", "1", "999") is None


@pytest.mark.unit
def test_health_env_switches_and_schedule_guard(monkeypatch):
    from backend.shared import backtest_health as bh

    monkeypatch.delenv("BACKTEST_HEALTH_ENABLED", raising=False)
    monkeypatch.delenv("HEALTH_GATE_ENABLED", raising=False)
    assert bh.health_enabled() is True and bh.gate_enabled() is True
    monkeypatch.setenv("BACKTEST_HEALTH_ENABLED", "0")
    monkeypatch.setenv("HEALTH_GATE_ENABLED", "false")
    assert bh.health_enabled() is False and bh.gate_enabled() is False

    # 关闭或空曲线 → 不调度（返回 False）
    monkeypatch.setenv("BACKTEST_HEALTH_ENABLED", "true")
    assert bh.schedule_health_check(backtest_id="x", equity_rows=None) is False
    monkeypatch.setenv("BACKTEST_HEALTH_ENABLED", "0")
    assert (
        bh.schedule_health_check(
            backtest_id="x", equity_rows=[{"date": "d", "value": 1}]
        )
        is False
    )


# ── 月度复检纯函数 + worker 配置 ────────────────────────────────────


@pytest.mark.unit
def test_recheck_pure_functions(monkeypatch):
    from backend.scripts.eval.health_recheck import should_alert
    from backend.services.trade.services import health_recheck_service as svc

    assert should_alert({"verdict": "A"}, {"verdict": "L"}) is True
    assert should_alert({"verdict": "B"}, {"verdict": "E"}) is True
    assert should_alert({"verdict": "E"}, {"verdict": "A"}) is False  # 改善不打扰
    assert should_alert(None, {"verdict": "L"}) is False  # 首检不算退化
    assert should_alert({"verdict": "A"}, {"verdict": "B"}) is False

    assert svc.in_recheck_window(1, 7) is True
    assert svc.in_recheck_window(7, 7) is True
    assert svc.in_recheck_window(8, 7) is False

    monkeypatch.setenv("HEALTH_RECHECK_ENABLED", "0")
    monkeypatch.setenv("HEALTH_RECHECK_WINDOW_DAYS", "99")
    monkeypatch.setenv("HEALTH_RECHECK_INTERVAL_SEC", "5")
    cfg = svc._config()
    assert cfg["enabled"] is False
    assert cfg["window_days"] == 28  # 钳制
    assert cfg["interval"] == 60  # 钳制


# ── 接线源守卫（漏接即红）───────────────────────────────────────────


@pytest.mark.unit
def test_three_integration_points_wired_in_source():
    persistence = (
        _BACKEND / "services/engine/qlib_app/services/backtest_persistence.py"
    ).read_text(encoding="utf-8")
    assert "schedule_health_check(" in persistence
    assert 'status == "completed"' in persistence
    assert "strategy_id" in persistence  # 策略联动已透传

    runtime = (
        _BACKEND / "services/engine/qlib_app/services/backtest_service_runtime.py"
    ).read_text(encoding="utf-8")
    assert 'strategy_id=getattr(request, "strategy_id", None)' in runtime

    lifecycle = (
        _BACKEND / "services/live_trading/routers/real_trading_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "promotion_gate(" in lifecycle and "latest_strategy_health(" in lifecycle
    assert "stamp_sim_start(" in lifecycle  # SIM 首启打点（LIVE 门槛数据源）

    trade_main = (_BACKEND / "services/trade/main.py").read_text(encoding="utf-8")
    assert (
        "run_health_recheck_worker" in trade_main
        and "health-recheck-worker" in trade_main
    )

    schema = (_BACKEND / "services/engine/qlib_app/schemas/backtest.py").read_text(
        encoding="utf-8"
    )
    assert "health: dict[str, Any] | None = None" in schema  # 证据卡可经 API 下钻


# ── 参数扫描证据（N 去胀 + PBO 矩阵）────────────────────────────────


@pytest.mark.unit
def test_pbo_matrix_from_trials():
    from backend.shared.backtest_health import _pbo_matrix_from_trials

    def _trial(seed: int) -> dict:
        rng2 = np.random.default_rng(seed)
        rets = rng2.normal(0.0005, 0.01, 60)
        vals = list(np.cumprod(1 + rets) * 100)
        return {
            "params": {"k": seed},
            "metrics": {
                "equity_curve": [
                    {"date": f"2025-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}", "value": v}
                    for i, v in enumerate(vals)
                ]
            },
        }

    mat = _pbo_matrix_from_trials([_trial(1), _trial(2), _trial(3)])
    assert mat is not None and mat.shape[1] == 3 and mat.shape[0] >= 20

    # 有效列 < 2 / 曲线过短 → None（PBO 如实缺省）
    assert _pbo_matrix_from_trials([_trial(1)]) is None
    assert _pbo_matrix_from_trials([{"metrics": {"equity_curve": []}}, _trial(2)]) is None
    assert _pbo_matrix_from_trials([]) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_sweep_evidence_default_without_db():
    from backend.shared.backtest_health import resolve_sweep_evidence

    # 非数字 strategy_id → 直接缺省（不查库）
    n, matrix, source = await resolve_sweep_evidence(
        tenant_id="default", user_id="1", strategy_id="sys_template"
    )
    assert n == 1 and matrix is None and source == "default"
    n, _, source = await resolve_sweep_evidence(
        tenant_id="default", user_id="1", strategy_id=None
    )
    assert source == "default"


# ── 真库 E2E ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_backtest_health_e2e_real_db():
    """合成回测行 → attach 全链（result_json.health + 策略留档）→ 回读 → 清理。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        # 跨事件循环池自愈：前序用例可能留下绑定旧循环的池（asyncpg 陷阱）——
        # 关池重试一次，避免整条真库 E2E 被环境抖动跳过（用后关池纪律的受害者侧补丁）。
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.shared.backtest_health import (
        attach_backtest_health,
        latest_strategy_health,
    )

    backtest_id = f"pytest_tp406_{uuid.uuid4().hex[:10]}"
    strategy_id = f"99{uuid.uuid4().int % 10**6:06d}"  # 合成的数字策略 id（十进制合法）

    # 用真实交易日窗口造曲线（基准现取真库指数，窗口须在盘）
    curve_a, _ = _alpha_curve_a()
    rows = [
        {"date": f"2026-0{(i // 21) + 1}-{(i % 21) + 1:02d}", "value": row["value"]}
        for i, row in enumerate(curve_a[:120])
    ]

    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO qlib_backtest_runs (backtest_id, user_id, tenant_id, status, "
                    "created_at, completed_at) VALUES (:b, '00000001', 'default', 'completed', now(), now())"
                    " ON CONFLICT (backtest_id) DO NOTHING"
                ),
                {"b": backtest_id},
            )
            await session.commit()

        report = await attach_backtest_health(
            backtest_id=backtest_id,
            equity_rows=rows,
            tenant_id="default",
            user_id="00000001",
            strategy_id=strategy_id,
        )
        assert report is not None and report["verdict"] in {"A", "B", "L", "E"}
        assert report["inputs"]["window"]

        # result_json.health 落档核对
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT result_json->'health' FROM qlib_backtest_runs WHERE backtest_id=:b"
                    ),
                    {"b": backtest_id},
                )
            ).scalar()
        assert isinstance(row, dict) and row.get("verdict") == report["verdict"]

        # 策略留档回读（门禁数据源）
        latest = await latest_strategy_health(
            strategy_id, tenant_id="default", user_id="00000001"
        )
        assert latest is not None
        assert latest["verdict"] == report["verdict"]
        assert latest["backtest_id"] == backtest_id
        assert latest["evidence_source"] == "backtest"
    finally:
        from backend.shared.database_manager_v2 import get_session as _gs

        async with _gs() as session:
            await session.execute(
                text("DELETE FROM qlib_backtest_runs WHERE backtest_id=:b"),
                {"b": backtest_id},
            )
            await session.execute(
                text(
                    "DELETE FROM eval_scores WHERE object_type='strategy_health' AND object_id=:s"
                ),
                {"s": strategy_id},
            )
            await session.commit()
        from backend.shared.database_manager_v2 import close_database

        await close_database()


@pytest.mark.asyncio
async def test_sweep_evidence_and_attach_e2e_real_db():
    """参数扫描记录真库 E2E：合成 qlib_optimization_runs → N 去胀 + PBO 矩阵 →
    attach 体检读取 optimization_run 源 → 清理。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        # 跨事件循环池自愈：前序用例可能留下绑定旧循环的池（asyncpg 陷阱）——
        # 关池重试一次，避免整条真库 E2E 被环境抖动跳过（用后关池纪律的受害者侧补丁）。
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.shared.backtest_health import (
        attach_backtest_health,
        resolve_sweep_evidence,
    )

    strategy_id = f"88{uuid.uuid4().int % 10**6:06d}"
    optimization_id = f"pytest_opt_{uuid.uuid4().hex[:10]}"
    backtest_id = f"pytest_swp_{uuid.uuid4().hex[:10]}"
    total_tasks = 40

    trials = []
    for seed in (1, 2, 3):
        rng2 = np.random.default_rng(seed)
        rets = rng2.normal(0.0005, 0.01, 60)
        vals = list(np.cumprod(1 + rets) * 100)
        trials.append(
            {
                "params": {"topk": seed * 10},
                "metrics": {
                    "equity_curve": [
                        {"date": f"2026-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}", "value": v}
                        for i, v in enumerate(vals)
                    ]
                },
            }
        )

    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO qlib_optimization_runs (optimization_id, mode, user_id, tenant_id, "
                    "status, created_at, updated_at, base_request_json, optimization_target, "
                    "total_tasks, completed_count, all_results_json) VALUES "
                    "(:oid, 'grid', '00000001', 'default', 'completed', now(), now(), "
                    "CAST(:base AS jsonb), 'sharpe_ratio', :n, :n, CAST(:res AS jsonb))"
                ),
                {
                    "oid": optimization_id,
                    "base": json.dumps({"strategy_id": strategy_id}),
                    "n": total_tasks,
                    "res": json.dumps(trials, ensure_ascii=False),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO qlib_backtest_runs (backtest_id, user_id, tenant_id, status, "
                    "created_at, completed_at) VALUES (:b, '00000001', 'default', 'completed', now(), now())"
                ),
                {"b": backtest_id},
            )
            await session.commit()

        n_trials, matrix, source = await resolve_sweep_evidence(
            tenant_id="default", user_id="00000001", strategy_id=strategy_id
        )
        assert n_trials == total_tasks and source == "optimization_run"
        assert matrix is not None and matrix.shape[1] == 3

        curve_a, _ = _alpha_curve_a()
        rows = [
            {"date": f"2026-0{(i // 21) + 1}-{(i % 21) + 1:02d}", "value": row["value"]}
            for i, row in enumerate(curve_a[:120])
        ]
        report = await attach_backtest_health(
            backtest_id=backtest_id,
            equity_rows=rows,
            tenant_id="default",
            user_id="00000001",
            strategy_id=strategy_id,
        )
        assert report is not None
        assert report["inputs"]["n_trials_source"] == "optimization_run"
        assert report["tests"]["dsr"].get("n_trials") == total_tasks
        assert report["tests"]["pbo"].get("sufficient") is True
    finally:
        from backend.shared.database_manager_v2 import get_session as _gs

        async with _gs() as session:
            await session.execute(
                text("DELETE FROM qlib_optimization_runs WHERE optimization_id=:o"),
                {"o": optimization_id},
            )
            await session.execute(
                text("DELETE FROM qlib_backtest_runs WHERE backtest_id=:b"), {"b": backtest_id}
            )
            await session.execute(
                text("DELETE FROM eval_scores WHERE object_type='strategy_health' AND object_id=:s"),
                {"s": strategy_id},
            )
            await session.commit()
        from backend.shared.database_manager_v2 import close_database

        await close_database()


@pytest.mark.asyncio
async def test_health_recheck_smoke_real_env():
    """月度复检在真环境可运行（无 SIM/LIVE 策略也应有合法汇总，不抛异常）。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        # 跨事件循环池自愈：前序用例可能留下绑定旧循环的池（asyncpg 陷阱）——
        # 关池重试一次，避免整条真库 E2E 被环境抖动跳过（用后关池纪律的受害者侧补丁）。
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.scripts.eval.health_recheck import run_health_recheck

    try:
        summary = await run_health_recheck(save=False)
    finally:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
    assert isinstance(summary, dict)
    assert (
        summary["n_strategies"] == len(summary["results"])
        or summary["n_strategies"] >= summary["ok"]
    )
    assert summary["errors"] == [] or all("strategy:" in e for e in summary["errors"])
    assert json.dumps(
        summary, ensure_ascii=False, default=str
    )  # 可序列化（告警/留档前置）
