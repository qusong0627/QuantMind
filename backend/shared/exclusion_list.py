"""候选信号排除名单（通道 A：用户基线名单）——构建 + 只读消费。

**这是什么**：用户手上那份「不买入」的名单的物化产物。源在隔壁 quant-Trader
（`data/fundamental_flags.json` 1606 只 / `data/risk_block.json` 1708 只 /
`data/news_blacklist_2026.json` 586 只 / `configs/live_symbols.json` 的 block_buy 55 只），
由**宿主侧**导入器 `backend/scripts/import_exclusion_list.py` 读成一份归一化 JSON
落到 `data/exclusions/<market>.json`（`./data` 已挂进容器），容器侧只读本模块。

**为什么是「离线生成 + 落盘」**：隔壁自己的纪律就是「只读已落盘的产物，不重算任何判据，
保证报表与闸门同源」（`exclusion_report.py`）；而本仓 `/list` 是单 worker uvicorn，
实时重算 1600 只的多层基本面判据会阻塞全部并发请求。所以这里只做一次 `set.isin`。

**两条不能破的纪律**：

1. **后缀归一**。源文件里 `fundamental_flags`/`risk_block` 是 6 位裸码，而过滤侧
   `df["Symbol"]` 是后缀式（`002622.SZ`）；不归一就是**静默查空**——一只都排不掉，
   界面上却一切正常。
2. **名单缺失 ≠ 空名单**。文件不在盘或损坏一律返回 ``None``，调用方必须显式显示
   「名单未导入」；静默当成「没有风险股」就是假证据。

**排除 vs 只告警**：隔壁对「质押比例」明写「只告警不拦买」、对「监管关注（问询/监管函）」
明写「只提醒不禁买，当因子看」。本模块沿用该分层——`:data:`BLOCKING_SOURCES` 参与排除，
`:data:`WARN_SOURCES` 只进标注，绝不静默升级成拦买。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

#: 名单产物目录（容器内 ``/data/exclusions``；宿主侧即 ``quantmind/data/exclusions``）
EXCLUSION_DIR_ENV = "QM_EXCLUSION_DIR"
DEFAULT_EXCLUSION_DIR = "/data/exclusions"

#: 源 → 是否参与「排除」。其余进 :data:`WARN_SOURCES`，只做标注。
#: ``user_manual`` 是用户在界面上手工加的票——与机器源**同等效力**（用户按自己的
#: 纪律排除，不该因为我们它是手写的就降级成「只提示」）。
BLOCKING_SOURCES: frozenset[str] = frozenset(
    {"fundamental_flags", "risk_block", "news_blacklist", "block_buy", "user_manual"}
)
#: 源 → 只标注不拦买（隔壁口径：质押只告警、监管关注只提醒）
WARN_SOURCES: frozenset[str] = frozenset({"risk_block_warn", "risk_block_watch"})

#: 用户层两个来源名（``exclusion_overlay`` 写入同一套字面量，两处必须一致）。
#: 定义在这里是因为**读侧要用它们做判定**（放行到期后要回落到机器判据），
#: 而写侧只是把同样的字符串写进 JSON。
SOURCE_MANUAL = "user_manual"
SOURCE_ALLOW = "user_allow"

#: 基准日超过这个天数就在界面上提示「名单该刷新了」（隔壁是每日刷新，本仓导入是手动一步）
STALE_WARN_DAYS = 14

_SOURCE_LABELS: dict[str, str] = {
    "fundamental_flags": "基本面长期排除名单",
    "risk_block": "事件风险清单",
    "news_blacklist": "年内新闻黑名单",
    "block_buy": "操作员黑名单",
    "risk_block_warn": "质押告警（只提示）",
    "risk_block_watch": "监管关注（只提示）",
    "user_manual": "手工排除（本人在个人中心添加）",
    "user_allow": "手工放行（本人在个人中心解除）",
}


def source_label(source: str) -> str:
    """源的中文显示名（前端徽章/下钻用）。未知源原样返回。"""
    return _SOURCE_LABELS.get(source, source)


def _today_iso() -> str:
    return date.today().isoformat()


def _norm_symbol(code: Any) -> str:
    """裸码/前缀式/后缀式一律归到后缀式（过滤侧口径）。空/无法识别返回空串。"""
    text = str(code or "").strip()
    if not text:
        return ""
    return StockCodeUtil.to_suffix(text) or ""


def _clean_flags(raw: Any) -> list[str]:
    """``kind`` 里的复合值（``penny+unlock+weak``）拆成单标签；去重稳定排序。"""
    out: set[str] = set()
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    for value in values:
        for part in str(value or "").split("+"):
            token = part.strip()
            if token:
                out.add(token)
    return sorted(out)


def _collect(
    raw: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """把四份源文档摊平成 ``({symbol: [逐源命中]}, {源: 基准日})``（纯归集，不做合并判断）。

    源名 = **策略名**而非文件名：``live_symbols.json`` 里只有 ``block_buy`` 这一条
    禁买策略（``allow_st`` 是全局开关、不是逐票来源），故源名取 ``block_buy``，
    与 ``items[*].sources`` 同一套命名——两处名字不一致的计数表迟早对不上。
    """
    hits: dict[str, list[dict[str, Any]]] = {}
    source_asof: dict[str, str] = {}

    def _add(symbol: Any, entry: dict[str, Any]) -> None:
        norm = _norm_symbol(symbol)
        if norm:
            hits.setdefault(norm, []).append(entry)

    fundamental = raw.get("fundamental_flags") or {}
    source_asof["fundamental_flags"] = str(fundamental.get("asof") or "").strip()
    for code, item in (fundamental.get("items") or {}).items():
        item = item if isinstance(item, dict) else {}
        _add(
            code,
            {
                "source": "fundamental_flags",
                "flags": _clean_flags(item.get("flags")),
                "reason": str(item.get("reason") or "").strip(),
                "expire": None,
            },
        )

    risk_block = raw.get("risk_block") or {}
    source_asof["risk_block"] = str(risk_block.get("asof") or "").strip()
    source_asof["risk_block_warn"] = source_asof["risk_block"]
    source_asof["risk_block_watch"] = str(
        risk_block.get("watch_asof") or risk_block.get("asof") or ""
    ).strip()
    for code, item in (risk_block.get("items") or {}).items():
        item = item if isinstance(item, dict) else {}
        _add(
            code,
            {
                "source": "risk_block",
                "flags": _clean_flags(item.get("kind")),
                "reason": str(item.get("reason") or "").strip(),
                "expire": str(item.get("expire") or "").strip() or None,
            },
        )
    # 质押比例：隔壁明写「只告警不拦买」
    for code, reason in (risk_block.get("warns") or {}).items():
        _add(
            code,
            {
                "source": "risk_block_warn",
                "flags": ["pledge"],
                "reason": str(reason or "").strip(),
                "expire": None,
            },
        )
    # 监管关注（问询函/监管函/警示函）：隔壁明写「只提醒不禁买，当因子看」
    for code, item in (risk_block.get("watch") or {}).items():
        item = item if isinstance(item, dict) else {}
        _add(
            code,
            {
                "source": "risk_block_watch",
                "flags": _clean_flags(item.get("kind")),
                "reason": str(item.get("reason") or "").strip(),
                "expire": str(item.get("until") or "").strip() or None,
            },
        )

    news = raw.get("news_blacklist") or {}
    # 年度累计名单：``until`` 是它的覆盖终点，比 ``since`` 更接近「基准日」
    source_asof["news_blacklist"] = str(
        news.get("until") or news.get("asof") or ""
    ).strip()
    for item in news.get("items") or []:
        item = item if isinstance(item, dict) else {}
        near = _clean_flags(item.get("near_cats")) or _clean_flags(item.get("cats"))
        window = ""
        if item.get("first") or item.get("last"):
            window = f"（{item.get('first') or '?'}~{item.get('last') or '?'}）"
        _add(
            item.get("code"),
            {
                "source": "news_blacklist",
                "flags": near,
                "reason": f"{item.get('name') or ''} 年内负面新闻 {item.get('n') or 0} 条{window}".strip(),
                "expire": None,
            },
        )

    live = raw.get("live_symbols") or {}
    source_asof["block_buy"] = str(live.get("asof") or "").strip()
    for code in live.get("block_buy") or []:
        _add(
            code,
            {
                "source": "block_buy",
                "flags": ["operator"],
                "reason": "操作员黑名单（人工复核后禁止买入）",
                "expire": None,
            },
        )

    return hits, source_asof


def _merge_reasons(reasons: list[str]) -> list[str]:
    """多个来源的理由 → 去重后的分句列表（**按分句**去重，不是按整段）。

    隔壁两份源文档经常各自带上同一句话：实测 ``600606.SH`` 的
    ``fundamental_flags`` 写「连续3年亏损（2023-2025）；资产负债率92%（高杠杆）；多年阴跌…」，
    ``risk_block`` 又原样重复这三句、前面再加自己那句「股价1.32元低于2.0元预警线…」。
    整段比对去不掉（两段文字并不相同），合并后同一句话出现两遍，读起来像系统复读，
    在表格里还会把真正独有的那句挤到看不见的地方。

    ``by_source`` 里各源的原话一字不动 —— 逐源明细要能对得上源文件，只有合并展示去重。
    """
    out: list[str] = []
    seen: set[str] = set()
    for reason in reasons or []:
        for clause in str(reason or "").split("；"):
            c = clause.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _merge(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """逐源命中 → 单条合并记录。

    ``expire`` 取「全来源都带窗口时的最晚者」；只要有一个永久来源就为 ``None``
    （否则永久命中会被另一个来源的临时窗口提前解除）。
    """
    ordered = sorted(entries, key=lambda e: e["source"])
    by_source = {
        e["source"]: {
            "flags": list(e["flags"]),
            "reason": e["reason"],
            "expire": e["expire"],
        }
        for e in ordered
    }
    flags = _clean_flags([f for e in ordered for f in e["flags"]])
    reasons = _merge_reasons([e["reason"] for e in ordered])
    expires = [e["expire"] for e in ordered]
    expire = max(expires) if all(expires) else None
    sources = [e["source"] for e in ordered]
    return {
        "sources": sources,
        "flags": flags,
        "reason": "；".join(reasons),
        "expire": expire,
        "blocking": any(s in BLOCKING_SOURCES for s in sources),
        "by_source": by_source,
    }


def build_payload(raw: Mapping[str, Any], *, generated_at: str) -> dict[str, Any]:
    """四份源文档 → 落盘载荷（纯函数；导入器与测试共用）。

    ``raw`` 取键 ``fundamental_flags`` / ``risk_block`` / ``news_blacklist`` /
    ``live_symbols``（缺失即视为该源为空）。产出结构::

        {"market", "asof", "generated_at",
         "sources": {name: {"asof", "count", "counts_blocking"}},
         "counts": {"total", "blocking", "by_source"},
         "items": {symbol: {"sources", "flags", "reason", "expire",
                            "blocking", "by_source"}}}
    """
    hits, source_asof = _collect(raw)
    items: dict[str, dict[str, Any]] = {}
    by_source: dict[str, int] = {}
    for symbol, entries in hits.items():
        items[symbol] = _merge(entries)
        for entry in entries:
            by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1

    sources = {
        name: {
            "asof": source_asof.get(name, ""),
            "count": count,
            "blocking": name in BLOCKING_SOURCES,
            "label": source_label(name),
        }
        for name, count in sorted(by_source.items())
    }

    blocking = sum(1 for it in items.values() if it["blocking"])
    return {
        "market": str(raw.get("market") or "CN").upper(),
        "asof": max(source_asof.values(), default=""),
        "generated_at": generated_at,
        "sources": sources,
        "counts": {"total": len(items), "blocking": blocking, "by_source": by_source},
        "items": items,
    }


@dataclass(frozen=True)
class ExclusionHit:
    """单只票的命中说明。``expired=True`` 表示窗口已过（不再排除，但仍可解释）。"""

    symbol: str
    sources: tuple[str, ...]
    flags: tuple[str, ...]
    reason: str
    expire: str | None
    blocking: bool
    by_source: Mapping[str, Mapping[str, Any]]
    expired: bool = False

    def as_dict(self) -> dict[str, Any]:
        """API 输出形态（前端直接吃）。"""
        return {
            "symbol": self.symbol,
            "sources": list(self.sources),
            "source_labels": [source_label(s) for s in self.sources],
            "flags": list(self.flags),
            "reason": self.reason,
            "expire": self.expire,
            "blocking": self.blocking,
            "expired": self.expired,
            "by_source": {
                k: {
                    "flags": list(v.get("flags") or []),
                    "reason": v.get("reason") or "",
                    "expire": v.get("expire"),
                    "label": source_label(k),
                }
                for k, v in self.by_source.items()
            },
        }


@dataclass(frozen=True)
class ExclusionList:
    """名单只读视图（不可变；``explain``/``symbols`` 都返回新对象或新集合）。"""

    market: str
    asof: str
    generated_at: str
    sources: Mapping[str, Mapping[str, Any]]
    counts: Mapping[str, int]
    items: Mapping[str, ExclusionHit]
    #: 用户层摘要（``{updated_at, manual, allow, allow_miss}``）；无手工条目时为 ``None``。
    #: 挂在只读视图上而不是让调用方自己再读一次文件：读一次算一次，两次读之间文件可能
    #: 已经变了，界面上就会出现「条数说了 3 条、表里只有 2 行」。
    overlay: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ExclusionList:
        items: dict[str, ExclusionHit] = {}
        for symbol, raw in (payload.get("items") or {}).items():
            raw = raw if isinstance(raw, dict) else {}
            items[str(symbol)] = ExclusionHit(
                symbol=str(symbol),
                sources=tuple(raw.get("sources") or ()),
                flags=tuple(raw.get("flags") or ()),
                reason=str(raw.get("reason") or ""),
                expire=(str(raw["expire"]) if raw.get("expire") else None),
                blocking=bool(raw.get("blocking", True)),
                by_source=raw.get("by_source") or {},
            )
        return cls(
            market=str(payload.get("market") or "CN").upper(),
            asof=str(payload.get("asof") or ""),
            generated_at=str(payload.get("generated_at") or ""),
            sources=payload.get("sources") or {},
            counts=payload.get("counts") or {},
            items=items,
            overlay=payload.get("overlay") or None,
        )

    def explain(self, symbol: str, *, today: str | None = None) -> ExclusionHit | None:
        """命中说明（含已过期项）。未命中返回 ``None``。"""
        hit = self.items.get(_norm_symbol(symbol))
        if hit is None:
            return None
        return hit if today is None else self._with_expiry(hit, today)

    def symbols(self, *, today: str | None = None) -> frozenset[str]:
        """当前**有效**的排除集合（只含 blocking 且未过期）——过滤侧直接用。"""
        ref = today or _today_iso()
        return frozenset(
            sym
            for sym, hit in self.items.items()
            if _effective_blocking(hit, ref) and not _is_expired(hit, ref)
        )

    def warn_hits(self, symbol: str, *, today: str | None = None) -> list[ExclusionHit]:
        """只告警不拦买的命中（质押/监管关注），供 UI 标注。"""
        hit = self.explain(symbol, today=today)
        if hit is None or hit.blocking:
            return []
        return [hit]

    def meta(self, *, today: str | None = None) -> dict[str, Any]:
        """运维元信息：基准日、陈旧天数、各源条数、是否已导入。"""
        ref = today or _today_iso()
        stale = _stale_days(self.asof, ref)
        return {
            "imported": True,
            "market": self.market,
            "asof": self.asof,
            "generated_at": self.generated_at,
            "stale_days": stale,
            "stale": stale is not None and stale > STALE_WARN_DAYS,
            "counts": dict(self.counts),
            "sources": {k: dict(v) for k, v in self.sources.items()},
            "blocking_now": len(self.symbols(today=ref)),
            "overlay": dict(self.overlay) if self.overlay else None,
        }

    def _with_expiry(self, hit: ExclusionHit, today: str) -> ExclusionHit:
        return ExclusionHit(
            symbol=hit.symbol,
            sources=hit.sources,
            flags=hit.flags,
            reason=hit.reason,
            expire=hit.expire,
            blocking=_effective_blocking(hit, today),
            by_source=hit.by_source,
            expired=_is_expired(hit, today),
        )


def _is_expired(hit: ExclusionHit, today: str) -> bool:
    """``expire`` 为 ``None`` 即永久；否则当天仍算有效（``expire >= today``）。"""
    if not hit.expire:
        return False
    return hit.expire < today


def _effective_blocking(hit: ExclusionHit, today: str) -> bool:
    """在 ``hit.blocking`` 之上再判一次**用户放行窗口**。

    用户层的 ``allow`` 把条目的 ``blocking`` 翻成 ``False``，但那个覆盖本身可以带
    到期日：``allow expire=2026-09-01`` 的意思是「9/1 之前可以买」。窗口一过必须
    **自动回到机器判据**（这只票本来就还在机器名单上），而不是等用户回去再点一次
    「取消放行」——他会忘，而忘掉的代价是买进一只他自己明令不买的票。

    判据放在读侧（``today`` 逐次传入）而不是合并时烘焙进载荷：服务进程可能连跑数天，
    烘焙的结果会在跨日那一刻静默过期失效——正是「配置改了但没生效」那类最难查的故障。
    """
    allow = (hit.by_source or {}).get(SOURCE_ALLOW)
    if not allow:
        return hit.blocking
    expire = allow.get("expire")
    if expire and str(expire) < today:
        return True
    return False


def _stale_days(asof: str, today: str) -> int | None:
    """基准日距今天数；asof 不可解析返回 ``None``（不猜）。"""
    try:
        return (date.fromisoformat(today) - date.fromisoformat(asof)).days
    except (TypeError, ValueError):
        return None


def exclusion_root(root: Path | None = None) -> Path:
    """产物目录：显式入参 > ``QM_EXCLUSION_DIR`` > 默认 ``/data/exclusions``。"""
    if root is not None:
        return Path(root)
    return Path(os.getenv(EXCLUSION_DIR_ENV, "") or DEFAULT_EXCLUSION_DIR)


def load_exclusion_list(
    market: str = "CN", *, root: Path | None = None, use_cache: bool = True
) -> ExclusionList | None:
    """读名单产物。**缺失/损坏一律返回 ``None``**（调用方必须显式显示「未导入」）。"""
    if use_cache:
        return _load_cached(str(exclusion_root(root)), str(market).upper())
    return _load_uncached(exclusion_root(root), market)


def _load_uncached(root: Path, market: str) -> ExclusionList | None:
    path = root / f"{str(market).lower()}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 半份名单比没有名单更危险：损坏按「未导入」处理并留痕
        logger.warning("[ExclusionList] 名单不可读，按未导入处理 %s: %s", path, exc)
        return None
    if not isinstance(payload, dict) or "items" not in payload:
        logger.warning("[ExclusionList] 名单结构不符，按未导入处理 %s", path)
        return None
    return ExclusionList.from_payload(_with_overlay(payload, market, root))


def _with_overlay(
    payload: Mapping[str, Any], market: str, root: Path
) -> dict[str, Any]:
    """叠上用户层（手工增删）。用户层读不到就是「还没手工加过」，不影响机器名单。

    **机器名单存在但用户层损坏**时只丢手工条目、保留机器基线——两个文件是独立故障域，
    让一份坏文件把另一份一起拖成「未导入」会把「名单坏了」与「我想加的那只没加上」
    混成同一个提示，用户没法从中判断该修哪个。
    """
    from backend.shared.exclusion_overlay import load_overlay, merge_into_payload

    overlay = load_overlay(market, root=root)
    if not overlay.entries:
        return dict(payload)
    merged, stats = merge_into_payload(payload, overlay)
    logger.debug(
        "[ExclusionList] 用户层合并 %s：手工 %d / 放行 %d / 放行未命中 %d",
        market,
        stats["manual"],
        stats["allow"],
        stats["allow_miss"],
    )
    return merged


_CACHE: dict[str, ExclusionList | None] = {}


def _stamp(path: Path) -> str:
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return "missing"


def _load_cached(root: str, market: str) -> ExclusionList | None:
    """进程内缓存。

    名单是**每日刷新**的产物、不是热点数据，故按 ``(root, market, 两个文件的 mtime)``
    缓存：mtime 变了自动失效（导入器覆盖写改 ``cn.json``、个人中心改 ``cn_user.json``），
    不需要 TTL 轮询。

    **两个 mtime 都要进键**：只盯机器名单的话，用户在个人中心加了票、下一次拉列表
    却还是旧结果——而他刚刚在界面上看到了「已保存」。
    """
    base = Path(root)
    key = f"{root}|{market}|{_stamp(base / f'{market.lower()}.json')}|{_stamp(base / f'{market.lower()}_user.json')}"
    if key not in _CACHE:
        _CACHE.clear()  # 只保留当前版本，避免导入多次后缓存无界增长
        _CACHE[key] = _load_uncached(base, market)
    return _CACHE[key]


def clear_cache() -> None:
    """测试/导入后手动失效。"""
    _CACHE.clear()


def now_iso() -> str:
    """带 Z 的 aware UTC 时间戳（与平台瞬时时间口径一致）。"""
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
