#!/usr/bin/env python3
"""
完善 stock_daily_latest 数据 — 集成 a-stock-data skill 数据源, 确保准确性.

目标列 (前端 ResearchPlatformPage 表格 + 量化因子), 当前问题:
  - turnover_rate      换手率:        0.1%  (几乎全空)        -> 腾讯财经批量补
  - pe_ttm / roe       PE/ROE:        78%/75% (负值被旧源 `>0` 剔除) -> 修复负值 + 腾讯补
  - consecutive_limit_up_days 连板:   100%覆盖但值全0 (无效) -> 重算 (涨停连续天数)
  - idx_hs300/zz1000/chinext/margin/all 指数归属: 全0 (无效) -> 成分股列表补
  - is_st              ST状态:        仅21行=1 (无效, A股有数百只ST) -> 名称含ST重算
  - kdj_k/beta_20/volume_ma_5/bp/ep_ttm/ln_mv_total 衍生因子: 0% -> DB计算
  - listed_days        上市天数:      0%   -> 东财个股信息/腾讯补

数据源策略 (遵循 a-stock-data skill 优先级: 能用腾讯/mootdx就别用东财):
  Phase A - 腾讯财经 API (不封IP, 批量, 字段校准):
            vals[38]=换手率 [39]=PE_TTM [44]=总市值 [45]=流通市值 [46]=PB
            全市场分批拉快照, 按前复权收盘价基一致缩放为逐日估值.
            历史日期用最新日 close 基准缩放; 最新日直接用快照值.
  Phase B - DB 重算 (无需外部源, 立竿见影):
            连板/指数归属/ST状态/衍生技术因子(kdj/beta/volume_ma/bp/ep/ln_mv).
  Phase C - QuantDB 前向填充 (roe 等季报数据, 报告期内不变).

准确性关键:
  - 负 PE/ROE 是亏损股的真实值, 必须保留 (旧源 `>0` 过滤是 bug, 这里只跳过 0/NULL).
  - 腾讯快照用于历史日期时按"前复权close(d)/前复权close(最新日)"缩放, 基一致 (非真实价),
    避免被 adj_factor 错误缩小. pe/pb/mv 随价格线性变化, TTM盈利/净资产/股本近似不变.

用法:
    python backend/scripts/enrich_sdl_data.py --diagnose           # 仅诊断
    python backend/scripts/enrich_sdl_data.py --phase-tencent      # 阶段A: 腾讯补 pe/pb/mv/换手率
    python backend/scripts/enrich_sdl_data.py --phase-recompute   # 阶段B: 重算连板/指数/ST/衍生因子
    python backend/scripts/enrich_sdl_data.py --phase-quantdb     # 阶段C: QuantDB+前向填充 roe等
    python backend/scripts/enrich_sdl_data.py --all               # 全部
    python backend/scripts/enrich_sdl_data.py --all --dry-run    # 试运行
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# QuantDB 覆盖之后, 由腾讯/重算补充
PARQUET_CUTOFF = date(2026, 5, 20)

FUND_COLS = ["pe_ttm", "pb", "roe", "total_mv", "float_mv", "industry"]

# 腾讯财经批量拉取参数 (skill §1.2, 单请求上限~80只)
TENCENT_BATCH = 80


def _get_engine():
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    if not db_url.startswith("postgresql"):
        db_url = (
            f"postgresql+psycopg2://{os.getenv('DB_USER', 'quantmind')}:"
            f"{os.getenv('DB_PASSWORD', 'quantmind2026')}@{os.getenv('DB_HOST', 'db')}:5432/quantmind"
        )
    return create_engine(db_url, pool_pre_ping=True, future=True)


# ─────────────────────────────────────────────────────────────────────────
# 诊断
# ─────────────────────────────────────────────────────────────────────────
def diagnose(engine):
    print("\n" + "=" * 72)
    print("  stock_daily_latest 数据完善诊断 (前端表格所需列)")
    print("=" * 72)
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM stock_daily_latest WHERE volume > 0")).scalar()
        r = conn.execute(
            text("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT symbol) FROM stock_daily_latest WHERE volume > 0")
        ).one()
        print(f"\n📊 总行数: {total:,}  股票数: {r[2]}  日期: {r[0]} → {r[1]}")

        checks = [
            # (列, 描述, 条件类型)
            ("turnover_rate", "换手率", "num>0"),
            ("pe_ttm", "PE(TTM)", "num!=0"),   # 保留负值, 仅跳过0/NULL
            ("pb", "PB", "num!=0"),
            ("roe", "ROE(%)", "num!=0"),
            ("total_mv", "总市值", "num>0"),
            ("float_mv", "流通市值", "num>0"),
            ("industry", "行业", "str"),
            ("stock_name", "名称", "str"),
            ("consecutive_limit_up_days", "连板(>0占比)", "num>0"),
            ("kdj_k", "KDJ-K", "num!=0"),
            ("beta_20", "20日Beta", "num!=0"),
            ("volume_ma_5", "5日均量", "num>0"),
            ("bp", "账面市值比", "num>0"),
            ("ep_ttm", "盈利收益率", "num!=0"),
            ("ln_mv_total", "对数市值", "num!=0"),
            ("listed_days", "上市天数", "num>0"),
            ("main_flow", "主力资金流", "num!=0"),
            ("flow_net_amount", "资金净额", "num!=0"),
            ("inst_ownership", "机构持股", "num>0"),
            ("profit_growth", "利润增长", "num!=0"),
        ]
        print("\n📋 列覆盖率:")
        for col, desc, kind in checks:
            if kind == "str":
                has = conn.execute(text(
                    f"SELECT COUNT(*) FROM stock_daily_latest WHERE volume>0 AND {col} IS NOT NULL AND {col} <> ''"
                )).scalar()
            elif kind == "num>0":
                has = conn.execute(text(
                    f"SELECT COUNT(*) FROM stock_daily_latest WHERE volume>0 AND {col} IS NOT NULL AND {col} > 0"
                )).scalar()
            else:  # num!=0 (保留负值)
                has = conn.execute(text(
                    f"SELECT COUNT(*) FROM stock_daily_latest WHERE volume>0 AND {col} IS NOT NULL AND {col} <> 0"
                )).scalar()
            pct = has / total * 100 if total else 0
            status = "✅" if pct > 80 else "⚠️ " if pct > 10 else "❌"
            print(f"   {status} {desc:14s} {col:24s}: {has:>8,} / {total:,} ({pct:5.1f}%)")

        # 指数归属/ST 实际值分布 (覆盖率100%但值可能全0)
        print("\n🔍 布尔列实际值分布 (覆盖率可能虚高):")
        for col, desc in [("idx_hs300", "沪深300成分"), ("idx_all", "全市场"), ("is_st", "ST状态")]:
            rows = conn.execute(text(
                f"SELECT {col}, COUNT(*) FROM stock_daily_latest WHERE volume>0 GROUP BY {col} ORDER BY 2 DESC LIMIT 2"
            )).fetchall()
            dist = "; ".join(f"{r[0]}={r[1]:,}" for r in rows)
            print(f"   {desc:14s} {col}: {dist}")
    return total


# ─────────────────────────────────────────────────────────────────────────
# Phase A: 腾讯财经批量补 pe_ttm/pb/total_mv/float_mv/turnover_rate (含负值修复)
# ─────────────────────────────────────────────────────────────────────────
def _suffix_to_tencent(symbol: str) -> str | None:
    """600000.SH -> sh600000 (腾讯格式)."""
    if not isinstance(symbol, str) or "." not in symbol:
        return None
    code, market = symbol.split(".")
    if not code.isdigit() or len(code) != 6:
        return None
    if market == "SH":
        return f"sh{code}"
    if market == "SZ":
        return f"sz{code}"
    if market == "BJ":
        return f"bj{code}"
    return None


def _tencent_quote_batch(tencent_codes: list[str]) -> dict[str, dict]:
    """腾讯财经批量拉取 (skill §1.2, 不封IP, 字段已校准). 返回 {tencent_code: {pe,pb,mv,fmv,turnover}}."""
    if not tencent_codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(tencent_codes)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    result = {}
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read().decode("gbk")
    except Exception as e:
        print(f"   ⚠️  腾讯请求失败: {type(e).__name__}: {str(e)[:60]}")
        return result
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]  # e.g. sh600519
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        try:
            def _f(idx):
                v = float(vals[idx]) if vals[idx] else 0.0
                return v if v != 0 else None  # 0 视为无效 (腾讯对缺失返回0)
            result[key] = {
                "symbol": None,  # 后面映射
                "code": code,
                "pe_ttm": _f(39),
                "pb": _f(46),
                "total_mv_yi": _f(44),   # 单位: 亿
                "float_mv_yi": _f(45),
                "turnover_rate": _f(38),  # 单位: %
            }
        except (ValueError, IndexError):
            continue
    return result


def phase_tencent(engine, dry_run: bool = False) -> int:
    """阶段A: 腾讯财经批量补 pe_ttm/pb/total_mv/float_mv/turnover_rate, 保留负值."""
    print("\n" + "=" * 72)
    print("  阶段A: 腾讯财经批量补 PE/PB/市值/换手率 (含负值修复)")
    print("=" * 72)

    # 1. 取全市场 symbol 列表
    print("\n1️⃣  读取全市场 symbol 列表...")
    with engine.connect() as conn:
        syms = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT symbol FROM stock_daily_latest WHERE volume > 0 ORDER BY symbol"
        )).fetchall()]
        snap_date = conn.execute(
            text("SELECT MAX(trade_date) FROM stock_daily_latest WHERE volume > 0")
        ).scalar()
    print(f"   {len(syms)} 只股票, 最新交易日: {snap_date}")

    # 过滤指数 (5位代码如 000300.SH/399300.SZ 是指数, 腾讯拉不到正常行情)
    sym_to_tc = {}
    for s in syms:
        tc = _suffix_to_tencent(s)
        if tc:
            sym_to_tc[s] = tc
    tcs = list(sym_to_tc.values())
    print(f"   待拉取 (剔除指数): {len(tcs)} 只")

    # 2. 分批拉取腾讯快照
    print(f"\n2️⃣  分批拉取腾讯快照 (每批 {TENCENT_BATCH} 只)...")
    snap = {}
    t0 = time.time()
    for i in range(0, len(tcs), TENCENT_BATCH):
        batch = tcs[i:i + TENCENT_BATCH]
        snap.update(_tencent_quote_batch(batch))
        if (i // TENCENT_BATCH + 1) % 10 == 0:
            print(f"   已拉 {min(i + TENCENT_BATCH, len(tcs))}/{len(tcs)} 只 ({time.time()-t0:.1f}s)")
        time.sleep(0.15)  # 腾讯不封IP, 但轻微节流更稳
    print(f"   ✅ 快照拉取完成: {len(snap)} 只 ({time.time()-t0:.1f}s)")
    if not snap:
        print("   ❌ 未拉到任何快照")
        return 0

    # 建 symbol -> 快照 映射
    tc_to_sym = {v: k for k, v in sym_to_tc.items()}
    snap_rows = []
    for tc, v in snap.items():
        sym = tc_to_sym.get(tc)
        if sym and v.get("pe_ttm") or v.get("pb") or v.get("total_mv_yi") or v.get("turnover_rate"):
            snap_rows.append({
                "symbol": sym,
                "pe_ttm": v.get("pe_ttm"),
                "pb": v.get("pb"),
                "total_mv": (v["total_mv_yi"] * 1e8) if v.get("total_mv_yi") else None,  # 亿 -> 元
                "float_mv": (v["float_mv_yi"] * 1e8) if v.get("float_mv_yi") else None,
                "turnover_rate": v.get("turnover_rate"),  # %
            })
    snap_df = pd.DataFrame(snap_rows)
    print(f"   有效快照: {len(snap_df)} 只")

    if dry_run:
        print("   [DRY RUN] 将用快照补全 pe_ttm/pb/total_mv/float_mv/turnover_rate")
        print(f"   快照样例:\n{snap_df.head(3).to_string()}")
        return 0

    # 3. 最新交易日: 直接用快照值填 (无 NULLIF(0) 限制, 保留负值)
    print(f"\n3️⃣  最新交易日 ({snap_date}) 直接填快照值 (保留负值)...")
    latest_df = snap_df.copy()
    latest_df["trade_date"] = pd.to_datetime(snap_date).date()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmp_tencent_latest"))
    latest_df.to_sql("tmp_tencent_latest", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
    with engine.begin() as conn:
        # 仅填 NULL/0 (保留已有的有效值, 含负值); turnover 全空直接填
        result = conn.execute(text("""
            UPDATE stock_daily_latest s
            SET pe_ttm       = COALESCE(NULLIF(s.pe_ttm, 0), t.pe_ttm),
                pb           = COALESCE(NULLIF(s.pb, 0), t.pb),
                total_mv     = COALESCE(NULLIF(s.total_mv, 0), t.total_mv),
                float_mv     = COALESCE(NULLIF(s.float_mv, 0), t.float_mv),
                turnover_rate = COALESCE(NULLIF(s.turnover_rate, 0), t.turnover_rate)
            FROM tmp_tencent_latest t
            WHERE s.symbol = t.symbol AND s.trade_date = t.trade_date AND s.volume > 0
        """))
        updated_latest = result.rowcount
        conn.execute(text("DROP TABLE IF EXISTS tmp_tencent_latest"))
    print(f"   ✅ 最新交易日: {updated_latest:,} 行已更新")

    # 4. 历史日期: 按前复权 close 基一致缩放 pe/pb/mv (turnover 不缩放, 前向填充)
    #    ratio = close(d) / close(最新日), 两者同前复权基准 = 真实价格变动比
    #    pe/pb/mv 随价格线性变化 (TTM盈利/净资产/股本近似不变)
    print("\n4️⃣  历史日期按前复权 close 缩放 PE/PB/市值 (基一致)...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT symbol, trade_date, close
            FROM stock_daily_latest
            WHERE volume > 0 AND trade_date < :latest
            ORDER BY symbol, trade_date
        """), {"latest": snap_date}).fetchall()
        ref_rows = conn.execute(text("""
            SELECT symbol, close AS ref_close
            FROM stock_daily_latest
            WHERE trade_date = :d AND volume > 0
        """), {"d": snap_date}).fetchall()
    if not rows:
        print("   无历史日期数据, 跳过")
        return updated_latest
    db = pd.DataFrame(rows, columns=["symbol", "trade_date", "close"])
    db["trade_date"] = db["trade_date"].astype(str)
    ref = pd.DataFrame(ref_rows)
    merged = db.merge(snap_df, on="symbol", how="inner").merge(ref, on="symbol", how="left")
    ratio = (merged["close"] / merged["ref_close"]).where(merged["ref_close"] > 0)
    merged["pe_fill"] = merged["pe_ttm"] * ratio
    merged["pb_fill"] = merged["pb"] * ratio
    merged["mv_fill"] = merged["total_mv"] * ratio
    merged["fmv_fill"] = merged["float_mv"] * ratio
    mask = merged[["pe_fill", "pb_fill", "mv_fill", "fmv_fill"]].notna().any(axis=1)
    fills = merged[mask][["symbol", "trade_date", "pe_fill", "pb_fill", "mv_fill", "fmv_fill"]].copy()
    print(f"   待回填 (历史缩放): {len(fills):,} 行")

    if not fills.empty:
        fills.columns = ["symbol", "trade_date", "pe_ttm", "pb", "total_mv", "float_mv"]
        fills["trade_date"] = pd.to_datetime(fills["trade_date"]).dt.date
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS tmp_tencent_hist"))
        fills.to_sql("tmp_tencent_hist", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
        with engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS tmp_th_sym_dt ON tmp_tencent_hist(symbol, trade_date)"))
            result = conn.execute(text("""
                UPDATE stock_daily_latest s
                SET pe_ttm   = COALESCE(NULLIF(s.pe_ttm, 0),   t.pe_ttm,   s.pe_ttm),
                    pb       = COALESCE(NULLIF(s.pb, 0),       t.pb,       s.pb),
                    total_mv = COALESCE(NULLIF(s.total_mv, 0), t.total_mv, s.total_mv),
                    float_mv = COALESCE(NULLIF(s.float_mv, 0), t.float_mv, s.float_mv)
                FROM tmp_tencent_hist t
                WHERE s.symbol = t.symbol AND s.trade_date = t.trade_date
            """))
            print(f"   ✅ 历史缩放: {result.rowcount:,} 行已更新")
            conn.execute(text("DROP TABLE IF EXISTS tmp_tencent_hist"))

    # 5. turnover_rate 前向填充 (换手率不随价格线性变化, 用最新日值前向填充近期)
    print("\n5️⃣  换手率前向填充 (用最近非空值)...")
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE stock_daily_latest t
            SET turnover_rate = src.turnover_rate
            FROM (
                SELECT DISTINCT ON (symbol) symbol, trade_date, turnover_rate
                FROM stock_daily_latest
                WHERE turnover_rate IS NOT NULL AND turnover_rate > 0
                ORDER BY symbol, trade_date DESC
            ) src
            WHERE t.symbol = src.symbol
              AND (t.turnover_rate IS NULL OR t.turnover_rate = 0)
        """))
        print(f"   ✅ 换手率前向填充: {result.rowcount:,} 行")
    return updated_latest


# ─────────────────────────────────────────────────────────────────────────
# Phase B: DB 重算 (连板/指数/ST/衍生因子) — 无需外部源
# ─────────────────────────────────────────────────────────────────────────
def _recompute_limit_up(engine, dry_run: bool = False) -> int:
    """重算 consecutive_limit_up_days: 连续涨停天数 (pandas 实现, 依赖 is_st 已先算).
    A股涨停: 主板±10%, 创业板/科创板±20%, ST股±5%. ST用4.8%阈值, 其余9.8%."""
    print("\n  [B1] 重算 consecutive_limit_up_days (连板)...")
    if dry_run:
        print("     [DRY RUN] 将按涨停规则重算连板天数")
        return 0
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT symbol, trade_date, pct_change, is_st
            FROM stock_daily_latest WHERE volume > 0 ORDER BY symbol, trade_date
        """)).fetchall()
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "trade_date", "pct_change", "is_st"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["pct_change"] = pd.to_numeric(df["pct_change"], errors="coerce")
    # 涨停判定: ST>=4.8%, 其余>=9.8% (创业板/科创板20%也能被9.8%捕获)
    is_st = df["is_st"].fillna(0).astype(int) != 0
    thresh = np.where(is_st, 4.8, 9.8)
    is_lz = (df["pct_change"].fillna(-99) >= thresh).astype(int)

    # 连续涨停天数: 每只股票内, 对 is_lz 序列做 "未涨停作为断点" 的累计计数
    # 用 cumsum 断点分组: 每遇到 is_lz=0 开新组, 组内累计涨停天数
    df["is_lz"] = is_lz
    df["brk"] = (df["is_lz"] == 0).astype(int)
    df["grp"] = df.groupby("symbol")["brk"].cumsum()
    # 连续涨停段内的序号(0-based)+1 = 连板天数; 未涨停行置0
    df["consecutive_limit_up_days"] = 0
    lz_mask = df["is_lz"] == 1
    df.loc[lz_mask, "consecutive_limit_up_days"] = (
        df.loc[lz_mask].groupby(["symbol", "grp"]).cumcount() + 1
    )

    out = df[["symbol", "trade_date", "consecutive_limit_up_days"]].copy()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmp_limitup"))
    out.to_sql("tmp_limitup", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS tmp_lu_sym_dt ON tmp_limitup(symbol, trade_date)"))
        result = conn.execute(text("""
            UPDATE stock_daily_latest s
            SET consecutive_limit_up_days = t.consecutive_limit_up_days
            FROM tmp_limitup t
            WHERE s.symbol = t.symbol AND s.trade_date = t.trade_date
        """))
        updated = result.rowcount
        conn.execute(text("DROP TABLE IF EXISTS tmp_limitup"))
    print(f"     ✅ 连板重算: {updated:,} 行")
    return updated


def _recompute_st(engine, dry_run: bool = False) -> int:
    """重算 is_st: 股票名称含 ST/*ST/退.*ST."""
    print("\n  [B2] 重算 is_st (名称含 ST/退市)...")
    if dry_run:
        return 0
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE stock_daily_latest
            SET is_st = CASE
                WHEN stock_name ~ '(^|\\*)ST|\\*ST|退' THEN 1
                ELSE 0 END
            WHERE volume > 0 AND stock_name IS NOT NULL AND stock_name <> ''
        """))
        # 统计更新后 ST 数
        st_cnt = conn.execute(text(
            "SELECT COUNT(DISTINCT symbol) FROM stock_daily_latest WHERE is_st <> 0"
        )).scalar()
    print(f"     ✅ is_st 重算完成, ST 股票数: {st_cnt}")
    return result.rowcount


def _recompute_derived_factors(engine, dry_run: bool = False) -> int:
    """重算衍生因子: kdj_k/beta_20/volume_ma_5/bp/ep_ttm/ln_mv_total.
    算法与 update_feature_parquet.py 一致."""
    print("\n  [B3] 重算衍生技术因子 (kdj/beta/volume_ma/bp/ep/ln_mv)...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT symbol, trade_date, open, high, low, close, volume, pe_ttm, pb, total_mv
            FROM stock_daily_latest WHERE volume > 0
            ORDER BY symbol, trade_date
        """)).fetchall()
    if not rows:
        print("     ❌ 无数据")
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "trade_date", "open", "high", "low",
                                     "close", "volume", "pe_ttm", "pb", "total_mv"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for c in ("open", "high", "low", "close", "volume", "pe_ttm", "pb", "total_mv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    print(f"     {len(df):,} 行, {df['symbol'].nunique()} 只股票, 计算中...")

    g = df.groupby("symbol", group_keys=False)

    # KDJ (9,3,3) K值 — 复用 update_feature_parquet._kdj 算法 (transform 高效, 无 warning)
    low_n = g["low"].transform(lambda x: x.rolling(9, min_periods=1).min())
    high_n = g["high"].transform(lambda x: x.rolling(9, min_periods=1).max())
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    df["kdj_k"] = rsv.groupby(df["symbol"]).transform(
        lambda x: x.ewm(alpha=1 / 3, adjust=False).mean()
    )

    # volume_ma_5: 5日成交量均线
    df["volume_ma_5"] = g["volume"].transform(lambda x: x.rolling(5, min_periods=1).mean())

    # beta_20: 个股收益 vs 市场收益 的20日beta. 无指数日线在表内, 用全市场等权日收益作市场代理.
    # beta = cov(ri, rm) / var(rm). 用滚动协方差公式展开 (cov=mean(rm)-mean(r)*mean(m)), 避免 apply.
    df["ret"] = g["close"].pct_change()
    mkt = df.groupby("trade_date")["ret"].transform("mean")
    df["_rm"] = df["ret"] * mkt  # 临时列: 个股收益 × 市场收益 (按 symbol 分组 rolling)
    mean_r = g["ret"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    mean_m = mkt.rolling(20, min_periods=10).mean()
    mean_rm = g["_rm"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    var_m = mkt.rolling(20, min_periods=10).var()
    cov_rm = mean_rm - mean_r * mean_m
    df["beta_20"] = (cov_rm / var_m.replace(0, np.nan)).values
    df = df.drop(columns=["_rm", "ret"])

    # 衍生估值因子
    df["bp"] = np.where(df["pb"] > 0, 1.0 / df["pb"], np.nan)        # 账面市值比 = 1/PB
    df["ep_ttm"] = np.where(df["pe_ttm"].abs() > 0, 1.0 / df["pe_ttm"], np.nan)  # 盈利收益率 = 1/PE (PE为负则EP为负)
    df["ln_mv_total"] = np.where(df["total_mv"] > 0, np.log(df["total_mv"]), np.nan)

    if dry_run:
        print(f"     [DRY RUN] 将更新 {len(df)} 行衍生因子")
        return 0

    # 批量写回 (仅非空)
    out = df[["symbol", "trade_date", "kdj_k", "beta_20", "volume_ma_5", "bp", "ep_ttm", "ln_mv_total"]].copy()
    out = out.replace([np.inf, -np.inf], np.nan)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmp_derived"))
    out.to_sql("tmp_derived", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS tmp_d_sym_dt ON tmp_derived(symbol, trade_date)"))
        result = conn.execute(text("""
            UPDATE stock_daily_latest s
            SET kdj_k = COALESCE(NULLIF(s.kdj_k,0), t.kdj_k, s.kdj_k),
                beta_20 = COALESCE(NULLIF(s.beta_20,0), t.beta_20, s.beta_20),
                volume_ma_5 = COALESCE(NULLIF(s.volume_ma_5,0), t.volume_ma_5, s.volume_ma_5),
                bp = COALESCE(NULLIF(s.bp,0), t.bp, s.bp),
                ep_ttm = COALESCE(NULLIF(s.ep_ttm,0), t.ep_ttm, s.ep_ttm),
                ln_mv_total = COALESCE(NULLIF(s.ln_mv_total,0), t.ln_mv_total, s.ln_mv_total)
            FROM tmp_derived t
            WHERE s.symbol = t.symbol AND s.trade_date = t.trade_date AND t.kdj_k IS NOT NULL
        """))
        updated = result.rowcount
        conn.execute(text("DROP TABLE IF EXISTS tmp_derived"))
    print(f"     ✅ 衍生因子: {updated:,} 行已更新")
    return updated


def _recompute_indices(engine, dry_run: bool = False) -> int:
    """重算 idx_*: 用东财/本地成分股列表. 简化版: idx_all 全市场=1, 其余按市值/代码规则近似.
    准确的成分股需外部列表, 这里先置 idx_all=1 (全市场基准)."""
    print("\n  [B4] 重算 idx_all (全市场基准=1)...")
    if dry_run:
        return 0
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE stock_daily_latest SET idx_all = 1 WHERE volume > 0 AND idx_all = 0
        """))
    print(f"     ✅ idx_all 置1: {result.rowcount:,} 行")
    print("     ℹ️  idx_hs300/zz1000/chinext/margin 需成分股列表, 见 phase_indices 外部补充")
    return result.rowcount


def phase_recompute(engine, dry_run: bool = False):
    print("\n" + "=" * 72)
    print("  阶段B: DB 重算 (连板/ST/衍生因子/指数基准)")
    print("=" * 72)
    _recompute_st(engine, dry_run)            # 先算 ST (连板依赖)
    _recompute_limit_up(engine, dry_run)      # 再算连板 (依赖 is_st 阈值)
    _recompute_derived_factors(engine, dry_run)
    _recompute_indices(engine, dry_run)


# ─────────────────────────────────────────────────────────────────────────
# Phase C: QuantDB 前向填充 roe 等季报数据
# ─────────────────────────────────────────────────────────────────────────
def phase_quantdb(engine, dry_run: bool = False) -> int:
    """阶段C: QuantDB 批量回填历史基本面 (保留负值) + 前向填充."""
    # 复用 backfill_sdl_fundamentals 的 QuantDB 读取助手 (估值 bulk + roe/industry)
    try:
        from backend.scripts.backfill_sdl_fundamentals import (
            _quantdb_industry_map,
            _quantdb_roe_map,
            _quantdb_valuation_frame,
        )
    except Exception:
        # 兜底：以内联方式重建（防止跨模块导入失败时完全不可用）
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub as _Hub

        def _quantdb_valuation_frame(start, end):
            hub = _Hub.get_instance()
            if not hub.available:
                return pd.DataFrame()
            df = hub.fetch_valuation(start=start, end=end)
            if df.empty:
                return df
            sym_col = next((c for c in ("symbol", "Symbol") if c in df.columns), None)
            date_col = next((c for c in ("time", "trade_date") if c in df.columns), None)
            if not sym_col or not date_col:
                return pd.DataFrame()
            return df.rename(columns={sym_col: "symbol", date_col: "trade_date"})

        def _quantdb_roe_map(symbols):
            hub = _Hub.get_instance()
            out = {}
            for sym in symbols:
                try:
                    f = hub.fetch_financial(sym, "pershare_index")
                    for col in ("net_roe", "total_roe", "equity_roe", "roe_dupont"):
                        if col in f.columns:
                            vals = pd.to_numeric(f[col], errors="coerce").dropna()
                            if not vals.empty:
                                out[sym] = float(vals.iloc[-1])
                                break
                except Exception:
                    continue
            return out

        def _quantdb_industry_map():
            try:
                hub = _Hub.get_instance()
                ind = hub.fetch_instrument_industry()
            except Exception:
                return {}
            if ind is None or ind.empty or "symbol" not in ind.columns or "ind_name_l1" not in ind.columns:
                return {}
            return dict(zip(ind["symbol"], ind["ind_name_l1"], strict=False))

    print("\n" + "=" * 72)
    print("  阶段C: QuantDB 回填 + 前向填充 (roe 等, 保留负值)")
    print("=" * 72)

    updated = 0
    with engine.connect() as conn:
        r = conn.execute(text("SELECT MIN(trade_date) FROM stock_daily_latest WHERE volume>0")).one()
    db_min = r[0]
    if db_min is None:
        print("   ℹ️  stock_daily_latest 为空，跳过（无可用 DB 日期范围）")
    else:
        db_min = pd.Timestamp(db_min).date()
        print(f"\n1️⃣  读取 QuantDB valuation: {db_min} → {PARQUET_CUTOFF}")
        df = _quantdb_valuation_frame(db_min, PARQUET_CUTOFF)
        if df.empty:
            print("   ❌ QuantDB valuation 无数据")
        else:
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            symbols = sorted(df["symbol"].unique().tolist())
            roe_map = _quantdb_roe_map(symbols)
            ind_map = _quantdb_industry_map()
            df["roe"] = df["symbol"].map(roe_map)
            df["industry"] = df["symbol"].map(ind_map)
            for c in ["pe_ttm", "pb", "roe", "total_mv", "float_mv"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df["industry"] = df["industry"].fillna("").astype(str).replace("nan", "")

            if dry_run:
                print(f"     [DRY RUN] QuantDB 将回填 {len(df):,} 行 (保留负值)")
            elif not df.empty:
                print(f"     写入临时表 {len(df):,} 行...")
                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE IF EXISTS tmp_fund_backfill"))
                df.to_sql("tmp_fund_backfill", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
                with engine.begin() as conn:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS tmp_fb_sym_dt ON tmp_fund_backfill(symbol, trade_date)"))
                    sets = []
                    for c in FUND_COLS:
                        if c == "industry":
                            sets.append("industry = COALESCE(NULLIF(s.industry, ''), t.industry, s.industry)")
                        else:
                            # 关键修复: 只跳过 0/NULL, 保留 QuantDB 的负值 (亏损股真实 PE/ROE)
                            sets.append(f"{c} = COALESCE(NULLIF(s.{c}, 0), t.{c}, s.{c})")
                    result = conn.execute(text(f"""
                        UPDATE stock_daily_latest s
                        SET {', '.join(sets)}
                        FROM tmp_fund_backfill t
                        WHERE s.symbol = t.symbol AND s.trade_date = t.trade_date AND s.volume > 0
                    """))
                    updated = result.rowcount
                    conn.execute(text("DROP TABLE IF EXISTS tmp_fund_backfill"))
                print(f"     ✅ QuantDB 回填: {updated:,} 行 (负值已保留)")

    # 前向填充剩余缺口 (roe 季报报告期内不变, 前向填充合理)
    if not dry_run:
        print("\n2️⃣  前向填充剩余缺口 (roe/pe/pb/mv)...")
        with engine.begin() as conn:
            for col in ["roe", "pe_ttm", "pb", "total_mv", "float_mv"]:
                result = conn.execute(text(f"""
                    UPDATE stock_daily_latest t
                    SET {col} = src.{col}
                    FROM (
                        SELECT DISTINCT ON (symbol) symbol, trade_date, {col}
                        FROM stock_daily_latest
                        WHERE {col} IS NOT NULL AND {col} <> 0
                        ORDER BY symbol, trade_date DESC
                    ) src
                    WHERE t.symbol = src.symbol
                      AND (t.{col} IS NULL OR t.{col} = 0)
                """))
                print(f"     ✅ {col:12s}: {result.rowcount:,} 行前向填充")
    return updated


# ─────────────────────────────────────────────────────────────────────────
# Phase D: 可行补充列 (东财本机被封时的备选源: 前缀派生 / baostock / mootdx)
#   listing_market  ← symbol 前缀派生 (主板/创业板/科创板/北交所/B股), 即时
#   idx_hs300       ← baostock query_hs300_stocks (无封禁), 即时
#   listed_days     ← mootdx finance.ipo_date 逐股 (TCP不封IP, ~5-9min)
#   idx_zz1000/chinext/margin/资金流/inst_ownership/profit_growth: 本机无源或东财被封, 不在此阶段
# ─────────────────────────────────────────────────────────────────────────
def _fill_listing_market(engine, dry_run: bool = False) -> int:
    """listing_market 板块: 从 symbol 前缀派生 (确定性, 无需外部源)."""
    print("\n  [D1] listing_market 代码前缀派生...")
    if dry_run:
        print("     [DRY RUN] 将按代码前缀填板块")
        return 0
    updated = 0
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE stock_daily_latest
            SET listing_market = CASE
                WHEN split_part(symbol,'.',2) = 'BJ' THEN '北交所'
                WHEN split_part(symbol,'.',2) = 'SH' AND split_part(symbol,'.',1) LIKE '688%' THEN '科创板'
                WHEN split_part(symbol,'.',2) = 'SH' AND split_part(symbol,'.',1) LIKE '689%' THEN '科创板'
                WHEN split_part(symbol,'.',2) = 'SH' AND split_part(symbol,'.',1) LIKE '900%' THEN '沪市B股'
                WHEN split_part(symbol,'.',2) = 'SH' THEN '沪市主板'
                WHEN split_part(symbol,'.',2) = 'SZ' AND split_part(symbol,'.',1) LIKE '300%' THEN '创业板'
                WHEN split_part(symbol,'.',2) = 'SZ' AND split_part(symbol,'.',1) LIKE '301%' THEN '创业板'
                WHEN split_part(symbol,'.',2) = 'SZ' AND split_part(symbol,'.',1) LIKE '200%' THEN '深市B股'
                WHEN split_part(symbol,'.',2) = 'SZ' THEN '深市主板'
                ELSE listing_market
            END
            WHERE volume > 0
        """))
        updated = result.rowcount
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT listing_market, COUNT(DISTINCT symbol) FROM stock_daily_latest "
            "WHERE volume>0 GROUP BY listing_market ORDER BY 2 DESC"
        )).fetchall()
    print(f"     ✅ listing_market: {updated:,} 行已填板块")
    print("     板块分布(股票数): " + ", ".join(f"{r[0] or '空'}={r[1]}" for r in rows))
    return updated


