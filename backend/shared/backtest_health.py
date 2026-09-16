"""回测体检接入层（T-P4-06）：三处强制接入的共享实现（设计《评估与打分体系》§6.3）。

三处接入：
1. **回测完成自动体检**：`BacktestPersistence.save_run`（status=completed）后台跑九项 →
   报告落 `qlib_backtest_runs.result_json.health`（证据卡），策略回测同时落
   `eval_scores`（object_type=strategy_health，晋级门禁/月度复检读）；
2. **晋级门禁**：`promotion_gate()` 纯函数——SIM/LIVE 晋级须体检 A/B；L/E 不得晋级；
3. **月度复检**：`scripts/eval/health_recheck.py` 每月对 SIM/LIVE 策略重算并留档。

纪律：
- 九项与判定的**唯一实现**在 `scripts/eval/health_check.py`（本模块只做 IO 与编排，禁止复制公式）；
- best-effort：体检失败只告警，绝不回滚/阻塞回测结果落库；
- 开关注入：``BACKTEST_HEALTH_ENABLED``（自动体检，默认开）、``HEALTH_GATE_ENABLED``
  （晋级门禁，默认开）、``HEALTH_RECHECK_ENABLED``（月度复检，默认开）。

基准/regime 指数按**回测窗口**取数（不是"最近 N 天"——历史回测的窗口在当下之前）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARK = "000300.SH"
DEFAULT_REGIME_INDEX = "000300.SH"
MIN_CURVE_DAYS = 30  # 曲线点 < 此值连回归都做不了，直接跳过（不产垃圾报告）

# 晋级门槛（T-P3-05 门槛总表唯一事实源——改这里即改全平台口径）
SIM_REQUIRED_VERDICTS = ("A", "B")  # B 允许晋级但须在门禁信息中标注收益来源
SIM_MIN_SAMPLES_DAYS = 60  # 体检曲线至少 60 个点，否则无从判 A/B
LIVE_SIM_MIN_TRADING_DAYS = (
    20  # LIVE：SIM 运行 ≥ 20 交易日（v1 由状态时间近似，见文档）
)
LIVE_MAX_TRACKING_ERROR = 0.15  # LIVE：模拟↔真单跟踪误差上限（有数据才校验）


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def health_enabled() -> bool:
    return _env_bool("BACKTEST_HEALTH_ENABLED", True)


def gate_enabled() -> bool:
    return _env_bool("HEALTH_GATE_ENABLED", True)


def _iso_to_dt(iso: str) -> int:
    return int(str(iso).replace("-", "")[:8])


# ── 纯函数：判定与门禁 ──────────────────────────────────────────────


def evaluate_equity_curve(
    equity_rows: list[dict[str, Any]] | None,
    *,
    benchmark_closes: list[float] | None = None,
    regime_closes: list[float] | None = None,
    daily_turnover: list[float] | None = None,
    performance_matrix: Any | None = None,
    n_trials: int = 1,
) -> dict[str, Any] | None:
    """净值曲线（[{date,value}] 或 [float]）→ 九项体检报告；样本不足 → None。"""
    from backend.scripts.eval.health_check import nav_curve_to_returns, run_health_check

    if not equity_rows:
        return None
    if isinstance(equity_rows[0], dict):
        nav = [row.get("value", row.get("nav")) for row in equity_rows]
    else:
        nav = list(equity_rows)
    nav = [float(v) for v in nav if v is not None]

    returns = nav_curve_to_returns(nav)
    if len(returns) < MIN_CURVE_DAYS:
        return None
    n = len(returns)

    def _to_returns(closes: list[float] | None):
        if not closes or len(closes) < 2:
            return None
        arr = np.asarray([float(c) for c in closes], dtype=float)
        prev = arr[:-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            rets = arr[1:] / prev - 1.0
        rets = rets[np.isfinite(rets)]
        return rets if len(rets) else None

    bench_returns = _to_returns(benchmark_closes)
    regime_returns = _to_returns(regime_closes)
    # 序列长度对齐（benchmark/regime 取窗口尾部与策略同日数，防长度错配的静默偏差）
    if bench_returns is not None and len(bench_returns) > n:
        bench_returns = bench_returns[-n:]
    if regime_returns is not None and len(regime_returns) > n:
        regime_returns = regime_returns[-n:]

    report = run_health_check(
        returns,
        benchmark_returns=bench_returns,
        index_closes=regime_returns if regime_returns is not None else None,
        daily_turnover=daily_turnover,
        performance_matrix=performance_matrix,
        n_trials=max(1, int(n_trials or 1)),
    )
    report["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return report


def promotion_gate(
    health: dict[str, Any] | None,
    *,
    mode: str = "SIMULATION",
    sim_trading_days: int | None = None,
    tracking_error: float | None = None,
) -> tuple[bool, str]:
    """晋级门禁（纯函数，T-P3-05 门槛总表）→ (是否放行, 说明)。

    - SIMULATION：须有体检记录且结论 ∈ {A, B}（B 在说明中标注收益来源）；
      无记录（未体检）/E（证据不足）/L（运气嫌疑）→ 拒绝；
    - REAL：在 SIM 门槛之上，SIM 运行 ≥ ``LIVE_SIM_MIN_TRADING_DAYS`` 交易日
      （``sim_trading_days=None`` 表示无状态史可考 → v1 只提示不拦）+
      跟踪误差 ≤ ``LIVE_MAX_TRACKING_ERROR``（``tracking_error=None`` 同上）。
    """
    mode_key = str(mode or "").strip().upper()
    if health is None:
        return False, (
            "策略尚未完成回测体检（T-P4-06）：请在本页重跑一次回测，"
            "回测完成后自动生成体检报告，结论 A/B 方可晋级。"
        )
    verdict = str(health.get("verdict") or "").strip().upper()
    confidence = health.get("confidence")
    reasons = "；".join(str(r) for r in (health.get("reasons") or [])[:2])
    if verdict not in SIM_REQUIRED_VERDICTS:
        label = health.get("verdict_label") or verdict
        return False, (
            f"回测体检结论为 {verdict}（{label}），不得晋级（T-P4-06 门禁需 A/B）。"
            + (f" 原因：{reasons}" if reasons else "")
            + (
                " 建议："
                + "；".join(str(s) for s in (health.get("suggestions") or [])[:2])
                if health.get("suggestions")
                else ""
            )
        )
    if mode_key == "SIMULATION":
        note = f"体检结论 {verdict}（可信度 {confidence}/100）"
        if verdict == "B":
            note += f"；B=收益主要来自市场/风格暴露：{reasons or '详见体检报告'}"
        return True, note
    # REAL
    if sim_trading_days is not None and sim_trading_days < LIVE_SIM_MIN_TRADING_DAYS:
        return False, (
            f"模拟盘运行 {sim_trading_days} 交易日 < 门槛 {LIVE_SIM_MIN_TRADING_DAYS} 日"
            "（T-P3-05 晋级门槛），请继续模拟验证。"
        )
    if tracking_error is not None and tracking_error > LIVE_MAX_TRACKING_ERROR:
        return False, (
            f"模拟↔真单跟踪误差 {tracking_error:.2%} > 门槛 {LIVE_MAX_TRACKING_ERROR:.0%}"
            "（T-P3-05 晋级门槛），请先排查执行偏差。"
        )
    return True, f"体检结论 {verdict}；模拟证据达标（T-P3-05）"


# ── IO：窗口取数 ────────────────────────────────────────────────────


def load_index_closes_between(symbol: str, start_iso: str, end_iso: str) -> list[float]:
    """按 [start, end]（ISO）取指数收盘（QuantDB index_daily）→ list[float]（升序）。"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        df = hub.fetch_series(
            "qdb_index_daily",
            symbol,
            _iso_to_dt(start_iso),
            _iso_to_dt(end_iso),
            columns=["close"],
        )
        if df is None or len(df) == 0:
            return []
        return df.sort_values("dt")["close"].astype(float).tolist()
    except Exception as exc:  # noqa: BLE001 - 数据缺失降级为"体检缺基准"
        logger.warning("[BacktestHealth] 指数取数失败 %s: %s", symbol, exc)
        return []


