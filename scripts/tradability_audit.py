#!/usr/bin/env python3
"""A股可交易性约束审计器（quantdb + 通达信桥适配版）。

方法论参考 QuantSkills skill-a-share-tradability-auditor（GPL-3.0，作者 13817660341-coder）
的制度规则表与检查框架；实现为本仓库原创，数据层适配本地栈：
  - 行情面板：quantdb daily_backward（后复权，比例法判封板，limit_reliable=false）
    或通达信桥 get_klines（未复权，可算精确涨跌停价）
  - 制度规则：scripts/ashare_rules.py（板块/ST 沿革按日期解析/碎股口径）
  - 交易流：logs/live_trade_*.jsonl（账本真实成交）或通用 JSONL
    （{code, side, volume, price, ts}）

逐笔判定：涨停买不进 / 跌停卖不出 / 停牌 / T+1 违约（FIFO 手数级）/ 裸卖空 /
碎股与手数非法 / 参与率超限 / 新股窗口 → 汇总"哪些成交是市场不会给你的"。

用法：
  python scripts/tradability_audit.py                     # 审计 logs/live_trade_*.jsonl 全部真实成交
  python scripts/tradability_audit.py --selftest          # 内置合成用例自检
  python scripts/tradability_audit.py --source bridge --start 2026-09-01
  python scripts/tradability_audit.py --trades a.jsonl b.jsonl --participation 0.05
"""
import argparse
import glob
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ashare_rules import (board_of, price_limit_pct,           # noqa: E402
                          round_buy_qty, round_sell_qty)
from trading_cal import is_trading_day                          # noqa: E402

QDB_ROOTS = [Path.home() / "projects/quantmind/data/quantdb/1_kline_data/daily_backward",
             Path("/data/quantdb/1_kline_data/daily_backward")]
NEW_LISTING_DAYS = 5


# ---------------- 行情面板 ----------------

def build_panel_quantdb(codes: set, start: date, end: date) -> dict:
    """quantdb 日线 → {code: {date: bar_dict}}（后复权：封板判定用涨跌幅比例法）。"""
    want = {(d.year, d.month, d.day) for d in
            (start + timedelta(days=i) for i in range((end - start).days + 1))}
    panel: dict = {c: {} for c in codes}
    for base in QDB_ROOTS:
        for fs in sorted(glob.glob(f"{base}/dt=*")):
            dt_key = fs.rsplit("dt=", 1)[-1]
            try:
                d = date(int(dt_key[:4]), int(dt_key[4:6]), int(dt_key[6:8]))
            except ValueError:
                continue
            if d < start - timedelta(days=8) or d > end:  # 多读几天拿 prev_close
                continue
            try:
                import duckdb

                con = duckdb.connect()
                ph = ",".join("?" * len(codes))
                rows = con.execute(
                    f"SELECT symbol, time, open, high, low, close, volume, amount "
                    f"FROM read_parquet(?) WHERE symbol IN ({ph})",
                    [fs, *sorted(codes)]).fetchall()
            except Exception:  # noqa: BLE001
                continue
            for sym, t, o, h, low, c, v, amt in rows:
                if sym not in panel:
                    panel[sym] = {}
                panel[sym][d] = {"open": o, "high": h, "low": low, "close": c,
                                 "volume": v, "amount": amt}
            if start <= d <= end:
                want.discard((d.year, d.month, d.day))
        break
    return _finalize_panel(panel, start, end)


def build_panel_bridge(codes: set, start: date, end: date) -> dict:
    """通达信桥日K（未复权）→ 面板。逐 code 调用（桥限流 ~1/s，只适合小集合/近窗）。"""
    from agent_tools.brokers.tdx_bridge import TdxBridgeBroker

    broker = TdxBridgeBroker()
    panel: dict = {c: {} for c in codes}
    for code in sorted(codes):
        try:
            bars = broker.get_klines(code, interval="daily")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 桥取 {code} 失败: {exc}")
            continue
        for b in bars or []:
            try:
                d = datetime.strptime(str(b.get("date") or b.get("time") or "")[:10],
                                      "%Y-%m-%d").date()
            except ValueError:
                continue
            if start - timedelta(days=8) <= d <= end:
                panel.setdefault(code, {})[d] = {
                    "open": float(b.get("open") or 0), "high": float(b.get("high") or 0),
                    "low": float(b.get("low") or 0), "close": float(b.get("close") or 0),
                    "volume": float(b.get("volume") or 0),
                    "amount": float(b.get("amount") or 0)}
        import time as _t
        _t.sleep(1)  # 桥限流
    return _finalize_panel(panel, start, end)


