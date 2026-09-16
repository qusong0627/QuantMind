"""月度体检复检（T-P4-06 ③）：SIM/LIVE 策略每月重跑回测体检并留档。

设计意图（《评估与打分体系》§6.3-3）："实盘/模拟策略每月自动体检一次——曲线增长后
结论可能变化（E→A 或 L→L 坐实）"。

证据源（v1 优先级，落档时如实标 evidence_source）：
1. ``sim_equity``：该策略当前是活跃策略且模拟盘净值序列 ≥30 点 → 用**模拟盘真实曲线**
   （"曲线增长"的本义；CN 账户级净值，v1 口径见 T-P3-05 表）；
2. ``backtest``：回退到最近一次体检留档的 backtest_id 对应回测曲线；
3. 两者皆无 → 记 skipped（待体检），不造假。

结论退化（上期 A/B → 本期 L/E）→ 写 Redis 告警键 ``health:recheck:alert:{tenant}:{user}:{sid}``
（TTL 90 天）+ ERROR 日志（通知中心接线留待前端批次）。

用法：python backend/scripts/eval/health_recheck.py [--tenant default] [--no-save] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.backtest_health import (  # noqa: E402
    MIN_CURVE_DAYS,
    evaluate_for_window,
    latest_strategy_health,
    record_strategy_health,
    resolve_sweep_evidence,
)
from backend.shared.simulation_account_keys import active_strategy_key  # noqa: E402

logger = logging.getLogger("eval.health_recheck")

ALERT_TTL_SECONDS = 90 * 24 * 3600
DEGRADED_VERDICTS = {"L", "E"}


def should_alert(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """结论退化告警判定（纯函数）：曾 A/B、现 L/E 才告警（E→A 是改善，不打扰）。"""
    prev_verdict = str((previous or {}).get("verdict") or "").strip().upper()
    cur_verdict = str(current.get("verdict") or "").strip().upper()
    return prev_verdict in {"A", "B"} and cur_verdict in DEGRADED_VERDICTS


async def _resolve_notification_user_id(raw: str) -> str:
    """策略 owner（int 归一形态，如 "1"）→ users 表实际存在的 user_id（如 "00000001"）。

    notifications 表对 users(user_id) 有 FK——直接拿策略 owner 发通知会 FK 失败且被
    publisher 的 best-effort 吞掉（静默无通知）。此处在 users 候选键形中实查，
    查不到返回空串（调用方跳过发布并告警，不静默发错人）。
    """
    base = str(raw or "").strip()
    if not base:
        return ""
    candidates = [base]
    if base.isdigit():
        candidates.extend([base.zfill(8), str(int(base))])
    candidates = list(dict.fromkeys(candidates))
    try:
        from sqlalchemy import text as _text

        from backend.shared.database_manager_v2 import get_session

        placeholders = ", ".join(f":c{i}" for i in range(len(candidates)))
        params = {f"c{i}": c for i, c in enumerate(candidates)}
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    _text(
                        f"SELECT user_id FROM users WHERE user_id IN ({placeholders})"
                    ),
                    params,
                )
            ).fetchall()
        found = {str(r[0]) for r in rows}
        for c in candidates:
            if c in found:
                return c
    except Exception as exc:  # noqa: BLE001 - 解析失败按原值兜底（发布失败只告警）
        logger.warning("[HealthRecheck] 通知收件人解析失败: %s", exc)
    return base


async def _publish_degradation_notification(
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    strategy_name: str,
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> None:
    """退化告警接入通知中心（T-P4-06 通知接线）：PG notifications + WS 实时推送。

    best-effort：失败只告警——Redis 告警键（health:recheck:alert:*）与 ERROR 日志
    仍是兜底证据。收件人经 users 表实查（FK 保证），查不到则跳过并点名。
    """
    try:
        resolved = await _resolve_notification_user_id(user_id)
        if not resolved:
            logger.warning(
                "[HealthRecheck] 策略 %s 退化告警：收件人无法解析（user_id=%s），"
                "跳过通知中心发布",
                strategy_id,
                user_id,
            )
            return
        from backend.shared.notification_publisher import publish_notification_async

        prev_verdict = (previous or {}).get("verdict")
        cur_verdict = current.get("verdict")
        reasons = "；".join(str(r) for r in (current.get("reasons") or [])[:2])
        name = strategy_name or f"策略 {strategy_id}"
        await publish_notification_async(
            user_id=resolved,
            tenant_id=tenant_id,
            title=f"体检复检：{name} 结论退化 {prev_verdict} → {cur_verdict}",
            content=(
                f"月度复检结论由 {prev_verdict} 退化为 {cur_verdict}"
                f"（可信度 {current.get('confidence')}）。{reasons}。"
                "按晋级门槛总表，L/E 结论不可晋级；请在策略管理查看体检报告并处理。"
            ),
            type="health",
            level="error" if str(cur_verdict).upper() == "L" else "warning",
            action_url="/strategy",
            expire_days=90,
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败不阻断复检
        logger.warning("[HealthRecheck] 退化通知发布失败（不阻断）: %s", exc)


def _redis_client():
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis

    client = get_trade_redis()
    if getattr(client, "client", None) is None:
        client.connect()
    return client


def _active_strategy_id(redis_like, tenant_id: str, user_id: str) -> str | None:
    """当前活跃策略 id（Redis trade:active_strategy，payload.strategy_id）。"""
    try:
        raw_client = getattr(redis_like, "client", None) or redis_like
        raw = raw_client.get(active_strategy_key(tenant_id, user_id))
        if not raw:
            return None
        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        if isinstance(payload, dict):
            sid = str(payload.get("strategy_id") or "").strip()
            return sid or None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[HealthRecheck] 活跃策略读取失败 %s/%s: %s", tenant_id, user_id, exc
        )
    return None


async def _sim_equity_rows(tenant_id: str, user_id: str) -> list[dict[str, Any]]:
    """模拟盘净值序列 → [{date, value}]（复用账户卡序列装载，键形探测同纪律）。"""
    from backend.scripts.eval.account_card import load_fund_series

    series = await load_fund_series(tenant_id, user_id, days=400)
    return [{"date": d, "value": v} for d, v in series]


async def _backtest_curve_rows(backtest_id: str) -> list[dict[str, Any]]:
    """最近体检留档所指回测的净值曲线（复用策略卡装载，含文件探测）。"""
    from backend.scripts.eval.strategy_card import load_backtest_curve

    loaded = await load_backtest_curve(backtest_id, None)
    if loaded.get("error"):
        return []
    dates = loaded.get("dates") or []
    values = loaded.get("equity_curve") or []
    return [{"date": d, "value": v} for d, v in zip(dates, values, strict=False)]


async def _sim_strategies() -> list[dict[str, Any]]:
    """SIM/LIVE 的策略清单（strategies 表无 tenant 列；status 存量含别名，统一大小写比较）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT id, user_id, name, UPPER(COALESCE(status,'')) AS status "
                        "FROM strategies WHERE UPPER(COALESCE(status,'')) IN ('SIM','LIVE') "
                        "ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def run_health_recheck(
    *, tenant_id: str = "default", save: bool = True
) -> dict[str, Any]:
    """执行一轮月度复检 → 汇总（单策略异常隔离，不拖垮整轮）。"""
    redis = _redis_client()
    strategies = await _sim_strategies()
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for strat in strategies:
        sid = str(strat["id"])
        user_id = str(strat.get("user_id") or "")
        try:
            previous = await latest_strategy_health(
                sid, tenant_id=tenant_id, user_id=user_id
            )
            evidence_source = "backtest"
            rows: list[dict[str, Any]] = []
            if _active_strategy_id(redis, tenant_id, user_id) == sid:
                rows = await _sim_equity_rows(tenant_id, user_id)
                if len(rows) >= MIN_CURVE_DAYS:
                    evidence_source = "sim_equity"
                else:
                    rows = []
            if not rows and (previous or {}).get("backtest_id"):
                rows = await _backtest_curve_rows(str((previous or {})["backtest_id"]))
            if not rows:
                results.append(
                    {
                        "strategy_id": sid,
                        "user_id": user_id,
                        "status": "skipped",
                        "reason": "无可用证据曲线（模拟净值不足且无体检留档回测）",
                    }
                )
                continue

            n_trials, matrix, trials_source = await resolve_sweep_evidence(
                tenant_id=tenant_id, user_id=user_id, strategy_id=sid
            )
            if trials_source == "default" and (previous or {}).get("n_trials"):
                n_trials = max(1, int(previous["n_trials"]))  # 沿用上期口径，防复检口径漂移
            report = await evaluate_for_window(
                rows, n_trials=n_trials, performance_matrix=matrix
            )
            if report is None:
                results.append(
                    {
                        "strategy_id": sid,
                        "user_id": user_id,
                        "status": "skipped",
                        "reason": f"曲线点不足（{len(rows)} < {MIN_CURVE_DAYS}）",
                    }
                )
                continue

            entry: dict[str, Any] = {
                "strategy_id": sid,
                "user_id": user_id,
                "status": "ok",
                "verdict": report["verdict"],
                "confidence": report["confidence"],
                "previous_verdict": (previous or {}).get("verdict"),
                "evidence_source": evidence_source,
                "n_trials": n_trials,
                "n_trials_source": trials_source,
            }
            if save:
                await record_strategy_health(
                    strategy_id=sid,
                    backtest_id=(previous or {}).get("backtest_id")
                    if evidence_source == "backtest"
                    else None,
                    report=report,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    evidence_source=evidence_source,
                )
            if should_alert(previous, report):
                entry["alert"] = True
                _write_alert(redis, tenant_id, user_id, sid, previous, report)
                # T-P4-06 通知接线：退化告警进通知中心（best-effort，不阻断复检）
                await _publish_degradation_notification(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    strategy_id=sid,
                    strategy_name=str(strat.get("name") or ""),
                    previous=previous,
                    current=report,
                )
                logger.error(
                    "[HealthRecheck] 策略 %s 体检结论退化：%s → %s（%s）",
                    sid,
                    (previous or {}).get("verdict"),
                    report["verdict"],
                    "；".join(report.get("reasons") or [])[:200],
                )
            results.append(entry)
        except Exception as exc:  # noqa: BLE001 - 单策略隔离
            errors.append(f"strategy:{sid}: {exc}")
            results.append(
                {
                    "strategy_id": sid,
                    "user_id": user_id,
                    "status": "error",
                    "error": str(exc),
                }
            )

    summary = {
        "date": date.today().isoformat(),
        "tenant": tenant_id,
        "n_strategies": len(strategies),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "alerts": sum(1 for r in results if r.get("alert")),
        "errors": errors,
        "results": results,
    }
    logger.info(
        "[HealthRecheck] %s 完成：策略 %d（复检 %d / 跳过 %d / 告警 %d / 异常 %d）",
        summary["date"],
        summary["n_strategies"],
        summary["ok"],
        summary["skipped"],
        summary["alerts"],
        len(errors),
    )
    return summary


