#!/usr/bin/env python3
"""单股 9 层深分一键取数（parquet 直读，host 与容器通用）
用法:
    python3 scripts/stock_9layer_fetch.py 000733.SZ          # 全量
    python3 scripts/stock_9layer_fetch.py 000733.SZ --l4b     # 只要订单微结构截面
输出: 控制台分段摘要；--json 时输出 /tmp/{code}_9layer.json
原理: 全部直读 QuantDB parquet（1_kline_data / 3_financial_data /
      5_technical_derived / 2_base_sector / 6_ml_datasets）。
      PG 端（模型信号/新闻）由 runbook 手动命令接管，本脚本不管。
"""
import sys
import os
import json
import glob
import argparse
import duckdb
import pandas as pd

# 数据根目录：宿主机为 <proj>/data/quantdb，容器内为 /data/quantdb
# 注意: host 上 /data/quantdb 可能是空目录，必须优先项目本地路径
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_QS = os.path.realpath(os.path.join(_SCRIPT_DIR, "..", "data", "quantdb"))
for _cand in (_LOCAL_QS, "/data/quantdb", "/data"):
    if os.path.isdir(_cand) and os.path.isdir(os.path.join(_cand, "6_ml_datasets")):
        QS = _cand
        break
else:
    QS = _LOCAL_QS

# ---- L4b 报告确认因子（26 → 按 IC 方向分组；负 IC 高位=利空，正 IC 高位=利多）----
L4B_NEG = ["vol_persistence", "flow_buy_amount", "flow_sell_amount",
           "micro_toxicity_persistence", "flow_order_arrival_rate",
           "micro_trade_interval_cv", "micro_trade_arrival_rate",
           "vol_tick_density", "vol_realized_jump", "vol_realized_rrv",
           "micro_jump_count_1pct", "micro_close_squeeze"]
L4B_POS = ["micro_vpin_vol_ratio", "flow_order_duration_p90",
           "micro_vpin_amount_ratio", "flow_cancel_lifetime",
           "micro_trade_interval_mean", "micro_vpin_50", "micro_vpin_ma_20",
           "micro_informed_ratio"]
L4B_REF = ["flow_super_net", "micro_liquidity_amihud_5", "micro_vpin_100",
           "micro_vpin_tail_risk", "micro_vpin_hurst"]

def latest_partition(dataset):
    ds = os.path.join(QS, dataset)
    pats = sorted(glob.glob(os.path.join(ds, "dt=*")))
    return os.path.basename(pats[-1]) if pats else None

def read_part(dataset, dt, where_sym=None, cols=None):
    p = os.path.join(QS, dataset, dt, "data.parquet")
    q = f"SELECT * FROM read_parquet('{p}')"
    if where_sym:
        q += f" WHERE symbol='{where_sym}'"
    return duckdb.connect().execute(q).df()

def fmt1(x, d=4):
    try:
        return f"{float(x):.{d}f}"
    except Exception:
        return str(x)

def block(title, lines):
    print(f"\n{'='*68}\n## {title}\n{'='*68}")
    for l in lines:
        print(l)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", help="股票代码，如 000733.SZ")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--l4b", action="store_true", help="只输出 L4b")
    args = ap.parse_args()
    SYM = args.code
    out = {"code": SYM}

    # 最新分区
    l2_dt = latest_partition("6_ml_datasets/l1_l2_factors")
    print(f"[info] QuantDB 根: {QS}")
    print(f"[info] L1+L2 最新分区: {l2_dt}")
    out["l1l2_dt"] = l2_dt

    if args.l4b:
        fetch_l4b(SYM, l2_dt)
        return
    fetch_kline(SYM, out)
    fetch_finance(SYM, out)
    fetch_valuation(SYM, out)
    fetch_margin(SYM, out)
    fetch_l4b(SYM, l2_dt, out)
    if args.json:
        with open(f"/tmp/{SYM.replace('.','_')}_9layer.json", "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[json] 已写 /tmp/{SYM.replace('.','_')}_9layer.json")

def fetch_kline(SYM, out):
    rows = []
    for d in sorted(glob.glob(os.path.join(QS, "1_kline_data/daily_forward/dt=*")))[-45:]:
        try:
            df = read_part("1_kline_data/daily_forward", os.path.basename(d), SYM)
            if len(df): rows.append(df)
        except Exception: pass
    k = pd.concat(rows).sort_values("time") if rows else pd.DataFrame()
    if not len(k):
        block("L3 行情", ["[缺失] daily_forward 无该标的行——用 valuation 收盘价代理（见估值节）"])
        return
    k["pct"] = k["close"].pct_change() * 100
    k["ma5"] = k["close"].rolling(5).mean(); k["ma10"] = k["close"].rolling(10).mean()
    k["ma20"] = k["close"].rolling(20).mean()
    t = k.tail(8)[["time", "open", "high", "low", "close", "pct", "volume", "amount", "ma5", "ma20"]]
    block("L3 行情（近8日, 前复权）", [t.to_string(index=False)])
    out["kline"] = t.astype(str).to_dict("records")

