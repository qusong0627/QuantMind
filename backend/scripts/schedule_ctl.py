#!/usr/bin/env python3
"""调度控制台（T-P1-06）：查看调度表 / 手动重跑。

用法（容器内）:
    python backend/scripts/schedule_ctl.py list
    python backend/scripts/schedule_ctl.py run sim_eod --date 2026-09-15 [--force]
    python backend/scripts/schedule_ctl.py run auto_inference --date 2026-09-14 [--force]
    python backend/scripts/schedule_ctl.py run market_sync_dispatch [--force]

设计：
- 任务清单唯一事实源 = backend/shared/scheduler_registry.py（禁止在本脚本另列任务）；
- `run` 只支持注册表里声明了 `rerun` 的任务（分发表 _RERUN_DISPATCH），未知/不支持 → 退出码 2；
- `--force`：保留参数——供未来有"当日已执行守卫"的任务显式绕过；当前无守卫任务，
  传参会提示（不造假语义）。

退出码：0 成功 / 1 执行失败 / 2 参数或任务不支持。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Callable

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

from backend.shared.scheduler_registry import (  # noqa: E402
    JOBS,
    JOBS_BY_KEY,
    heartbeat_key,
    switch_enabled,
)

_STATE_LABEL = {"ok": "✓ 新鲜", "stale": "✗ 过期", "off": "· 关闭", "missing": "! 无记录"}


def _redis_client():
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client

    return get_redis_sentinel_client()


def cmd_list() -> int:
    redis = _redis_client()
    now_ts = time.time()
    print(f"{'KEY':<22}{'名称':<12}{'归属':<9}{'周期':<26}{'开关':<8}{'心跳'}")
    for spec in JOBS:
        enabled = switch_enabled(spec)
        age = None
        try:
            raw = redis.get(heartbeat_key(spec.key))
            if raw is not None:
                age = int(now_ts - float(raw))
        except Exception:  # noqa: BLE001
            pass
        if spec.heartbeat_ttl is None:
            state = "· 未接线"
        elif not enabled:
            state = "· 关闭"
        elif age is None:
            state = "! 无记录"
        elif age <= spec.heartbeat_ttl:
            state = f"✓ 新鲜（{age}s）"
        else:
            state = f"✗ 过期（{age}s > {spec.heartbeat_ttl}s）"
        print(
            f"{spec.key:<22}{spec.name:<12}{spec.owner:<9}{spec.schedule:<26}"
            f"{'on' if enabled else 'OFF':<8}{state}"
        )
    print("\n可手动重跑：", ", ".join(k for k, s in JOBS_BY_KEY.items() if s.rerun))
    return 0


def _run_sim_eod(date_str: str | None, force: bool) -> int:
    import asyncio
    from datetime import date

    from backend.services.simulation.services.eod_service import _execute_eod

    target = date.fromisoformat(date_str) if date_str else datetime_now_shanghai_date()

    ok = asyncio.run(_execute_eod(target))
    print(f"sim_eod {target}: {'完成' if ok else '未完成（见容器日志）'}")
    return 0 if ok else 1


def datetime_now_shanghai_date():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _run_inference(date_str: str | None, force: bool) -> int:
    import asyncio
    from datetime import date, timedelta

    from backend.services.engine.inference.router_service import InferenceRouterService

    # data_trade_date：默认取最近一个工作日（信号按 T+1 生效日落库，与调度口径一致）
    if date_str:
        data_date = date.fromisoformat(date_str).isoformat()
    else:
        probe = datetime_now_shanghai_date()
        while probe.weekday() >= 5:
            probe -= timedelta(days=1)
        data_date = probe.isoformat()

    service = InferenceRouterService()
    result = asyncio.run(
        service.run_daily_inference_script(
            date=data_date,
            tenant_id="default",
            user_id="system",
            redis_client=_redis_client(),
        )
    )
    print(
        f"inference data_date={data_date}: success={result.success} "
        f"run_id={getattr(result, 'run_id', '')} signals={getattr(result, 'signals_count', '?')} "
        f"reason={getattr(result, 'fallback_reason', '') or '-'}"
    )
    return 0 if result.success else 1


def _run_data_sync(date_str: str | None, force: bool) -> int:
    script = os.path.join(PROJECT_ROOT, "backend", "scripts", "quantdb_daily_sync.py")
    print(f"执行 {script}（输出直通）...")
    completed = subprocess.run([sys.executable, script], check=False)
    return 0 if completed.returncode == 0 else 1


_RERUN_DISPATCH: dict[str, Callable[[str | None, bool], int]] = {
    # 键 = 注册表任务键（唯一标识，禁止别名——测试防止漂移）
    "sim_eod": _run_sim_eod,
    "auto_inference": _run_inference,
    "market_sync_dispatch": _run_data_sync,
}


def cmd_run(job_key: str, date_str: str | None, force: bool) -> int:
    spec = JOBS_BY_KEY.get(job_key)
    if spec is None:
        print(f"未知任务: {job_key}；可用: {', '.join(JOBS_BY_KEY)}", file=sys.stderr)
        return 2
    if not spec.rerun or job_key not in _RERUN_DISPATCH:
        print(
            f"任务 {job_key} 不支持手动重跑（可重跑: {', '.join(_RERUN_DISPATCH)}）",
            file=sys.stderr,
        )
        return 2
    if force:
        print("提示：--force 为保留参数（当前无带守卫的任务需要绕过）")
    print(f"== 手动重跑 {spec.key}（{spec.name}）{f'date={date_str}' if date_str else ''} ==")
    return _RERUN_DISPATCH[job_key](date_str, force)


def main() -> int:
    parser = argparse.ArgumentParser(description="调度控制台（注册表见 shared/scheduler_registry.py）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出调度表与心跳状态")
    run_p = sub.add_parser("run", help="手动重跑指定任务")
    run_p.add_argument("job", help="任务 key（如 sim_eod/inference/data_sync）")
    run_p.add_argument("--date", default="", help="目标日期 YYYY-MM-DD（语义按任务而定）")
    run_p.add_argument("--force", action="store_true", help="保留参数（绕过守卫，供未来使用）")
    args = parser.parse_args()

    if args.cmd == "list":
        return cmd_list()
    return cmd_run(args.job, args.date or None, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
