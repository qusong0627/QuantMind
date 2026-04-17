"""Step 2: 股票池确认 - 执行查询并展示结果"""

import logging
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, status
from sqlalchemy import text

from ..api.schemas.stock_pool import PoolItem, QueryPoolResponse

try:
    from backend.shared.database_pool import get_database_pool, get_db
except ImportError:
    try:
        from shared.database_pool import get_database_pool, get_db
    except ImportError:
        try:
            from backend.shared.strategy_storage import get_db

            def get_database_pool():
                raise ImportError("database_pool not available")

        except ImportError:
            try:
                from shared.strategy_storage import get_db

                def get_database_pool():
                    raise ImportError("database_pool not available")

            except ImportError:

                def get_database_pool():
                    raise ImportError("Cannot find database_pool module")

                def get_db():
                    raise ImportError("Cannot find database_pool module")


from ..services.validators.sql_validator import (
    SQLValidationError,
    safe_table_replace,
    validate_and_sanitize,
)
from .step1_stock_selection import (
    DSL_PREFIX,
    LATEST_TABLE,
    _map_factor,
    _parse_dsl,
)

logger = logging.getLogger(__name__)
TOTAL_MV_PER_YI = float(os.getenv("AI_STRATEGY_TOTAL_MV_PER_YI", "10000"))
TOTAL_MV_TO_YUAN = 100000000.0 / TOTAL_MV_PER_YI


