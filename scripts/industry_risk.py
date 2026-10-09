#!/usr/bin/env python3
"""行业级风险聚合（行业风险榜）→ 提示词用的赛道级**软提示**。

口径（2026-09-11 用户）：「这些行业也要谨慎点」。单看个股名单看不出
"整条赛道在沉"——把长期排除清单按通达信行业汇总，剔除率显著高于全市场基线
（当前 29.4%）的行业整条标出来，注入交易提示词。

**只提示不硬拦**：个股命中黑名单/劣化判据才是硬拦（buy_gate + symbol_policy）；
行业标签是统计口径，一刀切禁买会误伤同一行业里没问题的公司（软件服务 232 只
里被剔 112 只，剩下 120 只不该陪绑）。

与闸门同源：不重算判据，只聚合落盘产物（走 exclusion_report.load_layers）；
行业名取 quantdb `2_base_sector/instrument_detail` 的 rs_hyname（覆盖 5536 只）。

产物 data/industry_risk.json：
  {"asof","ts","rule",
   "items": [{"industry","n","total","rate"}],  # 风险榜（按剔除率降序）
   "warn_codes": {"600029": "航空机场"}}        # 上榜行业的**全部**成分股
                                                # （含未被剔除的——赛道提示不看个股判据）

榜单阈值（真源在 configs/live_symbols.json 的 risk 段，经 risk_list.load_conf 读）：
  industry_warn_rate_min / industry_warn_total_min / industry_warn_top_n

用法：
  python scripts/industry_risk.py build   # 重算落盘（cron 每日 08:35，紧随基本面扫描）
  python scripts/industry_risk.py         # 打印现状
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

QUANTDB = Path(os.environ.get("QUANTDB_DIR", "/home/zbox/projects/quantmind/data/quantdb"))
OUT = ROOT / "data" / "industry_risk.json"
BJ = timezone(timedelta(hours=8))

# 阈值兜底值：正常走 risk_list.load_conf()（configs/live_symbols.json 的 risk 段，
# 键名同存于 risk_list.DEFAULTS —— 单一事实来源，避免两处漂移）。
RATE_MIN, TOTAL_MIN, TOP_N = 0.45, 10, 15


def load_industries() -> tuple:
    """({code6: 行业}, Counter(全市场各行业股票数))——quantdb 通达信 rs_hyname。

    覆盖 5536/5563 只（98%+）。拿不到 → ({}, Counter())：报表少一列标签、
    提示词少一段，不影响任何硬拦（fail-open）。
    """
    try:
        import duckdb

        p = QUANTDB / "2_base_sector" / "instrument_detail" / "*.parquet"
        with duckdb.connect() as con:
            df = con.execute(
                f"SELECT Symbol, rs_hyname FROM read_parquet('{p}') "
                f"WHERE rs_hyname IS NOT NULL AND rs_hyname <> ''").df()
        ind = {str(s).split(".")[0]: str(h) for s, h in zip(df.Symbol, df.rs_hyname)}
        return ind, Counter(ind.values())
    except Exception:  # noqa: BLE001
        return {}, Counter()


def caution_items(rows: list, totals: Counter, conf: dict) -> list:
    """风险榜上榜行业 [(行业, 剔除数, 行业总数, 剔除率)]。

    上榜 = 剔除率 ≥ industry_warn_rate_min 且 行业总数 ≥ industry_warn_total_min，
    再取剔除率前 industry_warn_top_n。样本太小的行业（如 4/4=100%）不上榜：
    统计意义弱，写进提示词只会挤占别处的注意力。
    """
    from exclusion_report import industry_ranking

    total_min = int(conf.get("industry_warn_total_min", TOTAL_MIN))
    rate_min = float(conf.get("industry_warn_rate_min", RATE_MIN))
    top_n = int(conf.get("industry_warn_top_n", TOP_N))
    ranked = industry_ranking(rows, totals)      # 已按剔除率降序、样本 ≥3 只
    out = [it for it in ranked if it[2] >= total_min and it[3] >= rate_min]
    return out[:max(top_n, 0)]


def build(conf: dict | None = None, out: Path | None = None) -> dict:
    """重算并落盘 data/industry_risk.json（只聚合，不重算任何判据）。"""
    from exclusion_report import build as build_rows
    from risk_list import load_conf

    conf = load_conf() if conf is None else conf
    ind, totals = load_industries()
    rows, _transient = build_rows({}, ind)
    items = caution_items(rows, totals, conf)
    board = {it[0] for it in items}
    # 上榜行业的**全部**成分股（不只是被剔除的那些）——提示词要能提示"你在
    # 一条下沉赛道里"，哪怕这只票自己判据干净（它可能正是赛道里较好的那个，
    # 但也更容易被赛道拖着走）。
    warn = {c: h for c, h in ind.items() if h in board}
    now = datetime.now(BJ)
    doc = {
        "asof": now.date().isoformat(),
        "ts": now.isoformat(timespec="seconds"),
        "rule": (f"剔除率≥{conf.get('industry_warn_rate_min', RATE_MIN):.0%} 且 "
                 f"行业总数≥{int(conf.get('industry_warn_total_min', TOTAL_MIN))}，"
                 f"取前 {int(conf.get('industry_warn_top_n', TOP_N))}；"
                 "剔除率 = 该行业被长期排除清单剔除数 / 行业股票总数（quantdb rs_hyname）"),
        "items": [{"industry": h, "n": n, "total": t, "rate": round(r, 4)}
                  for h, n, t, r in items],
        "warn_codes": warn,
    }
    p = out or OUT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                 encoding="utf-8")
    return doc


def _valid_item(it) -> bool:
    """榜单条目结构校验——产物是提示词的输入边界，脏数据整份退回而不是半信半疑。"""
    if not isinstance(it, dict) or not str(it.get("industry") or ""):
        return False
    try:
        float(it["rate"])
        int(it["n"])
        int(it["total"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def load_doc(path: Path | None = None, days: int | None = None) -> dict:
    """读产物；缺失/损坏/过期（> 基本面缓存窗口 flag_days）→ {}（fail-open）。

    过期判据与 fundamental_flags 缓存同源：榜单是从那份缓存聚合来的，
    缓存失效了榜单也不该再被信。
    """
    try:
        doc = json.loads((path or OUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("warn_codes"), dict):
        return {}
    items = doc.get("items")
    if not isinstance(items, list) or not items or not all(map(_valid_item, items)):
        return {}
    if days is None:
        try:
            from risk_list import load_conf

            days = int(load_conf().get("flag_days", 7))
        except Exception:  # noqa: BLE001
            days = 7
    try:
        age = (datetime.now(BJ).date()
               - datetime.fromisoformat(str(doc.get("asof"))).date()).days
    except (TypeError, ValueError):
        return {}
    return doc if age <= int(days) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="行业风险榜（提示词软提示）")
    ap.add_argument("cmd", nargs="?", default="show", choices=["build", "show"])
    a = ap.parse_args()
    if a.cmd == "build":
        doc = build()
        print(f"行业风险榜 {len(doc['items'])} 个行业，"
              f"{len(doc['warn_codes'])} 只成分股 → {OUT}")
        for it in doc["items"]:
            print(f"  {it['industry']} {it['n']}/{it['total']}（{it['rate']:.0%}）")
        return 0
    doc = load_doc()
    if not doc:
        print("无有效产物（未生成或已过期）——重跑：python scripts/industry_risk.py build")
        return 0
    print(f"asof {doc['asof']}：{doc['rule']}")
    for it in doc["items"]:
        print(f"  {it['industry']} {it['n']}/{it['total']}（{it['rate']:.0%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
