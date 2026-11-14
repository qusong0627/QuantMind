"""今日 10:10 全市场快照 → 盘中提示词块（交易 agent 用）。

为什么单独做
------------
研究结论里最稳的盘中信号是 **10:10 广度 + MCL5**（<30% 当日 −0.20%，>60%
+0.245%；弱广度+收缩 −0.236%），但现有槽位 10:00/10:05 都在 10:10 之前，
下一个槽 11:00 已经过了 50 分钟。本模块把**今日** 10:10 的全市场/热集快照
压成提示词块，供 10:15 起的槽位使用；**不是**上一交易日的阶段快照。

数据源与口径
------------
* 全市场：``intraday_snapshot_1010.parquet`` 当日分区（px40 / 昨收）；
* 缺口：L0.5 热集 ``l05_snapshots/date=YYYYMMDD/*.parquet``（约 590 只，
  明确标注，不冒充全市场）；
* MCL5：``trin_daily.parquet`` 的日频 McClellan 5 日变化，截至上一交易日
  （盘中当日收盘行尚未产出），标注“日频”。
* Q1–Q5：按 10:10 涨跌幅排序分五档，Q1=最弱；给中位涨幅，不给“10:10→收盘”
  的事后收益（那是收盘复盘口径，盘中不可知）。
"""

from __future__ import annotations

import bisect
import functools
import logging
import os
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

CN_TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
if not ROOT.exists():
    ROOT = Path(__file__).resolve().parents[2] / "data" / "quantdb"
SNAP1010 = (
    ROOT / "5_technical_derived" / "market_breadth" / "intraday_snapshot_1010.parquet"
)
BREADTH = ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
UNADJ = ROOT / "1_kline_data" / "daily_unadjusted"
L05 = ROOT.parent / "l05_snapshots"

_EMPTY = pd.DataFrame(columns=["symbol", "ret"])
logger = logging.getLogger(__name__)


def _prev_close(day: pd.Timestamp) -> pd.DataFrame:
    """该日之前的最近一个未复权收盘（当日分区未落时就是上一交易日）。"""
    try:
        tags = sorted(os.listdir(UNADJ))
    except OSError:
        return pd.DataFrame(columns=["symbol", "prev_close"])
    tag = f"dt={day:%Y%m%d}"
    i = bisect.bisect_left(tags, tag) - 1
    if i < 0:
        return pd.DataFrame(columns=["symbol", "prev_close"])
    p = UNADJ / tags[i] / "data.parquet"
    if not p.exists():
        return pd.DataFrame(columns=["symbol", "prev_close"])
    t = pq.read_table(p, columns=["symbol", "close"]).to_pandas()
    return t.rename(columns={"close": "prev_close"})


def _full_snapshot(day: pd.Timestamp) -> pd.DataFrame:
    if not SNAP1010.exists():
        return _EMPTY
    q = f"""
        SELECT symbol, px40
        FROM read_parquet('{SNAP1010}')
        WHERE CAST(date AS DATE) = DATE '{day:%Y-%m-%d}' AND px40 > 0
    """
    try:
        snap = duckdb.connect().execute(q).fetchdf()
    except Exception:  # noqa: BLE001 - 快照损坏按缺口处理
        return _EMPTY
    if snap.empty:
        return _EMPTY
    prev = _prev_close(day)
    if prev.empty:
        return _EMPTY
    m = snap.merge(prev, on="symbol", how="inner")
    m = m[m["prev_close"] > 0].copy()
    m["ret"] = m["px40"] / m["prev_close"] - 1
    return m[["symbol", "ret"]]


def _hot_snapshot(day: pd.Timestamp) -> pd.DataFrame:
    b = L05 / f"date={day:%Y%m%d}"
    if not b.is_dir():
        return _EMPTY
    frames = []
    try:
        names = sorted(os.listdir(b))
    except OSError:
        return _EMPTY
    for f in names:
        if not f.endswith(".parquet"):
            continue
        try:
            frames.append(
                pq.read_table(
                    b / f, columns=["symbol", "refresh_time", "price", "pre_close"]
                ).to_pandas()
            )
        except Exception:  # noqa: BLE001 - 单分片坏不影响其余
            continue
    if not frames:
        return _EMPTY
    df = pd.concat(frames, ignore_index=True)
    df["rt"] = df["refresh_time"].astype(str).str.zfill(6).astype(int)
    s = df[df["rt"] <= 101000].sort_values("rt").groupby("symbol").tail(1)
    s = s[(s["pre_close"] > 0) & (s["price"] > 0)].copy()
    if s.empty:
        return _EMPTY
    s["ret"] = s["price"] / s["pre_close"] - 1
    return s[["symbol", "ret"]]