def _finalize_panel(panel: dict, start: date, end: date) -> dict:
    """补 prev_close 与封板/停牌/新股标记。封板判定：涨跌幅比例法（容差 0.2pp，
    兼容复权序列；跨除权日可能漏判/误判，assumptions 里注明）。"""
    for code, bars in panel.items():
        pct = price_limit_pct(code)
        dates = sorted(bars)
        prev_close = None
        seen = 0
        for i, d in enumerate(dates):
            bar = bars[d]
            bar["prev_close"] = prev_close
            bar["pct"] = pct
            bar["suspended"] = False
            bar["new_listing"] = seen < NEW_LISTING_DAYS
            seen += 1
            chg = ((bar["close"] / prev_close - 1) * 100) if prev_close else None
            bar["day_chg"] = chg
            touched_up = prev_close and bar["high"] and \
                (bar["high"] / prev_close - 1) * 100 >= pct - 0.2
            touched_down = prev_close and bar["low"] and \
                (bar["low"] / prev_close - 1) * 100 <= -pct + 0.2
            bar["limit_up_locked"] = bool(prev_close and bar["low"] and
                                          (bar["low"] / prev_close - 1) * 100 >= pct - 0.2)
            bar["limit_down_locked"] = bool(prev_close and bar["high"] and
                                            (bar["high"] / prev_close - 1) * 100 <= -pct + 0.2)
            bar["touched_up"], bar["touched_down"] = bool(touched_up), bool(touched_down)
            bar["limit_reliable"] = False  # quantdb 复权口径：无精确涨跌停价
            prev_close = bar["close"] if bar["close"] else prev_close
        # 交易日有、面板无 bar → 停牌
        d = start
        while d <= end:
            if is_trading_day(d) and d not in bars and any(bars):
                bars[d] = {"suspended": True, "new_listing": False, "limit_up_locked": False,
                           "limit_down_locked": False, "touched_up": False,
                           "touched_down": False, "pct": pct, "prev_close": None,
                           "day_chg": None, "limit_reliable": False, "close": None,
                           "amount": None, "open": None, "high": None, "low": None,
                           "volume": None}
            d += timedelta(days=1)
    return panel


# ---------------- 交易流 ----------------

def load_trades(paths: list) -> list:
    """账本/通用 JSONL → 规范化成交列表，按 (code, ts) 排序。

    - 剔除带 error 的拒单（那是意图不是成交，由闸门/延期层管）
    - 按 order_id 去重（execute 与 fill_confirm 双写同一单）
    - side 兼容 side/action 字段；两者皆缺且有 result 视为 buy（旧版买入行）
    """
    by_order: dict = {}
    generic: list = []
    for p in paths:
        # errors="replace"：含中文追加流水可能截断在多字节字符中间；本行式读取每个
        # 读取点都逐行容错，唯独解码一步没有 try，撕裂 = 整个审计崩（2026-09-12 批 10）
        for line in Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("error"):
                continue
            oid = str(row.get("order_id") or (row.get("result") or {}).get("order_id") or "")
            side = (row.get("side") or row.get("action") or "").lower()
            price = float(row.get("price") or (row.get("fill") or {}).get("filled_price") or 0)
            vol = int(row.get("volume") or (row.get("fill") or {}).get("filled_volume") or 0)
            ts = str(row.get("ts") or "")
            if not oid and price and vol and ts:
                generic.append({"code": str(row.get("code") or ""), "side": side or "buy",
                                "volume": vol, "price": price, "ts": ts})
                continue
            if not oid or not price or not vol or not ts:
                continue
            prev = by_order.get(oid)
            if prev is None or (side and not (prev.get("side") or prev.get("action"))):
                by_order[oid] = row
    trades = []
    for row in list(by_order.values()) + generic:
        side = (row.get("side") or row.get("action") or "buy").lower()
        price = float(row.get("price") or (row.get("fill") or {}).get("filled_price") or 0)
        vol = int(row.get("volume") or (row.get("fill") or {}).get("filled_volume") or 0)
        ts = str(row.get("ts") or "")
        code = str(row.get("code") or "")
        if not code or not price or not vol or not ts:
            continue
        trades.append({"code": code, "side": "sell" if side == "sell" else "buy",
                       "volume": int(vol), "price": float(price), "ts": ts,
                       "date": date.fromisoformat(ts[:10]), "agent": row.get("agent") or "",
                       "order_id": str(row.get("order_id") or
                                       (row.get("result") or {}).get("order_id") or "")})
    trades.sort(key=lambda t: (t["code"], t["ts"]))
    return trades


