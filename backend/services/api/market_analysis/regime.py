# mypy: disable-error-code=untyped-decorator

"""市场阶段（长期/中期/短期/当日）数据面。

端点：``GET /api/v1/market-analysis/regime?date=YYYY-MM-DD``
返回：五层读数、基础暴露、顶风险/底部信号、图表序列（近 250 日）、
2×2 散点、当日盘中广度/MCL 路径。

数据源全部为本地 parquet，不依赖外部 API。
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user

router = APIRouter()

from .regime_universe import (  # noqa: E402
    UNIVERSES,
    UNIVERSE_LABELS,
    build_frame,
    intraday_universe,
    quadrant_long,
    score_history,
    universe_symbols,
)

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
if not ROOT.exists():
    ROOT = Path(__file__).resolve().parents[4] / "data" / "quantdb"
PATH = ROOT / "5_technical_derived" / "market_breadth" / "intraday_path_panel.parquet"
SNAP = ROOT / "5_technical_derived" / "market_breadth" / "intraday_market_panel.parquet"
SNAP1010 = (
    ROOT / "5_technical_derived" / "market_breadth" / "intraday_snapshot_1010.parquet"
)
KLINE = ROOT / "1_kline_data" / "daily_backward"
INST = ROOT / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet"


def _last_kline_tag() -> str:
    return sorted(os.listdir(KLINE))[-1]


def _tags() -> list[str]:
    return sorted(os.listdir(KLINE))


def _day_tags(ts: pd.Timestamp) -> tuple[str, str]:
    tag = f"dt={ts:%Y%m%d}"
    tags = _tags()
    idx = np.searchsorted(tags, tag, side="right") - 1
    if idx < 1:
        raise HTTPException(status_code=404, detail="该日期没有日线数据")
    return tags[idx], tags[idx - 1]


@functools.lru_cache(maxsize=8)
def _day_returns(cur_tag: str, prev_tag: str) -> pd.DataFrame:
    cur = pq.read_table(
        KLINE / cur_tag / "data.parquet", columns=["symbol", "close", "amount"]
    ).to_pandas()
    prev = (
        pq.read_table(KLINE / prev_tag / "data.parquet", columns=["symbol", "close"])
        .to_pandas()
        .rename(columns={"close": "prev_close"})
    )
    inst = (
        pq.read_table(INST, columns=["Symbol", "Name", "rs_hyname"])
        .to_pandas()
        .rename(columns={"Symbol": "symbol", "Name": "name", "rs_hyname": "sector"})
    )
    m = cur.merge(prev, on="symbol", how="inner").merge(inst, on="symbol", how="left")
    m = m[(m["prev_close"] > 0) & (m["close"] > 0) & m["sector"].notna()]
    m["ret"] = m["close"] / m["prev_close"] - 1
    m["amount_yi"] = m["amount"] / 1e4
    return m[["symbol", "name", "sector", "ret", "amount_yi", "prev_close"]]


def _sector_snapshot(ts: pd.Timestamp, universe: str = "ALL") -> dict:
    cur_tag, prev_tag = _day_tags(ts)
    d = _day_returns(cur_tag, prev_tag)
    syms = universe_symbols(universe, set(d["symbol"]))
    if syms is not None:
        d = d[d["symbol"].isin(syms)]
    g = d.groupby("sector").agg(
        n=("symbol", "size"),
        ret=("ret", lambda x: float(x.mean() * 100)),
        up_share=("ret", lambda x: float((x > 0).mean() * 100)),
        median=("ret", lambda x: float(x.median() * 100)),
        amount_yi=("amount_yi", "sum"),
    )
    g = g.sort_values("ret", ascending=False)
    rows = [
        {
            "name": str(idx),
            "n": int(row["n"]),
            "ret": round(float(row["ret"]), 2),
            "up_share": round(float(row["up_share"]), 1),
            "median": round(float(row["median"]), 2),
            "amount_yi": round(float(row["amount_yi"]), 1),
        }
        for idx, row in g.iterrows()
    ]
    return {
        "up_share": round(float((g["ret"] > 0).mean() * 100), 1),
        "dispersion": round(float(g["ret"].std()), 2),
        "list": rows,
    }


@functools.lru_cache(maxsize=32)
def _rs_1010(date_str: str, universe: str = "ALL") -> pd.DataFrame:
    """该日 10:10 个股相对强弱（相对同宇宙中位数，%）；无数据返回空表。"""
    if not SNAP1010.exists():
        return pd.DataFrame(columns=["symbol", "rs_1010"])
    q = f"""
        SELECT symbol, px40
        FROM read_parquet('{SNAP1010}')
        WHERE CAST(date AS DATE) = DATE '{date_str}' AND px40 > 0
    """
    try:
        snap = duckdb.connect().execute(q).fetchdf()
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["symbol", "rs_1010"])
    if snap.empty:
        return pd.DataFrame(columns=["symbol", "rs_1010"])
    cur_tag, prev_tag = _day_tags(pd.Timestamp(date_str))
    d = _day_returns(cur_tag, prev_tag)[["symbol", "prev_close"]]
    m = snap.merge(d, on="symbol", how="inner")
    m = m[m["prev_close"] > 0]
    syms = universe_symbols(universe, set(m["symbol"]))
    if syms is not None:
        m = m[m["symbol"].isin(syms)]
    if m.empty:
        return pd.DataFrame(columns=["symbol", "rs_1010"])
    m["ret_m"] = m["px40"] / m["prev_close"] - 1
    m["rs_1010"] = (m["ret_m"] - m["ret_m"].median()) * 100
    return m[["symbol", "rs_1010"]]


def _sector_stocks(ts: pd.Timestamp, sector: str, universe: str = "ALL") -> list[dict]:
    cur_tag, prev_tag = _day_tags(ts)
    d = _day_returns(cur_tag, prev_tag)
    syms = universe_symbols(universe, set(d["symbol"]))
    if syms is not None:
        d = d[d["symbol"].isin(syms)]
    sub = d[d["sector"] == sector].sort_values("ret", ascending=False)
    rs = _rs_1010(ts.strftime("%Y-%m-%d"), universe)
    if not rs.empty:
        sub = sub.merge(rs, on="symbol", how="left")
    else:
        sub["rs_1010"] = np.nan
    return [
        {
            "symbol": str(r["symbol"]),
            "name": str(r["name"]),
            "ret": round(float(r["ret"]) * 100, 2),
            "amount_yi": round(float(r["amount_yi"]), 2),
            "rs_1010": round(float(r["rs_1010"]), 2)
            if np.isfinite(r["rs_1010"])
            else None,
        }
        for _, r in sub.head(80).iterrows()
    ]


def _pick(d: pd.DataFrame, date: str | None) -> pd.Timestamp:
    if date:
        try:
            ts = pd.Timestamp(date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="date 格式应为 YYYY-MM-DD"
            ) from exc
        sub = d.index[d.index <= ts]
        if len(sub) == 0:
            raise HTTPException(status_code=404, detail="该日期之前没有可用数据")
        return sub[-1]
    return d.index[-1]


def _color(score: int) -> str:
    return {2: "green", 1: "green", 0: "yellow", -1: "orange", -2: "red"}.get(
        score, "neutral"
    )


def _cap(above: bool, mom_up: bool, cheap_pct: float) -> tuple[float, int, str]:
    if above and mom_up:
        if cheap_pct > 0.8:
            return 1.0, 2, "趋势向上 + 估值便宜"
        if cheap_pct < 0.2:
            return 0.6, 1, "趋势向上但估值贵"
        return 0.8, 2, "趋势向上 + 估值中性"
    if (not above) and (not mom_up):
        if cheap_pct > 0.8:
            return 0.6, 0, "趋势向下但估值便宜"
        if cheap_pct < 0.2:
            return 0.2, -2, "趋势向下 + 估值贵"
        return 0.4, -1, "趋势向下 + 估值中性"
    return 0.6, 0, "趋势/动量混合"


def _layer(
    key: str, label: str, stage: str, score: int, metrics: list[dict], action: str
) -> dict:
    return {
        "key": key,
        "label": label,
        "stage": stage,
        "score": score,
        "color": _color(score),
        "metrics": metrics,
        "action": action,
    }


def _intraday(ts: pd.Timestamp) -> dict:
    if PATH.exists():
        p = pd.read_parquet(PATH)
        p["date"] = pd.to_datetime(p["date"])
        p["time"] = p["time"].astype(str).str.slice(0, 5)
        sub = p[p["date"] == ts]
        if len(sub):
            return {
                "available": True,
                "source": "l2_min1 全市场",
                "times": sub["time"].tolist(),
                "breadth": [round(float(x), 2) for x in sub["breadth"]],
                "mcl_chg5": [
                    round(float(x), 2) if np.isfinite(x) else None
                    for x in sub["mcl_chg5"]
                ],
                "note": "10:10 快照，等权全市场",
            }
    if SNAP.exists():
        p = pd.read_parquet(SNAP)
        p.index = pd.to_datetime(p.index)
        if ts in p.index:
            r = p.loc[ts]
            return {
                "available": True,
                "source": "10:10 快照",
                "times": ["10:10"],
                "breadth": [round(float(r["up_share"]), 2)],
                "mcl_chg5": [
                    round(float(r["mcl_chg5"]), 2)
                    if np.isfinite(r["mcl_chg5"])
                    else None
                ],
                "note": "仅 10:10，无盘中路径",
            }
    # 全市场分钟缺口时，用 L0.5 热集快照补当日路径（约 590 只，非全市场）
    try:
        from backend.scripts.intraday_breadth_probe import hot_set_path

        hp = hot_set_path(ts.strftime("%Y-%m-%d"))
        if not hp.empty:
            return {
                "available": True,
                "source": "L0.5 热集（非全市场）",
                "times": hp["time"].tolist(),
                "breadth": [round(float(x), 2) for x in hp["up_share"]],
                "mcl_chg5": [None] * len(hp),
                "note": "热集约 590 只，广度绝对值有偏，只看路径",
            }
    except Exception:  # noqa: BLE001
        pass
    return {
        "available": False,
        "times": [],
        "breadth": [],
        "mcl_chg5": [],
        "note": "该日期无盘中数据（l2_min1 覆盖 2017-2026，部分日期缺口）",
    }


def _series(d: pd.DataFrame, end: pd.Timestamp, n: int = 250) -> dict:
    sub = d.loc[:end].tail(n)
    return {
        "dates": [x.strftime("%Y-%m-%d") for x in sub.index],
        "close": [round(float(x), 2) for x in sub["close_000001.SH"]],
        "ma250": [round(float(x), 2) if np.isfinite(x) else None for x in sub["ma250"]],
        "above60": [
            round(float(x), 2) if np.isfinite(x) else None
            for x in sub["pct_above_ma60"] * 100
        ],
        "adl20": [
            round(float(x), 2) if np.isfinite(x) else None for x in sub["adl20_pct"]
        ],
        "up_ratio": [
            round(float(x), 2) if np.isfinite(x) else None for x in sub["up_ratio"]
        ],
        "pb_cheap": [
            round(float(x), 3) if np.isfinite(x) else None for x in sub["bp_capw_pct"]
        ],
        "breadth_5d": [
            round(float(x), 2) if np.isfinite(x) else None for x in sub["breadth_5d"]
        ],
    }


def _quadrant(current: pd.Series, long_d: pd.DataFrame) -> dict:
    """2×2 散点；`long_d` 是 2016 起的长历史（不受 560 日面板限制）。"""
    sub = long_d.loc["2018-01-01":].copy()
    sub = sub[
        sub["fwd250"].notna() & sub["bp_capw_pct"].notna() & sub["vs_ma250"].notna()
    ]
    sub = sub.iloc[::2]
    return {
        "x": [round(float(x) * 100, 2) for x in sub["vs_ma250"]],
        "y": [round(float(x), 3) for x in sub["bp_capw_pct"]],
        "ret": [
            round(float(x) * 100, 1) if np.isfinite(x) else None for x in sub["fwd250"]
        ],
        "current": {
            "x": round(float(current["vs_ma250"]) * 100, 2),
            "y": round(float(current["bp_capw_pct"]), 3),
        },
    }


def _block_history(d: pd.DataFrame, ts: pd.Timestamp, n: int = 60) -> list[dict]:
    """最近 n 日的六块评分，供雷达双圈与 sparkline。"""
    h = d.loc[:ts].tail(n)
    sc = score_history(h)
    cols = [
        "trend",
        "valuation",
        "breadth_level",
        "breadth_momentum",
        "concentration",
        "volatility",
    ]
    return [
        {
            "date": dt.strftime("%Y-%m-%d"),
            "scores": [int(row[c]) for c in cols],
        }
        for dt, row in sc.iterrows()
    ]


@router.get("/regime")
def market_regime(
    date: str | None = Query(None, description="YYYY-MM-DD，默认最新"),
    universe: str = Query("ALL", description="指数宇宙代码，默认 ALL"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    _ = current_user
    date = date if isinstance(date, str) else None
    universe = universe if isinstance(universe, str) else "ALL"
    if universe not in UNIVERSE_LABELS:
        raise HTTPException(status_code=400, detail=f"未知 universe: {universe}")
    d = build_frame(universe, _last_kline_tag())
    ts = _pick(d, date)
    r = d.loc[ts]
    cheap = float(r["bp_capw_pct"]) if np.isfinite(r["bp_capw_pct"]) else 0.5
    above = bool(r["close_000001.SH"] > r["ma250"])
    mom_up = bool(r["mom250"] > 0)
    cap, long_score, cap_reason = _cap(above, mom_up, cheap)

    above60 = float(r["pct_above_ma60"]) * 100
    adl20 = float(r["adl20"])
    if above60 > 50 and adl20 > 0:
        mult, med_score, med_stage = 1.0, 2, "上行修复"
    elif above60 < 30 or adl20 < 0:
        mult, med_score, med_stage = 0.5, -1, "下跌中继"
    else:
        mult, med_score, med_stage = 0.75, 0, "震荡/待确认"
    wash = float(r["breadth_5d"]) < 25
    trin_hi = float(r["trin_vol"]) > 2 if np.isfinite(r["trin_vol"]) else False
    if wash or trin_hi:
        med_stage = "投降段"
        med_score = 1
        mult = min(1.0, mult + 0.1)

    up = float(r["up_ratio"])
    if up < 15:
        short_score, short_stage = 1, "超卖（反抽候选）"
    elif up > 80:
        short_score, short_stage = 2, "强势延续"
    else:
        short_score, short_stage = 0, "中性"

    intra = _intraday(ts) if universe == "ALL" else intraday_universe(ts, universe, d)
    intra_score, intra_stage = 0, "无盘中数据"
    intra_k = 0
    if intra["available"] and intra["breadth"]:
        intra_k = intra["times"].index("10:10") if "10:10" in intra["times"] else 0
        b = intra["breadth"][intra_k]
        m = intra["mcl_chg5"][intra_k] if intra["mcl_chg5"] else None
        if b > 60 and (m is not None and m > 0):
            intra_score, intra_stage = 2, "强延续（下午偏强）"
        elif b > 60:
            intra_score, intra_stage = 1, "弱延续（下午转弱）"
        elif b < 30 and (m is not None and m < 0):
            intra_score, intra_stage = -1, "弱延续（下午继续弱）"
        elif b < 30:
            intra_score, intra_stage = 0, "止跌观察"
        else:
            intra_score, intra_stage = 0, "中性"

    top_risk = bool(r["idx_hi"] and not r["adl_hi"])
    bottom = bool(cheap > 0.8 and (wash or trin_hi))
    actual = round(min(cap, cap * mult + (0.1 if (wash or trin_hi) else 0.0)), 2)
    actual = max(0.0, actual)

    # 六块评分（-2..+2）：给雷达/柱状总结用
    trend_score = (
        2 if (above and mom_up) else -2 if ((not above) and (not mom_up)) else 0
    )
    val_score = 2 if cheap > 0.8 else 1 if cheap > 0.5 else -2 if cheap < 0.2 else -1
    breadth_score = (
        2 if (above60 > 50 and adl20 > 0) else -2 if (above60 < 30 and adl20 < 0) else 0
    )
    mcl = float(r["mcl_chg5"]) if np.isfinite(r["mcl_chg5"]) else 0.0
    mom_score = 2 if (up > 60 and mcl > 0) else -2 if (up < 30 and mcl < 0) else 0
    trin = float(r["trin_vol"]) if np.isfinite(r["trin_vol"]) else 1.0
    conc_score = 1 if trin > 2 else -1 if trin < 0.5 else 0
    vol_pct = float(r["vol20_pct"]) if np.isfinite(r["vol20_pct"]) else 0.5
    vol_score = 1 if vol_pct < 0.2 else -1 if vol_pct > 0.8 else 0
    blocks = [
        {
            "key": "trend",
            "label": "趋势",
            "score": trend_score,
            "value": round(float(r["vs_ma250"]) * 100, 1),
            "unit": "%",
            "read": "年线/250日动量",
        },
        {
            "key": "valuation",
            "label": "估值",
            "score": val_score,
            "value": round(cheap, 3),
            "unit": "",
            "read": "PB便宜分位",
        },
        {
            "key": "breadth_level",
            "label": "广度水平",
            "score": breadth_score,
            "value": round(above60, 1),
            "unit": "%",
            "read": "站上MA60/ADL20",
        },
        {
            "key": "breadth_momentum",
            "label": "广度动量",
            "score": mom_score,
            "value": round(up, 1),
            "unit": "%",
            "read": "涨家/MCL",
        },
        {
            "key": "concentration",
            "label": "资金结构",
            "score": conc_score,
            "value": round(trin, 2),
            "unit": "",
            "read": "TRIN_量",
        },
        {
            "key": "volatility",
            "label": "波动",
            "score": vol_score,
            "value": round(float(r["vol20"]), 1) if np.isfinite(r["vol20"]) else "—",
            "unit": "%",
            "read": f"20日波动分位 {vol_pct:.0%}",
        },
    ]
    total = sum(int(b["score"]) for b in blocks)
    state = (
        "风险偏好高"
        if total >= 6
        else "偏多"
        if total >= 2
        else "震荡"
        if total >= -1
        else "偏空"
        if total >= -5
        else "防守"
    )
    summary = {
        "score": total,
        "state": state,
        "one_liner": f"长期{'上方' if above else '下方'}、中期{med_stage}、"
        f"短期{short_stage}；基础暴露 {actual:.1f}（天花板 {cap:.1f}）",
    }

    layers = [
        _layer(
            "long",
            "长期",
            "MA250上方" if above else "MA250下方",
            long_score,
            [
                {
                    "label": "距年线",
                    "value": round(float(r["vs_ma250"]) * 100, 1),
                    "unit": "%",
                },
                {
                    "label": "250日动量",
                    "value": round(float(r["mom250"]) * 100, 1),
                    "unit": "%",
                },
                {"label": "PB便宜分位", "value": round(cheap, 3), "unit": ""},
            ],
            f"天花板 {cap:.1f}（{cap_reason}）",
        ),
        _layer(
            "medium",
            "中期",
            med_stage,
            med_score,
            [
                {"label": "站上MA60", "value": round(above60, 1), "unit": "%"},
                {"label": "ADL20", "value": round(adl20, 0), "unit": ""},
                {
                    "label": "TRIN_量",
                    "value": round(float(r["trin_vol"]), 2)
                    if np.isfinite(r["trin_vol"])
                    else "—",
                    "unit": "",
                },
            ],
            f"实际仓位 ×{mult:.2f}",
        ),
        _layer(
            "short",
            "短期",
            short_stage,
            short_score,
            [
                {"label": "收盘涨家", "value": round(up, 1), "unit": "%"},
                {
                    "label": "5日均涨家",
                    "value": round(float(r["breadth_5d"]), 1),
                    "unit": "%",
                },
            ],
            "超卖分批 / 过热等回踩 / 中性不动作",
        ),
        _layer(
            "intraday",
            "当日",
            intra_stage,
            intra_score,
            [
                {
                    "label": "10:10涨家",
                    "value": intra["breadth"][intra_k] if intra["breadth"] else "—",
                    "unit": "%",
                },
                {
                    "label": "MCL5",
                    "value": intra["mcl_chg5"][intra_k] if intra["mcl_chg5"] else "—",
                    "unit": "",
                },
            ],
            intra["note"],
        ),
        _layer(
            "stock",
            "个股",
            "强广度买落后 / 弱广度不接刀" if med_score >= 0 else "不新增趋势多单",
            max(0, min(med_score, short_score)),
            [
                {"label": "相对强弱", "value": "Q1-Q2 优先", "unit": ""},
                {"label": "板块", "value": "前30%", "unit": ""},
            ],
            "强广度日买早盘落后票，弱广度日不接飞刀",
        ),
    ]

    return {
        "date": ts.strftime("%Y-%m-%d"),
        "layers": layers,
        "exposure": {
            "ceiling": cap,
            "actual": actual,
            "label": f"{actual:.1f}（天花板 {cap:.1f}）",
        },
        "top_risk": {
            "flag": top_risk,
            "text": "ADL 背离：指数250日新高但ADL未新高（20日 −2.0%）"
            if top_risk
            else "无（指数未在250日新高，或ADL同步新高）",
        },
        "bottom": {
            "flag": bottom,
            "text": "底部信号（PB便宜+投降）"
            if bottom
            else "底部不成立（估值不便宜或未投降）",
        },
        "charts": _series(d, ts),
        "quadrant": _quadrant(r, quadrant_long(universe, _last_kline_tag())),
        "intraday": intra,
        "sector": _sector_snapshot(ts, universe),
        "blocks": blocks,
        "block_history": _block_history(d, ts),
        "summary": summary,
        "universe": universe,
        "universe_label": UNIVERSE_LABELS[universe],
        "universes": [{"code": c, "label": lb} for c, lb in UNIVERSES],
        "valuation_scope": "universe",
        "universe_note": "成分股权重为当前快照，非 PIT；历史回看有幸存者偏差"
        if universe != "ALL"
        else "全A等权，无成分股筛选",
        "dates": [x.strftime("%Y-%m-%d") for x in d.index[-250:]],
    }


def _state_label(score: int) -> str:
    if score >= 6:
        return "风险偏好高"
    if score >= 2:
        return "偏多"
    if score >= -1:
        return "震荡"
    if score >= -5:
        return "偏空"
    return "防守"


DEFAULT_COMPARE = (
    "ALL",
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000300.SH",
    "000852.SH",
)


def _compare_item(code: str, ts: pd.Timestamp) -> dict:
    d = build_frame(code, _last_kline_tag()).loc[:ts]
    if d.empty:
        return {"code": code, "label": UNIVERSE_LABELS[code], "date": None}
    sc = score_history(d)
    total = sc["total"]
    score = int(total.iloc[-1])
    pressure = 100.0 - (total + 12) / 24.0 * 100.0
    rank = pressure.expanding(min_periods=120).rank(pct=True)
    cur_rank = float(rank.iloc[-1]) if len(rank) else np.nan
    lo, hi = max(0.0, cur_rank - 0.1), min(1.0, cur_rank + 0.1)
    mask = (rank >= lo) & (rank <= hi)
    c = d["close_000001.SH"]
    fwd = {}
    for h in (5, 10, 20):
        y = (c.shift(-h) / c - 1.0) * 100.0
        x = y[mask].dropna()
        fwd[str(h)] = {
            "mean": round(float(x.mean()), 2) if len(x) else None,
            "win": round(float((x > 0).mean() * 100), 1) if len(x) else None,
            "n": int(len(x)),
        }
    chg = float(c.iloc[-1] / c.iloc[-2] - 1.0) * 100.0 if len(c) > 1 else np.nan
    return {
        "code": code,
        "label": UNIVERSE_LABELS[code],
        "date": d.index[-1].strftime("%Y-%m-%d"),
        "chg": round(chg, 2) if np.isfinite(chg) else None,
        "score": score,
        "pressure": round(float(pressure.iloc[-1]), 1),
        "pctile": round(cur_rank, 3) if np.isfinite(cur_rank) else None,
        "state": _state_label(score),
        "fwd": fwd,
        "n_days": len(d),
    }


@functools.lru_cache(maxsize=16)
def _compare_cached(date_str: str, codes: tuple[str, ...], last_tag: str) -> list[dict]:
    _ = last_tag
    items = []
    for code in codes:
        d = build_frame(code, _last_kline_tag())
        ts = _pick(d, date_str or None)
        items.append(_compare_item(code, ts))
    return items


@router.get("/regime/compare")
def regime_compare(
    date: str | None = Query(None, description="YYYY-MM-DD，默认最新"),
    universes: str | None = Query(None, description="逗号分隔的指数代码"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    _ = current_user
    date = date if isinstance(date, str) else None
    codes = (
        tuple(x.strip() for x in universes.split(",") if x.strip())
        if isinstance(universes, str) and universes.strip()
        else DEFAULT_COMPARE
    )
    for code in codes:
        if code not in UNIVERSE_LABELS:
            raise HTTPException(status_code=400, detail=f"未知 universe: {code}")
    items = _compare_cached(date or "", codes, _last_kline_tag())
    return {"date": items[0].get("date") if items else None, "items": items}


@router.get("/regime/sector-stocks")
def regime_sector_stocks(
    sector: str = Query(..., min_length=1, description="行业名，如 半导体"),
    date: str | None = Query(None, description="YYYY-MM-DD，默认最新"),
    universe: str = Query("ALL", description="指数宇宙代码，默认 ALL"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    _ = current_user
    date = date if isinstance(date, str) else None
    universe = universe if isinstance(universe, str) else "ALL"
    if universe not in UNIVERSE_LABELS:
        raise HTTPException(status_code=400, detail=f"未知 universe: {universe}")
    d = build_frame(universe, _last_kline_tag())
    ts = _pick(d, date)
    return {
        "date": ts.strftime("%Y-%m-%d"),
        "sector": sector,
        "universe": universe,
        "universe_label": UNIVERSE_LABELS[universe],
        "stocks": _sector_stocks(ts, sector, universe),
    }
