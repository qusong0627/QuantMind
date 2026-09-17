#!/usr/bin/env python3
"""同花顺 Fuyao 金融数据 REST 封装（纯标准库）。用法见 SKILL.md。

  ths_fuyao.py snapshot 600519.SH,300750.SZ
  ths_fuyao.py ztpool
  ths_fuyao.py ladder
  ths_fuyao.py lhb 2026-09-03
  ths_fuyao.py kline 600519.SH 2026-08-01 2026-09-03
  ths_fuyao.py raw /api/a-share/special-data/hot-stock-list '{"limit":10}'
Key 解析顺序：环境变量 THS_FUYAO_KEY → /quantmind/.env → 仓库 .env。
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://fuyao.aicubes.cn"
ENDPOINTS = {
    "snapshot": "/api/a-share/prices/snapshot",
    "kline": "/api/a-share/prices/historical",
    "valuation": "/api/a-share/valuations/snapshot",
    "fin": "/api/a-share/financials/indicators",
    "index-snap": "/api/a-share-index/prices/snapshot",
    "index-cons": "/api/a-share-index/constituents/ths-stock-list",
    "lhb": "/api/a-share/special-data/dragon-tiger-list",
    "ztpool": "/api/a-share/special-data/limit-up-pool",
    "dtpool": "/api/a-share/special-data/limit-down-pool",
    "ladder": "/api/a-share/special-data/limit-up-ladder",
    "hot": "/api/a-share/special-data/hot-stock-list",
    "anomaly": "/api/a-share/special-data/anomaly-analysis-list",
    "auction": "/api/a-share/auction/snapshot",
    "calendar": "/api/a-share/calendar/trading-days",
}


def _env_from(path: str) -> dict:
    env = {}
    try:
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^\s*([A-Z_]+)=\"?([^\"]*?)\"?\s*$", line)
            if m:
                env[m.group(1)] = m.group(2)
    except OSError:
        pass
    return env


def api_key() -> str:
    for src in (dict(os.environ),
                _env_from("/quantmind/.env"),
                _env_from(str(Path(__file__).resolve().parents[4] / ".env")),
                _env_from(str(Path(__file__).resolve().parents[4] / ".service.env"))):
        v = (src.get("THS_FUYAO_KEY") or "").strip()
        if v:
            return v
    return ""


def get(path: str, params: dict) -> dict:
    key = api_key()
    if not key:
        return {"code": -1, "message": "THS_FUYAO_KEY 未配置（.env）"}
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-api-key": key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("用法: ths_fuyao.py <子命令|raw> [参数…]  （子命令见 SKILL.md 能力地图）")
        return 2
    act = args[0]
    if act == "raw":
        path, q = args[1], json.loads(args[2]) if len(args) > 2 else {}
    else:
        path = ENDPOINTS.get(act)
        if not path:
            print(f"未知子命令: {act}")
            return 2
        rest = args[1:]
        q: dict = {}
        if act in ("snapshot",):
            if rest:
                q["thscode"] = ",".join(rest)
        elif act in ("kline", "fin"):
            q["thscode"] = rest[0] if rest else ""
            if len(rest) > 1:
                q["start"] = rest[1]
            if len(rest) > 2:
                q["end"] = rest[2]
        elif act in ("lhb", "ztpool", "dtpool", "ladder", "hot", "anomaly", "auction"):
            if rest:
                q["trade_date"] = rest[0]
        elif act in ("index-cons", "index-snap"):
            if rest:
                q["thscode"] = rest[0]
    q = {k: v for k, v in q.items() if v not in ("", None)}
    data = get(path, q)
    code = data.get("code")
    if code != 0:
        print(f"❌ code={code} {data.get('message')} (request_id={data.get('request_id')})")
        return 1
    d = data.get("data") or {}
    items = d.get("item")
    print(json.dumps({"count": len(items) if isinstance(items, list) else None,
                      "ts": d.get("timestamp"), "item": items if items is not None else d},
                     ensure_ascii=False, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())