# ---------------- 审计 ----------------

RULES = {
    "M1_buy_limit_up_locked": "涨停一字/封板买入（买不进）",
    "M2_sell_limit_down_locked": "跌停封板卖出（卖不出）",
    "M3_new_listing_window": "新股窗口（涨跌幅口径不可靠）",
    "M4_participation_breach": "单笔占当日成交额超参与率上限",
    "M5_naked_short": "卖出超持仓（裸卖空）",
    "M6_t1_violation": "T+1：卖出动用了当日买入手数",
    "M7_lot_size_illegal": "申报量违反板块手数/碎股规则",
    "M8_suspended": "停牌日有成交（数据/执行异常）",
}


def audit(trades: list, panel: dict, participation: float = 0.1) -> dict:
    """重放交易流 → 逐笔判定 + 分规则汇总。"""
    lots: dict = defaultdict(list)  # code -> [(date, qty)] FIFO
    findings: list = []
    for t in trades:
        code, d, side = t["code"], t["date"], t["side"]
        qty, price = t["volume"], t["price"]
        bar = (panel.get(code) or {}).get(d)
        hit = []
        if bar is None:
            hit.append(("M8_suspended", {"note": "面板无该日 bar（停牌或面板未覆盖）"}))
        else:
            if bar.get("new_listing"):
                hit.append(("M3_new_listing_window",
                            {"note": f"面板前 {NEW_LISTING_DAYS} 根 bar，涨跌幅口径不可靠"}))
            if side == "buy" and bar.get("limit_up_locked"):
                hit.append(("M1_buy_limit_up_locked",
                            {"day_chg": bar.get("day_chg"),
                             "note": "一字/封板涨停（低点=涨停价附近），市价买不进"}))
            if side == "sell" and bar.get("limit_down_locked"):
                hit.append(("M2_sell_limit_down_locked",
                            {"day_chg": bar.get("day_chg"),
                             "note": "一字/封板跌停（高点=跌停价附近），卖不出"}))
            # quantdb amount 单位为万元（按平安银行日成交 13.2 亿量级校准）→ 转元
            amount = float(bar.get("amount") or 0) * 10000.0
            if amount and qty * price > amount * participation:
                hit.append(("M4_participation_breach",
                            {"notional": round(qty * price, 2),
                             "day_amount": amount,
                             "ratio": round(qty * price / amount, 4)}))
        pos = lots.get(code, [])
        total = sum(q for _, q in pos)
        if side == "buy":
            legal = round_buy_qty(code, qty)
            if legal != qty:
                hit.append(("M7_lot_size_illegal",
                            {"qty": qty, "legal": legal,
                             "board": board_of(code), "note": "买入申报量不合规"}))
            lots.setdefault(code, []).append((d, qty))
        else:
            if qty > total:
                hit.append(("M5_naked_short", {"qty": qty, "holding": total,
                                               "note": "卖出超过当时持仓"}))
            avail_before = sum(q for ld, q in pos if ld < d)
            t1_qty = max(0, qty - avail_before)
            if t1_qty > 0 and total > 0:
                hit.append(("M6_t1_violation",
                            {"qty": qty, "t1_qty": t1_qty,
                             "note": f"{t1_qty} 股动用了当日买入手数（T+1 不可卖）"}))
            legal = round_sell_qty(code, qty, max(total, qty))
            if legal != qty and not (qty > total):
                hit.append(("M7_lot_size_illegal",
                            {"qty": qty, "legal": legal, "board": board_of(code),
                             "note": "卖出申报量不合规（板块手数/碎股口径）"}))
            # FIFO 扣减（即便违规也记账，保持账实连续）
            need = qty
            while need > 0 and pos:
                ld, q = pos[0]
                take = min(q, need)
                if q - take <= 0:
                    pos.pop(0)
                else:
                    pos[0] = (ld, q - take)
                need -= take
        for rule, ev in hit:
            findings.append({"ts": t["ts"], "agent": t["agent"], "code": code,
                             "side": side, "volume": qty, "price": price,
                             "notional": round(qty * price, 2),
                             "rule": rule, "rule_desc": RULES[rule], "evidence": ev})
        t["verdicts"] = [r for r, _ in hit]
    by_rule: dict = defaultdict(lambda: {"count": 0, "notional": 0.0})
    for f in findings:
        by_rule[f["rule"]]["count"] += 1
        by_rule[f["rule"]]["notional"] += f["notional"]
    return {"trades": trades, "findings": findings,
            "summary": {"total_trades": len(trades),
                        "clean_trades": sum(1 for t in trades if not t.get("verdicts")),
                        "total_notional": round(sum(t["volume"] * t["price"] for t in trades), 2),
                        "flagged_notional": round(sum(f["notional"] for f in findings), 2),
                        "by_rule": {k: v for k, v in sorted(by_rule.items())}},
            "assumptions": {
                "limit_detection": "quantdb 后复权序列比例法（容差0.2pp），跨除权日可能漏判；"
                                   "bridge 源未复权可复核精确涨跌停价",
                "participation": participation,
                "st_rule": "主板 ST 带宽按日期解析（2026-07-06 前 ±5 / 后 ±10）",
            }}


