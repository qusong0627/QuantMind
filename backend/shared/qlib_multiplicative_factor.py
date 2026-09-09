"""qlib 乘法复权因子的核心算法（离线重建工具与 QlibDataBuilder 共用一份）。

背景：数据包的 ``factor.day.bin`` 存的是「加法后复权价 / 不复权价」
``f = m + A/raw``，它随每个交易日的价格反向漂移、且非单调。qlib 的撮合引擎
把 ``$factor`` 当**乘法**因子用（``real_shares = adjusted_amount * factor``，
恒等式 ``close_bin = raw * factor``），于是整仓卖出的股数 =
买入股数 × f(卖日)/f(买日) —— 区间内没有任何公司行为也会多卖或欠卖。

正确的总收益关系是「除权日两侧总收益连续」，因此

    factor_mult[t] = PROD over ex-dates j <= t of (raw_{j-1} / ex_ref_price_j)
    close_bin[t]   = raw[t] * factor_mult[t]

除权参考价 ``ref = (p0 - cash + allotPrice * allot) / (1 + bonus + allot)``，
事件取自 QuantDB ``dividend_factors``（``time`` = 除权除息日，``interest`` =
每 10 股现金红利，``stockBonus`` = 每 10 股送股，``allotment`` = 每 10 股配股，
``allotPrice`` = **每股**配股价）。

两个方向的闸门：
1. 正向：每条事件必须能预测除权日附近的裸价跌幅（±涨跌停内），否则判定该事件行
   不可信并丢弃（实测不可信的全是 ``type=15`` 预案行，除权尚未实施）；
2. 反向：裸价跌穿涨停、后复权价却明显抗跌的日子必须是除权日，事件表查无此日即
   报 ``missing_event``（抓日期错位 / 事件表缺行）。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.quantdb_paths import resolve_quantdb_subdir

DROP_RATIO = 1.1  # “必然是除权日”的判定：裸价跌幅超过 1.1x 涨停幅度
RESILIENCE = (
    0.05  # 后复权价比裸价抗跌多少个百分点才算真的除权（加法阻尼本身能吃掉 ~2%）
)
EVENT_TOL = 0.02  # 事件预测的除权跌幅与实际裸价跌幅的允许偏离
PRICE_MOVE_EPS = 1e-4  # 除权日后价格“真正动了”的最小相对变化
MAX_VERIFY_STEPS = 40  # 除权日停牌时，最多向后找多少个交易日去校验
STEP_EPS = 1e-7  # 因子相邻两天相对变化超过它才算“发生了阶跃”
ADDITIVE_LOOKALIKE = 0.02  # 存量 factor.bin 里变化日占比超过它即判定为加法口径


def resolve_events_dir() -> Path:
    """事件表目录（QUANTDB_DIVIDEND_DIR 覆盖 → QuantDB 数据目录派生）。

    勿硬编码 ``/data/quantdb/...``：便携包（免 Docker）数据目录是
    ``$STORAGE_ROOT/quantdb``，硬编码会让事件表整体读空 → ``EventBook`` 空 →
    ``_multiplicative_factor`` 返回 None → CN 的 qlib ``$factor`` 静默退回
    加法口径，``real_shares = adjusted_amount * factor`` 下股数逐日漂移。
    """
    env_val = os.getenv("QUANTDB_DIVIDEND_DIR", "").strip()
    if env_val:
        return Path(env_val)
    return resolve_quantdb_subdir("3_financial_data", "dividend_factors")


def board_limit(symbol: str) -> float:
    """涨跌停幅度：科创板/创业板 20%，北交所 30%，主板 10%（ST 更严，只会让判定更保守）。"""
    num = symbol[2:] if symbol[:2].lower() in ("sh", "sz", "bj") else symbol
    if num.startswith(("68", "30")):
        return 0.20
    if num.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def to_code(symbol: str) -> str:
    """qlib 前缀式 sh600036 -> 事件表文件名用的 600036.SH。"""
    if len(symbol) > 2 and symbol[:2].lower() in ("sh", "sz", "bj"):
        return f"{symbol[2:]}.{symbol[:2].upper()}"
    return symbol


def load_events(events_dir: Path, code: str) -> pd.DataFrame | None:
    path = Path(events_dir) / f"{code}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if "time" not in df.columns:
        return None
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    return df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


class EventBook:
    """按标的缓存事件表，避免全库重建时逐日重复读 parquet。"""

    def __init__(self, events_dir: Path | str | None = None) -> None:
        self.events_dir = Path(events_dir) if events_dir else resolve_events_dir()
        self._cache: dict[str, pd.DataFrame | None] = {}

    def for_symbol(self, qlib_symbol: str) -> pd.DataFrame | None:
        code = to_code(qlib_symbol)
        if code not in self._cache:
            self._cache[code] = load_events(self.events_dir, code)
        return self._cache[code]


def build_multiplicative_factor(
    raw: pd.Series,
    events: pd.DataFrame,
    limit: float,
    *,
    seed: float = 1.0,
) -> tuple[pd.Series, list[dict], list[str], list[str], set[pd.Timestamp]]:
    """factor[t] = PROD(p0 / ex_reference) over ex-dates; validates each event ex post.

    ``seed`` 是给增量追加用的：把已落库的最后一天因子当起点，只在窗口内继续累乘
    （窗口 Series 需多带一天的历史，作为首个除权事件的 p0）。

    返回的 ``steps`` 是实际生效的因子阶跃日（除权日停牌时 = 复牌首日），供反向闸门去重。
    """
    f = pd.Series(seed, index=raw.index)
    cum = float(seed)
    applied: list[dict] = []
    problems: list[str] = []
    notes: list[str] = []
    steps: set[pd.Timestamp] = set()
    for row in events.itertuples(index=False):
        ex = pd.Timestamp(row.time).normalize()
        prev = raw.index[raw.index < ex]
        if len(prev) == 0:
            # 上市/挂牌前的分红送配（常见于北交所承接新三板），对本段因子无影响，不算异常
            notes.append(f"event_before_listed {ex.date()}")
            continue
        p0 = float(raw.loc[prev[-1]])
        bonus = float(getattr(row, "stockBonus", 0) or 0) / 10.0
        allot = float(getattr(row, "allotment", 0) or 0) / 10.0
        cash = float(getattr(row, "interest", 0) or 0) / 10.0
        # allotPrice 是「每股」配股价（实测 94.7% 的配股事件以此口径拟合除权跌幅）
        aprice = float(getattr(row, "allotPrice", 0) or 0)
        ref = (p0 - cash + aprice * allot) / (1.0 + bonus + allot)
        if not np.isfinite(ref) or ref <= 0:
            problems.append(f"nonpositive_ref {ex.date()}")
            continue
        ratio = p0 / ref
        nxt = raw.index[raw.index >= ex]
        if len(nxt) == 0:
            # 除权日晚于该标的最后一条数据，对已写入的因子段无影响
            notes.append(f"event_after_last_bar {ex.date()}")
            continue
        # 除权日可能停牌（长停牌期间除权），裸价要等到复牌第一天才反映参考价：
        # 因子阶跃必须下在“价格真正变动的第一天”，并以该日做 ex post 校验。
        step_date, p1 = nxt[0], None
        for d in nxt[:MAX_VERIFY_STEPS]:
            v = float(raw.loc[d])
            if abs(v / p0 - 1.0) > PRICE_MOVE_EPS:
                step_date, p1 = d, v
                break
        if p1 is None:
            notes.append(f"event_unverifiable_suspended {ex.date()}")
        else:
            lo, hi = (
                ref / p0 * (1 - limit) - EVENT_TOL,
                ref / p0 * (1 + limit) + EVENT_TOL,
            )
            if not (lo <= p1 / p0 <= hi):
                # 事件行本身不可信（实测全为 type=15 预案行，除权尚未实施）：丢弃该事件。
                # 若它是“日期错位”的真除权，缺口会落在别处，由反向闸门 missing_event 兜住。
                notes.append(
                    f"event_mismatch {ex.date()} pred_ref/p0={ref / p0:.4f} "
                    f"actual={p1 / p0:.4f} on {step_date.date()}"
                )
                continue
        cum *= ratio
        f.loc[f.index >= step_date] = cum
        steps.add(step_date.normalize())
        applied.append(
            {
                "ex_date": str(ex.date()),
                "step_date": str(step_date.date()),
                "jump": ratio - 1.0,
                "cash_per_share": cash,
                "bonus_per_share": bonus,
                "allot_per_share": allot,
                "allot_price": aprice,
            }
        )
    return f, applied, problems, notes, steps


def unexplained_ex_dates(
    raw: pd.Series, hfq: pd.Series, covered: set[pd.Timestamp], limit: float
) -> list[str]:
    """反向闸门：裸价跌穿涨停，而后复权价明显抗跌 -> 必然是除权日，事件表却查无此日。"""
    idx = raw.index
    r_raw = raw.pct_change(fill_method=None)
    r_hfq = hfq.reindex(idx).ffill().pct_change(fill_method=None)
    out: list[str] = []
    hits = r_raw[r_raw < -(limit * DROP_RATIO + 0.005)]
    for dt, rr in hits.items():
        if dt.normalize() in covered:
            continue
        rb = r_hfq.get(dt, np.nan)
        if not np.isfinite(rb) or (1.0 + rb) / (1.0 + rr) - 1.0 <= RESILIENCE:
            continue  # 两列同步下跌 = 行情毛刺/异常交易，不是除权
        out.append(f"missing_event {dt.date()} raw={rr:.2%} hfq={rb:.2%}")
    return out


def covered_dates(events: pd.DataFrame, steps: set[pd.Timestamp]) -> set[pd.Timestamp]:
    """反向闸门的“已解释”集合 = 事件表日期 ∪ 实际生效的阶跃日。"""
    got = set(pd.DatetimeIndex(pd.to_datetime(events["time"])).normalize())
    return got | set(steps)


def factor_is_additive(factor: np.ndarray) -> bool:
    """判断一段存量 factor 是否为逐日漂移的加法口径（变化日占比过高）。"""
    f = np.asarray(factor, dtype=np.float64)
    f = f[np.isfinite(f) & (f > 0)]
    if f.size < 30:
        return False
    rel = np.abs(np.diff(f) / f[:-1])
    changed = float((rel > STEP_EPS).mean())
    monotone_ok = bool((np.diff(f) / f[:-1] > -STEP_EPS).all())
    if not monotone_ok:
        return True  # 乘法因子不该回落，回落即旧口径
    return changed > ADDITIVE_LOOKALIKE
