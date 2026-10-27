#!/usr/bin/env python3
"""TRIN（Arms Index）与市场广度日频面板 —— 数据源 QuantDB 本地 parquet。

口径（唯一事实源在本文件，改动必须过 ``backend/tests/test_trin_breadth.py``）：

1. 涨跌判定：后复权 ``close`` 的日收益 ``close_t / close_{t-1} - 1``。
   用后复权序列而非不复权，除权除息日不会产生伪跌（不复权会在分红日
   凭空多出一批"下跌家数"）。
2. TRIN = (上涨家数 / 下跌家数) / (上涨成交额 / 下跌成交额)
   用户给的 ``(上涨家数 × 下跌成交额) / (下跌家数 × 上涨成交额)`` 是同一式子。
   成交额单位为万元，比值口径与单位无关。
3. 细分口径（主口径与 ``n_up/n_down/adr/udr`` 同宇宙，保证可对账）：
   - ``trin_amt``    全 A（含北交所）成交额版，零剔除 —— 主口径
   - ``trin_amt_shsz`` 仅沪深，剔除 ST（PIT）与上市不足 60 个交易日的次新
   - ``trin_vol``    全 A 股数版（经典 Arms Index 定义）
4. 平滑：``trin_ema10``（经典用法）与 ``trin_sum10``（分子分母各累加 10 日
   再相除，比 EMA 更抗单日极端值）。
5. 涨停/跌停判定复用 ``local_market_data.limit_pct`` 的板别规则，不写死 9.8%
   —— 20%/30% 板与 ST 5% 板都要对。

输出：``data/quantdb/5_technical_derived/market_breadth/trin_daily.parquet``
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.research_stats import expanding_pct_rank  # noqa: F401  （转发导出，单测从此处取）
from backend.services.simulation.services.local_market_data import (
    LIMIT_TOLERANCE,
    LIMIT_TOLERANCE_BSE,
    limit_pct,
)

log = logging.getLogger("trin_breadth")

#: QuantDB 根目录（容器内默认 /data/quantdb，宿主默认 data/quantdb）
QUANTDB_DIR = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
BACKWARD_DIR = QUANTDB_DIR / "1_kline_data" / "daily_backward"
INDEX_DIR = QUANTDB_DIR / "1_kline_data" / "index_daily"
ST_PIT_PATH = QUANTDB_DIR / "2_base_sector" / "st_pit" / "st_pit.parquet"
OUT_DIR = QUANTDB_DIR / "5_technical_derived" / "market_breadth"

#: 板别编码 —— 顺序即 ``limit_pct`` 的分支顺序，改动要同步 ``_band_table``
CAT_MAIN = 0
CAT_GROWTH = 1
CAT_STAR = 2
CAT_BSE = 3

#: 每个板别的代表代码（``limit_pct`` 只看前缀与日期，代表代码足够）
_CAT_REPRESENTATIVE = {
    CAT_MAIN: "600000.SH",
    CAT_GROWTH: "300001.SZ",
    CAT_STAR: "688001.SH",
    CAT_BSE: "920001.BJ",
}

#: 指数：预测目标与控制变量
INDEX_TARGETS = ("000300.SH", "000001.SH", "000852.SH", "399006.SZ")
INDEX_NAMES = {
    "000300.SH": "沪深300",
    "000001.SH": "上证指数",
    "000852.SH": "中证1000",
    "399006.SZ": "创业板指",
    "899050.BJ": "北证50",
}


# --------------------------------------------------------------------------
# 纯函数（可单测）
# --------------------------------------------------------------------------
def safe_div(num: pd.Series | float, den: pd.Series | float) -> pd.Series | float:
    """除法，分母 <= 0 或非有限时返回 NaN —— 绝不产出 ±inf。

    TRIN 的分母是「下跌家数」和「下跌成交额」。极端普涨日下跌家数可能为 0，
    直接相除会得到 inf，后续 EMA/相关性全部污染。
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.asarray(num, dtype=float) / np.asarray(den, dtype=float)
    out = np.where(np.isfinite(out), out, np.nan)
    if np.isscalar(num) and np.isscalar(den):
        return float(out)
    index = num.index if isinstance(num, pd.Series) else None
    return pd.Series(out, index=index)


def trin(
    up_count: pd.Series,
    down_count: pd.Series,
    up_size: pd.Series,
    down_size: pd.Series,
) -> pd.Series:
    """Arms Index：``(上涨家数/下跌家数) / (上涨规模/下跌规模)``。

    ``size`` 可以是成交量（股）或成交额（元/万元），比值与单位无关。
    """
    count_ratio = safe_div(up_count, down_count)
    size_ratio = safe_div(up_size, down_size)
    return pd.Series(safe_div(count_ratio, size_ratio))