def _selftest() -> int:
    """合成用例：封板买/卖、T+1、裸卖空、碎股。全部用 bridge 口径面板直塞。"""
    d1, d2, d3 = date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)
    panel = {"600309.SH": {
        d1: {"open": 70, "high": 77.5, "low": 77.5, "close": 77.5, "volume": 1e6,
             "amount": 5e7, "prev_close": 70.0, "pct": 10.0, "day_chg": 10.7,
             "limit_up_locked": True, "limit_down_locked": False, "touched_up": True,
             "touched_down": False, "new_listing": False, "suspended": False},
        d2: {"open": 77.5, "high": 78.0, "low": 76.0, "close": 76.5, "volume": 1e6,
             "amount": 6e7, "prev_close": 77.5, "pct": 10.0, "day_chg": -1.3,
             "limit_up_locked": False, "limit_down_locked": False, "touched_up": False,
             "touched_down": False, "new_listing": False, "suspended": False},
        d3: {"open": 76.5, "high": 76.6, "low": 68.85, "low2": None, "close": 68.85,
             "volume": 1e6, "amount": 4e7, "prev_close": 76.5, "pct": 10.0,
             "day_chg": -10.0, "limit_up_locked": False, "limit_down_locked": True,
             "touched_up": False, "touched_down": True, "new_listing": False,
             "suspended": False},
    }}
    trades = [
        {"code": "600309.SH", "side": "buy", "volume": 1000, "price": 77.5,
         "ts": f"{d1} 09:35:00", "date": d1, "agent": "t", "order_id": "a"},
        {"code": "600309.SH", "side": "sell", "volume": 2000, "price": 76.5,
         "ts": f"{d2} 10:00:00", "date": d2, "agent": "t", "order_id": "b"},
        {"code": "600309.SH", "side": "sell", "volume": 100, "price": 68.85,
         "ts": f"{d3} 10:00:00", "date": d3, "agent": "t", "order_id": "c"},
        {"code": "688183.SH", "side": "buy", "volume": 600, "price": 121.0,
         "ts": f"{d2} 09:40:00", "date": d2, "agent": "t", "order_id": "e"},
        {"code": "688183.SH", "side": "sell", "volume": 180, "price": 120.0,
         "ts": f"{d3} 10:00:00", "date": d3, "agent": "t", "order_id": "d"},
    ]
    # 688183（科创板）d2 买入 d3 卖出的正常面板
    panel["688183.SH"] = {
        d2: {"open": 120.5, "high": 122.0, "low": 120.0, "close": 121.0,
             "volume": 2e6, "amount": 2.4e8, "prev_close": 120.0, "pct": 20.0,
             "day_chg": 0.83, "limit_up_locked": False, "limit_down_locked": False,
             "touched_up": False, "touched_down": False, "new_listing": False,
             "suspended": False},
        d3: {"open": 120.8, "high": 121.0, "low": 119.0, "close": 119.5,
             "volume": 2e6, "amount": 2.4e8, "prev_close": 121.0, "pct": 20.0,
             "day_chg": -1.24, "limit_up_locked": False, "limit_down_locked": False,
             "touched_up": False, "touched_down": False, "new_listing": False,
             "suspended": False},
    }
    res = audit(trades, panel, participation=0.05)
    by = res["summary"]["by_rule"]
    t0, t1, t2, t4, t3 = res["trades"]
    assert "M1_buy_limit_up_locked" in t0["verdicts"], t0
    assert "M5_naked_short" in t1["verdicts"], t1            # 2000 > 1000
    assert "M2_sell_limit_down_locked" in t2["verdicts"], t2
    assert "M7_lot_size_illegal" in t3["verdicts"], t3       # 科创板卖 180 股（非 200 递增口径）
    assert "M6_t1_violation" not in t3["verdicts"], t3       # 600 股是昨日买入，T+1 合法
    assert by["M1_buy_limit_up_locked"]["notional"] == 77500.0
    print("✔ 自检 5 用例全过：封板买/裸卖空/封板卖/T+1 合法/碎股")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="A股可交易性约束审计（quantdb/桥 + 账本流水）")
    ap.add_argument("--trades", nargs="*", default=[], help="交易 JSONL（默认 logs/live_trade_*.jsonl）")
    ap.add_argument("--source", choices=["quantdb", "bridge"], default="quantdb")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--participation", type=float, default=0.1)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default="logs/tradability_audit")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    paths = a.trades or sorted(glob.glob(str(ROOT / "logs" / "live_trade_*.jsonl")))
    if not paths:
        print("无交易流水可审计")
        return 1
    trades = load_trades(paths)
    if not trades:
        print("流水里没有可审计的成交（全被拒单/空行）")
        return 1
    start = date.fromisoformat(a.start) if a.start else min(t["date"] for t in trades)
    end = date.fromisoformat(a.end) if a.end else max(t["date"] for t in trades)
    codes = {t["code"] for t in trades}
    print(f"审计 {len(trades)} 笔成交 · {len(codes)} 只标的 · {start}~{end} · 源={a.source}")
    panel = (build_panel_bridge if a.source == "bridge" else build_panel_quantdb)(
        codes, start, end)
    if a.source == "quantdb":
        # quantdb 每日盘后同步、覆盖不到"今天"：缺失的交易日自动用桥补（少量调用）
        missing = {(t["code"], t["date"]) for t in trades
                   if not (panel.get(t["code"]) or {}).get(t["date"], {}).get("close")}
        for code in sorted({c for c, _ in missing}):
            try:
                sub = build_panel_bridge({code}, start, end)
                for d, bar in (sub.get(code) or {}).items():
                    if bar.get("close"):
                        panel.setdefault(code, {})[d] = bar
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️ 桥补 {code} 失败: {exc}")
    res = audit(trades, panel, participation=a.participation)

    s = res["summary"]
    print(f"\n== 汇总：{s['total_trades']} 笔 / 干净 {s['clean_trades']} 笔 / "
          f"涉及金额 ¥{s['total_notional']:,.0f}（被标记 ¥{s['flagged_notional']:,.0f}）==")
    for rule, v in s["by_rule"].items():
        print(f"  {rule} {RULES[rule]}: {v['count']} 笔 / ¥{v['notional']:,.0f}")
    if res["findings"]:
        print("\n== 逐笔证据 ==")
        for f in res["findings"]:
            ev = json.dumps(f["evidence"], ensure_ascii=False)
            print(f"  [{f['ts'][:16]}] {f['agent']} {f['side']} {f['code']} "
                  f"{f['volume']}股@{f['price']} → {f['rule']}: {ev}")
    else:
        print("\n✅ 全部成交均可合法成交，无幽灵交易")
    out = Path(ROOT / a.out)
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    fp.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n报告 → {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