def _curve_window(equity_rows: list[dict[str, Any]]) -> tuple[str, str] | None:
    dates = [
        str(row.get("date"))
        for row in equity_rows
        if isinstance(row, dict) and row.get("date")
    ]
    if len(dates) < 2:
        return None
    return dates[0][:10], dates[-1][:10]


# ── 用例 A：回测完成自动体检 ────────────────────────────────────────


async def evaluate_for_window(
    equity_rows: list[dict[str, Any]] | None,
    *,
    benchmark_symbol: str | None = None,
    n_trials: int = 1,
    daily_turnover: list[float] | None = None,
    performance_matrix: Any | None = None,
) -> dict[str, Any] | None:
    """按曲线窗口装载基准/regime → 九项体检报告（回测挂钩与月度复检共用）。"""
    if not equity_rows or len(equity_rows) < MIN_CURVE_DAYS:
        return None
    window = _curve_window(equity_rows)
    if not window:
        return None
    start_iso, end_iso = window
    bench_symbol = str(benchmark_symbol or DEFAULT_BENCHMARK)
    # regime 需要 60 日趋势代理 → 起点回看 90 个自然日
    regime_start = (date.fromisoformat(start_iso) - timedelta(days=90)).isoformat()
    benchmark_closes = load_index_closes_between(bench_symbol, start_iso, end_iso)
    regime_closes = load_index_closes_between(
        DEFAULT_REGIME_INDEX, regime_start, end_iso
    )

    report = evaluate_equity_curve(
        equity_rows,
        benchmark_closes=benchmark_closes,
        regime_closes=regime_closes,
        daily_turnover=daily_turnover,
        performance_matrix=performance_matrix,
        n_trials=max(1, int(n_trials or 1)),
    )
    if report is None:
        return None
    report["inputs"] = {
        "benchmark": bench_symbol,
        "window": [start_iso, end_iso],
        "n_trials": int(report.get("n_trials") or 1),
    }
    return report


