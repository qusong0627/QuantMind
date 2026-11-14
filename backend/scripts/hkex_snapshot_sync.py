#!/usr/bin/env python3
"""港股数据补充：每日快照采集（当前可得，历史段付费/不可回补）。

采集：
1. HKEX 主板每日沽空成交（ASHTMAIN 页面；仅最新交易日，盘后才有数据）；
2. HKEX 北向合资格证券（SSE/SZSE 当前 CSV）；
3. HSI/HSCEI/HSTECH 当前 factsheet PDF（月度覆盖，向前积累）。

落盘：
  data/quanthk/2_base_sector/short_selling/dt=YYYYMMDD/data.parquet
  data/quanthk/2_base_sector/stock_connect_eligible/dt=YYYYMMDD/*.parquet
  data/quanthk/2_base_sector/index_factsheets/YYYYMMDD/*.pdf

用法：
  python backend/scripts/hkex_snapshot_sync.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

ROOT = Path(
    os.getenv(
        "QM_QUANTHK_DATA_DIR",
        "/data/quanthk" if Path("/data/quanthk").is_dir() else str(PROJECT_ROOT / "data" / "quanthk"),
    )
)
UA = {"User-Agent": "Mozilla/5.0"}


def _get(url: str, timeout: int = 30) -> requests.Response:
    r = requests.get(url, timeout=timeout, headers=UA)
    r.raise_for_status()
    return r


def sync_short_selling() -> dict:
    url = "https://www.hkex.com.hk/eng/stat/smstat/ssturnover/ncms/ASHTMAIN.HTM"
    text = _get(url).text
    pre = re.search(r"<pre>(.*?)</pre>", text, re.S | re.I)
    if not pre:
        return {"status": "no_pre"}
    body = re.sub(r"<[^>]+>", "", pre.group(1))
    m = re.search(r"TRADING DATE\s*[:：]?\s*(\d{1,2}\s+\w{3}\s+\d{4})", body)
    if not m:
        return {"status": "no_trading_date", "note": "盘前/休市占位页"}
    dt = pd.to_datetime(m.group(1), format="%d %b %Y", errors="coerce")
    if pd.isna(dt):
        return {"status": "bad_date", "raw": m.group(1)}
    # R hkex.api 的固定列宽解析：code 0-7, name 10-30, short qty 31-41, value 42-56
    lines = body.splitlines()
    rows = []
    started = False
    for line in lines:
        if "TRADING DATE" in line:
            started = True
            continue
        if not started:
            continue
        code = line[:7].strip()
        if not re.fullmatch(r"\d{1,5}", code):
            continue
        rows.append(
            {
                "code": code.zfill(5),
                "name": line[10:30].strip(),
                "short_qty": line[31:41].replace(",", "").strip(),
                "short_value": line[42:57].replace(",", "").strip(),
            }
        )
    if not rows:
        return {"status": "no_rows", "date": str(dt.date())}
    d = pd.DataFrame(rows)
    for c in ("short_qty", "short_value"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["date"] = dt.normalize()
    d["symbol"] = d["code"].map(lambda x: f"{int(x):04d}.HK")
    out = ROOT / "2_base_sector" / "short_selling" / f"dt={dt.strftime('%Y%m%d')}"
    out.mkdir(parents=True, exist_ok=True)
    d.to_parquet(out / "data.parquet", index=False)
    return {"status": "ok", "date": str(dt.date()), "rows": int(len(d))}


def sync_eligible() -> dict:
    base = "https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Eligible-Stocks/View-All-Eligible-Securities"
    out = ROOT / "2_base_sector" / "stock_connect_eligible" / f"dt={date.today().strftime('%Y%m%d')}"
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    for mkt in ("SSE", "SZSE"):
        url = f"{base}/{mkt}_Securities.csv"
        try:
            r = _get(url)
            path = out / f"{mkt}.csv"
            path.write_bytes(r.content)
            # HKEX 文件是 UTF-16 + TAB 分隔，前 4 行是标题
            df = pd.read_csv(path, encoding="utf-16", sep="\t", skiprows=4)
            df.to_parquet(out / f"{mkt}.parquet", index=False)
            res[mkt] = {"status": "ok", "rows": int(len(df)), "cols": list(df.columns)[:8]}
        except Exception as exc:  # noqa: BLE001
            res[mkt] = {"status": "error", "error": str(exc)}
    return res


def sync_factsheets() -> dict:
    """HSI 官方 factsheet 链接已改版，404；不再盲抓，留待人工补 URL/付费源。"""
    return {
        "status": "blocked",
        "note": "HSI factsheet 公开 URL 已 404；历史成分需 HSI Services/AASTOCKS/Wind 或 Wayback，当前未接入",
    }


def main() -> int:
    result = {
        "date": str(date.today()),
        "short_selling": sync_short_selling(),
        "eligible": sync_eligible(),
        "factsheets": sync_factsheets(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
