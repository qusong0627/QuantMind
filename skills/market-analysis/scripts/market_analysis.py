#!/usr/bin/env python3
"""市场分析取数脚本 — 复用后端 market-analysis 模块的 QuantDB 聚合口径。

输出（默认 data/reports/market_analysis/）：
  {date}_facts.json   全部原始数据（AI 解读依据）
  {date}_report.md    结构化报告骨架（数据表格齐全 + AI 解读占位）

用法:
  python3 market_analysis.py                # 最新交易日
  python3 market_analysis.py --out /tmp/ma  # 自定义输出目录
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from backend.services.api.market_analysis import quantdb_feed as qf  # noqa: E402


def _yi(v: float) -> str:
    """元 -> 亿元（保留 2 位）"""
    return f"{v / 1e8:.2f}"


def _pct(v: float) -> str:
    return f"{v:+.2f}%"


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    """Markdown 表格（数字列右对齐占位由渲染端处理，此处统一 markdown 表格）"""
    if not rows:
        return "_（无数据）_"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def fetch() -> dict:
    """清缓存后抓取市场分析全部数据段。"""
    qf.clear_cache()
    data = {
        "trade_date": None,
        "indices": qf.get_indices_overview(),
        "breadth": qf.get_market_breadth(),
        "heatmap_shenwan": qf.get_sector_heatmap("shenwan"),
        "heatmap_concept": qf.get_sector_heatmap("concept"),
        "stock_flow": qf.get_stock_money_flow(limit=20),
        # 个股资金流全量（供净流出 Top10 使用；接口按净额降序截断，须传 ≥ 全市场股票数的
        # limit 才能把真正的净流出头部（最负）包含进来，否则只拿到接近 0 的负值）
        "stock_flow_all": qf.get_money_flow_period("1d", "stock", "shenwan", 6000),
        "flow_1d": qf.get_money_flow_period("1d", "sector", "shenwan", 31),
        "flow_5d": qf.get_money_flow_period("5d", "sector", "shenwan", 31),
        "flow_10d": qf.get_money_flow_period("10d", "sector", "shenwan", 31),
        "tag_stats": qf.get_tag_stats(limit=30),
        "sector_multiday": fetch_sector_multiday(),
    }
    data["trade_date"] = data["breadth"].get("trade_date") or str(
        pd.Timestamp.now().date()
    )
    return data


def fetch_sector_multiday() -> dict:
    """行业多日涨跌幅对比：1/3/5 日累计涨幅（成分股中位数口径）。

    读 daily_unadjusted 最近 6 个交易日，按申万行业分组聚合。
    返回 {"latest": str, "items": [{name, pct_1d, pct_3d, pct_5d}, ...]}
    """
    latest = qf._latest_trade_date()
    dates = qf._trading_days(latest, 6)  # 降序，[0]=最新
    if len(dates) < 2:
        return {"latest": latest, "items": []}
    dt_in = ",".join(dates)
    df = qf._q(
        f"SELECT symbol, dt, close FROM qdb_daily_unadjusted WHERE dt IN ({dt_in})"
    )
    if df.empty:
        return {"latest": latest, "items": []}
    df["dt"] = df["dt"].astype(str)
    piv = df.pivot_table(index="symbol", columns="dt", values="close")
    cols = [c for c in dates if c in piv.columns]  # 保持日期顺序
    if len(cols) < 2:
        return {"latest": latest, "items": []}

    close_t0 = piv[cols[0]]

    def _pct(offset: int) -> pd.Series:
        idx = len(cols) - 1 - offset
        if idx < 0:
            return pd.Series(dtype=float)
        prev = piv[cols[idx]]
        return (close_t0 / prev - 1) * 100

    p1 = _pct(0)
    p3 = _pct(2) if len(cols) >= 3 else pd.Series(dtype=float)
    p5 = _pct(4) if len(cols) >= 5 else pd.Series(dtype=float)
    per = pd.DataFrame({"pct_1d": p1, "pct_3d": p3, "pct_5d": p5})

    groups = qf._sector_groups("shenwan")
    items = []
    for name, syms in groups.items():
        sub = per.loc[per.index.intersection(syms)]
        if sub.empty:
            continue
        row = {
            "name": name,
            "pct_1d": round(float(sub["pct_1d"].median()), 2),
            "pct_3d": round(float(sub["pct_3d"].median()), 2),
            "pct_5d": round(float(sub["pct_5d"].median()), 2),
        }
        items.append(row)
    items.sort(key=lambda x: x["pct_5d"], reverse=True)
    return {"latest": latest, "items": items}


# ---------------- 报告生成 ----------------

def _section_indices(d: dict) -> str:
    rows = []
    for it in d["indices"]:
        rows.append([
            it["name"], it["symbol"], f"{it['price']:.2f}", _pct(it["pct_change"]),
            f"{it['turnover']:.0f}",
        ])
    return _fmt_table(["指数", "代码", "收盘", "涨跌幅", "成交额(亿)"], rows)


def _section_breadth(d: dict) -> str:
    b = d["breadth"]
    return _fmt_table(
        ["指标", "数值"],
        [
            ["上涨家数", b.get("advance_count", 0)],
            ["下跌家数", b.get("decline_count", 0)],
            ["平盘家数", b.get("flat_count", 0)],
            ["涨停(≥9.8%)", b.get("limit_up_count", 0)],
            ["跌停(≤-9.8%)", b.get("limit_down_count", 0)],
            ["两市成交额(亿)", f"{b.get('total_turnover_yi', 0):.0f}"],
            ["赚钱效应(上涨占比%)", f"{b.get('profit_effect', 0):.1f}"],
            ["炸板率估算(%)", f"{b.get('exploded_ratio', 0):.1f}"],
        ],
    )


def _section_heatmap(items: list[dict], label: str, n: int = 10) -> str:
    pos = sorted(items, key=lambda x: x["pct_change"], reverse=True)[:n]
    neg = sorted(items, key=lambda x: x["pct_change"])[:n]
    head = ["板块", "均涨幅", "成交额(亿)", "领涨股", "领涨涨幅"]
    body = [
        "**涨幅前列**（{label}）".format(label=label),
        _fmt_table(head, [
            [it["name"], _pct(it["pct_change"]), f"{it['value']:.0f}",
             it["leader"], _pct(it["leader_pct"])]
            for it in pos
        ]),
        "",
        "**跌幅前列**",
        _fmt_table(head, [
            [it["name"], _pct(it["pct_change"]), f"{it['value']:.0f}",
             it["leader"], _pct(it["leader_pct"])]
            for it in neg
        ]),
    ]
    return "\n".join(body)


def _section_money_flow(items: list[dict], label: str) -> str:
    head = ["板块", "净流入(亿)", "主力占比%", "今日涨幅", "收盘"]
    rows = []
    for it in items:
        rows.append([
            it["name"], _yi(it["net_inflow"]), f"{it['main_ratio']:.1f}",
            _pct(it["pct_change"]), f"{it['close_price']:.2f}",
        ])
    return _fmt_table(head, rows)


def _section_stock_flow(items: list[dict]) -> str:
    head = ["股票", "代码", "净流向(亿)", "主力占比%", "今日涨幅", "收盘"]
    rows = []
    for it in items:
        rows.append([
            it["name"] or it["symbol"], it.get("symbol") or it.get("id") or "-",
            _yi(it["net_inflow"]), f"{it['main_ratio']:.1f}",
            _pct(it["pct_change"]), f"{it['close_price']:.2f}",
        ])
    return _fmt_table(head, rows)


def _section_tags(d: dict) -> str:
    t = d["tag_stats"]
    lines = [
        f"- 标签总数：{t.get('total_sectors', 0)}（行业+概念+风格）",
        f"- 已打标签股票：{t.get('total_stocks', 0)} 只，均标签 {t.get('avg_tags_per_stock', 0)} 个",
    ]
    hot = t.get("hot_tags", [])[:15]
    if hot:
        lines.append("")
        lines.append(_fmt_table(["标签", "类型", "成分数"], [
            [h["name"], h["type"], h["count"]] for h in hot
        ]))
    return "\n".join(lines)


def _section_multiday(md: dict) -> str:
    items = md.get("items", [])
    if not items:
        return "_（数据不足：需至少 3 个交易日）_"
    head = ["行业", "1日%", "3日%", "5日%"]
    pos = items[:10]
    neg = sorted(items, key=lambda x: x["pct_5d"])[:10]
    return "\n".join([
        "**5 日累计涨幅 Top10**（成分股中位数）",
        _fmt_table(head, [[it["name"], _pct(it["pct_1d"]), _pct(it["pct_3d"]), _pct(it["pct_5d"])] for it in pos]),
        "",
        "**5 日累计跌幅 Top10**",
        _fmt_table(head, [[it["name"], _pct(it["pct_1d"]), _pct(it["pct_3d"]), _pct(it["pct_5d"])] for it in neg]),
    ])


def build_report_md(d: dict) -> str:
    date_s = d["trade_date"]
    sw = sorted(d["heatmap_shenwan"], key=lambda x: x["pct_change"], reverse=True)
    cc = sorted(d["heatmap_concept"], key=lambda x: x["pct_change"], reverse=True)
    out_10 = sorted(d["flow_10d"], key=lambda x: x["net_inflow"], reverse=True)[:10]
    out_10_neg = sorted(d["flow_10d"], key=lambda x: x["net_inflow"])[:10]
    sf_neg = sorted(d["stock_flow_all"], key=lambda x: x["net_inflow"])[:10]

    md = f"""# 市场分析报告（{date_s}）

