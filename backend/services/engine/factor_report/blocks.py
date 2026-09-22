"""单因子详情的**读时派生层**（对外唯一入口）—— 机构级报告的九个块在这里合成。

``build_factor_report.py`` 只算「必须看到当日横截面矩阵才算得出来」的东西
（半截面 IC、中性化 IC、分域 IC、风格相关、逐组换手、可交易轨）。其余全部在
**这里**从已存的逐日序列派生 —— 构建耗时不变、页面仍是毫秒级。

口径常量与「三个必须知道的口径陷阱」写在 :mod:`blocks_common`，本模块只是组装方。
分块：

===============  ==========================================================
``blocks_common``  常量 + 小工具（分工表、口径陷阱都记在那里）
``blocks_ic``      §2 显著性块 + §3 IC 块
``blocks_bench``   §6 的叶子依赖（基准取数 / 超额统计 / 分年度，**有 IO**）
``blocks``（本文件）§1 概览 · §4 分组回测 · §5 成本容量 · §6 超额 · §7 风格 ·
                   §8 稳健性 · §9 组装
===============  ==========================================================

## 两条口径的分界（改本文件前先读这一段）

**只有一条规则**：按天取均值 / 直方图 / 月度矩阵这类「描述日序列」的量服从
``lookback``；累计曲线、回撤、分年度这类「描述整段历史」的量走全窗口。
每个块里两类量各自带轴（``dates`` vs ``cum_dates_full``），前端不得混用 ——
轴与序列等长是画图的前提，不等长时前端会整张图不画（静默少一张）。

## 降级原则

任一数据源缺失（风格产物未建、某 horizon 缺列、基准取不到、掩码没有）→ 该块返回
``{"available": False, "reason": ...}``，**绝不写 0 或用空数组冒充**。前端据此显式提示。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from . import metrics as M
from .blocks_bench import bench_daily_returns, _annual, _excess_stats
from .blocks_common import (
    BENCHMARKS,
    CAPACITY_NOTE_SCOPE,
    N_QUANTILES,
    _col,
    _col_mean,
    _episodes,
    _f,
    _hist,
    _mean_std,
    _missing_block,
    _seq,
    iso_dates,
)
from .blocks_ic import ic_block, significance_block

log = logging.getLogger(__name__)

_AMOUNT_CACHE: dict[int, tuple[float, float | None]] = {}
_AMOUNT_TTL = 1800.0


# ─────────────────────────── 1. 概览：7 指标环 ───────────────────────────


def legs_turnover(df: Any, long_group: int, short_group: int) -> float | None:
    """G{long}/G{short} 两条腿的单边换手均值（7 指标环里的 Turnover）。

    数据来自构建期存下的 ``gt1..gt10``（逐组换手，窗口均值）。**不要**退回用
    ``turnover`` 列 —— 那是全截面换组比例，数值更高且与组合成本无关。
    """
    vals = []
    for g in (long_group, short_group):
        col = _col(df, f"gt{int(g)}")
        if col is not None and np.isfinite(col).any():
            vals.append(float(np.nanmean(col)))
    if not vals:
        return None
    return float(np.mean(vals))


def headline_block(
    df: Any,
    q_mat: np.ndarray,
    ic: np.ndarray,
    *,
    long_group: int,
    short_group: int,
    cost_bps: float,
    k: int,
) -> dict[str, Any]:
    """7 指标环：Returns / IR / Turnover / IC / ICIR / Fitness / Margin（毛净并列）。"""
    daily = M.ls_daily(q_mat, long_group, short_group) / max(int(k), 1)
    t_legs = legs_turnover(df, long_group, short_group)
    gross = M.brain_headline(daily, t_legs)
    rate = float(cost_bps) / 10000.0
    # 换手缺失时净口径必须整块置空：`(t_legs or 0.0)` 会让净收益恒等于毛收益，
    # 报告上看起来像「成本为零」—— 实际是我们不知道这个策略的成本是多少。
    # 缺数据要显示成「不知道」，不能显示成「没有损耗」。
    if t_legs is None:
        net_h = {"returns": None, "ir": None, "fitness": None, "cum_return": None}
    else:
        net_h = M.brain_headline(daily - t_legs * rate, t_legs)
    ic_m, ic_s = _mean_std(ic)
    icir = None
    if ic_m is not None and ic_s is not None and ic_s > 0:
        icir = ic_m / ic_s
    return {
        "available": True,
        "long_group": int(long_group),
        "short_group": int(short_group),
        "cost_bps": float(cost_bps),
        "n_dates": int(np.isfinite(daily).sum()),
        "turnover": gross["turnover"],
        # 毛口径（与 WorldQuant BRAIN 参考形态对齐：Returns/IR/Fitness/Margin 都是毛的）
        "returns": gross["returns"],
        "ir": gross["ir"],
        "fitness": gross["fitness"],
        "margin": gross["margin"],
        "cum_return": gross["cum_return"],
        "mu_daily": gross["mu_daily"],
        "sigma_daily": gross["sigma_daily"],
        "ann_vol": gross["ann_vol"],
        # 净口径：让读者一眼看见成本吃掉多少（同一套恒等式，输入换成扣费后序列）
        "net_returns": net_h["returns"],
        "net_ir": net_h["ir"],
        "net_fitness": net_h["fitness"],
        "net_cum_return": net_h["cum_return"],
        "ic": ic_m,
        "ic_std": ic_s,
        "icir": _f(icir),
    }


# ─────────────────────────── 4. 分组回测块 ───────────────────────────


def _tradable_block(df: Any, dates_all: list[str], *, long_group: int, short_group: int, k: int,
                    ls_daily: np.ndarray, cut: slice | None = None) -> dict[str, Any]:
    """可交易轨 vs 理想轨（涨跌停/停牌被挡后的真实成交口径）。

    ``df`` / ``dates_all`` / ``ls_daily`` 走**全窗口**（两条轨要同轴对照，截断任一条
    都会让「双轨差额」变成两个不同区间的终值相减 —— 差额会凭空多出窗口起点那段）。
    ``ls_daily``（噪声型）按 ``cut`` 截尾，轴用 ``dates``；累计曲线用 ``cum_dates_full``。
    """
    ql = [_col(df, f"q{i}_trad_long") for i in range(1, N_QUANTILES + 1)]
    qs = [_col(df, f"q{i}_trad_short") for i in range(1, N_QUANTILES + 1)]
    if any(c is None for c in ql) or any(c is None for c in qs):
        return _missing_block(
            "该数据集尚未构建可交易轨（构建时掩码缺失或用了 --no-tradable），需重跑构建",
            # 键名与可用路径保持一致（ideal_cum_end）：前端只读一个键，不该按可用性分支
            ideal_cum_end=_f(M.cum_curve(ls_daily)[-1] - 1.0) if ls_daily.size else None,
            tradable_cum_end=None,
        )
    # 多头腿用「可买」口径（剔涨停/停牌）、空头腿用「可卖」口径（剔跌停/停牌）——
    # 两条腿的过滤集合不同，这是本块唯一容易写反的地方。
    tr = 0.5 * (ql[int(long_group) - 1] - qs[int(short_group) - 1]) / max(int(k), 1)
    bl = _col(df, "blk_long_n")
    bs = _col(df, "blk_short_n")
    blocked = np.zeros(tr.size, dtype=bool)
    if bl is not None:
        blocked |= np.nan_to_num(bl, nan=0.0) > 0
    if bs is not None:
        blocked |= np.nan_to_num(bs, nan=0.0) > 0
    # 上游（构建期）已把非有限值洗成 NaN；这里再兜一道 —— 单个 inf 混进 cumprod 会让
    # **整条**累计曲线变 NaN，而 available 仍可能是 True（判据是「有有限值」）。
    # 与其静默出一张空图，不如就地记数并在下面显式披露。
    bad_days = int((~np.isfinite(tr)).sum())
    tr = np.where(np.isfinite(tr), tr, np.nan)
    good = np.isfinite(tr)
    ideal_cum = M.cum_curve(ls_daily)
    trad_cum = M.cum_curve(tr)
    # ⚠️ cum_curve 返回的**净值因子** ∏(1+r)（起点 1+r₀），不是累计收益。
    # 不减去 1 会得到 0.99 这种数，前端按百分数渲染成「+99%」——错 100 个百分点，
    # 而且量级看着还挺合理，最难发现的那一类。
    ideal_end = _f(ideal_cum[-1] - 1.0) if ls_daily.size else None
    trad_end = _f(trad_cum[-1] - 1.0) if tr.size else None
    return {
        "available": bool(good.any()),
        "reason": None if good.any() else "掩码区间与报告区间没有交集",
        "n_days": int(good.sum()),
        "dates": dates_all if cut is None else dates_all[cut],
        "cum_dates_full": dates_all,
        "ls_daily": _seq(tr if cut is None else tr[cut]),
        "ls_cum": _seq(trad_cum),
        "ls_dd": _f(M.max_drawdown(trad_cum)),
        "blocked_days": int(blocked.sum()),
        # 该日可交易组合算不出来（源数据非有限）—— 曲线里按 0 收益处理，但必须披露，
        # 否则「0 收益」会被读成「那天没交易」，而实际是「那天不知道」
        "invalid_days": bad_days,
        "blocked_long_total": _f(np.nansum(bl)) if bl is not None else None,
        "blocked_short_total": _f(np.nansum(bs)) if bs is not None else None,
        "ideal_cum_end": ideal_end,
        "tradable_cum_end": trad_end,
        "lost_return": _f(ideal_end - trad_end) if ideal_end is not None and trad_end is not None else None,
        "note": (
            "「双轨差额」表达的是**理想口径高估了多少**，不是「策略会亏这么多」——"
            "被挡的成交在实盘里未必全丢（可以改价、分批、提前一天建仓）。"
        ),
    }


def group_block(df: Any, q_mat: np.ndarray, dates_all: list[str], *, long_group: int,
                short_group: int, cost_bps: float, k: int,
                cut: slice | None = None) -> dict[str, Any]:
    """分组回测：多空日收益/累计/回撤/分布/月度、逐组换手与均值、持有期扫描。

    ``df`` / ``q_mat`` / ``dates_all`` 必须是**全窗口**，``cut`` 选定噪声型序列的尾巴
    （口径见 :func:`build_blocks`）：**日收益走窗口、累计曲线走全窗口**。两者各自带
    自己的日期轴（``dates`` vs ``cum_dates_full``），混用会让曲线与横轴错位。
    """
    daily = q_mat / max(int(k), 1)          # T × 10（全窗口）
    ls = M.ls_daily(q_mat, long_group, short_group) / max(int(k), 1)
    long_d = daily[:, int(long_group) - 1]
    short_d = daily[:, int(short_group) - 1]
    long_cum, short_cum, ls_cum = M.cum_curve(long_d), M.cum_curve(short_d), M.cum_curve(ls)
    # 空头腿的**做空 P&L**累积：∏(1−short_d)。与「取负多头累计」不是一回事
    # （长区间会显著分叉），故在后端算一次，前端不再各写一遍。
    short_book_cum = M.cum_curve(-short_d)
    if cut is None:
        dfw, dts_w = df, dates_all
    else:
        dfw, dts_w = df[cut].reset_index(drop=True), dates_all[cut]
    # 换手是**按天取均值** → 窗口口径，与 7 指标环的 Turnover 同源。
    # 这里若用全窗口，同一张统计表上就会出现「换手 2588 天均值 × 日收益 250 天分布」。
    t_legs = legs_turnover(dfw, long_group, short_group)
    daily_w, ls_w = daily[cut], ls[cut]
    return {
        "available": True,
        "long_group": int(long_group),
        "short_group": int(short_group),
        "groups": list(range(1, daily.shape[1] + 1)),
        # 噪声型序列（日收益/分布/月度）的轴：服从 lookback
        "dates": dts_w,
        # 累计型序列的轴：全窗口（截断累计曲线 = 从窗口起点重新起算，失去「累计」的意义）
        "cum_dates_full": dates_all,
        "group_daily_mean": _seq(np.nanmean(daily_w, axis=0)),
        "long_daily": _seq(long_d[cut]),
        "short_daily": _seq(short_d[cut]),
        "ls_daily": _seq(ls_w),
        "long_cum": _seq(long_cum),
        "short_cum": _seq(short_cum),
        "short_book_cum": _seq(short_book_cum),
        "ls_cum": _seq(ls_cum),
        "long_dd": _f(M.max_drawdown(long_cum)),
        "short_dd": _f(M.max_drawdown(short_cum)),
        "ls_dd": _f(M.max_drawdown(ls_cum)),
        "ls_dd_episodes": _episodes(ls_cum, dates_all),
        "group_turnover": [_col_mean(dfw, f"gt{i}") for i in range(1, N_QUANTILES + 1)],
        "turnover_ls": t_legs,
        "ls_dist": M.return_distribution(ls_w),
        "ls_monthly": M.monthly_matrix(dts_w, ls_w),
        "holding_sweep": _holding_sweep(dfw, t_legs, cost_bps),
        "tradable": _tradable_block(df, dates_all, long_group=long_group,
                                    short_group=short_group, k=k, ls_daily=ls, cut=cut),
    }


def _holding_sweep(df: Any, turnover: float | None, cost_bps: float) -> list[dict[str, Any]]:
    """持有期扫描。⚠️ 用的是 ``ls_{h}`` = **Q10−Q1 极值价差**（构建期只存了这一种），
    与页面上可配的 G3/G9 不是同一个组合 —— 但 5 个前瞻期口径一致，比「哪个持有期
    最优」这件事本身仍然成立。前端必须原样标注这条。"""
    ls_by_h: dict[int, np.ndarray] = {}
    for h in (1, 2, 3, 5, 10, 20):
        a = _col(df, f"ls_{h}")
        if a is not None and np.isfinite(a).any():
            ls_by_h[h] = a
    if not ls_by_h:
        return []
    return M.holding_period_sweep(ls_by_h, np.full(next(iter(ls_by_h.values())).size, turnover or 0.0),
                                  cost_bps=cost_bps)


# ─────────────────────────── 5. 成本与容量 ───────────────────────────


def _universe_median_amount(dt_int: int) -> float | None:
    """全市场当日成交额中位数（**元**）。

    单位口径不硬编码：走 ``DailyBar.amount``（CN 由 ``_detect_amount_scale`` 逐日
    自动识别后归一到元）。已实测核对：QuantDB parquet 的 ``amount`` 是**万元**
    （600036.SH 20260918：close×volume/amount ≈ 1e4），与 ``skills/quantdb-fields``
    的结论一致，而 ``DailyBar`` 把它乘 1e4 归一到元 —— 两处不冲突，是「原始 vs 归一」。
    """
    import time
    from datetime import date as _date

    hit = _AMOUNT_CACHE.get(int(dt_int))
    if hit and time.time() - hit[0] < _AMOUNT_TTL:
        return hit[1]
    val: float | None = None
    try:
        from backend.services.simulation.services.local_market_data import (
            Market,
            get_local_market_data,
        )

        s = str(int(dt_int))
        mkt = get_local_market_data(Market.CN)
        # 报告末日未必是交易日（快照构建到某天，次日起停更）→ 先回退到最近一个有行情的交易日，
        # 否则 load_date 返回空表、容量块会因为「取不到数」而永远降级。
        session = mkt.latest_trade_date(on_or_before=_date(int(s[:4]), int(s[4:6]), int(s[6:8])))
        bars = mkt.load_date(session) if session else {}
        amts = np.asarray([b.amount for b in bars.values() if b.amount > 0], dtype=np.float64)
        val = float(np.median(amts)) if amts.size else None
    except Exception as e:  # noqa: BLE001 — 容量是附加信息，取不到不该拖垮详情接口
        log.warning("全市场成交额中位数取数失败(dt=%s)：%s", dt_int, e)
    _AMOUNT_CACHE[int(dt_int)] = (time.time(), val)
    return val


def cost_block(df: Any, ls_daily: np.ndarray, end_dt: int, turnover: float | None,
               n_valid_mean: float | None) -> dict[str, Any]:
    """成本敏感性 + 盈亏平衡成本 + 简化容量估算。"""
    t_series = np.full(ls_daily.size, turnover or 0.0)
    out: dict[str, Any] = {
        "available": bool(ls_daily.size) and turnover is not None,
        "sensitivity": M.cost_sensitivity(ls_daily, t_series) if ls_daily.size else {"rows": [], "break_even_bps": None},
    }
    if not out["available"]:
        out["reason"] = "缺换手序列（逐组换手 gt1..gt10 未生成，需重跑构建）"
    med = _universe_median_amount(end_dt) if end_dt else None
    # 持仓数用当日有效样本数 / 10 组 —— 真实参与数，不是拍的常数
    n_pos = int(round(n_valid_mean / N_QUANTILES)) if n_valid_mean else None
    cap = M.capacity_estimate(turnover, med, n_pos)
    cap["median_amount_scope"] = CAPACITY_NOTE_SCOPE
    out["capacity"] = cap
    return out


# ─────────────────────────── 6. 相对基准超额 ───────────────────────────


def excess_block(dates: list[str], long_daily: np.ndarray, ls_daily: np.ndarray,
                 bench_symbol: str | None, tail: int | None = None) -> dict[str, Any]:
    """多头超额：多基准累计超额、超额回撤、分年度、超额统计量。

    ``dates`` / ``long_daily`` / ``ls_daily`` 一律传**全窗口**：本块的主体（累计超额、
    分年度、回撤区间、超额统计量）都是累计型，截断会让「分年度」只剩窗口内那两根柱子、
    累计曲线从窗口起点重新起算。仅 ``ls_dates`` / ``ls_daily`` 这对**噪声型**输出按
    ``tail`` 截尾（它们与 :class:`GroupBlock` 的 ``ls_daily`` 同源，同属窗口口径）。

    ⚠️ 本函数与 ``bench_daily_returns``（:mod:`blocks_bench`）必须留在同一命名空间：
    它是测试替换基准取数的唯一接缝，分居两处会导致「一半被替换、一半没有」。
    """
    order = [bench_symbol] if bench_symbol else []
    order += [s for s, _ in BENCHMARKS if s != bench_symbol]
    items: list[dict[str, Any]] = []
    primary: dict[str, Any] | None = None
    # 描述「日序列」的量（直方图 / 尾部）服从窗口，与同页的多空日收益序列同口径 ——
    # 否则直方图的 n 会比它旁边那条时间序列长一个数量级，两个数看着都合理却不同源。
    w = slice(None) if tail is None else slice(max(0, len(dates) - tail), len(dates))
    for sym in order:
        name = dict(BENCHMARKS).get(sym, sym)
        b = bench_daily_returns(sym, dates)
        if b is None or not np.isfinite(b).any():
            items.append({"symbol": sym, "name": name, "available": False,
                          "reason": "该指数取不到数据（QuantDB index_daily 缺该代码或同步未覆盖）"})
            continue
        exc = long_daily - b
        exc_cum = M.cum_curve(exc)
        exc_w = exc[w]                        # 只给直方图/尾部用；统计量与曲线仍走全窗口
        dist = M.return_distribution(exc_w)
        entry = {
            "symbol": sym, "name": name, "available": True,
            "n_days": int(np.isfinite(exc).sum()),
            "long_excess_cum": _seq(exc_cum),
            "long_excess_dd": _f(M.max_drawdown(exc_cum)),
            "excess_annual": _f(np.nanmean(exc) * M.TRADING_DAYS),
            "excess_stats": _excess_stats(long_daily, b, exc),
            "excess_hist": _hist(exc_w),
            "top_drawdowns": _episodes(exc_cum, dates),
            "annual": _annual(dates, long_daily, ls_daily, b),
            "cvar_95": dist["cvar_95"],
            "cvar_99": dist["cvar_99"],
        }
        items.append(entry)
        if primary is None:
            primary = entry
    if primary is None:
        return _missing_block(
            "所有基准指数都取不到（QuantDB index_daily 缺失或未同步）", benchmarks=items
        )
    return {
        "available": True,
        "bench_symbol": primary["symbol"],
        "bench_name": primary["name"],
        "benchmarks": items,
        # 主基准的字段平铺，保持前端既有读取路径不变
        "dates": dates,
        "long_excess_cum": primary["long_excess_cum"],
        "long_excess_dd": primary["long_excess_dd"],
        "excess_annual": primary["excess_annual"],
        "excess_stats": primary["excess_stats"],
        "excess_hist": primary["excess_hist"],
        "top_drawdowns": primary["top_drawdowns"],
        "annual": primary["annual"],
        "cvar_95": primary["cvar_95"],
        "cvar_99": primary["cvar_99"],
        "ls_dates": dates if tail is None else dates[len(dates) - tail:],
        "ls_daily": _seq(ls_daily if tail is None else ls_daily[len(dates) - tail:]),
    }


# ─────────────────────────── 7. 风格块 ───────────────────────────


def style_block(df: Any, dates_all: list[str], ls_daily: np.ndarray,
                long_excess: np.ndarray | None, horizon: int) -> dict[str, Any]:
    """Barra 风格（自算 CNE5 式口径）：均值相关表 + 日序列 + 纯因子收益归因回归。"""
    from .style_model import STYLE_LABELS, STYLE_NAMES, load_pure_returns

    cols = {s: _col(df, f"sc_{s}") for s in STYLE_NAMES}
    if all(c is None for c in cols.values()):
        return _missing_block(
            "该数据集未构建风格相关性（风格产物缺失或构建时未接 --style-dir），需先跑 "
            "backend/scripts/build_style_factors.py 再重跑报告构建"
        )
    rows = []
    for i, s in enumerate(STYLE_NAMES):
        a = cols[s]
        mean, sd = _mean_std(a) if a is not None else (None, None)
        rows.append({
            "rank": i + 1, "style": s, "label": STYLE_LABELS.get(s, s),
            "mean_corr": mean, "std_corr": sd,
            "n_days": int(np.isfinite(a).sum()) if a is not None else 0,
        })
    rows.sort(key=lambda r: abs(r["mean_corr"] or 0.0), reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    out: dict[str, Any] = {
        "available": True,
        # 风格目录随 STYLE_NAMES 走：前端口径声明按它列举风格名，避免前端再写一份
        # 会过期的硬编码清单（expanded 到 12 风格时就是被这份硬编码漏掉的）。
        "styles": [{"key": s, "label": STYLE_LABELS.get(s, s)} for s in STYLE_NAMES],
        "exposures": rows,
        "exposure_ts": {s: _seq(cols[s]) for s in STYLE_NAMES if cols[s] is not None},
        "dates": dates_all,
        "excess_corr": [],
        "attribution": None,
    }
    pure = load_pure_returns(horizon)
    if pure is None:
        out["attribution_reason"] = "风格纯因子收益产物缺失（returns.parquet 无该前瞻期）"
        out["excess_corr_reason"] = out["attribution_reason"]
        return out
    dt_keys = {int(d.replace("-", "")): i for i, d in enumerate(dates_all)}
    common = sorted(set(dt_keys) & set(pure))
    if len(common) < len(STYLE_NAMES) + 5:
        out["attribution_reason"] = f"风格收益与报告日期交集仅 {len(common)} 天，不足以回归"
        out["excess_corr_reason"] = out["attribution_reason"]
        return out
    idx = np.asarray([dt_keys[d] for d in common])
    style_all = {s: np.asarray([pure[d][s] for d in common], dtype=np.float64) for s in STYLE_NAMES}
    # 代码风格集与产物风格集可能错位（新增风格后产物尚未重建）：整列无覆盖的风格
    # 若硬塞进回归，style_attribution 会把它按行丢弃 → 全体行被剔光 → α 直接变 None。
    # 剔除必须**点名**落进产物：报告上「回归里少了一个风格」否则完全看不出来。
    style_ret = {s: v for s, v in style_all.items() if np.isfinite(v).any()}
    dropped = [s for s in style_all if s not in style_ret]
    if dropped:
        out["attribution_dropped_styles"] = dropped
    if not style_ret:
        out["attribution_reason"] = "风格纯因子收益在报告区间内全部无覆盖（产物可能未重建）"
        return out
    out["attribution"] = M.style_attribution(ls_daily[idx], style_ret)
    if long_excess is not None:
        out["excess_attribution"] = M.style_attribution(long_excess[idx], style_ret)
        corr_rows = M.style_correlations(long_excess[idx], style_ret) or []
        # 中文名在**这一层**补：metrics 是纯函数层，不该依赖 style_model 的展示字典。
        # 不补的话前端表头只有英文标识（它读的是 label 字段）——静默地少半张表。
        for r in corr_rows:
            r["label"] = STYLE_LABELS.get(r["style"], r["style"])
        out["excess_corr"] = corr_rows
    else:
        # 空数组必须自带原因：页面把空数组渲染成一个虚线提示框，
        # 不写原因的话「基准取不到」与「构建期没接」在界面上长得一模一样。
        out["excess_corr_reason"] = (
            "多头超额收益序列不可用（基准取不到，或与报告日期无交集），无法计算风格相关性"
        )
    out["n_attribution_days"] = len(common)
    return out


# ─────────────────────────── 8. 稳健性 ───────────────────────────


def robust_block(df: Any, dates_all: list[str], ic: np.ndarray, ls_daily: np.ndarray,
                 turnover: float | None) -> dict[str, Any]:
    """稳健性分段 + 拥挤度 + 样本外衰减 + 市场状态分段。"""
    segs = M.sub_period_stats(ic, k=4)
    for s in segs:
        i0, i1 = s.get("i0"), s.get("i1")
        s["start"] = dates_all[i0] if isinstance(i0, int) and i0 < len(dates_all) else None
        s["end"] = dates_all[i1 - 1] if isinstance(i1, int) and 0 < i1 <= len(dates_all) else None
    half = ic.size // 2
    in_ic, out_ic = _mean_std(ic[:half])[0], _mean_std(ic[half:])[0]
    t_s = _col(df, "turnover")
    return {
        "available": True,
        "sub_period": segs,
        "ic_stability": _f(_stability(segs)),
        "oos": {
            "in_sample_ic": in_ic, "out_sample_ic": out_ic,
            "decay": _f(out_ic - in_ic) if in_ic is not None and out_ic is not None else None,
            "note": "前一半为样本内、后一半为样本外（等分，非滚动）；衰减为负说明近期有效性下滑。",
        },
        "crowding": M.crowding_score(ic, t_s if t_s is not None else np.array([])),
        "regime": _regime_block(dates_all, ls_daily),
    }


def _stability(segs: list[dict[str, Any]]) -> float | None:
    """分段 ICIR 的一致性：|均值| / 标准差（分段之间越一致值越大）。"""
    v = np.asarray([s["icir"] for s in segs if s.get("icir") is not None], dtype=np.float64)
    if v.size < 2 or float(v.std(ddof=1)) <= 0:
        return None
    return float(abs(v.mean()) / v.std(ddof=1))


def _regime_block(dates_all: list[str], ls_daily: np.ndarray) -> list[dict[str, Any]] | None:
    """按市场状态（牛/熊/震荡）分段的因子表现。

    状态口径取 ``shared/market_regime`` 的唯一谓词（与全平台同源），
    **只喂 close**（``volumes=None``）→ 只用 ret/vol 主分支，
    成交量分支不参与，避免拿 1.0 的常数序列算出一个假的量比。
    """
    try:
        from backend.shared.benchmark import load_index_frame
        from backend.shared.market_regime import DEFAULT_REGIME_INDEX, build_state_series

        # 状态判定要的是指数**点位**（滚动收益/波动/量比），不是日收益序列，
        # 故这里直接取点位；日期须与 ``dates_all`` 同为 ISO 格式，否则状态映射全 miss。
        frame = load_index_frame(
            columns=("close",), symbols=[DEFAULT_REGIME_INDEX]
        ).sort_values("time")
        idx_dates = frame["time"].dt.strftime("%Y-%m-%d").tolist()
        idx_close = [float(v) for v in frame["close"].astype(float)]
    except Exception as e:  # noqa: BLE001 — 状态分段是附加信息，取不到不该拖垮详情
        log.warning("市场状态取数失败：%s", e)
        return None
    states = build_state_series(idx_close, None, idx_dates)
    if not states:
        return None
    buckets: dict[str, list[int]] = {}
    for i, d in enumerate(dates_all):
        st = states.get(d)
        if st is None:
            continue
        buckets.setdefault(st, []).append(i)
    out = []
    for st in ("bull", "neutral", "bear"):
        idx = buckets.get(st) or []
        v = np.asarray([ls_daily[i] for i in idx], dtype=np.float64)
        f = v[np.isfinite(v)]
        out.append({
            "regime": st, "n_days": int(f.size),
            "ls_mean_daily": _f(f.mean()) if f.size else None,
            "ls_annual": _f(f.mean() * M.TRADING_DAYS) if f.size else None,
        })
    return out


# ─────────────────────────── 9. 组装 ───────────────────────────


def definitions() -> dict[str, str]:
    """口径文案的唯一来源：前端 ⓘ 悬停直接读它，避免前后端各写一份而漂移。"""
    return dict(M.DEFINITIONS)


def build_blocks(
    df: Any,
    *,
    factor: str,
    horizon: str,
    lookback: int,
    long_group: int,
    short_group: int,
    cost_bps: float,
    snapshot: dict[str, Any] | None,
    bench_symbol: str | None = None,
) -> dict[str, Any]:
    """把该因子的**全窗口**逐日序列装配成九个块。

    Args:
        df: 全窗口序列（未按 lookback 截断）——累计型指标要全窗口才有意义。
        lookback: 噪声型序列（日 IC / 日收益）保留的点数；**7 指标环同此窗口**
            （它与 IC/ICIR 是一组要一起读的数）。传 ``<= 0`` 表示整段全窗口。
            累计型曲线（净值、累计 IC、超额累计）不受影响，一律走全窗口。

    **两条口径的分界只有一条**：按天取均值 / 直方图 / 月度矩阵这类「描述日序列」的量
    服从 ``lookback``；累计曲线、回撤、分年度这类「描述整段历史」的量走全窗口。
    每个块里两类量各自带轴（``dates`` vs ``cum_dates_full``），前端不得混用 ——
    轴与序列等长是画图的前提，不等长时前端会整张图不画（静默少一张）。
    """
    k = int(horizon.split("_")[-1])
    dates_all = iso_dates(df["date"].tolist())
    dt_ints = [int(v) for v in df["date"].tolist()]
    q_mat = df[[f"q{i}" for i in range(1, N_QUANTILES + 1)]].to_numpy(dtype=np.float64)
    ic_all = df["ic"].to_numpy(dtype=np.float64)
    # lookback <= 0 = 全窗口（导出脚本走这条；页面被路由层限制在 20..1200）。
    # 若写成 max(lookback, 20)，lookback=0 会静默变成「只留最近 20 天」——
    # 报告上头部统计用全窗口、分布图却只有 20 天，两个数字对不上。
    n_tail = len(dates_all) if lookback <= 0 else min(max(int(lookback), 20), len(dates_all))
    cut = slice(len(dates_all) - n_tail, len(dates_all))

    # ls_full / long_full 走**全窗口** —— 累计净值、超额、风格归因这些曲线要全历史才有意义
    ls_full = M.ls_daily(q_mat, long_group, short_group) / max(k, 1)
    long_full = q_mat[:, int(long_group) - 1] / max(k, 1)
    # 7 指标环走**请求窗口**：它与 IC/ICIR（`ic_all[cut]`）是一组要一起读的数，
    # 之前 IC 取窗口、Returns 取全窗口，环上 ICIR 描述 250 天而 IR 描述 2588 天。
    # 累计型序列不受影响（曲线用 ls_full）。
    head = headline_block(df[cut].reset_index(drop=True), q_mat[cut], ic_all[cut],
                          long_group=long_group, short_group=short_group,
                          cost_bps=cost_bps, k=k)
    icb = ic_block(df, dates_all, cut, snapshot, factor, k)
    # 分组块自己再切一次：日收益/分布/月度走窗口，累计曲线走全窗口（见 group_block）。
    # 全窗口在这里传入、切片在里面做 —— 若在外面切完再传，累计曲线就只剩窗口那一段，
    # 而它的轴（cum_dates_full）是全窗口，图会从中间开始画。
    grp = group_block(df, q_mat, dates_all, cut=cut,
                      long_group=long_group, short_group=short_group, cost_bps=cost_bps, k=k)
    # 超额块走**全窗口**：它与 IC 块的累计 IC 是同一类展品（累计曲线 + 分年度 + 回撤区间），
    # 截断到请求窗口会让「分年度超额收益」只剩两根柱子、累计曲线从窗口起点重新起算，
    # 而同一页签的历史含义就没了。日频序列（HEADLINE / 分组）仍按窗口。
    # ⚠️ 与 style_block 的对齐：下面用**全窗口** excess_full 再 `[cut]`，两者必须同源。
    exc = excess_block(dates_all, long_full, ls_full, bench_symbol, tail=n_tail)
    # 风格归因用的超额序列按下标与 dates_all / ls_full 对齐 —— 先在全窗口上算出来，
    # 下游与这三者一起切同一段（长度不一致会错位，不是报错而是静默算错）
    excess_full = None
    if exc.get("available") and exc.get("bench_symbol"):
        b_full = bench_daily_returns(exc["bench_symbol"], dates_all)
        if b_full is not None:
            excess_full = long_full - b_full
    return {
        "headline": head,
        "significance": significance_block(ic_all[cut], ls_full[cut], snapshot, factor),
        "ic_block": icb,
        "group_block": grp,
        "cost_block": cost_block(df[cut].reset_index(drop=True), ls_full[cut], dt_ints[-1],
                                 head["turnover"], _col_mean(df[cut], "n_valid")),
        "excess_block": exc,
        # 风格块同样走请求窗口：df / dates_all / ls / excess 必须**同切**，否则
        # 归因回归里日期索引与收益序列会错位（这四者是按下标对齐的）
        "style_block": style_block(df[cut].reset_index(drop=True), dates_all[cut], ls_full[cut],
                                   None if excess_full is None else excess_full[cut], k),
        "robust_block": robust_block(df[cut].reset_index(drop=True), dates_all[cut],
                                     ic_all[cut], ls_full[cut], head["turnover"]),
        "definitions": definitions(),
    }


__all__ = [
    # §9 组装 —— service.py 只认这一个
    "build_blocks",
    # 各块（导出脚本 / 测试按名调用）
    "cost_block",
    "definitions",
    "excess_block",
    "group_block",
    "headline_block",
    "ic_block",
    "legs_turnover",
    "robust_block",
    "significance_block",
    "style_block",
    # 测试替换基准取数的**唯一接缝**（必须与本模块的 excess_block 同命名空间）
    "bench_daily_returns",
    # 小工具：`iso_dates` 被测试与导出脚本直接调用
    "iso_dates",
]
