"""排除名单的**用户层**（手工增删）——与机器基线分层存储、读时合并。

**为什么必须是两份文件**：`data/exclusions/cn.json` 是隔壁 quant-Trader 四份产物的
物化结果，由 `backend/scripts/import_exclusion_list.py` **整份覆盖写**。任何直接写进
那份文件的编辑，都会在下一次导入时被无声抹掉——用户在前端加的票第二天就没了，
而且导入日志一切正常。所以手工条目单独落 `cn_user.json`，导入器从不碰它，
读取侧（:func:`merge_into_payload`）把两层合起来。

**两个动作，一个表**：

- ``block`` 手工加入不买入名单（机器名单没有的票，或机器名单已有再加一条人工理由）
- ``allow`` 例外放行（机器名单命中了、但用户决定这只可以买）——放行是**显式**的：
  条目仍在名单里、理由仍可见，只是 ``blocking=False``，界面上标「已放行」而不是消失。
  让它消失会把「我决定放行」与「它本来就不在名单里」混成同一种状态。

**写盘纪律**：临时文件 + ``os.replace`` 原子替换（同目录内 POSIX 保证原子），
外层再套 ``fcntl.flock`` 排他锁 —— api 服务在 OSS 单容器里可能是多进程，
两个请求同时改名单时「后写覆盖先写」会静默丢条目。锁文件是 ``.lock`` 兄弟文件，
不参与内容读取。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from collections.abc import Iterator

from backend.shared.logging_config import get_logger
from backend.shared.stock_utils import StockCodeUtil

logger = get_logger(__name__)

#: 动作取值（其余一律拒绝，不猜）
ACTION_BLOCK = "block"
ACTION_ALLOW = "allow"
ACTIONS: frozenset[str] = frozenset({ACTION_BLOCK, ACTION_ALLOW})

ACTION_LABELS = {
    ACTION_BLOCK: "手工排除",
    ACTION_ALLOW: "例外放行",
}

#: 源名（合并进 :data:`backend.shared.exclusion_list.BLOCKING_SOURCES` 口径用）
SOURCE_MANUAL = "user_manual"
SOURCE_ALLOW = "user_allow"

#: 手工条目上限。名单是给人看的表，不是数据库；超过这个量级说明用错了地方
#: （该走导入器），拦住以免把 1 MB 的 JSON 写成 100 MB 拖垮每次列表请求。
MAX_ENTRIES = 5000

#: 用户层文件后缀（``cn.json`` → ``cn_user.json``）
_SUFFIX = "_user"

#: 进程内缓存（按 mtime 失效，与 exclusion_list 同一套纪律）
_CACHE: dict[str, tuple[str, Overlay]] = {}


class OverlayError(ValueError):
    """用户层读写错误（**可展示给用户**：全部是输入问题，不是内部故障）。"""


@dataclass(frozen=True)
class OverlayEntry:
    """一条手工条目。``expire`` 为 ``None`` 即永久（``>= expire`` 当天仍有效）。"""

    symbol: str
    action: str
    reason: str
    note: str
    expire: str | None
    operator: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "action_label": ACTION_LABELS.get(self.action, self.action),
            "reason": self.reason,
            "note": self.note,
            "expire": self.expire,
            "operator": self.operator,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_raw(cls, symbol: str, raw: Mapping[str, Any]) -> OverlayEntry:
        action = str(raw.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise OverlayError(f"{symbol}: 动作非法 {action!r}（只接受 block / allow）")
        return cls(
            symbol=symbol,
            action=action,
            reason=str(raw.get("reason") or "").strip(),
            note=str(raw.get("note") or "").strip(),
            expire=_clean_date(raw.get("expire"), symbol),
            operator=str(raw.get("operator") or "").strip(),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


@dataclass(frozen=True)
class Overlay:
    """用户层只读视图。``entries`` 的键是**后缀式**代码（与过滤侧同口径）。"""

    market: str
    updated_at: str
    entries: Mapping[str, OverlayEntry]

    def blocks(self) -> dict[str, OverlayEntry]:
        return {s: e for s, e in self.entries.items() if e.action == ACTION_BLOCK}

    def allows(self) -> dict[str, OverlayEntry]:
        return {s: e for s, e in self.entries.items() if e.action == ACTION_ALLOW}

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "updated_at": self.updated_at,
            "counts": {
                "total": len(self.entries),
                "block": len(self.blocks()),
                "allow": len(self.allows()),
            },
            "items": [
                e.as_dict()
                for e in sorted(self.entries.values(), key=lambda x: x.symbol)
            ],
        }


def _clean_date(raw: Any, symbol: str) -> str | None:
    """``expire`` 只接受 ``YYYY-MM-DD`` 或空；不可解析即报错（不静默当永久）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise OverlayError(
            f"{symbol}: 到期日不可解析 {text!r}（须 YYYY-MM-DD）"
        ) from exc
    return text


