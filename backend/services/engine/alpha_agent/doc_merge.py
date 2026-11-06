"""Merge allowlisted MinerU part directories into one owner-owned document."""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_IMG_REF_RE = re.compile(r"(?<![\w/])images/([^)\"'\s<>]+)")


@dataclass(frozen=True)
class MergePart:
    name: str
    parsed_dir: Path


@dataclass(frozen=True)
class MergeResult:
    md_path: Path
    image_count: int
    total_bytes: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _renamed(relative: str, part_index: int, used: set[str]) -> str:
    parent, _, basename = relative.rpartition("/")
    prefix = f"{parent}/" if parent else ""
    candidate = f"{prefix}p{part_index}_{basename}"
    suffix = 2
    while candidate in used:
        candidate = f"{prefix}p{part_index}_{suffix}_{basename}"
        suffix += 1
    return candidate


def _rewrite_refs(markdown: str, mapping: dict[str, str]) -> str:
    return _IMG_REF_RE.sub(
        lambda match: f"images/{mapping.get(match.group(1), match.group(1))}",
        markdown,
    )


def merge_parts(parts: Sequence[MergePart], dest: Path) -> MergeResult:
    """Merge part Markdown and images; failures leave no partial destination."""
    if not parts:
        raise ValueError("merge_parts requires at least one part")
    destination = Path(dest)
    try:
        return _merge_parts(parts, destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _merge_parts(parts: Sequence[MergePart], dest: Path) -> MergeResult:
    total = len(parts)
    dest.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    hash_to_kept: dict[str, str] = {}
    chunks: list[str] = []
    image_count = 0
    total_bytes = 0

    for index, part in enumerate(parts, 1):
        mapping: dict[str, str] = {}
        image_root = part.parsed_dir / "images"
        if image_root.is_dir():
            for image in sorted(path for path in image_root.rglob("*") if path.is_file()):
                relative = image.relative_to(image_root).as_posix()
                digest = _sha256_file(image)
                kept = hash_to_kept.get(digest)
                if kept is None:
                    kept = (
                        relative
                        if relative not in used_names
                        else _renamed(relative, index, used_names)
                    )
                    output = dest / "images" / kept
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(image, output)
                    used_names.add(kept)
                    hash_to_kept[digest] = kept
                    image_count += 1
                    total_bytes += output.stat().st_size
                mapping[relative] = kept

        markdown = (part.parsed_dir / "full.md").read_text(encoding="utf-8")
        markdown = _rewrite_refs(markdown, mapping)
        marker = f"<!-- 合并文档 第 {index}/{total} 部分：{part.name} -->"
        chunks.append(f"{marker}\n\n{markdown.strip()}\n")

    markdown_path = dest / "full.md"
    markdown_path.write_text("\n".join(chunks), encoding="utf-8")
    total_bytes += markdown_path.stat().st_size
    return MergeResult(markdown_path, image_count, total_bytes)
