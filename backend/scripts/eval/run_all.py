"""五卡 EOD 汇总（T-P4-05b）：因子/模型/策略/账户/每日选股 → eval_scores 落表。

- **单卡异常隔离**：任一张卡失败只记入 ``errors``，不影响其余卡与整体退出码语义
  （返回汇总，调用方按 errors 判断）；
- 每日选股按 ``--date`` 评当日（缺省最新交易日）；其余卡为当日快照；
- 进程内共享一个事件循环与 DB 池，结束统一 close（脚本口径，勿被 import 后残留）；
- 供 trade/main worker 与 schedule_ctl 手动重跑共用（唯一入口，禁止旁路）。

用法：python backend/scripts/eval/run_all.py [--date YYYY-MM-DD] [--no-save] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("eval.run_all")


def _score_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """结果列表 → {n, scored, errors}（纯函数，便于单测）。"""
    scored = [r for r in results if not r.get("error") and r.get("score") is not None]
    errors = [
        f"{r.get('object_type')}:{r.get('object_id')}: {r['error']}"
        for r in results
        if r.get("error")
    ]
    return {"n": len(results), "scored": len(scored), "errors": errors}


async def _save_result(result: dict[str, Any], *, snapshot_date: Any = None) -> bool:
    """统一落表：因子/模型/策略/账户结果 → eval_scores（object_type 自描述）。"""
    from backend.shared.eval_contract import save_eval_score

    if result.get("error") or result.get("score") is None:
        return False
    return await save_eval_score(
        object_type=str(result["object_type"]),
        object_id=str(result["object_id"]),
        snapshot_date=snapshot_date or date.today(),
        score=result.get("score"),
        grade=result.get("grade"),
        low_confidence=bool(result.get("low_confidence")),
        red_line_failed=result.get("red_line_failed") or [],
        dimensions=result.get("dimensions") or {},
        inputs_version=result.get("inputs_version") or {},
    )


async def _run_factor_card(save: bool, *, top: int, dataset: str) -> dict[str, Any]:
    from backend.scripts.eval.factor_card import score_factors

    results = score_factors(dataset, None, top)
    if save:
        for r in results:
            await _save_result(r)
    return _score_summary(results)


async def _run_model_card(save: bool, *, limit: int = 50) -> dict[str, Any]:
    from backend.scripts.eval.model_card import (
        list_production_models,
        list_user_models,
        score_model,
    )

    targets: list[tuple[str, Path | None]] = [
        (model_id, None) for model_id in list_production_models(limit=limit)
    ]
    try:
        targets += [
            (item["model_id"], Path(item["meta_path"]))
            for item in await list_user_models()
        ]
    except Exception as exc:  # noqa: BLE001 - 用户模型清单失败不拖垮系统模型卡
        logger.warning("[EvalRunAll] 用户模型清单获取失败: %s", exc)

    results: list[dict[str, Any]] = []
    for model_id, meta_path in targets:
        try:
            results.append(score_model(model_id, meta_path=meta_path))
        except Exception as exc:  # noqa: BLE001 - 单模型隔离
            results.append(
                {
                    "object_type": "model",
                    "object_id": model_id,
                    "error": f"评分失败: {exc}",
                }
            )
    if save:
        for r in results:
            await _save_result(r)
    return _score_summary(results)


async def _run_strategy_card(save: bool, *, limit: int) -> dict[str, Any]:
    from backend.scripts.eval.strategy_card import (
        candidate_backtest_ids,
        score_strategy,
    )

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    skipped = 0
    for backtest_id in await candidate_backtest_ids(limit=max(4, limit * 4)):
        if len(results) >= limit:
            break
        try:
            result = await score_strategy(backtest_id=backtest_id)
        except Exception as exc:  # noqa: BLE001 - 单回测隔离
            errors.append(f"strategy:{backtest_id}: 评分失败: {exc}")
            continue
        if result.get("error"):
            if result.get("skipped"):
                skipped += 1  # 结果文件已被清理的历史行（正常状态）
            else:
                errors.append(f"strategy:{backtest_id}: {result['error']}")
            continue
        results.append(result)
    if save:
        for r in results:
            await _save_result(r)
    summary = _score_summary(results)
    summary["skipped"] = skipped
    summary["errors"] = summary["errors"] + errors
    return summary


async def _run_account_card(
    save: bool, *, tenant: str, window_days: int
) -> dict[str, Any]:
    from backend.scripts.eval.account_card import score_all_accounts

    results = await score_all_accounts(
        tenant=tenant, window_days=window_days, save=save
    )
    # 未创建/空账户属正常状态（skipped），只有评分失败/Redis 不可用才是真异常
    skipped = [r for r in results if r.get("error") and r.get("skipped")]
    failed = [r for r in results if r.get("error") and not r.get("skipped")]
    return {
        "n": len(results),
        "scored": len(
            [r for r in results if not r.get("error") and r.get("score") is not None]
        ),
        "errors": [
            f"{r.get('object_type')}:{r.get('object_id')}: {r['error']}" for r in failed
        ],
        "skipped": len(skipped),
    }


async def _run_daily_selection(
    save: bool, *, trade_date: str | None, horizon: int
) -> dict[str, Any]:
    from backend.scripts.eval.daily_selection import score_daily_selection

    result = await score_daily_selection(trade_date, horizon=horizon, save=save)
    summary = _score_summary([result])
    summary["trade_date"] = result.get("trade_date")
    return summary


async def run_all(
    trade_date: str | None = None,
    *,
    save: bool = True,
    horizon: int = 5,
    tenant: str = "default",
    factor_top: int = 20,
    factor_dataset: str = "alpha_library",
    strategy_limit: int = 5,
    account_window: int = 30,
) -> dict[str, Any]:
    """顺序跑五卡（单卡隔离）→ 汇总。返回结构含各卡计数与 errors。"""
    started = time.time()
    cards: dict[str, Any] = {}

    async def _guard(name: str, coro) -> None:
        try:
            cards[name] = await coro
        except Exception as exc:  # noqa: BLE001 - 卡片级隔离
            logger.error("[EvalRunAll] %s 卡失败: %s", name, exc, exc_info=True)
            cards[name] = {"n": 0, "scored": 0, "errors": [f"{name} 卡异常: {exc}"]}

    await _guard(
        "factor", _run_factor_card(save, top=factor_top, dataset=factor_dataset)
    )
    await _guard("model", _run_model_card(save))
    await _guard("strategy", _run_strategy_card(save, limit=strategy_limit))
    await _guard(
        "account", _run_account_card(save, tenant=tenant, window_days=account_window)
    )
    await _guard(
        "daily_selection",
        _run_daily_selection(save, trade_date=trade_date, horizon=horizon),
    )

    total_scored = sum(int(c.get("scored") or 0) for c in cards.values())
    total_errors = [e for c in cards.values() for e in (c.get("errors") or [])]
    summary = {
        "date": trade_date or date.today().isoformat(),
        "save": bool(save),
        "tenant": tenant,
        "cards": cards,
        "total_scored": total_scored,
        "total_errors": len(total_errors),
        "errors": total_errors,
        "elapsed_sec": round(time.time() - started, 2),
    }
    logger.info(
        "[EvalRunAll] %s 完成：落分 %d，异常 %d，耗时 %.1fs",
        summary["date"],
        total_scored,
        len(total_errors),
        summary["elapsed_sec"],
    )
    return summary


async def _main_async(args) -> dict[str, Any]:
    from backend.shared.database_manager_v2 import close_database

    try:
        return await run_all(
            args.date,
            save=not args.no_save,
            horizon=args.horizon,
            tenant=args.tenant,
            strategy_limit=args.strategy_limit,
        )
    finally:
        await close_database()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="五卡 EOD 评分汇总（T-P4-05b）")
    parser.add_argument(
        "--date", default=None, help="每日选股交易日 YYYY-MM-DD（缺省最新）"
    )
    parser.add_argument("--no-save", action="store_true", help="只评不落表（演练）")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--strategy-limit", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    summary = asyncio.run(_main_async(args))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"五卡 EOD 汇总（{summary['date']}，save={summary['save']}，"
            f"落分 {summary['total_scored']}，异常 {summary['total_errors']}，"
            f"耗时 {summary['elapsed_sec']}s）"
        )
        for name, card in summary["cards"].items():
            errors = card.get("errors") or []
            print(
                f"  {name}: 评 {card.get('scored')}/{card.get('n')} "
                f"（跳过 {card.get('skipped', 0)}，异常 {len(errors)}）"
            )
            for e in errors[:5]:
                print(f"    ⚠ {e}")
    return 0 if summary["total_scored"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
