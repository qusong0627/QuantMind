"""日志尾读取（后台运维面板共用）：尾 N 行 + 字节上限 + 符号链接拒读。

两个消费方（rd_mined 物化面板、因子池刷新面板）展示的都是子进程写出的
日志文件，安全约束相同——**符号链接一律拒读**：日志路径可被同挂载域的
低权限写者摆成任意文件的链接，顺着读会把面板变成别人的文件浏览器
（读到的内容进管理员浏览器）。此处单一实现，防止两处拷贝漂移。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def tail_log_file(
    path: Path, max_lines: int = 120, max_bytes: int = 256 * 1024
) -> dict[str, Any]:
    """日志文件尾部；不存在返回 exists=False，超限按字节截断不吐半行。"""
    path = Path(path)
    if path.is_symlink():
        return {
            "path": str(path),
            "exists": False,
            "lines": [],
            "note": "路径是符号链接，拒绝读取",
        }
    if not path.is_file():
        return {"path": str(path), "exists": False, "lines": []}
    size = path.stat().st_size
    truncated = size > max_bytes
    with open(path, "rb") as fh:
        if truncated:
            start = size - max_bytes
            # 边界前一字节是换行 → 截断点恰好落在行首，首行是完整的，不能丢
            fh.seek(start - 1)
            boundary_aligned = fh.read(1) == b"\n"
            fh.seek(start)
        else:
            boundary_aligned = True
        raw = fh.read()
    content = raw.decode("utf-8", errors="replace")
    lines = content.splitlines()
    if truncated and not boundary_aligned and lines:
        lines = lines[1:]  # 截断点落在行中间：首行是半个行，丢弃残片
    return {
        "path": str(path),
        "exists": True,
        "size": size,
        "truncated": truncated,
        "lines": lines[-max(1, int(max_lines)) :],
    }
