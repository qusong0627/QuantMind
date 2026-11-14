"""指数宇宙（universe）广度面板。

支持：
- ALL：全 A 等权
- 000001.SH：全部沪市 A 股（上证综指近似）
- 399001.SZ：全部深市 A 股（深证成指近似）
- 000300.SH / 000852.SH / 399006.SZ / 000905.SH / 000906.SH / 000016.SH / 000688.SH：
  使用 `2_base_sector/index_weights/<code>.parquet` 的当前成分股

口径：
- 广度/ADL/MCL/TRIN 全部只用**该宇宙成分股**的日线计算；
- 估值分位暂用全市场（指数内估值历史未接），响应里用 `valuation_scope="market"` 标注；
- 成分股权重是**当前快照**，不是 PIT；历史回看有幸存者偏差，仅用于当前状态判断。
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
if not ROOT.exists():
    ROOT = Path(__file__).resolve().parents[4] / "data" / "quantdb"
KLINE = ROOT / "1_kline_data" / "daily_backward"
UNADJ = ROOT / "1_kline_data" / "daily_unadjusted"
VAL = ROOT / "5_technical_derived" / "market_breadth" / "valuation_daily.parquet"
BREADTH = ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
VAL_DIR = ROOT / "5_technical_derived" / "valuation"
WEIGHTS = ROOT / "2_base_sector" / "index_weights"
SNAP1010 = (
    ROOT / "5_technical_derived" / "market_breadth" / "intraday_snapshot_1010.parquet"
)
L05 = ROOT.parent / "l05_snapshots"

UNIVERSES: tuple[tuple[str, str], ...] = (
    ("ALL", "全A等权"),
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000300.SH", "沪深300"),
    ("000852.SH", "中证1000"),
    ("000905.SH", "中证500"),
    ("000906.SH", "中证800"),
    ("000016.SH", "上证50"),
    ("000688.SH", "科创50"),
)
UNIVERSE_LABELS = dict(UNIVERSES)


def _tags() -> list[str]:
    return sorted(os.listdir(KLINE))


def _last_tag() -> str:
    return _tags()[-1]


@functools.lru_cache(maxsize=1)
def _all_symbols() -> frozenset[str]:
    t = pq.read_table(
        KLINE / _last_tag() / "data.parquet", columns=["symbol"]
    ).to_pandas()
    return frozenset(t["symbol"].tolist())


def universe_symbols(code: str, available: set[str] | None = None) -> set[str] | None:
    """返回该宇宙的成分股集合；ALL 返回 None（不筛）。"""
    if code == "ALL":
        return None
    avail = set(available) if available is not None else set(_all_symbols())
    if code == "000001.SH":
        return {s for s in avail if s.endswith(".SH") and s.startswith("6")}
    if code == "399001.SZ":
        return {
            s
            for s in avail
            if s.endswith(".SZ") and (s.startswith("0") or s.startswith("3"))
        }
    p = WEIGHTS / f"{code}.parquet"
    if not p.exists():
        return set()
    w = pd.read_parquet(p, columns=["Symbol"])["Symbol"].astype(str)
    return set(w) & avail


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def _roll_pct(s: pd.Series, window: int = 756, min_periods: int = 500) -> pd.Series:
    """滚动分位：窗口内 ≤ 当前值的占比（等价于原 rolling.apply，快 ~100 倍）。"""
    a = s.to_numpy(dtype=float)
    out = np.full(len(a), np.nan)
    for i in range(min_periods - 1, len(a)):
        if not np.isfinite(a[i]):
            continue
        w = a[max(0, i - window + 1) : i + 1]
        if np.isfinite(w).sum() >= min_periods:
            out[i] = float(np.mean(w <= a[i]))
    return pd.Series(out, index=s.index)


@functools.lru_cache(maxsize=20)
def universe_valuation(code: str, last_tag: str, tags_n: int = 800) -> pd.DataFrame:  # noqa: ARG001
    """该宇宙成分股的市值加权 PB/EP 历史与滚动 3 年便宜分位。

    ALL 直接用预计算的 `valuation_daily.parquet`（全市场）；
    指数宇宙从 `valuation/dt=*` 逐日过滤成分股重算。

    `tags_n=0` 表示读取全部历史（2×2 长窗口散点用），默认 800 日
    （滚动 3 年分位只需 756 日，主面板不必付全历史的读取成本）。
    """
    if code == "ALL":
        v = pd.read_parquet(VAL).sort_index()
        return v[["bp_capw_pct", "ep_capw_pct", "med_pe"]]

    tags = sorted(os.listdir(VAL_DIR))
    if tags_n > 0:
        tags = tags[-tags_n:]
    syms = universe_symbols(code)
    if not syms:
        return pd.DataFrame(columns=["bp_capw_pct", "ep_capw_pct", "med_pe"])
    paths = [str(VAL_DIR / tag / "data.parquet") for tag in tags]
    q = """
        SELECT CAST(
                   strptime(regexp_extract(filename, 'dt=([0-9]{8})', 1), '%Y%m%d')
                   AS DATE
               ) AS date,
               sum(equity) FILTER (WHERE equity > 0)
                   / sum(total_mv) FILTER (WHERE equity > 0) AS bp_capw,
               coalesce(sum(net_profit_ttm), 0) / sum(total_mv) AS ep_capw,
               median(pe_ttm) FILTER (WHERE pe_ttm > 0 AND pe_ttm < 500) AS med_pe
        FROM read_parquet(?, filename=true)
        WHERE total_mv > 0 AND list_contains(?, symbol)
        GROUP BY 1 ORDER BY 1
    """
    v = duckdb.connect().execute(q, [paths, sorted(syms)]).fetchdf()
    if v.empty:
        return pd.DataFrame(columns=["bp_capw_pct", "ep_capw_pct", "med_pe"])
    v["date"] = pd.to_datetime(v["date"])
    v = v.set_index("date").sort_index()
    for c in ("bp_capw", "ep_capw"):
        v[f"{c}_pct"] = _roll_pct(v[c])
    v["med_pe_cheap_pct"] = 1.0 - _roll_pct(v["med_pe"])
    return v[["bp_capw_pct", "ep_capw_pct", "med_pe"]]


@functools.lru_cache(maxsize=24)
def build_frame(code: str, last_tag: str) -> pd.DataFrame:  # noqa: ARG001
    """构建该宇宙最近 320 个交易日的广度 + 指数行情面板。"""
    from backend.scripts.market_stage_dashboard import load_index

    tags = _tags()[-561:]
    syms = universe_symbols(code)
    closes: dict[pd.Timestamp, pd.Series] = {}
    vols: dict[pd.Timestamp, pd.Series] = {}
    amts: dict[pd.Timestamp, pd.Series] = {}
    for tag in tags:
        t = pq.read_table(
            KLINE / tag / "data.parquet",
            columns=["symbol", "close", "volume", "amount"],
        ).to_pandas()
        if syms is not None:
            t = t[t["symbol"].isin(syms)]
        t = t.set_index("symbol")
        d = pd.Timestamp(tag.split("=")[1])
        closes[d] = t["close"]
        vols[d] = t["volume"]
        amts[d] = t["amount"]
    dates = sorted(closes)
    close = pd.DataFrame({d: closes[d] for d in dates}).T
    vol = pd.DataFrame({d: vols[d] for d in dates}).T
    amt = pd.DataFrame({d: amts[d] for d in dates}).T
    ret = close / close.shift(1) - 1.0
    n_up = (ret > 0).sum(axis=1)
    n_down = (ret < 0).sum(axis=1)
    n_total = ret.notna().sum(axis=1)
    above20 = (close > close.rolling(20, min_periods=20).mean()).sum(axis=1)
    above60 = (close > close.rolling(60, min_periods=60).mean()).sum(axis=1)
    vol_up = vol.where(ret > 0).sum(axis=1)
    vol_down = vol.where(ret < 0).sum(axis=1)
    amt_up = amt.where(ret > 0).sum(axis=1)
    amt_down = amt.where(ret < 0).sum(axis=1)

    out = pd.DataFrame(
        {
            "n_up": n_up,
            "n_down": n_down,
            "n_total": n_total,
            "pct_above_ma20": above20 / n_total.replace(0, np.nan),
            "pct_above_ma60": above60 / n_total.replace(0, np.nan),
        }
    )
    out["up_ratio"] = (
        out["n_up"] / (out["n_up"] + out["n_down"]).replace(0, np.nan) * 100
    )
    out["breadth_5d"] = out["up_ratio"].rolling(5).mean()
    out["adl"] = (out["n_up"] - out["n_down"]).cumsum()
    out["adl_slope20"] = out["adl"].diff(20)
    out["adl20"] = out["adl_slope20"]
    out["adl20_pct"] = out["adl_slope20"] / out["n_total"].replace(0, np.nan) * 100
    net = (out["n_up"] - out["n_down"]).astype(float)
    out["mcclellan"] = _ema(net, 19) - _ema(net, 39)
    out["mcl_chg5"] = out["mcclellan"] - out["mcclellan"].shift(5)
    out["trin_vol"] = (out["n_up"] / out["n_down"].replace(0, np.nan)) / (
        vol_up / vol_down.replace(0, np.nan)
    )
    out["trin_amt"] = (out["n_up"] / out["n_down"].replace(0, np.nan)) / (
        amt_up / amt_down.replace(0, np.nan)
    )
    out["ret_mean"] = ret.mean(axis=1)
    out["amt_total"] = amt.sum(axis=1)
    out["amt_up"] = amt_up
    out["amt_down"] = amt_down

    if code == "ALL":
        idx = (1.0 + out["ret_mean"].fillna(0.0)).cumprod() * 1000.0
    else:
        idx = load_index(code, 620).reindex(out.index)
    out["close_000001.SH"] = idx
    c = out["close_000001.SH"]
    out["ma250"] = c.rolling(250, min_periods=250).mean()
    out["vs_ma250"] = c / out["ma250"] - 1.0
    out["mom250"] = c / c.shift(250) - 1.0
    out["high250"] = c.rolling(250, min_periods=250).max()
    out["idx_hi"] = c >= out["high250"] - 1e-9
    out["adl_hi"] = out["adl"] >= out["adl"].rolling(250, min_periods=250).max() - 1e-9
    out["fwd250"] = c.shift(-250) / c - 1.0
    out["vol20"] = c.pct_change().rolling(20).std() * np.sqrt(244) * 100
    out["vol20_pct"] = (
        out["vol20"]
        .rolling(500, min_periods=250)
        .apply(lambda s: float((s.iloc[-1] >= s).mean()), raw=False)
    )
    v = universe_valuation(code, _last_tag())
    out = out.join(v[["bp_capw_pct", "ep_capw_pct", "med_pe"]], how="left")
    return out


@functools.lru_cache(maxsize=12)
def quadrant_long(code: str, last_tag: str) -> pd.DataFrame:  # noqa: ARG001
    """2×2 长窗口散点（2016 起）：距年线 × PB 便宜分位 × 未来 250 日收益。

    `build_frame` 只有 560 日，MA250 与未来 250 日同时有效的样本只剩约 60 天
    （隔日取样后 ~30 点），散点几乎看不出分布。这里单独用长历史：
    - ALL：广度面板的等权日收益复利成等权指数；
    - 指数：`index_daily` 的该指数收盘价；
    - 估值：该宇宙成分股市值加权 PB 的全历史滚动 3 年分位（当前成分快照）。
    """
    from backend.scripts.market_stage_dashboard import load_index

    if code == "ALL":
        b = pd.read_parquet(BREADTH).set_index("date").sort_index()
        ret = b["ret_equal_weight"].astype(float).fillna(0.0) / 100.0
        c = (1.0 + ret).cumprod() * 1000.0
    else:
        c = load_index(code, 3200)
    ma250 = c.rolling(250, min_periods=250).mean()
    out = pd.DataFrame(
        {
            "vs_ma250": c / ma250 - 1.0,
            "fwd250": c.shift(-250) / c - 1.0,
        }
    )
    v = universe_valuation(code, _last_tag(), 0)
    return out.join(v[["bp_capw_pct"]], how="left")


@functools.lru_cache(maxsize=32)
def _snap_day(date_str: str) -> pd.DataFrame:
    if not SNAP1010.exists():
        return pd.DataFrame(columns=["symbol", "px40"])
    q = f"""
        SELECT symbol, px40 FROM read_parquet('{SNAP1010}')
        WHERE CAST(date AS DATE) = DATE '{date_str}' AND px40 > 0
    """
    try:
        return duckdb.connect().execute(q).fetchdf()
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["symbol", "px40"])


def score_history(d: pd.DataFrame) -> pd.DataFrame:
    """六块评分（-2..+2 each）逐日向量化，供 block_history 与多指数对比。"""
    above = d["close_000001.SH"] > d["ma250"]
    mom_up = d["mom250"] > 0
    trend = np.where(above & mom_up, 2, np.where((~above) & (~mom_up), -2, 0))
    cheap = d["bp_capw_pct"]
    val = np.select(
        [cheap > 0.8, cheap > 0.5, cheap < 0.2, cheap < 0.5], [2, 1, -2, -1], default=0
    )
    above60 = d["pct_above_ma60"] * 100
    breadth = np.where(
        (above60 > 50) & (d["adl20_pct"] > 0),
        2,
        np.where((above60 < 30) & (d["adl20_pct"] < 0), -2, 0),
    )
    mcl = d["mcl_chg5"]
    mom = np.where(
        (d["up_ratio"] > 60) & (mcl > 0),
        2,
        np.where((d["up_ratio"] < 30) & (mcl < 0), -2, 0),
    )
    trin = d["trin_vol"]
    conc = np.where(trin > 2, 1, np.where(trin < 0.5, -1, 0))
    volp = d["vol20_pct"]
    vol = np.where(volp < 0.2, 1, np.where(volp > 0.8, -1, 0))
    out = pd.DataFrame(
        {
            "trend": trend,
            "valuation": val,
            "breadth_level": breadth,
            "breadth_momentum": mom,
            "concentration": conc,
            "volatility": vol,
        },
        index=d.index,
    )
    out["total"] = out.sum(axis=1)
    return out


@functools.lru_cache(maxsize=32)
def hot_set_universe(day: str, code: str) -> pd.DataFrame:
    """L0.5 热集快照过滤到指数成分股，返回各时点上涨占比。"""
    b = L05 / f"date={day.replace('-', '')}"
    if not b.exists():
        return pd.DataFrame(columns=["time", "breadth"])
    frames = []
    for f in os.listdir(b):
        if not f.endswith(".parquet"):
            continue
        t = pq.read_table(
            b / f, columns=["symbol", "refresh_time", "price", "pre_close"]
        ).to_pandas()
        frames.append(t)
    if not frames:
        return pd.DataFrame(columns=["time", "breadth"])
    df = pd.concat(frames, ignore_index=True)
    df["rt"] = df["refresh_time"].astype(str).str.zfill(6).astype(int)
    syms = universe_symbols(code, set(df["symbol"]))
    if syms is not None:
        df = df[df["symbol"].isin(syms)]
    rows = []
    for t in (94000, 101000, 103000, 113000, 140000, 150000):
        s = df[df["rt"] <= t].sort_values("rt").groupby("symbol").tail(1)
        s = s[(s["pre_close"] > 0) & (s["price"] > 0)]
        if s.empty:
            continue
        up = float((s["price"] > s["pre_close"]).mean() * 100)
        rows.append(
            {"time": f"{t // 10000:02d}:{t // 100 % 100:02d}", "breadth": round(up, 2)}
        )
    return pd.DataFrame(rows)


def intraday_universe(ts: pd.Timestamp, code: str, frame: pd.DataFrame) -> dict:
    """指数成分内的 10:10 广度；无快照返回 available=False。"""
    if code == "ALL":
        return {}
    snap = _snap_day(ts.strftime("%Y-%m-%d"))
    if snap.empty:
        hp = hot_set_universe(ts.strftime("%Y-%m-%d"), code)
        if not hp.empty:
            return {
                "available": True,
                "source": "L0.5 热集（成分股）",
                "times": hp["time"].tolist(),
                "breadth": hp["breadth"].tolist(),
                "mcl_chg5": [None] * len(hp),
                "note": "热集过滤到成分股；MCL 不可用，只看路径",
            }
        return {
            "available": False,
            "times": [],
            "breadth": [],
            "mcl_chg5": [],
            "note": "该日期无 10:10 快照，也无热集成分数据",
        }
    syms = universe_symbols(code, set(snap["symbol"]))
    if syms:
        snap = snap[snap["symbol"].isin(syms)]
    if snap.empty:
        return {
            "available": False,
            "times": [],
            "breadth": [],
            "mcl_chg5": [],
            "note": "该日期成分股无 10:10 数据",
        }
    tags = _tags()
    tag = f"dt={ts:%Y%m%d}"
    i = np.searchsorted(tags, tag, side="right") - 1
    if i < 1:
        return {
            "available": False,
            "times": [],
            "breadth": [],
            "mcl_chg5": [],
            "note": "无前一交易日数据",
        }
    prev = (
        pq.read_table(UNADJ / tags[i - 1] / "data.parquet", columns=["symbol", "close"])
        .to_pandas()
        .rename(columns={"close": "prev_close"})
    )
    m = snap.merge(prev, on="symbol", how="inner")
    m = m[m["prev_close"] > 0]
    if m.empty:
        return {
            "available": False,
            "times": [],
            "breadth": [],
            "mcl_chg5": [],
            "note": "无有效昨收",
        }
    ret = m["px40"] / m["prev_close"] - 1
    up = float((ret > 0).mean() * 100)
    mcl = None
    if ts in frame.index and np.isfinite(frame.loc[ts, "mcl_chg5"]):
        mcl = round(float(frame.loc[ts, "mcl_chg5"]), 2)
    return {
        "available": True,
        "source": "10:10 指数成分",
        "times": ["10:10"],
        "breadth": [round(up, 2)],
        "mcl_chg5": [mcl],
        "note": "仅 10:10；MCL 用该指数日频序列",
    }
