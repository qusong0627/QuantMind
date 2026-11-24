"""模型截面排序能力报告（测试集）：RankIC / 中性化 / Fama-MacBeth / 分位 / 稳定性 / TopN 长多。

评的是「模型给股票打分后，横截面上排序对不对」，以及落到可执行的长多组合后
还剩多少。与 ``ff_attribution.py`` 的分工：
- 本脚本：截面排序（IC、FM、分位、稳定性）+ TopN 长多摘要（复用同一套 cohort 构造）；
- ``ff_attribution.py``：TopN 组合对 FF5 的时序回归与 α；
- ``signal_metrics.py``：纯计算层（数据装载、中性化、IC/FM/分位/稳定性）。

口径要点：
- IC 用模型自带 ``label_return``（训练口径：T+1 收盘买入 → T+N 收盘）；
  rank 基础模型（如 label_rank）没有 label_return 时，用行情现算同口径收益；
  另给「可执行口径」（T+1 开盘买入 → T+N 收盘）作对照；
- 中性化 = 截面 rank 后对 log 市值 + 行业哑变量回归取残差；
- Fama-MacBeth = 每月末截面回归斜率序列的 t 值（|t|>2 才算过）；
- 长多组合 = T+1 开盘买入、持 H 日、开盘涨停剔除、跌停顺延、PIT ST 剔除、
  20 日 ADV 门槛、双边成本。

用法（容器内）：
    docker exec -w /app quantmind python3 /app/backend/scripts/eval/model_signal_report.py \
        --model-id mdl_cn_train_20261123070546_06abbb90_e51bcdd8 \
        --split test --top-ns 20,50,100 --horizon 5 --min-adv 2e7
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.scripts.eval.ff_attribution import (  # noqa: E402
    build_cohort_daily_returns,
    load_picks,
    monthly_compound,
    perf_stats,
    resolve_model_dir,
    run_regression,
)
from backend.scripts.eval.ff_factors import build_or_load_factors  # noqa: E402
from backend.scripts.eval.signal_metrics import (  # noqa: E402,F401 — 测试从这里取
    DECAY_HORIZONS,
    _residualize,
    _spearman,
    _zscore,
    daily_cross_section_ic,
    fama_macbeth,
    ic_decay,
    ic_summary,
    load_signal_frame,
    price_forward_returns,
    quantile_report,
    stability_report,
)
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

log = logging.getLogger("model_signal_report")


def long_only_table(
    pred_path: Path,
    start: str,
    end: str,
    split: str,
    ns: list[int],
    *,
    horizon: int,
    cost_bps: float,
    min_adv: float,
    include_st: bool,
    factors: pd.DataFrame,
) -> list[dict]:
    rows = []
    for n in ns:
        picks = load_picks(pred_path, start, end, split, n, exclude_st=not include_st)
        daily, cohort = build_cohort_daily_returns(
            picks,
            horizon=horizon,
            cost_bps=cost_bps,
            start=start,
            end=end,
            min_adv=min_adv,
        )
        monthly = monthly_compound(daily)
        reg5 = run_regression(monthly, factors, ["MKT", "SMB", "HML", "RMW", "CMA"])
        try:
            reg6 = run_regression(
                monthly, factors, ["MKT", "SMB", "HML", "RMW", "CMA", "TURN"]
            )
            alpha6, t6 = reg6["alpha_annual"], reg6["alpha_t"]
        except RuntimeError:
            alpha6, t6 = np.nan, np.nan
        smb = reg5["loadings"].get("SMB", {})
        rows.append(
            {
                "n": n,
                "perf": perf_stats(monthly),
                "cohort": cohort,
                "alpha5": reg5["alpha_annual"],
                "t5": reg5["alpha_t"],
                "alpha6": alpha6,
                "t6": t6,
                "smb": smb.get("beta", np.nan),
                "smb_t": smb.get("t", np.nan),
                "r2": reg5["r2"],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _fmt_pct(x: float, digits: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:+.{digits}f}%"


def _fmt_num(x: float, digits: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:.{digits}f}"


def _fmt_wan(x: float | None) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x / 1e4:.0f} 万"


def write_report(
    out_dir: Path,
    model_id: str,
    args: argparse.Namespace,
    df: pd.DataFrame,
    ic_daily: pd.DataFrame,
    decay: pd.DataFrame,
    fm_uni: dict,
    fm_ctl: dict,
    quant: dict,
    stab: dict,
    long_rows: list[dict],
    exec_ic: dict[int, dict],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"model_signal_{stamp}.md"
    js_path = out_dir / f"model_signal_{stamp}.json"

    stats = {
        "raw": ic_summary(ic_daily["ic_raw"]),
        "neutral": ic_summary(ic_daily["ic_neutral"]),
        "double": ic_summary(ic_daily["ic_double"]),
    }
    dates = sorted(df["date_key"].unique())
    avg_names = float(df.groupby("date_key").size().mean())
    lines = [
        f"# 模型截面排序能力报告 — {model_id}",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 样本：split={args.split}，{args.start} ~ {args.end}；"
        f"IC 有标签区间 {dates[0]} ~ {dates[-1]}（{len(dates)} 个信号日、"
        f"{df['sym_suffix'].nunique()} 只股票、{len(df):,} 行；日均截面 {avg_names:.0f} 只）",
        f"- 过滤：ADV≥{args.min_adv / 1e4:.0f} 万元"
        + ("、PIT ST 剔除" if not args.include_st else "、含 ST"),
        "- IC 口径：模型自带 `label_return`（T+1 收盘买入 → T+N 收盘）；"
        "「可执行」= T+1 开盘买入 → T+N 收盘（按个股交易日对齐）",
        "- 中性化：截面 rank 后对 log 市值 + 行业哑变量回归取残差；"
        "行业为 instrument_detail 静态快照（非 PIT，已知边界）",
        "",
        "## 一、截面 RankIC",
        "",
        "| 口径 | 均值 | 标准差 | ICIR | 胜率 | t（朴素） | t（HAC lag4） |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    label_map = {
        "raw": "原始 pred",
        "neutral": "中性化信号（市值+行业）",
        "double": "双重中性化（信号+收益）",
    }
    for key in ("raw", "neutral", "double"):
        s = stats[key]
        win_txt = f"{s['win_rate']:.0%}" if np.isfinite(s["win_rate"]) else "—"
        lines.append(
            f"| {label_map[key]} | {_fmt_num(s['mean'], 4)} | {_fmt_num(s['std'], 4)} | "
            f"{_fmt_num(s['icir'])} | {win_txt} | "
            f"{_fmt_num(s['t'])} | {_fmt_num(s['hac_t'])} |"
        )
    if exec_ic:
        lines += [
            "",
            "| 可执行口径（T+1 开盘买） | 均值 | ICIR | 胜率 | t（HAC lag4） |",
            "|---|---:|---:|---:|---:|",
        ]
        for h, s in sorted(exec_ic.items()):
            lines.append(
                f"| h={h} | {_fmt_num(s['mean'], 4)} | {_fmt_num(s['icir'])} | "
                f"{s['win_rate']:.0%} | {_fmt_num(s['hac_t'])} |"
                if np.isfinite(s["win_rate"])
                else f"| h={h} | — | — | — | — |"
            )

    lines += [
        "",
        "## 二、IC 期限结构（持有期越长，信息剩多少）",
        "",
        "| 持有期（交易日） | 1 | 2 | 3 | 5 | 10 | 20 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    decay_map = {int(r["horizon"]): r for _, r in decay.iterrows()}
    lines.append(
        "| 平均 RankIC | "
        + " | ".join(
            _fmt_num(decay_map[h]["mean_ic"], 4) if h in decay_map else "—"
            for h in DECAY_HORIZONS
        )
        + " |"
    )
    lines.append(
        "| t | "
        + " | ".join(
            _fmt_num(decay_map[h]["t"]) if h in decay_map else "—"
            for h in DECAY_HORIZONS
        )
        + " |"
    )
    lines.append("")
    lines.append(
        "若 IC 随持有期上升（h20>h5>h1），说明信号偏慢、更适合长持有期；"
        "若快速衰减，则是短周期信号。"
    )

    lines += [
        "",
        "## 三、Fama-MacBeth（月末截面回归，斜率序列 t）",
        "",
        "| 口径 | 月数 | 平均斜率 | t | \\|t\\|>2 月份占比 | 斜率>0 占比 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, fm in (("单变量", fm_uni), ("加市值+行业控制", fm_ctl)):
        lines.append(
            f"| {label} | {fm['n']} | {_fmt_num(fm['mean_slope'], 4) if np.isfinite(fm['mean_slope']) else '—'} | "
            f"{_fmt_num(fm['t'])} | {fm['frac_abs_t_gt2']:.0%} | {fm['frac_positive']:.0%} |"
            if np.isfinite(fm["frac_abs_t_gt2"])
            else f"| {label} | {fm['n']} | — | — | — | — |"
        )
    lines += [
        "",
        "斜率 = 标准化 pred 每高 1 个截面标准差的 T+N 收益差；|t|>2 才算显著。",
        "",
        f"## 四、分位收益（{len(quant['buckets'])} 组，等权、模型标签口径）",
        "",
        "| 分位 | "
        + " | ".join(f"Q{i + 1}" for i in range(len(quant["buckets"])))
        + " |",
        "|---|" + "---:|" * len(quant["buckets"]),
        "| 平均 T+N 收益 | "
        + " | ".join(_fmt_pct(b, 2) for b in quant["buckets"])
        + " |",
        "",
        f"- 单调性（分位序号 vs 平均收益，Spearman）：**{_fmt_num(quant['monotonicity'])}**"
        f"（≥0.7 才算单调）",
        f"- Q高−Q低 平均价差：**{_fmt_pct(quant['spread_mean'])}**，t={_fmt_num(quant['spread_t'])}，"
        f"正值占比 {quant['spread_pos_rate']:.0%}（仅诊断：A 股无法做空）",
        "",
        "## 五、稳定性",
        "",
        f"- 相邻交易日 pred 秩相关：**{_fmt_num(stab['rank_autocorr_daily'], 3)}**"
        f"（{stab['rank_autocorr_n']} 对；越高说明打分越粘）",
        "- 每 H 日调仓的 TopN 重合率："
        + "、".join(
            f"Top{n} {stab['top_overlap'].get(n, float('nan')):.0%}"
            for n in sorted(stab["top_overlap"])
            if np.isfinite(stab["top_overlap"].get(n, np.nan))
        ),
        "",
        f"## 六、TopN 长多能力（可执行：T+1 开盘、持 {args.horizon} 日、"
        f"双边 {args.cost_bps:.0f}bp、ADV 门槛、剔 ST/开盘涨停）",
        "",
        "| TopN | 月数 | 年化 | 波动 | Sharpe | 最大回撤 | 平均持仓 | 容量(5%) | FF5 α 年化 | α t | +TURN α | α t | SMB(t) | R² |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in long_rows:
        p = row["perf"]
        c = row["cohort"]
        lines.append(
            f"| {row['n']} | {p.get('months', 0)} | {_fmt_pct(p.get('ann_return', np.nan), 1)} | "
            f"{p.get('ann_vol', np.nan) * 100:.1f}% | {_fmt_num(p.get('sharpe'))} | "
            f"{p.get('max_drawdown', np.nan) * 100:.1f}% | {c.get('avg_names', np.nan):.1f} | "
            f"{_fmt_wan(c.get('capacity_p5'))} | {_fmt_pct(row['alpha5'], 1)} | "
            f"{_fmt_num(row['t5'])} | {_fmt_pct(row['alpha6'], 1)} | {_fmt_num(row['t6'])} | "
            f"{_fmt_num(row['smb'], 2)} ({_fmt_num(row['smb_t'], 1)}) | {_fmt_num(row['r2'])} |"
        )

    verdict = _verdict(stats, fm_uni, fm_ctl, quant, decay_map, long_rows)
    lines += ["", "## 七、判读", "", *verdict, ""]
    lines += [
        "规则：RankIC 均值>0.02 且 HAC t≥2 才算有排序能力；中性化后仍 >0.01 且 t≥2 才算不是市值/行业 beta；",
        "FM 月度 t≥2 且 |t|>2 月份占比≥50% 才算月度稳健；分位单调性≥0.7 且 Q高−Q低>0；",
        "IC 期限结构：5 日 IC 不低于 1 日的一半（信息保留≥50%）；",
        "长多要求 FF5 α>0 且 t≥2（两种因子口径都过才算成立）。",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "model_id": model_id,
        "args": vars(args),
        "sample": {
            "dates": len(dates),
            "symbols": int(df["sym_suffix"].nunique()),
            "rows": int(len(df)),
            "avg_names": avg_names,
        },
        "ic": dict(stats),
        "executable_ic": exec_ic,
        "decay": decay.to_dict(orient="records"),
        "fama_macbeth": {"univariate": fm_uni, "with_controls": fm_ctl},
        "quantiles": quant,
        "stability": stab,
        "long_only": long_rows,
    }
    js_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8",
    )
    return md_path


def _verdict(stats, fm_uni, fm_ctl, quant, decay_map, long_rows) -> list[str]:
    raw, neu = stats["raw"], stats["neutral"]

    def ok(flag: bool) -> str:
        return "✅" if flag else "❌"

    ic_ok = np.isfinite(raw["mean"]) and raw["mean"] > 0.02 and raw["hac_t"] >= 2
    neu_ok = np.isfinite(neu["mean"]) and neu["mean"] > 0.01 and neu["hac_t"] >= 2
    fm_ok = (
        np.isfinite(fm_uni["t"])
        and fm_uni["t"] >= 2
        and np.isfinite(fm_uni["frac_abs_t_gt2"])
        and fm_uni["frac_abs_t_gt2"] >= 0.5
    )
    mono_ok = (
        np.isfinite(quant["monotonicity"])
        and quant["monotonicity"] >= 0.7
        and quant["spread_mean"] > 0
    )
    ic1 = decay_map.get(1, {}).get("mean_ic", np.nan)
    ic5 = decay_map.get(5, {}).get("mean_ic", np.nan)
    info_kept = bool(
        np.isfinite(ic1) and ic1 > 0 and np.isfinite(ic5) and ic5 >= 0.5 * ic1
    )
    slow_signal = bool(np.isfinite(ic1) and np.isfinite(ic5) and ic5 > ic1)
    long_ok = bool(long_rows) and all(
        np.isfinite(r["alpha5"])
        and r["alpha5"] > 0
        and np.isfinite(r["t5"])
        and r["t5"] >= 2
        for r in long_rows
    )
    out = [
        f"- 排序能力（原始 RankIC）：{ok(ic_ok)} 均值 {raw['mean']:.4f}，HAC t={raw['hac_t']:.2f}"
        if np.isfinite(raw["mean"])
        else "- 排序能力（原始 RankIC）：❌ 样本不足",
        f"- 剔除市值/行业后：{ok(neu_ok)} 中性化 RankIC {neu['mean']:.4f}，HAC t={neu['hac_t']:.2f}",
        f"- 分位单调性：{ok(mono_ok)} {quant['monotonicity']:.2f}，Q高−Q低 {quant['spread_mean'] * 100:+.2f}%",
        f"- IC 期限结构：{ok(info_kept)} h1={ic1:.4f} → h5={ic5:.4f}"
        + ("（随持有期上升：信号偏慢，更适合长持有期）" if slow_signal else "")
        if np.isfinite(ic1) and np.isfinite(ic5)
        else "- IC 期限结构：❌ 样本不足",
        f"- Fama-MacBeth 月度稳健性：{ok(fm_ok)} t={fm_uni['t']:.2f}，"
        f"|t|>2 月份 {fm_uni['frac_abs_t_gt2']:.0%}，斜率>0 月份 {fm_uni['frac_positive']:.0%}",
        f"- TopN 长多 FF5 α（各 N 均需 α>0、t≥2）：{ok(long_ok)}",
    ]
    rank_ok = ic_ok and neu_ok and mono_ok
    if rank_ok and not long_ok:
        out.append(
            "  → 结论：**截面排序有信息（IC 强、中性化后仍强、分位单调），但长多组合剥离风格后"
            "没有可确认的 α**——排序能力可用于选股输入/风险分层，不能直接当多头策略。"
        )
    elif rank_ok and long_ok:
        out.append(
            "  → 结论：**排序与长多两端都过线**，但仍需看双因子口径与分年稳定性后再谈准入。"
        )
    elif not rank_ok and long_ok:
        out.append(
            "  → 结论：**长多 α 与排序证据矛盾**（可能来自少数极端票/风格），先查分年与集中度，不采信。"
        )
    else:
        out.append("  → 结论：**没有可确认的截面排序能力**，不应作为选股信号使用。")
    if not fm_ok:
        out.append(
            "  注：FM 未过线说明**月频斜率方向不稳**（正斜率月份占比偏低或斜率波动大）；"
            "这不否定逐日截面排序，但意味着不能把月频斜率当稳定收益来源。"
        )
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="模型截面排序能力报告（测试集）")
    ap.add_argument("--model-id")
    ap.add_argument("--pred-path")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument(
        "--split", default="test", choices=["all", "train", "valid", "test"]
    )
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--top-ns", default="20,50,100")
    ap.add_argument("--quantiles", type=int, default=10)
    ap.add_argument(
        "--min-adv", type=float, default=2.0e7, help="20 日日均成交额下限（元）"
    )
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--include-st", action="store_true")
    ap.add_argument("--sort-mode", choices=["monthly", "academic"], default="academic")
    ap.add_argument("--orth-inv", action="store_true")
    ap.add_argument("--rf-annual", type=float, default=0.0)
    ap.add_argument("--rebuild-factors", action="store_true")
    ap.add_argument("--out-dir", default=None)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)
    if not args.pred_path and not args.model_id:
        raise SystemExit("需要 --model-id 或 --pred-path")
    model_dir = (
        Path(args.pred_path).parent
        if args.pred_path
        else resolve_model_dir(args.model_id)
    )
    pred_path = Path(args.pred_path) if args.pred_path else model_dir / "pred.parquet"
    model_id = args.model_id or model_dir.name

    head = pd.read_parquet(pred_path, columns=["trade_date", "split"])
    head["trade_date"] = pd.to_datetime(head["trade_date"])
    win = head[head["split"] == args.split] if args.split != "all" else head
    args.start = args.start or win["trade_date"].min().strftime("%Y-%m-%d")
    args.end = args.end or win["trade_date"].max().strftime("%Y-%m-%d")
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else resolve_quantdb_dir().parent
        / "factor_research"
        / "model_signal"
        / model_id
    )
    log.info("窗口 %s ~ %s；模型 %s", args.start, args.end, model_id)

    df = load_signal_frame(
        pred_path,
        args.start,
        args.end,
        args.split,
        min_adv=args.min_adv,
        exclude_st=not args.include_st,
    )
    exec_horizons = tuple(sorted({1, args.horizon, 20}))
    price_horizons = tuple(sorted(set(exec_horizons) | set(DECAY_HORIZONS)))
    rets = price_forward_returns(
        sorted(df["sym_suffix"].unique()), args.start, args.end, price_horizons
    )
    if "label_return" not in df.columns:
        log.info(
            "pred 无 label_return（rank 基础模型），用行情现算 T+1 收盘 → T+%d 收盘收益",
            args.horizon,
        )
        df = df.merge(
            rets["close_entry"][args.horizon].rename("label_return"),
            left_on=["date_key", "sym_suffix"],
            right_index=True,
            how="left",
        ).dropna(subset=["label_return"])
        df = df.reset_index(drop=True)
    if df.empty:
        raise SystemExit("样本为空：检查 split/日期/ADV 门槛")
    log.info(
        "样本 %d 行 / %d 日 / %d 只",
        len(df),
        df["date_key"].nunique(),
        df["sym_suffix"].nunique(),
    )

    log.info("逐日截面 IC …")
    ic_daily = daily_cross_section_ic(df)
    exec_ic = {}
    for h in exec_horizons:
        frame = df.merge(
            rets["open_entry"][h].rename("ret_exec"),
            left_on=["date_key", "sym_suffix"],
            right_index=True,
            how="left",
        )
        series = pd.Series(
            [
                _spearman(
                    g["pred"].to_numpy(dtype=float),
                    g["ret_exec"].to_numpy(dtype=float),
                )
                for _, g in frame.dropna(subset=["ret_exec"]).groupby("date_key")
            ]
        ).dropna()
        exec_ic[h] = ic_summary(series)

    log.info("IC 衰减 …")
    decay = ic_decay(df, rets["close_entry"])
    log.info("Fama-MacBeth …")
    fm_uni = fama_macbeth(df, with_controls=False)
    fm_ctl = fama_macbeth(df, with_controls=True)
    log.info("分位与稳定性 …")
    quant = quantile_report(df, args.quantiles)
    top_ns = sorted({int(x) for x in args.top_ns.split(",") if x.strip()})
    stab = stability_report(df, args.horizon, top_ns)

    log.info("TopN 长多（%s）…", top_ns)
    factors, _ = build_or_load_factors(
        args.start,
        args.end,
        rf_annual=args.rf_annual,
        sort_mode=args.sort_mode,
        orth_inv=args.orth_inv,
        rebuild=args.rebuild_factors,
    )
    long_rows = long_only_table(
        pred_path,
        args.start,
        args.end,
        args.split,
        top_ns,
        horizon=args.horizon,
        cost_bps=args.cost_bps,
        min_adv=args.min_adv,
        include_st=args.include_st,
        factors=factors,
    )
    md = write_report(
        out_dir,
        model_id,
        args,
        df,
        ic_daily,
        decay,
        fm_uni,
        fm_ctl,
        quant,
        stab,
        long_rows,
        exec_ic,
    )
    print(f"报告已生成：{md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
