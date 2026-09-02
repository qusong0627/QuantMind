#!/usr/bin/env python3
"""南向持股历史回填：旧「每股一文件」布局 → dt= 分区式主布局。

背景：quanthk 的 hsgt_south 目录存在两套布局——
- 主布局 `dt=YYYYMMDD/data.parquet`（quanthk_south_sync.py 每晚同步写入，2025-12-29 起）
- 旧布局 `{symbol}.HK.parquet`（quanthk_import_south.py 从外部 data-hs CSV 导入，
  覆盖 2024-11-27 ~ 2025-12-19，2025-12 起停更）

本脚本把旧布局的历史记录回填进 dt= 分区式布局，使南向时间线从 2024-11-27 起连续。
幂等：已存在的 dt 分区目录跳过；跳过损坏 parquet（如 2228/2582）并告警。

用法:
  python backend/scripts/quanthk_south_history_merge.py --dry-run
  python backend/scripts/quanthk_south_history_merge.py            # 实际执行
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quanthk_south_history_merge")

QUANTHK_DATA_DIR = Path(
    os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk"))
)
SOUTH_REL = QUANTHK_DATA_DIR / "2_base_sector" / "hsgt_south"
LEGACY_GLOB = "*.HK.parquet"

# 旧布局最新记录（2025-12-19）应早于主布局最早分区（2025-12-29），
# 两区间之间不应重叠；该常量仅用于日志校验，不回退主布局任一分区
EXPECTED_BOUNDARY = "20251229"


def _legacy_files() -> tuple[list[str], list[str]]:
    """返回 (可读旧布局文件, 损坏文件)。"""
    good, bad = [], []
    con = duckdb.connect()
    try:
        for f in sorted(glob.glob(str(SOUTH_REL / LEGACY_GLOB))):
            try:
                con.execute(f"SELECT * FROM read_parquet('{f}') LIMIT 0")
                good.append(f)
            except Exception:
                log.warning("跳过损坏旧布局文件: %s", os.path.basename(f))
                bad.append(f)
    finally:
        con.close()
    return good, bad


def _load_legacy(files: list[str]) -> pd.DataFrame:
    """读取全部旧布局文件（按文件数组读，schema 统一）。"""
    files_literal = "[" + ",".join(f"'{f}'" for f in files) + "]"
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT symbol, query_date, holding_quantity, holding_percentage "
            f"FROM read_parquet({files_literal}, union_by_name=true)"
        ).fetchdf()
    finally:
        con.close()


def _existing_partitions() -> set[str]:
    return {d[3:] for d in os.listdir(SOUTH_REL) if d.startswith("dt=")}


def _merge_rows(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """把旧布局记录按 query_date 分组为分区二维表，仅保留晚于主布局最早分区的日期。"""
    df = df.copy()
    df["query_date"] = df["query_date"].astype(str)
    # 只处理主布局区间之前的记录（幂等 + 不回退主布局）
    hist = df[df["query_date"] < "2025-12-29"].copy()
    # 统一转换为 YYYYMMDD：query_date 可能为 date/Timestamp/str 多种形态，
    # 必须用 str accessor 的显式替换（Series.replace 对新版 pandas 的 regex
    # 语义不一致，曾产出 dt=2024-11-27 这类非法分区名）
    hist["dt"] = hist["query_date"].str.slice(0, 10).str.replace("-", "", regex=False)
    groups: dict[str, pd.DataFrame] = {}
    for dt, g in hist.groupby("dt", sort=True):
        groups[dt] = g
    return groups


def _stock_names() -> dict[str, str]:
    """security_master 中文名映射（补 stock_name 列，与主布局 schema 对齐）。"""
    try:
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT symbol, cn_name FROM read_parquet("
                f"'{QUANTHK_DATA_DIR}/2_base_sector/security_master/data.parquet')"
            ).fetchall()
            return {str(s): cn for s, cn in rows if cn}
        finally:
            con.close()
    except Exception as exc:  # 名称表缺失不影响主数据回填
        log.warning("security_master 读取失败，stock_name 留空: %s", exc)
        return {}


def merge(*, dry_run: bool = False) -> dict:
    """执行回填。返回统计（写入分区数/行数/跳过）。"""
    good, bad = _legacy_files()
    if not good:
        return {"status": "no_source", "corrupt": [os.path.basename(b) for b in bad]}

    raw = _load_legacy(good)
    if raw.empty:
        return {"status": "no_data", "files": len(good)}

    groups = _merge_rows(raw)
    existing = _existing_partitions()
    names = _stock_names()

    todo = [dt for dt in sorted(groups) if dt not in existing]
    skipped = [dt for dt in sorted(groups) if dt in existing]
    total_rows = 0
    for dt in todo:
        total_rows += len(groups[dt])

    if dry_run:
        return {
            "status": "dry_run",
            "history_from": list(groups)[0],
            "history_to": list(groups)[-1],
            "partitions_to_write": len(todo),
            "rows_to_write": total_rows,
            "partitions_skipped": len(skipped),
            "corrupt": [os.path.basename(b) for b in bad],
        }

    for dt in todo:
        g = groups[dt]
        if names:
            g = g.assign(stock_name=g["symbol"].map(names).fillna(""))
        else:
            g = g.assign(stock_name="")
        out = SOUTH_REL / f"dt={dt}"
        out.mkdir(parents=True, exist_ok=True)
        g[
            [
                "symbol",
                "stock_name",
                "holding_quantity",
                "holding_percentage",
                "query_date",
            ]
        ].to_parquet(out / "data.parquet", index=False)
    log.info(
        "回填完成：%d 个分区，%d 行；跳过已存在 %d", len(todo), total_rows, len(skipped)
    )
    return {
        "status": "done",
        "history_from": list(groups)[0],
        "history_to": list(groups)[-1],
        "partitions_written": len(todo),
        "rows_written": total_rows,
        "partitions_skipped": len(skipped),
        "corrupt": [os.path.basename(b) for b in bad],
        "newest_expected": EXPECTED_BOUNDARY,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="南向历史回填（旧每股文件 → dt= 分区）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()
    result = merge(dry_run=args.dry_run)
    log.info("结果: %s", result)
    return (
        0 if result.get("status") in ("done", "no_source", "no_data", "dry_run") else 1
    )


if __name__ == "__main__":
    sys.exit(main())
