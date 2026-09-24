#!/usr/bin/env python3
"""滚动刷新 stock_daily_latest 最近 N 天（默认 30 天 ≈ 1 个月），数据源 QuantDB。

复用 `backend.scripts.quantdb_daily_sync.fill_pg_from_parquet`——与市场定时同步
**完全同口径**：前复权 OHLCV（qdb_daily_forward）+ adj_factor=1.0 + 估值/波动/收益
特征（qdb_features_daily）+ 基于前复权 close 重算的价格派生指标（ma*/ma_gap_*/
vol_atr_14），symbol 统一前缀式内码。

与既有增量脚本（只补 PG 最大日之后的新日期）不同，本脚本每天**重刷最近一个月**：
QuantDB 分区落盘时间不固定（可晚至次日中午），且迟到补数/修订需要回灌；upsert
幂等，可重复运行。

用法：
    # 滚动刷新最近 30 天（默认）
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py

    # 自定义窗口 / 预览
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --days 22
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --dry-run

    # 顺带清理窗口外历史行（真正滚动窗口；默认保留历史）
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --prune
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

# 以 `python /app/scripts/...` 直跑时 cwd 不在 sys.path，先补项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("rolling_sync_recent")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="滚动刷新 stock_daily_latest 最近 N 天（QuantDB 源）"
    )
    parser.add_argument("--days", type=int, default=30, help="回刷窗口（自然日，默认 30）")
    parser.add_argument("--batch-days", type=int, default=20, help="每批交易日数（默认 20）")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="删除窗口外的历史行（真正滚动窗口；默认保留历史）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只枚举窗口内交易日，不写库")
    return parser.parse_args()


def _db_url() -> str:
    import os
    from urllib.parse import quote

    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url.replace("+asyncpg", "").replace("+psycopg2", "")
    host = os.getenv("DB_HOST", "db")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "quantmind")
    password = quote(os.getenv("DB_PASSWORD", ""), safe="")
    dbname = os.getenv("DB_NAME", "quantmind")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def _table_range(db_url: str) -> tuple[int, object, object]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*)::bigint, MIN(trade_date), MAX(trade_date) "
                    "FROM public.stock_daily_latest"
                )
            ).fetchone()
        return int(row[0] or 0), row[1], row[2]
    finally:
        engine.dispose()


def _prune(db_url: str, before: date) -> int:
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            return conn.execute(
                text("DELETE FROM public.stock_daily_latest WHERE trade_date < :d"),
                {"d": before},
            ).rowcount
    finally:
        engine.dispose()


def _invalidate_cache() -> None:
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        get_redis_sentinel_client().delete("qm:admin:data_status")
        LOGGER.info("已清除数据状态缓存 qm:admin:data_status")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("清除数据状态缓存失败: %s", exc)


def _enrich_names_industry(db_url: str, start_date: date) -> int:
    """回填 stock_name / industry（fill_pg_from_parquet 不写这两列）。

    来源 QuantDB instrument_list（Symbol→Name、rs_hyname），统一转前缀式内码。
    仅更新窗口内行，避免整表写放大。
    """
    import psycopg2
    from psycopg2.extras import execute_values

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    from backend.shared.stock_utils import StockCodeUtil

    hub = QuantDBDataHub.get_instance()
    stock_list = hub.fetch_stock_list()
    industry = hub.fetch_instrument_industry()

    name_map: dict[str, str] = {}
    if not stock_list.empty and "symbol" in stock_list.columns:
        name_col = next(
            (c for c in ("Name", "name", "stock_name") if c in stock_list.columns), None
        )
        if name_col:
            name_map = {
                StockCodeUtil.to_prefix(str(s)): str(n)
                for s, n in zip(stock_list["symbol"], stock_list[name_col], strict=False)
                if n
            }
    ind_map: dict[str, str] = {}
    if not industry.empty and {"symbol", "ind_name_l1"}.issubset(industry.columns):
        ind_map = {
            StockCodeUtil.to_prefix(str(s)): str(n)
            for s, n in zip(industry["symbol"], industry["ind_name_l1"], strict=False)
            if n
        }
    if not name_map and not ind_map:
        LOGGER.warning("QuantDB 无名称/行业映射，跳过富化")
        return 0

    symbols = sorted(set(name_map) | set(ind_map))
    rows = [(s, name_map.get(s), ind_map.get(s)) for s in symbols]
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _sdl_name_map "
                "(symbol text PRIMARY KEY, stock_name text, industry text) ON COMMIT DROP"
            )
            execute_values(
                cur,
                "INSERT INTO _sdl_name_map (symbol, stock_name, industry) VALUES %s",
                rows,
                page_size=2000,
            )
            cur.execute(
                "UPDATE stock_daily_latest s SET "
                "stock_name = COALESCE(m.stock_name, s.stock_name), "
                "industry = COALESCE(m.industry, s.industry) "
                "FROM _sdl_name_map m "
                "WHERE s.symbol = m.symbol AND s.trade_date >= %s",
                (start_date,),
            )
            updated = cur.rowcount
        conn.commit()
        LOGGER.info("富化 stock_name/industry: 更新 %s 行（映射 %s 只）", updated, len(rows))
        return int(updated or 0)
    finally:
        conn.close()


# 概念板块名 → PG concept_* 固定列（板块名含任一关键词即置 1）
_CONCEPT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "concept_ai": (
        "人工智能", "AIGC", "多模态AI", "智谱AI", "AI", "算力", "数据中心",
        "大数据", "数据要素", "数据确权", "时空大数据", "机器人",
    ),
    "concept_chip": ("芯片", "半导体"),
    "concept_new_energy": ("新能源", "储能", "光伏", "锂"),
    "concept_pv": ("光伏",),
    "concept_lithium": ("锂",),
    "concept_military": ("军工", "国防"),
    "concept_medical": ("医药", "医疗"),
    "concept_fintech": ("数字货币", "金融科技", "互联网金融"),
    "concept_consumption": ("消费", "白酒", "食品饮料"),
    "concept_state_owned": ("国企", "央企", "国资"),
}
# 指数成分（QuantDB index_weights，按 symbol 静态快照）→ PG idx_* 列
_IDX_WEIGHT_FILES: dict[str, str] = {
    "idx_zz1000": "000852.SH.parquet",
    "idx_chinext": "399006.SZ.parquet",
}


def _to_num(series):
    import pandas as pd

    return pd.to_numeric(series, errors="coerce")


def _enrich_gap_columns(db_url: str, start_date: date, end_date: date) -> None:
    """回填 fill_pg_from_parquet 未覆盖的列。

    每交易日（features_daily）：is_st / idx_hs300 / idx_margin / idx_all /
      roe(=net_profit_ttm/equity，小数口径) / bp(=1/pb) / ep_ttm(=1/pe_ttm) /
      ln_mv_total(=ln(total_mv))。
    静态（index_weights / sector_concept）：idx_zz1000 / idx_chinext / concept_*。
    """
    import os

    import numpy as np
    import pandas as pd
    import psycopg2
    from psycopg2.extras import execute_values

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    from backend.shared.stock_utils import StockCodeUtil

    hub = QuantDBDataHub.get_instance()
    base = str(hub.data_dir)

    # ---- 每交易日列 ----
    fd = hub.fetch_features_daily(start=start_date, end=end_date)
    if not fd.empty and "trade_date" in fd.columns:
        mv = _to_num(fd.get("total_mv"))
        per_date = pd.DataFrame(
            {
                "symbol": fd["symbol"].map(lambda s: StockCodeUtil.to_prefix(str(s))),
                "trade_date": pd.to_datetime(fd["trade_date"]).dt.date,
                "is_st": _to_num(fd.get("is_st")).fillna(0).astype(int),
                "idx_hs300": _to_num(fd.get("in_hs300")).fillna(0).astype(int),
                "idx_margin": _to_num(fd.get("is_margin")).fillna(0).astype(int),
                "idx_all": 1,
                "roe": (
                    _to_num(fd.get("net_profit_ttm")) / _to_num(fd.get("equity"))
                ).round(6),
                "bp": (1.0 / _to_num(fd.get("pb")).replace(0, np.nan)).round(6),
                "ep_ttm": (1.0 / _to_num(fd.get("pe_ttm")).replace(0, np.nan)).round(6),
                "ln_mv_total": np.log(mv.where(mv > 0)).round(6),
            }
        )
        rows = [tuple(r) for r in per_date.itertuples(index=False, name=None)]
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE _sdl_per_date ("
                    "symbol text, trade_date date, is_st int, idx_hs300 int, "
                    "idx_margin int, idx_all int, roe double precision, "
                    "bp double precision, ep_ttm double precision, "
                    "ln_mv_total double precision) ON COMMIT DROP"
                )
                execute_values(cur, "INSERT INTO _sdl_per_date VALUES %s", rows, page_size=5000)
                cur.execute(
                    "UPDATE stock_daily_latest s SET "
                    "is_st=m.is_st, idx_hs300=m.idx_hs300, idx_margin=m.idx_margin, "
                    "idx_all=m.idx_all, roe=m.roe, bp=m.bp, ep_ttm=m.ep_ttm, "
                    "ln_mv_total=m.ln_mv_total "
                    "FROM _sdl_per_date m "
                    "WHERE s.symbol=m.symbol AND s.trade_date=m.trade_date"
                )
                LOGGER.info("富化每交易日列(is_st/idx_hs300/idx_margin/roe/bp/ep/ln_mv): %s 行", cur.rowcount)
            conn.commit()
        finally:
            conn.close()
    else:
        LOGGER.warning("features_daily 无数据，跳过每交易日列富化")

    # ---- 静态列：概念 / 指数成分 ----
    concept_flags: dict[str, set[str]] = {}
    sm_path = os.path.join(base, "2_base_sector", "sector_concept", "sector_members.parquet")
    if os.path.exists(sm_path):
        sm = pd.read_parquet(sm_path)
        # 只用概念板块（行业/地区板块名称含关键词会误命中，如"半导体"行业）
        if "SectorType" in sm.columns:
            sm = sm[sm["SectorType"].astype(str) == "概念板块"]
        for name, sym in zip(
            sm["SectorName"].astype(str), sm["Symbol"].astype(str), strict=False
        ):
            hit = [c for c, kws in _CONCEPT_KEYWORDS.items() if any(k in name for k in kws)]
            if hit:
                concept_flags.setdefault(StockCodeUtil.to_prefix(sym), set()).update(hit)

    idx_members: dict[str, set[str]] = {}
    for col, fname in _IDX_WEIGHT_FILES.items():
        p = os.path.join(base, "2_base_sector", "index_weights", fname)
        if os.path.exists(p):
            df = pd.read_parquet(p, columns=["Symbol"])
            idx_members[col] = {StockCodeUtil.to_prefix(str(s)) for s in df["Symbol"]}

    symbols = set(concept_flags)
    for members in idx_members.values():
        symbols |= members
    if symbols:
        concept_cols = list(_CONCEPT_KEYWORDS)
        idx_cols = list(_IDX_WEIGHT_FILES)
        rows2 = []
        for sym in sorted(symbols):
            flags = concept_flags.get(sym, set())
            rows2.append(
                (sym,)
                + tuple(1 if c in flags else 0 for c in concept_cols)
                + tuple(1 if sym in idx_members.get(c, set()) else 0 for c in idx_cols)
            )
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE _sdl_static (symbol text, "
                    + ", ".join(f"{c} int" for c in concept_cols + idx_cols)
                    + ") ON COMMIT DROP"
                )
                execute_values(cur, "INSERT INTO _sdl_static VALUES %s", rows2, page_size=5000)
                set_sql = ", ".join(f"{c}=m.{c}" for c in concept_cols + idx_cols)
                # 先清零窗口内旧值，再按映射置位（否则上一版误命中的 1 不会被清掉）
                cur.execute(
                    "UPDATE stock_daily_latest SET "
                    + ", ".join(f"{c}=0" for c in concept_cols + idx_cols)
                    + " WHERE trade_date >= %s",
                    (start_date,),
                )
                cur.execute(
                    f"UPDATE stock_daily_latest s SET {set_sql} "
                    "FROM _sdl_static m WHERE s.symbol=m.symbol"
                )
                LOGGER.info("富化概念/指数成分列: %s 行（映射 %s 只）", cur.rowcount, len(rows2))
            conn.commit()
        finally:
            conn.close()


def main() -> int:
    args = parse_args()
    days = max(1, int(args.days))
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    db_url = _db_url()

    from backend.scripts.quantdb_daily_sync import (
        _trade_dates,
        fill_pg_from_parquet,
    )
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    if not hub.available:
        LOGGER.error("QuantDB 数据不可用，终止")
        return 1

    trade_days = _trade_dates(hub, start_date, end_date)
    if not trade_days:
        LOGGER.warning("窗口 [%s, %s] 内无 QuantDB 交易日", start_date, end_date)
        return 0

    rows_before, min_before, max_before = _table_range(db_url)
    LOGGER.info(
        "同步前: rows=%s range=[%s, %s]；窗口=[%s, %s] 交易日=%d（%s..%s）",
        rows_before,
        min_before,
        max_before,
        start_date,
        end_date,
        len(trade_days),
        trade_days[0],
        trade_days[-1],
    )

    if args.dry_run:
        LOGGER.info("dry-run：将刷新 %d 个交易日，未写库", len(trade_days))
        return 0

    result = fill_pg_from_parquet(
        start_date=start_date, end_date=end_date, batch_days=args.batch_days
    )
    LOGGER.info("fill_pg_from_parquet 结果: %s", result)

    try:
        _enrich_names_industry(db_url, start_date)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("名称/行业富化失败（不阻断）: %s", exc)

    try:
        _enrich_gap_columns(db_url, start_date, end_date)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("缺口列富化失败（不阻断）: %s", exc)

    if args.prune:
        deleted = _prune(db_url, start_date)
        LOGGER.info("prune：删除窗口外行 %s 条（< %s）", deleted, start_date)

    _invalidate_cache()

    rows_after, min_after, max_after = _table_range(db_url)
    LOGGER.info("同步后: rows=%s range=[%s, %s]", rows_after, min_after, max_after)

    return 0 if result.get("status") in ("ok", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