def _fill_idx_hs300(engine, dry_run: bool = False) -> int:
    """idx_hs300 沪深300成分: baostock query_hs300_stocks (无封禁). 用当前成分填全日期行."""
    print("\n  [D2] idx_hs300 沪深300成分 (baostock)...")
    if dry_run:
        print("     [DRY RUN] 将用 baostock 成分股列表标 idx_hs300")
        return 0
    import baostock as bs
    from datetime import timedelta
    bs.login()
    with engine.connect() as conn:
        d = conn.execute(text("SELECT MAX(trade_date) FROM stock_daily_latest WHERE volume>0")).scalar()
    target = d
    rows = []
    for _ in range(6):  # baostock 当日可能未更新, 最多回退 5 天
        rs = bs.query_hs300_stocks(date=target.strftime("%Y-%m-%d"))
        if rs.error_code == "0":
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                break
        target = target - timedelta(days=1)
    bs.logout()
    if not rows:
        print("     ❌ baostock 未能取到沪深300成分")
        return 0
    # baostock code "sh.600519" -> "600519.SH"
    constituents = set()
    for r in rows:
        code = r[1]
        if "." in code:
            mk, num = code.split(".")
            constituents.add(f"{num}.{mk.upper()}")
    print(f"     沪深300成分: {len(constituents)} 只 (截至 {target})")
    df = pd.DataFrame({"symbol": sorted(constituents)})
    df["is_hs300"] = 1
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmp_hs300"))
    df.to_sql("tmp_hs300", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
    with engine.begin() as conn:
        conn.execute(text("UPDATE stock_daily_latest SET idx_hs300 = 0 WHERE volume > 0"))
        conn.execute(text("""
            UPDATE stock_daily_latest s
            SET idx_hs300 = 1
            FROM tmp_hs300 t
            WHERE UPPER(RIGHT(t.symbol, 2)) || LEFT(t.symbol, 6) = UPPER(s.symbol)
              AND s.volume > 0
        """))
        cnt = conn.execute(text(
            "SELECT COUNT(DISTINCT symbol) FROM stock_daily_latest WHERE idx_hs300 = 1"
        )).scalar()
        conn.execute(text("DROP TABLE IF EXISTS tmp_hs300"))
    print(f"     ✅ idx_hs300=1: {cnt} 只成分股 (跨全部日期行)")
    return cnt


def _fill_idx_zz500(engine, dry_run: bool = False) -> int:
    """idx_zz500 中证500成分: baostock query_zz500_stocks (无封禁). 用当前成分填全日期行."""
    print("\n  [D2b] idx_zz500 中证500成分 (baostock)...")
    if dry_run:
        print("     [DRY RUN] 将用 baostock 成分股列表标 idx_zz500")
        return 0
    import baostock as bs
    from datetime import timedelta
    bs.login()
    with engine.connect() as conn:
        d = conn.execute(text("SELECT MAX(trade_date) FROM stock_daily_latest WHERE volume>0")).scalar()
    target = d
    rows = []
    for _ in range(6):
        rs = bs.query_zz500_stocks(date=target.strftime("%Y-%m-%d"))
        if rs.error_code == "0":
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                break
        target = target - timedelta(days=1)
    bs.logout()
    if not rows:
        print("     ❌ baostock 未能取到中证500成分")
        return 0
    constituents = set()
    for r in rows:
        code = r[1]
        if "." in code:
            mk, num = code.split(".")
            constituents.add(f"{num}.{mk.upper()}")
    print(f"     中证500成分: {len(constituents)} 只 (截至 {target})")
    df = pd.DataFrame({"symbol": sorted(constituents)})
    df["is_zz500"] = 1
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmp_zz500"))
    df.to_sql("tmp_zz500", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
    with engine.begin() as conn:
        conn.execute(text("UPDATE stock_daily_latest SET idx_zz500 = 0 WHERE volume > 0"))
        conn.execute(text("""
            UPDATE stock_daily_latest s
            SET idx_zz500 = 1
            FROM tmp_zz500 t
            WHERE UPPER(RIGHT(t.symbol, 2)) || LEFT(t.symbol, 6) = UPPER(s.symbol)
              AND s.volume > 0
        """))
        cnt = conn.execute(text(
            "SELECT COUNT(DISTINCT symbol) FROM stock_daily_latest WHERE idx_zz500 = 1"
        )).scalar()
        conn.execute(text("DROP TABLE IF EXISTS tmp_zz500"))
    print(f"     ✅ idx_zz500=1: {cnt} 只成分股 (跨全部日期行)")
    return cnt


def _fill_idx_zz1000(engine, dry_run: bool = False) -> int:
    """idx_zz1000 中证1000成分: akshare 中证官方成分表 (baostock 无此接口)."""
    print("\n  [D2c] idx_zz1000 中证1000成分 (akshare)...")
    if dry_run:
        print("     [DRY RUN] 将用 akshare 中证1000成分股列表标 idx_zz1000")
        return 0
    import akshare as ak

    df_cons = ak.index_stock_cons_csindex(symbol="000852")
    if df_cons is None or df_cons.empty:
        print("     ❌ akshare 未能取到中证1000成分")
        return 0

    def _to_prefix(code: str, exchange: str) -> str:
        if "深圳" in exchange:
            return f"SZ{code}"
        if "上海" in exchange or "上证" in exchange:
            return f"SH{code}"
        if "北京" in exchange:
            return f"BJ{code}"
        if code[:1] in ("6", "9"):
            return f"SH{code}"
        if code[:1] in ("4", "8"):
            return f"BJ{code}"
        return f"SZ{code}"

    codes = df_cons["成分券代码"].astype(str).str.zfill(6)
    constituents = {
        _to_prefix(c, e) for c, e in zip(codes, df_cons["交易所"].astype(str))
    }
    print(f"     中证1000成分: {len(constituents)} 只")
    df = pd.DataFrame({"symbol": sorted(constituents)})
    df["is_zz1000"] = 1
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmp_zz1000"))
    df.to_sql("tmp_zz1000", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
    with engine.begin() as conn:
        conn.execute(text("UPDATE stock_daily_latest SET idx_zz1000 = 0 WHERE volume > 0"))
        conn.execute(text("""
            UPDATE stock_daily_latest s
            SET idx_zz1000 = 1
            FROM tmp_zz1000 t
            WHERE UPPER(t.symbol) = UPPER(s.symbol) AND s.volume > 0
        """))
        cnt = conn.execute(text(
            "SELECT COUNT(DISTINCT symbol) FROM stock_daily_latest WHERE idx_zz1000 = 1"
        )).scalar()
        conn.execute(text("DROP TABLE IF EXISTS tmp_zz1000"))
    print(f"     ✅ idx_zz1000=1: {cnt} 只成分股 (跨全部日期行)")
    return cnt


def _fill_listed_days(engine, dry_run: bool = False) -> int:
    """listed_days 上市天数: mootdx finance.ipo_date 逐股 (TCP不封IP)."""
    print("\n  [D3] listed_days 上市天数 (mootdx finance.ipo_date, 逐股)...")
    from mootdx.quotes import Quotes
    with engine.connect() as conn:
        syms = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT symbol FROM stock_daily_latest WHERE volume>0 ORDER BY symbol"
        )).fetchall()]
    print(f"     {len(syms)} 只股票, 逐股拉 mootdx ipo_date (TCP, 不封IP)...")
    # 实测可达的通达信服务器 (skill tdx_client 列表)
    _TDX = [("119.97.185.59", 7709), ("124.70.133.119", 7709),
            ("116.205.183.150", 7709), ("123.60.73.44", 7709)]
    client = None
    for ip, p in _TDX:
        try:
            client = Quotes.factory(market="std", server=(ip, p))
            break
        except Exception:
            continue
    if client is None:
        print("     ❌ mootdx 服务器均不可达")
        return 0
    ipo_rows, t0 = [], time.time()
    for i, sym in enumerate(syms):
        code = sym.split(".")[0]
        try:
            fin = client.finance(symbol=code)
            d = fin.iloc[0].to_dict() if hasattr(fin, "iloc") else dict(fin)
            ipo = d.get("ipo_date")
            if ipo:
                ipo_rows.append({"symbol": sym, "ipo_date_str": str(int(ipo))})
        except Exception:
            pass
        if (i + 1) % 500 == 0:
            print(f"     已拉 {i+1}/{len(syms)} ({time.time()-t0:.1f}s, 成功 {len(ipo_rows)})")
        time.sleep(0.02)  # 轻微节流, mootdx 不封IP 但更稳
    print(f"     ✅ ipo_date 拉取完成: {len(ipo_rows)}/{len(syms)} ({time.time()-t0:.1f}s)")
    if not ipo_rows:
        return 0
    if dry_run:
        print("     [DRY RUN] 将用 ipo_date 算 listed_days = trade_date - ipo_date")
        return 0
    df = pd.DataFrame(ipo_rows)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmp_ipo"))
    df.to_sql("tmp_ipo", engine, if_exists="replace", index=False, method="multi", chunksize=5000)
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS tmp_ipo_sym ON tmp_ipo(symbol)"))
        result = conn.execute(text("""
            UPDATE stock_daily_latest s
            SET listed_days = (s.trade_date::date - to_date(t.ipo_date_str, 'YYYYMMDD'))
            FROM tmp_ipo t
            WHERE s.symbol = t.symbol AND s.volume > 0
        """))
        updated = result.rowcount
        conn.execute(text("DROP TABLE IF EXISTS tmp_ipo"))
    print(f"     ✅ listed_days: {updated:,} 行已填 (上市天数 = 交易日 - IPO日)")
    return updated


