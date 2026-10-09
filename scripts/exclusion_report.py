#!/usr/bin/env python3
"""长期排除清单报告：把当日各层买入硬拦汇总成可读表格（md + csv）。

区分两个概念：
  - **长期排除**（本表主体）：黑名单 / 财务差 / 退市风险 / 长期下跌 / 长期横盘 /
    流动性枯竭 / 次新股 / 低价股 —— 由结构性判据产生，只有基本面或走势真正改善
    才出列；
  - **临时事件**（另一节）：解禁 / 负面新闻 / 立案 —— 有明确失效日，过期自动放行。

另附**行业风险榜**（剔除数/该行业总数）：剔除率高的行业整体谨慎（2026-09-11
用户口径「这些行业也要谨慎点」）——行业级景气下行时个体基本面会集体劣化，
单看个股名单看不出"整条赛道在沉"。

只读已落盘的产物（configs/live_symbols.json、data/fundamental_flags.json、
data/risk_block.json），不重算任何判据——保证报表与闸门**同源**。
行业列是**补充标签**（quantdb 通达信行业 rs_hyname，覆盖 5536 只），不参与判定。

用法：
  python scripts/exclusion_report.py            # 写 data/长期排除清单_<日期>.md|.csv
  python scripts/exclusion_report.py --print    # 只打印摘要
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

SYMBOLS = ROOT / "configs" / "live_symbols.json"
FUND = ROOT / "data" / "fundamental_flags.json"
RISK = ROOT / "data" / "risk_block.json"
BJ = timezone(timedelta(hours=8))

# 长期层：fundamental_flags 的 flag → 中文层名（顺序即表格列序）
FUND_LAYERS = [("fin", "财务差"), ("delist", "退市风险"), ("shell", "保壳"),
               ("trend", "长期下跌"), ("flat", "横盘"), ("illiquid", "流动性"),
               ("new", "次新股")]

# 表格的列序（md 的「层」列与 csv 的勾选列共用，防止两处漂移）
LAYER_ORDER = ["永久黑名单", "财务差", "退市风险", "保壳", "长期下跌", "横盘",
               "流动性", "次新股", "低价股"]

# 同一条理由会被三个来源各带一遍（黑名单文案 / 基本面缓存 / risk_block 的合并
# 文本），按**分句**去重——整串比对去不掉，因为合并文本里混了别层的句子。
CLAUSE_SEP = re.compile(r"[；;]")


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def load_layers() -> dict:
    """{code6: {"layers": [str], "reasons": [分句]}}（长期排除各层）。"""
    out: dict = {}

    def _add(code, layer, reason):
        c6 = str(code or "").split(".")[0]
        if len(c6) != 6 or not c6.isdigit():
            return
        it = out.setdefault(c6, {"layers": [], "reasons": []})
        if layer not in it["layers"]:
            it["layers"].append(layer)
        for part in CLAUSE_SEP.split(str(reason or "")):
            part = part.strip()
            if part and part not in it["reasons"]:
                it["reasons"].append(part)

    for sym in _read(SYMBOLS).get("block_buy") or []:
        _add(sym, "永久黑名单", "新闻复核入永久黑名单")

    doc = _read(FUND)
    for code, it in (doc.get("items") or {}).items():
        flags = it.get("flags") or []
        reason = str(it.get("reason") or "")
        for tag, label in FUND_LAYERS:
            if tag in flags:
                _add(code, label, reason)

    for code, it in (_read(RISK).get("items") or {}).items():
        if "penny" in _kinds(it):
            _add(code, "低价股", str(it.get("reason") or ""))
    return out


# risk_list 合并多层时 kind 是 "penny+weak" 这样的拼接串，必须按成分判而不是等值判。
LAYER_KINDS = {"unlock", "news", "grave"}


def _kinds(it: dict) -> set:
    return {k for k in str(it.get("kind") or "").split("+") if k}


def load_transient() -> list:
    """临时事件条目 [(code, kind, reason, expire)]（解禁/新闻/立案）。"""
    out = []
    for code, it in (_read(RISK).get("items") or {}).items():
        ks = _kinds(it) & LAYER_KINDS
        c6 = str(code).split(".")[0]
        if ks and len(c6) == 6 and c6.isdigit():
            out.append((c6, "+".join(sorted(ks)), str(it.get("reason") or ""),
                        str(it.get("expire") or "")))
    return sorted(out, key=lambda r: (r[1], r[0]))


def _names() -> dict:
    try:
        from news_blacklist_scan import load_name_index

        _idx, by_code = load_name_index()
        return {str(k).split(".")[0]: v for k, v in by_code.items()}
    except Exception:  # noqa: BLE001 名称表拿不到不影响清单本体
        return {}


def build(names: dict, industries: dict | None = None) -> tuple:
    from industry_risk import load_industries

    layers = load_layers()
    ind = load_industries()[0] if industries is None else industries
    rows = []
    for code, it in layers.items():
        ls = sorted(it["layers"],
                    key=lambda x: LAYER_ORDER.index(x) if x in LAYER_ORDER else 99)
        rows.append({"code": code, "name": names.get(code, ""),
                     "industry": ind.get(code, ""), "layers": ls,
                     "reason": "；".join(it["reasons"])})
    rows.sort(key=lambda r: (-len(r["layers"]), r["code"]))
    return rows, load_transient()


def industry_ranking(rows: list, totals: Counter) -> list:
    """行业风险榜：[(行业, 剔除数, 行业总数, 剔除率)]，按剔除率降序（≥3 只才上榜）。

    没有行业总数（quantdb 拿不到）就整榜不出——宁可没有，也不给一个
    "剔除数/剔除数=100%" 的假榜。
    """
    if not totals:
        return []
    hit = Counter(r["industry"] for r in rows if r.get("industry"))
    out = [(h, n, totals.get(h, n), n / max(totals.get(h, n), 1))
           for h, n in hit.items() if n >= 3]
    return sorted(out, key=lambda x: (-x[3], -x[1]))


def write(rows: list, transient: list, names: dict, asof: str,
          totals: Counter | None = None) -> tuple:
    out_dir = ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"长期排除清单_{asof}.md"
    csv_p = out_dir / f"长期排除清单_{asof}.csv"
    total = len(rows)
    pct = total / 5563 * 100
    lines = [f"# 长期排除清单（买入硬拦，{asof}）", "",
             f"共 {total} 只（全市场 5563 只的 {pct:.1f}%）。卖出不受限；本表随每日闸门刷新",
             "（重跑：python scripts/exclusion_report.py）。", "",
             "| # | 代码 | 名称 | 行业 | 命中层数 | 层 | 理由 |",
             "|---:|---|---|---|---:|---|---|"]
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | {r['code']} | {r['name']} | {r.get('industry', '')} | "
                     f"{len(r['layers'])} | {'/'.join(r['layers'])} | {r['reason']} |")
    rank = industry_ranking(rows, totals or Counter())
    if rank:
        from risk_list import load_conf

        c = load_conf()
        lines += ["", "## 行业风险榜（剔除率 = 被排除数 / 该行业全部股票数）", "",
                  "> 剔除率高的行业整体谨慎：赛道景气下行时个体会集体劣化，",
                  "> 单看个股名单看不出「整条赛道在沉」。",
                  f"> 提示词只取其中 剔除率≥{float(c.get('industry_warn_rate_min', 0.45)):.0%} "
                  f"且 行业总数≥{int(c.get('industry_warn_total_min', 10))} 的前 "
                  f"{int(c.get('industry_warn_top_n', 15))} 个（阈值见 configs/live_symbols.json "
                  "的 risk 段）——小样本行业这里列出供参考，但不进决策链，",
                  "> 避免 4/4=100% 这类噪声挤占注意力。", "",
                  "| 行业 | 被排除 | 行业总数 | 剔除率 |", "|---|---:|---:|---:|"]
        for h, n, t, r in rank[:25]:
            lines.append(f"| {h} | {n} | {t} | {r:.0%} |")
    lines += ["", f"## 临时事件（{len(transient)} 只，到期自动放行，不计入上表）", "",
              "| 代码 | 名称 | 类型 | 失效日 | 理由 |", "|---|---|---|---|---|"]
    for code, kind, reason, expire in transient:
        lines.append(f"| {code} | {names.get(code, '')} | {kind} | {expire[:10]} | {reason} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cols = LAYER_ORDER
    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "行业", "命中层数", *cols, "理由"])
        for r in rows:
            w.writerow([r["code"], r["name"], r.get("industry", ""), len(r["layers"]),
                        *["✓" if o in r["layers"] else "" for o in cols], r["reason"]])
    return md, csv_p


def main() -> int:
    ap = argparse.ArgumentParser(description="长期排除清单报告")
    ap.add_argument("--print", dest="print_only", action="store_true", help="只打印摘要")
    a = ap.parse_args()
    from industry_risk import load_industries

    names = _names()
    ind, totals = load_industries()
    rows, transient = build(names, ind)
    asof = datetime.now(BJ).date().isoformat()

    c = Counter(layer for r in rows for layer in r["layers"])
    print(f"长期排除 {len(rows)} 只（{len(rows) / 5563 * 100:.1f}%）："
          + " / ".join(f"{k} {v}" for k, v in c.most_common())
          + f" | 临时事件 {len(transient)} 只")
    rank = industry_ranking(rows, totals)
    if rank:
        print("行业风险榜 Top10：" + " / ".join(
            f"{h} {n}/{t}({r:.0%})" for h, n, t, r in rank[:10]))
    if not a.print_only:
        md, csv_p = write(rows, transient, names, asof, totals)
        print(f"→ {md}\n→ {csv_p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
