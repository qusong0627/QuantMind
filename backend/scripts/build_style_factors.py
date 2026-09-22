"""构建自算 CNE5 式风格产物：暴露分区 + 纯因子收益 + meta。

产物落在 ``5_technical_derived/style_factors/``（经 ``quantdb_paths`` 解析）：:

    style_factors/
      exposures/dt=YYYYMMDD/data.parquet    # symbol + 12 个风格暴露（float32，已标准化）
      returns.parquet                       # dt,horizon,n_used,n_universe + 12 个纯因子收益
      meta.json                             # 覆盖率、口径假设、残差 size 相关、单位实测记录

``build_factor_report.py`` 的 ``--style-dir`` 指向 ``style_factors/exposures``（默认自动探测
到 ``style_factors`` 时会往下找一层；两边约定见各自的 docstring）。

## 口径

十二个风格的定义、标准化流水线、NaN 纪律**全在** ``factor_report/style_model.py``，
本脚本只负责取数与落盘。这样单测可以对着纯函数写已知答案，不必搭数据环境。

**爬坡段（一）**：面板从 ``--start`` 起算，其前 252 行没有完整回看窗口 → 四个窗口型风格
（beta/momentum/residvol/liquidity）在该段一律 NaN（口径见 ``style_model.WINDOW_DESCRIPTORS``）。
故报告窗口要从 ``--start`` 往后至少 252 个交易日 —— 建 2016 起、报 2018 起即满足。

**爬坡段（二）**：rmw/cma 的分子是四季 TTM，而 QuantDB 财务表从 2016Q1 起收录
（逐票还更晚）→ 完整四季要等到 2016 年报公告（约 2017-04）才出现。故纯因子收益的
有效起点从「建 2016」时的 2017-01 顺延到 2017Q2；报告窗口取 2018+ 完全不受影响。
WLS 的「存在缺失即整只剔除」会把这段的 n_used 压到 0（逐日 n_used/n_universe 已落盘备查），
这是**有意**的诚实行为：不拿规则外的插值去凑一整段风格收益。

## 单位（2026-09-19 实测，勿凭印象改）

``valuation``：``total_mv / net_profit_ttm / revenue_ttm`` = 元，``circulating_capital`` = 股；
``daily_forward``：``volume`` = 股；``balance``：金额 = 元。
四个比值型风格因此**量纲自洽**，脚本里没有任何换算常数（``grep 1e4`` 应为空）。

## 用法

    python3 backend/scripts/build_style_factors.py --start 2016-01-01
    python3 backend/scripts/build_style_factors.py --limit-days 30   # 冒烟

## 成本

逐日读 valuation + daily_forward 两个分区（~2600 天 × 2 次读）；纯因子收益按 horizon
各解一次 WLS（4000×138 的 lstsq，秒级）。峰值内存 ~5GB（10 个 (T,N) 面板 × float64）。
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.services.engine.factor_report import style_model as SM  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_subdir  # noqa: E402

log = logging.getLogger("build_style_factors")

EXPOSURE_COLS = ("symbol", *SM.STYLE_NAMES)
DEFAULT_HORIZONS = (1, 5, 10, 20)
VALUATION_COLS = ("total_mv", "float_mv", "pb", "net_profit_ttm", "revenue_ttm", "circulating_capital")
KLINE_COLS = ("close", "volume")


def list_dates(val_dir: Path, start: str, end: str) -> list[str]:
    """交易日列表 = ``valuation`` 的 hive 分区名（该数据集逐日全市场落盘）。"""
    out = []
    for p in sorted(glob.glob(str(val_dir / "dt=*"))):
        dt = os.path.basename(p).split("=", 1)[1]
        if len(dt) == 8 and dt.isdigit() and start <= dt <= end:
            out.append(dt)
    return out


def load_universe() -> np.ndarray:
    """证券主表（``instrument_detail``）的 Symbol 全集 —— 含退市股，故历史日不会被截断。"""
    import pyarrow.parquet as pq

    files = glob.glob(str(resolve_quantdb_subdir("2_base_sector", "instrument_detail") / "*.parquet"))
    if not files:
        raise FileNotFoundError("instrument_detail 缺失，无法确定股票池")
    df = pq.read_table(files, columns=["Symbol"]).to_pandas()
    return np.array(sorted(df["Symbol"].dropna().astype(str).unique()))


def load_panels(dates: list[str], symbols: np.ndarray) -> dict[str, np.ndarray]:
    """逐日读 valuation + kline 分区，铺成 (T, N) 面板；缺失一律 NaN。

    按主表对齐（``reindex``）而非按当日出现的顺序：顺序错位会静默把 A 股票的收益
    配到 B 股票的风格上，是这一层最容易犯且最难发现的错。
    """
    import pyarrow.parquet as pq

    val_dir = resolve_quantdb_subdir("5_technical_derived", "valuation")
    kline_dir = resolve_quantdb_subdir("1_kline_data", "daily_forward")
    idx = pd.Index(symbols)
    T, N = len(dates), len(symbols)
    panels = {c: np.full((T, N), np.nan) for c in (*VALUATION_COLS, *KLINE_COLS)}
    missing = {"valuation": 0, "kline": 0}
    t0 = time.time()
    for i, dt in enumerate(dates):
        for src, cols, d in (("valuation", VALUATION_COLS, val_dir), ("kline", KLINE_COLS, kline_dir)):
            path = d / f"dt={dt}" / "data.parquet"
            if not path.exists():
                missing[src] += 1
                continue
            df = pq.read_table(path, columns=["symbol", *cols]).to_pandas()
            df = df.drop_duplicates("symbol").set_index("symbol").reindex(idx)
            for c in cols:
                panels[c][i] = df[c].to_numpy(dtype=np.float64)
        if (i + 1) % 250 == 0:
            log.info("面板读取 %d/%d（%.0fs）", i + 1, T, time.time() - t0)
    if missing["valuation"] or missing["kline"]:
        log.warning("分区缺失：valuation %d 天、kline %d 天（对应格子为 NaN）", missing["valuation"], missing["kline"])
    return panels


def pit_align(ann_sorted: np.ndarray, val_sorted: np.ndarray, date_int: np.ndarray) -> np.ndarray:
    """按公告日的 PIT 对齐：每个 dt 取「公告日 ≤ dt 的最近一期」的值（无 → NaN）。

    ``ann_sorted`` 必须已按公告日升序 —— 同日内多期取报告期最晚的那条
    （``side="right"`` 落在同值末尾），重述公告因此稳定地覆盖原值。
    """
    pos = np.searchsorted(ann_sorted, date_int, side="right") - 1
    out = np.full(date_int.shape, np.nan)
    valid = pos >= 0
    out[valid] = val_sorted[pos[valid]]
    return out


def ttm_from_reports(timetags: np.ndarray, values: np.ndarray, *, cumulative: bool) -> np.ndarray:
    """按报告期序列算「每个报告日的 TTM」，不足四季 / 季度不连续 / 值缺失 → NaN。

    ``cumulative=False``（income 实测为**单季**值）：直接滚动四季求和。
    ``cumulative=True``（cashflow 实测为**年初至今累计**）：先差分还原单季
    （Q1 即累计值的年初重置点，原样取用），再滚动四季求和。

    语义实测（2026-09-22，勿凭印象改）：income.revenue 逐季不单调（单季），
    cashflow.cash_pay_acq_const_fiolta 年内单调上升、跨年重置（累计），
    且 600036 的 income 扣非四季和 ≈ valuation.net_profit_ttm（比值 0.8~1.1）。
    """
    tt = np.asarray(timetags, dtype=np.int64).ravel()
    v = np.asarray(values, dtype=np.float64).ravel()
    n = tt.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    qidx = (tt // 100 % 100 - 1) // 3                 # 0..3（报告月份 → 季度序号）
    ordinal = tt // 10000 * 4 + qidx                  # 相邻季度相差 1
    adjacent = np.zeros(n, dtype=bool)
    adjacent[1:] = (ordinal[1:] - ordinal[:-1]) == 1
    if cumulative:
        # Q1 是累计值的年初重置点（当日披露值即单季，**优先于差分**）；非相邻上一期无法差分 → NaN
        prev_v = np.concatenate([[np.nan], v[:-1]])
        q = np.where(qidx == 0, v, np.where(adjacent, v - prev_v, np.nan))
    else:
        q = v.copy()
    # 连续四季（且值齐全）才出 TTM：中间缺一期就整段作废，不用「有多少算多少」
    run = np.zeros(n, dtype=np.int64)
    for i in range(n):
        if not np.isfinite(q[i]):
            continue
        run[i] = run[i - 1] + 1 if (i > 0 and adjacent[i] and run[i - 1] > 0) else 1
        if run[i] >= 4:
            out[i] = q[i] + q[i - 1] + q[i - 2] + q[i - 3]
    return out


BALANCE_COLS = ("total_equity", "tot_assets")
FLOW_SOURCES = {
    # 面板键 → (子目录, 列名, 是否年初至今累计)
    "deducted_net_profit_ttm": ("income", "deducted_net_profit", False),
    "capex_ttm": ("cashflow", "cash_pay_acq_const_fiolta", True),
}


def load_balance_pit(dates: list[str], symbols: np.ndarray) -> dict[str, np.ndarray]:
    """资产负债表面板（PIT）：长期负债 + 净资产 + 总资产，按 ``m_anntime ≤ dt`` 取最近一期。

    用公告日而非报告期：用报告期会让 3 月就看到年报数据（前视偏差），
    这是财务数据接入最经典的一类错。三列共用一次读盘（单票只读一遍 balance）。

    Returns:
        ``{"long_term_debt": (T,N), "book_equity": (T,N), "total_assets": (T,N)}``
    """
    bal_dir = resolve_quantdb_subdir("3_financial_data", "balance")
    date_int = np.array([int(d) for d in dates], dtype=np.int64)
    cols = ("long_term_loans", "bonds_payable", *BALANCE_COLS)
    out = {k: np.full((len(dates), len(symbols)), np.nan) for k in ("long_term_debt", "book_equity", "total_assets")}
    t0 = time.time()
    hit = 0
    for j, sym in enumerate(symbols):
        path = bal_dir / f"{sym}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["m_anntime", *cols])
        except Exception as e:  # noqa: BLE001 — 单票损坏不该中断全量构建
            log.debug("balance 读取失败 %s: %s", sym, e)
            continue
        ann = pd.to_numeric(df["m_anntime"], errors="coerce")
        ok = ann.notna().to_numpy()
        if not ok.any():
            continue
        order = np.argsort(ann.to_numpy()[ok])
        ann_s = ann.to_numpy()[ok][order]
        ld = pd.to_numeric(df["long_term_loans"], errors="coerce").fillna(0.0) + pd.to_numeric(
            df["bonds_payable"], errors="coerce"
        ).fillna(0.0)
        series = {
            "long_term_debt": ld,
            "book_equity": pd.to_numeric(df["total_equity"], errors="coerce"),
            "total_assets": pd.to_numeric(df["tot_assets"], errors="coerce"),
        }
        for key, s in series.items():
            out[key][:, j] = pit_align(ann_s, s.to_numpy()[ok][order], date_int)
        hit += 1
        if (j + 1) % 1000 == 0:
            log.info("资产负债表面板 %d/%d（%.0fs）", j + 1, len(symbols), time.time() - t0)
    log.info("资产负债表面板完成：%d/%d 只取到（%.0fs）", hit, len(symbols), time.time() - t0)
    return out


def load_flow_pit(dates: list[str], symbols: np.ndarray, panel_key: str) -> np.ndarray:
    """利润/现金流面板（PIT）：把单票的季度序列转成 TTM，再按公告日对齐到交易日。

    语义与差分规则见 :func:`ttm_from_reports`（income 单季、cashflow 累计）。
    """
    subdir, col, cumulative = FLOW_SOURCES[panel_key]
    src_dir = resolve_quantdb_subdir("3_financial_data", subdir)
    date_int = np.array([int(d) for d in dates], dtype=np.int64)
    out = np.full((len(dates), len(symbols)), np.nan)
    t0 = time.time()
    hit = 0
    for j, sym in enumerate(symbols):
        path = src_dir / f"{sym}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["m_anntime", "m_timetag", col])
        except Exception as e:  # noqa: BLE001 — 单票损坏不该中断全量构建
            log.debug("%s 读取失败 %s: %s", subdir, sym, e)
            continue
        ann = pd.to_numeric(df["m_anntime"], errors="coerce")
        tt = pd.to_numeric(df["m_timetag"], errors="coerce")
        val = pd.to_numeric(df[col], errors="coerce")
        ok = (ann.notna() & tt.notna()).to_numpy()
        if not ok.any():
            continue
        # TTM 按报告期顺序算 → 再按公告日排序做 PIT（重述公告 = 更晚的公告日，自然覆盖）
        tt_a, val_a = tt.to_numpy()[ok], val.to_numpy()[ok]
        by_period = np.argsort(tt_a, kind="stable")
        ttm = ttm_from_reports(tt_a[by_period], val_a[by_period], cumulative=cumulative)
        ann_a = ann.to_numpy()[ok]
        by_ann = np.argsort(ann_a, kind="stable")
        out[:, j] = pit_align(ann_a[by_ann], ttm[by_period][by_ann], date_int)
        hit += 1
        if (j + 1) % 1000 == 0:
            log.info("%s TTM 面板 %d/%d（%.0fs）", subdir, j + 1, len(symbols), time.time() - t0)
    log.info("%s TTM 面板完成：%d/%d 只取到（%.0fs）", subdir, hit, len(symbols), time.time() - t0)
    return out


def load_bench_returns(dates: list[str]) -> np.ndarray:
    """沪深300 日收益，按交易日列表对齐（缺失交易日 → NaN，不前后填充）。

    ⚠️ 取不到基准**不中止构建**：beta 整列 NaN 并由覆盖率日志点名，其余 9 个风格照常。
    基准链有「>500 行才算可用」的守卫（``shared/benchmark.py``），短区间冒烟时必然触发 ——
    这不是错误，是该守卫按设计工作。
    """
    from backend.shared.benchmark import load_benchmark_closes

    try:
        sym, close = load_benchmark_closes(start=dates[0], end=dates[-1])
    except (ValueError, FileNotFoundError) as e:
        log.warning("基准指数不可用（%s）→ beta 整列 NaN", e)
        return np.full(len(dates), np.nan)
    if len(close) == 0:
        log.warning("基准指数取数为空，beta 将整列 NaN")
        return np.full(len(dates), np.nan)
    idx = pd.to_datetime([f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates])
    s = close.copy()
    s.index = pd.to_datetime(s.index)
    aligned = s.reindex(idx)
    ret = np.log(aligned / aligned.shift(1)).to_numpy()
    log.info("基准 %s：%d 天对齐，%d 天有收益", sym, len(dates), int(np.isfinite(ret).sum()))
    return ret


def industry_codes(symbols: np.ndarray) -> np.ndarray:
    """行业代码（按股票，静态快照）。取不到的记 ``"nan"`` —— 它们自成一组去均值。"""
    from backend.services.engine.factor_report.neutralize import load_industry_map

    m = load_industry_map()
    if len(m) == 0:
        log.warning("行业映射缺失：行业去均值这一步将退化为全市场去均值")
        return np.array(["nan"] * len(symbols), dtype=object)
    codes = m.reindex(pd.Index(symbols)).fillna("nan").astype(str).to_numpy(dtype=object)
    log.info("行业映射：%d/%d 只命中，%d 个行业", int((codes != "nan").sum()), len(symbols), len(set(codes)))
    return codes


def write_exposures(out_dir: Path, dates: list[str], symbols: np.ndarray, exposures: dict) -> dict:
    """逐日落盘（原子写）：临时文件 + ``os.replace``，中途失败不留半截产物。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    base = out_dir / "exposures"
    base.mkdir(parents=True, exist_ok=True)
    rows_written, rows_all_nan, per_style_days = 0, 0, dict.fromkeys(SM.STYLE_NAMES, 0)
    for i, dt in enumerate(dates):
        cols = {k: exposures[k][i].astype(np.float32) for k in SM.STYLE_NAMES}
        df = pd.DataFrame({"symbol": symbols, **cols})
        for k in SM.STYLE_NAMES:
            per_style_days[k] += int(np.isfinite(cols[k]).sum() > 0)
        keep = np.isfinite(np.column_stack([cols[k] for k in SM.STYLE_NAMES])).any(axis=1)
        rows_all_nan += int((~keep).sum())
        df = df.loc[keep]
        rows_written += len(df)
        part = base / f"dt={dt}"
        part.mkdir(parents=True, exist_ok=True)
        tmp = part / "data.parquet.tmp"
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp)
        os.replace(tmp, part / "data.parquet")
    return {"rows": rows_written, "rows_all_nan": rows_all_nan, "style_days": per_style_days}


