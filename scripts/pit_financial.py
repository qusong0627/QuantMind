#!/usr/bin/env python3
"""A股财务 PIT（Point-in-Time）快照构建器 —— quantdb 适配版。

方法论参考 QuantSkills skill-a-share-pit-fundamental-vintage-builder（GPL-3.0）
的核心规则；实现为本仓库原创，数据层直接用 quantdb 3_financial_data
（balance/income/cashflow，per-symbol parquet，含 m_timetag 报告期 + m_anntime 公告日）。

PIT 保护规则：每个历史时点 T 只使用"公告日 + lag 个交易日 ≤ T"的财报版本；
同一报告期存在多版本（重述）时，取公告时间 ≤ cutoff 的最新版本。
naive 合并（只按报告期取最新行）会偷看后续重述 → audit_naive 量化这个差距。

常用派生因子（由 PIT 行现算，绝不用未来行）：营收/净利 TTM、ROA_TTM、
资产负债率、营收 TTM 同比。

用法：
  python scripts/pit_financial.py --asof 2026-09-03 --codes 600309.SH,688183.SH
  python scripts/pit_financial.py --asof 2026-09-03 --audit-naive      # 全市场泄漏审计
  python scripts/pit_financial.py --asof 2026-09-03 --full-market --out logs/pit
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from trading_cal import next_trading_day  # noqa: E402

FIN_ROOTS = [Path.home() / "projects/quantmind/data/quantdb/3_financial_data",
             Path("/data/quantdb/3_financial_data")]
TABLES = ("balance", "income", "cashflow")


def _fin_root() -> Path:
    for r in FIN_ROOTS:
        if (r / "income").is_dir():
            return r
    raise FileNotFoundError("quantdb 3_financial_data 未找到")


def _ann_date(v) -> date | None:
    s = str(v or "").strip()[:10].replace("-", "")
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    return None


def _load_code(code: str, table: str) -> list[dict]:
    """单只股票一张财务表的全部历史行（含重述版本）。"""
    f = _fin_root() / table / f"{code}.parquet"
    if not f.is_file():
        return []
    import duckdb

    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet(?)", [str(f)]).fetchall()
    cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_parquet(?)",
                                      [str(f)]).fetchall()]
    out = []
    for r in rows:
        d = {c: v for c, v in zip(cols, r)}
        d["ann_d"] = _ann_date(d.get("m_anntime") or d.get("actual_ann_dt"))
        d["period"] = str(d.get("m_timetag") or "")
        out.append(d)
    return out


def _usable_date(ann_d: date, lag_days: int) -> date:
    """公告日 T → 可用日期（保守口径：T 的下一 lag 个交易日起可用）。"""
    d = ann_d
    for _ in range(max(lag_days, 1)):
        d = next_trading_day(d)
    return d


def pit_latest(code: str, table: str, asof: date, lag_days: int = 1) -> list[dict]:
    """asof 时点该表可用的财务行：每报告期取公告 ≤ cutoff 的最新版本。"""
    usable = []
    for r in _load_code(code, table):
        ann = r.get("ann_d")
        if ann and _usable_date(ann, lag_days) <= asof:
            usable.append(r)
    latest: dict = {}
    for r in usable:  # 同报告期多版本（重述）→ 取公告最晚的版本
        k = r["period"]
        if k not in latest or (r.get("ann_d") or date.min) >= (latest[k].get("ann_d") or date.min):
            latest[k] = r
    return sorted(latest.values(), key=lambda r: r["period"])


def _singles_from_cumulative(rows: list, field: str) -> dict:
    """累计值（年初至今）→ 单季值。Q1 单季=Q1 累计；其余=本期累计-上期累计。"""
    cum = {}
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        p = r["period"]
        if len(p) >= 6 and p[:6].isdigit():
            cum[(int(p[:4]), int(p[4:6]))] = float(v)
    singles: dict = {}
    for (y, m), v in sorted(cum.items()):
        if m == 3:  # Q1：累计即单季（上年 12 月累计是全年，不能减）
            singles[(y, m)] = v
            continue
        prev = cum.get((y, m - 3))
        singles[(y, m)] = v - prev if prev is not None else v
    return singles


def _ttm_from_cumulative(rows: list, field: str) -> float | None:
    """最近 4 个单季之和。"""
    singles = _singles_from_cumulative(rows, field)
    keys = sorted(singles)[-4:]
    if len(keys) < 4:
        return None
    return sum(singles[k] for k in keys)


def derive_factors(code: str, asof: date, lag_days: int = 1) -> dict:
    """PIT 快照 + 常用派生因子（只用 asof 时点可见的数据）。"""
    out: dict = {"code": code, "asof": str(asof), "tables": {}}
    inc = pit_latest(code, "income", asof, lag_days)
    bal = pit_latest(code, "balance", asof, lag_days)
    cfl = pit_latest(code, "cashflow", asof, lag_days)
    out["tables"] = {"income": inc[-4:], "balance": bal[-2:], "cashflow": cfl[-4:]}
    f: dict = {}
    f["revenue_ttm"] = _ttm_from_cumulative(inc, "revenue")
    f["net_profit_ttm"] = _ttm_from_cumulative(inc, "net_profit_incl_min_int_inc")
    f["ocf_ttm"] = _ttm_from_cumulative(cfl, "net_cash_flows_oper_act")
    tot_assets = bal[-1].get("tot_assets") if bal else None
    tot_liab = bal[-1].get("tot_liab") if bal else None
    if f["net_profit_ttm"] and tot_assets:
        f["roa_ttm"] = f["net_profit_ttm"] / float(tot_assets)
    if tot_assets and tot_liab:
        f["debt_ratio"] = float(tot_liab) / float(tot_assets)
    # 营收 TTM 同比：TTM(最新4季) / TTM(此前4季) - 1
    singles = _singles_from_cumulative(inc, "revenue")
    ks = sorted(singles)
    if len(ks) >= 8:
        now_ttm = sum(singles[k] for k in ks[-4:])
        prev_ttm = sum(singles[k] for k in ks[-8:-4])
        if prev_ttm:
            f["revenue_ttm_yoy"] = now_ttm / prev_ttm - 1
    f["periods_used"] = [r["period"] for r in inc[-8:]]
    out["factors"] = {k: (round(v, 6) if isinstance(v, float) else v)
                      for k, v in f.items() if v is not None}
    return out


def audit_naive(asof: date, tables: tuple = TABLES, lag_days: int = 1) -> dict:
    """全市场泄漏审计：naive（无视公告日、每报告期直接取最新行）vs PIT 的差异。

    差异行 = 若不做 PIT 会偷看到的重述/未来数据。返回计数与示例。"""
    root = _fin_root()
    result = {"asof": str(asof), "tables": {}}
    import duckdb

    con = duckdb.connect()
    for table in tables:
        files = sorted((root / table).glob("*.parquet"))
        # 单条 glob 查询读全市场（filename=true 从路径提取代码），避免逐文件开连接
        rows = con.execute(
            f"SELECT filename, m_timetag, m_anntime FROM "
            f"read_parquet('{root / table}/*.parquet', filename=true)").fetchall()
        per_code: dict = defaultdict(list)
        for fname, period, anntime in rows:
            code = Path(fname).stem
            ann = _ann_date(anntime)
            if ann:
                per_code[code].append((str(period), ann))
        leak_codes = []
        for code, items in per_code.items():
            naive_p = {}
            for period, ann in items:  # naive：不看公告日，同报告期取最后出现（=最新版）
                naive_p[period] = ann
            pit_p = {p: ann for p, ann in items
                     if _usable_date(ann, lag_days) <= asof}
            future = sorted(set(naive_p) - set(pit_p))
            restated = sum(1 for p in pit_p if naive_p[p] != pit_p[p])
            if future or restated:
                leak_codes.append({"code": code,
                                   "future_periods": future[-3:],
                                   "restated_periods": restated})
        result["tables"][table] = {"symbols": len(files),
                                   "leaked_symbols": len(leak_codes),
                                   "leak_ratio": round(len(leak_codes) / max(len(per_code), 1), 3),
                                   "examples": leak_codes[:10]}
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="A股财务 PIT 快照/防前视审计（quantdb 版）")
    ap.add_argument("--asof", default=date.today().isoformat())
    ap.add_argument("--codes", default="", help="逗号分隔；缺省=三只当前持仓")
    ap.add_argument("--tables", default="balance,income,cashflow")
    ap.add_argument("--lag-days", type=int, default=1, help="公告后 N 个交易日可用（默认 1）")
    ap.add_argument("--full-market", action="store_true", help="全市场快照（较慢）")
    ap.add_argument("--audit-naive", action="store_true", help="全市场 naive-vs-PIT 泄漏审计")
    ap.add_argument("--out", default="logs/pit")
    a = ap.parse_args()
    asof = date.fromisoformat(a.asof)

    if a.audit_naive:
        res = audit_naive(asof, tuple(a.tables.split(",")), a.lag_days)
        print(json.dumps(res, ensure_ascii=False, indent=1)[:3000])
        out = Path(ROOT / a.out)
        out.mkdir(parents=True, exist_ok=True)
        fp = out / f"naive_audit_{asof:%Y%m%d}.json"
        fp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ {fp}")
        return 0

    codes = ([c.strip() for c in a.codes.split(",") if c.strip()]
             or ["600309.SH", "688183.SH", "300750.SZ"])
    snaps = []
    if a.full_market:
        files = sorted((_fin_root() / "income").glob("*.parquet"))
        codes = [f.stem for f in files]
    for code in codes:
        snaps.append(derive_factors(code, asof, a.lag_days))
        s = snaps[-1]["factors"]
        print(f"{code}: 营收TTM {s.get('revenue_ttm', 0) / 1e8:,.1f}亿 · "
              f"净利TTM {s.get('net_profit_ttm', 0) / 1e8:,.2f}亿 · "
              f"ROA_TTM {s.get('roa_ttm', 0):.2%} · 负债率 {s.get('debt_ratio', 0):.1%} · "
              f"营收TTM同比 {s.get('revenue_ttm_yoy', 0):+.1%}")
    out = Path(ROOT / a.out)
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"snapshot_{asof:%Y%m%d}.json"
    fp.write_text(json.dumps(snaps, ensure_ascii=False, indent=1, default=str),
                  encoding="utf-8")
    print(f"→ {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
