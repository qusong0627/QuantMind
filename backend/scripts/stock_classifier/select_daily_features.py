#!/usr/bin/env python3
"""日频 T+3/T+5 模型的特征选择（2016–2024 训练期，2025 验证，2026 测试）。

协议（用户 2026-11-10 拍板）：
- 两个模型：T+3、T+5；
- 统一用 T+5 选特征，T+1 做「隔天买入能力」硬门槛；
- 近期失效/持续低效只按 **截止 2024-12** 的 IC 判定（2025/2026 不参与选特征）；
- 去重 |ρ|≥0.9；类别配额均衡；L2 单列配额；正反方向都保留（树模型不依赖符号）；
- 训练 2016-07~2024-12，2025 验证，2026 测试。

流程：
1. 因子研究私有库目录（4585）→ 0.9 去重 → 截止 2024-12 时效标签 → 约 1387 候选；
2. 月度 ICIR 粗筛到 ~320（类别配额），控制日频读取量；
3. 每 10 个交易日取一个采样日（2016-07~2024-12），用 QuantDBFactorReader 跨库读日频值；
   全截面算 T+5/T+3/T+1 RankIC，按市值三档算 T+5 IC；
4. cap 平衡抽样训 LightGBM（T+5 截面 rank），取 gain + 打乱标签的 null gain；
5. 门槛（T+5 IC/ICIR、T+1 同号、cap 同号、覆盖、gain>null）× 类别配额 × 相关性终检 → 100/200；
6. 输出 JSON（逐条理由）+ CSV，供平台训练 job 使用。

用法（容器内，仓库根 /app）：
    python3 -m backend.scripts.stock_classifier.select_daily_features --smoke
    python3 -m backend.scripts.stock_classifier.select_daily_features \
        --coarse 320 --step 10 --top 100,200
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.stock_classifier.panel import (
    MIN_AMOUNT_WAN,
    is_limit_up,
    rankdata_nan,
)
from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
)
from backend.services.engine.factor_research import (
    dedup,
    discovery,
    scorecard,
    taxonomy,
)
from backend.shared.stock_utils import StockCodeUtil

log = logging.getLogger("select_daily_features")

QUANTDB = Path("/data/quantdb")
FR = QUANTDB / "factor_research_private"
OUT = Path("/data/stock_classifier/feature_selection")

TRAIN_START = "2016-07-01"
TRAIN_END = "2024-12-31"
VALID_START = "2025-01-01"
VALID_END = "2025-12-31"
TEST_START = "2026-01-01"
TEST_END = "2026-11-05"

MIN_MONTHS = 24
MIN_ABS_IC5 = 0.010
MIN_ABS_ICIR5 = 0.15
MIN_ABS_IC1 = 0.005
MIN_COVERAGE = 0.60
GAIN_NULL_MIN = 1.0
CORR_DEDUP = 0.90
SAMPLE_STEP = 10
PER_CAP_PER_DAY = 400
N_CAP = 3

CATEGORY_QUOTAS = {
    "量价/挖掘": 0.35,
    "基本面": 0.20,
    "资金流/情绪": 0.10,
    "L2/微观": 0.15,
    "风格/风险": 0.05,
    "其他": 0.15,
}
RAW_COLUMNS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "vwap",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "Amount",
    "VWAP",
}
LEAK_KEYWORDS = (
    "future_return",
    "return_1d",
    "return_3d",
    "return_5d",
    "return_10d",
    "return_20d",
    "return_60d",
    "fwd_ret",
    "label",
    "target",
    "next_ret",
)


def _prefix(symbol: str) -> str:
    return StockCodeUtil.to_prefix(str(symbol))


def _suffix(symbol: str) -> str:
    return StockCodeUtil.to_suffix(str(symbol))


def _rank_pct(values: np.ndarray) -> np.ndarray:
    r = rankdata_nan(values)
    ok = np.isfinite(r)
    out = np.full(len(r), np.nan)
    n = int(ok.sum())
    if n:
        out[ok] = (r[ok] - 0.5) / n
    return out


def _size_excess(y: np.ndarray, mv: np.ndarray, n_buckets: int = 3) -> np.ndarray:
    """按当期总市值分桶，减去同桶均值（size-neutral 标签）。"""
    values = np.asarray(y, dtype=float).copy()
    size = np.asarray(mv, dtype=float)
    ok = np.isfinite(values) & np.isfinite(size)
    if int(ok.sum()) < 3:
        return values
    edges = np.nanquantile(size[ok], [1.0 / n_buckets, 2.0 / n_buckets])
    bucket = np.digitize(size, edges)
    for k in range(n_buckets):
        mask = ok & (bucket == k)
        if mask.any():
            values[mask] = values[mask] - np.nanmean(values[mask])
    return values


def _ic_one(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if int(ok.sum()) < 100:
        return float("nan")
    xr = rankdata_nan(x[ok])
    yr = rankdata_nan(y[ok])
    xd = xr - xr.mean()
    yd = yr - yr.mean()
    den = math.sqrt(float((xd * xd).sum()) * float((yd * yd).sum()))
    return float((xd * yd).sum() / den) if den > 0 else float("nan")


def _ic_matrix(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """(n, f) → 每列对 y 的 RankIC（NaN 列 → NaN）。"""
    out = np.full(x.shape[1], np.nan)
    y_ok = np.isfinite(y)
    if int(y_ok.sum()) < 100:
        return out
    for j in range(x.shape[1]):
        col = x[:, j]
        ok = y_ok & np.isfinite(col)
        if int(ok.sum()) < 100:
            continue
        out[j] = _ic_one(col[ok], y[ok])
    return out


def _category_group(
    code: str, lib: str, l1: str, l2: str, cname: str, category_id: str
) -> str:
    """因子 → 配额分组。

    优先用因子研究内置分类（``taxonomy.category_for``，已按正则锚定），
    只有兜底 ``other`` 才走来源库/关键词启发式。不要用 ``"pe"``/``"pb"``
    这类两字母子串做关键词——``OPEN55``、``peak``、``type`` 都会被误判成
    基本面（2026-11-10 实测，OPEN29/OPEN55 被分进基本面配额）。
    """
    by_cat = {
        "micro": "L2/微观",
        "flow": "资金流/情绪",
        "concept": "资金流/情绪",
        "valuation": "基本面",
        "quality": "基本面",
        "growth": "基本面",
        "style": "风格/风险",
        "momentum": "量价/挖掘",
        "volatility": "量价/挖掘",
        "pattern": "量价/挖掘",
        "price_volume": "量价/挖掘",
        "alpha_classic": "量价/挖掘",
    }
    if category_id in by_cat:
        return by_cat[category_id]

    text = f"{code} {lib} {l1} {l2} {cname}".lower()
    code_l = code.lower()
    if lib in ("l2_factors", "factor_dict_minute") or any(
        k in text
        for k in ("l2", "逐笔", "盘口", "订单", "微观", "高频", "委买", "委卖")
    ):
        return "L2/微观"
    if lib == "jkp_ashare_daily":
        # JKP 会计类特征（账面市值比、净发行、股权发行等）归基本面；
        # 波动/动量/换手类归风格风险。其余按会计口径兜底基本面。
        if code_l.startswith(("ivol", "beta", "mom", "ret_", "turn")):
            return "风格/风险"
        return "基本面"
    if any(
        k in text
        for k in (
            "资金",
            "流向",
            "北向",
            "龙虎",
            "情绪",
            "新闻",
            "研报",
            "筹码",
            "融资",
            "融券",
        )
    ):
        return "资金流/情绪"
    if any(
        k in text
        for k in (
            "财务",
            "基本",
            "盈利",
            "营收",
            "成长",
            "估值",
            "质量",
            "roe",
            "市盈",
            "市净",
            "pe_ttm",
            "pb_ratio",
            "pead",
            "现金流",
            "毛利",
        )
    ):
        return "基本面"
    if any(
        k in text
        for k in ("风格", "风险", "beta", "市值", "行业", "中性", "波动率风险")
    ):
        return "风格/风险"
    if lib in (
        "rd_mined",
        "gap_mined",
        "cand_factors",
        "factor_dict_mined",
        "factor_dict_mined_full",
    ) or any(
        k in text
        for k in (
            "挖掘",
            "量价",
            "动量",
            "反转",
            "技术",
            "换手",
            "流动",
            "alpha",
            "plv",
        )
    ):
        return "量价/挖掘"
    return "其他"


def _load_catalog() -> pd.DataFrame:
    cat = json.loads((FR / "factors.json").read_text(encoding="utf-8"))
    factors = cat.get("factors", [])
    ic = pd.read_parquet(FR / "ic.parquet")
    ic["trade_date"] = pd.to_datetime(ic["trade_date"])
    ic = ic[ic["trade_date"] <= pd.Timestamp(TRAIN_END)]
    pivot = ic.pivot_table(
        index="trade_date", columns="factor_code", values="ic", aggfunc="last"
    )
    dates = pd.DatetimeIndex(pivot.index)
    mean_ic = pivot.mean()
    std_ic = pivot.std()
    icir = mean_ic / std_ic.replace(0, np.nan)
    coverage = pivot.notna().sum() / max(len(dates), 1)

    codes = [str(f["code"]) for f in factors]
    z = np.load(FR / "cross_corr.npz", allow_pickle=False)
    names = [str(x) for x in z["names"]]
    matrix = np.asarray(z["matrix"])
    # 先去重再算时效：去重序用月度 |ICIR|，把更有解释力的代表留在前面
    order = sorted(
        codes,
        key=lambda c: (
            -abs(float(icir.get(c, np.nan))) if np.isfinite(icir.get(c, np.nan)) else 0
        ),
    )
    kept, dropped, duplicate_of = dedup.greedy_dedup(order, names, matrix, CORR_DEDUP)
    tags = scorecard.time_tags(ic, kept, dates.to_numpy())

    rows = []
    for f in factors:
        code = str(f["code"])
        if code not in set(kept):
            continue
        lib = discovery.source_of(str(f.get("wind_source", "")))
        if not lib or lib in ("features_daily",):
            continue
        if code in RAW_COLUMNS or any(k in code.lower() for k in LEAK_KEYWORDS):
            continue
        meta = taxonomy.enrich(dict(f))
        l1, l2 = str(meta.get("l1") or ""), str(meta.get("l2") or "")
        cname = str(meta.get("category_name") or "")
        mi, cov = float(mean_ic.get(code, np.nan)), float(coverage.get(code, 0))
        rows.append(
            {
                "code": code,
                "library": lib,
                "l1": l1,
                "l2": l2,
                "category_name": cname,
                "category_id": str(meta.get("category") or ""),
                "group": _category_group(
                    code, lib, l1, l2, cname, str(meta.get("category") or "")
                ),
                "monthly_ic": mi,
                "monthly_icir": float(icir.get(code, np.nan)),
                "monthly_coverage": cov,
                "time_tag": tags.get(code, "长期稳定型"),
                "duplicate_of": duplicate_of.get(code, ""),
            }
        )
    df = pd.DataFrame(rows)
    df = df[~df["time_tag"].isin(("近期失效", "持续低效"))]
    df = df[df["monthly_coverage"] >= 0.35]
    df = df[np.isfinite(df["monthly_icir"])]
    log.info("候选：去重 %d → 过滤后 %d（时效/覆盖/原始列）", len(kept), len(df))
    return df


def _coarse_select(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """类别配额粗筛：每组按 |月度 ICIR| 取配额×1.3，余量回填。"""
    picked: list[str] = []
    for group, quota in CATEGORY_QUOTAS.items():
        sub = df[df["group"] == group].sort_values(
            "monthly_icir", key=lambda s: s.abs(), ascending=False
        )
        take = max(1, int(math.ceil(n * quota)))
        picked.extend(sub["code"].head(take).tolist())
    picked = list(dict.fromkeys(picked))
    if len(picked) < n:
        rest = df[~df["code"].isin(picked)].sort_values(
            "monthly_icir", key=lambda s: s.abs(), ascending=False
        )
        picked.extend(rest["code"].head(n - len(picked)).tolist())
    out = df[df["code"].isin(picked[:n])].copy()
    log.info("粗筛 %d 个：%s", len(out), dict(Counter(out["group"])))
    return out


def _load_price_wide() -> tuple[pd.DataFrame, pd.DataFrame]:
    """daily_backward 收盘/开盘宽表（前复权，date × prefix symbol）。"""
    import duckdb

    root = QUANTDB / "1_kline_data" / "daily_backward"
    con = duckdb.connect(config={"memory_limit": "8GB", "threads": "8"})
    try:
        df = con.execute(
            "SELECT symbol, time, close, open FROM read_parquet(?, hive_partitioning=true, union_by_name=true)",
            [str(root / "dt=*" / "data.parquet")],
        ).df()
    finally:
        con.close()
    df["symbol"] = df["symbol"].map(_prefix)
    df["time"] = pd.to_datetime(df["time"].astype(str), errors="coerce")
    df = df.dropna(subset=["time", "symbol"])
    close = df.pivot_table(
        index="time", columns="symbol", values="close", aggfunc="last"
    ).astype("float32")
    open_ = df.pivot_table(
        index="time", columns="symbol", values="open", aggfunc="last"
    ).astype("float32")
    close = close.sort_index()
    open_ = open_.reindex(index=close.index, columns=close.columns)
    log.info("行情宽表 %s", close.shape)
    return close, open_


def _load_st() -> pd.DataFrame:
    st = pd.read_parquet(QUANTDB / "2_base_sector" / "st_pit" / "st_pit.parquet")
    st["symbol"] = st["symbol"].map(_prefix)
    st["time"] = pd.to_datetime(
        st["time"].astype(str), format="%Y%m%d", errors="coerce"
    )
    return st.dropna(subset=["time", "symbol"])


def _load_total_mv(date: pd.Timestamp) -> pd.Series:
    p = (
        QUANTDB
        / "5_technical_derived"
        / "valuation"
        / f"dt={date.strftime('%Y%m%d')}"
        / "data.parquet"
    )
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_parquet(p, columns=["symbol", "total_mv"])
    df["symbol"] = df["symbol"].map(_prefix)
    return df.drop_duplicates("symbol").set_index("symbol")["total_mv"].astype(float)


def _sample_dates(
    close: pd.DataFrame, step: int, start: str, end: str
) -> list[pd.Timestamp]:
    idx = close.index
    sel = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]
    dates = list(sel[::step])
    if dates and dates[-1] > pd.Timestamp(end):
        dates = dates[:-1]
    return dates


def _limit_mask(frame: pd.DataFrame, close: pd.DataFrame) -> np.ndarray:
    t = frame["trade_date"].iloc[0]
    i = close.index.get_loc(t)
    if i <= 0:
        return np.zeros(len(frame), dtype=bool)
    prev = close.iloc[i - 1].reindex(frame["symbol"]).to_numpy(float)
    cur = close.iloc[i].reindex(frame["symbol"]).to_numpy(float)
    ret = cur / prev - 1.0
    return is_limit_up(frame["symbol"], pd.Series(ret))


def _lgb_fit(
    x: np.ndarray,
    y: np.ndarray,
    w: np.ndarray,
    xv: np.ndarray,
    yv: np.ndarray,
    feature_names: list[str],
    seed: int = 42,
    rounds: int = 1200,
):
    import lightgbm as lgb

    model = lgb.LGBMRegressor(
        objective="regression",
        metric="rmse",
        learning_rate=0.04,
        num_leaves=31,
        max_depth=8,
        min_data_in_leaf=500,
        feature_fraction=0.45,
        bagging_fraction=0.8,
        bagging_freq=1,
        lambda_l2=20.0,
        max_bin=127,
        n_estimators=rounds,
        num_threads=20,
        verbosity=-1,
        seed=seed,
        deterministic=True,
        force_row_wise=True,
    )
    model.fit(
        x,
        y,
        sample_weight=w,
        eval_set=[(xv, yv)],
        eval_metric="rmse",
        callbacks=[lgb.early_stopping(100, verbose=False)],
        feature_name=feature_names,
    )
    return model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--coarse", type=int, default=320)
    ap.add_argument("--step", type=int, default=SAMPLE_STEP)
    ap.add_argument(
        "--label-mode",
        default="raw",
        choices=["raw", "size_excess"],
        help="raw=v1 原始远期收益；size_excess=同日市值三桶内超额（H1）",
    )
    ap.add_argument("--top", default="100,200")
    ap.add_argument("--out", default=str(OUT / "daily_feature_selection.json"))
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    t0 = time.time()
    tops = [int(x) for x in str(args.top).split(",") if x.strip()]
    n_coarse = 60 if args.smoke else args.coarse
    step = max(20, args.step * 5) if args.smoke else args.step

    catalog = _load_catalog()
    coarse = _coarse_select(catalog, n_coarse)
    features = coarse["code"].tolist()
    sources = {r.code: f"{r.library}:{r.code}" for r in coarse.itertuples()}
    reader = QuantDBFactorReader(market="CN")
    # 按各库最新 schema 校验列存在性；缺列的因子直接剔除（不等整日读取时炸）
    keep: list[str] = []
    desc_cache: dict[str, set[str]] = {}
    for f in features:
        lib = sources[f].split(":", 1)[0]
        try:
            if lib not in desc_cache:
                desc_cache[lib] = set(reader.describe(lib).columns)
            cols = desc_cache[lib]
        except Exception:  # noqa: BLE001
            continue
        if f in cols:
            keep.append(f)
    features = keep
    sources = {f: sources[f] for f in features}
    coarse = coarse[coarse["code"].isin(features)].reset_index(drop=True)
    log.info("schema 校验后 %d 个（%s）", len(features), dict(Counter(coarse["group"])))

    close, open_ = _load_price_wide()
    st = _load_st()
    st_by_date = {d: set(g["symbol"]) for d, g in st.groupby("time")}
    listed_ok = close.notna().rolling(60, min_periods=60).sum() >= 60
    dates = _sample_dates(close, step, TRAIN_START, TRAIN_END)
    log.info(
        "采样日 %d（%s ~ %s，step=%d）",
        len(dates),
        dates[0].date(),
        dates[-1].date(),
        step,
    )

    ic5: list[np.ndarray] = []
    ic3: list[np.ndarray] = []
    ic1: list[np.ndarray] = []
    ic1cc: list[np.ndarray] = []
    cap_ic: list[np.ndarray] = []  # (n_caps, f)
    sample_x: list[np.ndarray] = []
    sample_y: list[np.ndarray] = []
    sample_w: list[np.ndarray] = []
    sample_dates: list[np.datetime64] = []
    coverage_hits = np.zeros(len(features), dtype=np.int64)
    seen = 0
    rng = np.random.default_rng(7)
    for di, date in enumerate(dates, 1):
        i = close.index.get_loc(date)
        if i + 6 >= len(close.index):
            continue
        try:
            frame = reader.read_day(
                "l1_factors",
                features=features,
                feature_sources=sources,
                trade_date=date.strftime("%Y-%m-%d"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 读取失败跳过: %s", date.date(), exc)
            continue
        if frame.empty:
            continue
        frame["symbol"] = frame["symbol"].astype(str)
        missing = [f for f in features if f not in frame.columns]
        if missing:
            for f in missing:
                frame[f] = np.nan
        syms = frame["symbol"].to_numpy()
        vals = frame[features].to_numpy(dtype=np.float32)
        close_t = close.iloc[i].reindex(syms).to_numpy(float)
        amount = (
            frame["amount"].to_numpy(float)
            if "amount" in frame
            else np.full(len(frame), np.nan)
        )
        mv = _load_total_mv(date).reindex(syms).to_numpy(float)
        st_set = st_by_date.get(date, set())
        listed = listed_ok.iloc[i].reindex(syms).to_numpy(bool)
        limit = _limit_mask(frame, close)
        ok = (
            np.isfinite(close_t)
            & (close_t > 0)
            & np.isfinite(amount)
            & (amount >= MIN_AMOUNT_WAN)
            & listed
            & np.isfinite(mv)
            & ~limit
            & np.array([s not in st_set for s in syms])
        )
        if int(ok.sum()) < 300:
            continue
        idx5 = close.index[i + 5]
        idx3 = close.index[i + 3]
        idx1 = close.index[i + 1]
        o1 = open_.loc[idx1].reindex(syms).to_numpy(float)
        c1 = close.loc[idx1].reindex(syms).to_numpy(float)
        c3 = close.loc[idx3].reindex(syms).to_numpy(float)
        c5 = close.loc[idx5].reindex(syms).to_numpy(float)
        y1 = c1 / o1 - 1.0  # 次日开盘买入、次日收盘卖出的可交易收益
        y1cc = c1 / close_t - 1.0  # 次日收盘对当日收盘（含隔夜）
        y3 = c3 / o1 - 1.0
        y5 = c5 / o1 - 1.0
        sub = ok & np.isfinite(y5)
        if int(sub.sum()) < 300:
            continue
        x = vals[sub]
        coverage_hits += np.isfinite(x).sum(axis=0)
        seen += int(sub.sum())
        mv_sub = mv[sub]
        if args.label_mode == "size_excess":
            y5v = _size_excess(y5[sub], mv_sub)
            y3v = _size_excess(y3[sub], mv_sub)
            y1v = _size_excess(y1[sub], mv_sub)
            y1ccv = _size_excess(y1cc[sub], mv_sub)
        else:
            y5v, y3v, y1v, y1ccv = y5[sub], y3[sub], y1[sub], y1cc[sub]
        ic5.append(_ic_matrix(x, y5v))
        ic3.append(_ic_matrix(x, y3v))
        ic1.append(_ic_matrix(x, y1v))
        ic1cc.append(_ic_matrix(x, y1ccv))
        # 市值三档
        edges = np.nanquantile(mv_sub, [1 / 3, 2 / 3])
        row = np.full((N_CAP, len(features)), np.nan, dtype=np.float32)
        for k in range(N_CAP):
            mk = (
                (mv_sub <= edges[0])
                if k == 0
                else (mv_sub > edges[0]) & (mv_sub <= edges[1])
                if k == 1
                else (mv_sub > edges[1])
            )
            if int(mk.sum()) >= 100:
                row[k] = _ic_matrix(x[mk], y5v[mk])
        cap_ic.append(row)
        # cap 平衡抽样（LGB 只学截面 rank，不做全量）
        yr = _rank_pct(y5v)
        for k in range(N_CAP):
            mk = (
                (mv_sub <= edges[0])
                if k == 0
                else (mv_sub > edges[0]) & (mv_sub <= edges[1])
                if k == 1
                else (mv_sub > edges[1])
            )
            pos = np.flatnonzero(mk & np.isfinite(yr))
            take = min(PER_CAP_PER_DAY, len(pos))
            if take:
                choice = rng.choice(pos, size=take, replace=False)
                sample_x.append(x[choice])
                sample_y.append(yr[choice])
                sample_w.append(np.full(take, 1.0 / N_CAP, dtype=np.float32))
                sample_dates.extend([date] * take)
        if di % 20 == 0:
            log.info("采样 %d/%d 日（%.0fs）", di, len(dates), time.time() - t0)
    if not ic5:
        log.error("没有可用采样日")
        return 1
    ic5a, ic3a, ic1a, ic1cca = map(np.vstack, (ic5, ic3, ic1, ic1cc))
    capa = np.stack(cap_ic, axis=0)  # (n_dates, n_caps, f)；vstack 会把 cap 轴压成行
    mean5, mean3, mean1, mean1cc = (
        np.nanmean(ic5a, axis=0),
        np.nanmean(ic3a, axis=0),
        np.nanmean(ic1a, axis=0),
        np.nanmean(ic1cca, axis=0),
    )
    std5 = np.nanstd(ic5a, axis=0)
    icir5 = np.divide(mean5, std5, out=np.zeros_like(mean5), where=std5 > 0)
    pos5 = np.nanmean(ic5a > 0, axis=0)
    cap_mean = np.nanmean(capa, axis=0)  # (n_caps, f)

    x_all = np.vstack(sample_x)
    y_all = np.concatenate(sample_y)
    w_all = np.concatenate(sample_w)
    d_all = np.array(sample_dates, dtype="datetime64[D]")
    del sample_x, sample_y, sample_w, sample_dates
    log.info(
        "LGB 样本 %s，特征 %d，日期 %s ~ %s",
        x_all.shape,
        len(features),
        d_all.min(),
        d_all.max(),
    )
    log.info(
        "日期样本 %s", pd.Series(d_all).value_counts().sort_index().tail(6).to_dict()
    )
    train_mask = d_all <= np.datetime64("2023-12-31")
    val_mask = (d_all >= np.datetime64("2024-01-01")) & (
        d_all <= np.datetime64("2024-12-31")
    )
    min_rows = 100 if args.smoke else 1000
    log.info("train_rows=%d val_rows=%d", int(train_mask.sum()), int(val_mask.sum()))
    if int(train_mask.sum()) < min_rows or int(val_mask.sum()) < min_rows:
        log.error("LGB 训练/验证样本不足")
        return 1
    model = _lgb_fit(
        x_all[train_mask],
        y_all[train_mask],
        w_all[train_mask],
        x_all[val_mask],
        y_all[val_mask],
        features,
    )
    gain = model.booster_.feature_importance("gain").astype(float)
    # null importance：截面内打乱标签，同参数短训练
    y_null = y_all.copy()
    df_null = pd.DataFrame({"d": d_all, "y": y_null})
    for _, g in df_null.groupby("d"):
        y_null[g.index] = rng.permutation(g["y"].to_numpy())
    null_model = _lgb_fit(
        x_all[train_mask],
        y_null[train_mask],
        w_all[train_mask],
        x_all[val_mask],
        y_null[val_mask],
        features,
        seed=99,
        rounds=400,
    )
    null_gain = null_model.booster_.feature_importance("gain").astype(float)
    gain_ratio = gain / np.maximum(null_gain, 1e-9)

    metrics = coarse.set_index("code").reindex(features).reset_index()
    metrics["ic5"] = mean5
    metrics["ic3"] = mean3
    metrics["ic1"] = mean1
    metrics["ic1_cc"] = mean1cc
    metrics["icir5"] = icir5
    metrics["pos5"] = pos5
    metrics["coverage"] = coverage_hits / max(seen, 1)
    metrics["cap_ic_small"] = cap_mean[0]
    metrics["cap_ic_mid"] = cap_mean[1]
    metrics["cap_ic_large"] = cap_mean[2]
    metrics["cap_sign_agree"] = (
        (np.sign(cap_mean) == np.sign(mean5)[None, :]) & np.isfinite(cap_mean)
    ).sum(axis=0)
    metrics["lgb_gain"] = gain
    metrics["null_gain"] = null_gain
    metrics["gain_ratio"] = gain_ratio
    metrics["gain_rank"] = pd.Series(gain).rank(ascending=False).to_numpy()

    def _t1_ok(row) -> bool:
        s5 = np.sign(row.ic5)
        return bool(
            (np.sign(row.ic1) == s5 and abs(row.ic1) >= MIN_ABS_IC1)
            or (np.sign(row.ic1_cc) == s5 and abs(row.ic1_cc) >= MIN_ABS_IC1)
        )

    metrics["t1_same_sign"] = metrics.apply(_t1_ok, axis=1)

    # 门槛
    gate = (
        (metrics["coverage"] >= MIN_COVERAGE)
        & (metrics["ic5"].abs() >= MIN_ABS_IC5)
        & (metrics["icir5"].abs() >= MIN_ABS_ICIR5)
        & (metrics["t1_same_sign"])
        & (metrics["cap_sign_agree"] >= 2)
        & (metrics["gain_ratio"] >= GAIN_NULL_MIN)
    )
    passed = metrics[gate].copy()
    log.info(
        "门槛通过 %d/%d（t1 %d, cap %d, gain %d）",
        len(passed),
        len(metrics),
        int(metrics["t1_same_sign"].sum()),
        int((metrics["cap_sign_agree"] >= 2).sum()),
        int((metrics["gain_ratio"] >= GAIN_NULL_MIN).sum()),
    )
    log.info(
        "ICIR top:\n%s",
        metrics.reindex(metrics["icir5"].abs().sort_values(ascending=False).index)[
            [
                "code",
                "group",
                "ic5",
                "icir5",
                "ic1",
                "ic1_cc",
                "coverage",
                "cap_sign_agree",
                "cap_ic_small",
                "cap_ic_mid",
                "cap_ic_large",
                "gain_ratio",
            ]
        ]
        .head(10)
        .to_string(index=False),
    )
    if passed.empty:
        log.error("没有因子通过门槛")
        return 1

    # 相关性终检（用 LGB 样本的 20k 行）
    corr_rows = min(20000, len(x_all))
    corr_idx = rng.choice(len(x_all), size=corr_rows, replace=False)
    corr = pd.DataFrame(x_all[corr_idx], columns=features).corr().abs().to_numpy()

    def _score(row) -> float:
        return (
            0.45 * abs(float(row.icir5))
            + 0.25 * math.log1p(max(float(row.gain_ratio), 0.0))
            + 0.20 * abs(float(row.ic1)) * 20
            + 0.10 * float(row.cap_sign_agree)
        )

    passed["score"] = passed.apply(_score, axis=1)
    passed = passed.sort_values("score", ascending=False)

    def _ratio_text(row) -> str:
        if abs(float(row.null_gain)) < 1e-6:
            return "null≈0"
        return f"{row.gain_ratio:.1f}×"

    selections: dict[str, list[dict]] = {}
    for top_n in tops:
        quotas = {
            g: max(1, int(math.ceil(top_n * q))) for g, q in CATEGORY_QUOTAS.items()
        }
        chosen: list[str] = []
        reasons: dict[str, str] = {}
        for group, quota in quotas.items():
            sub = passed[passed["group"] == group]
            for row in sub.itertuples():
                if (
                    len(
                        [
                            c
                            for c in chosen
                            if passed.set_index("code").loc[c, "group"] == group
                        ]
                    )
                    >= quota
                ):
                    break
                if row.code in chosen:
                    continue
                j = features.index(row.code)
                dup = next(
                    (
                        c
                        for c in chosen
                        if abs(corr[j, features.index(c)]) >= CORR_DEDUP
                    ),
                    None,
                )
                if dup is not None:
                    continue
                chosen.append(row.code)
                reasons[row.code] = (
                    f"{row.group}｜{row.library}｜T+5 ICIR {row.icir5:+.2f}"
                    f"（|IC| {abs(row.ic5):.3f}，同号率 {row.pos5:.0%}）"
                    f"｜T+1 IC {row.ic1:+.3f}/{row.ic1_cc:+.3f} 同号｜cap 三档 {int(row.cap_sign_agree)}/3 同号"
                    f"｜LGB gain 第 {int(row.gain_rank)}（{_ratio_text(row)}）"
                    f"｜覆盖 {row.coverage:.0%}"
                )
        # 配额不足/超额：按全表补足/裁剪
        if len(chosen) < top_n:
            for row in passed.itertuples():
                if len(chosen) >= top_n:
                    break
                if row.code in chosen:
                    continue
                j = features.index(row.code)
                if any(abs(corr[j, features.index(c)]) >= CORR_DEDUP for c in chosen):
                    continue
                chosen.append(row.code)
                reasons[row.code] = (
                    f"{row.group}｜{row.library}｜T+5 ICIR {row.icir5:+.2f}｜T+1 IC {row.ic1:+.3f}/{row.ic1_cc:+.3f}｜"
                    f"cap {int(row.cap_sign_agree)}/3｜gain 第 {int(row.gain_rank)}（{_ratio_text(row)}）"
                )
        chosen = chosen[:top_n]
        selections[str(top_n)] = [
            {
                "code": c,
                "reason": reasons.get(c, ""),
                "group": passed.set_index("code").loc[c, "group"],
                "library": passed.set_index("code").loc[c, "library"],
                "source": sources[c],
            }
            for c in chosen
        ]
        log.info(
            "入选 top-%d：%d（%s）",
            top_n,
            len(chosen),
            dict(Counter(x["group"] for x in selections[str(top_n)])),
        )

    payload = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "protocol": {
            "models": ["T+3", "T+5"],
            "selection_label": "T+5",
            "next_day_gate": f"T+1 rank IC same sign, |IC|>={MIN_ABS_IC1:.3f}",
            "tag_cutoff": TRAIN_END,
            "train": [TRAIN_START, TRAIN_END],
            "valid": [VALID_START, VALID_END],
            "test": [TEST_START, TEST_END],
            "dedup": CORR_DEDUP,
            "sample_step": step,
            "coarse_n": n_coarse,
            "n_sample_days": len(ic5),
            "n_sample_rows": int(seen),
            "n_lgb_rows": int(len(x_all)),
            "label_mode": args.label_mode,
        },
        "selected": selections,
        "metrics": {
            r.code: {
                "group": r.group,
                "library": r.library,
                "ic5": None if not np.isfinite(r.ic5) else round(float(r.ic5), 5),
                "ic3": None if not np.isfinite(r.ic3) else round(float(r.ic3), 5),
                "ic1": None if not np.isfinite(r.ic1) else round(float(r.ic1), 5),
                "ic1_cc": None
                if not np.isfinite(r.ic1_cc)
                else round(float(r.ic1_cc), 5),
                "icir5": None if not np.isfinite(r.icir5) else round(float(r.icir5), 4),
                "pos5": None if not np.isfinite(r.pos5) else round(float(r.pos5), 3),
                "coverage": round(float(r.coverage), 3),
                "cap_ic": [
                    None if not np.isfinite(v) else round(float(v), 5)
                    for v in (r.cap_ic_small, r.cap_ic_mid, r.cap_ic_large)
                ],
                "lgb_gain": round(float(r.lgb_gain), 2),
                "gain_ratio": round(float(r.gain_ratio), 3),
                "gain_rank": int(r.gain_rank),
            }
            for r in metrics.itertuples()
        },
        "rejected": [
            {
                "code": r.code,
                "group": r.group,
                "reason": (
                    f"coverage<{MIN_COVERAGE:.2f}"
                    if r.coverage < MIN_COVERAGE
                    else f"|IC5|<{MIN_ABS_IC5:.3f}"
                    if abs(r.ic5) < MIN_ABS_IC5
                    else f"|ICIR5|<{MIN_ABS_ICIR5:.2f}"
                    if abs(r.icir5) < MIN_ABS_ICIR5
                    else "T+1 反向/不足"
                    if not (r.t1_same_sign and abs(r.ic1) >= MIN_ABS_IC1)
                    else "cap 不同号"
                    if r.cap_sign_agree < 2
                    else "gain<=null"
                    if r.gain_ratio < GAIN_NULL_MIN
                    else "类别配额/相关性"
                ),
            }
            for r in metrics.itertuples()
            if not gate.loc[r.Index]
        ][:300],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    csv = out.with_suffix(".csv")
    metrics.to_csv(csv, index=False, encoding="utf-8-sig")
    log.info("完成 → %s（%.0fs）", out, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
