"""多文件解析产物合并（T-FM-19a）—— 纯文件操作，无服务依赖。

把同一批次各部件解包出的 parsed 目录（``full.md`` + ``images/``）合并为
一份 ``full.md`` + 一份 ``images/``。三条纪律（与 test_doc_merge 对齐）：

1. **分隔标记**：每个部件前插入 ``<!-- 合并文档 第 i/N 部分：name -->``。
   整理链提示词靠它感知章节边界（正文+附录/多图）；HTML 注释不进可见正文。
2. **图片两份账**：内容相同 → 只留一份（跨部件去重）；相对名相同但内容
   不同 → 改名 ``p{i}_原名``（**绝不覆盖**——覆盖 = 静默丢图）。
3. **两种引用写法都重写**：markdown ``](images/x)`` 与 html ``"images/x"``；
   外链（``https://…/images/x``）因前缀 ``/`` 不命中，原样保留。

失败语义：任何异常（部件缺 ``full.md``、IO 错误）→ 清掉 dest 再抛。
dest 必须由本函数独占（合并失败 rmtree 会把既有内容一并带走），与
``mineru_client.extract_zip_whitelist`` 同一约定。
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: markdown 与 html 两种图片引用；负向后顾挡住外链（``x.com/images/`` 前是 ``/``）
_IMG_REF_RE = re.compile(r"(?<![\w/])images/([^)\"'\s<>]+)")


@dataclass(frozen=True)
class MergePart:
    """一个部件的解析产物目录（含 ``full.md``，可选 ``images/``）。"""

    name: str
    parsed_dir: Path


@dataclass(frozen=True)
class MergeResult:
    md_path: Path
    image_count: int
    total_bytes: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _renamed(rel: str, part_index: int, used: set[str]) -> str:
    """同名不同内容的改名：``p{i}_原名``（保留子目录），再撞追加序号。"""
    parent, _, base = rel.rpartition("/")
    prefix_dir = f"{parent}/" if parent else ""
    candidate = f"{prefix_dir}p{part_index}_{base}"
    n = 2
    while candidate in used:
        candidate = f"{prefix_dir}p{part_index}_{n}_{base}"
        n += 1
    return candidate


def _rewrite_refs(text: str, mapping: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        rel = match.group(1)
        return f"images/{mapping.get(rel, rel)}"

    return _IMG_REF_RE.sub(repl, text)


def merge_parts(parts: Sequence[MergePart], dest: Path) -> MergeResult:
    """合并部件产物到 dest（``full.md`` + ``images/``）。

    失败（任何异常，含部件缺 ``full.md``）→ 清掉 dest 再抛：绝不留半成品，
    免得下游把残缺目录当成功产物。
    """
    if not parts:
        raise ValueError("merge_parts: 部件列表为空")
    dest = Path(dest)
    try:
        return _merge_impl(parts, dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _merge_impl(parts: Sequence[MergePart], dest: Path) -> MergeResult:
    total = len(parts)
    dest.mkdir(parents=True, exist_ok=True)
    dest_images = dest / "images"

    used_names: set[str] = set()
    hash_to_kept: dict[str, str] = {}
    image_count = 0
    total_bytes = 0
    chunks: list[str] = []

    for idx, part in enumerate(parts, 1):
        mapping: dict[str, str] = {}
        images_root = part.parsed_dir / "images"
        if images_root.is_dir():
            files = sorted(p for p in images_root.rglob("*") if p.is_file())
            for img in files:
                orig_rel = img.relative_to(images_root).as_posix()
                digest = _sha256_file(img)
                kept = hash_to_kept.get(digest)
                if kept is None:
                    rel = (
                        orig_rel
                        if orig_rel not in used_names
                        else _renamed(orig_rel, idx, used_names)
                    )
                    dest_img = dest_images / rel
                    dest_img.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(img, dest_img)
                    used_names.add(rel)
                    hash_to_kept[digest] = rel
                    kept = rel
                    image_count += 1
                    total_bytes += dest_img.stat().st_size
                mapping[orig_rel] = kept

        # 读全文（缺文件在 _merge_impl 外统一清理并抛出）
        text = (part.parsed_dir / "full.md").read_text(encoding="utf-8")
        if mapping:
            text = _rewrite_refs(text, mapping)
        separator = f"<!-- 合并文档 第 {idx}/{total} 部分：{part.name} -->"
        chunks.append(f"{separator}\n\n{text.strip()}\n")

    md_text = "\n".join(chunks)
    md_path = dest / "full.md"
    md_path.write_text(md_text, encoding="utf-8")
    total_bytes += len(md_text.encode("utf-8"))
    logger.info(
        "merge_parts: %d 部件 → %s（%d 图 / %d 字节）",
        total,
        md_path,
        image_count,
        total_bytes,
    )
    return MergeResult(
        md_path=md_path, image_count=image_count, total_bytes=total_bytes
    )
