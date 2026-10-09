#!/usr/bin/env python3
"""晚间复盘 Agent（news-review）：当日新闻分子 vs 个股实际涨跌 → 信号有效性复盘。

流程（交易日收盘后运行，cron 北京 22:30 = JST 23:30）：
  1. 读当日 data/news_brief/history.jsonl（各轮分子：holdings 逐票 verdict/impact
     + micro 事件 sentiment）
  2. 汇总当日被点评过的代码 → 桥日K取「当日 bar vs 昨收」实际涨跌
  3. 逐条判定：利好&涨=hit(有用) / 利好&跌=reverse(反向) / |涨跌|≤1%=flat(无用)；利空对称
  4. 聚合进 data/news_brief/lessons.json（按 事件类型 × 来源 的 hit/reverse/flat 计数，
     跨日累计——供新闻管线每日注入「历史经验」提示词，越用越准）
  5. 复盘对话落盘 news-review agent（前端新闻 tab 可见）

诚实边界：以「当日涨跌」为验证口径是粗粒度（当天涨停可能有多因），
lessons 只做提示词先验，不做硬闸门。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from trading_cal import is_trading_day  # noqa: E402
from news_brief import (CN_TZ, HISTORY_FILE, LESSONS_FILE,  # noqa: E402
                        append_log, normalize_event_type)

REVIEW = "news-review"
AGENT_CN = "晚间复盘"
FLAT_BAND = 1.0        # |当日涨跌| ≤ 1% 视为"无反应"
MICRO_MIN_SENTIMENT = 0.5  # micro 事件 |sentiment| 低于此不参与复盘（弱信号宁精勿滥）
MAX_CODES = 30         # 单日复盘个股上限（桥调用预算）


def load_day_briefs(day: str, path: Path | None = None) -> list[dict]:
    """history.jsonl 中当日（北京）的分子列表。"""
    out = []
    try:
        # errors="replace"：news history 是含中文的追加日志（news_brief.py:680 open("a")），
        # 截断在多字节字符中间会整文件抛 UnicodeDecodeError（不是 OSError）；
        # 逐行 handler 本就容错，替换成 U+FFFD 只损失撕裂那一行（2026-09-12 批 10）
        for line in (path or HISTORY_FILE).read_text(encoding="utf-8",
                                                     errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(d.get("ts") or "")
            if ts[:10] == day:
                out.append(d)
    except OSError:
        pass
    return out


def collect_mentions(briefs: list[dict]) -> list[dict]:
    """当日新闻提及的个股清单（micro 事件 + 持仓/盯盘行，含中性），供走势回顾。"""
    seen, out = set(), []
    for b in briefs:
        micro = (b.get("segments") or {}).get("news-micro") or {}
        for e in (micro.get("events") or []):
            if not isinstance(e, dict):
                continue
            for code in (e.get("tickers") or []):
                code = str(code)
                if code and code not in seen:
                    seen.add(code)
                    out.append({"code": code, "name": str(e.get("name") or ""),
                                "event": normalize_event_type(
                                    e.get("event_type"), e.get("note"), e.get("name")),
                                "sentiment": e.get("sentiment")})
        for h in (b.get("holdings") or []):
            if isinstance(h, dict) and h.get("code"):
                code = str(h["code"])
                if code not in seen:
                    seen.add(code)
                    out.append({"code": code, "name": str(h.get("name") or ""),
                                "event": normalize_event_type(
                                    h.get("event_type"), h.get("headline"), h.get("name")),
                                "sentiment": None})
        for w in (b.get("watch_list") or []):
            if isinstance(w, dict) and w.get("code"):
                code = str(w["code"])
                if code not in seen:
                    seen.add(code)
                    out.append({"code": code, "name": str(w.get("name") or ""),
                                "event": "盯盘", "sentiment": None})
    return out


def collect_rows(briefs: list[dict]) -> list[dict]:
    """各轮分子的逐票信号去重合并（同 code 同事件只留一条，保留最强 impact）。

    两个来源：
    - holdings 逐票行（verdict 直接给出）——主编保真转写。**2026-09-09 起复活**：
      持仓情报段改走「板块→个股传导」（证据闸门 + 串行在 macro/micro 之后），
      不再等新闻标题点名持仓（此前该段自 09-07 起零输出，holdings 全天 0 条）。
      来源列「板块传导」= 推断，其它 = 新闻直接命中，复盘时可分开统计。
    - micro 事件（sentiment -1..1 → 利好/利空）——每日稳定的个股级信号流
      （约 5 条/轮）

    2026-09-08 前只复盘 holdings → rows 恒空 → lessons.json 恒为 `{}`，
    "越用越准"的经验闭环从未启动。弱信号（|sentiment|<0.5）不参与，宁精勿滥。
    """
    best: dict = {}

    def _add(code, name, verdict: str, impact, event_type, source) -> None:
        try:
            imp = float(impact)
        except (TypeError, ValueError):
            return
        if not code or verdict not in ("利好", "利空") or imp == 0:
            return
        ev = normalize_event_type(event_type)
        key = (str(code), ev)
        prev = best.get(key)
        if prev is None or abs(imp) > abs(float(prev.get("impact") or 0)):
            best[key] = {"code": str(code), "name": str(name or ""),
                         "verdict": verdict, "impact": imp,
                         "event_type": ev,
                         "source": str(source or "")}

    for b in briefs:
        for h in (b.get("holdings") or []):
            if not isinstance(h, dict):
                continue
            _add(h.get("code"), h.get("name"), str(h.get("verdict") or "中性"),
                 h.get("impact"), h.get("event_type"), h.get("source"))
        micro = (b.get("segments") or {}).get("news-micro") or {}
        for e in (micro.get("events") or []):
            if not isinstance(e, dict):
                continue
            try:
                senti = float(e.get("sentiment"))
            except (TypeError, ValueError):
                continue
            if abs(senti) < MICRO_MIN_SENTIMENT:
                continue
            for code in (e.get("tickers") or []):
                _add(code, e.get("name"), "利好" if senti > 0 else "利空", senti,
                     e.get("event_type"), "micro")
    return list(best.values())


def day_moves(codes: list[str]) -> dict:
    """桥日K：{code: 当日涨跌%}。当日 bar 缺失/桥失败 → 缺该 code（复盘条目标记无数据）。"""
    moves: dict = {}
    if not codes:
        return moves
    try:
        from agent_tools.brokers.tdx_bridge import TdxBridgeBroker

        broker = TdxBridgeBroker()
        today = datetime.now(CN_TZ).strftime("%Y%m%d")
        for code in codes[:MAX_CODES]:
            try:
                bars = broker.get_klines(code, interval="daily")
                if len(bars) < 2:
                    continue
                last = bars[-1]
                if str(last.get("date") or "").replace("-", "") != today:
                    continue  # 当日 bar 未生成（数据滞后）→ 跳过
                prev = float(bars[-2].get("close") or 0)
                close = float(last.get("close") or 0)
                if prev > 0 and close > 0:
                    moves[code] = round((close / prev - 1) * 100, 2)
            except Exception:  # noqa: BLE001 单只失败不影响整体
                continue
    except Exception:  # noqa: BLE001 桥不可用 → 全部无数据，复盘降级为记录
        pass
    return moves


def classify(verdict: str, impact: float, move: float | None) -> str:
    """hit=方向兑现 / reverse=反向 / flat=无反应 / nodata。"""
    if move is None:
        return "nodata"
    if abs(move) <= FLAT_BAND:
        return "flat"
    up = move > 0
    if (verdict == "利好") == up:
        return "hit"
    return "reverse"


def update_lessons(rows: list[dict], moves: dict, path: Path | None = None) -> dict:
    """累计 lessons.json：by_event_type / by_source 的 hit/reverse/flat 计数。"""
    lessons = {"updated": "", "by_event_type": {}, "by_source": {}}
    try:
        old = json.loads((path or LESSONS_FILE).read_text(encoding="utf-8"))
        if isinstance(old, dict):
            lessons["by_event_type"] = old.get("by_event_type") or {}
            lessons["by_source"] = old.get("by_source") or {}
    except (OSError, json.JSONDecodeError):
        pass

    def _bump(bucket: dict, key: str, cls: str) -> None:
        e = bucket.setdefault(key, {"hit": 0, "reverse": 0, "flat": 0})
        if cls in e:
            e[cls] += 1

    for h in rows:
        cls = classify(str(h.get("verdict") or "中性"),
                       float(h.get("impact") or 0), moves.get(str(h.get("code"))))
        if cls == "nodata":
            continue
        _bump(lessons["by_event_type"], normalize_event_type(h.get("event_type")), cls)
        _bump(lessons["by_source"], str(h.get("source") or "未知")[:24], cls)
    lessons["updated"] = datetime.now(CN_TZ).isoformat()
    out_path = path or LESSONS_FILE
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(lessons, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    return lessons


def lessons_text(lessons: dict, top: int = 5) -> str:
    """lessons → 提示词注入文本（样本≥3 的事件类型才展示，宁缺毋滥）。
    "其他/未分类"是兜底桶，跨类型混装，不作为经验注入。"""
    lines = []
    et = {k: v for k, v in (lessons.get("by_event_type") or {}).items()
          if k not in ("其他", "未分类")
          and v.get("hit", 0) + v.get("reverse", 0) + v.get("flat", 0) >= 3}
    for k, v in sorted(et.items(), key=lambda kv: -(kv[1]["hit"] + kv[1]["reverse"] + kv[1]["flat"]))[:top]:
        n = v["hit"] + v["reverse"] + v["flat"]
        lines.append(f"- {k}：信号{n}次，方向兑现 {v['hit']}/{n}"
                     f"（反向 {v['reverse']}，无反应 {v['flat']}）")
    return "\n".join(lines)


def run_review(day: str = "") -> int:
    now = datetime.now(CN_TZ)
    day = day or now.strftime("%Y-%m-%d")
    if not is_trading_day(datetime.fromisoformat(day).date()):
        print(f"⏭️ {day} 非交易日，不复盘")
        return 0
    briefs = load_day_briefs(day)
    rows = collect_rows(briefs)
    mentions = collect_mentions(briefs)
    if not rows and not mentions:
        msg = f"{day} 无可复盘内容（当日分子无信号/提及个股）"
        print("⏭️ " + msg)
        append_log(msg, "（无复盘输入）", REVIEW)
        return 0
    codes = sorted({str(r.get("code")) for r in rows}
                   | {str(m.get("code")) for m in mentions})
    moves = day_moves(codes)
    judged = []
    for r in rows:
        cls = classify(str(r.get("verdict") or "中性"),
                       float(r.get("impact") or 0), moves.get(str(r.get("code"))))
        mv = moves.get(str(r.get("code")))
        judged.append({"code": r.get("code"), "name": r.get("name"),
                       "verdict": r.get("verdict"), "event": r.get("event_type"),
                       "move": mv, "result": cls})
    n_hit = sum(1 for j in judged if j["result"] == "hit")
    n_rev = sum(1 for j in judged if j["result"] == "reverse")
    n_flat = sum(1 for j in judged if j["result"] == "flat")
    n_nodata = sum(1 for j in judged if j["result"] == "nodata")
    lessons = update_lessons(rows, moves)
    # 当日新闻提及个股的走势回顾（后期回顾档案）
    review_rows = [{"code": m["code"], "name": m["name"], "event": m["event"],
                    "move": moves.get(m["code"])} for m in mentions]
    review_rows.sort(key=lambda x: -(x["move"] if x["move"] is not None else -99))
    archive = {"day": day, "ts": now.isoformat(),
               "signals": judged, "mentions": review_rows,
               "summary": {"hit": n_hit, "reverse": n_rev,
                           "flat": n_flat, "nodata": n_nodata}}
    try:
        adir = ROOT / "data" / "news_brief" / "reviews"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / f"{day}.json").write_text(
            json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass

    def _mv(code: str) -> str:
        v = moves.get(code)
        return f"{v:+.2f}%" if v is not None else "无数据"

    user = (f"{day} 晚间复盘：当日新闻信号对照实际涨跌（±{FLAT_BAND}% 内=无反应）\n"
            + "\n".join(f"- {j['name']}({j['code']}) 信号[{j['verdict']}] {j['event']} "
                        f"→ 当日 {_mv(str(j['code']))} = {j['result']}" for j in judged)
            + "\n\n当日新闻提及个股·走势回顾：\n"
            + "\n".join(f"- {m['name']}({m['code']}) [{m['event']}] 当日 {_mv(m['code'])}"
                        for m in review_rows[:20]))
    summary = (f"复盘完成：有用(方向兑现) {n_hit} / 反向 {n_rev} / 无反应 {n_flat} "
               f"/ 无数据 {n_nodata}；当日新闻提及个股 {len(review_rows)} 只已归档"
               f"（data/news_brief/reviews/{day}.json，含新闻→当日走势，供后期回顾）。\n"
               + ("经验已累计进 lessons.json（按事件类型/来源）。" if n_hit + n_rev + n_flat else ""))
    append_log(user, summary, REVIEW)
    print(f"✓ [{AGENT_CN}] {summary.splitlines()[0]}")
    print(f"  事件类型经验 Top：\n{lessons_text(lessons) or '  （样本不足，暂无）'}")
    return 0


def cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="晚间复盘 agent")
    ap.add_argument("--day", default="", help="YYYY-MM-DD（默认今天，北京）")
    a = ap.parse_args()
    try:
        return run_review(a.day)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 复盘失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
