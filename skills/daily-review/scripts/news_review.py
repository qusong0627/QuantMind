"""当日新闻情绪聚合（升级版复盘：六、新闻情绪）。

在容器内运行（需要 /data/huntly/db.sqlite + PostgreSQL news_article_enrichment）：
    docker exec quantmind python3 /app/skills/daily-review/scripts/news_review.py --date 20260819

产出 <repo>/data/reports/daily_review/{date}_news.json（daily_review.py 读它渲染「六、新闻情绪」章节，
并让方向引擎的「新闻情绪」维度生效、数据完整度提到 ★★★★★）。宿主机与容器 /app 挂载同一份 repo，
写完即可被宿主机侧的 daily_review.py 读到。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import duckdb


def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "main_oss.py").is_file():
            return p
    # 容器内用 docker cp 到 /tmp 跑时，向上找不到；回退到已知挂载根
    for cand in (Path("/app"), Path("/home/zbox/projects/quantmind")):
        if (cand / "backend" / "main_oss.py").is_file():
            return cand
    raise FileNotFoundError("未找到仓库根（含 backend/main_oss.py）")


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())

_HUNTLY_SQLITE = os.getenv("HUNTLY_SQLITE_PATH", "/data/huntly/db.sqlite")

# 与 report_match.py 同步的来源分级（白名单=预测力Top，黑名单=反向指标）
_GOLD_KW = ("财联社", "同花顺", "瓦斯", "界面", "东方财富", "东财")
_REVERSE_KW = ("南华早报", "创业邦", "彭博", "华尔街见闻", "雅虎", "链捕手",
               "路透", "人民日报", "商业 - 最新新闻")
_A_SHARE_SUF = (".SH", ".SZ", ".BJ")
# 富集 event_tags 里的泛分类噪声（非事件），展示时过滤掉
_NOISE_TAG_WORDS = ("产业", "概念", "市场", "省份", "城市", "国家", "地区", "宏观", "行业", "板块", "指数")


def _is_noise_tag(t: str) -> bool:
    return any(w in t for w in _NOISE_TAG_WORDS)


def _pg_conn():
    import psycopg2

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )


def _connector_names() -> dict[str, str]:
    con = sqlite3.connect(f"file:{_HUNTLY_SQLITE}?immutable=1", uri=True, timeout=3)
    try:
        return {str(r[0]): str(r[1] or "") for r in con.execute("SELECT id, name FROM connector")}
    finally:
        con.close()


def _source_tier(name: str) -> str:
    if any(k in name for k in _GOLD_KW):
        return "gold"
    if any(k in name for k in _REVERSE_KW):
        return "reverse"
    return "neutral"


def _hour_quality(published_at: str | None) -> str:
    if not published_at or len(published_at) < 13:
        return "normal"
    try:
        h = int(published_at[11:13])
    except (TypeError, ValueError):
        return "normal"
    if h in (19, 20, 21):
        return "gold"
    if h == 0 or h == 23 or 1 <= h <= 5:
        return "noise"
    return "normal"


def _load_instrument(data_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    detail = None
    for cand in (
        data_dir / "2_base_sector" / "instrument_detail" / "instrument_list.parquet",
        data_dir / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet",
    ):
        if cand.exists():
            detail = cand
            break
    if detail is None:
        return {}, {}
    con = duckdb.connect()
    df = con.execute(
        f"SELECT Symbol, Name, rs_hyname FROM read_parquet('{detail}')"
    ).fetchdf()
    names = dict(zip(df["Symbol"], df["Name"]))
    inds = dict(zip(df["Symbol"], df["rs_hyname"]))
    return names, inds


def _resolve_date(data_dir: Path, request: str | None) -> str:
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT max(dt) FROM read_parquet('{data_dir}/1_kline_data/daily_unadjusted/dt=*/data.parquet',"
        f" hive_partitioning=true)"
    ).fetchall()
    latest = str(rows[0][0]) if rows and rows[0][0] else ""
    if not latest:
        raise RuntimeError("daily_unadjusted 无数据")
    if not request:
        return latest
    request = request.replace("-", "")
    if request > latest:
        raise SystemExit(f"请求日期 {request} 晚于数据最新交易日 {latest}")
    rows = con.execute(
        f"SELECT max(dt) FROM read_parquet('{data_dir}/1_kline_data/daily_unadjusted/dt=*/data.parquet',"
        f" hive_partitioning=true) WHERE dt <= '{request}'"
    ).fetchall()
    if not rows or rows[0][0] is None:
        raise SystemExit(f"{request} 之前无交易日数据")
    return str(rows[0][0])


def main() -> None:
    ap = argparse.ArgumentParser(description="当日新闻情绪聚合")
    ap.add_argument("--date", help="交易日 YYYYMMDD，默认最新交易日")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--top", type=int, default=20, help="个股明细条数输出上限（json 全部保留）")
    args = ap.parse_args()

    if not Path(_HUNTLY_SQLITE).exists():
        raise SystemExit(f"Huntly SQLite 不存在：{_HUNTLY_SQLITE}（必须在容器内运行）")
    data_dir = Path(args.data_dir) if args.data_dir else (
        Path("/data/quantdb") if Path("/data/quantdb").is_dir() else _REPO_ROOT / "data" / "quantdb"
    )
    out_dir = Path(args.out_dir) if args.out_dir else (
        Path("/data/reports/daily_review") if Path("/data/quantdb").is_dir()
        else _REPO_ROOT / "data" / "reports" / "daily_review"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    trade_date = _resolve_date(data_dir, args.date)

    conn_names = _connector_names()

    # 1) 当日 Huntly 页面（updated_at 前缀匹配交易日，Shanghai 本地）
    con = sqlite3.connect(f"file:{_HUNTLY_SQLITE}?immutable=1", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, connector_id, title, updated_at FROM page WHERE updated_at LIKE ?",
        (f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}%",),
    ).fetchall()
    con.close()
    pages = {r["id"]: {
        "connector": str(r["connector_id"] or ""),
        "title": r["title"] or "",
        "updated_at": str(r["updated_at"] or "")[:19],
    } for r in rows}
    print(f"当日页面: {len(pages)} 篇")

    if not pages:
        json.dump({"date": trade_date, "n": 0, "stocks": [], "sector_focus": []},
                  open(out_dir / f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}_news.json", "w", encoding="utf-8"), ensure_ascii=False)
        print("当日无页面，空集会，退出")
        return

    # 2) PG 富集 join
    enrich: dict[int, dict] = {}
    ids = [int(i) for i in pages]
    try:
        pg = _pg_conn()
        with pg.cursor() as cur:
            cur.execute(
                "SELECT huntly_page_id, tickers, sentiment_label, sentiment_score, event_tags "
                "FROM news_article_enrichment WHERE huntly_page_id = ANY(%s)",
                (ids,),
            )
            for pid, tickers, label, score, tags in cur.fetchall():
                enrich[int(pid)] = {
                    "tickers": list(tickers or []),
                    "label": label,
                    "score": float(score) if score is not None else None,
                    "tags": list(tags or []),
                }
        pg.close()
    except Exception as exc:  # noqa: BLE001
        print(f"PG 富集读取失败（{exc}）；只有无情绪页面", file=sys.stderr)

    # 3) 逐股票聚合
    names, inds = _load_instrument(data_dir)
    stock: dict[str, dict] = {}
    gold_news = reverse_news = 0
    gold_hour_total = gold_hour_bullish = 0
    bull_total = bear_total = neutral_total = 0
    page_with_sent = 0
    for pid, p in pages.items():
        e = enrich.get(pid)
        if not e:
            continue
        label = e["label"]
        if label not in ("bullish", "bearish", "neutral"):
            label = "neutral"
        page_with_sent += 1
        if label == "bullish":
            bull_total += 1
        elif label == "bearish":
            bear_total += 1
        else:
            neutral_total += 1
        src_name = conn_names.get(p["connector"], p["connector"])
        tier = _source_tier(src_name)
        if tier == "gold":
            gold_news += 1
        elif tier == "reverse":
            reverse_news += 1
        if _hour_quality(p["updated_at"]) == "gold":
            gold_hour_total += 1
            if label == "bullish":
                gold_hour_bullish += 1
        seen_tags: set[str] = set()
        for t in e["tags"]:
            if isinstance(t, str) and "/" not in t and not _is_noise_tag(t):
                seen_tags.add(t)
        for sym in e["tickers"]:
            s = str(sym)
            if not s or not s.endswith(_A_SHARE_SUF):
                continue
            rec = stock.setdefault(s, {
                "symbol": s, "name": names.get(s, ""), "news_count": 0, "bullish": 0,
                "bearish": 0, "neutral": 0, "score_sum": 0.0, "tags": {}
            })
            rec["news_count"] += 1
            rec[label] = rec[label] + 1
            if e["score"] is not None:
                rec["score_sum"] += e["score"]
            for tag in seen_tags:
                rec["tags"][tag] = rec["tags"].get(tag, 0) + 1

    stock_list = sorted(
        ({**r, "net_ratio": round((r["bullish"] - r["bearish"]) / r["news_count"], 3),
            "score_mean": round(r["score_sum"] / r["news_count"], 3),
            "tags": [k for k, _ in sorted(r["tags"].items(), key=lambda kv: -kv[1])[:5]]}
         for r in stock.values()),
        key=lambda r: -r["news_count"],
    )
    names_map = {s["symbol"]: s["name"] for s in stock_list}

    # 4) 板块聚焦（按 申万一级）
    sec: dict[str, dict] = {}
    for r in stock_list:
        ind = inds.get(r["symbol"]) or "未知"
        g = sec.setdefault(ind, {"industry": ind, "n": 0, "bullish": 0, "bearish": 0, "news": 0})
        g["n"] += 1
        g["bullish"] += r["bullish"]
        g["bearish"] += r["bearish"]
        g["news"] += r["news_count"]
    sector_focus = sorted(
        ({**g, "net_ratio": round((g["bullish"] - g["bearish"]) / max(1, g["news"]), 3)} for g in sec.values()),
        key=lambda g: -g["news"],
    )

    # 5) 事件标签频次
    tag_freq: dict[str, int] = {}
    for r in stock_list:
        for t in r["tags"]:
            tag_freq[t] = tag_freq.get(t, 0) + 1
    top_tags = sorted(tag_freq.items(), key=lambda kv: -kv[1])[:10]

    total_news = bull_total + bear_total + neutral_total
    result = {
        "date": trade_date,
        "n": total_news,
        "page_total": len(pages),
        "stock_count": len(stock_list),
        "bullish": bull_total, "bearish": bear_total, "neutral": neutral_total,
        "net_ratio": round((bull_total - bear_total) / total_news, 3) if total_news else 0,
        "gold_news": gold_news, "reverse_news": reverse_news,
        "golden_hour_total": gold_hour_total, "golden_hour_bullish": gold_hour_bullish,
        "event_tags": [{"tag": k, "n": v} for k, v in top_tags],
        "sector_focus": sector_focus[: args.top],
        "stocks": stock_list[: args.top],
    }
    out = out_dir / f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}_news.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"新闻聚合 JSON: {out}")
    print(
        f"{trade_date}：命中 {total_news} 篇 / 股票 {len(stock_list)} 只；"
        f"利好 {bull_total} / 利空 {bear_total} / 中性 {neutral_total}（净情绪 {result['net_ratio']:+.0%}）；"
        f"高质量源 {gold_news} / 反向源 {reverse_news}"
    )
    if sector_focus:
        print("新闻聚焦板块 Top5：" + "、".join(
            f"{s['industry']}({s['n']}股,{s['net_ratio']:+.0%})" for s in sector_focus[:5]
        ))
    if top_tags:
        print("事件标签 Top5：" + "、".join(f"{k}×{v}" for k, v in top_tags[:5]))


if __name__ == "__main__":
    main()