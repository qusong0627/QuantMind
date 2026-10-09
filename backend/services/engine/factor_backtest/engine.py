"""T-FB-04 求值核心：因子 × 市场 → 指标 + 序列 + 终态。

设计边界（与现行 CN 回测的关系）：

- **执行/对齐/IC 口径全部复用** ``alpha_agent`` 现成实现（import 不改旧码）：
  子进程隔离执行、因子-价格对齐、挖掘同源富化 h5、取消机制（共享
  ``_backtest_processes``/``_backtest_cancelled``，按 factor_id 取消）。
- 本模块只负责**装配与裁决**：市场档案 → universe 解析 → 数据装载 →
  状态机（ok/data_unsupported/insufficient/unavailable/failed）→ 指标与
  序列（``ic.py``）。
- **跨市场 run 绝不改 rd_agent_factors 行**（那是 CN 因子列表的家，写一次
  就会被 HK/US 指标覆盖）；落账由 ``store.py``/路由层走 run 台账。

修复的一处旧缺陷：旧 qlib 路径 fields 只有 6 列（缺 ``$amount``），17 条
金额档因子在非 CN 必挂——本引擎 fields 带上 ``$amount``（五市场 bin 实测同构）。

并发纪律：子进程登记按 factor_id 键，**同一因子不得跨市场并发**——矩阵/批量
引擎逐市场串行循环同一因子（路由层用 ``_running_backtests`` 去重兜底）。
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
import pandas as pd

from backend.services.engine.factor_backtest import profiles as profiles_mod
from backend.services.engine.factor_backtest.benchmarks import load_benchmark_returns
from backend.services.engine.factor_backtest.compat import classify_factor
from backend.services.engine.factor_backtest.ic import (
    DEFAULT_N_BUCKETS,
    DEFAULT_TOP_PCT,
    build_series_payload,
    daily_ic_series,
    daily_ic_stats,
    perf_metrics,
    portfolio_curves,
)
from backend.services.engine.factor_backtest.profiles import (
    MarketProfile,
    columns_for_market,
    effective_cost_bps,
    effective_min_days,
    effective_universe_top_n,
    get_market_profile,
)
from backend.services.engine.routers.alpha_agent import (
    FactorBacktestCancelled,
    _align_factor_returns,
    _detect_factor_kind,
    _format_backtest_error,
    _resolve_instruments_for_universe,
    _resolve_mining_source_h5,
    _run_factor_class_subprocess,
    _run_functional_factor_subprocess,
    _run_mining_evaluators,
)

logger = logging.getLogger(__name__)

#: 五市场 qlib bin 实测列（含 ``$amount``——旧路径漏了它，金额档因子会挂）。
_FEATURE_FIELDS = ["$open", "$high", "$low", "$close", "$volume", "$factor", "$amount"]

#: 动态流动性池回看交易日数（60 日日均成交额）。
_LIQUID_LOOKBACK_DAYS = 60

#: 进程内动态池缓存：key=(qlib_market, top_n, end) → 名单（只留最新一份）。
_TOP_N_CACHE: dict[tuple, list[str]] = {}

#: 拆股形态判别（T-FB-05 硬闸）。非 CN 行情未复权（US/HK bin 的 ``$factor``
#: 恒 1.0）：拆股日 close-to-close 收益 ≈ -(1 - 1/k)（10:1 ≈ -89.9%），
#: 反向拆股 ≈ +(k-1)（1:5 ≈ +401%）。真实大跌/暴涨几乎不落在「整数比形态」
#: 上——按隐含比例距整数 k 的相对容差判别。2026-10-09 美股全池 3 年校准：
#: 65 个 |ret|≥0.4 bar 中掩码 29 个拆股形态（NVDA k=10、CMG k=50、WMT k=3…），
#: 36 个非整数比真实暴动（SBNY 危机期 ±50%~+450% 连续跳动）原样保留。
_SPLIT_DROP_THRESHOLD = -0.35  # 跌向拆股判别门槛（2:1 拆股 ≈ -50%）
_SPLIT_JUMP_THRESHOLD = 0.9  # 涨向反向拆股判别门槛（1:2 ≈ +100%）
_SPLIT_RATIO_TOLERANCE = 0.05  # |k - round(k)| / round(k) 容差上限
_SPLIT_RATIO_MIN = 2  # 最小拆分比（2:1）
_SPLIT_RATIO_MAX = 100  # 最大拆分比（100:1；再大是非拆股形态）
_EXTREME_RETURN_THRESHOLD = 0.4  # 「极端收益」计数阈值（仅计数，不掩码）


def _mask_split_like_returns(r: pd.Series) -> tuple[pd.Series, int, int]:
    """拆股形态收益掩码——返回 (掩码后序列, 掩码数, 极端收益保留数)。

    只置 NaN、不删行（下游 ``_finite_frame`` 统一剔除）：因子观测仍有效，
    只是该 instrument-day 的收益不可信。计数随 metrics 暴露，**绝不静默**。
    注意掩码必然落在 |ret| ≥ 0.4 区（k≥1.95 时跌向 ≤ -48.7% / 涨向 ≥ +95%），
    所以「极端收益保留数」= 极端区里非整数比的真实暴动条数。
    """
    values = r.to_numpy(dtype=float, copy=False)
    ratio = 1.0 + values
    # 隐含拆分比 k：跌向取 1/ratio（拆股），涨向取 ratio（反向拆股）
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.where(values <= _SPLIT_DROP_THRESHOLD, 1.0 / ratio, ratio)
    in_zone = (values <= _SPLIT_DROP_THRESHOLD) | (values >= _SPLIT_JUMP_THRESHOLD)
    k_round = np.round(k)
    masked = (
        np.isfinite(values)
        & in_zone
        & np.isfinite(k)
        & (k_round >= _SPLIT_RATIO_MIN)
        & (k_round <= _SPLIT_RATIO_MAX)
        # +1e-9：恰落窗界（如 +90.0% → k=1.9，浮点尾差把 5% 挤成 5.0000000000000044%）
        & (np.abs(k - k_round) / k_round <= _SPLIT_RATIO_TOLERANCE + 1e-9)
    )
    n_masked = int(masked.sum())
    extreme = np.isfinite(values) & (np.abs(values) >= _EXTREME_RETURN_THRESHOLD)
    n_extreme_kept = int((extreme & ~masked).sum())
    if n_masked == 0:
        return r, 0, n_extreme_kept
    out = r.copy()
    out[masked] = np.nan
    return out, n_masked, n_extreme_kept


def _ensure_qlib(qlib_market: str) -> None:
    """幂等 qlib.init（region 口径与旧实现一致）；失败只告警。"""
    import qlib

    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    provider_uri = resolve_qlib_provider_uri(qlib_market)
    try:
        qlib.init(
            provider_uri=provider_uri,
            region="cn" if qlib_market in ("CN", "HK", "FUTURES", "CRYPTO") else "us",
        )
    except Exception as e:  # noqa: BLE001 — 重复 init 快速返回；真失败由后续取数暴露
        logger.warning("qlib.init(%s) raised: %s", provider_uri, e)


def _load_features(instruments, fields, start: str, end: str) -> pd.DataFrame:
    """同步 D.features 装载（冷启动数十秒；调用方必须 to_thread）。"""
    from qlib.data import D

    return D.features(instruments, fields, start_time=start, end_time=end, freq="day")


def _liquid_top_n(qlib_market: str, top_n: int, end: str) -> list[str]:
    """动态流动性池：过去 60 交易日日均成交额 top-N（进程内缓存，只留最新）。"""
    key = (qlib_market, int(top_n), end)
    cached = _TOP_N_CACHE.get(key)
    if cached is not None:
        return list(cached)

    from qlib.data import D

    dates = D.calendar(end_time=end, freq="day")
    if len(dates) == 0:
        return []
    lookback = min(len(dates), _LIQUID_LOOKBACK_DAYS)
    start = str(pd.Timestamp(dates[-lookback]).date())
    df = D.features(
        D.instruments(market="all"),
        ["$amount"],
        start_time=start,
        end_time=end,
        freq="day",
    )
    if df is None or df.empty or "$amount" not in df.columns:
        return []
    avg = df["$amount"].groupby(level="instrument").mean().dropna()
    top = sorted(avg.nlargest(int(top_n)).index.tolist())
    _TOP_N_CACHE.clear()
    _TOP_N_CACHE[key] = top
    return list(top)


def _result(base: dict, status: str, *, reason: str | None = None, **extra) -> dict:
    """收口结果：终态 + 原因 + 耗时（序列/指标缺省 None，绝不以 0 冒充）。"""
    out = {
        **base,
        "status": status,
        "reason": reason,
        "metrics": None,
        "series": None,
    }
    out.update(extra)
    return out


async def evaluate_factor_market(
    factor: dict,
    *,
    market: str,
    universe: str | None = None,
    start: str | None = None,
    end: str | None = None,
    cost_bps: int | None = None,
    top_pct: float = DEFAULT_TOP_PCT,
    n_buckets: int = DEFAULT_N_BUCKETS,
) -> dict:
    """单因子单市场求值（终态语义见模块 docstring）。

    Args:
        factor: 因子行（``factor_id`` / ``factor_code`` 必填）。
        market: 应用侧市场键（a_share/hong_kong/us_stock/crypto/futures）。
        universe/start/end/cost_bps: 缺省按市场档案（start/end 由档案日历推算）。

    Returns: dict（``status/reason/message/metrics/series/compat/kind/window/
        universe/n_days/n_obs/elapsed_s`` 等）。

    Raises:
        KeyError: 未知市场（路由层翻 400）。
        FactorBacktestCancelled: 用户取消（路由层把台账收口为 cancelled）。
    """
    t0 = time.monotonic()
    profile = get_market_profile(market)  # 未知市场在此抛 KeyError
    factor_id = str(factor.get("factor_id") or "")
    code = str(factor.get("factor_code") or "")
    universe_value = universe or profile.default_universe

    base = {
        "factor_id": factor_id,
        "market": market,
        "label": profile.label,
        "in_sample": profile.in_sample,
        "experimental": profile.experimental,
        "kind": None,
        "compat": None,
        "data_source": "qlib_bin",
        "window": {"start": start, "end": end},
        "universe": universe_value,
        "cost_bps": None,
        "n_days": None,
        "n_obs": None,
        "message": None,
        "elapsed_s": None,
    }

    def _finish(res: dict) -> dict:
        res["elapsed_s"] = round(time.monotonic() - t0, 3)
        return res

    # ① 静态兼容性——缺列免跑（最便宜的判据放最前，失败也最先返回）
    compat = classify_factor(code, columns_for_market(market))
    base["compat"] = compat
    if compat["status"] == "data_unsupported":
        return _finish(
            _result(
                base,
                "data_unsupported",
                reason="missing_columns",
                message=f"目标市场缺少列: {', '.join(compat['missing'])}",
            )
        )

    # ② 因子类型——判不出直接失败（不进 qlib，不烧算力）
    try:
        kind = _detect_factor_kind(code)
    except RuntimeError as exc:
        return _finish(_result(base, "failed", reason="syntax_error", message=str(exc)))
    base["kind"] = kind
    if kind == "unknown":
        return _finish(
            _result(
                base,
                "failed",
                reason="unknown_factor_kind",
                message="因子代码中未找到可调用的 Factor 类或 calculate_* 函数",
            )
        )

    # ③ 数据面就绪——provider 缺失时矩阵标 unavailable（不是因子的问题）
    status = profiles_mod.profile_status(profile)
    if not status["ready"]:
        return _finish(
            _result(
                base,
                "unavailable",
                reason="provider_not_ready",
                message=f"{profile.label} qlib 数据未就绪: {status.get('provider')}",
            )
        )

    # ④ 窗口与费率
    if not start or not end:
        win_start, win_end = profiles_mod.default_window(profile)
        start = start or win_start
        end = end or win_end
    base["window"] = {"start": start, "end": end}
    if not start or not end:
        return _finish(
            _result(
                base, "unavailable", reason="window_unavailable", message="日历为空"
            )
        )
    cost = int(cost_bps if cost_bps is not None else effective_cost_bps(profile))
    base["cost_bps"] = cost
    min_days = effective_min_days()

    # ⑤ qlib 初始化必须早于 universe 解析：非 CN 的 ``D.instruments(market="all")``
    #    与 CN 原生池都要求 provider 已 init（旧实现同序；在测试里此函数被打桩）。
    _ensure_qlib(profile.qlib_market)

    # ⑥ universe 解析（HK 动态流动性池 / 其余走既有池解析，CN 缺省 csi300）
    if profile.universe_mode == "liquid_top_n":
        instruments = await asyncio.to_thread(
            _liquid_top_n, profile.qlib_market, effective_universe_top_n(profile), end
        )
        base["universe_size"] = len(instruments)
        if not instruments:
            return _finish(
                _result(
                    base, "failed", reason="universe_empty", message="动态流动性池为空"
                )
            )
    else:
        instruments = _resolve_instruments_for_universe(
            profile.qlib_market, universe_value
        )

    # ⑦ 行情装载（冷读 bin：必须 to_thread，否则看门狗把 engine 强杀）
    df = await asyncio.to_thread(
        _load_features, instruments, _FEATURE_FIELDS, start, end
    )
    if df is None or df.empty:
        return _finish(
            _result(
                base,
                "failed",
                reason="empty_data",
                message=f"Qlib 数据为空: market={market} window={start}~{end}",
            )
        )

    # ⑦ 因子值计算（subprocess 隔离；CN 优先挖掘同源富化 39 列）
    try:
        if kind == "functional":
            mining_source = (
                _resolve_mining_source_h5(market) if market == "a_share" else None
            )
            factor_series = await _run_functional_factor_subprocess(
                factor_id, code, df, source_h5=mining_source
            )
        else:
            factor_series = await _run_factor_class_subprocess(factor_id, code, df)
    except FactorBacktestCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — 失败原因收口进结果，绝不吞
        logger.exception("[factor-backtest] %s market=%s 执行失败", factor_id, market)
        return _finish(
            _result(
                base,
                "failed",
                reason="execution_error",
                message=_format_backtest_error(exc),
            )
        )
    if factor_series is None or len(factor_series) == 0:
        return _finish(
            _result(
                base,
                "failed",
                reason="empty_factor_output",
                message="因子计算无输出，请检查 calculate_* 函数或 Factor 类",
            )
        )

    # ⑧ 对齐 + 有限值清洗（与旧实现同一套规整；层序/大小写两坑都在里面）
    f, r = _align_factor_returns(factor_series, df["$close"])
    mask = np.isfinite(f.values) & np.isfinite(r.values)
    n_obs = int(mask.sum())
    base["n_obs"] = n_obs
    if n_obs == 0:
        return _finish(
            _result(
                base,
                "failed",
                reason="no_valid_pairs",
                message="因子与收益对齐后无有效样本（层序/代码大小写/列不匹配）",
            )
        )
    f_clean = pd.Series(f.values[mask], index=f.index[mask])
    r_clean = pd.Series(r.values[mask], index=r.index[mask])

    # ⑧b 拆股形态收益掩码（T-FB-05 硬闸）：未复权数据（US/HK）在拆股日有
    #     ≈-90% 之类的假收益，不掩码会污染 IC/组合曲线的当日截面。
    #    CN 已复权（$factor 真值），命中数应为 0——零回归的判据之一。
    r_clean, n_suspect, n_extreme_kept = _mask_split_like_returns(r_clean)

    # ⑨ 指标（ic.py 与挖掘/现行回测逐字同口径）
    ic_series = daily_ic_series(f_clean, r_clean)
    stats = daily_ic_stats(f_clean, r_clean)
    n_days = int(stats["n_days"])
    base["n_days"] = n_days
    if n_days == 0:
        return _finish(
            _result(
                base,
                "failed",
                reason="ic_all_nan",
                message="日度 IC 全部为 NaN，因子可能与价格列不匹配",
            )
        )
    if n_days < min_days:
        return _finish(
            _result(
                base,
                "insufficient",
                reason="too_few_days",
                message=f"有效交易日 {n_days} < 阈值 {min_days}，不足以参与跨市场对比",
            )
        )

    curves = portfolio_curves(f_clean, r_clean, top_pct=top_pct, n_buckets=n_buckets)
    perf = perf_metrics(curves["ret_long"], curves["traded"], cost)
    # 多空腿毛口径（traded 传 0 序列 → 成本项恒 0，只取毛指标）
    perf_ls = perf_metrics(curves["ret_ls"], curves["ret_ls"] * 0.0, cost)

    # ⑨b 基准列（T-FB-19）：档案请求真实指数（csi300/hsi/spx）时读 QuantDB
    #     index_daily 并换掉等权列；读数失败/覆盖不足 → 保持等权兜底。载荷
    #     bench 与 metrics.bench_used 只记**实际用上的**口径，绝不冒充指数
    #     （parquet 读取是同步重活，走 to_thread 防看门狗强杀）。
    bench_label = "equal_weight"
    if profile.benchmark != "equal_weight":
        bench_ret = await asyncio.to_thread(
            load_benchmark_returns, profile.benchmark, curves.index
        )
        if bench_ret is not None:
            curves = curves.copy()
            curves["bench"] = bench_ret
            bench_label = profile.benchmark

    payload = build_series_payload(
        curves, ic_series, cost, top_pct=top_pct, bench=bench_label
    )

    metrics: dict = {
        **stats,
        "sharpe": perf["sharpe"],
        "ann_return": perf["ann_return"],
        "ann_vol": perf["ann_vol"],
        "max_drawdown": perf["max_drawdown"],
        "ann_turnover": perf["ann_turnover"],
        "ann_return_net": perf["ann_return_net"],
        "sharpe_net": perf["sharpe_net"],
        "ls_sharpe": perf_ls["sharpe"],
        "ls_ann_return": perf_ls["ann_return"],
        "suspect_returns_masked": n_suspect,
        "extreme_returns_kept": n_extreme_kept,
        "cost_bps": cost,
        "top_pct": top_pct,
        "benchmark": profile.benchmark,
        "bench_used": bench_label,
        "universe": universe_value,
        "window": f"{start}~{end}",
        "data_source": "qlib_bin",
    }
    if market == "a_share":
        evaluators = _run_mining_evaluators(
            f_clean,
            r_clean,
            market=market,
            universe=universe_value,
            factor_id=factor_id,
        )
        if evaluators:
            metrics["evaluators"] = evaluators

    logger.info(
        "[factor-backtest] %s market=%s ok ic=%.4f icir=%.4f sharpe=%s n_days=%d net=%s",
        factor_id,
        market,
        stats["ic"],
        stats["icir"],
        f"{perf['sharpe']:.3f}" if perf["sharpe"] is not None else "N/A",
        n_days,
        f"{perf['ann_return_net']:.3f}"
        if perf["ann_return_net"] is not None
        else "N/A",
    )
    return _finish(_result(base, "ok", metrics=metrics, series=payload))
