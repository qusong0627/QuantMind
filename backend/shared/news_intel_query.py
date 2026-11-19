"""资讯中心 2.0 的纯查询逻辑（无 FastAPI / 无 DB 依赖，便于金样测试）。

路由层只负责取数与鉴权；这里的函数全部可单测。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from backend.shared.news_event_rules import classify_event, is_speculative, tier_for
from backend.shared.news_intel_rank import RankedArticle, assign_echo_layers, rank_article
from backend.shared.news_tagging import article_is_noise, subject_is_confirmed
from backend.shared.stock_utils import StockCodeUtil

_CN_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_HK_RE = re.compile(r"^\d{4,5}\.HK$")
_US_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

#: 出版方域名 = 该上市公司 / 媒体自己的稿子，不能算「被报道」。
#: 与 `news_tagging.SUBJECT_NAME_REQUIRED` / 研究口径 `publisher_conflict` 同源。
PUBLISHER_CONFLICTS: dict[str, tuple[str, ...]] = {
    "300033.SZ": ("10jqka",),
    "300059.SZ": ("eastmoney",),
    "603888.SH": ("xinhuanet", "news.cn"),
    "603000.SH": ("people.com.cn",),
    "000681.SZ": ("vcg",),
    "BIDU": ("baidu",),
    "9888.HK": ("baidu",),
    "9999.HK": ("163.com",),
    "JD": ("jd.com",),
    "PDD": ("pinduoduo",),
    "0700.HK": ("qq.com", "tencent"),
    "3690.HK": ("meituan",),
    "BABA": ("alibaba", "taobao"),
}

#: 高歧义实体：通用词/地名/媒体名。标题命中不代表这家上市公司是主体，
#: 只能当「提及」，不能据此打风险/决策（研究口径 §2.1 的 ambiguous_only）。
AMBIGUOUS_TICKERS: frozenset[str] = frozenset(
    {
        "300024.SZ",  # 机器人
        "000061.SZ",  # 农产品
        "603883.SH",  # 老百姓
        "600558.SH",  # 大西洋
        "000402.SZ",  # 金融街
        "601008.SH",  # 连云港
        "600663.SH",  # 陆家嘴
        "600295.SH",  # 鄂尔多斯
        "000591.SZ",  # 太阳能
        "300959.SZ",  # 线上线下
        "300785.SZ",  # 值得买
        "601099.SH",  # 太平洋
        "300832.SZ",  # 新产业
        "600100.SH",  # 同方股份（同方/清华同方歧义）
        "002095.SZ",  # 生意宝
    }
)

#: 期货页的品种词表（标题/关键词命中）。不做个股推理，只做新闻热度。
COMMODITY_THEMES: dict[str, tuple[str, ...]] = {
    "黄金": ("黄金", "金价", "沪金", "伦敦金", "COMEX"),
    "白银": ("白银", "银价", "沪银"),
    "原油": ("原油", "油价", "布伦特", "WTI"),
    "铜": ("铜价", "沪铜", "LME铜", "精铜"),
    "螺纹钢": ("螺纹钢", "钢材", "钢价"),
    "铁矿石": ("铁矿石", "铁矿"),
    "大豆": ("大豆", "豆粕", "豆油"),
    "玉米": ("玉米",),
    "生猪": ("生猪", "猪价"),
    "碳酸锂": ("碳酸锂", "锂价"),
    "煤炭": ("动力煤", "焦煤", "煤价"),
}


def market_of(code: str) -> str:
    """代码 → CN / HK / US / UNKNOWN。"""
    raw = str(code or "").strip().upper()
    if _CN_RE.match(raw):
        return "CN"
    if _HK_RE.match(raw):
        return "HK"
    if _US_RE.match(raw) and not raw.isdigit():
        return "US"
    if re.match(r"^\d{6}$", raw):
        return "CN"
    return "UNKNOWN"


def is_supported_symbol(code: str) -> bool:
    return market_of(code) != "UNKNOWN"


def publisher_conflict(code: str, domain: str | None) -> bool:
    text = str(domain or "").lower()
    if not text:
        return False
    return any(key in text for key in PUBLISHER_CONFLICTS.get(suffix(code), ()))

#: 「概念板块」里混着指数成分/风格/风险名单（MSCI 成份、近期新低、ST 板块…）。
#: 它们的热度来自成员多，不是题材本身；默认从雷达里去掉，避免把「低价股」当主线。
_STYLE_SECTOR = re.compile(
    r"重仓|成份|指数|新低|新高|低价股|ST板块|风险提示|低安全分|连续亏损|"
    r"昨日|情绪指数|破净资产|高应收款|破发行价|回购计划|高股息|"
    r"大盘股|中盘股|小盘股|微盘股|百元股|高价股|低价股|周期股|非周期股|含[HAB]股|GDR|AH股|AB股|"
    r"绩优股|成长股|价值股|超跌股|强势股|活跃股|热门股|龙头股|妖股|次新股|高送转|"
    r"融资融券|转融券|沪股通|深股通|转债标的|通达信\d+|"
    r"MSCI|富时|标普|证金|汇金|QFII|社保持仓|基金重仓|北向|机构重仓"
)


def suffix(code: str) -> str:
    raw = str(code or "").strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ")):
        return raw
    return StockCodeUtil.to_suffix(raw)


def bare(code: str) -> str:
    return suffix(code).split(".")[0]


def stock_name_from_row(row: dict[str, Any]) -> str:
    for key in ("stock_name", "name", "stock_code"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def entity_label(row: dict[str, Any], code: str) -> str | None:
    raw = row.get("entity_sentiments") or {}
    if not isinstance(raw, dict):
        return None
    value = raw.get(f"ticker:{suffix(code)}")
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num >= 0.25:
        return "bullish"
    if num <= -0.25:
        return "bearish"
    return "neutral"


def collapse_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一归一标题 6 小时内只保留首发（研究口径 §2.1 近重复折叠）。"""
    ordered = sorted(
        rows,
        key=lambda r: r.get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    assign_echo_layers(ordered)
    return [r for r in rows if not r.get("is_dup")]


def rank_rows(
    rows: list[dict[str, Any]],
    *,
    code: str,
    name: str,
    pre5: float | None,
) -> list[tuple[dict[str, Any], RankedArticle]]:
    """返回 ``(row, rank)``，顺序与输入一致；转载层在内部按时间升序计算。

    同题重复（6 小时内）直接丢弃——它们不是独立信源，留在列表里只会让
    「N 条新闻」看起来比实际更热。
    """
    ordered = sorted(
        rows,
        key=lambda r: r.get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
    assign_echo_layers(ordered)
    rows = [r for r in rows if not r.get("is_dup")]
    kept: list[dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title") or "")
        domain = str(row.get("source_domain") or "")
        if article_is_noise(title, domain):
            continue
        tier = tier_for(title, suffix(code), name)
        # 出版方域名只剔除 L3/L4（正文顺带提到），L1/L2 的公司自身新闻仍保留。
        if publisher_conflict(code, domain) and tier not in ("L1", "L2"):
            continue
        if not subject_is_confirmed(suffix(code), title):
            continue
        kept.append(row)
    rows = kept
    out: list[tuple[dict[str, Any], RankedArticle]] = []
    for row in rows:
        label = entity_label(row, code) or row.get("sentiment_label")
        out.append(
            (
                row,
                rank_article(
                    title=str(row.get("title") or ""),
                    code=suffix(code),
                    name=name,
                    event_tags=row.get("event_tags") or (),
                    sentiment_label=label,
                    source=str(row.get("source") or ""),
                    source_domain=str(row.get("source_domain") or ""),
                    published_at=row.get("published_at"),
                    pre5=pre5,
                    layer=int(row.get("layer") or 0),
                    is_dup=bool(row.get("is_dup")),
                    force_mention=suffix(code) in AMBIGUOUS_TICKERS,
                ),
            )
        )
    return out


def article_payload(row: dict[str, Any], rank: RankedArticle) -> dict[str, Any]:
    published = row.get("published_at")
    return {
        "page_id": int(row["page_id"]),
        "title": str(row.get("title") or ""),
        "url": row.get("url") or "",
        "source": row.get("source") or str(row.get("source_domain") or ""),
        "published_at": (
            published.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if isinstance(published, datetime)
            else None
        ),
        "sentiment_label": row.get("sentiment_label"),
        **rank.to_dict(),
    }


def read_rule(ranked: list[RankedArticle], pre5: float | None) -> str:
    """把研究规则翻译成一句人话（不是 LLM 生成，不随页面变化）。"""
    if any(r.decision == "P0" for r in ranked):
        return "避雷：硬利空 + pre5>5%，历史同格 T+1 回避（+186.8bp/事件，t=2.81）"
    if any(r.decision == "P2" for r in ranked):
        return "不追：利好 + pre5>5%，历史同格 5 日 -103bp；不加仓，不当卖出信号"
    if pre5 is not None and 0.02 < pre5 <= 0.05 and any(r.category == "硬利空" for r in ranked):
        return "观察：硬利空 + pre5 2~5%，样本内 -82bp 但不显著（p=0.106），只降权不动作"
    if any(r.decision == "WATCH" and r.category == "软利空" for r in ranked):
        return "不追空：软利空 + pre5 -2~5%，历史同格 +78~+133bp（反弹）"
    return "无强规则；按事件类型/来源再看"


def user_key(user: dict[str, Any]) -> tuple[str, str]:
    return str(user.get("tenant_id") or "default"), str(user.get("user_id") or "")


def _sector_keyword(name: str) -> str:
    """板块名 → 标题关键词：``DeepSeek概念`` → ``DeepSeek``；``行业板块(一级)`` → 原名。"""
    text = re.sub(r"[（(]?(一级|二级)[)）]?", "", str(name or ""))
    text = re.sub(r"(概念|板块|指数|行业)$", "", text).strip()
    return text or str(name or "")


class SectorIndex:
    """SectorCode/SectorName/SectorType/Symbol → 双向索引。"""

    def __init__(self, frame) -> None:
        self.by_symbol: dict[str, list[tuple[str, str, str]]] = {}
        self.by_code: dict[str, dict[str, Any]] = {}
        for rec in frame.itertuples(index=False):
            symbol = suffix(str(getattr(rec, "Symbol", "") or ""))
            code = str(getattr(rec, "SectorCode", "") or "")
            name = str(getattr(rec, "SectorName", "") or "")
            raw_type = str(getattr(rec, "SectorType", "") or "")
            if not symbol or not code:
                continue
            kind = (
                "concept" if "概念" in raw_type
                else "industry1" if "一级" in raw_type
                else "industry2" if "二级" in raw_type
                else "other"
            )
            self.by_symbol.setdefault(symbol, []).append((kind, code, name))
            entry = self.by_code.get(code)
            if entry is None:
                entry = {
                    "code": code,
                    "name": name,
                    "type": kind,
                    "symbols": [],
                    "style": bool(_STYLE_SECTOR.search(name)),
                    "keyword": _sector_keyword(name),
                }
                self.by_code[code] = entry
            entry["symbols"].append(symbol)


def aggregate_sectors(
    rows: list[dict[str, Any]],
    index: SectorIndex,
    *,
    match_by_title: bool = True,
) -> list[dict[str, Any]]:
    """板块热度。

    ``match_by_title=True`` 时按「标题命中板块名」归属——这是研究口径
    （``board_day_agg.n_title_hits``）。按成分股 tickers 归属会把
    「江西宜春：脱胎漆器…」这种只要提到某只成分股的稿子算进「人工智能」，
    实测噪声远大于信号。没有 title 的行才退回成员映射。
    """
    keyword_map = [
        (code, entry["keyword"])
        for code, entry in index.by_code.items()
        if not entry.get("style") and len(entry["keyword"]) >= 2
    ]
    stats: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[int]] = {}
    for row in rows:
        tags = row.get("event_tags") or []
        category, direction, _ = classify_event("", tags, str(row.get("sentiment_label") or ""))
        risk = category == "硬利空"
        spec = is_speculative("", tags)
        page_id = int(row["page_id"])
        title = str(row.get("title") or "")
        if match_by_title and title:
            codes = {code for code, keyword in keyword_map if keyword in title}
        else:
            codes = {
                code
                for ticker in row.get("tickers") or []
                for _kind, code, _name in index.by_symbol.get(suffix(str(ticker)), ())
            }
        for code in codes:
            entry = index.by_code.get(code)
            if entry is None:
                continue
            item = stats.get(code)
            if item is None:
                item = {
                    "code": code,
                    "name": entry["name"],
                    "type": entry["type"],
                    "style": bool(entry.get("style")),
                    "n_articles": 0,
                    "n_positive": 0,
                    "n_negative": 0,
                    "n_risk": 0,
                    "n_spec": 0,
                    "sent_mean": 0.0,
                    "_sent_n": 0,
                }
                stats[code] = item
                seen[code] = set()
            if page_id not in seen[code]:
                seen[code].add(page_id)
                item["n_articles"] += 1
            if direction == "利好":
                item["n_positive"] += 1
            elif direction == "利空":
                item["n_negative"] += 1
            if risk:
                item["n_risk"] += 1
            if spec:
                item["n_spec"] += 1
            score = row.get("sentiment_score")
            if isinstance(score, (int, float)):
                item["sent_mean"] += float(score)
                item["_sent_n"] += 1
    out: list[dict[str, Any]] = []
    for item in stats.values():
        n = max(1, item["n_articles"])
        out.append(
            {
                **{k: v for k, v in item.items() if not k.startswith("_")},
                "neg_ratio": round(item["n_negative"] / n, 3),
                "risk_ratio": round(item["n_risk"] / n, 3),
                "spec_ratio": round(item["n_spec"] / n, 3),
                "sent_mean": (
                    round(item["sent_mean"] / item["_sent_n"], 3) if item["_sent_n"] else None
                ),
            }
        )
    # 先按新闻热度（篇数），再按风险数；风险密度在右侧读数里单独展示。
    out.sort(key=lambda x: (x["n_articles"], x["n_risk"]), reverse=True)
    return out


def matches_commodity(row: dict[str, Any]) -> bool:
    # 个股稿（如「中金黄金董事长离任」）不是商品新闻；期货页只看商品/宏观。
    if any(str(t).endswith((".SH", ".SZ", ".BJ")) for t in (row.get("tickers") or [])):
        return False
    text = str(row.get("title") or "") + " " + " ".join(
        str(t) for t in (row.get("key_terms") or [])
    )
    return any(word in text for words in COMMODITY_THEMES.values() for word in words)


#: key_terms 里的通用词：不是题材，进主题榜只会占位。
_GENERIC_TERMS = frozenset(
    {
        "净利润", "营收", "营业收入", "同比增长", "下降", "上涨", "下跌", "亿元",
        "公司", "市场", "股价", "涨停", "跌停", "A股", "港股", "美股", "创业板",
        "科创板", "IPO", "分红", "业绩", "财报", "减持", "增持", "回购", "订单",
        "成交额", "成交量", "每股收益", "市盈率", "市净率", "换手率", "振幅",
        "市值", "板块", "概念", "指数", "基金", "期货", "期权", "债券", "汇率",
        "利率", "政策", "数据", "报告", "证券", "银行", "保险",
        "利润率", "净利率", "毛利率", "主力资金", "流动性", "融资余额",
        "CPI", "GDP", "PPI", "PMI", "社融", "估值", "现金流", "负债率",
        "股息率", "北向资金", "南向资金", "环比增长", "经济数据", "行情",
    }
)


def _topic_direction(row: dict[str, Any]) -> tuple[str, str]:
    category, direction, _ = classify_event(
        "", row.get("event_tags") or (), str(row.get("sentiment_label") or "")
    )
    return category, direction


def aggregate_topics(rows: list[dict[str, Any]], *, limit: int = 40) -> list[dict[str, Any]]:
    """港股/美股：按 enrichment ``key_terms`` 聚合主题。

    只做展示口径：direction 用事件标签优先、情绪兜底；风险只认硬利空。
    """
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        terms = {
            str(t).strip()
            for t in (row.get("key_terms") or [])
            if str(t).strip() and str(t).strip() not in _GENERIC_TERMS
        }
        if not terms:
            continue
        category, direction = _topic_direction(row)
        risk = category == "硬利空"
        page_id = int(row["page_id"])
        for term in terms:
            item = stats.get(term)
            if item is None:
                item = {
                    "name": term,
                    "n_articles": 0,
                    "n_positive": 0,
                    "n_negative": 0,
                    "n_risk": 0,
                    "_seen": set(),
                    "_tickers": {},
                }
                stats[term] = item
            if page_id not in item["_seen"]:
                item["_seen"].add(page_id)
                item["n_articles"] += 1
            if direction == "利好":
                item["n_positive"] += 1
            elif direction == "利空":
                item["n_negative"] += 1
            if risk:
                item["n_risk"] += 1
            for ticker in row.get("tickers") or []:
                code = suffix(str(ticker))
                item["_tickers"][code] = item["_tickers"].get(code, 0) + 1
    out: list[dict[str, Any]] = []
    for item in stats.values():
        if item["n_articles"] < 3:
            continue
        top = sorted(item["_tickers"].items(), key=lambda kv: kv[1], reverse=True)[:6]
        out.append(
            {
                "name": item["name"],
                "n_articles": item["n_articles"],
                "n_positive": item["n_positive"],
                "n_negative": item["n_negative"],
                "n_risk": item["n_risk"],
                "top_tickers": [{"code": c, "n": n} for c, n in top],
            }
        )
    out.sort(key=lambda x: (x["n_articles"], x["n_risk"]), reverse=True)
    return out[:limit]


def aggregate_commodities(
    rows: list[dict[str, Any]], *, limit: int = 30
) -> list[dict[str, Any]]:
    """期货页：按品种词表命中标题/关键词，聚合新闻热度。"""
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        if any(str(t).endswith((".SH", ".SZ", ".BJ")) for t in (row.get("tickers") or [])):
            continue
        text = str(row.get("title") or "") + " " + " ".join(
            str(t) for t in (row.get("key_terms") or [])
        )
        matched = [
            name
            for name, words in COMMODITY_THEMES.items()
            if any(word in text for word in words)
        ]
        if not matched:
            continue
        category, direction = _topic_direction(row)
        risk = category == "硬利空"
        page_id = int(row["page_id"])
        for name in matched:
            item = stats.get(name)
            if item is None:
                item = {
                    "name": name,
                    "n_articles": 0,
                    "n_positive": 0,
                    "n_negative": 0,
                    "n_risk": 0,
                    "_seen": set(),
                }
                stats[name] = item
            if page_id not in item["_seen"]:
                item["_seen"].add(page_id)
                item["n_articles"] += 1
            if direction == "利好":
                item["n_positive"] += 1
            elif direction == "利空":
                item["n_negative"] += 1
            if risk:
                item["n_risk"] += 1
    out = [
        {
            "name": item["name"],
            "n_articles": item["n_articles"],
            "n_positive": item["n_positive"],
            "n_negative": item["n_negative"],
            "n_risk": item["n_risk"],
        }
        for item in stats.values()
        if item["n_articles"] >= 3
    ]
    out.sort(key=lambda x: (x["n_articles"], x["n_risk"]), reverse=True)
    return out[:limit]
