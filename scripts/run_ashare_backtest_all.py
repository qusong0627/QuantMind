#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量回测全部 A 股策略模板，结果写入 JSON（在 quantmind 容器内运行）。

- 顺序执行（单个 qlib 回测会占满 ~10 核，串行避免拖垮线上服务）；
- 可断点续跑：已完成的模板直接跳过，重跑只补缺失项；
- 结果供 gen_ashare_strategy_templates.py 写进模板 .py 开头注释。

用法（容器内）：
    docker exec quantmind python /app/scripts/run_ashare_backtest_all.py \
        --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7 \
        --start 2024-01-02 --end 2024-12-31
    # 只跑某几个 / 强制重跑
    docker exec quantmind python /app/scripts/run_ashare_backtest_all.py --ids as01_core_multifactor as16_momentum_20 --force
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

TEMPLATE_DIR = Path("/app/strategy_templates")
if not TEMPLATE_DIR.exists():
    TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "strategy_templates"

RESULT_PATH = Path("/app/scripts/ashare_backtest_results.json")
if not Path("/app/scripts").exists():
    RESULT_PATH = Path(__file__).resolve().parent / "ashare_backtest_results.json"

# 与回测对照的基线模板（平台内置，无 A 股定制）
BASELINE_ID = "standard_topk"


def _all_template_ids() -> list[str]:
    return sorted(p.stem for p in TEMPLATE_DIR.glob("as*.json"))


def _load_results() -> dict:
    if RESULT_PATH.exists():
        return json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    return {"runs": {}}


def _save_results(data: dict) -> None:
    RESULT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _run_one(template_id: str, start: str, end: str, model_id: str | None) -> dict:
    from backend.services.engine.qlib_app.schemas.backtest import (
        QlibBacktestRequest,
        QlibStrategyParams,
    )
    from backend.services.engine.qlib_app.services.backtest_service import QlibBacktestService

    meta = json.loads((TEMPLATE_DIR / f"{template_id}.json").read_text(encoding="utf-8"))
    request_fields = {
        "topk", "short_topk", "n_drop", "min_score", "max_weight", "long_exposure",
        "short_exposure", "momentum_period", "topk_sectors", "lookback_days",
        "vol_lookback", "stop_loss", "take_profit", "rebalance_days",
        "enable_short_selling", "max_leverage", "account_stop_loss",
    }
    params = {
        p["name"]: p["default"] for p in meta.get("params", []) if p["name"] in request_fields
    }
    try:
        strategy_params = QlibStrategyParams(**params)
    except Exception as exc:  # 参数超出 schema 范围时记录并继续，不要整批中断
        return {"status": f"error:{type(exc).__name__}", "detail": str(exc)[:300]}
    request = QlibBacktestRequest(
        strategy_type=template_id,
        strategy_params=strategy_params,
        start_date=start,
        end_date=end,
        user_id="00000001",
        tenant_id="default",
        model_id=model_id,
    )
    try:
        result = await QlibBacktestService().run_backtest(request)
    except Exception as exc:  # noqa: BLE001
        return {"status": f"error:{type(exc).__name__}", "detail": str(exc)[:300]}
    return {
        "status": result.status,
        "annual_return": result.annual_return,
        "sharpe_ratio": result.sharpe_ratio,
        "max_drawdown": result.max_drawdown,
        "benchmark_return": result.benchmark_return,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="全量 A 股模板回测")
    parser.add_argument("--ids", nargs="*", default=None, help="缺省=全部 as* 模板")
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--force", action="store_true", help="忽略已有结果，重跑")
    args = parser.parse_args()

    ids = args.ids or _all_template_ids()
    if BASELINE_ID not in ids:
        ids = [BASELINE_ID, *ids]

    data = _load_results()
    data.setdefault("runs", {})
    data["window"] = {"start": args.start, "end": args.end}
    data["model_id"] = args.model_id
    data["baseline_id"] = BASELINE_ID

    for template_id in ids:
        done = data["runs"].get(template_id, {})
        same_run = (
            done.get("status") == "completed"
            and done.get("start") == args.start
            and done.get("end") == args.end
            and done.get("model_id") == args.model_id
        )
        if not args.force and same_run:
            print(f"[skip] {template_id} 已完成", flush=True)
            continue
        print(f"[run ] {template_id} {datetime.now():%H:%M:%S}", flush=True)
        row = await _run_one(template_id, args.start, args.end, args.model_id)
        row.update({"start": args.start, "end": args.end, "model_id": args.model_id,
                    "ran_at": datetime.now().isoformat(timespec="seconds")})
        data["runs"][template_id] = row
        _save_results(data)
        print(f"[done] {template_id} {row.get('status')} "
              f"年化={row.get('annual_return')} 夏普={row.get('sharpe_ratio')}", flush=True)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    # qlib 数据加载会留下非守护线程（实测 80 个），解释器正常退出时会一直挂住
    # 等它们 join；结果已经逐条落盘，这里直接硬退出，避免批量进程残留。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
