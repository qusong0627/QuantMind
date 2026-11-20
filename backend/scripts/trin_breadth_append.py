#!/usr/bin/env python3
"""trin_daily 追加式刷新（只补新交易日，历史行逐值不动）。

背景：``trin_breadth.py`` 是整表重建——它会用当前 daily_backward 重算全部历史，
一旦上游复权/成交量被重新回灌，历史 TRIN/广度数值就会整体位移。而 trin_daily
自 2026-10-28 起冻结时没有任何调度接管，本次修复把它接入盘后链，因此采用
「重建到临时文件 → 只取比现有最大日期更新的行 → 原子替换」的追加口径：
历史段保持既有 vintage，口径不漂移。

用法：``python -m backend.scripts.trin_breadth_append``（幂等；无新日期直接退 0）
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

log = logging.getLogger("trin_breadth_append")

QUANTDB_DIR = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
BREADTH = QUANTDB_DIR / "5_technical_derived" / "market_breadth"
PANEL = BREADTH / "trin_daily.parquet"
BACKWARD = QUANTDB_DIR / "1_kline_data" / "daily_backward"


def _latest_partition() -> str | None:
    if not BACKWARD.is_dir():
        return None
    days = sorted(p.name.split("=", 1)[1] for p in BACKWARD.glob("dt=*") if p.name[3:].isdigit())
    return days[-1] if days else None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    latest = _latest_partition()
    if latest is None:
        log.error("找不到 daily_backward 分区")
        return 1

    if PANEL.is_file():
        old = pd.read_parquet(PANEL, columns=["date"])
        have_max = str(pd.Timestamp(old["date"].max()).date())
        have_min = str(pd.Timestamp(old["date"].min()).date())
    else:
        old, have_max, have_min = None, "1970-01-01", "2016-01-04"
    iso_latest = f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"
    if have_max >= iso_latest:
        log.info("已是最新（%s），无需刷新", have_max)
        return 0

    root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "trin_daily.rebuild.parquet"
        proc = subprocess.run(
            [sys.executable, "-m", "backend.scripts.trin_breadth",
             "--start", have_min, "--out", str(tmp)],
            cwd=str(root), capture_output=True, text=True, timeout=3600,
        )
        if proc.returncode != 0:
            log.error("trin_breadth 失败 code=%d stderr=%s", proc.returncode, proc.stderr[-1500:])
            return proc.returncode
        fresh = pd.read_parquet(tmp)
        if old is not None:
            add = fresh[fresh["date"] > pd.Timestamp(have_max)]
        else:
            add = fresh
        log.info("重建 %d 行，其中新增 %d 行（%s → %s）", len(fresh), len(add), have_max, iso_latest)
        if add.empty:
            log.info("无新增交易日")
            return 0
        final = add.copy() if old is None else pd.concat(
            [pd.read_parquet(PANEL), add], ignore_index=True
        )
        out_tmp = PANEL.with_suffix(".parquet.append.tmp")
        final.to_parquet(out_tmp, index=False)
        os.replace(out_tmp, PANEL)
        log.info("写出 %s：%d 行，%s → %s", PANEL, len(final),
                 pd.Timestamp(final["date"].min()).date(),
                 pd.Timestamp(final["date"].max()).date())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
