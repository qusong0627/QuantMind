#!/usr/bin/env python3
"""P3 新闻决策审计：按 decision_id 看 T+1/T+5 实际收益。

用法：
  python scripts/news_decision_audit.py --date 20261110
  python scripts/news_decision_audit.py --date 20261110 --decisions data/news_brief/news_decisions.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "data" / "news_brief"
DEFAULT_DECISIONS = NB / "news_decisions.json"
DEFAULT_OUT = NB / "decision_audit"
QUANTDB = Path(os.getenv("QM_QUANTDB_DATA_DIR") or "/data/quantdb")


def _load_items(path: Path, day: str) -> list[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        x for x in (doc.get("items") or [])
        if isinstance(x, dict) and str(x.get("decision_id") or "").startswith(day + "-")
    ]


def _daily_codes(codes: list[str], day: str) -> dict[str, list[dict]]:
    if not codes:
        return {}
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT symbol, dt, close
            FROM read_parquet('{QUANTDB}/1_kline_data/daily_unadjusted/dt=*/data.parquet')
            WHERE dt >= {int(day)} AND symbol IN ({','.join("?" * len(codes))})
            ORDER BY symbol, dt
            """,
            codes,
        ).fetchall()
    finally:
        con.close()
    out: dict[str, list[dict]] = {}
    for sym, dt, close in rows:
        out.setdefault(str(sym), []).append({"dt": int(dt), "close": float(close)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--decisions", default=str(DEFAULT_DECISIONS))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    a = ap.parse_args()

    items = _load_items(Path(a.decisions), a.date)
    if not items:
        print(f"no decisions for {a.date}")
        return 1
    codes = sorted({str(x.get("code")) for x in items if x.get("code")})
    bars = _daily_codes(codes, a.date)

    rows = []
    for x in items:
        series = bars.get(str(x.get("code"))) or []
        base = None
        for b in series:
            if b["dt"] >= int(a.date):
                base = b
                break
        if base is None:
            rows.append({**x, "ret1": None, "ret5": None})
            continue
        after = [b for b in series if b["dt"] > base["dt"]]
        ret1 = (after[0]["close"] / base["close"] - 1) if after else None
        ret5 = (after[4]["close"] / base["close"] - 1) if len(after) >= 5 else None
        rows.append({**x, "ret1": ret1, "ret5": ret5})

    df = pd.DataFrame(rows)
    summary = []
    for dec, g in df.groupby("decision"):
        ret1 = pd.to_numeric(g["ret1"], errors="coerce").dropna()
        ret5 = pd.to_numeric(g["ret5"], errors="coerce").dropna()
        summary.append({
            "decision": dec,
            "n": len(g),
            "n_ret1": len(ret1),
            "ret1_bp": round(float(ret1.mean()) * 10000, 1) if len(ret1) else None,
            "n_ret5": len(ret5),
            "ret5_bp": round(float(ret5.mean()) * 10000, 1) if len(ret5) else None,
            "win5": round(float((ret5 > 0).mean()) * 100, 1) if len(ret5) else None,
        })
    out = {
        "date": a.date,
        "as_of": pd.Timestamp.now().isoformat(),
        "summary": summary,
        "items": df.to_dict(orient="records"),
    }
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{a.date}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(pd.DataFrame(summary).to_string(index=False))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
