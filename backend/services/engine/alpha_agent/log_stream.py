"""Mining subprocess log tailing for Server-Sent Events.

日志正文仍以文件为唯一事实源（``subprocess_stdout.log``）。这里只做增量读取：
按字节偏移读新增内容、按行切分、脱敏后以 SSE 事件推送，避免每个查看者每次
轮询都把整个文件重读一遍（长任务可达几十 MB，多人同时查看时是读放大）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from collections.abc import AsyncIterator, Awaitable, Callable

from backend.shared.secret_redaction import redact_text

#: 首帧最多回看的字节数，避免一次性把超长历史塞给浏览器。
_TAIL_BYTES = 512 * 1024
#: 单条 SSE 事件最多携带的行数，防止单帧过大。
_BATCH_LINES = 200
#: 无新数据时的心跳间隔。
_HEARTBEAT_SECONDS = 15.0


def tail_offset(path: Path, lines: int) -> int:
    """返回保留最后 ``lines`` 行所需的最小字节偏移。"""
    if lines <= 0:
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size == 0:
        return 0
    start = max(0, size - _TAIL_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return 0
    if start > 0:
        newline = data.find(b"\n")
        if newline < 0:
            return size
        data = data[newline + 1 :]
    chunks = data.splitlines(keepends=True)
    kept = chunks[-lines:] if lines < len(chunks) else chunks
    return size - sum(len(chunk) for chunk in kept)


def read_new_lines(
    path: Path, offset: int, partial: bytes
) -> tuple[list[str], int, bytes]:
    """从 ``offset`` 读取新增字节，返回完整行、新偏移与未结束的尾行。"""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset, partial
    if not data:
        return [], offset, partial
    offset += len(data)
    buffered = partial + data
    pieces = buffered.split(b"\n")
    partial = pieces.pop()
    # 兼容 CRLF 写入（Windows 调试环境），不改变行内容。
    return [
        piece.decode("utf-8", "replace").removesuffix("\r") for piece in pieces
    ], offset, partial


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _batch(lines: list[str]) -> list[list[str]]:
    return [lines[i : i + _BATCH_LINES] for i in range(0, len(lines), _BATCH_LINES)]


async def stream_log(
    path: Path,
    *,
    tail_lines: int,
    is_done: Callable[[], Awaitable[bool]],
    poll_seconds: float = 0.5,
) -> AsyncIterator[str]:
    """增量推送日志；任务终态且读完后发送 ``done`` 事件并结束。"""
    offset = tail_offset(path, tail_lines)
    partial = b""
    idle = 0.0
    while True:
        lines, offset, partial = await asyncio.to_thread(
            read_new_lines, path, offset, partial
        )
        if lines:
            idle = 0.0
            for chunk in _batch(lines):
                yield _event({"lines": redact_text("\n".join(chunk)).split("\n")})
        else:
            idle += poll_seconds
        if await is_done():
            lines, offset, partial = await asyncio.to_thread(
                read_new_lines, path, offset, partial
            )
            if lines:
                for chunk in _batch(lines):
                    yield _event(
                        {"lines": redact_text("\n".join(chunk)).split("\n")}
                    )
            yield _event({"done": True})
            return
        if not lines and idle >= _HEARTBEAT_SECONDS:
            idle = 0.0
            yield ": ping\n\n"
        await asyncio.sleep(poll_seconds)
