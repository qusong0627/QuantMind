"""模型推理选股的 Fama-French 五因子（A 股版）归因。

评的是什么：模型 Top-N 选股组成的**组合**，不是单只股票。
产出：组合月度收益对 FF5（可选再加换手因子 TURN）的回归——
α（剥离风格后的残余超额）与五个/六个因子载荷，附 HAC(Newey-West) t 值、
R²、分年 α。判读规则写在报告末尾。

用法（容器内）：
    docker exec quantmind python3 /app/backend/scripts/eval/ff_attribution.py \
        --model-id mdl_cn_train_20261123070546_06abbb90_e51bcdd8 \
        --top-n 20 --horizon 5 --cost-bps 15

口径与构建细节见 ``ff_factors.py``；本脚本只负责组合、回归与报告。
"""

from __future__ import annotations

import argparse
import glob
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

from backend.scripts.eval.ff_factors import (  # noqa: E402
    build_or_load_factors,
    factor_diagnostics,
)
from backend.shared.quantdb_paths import resolve_quantdb_dir, resolve_quantdb_subdir  # noqa: E402
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402

from backend.scripts.eval.ff_attribution_report import (  # noqa: E402
    FACTOR_LABELS,
    write_report,
)

log = logging.getLogger("ff_attribution")


# ---------------------------------------------------------------------------
# HAC（Newey-West）OLS
# ---------------------------------------------------------------------------
def hac_ols(y: np.ndarray, X: np.ndarray, lags: int | None = None) -> dict:
    """带 Newey-West HAC 标准误的 OLS（含截距）。

    返回 beta/se/t/r2/n/lags；beta[0] 即 α。协方差按
    ``(X'X)^-1 · n·S · (X'X)^-1``，S 为 Bartlett 核加权的 score 协方差。
    """
    y = np.asarray(y, dtype=np.float64).ravel()
    X = np.asarray(X, dtype=np.float64)
    if X.ndim == 1:
        X = X[:, None]
    n = y.size
    if n < X.shape[1] + 3:
        raise ValueError(f"样本不足：n={n}, k={X.shape[1]}")
    Xc = np.column_stack([np.ones(n), X])
    xtx_inv = np.linalg.pinv(Xc.T @ Xc)
    beta = xtx_inv @ Xc.T @ y
    resid = y - Xc @ beta
    if lags is None:
        lags = max(1, min(n // 3, int(np.floor(4 * (n / 100.0) ** (2.0 / 9.0)))))
    scores = Xc * resid[:, None]
    S = scores.T @ scores / n
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        G = scores[lag:].T @ scores[:-lag] / n
        S += weight * (G + G.T)
    cov = xtx_inv @ S @ xtx_inv * n
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        tvals = np.where(se > 0, beta / se, np.nan)
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return {
        "beta": beta,
        "se": se,
        "t": tvals,
        "r2": r2,
        "n": int(n),
        "lags": int(lags),
        "resid": resid,
    }


# ---------------------------------------------------------------------------
# 组合构造
# ---------------------------------------------------------------------------
def limit_pct_for_symbol(symbol: str) -> float:
    """板块涨跌幅上限（主板 10% / 创业板科创 20% / 北交 30%）。"""
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[:6]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


def _read_partitions(
    subdir: Path, dates: list[str], symbols: set[str], cols: tuple[str, ...]
) -> pd.DataFrame:
    """逐日分区读行情并只保留目标股票（5k 行/日，pandas 过滤足够快）。"""
    frames = []
    for dt in dates:
        path = subdir / f"dt={dt}" / "data.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["symbol", "time", *cols])
        except Exception as exc:  # noqa: BLE001 — 单日坏分区跳过并披露
            log.warning("读取分区失败 %s: %s", path, exc)
            continue
        df = df[df["symbol"].astype(str).isin(symbols)]
        if not df.empty:
            df["dt"] = dt
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["symbol", "dt", *cols])
    return pd.concat(frames, ignore_index=True)


def _trading_days(start: str, end: str) -> list[str]:
    fwd = resolve_quantdb_subdir("1_kline_data", "daily_forward")
    lo, hi = int(start.replace("-", "")), int(end.replace("-", ""))
    out = []
    for p in sorted(glob.glob(str(fwd / "dt=*"))):
        dt = p.rsplit("=", 1)[-1]
        if len(dt) == 8 and dt.isdigit() and lo <= int(dt) <= hi:
            out.append(dt)
    return out


def resolve_model_dir(model_id: str) -> Path:
    for root in ("/app/models", str(_ROOT / "models")):
        hits = glob.glob(f"{root}/users/*/*/{model_id}")
        for hit in hits:
            if (Path(hit) / "pred.parquet").exists():
                return Path(hit)
    raise FileNotFoundError(f"找不到模型目录：{model_id}")


def _load_st_pit() -> set[tuple[str, str]] | None:
    """PIT ST 稀疏表（symbol, YYYYMMDD）→ 集合；缺失返回 None。

    用公告/逐日 isST 而不是当前快照：拿「现在是不是 ST」过滤历史 = 前视。
    """
    path = resolve_quantdb_subdir("2_base_sector", "st_pit", "st_pit.parquet")
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["symbol", "time", "is_st"])
    df = df[df["is_st"] == True]  # noqa: E712 — 兼容 bool/np.bool_
    if df.empty:
        return set()
    days = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y%m%d")
    return {
        (str(s), str(d))
        for s, d in zip(df["symbol"], days, strict=True)
        if isinstance(d, str)
    }