> 数据来源：QuantDB 本地数据集 ｜ 生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}
> 本文档为数据骨架，解读部分由 AI 基于 `{date_s}_facts.json` 撰写。

## 一、核心指数

{_section_indices(d)}

## 二、市场广度与情绪

{_section_breadth(d)}

## 三、行业板块

{_section_heatmap(d["heatmap_shenwan"], "行业板块")}

## 四、行业多日涨跌幅对比（截至 {d['sector_multiday'].get('latest', '')}）

{_section_multiday(d["sector_multiday"])}

## 五、热门概念

{_section_heatmap(d["heatmap_concept"], "概念板块")}

## 六、板块资金流

### 6.1 当日净流入 Top10

{_section_money_flow(d["flow_1d"][:10], "1日")}

### 6.2 近 10 日净流入 Top10

{_section_money_flow(out_10, "10日")}

### 6.3 近 10 日净流出 Top10

{_section_money_flow(out_10_neg, "10日")}

## 七、个股主力资金（当日）

### 7.1 主力净流入 Top20

{_section_stock_flow(d["stock_flow"])}

### 7.2 主力净流出 Top10

{_section_stock_flow(sf_neg)}

## 八、标签体系

{_section_tags(d)}

## 九、市场解读与次日关注（AI 撰写）