def overlay_path(market: str = "CN", *, root: Path | None = None) -> Path:
    """用户层文件路径：``<root>/<market>_user.json``。"""
    from backend.shared.exclusion_list import exclusion_root

    base = exclusion_root(root)
    return base / f"{str(market).lower()}{_SUFFIX}.json"


def _empty(market: str) -> Overlay:
    return Overlay(market=str(market).upper(), updated_at="", entries={})


def load_overlay(
    market: str = "CN", *, root: Path | None = None, use_cache: bool = True
) -> Overlay:
    """读用户层。

    **文件不存在不是错误**——它是「还没手工加过任何票」的正常初始态，返回空 Overlay
    （与 ``load_exclusion_list`` 的「缺失 = None = 必须显式显示未导入」不同：
    机器名单缺失会让用户以为过滤生效了，而用户层为空就是字面意义上的空）。
    文件**损坏**则记日志并按空处理，同时 (:meth:`Overlay.as_dict`) 不会谎报条数——
    调用方拿到的 total 就是 0，而界面上「手工条目」区块本来就是从这份数据渲染的。
    """
    market_key = str(market).upper()
    path = overlay_path(market_key, root=root)
    if not use_cache:
        return _load_uncached(path, market_key)
    try:
        stamp = str(path.stat().st_mtime_ns)
    except OSError:
        stamp = "missing"
    cached = _CACHE.get(market_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    loaded = _load_uncached(path, market_key)
    _CACHE[market_key] = (stamp, loaded)
    return loaded


def _load_uncached(path: Path, market: str) -> Overlay:
    if not path.is_file():
        return _empty(market)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[ExclusionOverlay] 用户层不可读，按空处理 %s: %s", path, exc)
        return _empty(market)
    if not isinstance(payload, dict):
        logger.warning("[ExclusionOverlay] 用户层结构不符，按空处理 %s", path)
        return _empty(market)

    entries: dict[str, OverlayEntry] = {}
    for code, raw in (payload.get("items") or {}).items():
        symbol = _norm_symbol(code)
        if not symbol:
            logger.warning("[ExclusionOverlay] 跳过无法识别的代码 %r", code)
            continue
        try:
            entries[symbol] = OverlayEntry.from_raw(
                symbol, raw if isinstance(raw, dict) else {}
            )
        except OverlayError as exc:
            # 单条坏数据不该让整份用户层消失——但必须留痕，否则用户会以为编辑没保存
            logger.warning("[ExclusionOverlay] 跳过非法条目 %s", exc)
    return Overlay(
        market=str(payload.get("market") or market).upper(),
        updated_at=str(payload.get("updated_at") or ""),
        entries=entries,
    )


#: 归一到后缀式之后必须长成这个形状，否则拒绝写入。
#: ``StockCodeUtil.to_suffix`` 对认不出的输入**原样返回**（``不是代码`` → ``不是代码``），
#: 只判「非空」的话，用户手抖输错一位、或界面传了个中文，就会落一条永远匹配不上任何
#: 行情的记录——名单上看得见、候选列表里排不掉，而且没有任何报错。
_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def _norm_symbol(code: Any) -> str:
    """任意形态代码 → 后缀式；**不是合法 A 股代码就是空串**（调用方据此拒绝）。

    与机器名单同一归一规则（否则两层键形不同、交集恒空），但多一道形状校验：
    机器那边的输入由导入器保证，这边的输入来自人。
    """
    text = str(code or "").strip()
    if not text:
        return ""
    normalized = StockCodeUtil.to_suffix(text) or ""
    return normalized if _SYMBOL_RE.match(normalized) else ""


def clear_cache() -> None:
    """测试/写入后手动失效（写入方自己失效；其他进程靠 mtime 自动失效）。"""
    _CACHE.clear()


# ---------------------------------------------------------------- 写入


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """排他锁（``flock``）。取不到 ``fcntl``（非 POSIX）就退化成无锁——
    OSS 部署是 Linux 容器，退化分支只为可移植，不改变语义。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):  # pragma: no cover - 非 POSIX 平台
            logger.debug("[ExclusionOverlay] 无文件锁可用，按单写者假设继续")
        yield
    finally:
        handle.close()


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """临时文件 + ``os.replace`` —— 半份 JSON 比没有 JSON 更危险（读侧会整份丢弃）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp_excl_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp 一律建 0600，而 os.replace 会把这份权限原样搬给目标 —— 不显式放开，
        # 用户层就成了 root 独占（实测 /data/exclusions/cn_user.json 为 -rw-------），
        # 运维在宿主侧既读不到也备份不了。对齐机器名单 cn.json 的 0644。
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _dump(entries: Mapping[str, OverlayEntry], market: str) -> dict[str, Any]:
    from backend.shared.exclusion_list import now_iso

    return {
        "market": market,
        "version": 1,
        "updated_at": now_iso(),
        "items": {
            symbol: {
                "action": entry.action,
                "reason": entry.reason,
                "note": entry.note,
                "expire": entry.expire,
                "operator": entry.operator,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
            }
            for symbol, entry in sorted(entries.items())
        },
    }


def upsert_entry(
    symbol: str,
    *,
    action: str,
    reason: str = "",
    note: str = "",
    expire: str | None = None,
    operator: str = "",
    market: str = "CN",
    root: Path | None = None,
) -> OverlayEntry:
    """新增或覆盖一条手工条目（同一代码重复提交即改判，不产生第二条）。

    ``allow`` 与 ``block`` 是同一张表的两种取值：同一只票从「放行」改成「排除」
    就是一次 upsert，不留历史——用户看到的永远只有当前状态，避免「两条互相矛盾的
    条目谁生效」这种没有正确答案的问题。
    """
    from backend.shared.exclusion_list import now_iso

    normalized = _norm_symbol(symbol)
    if not normalized:
        raise OverlayError(f"无法识别的股票代码：{symbol!r}")

    entry = OverlayEntry(
        symbol=normalized,
        action=str(action or "").strip().lower(),
        reason=str(reason or "").strip(),
        note=str(note or "").strip(),
        expire=_clean_date(expire, normalized),
        operator=str(operator or "").strip(),
        created_at="",
        updated_at=now_iso(),
    )
    if entry.action not in ACTIONS:
        raise OverlayError(f"动作非法 {action!r}（只接受 block / allow）")

    path = overlay_path(market, root=root)
    with _exclusive(path):
        current = _load_uncached(path, str(market).upper())
        previous = current.entries.get(normalized)
        if previous is None and len(current.entries) >= MAX_ENTRIES:
            raise OverlayError(
                f"手工条目已达上限 {MAX_ENTRIES} 条，请改用导入器维护基线名单"
            )
        entry = OverlayEntry(
            symbol=entry.symbol,
            action=entry.action,
            reason=entry.reason,
            note=entry.note,
            expire=entry.expire,
            operator=entry.operator,
            created_at=previous.created_at if previous else entry.updated_at,
            updated_at=entry.updated_at,
        )
        entries = dict(current.entries)
        entries[normalized] = entry
        _write_atomic(path, _dump(entries, str(market).upper()))
    clear_cache()
    return entry


def delete_entry(symbol: str, *, market: str = "CN", root: Path | None = None) -> bool:
    """删除一条手工条目。返回 ``False`` 表示本来就没有（**不是错误**：
    用户连点两次删除、或另一个标签页已经删过，结果都是「它现在不在表里」）。"""
    normalized = _norm_symbol(symbol)
    if not normalized:
        raise OverlayError(f"无法识别的股票代码：{symbol!r}")
    path = overlay_path(market, root=root)
    with _exclusive(path):
        current = _load_uncached(path, str(market).upper())
        if normalized not in current.entries:
            return False
        entries = dict(current.entries)
        del entries[normalized]
        _write_atomic(path, _dump(entries, str(market).upper()))
    clear_cache()
    return True


# ---------------------------------------------------------------- 合并


def merge_into_payload(
    payload: Mapping[str, Any], overlay: Overlay
) -> tuple[dict[str, Any], dict[str, int]]:
    """机器名单载荷 + 用户层 → 合并载荷（**纯函数**）。返回 ``(载荷, 统计)``。

    统计键：``manual`` 生效的手工排除数、``allow`` 生效的放行数、
    ``allow_miss`` **没命中任何机器条目**的放行数（用户以为放行了、实际那只票
    本来就不在名单里 —— 必须报出来，否则用户会以为自己解除了一个不存在的限制）。

    ``counts`` 与 ``sources`` 在合并后**重算**，不做增量加减：增量改动一旦漏掉某条
    分支，条数就会与 ``items`` 长期对不上，而这种偏差在界面上表现为「名单说 1808 只，
    实际排掉 1809 只」，极难被发现。
    """
    stats = {"manual": 0, "allow": 0, "allow_miss": 0}
    items: dict[str, dict[str, Any]] = {
        str(k): dict(v)
        for k, v in (payload.get("items") or {}).items()
        if isinstance(v, dict)
    }

    for symbol, entry in overlay.blocks().items():
        stats["manual"] += 1
        base = items.get(symbol)
        if base is None:
            items[symbol] = _manual_item(entry)
            continue
        items[symbol] = _with_source(base, SOURCE_MANUAL, entry, blocking=True)

    for symbol, entry in overlay.allows().items():
        base = items.get(symbol)
        if base is None:
            stats["allow_miss"] += 1
            continue
        stats["allow"] += 1
        # 放行是「显式解除」，不是「删除」：命中理由全部保留，只把 blocking 翻成 False，
        # 于是 symbols()（过滤侧）不再选中它，而 explain() 仍能解释「为什么它出现在名单里」
        items[symbol] = _with_source(base, SOURCE_ALLOW, entry, blocking=False)

    merged = {
        **payload,
        "items": items,
        "counts": _recount(items),
        "sources": _resource(payload.get("sources") or {}, items),
        "overlay": {
            "updated_at": overlay.updated_at,
            "manual": stats["manual"],
            "allow": stats["allow"],
            "allow_miss": stats["allow_miss"],
        },
    }
    return merged, stats


def _manual_item(entry: OverlayEntry) -> dict[str, Any]:
    """机器名单里没有的票：造一条与机器条目**同构**的记录（下游只认这一种形状）。"""
    return {
        "sources": [SOURCE_MANUAL],
        "flags": ["manual"],
        "reason": _entry_reason(entry),
        "expire": entry.expire,
        "blocking": True,
        "by_source": {
            SOURCE_MANUAL: {
                "flags": ["manual"],
                "reason": _entry_reason(entry),
                "expire": entry.expire,
            }
        },
    }


def _with_source(
    base: Mapping[str, Any], source: str, entry: OverlayEntry, *, blocking: bool
) -> dict[str, Any]:
    """在既有机器条目上叠加一个来源。

    ``flags`` 与 ``reason`` 是**追加**而不是替换：机器判据说「连续 3 年亏损」、
    用户手记说「朋友在这家公司」，两条理由都要留着——只留一条会让另一方的当事人在
    半年后完全看不懂这条记录是怎么来的。
    """
    by_source = {k: dict(v) for k, v in dict(base.get("by_source") or {}).items()}
    by_source[source] = {
        "flags": ["manual" if source == SOURCE_MANUAL else "allow"],
        "reason": _entry_reason(entry),
        "expire": entry.expire,
    }
    sources = list(dict.fromkeys([*(base.get("sources") or []), source]))
    flags = sorted(
        {*(base.get("flags") or []), "manual" if source == SOURCE_MANUAL else "allow"}
    )
    reasons: list[str] = []
    for reason in [str(base.get("reason") or ""), _entry_reason(entry)]:
        if reason and reason not in reasons:
            reasons.append(reason)
    # 放行时 ``expire`` 取原值：机器窗口该什么时候结束就什么时候结束，
    # 用户层不参与「这条名单多久失效」的判断（放行本身另有自己的 expire）
    return {
        **base,
        "sources": sources,
        "flags": flags,
        "reason": "；".join(reasons),
        "expire": base.get("expire"),
        "blocking": blocking,
        "by_source": by_source,
    }


def _entry_reason(entry: OverlayEntry) -> str:
    label = ACTION_LABELS.get(entry.action, entry.action)
    parts = [p for p in (entry.reason, entry.note) if p]
    body = "：".join(parts) if parts else ""
    who = f"（{entry.operator}）" if entry.operator else ""
    return f"{label}{who}{(' ' + body) if body else ''}"


def _recount(items: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """条目数 / 阻断数 / 逐源计数（**全量重算**，见 :func:`merge_into_payload`）。"""
    by_source: dict[str, int] = {}
    blocking = 0
    for item in items.values():
        if item.get("blocking", True):
            blocking += 1
        for source in item.get("sources") or []:
            by_source[str(source)] = by_source.get(str(source), 0) + 1
    return {
        "total": len(items),
        "blocking": blocking,
        "by_source": dict(sorted(by_source.items())),
    }


def _resource(
    sources: Mapping[str, Mapping[str, Any]], items: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """源元信息：机器源保留原始 ``asof``，用户源补上（数量由重算结果给出）。"""
    from backend.shared.exclusion_list import (
        BLOCKING_SOURCES,
        source_label,
    )

    out = {k: dict(v) for k, v in sources.items()}
    counts = _recount(items)["by_source"]
    for source in (SOURCE_MANUAL, SOURCE_ALLOW):
        if source not in counts and source not in out:
            continue
        out[source] = {
            "asof": "",
            "count": counts.get(source, 0),
            "blocking": source in BLOCKING_SOURCES,
            "label": source_label(source),
        }
    return dict(sorted(out.items()))


def overlay_excused_symbols(
    overlay: Overlay, *, today: str | None = None
) -> frozenset[str]:
    """当前**有效**的放行集合（未过期的 allow 条目）。

    只用于界面提示「这些票被手工放行」；过滤侧不需要它——合并后的
    ``symbols()`` 已经把放行项排除掉了，再减一次等于把两层逻辑各写一遍。
    """
    ref = today or date.today().isoformat()
    return frozenset(
        s for s, e in overlay.allows().items() if not (e.expire and e.expire < ref)
    )
