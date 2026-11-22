#!/usr/bin/env python3
"""把 pynecore-strategies 语料导入「行情回测 → Pine 策略库」。

语料是 449 条 TradingView 社区策略的 PyneComp 编译产物（PyneCore 6.12.1 验证），
与平台 ``scripts/pine_to_pyne.py`` 的候选格式相同。本脚本只做导入，不改编译代码：

- ``data/pine_library/source/<id>.pine``：原始 Pine（byte copy）
- ``data/pine_transpile/<id>/candidate.py``：原样编译产物（运行时另做 decorator 覆盖）
- ``data/pine_transpile/<id>/meta.json``：imported=true + 来源/分类/许可/校验
- ``data/pine_library/custom/index.json``：列表条目（source_kind=imported）
- ``data/pine_library/lib/``：语料 ``scripts/lib`` 的副本（``import lib.*`` 用）

分类规则（2026-11-21 实测 449 条）：
- 无 ``request.security`` → ready；有 → deferred（P4 再接多周期）
- 无 ``strategy.short`` → ``market_hint=cn``（A 股多头）
- 有 ``strategy.short`` → ``market_hint=us``（美股多空；港股只作第二展示市场）
- ``import lib.*`` 不阻断：lib 目录随导入复制，运行时挂 sys.path

用法（宿主）::

    python3 scripts/import_pynecore_corpus.py --corpus /path/to/pynecore-strategies
    python3 scripts/import_pynecore_corpus.py --corpus ... --apply
    python3 scripts/import_pynecore_corpus.py --corpus ... --apply --limit 10
    python3 scripts/import_pynecore_corpus.py --corpus ... --apply --include-deferred
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.agent_arena import pine_library as pl  # noqa: E402
from backend.services.agent_arena import pyne_overrides as po  # noqa: E402
from backend.services.agent_arena import pyne_security as ps  # noqa: E402

SOURCE_REPO = "theonetrade/pynecore-strategies"
ID_BASE = 9001
ID_MAX = 9999
SCRIPT_RE = re.compile(r"^strategy_(?P<slug>.+)_(?P<sid>[A-Za-z0-9]{8})\.py$")
VERSION_RE = re.compile(r"//@version=(\d+)")
AUTHOR_RE = re.compile(r"^\s*//\s*(?:©|\(c\)|by)\s*(.+)$", re.IGNORECASE)
TITLE_RE = re.compile(
    r"@script\.strategy\(\s*(?:title\s*=\s*)?(['\"])(?P<title>.+?)\1",
    re.DOTALL,
)
SHORT_RE = re.compile(r"strategy\.short|strategy\.entry\([^)]*strategy\.short")
LIB_RE = re.compile(r"^(?:import|from)\s+lib\.", re.MULTILINE)
# PyneCore 6.9.2 未实现的 request.* 家族：命中就不导入（运行必抛 NotImplementedError）
UNSUPPORTED_REQUESTS = (
    "request.earnings",
    "request.dividends",
    "request.splits",
    "request.financial",
    "request.quandl",
    "request.seed",
    "request.currency_rate",
)

CATEGORY_CN = "TradingView·A股多头"
CATEGORY_US = "TradingView·美股多空"
CATEGORY_CRYPTO = "TradingView·加密周期"
CATEGORY_DEFERRED = "TradingView·待补多周期"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pine_files(corpus: Path) -> dict[str, tuple[Path, str]]:
    """source_id → (pine 路径, tier)。bit-exact 优先于 verified。"""
    out: dict[str, tuple[Path, str]] = {}
    for tier in ("verified", "bit-exact"):  # 先 verified，后 bit-exact 覆盖
        for p in sorted((corpus / "assets/strategy" / tier).glob("*.pine")):
            m = re.search(r"_([A-Za-z0-9]{8})$", p.stem) or re.fullmatch(
                r"([A-Za-z0-9]{8})", p.stem
            )
            if m:
                out[m.group(1)] = (p, tier)
    return out


def _title(source: str) -> str:
    m = TITLE_RE.search(source)
    return re.sub(r"\s+", " ", m.group("title")).strip() if m else ""


def _author(pine_text: str) -> str:
    for line in pine_text.splitlines()[:12]:
        m = AUTHOR_RE.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _decorator(source: str) -> str:
    bounds = po._scan_decorator(source)  # noqa: SLF001 - 同包纯函数
    if bounds is None:
        return ""
    start, end = bounds
    head = source.rfind("@script.strategy(", 0, start)
    return source[head : end + 1].strip()


def discover(corpus: Path) -> list[dict[str, Any]]:
    """扫描语料 → 待导入记录（不写盘；deferred 也返回，由调用方过滤）。"""
    pine_by_sid = _pine_files(corpus)
    records: list[dict[str, Any]] = []
    for py_path in sorted((corpus / "scripts").glob("strategy_*.py")):
        m = SCRIPT_RE.match(py_path.name)
        if not m:
            continue
        sid = m.group("sid")
        source = py_path.read_text(encoding="utf-8", errors="replace")
        pine_path, tier = pine_by_sid.get(sid, (None, ""))
        pine_text = (
            pine_path.read_text(encoding="utf-8", errors="replace")
            if pine_path is not None
            else ""
        )
        has_short = bool(SHORT_RE.search(source))
        has_security = "request.security" in source
        has_lib = bool(LIB_RE.search(source))
        security_tfs: list[str] = []
        security_markets: list[str] = ["cn", "hk", "us", "futures", "crypto"]
        security_unresolved: list[str] = []
        feature_unresolved = [f for f in UNSUPPORTED_REQUESTS if f in source]
        if feature_unresolved:
            status = "deferred_feature"
            security_markets = []
        elif has_security:
            wanted, unresolved = ps.requested_timeframes(source, "1D")
            if unresolved:
                status = "deferred_security"
                security_markets = []
                security_unresolved = unresolved
            else:
                status = "ready_security"
                security_tfs = sorted(set(wanted.values()))
                security_markets = ps.supported_markets(security_tfs)
        else:
            status = "ready"
        if status not in ("ready", "ready_security"):
            category = CATEGORY_DEFERRED
        elif has_short:
            category = CATEGORY_US
        elif "cn" in security_markets:
            category = CATEGORY_CN
        elif "us" in security_markets or "hk" in security_markets:
            category = CATEGORY_US
        else:
            category = CATEGORY_CRYPTO
        records.append(
            {
                "source_id": sid,
                "slug": m.group("slug"),
                "title": _title(source) or m.group("slug").replace("_", " "),
                "title_en": m.group("slug").replace("_", " "),
                "url": f"https://www.tradingview.com/script/{sid}/",
                "author": _author(pine_text),
                "tier": tier,
                "version": int(VERSION_RE.search(pine_text).group(1))
                if VERSION_RE.search(pine_text)
                else 0,
                "has_short": has_short,
                "has_security": has_security,
                "has_lib": has_lib,
                "security_timeframes": security_tfs,
                "security_markets": security_markets,
                "security_unresolved": security_unresolved,
                "feature_unresolved": feature_unresolved,
                "market_hint": "us" if has_short else "cn",
                "import_status": status,
                "category": category,
                "compiled_path": py_path,
                "pine_path": pine_path,
                "pine_text": pine_text,
                "source_sha256": _sha256(py_path),
                "pine_sha256": _sha256(pine_path) if pine_path else None,
                "original_strategy_decorator": _decorator(source),
            }
        )
    return records


def assign_ids(
    records: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """源 ID → 稳定 9xxx 库 ID；已导入的复用，新条目从当前最大值递增。"""
    by_source = {
        str(it.get("source_id")): str(it.get("id"))
        for it in existing
        if it.get("source_kind") == "imported" and it.get("source_id")
    }
    used = [
        int(it["id"])
        for it in existing
        if str(it.get("id", "")).isdigit() and int(it["id"]) >= ID_BASE
    ]
    next_id = max(used, default=ID_BASE - 1) + 1
    out: list[dict[str, Any]] = []
    for rec in records:
        sid = rec["source_id"]
        if sid in by_source:
            rec["id"] = by_source[sid]
        else:
            if next_id > ID_MAX:
                raise RuntimeError(f"导入 ID 用尽（>{ID_MAX}）")
            rec["id"] = str(next_id)
            next_id += 1
        out.append(rec)
    return out


def _custom_item(rec: dict[str, Any]) -> dict[str, Any]:
    pine_text = rec.get("pine_text") or ""
    pmeta = (
        pl._pine_meta(pine_text)
        if pine_text
        else {  # noqa: SLF001
            "version": 0,
            "lines": 0,
            "bytes": 0,
            "indent_ok": True,
            "has_strategy": True,
        }
    )
    return {
        "id": rec["id"],
        "title": rec["title"],
        "title_en": rec["title_en"],
        "file": f"{rec['id']}.pine",
        "url": rec["url"],
        "version": pmeta["version"],
        "lines": pmeta["lines"],
        "bytes": pmeta["bytes"],
        "indent_ok": pmeta["indent_ok"],
        "has_strategy": pmeta["has_strategy"],
        "source_kind": "imported",
        "source_id": rec["source_id"],
        "market_hint": rec["market_hint"],
        "has_security": rec["has_security"],
        "security_timeframes": rec.get("security_timeframes") or [],
        "security_markets": rec.get("security_markets") or [],
        "import_status": rec["import_status"],
        "tier": rec["tier"],
        "category": rec["category"],
        "mtime": _now(),
    }


def _meta(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "imported": True,
        "source_repo": SOURCE_REPO,
        "source_id": rec["source_id"],
        "title": rec["title"],
        "title_en": rec["title_en"],
        "url": rec["url"],
        "author": rec["author"],
        "tier": rec["tier"],
        "has_short": rec["has_short"],
        "has_security": rec["has_security"],
        "has_lib": rec["has_lib"],
        "security_timeframes": rec.get("security_timeframes") or [],
        "security_markets": rec.get("security_markets") or [],
        "market_hint": rec["market_hint"],
        "import_status": rec["import_status"],
        "problems": [],
        "original_strategy_decorator": rec["original_strategy_decorator"],
        "pyne_compile": "6.0.79",
        "pynecore_tested": "6.12.1",
        "source_sha256": rec["source_sha256"],
        "pine_sha256": rec["pine_sha256"],
        "license_note": (
            "TradingView community publication; local research only; "
            "not for redistribution. See the original publication URL."
        ),
        "imported_at": _now(),
    }


def write_records(
    records: list[dict[str, Any]],
    *,
    library_root: Path,
    force: bool = False,
) -> dict[str, int]:
    """写 source / candidate / meta / custom index；返回计数。"""
    library_root = Path(library_root)
    source_dir = library_root / "source"
    transpile_root = library_root.parent / "pine_transpile"
    source_dir.mkdir(parents=True, exist_ok=True)
    transpile_root.mkdir(parents=True, exist_ok=True)

    existing = pl._load_custom()  # noqa: SLF001
    by_id = {str(it.get("id")): it for it in existing}
    counts = {"added": 0, "updated": 0, "skipped": 0}

    for rec in records:
        item_dir = transpile_root / rec["id"]
        item_dir.mkdir(parents=True, exist_ok=True)
        cand = item_dir / "candidate.py"
        meta_path = item_dir / "meta.json"
        pine_dst = source_dir / f"{rec['id']}.pine"
        existed = cand.exists()
        if existed and not force:
            counts["skipped"] += 1
        else:
            shutil.copyfile(rec["compiled_path"], cand)
            counts["updated" if existed else "added"] += 1
        meta_path.write_text(
            json.dumps(_meta(rec), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if rec.get("pine_text"):
            pine_dst.write_text(rec["pine_text"], encoding="utf-8")
        else:
            pine_dst.write_text(
                f"// source unavailable; compiled from {SOURCE_REPO}\n"
                f"// {rec['url']}\n",
                encoding="utf-8",
            )
        by_id[rec["id"]] = _custom_item(rec)

    items = sorted(by_id.values(), key=lambda it: str(it.get("id")))
    pl._save_custom(items)  # noqa: SLF001
    return counts


def write_deferred_manifest(
    records: list[dict[str, Any]], library_root: Path
) -> Path | None:
    """把未导入/不可解析的语料落一份清单（审计用，界面不读）。"""
    deferred = [
        r for r in records if r["import_status"] not in ("ready", "ready_security")
    ]
    if not deferred:
        return None
    payload = {
        "generated": _now(),
        "total": len(deferred),
        "note": (
            "未导入：request.* 特性未实现，或 request.security 的 symbol/timeframe "
            "无法静态解析（跨标的或动态变量），未猜成图表周期——猜错会把另一条"
            "周期算成当前周期。"
        ),
        "items": [
            {
                "source_id": r["source_id"],
                "title": r["title"],
                "url": r["url"],
                "status": r["import_status"],
                "has_short": r["has_short"],
                "unresolved": r.get("security_unresolved") or [],
                "features": r.get("feature_unresolved") or [],
            }
            for r in deferred
        ],
    }
    path = Path(library_root) / "imported_deferred.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_deferred_items(all_records: list[dict[str, Any]]) -> int:
    """已导入条目若新判定为 deferred，更新列表状态（不删除，保住备注/编辑）。"""
    items = pl._load_custom()  # noqa: SLF001
    by_source = {
        str(it.get("source_id")): it
        for it in items
        if it.get("source_kind") == "imported" and it.get("source_id")
    }
    changed = 0
    for rec in all_records:
        if rec["import_status"] in ("ready", "ready_security"):
            continue
        item = by_source.get(rec["source_id"])
        if item is None:
            continue
        item["import_status"] = rec["import_status"]
        item["category"] = rec["category"]
        item["feature_unresolved"] = rec.get("feature_unresolved") or []
        changed += 1
    if changed:
        pl._save_custom(sorted(items, key=lambda it: str(it.get("id"))))  # noqa: SLF001
    return changed


def copy_lib(corpus: Path, library_root: Path) -> dict[str, Any]:
    """把语料 ``scripts/lib`` 复制到 ``<library_root>/lib``（幂等）。"""
    src = Path(corpus) / "scripts/lib"
    dst = Path(library_root) / "lib"
    if not src.is_dir():
        return {"copied": False, "reason": "corpus scripts/lib missing"}
    shutil.copytree(
        src,
        dst,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return {"copied": True, "files": sum(1 for _ in dst.rglob("*.py"))}


def summarize(records: list[dict[str, Any]]) -> dict[str, int]:
    out = {
        "total": len(records),
        "cn": 0,
        "us": 0,
        "deferred": 0,
        "lib": 0,
        "security": 0,
    }
    for rec in records:
        status = rec["import_status"]
        if status not in ("ready", "ready_security"):
            out["deferred"] += 1
        elif rec["has_short"]:
            out["us"] += 1
        else:
            out["cn"] += 1
        if rec["has_lib"]:
            out["lib"] += 1
        if status == "ready_security":
            out["security"] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导入 pynecore-strategies 语料")
    ap.add_argument("--corpus", required=True, help="pynecore-strategies 仓库路径")
    ap.add_argument(
        "--library-root",
        default="",
        help="语料库根（默认 PINE_LIBRARY_DIR 或 数据根/pine_library）",
    )
    ap.add_argument("--apply", action="store_true", help="真正写盘（缺省 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（冒烟）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 candidate/meta")
    ap.add_argument(
        "--include-deferred",
        action="store_true",
        help="连 request.security 的条目一起导入（默认跳过）",
    )
    ap.add_argument("--skip-lib", action="store_true", help="不复制 scripts/lib")
    args = ap.parse_args(argv)

    corpus = Path(args.corpus).expanduser().resolve()
    if not (corpus / "scripts").is_dir():
        print(f"不是 pynecore-strategies 仓库: {corpus}", file=sys.stderr)
        return 2
    if args.library_root:
        os.environ["PINE_LIBRARY_DIR"] = str(
            Path(args.library_root).expanduser().resolve()
        )
        # pine_library 的 lib_dir() 在调用时读 env；transpile_dir() 随之改道
    library_root = pl.lib_dir()
    print(f"语料: {corpus}")
    print(f"库根: {library_root}")
    print(f"回测产物根: {pl.transpile_dir()}")

    all_records = discover(corpus)
    all_summary = summarize(all_records)
    records = [
        r
        for r in all_records
        if args.include_deferred or r["import_status"] in ("ready", "ready_security")
    ]
    records = assign_ids(records, pl._load_custom())  # noqa: SLF001
    if args.limit:
        records = records[: args.limit]
    summary = summarize(records)
    print("全量分类:", json.dumps(all_summary, ensure_ascii=False))
    print("本次导入:", json.dumps(summary, ensure_ascii=False))
    for rec in records[:10]:
        print(f"  {rec['id']} {rec['category']:>18s}  {rec['title'][:48]}")

    if not args.apply:
        print("dry-run：未写盘（加 --apply 执行）")
        return 0

    if not args.skip_lib:
        print("lib:", copy_lib(corpus, library_root))
    counts = write_records(records, library_root=library_root, force=args.force)
    print("写入:", json.dumps(counts, ensure_ascii=False))
    changed = update_deferred_items(all_records)
    if changed:
        print("已导入但新判定 deferred:", changed)
    manifest = write_deferred_manifest(all_records, library_root)
    if manifest:
        print("未导入清单:", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
