#!/usr/bin/env python3
"""分市场广度面板 + 全 A 合成 K 线（2026-10-28）。

产出（与 ``trin_breadth.py`` 的旧面板并存，旧文件一列不动）：

- ``market_breadth/by_universe.parquet`` —— 长表，一市场一段，列同
  ``trin_breadth.build_breadth_panel``，另加涨跌分布与合成 K 线。
- ``market_breadth/index_ohlc.parquet`` —— 展示用官方指数日线（6 个指数），
  由 ``1_kline_data/index_daily`` 一次装载，供情绪快照直接读。

宇宙口径（**全部 PIT，无前视**）：

- ``all`` / ``sh`` / ``sz`` / ``cyb``：板别规则，当日在该板且有价的全部 A 股；
- ``hs300`` / ``zz1000`` / ``sz500`` / ``cyb100``：**逐日流通市值排名代理**
  （前 300 / 801–1800 / 深市前 500 / 创业板前 100）。

排名用**前一交易日**的流通市值：用当日市值排名会把当日大涨的票选进宇宙，
等于让宇宙本身包含了当日收益（自我选择偏差）。成分股历史拿不到（本地
``index_weights`` 只有最新一期快照），所以不用「当前成分股回溯」——那是
把今天的成分拿去解释 2016 年。

全 A 合成 K 线：**月度再平衡的等权指数**。日频再平衡会白送再平衡收益
（2016 起日频 +67.6% vs 月频 +57.7% vs 季频 +36.8%），口径必须写死并披露。
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.breadth_regime import (
    INDICATORS,
    build_features,
    composite_scores,
    prepare_panel,
)
from backend.scripts.research_stats import expanding_pct_rank
from backend.scripts.trin_breadth import (
    PriceMatrices,
    build_breadth_panel,
    load_st_matrix,
    limit_threshold_matrix,
    symbol_category,
)

log = logging.getLogger("breadth_universe")

QUANTDB_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
BACKWARD_DIR = QUANTDB_DIR / "1_kline_data" / "daily_backward"
INDEX_DIR = QUANTDB_DIR / "1_kline_data" / "index_daily"
VALUATION_DIR = QUANTDB_DIR / "5_technical_derived" / "valuation"
OUT_DIR = QUANTDB_DIR / "5_technical_derived" / "market_breadth"

#: 展示指数 → 价格来源 / 广度宇宙
INDEX_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("000001.SH", "上证指数", "sh"),
    ("399001.SZ", "深证成指", "sz500"),
    ("399006.SZ", "创业板指", "cyb100"),
    ("000300.SH", "沪深300", "hs300"),
    ("000852.SH", "中证1000", "zz1000"),
    ("ALL.A", "A股指数（全A等权）", "all"),
)
INDEX_OHLC_SYMBOLS = tuple(s for s, _, _ in INDEX_TARGETS if s != "ALL.A")

#: 涨跌分布七档（互斥）。阈值 5% 是展示口径，不参与压力分。
DIST_BIG = 0.05


@dataclass(frozen=True)
class Universe:
    key: str
    label: str
    mode: str  # board | rank
    suffix: str | None = None
    prefixes: tuple[str, ...] | None = None
    top: int | None = None
    start: int | None = None
    end: int | None = None
    note: str = ""


UNIVERSES: tuple[Universe, ...] = (
    Universe("all", "全A", "board", note="全部 A 股（含北交所）"),
    Universe("sh", "沪市", "board", suffix=".SH", note="沪市全部 A 股"),
    Universe("sz", "深市", "board", suffix=".SZ", note="深市全部 A 股"),
    Universe(
        "cyb", "创业板", "board", prefixes=("300", "301", "302"), note="创业板全部 A 股"
    ),
    Universe(
        "hs300", "沪深300", "rank", top=300, note="全A流通市值前 300（逐日排名代理）"
    ),
    Universe(
        "zz1000",
        "中证1000",
        "rank",
        start=800,
        end=1800,
        note="全A流通市值 801–1800 名（逐日排名代理）",
    ),
    Universe(
        "sz500",
        "深证成指",
        "rank",
        suffix=".SZ",
        top=500,
        note="深市流通市值前 500（逐日排名代理）",
    ),
    Universe(
        "cyb100",
        "创业板指",
        "rank",
        prefixes=("300", "301", "302"),
        top=100,
        note="创业板流通市值前 100（逐日排名代理）",
    ),
)
UNIVERSE_BY_KEY = {u.key: u for u in UNIVERSES}


def board_mask(
    symbols: np.ndarray, suffix: str | None, prefixes: tuple[str, ...] | None
) -> np.ndarray:
    """板别掩码（静态，与日期无关）。"""
    m = np.ones(len(symbols), dtype=bool)
    if suffix:
        m &= np.array([str(s).endswith(suffix) for s in symbols], dtype=bool)
    if prefixes:
        m &= np.array(
            [str(s).split(".")[0][:3] in prefixes for s in symbols], dtype=bool
        )
    return m


def load_full_matrices(
    start: str | None = None, end: str | None = None
) -> PriceMatrices:
    """OHLCV 全量宽表（合成 K 线需要开高低）。与 ``load_price_matrices`` 同序同符号。"""
    days = _index_days(BACKWARD_DIR, start, end)
    symbols: set[str] = set()
    for d in days:
        symbols.update(
            pq.read_table(BACKWARD_DIR / f"dt={d}" / "data.parquet", columns=["symbol"])
            .column("symbol")
            .to_pylist()
        )
    syms = np.array(sorted(symbols))
    index = {s: i for i, s in enumerate(syms)}
    n_d, n_s = len(days), len(syms)
    fields = {
        k: np.full((n_d, n_s), np.nan)
        for k in ("open", "high", "low", "close", "amount")
    }
    for i, d in enumerate(days):
        df = pq.read_table(
            BACKWARD_DIR / f"dt={d}" / "data.parquet",
            columns=["symbol", "open", "high", "low", "close", "amount"],
        ).to_pandas()
        j = df["symbol"].map(index).to_numpy()
        for k in fields:
            fields[k][i, j] = df[k].to_numpy()
    return PriceMatrices(
        dates=pd.to_datetime([pd.Timestamp(d) for d in days]),
        symbols=syms,
        close=fields["close"],
        volume=fields["close"].copy(),
        amount=fields["amount"],
        open=fields["open"],
        high=fields["high"],
        low=fields["low"],
    )


def _index_days(base: Path, start: str | None, end: str | None) -> list[str]:
    if not base.exists():
        raise FileNotFoundError(f"QuantDB 目录不存在: {base}")
    days = sorted(p.name.split("=", 1)[1] for p in base.glob("dt=*"))
    if start:
        days = [d for d in days if d >= start.replace("-", "")]
    if end:
        days = [d for d in days if d <= end.replace("-", "")]
    if not days:
        raise ValueError(f"{base} 在 {start}~{end} 区间没有任何分区")
    return days


def _load_float_mv(dates: pd.DatetimeIndex, symbols: np.ndarray) -> np.ndarray:
    """逐日流通市值 (D,S)，单位元。缺失为 NaN。"""
    index = {s: i for i, s in enumerate(symbols)}
    out = np.full((len(dates), len(symbols)), np.nan)
    for i, d in enumerate(dates):
        p = VALUATION_DIR / f"dt={d:%Y%m%d}" / "data.parquet"
        if not p.exists():
            continue
        df = pq.read_table(p, columns=["symbol", "float_mv"]).to_pandas()
        j = df["symbol"].map(index)
        ok = j.notna()
        out[i, j[ok].to_numpy().astype(int)] = df.loc[ok, "float_mv"].to_numpy()
    return out


def rank_mask(
    fmv: np.ndarray,
    valid: np.ndarray,
    static: np.ndarray,
    *,
    top: int | None = None,
    start: int | None = None,
    end: int | None = None,
) -> np.ndarray:
    """逐日市值排名宇宙。**用前一交易日市值排名**，当日只决定「谁能交易」。"""
    out = np.zeros_like(valid)
    for i in range(1, len(valid)):
        elig = valid[i] & static & np.isfinite(fmv[i - 1])
        n = int(elig.sum())
        if n == 0:
            continue
        order = np.argsort(-np.where(elig, fmv[i - 1], -np.inf))
        sel = order[: min(top, n)] if top is not None else order[start : min(end, n)]
        out[i, sel] = True
    return out


def distribution_counts(
    ret: np.ndarray, threshold: np.ndarray, base: np.ndarray
) -> dict[str, np.ndarray]:
    """七档涨跌分布（互斥）：涨停 / 涨>5% / 涨0~5% / 平 / 跌0~5% / 跌>5% / 跌停。"""
    limit_up = base & (ret >= threshold)
    limit_down = base & (ret <= -threshold)
    up = base & (ret > 0) & ~limit_up
    down = base & (ret < 0) & ~limit_down
    return {
        "dist_limit_up": limit_up.sum(axis=1),
        "dist_up_big": (up & (ret >= DIST_BIG)).sum(axis=1),
        "dist_up_small": (up & (ret < DIST_BIG)).sum(axis=1),
        "dist_flat": (base & (ret == 0)).sum(axis=1),
        "dist_down_small": (down & (ret > -DIST_BIG)).sum(axis=1),
        "dist_down_big": (down & (ret <= -DIST_BIG)).sum(axis=1),
        "dist_limit_down": limit_down.sum(axis=1),
    }


def equal_weight_aggregate(
    px: PriceMatrices,
    univ: np.ndarray,
    fmv: np.ndarray,
    base_level: float = 1000.0,
) -> pd.DataFrame:
    """月度再平衡的等权合成 K 线。

    - 权重：每个自然月首个交易日重置为等权，月内随涨跌漂移；
    - 开/高/低/收：都用**当日起始权重**加权成分股相对昨收的涨跌幅；
    - 成交额：成分股当日成交额之和（可加），换手率 = Σ成交额 / Σ昨流通市值。

    ⚠️ 加权的「最高/最低」是**成分股加权高低点**，不是指数日内极值（每只票
    的高点发生在不同时刻，平均出来的高点系统性偏高）。前端对合成口径不画蜡烛。
    """
    if px.open is None or px.high is None or px.low is None:
        raise ValueError(
            "合成 K 线需要 open/high/low：请用 breadth_universe.load_full_matrices"
        )
    ret = np.full_like(px.close, np.nan)
    ret[1:] = px.close[1:] / px.close[:-1] - 1.0
    rel_o = np.full_like(px.close, np.nan)
    rel_h = np.full_like(px.close, np.nan)
    rel_l = np.full_like(px.close, np.nan)
    rel_o[1:] = px.open[1:] / px.close[:-1] - 1.0
    rel_h[1:] = px.high[1:] / px.close[:-1] - 1.0
    rel_l[1:] = px.low[1:] / px.close[:-1] - 1.0

    valid = np.isfinite(ret) & np.isfinite(px.amount) & (px.amount > 0)
    months = px.dates.to_period("M")
    n_d, n_s = px.close.shape
    level = np.full(n_d, np.nan)
    o_lvl = np.full(n_d, np.nan)
    h_lvl = np.full(n_d, np.nan)
    l_lvl = np.full(n_d, np.nan)
    amt = np.zeros(n_d)
    turn = np.full(n_d, np.nan)
    level[0] = base_level

    w = np.zeros(n_s)
    prev_month = None
    for i in range(1, n_d):
        m = valid[i] & univ[i]
        cnt = int(m.sum())
        if cnt == 0:
            level[i] = level[i - 1]
            continue
        if w.sum() <= 0 or months[i] != prev_month:
            w = np.where(m, 1.0 / cnt, 0.0)
            prev_month = months[i]
        r = np.where(m, ret[i], 0.0)
        port = float(np.dot(w, r))
        level[i] = level[i - 1] * (1.0 + port)
        o_lvl[i] = level[i - 1] * (1.0 + float(np.dot(w, np.where(m, rel_o[i], 0.0))))
        h_lvl[i] = level[i - 1] * (1.0 + float(np.dot(w, np.where(m, rel_h[i], 0.0))))
        l_lvl[i] = level[i - 1] * (1.0 + float(np.dot(w, np.where(m, rel_l[i], 0.0))))
        amt[i] = float(np.where(m, px.amount[i], 0.0).sum())
        base_cap = float(np.where(m & np.isfinite(fmv[i - 1]), fmv[i - 1], 0.0).sum())
        if base_cap > 0:
            turn[i] = amt[i] * 1e4 / base_cap
        # 权重随收盘涨跌漂移（停牌票权重不动）
        denom = 1.0 + port
        w = w * (1.0 + r) / denom if denom > 0 else w

    return pd.DataFrame(
        {
            "date": px.dates,
            "ew_open": o_lvl,
            "ew_high": h_lvl,
            "ew_low": l_lvl,
            "ew_close": level,
            "ew_amount": amt,
            "ew_turnover": turn,
        }
    )


def build_universe_panel(
    px: PriceMatrices,
    st: np.ndarray,
    fmv: np.ndarray,
    universes: tuple[Universe, ...] = UNIVERSES,
) -> pd.DataFrame:
    """每个宇宙一行广度面板，拼成长表。"""
    ret = np.full_like(px.close, np.nan)
    ret[1:] = px.close[1:] / px.close[:-1] - 1.0
    valid = np.isfinite(ret) & np.isfinite(px.amount) & (px.amount > 0)
    cats = symbol_category(px.symbols)
    threshold = limit_threshold_matrix(px.dates, cats, st)

    frames: list[pd.DataFrame] = []
    for u in universes:
        static = board_mask(px.symbols, u.suffix, u.prefixes)
        if u.mode == "rank":
            mask = rank_mask(fmv, valid, static, top=u.top, start=u.start, end=u.end)
        else:
            mask = np.broadcast_to(static, valid.shape)
        panel = build_breadth_panel(px, st, univ=mask)
        base = valid & mask
        dist = distribution_counts(ret, threshold, base)
        agg = equal_weight_aggregate(px, mask, fmv)
        panel = pd.concat(
            [panel, pd.DataFrame(dist), agg.drop(columns=["date"])], axis=1
        )
        panel["universe"] = u.key
        # 分位与压力分在这里一次算完：全部是扩张窗口，因果；快照/API 只切片，
        # 请求路径零计算（否则回看模式每次要重算 8×11 个 O(n²) 分位）。
        prepared = prepare_panel(panel.set_index("date"))
        # 派生列回写面板：快照/API 只读这张表，不能再依赖现场 prepare_panel
        for col in ("limit_net", "amt_shock", "nh_nl_pct", "adl", "adl_slope"):
            panel[col] = prepared[col].to_numpy()
        feats = build_features(prepared)
        for ind in INDICATORS:
            panel[f"pct_{ind.name}"] = feats[ind.name].to_numpy()
        panel["stress"] = composite_scores(feats)["stress"].to_numpy()
        panel["stress_pct"] = expanding_pct_rank(
            pd.Series(panel["stress"].to_numpy()), 500
        ).to_numpy()
        panel["disp_pct"] = expanding_pct_rank(prepared["ret_disp"], 500).to_numpy()
        frames.append(panel)
        log.info(
            "宇宙 %-7s 末行 n=%d 压力=%.1f",
            u.key,
            int(panel["n_total"].iloc[-1]),
            float(panel["stress"].iloc[-1]),
        )
    return pd.concat(frames, ignore_index=True)


def load_index_ohlc(symbols: tuple[str, ...] = INDEX_OHLC_SYMBOLS) -> pd.DataFrame:
    """一次遍历 ``index_daily``，取展示指数日线（不 ffill，缺失保留）。"""
    days = _index_days(INDEX_DIR, None, None)
    rows = []
    for d in days:
        p = INDEX_DIR / f"dt={d}" / "data.parquet"
        if not p.exists():
            continue
        df = pq.read_table(
            p, columns=["symbol", "open", "high", "low", "close", "amount"]
        ).to_pandas()
        df = df[df["symbol"].isin(symbols)]
        if not df.empty:
            df = df.assign(date=pd.Timestamp(d))
            rows.append(df)
    if not rows:
        return pd.DataFrame(
            columns=["date", "symbol", "open", "high", "low", "close", "amount"]
        )
    out = pd.concat(rows, ignore_index=True)
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def _write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """临时文件 + ``os.replace``：容器（root）与宿主机（zbox）交替跑也能覆盖。

    直接 ``df.to_parquet(path)`` 会原地截断：先写的一方把属主改成自己，另一方
    从此写不进去；进程中途死掉还会留下半截 parquet 让 API 读到坏文件。
    原子写只需要目录写权限，两边都能替换。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def run(
    start: str | None = None, end: str | None = None, out_dir: Path = OUT_DIR
) -> dict:
    px = load_full_matrices(start, end)
    st = load_st_matrix(px.dates, px.symbols)
    fmv = _load_float_mv(px.dates, px.symbols)
    panel = build_universe_panel(px, st, fmv)
    out_dir.mkdir(parents=True, exist_ok=True)
    panel_path = out_dir / "by_universe.parquet"
    _write_parquet_atomic(panel, panel_path)
    ohlc = load_index_ohlc()
    ohlc_path = out_dir / "index_ohlc.parquet"
    _write_parquet_atomic(ohlc, ohlc_path)
    log.info(
        "写出 %s（%d 行 × %d 列，%d 个宇宙）与 %s（%d 行）",
        panel_path,
        len(panel),
        panel.shape[1],
        panel["universe"].nunique(),
        ohlc_path,
        len(ohlc),
    )
    return {"panel": str(panel_path), "ohlc": str(ohlc_path), "rows": len(panel)}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="分市场广度面板 + 全 A 合成 K 线")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--out", default=str(OUT_DIR))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args(argv)
    run(args.start, args.end, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
