"""P2 回放配对统计（纯函数，无 IO）：共同日期 ∩ 共同池上的 ΔIC 与月度/换手汇总。

设计 §5.1 的「vintage 回放」层：挑战者与冠军在同一段样本外窗口上必须
**先取共同日期、再取共同池**，然后逐日配对求 ΔIC = challenger_ic − champion_ic
——跨池直接比均值会得出方向相反的结论（实测教训：跨池比均值必先取共同池）。

纪律：

- 秩退化日 / 单票日 / 一侧缺 IC 的日期一律**不参与配对**，但都逐项计数——
  缺口不许无声消失；
- t 用样本标准差（ddof=1）与**单侧** p；零方差如实返回 ``t=None``（不编 ±inf
  装显著）；样本不足如实说不足；
- 月度统计默认取近 12 个月（G3 口径）；
- 换手口径转发 ``realized_stats``（topk_by_date + topk_turnover），不重造公式。

IO 在 ``walkforward_rollup.py``（campaign 拼接、模型目录解析、落盘），
闸门口径在 ``backend/shared/model_rollout.py``（G0-G6，吃本模块产出的证据 dict）。
"""

from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from backend.scripts.eval.realized_stats import (
    TRADING_DAYS,
    daily_ic,
    topk_by_date,
    topk_turnover,
)
from backend.shared.stock_utils import StockCodeUtil

#: 与 ``model_realized.DEFAULT_TOP_K`` 对齐（评分卡与配对检验换手截断同一口径）
DEFAULT_TOP_K = 50

#: 配对 t 检验的最少天数（少于 2 天样本标准差无定义）
MIN_PAIRED_DAYS = 2

