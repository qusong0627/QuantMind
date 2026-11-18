#!/usr/bin/env python3
"""P0 影子：从已验证事件生成 news_decisions.json，不接交易。

用法：
  python scripts/news_shadow_decisions.py --date 20261118
  NEWS_INTEL_BASE=/home/zbox/projects/quantmind/data/news_intel/20261118 \
    python scripts/news_shadow_decisions.py --date 20261118

只落盘，不写 Redis、不告警、不改 latest.json。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.shared.news_decision import decide, to_envelope, validate_decision  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
OUT = ROOT / "data" / "news_brief" / "news_decisions.json"


def _iso(value) -> str:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return datetime.now(CN_TZ).isoformat(timespec="seconds")
    if pd.isna(ts):
        return datetime.now(CN_TZ).isoformat(timespec="seconds")
    if ts.tzinfo is None:
        ts = ts.tz_localize(CN_TZ)
    else:
        ts = ts.tz_convert(CN_TZ)
    return ts.isoformat(timespec="seconds")


def _num(value, default=0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--events", default="", help="stock_event_validated_v4.parquet")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    base = Path(os.environ.get("NEWS_INTEL_BASE", "/data/news_intel/20261118"))
    events_path = Path(a.events) if a.events else base / "out" / "stock_event_validated_v4.parquet"
    if not events_path.is_file():
        raise SystemExit(f"events not found: {events_path}")

    df = pd.read_parquet(events_path)
    if "valid" in df.columns:
        df = df[df.valid.fillna(False)]
    day = pd.to_datetime(df["event_date"]).dt.strftime("%Y%m%d")
    df = df[day == a.date].copy()
    if df.empty:
        raise SystemExit(f"no events for {a.date}")

    items = []
    for row in df.itertuples(index=False):
        first_seen = getattr(row, "published_local", None)
        as_of = first_seen
        event = {
            "decision_id": f"{a.date}-{row.ticker}-{row.event_id}",
            "code": str(row.ticker),
            "name": "",
            "as_of": _iso(as_of),
            "first_seen": _iso(first_seen),
            "expire_at": (_iso(as_of) and (
                datetime.fromisoformat(_iso(as_of)) + timedelta(days=1)
            ).isoformat(timespec="seconds")),
            "tier": str(getattr(row, "match_tier", "") or "L2"),
            "event_type": str(getattr(row, "event_type_v", "") or ""),
            "category": str(getattr(row, "category", "") or ""),
            "direction": str(getattr(row, "event_dir", "") or ""),
            "pre5": getattr(row, "pre5", None),
            "session": str(getattr(row, "session", "") or ""),
            "cluster_size": 1,
            "layer": 0,
            "echo_count": 0,
            "source": str(getattr(row, "connector_name", "") or ""),
            "source_tier": "",
            "source_quality": None,
            "n_speculative": int(_num(getattr(row, "n_speculative", 0))),
            "n_articles": int(_num(getattr(row, "n_articles", 0))),
            "n_sources": int(_num(getattr(row, "n_sources", 0))),
            "source_hhi": getattr(row, "source_hhi", None),
            "stale": False,
        }
        d = decide(event)
        validate_decision(d.to_dict())
        items.append(d)

    env = to_envelope(
        items,
        as_of=datetime.now(CN_TZ).isoformat(timespec="seconds"),
        trade_date=a.date,
        market_risk={"source": "shadow", "n_items": len(items)},
    )
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)

    counts: dict[str, int] = {}
    for d in items:
        counts[d.decision.value] = counts.get(d.decision.value, 0) + 1
    print(f"wrote {out} items={len(items)} counts={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