def load_picks(
    pred_path: Path,
    start: str,
    end: str,
    split: str,
    top_n: int,
    *,
    bottom: bool = False,
    exclude_st: bool = True,
) -> dict[str, list[str]]:
    """逐信号日取 TopN（或 BottomN）股票（suffix 代码）；默认剔除信号日 PIT ST。"""
    df = pd.read_parquet(pred_path, columns=["symbol", "trade_date", "pred", "split"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    df = df[(df["trade_date"] >= lo) & (df["trade_date"] <= hi)]
    if split != "all":
        df = df[df["split"] == split]
    df = df.dropna(subset=["pred"])
    st_set = _load_st_pit() if exclude_st else None
    picks: dict[str, list[str]] = {}
    for trade_date, frame in df.groupby("trade_date", sort=True):
        frame = frame.sort_values("pred", ascending=bool(bottom))
        key = trade_date.strftime("%Y%m%d")
        syms = [
            StockCodeUtil.to_suffix(str(s)) for s in frame["symbol"].head(top_n * 2)
        ]
        if st_set is not None:
            syms = [s for s in syms if (s, key) not in st_set]
        picks[key] = syms[:top_n]
    return picks


def build_cohort_daily_returns(
    picks: dict[str, list[str]],
    *,
    horizon: int,
    cost_bps: float,
    start: str,
    end: str,
    min_adv: float = 2.0e7,
    adv_window: int = 20,
) -> tuple[pd.Series, dict]:
    """把逐日 TopN 组合成「重叠 cohort」日收益序列。

    - 信号日 T 的股票在 T+1 开盘买入（前复权 open），持有 H 个交易日后开盘卖出；
    - T+1 开盘涨停（相对 T 收盘）视为买不进 → 剔除；退出日开盘跌停 → 顺延一日；
    - ``min_adv``：信号日 20 日日均成交额下限（元），低于则不可配置 → 剔除；
    - 成本 = 双边 ``2×cost_bps``，均摊到持有的 H 天；
    - 每日组合收益 = 当日所有活跃 cohort 日收益的等权均值；
    - 容量：按参与率 5%/10% 与等权持仓，估计每个 cohort 的最大资金规模。
    """
    symbols = sorted({s for syms in picks.values() for s in syms})
    if not symbols:
        raise RuntimeError("窗口内没有可用选股")
    pad_start = pd.Timestamp(start) - pd.Timedelta(days=20)
    # 需要 H 个交易日之后的退出日：多取一段日历
    extra_end = (pd.Timestamp(end) + pd.Timedelta(days=horizon * 3 + 20)).strftime(
        "%Y-%m-%d"
    )
    tdays = _trading_days(pad_start.strftime("%Y-%m-%d"), extra_end)
    idx_of = {d: i for i, d in enumerate(tdays)}

    sym_set = set(symbols)
    fwd = resolve_quantdb_subdir("1_kline_data", "daily_forward")
    raw = resolve_quantdb_subdir("1_kline_data", "daily_unadjusted")
    fwd_df = _read_partitions(fwd, tdays, sym_set, ("open", "close", "amount"))
    raw_df = _read_partitions(raw, tdays, sym_set, ("open", "close"))
    fwd_open = fwd_df.pivot(index="dt", columns="symbol", values="open").reindex(tdays)
    # daily_forward 的 amount 单位是「万元」（实测 000001.SZ 2025-03-05 = 125489.2
    # 万元 ≈ 12.5 亿元）；门槛与容量均按元计，这里先统一乘 1e4。
    fwd_amount_yuan = (
        fwd_df.pivot(index="dt", columns="symbol", values="amount").reindex(tdays) * 1e4
    )
    adv = fwd_amount_yuan.rolling(
        adv_window, min_periods=max(5, adv_window // 2)
    ).median()
    raw_open = raw_df.pivot(index="dt", columns="symbol", values="open").reindex(tdays)
    raw_close = raw_df.pivot(index="dt", columns="symbol", values="close").reindex(
        tdays
    )

    rt_cost = 2.0 * float(cost_bps) / 1e4
    daily: dict[str, list[float]] = {}
    stats = {
        "signals": 0,
        "cohorts": 0,
        "dropped_limit_up": 0,
        "dropped_low_adv": 0,
        "dropped_missing": 0,
        "dropped_short": 0,
        "avg_names": [],
        "exit_extended": 0,
        "capacity_p5": [],
        "capacity_p10": [],
    }
    for signal, syms in picks.items():
        if signal not in idx_of:
            continue
        i = idx_of[signal]
        if i + 1 + horizon >= len(tdays):
            continue
        entry_i = i + 1
        entry = tdays[entry_i]
        stats["signals"] += 1
        names: list[str] = []
        for sym in syms:
            e_open = fwd_open.at[entry, sym] if sym in fwd_open.columns else np.nan
            if not np.isfinite(e_open):
                stats["dropped_missing"] += 1
                continue
            # 开盘涨停买不进
            prev_close = (
                raw_close.at[signal, sym] if sym in raw_close.columns else np.nan
            )
            o_raw = raw_open.at[entry, sym] if sym in raw_open.columns else np.nan
            if np.isfinite(prev_close) and np.isfinite(o_raw) and prev_close > 0:
                if o_raw / prev_close - 1.0 >= limit_pct_for_symbol(sym) - 0.005:
                    stats["dropped_limit_up"] += 1
                    continue
            # 20 日日均成交额门槛：流动性不足的票不计入可配置组合
            if min_adv > 0:
                adv_val = adv.at[signal, sym] if sym in adv.columns else np.nan
                if not np.isfinite(adv_val) or adv_val < min_adv:
                    stats["dropped_low_adv"] += 1
                    continue
            # 退出日：默认 entry+H；遇跌停顺延，最多 +10 天
            exit_i = entry_i + horizon
            limit_down = limit_pct_for_symbol(sym) - 0.005
            while exit_i < len(tdays):
                exit_day = tdays[exit_i]
                if sym not in fwd_open.columns or not np.isfinite(
                    fwd_open.at[exit_day, sym]
                ):
                    stats["dropped_missing"] += 1
                    exit_i = -1
                    break
                o_exit = (
                    raw_open.at[exit_day, sym] if sym in raw_open.columns else np.nan
                )
                pc = (
                    raw_close.at[tdays[exit_i - 1], sym]
                    if sym in raw_close.columns
                    else np.nan
                )
                if (
                    np.isfinite(o_exit)
                    and np.isfinite(pc)
                    and pc > 0
                    and o_exit / pc - 1.0 <= -limit_down
                ):
                    exit_i += 1
                    stats["exit_extended"] += 1
                    continue
                break
            if exit_i < 0 or exit_i >= len(tdays):
                continue
            # cohort 日收益（open→open）
            rets = []
            ok = True
            for j in range(entry_i + 1, exit_i + 1):
                p0 = fwd_open.at[tdays[j - 1], sym]
                p1 = fwd_open.at[tdays[j], sym]
                if not (np.isfinite(p0) and np.isfinite(p1) and p0 > 0):
                    ok = False
                    break
                rets.append(float(p1 / p0 - 1.0))
            if not ok or not rets:
                stats["dropped_missing"] += 1
                continue
            names.append(sym)
            for j, r in zip(range(entry_i + 1, exit_i + 1), rets, strict=True):
                daily.setdefault(tdays[j], []).append(r - rt_cost / len(rets))
        if len(names) < 3:
            stats["dropped_short"] += 1
            continue
        stats["cohorts"] += 1
        stats["avg_names"].append(len(names))
        if min_adv > 0 and names:
            adv_vals = [
                adv.at[signal, s]
                for s in names
                if s in adv.columns and np.isfinite(adv.at[signal, s])
            ]
            if adv_vals:
                # 等权持仓：组合规模 S 需满足 S/N <= p·ADV_i → S <= N·p·min(ADV)
                base = len(names) * float(np.min(adv_vals))
                stats["capacity_p5"].append(base * 0.05)
                stats["capacity_p10"].append(base * 0.10)

    if not daily:
        raise RuntimeError("没有成功构建任何 cohort")
    series = pd.Series({d: float(np.mean(v)) for d, v in daily.items()}).sort_index()
    series.index = pd.to_datetime(series.index, format="%Y%m%d")
    stats["avg_names"] = (
        float(np.mean(stats["avg_names"])) if stats["avg_names"] else 0.0
    )
    for key in ("capacity_p5", "capacity_p10"):
        vals = stats.pop(key)
        stats[key] = float(np.median(vals)) if vals else None
        stats[f"{key}_p10"] = float(np.percentile(vals, 10)) if vals else None
    return series, stats


def monthly_compound(daily: pd.Series) -> pd.Series:
    """日收益 → 月度复利收益（month 标签 YYYY-MM）。"""
    grouped = (1.0 + daily).groupby(daily.index.strftime("%Y-%m")).prod() - 1.0
    return grouped


def perf_stats(monthly: pd.Series) -> dict:
    if monthly.empty:
        return {}
    nav = (1.0 + monthly).cumprod()
    ret_ann = float(nav.iloc[-1] ** (12.0 / len(monthly)) - 1.0)
    vol = float(monthly.std(ddof=1) * np.sqrt(12.0)) if len(monthly) > 1 else np.nan
    sharpe = float(ret_ann / vol) if vol and np.isfinite(vol) and vol > 0 else np.nan
    dd = float((nav / nav.cummax() - 1.0).min())
    return {
        "months": int(len(monthly)),
        "ann_return": ret_ann,
        "ann_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": dd,
        "win_rate": float((monthly > 0).mean()),
    }


# ---------------------------------------------------------------------------
# 回归报告
# ---------------------------------------------------------------------------
def run_regression(
    monthly: pd.Series, factors: pd.DataFrame, factor_cols: list[str]
) -> dict:
    df = pd.DataFrame({"ret": monthly})
    df.index.name = "month"
    merged = df.join(factors.set_index("month")[[*factor_cols]], how="inner").dropna()
    if len(merged) < len(factor_cols) + 8:
        raise RuntimeError(f"回归样本不足：{len(merged)} 个月")
    y = merged["ret"].to_numpy() - merged["ret"].mean() * 0.0
    X = merged[factor_cols].to_numpy()
    res = hac_ols(y, X)
    beta, tvals = res["beta"], res["t"]
    alpha_m = float(beta[0])
    out = {
        "alpha_monthly": alpha_m,
        "alpha_annual": float((1.0 + alpha_m) ** 12 - 1.0),
        "alpha_t": float(tvals[0]),
        "r2": res["r2"],
        "n": res["n"],
        "hac_lags": res["lags"],
        "loadings": {
            name: {
                "beta": float(beta[i + 1]),
                "t": float(tvals[i + 1]),
                "label": FACTOR_LABELS.get(name, name),
            }
            for i, name in enumerate(factor_cols)
        },
        "_merged": merged,
        "_resid": res["resid"],
        "_beta": beta,
    }
    return out


def _empty_reg() -> dict:
    return {
        "n": 0,
        "alpha_monthly": np.nan,
        "alpha_annual": np.nan,
        "alpha_t": np.nan,
        "r2": np.nan,
        "loadings": {},
        "_merged": pd.DataFrame(),
        "_resid": np.array([]),
        "_beta": np.array([]),
    }


def yearly_alpha(reg: dict, factor_cols: list[str]) -> list[dict]:
    """用全样本载荷（不含截距）做分年残差：年化 α 与正月份占比（不是逐年重估载荷）。"""
    merged = reg["_merged"]
    beta = reg["_beta"]
    pred = merged[factor_cols].to_numpy() @ beta[1:]
    resid = merged["ret"].to_numpy() - pred
    if len(resid) != len(merged):
        return []
    frame = pd.DataFrame({"month": merged.index, "resid": resid})
    frame["year"] = frame["month"].str.slice(0, 4)
    rows = []
    for year, sub in frame.groupby("year"):
        rows.append(
            {
                "year": year,
                "alpha_annual": float(sub["resid"].mean() * 12.0),
                "positive_months": float((sub["resid"] > 0).mean()),
                "months": int(len(sub)),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="模型 Top-N 组合的 Fama-French 五因子归因")
    ap.add_argument("--model-id", help="模型 ID（从 models/users 下解析 pred.parquet）")
    ap.add_argument("--pred-path", help="直接指定 pred.parquet（优先于 --model-id）")
    ap.add_argument(
        "--start", help="信号开始日 YYYY-MM-DD（默认取该模型 valid/test 起点）"
    )
    ap.add_argument("--end", help="信号结束日 YYYY-MM-DD（默认 pred 最后一日）")
    ap.add_argument("--split", default="all", choices=["all", "train", "valid", "test"])
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument(
        "--top-ns",
        default="",
        help="同时对比的 TopN 列表（逗号分隔，如 20,50,100）；报告输出长多对比表",
    )
    ap.add_argument(
        "--bottom-n",
        type=int,
        default=None,
        help="可选：同时构建 BottomN 并做 Top−Bottom 多空归因",
    )
    ap.add_argument("--horizon", type=int, default=5, help="持有交易日数（模型 T+N）")
    ap.add_argument(
        "--cost-bps", type=float, default=15.0, help="单边成本（bp），双边计入"
    )
    ap.add_argument(
        "--include-st", action="store_true", help="不剔除信号日 PIT ST 标的（默认剔除）"
    )
    ap.add_argument(
        "--min-adv",
        type=float,
        default=2.0e7,
        help="信号日 20 日日均成交额下限（元，默认 2000 万；0=关闭）",
    )
    ap.add_argument("--rf-annual", type=float, default=0.0)
    ap.add_argument(
        "--sort-mode",
        choices=["monthly", "academic"],
        default="academic",
        help="因子排序口径：academic=6 月固定一年（默认，FF 原文）；monthly=逐月重排（敏感性对照）",
    )
    ap.add_argument(
        "--orth-inv",
        action="store_true",
        help="Inv 对 OP 截面正交化（缓解 CMA/RMW 共线）",
    )
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--rebuild-factors", action="store_true")
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

    # 默认窗口：valid/test 起点 → pred 最后一日
    head = pd.read_parquet(pred_path, columns=["trade_date", "split"])
    head["trade_date"] = pd.to_datetime(head["trade_date"])
    oos = head[head["split"].isin(["valid", "test"])]["trade_date"]
    args.start = args.start or (
        oos.min().strftime("%Y-%m-%d")
        if not oos.empty
        else head["trade_date"].min().strftime("%Y-%m-%d")
    )
    args.end = args.end or head["trade_date"].max().strftime("%Y-%m-%d")
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else resolve_quantdb_dir().parent
        / "factor_research"
        / "ff_attribution"
        / model_id
    )

    log.info("窗口 %s ~ %s；模型 %s", args.start, args.end, model_id)
    factors, _diag = build_or_load_factors(
        args.start,
        args.end,
        rf_annual=args.rf_annual,
        sort_mode=args.sort_mode,
        orth_inv=args.orth_inv,
        rebuild=args.rebuild_factors,
    )
    log.info(
        "因子就绪：%d 个月（%s ~ %s）",
        len(factors),
        factors["month"].min(),
        factors["month"].max(),
    )

    def evaluate_n(n: int) -> dict:
        picks_n = load_picks(
            pred_path,
            args.start,
            args.end,
            args.split,
            n,
            exclude_st=not args.include_st,
        )
        daily_n, cohort_n = build_cohort_daily_returns(
            picks_n,
            horizon=args.horizon,
            cost_bps=args.cost_bps,
            start=args.start,
            end=args.end,
            min_adv=args.min_adv,
        )
        monthly_n = monthly_compound(daily_n)
        r5 = run_regression(monthly_n, factors, ["MKT", "SMB", "HML", "RMW", "CMA"])
        try:
            r6 = run_regression(
                monthly_n, factors, ["MKT", "SMB", "HML", "RMW", "CMA", "TURN"]
            )
        except RuntimeError:
            r6 = _empty_reg()
        return {
            "n": n,
            "daily": daily_n,
            "monthly": monthly_n,
            "perf": perf_stats(monthly_n),
            "cohort_stats": cohort_n,
            "reg5": r5,
            "reg6": r6,
        }

    ns = sorted(
        {
            int(args.top_n),
            *[int(x) for x in (args.top_ns or "").split(",") if x.strip()],
        }
    )
    multi: list[dict] = []
    primary: dict | None = None
    for n in ns:
        log.info("评估 Top%d …", n)
        res = evaluate_n(n)
        multi.append(
            {
                "n": n,
                "perf": res["perf"],
                "alpha5": res["reg5"]["alpha_annual"],
                "t5": res["reg5"]["alpha_t"],
                "alpha6": res["reg6"]["alpha_annual"],
                "t6": res["reg6"]["alpha_t"],
                "smb": res["reg5"]["loadings"].get("SMB", {}).get("beta"),
                "smb_t": res["reg5"]["loadings"].get("SMB", {}).get("t"),
                "r2": res["reg5"]["r2"],
                "avg_names": res["cohort_stats"].get("avg_names"),
            }
        )
        if n == args.top_n:
            primary = res
    assert primary is not None
    monthly = primary["monthly"]
    stats = primary["perf"]
    cohort_stats = primary["cohort_stats"]
    reg5 = primary["reg5"]
    reg6 = primary["reg6"]
    year_rows = yearly_alpha(reg5, ["MKT", "SMB", "HML", "RMW", "CMA"])

    longshort = None
    if args.bottom_n:
        picks_bottom = load_picks(
            pred_path,
            args.start,
            args.end,
            args.split,
            args.bottom_n,
            bottom=True,
            exclude_st=not args.include_st,
        )
        daily_b, _stats_b = build_cohort_daily_returns(
            picks_bottom,
            horizon=args.horizon,
            cost_bps=args.cost_bps,
            start=args.start,
            end=args.end,
            min_adv=args.min_adv,
        )
        monthly_b = monthly_compound(daily_b)
        spread = (monthly - monthly_b).dropna()
        reg5_ls = run_regression(spread, factors, ["MKT", "SMB", "HML", "RMW", "CMA"])
        try:
            reg6_ls = run_regression(
                spread, factors, ["MKT", "SMB", "HML", "RMW", "CMA", "TURN"]
            )
        except RuntimeError:
            reg6_ls = _empty_reg()
        longshort = {
            "bottom_n": args.bottom_n,
            "stats": perf_stats(spread),
            "reg5": reg5_ls,
            "reg6": reg6_ls,
        }

    ff_diag = factor_diagnostics(factors)
    md_path = write_report(
        out_dir,
        model_id,
        args,
        monthly,
        stats,
        cohort_stats,
        factors,
        reg5,
        reg6,
        year_rows,
        longshort,
        ff_diag,
        multi,
    )
    print(f"报告已生成：{md_path}")
    print(
        f"FF5 α 年化 {reg5['alpha_annual']:+.2%}（t={reg5['alpha_t']:.2f}），FF5+TURN α 年化 {reg6['alpha_annual']:+.2%}（t={reg6['alpha_t']:.2f}）"
    )
    if longshort:
        ls5 = longshort["reg5"]
        print(
            f"Top−Bottom FF5 α 年化 {ls5['alpha_annual']:+.2%}（t={ls5['alpha_t']:.2f}）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
