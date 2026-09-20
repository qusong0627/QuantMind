"""候选列表的风险排除与新闻标注（通道 A 名单 / 通道 B 近 20 天新闻）。

`/stock-terminal/list` 的 handler 已经很长，这里把「名单怎么读、新闻怎么取、
排除几只看哪里」收到一处，好处是这层可以脱离 FastAPI 直接单测：

- :func:`list_channel` —— 通道 A：用户基线的「不买入」名单（`data/exclusions/cn.json`）。
- :func:`news_risk_channel` —— 通道 B：近 20 天新闻里 direction=risk 的标的（默认排除）。
- :func:`news_annotations` —— 逐行标注：这只票近 20 天有哪些利空/利好标签。

**名单缺失 ≠ 空名单**（沿用 `exclusion_list` 的纪律）：文件不在盘或损坏时返回空集合
但把 ``imported=False`` 一起交出去，调用方必须显式显示「名单未导入」。静默当空名单
就是假证据——界面一切正常，实际一只都没排掉。
"""

from __future__ import annotations

import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

import pandas as pd

from backend.shared.logging_config import get_logger
from backend.shared.news_tagging import ALL_DIRECTIONS, DIR_RISK

logger = get_logger(__name__)

#: 单市场名单（港股/美股各有自己的数据层，本模块只服务 A 股候选列表）
CN_MARKET = "CN"

#: 新闻利空集合的进程内缓存 TTL。rollup 半小时一跑，60s 足够让新标签及时生效，
#: 又不至于每次翻页都去查一次表（列表是滚动翻页的高频接口）。
_NEWS_TTL = 60.0
_news_cache: dict[str, Any] = {"by_symbol": None, "ts": 0.0}

#: 逐行标注里带证据标题的方向。只有 risk 需要——它是唯一会拦买的档，
#: 用户必须能一眼看到「凭什么说它利空」；其余四档只给标签名与条数（100 行 ×
#: 5 档 × 3 条标题会把列表响应撑大一个数量级，而列表是滚动翻页的高频接口）。
_EVIDENCE_DIRECTIONS = frozenset({DIR_RISK})


def list_channel(
    *, today: str | None = None
) -> tuple[Any | None, frozenset[str], dict[str, Any]]:
    """通道 A：名单只读视图 + 当前**有效**阻断集合 + 元信息。

    元信息永远带 ``imported`` 键——``False`` 表示名单没读到，此时集合为空，
    调用方**必须**把它显示出来（不能把「没名单」渲染成「已按名单过滤」）。
    """
    from backend.shared.exclusion_list import load_exclusion_list

    today = today or date.today().isoformat()
    lst = load_exclusion_list(CN_MARKET)
    if lst is None:
        return (
            None,
            frozenset(),
            {
                "imported": False,
                "reason": "名单文件未导入（data/exclusions/cn.json）",
            },
        )
    return lst, lst.symbols(today=today), lst.meta(today=today)


async def news_risk_channel() -> tuple[frozenset[str], dict[str, Any]]:
    """通道 B：近 20 天新闻里 direction=risk 的标的集合 + 元信息。"""
    by_symbol, meta = await _news_by_symbol()
    return risk_symbols(by_symbol), meta


def risk_symbols(by_symbol: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """从分桶结果里挑出**有 risk 档标签**的标的（纯函数，便于盯口径）。

    只认 ``direction='risk'`` 一档——``weak``（业绩类）与 ``warn``（减持解禁）
    只标注不排除，把它们也拉进来会让「默认排除」变成噪声判决（见 `news_tagging`）。

    注意判据是「risk 桶非空」，**不是**「这只票出现在标签表里」：表里绝大多数行
    是涨停/大涨这类行情标签（近 20 天命中全市场约 39%），拿「有标签」当排除判据
    会一口气废掉三分之一的市场，而且表面上看不出错。
    """
    return frozenset(s for s, buckets in by_symbol.items() if buckets.get(DIR_RISK))


async def news_annotations(
    symbols: Sequence[str], *, by_symbol: Mapping[str, Any] | None = None
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """逐行标注：本页标的 → 各方向的标签桶（空桶保留，前端按固定桶渲染）。

    只输出本页（默认 100 只）；数据源是 `_news_by_symbol` 的全量缓存，
    筛选判据与标注同源，不会出现「界面标了利空却没被排除」。
    """
    wanted = {str(s) for s in symbols if s}
    if not wanted:
        return {}
    if by_symbol is None:
        by_symbol, _ = await _news_by_symbol()
    return {s: by_symbol[s] for s in wanted if s in by_symbol}


async def _news_by_symbol() -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]], dict[str, Any]
]:
    """全量标签按标的分桶（带 TTL 缓存）。"""
    now = time.time()
    cached = _news_cache["by_symbol"]
    if cached is not None and now - float(_news_cache["ts"]) < _NEWS_TTL:
        return cached, _news_cache["meta"]

    from backend.shared.news_tag_contract import (
        load_all_stock_tags,
        load_stock_tags_meta,
    )

    meta = await load_stock_tags_meta()
    raw = await load_all_stock_tags() if meta.get("available") else {}
    # rollup 没跑过时 meta 自带 available=False + reason，列表照常返回（只是没有标注），
    # 不假装「全市场无利空」
    out = {
        symbol: {
            direction: [_slim_tag(t, direction) for t in buckets.get(direction, [])]
            for direction in ALL_DIRECTIONS
        }
        for symbol, buckets in raw.items()
    }
    _news_cache.update({"by_symbol": out, "ts": now, "meta": meta})
    return out, meta


