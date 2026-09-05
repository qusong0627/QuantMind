"""模型推理信号查询模块：昨日推理→今日验证 + 今日推理→明日 Top5。

数据源：Postgres `engine_signal_scores`(trade_date,symbol,fusion_score) +
`qm_model_inference_runs`(run_id,model_id,data_trade_date,prediction_trade_date,status)。
symbol 纯数字(如 300502) → `StockCodeUtil.to_suffix` 归一化为 suffix(300502.SZ) 对齐 QuantDB。

连接复用 daily_review.py 的 PG 配置（env 覆盖，默认 127.0.0.1:5432）。
本模块只做 IO + run 选择，命中率计算在 review_stats.py（纯函数）。
"""
from __future__ import annotations

import os
from datetime import date

import psycopg2

from review_stats import infer_signal_symbol

DEFAULT_MODEL_ID = "mdl_cn_train_20260815151359_5eea5418_07cd6533"


def pg_connect():
    """连接推理信号 PG。host 默认 127.0.0.1（容器内 /data 同目录逻辑不适用——PG 独立）。"""
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
        connect_timeout=5,
    )


def _latest_completed_run(cur, model_id: str, data_trade_date: date | None,
                          prediction_trade_date: date | None) -> dict | None:
    """找 model 的已完成 run；可按 data/prediction 精确定位，否则按 created_at DESC 最新。"""
    conds = ["model_id=%s", "status='completed'"]
    args: list = [model_id]
    if data_trade_date is not None:
        conds.append("data_trade_date=%s")
        args.append(data_trade_date)
    if prediction_trade_date is not None:
        conds.append("prediction_trade_date=%s")
        args.append(prediction_trade_date)
    cur.execute(
        "SELECT run_id, model_id, data_trade_date, prediction_trade_date, created_at "
        f"FROM qm_model_inference_runs WHERE {' AND '.join(conds)} "
        "ORDER BY created_at DESC LIMIT 1",
        args,
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "run_id": row[0], "model_id": row[1],
        "data_trade_date": row[2], "prediction_trade_date": row[3],
        "created_at": row[4],
    }


def _signals_for_run(cur, run_id: str, limit: int) -> list[dict]:
    cur.execute(
        "SELECT symbol, fusion_score, signal_side FROM engine_signal_scores "
        "WHERE run_id=%s ORDER BY fusion_score DESC LIMIT %s",
        (run_id, limit),
    )
    out = [
        {"symbol": infer_signal_symbol(r[0]), "fusion_score": float(r[1]), "signal_side": r[2]}
        for r in cur.fetchall()
    ]
    return out[:limit]


def resolve_latest_model_id(conn=None) -> str:
    """最近一次 celery 自动推理成功用的每日推理用户模型 id（复盘/补跑跟随）。

    数据源用 qm_model_inference_dispatch_logs（celery 链路不写 qm_model_inference_runs，
    且补跑会污染 run 表最新记录）；sys-/model_qlib 为系统模型（读 model_features
    派生层，非每日推理链路）不在此列。无记录时回退 DEFAULT_MODEL_ID。
    """
    need_close = conn is None
    conn = conn or pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT model_id FROM qm_model_inference_dispatch_logs "
                "WHERE trigger_source='celery_auto_inference_if_needed' "
                "AND status='success' AND model_id IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else DEFAULT_MODEL_ID
    except Exception:
        return DEFAULT_MODEL_ID
    finally:
        if need_close:
            conn.close()


