#!/usr/bin/env python3
"""因子筛选：质量门槛 + 同源去重（多库联合：alpha_library ∪ tdxgs ∪ jq110 ∪ alpha360 ∪ l1_factors ∪ l2_factors ∪ factor_research）

输入（均已存在，无需重新计算因子值）：
  <dataset>/report/factor_report.json   —— 因子报告快照（IC/ICIR/换手 + 库内相关矩阵）
      数据集：alpha_library(429) / tdxgs(88) / jq110(109) / alpha360(360) /
              l1_factors(110) / l2_factors(211，2022 起——缺分区的库在早期日期按「无数据」参与，
              其列逐期成对排除，不再跳过整天）；l1_l2_factors 为两者拼接，不入筛选避免重复
  factor_research/{metrics.json,corr.parquet,monthly_scores.parquet}  —— 82 因子研究库
  <dataset>/dt=*/data.parquet            —— 月末采样做「跨库」相关（单遍联合，缓存 npz）

筛选逻辑：
  1. 质量门槛：|IC 均值| ≥ min_ic 且 |ICIR| ≥ min_icir（各库同一把尺）；
  2. 去重（贪心直接去重）：候选按强度（|ICIR|，同分比 |IC|）降序，逐个与**已保留**因子比
     直接相关 |ρ|（联合矩阵，月末截面 Spearman 均值）：≥ 阈值（默认 0.9）者剔除并标注
     duplicate_of（取其最相关的已保留因子）与 |ρ|；否则保留。
     不用并查集传递闭包——那会把「只通过链式中等相关相连」的因子误并成巨簇（曾出现
     318 成员巨型簇，CLOSE24 与代表 MOM60 直接 ρ 仅 0.54）。
  3. 已知同构对（a101≈gtja、TDXGS_MA≈a158_MA、JQ110_ROC≈a158_ROC 等）由直接相关自然捕获。

输出（<quantdb>/factor_research/screening/）：
  factor_selection.json       机器可读：kept（按库分组）/ dropped（原因/重复对象）/ 门槛与统计
  筛选报告_YYYYMMDD.md         人读报告（各库清单 + 去重明细）
  kept_features.txt           训练可直接消费的特征名清单（每行一个）
  cross_corr.npz              联合相关缓存（--refresh-cross 重算）

用法：
  python3 backend/scripts/screen_factors.py                       # 默认门槛（全库联合）
  python3 backend/scripts/screen_factors.py --min-ic 0.03 --min-icir 0.3 --corr 0.85
  python3 backend/scripts/screen_factors.py --skip-cross          # 只用库内矩阵（快，无跨库去重）
  python3 backend/scripts/screen_factors.py --libraries alpha_library,tdxgs   # 只筛部分库
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.services.engine.factor_research.catalog import BY_CODE  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

# factor_report 包 __init__ 会拉 FastAPI（宿主裸跑没有），按文件直载 clusters 模块（纯 numpy）
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "fr_clusters",
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "factor_report"
    / "clusters.py",
)
_fr_clusters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fr_clusters)
cluster_by_correlation = (
    _fr_clusters.cluster_by_correlation
)  # 供 --method cluster 备用（默认不用）

REPORT_DATASETS = [
    "alpha_library",
    "tdxgs",
    "jq110",
    "alpha360",
    "l1_factors",
    "l2_factors",
    "gap_mined",  # GAP_MINED_MARK
]
OURS = "factor_research"
DATASET_LABEL = {
    "alpha_library": "Alpha 库（Alpha101 / GTJA191 / Alpha158）",
    "tdxgs": "TDXGS 通达信技术指标",
    "jq110": "JQ110 聚宽因子",
    "alpha360": "Alpha360 原始量价回溯",
    "l1_factors": "L1 因子（量价/换手/波动等基础因子）",
    "l2_factors": "L2 因子（逐笔微观结构，2022 起）",
    "factor_research": "因子研究（行情+财务+行为）",
    "gap_mined": "空档挖掘因子（股东机构/财务PIT/板块概念/两融/L2二阶/新闻情绪）",  # GAP_MINED_MARK
}


def _dataset_dir(dataset: str) -> Path:
    return resolve_quantdb_dir() / "6_ml_datasets" / dataset


def _load_report(dataset: str) -> tuple[list[str], np.ndarray, dict[str, dict]]:
    rep = json.loads(
        (_dataset_dir(dataset) / "report" / "factor_report.json").read_text(
            encoding="utf-8"
        )
    )
    names = list(rep["correlation"]["factors"])
    matrix = np.asarray(rep["correlation"]["matrix"], dtype=np.float64)
    by_name = {f["name"]: f for f in rep["factors"]}
    metrics = {}
    for n in names:
        f = by_name.get(n) or {}
        sub = f.get("library") or dataset  # 报告里的子库（alpha101/gtja191/alpha158）
        metrics[n] = {
            "ic_mean": f.get("ic_mean"),
            "icir": f.get("icir"),
            "turnover": f.get("turnover"),
            "display_name": f.get("display_name") or n,
            "library": dataset,
            "sublibrary": sub,
        }
    return names, matrix, metrics


def _load_ours() -> tuple[list[str], np.ndarray, dict[str, dict]]:
    root = resolve_quantdb_dir() / OURS
    m = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    metrics_json = m.get("metrics", {})
    names = sorted(metrics_json)
    corr_df = pd.read_parquet(root / "corr.parquet")
    idx = {n: i for i, n in enumerate(names)}
    M = np.eye(len(names))
    for r in corr_df.to_dict("records"):
        i, j = idx.get(r["factor_a"]), idx.get(r["factor_b"])
        if i is None or j is None or r["corr"] is None:
            continue
        M[i, j] = M[j, i] = float(r["corr"])
    metrics = {}
    for n in names:
        k = metrics_json[n]
        blob = BY_CODE.get(n) or {}
        metrics[n] = {
            "ic_mean": k.get("ic_mean"),
            "icir": k.get("ic_ir"),
            "turnover": None,
            "display_name": blob.get("name_cn") or n,
            "library": OURS,
            "sublibrary": f"{blob.get('l1', '')}/{blob.get('l2', '')}".strip("/"),
        }
    return names, M, metrics


def _union_cross_corr(
    blocks: list[tuple[str, list[str]]], our_names: list[str], max_dates: int = 0
) -> tuple[np.ndarray, int]:
    """单遍联合相关：各库分区 × factor_research 打分，月末截面 Spearman 逐期均值。

    blocks: [(dataset, 因子名列表)]；返回 (全库联合相关均值矩阵, 使用的期数)。
    列顺序 = blocks 依次拼接 + our_names。
    """
    root = resolve_quantdb_dir()
    scores = pd.read_parquet(
        root / OURS / "monthly_scores.parquet",
        columns=["trade_date", "symbol", "factor_code", "score"],
    )
    scores["symbol"] = scores["symbol"].astype("category")
    scores["factor_code"] = scores["factor_code"].astype("category")
    groups = {pd.Timestamp(d): g for d, g in scores.groupby("trade_date", sort=True)}
    dates = sorted(groups)
    if max_dates:
        dates = dates[-max_dates:]

    all_names = [n for _, names in blocks for n in names] + list(our_names)
    n_all = len(all_names)
    acc = np.zeros((n_all, n_all))
    cnt = np.zeros((n_all, n_all))
    offsets = []
    off = 0
    for _, names in blocks:
        offsets.append((off, off + len(names)))
        off += len(names)
    used = 0
    t_batch = time.time()
    ds_names = [n for _, names in blocks for n in names]
    for d in dates:
        dt = pd.Timestamp(d).strftime("%Y%m%d")
        comb = None
        for (ds, names), _span in zip(blocks, offsets, strict=True):
            f = root / "6_ml_datasets" / ds / f"dt={dt}" / "data.parquet"
            if not f.exists():
                continue  # 该库当日期无分区（如 L2 早于 2022）→ 其列本期为 NaN，成对排除
            try:
                part = pd.read_parquet(f, columns=["symbol", *names]).set_index(
                    "symbol"
                )
            except Exception:  # noqa: BLE001
                continue
            comb = part if comb is None else comb.join(part, how="inner")
        if comb is None:
            continue
        comb = comb.reindex(columns=ds_names)  # 缺失库的列补 NaN
        our_d = groups[d].pivot_table(
            index="symbol", columns="factor_code", values="score", aggfunc="last"
        )
        our_d = our_d.reindex(columns=our_names)
        comb = comb.join(our_d, how="inner").reindex(columns=[*ds_names, *our_names])
        if len(comb) < 60:
            continue
        rk = comb.rank()
        cm = rk.corr(min_periods=50).to_numpy(dtype=np.float64)
        good = np.isfinite(cm)
        acc[good] += cm[good]
        cnt[good] += 1
        used += 1
        if used % 10 == 0:
            print(
                f"      {used}/{len(dates)} 期（{time.time() - t_batch:.0f}s/10期）",
                flush=True,
            )
    with np.errstate(invalid="ignore"):
        mean = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    np.fill_diagonal(mean, 1.0)
    return mean, used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--min-ic", type=float, default=0.02, help="|IC 均值| 门槛（默认 0.02）"
    )
    ap.add_argument(
        "--min-icir", type=float, default=0.2, help="|ICIR| 门槛（默认 0.2）"
    )
    ap.add_argument(
        "--corr", type=float, default=0.9, help="去重相关阈值 |ρ|（默认 0.9）"
    )
    ap.add_argument(
        "--skip-cross", action="store_true", help="跳过跨库相关（仅库内矩阵，快）"
    )
    ap.add_argument(
        "--refresh-cross", action="store_true", help="忽略联合相关缓存，重算"
    )
    ap.add_argument(
        "--libraries",
        default=",".join([*REPORT_DATASETS, OURS]),
        help="参与筛选的库（逗号分隔；默认全部）",
    )
    args = ap.parse_args()
    wanted = [x.strip() for x in args.libraries.split(",") if x.strip()]

    t0 = time.time()
    names_all: list[str] = []
    metrics: dict[str, dict] = {}
    blocks: list[tuple[str, list[str], np.ndarray]] = []  # (dataset, names, 库内矩阵)
    use_ours = OURS in wanted
    for ds in REPORT_DATASETS:
        if ds not in wanted:
            continue
        rep_file = _dataset_dir(ds) / "report" / "factor_report.json"
        if not rep_file.exists():
            print(
                f"      ⚠ {ds} 无因子报告快照，跳过（先跑 build_factor_report.py --dataset {ds}）"
            )
            continue
        nms, mat, mets = _load_report(ds)
        print(f"[1/5] {ds}: {len(nms)} 因子（报告矩阵 {mat.shape}）")
        blocks.append((ds, nms, mat))
        names_all.extend(nms)
        metrics.update(mets)
    if use_ours:
        nms, mat, mets = _load_ours()
        print(f"[2/5] {OURS}: {len(nms)} 因子")
        blocks.append((OURS, nms, mat))
        names_all.extend(nms)
        metrics.update(mets)
    if not names_all:
        raise SystemExit("没有可筛选的库（--libraries 为空或缺报告快照）")

    dup_names = len(names_all) - len(set(names_all))
    if dup_names:
        raise SystemExit(f"存在跨库重名 {dup_names} 个（命名空间冲突）")

    # ---- 联合相关矩阵 ----
    off = {}
    pos = 0
    for ds, nms, _ in blocks:
        off[ds] = (pos, pos + len(nms))
        pos += len(nms)
    M = np.full((len(names_all), len(names_all)), np.nan)
    if args.skip_cross or not use_ours:
        note = (
            "--skip-cross"
            if args.skip_cross
            else "未选 factor_research（跨库相关以其打分为基准）"
        )
        print(f"[3/5] 跳过跨库相关（{note}）：库内矩阵拼接")
        for ds, _nms, mat in blocks:
            a, b = off[ds]
            M[a:b, a:b] = mat
        cross_note = f"未计算跨库（{note}；仅库内去重）"
    else:
        cache_file = resolve_quantdb_dir() / OURS / "screening" / "cross_corr.npz"
        cache_hit = False
        if cache_file.exists() and not args.refresh_cross:
            try:
                z = np.load(cache_file, allow_pickle=False)
                valid = "names" in z and "matrix" in z and "used" in z
            except Exception:  # noqa: BLE001
                valid = False
            if valid and [str(x) for x in z["names"]] == names_all:
                M = z["matrix"]
                cross_note = f"联合月末截面 Spearman 均值（{int(z['used'])} 期，缓存）"
                print(f"[3/5] 载入联合相关缓存（{int(z['used'])} 期）")
                cache_hit = True
            else:
                print("[3/5] 联合相关缓存缺失/不兼容/与本次库集合不一致，重算")
        if not cache_hit:
            cross_blocks = [(ds, nms) for ds, nms, _ in blocks if ds != OURS]
            our_names = [n for ds, nms, _ in blocks if ds == OURS for n in nms]
            if cross_blocks:
                print(
                    "[3/5] 计算联合跨库相关（各库分区 × factor_research 打分，月末截面）..."
                )
                M, used = _union_cross_corr(cross_blocks, our_names)
            else:
                M, used = _union_cross_corr([(OURS, our_names)], [])
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                cache_file, matrix=M, used=used, names=np.array(names_all, dtype=str)
            )
            cross_note = f"联合月末截面 Spearman 均值（{used} 期）"
            print(
                f"      完成：{used} 期 × {M.shape}（{time.time() - t0:.0f}s，已缓存）"
            )

    # ---- 门槛 + 联合去重 ----
    print("[4/5] 质量门槛 + 联合去重 ...")
    gated_out: dict[str, str] = {}
    candidates: list[str] = []
    for n in names_all:
        m = metrics[n]
        ic, icir = m.get("ic_mean"), m.get("icir")
        if ic is None or icir is None:
            gated_out[n] = "无 IC/ICIR 指标（未计算或退化）"
            continue
        if abs(ic) < args.min_ic:
            gated_out[n] = f"|IC|={abs(ic):.4f} < {args.min_ic}"
            continue
        if abs(icir) < args.min_icir:
            gated_out[n] = f"|ICIR|={abs(icir):.3f} < {args.min_icir}"
            continue
        candidates.append(n)

    idx_all = {n: i for i, n in enumerate(names_all)}

    def _strength(n: str) -> float:
        m = metrics[n]
        return abs(float(m.get("icir") or 0)) * 1000 + abs(float(m.get("ic_mean") or 0))

    order = sorted(candidates, key=_strength, reverse=True)
    kept: list[str] = []
    dup_of: dict[str, tuple[str, float]] = {}
    for n in order:
        i = idx_all[n]
        hits: list[tuple[str, float]] = []
        for k in kept:
            rho = M[i, idx_all[k]]
            if np.isfinite(rho) and abs(rho) >= args.corr:
                hits.append((k, abs(float(rho))))
        if hits:
            rep, cc = max(
                hits, key=lambda x: x[1]
            )  # 与本次最相关的已保留因子作为「重复于」
            dup_of[n] = (rep, cc)
        else:
            kept.append(n)

    # 报告视图：按代表归组的直接去重明细（无链式传递）
    groups: dict[str, list[tuple[str, float]]] = {}
    for n, (rep, cc) in dup_of.items():
        groups.setdefault(rep, []).append((n, cc))
    cluster_rows = [
        {
            "representative": rep,
            "size": len(members) + 1,
            "members": [
                {"name": rep, "is_rep": True, "corr_to_rep": 1.0},
                *[
                    {"name": n, "is_rep": False, "corr_to_rep": round(cc, 3)}
                    for n, cc in sorted(members, key=lambda x: -x[1])
                ],
            ],
        }
        for rep, members in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]
    n_clusters = len(cluster_rows)
    print(
        f"      候选 {len(candidates)} → 保留 {len(kept)}"
        f"（门槛剔除 {len(gated_out)}，去重剔除 {len(dup_of)}，{n_clusters} 组同源）"
    )

    # ---- 落盘 ----
    print("[5/5] 落盘 ...")
    out_dir = resolve_quantdb_dir() / OURS / "screening"
    out_dir.mkdir(parents=True, exist_ok=True)

    def _row(n: str) -> dict:
        m = metrics[n]
        return {
            "name": n,
            "display_name": m.get("display_name"),
            "library": m.get("library"),
            "sublibrary": m.get("sublibrary"),
            "ic_mean": m.get("ic_mean"),
            "icir": m.get("icir"),
            "turnover": m.get("turnover"),
        }

    selected = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "dedup_method": "greedy_direct_corr",
        "gates": {
            "min_abs_ic": args.min_ic,
            "min_abs_icir": args.min_icir,
            "corr_threshold": args.corr,
        },
        "libraries": [ds for ds, _, _ in blocks],
        "cross_corr": cross_note,
        "counts": {
            "candidates": len(candidates),
            "kept": len(kept),
            "gated_out": len(gated_out),
            "deduped": len(dup_of),
            "total_considered": len(names_all),
        },
        "kept": [_row(n) for n in kept],
        "dropped_gated": [
            {"name": n, "library": metrics[n]["library"], "reason": r}
            for n, r in sorted(gated_out.items())
        ],
        "dropped_duplicate": [
            {
                "name": n,
                "library": metrics[n]["library"],
                "duplicate_of": rep,
                "abs_corr": round(cc, 3),
            }
            for n, (rep, cc) in sorted(dup_of.items())
        ],
        "clusters": cluster_rows,
        "cluster_summary": {
            "n_groups": n_clusters,
            "n_duplicates": len(dup_of),
            "largest_group": cluster_rows[0]["representative"]
            if cluster_rows
            else None,
            "largest_group_size": cluster_rows[0]["size"] if cluster_rows else 0,
        },
    }
    (out_dir / "factor_selection.json").write_text(
        json.dumps(selected, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / "kept_features.txt").write_text("\n".join(kept) + "\n", encoding="utf-8")

    # ---- 报告 md ----
    lines = []
    lines.append(f"# 因子筛选报告（{pd.Timestamp.now().strftime('%Y-%m-%d')}）")
    lines.append("")
    libs_label = " ∪ ".join(
        DATASET_LABEL.get(ds, ds) + f"（{len(nms)}）" for ds, nms, _ in blocks
    )
    lines.append(f"> 来源：{libs_label}")
    lines.append(
        f"> 门槛：|IC 均值| ≥ {args.min_ic}，|ICIR| ≥ {args.min_icir}；去重：联合相关 |ρ| ≥ {args.corr} 每簇留最优（|ICIR|）"
    )
    lines.append(f"> 相关：{cross_note}")
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append("| 项 | 数量 |")
    lines.append("|---|---|")
    lines.append(f"| 参与筛选 | {len(names_all)} |")
    lines.append(f"| 过门槛候选 | {len(candidates)} |")
    lines.append(f"| **最终保留** | **{len(kept)}** |")
    lines.append(f"| 门槛剔除 | {len(gated_out)} |")
    lines.append(f"| 同源去重剔除 | {len(dup_of)}（{n_clusters} 组） |")
    lines.append("")
    for ds, nms, _ in blocks:
        rows = [n for n in kept if metrics[n]["library"] == ds]
        kept_ratio = len(rows) / max(len(nms), 1)
        lines.append(
            f"## 保留清单 · {DATASET_LABEL.get(ds, ds)}（{len(rows)}/{len(nms)} = {kept_ratio:.0%}）"
        )
        lines.append("")
        if not rows:
            lines.append("（无）")
            lines.append("")
            continue
        sub_col = ds == "alpha_library"
        head = (
            "| # | 因子 |"
            + (" 子库 |" if sub_col else "")
            + " 说明 | IC 均值 | ICIR | 换手 |"
        )
        sep = "|---|---|" + ("---|" if sub_col else "") + "---|---|---|---|"
        lines.append(head)
        lines.append(sep)
        for i, n in enumerate(rows, 1):
            m = metrics[n]
            tv = f"{m['turnover']:.2f}" if m.get("turnover") is not None else "—"
            mid = f" {m.get('sublibrary')} |" if sub_col else ""
            lines.append(
                f"| {i} | `{n}` |{mid} {m.get('display_name')} | {m.get('ic_mean')} | {m.get('icir')} | {tv} |"
            )
        lines.append("")
    lines.append(
        f"## 去重剔除（{len(dup_of)}）—— 与保留因子直接相关 |ρ| ≥ {args.corr}，**不要重复进训练**"
    )
    lines.append("")
    lines.append(
        "> 贪心直接去重：每个剔除项与「重复于」的保留因子直接相关 ≥ 阈值（非链式传递）。"
    )
    lines.append("")
    lines.append("| 因子 | 库 | 重复于 | \\|ρ\\| | 说明 |")
    lines.append("|---|---|---|---|---|")
    for n, (rep, cc) in sorted(dup_of.items(), key=lambda x: -x[1][1]):
        lines.append(
            f"| `{n}` | {metrics[n]['library']} | `{rep}` | {cc:.3f} | {metrics[n].get('display_name')} |"
        )
    lines.append("")
    lines.append(f"### 同源组一览（{n_clusters} 组，按组大小降序，前 20）")
    lines.append("")
    for c in cluster_rows[:20]:
        mems = "、".join(f"`{m['name']}`" for m in c["members"] if not m["is_rep"])
        rep = c["representative"]
        lines.append(f"- **{rep}**（保留）← 直接重复 {c['size'] - 1} 个：{mems}")
    lines.append("")
    lines.append("## 训练接入")
    lines.append("")
    lines.append(
        "- 特征清单：`kept_features.txt`（每行一个因子名；跨库使用时按所属数据集分别取列）"
    )
    lines.append(
        "- 机器可读结果：`factor_selection.json`（kept / dropped 原因 / 簇结构）"
    )
    lines.append(
        "- 提醒：>0.9 相关的一对因子只保留一个；若嫌去重过狠，可用 `--corr 0.95` 收紧阈值。"
    )
    lines.append("")
    md_name = f"筛选报告_{pd.Timestamp.now().strftime('%Y%m%d')}.md"
    (out_dir / md_name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"完成 → {out_dir}（{time.time() - t0:.0f}s）")
    print(f"  保留 {len(kept)} 个；报告 {md_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
