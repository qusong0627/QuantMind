#!/usr/bin/env python3
"""因子去重报告：同源因子簇清单 → Markdown + PDF（落到技能中心「报告档案」）。

为什么需要：429 个 Alpha 因子（L1/L2/L1+L2 同理）里大量是同源变体 ——
`a158_ROC20` 与 `gtja_088` 相关性 −0.978、`a101_040` 与 `gtja_042` 完全同源、
L2 的 `flow_sell_amount` 与 `flow_buy_amount` +0.97。一起进模型等于同一份信息数两遍。
本脚本按 |ρ| ≥ 阈值聚簇，每簇只留一个代表（默认 |ICIR| 最大），其余标为重复项。
各数据集另附**多样性体检**：全库 vs 去重后的有效因子数（N_eff = exp(特征值熵)），
回答「聚类去重会不会把多样性也削掉」。

用法：
  python backend/scripts/factor_dedup_report.py                      # 四个数据集全跑，阈值 0.9
  python backend/scripts/factor_dedup_report.py --dataset l2_factors --threshold 0.95
  python backend/scripts/factor_dedup_report.py --no-pdf             # 只出 Markdown

产出（技能中心「报告档案」可直接预览 PDF；目录与档案读取同源，见 archive_root()）：
  <报告档案根>/因子去重/因子去重报告_YYYYMMDD.md
  <报告档案根>/因子去重/因子去重报告_YYYYMMDD.pdf
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.engine.factor_report import service  # noqa: E402
from backend.services.engine.factor_report.clusters import summarize  # noqa: E402
from backend.services.engine.factor_report.datasets import DATASETS  # noqa: E402


# 报告档案根目录 —— 必须与 skills-center「报告档案」读取的**同一个**目录。
# 解析逻辑（含「建目录会改变解析结果」这条历史教训）已收口到 backend/shared/report_archive.py，
# 该模块自带单测覆盖解析顺序与「不得有建目录副作用」；本脚本只是调用方。
from backend.shared.report_archive import archive_root  # noqa: E402


OUT_DIR = archive_root() / "因子去重"


def _fmt(v, digits: int = 3, signed: bool = False) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if (signed and f >= 0) else ""
    return f"{sign}{f:.{digits}f}"


def build_dataset_section(dataset: str, threshold: float, keep: str, max_members: int) -> tuple[str, dict]:
    """生成单个数据集的去重章节，返回 (markdown, 统计)。"""
    res = service.correlation_clusters(dataset, threshold=threshold, keep=keep)
    label = str((DATASETS.get(dataset) or {}).get("label") or dataset)
    if not res.get("available"):
        return f"### {label}\n\n> 快照尚未生成（`build_factor_report.py --dataset {dataset}`），本次跳过。\n\n", {}

    summary = res["summary"]
    clusters = res["clusters"]
    lines: list[str] = []
    lines.append(f"### {label}")
    lines.append("")

    def _diversity_line(div: dict | None) -> str:
        """全库 vs 去重后的有效因子数（N_eff = exp(多样性熵)）。"""
        if not div or div.get("n_eff") is None:
            return ""
        keep = f"；去重后 {div['n_keep']} 个 ≈ **{div['n_eff_after']}** 个独立因子"
        change = ""
        if div.get("n_eff_after") is not None and div["n_eff"]:
            delta = (div["n_eff_after"] - div["n_eff"]) / div["n_eff"] * 100
            # N_eff 对「近重复成簇」是超线性惩罚：剔掉同源复制后有效因子数往往不降反升
            change = (
                f"（多样性 +{delta:.1f}%：剔除同源复制反而提升有效因子数）"
                if delta >= 0
                else f"（多样性损失 {abs(delta):.1f}%）"
            )
        return (
            f"**多样性**：全库 {div['n_total']} 个因子 ≈ **{div['n_eff']}** 个独立因子"
            f"（多样性熵 {div['entropy']}）{keep}{change}。"
        )

    if not clusters:
        lines.append(f"未发现 |ρ| ≥ {threshold} 的同源因子（{summary['n_total']} 个因子互不重复）。")
        div_line = _diversity_line(res.get("diversity"))
        if div_line:
            lines.append("")
            lines.append(div_line)
        lines.append("")
        return "\n".join(lines), summary

    lines.append(
        f"共 {summary['n_total']} 个因子，检出 **{summary['n_clusters']} 个同源簇**，"
        f"可去除 **{summary['n_duplicates']} 个重复因子**（保留 {summary['n_keep']} 个），"
        f"最大簇 {summary['largest_cluster']} 个成员。"
    )
    div_line = _diversity_line(res.get("diversity"))
    if div_line:
        lines.append("")
        lines.append(div_line)
    lines.append("")
    lines.append("| 代表因子（保留） | ICIR | 其余成员（建议剔除） | 与代表相关性 |")
    lines.append("|---|---|---|---|")
    for c in clusters:
        rep = c["representative"]
        rep_label = f"`{rep}`" + (f" {c['representative_display']}" if c.get("representative_display") else "")
        others = [m for m in c["members"] if not m["is_rep"]]
        shown = others[:max_members]
        cell = "<br/>".join(
            f"`{m['name']}`{(' ' + m['display_name']) if m.get('display_name') else ''} "
            f"(ICIR {_fmt(m['icir'], 2)})"
            for m in shown
        )
        if len(others) > len(shown):
            cell += f"<br/>… 另 {len(others) - len(shown)} 个"
        corrs = "<br/>".join(_fmt(m["corr_to_rep"], 3, signed=True) for m in shown)
        lines.append(f"| {rep_label} | {_fmt(c['representative_icir'], 3)} | {cell} | {corrs} |")
    lines.append("")
    return "\n".join(lines), summary


def build_markdown(datasets: list[str], threshold: float, keep: str, max_members: int) -> tuple[str, dict]:
    now = datetime.now()
    sections: list[str] = []
    totals = {"n_total": 0, "n_duplicates": 0, "n_keep": 0, "n_clusters": 0}
    for ds in datasets:
        md, s = build_dataset_section(ds, threshold, keep, max_members)
        sections.append(md)
        if s:
            for k in totals:
                totals[k] += int(s.get(k) or 0)

    head = [
        "# 因子去重体检报告",
        "",
        f"> 数据窗口：各数据集快照为准　|　同源阈值：|ρ| ≥ {threshold}　|　"
        f"保留口径：{'|ICIR| 最大' if keep == 'icir' else '|IC| 最大' if keep == 'abs_ic' else '多空价差最大'}　|　"
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 一、结论速览",
        "",
        f"- 全部数据集合计 **{totals['n_total']} 个因子**，检出 **{totals['n_clusters']} 个同源簇**，"
        f"建议剔除 **{totals['n_duplicates']} 个重复因子**，保留 {totals['n_keep']} 个。",
        "- 重复因子一起进模型 = 同一份信息数两遍：放大噪声、扭曲特征重要性、抬高过拟合风险。",
        "- 剔除原则：每个同源簇只保留一名代表（默认 |ICIR| 最高，即最稳的那个），其余为同源变体。",
        "- 反向因子同样视为同源（相关性取绝对值）——`a158_ROC20` 与 `gtja_088` 相关系数 −0.978，"
        "本质是同一公式的符号约定差异。",
        "",
        "## 二、各数据集去重明细",
        "",
    ]
    tail = [
        "## 三、方法说明",
        "",
        "- **相关性**：逐日横截面秩相关（Spearman）矩阵，在快照窗口（近 5 年）内按日平均。",
        "- **聚类**：以 |ρ| ≥ 阈值建图取连通分量。允许链式相连（A~B、B~C 可能并成一簇），"
        "因此每个成员都给出「与代表的相关性」——若明显低于阈值，说明它只是间接相连，可人工复核。",
        "- **代表选择**：默认保留 |ICIR| 最大者（稳定性优先），可选 `--keep abs_ic`（强度优先）。",
        "- **数据来源**：各数据集快照（`factor_report.json`），由 `build_factor_report.py` 生成；"
        "口径详见 `docs/因子报告_设计方案.md`。",
        "",
        "## 四、使用建议",
        "",
        "1. 训练前按本清单剔除重复项，再跑既有因子筛选（`factor_selection`，相关性阈值 0.9）——两者口径一致。",
        "2. 因子挖掘（RD-Agent）产出的新因子，先查它与代表的相关系数，避免「换个写法的老因子」入库。",
        "3. 阈值可调：0.9 偏保守（只去强同源），0.8 更激进（同类因子也归并），按特征预算取舍。",
        "",
        "---",
        "",
        "> 本报告由 QuantMind 自动生成，仅用于内部因子研究，不构成任何投资建议。",
    ]
    return "\n".join(head + sections + tail), totals


def main() -> int:
    ap = argparse.ArgumentParser(description="生成因子去重报告（Markdown + PDF）")
    ap.add_argument("--dataset", default="all", help="all 或单个数据集名")
    ap.add_argument("--threshold", type=float, default=0.9, help="|ρ| 阈值（默认 0.9）")
    ap.add_argument("--keep", default="icir", choices=["icir", "abs_ic", "ls"], help="每簇保留口径")
    ap.add_argument("--max-members", type=int, default=6, help="每簇表格里最多列几个重复项")
    ap.add_argument("--no-pdf", action="store_true", help="只出 Markdown")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认报告档案根）")
    args = ap.parse_args()

    datasets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    datasets = [d for d in datasets if d in DATASETS] or list(DATASETS)

    md_text, totals = build_markdown(datasets, args.threshold, args.keep, args.max_members)

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    md_path = out_dir / f"因子去重报告_{stamp}.md"
    md_path.write_text(md_text, encoding="utf-8")
    print(f"[ok] Markdown: {md_path}")

    if not args.no_pdf:
        try:
            from backend.scripts.md_to_pdf_report import main as md_to_pdf

            pdf_path = out_dir / f"因子去重报告_{stamp}.pdf"
            md_to_pdf(str(md_path), str(pdf_path))
            print(f"[ok] PDF: {pdf_path}（{pdf_path.stat().st_size / 1024:.0f} KB）")
        except Exception as e:  # noqa: BLE001 — PDF 失败不影响 Markdown 交付
            print(f"[warn] PDF 生成失败（Markdown 已产出）：{e}")
            return 1

    print(
        f"[汇总] 因子 {totals['n_total']} 个 → 同源簇 {totals['n_clusters']} 个，"
        f"建议剔除 {totals['n_duplicates']} 个，保留 {totals['n_keep']} 个"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
