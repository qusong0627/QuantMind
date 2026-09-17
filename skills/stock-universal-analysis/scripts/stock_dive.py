#!/usr/bin/env python3
"""个股全维度数据体检（QuantDB 直读）——在 quantmind 容器内执行。

用法：本脚本被 skill 复制进容器后运行：
  docker cp stock_dive.py quantmind:/tmp/ && \
  docker exec -w /app quantmind python3 /tmp/stock_dive.py 601138.SH [all|kline|sector|financial|valuation|factors]
输出：JSON facts（查不到 → null + note），禁止编造。
"""
import glob
import json
import os
import sys

CODE = sys.argv[1] if len(sys.argv) > 1 else ""
DIMS = sys.argv[2] if len(sys.argv) > 2 else "all"
if not CODE:
    print(json.dumps({"ok": False, "error": "用法: stock_dive.py <CODE.SH> [维度]"}))
    sys.exit(1)

# 数据根：容器内 /data/quantdb，宿主机调试 /home/zbox/projects/quantmind/data/quantdb
DATA = next((p for p in ("/data/quantdb", "/quantmind/data/quantdb") if os.path.isdir(p)), None)
out = {"ok": bool(DATA), "code": CODE, "data_root": DATA, "facts": {}}

try:
    import duckdb

    con = duckdb.connect()
except Exception as exc:  # noqa: BLE001
    out["ok"] = False
    out["error"] = f"容器内缺 duckdb: {exc}"
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)

SYM = CODE.split(".")[0]


def last_parquet(pattern: str):
    hits = sorted(glob.glob(pattern))
    return hits[-1] if hits else None


def read_code(pattern_root: str):
    """数据集目录 → 找该代码的 parquet（优先精确文件名，再按目录内容猜测）"""
    for p in (f"{pattern_root}/{CODE}.parquet", f"{pattern_root}/{SYM}.parquet",
              f"{pattern_root}/{CODE}.csv"):
        if os.path.exists(p):
            return p
    return None


def dim_kline():
    base = f"{DATA}/1_kline_data"
    for adj, name in (("daily_forward", "前复权"), ("daily_backward", "后复权"),
                      ("daily_unadjusted", "不复权")):
        p = read_code(f"{base}/{adj}")
        if not p:
            continue
        df = con.execute(f"SELECT * FROM read_parquet('{p}')").fetchdf()
        if df.empty:
            continue
        df = df.sort_values("trade_time") if "trade_time" in df.columns else df
        tail = df.tail(60)
        out["facts"][f"日线_{name}"] = {
            "最新日期": str(tail.iloc[-1].get("trade_time", "")),
            "近5日": tail.tail(5)[["open", "high", "low", "close", "volume", "amount"]]
                     .round(3).to_dict("records")
                     if all(c in tail.columns for c in ("open", "high")) else None,
            "60日涨跌幅%": round(
                (float(tail.iloc[-1]["close"]) / float(tail.iloc[0]["close"]) - 1) * 100, 2)
                if len(tail) > 1 and "close" in tail.columns else None,
        }


def dim_sector():
    base = f"{DATA}/2_base_sector"
    p = read_code(f"{base}/instrument_detail")
    if p:
        df = con.execute(f"SELECT * FROM read_parquet('{p}')").fetchdf()
        row = df[df.get("Symbol", "").astype(str) == CODE]
        if row.empty:
            row = df[df.get("Symbol", "").astype(str) == SYM]
        if not row.empty:
            r = row.iloc[-1]
            out["facts"]["板块_市值"] = {
                "名称": str(r.get("InstrumentName") or r.get("Name") or ""),
                "HqDate": str(r.get("HqDate", "")), "行业": str(r.get("IndustryName") or ""),
                "总市值亿": round(float(r.get("Zsz") or 0), 2),
                "流通市值亿": round(float(r.get("Ltsz") or 0), 2),
                "总股本万股": round(float(r.get("J_zgb") or 0), 0),
                "基本每股收益元": r.get("J_mgsy"),
                "营收(万元)": r.get("J_yysy"), "净利(万元)": r.get("J_jly"),
            }
    iw = f"{DATA}/2_base_sector/index_weights"
    for hit in sorted(glob.glob(f"{iw}/*.parquet")):
        df = con.execute(f"SELECT * FROM read_parquet('{hit}')").fetchdf()
        row = df[df.get("Symbol", "").astype(str) == CODE]
        if not row.empty:
            out["facts"]["指数权重"] = {
                "指数": os.path.basename(hit),
                "权重%": round(float(row.iloc[-1].get("Weight") or 0), 4),
            }


def dim_financial():
    base = f"{DATA}/3_financial_data"
    for ds in ("balance", "income", "cashflow"):
        p = read_code(f"{base}/{ds}")
        if not p:
            continue
        df = con.execute(f"SELECT * FROM read_parquet('{p}')").fetchdf()
        if df.empty:
            continue
        col_date = next((c for c in ("end_date", "report_date", "trade_time") if c in df.columns), None)
        last = df.iloc[-1]
        out["facts"][f"财报_{ds}"] = {
            "报告期": str(last.get(col_date, "")),
            "关键科目数": int(df.shape[1]),
        }
    p = read_code(f"{base}/dividend_factors")
    if p:
        df = con.execute(f"SELECT * FROM read_parquet('{p}')").fetchdf()
        if not df.empty:
            r = df.iloc[-1]
            out["facts"]["分红"] = {"每10股派息(元)": r.get("interest"),
                                    "送/转/配(每10股)": (r.get("stockBonus"), r.get("stockGift"), r.get("allotNum"))}
    p = read_code(f"{base}/holder_num")
    if p:
        df = con.execute(f"SELECT * FROM read_parquet('{p}')").fetchdf()
        if not df.empty:
            cols = [c for c in df.columns if "holder" in c.lower() or "date" in c.lower()]
            out["facts"]["股东户数"] = {"最近": df.iloc[-1][cols].to_dict()
                                          if cols else df.iloc[-1].to_dict()}


def dim_valuation():
    for ds, name in (("valuation", "估值"), ("technical_indicators", "技术指标"),
                     ("market_sentiment", "情绪")):
        p = read_code(f"{DATA}/5_technical_derived/{ds}")
        if not p:
            continue
        df = con.execute(f"SELECT * FROM read_parquet('{p}')").fetchdf()
        if not df.empty:
            r = df.iloc[-1].to_dict()
            keep = {k: (round(float(v), 4) if isinstance(v, (int, float)) else v)
                    for k, v in list(r.items())[:24]}
            out["facts"][name] = keep
    for ds in ("features_daily", "l1_factors", "l2_factors"):
        hits = glob.glob(f"{DATA}/6_ml_datasets/{ds}/*{SYM}*")
        if hits:
            df = con.execute(f"SELECT * FROM read_parquet('{hits[0]}')").fetchdf()
            if not df.empty:
                out["facts"][f"因子_{ds}"] = {"行数": int(df.shape[0]),
                                               "最新一行摘要": {k: v for k, v in list(df.iloc[-1].to_dict().items())[:20]}}


DIM_FN = {"kline": dim_kline, "sector": dim_sector, "financial": dim_financial,
          "valuation": dim_valuation, "factors": lambda: dim_valuation() or None}
if DIMS != "all":
    (DIM_FN.get(DIMS) or dim_kline)()
else:
    for fn in (dim_kline, dim_sector, dim_financial, dim_valuation):
        fn()
out["note"] = "单位：日线 volume=股/amount=万元，板块 J_zgb=万股/Zsz=亿元，财务科目=元；板块 HqDate 可能滞后"
print(json.dumps(out, ensure_ascii=False, default=str))