<!-- AI: 基于 facts 撰写以下内容 -->
### 8.1 市场总览
<!-- AI: 指数结构、量能、情绪温度一句话总览 -->

### 8.2 结构性机会
<!-- AI: 强势行业/概念及其逻辑；资金持续流入的方向 -->

### 8.3 风险提示
<!-- AI: 净流出行业、跌停/炸板情况、指数背离 -->

### 8.4 次日关注
<!-- AI: 2-3 条可跟踪的具体信号（板块/资金/情绪） -->

## 十、数据说明与免责声明

- 涨跌家数与涨跌停：以 `technical_indicators.pct_change`（%）为口径，涨停≈≥9.8%、跌停≈≤-9.8%
- 成交额：指数 `index_daily.amount`（万元）；两市总额为全市场日线 amount 聚合，已转亿元
- 资金流：`l2_factors.flow_*`（元），报告内统一换算为亿元；主力占比 = 主力净额/总成交额
- 概念/行业归属：`sector_members`（申万一级 + 概念板块）
- 本报告由 AI 自动生成，仅供学习研究，不构成投资建议
"""
    return md


def main() -> int:
    ap = argparse.ArgumentParser(description="市场分析取数 + 报告骨架")
    ap.add_argument(
        "--out",
        default=str(
            Path(os.getenv("QM_REPORTS_DIR", str(PROJECT_ROOT / "data" / "reports")))
            / "market_analysis"
        ),
    )
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = fetch()
    if not data["indices"] and not data["breadth"].get("trade_date"):
        print("QuantDB 数据不可用：无指数/广度数据，请检查 data/quantdb 是否已同步")
        return 1

    date_s = data["trade_date"]
    (out_dir / f"{date_s}_facts.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    (out_dir / f"{date_s}_report.md").write_text(
        build_report_md(data), encoding="utf-8"
    )
    print(json.dumps({
        "trade_date": date_s,
        "facts": str(out_dir / f"{date_s}_facts.json"),
        "report": str(out_dir / f"{date_s}_report.md"),
        "indices": len(data["indices"]),
        "shenwan": len(data["heatmap_shenwan"]),
        "concepts": len(data["heatmap_concept"]),
        "stock_flow": len(data["stock_flow"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