async def attach_backtest_health(
    *,
    backtest_id: str,
    equity_rows: list[dict[str, Any]] | None,
    tenant_id: str = "default",
    user_id: str = "",
    strategy_id: str | None = None,
    benchmark_symbol: str | None = None,
    n_trials: int | None = None,
) -> dict[str, Any] | None:
    """回测完成 → 九项体检 → 报告落 result_json.health（＋策略关联落 eval_scores）。"""
    if not health_enabled():
        return None
    if not equity_rows or len(equity_rows) < MIN_CURVE_DAYS:
        logger.info(
            "[BacktestHealth] %s 曲线点不足（%s），跳过体检",
            backtest_id,
            len(equity_rows or []),
        )
        return None
    report = await evaluate_for_window(
        equity_rows,
        benchmark_symbol=benchmark_symbol,
        n_trials=int(
            n_trials
            if n_trials is not None
            else os.getenv("BACKTEST_HEALTH_TRIALS", "1")
        ),
    )
    if report is None:
        return None

    await _store_on_run(backtest_id=backtest_id, report=report)
    sid = str(strategy_id or "").strip()
    if sid.isdigit():
        await record_strategy_health(
            strategy_id=sid,
            backtest_id=backtest_id,
            report=report,
            tenant_id=tenant_id,
            user_id=user_id,
            evidence_source="backtest",
        )
    logger.info(
        "[BacktestHealth] %s 体检完成：%s（可信度 %s）strategy=%s",
        backtest_id,
        report.get("verdict"),
        report.get("confidence"),
        sid or "-",
    )
    return report


