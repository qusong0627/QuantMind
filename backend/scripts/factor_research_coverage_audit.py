#!/usr/bin/env python3
"""因子研究 coverage 审计：按**因子库**统计每月「行情有票、该库无行」的比例。

为什么需要单独一层：构建器里的 ``survivorship_bias`` 用**所有因子库的并集**判断
「该日有没有任何因子值」，PIT 库（factor_dict_mined_full）覆盖了退市股时，并集
永远满员 —— 2016–2019 实际缺失 6–8% 退市股，却被标成无缺口。本脚本逐库统计
（行存在性，是缺口的**下界**），取最差库的月缺口作为该月退市覆盖缺口。

用法（仓库根）：
    python3 backend/scripts/factor_research_coverage_audit.py --dataset private --market CN
    python3 backend/scripts/factor_research_coverage_audit.py --dataset private --check  # 只打印

写回 ``coverage.json``：每月新增 ``library_missing`` / ``worst_library_missing`` /
``worst_library``，并重算顶层 ``survivorship_bias``（>threshold 即 true）。
classic 因子由 kline 现算，无因子库缺口，跳过。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.services.engine.factor_research import data as frdata  # noqa: E402
from backend.services.engine.factor_research import discovery  # noqa: E402
from backend.services.engine.factor_research import store  # noqa: E402
from backend.services.engine.factor_research import windows as frwindows  # noqa: E402


def _symbols(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["symbol"])
    except Exception:  # noqa: BLE001 - 单库读取失败按无数据处理，不拖垮审计
        return None
    return {str(s) for s in df["symbol"].dropna().unique()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="private", choices=("private", "classic"))
    ap.add_argument("--market", default="CN")
    ap.add_argument("--threshold", type=float, default=0.05)
    ap.add_argument("--check", action="store_true", help="只打印，不写回 coverage.json")
    args = ap.parse_args()
    if args.dataset != "private":
        print("classic 因子由 kline 现算，无因子库缺口，跳过")
        return 0
    mk = args.market.upper()
    root = store.artifact_dir("private", mk)
    cov_path = root / frwindows.COVERAGE_FILE
    if not cov_path.exists():
        print(f"coverage.json 缺失: {cov_path}")
        return 1
    cov = json.loads(cov_path.read_text(encoding="utf-8"))
    qroot = frdata._market_root(mk)  # noqa: SLF001 - 路径解析唯一入口在 data 层
    cat = store.factors_meta("private", mk) or {}
    meta = cat.get("meta", {})
    # 来源库从**目录全量**反查（append 块的 meta.sources 只记最后一块；盘扫会带进
    # _staging/标签库）。wind_source 前缀是 discovery 的唯一约定。
    libs = sorted(
        {
            discovery.source_of(str(e.get("wind_source", "")))
            for e in cat.get("factors", [])
        }
        - {""}
    )
    if not libs:  # 目录为空/旧格式兜底
        libs = [k for k in (meta.get("sources") or {}) if k != "factor_research"]
    updated = 0
    for month in cov.get("months", []):
        date = str(month["date"])
        dt = date.replace("-", "")
        kline = _symbols(qroot / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet")
        if not kline:
            continue
        ratios: dict[str, float] = {}
        for lib in libs:
            if lib.startswith("_"):
                continue
            f = qroot / "6_ml_datasets" / lib / f"dt={dt}" / "data.parquet"
            syms = _symbols(f)
            if syms is None:  # 该日无该库分区（库尚未开始）→ 不计入缺口
                continue
            if not syms:
                continue
            missing = len(kline - syms)
            ratios[lib] = round(missing / len(kline), 4)
        if not ratios:
            continue
        worst_lib = max(ratios, key=ratios.get)
        month["library_missing"] = {k: v for k, v in ratios.items() if v > 0}
        month["worst_library"] = worst_lib
        month["worst_library_missing"] = ratios[worst_lib]
        updated += 1
    befores = int(bool(cov.get("survivorship_bias")))
    cov["survivorship_bias"] = any(
        float(m.get("worst_library_missing") or 0) > args.threshold
        for m in cov.get("months", [])
    )
    cov["survivorship_threshold"] = args.threshold
    cov["audit"] = {
        "method": "per_library_row_presence_lower_bound",
        "libraries": libs,
        "months_updated": updated,
        "updated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }
    cov.setdefault("notes", []).append(
        "survivorship_bias 以『最差因子库的行缺口』判定（行存在性是下界）；"
        "并集覆盖不能代表每个因子都覆盖退市股。"
    )
    print(
        json.dumps(
            {
                "updated_months": updated,
                "survivorship_bias_before": befores,
                "survivorship_bias_after": int(bool(cov["survivorship_bias"])),
                "worst_months": sorted(
                    (
                        {
                            "date": m["date"],
                            "lib": m.get("worst_library"),
                            "ratio": m.get("worst_library_missing"),
                        }
                        for m in cov.get("months", [])
                        if m.get("worst_library_missing")
                    ),
                    key=lambda x: float(x["ratio"] or 0),
                    reverse=True,
                )[:8],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    if not args.check:
        tmp = cov_path.with_name(cov_path.name + ".tmp")
        tmp.write_text(json.dumps(cov, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, cov_path)
        # 同步目录 meta：外部读取 factors.json.meta 的地方与 coverage 保持一致
        cat_path = root / "factors.json"
        if cat_path.exists():
            try:
                cat = json.loads(cat_path.read_text(encoding="utf-8"))
                cat.setdefault("meta", {})["survivorship_bias"] = bool(
                    cov["survivorship_bias"]
                )
                cat["meta"]["coverage_audit"] = cov["audit"]
                ct = cat_path.with_name(cat_path.name + ".tmp")
                ct.write_text(
                    json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                os.replace(ct, cat_path)
            except (OSError, ValueError, KeyError):
                pass
        print(f"写回 {cov_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
