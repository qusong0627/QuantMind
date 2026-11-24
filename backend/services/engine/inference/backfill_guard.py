"""重复推理防线：缺口回补前先看库里是否已有分数。

背景（自动推理把 PG 撑到几十 GB 的根因）：

1. 「这一天是否已经推理过」这件事，唯一的凭证是模型目录的 ``pred.parquet``
   （``compute_coverage`` 只读 parquet 的 trade_date 集合）。
2. 但推理落库（``engine_signal_scores`` + ``qm_research_candidate_snapshot``，
   同一事务 commit）发生在 parquet 合并**之前**：
   - ``script_runner.execute()`` 里先 ``_persist_and_publish``；
   - ``gap_backfill.backfill_model_gaps`` 要等整个 for 循环跑完，才在循环外
     调一次 ``merge_signals_into_pred``，且 ``except`` 只往 logs 里塞一行文案。
3. 于是只要 parquet 没写上（合并抛异常、worker 被 OOM 杀掉、任务超时被
   SIGKILL），缺口就永不闭合；而 Beat 的 ``backfill-default-inference-weekdays``
   是工作日 6-16 点每 30 分钟一轮（``AUTO_INFERENCE_POLL_MINUTE/HOUR``），
   下一轮又看到同样的缺口。
4. 每次执行都生成新 ``run_id``（``run_{date}_{uuid4[:8]}``），而
   ``engine_signal_scores`` 的唯一键含 ``run_id``，Step 0.2 又刻意「只删本 run」
   —— 重复推理不再互相覆盖，而是净新增整份全市场截面。

本模块提供第 3 步的止血：缺口日期若在 PG 里已经有足够的落库分数，
**不再重新推理**，改为把已有分数回写 ``pred.parquet``（既省一次全量推理，
又把缺口真正闭合）。symbol 口径注意：``engine_signal_scores.symbol`` 是纯数字，
``pred.parquet`` 用后缀式，交给 ``merge_signals_into_pred`` 归一。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# 单日「已有落库分数」达到这个规模就认为该日已推理过，不再重推。
# 阈值取自就绪口径的下限一档：全市场约 5200 只，股票池/单股 run 远小于它，
# 不会被误判成「已推理」。覆盖 INFERENCE_BACKFILL_DB_MIN_SYMBOLS。
_MIN_DB_SYMBOLS = int(os.getenv("INFERENCE_BACKFILL_DB_MIN_SYMBOLS", "1000"))


def _sync_session() -> tuple[Any, Any]:
    """同步 SQLAlchemy 会话（补全链路全程是同步 DB 访问）。

    与 ``gap_backfill.list_default_models`` 同口径：``DATABASE_URL`` 是 asyncpg
    时换成 psycopg2。拿不到连接时返回 (None, None)，调用方按「无证据」处理，
    绝不因为守卫失败而阻断正常补全。
    """
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker

    sync_db_url = str(os.getenv("DATABASE_URL", "")).strip()
    if "+asyncpg" in sync_db_url:
        sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
    if not sync_db_url or "postgresql" not in sync_db_url:
        return None, None
    engine = sa_create_engine(sync_db_url, pool_pre_ping=True)
    return sa_sessionmaker(bind=engine)(), engine


def _close(db: Any, engine: Any) -> None:
    if db is not None:
        db.close()
    if engine is not None:
        engine.dispose()


def covered_dates_from_db(
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    dates: Iterable[str],
) -> dict[str, int]:
    """返回 {数据日: 该日最大单 run 符号数}，只包含库里已有的日期。

    以 ``qm_research_candidate_snapshot`` 为准而不是 engine_signal_scores：
    候选快照带 ``model_id``（一次 run 的归属）与 ``data_trade_date``
    （特征数据日，与 pred.parquet 的 trade_date 同口径），
    engine_signal_scores 只有 T+1 的生效日，无法安全映射回数据日。
    """
    date_list = [str(d)[:10] for d in dates if d]
    if not date_list or not (tenant_id and user_id and model_id):
        return {}
    from sqlalchemy import text as sa_text

    db, engine = _sync_session()
    if db is None:
        return {}
    try:
        rows = db.execute(
            sa_text(
                """
                SELECT data_trade_date, max(n) AS n
                FROM (
                    SELECT data_trade_date, run_id, count(DISTINCT symbol) AS n
                    FROM qm_research_candidate_snapshot
                    WHERE tenant_id = :tenant_id
                      AND user_id = :user_id
                      AND model_id = :model_id
                      AND data_trade_date = ANY(CAST(:dates AS date[]))
                    GROUP BY data_trade_date, run_id
                ) per_run
                GROUP BY data_trade_date
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "model_id": model_id,
                "dates": date_list,
            },
        ).fetchall()
        return {str(r[0])[:10]: int(r[1] or 0) for r in rows if r[0] is not None}
    except Exception as exc:  # 守卫本身出错时不改变原有行为
        logger.warning("[InferenceBackfillGuard] 查库内已有分数失败: %s", exc)
        return {}
    finally:
        _close(db, engine)