async def _store_on_run(*, backtest_id: str, report: dict[str, Any]) -> bool:
    """result_json.health 幂等写入（jsonb_set 合并，保留原有摘要字段）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session() as session:
            await session.execute(
                _text(
                    "UPDATE qlib_backtest_runs SET result_json = "
                    "jsonb_set(COALESCE(result_json, '{}'::jsonb), '{health}', "
                    "CAST(:report AS jsonb), true) WHERE backtest_id = :bid"
                ),
                {
                    "report": json.dumps(report, ensure_ascii=False, default=str),
                    "bid": backtest_id,
                },
            )
            await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 证据卡附属信息，不阻断
        logger.warning(
            "[BacktestHealth] %s 体检报告写入失败（不阻断）: %s", backtest_id, exc
        )
        return False


async def record_strategy_health(
    *,
    strategy_id: str,
    backtest_id: str | None,
    report: dict[str, Any],
    tenant_id: str,
    user_id: str,
    evidence_source: str = "backtest",
) -> bool:
    """策略维度体检留档（eval_scores object_type=strategy_health，门禁/复检读取）。"""
    from backend.shared.eval_contract import save_eval_score

    tests = report.get("tests") or {}
    dims = {
        "verdict_label": report.get("verdict_label"),
        "reasons": report.get("reasons") or [],
        "suggestions": report.get("suggestions") or [],
        "items": {
            "alpha_t": (tests.get("factor_regression") or {}).get("alpha_t"),
            "alpha_annual": (tests.get("factor_regression") or {}).get("alpha_annual"),
            "r2": (tests.get("factor_regression") or {}).get("r2"),
            "dsr": (tests.get("dsr") or {}).get("dsr"),
            "min_trl_adequate": (tests.get("min_trl") or {}).get("adequate"),
            "bootstrap_ci_crosses_zero": (tests.get("bootstrap") or {}).get(
                "return_ci_crosses_zero"
            ),
            "concentration_kills_alpha": (tests.get("concentration") or {}).get(
                "kills_alpha"
            ),
            "regimes_covered": (tests.get("regime") or {}).get("regimes_covered"),
        },
    }
    return await save_eval_score(
        object_type="strategy_health",
        object_id=strategy_id,
        snapshot_date=date.today(),
        score=report.get("confidence"),
        grade=report.get("verdict"),
        low_confidence=report.get("verdict") == "E",
        red_line_failed=(["verdict_luck"] if report.get("verdict") == "L" else []),
        dimensions=dims,
        inputs_version={
            "backtest_id": backtest_id,
            "evidence_source": evidence_source,
            "n_trials": report.get("n_trials"),
            "inputs": report.get("inputs") or {},
            "generated_at": report.get("generated_at"),
        },
        tenant_id=tenant_id or "default",
        user_id=str(user_id or ""),
    )


async def latest_strategy_health(
    strategy_id: str, *, tenant_id: str = "default", user_id: str | None = None
) -> dict[str, Any] | None:
    """策略最近一次体检留档 → 门禁/复检读取（无 → None）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    sid = str(strategy_id or "").strip()
    if not sid.isdigit():
        return None
    try:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        _text(
                            "SELECT score, grade, low_confidence, dimensions, inputs_version, "
                            "snapshot_date, created_at FROM eval_scores "
                            "WHERE object_type='strategy_health' AND object_id=:sid "
                            "AND tenant_id=:tid"
                            + (" AND user_id=:uid" if user_id is not None else "")
                            + " ORDER BY snapshot_date DESC, created_at DESC LIMIT 1"
                        ),
                        {
                            "sid": sid,
                            "tid": tenant_id or "default",
                            **({"uid": str(user_id)} if user_id is not None else {}),
                        },
                    )
                )
                .mappings()
                .first()
            )
    except Exception as exc:  # noqa: BLE001 - 门禁读失败按"无记录"处理（拒绝=安全侧）
        logger.warning("[BacktestHealth] 体检留档读取失败: %s", exc)
        return None
    if not row:
        return None
    dims = row["dimensions"] or {}
    inputs = row["inputs_version"] or {}
    return {
        "verdict": row["grade"],
        "verdict_label": dims.get("verdict_label"),
        "confidence": row["score"],
        "low_confidence": bool(row["low_confidence"]),
        "reasons": dims.get("reasons") or [],
        "suggestions": dims.get("suggestions") or [],
        "items": dims.get("items") or {},
        "backtest_id": inputs.get("backtest_id"),
        "evidence_source": inputs.get("evidence_source") or "backtest",
        "n_trials": inputs.get("n_trials"),
        "snapshot_date": row["snapshot_date"].isoformat()
        if row["snapshot_date"]
        else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# ── SIM 运行时长（T-P3-05 LIVE 门槛 v1）与跟踪误差读取 ──────────────


def sim_start_key(tenant_id: str, user_id: str, strategy_id: str) -> str:
    """SIM 首启时间戳键（首启优先，重启不刷新——v1 近似口径，见 T-P3-05 表）。"""
    return f"strategy:sim_start:{tenant_id or 'default'}:{user_id}:{strategy_id}"


def stamp_sim_start(
    redis_like: Any, tenant_id: str, user_id: str, strategy_id: str
) -> None:
    """SIM 启动打点（NX 幂等：保留最早首启；best-effort，不阻断启动）。"""
    sid = str(strategy_id or "").strip()
    if not sid or redis_like is None:
        return
    try:
        client = getattr(redis_like, "client", None) or redis_like
        client.set(
            sim_start_key(tenant_id, user_id, sid),
            date.today().isoformat(),
            nx=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BacktestHealth] SIM 首启打点失败（不阻断）: %s", exc)


def trading_days_between(start_iso: str, end_iso: str) -> int:
    """交易日数（区间左开右闭语义：计数 (start, end] 的交易日）。

    优先 QuantDB 指数日历（index_daily 有行即交易日）；数据不可用回退工作日近似。
    """
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        df = hub.fetch_series(
            "qdb_index_daily",
            DEFAULT_BENCHMARK,
            _iso_to_dt(start_iso),
            _iso_to_dt(end_iso),
            columns=["close"],
        )
        if df is not None and len(df):
            days = sorted({int(d) for d in df["dt"]})
            return sum(
                1 for d in days if _iso_to_dt(start_iso) < d <= _iso_to_dt(end_iso)
            )
    except Exception:  # noqa: BLE001 - 回退工作日近似
        pass
    start = date.fromisoformat(str(start_iso)[:10])
    end = date.fromisoformat(str(end_iso)[:10])
    n = 0
    probe = start + timedelta(days=1)
    while probe <= end:
        if probe.weekday() < 5:
            n += 1
        probe += timedelta(days=1)
    return n


def sim_trading_days_since(
    redis_like: Any, tenant_id: str, user_id: str, strategy_id: str
) -> int | None:
    """自 SIM 首启以来的交易日数；无打点记录（历史启动）→ None（门禁按"无数据只提示"）。"""
    sid = str(strategy_id or "").strip()
    if not sid or redis_like is None:
        return None
    try:
        client = getattr(redis_like, "client", None) or redis_like
        raw = client.get(sim_start_key(tenant_id, user_id, sid))
        if not raw:
            return None
        start_iso = raw.decode() if isinstance(raw, bytes) else str(raw)
        return trading_days_between(start_iso[:10], date.today().isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BacktestHealth] SIM 时长读取失败: %s", exc)
        return None


def latest_user_tracking_error(redis_like: Any, user_id: str) -> float | None:
    """最近影子对照日报的模拟↔真单年化跟踪误差（小数）；无足够数据 → None（只提示不拦）。"""
    try:
        from backend.services.trade.services.shadow_compare_service import (
            load_latest_report,
        )

        report = load_latest_report(redis_like)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BacktestHealth] 影子对照日报读取失败: %s", exc)
        return None
    if not report:
        return None
    tracking = report.get("tracking_error")
    if not isinstance(tracking, dict):
        return None
    uid = str(user_id or "").strip()
    candidates = [uid]
    if uid.isdigit():
        candidates += [str(int(uid)), str(int(uid)).zfill(8)]
    for key in candidates:
        item = tracking.get(key)
        if (
            isinstance(item, dict)
            and item.get("sufficient")
            and item.get("te_ann_bps") is not None
        ):
            return float(item["te_ann_bps"]) / 10000.0
    return None


def schedule_health_check(
    *,
    backtest_id: str,
    equity_rows: list[dict[str, Any]] | None,
    tenant_id: str = "default",
    user_id: str = "",
    strategy_id: str | None = None,
    benchmark_symbol: str | None = None,
) -> bool:
    """后台调度体检（回测落库路径专用）：无事件循环/失败只告警，绝不阻塞回测。"""
    if not health_enabled() or not equity_rows:
        return False
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("[BacktestHealth] 无事件循环，跳过 %s 体检", backtest_id)
        return False

    async def _run() -> None:
        try:
            await attach_backtest_health(
                backtest_id=backtest_id,
                equity_rows=equity_rows,
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                benchmark_symbol=benchmark_symbol,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning(
                "[BacktestHealth] %s 后台体检异常（不阻断）: %s", backtest_id, exc
            )

    loop.create_task(_run())
    return True