def _write_alert(
    redis_like,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> None:
    """退化告警落 Redis（best-effort；通知中心接线留待前端批次）。"""
    try:
        raw_client = getattr(redis_like, "client", None) or redis_like
        key = f"health:recheck:alert:{tenant_id}:{user_id}:{strategy_id}"
        raw_client.set(
            key,
            json.dumps(
                {
                    "strategy_id": strategy_id,
                    "previous_verdict": (previous or {}).get("verdict"),
                    "verdict": current.get("verdict"),
                    "confidence": current.get("confidence"),
                    "reasons": current.get("reasons") or [],
                    "date": date.today().isoformat(),
                },
                ensure_ascii=False,
            ),
            ex=ALERT_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[HealthRecheck] 退化告警写入失败（不阻断）: %s", exc)


async def _main_async(args) -> dict[str, Any]:
    from backend.shared.database_manager_v2 import close_database

    try:
        return await run_health_recheck(tenant_id=args.tenant, save=not args.no_save)
    finally:
        await close_database()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="月度体检复检（T-P4-06 ③）")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--no-save", action="store_true", help="只复检不落档（演练）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    summary = asyncio.run(_main_async(args))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"体检复检（{summary['date']}）：策略 {summary['n_strategies']} 个 → "
            f"复检 {summary['ok']} / 跳过 {summary['skipped']} / 告警 {summary['alerts']} / "
            f"异常 {len(summary['errors'])}"
        )
        for r in summary["results"]:
            if r["status"] == "ok":
                print(
                    f"  {r['strategy_id']}: {r.get('previous_verdict')} → {r['verdict']}"
                    f"（可信度 {r['confidence']}，证据 {r['evidence_source']}）"
                    + ("  ⚠退化告警" if r.get("alert") else "")
                )
            else:
                print(
                    f"  {r['strategy_id']}: {r['status']} — {r.get('reason') or r.get('error')}"
                )
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