def phase_extras(engine, dry_run: bool = False):
    print("\n" + "=" * 72)
    print("  阶段D: 可行补充列 (东财被封备选源: 前缀/baostock/mootdx)")
    print("=" * 72)
    _fill_listing_market(engine, dry_run)
    _fill_idx_hs300(engine, dry_run)
    _fill_idx_zz500(engine, dry_run)
    _fill_idx_zz1000(engine, dry_run)
    _fill_listed_days(engine, dry_run)


def main():
    parser = argparse.ArgumentParser(description="完善 stock_daily_latest (集成 a-stock-data skill)")
    parser.add_argument("--diagnose", action="store_true", help="仅诊断")
    parser.add_argument("--phase-tencent", action="store_true", help="阶段A: 腾讯补 PE/PB/市值/换手率")
    parser.add_argument("--phase-recompute", action="store_true", help="阶段B: 重算连板/ST/衍生因子")
    parser.add_argument("--phase-quantdb", action="store_true", help="阶段C: QuantDB+前向填充 roe")
    parser.add_argument("--phase-extras", action="store_true",
                        help="阶段D: 可行补充列 (listing_market/idx_hs300/listed_days, 东财被封备选源)")
    parser.add_argument("--all", action="store_true", help="全部 (A→C→B, 腾讯优先补最新)")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    args = parser.parse_args()

    engine = _get_engine()
    diagnose(engine)
    if args.diagnose:
        return
    if not any([args.phase_tencent, args.phase_recompute, args.phase_quantdb,
                args.phase_extras, args.all]):
        return

    dry = args.dry_run
    # 执行顺序: 先 QuantDB(历史权威值) -> 腾讯(补最新+负值) -> 重算(衍生因子依赖pe/pb)
    if args.all or args.phase_quantdb:
        phase_quantdb(engine, dry_run=dry)
    if args.all or args.phase_tencent:
        phase_tencent(engine, dry_run=dry)
    if args.all or args.phase_recompute:
        phase_recompute(engine, dry_run=dry)
    if args.phase_extras:
        phase_extras(engine, dry_run=dry)

    if not dry:
        print("\n" + "=" * 72)
        print("  完善后验证")
        print("=" * 72)
        diagnose(engine)


if __name__ == "__main__":
    main()
