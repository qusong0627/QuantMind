"""因子研究 —— 区间评分卡：月末名次面板 → 任意 N / 任意区间的回测与标签。

数据底座是 ``factor_panel.parquet``（每因子每期前 K 名，见 build_factor_research.py），
本模块全部在内存矩阵上计算（毫秒级），不落盘：

- ``Panel``              面板矩阵（因子 × 月份 × 名次 的 score/raw/fwd_ret/symbol）
- ``topn_series``        任意 N 的组合月收益（扣 0.2% 双边成本 × 换手）与净值
- ``kpi_of``             组合 KPI（年化/夏普/回撤/月胜率/Calmar，复用 analysis.kpi）
- ``nscan``              期末净值 vs 持仓数（top-1 ~ top-100 全扫描）
- ``tags``               环境标签（牛/熊/震荡/全天候）+ 时效标签（近 12 月 RankIC 对比）
- ``composite_scores``   综合分 = 0.5×有效性 z + 0.5×业绩 z（demo 口径）

口径（与 demo 对齐，全站统一）：
- 月末收盘调仓、Top-N 等权、双边成本 0.2%×换手、区间内重建净值（首月全额计费）；
- 基准：沪深300（超额同时给中证800/中证500）；
- 区间语义：区间内相邻月末的持有期收益逐月复利，区间首月末为净值 1.0。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.services.engine.factor_research import analysis
from backend.shared.benchmark import BENCHMARK_NAME, BENCHMARK_SYMBOL

COST_RATE = analysis.COST_RATE
BENCH_PRIMARY = BENCHMARK_SYMBOL
BENCH_ORDER = (BENCHMARK_SYMBOL, "000906.SH", "000905.SH")
BENCH_NAMES = {BENCHMARK_SYMBOL: BENCHMARK_NAME, "000906.SH": "中证800", "000905.SH": "中证500"}
MAX_SCAN_N = 100

# 环境标签阈值（demo 口径）
ENV_BULL, ENV_BEAR = 0.05, -0.05
TAG_TIME_DELTA = 0.012
TAG_TIME_WINDOW = 12
TAG_IDLE_IC = 0.01

CAP_BUCKETS = [
    (0, 50),
    (50, 100),
    (100, 300),
    (300, 1000),
    (1000, 3000),
    (3000, np.inf),
]
CAP_LABELS = ["<50亿", "50-100亿", "100-300亿", "300-1000亿", "1000-3000亿", ">3000亿"]


class Panel:
    """月末名次面板矩阵。fwd/score/raw 形状 (F, M, K)，K 固定补齐（缺位 NaN）。

    symbol 以整型编码存储（-1=空位）——1300+ 因子 × 1600 万行时字符串数组会吃 1GB+。
    取用时经 ``sym_at()/sym_codes()`` 映射回代码。
    """

    def __init__(self, df: pd.DataFrame) -> None:
        tds = pd.to_datetime(df["trade_date"]).to_numpy()  # datetime64[ns]
        dates = np.unique(tds)
        codes = sorted(str(c) for c in df["factor_code"].unique())
        ci = {c: i for i, c in enumerate(codes)}
        k = int(df["rank"].max())
        f, m = len(codes), len(dates)
        self.fwd = np.full((f, m, k), np.nan, dtype=np.float32)
        self.score = np.full((f, m, k), np.nan, dtype=np.float32)
        self.raw = np.full((f, m, k), np.nan, dtype=np.float32)
        self._sym_codes = np.full((f, m, k), -1, dtype=np.int32)
        fi = df["factor_code"].astype(str).map(ci).to_numpy(dtype=int)
        # 月份下标：`dates` 就是 `tds` 的取值集合且已排序，searchsorted 与「建字典再逐行查」
        # 逐元素等价。原写法是 Python 循环 + 字典查找，2754 因子 × 3226 万行实测 24.8s，
        # 且整个循环持 GIL —— 引擎健康检查正是被这一段饿死、进而被看门狗判「无响应」重启的。
        # searchsorted 实测 0.45s。
        mi = np.searchsorted(dates, tds)
        rk = df["rank"].to_numpy(dtype=int) - 1
        self.fwd[fi, mi, rk] = df["fwd_ret"].to_numpy(dtype=np.float32)
        self.score[fi, mi, rk] = df["score"].to_numpy(dtype=np.float32)
        self.raw[fi, mi, rk] = df["raw"].to_numpy(dtype=np.float32)
        cats = df["symbol"].astype(str).astype("category").cat
        self.symbols = [str(c) for c in cats.categories]
        self._sym_codes[fi, mi, rk] = cats.codes.to_numpy(dtype=np.int32)
        self.codes = codes
        self.dates = dates
        self.ci = ci

    def index(self, code: str) -> int:
        return self.ci[code]

    def sym_codes(self, fi: int, mi: int) -> np.ndarray:
        """第 (因子, 月) 的符号编码（-1=无效位）。"""
        return self._sym_codes[fi, mi]

    def sym_at(self, fi: int, mi: int, n: int | None = None) -> list[str]:
        """第 (因子, 月) 的前 n 名股票代码（跳过空位）。"""
        codes = self._sym_codes[fi, mi, :n]
        syms = self.symbols
        return [syms[c] for c in codes if c >= 0]


def month_mask(dates: np.ndarray, start: str | None, end: str | None) -> np.ndarray:
    """区间掩码。start/end 支持 'YYYY-MM-DD' / 'YYYY-MM' / None（开放端）。"""
    mask = np.ones(len(dates), dtype=bool)
    if start:
        ts = pd.Timestamp(start if len(str(start)) > 7 else f"{start}-01")
        mask &= dates >= np.datetime64(ts)
    if end:
        s = str(end)
        ts = pd.Timestamp(s if len(s) > 7 else f"{s}-01") + pd.offsets.MonthEnd(0)
        mask &= dates <= np.datetime64(ts)
    return mask


def _turnover(prev_syms: set, cur_syms: set, n: int) -> float:
    if not prev_syms:
        return 1.0
    return len(cur_syms - prev_syms) / max(len(cur_syms), 1)


def topn_runs(
    panel: Panel, fi: int, n: int, mask: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """区间内 Top-N 组合：返回 (dates, ret_net, turnover)。

    ret 长度 = 区间月数 − 1（逐对相邻月末）；首月换手 100% 全额计费。
    """
    idx = np.where(mask)[0]
    dates, rets, tos = [], [], []
    prev: set = set()
    for a, b in zip(idx[:-1], idx[1:], strict=False):
        fwd = panel.fwd[fi, a, :n]
        r = float(np.nanmean(fwd)) if np.isfinite(fwd).any() else np.nan
        cur = set(panel.sym_at(fi, a, n))
        to = _turnover(prev, cur, n)
        cost = to * COST_RATE
        rets.append(r - cost if pd.notna(r) else np.nan)
        tos.append(to)
        dates.append(panel.dates[b])
        prev = cur
    return np.asarray(dates), np.asarray(rets), np.asarray(tos)


def nav_from_rets(rets: np.ndarray) -> np.ndarray:
    """净值序列（首点 1.0，之后逐月复利）。"""
    nav = np.cumprod(1 + np.nan_to_num(rets, nan=0.0))
    return np.concatenate([[1.0], nav])


MIN_MONTHS = 6  # 回测最少月数：不足则不给年化/夏普等（避免 2 个月年化出假数）


def gate_kpi(kpi: dict, min_months: int = MIN_MONTHS) -> dict:
    """样本不足（n_months < min_months）时清空年化/夏普/回撤/胜率/Calmar。"""
    out = dict(kpi)
    if int(out.get("n_months") or 0) < min_months:
        for k in ("annual_return", "sharpe", "max_drawdown", "win_rate", "calmar"):
            out[k] = None
    return out


def kpi_of(rets: np.ndarray, nav: np.ndarray) -> dict:
    r = pd.Series(rets).dropna()
    if r.empty:
        return {
            "annual_return": None,
            "sharpe": None,
            "max_drawdown": None,
            "win_rate": None,
            "calmar": None,
            "n_months": 0,
        }
    return analysis.kpi(r, pd.Series(nav))


def topn_series(panel: Panel, fi: int, n: int, mask: np.ndarray) -> dict:
    """组合序列 + KPI（供区间内重建净值）。返回 dates/ret/nav/turnover/kpi。"""
    dates, rets, tos = topn_runs(panel, fi, n, mask)
    nav = nav_from_rets(rets)
    idx = np.where(mask)[0]
    nav_dates = panel.dates[idx[: len(nav)]]
    return {
        "dates": nav_dates,
        "ret": rets,
        "nav": nav,
        "turnover": tos,
        "kpi": kpi_of(rets, nav),
    }


def bench_series(
    bench: pd.DataFrame, mask: np.ndarray, dates: np.ndarray
) -> dict[str, dict]:
    """区间内各基准的净值（首点 1.0）与 KPI。bench: (trade_date, nav, index_code)。"""
    out: dict[str, dict] = {}
    for code in BENCH_ORDER:
        sub = bench[bench["index_code"] == code].set_index("trade_date")["nav"]
        if sub.empty:
            continue
        sub = sub.reindex(pd.DatetimeIndex(dates)).ffill().bfill()
        vals = sub.to_numpy(dtype=float)
        m = mask
        if not m.any():
            continue
        base = vals[m][0]
        if not np.isfinite(base) or base == 0:
            continue
        nav = vals[m] / base
        rets = nav[1:] / nav[:-1] - 1
        out[code] = {
            "dates": dates[m],
            "nav": nav,
            "kpi": kpi_of(rets, nav),
        }
    return out


def excess_vs(bench_kpis: dict[str, dict], kpi: dict) -> dict:
    """年化超额（组合年化 − 基准年化，demo 口径）。"""
    out = {}
    for code in BENCH_ORDER:
        b = bench_kpis.get(code, {}).get("kpi", {})
        a, c = kpi.get("annual_return"), b.get("annual_return")
        out[code] = round(a - c, 4) if a is not None and c is not None else None
    return out


def nscan(
    panel: Panel, fi: int, mask: np.ndarray, max_n: int = MAX_SCAN_N
) -> list[dict]:
    """期末净值 vs 持仓数：N=1..min(max_n, K) 的区间终值与年化。"""
    idx = np.where(mask)[0]
    n_months = max(len(idx) - 1, 1)
    kmax = min(max_n, panel.fwd.shape[2])
    rows = []
    for n in range(1, kmax + 1):
        _, rets, _ = topn_runs(panel, fi, n, mask)
        nav = nav_from_rets(rets)
        final = float(nav[-1])
        ann = final ** (12 / n_months) - 1 if final > 0 else -1.0
        rows.append(
            {"n": n, "final_nav": round(final, 4), "annual_return": round(ann, 4)}
        )
    return rows


_EMPTY_IC_STATS = {
    "ic_mean": None,
    "ic_std": None,
    "ic_ir": None,
    "ic_win_rate": None,
}


def ic_index(ic: pd.DataFrame) -> dict[str, pd.Series]:
    """factor_code →（trade_date → RankIC）索引，供批量场景用。

    `ic_stats` 每调一次就 `ic[ic["factor_code"] == code]` 把长表全扫一遍；私人库
    2746 因子 × 21.2 万行实测 22.2s —— 排行榜 26.7s 里的大头就是它。分组一次约 0.3s。
    """
    return {
        str(code): g.set_index("trade_date")["ic"]
        for code, g in ic.groupby("factor_code", sort=False)
    }


def ic_stats_from(series: pd.Series | None, mask_dates: np.ndarray) -> dict:
    """单因子区间 IC 统计；series 取自 `ic_index()`（None = 该因子无 IC）。"""
    if series is None or series.empty:
        return dict(_EMPTY_IC_STATS)
    s = series.reindex(pd.DatetimeIndex(mask_dates)).dropna()
    if s.empty:
        return dict(_EMPTY_IC_STATS)
    mean, std = float(s.mean()), float(s.std())
    return {
        "ic_mean": round(mean, 4),
        "ic_std": round(std, 4),
        "ic_ir": round(mean / std, 3) if std > 0 else None,
        "ic_win_rate": round(float((s > 0).mean()), 4),
    }


def ic_stats(ic: pd.DataFrame, code: str, mask_dates: np.ndarray) -> dict:
    """区间内 RankIC 统计（ic.parquet 长表）。

    单因子入口；批量（每因子一次）请先 `ic_index()` 再走 `ic_stats_from()`，
    否则每个因子都要全表扫一遍。
    """
    sub = ic[(ic["factor_code"] == code)]
    if sub.empty:
        return dict(_EMPTY_IC_STATS)
    return ic_stats_from(sub.set_index("trade_date")["ic"], mask_dates)


def env_tags(
    rets_by_factor: dict[str, np.ndarray],
    bench_ret: np.ndarray,
) -> dict[str, str]:
    """环境标签：滚动 3 月沪深300 分牛/熊/震荡，因子月均超额 z 取最高者。

    rets_by_factor: {code: 区间月收益数组}；bench_ret: 区间基准月收益（同长度）。
    """
    n = len(bench_ret)
    if n < 6:
        return dict.fromkeys(rets_by_factor, "全天候型")
    # 滚动 3 月基准累计收益 → 三态
    r3 = (
        pd.Series(bench_ret)
        .rolling(3, min_periods=3)
        .apply(lambda x: np.prod(1 + x) - 1)
    )
    regime = np.where(r3 > ENV_BULL, "bull", np.where(r3 < ENV_BEAR, "bear", "range"))
    valid = r3.notna().to_numpy()
    frames = {}
    for label, key in (
        ("牛市进攻型", "bull"),
        ("熊市防御型", "bear"),
        ("震荡占优型", "range"),
    ):
        m = valid & (regime == key)
        if m.sum() < 3:
            frames[label] = pd.Series(dtype=float)
            continue
        exc = {
            c: float(np.nanmean(r[m] - bench_ret[m])) for c, r in rets_by_factor.items()
        }
        s = pd.Series(exc)
        sd = s.std()
        frames[label] = (s - s.mean()) / sd if sd > 0 else s * 0
    z = pd.DataFrame(frames)
    out = {}
    for c in z.index:
        row = z.loc[c].dropna()
        if row.empty or float(row.max()) < 0.5:
            out[c] = "全天候型"
        else:
            mx = float(row.max())
            # 近似并列（1e-9 容差）按固定顺序 牛/熊/震荡 取先者，避免浮点末位随机
            out[c] = str(row.index[row >= mx - 1e-9][0])
    return out


def time_tags(
    ic: pd.DataFrame, codes: list[str], mask_dates: np.ndarray
) -> dict[str, str]:
    """时效标签：近 12 月 RankIC 均值 − 区间全样本 RankIC 均值（±0.012）。"""
    out = {}
    end = pd.DatetimeIndex(mask_dates).max()
    recent_start = end - pd.DateOffset(months=TAG_TIME_WINDOW - 1)
    pivot = ic[ic["factor_code"].isin(codes)].pivot_table(
        index="trade_date", columns="factor_code", values="ic", aggfunc="last"
    )
    pivot = pivot.reindex(pd.DatetimeIndex(mask_dates))
    recent = pivot[pivot.index >= recent_start]
    full_mean = pivot.mean()
    rec_mean = recent.mean()
    for c in codes:
        fm, rm = full_mean.get(c, np.nan), rec_mean.get(c, np.nan)
        if pd.isna(fm) or pd.isna(rm):
            out[c] = "长期稳定型"
            continue
        if abs(fm) < TAG_IDLE_IC and abs(rm) < TAG_IDLE_IC:
            out[c] = "持续低效"
        elif rm - fm > TAG_TIME_DELTA:
            out[c] = "近期转强"
        elif rm - fm < -TAG_TIME_DELTA:
            out[c] = "近期失效"
        else:
            out[c] = "长期稳定型"
    return out


def _z(s: pd.Series) -> pd.Series:
    sd = s.std()
    if not np.isfinite(sd) or sd == 0:
        return s * 0.0
    return (s - s.mean()) / sd


def composite_scores(df: pd.DataFrame) -> pd.DataFrame:
    """综合分：0.5×有效性 z(mean(z(IC), z(ICIR))) + 0.5×业绩 z(mean(z(年化),z(夏普),z(−回撤),z(月胜率)))。

    df 需含列 ic_mean/ic_ir/annual_return/sharpe/max_drawdown/win_rate（方向已统一为越大越好）。
    """
    eff = pd.concat([_z(df["ic_mean"]), _z(df["ic_ir"])], axis=1).mean(axis=1)
    perf = pd.concat(
        [
            _z(df["annual_return"]),
            _z(df["sharpe"]),
            _z(-df["max_drawdown"]),
            _z(df["win_rate"]),
        ],
        axis=1,
    ).mean(axis=1)
    out = df.copy()
    out["eff_z"] = eff.round(4)
    out["perf_z"] = perf.round(4)
    out["composite"] = (0.5 * eff.fillna(0) + 0.5 * perf.fillna(0)).round(4)
    return out


def cap_bucket(mv_yi: float | None) -> str | None:
    if mv_yi is None or not np.isfinite(mv_yi) or mv_yi <= 0:
        return None
    for (lo, hi), label in zip(CAP_BUCKETS, CAP_LABELS, strict=False):
        if lo <= mv_yi < hi:
            return label
    return None