def ema(values: pd.Series, span: int) -> pd.Series:
    """指数移动平均。用 ``adjust=False`` 的递推口径（与技术分析软件一致）。"""
    return values.ewm(span=span, adjust=False, min_periods=span).mean()


def mcclellan_net(net: pd.Series, fast: int = 19, slow: int = 39) -> pd.Series:
    """McClellan 摆动指标 = EMA19(涨跌家数净差) − EMA39(涨跌家数净差)。

    ``net`` 是 ``上涨家数 − 下跌家数``。正值区间表示广度扩张。
    """
    return ema(net, fast) - ema(net, slow)





def rolling_sums(values: pd.Series, window: int) -> pd.Series:
    """滚动求和，窗口内任一值缺失则结果为缺失（不做前向填充）。"""
    return values.rolling(window, min_periods=window).sum()


# --------------------------------------------------------------------------
# 数据装载
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PriceMatrices:
    """日线宽表：行=交易日，列=标的。"""

    dates: pd.DatetimeIndex
    symbols: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray


def _day_dirs(base: Path, start: str | None, end: str | None) -> list[str]:
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


def load_price_matrices(
    start: str | None = None, end: str | None = None
) -> PriceMatrices:
    """把逐日 parquet 拼成 (D, S) 矩阵。一次遍历，内存约 3×D×S×8 字节。"""
    days = _day_dirs(BACKWARD_DIR, start, end)
    symbols: set[str] = set()
    for d in days:
        col = pq.read_table(
            BACKWARD_DIR / f"dt={d}" / "data.parquet", columns=["symbol"]
        ).column("symbol")
        symbols.update(col.to_pylist())
    syms = np.array(sorted(symbols))
    index = {s: i for i, s in enumerate(syms)}

    n_d, n_s = len(days), len(syms)
    close = np.full((n_d, n_s), np.nan, dtype=np.float64)
    volume = np.full((n_d, n_s), np.nan, dtype=np.float64)
    amount = np.full((n_d, n_s), np.nan, dtype=np.float64)

    for i, d in enumerate(days):
        df = pq.read_table(
            BACKWARD_DIR / f"dt={d}" / "data.parquet",
            columns=["symbol", "close", "volume", "amount"],
        ).to_pandas()
        j = df["symbol"].map(index).to_numpy()
        close[i, j] = df["close"].to_numpy()
        volume[i, j] = df["volume"].to_numpy()
        amount[i, j] = df["amount"].to_numpy()
    log.info("装载 %d 个交易日 × %d 个标的", n_d, n_s)
    return PriceMatrices(
        dates=pd.to_datetime([pd.Timestamp(d) for d in days]),
        symbols=syms,
        close=close,
        volume=volume,
        amount=amount,
    )


def load_st_matrix(
    dates: pd.DatetimeIndex, symbols: np.ndarray
) -> np.ndarray:
    """ST 状态 (D, S) bool。st_pit 只存 True 行，缺失即非 ST。"""
    if not ST_PIT_PATH.exists():
        log.warning("缺少 %s，ST 剔除口径退化为「不剔除」", ST_PIT_PATH)
        return np.zeros((len(dates), len(symbols)), dtype=bool)
    st = pq.read_table(ST_PIT_PATH).to_pandas()
    st["d"] = pd.to_datetime(st["time"], format="%Y%m%d")
    st = st[st["is_st"].astype(bool)]
    date_idx = {d: i for i, d in enumerate(dates)}
    sym_idx = {s: i for i, s in enumerate(symbols)}
    rows = st["d"].map(date_idx)
    cols = st["symbol"].map(sym_idx)
    ok = rows.notna() & cols.notna()
    out = np.zeros((len(dates), len(symbols)), dtype=bool)
    out[rows[ok].astype(int), cols[ok].astype(int)] = True
    return out


def symbol_category(symbols: np.ndarray) -> np.ndarray:
    """板别编码 —— 与 ``local_market_data._board_pct`` 同判据。"""
    cats = np.full(len(symbols), CAT_MAIN, dtype=np.int8)
    for i, s in enumerate(symbols):
        code = str(s).split(".")[0]
        if str(s).endswith(".BJ") or code[:2] in ("43", "83", "87", "88", "92"):
            cats[i] = CAT_BSE
        elif code[:3] in ("300", "301", "302"):
            cats[i] = CAT_GROWTH
        elif code[:3] == "688":
            cats[i] = CAT_STAR
    return cats


