"""多文件解析产物合并（doc_merge）单元测试。

合并发生在 MinerU 各部件解包**之后**、落 ``parsed/`` **之前**。三条红线：

1. 分隔标记齐全（第 i/N 部分 + 原名）——整理链靠它感知章节边界；
2. 图片两份账：内容相同去重、同名不同内容改名——**绝不静默丢图**；
3. 两种引用写法（markdown ``(...)`` 与 html ``"..."``）都要重写到位，
   漏一种就是合并后裂图。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_merge import (  # noqa: E402
    MergePart,
    merge_parts,
)


def _mk_part(
    root: Path, name: str, md_text: str, images: dict[str, bytes]
) -> MergePart:
    part_dir = root / name.replace("/", "_")
    (part_dir / "images").mkdir(parents=True, exist_ok=True)
    (part_dir / "full.md").write_text(md_text, encoding="utf-8")
    for img_name, content in images.items():
        p = part_dir / "images" / img_name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return MergePart(name=name, parsed_dir=part_dir)


def test_merge_joins_parts_with_separators(tmp_path: Path) -> None:
    p1 = _mk_part(tmp_path, "正文.pdf", "# 第一章\n\n内容甲\n", {})
    p2 = _mk_part(tmp_path, "附录.pdf", "# 附录\n\n内容乙\n", {})
    dest = tmp_path / "out"

    result = merge_parts([p1, p2], dest)

    md = (dest / "full.md").read_text(encoding="utf-8")
    assert "<!-- 合并文档 第 1/2 部分：正文.pdf -->" in md
    assert "<!-- 合并文档 第 2/2 部分：附录.pdf -->" in md
    assert md.index("内容甲") < md.index("内容乙"), "部件顺序 = 上传顺序"
    assert result.md_path == dest / "full.md"
    assert md.endswith("\n")


def test_merge_rewrites_markdown_and_html_image_refs(tmp_path: Path) -> None:
    p1 = _mk_part(
        tmp_path,
        "a.pdf",
        '正文 ![图](images/img1.jpg)\n\n<img src="images/img1.jpg">\n',
        {"img1.jpg": b"jpeg-bytes-1"},
    )
    dest = tmp_path / "out"

    merge_parts([p1], dest)

    md = (dest / "full.md").read_text(encoding="utf-8")
    assert "images/img1.jpg" in md
    assert (dest / "images" / "img1.jpg").read_bytes() == b"jpeg-bytes-1"


def test_identical_images_deduped_across_parts(tmp_path: Path) -> None:
    """同图不同名/同名同内容 → 只留一份，两份引用都指到它。"""
    p1 = _mk_part(
        tmp_path, "a.pdf", "![x](images/hash_a.jpg)\n", {"hash_a.jpg": b"same"}
    )
    p2 = _mk_part(
        tmp_path, "b.pdf", "![y](images/hash_b.jpg)\n", {"hash_b.jpg": b"same"}
    )
    dest = tmp_path / "out"

    result = merge_parts([p1, p2], dest)

    images = sorted(p.name for p in (dest / "images").iterdir())
    assert len(images) == 1, f"同内容图片应去重，实际 {images}"
    assert result.image_count == 1
    md = (dest / "full.md").read_text(encoding="utf-8")
    kept = images[0]
    assert md.count(f"images/{kept}") == 2, "两部分引用都指到保留的那份"


def test_same_name_different_content_renamed_not_overwritten(tmp_path: Path) -> None:
    p1 = _mk_part(tmp_path, "a.pdf", "![x](images/pic.jpg)\n", {"pic.jpg": b"v1"})
    p2 = _mk_part(tmp_path, "b.pdf", "![y](images/pic.jpg)\n", {"pic.jpg": b"v2"})
    dest = tmp_path / "out"

    result = merge_parts([p1, p2], dest)

    files = {p.name: p.read_bytes() for p in (dest / "images").iterdir()}
    assert files.get("pic.jpg") == b"v1", "第一份保留原名"
    renamed = [n for n, content in files.items() if content == b"v2"]
    assert renamed == ["p2_pic.jpg"], f"第二份应改名保留，实际 {sorted(files)}"
    md = (dest / "full.md").read_text(encoding="utf-8")
    assert "images/p2_pic.jpg" in md, "改名后 md 引用必须跟着改"
    assert result.image_count == 2


def test_non_image_parts_of_md_untouched(tmp_path: Path) -> None:
    """不是 images/ 下的引用不许多管（如外链、普通相对链接）。"""
    md_text = "[链接](docs/manual.pdf) 与 ![外链](https://x.com/images/a.jpg)\n"
    p1 = _mk_part(tmp_path, "a.pdf", md_text, {})
    dest = tmp_path / "out"

    merge_parts([p1], dest)

    out = (dest / "full.md").read_text(encoding="utf-8")
    assert "[链接](docs/manual.pdf)" in out


def test_missing_full_md_raises_and_cleans_dest(tmp_path: Path) -> None:
    p1 = _mk_part(tmp_path, "a.pdf", "# ok\n", {})
    bad = MergePart(name="坏件.pdf", parsed_dir=tmp_path / "nope")
    dest = tmp_path / "out"

    with pytest.raises(FileNotFoundError):  # 缺 full.md 由 read_text 抛出
        merge_parts([p1, bad], dest)
    assert not dest.exists(), "合并失败不许留下半成品目录"


def test_empty_parts_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        merge_parts([], tmp_path / "out")


def test_subdir_image_refs_rewritten(tmp_path: Path) -> None:
    """images/ 子目录引用同样要能重写（MinerU 偶尔带分页子目录）。"""
    p1 = _mk_part(
        tmp_path, "a.pdf", "![x](images/sub/pic.png)\n", {"sub/pic.png": b"png"}
    )
    dest = tmp_path / "out"

    merge_parts([p1], dest)

    out = (dest / "full.md").read_text(encoding="utf-8")
    assert "images/sub/pic.png" in out
    assert (dest / "images" / "sub" / "pic.png").read_bytes() == b"png"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
