"""Fama-French 五因子（A 股版）月度因子构建。

用途：给「模型推理选股组合」做风格剥离后的 α 归因（见 ``ff_attribution.py``）。

口径（月度，全部 PIT）：
- MKT：全市场市值加权月度收益 − 无风险利率（默认 0）
- SMB：2×3 独立排序（规模中位数 × B/M、ROE 型盈利、资产增速 30/70）三组 SMB 的均值
- HML：高 B/M − 低 B/M（规模内配对后跨规模平均）
- RMW：高盈利 − 低盈利；盈利 = 营业利润 TTM / 归母净资产（PIT）
- CMA：低资产增速（保守）− 高资产增速（激进）
- TURN（可选第六因子）：高换手 − 低换手（CH-4 口径的换手/流动性轴）

数据来源（只读）：
- 行情：``1_kline_data/daily_forward``（前复权，收益）与 ``daily_unadjusted``（涨停判定另用）
- 估值：``5_technical_derived/valuation``（total_mv / pb / circulating_capital，逐日全市场）
- 财务：``3_financial_data/{balance,income}``（m_anntime 公告日 PIT；income 为单季值）

PIT/TTM 语义复用 ``backend/scripts/build_style_factors.py`` 的已验证实现
（``pit_order`` / ``pit_align`` / ``ttm_from_reports`` / ``load_balance_pit``），
避免两套口径漂移。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.shared.quantdb_paths import resolve_quantdb_dir, resolve_quantdb_subdir  # noqa: E402

log = logging.getLogger("ff_factors")

FACTOR_COLS = ("MKT", "SMB", "HML", "RMW", "CMA", "TURN")
CACHE_VERSION = 4


# ---------------------------------------------------------------------------
# 月份与市场快照
# ---------------------------------------------------------------------------
def month_end_dates(start: str, end: str) -> list[str]:
    """区间内每个自然月的最后一个 valuation 交易日（YYYYMMDD，升序）。

    valuation 逐日全市场落盘，分区名即交易日；用它的最后一天作为月末截面，
    与行情日历天然一致。
    """
    val_dir = resolve_quantdb_subdir("5_technical_derived", "valuation")
    start_i, end_i = int(start.replace("-", "")), int(end.replace("-", ""))
    by_month: dict[str, str] = {}
    for p in sorted(glob.glob(str(val_dir / "dt=*"))):
        dt = os.path.basename(p).split("=", 1)[1]
        if not (len(dt) == 8 and dt.isdigit() and start_i <= int(dt) <= end_i):
            continue
        by_month[dt[:6]] = dt
    return [by_month[m] for m in sorted(by_month)]


def _parquet_glob(subdir: Path) -> str:
    return str(subdir / "dt=*" / "*.parquet").replace("\\", "/")


def load_market_monthly(start: str, end: str) -> pd.DataFrame:
    """月度行情/估值快照：symbol, month, close_fwd, total_mv, pb, circ_cap, avg_volume, days。

    - ``close_fwd``：前复权收盘（月末最后一个交易日），用于月度收益
    - ``total_mv`` / ``pb`` / ``circ_cap``：valuate 月末最后一笔
    - ``avg_volume`` / ``days``：当月日均成交量（股）与交易日数，用于换手
    """
    import duckdb

    fwd = _parquet_glob(resolve_quantdb_subdir("1_kline_data", "daily_forward"))
    val = _parquet_glob(resolve_quantdb_subdir("5_technical_derived", "valuation"))
    con = duckdb.connect()
    try:
        px = con.execute(
            f"""
            SELECT symbol, strftime(time, '%Y-%m') AS month,
                   arg_max(close, time) AS close_fwd,
                   avg(volume) AS avg_volume,
                   count(*) AS days
            FROM read_parquet('{fwd}', hive_partitioning=1)
            WHERE time >= DATE '{start}' AND time <= DATE '{end}'
            GROUP BY 1, 2
            """
        ).fetchdf()
        mv = con.execute(
            f"""
            SELECT symbol, strftime(time, '%Y-%m') AS month,
                   arg_max(total_mv, time) AS total_mv,
                   arg_max(pb, time) AS pb,
                   arg_max(circulating_capital, time) AS circ_cap
            FROM read_parquet('{val}', hive_partitioning=1)
            WHERE time >= DATE '{start}' AND time <= DATE '{end}'
            GROUP BY 1, 2
            """
        ).fetchdf()
    finally:
        con.close()
    out = mv.merge(px, on=["symbol", "month"], how="outer")
    return out


def load_universe() -> np.ndarray:
    """证券主表全集（含退市），保证历史截面不被「当前在市」截断。"""
    from backend.scripts.build_style_factors import load_universe as _load

    return _load()


# ---------------------------------------------------------------------------
# PIT 财务：营业利润 TTM + 归母净资产 + 总资产
# ---------------------------------------------------------------------------
def load_oper_profit_ttm_pit(dates: list[str], symbols: np.ndarray) -> np.ndarray:
    """营业利润 TTM（单季值滚动四季），按公告日 PIT 对齐到月末日期。

    与 ``build_style_factors.load_flow_pit`` 同一套 TTM/PIT 语义，只是列换成
    ``income.oper_profit``（实测为单季值）。
    """
    from backend.scripts.build_style_factors import (
        pit_align,
        pit_order,
        ttm_from_reports,
    )

    src_dir = resolve_quantdb_subdir("3_financial_data", "income")
    date_int = np.array([int(d) for d in dates], dtype=np.int64)
    out = np.full((len(dates), len(symbols)), np.nan)
    hit = 0
    for j, sym in enumerate(symbols):
        path = src_dir / f"{sym}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(
                path, columns=["m_anntime", "m_timetag", "oper_profit"]
            )
        except Exception as exc:  # noqa: BLE001 — 单票损坏不中断全量构建
            log.debug("income 读取失败 %s: %s", sym, exc)
            continue
        ann = pd.to_numeric(df["m_anntime"], errors="coerce")
        tt = pd.to_numeric(df["m_timetag"], errors="coerce")
        val = pd.to_numeric(df["oper_profit"], errors="coerce")
        ok = (ann.notna() & tt.notna() & val.notna()).to_numpy()
        if not ok.any():
            continue
        tt_i = tt.to_numpy()[ok]
        ann_i = ann.to_numpy()[ok]
        val_i = val.to_numpy()[ok]
        order = pit_order(ann_i, tt_i)
        ann_s, tt_s, val_s = ann_i[order], tt_i[order], val_i[order]
        ttm = ttm_from_reports(tt_s, val_s, cumulative=False)
        out[:, j] = pit_align(ann_s, ttm, date_int)
        hit += 1
    log.info("营业利润 TTM 面板完成：%d/%d 只", hit, len(symbols))
    return out


def load_pit_financials(
    dates: list[str],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """返回 (symbols, equity[T,N], assets[T,N], op_ttm[T,N])，三者均按公告日 PIT。"""
    from backend.scripts.build_style_factors import load_balance_pit

    symbols = load_universe()
    if not symbols.size:
        raise RuntimeError("instrument_detail 为空，无法确定股票池")
    t0 = time.time()
    bal = load_balance_pit(dates, symbols)
    op = load_oper_profit_ttm_pit(dates, symbols)
    log.info(
        "PIT 财务面板就绪：%d 月 × %d 只（%.0fs）",
        len(dates),
        len(symbols),
        time.time() - t0,
    )
    return symbols, bal["book_equity"], bal["total_assets"], op


# ---------------------------------------------------------------------------
# 因子构建（纯函数，单测直接喂合成面板）
# ---------------------------------------------------------------------------
def _vw_return(rets: np.ndarray, weights: np.ndarray) -> float:
    w = np.asarray(weights, dtype=np.float64)
    r = np.asarray(rets, dtype=np.float64)
    ok = np.isfinite(w) & np.isfinite(r) & (w > 0)
    if ok.sum() < 3:
        return np.nan
    w = w[ok]
    return float(np.sum(w * r[ok]) / np.sum(w))


def _char_sort_stats(
    frame: pd.DataFrame, char_col: str, *, size_median: float, min_names: int = 3
) -> dict:
    """单特征 2×3 独立排序：返回 6 个组合的市值加权收益与计数。"""
    d = frame[
        np.isfinite(frame["size"])
        & np.isfinite(frame["ret"])
        & np.isfinite(frame[char_col])
    ]
    if len(d) < 6:
        return {}
    lo, hi = d[char_col].quantile([0.3, 0.7])
    small = d["size"] <= size_median
    groups = {
        "SL": small & (d[char_col] <= lo),
        "SN": small & (d[char_col] > lo) & (d[char_col] < hi),
        "SH": small & (d[char_col] >= hi),
        "BL": (~small) & (d[char_col] <= lo),
        "BN": (~small) & (d[char_col] > lo) & (d[char_col] < hi),
        "BH": (~small) & (d[char_col] >= hi),
    }
    out = {}
    for key, mask in groups.items():
        sub = d[mask.to_numpy()]
        out[key] = {
            "ret": _vw_return(sub["ret"].to_numpy(), sub["size"].to_numpy())
            if len(sub) >= min_names
            else np.nan,
            "n": int(len(sub)),
        }
    return out


def _mean_ok(values: list[float]) -> float:
    arr = np.asarray(values, dtype=np.float64)
    return float(np.nanmean(arr)) if np.isfinite(arr).any() else np.nan


def compute_factors_from_panel(
    panel: pd.DataFrame, *, rf_annual: float = 0.0
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """由长表面板计算月度 FF5（+换手）因子收益。

    ``panel`` 需含列：month, symbol, size, bm, op, inv, turn, ret（均为当月截面 /
    下月收益）。返回 (factors[month, MKT,SMB,HML,RMW,CMA,TURN,n_universe],
    diag[month, 各组合计数])。
    """
    rf_m = rf_annual / 12.0
    rows, diags = [], []
    for month, frame in panel.groupby("month", sort=True):
        base = frame[
            np.isfinite(frame["size"]) & np.isfinite(frame["ret"]) & (frame["size"] > 0)
        ]
        if len(base) < 20:
            continue
        size_med = float(base["size"].median())
        mkt = _vw_return(base["ret"].to_numpy(), base["size"].to_numpy()) - rf_m

        sort_bm = _char_sort_stats(frame, "bm", size_median=size_med)
        sort_op = _char_sort_stats(frame, "op", size_median=size_med)
        sort_inv = _char_sort_stats(frame, "inv", size_median=size_med)
        sort_turn = _char_sort_stats(frame, "turn", size_median=size_med)

        def _ret(sorts: dict, key: str) -> float:
            return sorts.get(key, {}).get("ret", np.nan)

        smb_parts = []
        for sorts in (sort_bm, sort_op, sort_inv):
            if not sorts:
                continue
            small = _mean_ok([_ret(sorts, "SL"), _ret(sorts, "SN"), _ret(sorts, "SH")])
            big = _mean_ok([_ret(sorts, "BL"), _ret(sorts, "BN"), _ret(sorts, "BH")])
            if np.isfinite(small) and np.isfinite(big):
                smb_parts.append(small - big)

        hml = np.nan
        if sort_bm:
            hml = _mean_ok([_ret(sort_bm, "SH"), _ret(sort_bm, "BH")]) - _mean_ok(
                [_ret(sort_bm, "SL"), _ret(sort_bm, "BL")]
            )
        rmw = np.nan
        if sort_op:
            rmw = _mean_ok([_ret(sort_op, "SH"), _ret(sort_op, "BH")]) - _mean_ok(
                [_ret(sort_op, "SL"), _ret(sort_op, "BL")]
            )
        cma = np.nan
        if sort_inv:
            # 低资产增速=保守：CMA = 低增速 − 高增速
            cma = _mean_ok([_ret(sort_inv, "SL"), _ret(sort_inv, "BL")]) - _mean_ok(
                [_ret(sort_inv, "SH"), _ret(sort_inv, "BH")]
            )
        turn = np.nan
        if sort_turn:
            turn = _mean_ok([_ret(sort_turn, "SH"), _ret(sort_turn, "BH")]) - _mean_ok(
                [_ret(sort_turn, "SL"), _ret(sort_turn, "BL")]
            )

        rows.append(
            {
                "month": month,
                "MKT": mkt,
                "SMB": _mean_ok(smb_parts),
                "HML": hml,
                "RMW": rmw,
                "CMA": cma,
                "TURN": turn,
                "n_universe": int(len(base)),
            }
        )
        diag = {"month": month, "size_median": size_med}
        for name, sorts in (
            ("bm", sort_bm),
            ("op", sort_op),
            ("inv", sort_inv),
            ("turn", sort_turn),
        ):
            for key in ("SL", "SN", "SH", "BL", "BN", "BH"):
                diag[f"{name}_{key}_n"] = int(sorts.get(key, {}).get("n", 0))
        diags.append(diag)

    factors = pd.DataFrame(rows).sort_values("month").reset_index(drop=True)
    diag_df = pd.DataFrame(diags).sort_values("month").reset_index(drop=True)
    return factors, diag_df


# ---------------------------------------------------------------------------
# 数据装配 + 缓存
# ---------------------------------------------------------------------------
def _assemble_panel(start: str, end: str) -> pd.DataFrame:
    """月度长表面板：market snapshot + PIT 财务 → size/bm/op/inv/turn/ret。"""
    dates = month_end_dates(start, end)
    if len(dates) < 3:
        raise RuntimeError(f"月度截面不足（{len(dates)} 个月），检查 {start}~{end}")
    market = load_market_monthly(start, end)
    market["month"] = market["month"].astype(str)
    market = market.dropna(subset=["symbol"])
    market["symbol"] = market["symbol"].astype(str)

    symbols, equity, assets, op_ttm = load_pit_financials(dates)
    sym_index = pd.Index(symbols.astype(str))
    months = pd.to_datetime([f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]).strftime(
        "%Y-%m"
    )

    # 财务面板 → 长表（月末）
    fin = []
    for i, month in enumerate(months):
        fin.append(
            pd.DataFrame(
                {
                    "symbol": sym_index,
                    "month": month,
                    "equity": equity[i],
                    "assets": assets[i],
                    "op_ttm": op_ttm[i],
                }
            )
        )
    fin_df = pd.concat(fin, ignore_index=True)

    panel = market.merge(fin_df, on=["symbol", "month"], how="left")

    # 月度收益：下月收盘 / 本月收盘 - 1（前复权）
    panel = panel.sort_values(["symbol", "month"])
    panel["ret"] = (
        panel.groupby("symbol")["close_fwd"].shift(-1) / panel["close_fwd"] - 1.0
    )
    # 因子月份 = 收益所属月份（t→t+1 的收益记在 t+1 月），避免与组合月收益错位一个月
    panel["factor_month"] = panel.groupby("symbol")["month"].shift(-1)
    # 资产增速（12 个月 PIT 差分）与换手
    panel["assets_prev"] = panel.groupby("symbol")["assets"].shift(12)
    panel["size"] = panel["total_mv"]
    panel["bm"] = 1.0 / panel["pb"].where(panel["pb"] > 0)
    panel["op"] = panel["op_ttm"] / panel["equity"].where(panel["equity"] > 0)
    panel["inv"] = (
        panel["assets"] / panel["assets_prev"].where(panel["assets_prev"] > 0) - 1.0
    )
    panel["turn"] = (
        panel["avg_volume"]
        * panel["days"]
        / panel["circ_cap"].where(panel["circ_cap"] > 0)
    )

    cols = [
        "factor_month",
        "month",
        "symbol",
        "size",
        "bm",
        "op",
        "inv",
        "turn",
        "ret",
        "close_fwd",
    ]
    out = panel[cols].dropna(subset=["factor_month"]).copy()
    # 保留特征月（char_month）供「学术口径：按 6 月截面固定一年」使用
    out = out.rename(columns={"month": "char_month", "factor_month": "month"})
    return out[
        [
            "month",
            "char_month",
            "symbol",
            "size",
            "bm",
            "op",
            "inv",
            "turn",
            "ret",
            "close_fwd",
        ]
    ]


def apply_academic_sorts(panel: pd.DataFrame) -> pd.DataFrame:
    """学术口径：每年 6 月截面固定特征，持有 7 月~次年 6 月（对齐 FF 原文/teal0range 实现）。

    - 规模/BM/OP/Inv/换手 全部取当年 6 月的 PIT 值（6 月时年报已披露 → 等价于
      「t-1 年 12 月财务 + 6 月市值」且不产生前视）；
    - 收益仍按月度；factor_month 在 7~12 月归当年、1~6 月归上一年。
    """
    june = panel[panel["char_month"].str.endswith("-06")].copy()
    if june.empty:
        raise RuntimeError("学术口径需要至少一个 6 月截面；请把起点前移")
    june["acad_year"] = june["char_month"].str.slice(0, 4).astype(int)
    keep = ["size", "bm", "op", "inv", "turn"]
    june = june[["symbol", "acad_year", *keep]].rename(
        columns={c: f"{c}_jun" for c in keep}
    )
    out = panel.copy()
    month_num = out["month"].str.slice(5, 7).astype(int)
    out["acad_year"] = out["month"].str.slice(0, 4).astype(int) - (
        month_num < 7
    ).astype(int)
    out = out.merge(june, on=["symbol", "acad_year"], how="left")
    for c in keep:
        out[c] = out[f"{c}_jun"]
    return out.drop(columns=[f"{c}_jun" for c in keep] + ["acad_year"])


def orthogonalize_inv(panel: pd.DataFrame) -> pd.DataFrame:
    """把 Inv 对 OP 做截面正交化（华泰 A 股实证：投资因子与盈利因子反向共线）。

    逐月对 ``inv ~ 1 + op`` 回归取残差；op 缺失的行保持原 inv（不因控制变量缺失去掉样本）。
    """
    frames = []
    for _, frame in panel.groupby("month", sort=True):
        frame = frame.copy()
        ok = np.isfinite(frame["inv"]) & np.isfinite(frame["op"])
        if int(ok.sum()) >= 20:
            x = np.column_stack(
                [np.ones(int(ok.sum())), frame.loc[ok, "op"].to_numpy()]
            )
            y = frame.loc[ok, "inv"].to_numpy()
            beta, *_ = np.linalg.lstsq(x, y, rcond=None)
            frame.loc[ok, "inv"] = y - x @ beta
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def factor_diagnostics(factors: pd.DataFrame) -> dict:
    """因子模型自检：溢价 t、因子间相关、因子互相回归的 α（spanning test）。

    - premium：各因子月度均值与 t 值（A 股经验：SMB 最强，HML/RMW/CMA 递减）；
    - corr：因子收益相关矩阵（CMA/RMW 共线是 A 股已知坑）；
    - spanning：每个因子对其余因子回归，α 的 HAC t——理想 FF5 下其余因子不应
      能解释某个因子（α 显著 = 该因子有独立信息或模型设定有问题）。
    """
    from backend.scripts.eval.ff_attribution import hac_ols  # 延迟导入避免循环

    ff = [c for c in ("MKT", "SMB", "HML", "RMW", "CMA") if c in factors.columns]
    df = factors[ff].dropna()
    premium = {}
    for col in ff:
        x = df[col].to_numpy()
        t = (
            float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))
            if len(x) > 2 and x.std(ddof=1) > 0
            else np.nan
        )
        premium[col] = {
            "mean_monthly": float(x.mean()),
            "ann": float(x.mean() * 12.0),
            "t": t,
        }
    corr = df.corr().round(4).to_dict() if not df.empty else {}
    spanning = {}
    for col in ff:
        others = [c for c in ff if c != col]
        if not others or len(df) < len(others) + 5:
            continue
        res = hac_ols(df[col].to_numpy(), df[others].to_numpy())
        spanning[col] = {
            "alpha_monthly": float(res["beta"][0]),
            "alpha_t": float(res["t"][0]),
        }
    return {"premium": premium, "corr": corr, "spanning": spanning, "n": int(len(df))}


def _panel_tag(start: str, end: str, sort_mode: str, orth_inv: bool) -> str:
    return (
        f"{start.replace('-', '')}_{end.replace('-', '')}"
        f"_{sort_mode}{'_orthinv' if orth_inv else ''}_v{CACHE_VERSION}"
    )


def build_or_load_panel(
    start: str,
    end: str,
    *,
    sort_mode: str = "academic",
    orth_inv: bool = False,
    cache_dir: Path | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    """构建（或读取缓存）月度长表面板，供因子与 5×5 验证共用。"""
    if sort_mode not in {"monthly", "academic"}:
        raise ValueError(f"未知 sort_mode: {sort_mode}")
    cache_dir = cache_dir or resolve_quantdb_dir().parent / "factor_research" / "ff5"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ppath = (
        cache_dir / f"ff5_panel_{_panel_tag(start, end, sort_mode, orth_inv)}.parquet"
    )
    if ppath.exists() and not rebuild:
        return pd.read_parquet(ppath)

    # 资产增速用 12 个月前的 PIT 总资产：面板要额外向前取 13 个月，
    # 否则窗口首月的 inv 全是 NaN → CMA 整段缺失。
    # 学术口径还要再往前一年：首个学术年的 6 月截面的 inv 需要再前 12 个月的资产。
    back_months = 25 if sort_mode == "academic" else 13
    panel_start = (pd.Timestamp(start) - pd.DateOffset(months=back_months)).strftime(
        "%Y-%m-%d"
    )
    panel = _assemble_panel(panel_start, end)
    if sort_mode == "academic":
        panel = apply_academic_sorts(panel)
    if orth_inv:
        panel = orthogonalize_inv(panel)
    panel.to_parquet(ppath, index=False)
    return panel


def size_char_5x5_validation(
    panel: pd.DataFrame,
    factors: pd.DataFrame,
    *,
    n_groups: int = 5,
) -> dict:
    """5×5 Size×{BM,OP,Inv} 组合对 FF5 的回归检验（因子模型稳健性）。

    学术做法（teal0range/FF5）：按 size 与特征各自 5 分组取交集，得到 25 个
    市值加权组合；每个组合对 FF5 做时间序列回归。若因子模型成立，绝大多数
    组合的 α 应不显著；这里汇总 α 的 HAC t 分布与 R²。
    """
    from backend.scripts.eval.ff_attribution import hac_ols  # 延迟导入避免循环

    ff = [c for c in ("MKT", "SMB", "HML", "RMW", "CMA") if c in factors.columns]
    fdf = factors.set_index("month")[ff].dropna()
    months = set(fdf.index)
    p = panel[panel["month"].isin(months)].copy()
    rows = []
    for char in ("bm", "op", "inv"):
        for month, frame in p.groupby("month", sort=True):
            d = frame[
                np.isfinite(frame["size"])
                & np.isfinite(frame["ret"])
                & np.isfinite(frame[char])
            ]
            if len(d) < n_groups * n_groups * 5:
                continue
            try:
                size_g = pd.qcut(d["size"], n_groups, labels=False, duplicates="drop")
                char_g = pd.qcut(d[char], n_groups, labels=False, duplicates="drop")
            except ValueError:
                continue
            for si in range(n_groups):
                for cj in range(n_groups):
                    sub = d[(size_g == si) & (char_g == cj)]
                    if len(sub) < 3:
                        continue
                    w = sub["size"].to_numpy()
                    r = sub["ret"].to_numpy()
                    ok = np.isfinite(w) & np.isfinite(r) & (w > 0)
                    if ok.sum() < 3:
                        continue
                    rows.append(
                        {
                            "month": month,
                            "char": char,
                            "size_g": si,
                            "char_g": cj,
                            "ret": float(np.sum(w[ok] * r[ok]) / np.sum(w[ok])),
                        }
                    )
    if not rows:
        return {"portfolios": 0, "by_char": {}}
    port = pd.DataFrame(rows)
    by_char = {}
    all_alpha_t = []
    all_r2 = []
    for char, cframe in port.groupby("char"):
        pivot = cframe.pivot_table(
            index="month", columns=["size_g", "char_g"], values="ret"
        ).sort_index()
        alphas = []
        for col in pivot.columns:
            y = pivot[col].dropna()
            common = y.index.intersection(fdf.index)
            if len(common) < len(ff) + 8:
                continue
            res = hac_ols(y.loc[common].to_numpy(), fdf.loc[common].to_numpy())
            alphas.append(
                {
                    "size_g": int(col[0]),
                    "char_g": int(col[1]),
                    "alpha_monthly": float(res["beta"][0]),
                    "alpha_t": float(res["t"][0]),
                    "r2": float(res["r2"]),
                }
            )
        if not alphas:
            continue
        ts = np.array([a["alpha_t"] for a in alphas])
        r2s = np.array([a["r2"] for a in alphas])
        by_char[char] = {
            "n": len(alphas),
            "mean_r2": float(r2s.mean()),
            "frac_abs_t_gt2": float((np.abs(ts) > 2).mean()),
            "max_abs_t": float(np.abs(ts).max()),
            "min_abs_t": float(np.abs(ts).min()),
        }
        all_alpha_t.extend(ts.tolist())
        all_r2.extend(r2s.tolist())
    return {
        "portfolios": len(all_alpha_t),
        "mean_r2": float(np.mean(all_r2)) if all_r2 else None,
        "frac_abs_t_gt2": float((np.abs(all_alpha_t) > 2).mean())
        if all_alpha_t
        else None,
        "max_abs_t": float(np.max(np.abs(all_alpha_t))) if all_alpha_t else None,
        "by_char": by_char,
    }


def build_or_load_factors(
    start: str,
    end: str,
    *,
    rf_annual: float = 0.0,
    sort_mode: str = "academic",
    orth_inv: bool = False,
    cache_dir: Path | None = None,
    rebuild: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构建（或读取缓存）月度 FF5+TURN 因子。

    ``sort_mode``: ``academic``（6 月固定一年，默认）/ ``monthly``（月度重排）。
    ``orth_inv``: Inv 对 OP 截面正交化后再排序（缓解 A 股 CMA/RMW 共线）。
    """
    if sort_mode not in {"monthly", "academic"}:
        raise ValueError(f"未知 sort_mode: {sort_mode}")
    start_i, end_i = start.replace("-", ""), end.replace("-", "")
    cache_dir = cache_dir or resolve_quantdb_dir().parent / "factor_research" / "ff5"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tag = (
        f"{start_i}_{end_i}_rf{rf_annual:g}_{sort_mode}"
        f"{'_orthinv' if orth_inv else ''}_v{CACHE_VERSION}"
    )
    fpath = cache_dir / f"ff5_monthly_{tag}.parquet"
    dpath = cache_dir / f"ff5_diag_{tag}.parquet"
    if fpath.exists() and dpath.exists() and not rebuild:
        return pd.read_parquet(fpath), pd.read_parquet(dpath)

    panel = build_or_load_panel(
        start,
        end,
        sort_mode=sort_mode,
        orth_inv=orth_inv,
        cache_dir=cache_dir,
        rebuild=rebuild,
    )
    factors, diag = compute_factors_from_panel(panel, rf_annual=rf_annual)
    factors = factors[factors["month"] >= start[:7]].reset_index(drop=True)
    diag = diag[diag["month"] >= start[:7]].reset_index(drop=True)
    factors.to_parquet(fpath, index=False)
    diag.to_parquet(dpath, index=False)
    meta = {
        "version": CACHE_VERSION,
        "start": start,
        "end": end,
        "rf_annual": rf_annual,
        "sort_mode": sort_mode,
        "orth_inv": orth_inv,
        "months": len(factors),
        "built_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "definitions": {
            "MKT": "全市场市值加权 − rf/12",
            "SMB": "2×3（size × bm/op/inv）三组 SMB 均值",
            "HML": "高 B/M − 低 B/M（size 内配对）",
            "RMW": "营业利润TTM/归母净资产，高 − 低",
            "CMA": "低资产增速（保守）− 高资产增速",
            "TURN": "高换手 − 低换手（月度日均量×交易日/流通股本）",
        },
        "pit": "财务按公告日 m_anntime 对齐；income 单季值滚动四季出 TTM；资产增速=12个月PIT差分",
    }
    (cache_dir / f"ff5_meta_{tag}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return factors, diag
