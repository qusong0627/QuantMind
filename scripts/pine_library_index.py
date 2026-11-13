#!/usr/bin/env python3
"""把桌面 Pine 语料 / 重爬结果建成策略库索引（data/pine_library/）。

语料有三处，按优先级取生效版本：

1. ``data/pine_library/edited/<id>.pine``  —— 界面里编辑保存的（永不自动覆盖）
2. ``data/pine_library/recrawl/*.pine``   —— 修好缩进后重爬的（按 URL 回贴到分类清单）
3. ``<source>/<分类>/<文件名>``            —— 桌面原始语料（旧版爬虫产物，缩进被拍平）

产物：
  ``index.json``        列表元数据（分类/版本/行数/缩进是否完整/生效来源）
  ``source/<id>.pine``  生效源码副本（容器内可读；桌面目录容器看不到）

用法::

    /usr/bin/python3 scripts/pine_library_index.py
    /usr/bin/python3 scripts/pine_library_index.py --source ~/桌面/TradingView策略

移植说明（2026-10-08，自 quant-Trader 逐字节迁移）：宿主工具（读桌面语料），
``pine_to_pyne.py --source`` 的候选元信息就取自这里；路径均可由环境变量覆盖。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = Path(os.environ.get("PINE_LIBRARY_DIR") or ROOT / "data/pine_library")


def _default_source() -> Path:
    """Pine 语料源目录：``PINE_LIBRARY_SOURCE`` 优先；本机桌面旧路径存在则沿用
    （运营者的 TradingView 语料），否则落到仓内 ``data/pine_library/source``——
    客户把语料放这里即可，无需改代码。"""
    env = os.environ.get("PINE_LIBRARY_SOURCE")
    if env:
        return Path(env).expanduser()
    legacy = Path.home() / "桌面" / "TradingView策略"
    if legacy.is_dir():
        return legacy
    return LIB_DIR / "source"


DEFAULT_SOURCE = _default_source()

CSV_NAME = "分类清单.csv"
VERSION_RE = re.compile(r"//@version=(\d+)")
URL_RE = re.compile(r"^//\s*来源:\s*(\S+)", re.MULTILINE)
ID_RE = re.compile(r"^(\d{1,4})_")
# 真正需要缩进体的行：if/for/while/switch/else 以及函数定义的 =>
BLOCK_RE = re.compile(r"^\s*(?:if|for|while|switch|else)\b|=>\s*$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _indent_ok(lines: list[str]) -> bool:
    """缩进是否完整——只看真正需要缩进的块。

    旧判据「存在缩进行」两头都错：没有块语句的短脚本（0970 全用 ``when=``）一行不缩进
    也是合法 Pine，会被误标缩进异常；块体被拍平但恰好有续行缩进的文件又会漏判。
    """
    for i, ln in enumerate(lines):
        if not BLOCK_RE.search(ln.rstrip()):
            continue
        for nxt in lines[i + 1:]:
            if not nxt.strip() or nxt.lstrip().startswith("//"):
                continue
            return nxt[:1] in (" ", "\t")
    return True


def _meta(text: str) -> dict:
    """版本 / 行数 / 是否保留块缩进（旧版爬虫把缩进拍平了，Pine 编译不了）。"""
    m = VERSION_RE.search(text)
    lines = text.splitlines()
    return {
        "version": int(m.group(1)) if m else 0,
        "lines": len(lines),
        "bytes": len(text.encode("utf-8")),
        "indent_ok": _indent_ok(lines),
        "has_strategy": bool(re.search(r"^\s*strategy\s*\(", text, re.MULTILINE)),
    }


def load_catalog(source: Path) -> list[dict]:
    """读分类清单（没有清单时退化为扫目录，分类=子目录名）。"""
    csv_path = source / CSV_NAME
    if csv_path.exists():
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return [{"id": f"{int(r['序号']):04d}", "category": r.get("分类", "").strip(),
                 "title": r.get("中文标题", "").strip(), "title_en": r.get("原标题", "").strip(),
                 "file": r.get("文件名", "").strip(), "url": r.get("URL", "").strip()}
                for r in rows if r.get("序号", "").strip().isdigit()]

    items: list[dict] = []
    for path in sorted(source.rglob("*.pine")):
        m = ID_RE.match(path.name)
        items.append({"id": f"{int(m.group(1)):04d}" if m else f"{len(items) + 1:04d}",
                      "category": path.parent.name, "title": path.stem, "title_en": "",
                      "file": path.name, "url": ""})
    return items


def _recrawl_by_url(recrawl_dir: Path) -> dict[str, Path]:
    """重爬目录里每个文件头部的来源 URL → 路径。"""
    out: dict[str, Path] = {}
    if not recrawl_dir.exists():
        return out
    for path in recrawl_dir.glob("*.pine"):
        m = URL_RE.search(_read(path))
        if m:
            out[m.group(1)] = path
    return out


def build(source: Path = DEFAULT_SOURCE, lib_dir: Path = LIB_DIR) -> dict:
    lib_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = lib_dir / "source"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    edited_dir = lib_dir / "edited"
    recrawl = _recrawl_by_url(lib_dir / "recrawl")

    catalog = load_catalog(source)
    items: list[dict] = []
    for row in catalog:
        edited = edited_dir / f"{row['id']}.pine"
        recrawled = recrawl.get(row["url"]) if row["url"] else None
        original = source / row["category"] / row["file"]

        if edited.exists():
            kind, src = "edited", edited
        elif recrawled is not None:
            kind, src = "recrawl", recrawled
        elif original.exists():
            kind, src = "desktop", original
        else:
            items.append({**row, "source_kind": "missing", "version": 0, "lines": 0,
                          "bytes": 0, "indent_ok": False, "has_strategy": False,
                          "mtime": ""})
            continue

        text = _read(src)
        dst = scripts_dir / f"{row['id']}.pine"
        if src != dst:
            shutil.copyfile(src, dst)
        items.append({**row, "source_kind": kind,
                      "mtime": datetime.fromtimestamp(src.stat().st_mtime,
                                                      tz=timezone.utc).isoformat(timespec="seconds"),
                      **_meta(text)})

    # 重爬目录里没进清单的文件（清单缺行时的兜底），单独挂到「未归类」
    known = {it["url"] for it in items if it["url"]}
    for url, path in sorted(recrawl.items()):
        if url in known:
            continue
        text = _read(path)
        items.append({"id": f"x{len(items) + 1:03d}", "category": "未归类",
                      "title": path.stem, "title_en": "", "file": path.name, "url": url,
                      "source_kind": "recrawl", "mtime": "",
                      **_meta(text)})

    counts: dict[str, int] = {}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
    categories = [{"name": name, "count": n}
                  for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    index = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_dir": str(source),
        "total": len(items),
        "usable": sum(1 for it in items if it["indent_ok"]),
        "categories": categories,
        "items": items,
    }
    tmp = lib_dir / "index.json.tmp"
    tmp.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(lib_dir / "index.json")
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 Pine 策略库索引")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE), help="原始语料目录")
    ap.add_argument("--out", default=str(LIB_DIR), help="索引输出目录")
    args = ap.parse_args()

    source = Path(args.source).expanduser()
    if not source.exists():
        print(f"语料目录不存在：{source}", file=sys.stderr)
        return 1

    idx = build(source=source, lib_dir=Path(args.out))
    by_kind: dict[str, int] = {}
    for it in idx["items"]:
        by_kind[it["source_kind"]] = by_kind.get(it["source_kind"], 0) + 1
    print(f"索引 {idx['total']} 条 → {Path(args.out) / 'index.json'}")
    print(f"缩进完整（可编译）：{idx['usable']} / {idx['total']}")
    print("生效来源：", "，".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
    print("分类：", "，".join(f"{c['name']}({c['count']})" for c in idx["categories"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
