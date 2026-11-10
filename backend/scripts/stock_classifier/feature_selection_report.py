#!/usr/bin/env python3
"""把日频特征选择 JSON 渲染成可读报告（Markdown + CSV）。

用法：
    python3 backend/scripts/stock_classifier/feature_selection_report.py \
        --selection data/stock_classifier/feature_selection/daily_feature_selection_2016_2024.json \
        --top 100 --out docs/日频T3T5_100特征选择_20261110.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _f(v, digits=3):
    return "—" if v is None else f"{v:.{digits}f}"


def _pct(v):
    return "—" if v is None else f"{v:.0%}"


def _ratio(m: dict) -> str:
    null = m.get("null_gain")
    g = m.get("gain_ratio")
    if (null is not None and abs(float(null)) < 1e-6) or (
        null is None and g is not None and float(g) > 1000
    ):
        return "null≈0"
    return "—" if g is None else f"{g:.1f}×"


def _reason(e: dict, m: dict) -> str:
    cap = m.get("cap_ic") or [None, None, None]
    return (
        f"{e.get('group')}｜{e.get('library')}｜T+5 ICIR {_f(m.get('icir5'), 2)}"
        f"（|IC| {_f(abs(m.get('ic5') or 0))}，同号率 {_pct(m.get('pos5'))}）"
        f"｜T+1 IC {_f(m.get('ic1'))}/{_f(m.get('ic1_cc'))} 同号"
        f"｜cap 三档 {'/'.join(_f(x) for x in cap)}"
        f"｜LGB gain 第 {m.get('gain_rank')}（{_ratio(m)}）"
        f"｜覆盖 {_pct(m.get('coverage'))}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", required=True)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data = json.loads(Path(args.selection).read_text(encoding="utf-8"))
    entries = data["selected"][str(args.top)]
    metrics = data.get("metrics", {})
    proto = data.get("protocol", {})
    lines = [
        f"# 日频 T+3 / T+5 模型：Top {args.top} 特征选择报告",
        "",
        f"> 生成时间：{data.get('generated_at')}　选择器：`backend/scripts/stock_classifier/select_daily_features.py`",
        "",
        "## 一、口径（先对账，再看名单）",
        "",
        f"- 候选：因子研究私有库 4585 → |ρ|≥0.9 去重 → 截止 **{proto.get('tag_cutoff')}** 的时效标签"
        "（剔除近期失效/持续低效）→ 过滤原始 OHLCV/泄漏列/低覆盖 → "
        f"**{data.get('protocol', {}).get('coarse_n')}** 个粗筛；",
        "- 粗筛：月度 |ICIR| + 类别配额（量价 35% / 基本面 20% / L2 15% / 资金流 10% / 风格 5% / 其他 15%）；"
        "类别优先取因子研究内置分类（`taxonomy`，代码正则锚定），仅 `other` 才按来源库/关键词兜底——"
        "该分组只服务配额，不是学术分类；",
        f"- 日频采样：{proto.get('n_sample_days')} 个截面（每 {proto.get('sample_step')} 个交易日，"
        f"{proto.get('train')[0]} ~ {proto.get('train')[1]}），全截面算 T+5/T+3/T+1 RankIC，按市值三档分别算；",
        f"- LGB：cap 平衡抽样 {proto.get('n_lgb_rows')} 行，T+5 截面 rank 标签；"
        "gain + 打乱标签的 null gain；T+1 同号做「隔天买入能力」硬门槛；",
        f"- 相关性终检：|ρ|≥{proto.get('dedup')} 剔除；类别配额后取 Top {args.top}；",
        f"- 训练/验证/测试：{proto.get('train')} / {proto.get('valid')} / {proto.get('test')}；"
        "2025/2026 **不参与**选特征。",
        "",
        "## 二、分布",
        "",
        "| 类别 | 数量 | 来源库 | 数量 |",
        "|---|---:|---|---:|",
    ]
    g = Counter(e.get("group", "") for e in entries)
    lib = Counter(e.get("library", "") for e in entries)
    gl = sorted(g.items(), key=lambda x: -x[1])
    ll = sorted(lib.items(), key=lambda x: -x[1])
    for i in range(max(len(gl), len(ll))):
        a = f"{gl[i][0]} | {gl[i][1]}" if i < len(gl) else " | "
        b = f"{ll[i][0]} | {ll[i][1]}" if i < len(ll) else " | "
        lines.append(f"| {a} | {b} |")
    lines += [
        "",
        "## 三、Top 特征清单（按选择顺序）",
        "",
        "| # | 因子 | 类别 | 库 | T+5 IC | ICIR5 | T+1 IC(执行/收盘) | 市值三档 IC | 覆盖 | LGB gain 排名 | real/null |",
        "|---:|---|---|---|---:|---:|---|---|---:|---:|---:|",
    ]
    for i, e in enumerate(entries, 1):
        m = metrics.get(e["code"], {})
        cap = m.get("cap_ic") or [None, None, None]
        cap_s = "/".join(_f(x) for x in cap)
        lines.append(
            f"| {i} | `{e['code']}` | {e.get('group')} | {e.get('library')} | "
            f"{_f(m.get('ic5'))} | {_f(m.get('icir5'), 2)} | "
            f"{_f(m.get('ic1'))} / {_f(m.get('ic1_cc'))} | {cap_s} | "
            f"{_pct(m.get('coverage'))} | {m.get('gain_rank')} | {_ratio(m)} |"
        )
    lines += ["", "## 四、逐条理由", ""]
    for i, e in enumerate(entries, 1):
        lines.append(
            f"{i}. **`{e['code']}`** — {_reason(e, metrics.get(e['code'], {}))}"
        )
    rejected = data.get("rejected", [])
    if rejected:
        lines += [
            "",
            "## 五、未入选（前 30）",
            "",
            "| 因子 | 类别 | 原因 |",
            "|---|---|---|",
        ]
        for r in rejected[:30]:
            lines.append(f"| `{r['code']}` | {r.get('group')} | {r.get('reason')} |")
    lines += [
        "",
        "## 六、边界（必须随名单一起读）",
        "",
        "- 这是**样本内选特征**：用 2016–2024 的 IC/ICIR 与 LGB importance；2025 只做验证，2026 只做测试。",
        "- 日频采样为每 20 个交易日一个截面；ICIR 的样本数约 104，不等同于逐日全样本 ICIR。",
        "- T+1 门槛同时看「次日开盘买、次日收盘卖」与「次日收盘对当日收盘」两口径；"
        "满足其一且与 T+5 同号即通过，不是保证实盘可成交。",
        "- 因子研究库自带的「近期失效/持续低效」是全窗口（到 2026）标签；本报告按截止 2024-12 重算，"
        "所以名单与 UI 上的 1500 个候选不是同一批。",
        "- LGB gain 对高基数/连续特征有偏好，故叠加 null gain 比值与 cap 三档同号；"
        "最终模型仍要 2026 test + 扣费 + 分层回测验证。",
        "",
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