def limit_threshold_matrix(
    dates: pd.DatetimeIndex,
    cats: np.ndarray,
    st: np.ndarray,
) -> np.ndarray:
    """(D, S) 涨跌停判定阈值（比例，已扣板别容差）。

    逐日 × 板别 × ST 调 ``limit_pct``，再把结果铺到标的维度。这样 20%/30%
    板、ST 5% 板、创业板 2020-08-24 改革、2026-07-06 ST 放宽全部自动跟上，
    不需要在本文件重复一套规则。
    """
    bands = np.zeros((len(dates), 4 * 2), dtype=np.float64)
    for i, d in enumerate(dates):
        day = date(d.year, d.month, d.day)
        for cat, sym in _CAT_REPRESENTATIVE.items():
            for st_flag in (False, True):
                bands[i, cat + 4 * int(st_flag)] = float(
                    limit_pct(sym, is_st=st_flag, trade_date=day)
                )
    tol = np.where(cats == CAT_BSE, LIMIT_TOLERANCE_BSE, LIMIT_TOLERANCE)
    key = cats[None, :] + 4 * st
    return bands[np.arange(len(dates))[:, None], key] - tol[None, :]


# --------------------------------------------------------------------------
# 广度面板
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BreadthConfig:
    new_listing_days: int = 60
    ma_windows: tuple[int, ...] = (20, 60)


DEFAULT_CONFIG = BreadthConfig()


@dataclass(frozen=True)
class _Variant:
    """一个 TRIN 口径的掩码组合。"""

    name: str
    size_field: str  # 'amount' | 'volume'
    shsz_only: bool
    drop_st: bool
    drop_new: bool


VARIANTS: tuple[_Variant, ...] = (
    # 主口径：全 A 成交额版，零剔除 —— 与 n_up/n_down/adr/udr 同一宇宙
    _Variant("trin_amt", "amount", False, False, False),
    # 最干净口径：沪深 + 剔 ST + 剔次新
    _Variant("trin_amt_shsz", "amount", True, True, True),
    # 经典股数版
    _Variant("trin_vol", "volume", False, False, False),
)