def scores_for_date(
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    data_trade_date: str,
    min_symbols: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """取该数据日「最完整的那个 run」的分数，用于回写 pred.parquet。

    返回 ``(run_id, signals)``；``signals`` 是 ``[{"symbol","score"}]``，
    与 ``merge_signals_into_pred`` 期望的 runner 输出结构一致（symbol 由
    merge 内部归一为后缀式）。库里没有达标 run 时返回 ``("", [])``。
    """
    floor = int(min_symbols if min_symbols is not None else _MIN_DB_SYMBOLS)
    if not (tenant_id and user_id and model_id and data_trade_date):
        return "", []
    from sqlalchemy import text as sa_text

    db, engine = _sync_session()
    if db is None:
        return "", []
    try:
        run_row = db.execute(
            sa_text(
                """
                SELECT run_id, count(DISTINCT symbol) AS n
                FROM qm_research_candidate_snapshot
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND model_id = :model_id
                  AND data_trade_date = :data_trade_date
                GROUP BY run_id
                HAVING count(DISTINCT symbol) >= :floor
                ORDER BY n DESC, max(created_at) DESC
                LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "model_id": model_id,
                "data_trade_date": str(data_trade_date)[:10],
                "floor": floor,
            },
        ).first()
        if not run_row:
            return "", []
        run_id = str(run_row[0])
        rows = db.execute(
            sa_text(
                """
                SELECT symbol, fusion_score
                FROM qm_research_candidate_snapshot
                WHERE run_id = :run_id
                  AND tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND fusion_score IS NOT NULL
                """
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
        ).fetchall()
        signals = [
            {"symbol": str(r[0]), "score": float(r[1])}
            for r in rows
            if r[0] is not None and r[1] is not None
        ]
        return run_id, signals
    except Exception as exc:
        logger.warning(
            "[InferenceBackfillGuard] 回读 %s 落库分数失败: %s", data_trade_date, exc
        )
        return "", []
    finally:
        _close(db, engine)


def scores_for_dates(
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    dates: Iterable[str],
    min_symbols: int | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """批量版 ``scores_for_date``：每个数据日各取「最完整的那个 run」。

    一次查询拿回全部行，避免按日 N 次往返（缺口动辄几十天）。
    返回 ``[(data_trade_date, signals), ...]``，只包含真正取到分数的日期。
    """
    floor = int(min_symbols if min_symbols is not None else _MIN_DB_SYMBOLS)
    date_list = sorted({str(d)[:10] for d in dates if d})
    if not date_list or not (tenant_id and user_id and model_id):
        return []
    from sqlalchemy import text as sa_text

    db, engine = _sync_session()
    if db is None:
        return []
    try:
        # 每个数据日挑一个 run：优先符号数最多、其次最新
        picked = db.execute(
            sa_text(
                """
                SELECT DISTINCT ON (data_trade_date)
                       data_trade_date, run_id, n
                FROM (
                    SELECT data_trade_date, run_id,
                           count(DISTINCT symbol) AS n,
                           max(created_at) AS last_created
                    FROM qm_research_candidate_snapshot
                    WHERE tenant_id = :tenant_id
                      AND user_id = :user_id
                      AND model_id = :model_id
                      AND data_trade_date = ANY(CAST(:dates AS date[]))
                    GROUP BY data_trade_date, run_id
                    HAVING count(DISTINCT symbol) >= :floor
                ) per_run
                ORDER BY data_trade_date, n DESC, last_created DESC
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "model_id": model_id,
                "dates": date_list,
                "floor": floor,
            },
        ).fetchall()
        if not picked:
            return []
        run_ids = [str(r[1]) for r in picked]
        rows = db.execute(
            sa_text(
                """
                SELECT run_id, symbol, fusion_score
                FROM qm_research_candidate_snapshot
                WHERE run_id = ANY(:run_ids)
                  AND tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND fusion_score IS NOT NULL
                """
            ),
            {"run_ids": run_ids, "tenant_id": tenant_id, "user_id": user_id},
        ).fetchall()
        by_run: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r[1] is None or r[2] is None:
                continue
            by_run.setdefault(str(r[0]), []).append(
                {"symbol": str(r[1]), "score": float(r[2])}
            )
        out: list[tuple[str, list[dict[str, Any]]]] = []
        for d, run_id, _n in picked:
            signals = by_run.get(str(run_id)) or []
            if signals:
                out.append((str(d)[:10], signals))
        return out
    except Exception as exc:
        logger.warning("[InferenceBackfillGuard] 批量回读落库分数失败: %s", exc)
        return []
    finally:
        _close(db, engine)
