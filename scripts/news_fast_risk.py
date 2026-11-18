#!/usr/bin/env python3
"""P1 新闻快速通道：每 1–2 分钟扫新增 A 股相关新闻，确定性生成 P0/P1/P2。

只写 ``data/news_brief/news_decisions.json`` + 可选 Redis/告警；不改 latest.json、
不下单。叙事通道 ``news_brief.py`` 仍负责主编分子。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.shared.news_decision import NewsDecision, validate_decision  # noqa: E402
from backend.shared.news_fast_core import build_fast_event, update_agg  # noqa: E402
from backend.shared.news_pre5 import load_pre5_map, pre5_lookup  # noqa: E402
from news_brief import (  # noqa: E402
    _name_lookup,
    fetch_incr,
    load_watch_codes,
    pick_relevant,
)

CN_TZ = timezone(timedelta(hours=8))
NB = ROOT / "data" / "news_brief"
STATE = NB / "fast_state.json"
AGG = NB / "fast_day_agg.json"
OUT = NB / "news_decisions.json"
A_SHARE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
DAY = 86400


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _suffix(ticker: str) -> str:
    raw = str(ticker or "").upper().strip()
    if A_SHARE.match(raw):
        return raw
    compact = re.sub(r"[^0-9A-Z]", "", raw)
    if compact.startswith(("SH", "SZ", "BJ")) and len(compact) >= 8:
        return f"{compact[2:8]}.{compact[:2]}"
    if len(compact) == 6 and compact.isdigit():
        if compact.startswith(("6", "9")):
            return f"{compact}.SH"
        if compact.startswith(("4", "8")):
            return f"{compact}.BJ"
        return f"{compact}.SZ"
    return ""


def _prune(seen: dict[str, float], now_s: float) -> dict[str, float]:
    return {k: v for k, v in seen.items() if now_s - v < 2 * DAY}


def _write_redis(items: list[NewsDecision]) -> None:
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        client = get_redis_sentinel_client()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ Redis 不可用，跳过：{str(exc)[:80]}")
        return
    for d in items:
        try:
            day = d.decision_id.split("-")[0]
            client.hset(
                f"qm:news:decision:{day}", d.code,
                json.dumps(d.to_dict(), ensure_ascii=False),
            )
            client.set(
                f"qm:news:stock:{d.code}:{day}",
                json.dumps(d.to_dict(), ensure_ascii=False),
                ex=2 * DAY,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ Redis 写失败 {d.code}: {str(exc)[:80]}")
            return


def _alert_p0(items: list[NewsDecision], state: dict) -> None:
    if str(os.getenv("NEWS_P0_ALERT_ENABLED", "false")).lower() not in ("true", "1"):
        return
    alerted = dict(state.get("alerted") or {})
    fresh = [d for d in items if d.decision.value == "P0" and d.decision_id not in alerted]
    if not fresh:
        return
    try:
        from push_notify import send_text
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 推送不可用：{str(exc)[:80]}")
        return
    for d in fresh[:5]:
        text = (
            f"🚨 新闻P0 {d.name}({d.code})\n"
            f"{d.event_type} · pre5 {('%+.1f%%' % (d.pre5 * 100)) if d.pre5 is not None else '—'}\n"
            f"{d.reason}"
        )
        try:
            send_text(text)
            alerted[d.decision_id] = time.time()
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ P0 推送失败 {d.code}: {str(exc)[:80]}")
            break
    state["alerted"] = alerted


def run_once(*, since: str | None, until: str | None, dry: bool) -> int:
    now = datetime.now(CN_TZ)
    state = _load(STATE)
    day = now.strftime("%Y%m%d")
    agg_doc = _load(AGG)
    if str(agg_doc.get("date") or "") != day:
        agg_doc = {"date": day, "tickers": {}}
    tickers = dict(agg_doc.get("tickers") or {})

    since_iso = since or str(state.get("last_seen") or "")
    if not since_iso:
        since_iso = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    until_iso = until or now.isoformat(timespec="seconds")

    raw, fails = fetch_incr(since_iso, until_iso)
    arts = [a for a in raw if pick_relevant(a)]
    print(f"快速通道 {since_iso[11:16]}→{until_iso[11:16]} 原始 {len(raw)} 候选 {len(arts)}"
          + (f" 桶失败 {len(fails)}" if fails else ""))

    pre5_map = load_pre5_map(day)
    names = _name_lookup()
    seen = _prune(dict(state.get("seen") or {}), now.timestamp())
    items: list[NewsDecision] = []
    for a in arts:
        pid = a.get("id")
        title = str(a.get("title") or "")
        enr = a.get("enrichment") or {}
        for raw_ticker in (enr.get("tickers") or []):
            ticker = _suffix(str(raw_ticker))
            if not ticker:
                continue
            key = f"{pid}:{ticker}"
            if key in seen:
                continue
            name = names.get(ticker) or names.get(
                ticker[:6] + "." + ticker[-2:]
            ) or ""
            cur = dict(tickers.get(ticker) or {})
            spec = bool(
                re.search(
                    r"涨停|跌停|封板|连板|首板|题材|概念|热点|炒作|妖股|龙头|打板|"
                    r"游资|龙虎榜|异动|高换手|换手率|放量|缩量|次新股|低价股|小盘股|ST",
                    title,
                )
            )
            d = build_fast_event(
                article=a,
                ticker=ticker,
                name=name,
                pre5=pre5_lookup(ticker, pre5_map),
                agg=cur,
                trade_date=day,
                now=until_iso,
            )
            tickers[ticker] = update_agg(cur, ticker, str(a.get("source_name") or ""),
                                         speculative=spec)
            seen[key] = now.timestamp()
            if d is not None:
                validate_decision(d.to_dict())
                items.append(d)

    # 合并当日已有决策（按 decision_id 去重）
    env = _load(OUT)
    merged = {str(x.get("decision_id")): x for x in (env.get("items") or [])
              if isinstance(x, dict) and str(x.get("decision_id"))}
    for d in items:
        merged[d.decision_id] = d.to_dict()
    today = [v for k, v in merged.items() if k.startswith(day + "-")]
    out = {
        "schema_version": "news_decision.v1",
        "as_of": now.isoformat(timespec="seconds"),
        "trade_date": day,
        "items": sorted(today, key=lambda x: str(x.get("decision_id"))),
        "risk_overlay": {"source": "fast", "n_items": len(today)},
    }
    counts: dict[str, int] = {}
    for x in out["items"]:
        k = str(x.get("decision") or "?")
        counts[k] = counts.get(k, 0) + 1
    if not dry:
        _write(OUT, out)
        _write(AGG, {"date": day, "tickers": tickers})
        if not fails:
            state["last_seen"] = until_iso
        state["seen"] = seen
        try:
            watch_codes, _ = load_watch_codes()
            focus = set(watch_codes)
        except Exception:  # noqa: BLE001 - 关注池不可达时只告警 P0（宁多勿漏）
            focus = set()
        _alert_p0([d for d in items if not focus or d.code in focus], state)
        _write_redis(items)
        _write(STATE, state)
    print(f"快速通道决策 {counts} → {OUT}")
    return 0 if not fails else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return run_once(since=a.since or None, until=a.until or None, dry=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
