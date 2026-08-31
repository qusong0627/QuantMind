#!/usr/bin/env python3
"""picks_with_news.py — 每日选股 × 新闻增强（stock-picks 技能第 2.5 步）。

读最新（或指定日期）picks.json 候选池，逐只拉 Huntly/RSS 聚合新闻
（quantmind /api/v1/news/articles，含 LLM 情感标注），合并输出推荐表：

  候选分数/行业 + 近 24h 新闻标题/来源/时间/利好利空
  → 标注：★ 新闻强化（≥2 利好且多于利空）/ ⚠ 利空警示（≥2 利空且多于利好）

用法:
  python3 skills/stock-picks/scripts/picks_with_news.py                # 最新日期 top 10
  python3 skills/stock-picks/scripts/picks_with_news.py --date 20260831 --top 15
  python3 skills/stock-picks/scripts/picks_with_news.py --json         # 供上游脚本解析

输出: data/reports/stock_picks/{date}_picks_news.md（速览同打印）
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # quantmind/
PICKS_DIR = ROOT / "data" / "reports" / "stock_picks"
API = "http://127.0.0.1:8000/api/v1/news/articles"
EMOJI = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
WINDOW_H = 24
NEWS_LIMIT = 5


def to_suffix(sym: str) -> str:
    """6 位代码 → 后缀式（600519.SH / 000789.SZ / 830799.BJ）。"""
    s = str(sym or "").strip()
    if not s:
        return ""
    if "." in s:
        return s
    if s.startswith(("6", "9", "5")):
        return f"{s}.SH"
    if s.startswith(("0", "3")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    return s


def fetch_news(code: str, hours: int = WINDOW_H) -> list:
    """单只候选近 N 小时新闻（ticker 过滤，按时间倒序）。失败返回空列表。"""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{API}?tickers={code}&since={since}&page_size={NEWS_LIMIT}&sort=time_desc"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return (json.loads(resp.read().decode()) or {}).get("articles") or []
    except Exception:  # noqa: BLE001
        return []


def bj(iso: str) -> str:
    """ISO → 北京时间 MM-DD HH:MM。"""
    try:
        t = datetime.fromisoformat((iso or "").replace("Z", "+00:00")) + timedelta(hours=8)
        return t.strftime("%m-%d %H:%M")
    except ValueError:
        return ""


def flag_of(sentiments: list) -> tuple[str, int, int]:
    bulls = sum(1 for s in sentiments if s == "bullish")
    bears = sum(1 for s in sentiments if s == "bearish")
    if not sentiments:
        flag = "无新闻"
    elif bulls >= 2 and bulls > bears:
        flag = "★ 新闻强化"
    elif bears >= 2 and bears > bulls:
        flag = "⚠ 利空警示"
    else:
        flag = "—"
    return flag, bulls, bears


def main() -> int:
    ap = argparse.ArgumentParser(description="每日选股 × 新闻增强")
    ap.add_argument("--date", default="", help="YYYYMMDD；默认最新 picks.json")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="输出 JSON（供上游脚本解析）")
    args = ap.parse_args()

    date = args.date
    if not date:
        files = sorted(PICKS_DIR.glob("*_picks.json"))
        if not files:
            print("无 picks.json", file=sys.stderr)
            return 1
        date = files[-1].stem.split("_")[0]
    path = PICKS_DIR / f"{date}_picks.json"
    if not path.is_file():
        print(f"无 {date}_picks.json", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    cands = (data.get("candidates") or [])[: args.top]
    direction = data.get("market_direction") or {}
    print(f"候选 {len(cands)} 只  大盘: {direction.get('direction', '—')}"
          f"（{direction.get('total_score', '—')}/11）  逐只拉新闻中…", file=sys.stderr)

    out = []
    for c in cands:
        code = to_suffix(c.get("symbol") or c.get("code") or "")
        news = fetch_news(code)
        sentiments = [(n.get("enrichment") or {}).get("sentiment_label") for n in news]
        flag, bulls, bears = flag_of(sentiments)
        out.append({
            "rank": c.get("rank"), "code": code, "name": c.get("name"),
            "industry": c.get("industry"), "score": c.get("score"),
            "side": c.get("side"), "flag": flag, "bulls": bulls, "bears": bears,
            "news": [{"title": (n.get("title") or "").strip(),
                      "source": n.get("source_name") or "",
                      "time": bj(n.get("published_at")),
                      "sentiment": (n.get("enrichment") or {}).get("sentiment_label")}
                     for n in news[:3]],
        })
        print(f"  {out[-1]['flag']:8s} {code} {c.get('name', '')}", file=sys.stderr)

    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return 0

    lines = [
        f"# 每日选股推荐（新闻增强）{date}",
        "",
        f"大盘方向：{direction.get('direction', '—')}（{direction.get('total_score', '—')}/11）",
        "",
        "| 排名 | 代码 | 名称 | 行业 | 总分 | 信号 | 新闻标注 | 利好/利空 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for o in out:
        lines.append(f"| {o['rank']} | {o['code']} | {o['name']} | {o['industry']} "
                     f"| {o['score']} | {o['side']} | {o['flag']} | {o['bulls']}/{o['bears']} |")
    lines += ["", "## 新闻详情", ""]
    for o in out:
        lines.append(f"### {o['rank']}. {o['name']}（{o['code']}）{o['flag']}")
        if not o["news"]:
            lines.append(f"- 近 {WINDOW_H} 小时无新闻")
        for n in o["news"]:
            lines.append(f"- {EMOJI.get(n['sentiment'], '⚪')} {n['title']}"
                         f"（{n['source']} {n['time']}）")
        lines.append("")
    md = "\n".join(lines)
    dst = PICKS_DIR / f"{date}_picks_news.md"
    dst.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n已落盘: {dst}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