def write_returns(out_dir: Path, dates: list[str], horizons, panels, exposures, codes) -> dict:
    """按 horizon 各解一次 WLS，长表落盘（dt, horizon, n_used, n_universe + 10 风格）。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    close = panels["close"]
    # 权重 √float_mv：市值缺失/为 0（实测有精确 0 的格子）→ 权重 0，被 WLS 剔除
    weight = np.sqrt(np.clip(panels["float_mv"], 0.0, None))
    frames, stats = [], {}
    for h in horizons:
        if h < 1:
            continue
        fwd = np.full_like(close, np.nan)
        fwd[:-h] = close[h:] / close[:-h] - 1.0
        fwd[~np.isfinite(fwd)] = np.nan
        pure, diag = SM.pure_factor_returns(exposures, codes, fwd, weight)
        frames.append(pd.DataFrame({"dt": dates, "horizon": h, **{
            "n_used": diag["n_used"], "n_universe": diag["n_universe"],
            **{k: pure[:, j] for j, k in enumerate(diag["styles"])},
        }}))
        stats[h] = {
            "days": int(np.isfinite(pure).any(axis=1).sum()),
            "n_used_median": int(np.median(diag["n_used"])),
        }
        log.info("纯因子收益 h=%d：%d 天出数，中位使用 %d 只", h, stats[h]["days"], stats[h]["n_used_median"])
    df = pd.concat(frames, ignore_index=True)
    tmp = out_dir / "returns.parquet.tmp"
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp)
    os.replace(tmp, out_dir / "returns.parquet")
    return stats


def build_meta(dates, symbols, exposures, coverage, codes, elapsed) -> dict:
    """口径与覆盖率的自述文件 —— 报告页的「自算 CNE5 式」提示与降级说明都读它。"""
    years = np.array([d[:4] for d in dates])
    by_style = {}
    for k in SM.STYLE_NAMES:
        v = np.asarray(exposures[k])
        cnt = np.isfinite(v).sum(axis=1)
        by_year = {y: int(np.isfinite(v[years == y]).any(axis=1).sum()) for y in sorted(set(years))}
        by_style[k] = {
            "days": int((cnt > 0).sum()),
            "median_names": int(np.median(cnt)),
            "by_year": by_year,
        }
    return {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(elapsed, 1),
        "range": [dates[0], dates[-1]] if dates else [],
        "n_days": len(dates),
        "n_symbols": len(symbols),
        "styles": list(exposures),
        "size_orthogonal_styles": list(SM.SIZE_ORTHOGONAL_STYLES),
        "size_corr_residual": SM.size_residual_corr(exposures),
        "coverage": by_style,
        "industry_missing": int((codes == "nan").sum()),
        "assumptions": {
            "ramp_mask_rows": SM.BETA_WINDOW_DAYS,
            "ramp_mask_styles": list(SM.WINDOW_DESCRIPTORS),
            "beta_window_days": SM.BETA_WINDOW_DAYS,
            "beta_halflife_days": SM.BETA_HALFLIFE_DAYS,
            "momentum_skip_days": SM.MOMENTUM_SKIP_DAYS,
            "min_window_fraction": SM.MIN_WINDOW_FRACTION,
            "residvol_weights": dict(SM.RESIDVOL_WEIGHTS),
            "liquidity_windows": {str(w): f for w, f in SM.LIQUIDITY_WINDOWS},
            "rmw": "扣非净利TTM / 净资产（balance.total_equity，PIT 最近一期公告）",
            "cma": "−资本开支TTM / 总资产（负号：高暴露 = 投资保守），非 FF5 原版的增速口径",
            "flow_ttm": {
                "income.deducted_net_profit": "单季值直接滚动四季求和（实测非累计）",
                "cashflow.cash_pay_acq_const_fiolta": "年初至今累计，先差分还原单季再滚动四季",
                "rule": "四季必须连续且值齐全，否则该报告期 TTM 为 NaN",
            },
        },
        "units_verified": "valuation:元/股; daily_forward:volume=股; balance:元（2026-09-19 实测）",
        "caveat": "自算 CNE5 式口径，与商业 Barra 数据不可比（Beta 无贝叶斯收缩、Growth 用 1 年 TTM 同比）",
        "exposures": coverage,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="构建自算风格产物（CNE5 式）")
    p.add_argument("--start", default="2016-01-01", help="起始日（YYYY-MM-DD）")
    p.add_argument("--end", default="2099-12-31", help="结束日（YYYY-MM-DD）")
    p.add_argument("--out-dir", default=None, help="产物根目录（默认 QuantDB 5_technical_derived/style_factors）")
    p.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    p.add_argument("--limit-days", type=int, default=0, help="只跑最近 N 天（冒烟用）")
    p.add_argument("--skip-returns", action="store_true", help="只出暴露，不出纯因子收益")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    t0 = time.time()
    out_dir = Path(args.out_dir) if args.out_dir else resolve_quantdb_subdir("5_technical_derived", "style_factors")
    out_dir.mkdir(parents=True, exist_ok=True)

    val_dir = resolve_quantdb_subdir("5_technical_derived", "valuation")
    dates = list_dates(val_dir, args.start.replace("-", ""), args.end.replace("-", ""))
    if args.limit_days:
        dates = dates[-args.limit_days :]
    if not dates:
        log.error("区间内没有 valuation 分区，无法构建")
        return 1
    symbols = load_universe()
    log.info("区间 %s ~ %s：%d 个交易日 × %d 只股票", dates[0], dates[-1], len(dates), len(symbols))

    panels = load_panels(dates, symbols)
    panels.update(load_balance_pit(dates, symbols))
    for key in FLOW_SOURCES:
        panels[key] = load_flow_pit(dates, symbols, key)
    bench = load_bench_returns(dates)
    codes = industry_codes(symbols)

    log.info("计算描述子…")
    raw = SM.descriptors_from_panels(panels, bench)
    log.info("标准化流水线…")
    exposures = SM.standardize_pipeline(raw, codes)
    _log_coverage(exposures, dates)

    cov = write_exposures(out_dir, dates, symbols, exposures)
    log.info("暴露已落盘：%d 行（全 NaN 行丢弃 %d）", cov["rows"], cov["rows_all_nan"])
    meta = build_meta(dates, symbols, exposures, cov, codes, time.time() - t0)
    if not args.skip_returns:
        horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
        meta["returns"] = write_returns(out_dir, dates, horizons, panels, exposures, codes)
    tmp = out_dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out_dir / "meta.json")
    log.info("完成：%s（%.0fs）", out_dir, time.time() - t0)
    return 0


def _log_coverage(exposures: dict, dates: list[str]) -> None:
    """逐风格打印覆盖率；整列无覆盖时告警（**零项即失败**，不能静默出空产物）。"""
    for k in SM.STYLE_NAMES:
        v = np.asarray(exposures[k])
        cnt = np.isfinite(v).sum(axis=1)
        days = int((cnt > 0).sum())
        if days == 0:
            log.warning("风格 %s 全区间无覆盖 —— 检查源数据与口径", k)
            continue
        log.info("风格 %-14s 出数 %4d/%d 天，中位覆盖 %d 只", k, days, len(dates), int(np.median(cnt[cnt > 0])))


if __name__ == "__main__":
    raise SystemExit(main())
