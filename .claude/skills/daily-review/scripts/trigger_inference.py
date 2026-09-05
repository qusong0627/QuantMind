#!/usr/bin/env python3
"""每日推理补跑触发脚本（容器内执行，10-30 秒）。

复盘发现当日无推理 run 时调用：补跑 data_trade_date 的推理并落库
（engine_signal_scores + qm_model_inference_runs），供复盘验证/明日信号。

用法:
  docker cp scripts/trigger_inference.py quantmind:/tmp/
  docker exec quantmind python3 /tmp/trigger_inference.py --date 20260827
  # --date 为特征日（data_trade_date）；prediction 自动 = 下一交易日
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")


def main() -> int:
    ap = argparse.ArgumentParser(description="每日推理补跑")
    ap.add_argument("--date", required=True, help="特征日 YYYYMMDD 或 YYYY-MM-DD")
    ap.add_argument("--model", default="", help="模型 id（默认取最近一次用户推理 run 的模型，即每日推理模型）")
    ap.add_argument("--tenant", default="")
    ap.add_argument("--user", default="")
    args = ap.parse_args()

    date_str = args.date.replace("-", "")
    iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    tenant_id, user_id, model_id = args.tenant, args.user, args.model
    if not model_id:
        # 与每日推理链路一致：取 celery 自动推理最近成功记录的
        # (tenant, user, model) —— 用户每日推理模型（直读 QuantDB，训练推理特征同源）。
        # 注意不能从 qm_model_inference_runs 取（celery 链路不写该表，且补跑会污染
        # 最新记录）；sys-/model_qlib 是系统模型（读 model_features 派生层），排除。
        try:
            import asyncpg
            import os

            async def _latest_run() -> tuple[str, str, str]:
                conn = await asyncpg.connect(
                    host=os.getenv("DB_HOST", "quantmind-db"),
                    port=int(os.getenv("DB_PORT", "5432")),
                    user=os.getenv("DB_USER", "quantmind"),
                    password=os.getenv("DB_PASSWORD", "quantmind123"),
                    database=os.getenv("DB_NAME", "quantmind"), timeout=10,
                )
                try:
                    row = await conn.fetchrow(
                        "SELECT user_id, model_id FROM qm_model_inference_dispatch_logs "
                        "WHERE trigger_source='celery_auto_inference_if_needed' "
                        "AND status='success' AND model_id IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                    return (
                        "default",
                        str(row["user_id"]) if row else "",
                        str(row["model_id"]) if row else "",
                    )
                finally:
                    await conn.close()

            import asyncio

            tenant_id, user_id, model_id = asyncio.run(_latest_run())
        except Exception:  # noqa: BLE001
            pass

    from backend.services.engine.inference.router_service import InferenceRouterService

    svc = InferenceRouterService()
    result = svc.run_daily_inference_script(
        date=iso,
        tenant_id=tenant_id or "default",
        user_id=user_id or "system",
        model_id=model_id or None,
    )
    if not result.success:
        print(f"推理失败: {getattr(result, 'error', '') or result.stdout or ''}", flush=True)
        return 1

    # InferenceScriptRunner.execute 只写 engine_signal_scores；
    # qm_model_inference_runs 由调用方负责（复盘/信号查询按该表定位 run），补写：
    try:
        import asyncpg
        import os
        from datetime import date as _date

        async def _register_run() -> None:
            conn = await asyncpg.connect(
                host=os.getenv("DB_HOST", "quantmind-db"),
                port=int(os.getenv("DB_PORT", "5432")),
                user=os.getenv("DB_USER", "quantmind"),
                password=os.getenv("DB_PASSWORD", "quantmind123"),
                database=os.getenv("DB_NAME", "quantmind"), timeout=10,
            )
            try:
                # prediction_trade_date = 信号表 trade_date（INSERT 时即 prediction 日）
                pred = await conn.fetchval(
                    "SELECT max(trade_date) FROM engine_signal_scores WHERE run_id=$1",
                    result.run_id,
                )
                model_id = str(getattr(result, "active_model_id", "") or "")
                data_d = _date.fromisoformat(iso)
                pred_d = _date.fromisoformat(str(pred)) if pred else data_d
                await conn.execute(
                    """
                    INSERT INTO qm_model_inference_runs (
                      run_id, tenant_id, user_id, model_id, data_trade_date, prediction_trade_date,
                      status, signals_count, duration_ms, fallback_used, fallback_reason,
                      failure_stage, error_message, stdout, stderr,
                      active_model_id, effective_model_id, model_source, active_data_source,
                      request_json, result_json, created_at, updated_at
                    ) VALUES (
                      $1, $2, $3, $4, $5, $6,
                      'completed', $7, NULL, $8, NULL,
                      NULL, NULL, NULL, NULL,
                      $9, NULL, NULL, NULL,
                      '{}'::jsonb, NULL, NOW(), NOW()
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                      status = EXCLUDED.status,
                      signals_count = EXCLUDED.signals_count,
                      updated_at = NOW()
                    """,
                    result.run_id, tenant_id or "default", user_id or "system", model_id,
                    data_d, pred_d,
                    result.signals_count, result.fallback_used, model_id,
                )
            finally:
                await conn.close()

        import asyncio

        asyncio.run(_register_run())
        print(
            f"推理完成: run_id={result.run_id} data={iso} "
            f"signals={result.signals_count} fallback={result.fallback_used}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"推理完成但 run 登记失败: run_id={result.run_id} signals={result.signals_count} "
            f"err={str(exc)[:120]}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
