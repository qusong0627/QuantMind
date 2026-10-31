#!/usr/bin/env python3
"""个股深度研究数据包 — 全部走 QuantMind 本地数据（QuantDB + PG 新闻富集 + Huntly）。

一次取全：个股行情/技术指标/估值/L2资金流/财务/行业归属 + 市场背景（指数/行业涨幅/
板块资金流，复用市场分析聚合口径）+ 新闻情绪（FinBERT 富集 + Huntly 原文时间来源）。

用法（容器内跑，.claude 未挂载进容器）:
  docker cp skills/stock-research/scripts/research_data.py quantmind:/tmp/
  docker exec quantmind python3 /tmp/research_data.py --symbol 600036.SH --days 120

输出:
  /data/reports/stock_research/{symbol}_{date}.json  完整数据包（AI 分析依据）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/app")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("stock_research")

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub  # noqa: E402
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402

HUNTLY_DB = "/data/huntly/db.sqlite"
OUT_DIR = Path(os.getenv("QM_REPORTS_DIR", "/data/reports")) / "stock_research"


def _hub() -> QuantDBDataHub:
    return QuantDBDataHub.get_instance()


def _normalize(symbol: str) -> str:
    """任意输入 → 后缀格式（600036.SH）。"""
    s = symbol.strip()
    if "." not in s and s[:2] not in ("SH", "SZ", "BJ"):
        s = StockCodeUtil.to_suffix(f"SH{s}" if s.startswith("6") else f"SZ{s}")
    else:
        s = StockCodeUtil.to_suffix(s)
    return s


def fetch_quote(hub, symbol: str, days: int = 120) -> dict:
    """日K（前复权）+ 最新涨跌。"""
    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 10)
    df = hub.fetch_daily_kline(symbol, start, end, adjust="qfq")
    if df.empty:
        return {"ok": False, "reason": "无日K数据"}
    df = df.sort_values("trade_date")
    closes = df["close"].tolist()
    last = float(closes[-1])
    prev = float(closes[-2]) if len(closes) > 1 else last
    return {
        "ok": True,
        "latest_date": str(df["trade_date"].iloc[-1])[:10],
        "last_close": round(last, 2),
        "pct_change": round((last / prev - 1) * 100, 2) if prev else 0.0,
        "kline_60d": [
            {
                "date": str(r["trade_date"])[:10],
                "open": round(float(r["open"]), 2), "high": round(float(r["high"]), 2),
                "low": round(float(r["low"]), 2), "close": round(float(r["close"]), 2),
                "volume": int(r.get("volume") or 0), "amount_yi": round(float(r.get("amount") or 0) / 1e4, 2),
            }
            for r in df.tail(60).to_dict("records")
        ],
        "high_60d": round(float(df["high"].tail(60).max()), 2),
        "low_60d": round(float(df["low"].tail(60).min()), 2),
        "chg_20d": round((last / float(closes[-21]) - 1) * 100, 2) if len(closes) > 20 else None,
        "chg_60d": round((last / float(closes[-61]) - 1) * 100, 2) if len(closes) > 60 else None,
    }


def fetch_indicators(hub, symbol: str) -> dict:
    """技术指标（最新日）。注意：该数据集 close/ma* 为后复权价位，与 K 线前复权
    不一致，故只输出量纲无关指标（rsi/kdj/macd/乖离率/量能/波动率），
    绝对价位与均线一律由 quote.kline 计算，避免误导。"""
    df = hub.fetch_technical_indicators(symbol)
    if df.empty:
        return {}
    df = df.sort_values("dt")
    latest = df.iloc[-1]
    # 后复权绝对价位字段（与 quote 前复权错位，剔除）
    skip = {"symbol", "dt", "time", "close", "ma5", "ma10", "ma20", "ma60", "close_20d"}
    out = {}
    for c in df.columns:
        if c in skip:
            continue
        v = latest[c]
        try:
            out[c] = round(float(v), 3) if v == v and v is not None else None
        except (TypeError, ValueError):
            out[c] = None
    return out


def fetch_valuation(hub, symbol: str) -> dict:
    df = hub.fetch_valuation(symbol)
    if df.empty:
        return {}
    latest = df.sort_values("dt").iloc[-1]
    out = {}
    for c in ("pe_ttm", "pe_static", "pb", "ps_ttm", "total_mv", "float_mv", "net_profit_ttm", "revenue_ttm"):
        if c in latest.index:
            v = latest[c]
            try:
                out[c] = round(float(v), 2) if v == v and v is not None else None
            except (TypeError, ValueError):
                out[c] = None
    return out


def fetch_l2_flow(hub, symbol: str, days: int = 10) -> dict:
    """L2 主力资金流（近 days 日 + 汇总）。"""
    end = date.today()
    start = end - timedelta(days=days * 2 + 5)
    try:
        df = hub.fetch_l1_l2_factors(start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        log.warning("l2 读取失败: %s", exc)
        return {}
    if df.empty:
        return {}
    df = df[df["symbol"] == symbol].sort_values("dt")
    if df.empty:
        return {}
    rows = []
    for r in df.tail(days).to_dict("records"):
        rows.append({
            "date": str(r["dt"])[:8],
            "net_yi": round(float(r.get("flow_net_amount") or 0) / 1e8, 3),
            "super_yi": round(float(r.get("flow_super_net") or 0) / 1e8, 3),
            "large_yi": round(float(r.get("flow_large_net") or 0) / 1e8, 3),
            "main_ratio": round(float(r.get("flow_net_ratio") or 0), 2),
        })
    net_5 = sum(x["net_yi"] for x in rows[-5:])
    net_10 = sum(x["net_yi"] for x in rows)
    return {"daily": rows, "net_5d_yi": round(net_5, 2), "net_10d_yi": round(net_10, 2)}


def fetch_financials(hub, symbol: str) -> dict:
    """财务三表核心科目（最新报告期，m_timetag 报告期）。"""
    out = {}
    for ds in ("income", "balance", "cashflow"):
        try:
            df = hub.fetch_financial(symbol, statement_type=ds)
        except Exception as exc:  # noqa: BLE001
            log.warning("财务 %s 读取失败: %s", ds, exc)
            continue
        if df.empty:
            continue
        latest = df.sort_values("m_timetag").iloc[-1]
        rec = {str(c): _safe_num(v) for c, v in latest.items()
               if c not in ("symbol", "Symbol", "m_timetag", "m_anntime", "m_quarter")}
        out[ds] = {"report_date": str(latest.get("m_timetag", ""))[:10], "values": rec}
    return out


def _safe_num(v):
    try:
        f = float(v)
        return round(f, 2) if f == f else None
    except (TypeError, ValueError):
        return str(v)[:40] if v is not None else None


def fetch_sector(hub, symbol: str) -> dict:
    """行业归属（CSRC 一级行业，instrument_detail）。"""
    try:
        df = hub.fetch_instrument_industry()
    except Exception:  # noqa: BLE001
        return {}
    if df.empty:
        return {}
    row = df[df["symbol"].astype(str) == symbol]
    if row.empty:
        return {}
    r = row.iloc[0]
    return {
        "ind_name_l1": str(r.get("ind_name_l1", "") or ""),
        "ind_code_l1": int(r.get("ind_code_l1", -1)) if r.get("ind_code_l1") == r.get("ind_code_l1") else None,
    }


# ---------------- 市场背景（复用市场分析聚合） ----------------

def fetch_market_context(symbol: str) -> dict:
    """指数/行业涨幅/板块资金流（quantdb_feed 聚合口径，与市场分析报告一致）。"""
    try:
        from backend.services.api.market_analysis import quantdb_feed as qf
    except Exception as exc:  # noqa: BLE001
        log.warning("市场背景不可用: %s", exc)
        return {}
    try:
        qf.clear_cache()
        indices = qf.get_indices_overview()
        sw = qf.get_sector_heatmap("shenwan")
        f1 = qf.get_money_flow_period("1d", "sector", "shenwan", 31)
        f5 = qf.get_money_flow_period("5d", "sector", "shenwan", 31)
        f10 = qf.get_money_flow_period("10d", "sector", "shenwan", 31)
    except Exception as exc:  # noqa: BLE001
        log.warning("市场背景聚合失败: %s", exc)
        return {}
    # 找到个股所属行业（通过 sector_members）
    my_sector = None
    try:
        members = qf._sector_members()
        sub = members[members["symbol"] == symbol]
        if sub.empty:
            sub = members[members["symbol"] == StockCodeUtil.to_prefix(symbol)]
        if not sub.empty:
            my_sector = str(sub.iloc[0]["sector_name"])
    except Exception:  # noqa: BLE001
        pass
    return {
        "indices": [
            {"name": i["name"], "pct_change": i["pct_change"], "turnover_yi": i["turnover"]}
            for i in indices
        ],
        "my_sector": my_sector,
        "sector_rank": sorted(sw, key=lambda x: x["pct_change"], reverse=True),
        "sector_flow_1d": f1,
        "sector_flow_5d": f5,
        "sector_flow_10d": f10,
    }


# ---------------- 模型推理分数（engine_signal_scores） ----------------

def fetch_model_score(symbol: str) -> dict:
    """最新推理 run 中目标股票的模型分数（融合/轻量/TFT + 信号方向/预期价/分位）。"""
    try:
        import asyncpg
    except ImportError:
        return {"ok": False, "reason": "asyncpg 不可用"}

    async def _query():
        conn = await asyncpg.connect(
            host=os.getenv("DB_HOST", "quantmind-db"), port=int(os.getenv("DB_PORT", "5432")),
            user=os.getenv("DB_USER", "quantmind"), password=os.getenv("DB_PASSWORD", "quantmind123"),
            database=os.getenv("DB_NAME", "quantmind"), timeout=10,
        )
        try:
            run = await conn.fetchrow(
                "SELECT run_id, model_id, data_trade_date, prediction_trade_date "
                "FROM qm_model_inference_runs WHERE status='completed' "
                "ORDER BY prediction_trade_date DESC NULLS LAST LIMIT 1"
            )
            if run is None:
                return {}
            num = symbol.split(".")[0]  # engine_signal_scores.symbol 纯数字
            row = await conn.fetchrow(
                "SELECT light_score, tft_score, fusion_score, signal_side, expected_price, "
                "       regime, score_rank, trade_date "
                "FROM engine_signal_scores WHERE run_id=$1 AND symbol=$2",
                run["run_id"], num,
            )
            if row is None:
                return {"run": dict(run), "found": False}
            # 全市场分数分位（score_rank 为该 run 内排名）
            total = await conn.fetchval(
                "SELECT count(*) FROM engine_signal_scores WHERE run_id=$1", run["run_id"]
            )
            return {
                "run": {
                    "run_id": run["run_id"],
                    "model_id": run["model_id"],
                    "data_trade_date": str(run["data_trade_date"]),
                    "prediction_trade_date": str(run["prediction_trade_date"]),
                },
                "found": True,
                "light_score": round(float(row["light_score"]), 4) if row["light_score"] is not None else None,
                "tft_score": round(float(row["tft_score"]), 4) if row["tft_score"] is not None else None,
                "fusion_score": round(float(row["fusion_score"]), 4) if row["fusion_score"] is not None else None,
                "signal_side": row["signal_side"],
                "expected_price": round(float(row["expected_price"]), 2) if row["expected_price"] else None,
                "regime": row["regime"],
                "score_rank": int(row["score_rank"]) if row["score_rank"] is not None else None,
                "rank_pct": round(int(row["score_rank"]) / max(int(total), 1) * 100, 1) if row["score_rank"] else None,
                "universe_size": int(total),
            }
        finally:
            await conn.close()

    try:
        import asyncio

        return asyncio.run(_query())
    except Exception as exc:  # noqa: BLE001
        log.warning("模型分数查询失败: %s", exc)
        return {"ok": False, "reason": str(exc)[:100]}


# ---------------- 新闻情绪（PG 富集 + Huntly 原文） ----------------

def fetch_news(symbol: str, days: int = 60) -> dict:
    """个股新闻情绪：PG news_article_enrichment（FinBERT）→ Huntly 原文时间/来源。"""
    try:
        import asyncpg
    except ImportError:
        return {"ok": False, "reason": "asyncpg 不可用"}

    async def _query():
        conn = await asyncpg.connect(
            host=os.getenv("DB_HOST", "quantmind-db"), port=int(os.getenv("DB_PORT", "5432")),
            user=os.getenv("DB_USER", "quantmind"), password=os.getenv("DB_PASSWORD", "quantmind123"),
            database=os.getenv("DB_NAME", "quantmind"), timeout=10,
        )
        try:
            rows = await conn.fetch(
                """
                SELECT huntly_page_id, sentiment_label, sentiment_score, sentiment_confidence,
                       event_tags, industries, title
                FROM news_article_enrichment
                WHERE $1 = ANY(tickers)
                  AND sentiment_label IN ('bullish', 'bearish')
                ORDER BY enriched_at DESC LIMIT 300
                """,
                symbol,
            )
        finally:
            await conn.close()
        return rows

    try:
        import asyncio

        rows = asyncio.run(_query())
    except Exception as exc:  # noqa: BLE001
        log.warning("新闻富集查询失败: %s", exc)
        return {"ok": False, "reason": str(exc)[:100]}

    if not rows:
        return {"ok": True, "events": [], "stats": {}}

    # Huntly 原文（时间/来源）
    page = {}
    db_path = Path(HUNTLY_DB)
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            ids = [int(r["huntly_page_id"]) for r in rows if r["huntly_page_id"]]
            ph = ",".join("?" for _ in ids)
            for r in conn.execute(
                f"SELECT p.id, p.connected_at, p.title, c.name AS source_name "
                f"FROM page p LEFT JOIN connector c ON c.id = p.connector_id "
                f"WHERE p.id IN ({ph})",
                ids,
            ):
                page[int(r["id"])] = {
                    "time": r["connected_at"],
                    "title": r["title"],
                    "source": r["source_name"] or "unknown",
                }
            conn.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Huntly 读取失败: %s", exc)

    # 组装事件（按时间倒序，截断到 days 天）
    events = []
    for r in rows:
        pid = int(r["huntly_page_id"])
        pinfo = page.get(pid, {})
        t = pinfo.get("time") or ""
        d = t[:10] if t else None
        if d and d < (date.today() - timedelta(days=days)).strftime("%Y-%m-%d"):
            continue
        events.append({
            "date": d or "",
            "source": pinfo.get("source", "unknown"),
            "title": r["title"] or pinfo.get("title") or "",
            "label": r["sentiment_label"],
            "score": round(float(r["sentiment_score"] or 0), 3),
            "confidence": round(float(r["sentiment_confidence"] or 0), 2) if r["sentiment_confidence"] else None,
            "tags": list(r["event_tags"] or []),
            "industries": list(r["industries"] or [])[:3],
        })
    events.sort(key=lambda x: x["date"], reverse=True)

    bullish = [e for e in events if e["label"] == "bullish"]
    bearish = [e for e in events if e["label"] == "bearish"]
    from collections import Counter

    tag_counter = Counter(t for e in events for t in e["tags"])
    src_counter = Counter(e["source"] for e in events)

    # 新闻类型聚合（按 event_tags 关键词归类，利好/利空分布）
    _TYPE_RULES = [
        ("业绩财报", ("财报", "业绩", "净利", "营收", "盈利", "利润", "中报", "年报")),
        ("产业景气", ("产业", "行业", "概念", "景气", "出货", "产能", "订单")),
        ("政策监管", ("政策", "监管", "补贴", "规划", "发改委", "工信部")),
        ("资本运作", ("减持", "增持", "回购", "融资", "定增", "入股", "收购", "股权")),
        ("合作签约", ("战略合作", "合作", "签约", "共建", "协议")),
        ("价格成本", ("涨价", "降价", "价格", "成本", "原材料")),
        ("技术产品", ("技术", "产品", "发布", "量产", "研发", "新品")),
    ]
    type_dist = []
    for tname, kws in _TYPE_RULES:
        hit = [e for e in events if any(k in t for t in e["tags"] for k in kws)]
        if not hit:
            continue
        type_dist.append({
            "type": tname,
            "total": len(hit),
            "bullish": sum(1 for e in hit if e["label"] == "bullish"),
            "bearish": sum(1 for e in hit if e["label"] == "bearish"),
        })

    stats = {
        "total": len(events),
        "bullish": len(bullish),
        "bearish": len(bearish),
        "bull_ratio": round(len(bullish) / max(len(events), 1) * 100, 1),
        "avg_score_bull": round(sum(e["score"] for e in bullish) / max(len(bullish), 1), 3),
        "avg_score_bear": round(sum(e["score"] for e in bearish) / max(len(bearish), 1), 3),
        "top_tags": [{"tag": k, "count": v} for k, v in tag_counter.most_common(8)],
        "top_sources": [{"source": k, "count": v} for k, v in src_counter.most_common(5)],
        "type_dist": type_dist,
    }
    return {"ok": True, "events": events[:30], "stats": stats}


def main() -> int:
    ap = argparse.ArgumentParser(description="个股深度研究数据包")
    ap.add_argument("--symbol", required=True, help="股票代码（600036 / SH600036 / 600036.SH / 招商银行）")
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()

    symbol = _normalize(args.symbol)
    hub = _hub()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "input": args.symbol.strip(),
        "symbol": symbol,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quote": fetch_quote(hub, symbol, args.days),
        "indicators": fetch_indicators(hub, symbol),
        "valuation": fetch_valuation(hub, symbol),
        "l2_flow": fetch_l2_flow(hub, symbol),
        "financials": fetch_financials(hub, symbol),
        "sector": fetch_sector(hub, symbol),
        "market_context": fetch_market_context(symbol),
        "news": fetch_news(symbol),
        "model_score": fetch_model_score(symbol),
    }
    out_path = OUT_DIR / f"{symbol.replace('.', '_')}_{date.today():%Y%m%d}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(json.dumps({
        "symbol": symbol,
        "out": str(out_path),
        "quote": data["quote"].get("ok"),
        "last_close": data["quote"].get("last_close"),
        "pct_change": data["quote"].get("pct_change"),
        "valuation": bool(data["valuation"]),
        "l2": bool(data["l2_flow"]),
        "financials": list(data["financials"].keys()),
        "sector": data["sector"],
        "news": data["news"].get("stats", {}),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
