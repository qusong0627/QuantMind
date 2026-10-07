#!/usr/bin/env python3
"""假设库实验室 v1：把定性认知变成带胜率的可验证假设（阶段2 P1）。

首批 3 条价格代理规则（未来可用事件数据升级）：
 R1 放量滞涨：量>2×5日均量 且当日涨 0~3% → 次5日
 R2 缩量回踩：连跌3日 且末日量<0.7×5日均量 → 次5日
 R3 动量追高：5日涨>8% 后 5 日表现（追高回撤检测）
统计口径：次5日胜率=P(chg>0)、均值、样本 n；市场=全体样本对照。
Walk-forward 纪律（2026-09-04）：全样本按 wf-window 交易日切滚动窗，
  pooled 判定 verified 还须 ①OOS（末窗）与 IS（此前各窗）方向一致
  ②同向窗口占比 ≥60%。pooled=contradicted 维持不变；WF 不达标降为 proposed。
用法：python scripts/hypothesis_lab.py [--symbols 50] [--days 260] [--wf-window 40]
结果写回 configs/hypotheses.json（win_rate/n/updated/wf），供提示词注入。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HYP_FILE = ROOT / "configs" / "hypotheses.json"

DEFAULT_HYPOTHESES = {
    "R1_volume_stall": {
        "name": "放量滞涨（量>2×5日均量 且日涨0~3%）", "direction": "次日5日偏空",
        "win_rate": None, "avg_ret": None, "n": None, "updated": None,
        "status": "proposed", "note": "价格代理，待事件数据升级"},
    "R2_shrink_pullback": {
        "name": "缩量回踩（连跌3日 末日量<0.7×均量）", "direction": "次日5日偏多",
        "win_rate": None, "avg_ret": None, "n": None, "updated": None,
        "status": "proposed", "note": "价格代理"},
    "R3_momentum_chase": {
        "name": "动量追高（5日涨>8%后次5日）", "direction": "追高回撤检测",
        "win_rate": None, "avg_ret": None, "n": None, "updated": None,
        "status": "proposed", "note": "价格代理"},
    "R4_big_drop_volume": {
        "name": "放量大阴（量>2×均量 且日跌≥3%）", "direction": "次日5日",
        "win_rate": None, "avg_ret": None, "n": None, "updated": None,
        "status": "proposed", "note": "价格代理"},
}


def decide_status(want_bull: bool, diff: float | None, n: int) -> str:
    """假设状态判定（P0-3 可测）：方向与实测一致且差≥3pp=verified；相反=contradicted。"""
    if n < 30 or diff is None:
        return "insufficient" if n < 30 else "proposed"
    if abs(diff) < 0.03:
        return "proposed"
    return "verified" if (diff > 0) == want_bull else "contradicted"


def decide_status_wf(pooled: str, is_diff: float | None, oos_diff: float | None,
                     stable_ratio: float | None) -> tuple[str, str]:
    """Walk-forward 纪律：verified 须 OOS 与 IS 同向且稳定率达标；
    contradicted 维持（反向证据明确）；其余透传。返回 (status, note)。"""
    if pooled != "verified":
        return pooled, ""
    if is_diff is None or oos_diff is None or stable_ratio is None:
        return "proposed", "WF 窗口不足，无法验证 OOS"
    same_dir = (is_diff > 0) == (oos_diff > 0)
    if same_dir and stable_ratio >= 0.6:
        return "verified", ""
    why = []
    if not same_dir:
        why.append("OOS 与 IS 方向相反")
    if stable_ratio < 0.6:
        why.append(f"同向窗占比仅 {stable_ratio:.0%}")
    return "proposed", "WF 未达标（" + "、".join(why) + "）"


def _pick_samples(top: int) -> list:
    """用最新 partition 按成交额取流动性前 top 只（避免全市场权重失真）。"""
    import duckdb
    import glob

    base = ROOT / "data" / "quantdb" / "1_kline_data" / "daily_backward"
    if not base.is_dir():
        base = Path("/data/quantdb/1_kline_data/daily_backward")
    parts = sorted(glob.glob(f"{base}/dt=*"))[-2:]
    files = [f"{p}/*.parquet" for p in parts]
    con = duckdb.connect()
    df = con.execute(
        f"SELECT symbol, sum(amount) amt FROM read_parquet({files!r}) GROUP BY 1 ORDER BY 2 DESC LIMIT {int(top)}").df()
    return list(df["symbol"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=60)
    ap.add_argument("--days", type=int, default=260)
    ap.add_argument("--wf-window", type=int, default=40, help="walk-forward 滚动窗长（交易日）")
    args = ap.parse_args()

    import duckdb
    import glob

    base = ROOT / "data" / "quantdb" / "1_kline_data" / "daily_backward"
    if not base.is_dir():
        base = Path("/data/quantdb/1_kline_data/daily_backward")
    syms = _pick_samples(args.symbols)
    parts = sorted(glob.glob(f"{base}/dt=*"))[-args.days:]
    print(f"样本 {len(syms)} 只 · 窗口 {len(parts)} 交易日")
    con = duckdb.connect()
    df = con.execute(
        f"SELECT symbol, dt d, close, volume, amount "
        f"FROM read_parquet({[f'{p}/*.parquet' for p in parts]!r}) "
        f"WHERE symbol IN ({','.join(repr(s) for s in syms)})").df()
    df = df.sort_values(["symbol", "d"]).reset_index(drop=True)
    g = df.groupby("symbol")
    df["vol5"] = g["volume"].transform(lambda s: s.rolling(5, min_periods=4).mean().shift(1))
    df["chg"] = g["close"].pct_change() * 100
    df["chg5"] = g["close"].pct_change(5) * 100
    f = g["close"].shift(-5)  # 分组内前视，防跨股票边界错位
    df["fwd5"] = (f / df["close"] - 1) * 100

    # 条件掩码（walk-forward 按窗重算胜率差）
    stats: dict = {}
    cond = df["vol5"] > 0
    # R1 放量滞涨
    stats["R1_volume_stall"] = cond & (df["volume"] > 2 * df["vol5"]) & (df["chg"] >= 0) & (df["chg"] <= 3)
    # R2 缩量回踩
    down3 = df.groupby("symbol")["chg"].transform(
        lambda s: (s < 0) & (s.shift(1) < 0) & (s.shift(2) < 0))
    stats["R2_shrink_pullback"] = cond & down3 & (df["volume"] < 0.7 * df["vol5"])
    # R3 动量追高
    stats["R3_momentum_chase"] = df["chg5"] > 8
    # R4 放量大阴
    stats["R4_big_drop_volume"] = cond & (df["volume"] > 2 * df["vol5"]) & (df["chg"] <= -3)
    # walk-forward 滚动窗：按交易日序号切窗
    uniq_days = sorted(df["d"].unique())
    df["wf_win"] = df["d"].map({d: i // max(args.wf_window, 1) for i, d in enumerate(uniq_days)})
    n_windows = int(df["wf_win"].max()) + 1 if len(uniq_days) else 0
    # 市场对照
    base_mask = df["fwd5"].notna()

    base_win = float((df.loc[base_mask, "fwd5"] > 0).mean())
    base_avg = float(df.loc[base_mask, "fwd5"].mean())
    hyps = json.loads(HYP_FILE.read_text(encoding="utf-8")) if HYP_FILE.is_file() else dict(DEFAULT_HYPOTHESES)

    def _win_stats(mask, w):
        sel = mask & (df["wf_win"] == w)
        v = df.loc[sel, "fwd5"].dropna()
        win = float((v > 0).mean()) if len(v) else None
        diff = (win - base_win) if win is not None else None
        return int(len(v)), win, diff

    for key, mask in stats.items():
        s = df.loc[mask, "fwd5"].dropna()
        n = int(len(s))
        win = float((s > 0).mean()) if n else None
        avg = float(s.mean()) if n else None
        if key not in hyps:
            hyps[key] = dict(DEFAULT_HYPOTHESES[key])
        diff = (win - base_win) if win is not None else None
        want_bull = "多" in str(hyps[key].get("direction", ""))
        pooled = decide_status(want_bull, diff, n)
        # walk-forward：逐窗 vs 同窗基准；IS=除末窗外，OOS=末窗
        wf, status, wf_note = None, pooled, ""
        if pooled == "verified" and n < 60:
            # 样本撑不起滚动验证："已验证"名不副实 → 降为 proposed（2026-09-04 R4 案例）
            status, wf_note = "proposed", "WF 样本不足（n<60 无法滚动验证）"
        elif n_windows >= 4 and n >= 60:
            wins = []
            for w in range(n_windows):
                wn, wwin, wdiff = _win_stats(mask, w)
                _, _, wdiff_b = _win_stats(base_mask, w)
                if wwin is not None and wdiff_b is not None:
                    wins.append({"win": w, "n": wn,
                                 "diff_pp": round((wdiff - wdiff_b) * 100, 1)})
            if len(wins) >= 4:
                is_diff = sum(x["diff_pp"] for x in wins[:-1]) / (len(wins) - 1)
                oos_diff = wins[-1]["diff_pp"]
                qualifying = [x for x in wins if x["n"] >= 15]
                stable_ratio = (round(sum(1 for x in qualifying
                                          if (x["diff_pp"] > 0) == (diff > 0))
                                      / len(qualifying), 2)
                                if len(qualifying) >= 3 else None)
                status, wf_note = decide_status_wf(pooled, is_diff, oos_diff, stable_ratio)
                wf = {"windows": wins, "is_diff_pp": round(is_diff, 1),
                      "oos_diff_pp": round(oos_diff, 1),
                      "stable_ratio": stable_ratio, "verdict": status, "note": wf_note}
        if wf_note:
            hyps[key]["note"] = (str(hyps[key].get("note") or "") +
                                 ("" if not hyps[key].get("note") else "；") + wf_note).strip("；")
        hyps[key].update({"win_rate": round(win, 3) if win is not None else None,
                          "avg_ret": round(avg, 3) if avg is not None else None,
                          "vs_base_pp": round(diff * 100, 1) if diff is not None else None,
                          "n": n, "updated": datetime.now().strftime("%Y-%m-%d"),
                          "status": status, "wf": wf})
        msg = (f"{key}: n={n} 胜率 {win:.1%} vs 基准 {base_win:.1%} → pooled={pooled} "
               f"({diff * 100:+.1f}pp)")
        if wf:
            msg += f" | WF: IS {wf['is_diff_pp']:+.1f}pp / OOS {wf['oos_diff_pp']:+.1f}pp / 稳定率 {wf['stable_ratio']} → {status}"
            if wf_note:
                msg += f"（{wf_note}）"
        print(msg)
    HYP_FILE.parent.mkdir(exist_ok=True)
    HYP_FILE.write_text(json.dumps(hyps, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 假设库已更新 → configs/hypotheses.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())