_A_SHARE_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")
_HK_PREFIX_RE = re.compile(r"^HK(\d{4,5})$")
_HK_BARE_RE = re.compile(r"^\d{4,5}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

#: 标签一致性判据（同一 (日期, 标的) 的已实现收益两侧应逐位相同；超过即计数留痕）
_LABEL_EPS = 1e-9

#: 证据里日期列表的截断长度（全量在配对序列里，这里只是给人看的样本）
_DATE_SAMPLE_CAP = 20
_SYMBOL_SAMPLE_CAP = 3


def canon_symbol(raw: Any) -> str:
    """任意键形 → 配对键。

    A 股（裸 6 位 / 后缀 / 前缀）统一为 prefix（``SH600036``）；港股各形态
    统一为 4 位 + ``.HK``（与训练产物口径一致）；其余（美股 Ticker 等）
    大写去空白。两侧同一函数归一后做等值配对，形态分叉只会表现为
    「共同池为空」而不是静默错配——那种情况由 ``paired_daily_delta`` 的
    ``symbols_sample`` 字段直接暴露。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    up = s.upper()
    p = StockCodeUtil.to_prefix(up)
    if _A_SHARE_RE.match(p):
        return p
    hk_match = _HK_PREFIX_RE.match(up)
    if hk_match:
        up = hk_match.group(1)
    if up.endswith(".HK") or _HK_BARE_RE.match(up):
        hk = StockCodeUtil.to_hk_suffix(up)
        if hk:
            return hk
    return up


def prepare_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """已归一帧（``date_key/symbol/pred/label``）→ 配对可用帧 + 剔除计数。

    剔除项：空符号、pred/label 非有限值、重复 ``(date_key, symbol)``（keep last
    ——多 vintage 拼接时窗口意外重叠会走到这里，留痕不静默）。入参不被修改。
    """
    required = ("date_key", "symbol", "pred", "label")
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(
            f"配对帧缺列 {missing}：需先过 normalize_pred_frame（pred/label/trade_date）"
        )
    out = frame.loc[:, list(required)].copy()
    rows_in = len(out)
    out["symbol"] = out["symbol"].map(canon_symbol)
    out["pred"] = pd.to_numeric(out["pred"], errors="coerce")
    out["label"] = pd.to_numeric(out["label"], errors="coerce")
    out["date_key"] = out["date_key"].astype(str)

    before = len(out)
    out = out[out["symbol"] != ""]
    dropped_symbol = before - len(out)

    before = len(out)
    out = out[np.isfinite(out["pred"]) & np.isfinite(out["label"])]
    dropped_nan = before - len(out)

    before = len(out)
    out = out.drop_duplicates(subset=["date_key", "symbol"], keep="last")
    dropped_dup = before - len(out)

    notes = {
        "rows_in": int(rows_in),
        "rows_out": int(len(out)),
        "dropped_symbol": int(dropped_symbol),
        "dropped_nan": int(dropped_nan),
        "dropped_dup": int(dropped_dup),
    }
    return out, notes


def _side_profile(frame: pd.DataFrame) -> dict[str, Any]:
    """单侧概览（天数/行数/日期跨度/符号样本）——池对不上时先看这里。"""
    if frame.empty:
        return {"n_days": 0, "n_rows": 0, "date_span": None, "symbols_sample": []}
    dates = sorted(set(frame["date_key"]))
    return {
        "n_days": len(dates),
        "n_rows": int(len(frame)),
        "date_span": [dates[0], dates[-1]],
        "symbols_sample": sorted(set(frame["symbol"]))[:_SYMBOL_SAMPLE_CAP],
    }


def paired_daily_delta(
    challenger: pd.DataFrame,
    champion: pd.DataFrame,
    *,
    challenger_notes: dict[str, Any] | None = None,
    champion_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """共同日期 ∩ 共同池上的逐日配对 IC 与 ΔIC（**入参须先过 ``prepare_frame``**）。

    逐日 IC 在**该日交集池**上分别计算（同一批行、各自模型），因此两侧 IC 严格
    可比；一侧缺 IC（秩退化）的日期不配对但计数。返回证据 dict（JSON 可序列化），
    ``delta`` 键按日期升序。
    """
    c_dates = set(challenger["date_key"])
    h_dates = set(champion["date_key"])
    merged = challenger.merge(champion, on=["date_key", "symbol"], suffixes=("_c", "_h"))

    base: dict[str, Any] = {
        "delta": {},
        "challenger_ic": {},
        "champion_ic": {},
        "n_days_paired": 0,
        "n_days_rows_common": 0,
        "n_days_thin_common": 0,
        "n_days_challenger_no_ic": 0,
        "n_days_champion_no_ic": 0,
        "n_dates_only_challenger": len(c_dates - h_dates),
        "dates_only_challenger": sorted(c_dates - h_dates)[:_DATE_SAMPLE_CAP],
        "n_dates_only_champion": len(h_dates - c_dates),
        "dates_only_champion": sorted(h_dates - c_dates)[:_DATE_SAMPLE_CAP],
        "pool_median": None,
        "pool_min": None,
        "pool_max": None,
        "overlap_share_challenger_median": None,
        "overlap_share_champion_median": None,
        "n_label_mismatch_rows": 0,
        "label_mismatch_dates": [],
        "challenger": _side_profile(challenger),
        "champion": _side_profile(champion),
        "prep": {"challenger": challenger_notes, "champion": champion_notes},
    }
    if merged.empty:
        return base

    sizes = merged.groupby("date_key").size()
    common_dates = set(sizes.index)
    base["n_days_rows_common"] = len(common_dates)
    base["n_days_thin_common"] = int((sizes < 2).sum())
    base["pool_median"] = int(sizes.median())
    base["pool_min"] = int(sizes.min())
    base["pool_max"] = int(sizes.max())

    c_sizes = challenger.groupby("date_key").size()
    h_sizes = champion.groupby("date_key").size()
    share_c = (sizes / c_sizes.reindex(sizes.index)).median()
    share_h = (sizes / h_sizes.reindex(sizes.index)).median()
    base["overlap_share_challenger_median"] = round(float(share_c), 6)
    base["overlap_share_champion_median"] = round(float(share_h), 6)

    diff = (merged["label_c"] - merged["label_h"]).abs()
    mismatch = merged.loc[diff > _LABEL_EPS, "date_key"]
    base["n_label_mismatch_rows"] = int(len(mismatch))
    base["label_mismatch_dates"] = sorted(set(mismatch))[:_DATE_SAMPLE_CAP]

    dates_arr = merged["date_key"].to_numpy()
    ic_c = daily_ic(
        merged["pred_c"].to_numpy(), merged["label_c"].to_numpy(), dates_arr
    )
    ic_h = daily_ic(
        merged["pred_h"].to_numpy(), merged["label_h"].to_numpy(), dates_arr
    )
    base["challenger_ic"] = ic_c
    base["champion_ic"] = ic_h

    # 非单票的共同日里、某一侧没算出 IC = 该侧秩退化（daily_ic 对退化日不产 IC）
    nonthin = {d for d in common_dates if sizes[d] >= 2}
    base["n_days_challenger_no_ic"] = len(nonthin - set(ic_c))
    base["n_days_champion_no_ic"] = len(nonthin - set(ic_h))

    paired = {
        d: round(ic_c[d] - ic_h[d], 6) for d in sorted(set(ic_c) & set(ic_h))
    }
    base["delta"] = paired
    base["n_days_paired"] = len(paired)
    return base


def summarize_delta(
    delta_by_date: dict[str, float], *, min_days: int = MIN_PAIRED_DAYS
) -> dict[str, Any]:
    """ΔIC 日序 → mean/std(ddof=1)/单侧 t/p 与正比例。

    样本 < ``min_days`` → ``sufficient=False`` + reason，但仍给出 mean/median
    （有多少说多少）。std 零方差 → ``t=None`` + reason（t 无定义，不编 ±inf）。
    """
    out: dict[str, Any] = {
        "sufficient": False,
        "reason": None,
        "n_days": 0,
        "mean": None,
        "median": None,
        "std": None,
        "t": None,
        "p_one_sided": None,
        "share_positive": None,
    }
    values = [float(v) for _, v in sorted(delta_by_date.items())]
    n = len(values)
    out["n_days"] = n
    if n == 0:
        out["reason"] = "无可配对日期（共同日期 ∩ 共同池为空）"
        return out
    arr = np.asarray(values, dtype=float)
    out["mean"] = round(float(arr.mean()), 6)
    out["median"] = round(float(np.median(arr)), 6)
    out["share_positive"] = round(float((arr > 0).mean()), 6)
    if n < min_days:
        out["reason"] = f"配对天数不足（{n} < {min_days}）"
        return out
    std = float(arr.std(ddof=1))
    out["std"] = round(std, 6)
    out["sufficient"] = True
    if std <= 1e-12:
        out["reason"] = "ΔIC 零方差（逐日完全相同），t 无定义"
        return out
    t = float(arr.mean() / (std / math.sqrt(n)))
    out["t"] = round(t, 4)
    out["p_one_sided"] = round(float(scipy_stats.t.sf(t, df=n - 1)), 6)
    return out


def monthly_ic_stats(
    ic_by_date: dict[str, float | None], *, last_months: int = 12
) -> dict[str, Any]:
    """逐日 IC → 月度均值与近 N 个月的最差月/月间 std（G3 口径）。

    月键取日期前 7 位（``YYYY-MM``）；按时间轴取**最近** ``last_months`` 个月
    （不按字典序截断——跨年时字典序恰好一致，但口径写成「时间轴」更稳）。
    月间 std 需 ≥2 个月，否则如实 ``None``。
    """
    by_month: dict[str, list[float]] = {}
    for date_key, value in ic_by_date.items():
        month = str(date_key)[:7]
        if value is None or not _MONTH_RE.match(month):
            continue
        by_month.setdefault(month, []).append(float(value))
    all_months = sorted(by_month)
    kept = all_months[-last_months:] if last_months > 0 else all_months
    months = {m: round(float(np.mean(by_month[m])), 6) for m in kept}
    out: dict[str, Any] = {
        "months": months,
        "n_months": len(months),
        "n_months_total": len(all_months),
        "window": [kept[0], kept[-1]] if kept else None,
        "worst_month": None,
        "worst": None,
        "month_std": None,
    }
    if not months:
        return out
    worst_m = min(months, key=lambda m: months[m])
    out["worst_month"] = [worst_m, months[worst_m]]
    out["worst"] = months[worst_m]
    if len(kept) >= 2:
        vals = np.asarray([months[m] for m in kept], dtype=float)
        out["month_std"] = round(float(vals.std(ddof=1)), 6)
    return out


def annualized_turnover(
    frame: pd.DataFrame,
    *,
    k: int = DEFAULT_TOP_K,
    round_trip_cost: float | None = None,
    trading_days: int = TRADING_DAYS,
) -> dict[str, Any]:
    """Top-k 日换手与年化成本拖累（转发 ``realized_stats`` 的既有口径）。

    ``frame`` 须是配对帧（``prepare_frame`` 输出，含 ``date_key/symbol/pred``）。
    ``round_trip_cost=None`` 时取 ``CostModel()`` 默认费率（A 股口径；调用方
    可传模型 metadata 解析出的费率覆盖——费率唯一出处仍是 ``trading_cost``）。
    """
    if round_trip_cost is None:
        # 局部引入：纯统计模块不背服务层依赖，只在需要默认费率时碰一次
        from backend.services.engine.inference.trading_cost import CostModel

        round_trip_cost = CostModel().round_trip_cost()
    ranked = topk_by_date(frame, k=k)
    return topk_turnover(
        ranked, k=int(k), round_trip_cost=float(round_trip_cost), trading_days=trading_days
    )
