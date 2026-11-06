"""汇总三个周期的训练报告为一张对比表（Markdown + 终端）。"""

from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime
from pathlib import Path

ROOT = Path("/data/stock_classifier")


def latest_report(horizon: int, tag: str) -> Path | None:
    files = sorted(
        glob.glob(str(ROOT / "reports" / f"stock_multiclass_h{horizon}_{tag}_*.json"))
    )
    return Path(files[-1]) if files else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="v1")
    parser.add_argument("--horizons", default="1,3,5")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    reports = {}
    for horizon in horizons:
        path = latest_report(horizon, args.tag)
        if path:
            reports[horizon] = json.loads(path.read_text("utf-8"))
    if not reports:
        raise SystemExit("没有找到报告")

    lines = [
        "# 全市场多分类模型 · 三周期汇总",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        "- 标签：逐日截面 5 等分；评估看截面 IC / 分层价差，不看分类准确率",
        "",
        "| 周期 | 切分 | n | 准确率 | macro-F1 | IC | ICIR | IC>0 占比 | 头尾价差 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon in horizons:
        r = reports.get(horizon)
        if not r:
            continue
        for split in ("valid", "test", "oos"):
            s = r["splits"].get(split)
            if not s:
                continue
            c, ic, dc = s["classification"], s["ic"], s["decile"]
            lines.append(
                f"| T+{horizon} | {split} | {c['n']} | {c['accuracy']:.3f} | "
                f"{c['macro_f1']:.3f} | {ic.get('ic_mean', 0):.4f} | "
                f"{ic.get('ic_ir', 0):.2f} | {ic.get('ic_pos_share', 0):.1%} | "
                f"{dc.get('top_minus_bottom', 0):+.4f} |"
            )
    lines += ["", "## 分层平均收益（0=最差，9=最好）", ""]
    for horizon in horizons:
        r = reports.get(horizon)
        if not r:
            continue
        for split in ("valid", "test", "oos"):
            bins = r["splits"].get(split, {}).get("decile", {}).get("bin_means", [])
            if bins:
                lines.append(
                    f"- **T+{horizon} {split}**："
                    + " / ".join(f"{x:+.4f}" for x in bins)
                )
    lines += ["", "## 高置信子集命中率", ""]
    for horizon in horizons:
        r = reports.get(horizon)
        if not r:
            continue
        for split in ("valid", "test", "oos"):
            sel = r["splits"].get(split, {}).get("selective", {})
            if sel:
                lines.append(
                    f"- **T+{horizon} {split}**："
                    + "，".join(
                        f"{k} n={v['n']} 命中={v['hit_rate']:.3f}"
                        for k, v in sel.items()
                    )
                )

    text = "\n".join(lines) + "\n"
    out = (
        Path(args.out)
        if args.out
        else ROOT / "reports" / f"summary_{args.tag}_{datetime.now():%Y%m%d_%H%M}.md"
    )
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n写出：{out}")


if __name__ == "__main__":
    main()
