#!/usr/bin/env python3
"""导入「候选信号排除名单」（通道 A：用户基线名单）——**宿主侧**运行。

把隔壁 quant-Trader 的四份产物归一成一份 JSON，落到 ``data/exclusions/cn.json``
（``./data:/data`` 已挂进容器，落盘即容器可读，零部署改动）：

| 源文件 | 源名 | 形态 |
|---|---|---|
| ``data/fundamental_flags.json`` | ``fundamental_flags`` | 6 位裸码 |
| ``data/risk_block.json`` | ``risk_block`` / ``_warn`` / ``_watch`` | 6 位裸码 |
| ``data/news_blacklist_2026.json`` | ``news_blacklist`` | 已后缀 |
| ``configs/live_symbols.json`` | ``block_buy`` | 已后缀 |

**为什么在宿主侧而不是容器内实时算**：隔壁的纪律就是「离线生成 + 落盘 + 每日刷新，
报表与闸门同源」，且容器**没有**挂载隔壁目录（客户机也没有该目录），把跨仓路径钉进
compose 会污染部署。本仓 ``/list`` 是单 worker uvicorn，实时重算 1600 只的多层基本面
判据会阻塞全部并发请求——所以这里只落一份查表产物。

**本脚本的一半价值在审计**：源文件与名单最危险的失效方式是**静默少排**
（代码归一碰撞、后缀写错交易所），界面上完全看不出来。故 ``audit()`` 会把
「源条数 vs 入库条数」「后缀与交易所推断是否自洽」逐条对出来，对不上就以非零码退出。

用法::

    python backend/scripts/import_exclusion_list.py                  # 导入（默认源目录）
    python backend/scripts/import_exclusion_list.py --dry-run        # 只审计并打差异
    python backend/scripts/import_exclusion_list.py --from /path/to/quant-Trader
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.exclusion_list import (  # noqa: E402
    build_payload,
    now_iso,
    source_label,
)
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402

#: 隔壁仓库默认位置（客户机上不存在——那时用 ``--from`` 指定）
DEFAULT_SOURCE_ROOT = "/home/zbox/quant-Trader"
#: 本仓产物目录（``./data:/data`` 挂载，容器内即 ``/data/exclusions``）
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "exclusions"

#: 源文件名 → 载荷里的源名（审计报告逐源对账用）
SOURCE_FILES: dict[str, str] = {
    "data/fundamental_flags.json": "fundamental_flags",
    "data/risk_block.json": "risk_block",
    "data/news_blacklist_2026.json": "news_blacklist",
    "configs/live_symbols.json": "block_buy",
}

_SUFFIXED = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


def _read_json(path: Path) -> Any:
    """读 JSON；文件缺失/损坏**抛出**（导入器宁可失败也不要半份名单）。"""
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_sources(source_root: Path) -> dict[str, Any]:
    """读四份源文件 → ``build_payload`` 的入参（缺一份即报错，不静默跳过）。"""
    root = Path(source_root)
    missing = [rel for rel in SOURCE_FILES if not (root / rel).is_file()]
    if missing:
        raise FileNotFoundError(
            f"源文件缺失：{', '.join(missing)}（源目录 {root}）"
            "——名单不完整比没有名单更危险，拒绝生成"
        )
    return {
        "market": "CN",
        "fundamental_flags": _read_json(root / "data/fundamental_flags.json"),
        "risk_block": _read_json(root / "data/risk_block.json"),
        "news_blacklist": _read_json(root / "data/news_blacklist_2026.json"),
        "live_symbols": _read_json(root / "configs/live_symbols.json"),
    }


def _expected_counts(raw: Mapping[str, Any]) -> dict[str, int]:
    """各源「应入库条数」——直接数源文件，不看产物（否则是自证）。"""
    fundamental = raw.get("fundamental_flags") or {}
    risk_block = raw.get("risk_block") or {}
    news = raw.get("news_blacklist") or {}
    live = raw.get("live_symbols") or {}
    return {
        "fundamental_flags": len(fundamental.get("items") or {}),
        "risk_block": len(risk_block.get("items") or {}),
        "news_blacklist": len(news.get("items") or []),
        "block_buy": len(live.get("block_buy") or []),
    }


def _suffix_problems(raw: Mapping[str, Any]) -> list[str]:
    """检查**已带后缀**的源码后缀与交易所推断是否自洽。

    这是本次踩到的真实坑：`605199`（沪主板）与 `688368`（科创板）被手写成 ``.SZ``
    时，``to_suffix`` 按「已是合法后缀格式」原样返回，于是名单里躺着一个
    永远不会被命中的键——少排一只，且毫无提示。
    """
    problems: list[str] = []
    candidates: list[tuple[str, str]] = []

    for item in (raw.get("news_blacklist") or {}).get("items") or []:
        if isinstance(item, dict):
            candidates.append(("news_blacklist", str(item.get("code") or "")))
    for code in (raw.get("live_symbols") or {}).get("block_buy") or []:
        candidates.append(("block_buy", str(code)))

    for source, code in candidates:
        m = _SUFFIXED.match(code.strip().upper())
        if not m:
            if code:
                problems.append(f"[{source}] 代码格式可疑（既非 6 位裸码也非合法后缀）：{code!r}")
            continue
        digits, actual = m.groups()
        inferred = StockCodeUtil.to_suffix(digits)
        if inferred != f"{digits}.{actual}":
            problems.append(
                f"[{source}] {code} 后缀与交易所不符：按代码规则应为 {inferred}"
                "（该键永远不会被命中 = 静默少排）"
            )
    return problems


def _bare_code_problems(raw: Mapping[str, Any]) -> list[str]:
    """检查**裸码**是否都能归一（归一出空串 = 该票直接消失，且无提示）。"""
    problems: list[str] = []
    groups = {
        "fundamental_flags": (raw.get("fundamental_flags") or {}).get("items") or {},
        "risk_block": (raw.get("risk_block") or {}).get("items") or {},
    }
    for source, items in groups.items():
        for code in items:
            if not StockCodeUtil.to_suffix(str(code).strip()):
                problems.append(f"[{source}] 裸码无法归一：{code!r}")
    return problems


def audit(raw: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """源 → 产物对账。返回 ``(errors, notes)``。

    **errors 拦写盘、notes 不拦**：解禁窗口过期是常态（正是设计的一部分），
    若把它算成 error，用户会习惯性加 ``--force``，审计就废了。只有
    「条数对不上 / 后缀写错交易所 / 产物为空」这类**静默少排**才是错误。
    """
    errors: list[str] = []
    notes: list[str] = []
    expected = _expected_counts(raw)
    actual = (payload.get("counts") or {}).get("by_source") or {}

    for source, want in expected.items():
        got = int(actual.get(source, 0))
        if got != want:
            errors.append(
                f"[{source}] 条数不符：源 {want} 条 → 入库 {got} 条"
                f"（少 {want - got} 条，检查代码归一是否碰撞）"
            )

    errors.extend(_suffix_problems(raw))
    errors.extend(_bare_code_problems(raw))

    if not (payload.get("items") or {}):
        errors.append("[*] 产物为空：四份源一条都没进去，检查源目录是否指对了")

    today = date.today().isoformat()
    expired = [
        sym
        for sym, it in (payload.get("items") or {}).items()
        if it.get("expire") and it["expire"] < today
    ]
    if expired:
        notes.append(
            f"{len(expired)} 条命中窗口已过期（不再参与排除，仍可解释）："
            f"{sorted(expired)[:6]}"
        )
    warn_only = sum(
        1 for it in (payload.get("items") or {}).values() if not it.get("blocking", True)
    )
    if warn_only:
        notes.append(f"{warn_only} 只只命中「只提示」类来源，不参与排除")
    return errors, notes


def _diff_against(path: Path, payload: Mapping[str, Any]) -> list[str]:
    """与盘上现产物做差异（dry-run 与覆盖前提示用）。"""
    if not path.is_file():
        return ["盘上无既有产物 → 本次为首次导入"]
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"盘上产物不可读（将整体覆盖）：{exc}"]
    old_items = set((old.get("items") or {}))
    new_items = set((payload.get("items") or {}))
    added, removed = new_items - old_items, old_items - new_items
    return [
        f"盘上产物 asof={old.get('asof')} total={len(old_items)}"
        f" → 本次 asof={payload.get('asof')} total={len(new_items)}",
        f"新增 {len(added)} 只，移除 {len(removed)} 只",
        *([f"  移除样例：{sorted(removed)[:8]}"] if removed else []),
    ]


def _print_report(
    payload: Mapping[str, Any], errors: list[str], notes: list[str], diff: list[str]
) -> None:
    counts = payload.get("counts") or {}
    print(f"\n名单基准日 asof={payload.get('asof') or '(未知)'}  "
          f"生成于 {payload.get('generated_at')}")
    print(f"合计 {counts.get('total', 0)} 只（其中参与排除 {counts.get('blocking', 0)} 只）")
    print("\n逐源条数：")
    for name, meta in sorted((payload.get("sources") or {}).items()):
        kind = "排除" if meta.get("blocking") else "只提示"
        print(f"  {name:<18} {meta.get('count', 0):>5} 条  [{kind}] {source_label(name)}"
              f"  asof={meta.get('asof') or '-'}")
    if diff:
        print("\n与盘上产物差异：")
        for line in diff:
            print(f"  {line}")
    if notes:
        print("\n提示：")
        for line in notes:
            print(f"  - {line}")
    if errors:
        print("\n[!] 审计发现问题：")
        for line in errors:
            print(f"  - {line}")
    else:
        print("\n[OK] 审计通过：逐源条数一致、后缀与交易所自洽。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入候选信号排除名单（通道 A）")
    parser.add_argument("--from", dest="source_root", default=DEFAULT_SOURCE_ROOT,
                        help=f"隔壁仓库根目录（默认 {DEFAULT_SOURCE_ROOT}）")
    parser.add_argument("--out", dest="out_dir", default=str(DEFAULT_OUT_DIR),
                        help=f"本仓产物目录（默认 {DEFAULT_OUT_DIR}）")
    parser.add_argument("--market", default="CN", help="市场（默认 CN）")
    parser.add_argument("--dry-run", action="store_true", help="只审计并打差异，不写盘")
    parser.add_argument("--force", action="store_true",
                        help="审计有问题也照样写盘（默认拒绝，避免半份名单上线）")
    args = parser.parse_args(argv)

    raw = load_sources(Path(args.source_root))
    raw["market"] = args.market
    payload = build_payload(raw, generated_at=now_iso())

    out_path = Path(args.out_dir) / f"{args.market.lower()}.json"
    errors, notes = audit(raw, payload)
    diff = _diff_against(out_path, payload)
    _print_report(payload, errors, notes, diff)

    if errors and not args.force:
        print("\n[拒绝写盘] 审计未通过。确认无误可加 --force。", file=sys.stderr)
        return 1
    if args.dry_run:
        print("\n[dry-run] 未写盘。")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换：读侧按 mtime 失效缓存，写一半会被读到半份名单
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    size_kb = out_path.stat().st_size / 1024
    print(f"\n[已写入] {out_path}（{size_kb:.0f} KB，容器内 /data/exclusions/"
          f"{out_path.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