def _get_table_columns(session, table_name: str) -> set[str]:
    try:
        rows = session.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = :table_name
                """),
            {"table_name": table_name},
        ).fetchall()
        return {str(r[0]).lower() for r in rows}
    except Exception:
        return set()


def _resolve_hs300_column(columns: set[str]) -> str:
    if "is_hs300" in columns:
        return "is_hs300"
    if "is_csi300" in columns:
        return "is_csi300"
    # 回退默认值，保持兼容
    return "is_hs300"


def _normalize_index_flag_columns(sql: str, hs300_col: str) -> str:
    # 统一把两种写法都改成当前库真实列名
    out = re.sub(r"\bis_hs300\b", hs300_col, sql, flags=re.IGNORECASE)
    out = re.sub(r"\bis_csi300\b", hs300_col, out, flags=re.IGNORECASE)
    return out


def _query_pool_limit() -> int:
    """查询结果上限（防止超大结果拖垮接口），默认 10000。"""
    raw = (os.getenv("AI_STRATEGY_QUERY_POOL_LIMIT", "10000") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 10000
    return max(1, min(value, 50000))


def _require_user_id(request: Request) -> str:
    # 企业环境严格要求鉴权上下文，禁止回退默认用户。
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证：缺少用户上下文",
        )

    if isinstance(user, dict):
        user_id = user.get("user_id")
        tenant_id = user.get("tenant_id")
    else:
        user_id = getattr(user, "user_id", None)
        tenant_id = getattr(user, "tenant_id", None)

    if not user_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="未授权：缺少 user_id 或 tenant_id",
        )

    return str(user_id)


def _get_universe_total(user_id: str) -> int:
    """
    获取覆盖率分母（候选全集大小）。

    规则（尽量"不会超过数据表中股票列表"）：
    1. 若存在 user_universe 且该用户有授权标的，则以其为全集（多租户隔离更严格）。
    2. 否则退化为 stock_daily_latest 全量记录数。
    """
    try:
        with get_db() as session:
            # 优先使用多租户白名单表（如果存在且有数据）
            try:
                n = session.execute(
                    text("select count(1) from user_universe where user_id = :uid"),
                    {"uid": user_id},
                ).scalar()
                n_int = int(n or 0)
                if n_int > 0:
                    return n_int
            except Exception:
                # 表不存在/无权限等，忽略并走 fallback
                try:
                    session.rollback()
                except Exception:
                    pass
                pass

            n2 = session.execute(text(f"select count(1) from {LATEST_TABLE}")).scalar()
            return int(n2 or 0)
    except Exception:
        return 0


def _execute_raw_selection_sql(sql: str) -> tuple[list[PoolItem], date | None]:
    """直接执行由 LLM 生成的 SQL 语句

    安全改进:
    1. 使用 SQL 验证器防止注入
    2. 使用安全的表名替换
    3. 强制添加 LIMIT 限制
    """
    try:
        # === 安全验证：防止SQL注入 ===
        try:
            validated_sql = validate_and_sanitize(sql)
        except SQLValidationError as e:
            logger.error(f"SQL validation failed: {e}")
            raise HTTPException(status_code=400, detail=f"SQL验证失败: {str(e)}")

        sql_lower = validated_sql.lower()
        normalized_sql = validated_sql

        # 使用安全的表名替换函数
        if "from stock_selection" in sql_lower or "from stock_daily" in sql_lower:
            try:
                if "from stock_selection" in sql_lower:
                    normalized_sql = safe_table_replace(normalized_sql, "stock_selection", LATEST_TABLE)
                if "from stock_daily" in sql_lower:
                    normalized_sql = safe_table_replace(normalized_sql, "stock_daily", LATEST_TABLE)
            except SQLValidationError as e:
                logger.error(f"Table replacement failed: {e}")
                raise HTTPException(status_code=400, detail=f"表名替换失败: {str(e)}")

        target_table = LATEST_TABLE if f"from {LATEST_TABLE}" in normalized_sql.lower() else "stock_selection"

        # 强制清除 LLM 可能生成的 LIMIT 限制，确保返回足够多的股票
        normalized_sql = re.sub(r"limit\s+\d+", "", normalized_sql, flags=re.IGNORECASE).strip()
        max_rows = _query_pool_limit()
        if not normalized_sql.lower().endswith(f"limit {max_rows}"):
            normalized_sql += f" LIMIT {max_rows}"

        with get_db() as session:
            target_columns = _get_table_columns(session, target_table)
            hs300_col = _resolve_hs300_column(target_columns)
            normalized_sql = _normalize_index_flag_columns(normalized_sql, hs300_col)

            as_of_date = session.execute(text(f"select max(trade_date) from {target_table}")).scalar()

            result = session.execute(text(normalized_sql)).fetchall()

            items: list[PoolItem] = []
            for row in result:
                row_dict = row._asdict() if hasattr(row, "_asdict") else None

                if row_dict:
                    symbol = str(row_dict.get("symbol") or row_dict.get("code") or "")
                    name = row_dict.get("name") or row_dict.get("code") or symbol

                    market_cap = row_dict.get("market_cap")
                    if market_cap is None:
                        market_cap = row_dict.get("total_mv")

                    pe = row_dict.get("pe_ratio")
                    if pe is None:
                        pe = row_dict.get("pe_ttm")

                    pb = row_dict.get("pb_ratio")
                    if pb is None:
                        pb = row_dict.get("pb")

                    metrics = {
                        # total_mv -> 元（默认 total_mv 为万元）
                        "market_cap": float(market_cap or 0) * TOTAL_MV_TO_YUAN,
                        "pe": float(pe or 0),
                        "close": float(row_dict.get("close") or 0),
                        "pb": float(pb or 0),
                        "roe": float(row_dict.get("roe") or 0),
                    }
                else:
                    symbol = str(row[0])
                    name = row[1] if len(row) > 1 else None
                    metrics = {"close": float(row[2]) if len(row) > 2 else 0.0}

                items.append(PoolItem(symbol=symbol, name=name, metrics=metrics))
            return items, as_of_date
    except Exception as e:
        logger.error(f"Error in _execute_raw_selection_sql: {e}")
        raise


def _query_stock_pool(
    conditions: list[dict[str, Any]], combiners: list[str], user_id: str
) -> tuple[list[PoolItem], date | None]:
    try:
        with get_db() as session:
            target_columns = _get_table_columns(session, LATEST_TABLE)
            hs300_col = _resolve_hs300_column(target_columns)
            # 1. 确定一个能覆盖绝大多数股票的有效"最新日期"
            # 而不是简单的 MAX，因为有些股票可能在最新一天停牌或未更新
            date_res = session.execute(
                text(
                    f"SELECT trade_date, COUNT(*) as cnt FROM {LATEST_TABLE} GROUP BY trade_date ORDER BY cnt DESC LIMIT 1"
                )
            ).fetchone()

            if not date_res:
                return [], None

            as_of_date = date_res[0]
            logger.info(f"Targeting trade_date: {as_of_date} (covers {date_res[1]} stocks)")

            # 2. 组装参数和条件
            params = {"d": as_of_date}
            # 基础条件：日期匹配
            where_clauses = ["trade_date = :d"]

            # 3. 翻译 DSL 条件
            flag_cols = {"is_st", "is_hs300", "is_csi300", "is_csi1000"}
            for idx, cond in enumerate(conditions):
                col = _map_factor(cond["factor"])
                if col in {"is_hs300", "is_csi300"}:
                    col = hs300_col
                param_key = f"p{idx}"
                op = "=" if cond["op"] == "==" else cond["op"]

                # 处理数值和类型
                val = cond["value"]
                if col in flag_cols:
                    try:
                        val = int(float(val))
                    except:
                        val = 1 if str(val).lower() in ("true", "1", "yes") else 0

                params[param_key] = val
                where_clauses.append(f"{col} {op} :{param_key}")

            # 4. 组合最终 WHERE 语句 (稳健拼接)
            # 我们强制要求日期匹配 AND (其他条件)
            final_where = f"({where_clauses[0]})"
            if len(where_clauses) > 1:
                # 组合用户的业务条件
                user_conds = f"({where_clauses[1]})"
                for i, combiner in enumerate(combiners):
                    if i + 2 < len(where_clauses):
                        user_conds += f" {combiner} ({where_clauses[i + 2]})"
                final_where += f" AND ({user_conds})"

            # 5. 执行最终查询 (全量返回，最高支持 10000 只股票)
            sql = f"""
            SELECT
                code as symbol,
                stock_name as name,
                total_mv as market_cap,
                pe_ttm as pe_ratio,
                pb as pb_ratio,
                close,
                turnover,
                volume
            FROM {LATEST_TABLE}
            WHERE {final_where}
            ORDER BY total_mv DESC NULLS LAST
            LIMIT 10000
            """

            logger.info(f"Generated Selection SQL: {sql}")
            logger.info(f"SQL Params: {params}")

            result = session.execute(text(sql), params).fetchall()
            logger.info(f"Query returned {len(result)} rows")

            items: list[PoolItem] = []
            for row in result:
                items.append(
                    PoolItem(
                        symbol=row[0],
                        name=row[1],
                        metrics={
                            # total_mv -> 元（默认 total_mv 为万元）
                            "market_cap": float(row[2] or 0) * TOTAL_MV_TO_YUAN,
                            "pe": float(row[3] or 0),
                            "pb": float(row[4] or 0),
                            "close": float(row[5] or 0),
                            "amount": float(row[6] or 0),
                            "volume": float(row[7] or 0),
                        },
                    )
                )
            return items, as_of_date
    except Exception as e:
        logger.error(f"Critical error in _query_stock_pool: {e}", exc_info=True)
        raise


def _build_pool_summary(
    items: list[PoolItem],
    as_of_date: date | None,
    universe_total: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    total = len(items)
    caps = [x.metrics.get("market_cap", 0) for x in items]
    bucket_lt_100 = sum(1 for v in caps if v < 100e8)
    bucket_100_200 = sum(1 for v in caps if 100e8 <= v < 200e8)
    bucket_gte_200 = sum(1 for v in caps if v >= 200e8)
    denom = int(universe_total) if universe_total and universe_total > 0 else 0
    # 防止出现 >100% 的覆盖率（例如分母写死/分母小于实际候选数）
    match_rate = 0.0
    if total and denom:
        match_rate = min(100.0, round(100.0 * total / denom, 2))
    summary = {
        "matchRate": match_rate,
        "totalCandidates": total,
        "universeTotal": denom or None,
        "asOf": as_of_date.isoformat() if as_of_date else None,
    }
    charts = {
        "marketCap": [
            {"bucket": "<100亿", "value": bucket_lt_100},
            {"bucket": "100-200亿", "value": bucket_100_200},
            {"bucket": ">=200亿", "value": bucket_gte_200},
        ]
    }
    return summary, charts


def _is_full_market_query(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return False
    normalized = re.sub(r"[，。,\.!！?？:：;；、]", "", normalized)
    full_market_terms = [
        "全市场",
        "全市场股票",
        "全部市场",
        "全部市场股票",
        "全部股票",
        "全A股",
        "全A股股票",
        "全A股市场",
        "全A股市场股票",
        "全A股市场全部股票",
    ]
    return any(term in normalized for term in full_market_terms)


def _build_full_market_sql() -> str:
    return (
        "SELECT code as symbol, stock_name as name, close, total_mv as market_cap, pe_ttm as pe_ratio\n"
        "FROM stock_daily_latest"
    )


def _is_full_market_sql(sql: str) -> bool:
    if not sql:
        return False
    s = sql.strip().rstrip(";")
    pattern = re.compile(
        r"from\s+stock_selection\s+where\s+trade_date\s*=\s*"
        r"\(\s*select\s+max\(trade_date\)\s+from\s+stock_selection\s*\)\s*$",
        re.IGNORECASE,
    )
    return pattern.search(s) is not None


async def _ensure_latest_table_data(session) -> bool:
    """确保最新数据表中有可用数据，否则尝试检查原始表。"""
    try:
        # 1. 检查 latest 表
        res = session.execute(text(f"SELECT COUNT(*) FROM {LATEST_TABLE}")).scalar()
        if int(res or 0) > 0:
            return True

        # 2. 如果 latest 为空，检查原始 stock_daily 表
        logger.warning(f"表 {LATEST_TABLE} 为空，尝试检查原始 stock_daily 表...")
        res_raw = session.execute(
            text("SELECT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 1")
        ).fetchone()

        if res_raw:
            latest_date = res_raw[0]
            error_msg = (
                f"选股数据未就绪：{LATEST_TABLE} 表为空。 "
                f"检测到原始数据表中最新日期为 {latest_date}，请运行同步脚本: "
                "python scripts/sync_latest_stocks.py"
            )
            logger.error(error_msg)
            raise HTTPException(status_code=503, detail=error_msg)
        else:
            raise HTTPException(status_code=503, detail="数据库中未发现任何行情数据，请先执行数据导入。")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        logger.error(f"检查数据可用性失败: {e}")
        return False


async def query_pool(dsl: str, user_id: str) -> QueryPoolResponse:
    """执行 DSL/SQL 查询并返回股票池"""
    # 增加数据就绪性检查
    with get_db() as session:
        await _ensure_latest_table_data(session)

    if dsl.startswith("SQL: "):
        raw_sql = dsl.replace("SQL: ", "").strip()
        items, as_of_date = _execute_raw_selection_sql(raw_sql)
    else:
        if not dsl.startswith(DSL_PREFIX):
            raise ValueError("DSL格式不正确，必须以 'SELECT symbol WHERE' 开头")

        conditions, combiners = _parse_dsl(dsl)
        items, as_of_date = _query_stock_pool(conditions, combiners, user_id)

    universe_total = _get_universe_total(user_id)
    summary, charts = _build_pool_summary(items, as_of_date, universe_total=universe_total)
    return QueryPoolResponse(items=items, summary=summary, charts=charts)