def _slim_tag(tag: Mapping[str, Any], direction: str) -> dict[str, Any]:
    """标签行 → 列表载荷（risk 带证据标题，其余只带标签与条数）。"""
    slim: dict[str, Any] = {
        "tag": tag.get("tag"),
        "n": int(tag.get("n") or 0),
        "last": tag.get("last"),
    }
    if direction in _EVIDENCE_DIRECTIONS:
        slim["samples"] = list(tag.get("samples") or [])[:3]
    return slim


def apply_exclusions(
    df: pd.DataFrame,
    *,
    blocked: Iterable[str] = (),
    news_risk: Iterable[str] = (),
    exclude_risk_list: bool,
    exclude_news_risk: bool,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """按两个通道过滤候选表。返回 ``(新 df, {risk_list, news_risk} 实际排除只数)``。

    纯函数（不读文件/不查库），两个通道的**交集只算一次**：同一只票同时上名单又上新闻时，
    各自计数仍然各记一笔（用户要看的是「每个通道各排掉多少」），但表只被削减一次。
    """
    counts = {"risk_list": 0, "news_risk": 0}
    if df.empty:
        return df, counts
    codes = _suffix_codes(df)

    blocked_set = frozenset(blocked)
    news_set = frozenset(news_risk)
    enabled = [
        (exclude_risk_list, blocked_set, "risk_list"),
        (exclude_news_risk, news_set, "news_risk"),
    ]
    for on, syms, key in enabled:
        if not on or not syms:
            continue
        counts[key] = int(codes.isin(syms).sum())

    drop: set[str] = set()
    if exclude_risk_list:
        drop |= blocked_set
    if exclude_news_risk:
        drop |= news_set
    if not drop:
        return df, counts
    return df[~codes.isin(drop)], counts


def _suffix_codes(df: pd.DataFrame) -> pd.Series:
    """取后缀式代码列。

    行情层口径是后缀式（``600036.SH``），名单与新闻标签也一律后缀式——同口径比对，
    不做归一（`StockCodeUtil.to_prefix` 往返一次既慢又会在不认识的码上原样返回）。
    """
    return df["Symbol"].astype(str)


def row_risk(
    symbol: str,
    *,
    lst: Any | None,
    blocked: Collection[str] = (),
    news: Mapping[str, list[dict[str, Any]]] | None = None,
    today: str | None = None,
) -> dict[str, Any] | None:
    """单行的风险载荷（无任何命中/标注时返回 ``None``，前端据此不渲染徽章区）。

    ``lst`` 是 ``ExclusionList``（也可以传 ``None`` = 名单未导入）；
    ``blocked`` 是它的 ``symbols()`` 结果，由调用方算一次后复用（逐行重建集合很浪费）。

    ``today`` **必须能落到具体日期**：``explain()`` 收到 ``None`` 时不做到期判定
    （``expired`` 恒为 False），窗口已过的条目会被当成有效命中渲染成「名单命中」
    徽章——而它其实并不在被排除的集合里（``symbols()`` 会滤掉过期项），
    于是界面出现「标了命中却没被排除」的自相矛盾。
    """
    ref = today or date.today().isoformat()
    payload: dict[str, Any] = {}
    blocking_hit = False

    if lst is not None:
        hit = lst.explain(symbol, today=ref)
        if hit is not None and not hit.expired:
            payload["hits"] = [hit.as_dict()]
            blocking_hit = hit.blocking
        else:
            hit = None

    buckets = {d: list((news or {}).get(d) or []) for d in ALL_DIRECTIONS}
    if any(buckets.values()):
        payload["news"] = buckets

    if not payload:
        return None
    # `excluded` 说的是「命中默认排除判据」，与开关是否打开无关——
    # 关掉开关时前端仍要把这些行标出来（用户按需自行判断，而不是眼不见为净）
    payload["excluded"] = symbol in blocked or blocking_hit or bool(buckets[DIR_RISK])
    return payload


def exclusion_meta(
    *,
    list_meta: Mapping[str, Any],
    news_meta: Mapping[str, Any],
    counts: Mapping[str, int],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """响应级元信息：两条通道各自的基准/新鲜度 + 本轮实际排除只数。

    前端拿它渲染「已排除 N 只（ST / 名单 / 新闻）」与陈旧警示——
    只减不说的列表会让用户以为数据丢了。
    """
    meta: dict[str, Any] = {
        "list": dict(list_meta),
        "news": dict(news_meta),
        "excluded": dict(counts),
    }
    if extra:
        meta.update(extra)
    return meta