def _mcl5(day: pd.Timestamp) -> tuple[float | None, str]:
    """日频 MCL5（McClellan − 5 日前），取该日之前最近一行；返回 (值, 日期)。

    先用 ``load_breadth_tail`` 从原始日线补最近 260 天（``trin_daily`` 面板常落后
    数周）；尾部不可用再退回面板。两者都缺 → ``(None, "")``，块里渲染 ``—``。
    """
    cutoff = day - pd.Timedelta(days=1)
    try:
        from backend.scripts.market_stage_dashboard import load_breadth_tail

        tail = load_breadth_tail(260)
        if "mcl_chg5" in tail.columns:
            s = tail["mcl_chg5"].dropna()
            s = s[s.index <= cutoff]
            if not s.empty:
                return float(s.iloc[-1]), s.index[-1].strftime("%Y-%m-%d")
    except Exception as exc:  # noqa: BLE001 - 尾部失败退回面板
        logger.warning("[Intraday1010] 日线尾部 MCL5 不可用，退回面板: %s", exc)
    try:
        d = pd.read_parquet(BREADTH).set_index("date").sort_index()
    except Exception:  # noqa: BLE001
        return None, ""
    if "mcclellan" not in d.columns:
        return None, ""
    chg = d["mcclellan"] - d["mcclellan"].shift(5)
    sub = chg.loc[:cutoff].dropna()
    if sub.empty:
        return None, ""
    return float(sub.iloc[-1]), sub.index[-1].strftime("%Y-%m-%d")


@functools.lru_cache(maxsize=8)
def _bundle(day_str: str) -> dict[str, Any]:
    day = pd.Timestamp(day_str)
    m = _full_snapshot(day)
    sample = "full"
    if m.empty:
        m = _hot_snapshot(day)
        sample = "hot_set"
    if m.empty:
        return {"available": False, "sample": "", "n": 0, "day": day_str}
    up = int((m["ret"] > 0).sum())
    down = int((m["ret"] < 0).sum())
    flat = int((m["ret"] == 0).sum())
    total = len(m)
    m = m.copy()
    m["q"] = pd.qcut(
        m["ret"].rank(method="first"), 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"]
    )
    g = m.groupby("q", observed=True)["ret"].agg(["size", "median"])
    quintiles = [
        {
            "q": str(idx),
            "n": int(row["size"]),
            "med": round(float(row["median"]) * 100, 2),
        }
        for idx, row in g.iterrows()
    ]
    mcl, mcl_day = _mcl5(day)
    return {
        "available": True,
        "sample": sample,
        "n": total,
        "day": day_str,
        "up": up,
        "down": down,
        "flat": flat,
        "up_share": round(up / total * 100, 1) if total else None,
        "quintiles": quintiles,
        "mcl5": round(mcl, 2) if mcl is not None else None,
        "mcl5_day": mcl_day,
    }


def _num(value: Any, digits: int = 2, sign: bool = True) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(f):
        return "—"
    return f"{f:+.{digits}f}" if sign else f"{f:.{digits}f}"


def build_intraday_1010_block(now: datetime | None = None) -> str:
    """今日 10:10 提示词块；10:10 之前/无数据 → 空串或缺口提示。"""
    moment = now or datetime.now(CN_TZ)
    if moment.time() < time(10, 10):
        return ""
    b = _bundle(moment.strftime("%Y-%m-%d"))
    if not b.get("available"):
        return (
            "【今日10:10快照】数据缺口（全市场快照与 L0.5 热集都未取到）"
            "——不要用昨日快照冒充今日。"
        )
    sample_txt = (
        f"全市场 {b['n']:,} 只"
        if b["sample"] == "full"
        else f"L0.5 热集 {b['n']} 只（非全市场，只看形状）"
    )
    age = max(
        0,
        int(
            (
                moment - moment.replace(hour=10, minute=10, second=0, microsecond=0)
            ).total_seconds()
            // 60
        ),
    )
    mcl = b.get("mcl5")
    mcl_txt = "—"
    if mcl is not None:
        direction = "扩张" if mcl > 0 else "收缩" if mcl < 0 else "持平"
        mcl_txt = f"{_num(mcl, 2)}（{direction}，日频截至 {b.get('mcl5_day') or '—'}）"
    q_txt = " / ".join(
        f"{row['q']} {_num(row['med'], 2)}" for row in b.get("quintiles") or []
    )
    lines = [
        f"【今日10:10快照·盘中参考】（{sample_txt}；截至 10:10）",
        f"- 广度：涨 {b['up']} / 跌 {b['down']} / 平 {b['flat']}"
        f"（上涨占比 {_num(b.get('up_share'), 1, sign=False)}%）",
        f"- MCL5：{mcl_txt}",
        f"- 10:10 相对强弱五档中位：{q_txt}（Q1=最弱）",
        "- 读法：10:10 广度 >60% 且 MCL5 扩张 → 强延续；<30% 且收缩 → "
        "弱延续/不接飞刀；这是当日执行信号，不是拐点预测。",
    ]
    if b["sample"] == "hot_set":
        lines.append(
            "- 口径提醒：热集约 590 只，30/60% 阈值来自全市场研究口径；"
            "热集读数只做方向参考，不把阈值当铁线。"
        )
    lines.append(f"- 数据时点：10:10（当前 {moment:%H:%M}，{age} 分钟前）")
    return "\n".join(lines)