def _latest_dispatch_run(cur, model_id: str, data_trade_date: date | None,
                           prediction_trade_date: date | None) -> dict | None:
    """celery 每日推理链路 run（qm_model_inference_dispatch_logs）。

    celery 的 run 只写 dispatch_logs + engine_signal_scores，不写 qm_model_inference_runs；
    复盘/补跑按此表定位用户模型推理。"""
    conds = ["trigger_source='celery_auto_inference_if_needed'", "status='success'", "model_id=%s"]
    args: list = [model_id]
    if data_trade_date is not None:
        conds.append("data_trade_date=%s")
        args.append(data_trade_date)
    if prediction_trade_date is not None:
        conds.append("prediction_trade_date=%s")
        args.append(prediction_trade_date)
    cur.execute(
        "SELECT run_id, model_id, data_trade_date, prediction_trade_date, created_at "
        f"FROM qm_model_inference_dispatch_logs WHERE {' AND '.join(conds)} "
        "ORDER BY created_at DESC LIMIT 1",
        args,
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "run_id": row[0], "model_id": row[1],
        "data_trade_date": row[2], "prediction_trade_date": row[3],
        "created_at": row[4],
    }


def load_prev_vs_today(model_id: str, trade_date: date, conn=None, top_n: int = 20,
                     fallback: bool = True) -> dict | None:
    """昨日推理 → 今日信号：prediction_trade_date=trade_date（无论 data 日期，跨周末安全）。

    fallback=True 且当日无对应推理 run 时，回退到最近一次已完成 run（验证其
    prediction_trade_date 的实际表现），返回带 fallback/fallback_note 标记。

    Returns: {run: {...}, signals: [top_n 条, 已归一化 symbol]}
    """
    need_close = conn is None
    conn = conn or pg_connect()
    try:
        with conn.cursor() as cur:
            run = _latest_completed_run(cur, model_id, data_trade_date=None,
                                        prediction_trade_date=trade_date)
            if not run:
                run = _latest_dispatch_run(cur, model_id, data_trade_date=None,
                                           prediction_trade_date=trade_date)
                if run:
                    run["from_dispatch"] = True
            if not run and fallback:
                run = _latest_completed_run(cur, model_id, data_trade_date=None,
                                            prediction_trade_date=None)
                if not run:
                    run = _latest_dispatch_run(cur, model_id, data_trade_date=None,
                                               prediction_trade_date=None)
                if run:
                    run["fallback"] = True
                    run["fallback_note"] = (
                        f"当日无 prediction={trade_date} 的推理 run，取最近一次"
                        f"（推理 {run['data_trade_date']} → 信号 {run['prediction_trade_date']}）"
                    )
            if not run:
                return None
            run["signals"] = _signals_for_run(cur, run["run_id"], top_n)
            return run
    finally:
        if need_close:
            conn.close()


def load_next_top_n(model_id: str, trade_date: date, conn=None, top_n: int = 5,
                    fallback: bool = True) -> dict | None:
    """今日推理 → 明日信号 TopN：data_trade_date=trade_date（预测未来交易日）。

    今日推理未跑时 fallback 到最近一次已完成 run，字段带 `fallback`/`fallback_note`。
    Returns: {run: {...}, signals: [top_n 条], fallback: bool}
    """
    need_close = conn is None
    conn = conn or pg_connect()
    try:
        with conn.cursor() as cur:
            run = _latest_completed_run(cur, model_id, data_trade_date=trade_date,
                                        prediction_trade_date=None)
            if not run:
                run = _latest_dispatch_run(cur, model_id, data_trade_date=trade_date,
                                           prediction_trade_date=None)
                if run:
                    run["from_dispatch"] = True
            is_fallback = run is None
            if run is None and fallback:
                run = _latest_completed_run(cur, model_id, data_trade_date=None,
                                            prediction_trade_date=None)
                if not run:
                    run = _latest_dispatch_run(cur, model_id, data_trade_date=None,
                                               prediction_trade_date=None)
            if not run:
                return None
            run["signals"] = _signals_for_run(cur, run["run_id"], top_n)
            run["fallback"] = is_fallback
            run["fallback_note"] = (
                f"基于 {run['data_trade_date']} 推理，预测 {run['prediction_trade_date']}（今日推理未跑，取最近一次）"
                if is_fallback else ""
            )
            return run
    finally:
        if need_close:
            conn.close()