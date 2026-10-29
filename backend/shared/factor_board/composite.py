"""因子概率板：收盘矩阵（qfq）→ 前向收益 rank → 加权合成 → 持有期画像。

价格侧唯一出口：``1_kline_data/daily_forward``（qfq）。跨日收益绝不允许用
``daily_backward``（hfq，逐分区拼接的不自洽序列，见 docs 细案 §2.6）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.shared.factor_board import config as C

log = logging.getLogger("factor_board.composite")


def list_factor_dates(quantdb_root: Path, lib: str = "l1_factors") -> list[str]:
    """6_ml_datasets 下某库的 dt 分区日期（升序）。"""
    root = quantdb_root / "6_ml_datasets" / lib
    if not root.is_dir():
        raise FileNotFoundError(f"因子库目录缺失: {root}")
    return sorted(
        d.name[3:] for d in root.iterdir() if d.is_dir() and d.name[3:].isdigit()
    )


def common_factor_dates(
    quantdb_root: Path, libs: list[str]
) -> tuple[list[str], dict[str, str]]:
    """候选库的**共同**分区日期（升序）与各库最新日期。

    为什么不用 l1 的日期：各库同步有先后（实测 2026-10-29 l1/l2 已到 10-28，
    alpha360/alpha_library/jq110/tdxgs/rd_mined 仍停在 10-27）。若按 l1 最新日
    产板，其余库会被整库剔除，入选因子数骤降（实测 6 < 硬门 10）——宁可整体
    退到全库都有数据的 10-27，也不要用残缺横截面选因子。
    """
    sets: dict[str, set[str]] = {}
    latest: dict[str, str] = {}
    for lib in libs:
        try:
            ds = set(list_factor_dates(quantdb_root, lib))
        except FileNotFoundError:
            continue
        if ds:
            sets[lib] = ds
            latest[lib] = max(ds)
    if not sets:
        return [], latest
    common = set.intersection(*sets.values())
    return sorted(common), latest


def load_close_matrix(quantdb_root: Path, dates: list[str]) -> pd.DataFrame:
    """qfq 收盘矩阵（index=symbol 后缀式，columns=dt）。缺分区→该列 NaN 并告警。"""
    root = quantdb_root / "1_kline_data" / "daily_forward"
    cols: dict[str, pd.Series] = {}
    missing = []
    for dt in dates:
        p = root / f"dt={dt}" / "data.parquet"
        if not p.exists():
            missing.append(dt)
            continue
        # 分区内偶见重复 symbol（追加写/多源拼接）；不先按 symbol 去重会让
        # 后续 fwd_ret/rank 得到重复索引，v.notna() & fr.notna() 直接抛
        # 「cannot reindex on an axis with duplicate labels」。
        d = pd.read_parquet(p, columns=["symbol", "close"])
        cols[dt] = d.drop_duplicates("symbol", keep="last").set_index("symbol")["close"]
    if missing:
        log.warning("daily_forward 缺 %d 个分区: %s", len(missing), missing[:5])
    return pd.DataFrame(cols)


def fwd_ret(close: pd.DataFrame, i: int, h: int) -> pd.Series | None:
    """第 i 列起、未来 h 日**原始**收益（qfq close(t+h)/close(t)-1）。

    净 IC 的成本必须扣在原始收益上（rank 不可逆，扣费发生在 rank 之前——
    与 ``statistical/costs.net_rank_ic`` 同口径）。
    """
    if i + h >= close.shape[1]:
        return None
    f = close.iloc[:, i + h] / close.iloc[:, i] - 1
    return f.replace([np.inf, -np.inf], np.nan)


def fwd_rank(close: pd.DataFrame, i: int, h: int) -> pd.Series | None:
    """第 i 列起、未来 h 日收益的 rank（前向收益口径 = qfq close(t+h)/close(t)-1）。"""
    f = fwd_ret(close, i, h)
    return None if f is None else f.rank()


def fwd_rets(
    close: pd.DataFrame, dates: list[str], indices: range, h: int
) -> dict[str, pd.Series]:
    """``indices`` 范围内每个评估日的原始前向 h 日收益（净 IC 轨专用）。"""
    out: dict[str, pd.Series] = {}
    for i in indices:
        fr = fwd_ret(close, i, h)
        if fr is not None:
            out[dates[i]] = fr
    return out


def fwd_ranks(
    close: pd.DataFrame, dates: list[str], indices: range, h: int
) -> dict[str, pd.Series]:
    """``indices`` 范围内每个评估日的前向 h 日收益 rank（供 IC/验证/画像复用）。"""
    out: dict[str, pd.Series] = {}
    for i in indices:
        fr = fwd_rank(close, i, h)
        if fr is not None:
            out[dates[i]] = fr
    return out


def composite_day(
    selected: pd.DataFrame, lib_frames: dict[str, pd.DataFrame]
) -> pd.Series:
    """单日加权合成（纯函数）。

    ``lib_frames[lib]``：index=symbol，只含该库入选因子列。
    z = 2·rank_pct−1；contrib = sgn·z·w；composite = Σcontrib/Σ已覆盖 w；
    已覆盖权重 <COVERAGE_GATE·总权重 → null（dropna 交由调用方）。
    """
    w_map = {(r["lib"], r["name"]): float(r["w"]) for _, r in selected.iterrows()}
    sgn_map = {(r["lib"], r["name"]): int(r["sgn"]) for _, r in selected.iterrows()}
    total_w = float(sum(w_map.values()))

    c: pd.Series | None = None
    wc: pd.Series | None = None
    for lib, names in selected.groupby("lib")["name"].apply(list).items():
        d = lib_frames.get(lib)
        if d is None:
            continue
        for n in names:
            v = d[n]
            z = 2 * v.rank(pct=True) - 1
            if c is None:
                c = pd.Series(0.0, index=d.index)
                wc = pd.Series(0.0, index=d.index)
            c = c.add(sgn_map[(lib, n)] * z * w_map[(lib, n)], fill_value=0)
            wc = wc.add(w_map[(lib, n)] * v.notna().astype(float), fill_value=0)
    if c is None or wc is None:
        return pd.Series(dtype=float)
    comp = c / wc.replace(0, np.nan)
    comp[wc < C.COVERAGE_GATE * total_w] = np.nan
    return comp


def read_selected_frames(
    quantdb_root: Path, selected: pd.DataFrame, dt: str
) -> dict[str, pd.DataFrame]:
    """读某日各库的入选因子列（缺分区/缺列→该库缺失或列全 NaN）。"""
    frames: dict[str, pd.DataFrame] = {}
    for lib, names in selected.groupby("lib")["name"].apply(list).items():
        p = quantdb_root / "6_ml_datasets" / lib / f"dt={dt}" / "data.parquet"
        if not p.exists():
            continue
        want = sorted(set(names))
        try:
            d = pd.read_parquet(p, columns=["symbol"] + want).set_index("symbol")
        except KeyError as exc:  # 列缺失（分区 schema 漂移）→ 缺失列补 NaN
            log.warning("库 %s 于 %s 读列失败（%s），缺失列补 NaN", lib, dt, exc)
            have = set(pq.ParquetFile(p).schema_arrow.names)
            d = pd.read_parquet(p, columns=["symbol"] + [n for n in want if n in have])
            d = d.set_index("symbol")
            for n in want:
                if n not in d.columns:
                    d[n] = np.nan
        d = d[~d.index.duplicated(keep="last")]
        frames[lib] = d
    return frames


def build_composite_history(
    quantdb_root: Path, selected: pd.DataFrame, dates: list[str], start_idx: int
) -> dict[str, pd.Series]:
    """最近 ``len(dates)-start_idx`` 个交易日的合成截面（dropna 后）。"""
    out: dict[str, pd.Series] = {}
    for i in range(start_idx, len(dates)):
        dt = dates[i]
        frames = read_selected_frames(quantdb_root, selected, dt)
        comp = composite_day(selected, frames).dropna()
        if len(comp):
            out[dt] = comp
    return out


def horizon_table(
    quantdb_root: Path,
    selected: pd.DataFrame,
    dates: list[str],
    start_idx: int,
    fwd_by_h: dict[int, dict[str, pd.Series]],
) -> pd.DataFrame:
    """入选因子 × 持有期（1/2/5/10/20 日）的近窗**方向对齐** IC 均值。

    对齐口径与合成一致：``sgn × RankIC``。原始符号由入选表「方向」列显式
    携带，画像回答「按入选方向使用，该持有期的 IC 有多强」；对齐后单值仍可为
    负（该持有期跨日翻转占优），是有效信息不是缺陷。
    """
    acc: dict[tuple[str, str], dict[int, list[float]]] = {
        (r["lib"], r["name"]): {h: [] for h in C.HORIZON_DAYS}
        for _, r in selected.iterrows()
    }
    sgn_map = {(r["lib"], r["name"]): int(r["sgn"]) for _, r in selected.iterrows()}
    by_lib = selected.groupby("lib")["name"].apply(list).to_dict()
    for i in range(start_idx, len(dates)):
        dt = dates[i]
        frs = {h: fwd_by_h[h].get(dt) for h in C.HORIZON_DAYS}
        if all(f is None for f in frs.values()):
            continue
        for lib, names in by_lib.items():
            frames = read_selected_frames(
                quantdb_root, selected[selected["lib"] == lib], dt
            )
            d = frames.get(lib)
            if d is None:
                continue
            for n in names:
                v = d[n]
                for h, fr in frs.items():
                    if fr is None:
                        continue
                    m = v.notna() & fr.notna()
                    if int(m.sum()) >= C.MIN_STOCK_SAMPLE:
                        ic = float(v[m].rank().corr(fr[m]))
                        acc[(lib, n)][h].append(sgn_map[(lib, n)] * ic)
    rows = {}
    for key, hz in acc.items():
        rows[key] = {
            h: (float(np.nanmean(vals)) if vals else None) for h, vals in hz.items()
        }
    df = pd.DataFrame(rows).T
    df.index = pd.MultiIndex.from_tuples(df.index, names=["lib", "name"])
    return df