def fetch_finance(SYM, out):
    base = os.path.join(QS, "3_financial_data")
    block("L2 财务三表", ["<字段说明：单位=元，m_timetag=YYYYMMDD 季频>"])
    def last8(name, cols):
        p = os.path.join(base, name, f"{SYM}.parquet")
        try:
            df = pd.read_parquet(p).sort_values("m_timetag")
            cc = [c for c in ["m_timetag"] + cols if c in df.columns]
            print(f"\n-- {name} --\n{df.tail(8)[cc].to_string(index=False)}")
        except Exception as e:
            print(f"-- {name}: {e}")
    last8("income", ["revenue", "net_profit_incl_min_int_inc"])
    last8("cashflow", ["net_cash_flows_oper_act"])
    last8("balance", ["tot_assets", "tot_liab", "account_receivable", "inventories", "tot_shrhldr_eqy_excl_min_int", "goodwill"])
    last8("pershare_index", ["sales_gross_profit", "inc_net_profit_rate", "equity_roe", "s_fa_eps_basic", "s_fa_bps"])
    for nm, f in (("股东户数", "holder_num"), ("分红", "dividend_factors")):
        try:
            df = pd.read_parquet(os.path.join(base, f, f"{SYM}.parquet"))
            tcol = "endDate" if "endDate" in df.columns else "time"
            df = df.sort_values(tcol)
            print(f"\n-- {nm} --\n{df.tail(4).to_string(index=False)}")
        except Exception as e:
            print(f"-- {nm}: {e}")

def fetch_valuation(SYM, out):
    vcol = ["time", "pe_ttm", "pb", "ps_ttm", "dividend_rate", "total_mv", "float_mv", "close"]
    rows = []
    for d in sorted(glob.glob(os.path.join(QS, "5_technical_derived/valuation/dt=*")))[-760:]:
        try:
            df = read_part("5_technical_derived/valuation", os.path.basename(d), SYM)
            if len(df): rows.append(df.iloc[0])
        except Exception: pass
    v = pd.DataFrame(rows).sort_values("time") if rows else pd.DataFrame()
    block("L1 估值（3年截面分位）", [])
    if not len(v):
        print("  [缺失] valuation 无数据")
        return
    mini = v[["time", "close"]].tail(1)
    print(f"  最近收盘参考: {mini.to_string(index=False)}")
    for c in ["pe_ttm", "pb", "ps_ttm", "dividend_rate"]:
        if c not in v.columns: continue
        s = v[c].dropna()
        if len(s) < 10: continue
        cur = s.iloc[-1]; pct = (s < cur).mean()
        print(f"  {c:12s} 当前={cur:.2f}  3年分位={pct*100:.0f}%")
    out["valuation"] = {c: float(v[c].iloc[-1]) for c in ["pe_ttm", "pb", "ps_ttm"] if c in v.columns}

def fetch_margin(SYM, out):
    rows = []
    for d in sorted(glob.glob(os.path.join(QS, "2_base_sector/margin_trading/dt=*")))[-40:]:
        try:
            df = read_part("2_base_sector/margin_trading", os.path.basename(d), SYM)
            if len(df): rows.append(df)
        except Exception: pass
    if not rows:
        block("L4 两融", ["[缺失] margin_trading 无该标的"])
        return
    m = pd.concat(rows)
    m["time"] = m["time"].astype(str)
    m = m.sort_values("time")
    cc = [c for c in ["time", "finance_balance", "finance_net", "slo_net"] if c in m.columns]
    block("L4 两融（近8日；finance_*=万元）", [m.tail(8)[cc].to_string(index=False)])

def fetch_l4b(SYM, dt, out=None):
    try:
        mkt = read_part("6_ml_datasets/l1_l2_factors", dt)
    except Exception as e:
        print(f"[error] L4b 读 {dt} 失败: {e}")
        return
    s = mkt[mkt.symbol == SYM]
    if not len(s):
        print(f"[缺失] L4b {dt} 无 {SYM}")
        return
    s = s.iloc[0]
    block(f"L4b 订单微结构截面分位（{dt}，全市场 {len(mkt)} 只）", [
        "负IC因子（高位=利空） · 正IC因子（高位=利多） · 参考因子",
    ])
    rows = []
    for f in L4B_NEG + L4B_REF + L4B_POS:
        try:
            v = float(s[f]); pct = (mkt[f].astype(float) < v).mean()
        except Exception:
            continue
        grp = "负IC" if f in L4B_NEG else ("正IC" if f in L4B_POS else "参考")
        rows.append(f"{grp} {f:32s} {v:12.4f}  {pct*100:5.1f}%")
    for r in rows: print("  " + r)
    if out: out["l4b"] = {"dt": dt, "rows": rows}

if __name__ == "__main__":
    main()