def _masked_sum(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return np.where(mask, values, 0.0).sum(axis=1)


def build_breadth_panel(
    px: PriceMatrices,
    st: np.ndarray,
    config: BreadthConfig = DEFAULT_CONFIG,
) -> pd.DataFrame:
    """构建日频广度面板。所有指标只用当日及更早数据。"""
    ret = np.full_like(px.close, np.nan)
    ret[1:] = px.close[1:] / px.close[:-1] - 1.0

    valid = np.isfinite(ret) & np.isfinite(px.amount) & (px.amount > 0)
    up = valid & (ret > 0)
    down = valid & (ret < 0)
    flat = valid & (ret == 0)

    cats = symbol_category(px.symbols)
    is_bse = cats == CAT_BSE
    threshold = limit_threshold_matrix(px.dates, cats, st)
    limit_up = valid & (ret >= threshold)
    limit_down = valid & (ret <= -threshold)

    # 上市天数（按「有收盘价的天数」计，停牌不计age，避免停牌半年后仍算次新）
    age = np.cumsum(np.isfinite(px.close), axis=0) - 1
    seasoned = age >= config.new_listing_days

    out = pd.DataFrame({"date": px.dates})
    base = valid
    up_c = up.sum(axis=1)
    down_c = down.sum(axis=1)
    out["n_total"] = base.sum(axis=1)
    out["n_up"] = up_c
    out["n_down"] = down_c
    out["n_flat"] = flat.sum(axis=1)
    out["n_limit_up"] = limit_up.sum(axis=1)
    out["n_limit_down"] = limit_down.sum(axis=1)
    out["amt_total"] = _masked_sum(px.amount, base)
    out["amt_up"] = _masked_sum(px.amount, up)
    out["amt_down"] = _masked_sum(px.amount, down)
    out["vol_up"] = _masked_sum(px.volume, up)
    out["vol_down"] = _masked_sum(px.volume, down)
    # 等权收益：有效标的的均值。必须用「和 ÷ 个数」而不是 np.mean ——
    # np.mean 遇到任何 NaN 会整行为 NaN（这里每天都有停牌/次新，曾经整列全 NaN）。
    _cnt = base.sum(axis=1)
    _sum = np.where(base, ret, 0.0).sum(axis=1)
    _ew = np.divide(_sum, _cnt, out=np.full(len(_cnt), np.nan), where=_cnt > 0)
    out["ret_equal_weight"] = _ew * 100.0
    # 横截面扩散度（个股收益标准差，%）：市场是"齐涨齐跌"还是"分化"，
    # 是 A 股最有信息量的状态变量之一；只用当日有效标的，停牌不参与。
    _var = np.where(base, (ret - np.nan_to_num(_ew)[:, None]) ** 2, 0.0).sum(axis=1) / np.maximum(_cnt, 1)
    out["ret_disp"] = np.sqrt(_var) * 100.0

    for v in VARIANTS:
        mask = valid.copy()
        if v.shsz_only:
            mask &= ~is_bse
        if v.drop_st:
            mask &= ~st
        if v.drop_new:
            mask &= seasoned
        u, d = mask & (ret > 0), mask & (ret < 0)
        size = px.amount if v.size_field == "amount" else px.volume
        out[v.name] = trin(
            pd.Series(u.sum(axis=1)),
            pd.Series(d.sum(axis=1)),
            pd.Series(_masked_sum(size, u)),
            pd.Series(_masked_sum(size, d)),
        )

    # 平滑版主口径（trin_amt）
    out["trin_ema10"] = ema(out["trin_amt"], 10)
    mask_amt = valid.copy()
    u, d = mask_amt & (ret > 0), mask_amt & (ret < 0)
    up10 = rolling_sums(pd.Series(u.sum(axis=1), index=out.index), 10)
    dn10 = rolling_sums(pd.Series(d.sum(axis=1), index=out.index), 10)
    aup10 = rolling_sums(pd.Series(_masked_sum(px.amount, u)), 10)
    adn10 = rolling_sums(pd.Series(_masked_sum(px.amount, d)), 10)
    out["trin_sum10"] = trin(up10, dn10, aup10, adn10)

    # 辅助广度
    out["adr"] = safe_div(out["n_up"], out["n_down"])
    out["udr_amt"] = safe_div(out["amt_up"], out["amt_down"])
    net = out["n_up"] - out["n_down"]
    out["mcclellan"] = mcclellan_net(net)
    # 经典 ADL = 涨跌家数净差的累计和；再用 250 日均家数归一，便于跨年份比较
    out["adl"] = net.cumsum()
    out["adl_pct"] = out["adl"] / out["n_total"].rolling(250, min_periods=20).mean()
    out["net_advances"] = net

    close_df = pd.DataFrame(px.close)
    for w in config.ma_windows:
        ma = close_df.rolling(w, min_periods=w).mean().to_numpy()
        above = np.isfinite(ma) & (px.close > ma) & valid
        denom = np.isfinite(ma) & valid
        out[f"pct_above_ma{w}"] = np.where(
            denom.sum(axis=1) > 0,
            above.sum(axis=1) / np.maximum(denom.sum(axis=1), 1),
            np.nan,
        )

    # 52 周新高/新低：分母同样只用当日可比的标的
    hi = close_df.rolling(252, min_periods=252).max().to_numpy()
    lo = close_df.rolling(252, min_periods=252).min().to_numpy()
    has52 = np.isfinite(hi) & np.isfinite(lo) & valid
    out["n_high_52w"] = (has52 & (px.close >= hi - 1e-9)).sum(axis=1)
    out["n_low_52w"] = (has52 & (px.close <= lo + 1e-9)).sum(axis=1)
    out["n_52w_base"] = has52.sum(axis=1)

    return out


def load_index_close(symbol: str, dates: pd.DatetimeIndex) -> pd.Series:
    """指数收盘（按交易日对齐，缺失显式保留 NaN，不 ffill）。"""
    vals = {}
    for d in dates:
        tag = d.strftime("%Y%m%d")
        p = INDEX_DIR / f"dt={tag}" / "data.parquet"
        if not p.exists():
            continue
        df = pq.read_table(p, columns=["symbol", "close"]).to_pandas()
        row = df[df["symbol"] == symbol]
        if not row.empty:
            vals[d] = float(row["close"].iloc[0])
    return pd.Series(vals).reindex(dates)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="构建 TRIN / 市场广度日频面板")
    p.add_argument("--start", default=None, help="起始日 YYYY-MM-DD")
    p.add_argument("--end", default=None, help="结束日 YYYY-MM-DD")
    p.add_argument(
        "--out",
        default=str(OUT_DIR / "trin_daily.parquet"),
        help="输出 parquet 路径",
    )
    p.add_argument("--csv", action="store_true", help="同时写一份 CSV（utf-8-sig）")
    p.add_argument("--tail", type=int, default=0, help="构建后打印最近 N 行")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)

    px = load_price_matrices(args.start, args.end)
    st = load_st_matrix(px.dates, px.symbols)
    panel = build_breadth_panel(px, st)

    for sym in INDEX_TARGETS:
        panel[f"close_{sym}"] = load_index_close(sym, px.dates).to_numpy()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out, index=False)
    if args.csv:
        panel.to_csv(out.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    log.info("写出 %s（%d 行 × %d 列）", out, len(panel), panel.shape[1])

    if args.tail:
        cols = [
            "date", "n_up", "n_down", "n_limit_up", "n_limit_down",
            "amt_total", "trin_amt", "trin_amt_shsz", "trin_ema10",
            "adr", "udr_amt", "pct_above_ma20",
        ]
        with pd.option_context("display.width", 200, "display.max_columns", 30):
            print(panel[cols].tail(args.tail).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
