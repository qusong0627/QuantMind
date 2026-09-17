#!/usr/bin/env python3
"""通达信桥实时行情直读（纯标准库）。用法见 SKILL.md。

  tdx_realtime.py health                 # 链路 + 行情新鲜度
  tdx_realtime.py snapshot 300750.SZ 600309.SH
  tdx_realtime.py daily 688183.SH
  tdx_realtime.py index
Token 解析顺序：环境变量 TDX_BRIDGE_TOKEN > /quantmind/.env > 本文件同目录配置。
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_URL = "http://192.168.31.13:8550"
BJ = timezone(timedelta(hours=8))
INDEXES = [("000001.SH", "上证指数"), ("000016.SH", "上证50"), ("000300.SH", "沪深300"),
           ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"), ("000688.SH", "科创50")]


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


def bridge():
    env = dict(os.environ)
    for p in ("/quantmind/.env", "/quantmind/runtime_env_cn.json"):
        env.update(_env_from(p))
    url = env.get("TDX_BRIDGE_URL", DEFAULT_URL).rstrip("/")
    token = env.get("TDX_BRIDGE_TOKEN", "")
    return url, token


def call(url, token, method, params):
    body = json.dumps({"method": method, "params": params}).encode()
    req = urllib.request.Request(f"{url}/api/v1/tdx/call", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode())


def quote_fresh(url, token, probe="600519.SH"):
    """日K最后 bar 日期 ≥ 北京今天（周末顺延）→ 新鲜。"""
    now = datetime.now(BJ)
    blk = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 1, 6: 2}.get(now.weekday(), 0)
    anchor = (now - timedelta(days=blk)).strftime("%Y%m%d")
    try:
        r = call(url, token, "get_market_data",
                 {"stock_list": [probe], "period": "1d", "count": 3,
                  "dividend_type": "none"})
        dates = ((r.get("result") or {}).get("Value") or {}).get(probe, {}).get("Date") or []
        if not dates:
            return {"fresh": False, "reason": "无法取日K"}
        last = str(dates[-1])
        return {"fresh": last >= anchor, "last_bar": last, "anchor": anchor}
    except Exception as exc:  # noqa: BLE001
        return {"fresh": False, "reason": str(exc)}


def fmt_chg(now, last_close):
    if not last_close:
        return "—"
    return f"{(now / last_close - 1) * 100:+.2f}%"


def snapshot_lines(url, token, codes):
    out = []
    for code in codes:
        try:
            r = (call(url, token, "get_market_snapshot", {"stock_code": code})
                 .get("result") or {})
        except Exception as exc:  # noqa: BLE001
            out.append(f"{code}: 失败 {exc}")
            continue
        if str(r.get("ErrorId") or "0") != "0":
            out.append(f"{code}: ErrorId={r.get('ErrorId')}（数据不可用）")
            continue
        now = float(r.get("Now") or 0)
        last = float(r.get("LastClose") or 0)
        b1 = (r.get("Buyp") or ["—"])[0]
        s1 = (r.get("Sellp") or ["—"])[0]
        bv1 = (r.get("Buyv") or ["—"])[0]
        sv1 = (r.get("Sellv") or ["—"])[0]
        out.append(
            f"{code}: 现价 {now} 涨跌幅 {fmt_chg(now, last)} "
            f"开 {r.get('Open')} 高 {r.get('Max')} 低 {r.get('Min')} "
            f"Vol {r.get('Volume')} 买一 {b1}×{bv1} 卖一 {s1}×{sv1}")
    return out


def main():
    url, token = bridge()
    act = sys.argv[1] if len(sys.argv) > 1 else "health"
    if act == "health":
        try:
            with urllib.request.urlopen(f"{url}/api/v1/health", timeout=6) as r:
                h = json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            print(f"桥不可达: {exc}")
            return 1
        f = quote_fresh(url, token)
        print(json.dumps({"health": h, "freshness": f}, ensure_ascii=False))
        return 0
    if act == "snapshot":
        codes = sys.argv[2:] or ["300750.SZ"]
        f = quote_fresh(url, token)
        if not f.get("fresh"):
            print(f"⚠️ 行情新鲜度异常：{f.get('reason', '停更')}——价格可能陈旧")
        for line in snapshot_lines(url, token, codes):
            print(line)
        return 0
    if act == "daily":
        codes = sys.argv[2:] or ["688183.SH"]
        for code in codes:
            r = call(url, token, "get_market_data",
                     {"stock_list": [code], "period": "1d", "count": 6,
                      "dividend_type": "none"}).get("result") or {}
            b = ((r.get("Value") or {}).get(code) or {})
            print(f"{code}: 日期 {b.get('Date')} 收盘 {b.get('Close')} "
                  f"量 {b.get('Volume')}（最后一根=今日实时bar{' ✓' if b.get('Date') else ' ✗'}）")
        return 0
    if act == "index":
        for line in snapshot_lines(url, token, [c for c, _ in INDEXES]):
            print(line)
        return 0
    print("用法: tdx_realtime.py health|snapshot|daily|index [CODE...]")
    return 2


if __name__ == "__main__":
    sys.exit(main())