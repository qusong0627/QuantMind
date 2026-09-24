"""对外数据同步端点（`/api/v1/public/sync/*`）。

2026-09-23 加固
---------------
审计发现两处生产级问题，均已修：

1. **源码里硬编码了另一台机器的生产库口令**（具体的库地址与账号已从本文件移除，
   勿再写回）。口令进过 git 历史即视为已泄露——**必须在服务端轮换**，
   本文件的改动只保证源码里不再有第二份。现在远程库地址从
   ``QM_PUBLIC_SYNC_REMOTE_DB_URL``（或 runtime.env）读，**未配置即拒**，
   不回落到任何默认值：公开默认回退值正是 C1 事故（匿名者凭公开默认密钥
   直下真单）的成因。

2. **三个端点零鉴权**，而它们返回全市场行情/特征快照的整表分页数据。
   现在整个 router 挂 ``get_current_user``，匿名一律 401。

⚠️ 行为变更：此前匿名可用。若仓库外有同步端依赖匿名访问，它会在升级后拿到
401——这是有意的（目标形态是"对外访问一律鉴权"）。届时给那个端签发 JWT 或
API Key，**不要**加匿名开关绕回去。

本 router 在仓库内没有任何调用方（前端/测试/其他代码都无），改动不影响站内功能。

2026-09-24 修
-------------
``/calendar`` 用的列名是 ``day``，实表（``qm_market_calendar_day``）是
``trade_date`` ⇒ 每次调用 500，**从未成功返回过一行**。加固那一轮只审了鉴权与
凭据，一个「永远 500 的端点」在鉴权用例下照样全绿——所以这次补了一条真连库的
行为用例（真 SQL 打到真表上），并把 ``SELECT *`` 收窄成显式列。
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.shared.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.market_calendar_seed import DEFAULT_TENANT, DEFAULT_USER
from backend.shared.runtime_secrets import get_secret

#: 远程库地址的配置键。runtime.env 权威（管理台可热换）→ 环境变量 → 未配置即拒。
REMOTE_DB_URL_ENV_KEY = "QM_PUBLIC_SYNC_REMOTE_DB_URL"

#: 鉴权是整段 router 的属性，不是逐端点的——避免新加端点时漏挂。
router = APIRouter(
    prefix="/public/sync",
    tags=["Public Data Sync"],
    dependencies=[Depends(get_current_user)],
)


def resolve_remote_db_url() -> str:
    """远程数据源地址（含 152 维原始特征与行情）。

    每次调用**实时读取**（不是导入期快照）——轮换后免重启即生效。
    未配置时抛 ``RuntimeError``，由 FastAPI 转成 500：宁可端点不可用，
    也不回落到任何硬编码地址。
    """
    # `get_secret` 的优先级已经是「真实环境变量 > runtime.env > default」，
    # 且它内部就读 `os.environ`。所以这里**不需要**再 `or os.getenv(...)`：
    # 那个分支是死的（get_secret 返回空 ⟺ 两处都没有值），只会让读者以为
    # 「patch 掉 get_secret 还会回落到环境变量」。
    url = get_secret(REMOTE_DB_URL_ENV_KEY, "").strip()
    if not url:
        raise RuntimeError(
            f"{REMOTE_DB_URL_ENV_KEY} 未配置，/public/sync/feature-snapshots 不可用。"
            "请在 .env 或「系统设置」中配置远程数据源地址。"
        )
    return url


#: 认可的 DSN 前缀。**只**放行 PostgreSQL 系——本模块的 SQL 与列名都按 PG 写，
#: 换个协议连上去只会在第一条语句炸，而错误现场离配置处很远。
_ALLOWED_DSN_SCHEMES = ("postgresql://", "postgresql+asyncpg://")


def _async_dsn(url: str) -> str:
    """psycopg 风格 DSN → asyncpg 驱动 DSN。

    非法前缀**在这里就拒**：此前用 `str.replace` 静默处理，`mysql://…` 或
    手滑写错的 `postgres://`（少个 ql）会原样透给 `create_async_engine`，
    报出来的是 SQLAlchemy 的方言解析错，看不出是配置写错了。
    """
    if not url.startswith(_ALLOWED_DSN_SCHEMES):
        raise RuntimeError(
            f"{REMOTE_DB_URL_ENV_KEY} 的协议不被支持（只接受 postgresql://）。"
            "请检查该配置值——注意是 postgresql，不是 postgres。"
        )
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@router.get("/stock-daily")
async def sync_processed_data(
    trade_date: date = Query(..., description="Start date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(1000, ge=1, le=5000),
) -> dict[str, Any]:
    """
    [88维] 拉取本地已校准的全指标数据 (stock_daily_latest)
    包含行情、基本面、技术指标和资金流向。
    """
    offset = (page - 1) * page_size
    sql = (
        "SELECT * FROM stock_daily_latest WHERE trade_date >= :t_date "
        "ORDER BY trade_date, symbol LIMIT :limit OFFSET :offset"
    )

    async with get_session(read_only=True) as session:
        result = await session.execute(
            text(sql), {"t_date": trade_date, "limit": page_size, "offset": offset}
        )
        rows = [dict(r._mapping) for r in result]
        return {"code": 200, "data": rows}


@router.get("/feature-snapshots")
async def sync_feature_data(
    trade_date: date = Query(..., description="Start date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    """
    [152维] 拉取远程深度 AI 特征数据 (feature_snapshots)
    由于列数较多，建议单页大小控制在 2000 以内。
    """
    engine = create_async_engine(_async_dsn(resolve_remote_db_url()))
    offset = (page - 1) * page_size

    sql = (
        "SELECT * FROM feature_snapshots WHERE trade_date >= :t_date "
        "ORDER BY trade_date, symbol LIMIT :limit OFFSET :offset"
    )

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(sql), {"t_date": trade_date, "limit": page_size, "offset": offset}
            )
            rows = [dict(r._mapping) for r in result]
            return {"code": 200, "data": rows}
    finally:
        await engine.dispose()


@router.get("/calendar")
async def sync_calendar(
    start_date: date = Query(..., description="Start date"),
    end_date: date | None = None,
    tenant_id: str = Query(
        DEFAULT_TENANT, description="作用域租户（默认 default）"
    ),
    user_id: str = Query(DEFAULT_USER, description="作用域用户（* = 全局兜底）"),
) -> dict[str, Any]:
    """同步交易日历（``qm_market_calendar_day``）。

    2026-09-24 修：本端点此前把列名写成 ``day``，而实表列名是 ``trade_date``
    ——每次调用都抛 ``UndefinedColumnError``（500），**从未成功返回过一行**。
    同时把 ``SELECT *`` 收窄成显式列：原写法会把 ``tenant_id`` / ``user_id`` /
    ``metadata`` 一起带出去（同步方要的是「哪天开市」，不是库内的作用域标记）。

    作用域默认 ``('default','*')``＝**全局兜底**（播种器 CLI 的默认落点，也是任何
    (tenant,user) 查询 override 时的最后一档），要取某个租户的专属覆盖就显式传参。
    """
    sql = (
        "SELECT trade_date, market, is_trading_day, source, version "
        "FROM qm_market_calendar_day "
        "WHERE tenant_id = :tenant AND user_id = :user AND trade_date >= :s_date"
    )
    params: dict[str, Any] = {
        "tenant": tenant_id,
        "user": user_id,
        "s_date": start_date,
    }
    if end_date:
        sql += " AND trade_date <= :e_date"
        params["e_date"] = end_date
    sql += " ORDER BY market, trade_date"

    async with get_session(read_only=True) as session:
        result = await session.execute(text(sql), params)
        return {"code": 200, "data": [dict(r._mapping) for r in result]}
