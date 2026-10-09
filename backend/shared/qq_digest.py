"""QQ 告警摘要（降噪）：一段行情里几十条同类告警折叠成一条。

背景（2026-10-09）：市场跳水 6 分钟内 QQ 收到 26 条消息（19 条
``[sentinel] … 大幅下行`` + 6 条 ``[holding_alert] … 盘中异动`` + 1 条新闻），
一条一只票。单条文案没错，错的是**量**——同轮扫描/同一段行情里的批量告警
逐条推送 = 手机被淹没 = 用户不再看，真告警也被淹掉。

分工（与 :mod:`backend.shared.qq_notify` 的既有出口并列，互不替代）：
* ``qq_notify.notify_async`` —— **单发**事件（成交回执、单条运维事件）；
* ``qq_notify.alert_async`` —— 单条告警（桥健康、风控这类「系统类、低频、
  单条即全貌」的告警，保持即时）；
* **本模块** —— **批量告警面**（市场情报、持仓预警这类「一轮一批、内容同构」
  的告警）。生产者照旧逐条投递站内通知/留痕（交易台卡片、``sentinel_alerts``
  表不受影响），QQ 面改走这里合并。

口径：
* 按 ``digest_key`` 分组累积；**固定窗口，不做逐条顺延的 debounce**——
  首次入队起算 ``window_s``（默认 120s，``QQ_DIGEST_WINDOW_S`` 可调）。
  连续阴跌时 debounce 会把整段行情憋到收盘才发；固定窗口的延迟有上界
  （≤ window_s + 发送耗时），节奏也有上界（≤ 1 条 / 窗口）。
* 窗口到期合并成一条：``{header} · {n} 条`` + 明细行；超过 ``MAX_LINES`` 的
  折叠为「另有 N 条」（明细在交易台/留痕表里全量可查）。
* 进程内缓冲（``threading.Lock``）：当前两个生产者都在 trade 进程。跨进程
  生产者各自成摘要，不共享窗口——这不会合并错，只是少合并一次。
* 本模块永不抛、不阻塞调用方；定时器起不来就立即发（宁早勿丢）。

**直回执纪律**：成交回执/单条系统告警不要接进来——摘要是给批量告警降噪的，
把「立即送达」的事件塞进窗口等于给它们加 2 分钟延迟。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 固定窗口秒数（首次入队起算）；环境变量可调，下限 5s 防呆
DEFAULT_WINDOW_S = 120.0
#: 明细行展示上限（超出折叠为「另有 N 条」）
MAX_LINES = 15
#: 缓冲行上限（防极端风暴下无界占内存；超出的只计数不存文本）
MAX_BUFFERED_LINES = 60


@dataclass
class _Digest:
    header: str
    channel: str
    footer: str
    first_ts: float
    lines: list[str] = field(default_factory=list)
    folded: int = 0


_buffers: dict[str, _Digest] = {}
_lock = threading.Lock()


def _window_s() -> float:
    try:
        return max(5.0, float(os.getenv("QQ_DIGEST_WINDOW_S") or DEFAULT_WINDOW_S))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_S


def enqueue(
    *,
    digest_key: str,
    header: str,
    line: str,
    channel: str = "default",
    footer: str = "详情见交易台。",
    window_s: float | None = None,
) -> None:
    """把一行明细并入 ``digest_key`` 的窗口缓冲；窗口到期自动发出。永不抛。"""
    key = str(digest_key or "").strip()
    text = str(line or "").strip()
    if not key or not text:
        return
    started: _Digest | None = None
    with _lock:
        entry = _buffers.get(key)
        if entry is None:
            entry = _Digest(
                header=str(header or "告警摘要"),
                channel=str(channel or "default"),
                footer=str(footer or ""),
                first_ts=time.time(),
            )
            _buffers[key] = entry
            started = entry
        if len(entry.lines) < MAX_BUFFERED_LINES:
            entry.lines.append(text)
        else:
            entry.folded += 1
    if started is not None:
        _start_timer(key, started, window_s or _window_s())


def _start_timer(key: str, entry: _Digest, window_s: float) -> None:
    try:
        timer = threading.Timer(window_s, _flush_entry, args=(key, entry))
        timer.daemon = True
        timer.start()
    except Exception as exc:  # noqa: BLE001 - 定时器起不来就立即发，宁早勿丢
        logger.warning("[QQDigest] 定时器创建失败，立即发送: %s", exc)
        _flush_entry(key, entry)


def _flush_entry(key: str, entry: _Digest) -> None:
    """窗口到期：原子摘除并发送（换代/已冲刷的旧条目静默退出）。"""
    with _lock:
        if _buffers.get(key) is not entry:
            return
        _buffers.pop(key, None)
    try:
        _send(entry)
    except Exception as exc:  # noqa: BLE001 - 摘要发送失败只记日志，无留痕责任
        logger.warning("[QQDigest] 摘要发送失败: %s", exc)


def _compose(entry: _Digest) -> tuple[str, str]:
    """（标题, 正文）。折叠计数 = 展示上限之外的 + 缓冲上限之外的。"""
    total = len(entry.lines) + entry.folded
    title = f"{entry.header} · {total} 条"
    shown = entry.lines[:MAX_LINES]
    hidden = total - len(shown)
    body = "\n".join(shown)
    if hidden > 0:
        body += f"\n另有 {hidden} 条"
    if entry.footer:
        body += f"\n\n{entry.footer}"
    return title, body


def _send(entry: _Digest) -> None:
    from backend.shared.qq_notify import notify_async

    title, body = _compose(entry)
    notify_async(title, body, channel=entry.channel)


def flush_all() -> int:
    """立即冲刷全部缓冲（测试/关机用）；返回冲刷组数。永不抛。"""
    with _lock:
        items = list(_buffers.items())
        _buffers.clear()
    for _key, entry in items:
        try:
            _send(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QQDigest] 摘要发送失败: %s", exc)
    return len(items)
