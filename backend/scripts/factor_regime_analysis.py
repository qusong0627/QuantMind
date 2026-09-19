#!/usr/bin/env python3
"""因子 × 市场状态（牛/熊/震荡）分析 → Markdown + PDF 报告。

回答三个问题：
  1) **这段历史里哪些时段是牛/熊/震荡** —— 用沪深300 的 200 日均线 + 250 日动量分段（规则透明、可复现）
  2) **每个因子在哪种状态里有效** —— 分 regime 统计因子日度 IC 与多空价差
     （直接读快照构建时落盘的 factor_series.parquet，不用重算）
  3) **哪些因子能识别牛熊** —— 把因子的「横截面聚合值」（中位数=整体水平、标准差=分歧度）
     与**未来指数收益**做相关，排出择时能力榜（这一步需要扫一遍因子表）

用法：
  python backend/scripts/factor_regime_analysis.py                       # L1+L2/L1/L2 全跑（默认）
  python backend/scripts/factor_regime_analysis.py --dataset l1_factors   # 只跑一个数据集
  python backend/scripts/factor_regime_analysis.py --timing-horizon 20   # 择时用 20 日前瞻
  python backend/scripts/factor_regime_analysis.py --no-pdf

产出（技能中心「报告档案 → 因子研究」可直接预览）：
  <报告档案根>/因子研究/因子市场状态分析_YYYYMMDD.md / .pdf
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.engine.factor_report.datasets import DATASETS, dataset_dir, label_dir  # noqa: E402
from backend.shared.benchmark import load_benchmark_closes  # noqa: E402

# 基准指数口径单源：000300.SH（沪深300）优先、000001.SH（上证）回退。
# 台账（基准 / 择时 / 市场腿三角色）见 backend/shared/benchmark.py。
META_COLS = {"symbol", "date", "time", "dt", "open", "high", "low", "close",
             "volume", "amount", "release_id", "published_at"}

BULL_MOM, BEAR_MOM = 0.10, -0.10   # 250 日动量阈值
MA_WINDOW = 200


def archive_root() -> Path:
    """报告档案根（与 skills-center「报告档案」同源；详见 factor_dedup_report.archive_root 注释）。"""
    try:
        from backend.services.engine.routers.trading_agents import _resolve_results_dir

        return _resolve_results_dir()
    except Exception:  # noqa: BLE001
        for cand in (Path("/data/reports/trading_agents"), Path("/app/db/trading_agents_results")):
            if cand.is_dir():
                return cand
        return Path("/data/reports/trading_agents")


def load_index() -> tuple[str, pd.Series]:
    """读基准指数日线（收盘价，按日期升序）。口径与取数均委托共享单源。"""
    try:
        return load_benchmark_closes()
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def label_regimes(close: pd.Series) -> pd.DataFrame:
    """按 MA200 + 250 日动量给每个交易日打 regime 标签。"""
    ma = close.rolling(MA_WINDOW).mean()
    mom = close / close.shift(250) - 1.0

    def _tag(row) -> str:
        if not np.isfinite(row["mom"]):
            return "warming"
        if row["close"] > row["ma"] and row["mom"] > BULL_MOM:
            return "bull"
        if row["close"] < row["ma"] and row["mom"] < BEAR_MOM:
            return "bear"
        return "range"

    df = pd.DataFrame({"close": close, "ma": ma, "mom": mom})
    df["regime"] = df.apply(_tag, axis=1)
    df["fwd5"] = close.shift(-5) / close - 1
    df["fwd20"] = close.shift(-20) / close - 1
    return df


def regime_episodes(labeled: pd.DataFrame) -> list[dict]:
    """把逐日标签合并成连续区间（用于报告里"哪段是牛/熊"）。"""
    out: list[dict] = []
    cur = None
    for dt, row in labeled.iterrows():
        tag = row["regime"]
        if tag == "warming":
            continue
        if cur is None or cur["regime"] != tag:
            if cur is not None:
                out.append(cur)
            cur = {"regime": tag, "start": dt, "end": dt, "n": 1, "start_close": row["close"], "end_close": row["close"]}
        else:
            cur["end"] = dt
            cur["n"] += 1
            cur["end_close"] = row["close"]
    if cur is not None:
        out.append(cur)
    for e in out:
        e["ret"] = e["end_close"] / e["start_close"] - 1.0
    return [e for e in out if e["n"] >= 5]


# ─────────────────── 分 regime 的因子表现（读序列快照）───────────────────

def factor_stats_by_regime(dataset: str, labels: pd.Series) -> pd.DataFrame:
    """从 factor_series.parquet 取每日 IC / 分位，按 regime 分组统计。"""
    path = dataset_dir(dataset) / "report" / "factor_series.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pq.read_table(path, columns=["factor", "date", "ic", "q1", "q10"]).to_pandas()
    df["dt"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    df["regime"] = df["dt"].map(labels).fillna("warming")
    df = df[df["regime"].isin(["bull", "bear", "range"])]
    df["ls"] = df["q10"] - df["q1"]

    g = df.groupby(["factor", "regime"])
    agg = g.agg(ic=("ic", "mean"), ls=("ls", "mean"), days=("ic", "size")).reset_index()
    pivot = agg.pivot(index="factor", columns="regime")
    out = pd.DataFrame(index=pivot.index)
    for regime in ("bull", "bear", "range"):
        for metric in ("ic", "ls", "days"):
            col = (metric, regime)
            out[f"{metric}_{regime}"] = pivot[col] if col in pivot.columns else np.nan
    return out.reset_index().rename(columns={"factor": "name"})


def factor_stats_by_year(dataset: str) -> pd.DataFrame:
    """逐年 IC / 多空（"循环计算"视角：因子在哪一年有效、哪一年失效或反转）。"""
    path = dataset_dir(dataset) / "report" / "factor_series.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pq.read_table(path, columns=["factor", "date", "ic", "q1", "q10"]).to_pandas()
    df["year"] = (df["date"] // 10000).astype(int)
    df["ls"] = df["q10"] - df["q1"]
    agg = df.groupby(["factor", "year"]).agg(ic=("ic", "mean"), ls=("ls", "mean"), days=("ic", "size")).reset_index()
    piv = agg.pivot(index="factor", columns="year", values="ic")
    piv.columns = [f"ic_{c}" for c in piv.columns]
    return piv.reset_index().rename(columns={"factor": "name"})


# ─────────────────── 牛熊识别因子（横截面聚合 vs 未来指数收益）───────────────────

def timing_scan(dataset: str, labels: pd.DataFrame, horizon: int, max_dates: int | None = None) -> pd.DataFrame:
    """扫因子表：每日算各因子的横截面中位数与标准差 → 与未来指数收益相关。

    中位数 = 该因子的「市场整体水平」；标准差 = 「分歧度/拥挤度」。
    两者都可能携带市场状态信息（例如波动率因子的整体水平随风险偏好起落）。
    """
    root = dataset_dir(dataset)
    parts = sorted(p for p in root.glob("dt=*") if p.is_dir())
    if not parts:
        return pd.DataFrame()
    if max_dates:
        parts = parts[-max_dates:]

    fwd_col = f"fwd{horizon}"
    if fwd_col not in labels.columns:
        raise SystemExit(f"指数数据不足以计算 {horizon} 日前瞻收益")

    # 因子列名（取最后一个分区的 schema，按 dtype 过滤）
    sch = pq.ParquetFile(f"{parts[-1]}/data.parquet").schema_arrow
    cols = [c for c in sch.names
            if c not in META_COLS and str(sch.field(c).type).startswith(("float", "double", "int", "decimal"))]

    med_rows: list[np.ndarray] = []
    std_rows: list[np.ndarray] = []
    row_dates: list[str] = []
    for p in parts:
        dt = p.name.split("=")[1]
        try:
            t = pq.read_table(f"{p}/data.parquet", columns=cols).to_pandas()
        except Exception:  # noqa: BLE001 — 单个分区读失败不影响整体
            continue
        arr = t.to_numpy(dtype=np.float32)
        with np.errstate(invalid="ignore"):
            med_rows.append(np.nanmedian(arr, axis=0))
            std_rows.append(np.nanstd(arr, axis=0))
        row_dates.append(dt)
    if not row_dates:
        return pd.DataFrame()

    med = pd.DataFrame(np.vstack(med_rows), index=pd.to_datetime(row_dates, format="%Y%m%d"), columns=cols)
    std = pd.DataFrame(np.vstack(std_rows), index=med.index, columns=cols)
    fwd = labels[fwd_col].reindex(med.index)

    ok = fwd.notna()
    if ok.sum() < 100:
        return pd.DataFrame()

    def _corr(frame: pd.DataFrame, method: str = "spearman") -> pd.Series:
        return frame[ok].corrwith(fwd[ok], method=method)

    out = pd.DataFrame({
        "ic_level": _corr(med),      # 因子整体水平 vs 未来指数收益
        "ic_disp": _corr(std),       # 因子分歧度 vs 未来指数收益
    })
    out.index.name = "name"
    return out.reset_index()


# ─────────────────── 报告渲染 ───────────────────

REGIME_LABEL = {"bull": "牛市", "bear": "熊市", "range": "震荡"}


def _pct(v, digits: int = 2) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:+.{digits}f}%"


def _num(v, digits: int = 3) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v:+.{digits}f}"


def build_markdown(datasets: list[str], bench: str, labeled: pd.DataFrame, episodes: list[dict],
                   per_ds_stats: dict[str, pd.DataFrame], per_ds_year: dict[str, pd.DataFrame],
                   timing: dict[str, pd.DataFrame], horizon: int) -> str:
    now = datetime.now()
    lines = [
        "# 因子 × 市场状态（牛/熊/震荡）分析报告",
        "",
        f"> 基准指数：{bench}　|　分段规则：收盘 > MA200 且 250 日动量 > +10% 为牛市；"
        f"收盘 < MA200 且动量 < −10% 为熊市；其余震荡　|　生成时间：{now.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 一、这段历史里的牛熊分段",
        "",
        "| 状态 | 区间 | 交易日 | 指数涨跌 |",
        "|---|---|---|---|",
    ]
    for e in episodes[-14:]:
        lines.append(
            f"| {REGIME_LABEL[e['regime']]} | {e['start'].date()} ~ {e['end'].date()} | {e['n']} | {_pct(e['ret'])} |"
        )
    counts = labeled["regime"].value_counts()
    total = int(counts.get("bull", 0) + counts.get("bear", 0) + counts.get("range", 0))
    if total:
        lines += [
            "",
            f"样本构成：牛市 {counts.get('bull', 0)} 天（{counts.get('bull', 0) / total:.0%}）、"
            f"熊市 {counts.get('bear', 0)} 天（{counts.get('bear', 0) / total:.0%}）、"
            f"震荡 {counts.get('range', 0)} 天（{counts.get('range', 0) / total:.0%}）。",
        ]

    for ds in datasets:
        stats = per_ds_stats.get(ds)
        if stats is None or stats.empty:
            continue
        label = str(DATASETS.get(ds, {}).get("label") or ds)
        lines += ["", f"## 二、{label}：因子在三种状态下的表现", ""]
        s = stats.copy()
        s["ic_bull"] = s.get("ic_bull")
        s["ic_bear"] = s.get("ic_bear")
        s["ic_range"] = s.get("ic_range")

        bull = s.sort_values("ic_bull", key=lambda x: x.abs(), ascending=False).head(8)
        lines.append("**牛市里最强（按 |IC|）**：")
        lines += ["", "| 因子 | 牛市 IC | 熊市 IC | 震荡 IC | 牛市多空 |", "|---|---|---|---|---|"]
        for _, r in bull.iterrows():
            lines.append(f"| `{r['name']}` | {_num(r.get('ic_bull'))} | {_num(r.get('ic_bear'))} | {_num(r.get('ic_range'))} | {_pct(r.get('ls_bull'))} |")

        bear = s.sort_values("ic_bear", key=lambda x: x.abs(), ascending=False).head(8)
        lines += ["", "**熊市里最强（抗跌/防御，按 |IC|）**：", "", "| 因子 | 熊市 IC | 牛市 IC | 震荡 IC | 熊市多空 |", "|---|---|---|---|---|"]
        for _, r in bear.iterrows():
            lines.append(f"| `{r['name']}` | {_num(r.get('ic_bear'))} | {_num(r.get('ic_bull'))} | {_num(r.get('ic_range'))} | {_pct(r.get('ls_bear'))} |")

        s["flip"] = s["ic_bear"].abs() - s["ic_bull"].abs()
        flip = s.dropna(subset=["flip"]).sort_values("flip", ascending=False).head(6)
        if not flip.empty:
            lines += ["", "**牛熊差异最大（同样的因子，换个状态就失效或反向）**：", "",
                      "| 因子 | 牛市 IC | 熊市 IC | 差异(\\|熊\\|−\\|牛\\|) |", "|---|---|---|---|"]
            for _, r in flip.iterrows():
                lines.append(f"| `{r['name']}` | {_num(r.get('ic_bull'))} | {_num(r.get('ic_bear'))} | {_num(r.get('flip'))} |")

        years = per_ds_year.get(ds)
        if years is not None and not years.empty:
            ycols = sorted([c for c in years.columns if c.startswith("ic_")])
            top = s.sort_values("ic_bull", key=lambda x: x.abs(), ascending=False).head(10)
            names = [n for n in top["name"] if n in set(years["name"])]
            ysub = years[years["name"].isin(names)].set_index("name").reindex(names)
            lines += ["", "**逐年 IC（看因子生命周期：" + "、".join(c.replace('ic_', '') for c in ycols) + "）**：", "",
                      "| 因子 | " + " | ".join(c.replace("ic_", "") for c in ycols) + " |",
                      "|---" * (len(ycols) + 1) + "|"]
            for name, row in ysub.iterrows():
                lines.append("| `" + str(name) + "` | " + " | ".join(_num(row.get(c), 3) for c in ycols) + " |")

        t = timing.get(ds)
        if t is not None and not t.empty:
            t = t.copy()
            t["best"] = t[["ic_level", "ic_disp"]].abs().max(axis=1)
            top = t.sort_values("best", ascending=False).head(12)
            lines += ["", f"**能识别牛熊的因子（横截面聚合 vs 未来 {horizon} 日指数收益，Top12）**：", "",
                      "| 因子 | 整体水平相关 | 分歧度相关 | 读法 |", "|---|---|---|---|"]
            for _, r in top.iterrows():
                use_disp = abs(r["ic_disp"]) > abs(r["ic_level"])
                if use_disp:
                    reading = "分歧度越高、后市越" + ("强" if r["ic_disp"] > 0 else "弱")
                else:
                    reading = "整体水平越高、后市越" + ("强" if r["ic_level"] > 0 else "弱")
                lines.append(f"| `{r['name']}` | {_num(r['ic_level'])} | {_num(r['ic_disp'])} | {reading} |")

    lines += [
        "", "## 三、怎么用（结论）", "",
        "1. **分状态用因子**：把「牛市/熊市差异最大」那批因子当条件因子——只在对应状态里启用，"
        "否则它们的 IC 会被另一种状态反向稀释（上面每个数据集都列了这批）。",
        "2. **择时/仓位开关**：择时榜里相关性强且方向稳定的因子，可以用它的横截面聚合值当市场状态的"
        "辅助信号（例如分歧度骤升 = 风险偏好恶化），与价格类信号（MA、动量）互补验证。",
        "3. **别在错误的周期评估**：价值类（B/P、E/P）与基本面因子在短周期本就弱，"
        "分状态看也要配合长前瞻期（T+20 起）。",
        "4. **口径提醒**：本报告的分状态 IC 来自快照窗口内每日 IC 的重新分组，"
        "窗口越短、单状态样本越少，结论越要谨慎（报告已给出各状态天数）。",
        "",
        "## 四、方法说明", "",
        f"- 状态标签逐日计算：`close > MA{MA_WINDOW} 且 250 日动量 > {BULL_MOM:+.0%}` = 牛市；"
        f"`close < MA{MA_WINDOW} 且动量 < {BEAR_MOM:+.0%}` = 熊市；其余震荡（预热期不参与）。",
        "- 分状态因子表现：取 `factor_series.parquet` 的日度 IC 与十分位收益，按状态分组求均值。",
        f"- 择时扫描：逐日算每个因子的横截面**中位数**（整体水平）与**标准差**（分歧度），"
        f"与未来 {horizon} 日指数收益做 Spearman 相关。",
        "- 数据来源：QuantDB 因子表 + `index_daily`（基准指数）；因子口径见 `docs/因子报告_设计方案.md`。",
        "",
        "---",
        "",
        "> 本报告由 QuantMind 自动生成，仅用于内部因子研究，不构成任何投资建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="因子 × 市场状态（牛熊）分析")
    ap.add_argument("--dataset", default="all", help="all 或单个数据集")
    ap.add_argument("--timing-horizon", type=int, default=5, choices=[1, 2, 5, 10, 20], help="择时扫描的前瞻期")
    ap.add_argument("--max-dates", type=int, default=None, help="择时扫描最多用多少个交易日（调试用）")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    datasets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    datasets = [d for d in datasets if d in DATASETS] or list(DATASETS)

    bench, close = load_index()
    labeled = label_regimes(close)
    labels = labeled["regime"]
    episodes = regime_episodes(labeled)
    print(f"[index] {bench}：{len(close)} 天，{close.index[0].date()} ~ {close.index[-1].date()}，"
          f"牛 {int((labels=='bull').sum())} / 熊 {int((labels=='bear').sum())} / 震荡 {int((labels=='range').sum())} 天")

    per_ds_stats: dict[str, pd.DataFrame] = {}
    per_ds_year: dict[str, pd.DataFrame] = {}
    timing: dict[str, pd.DataFrame] = {}
    for ds in datasets:
        stats = factor_stats_by_regime(ds, labels)
        per_ds_stats[ds] = stats
        print(f"[regime] {ds}: {len(stats)} 个因子完成分状态统计")
        per_ds_year[ds] = factor_stats_by_year(ds)
        print(f"[year] {ds}: 逐年 IC 完成（{per_ds_year[ds].shape[1] - 1} 年）")
        t = timing_scan(ds, labeled, args.timing_horizon, args.max_dates)
        timing[ds] = t
        print(f"[timing] {ds}: {len(t)} 个因子完成择时扫描")

    md = build_markdown(datasets, bench, labeled, episodes, per_ds_stats, per_ds_year, timing, args.timing_horizon)

    out_dir = Path(args.out_dir) if args.out_dir else (archive_root() / "因子研究")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    md_path = out_dir / f"因子市场状态分析_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"[ok] Markdown: {md_path}")

    if not args.no_pdf:
        try:
            from backend.scripts.md_to_pdf_report import main as md_to_pdf

            pdf_path = out_dir / f"因子市场状态分析_{stamp}.pdf"
            md_to_pdf(str(md_path), str(pdf_path))
            print(f"[ok] PDF: {pdf_path}（{pdf_path.stat().st_size / 1024:.0f} KB）")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] PDF 生成失败（Markdown 已产出）：{e}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
