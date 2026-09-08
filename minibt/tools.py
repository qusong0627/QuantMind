"""
minibt 分析工具函数
=====================
提供配对交易筛选功能，支持 6 种方法:
  - coint:        Engle-Granger 协整检验 + 卡尔曼滤波动态对冲
  - distance:     归一化价格距离法 (SSD)
  - halflife:     相关性 + OU 半衰期
  - hurst:        Hurst 指数均值回归检测
  - rolling_coint: 滚动窗口协整比例
  - johansen:     Johansen 多变量协整检验

统一入口: analyze_pair() / find_pairs()，通过 method 参数切换。
旧接口 analyze_cointegration() / find_cointegrated_pairs() 保留兼容。
"""
from __future__ import annotations
import sys
import os
import warnings
from typing import List, Dict, Optional, Union, Literal
from itertools import combinations

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint
from statsmodels.tsa.vector_ar.vecm import coint_johansen
from statsmodels.tsa.stattools import adfuller as _adfuller_fn

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ---- 常量 ----
DEFAULT_OUTPUT_DIR = "coint_analysis"

# 可用的筛选方法
AVAILABLE_METHODS = [
    'coint', 'distance', 'halflife', 'hurst', 'rolling_coint', 'johansen',
]


# ================================================================
#  数据获取 & 预处理
# ================================================================

def _get_close(contract, datas=None):
    """从 str / DataString / pd.DataFrame 获取 close 价格 Series（含 datetime 索引）。

    Returns:
        (name: str, close_series: pd.Series)
    """
    if isinstance(contract, pd.DataFrame):
        df = contract
        required = ["datetime", "close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"DataFrame缺少必要列: {missing}，需要{required}")
        df = df.copy()
        # 优先使用 DataFrame.attrs 中的名称，如果没有则使用默认格式
        if "symbol" in df.columns:
            name = df["symbol"].iloc[0]
            # 去掉交易所前缀让名称更简洁
            if "." in name:
                name = name.split(".", 1)[1]
        elif hasattr(df, "attrs") and "name" in df.attrs:
            name = df.attrs["name"]
        else:
            name = f"DF({len(df)})"
        if not isinstance(df["datetime"].iloc[0], pd.Timestamp):
            df["datetime"] = pd.to_datetime(df["datetime"])
        close_series = df.set_index("datetime")["close"].astype(float)
        return name, close_series

    if isinstance(contract, str):
        if datas is None:
            raise ValueError("contract为字符串时必须提供datas参数")
        ds = getattr(datas, contract, None)
        if ds is None:
            raise ValueError(f"LocalDatas中不存在合约: {contract}")
    else:
        ds = contract
    name = str(ds)
    kline = ds.kline
    close_series = pd.Series(
        kline.close.values,
        index=pd.to_datetime(kline.datetime),
        name=name,
    )
    return name, close_series


def _prepare_data(leg_a, leg_b, datas, leg_a_label, leg_b_label, min_points=70):
    """加载并对齐两个合约的数据。

    Returns:
        (name_a, name_b, label_a, label_b, close_a, close_b)
            其中 close_a/close_b 为对齐后的 np.ndarray。
    """
    name_a, ser_a = _get_close(leg_a, datas)
    name_b, ser_b = _get_close(leg_b, datas)
    label_a = leg_a_label or name_a
    label_b = leg_b_label or name_b

    aligned = ser_a.to_frame("a").join(ser_b.to_frame("b"), how="inner")
    if len(aligned) < min_points:
        raise ValueError(
            f"共同时间点不足: {len(aligned)} < {min_points}，"
            f"请检查 {label_a} 和 {label_b} 的时间重叠范围"
        )
    close_a = aligned["a"].values.astype(float)
    close_b = aligned["b"].values.astype(float)
    return name_a, name_b, label_a, label_b, close_a, close_b, len(ser_a), len(ser_b)


def _base_result(name_a, name_b, label_a, label_b, n, n_raw_a, n_raw_b):
    return {
        'leg_a': name_a, 'leg_b': name_b,
        'label_a': label_a, 'label_b': label_b,
        'n_points': n, 'n_raw_a': n_raw_a, 'n_raw_b': n_raw_b,
    }


# ================================================================
#  方法实现: 1. Engle-Granger 协整 (coint)
# ================================================================

def _analyze_coint(close_a, close_b, kalman_state_var=0.0001, kalman_obs_var=0.01,
                   zscore_window=60, coint_p_threshold=0.05):
    """Engle-Granger 协整 + 卡尔曼滤波分析。"""
    n = len(close_a)
    result = {}

    # ADF 平稳性
    def _adf(series, desc):
        r = adfuller(series, maxlag=20, autolag='AIC')
        return {
            f'adf_{desc}_stat': r[0], f'adf_{desc}_pval': r[1],
            f'adf_{desc}_1pct': r[4]['1%'], f'adf_{desc}_is_stationary': r[1] < 0.05,
        }
    result.update(_adf(close_a, 'a_level'))
    result.update(_adf(close_b, 'b_level'))
    result.update(_adf(np.diff(close_a), 'a_diff'))
    result.update(_adf(np.diff(close_b), 'b_diff'))

    # Engle-Granger
    cr = coint(close_b, close_a, trend='c', maxlag=20, autolag='AIC')
    result['coint_t'] = cr[0]
    result['coint_p'] = cr[1]
    result['coint_crit_1pct'] = cr[2][0]
    result['coint_crit_5pct'] = cr[2][1]
    result['coint_crit_10pct'] = cr[2][2]
    result['is_cointegrated'] = cr[1] < coint_p_threshold

    # Kalman 滤波
    state_mean = np.ones(n)
    state_var_arr = np.ones(n)
    for i in range(10, n):
        pv = state_var_arr[i - 1] + kalman_state_var
        kg = pv / (pv * close_b[i] ** 2 + kalman_obs_var)
        state_mean[i] = state_mean[i - 1] + kg * (
            close_a[i] - state_mean[i - 1] * close_b[i]
        )
        state_var_arr[i] = (1 - kg * close_b[i]) * pv

    kb = state_mean[zscore_window:]
    result['kalman_beta_mean'] = float(np.mean(kb))
    result['kalman_beta_std'] = float(np.std(kb))

    # 滚动 OLS
    rolling_beta = np.full(n, np.nan)
    for i in range(zscore_window, n):
        x, y = close_b[i - zscore_window:i], close_a[i - zscore_window:i]
        xm, ym = np.mean(x), np.mean(y)
        d = np.sum((x - xm) ** 2)
        if d != 0:
            rolling_beta[i] = np.sum((x - xm) * (y - ym)) / d
    result['ols_beta_mean'] = float(np.nanmean(rolling_beta))

    # 价差 & Z-Score
    spread_raw = close_a - state_mean * close_b
    spread_series = pd.Series(spread_raw)
    spread_ma = spread_series.rolling(zscore_window).mean()
    spread_std = spread_series.rolling(zscore_window).std()
    zs_all = np.asarray((spread_series - spread_ma) / spread_std, dtype=float)

    # 价差 ADF
    spread_clean = np.asarray(spread_raw[zscore_window:], dtype=float)
    spread_clean = spread_clean[~np.isnan(spread_clean)]
    try:
        asr = adfuller(spread_clean, maxlag=20, autolag='AIC')
        result['spread_adf_stat'] = asr[0]
        result['spread_adf_p'] = asr[1]
        result['spread_is_stationary'] = asr[1] < 0.05
    except ValueError:
        result['spread_adf_stat'] = np.nan
        result['spread_adf_p'] = 1.0
        result['spread_is_stationary'] = False

    # 信号频次
    result['signals'] = {
        'cross_up_2sigma': int(np.sum((zs_all[:-1] < 2) & (zs_all[1:] >= 2))),
        'cross_down_2sigma': int(np.sum((zs_all[:-1] > -2) & (zs_all[1:] <= -2))),
        'cross_mid': int(np.sum(
            ((zs_all[:-1] < 0.5) & (zs_all[1:] >= 0.5)) |
            ((zs_all[:-1] > -0.5) & (zs_all[1:] <= -0.5))
        )),
    }

    # 综合评分 & 适用性
    result['method'] = 'coint'
    result['score'] = 1.0 - result['coint_p']  # p值越小分越高
    result['is_suitable'] = (
        result['is_cointegrated']
        and result['kalman_beta_std'] < 0.05
        and result.get('spread_adf_p', 1.0) < 0.10
        and not result['adf_a_level_is_stationary']
        and not result['adf_b_level_is_stationary']
    )

    # 返回附加数据（供绘图）
    result['_plot_data'] = {
        'state_mean': state_mean, 'rolling_beta': rolling_beta,
        'spread_series': spread_series, 'zscore_series': np.asarray(zs_all, dtype=float),
        'close_a': close_a, 'close_b': close_b,
    }
    return result


# ================================================================
#  方法实现: 2. 距离法 (distance)
# ================================================================

def _analyze_distance(close_a, close_b):
    """归一化价格距离法 (SSD)。

    将两个价格序列 z-score 标准化后计算平方距离和。
    SSD 越小，两序列走势越接近，适合配对。
    """
    n = len(close_a)
    norm_a = (close_a - np.mean(close_a)) / np.std(close_a)
    norm_b = (close_b - np.mean(close_b)) / np.std(close_b)
    ssd = float(np.sum((norm_a - norm_b) ** 2))

    # 滚动相关系数
    window = min(60, n // 5)
    corr_series = pd.Series(close_a).rolling(window).corr(pd.Series(close_b))
    mean_corr = float(corr_series.mean())
    corr_std = float(corr_series.std())

    result = {
        'method': 'distance',
        'ssd': ssd,
        'ssd_per_point': ssd / n,
        'correlation_mean': mean_corr,
        'correlation_std': corr_std,
    }
    result['score'] = 1.0 / (1.0 + result['ssd_per_point'])  # SSD 越小分越高
    result['is_suitable'] = result['ssd_per_point'] < 5.0 and mean_corr > 0.5

    result['_plot_data'] = {
        'norm_a': norm_a, 'norm_b': norm_b, 'close_a': close_a, 'close_b': close_b,
    }
    return result


# ================================================================
#  方法实现: 3. 半衰期法 (halflife)
# ================================================================

def _analyze_halflife(close_a, close_b, max_half_life=60):
    """相关性 + OU 半衰期法。

    先用 OLS 求对冲比率 beta，再对价差拟合 OU 过程:
      dS = theta * S + eps
    半衰期 = ln(2) / |theta|，表示价差回归均值所需的 K 线数。
    """
    n = len(close_a)

    # OLS 估计 beta
    xm, ym = np.mean(close_b), np.mean(close_a)
    cov = np.sum((close_b - xm) * (close_a - ym))
    var = np.sum((close_b - xm) ** 2)
    beta = cov / var if var != 0 else 1.0

    # 价差 (OLS 残差)
    spread = close_a - beta * close_b

    # OU 半衰期估计: S_t = a + b*S_{t-1} + eps
    s_lag = spread[:-1]
    s_diff = np.diff(spread)
    x_val = np.column_stack([np.ones(len(s_diff)), s_lag])
    try:
        coeffs = np.linalg.lstsq(x_val, s_diff, rcond=None)[0]
        theta = -coeffs[1]
        if theta > 0:
            half_life = np.log(2) / theta
        else:
            half_life = np.inf
    except np.linalg.LinAlgError:
        theta = 0.0
        half_life = np.inf

    # 相关性
    corr = float(np.corrcoef(close_a, close_b)[0, 1])

    # 价差 ADF
    sc = spread[~np.isnan(spread)]
    try:
        asr = adfuller(sc, maxlag=20, autolag='AIC')
        spread_adf_p = asr[1]
        spread_stationary = asr[1] < 0.05
    except ValueError:
        spread_adf_p = 1.0
        spread_stationary = False

    result = {
        'method': 'halflife',
        'ols_beta': float(beta),
        'half_life': float(half_life) if half_life != np.inf else np.inf,
        'theta': float(theta),
        'correlation': corr,
        'spread_adf_p': spread_adf_p,
        'spread_is_stationary': spread_stationary,
    }
    result['score'] = 1.0 / (1.0 + result['half_life']) if half_life != np.inf else 0.0
    result['is_suitable'] = (
        half_life > 0 and half_life < max_half_life
        and corr > 0.5
        and spread_adf_p < 0.10
    )

    result['_plot_data'] = {
        'spread': spread, 'close_a': close_a, 'close_b': close_b,
    }
    return result


# ================================================================
#  方法实现: 4. Hurst 指数法 (hurst)
# ================================================================

def _compute_hurst(series, min_window=10):
    """R/S 分析法计算 Hurst 指数。

    H < 0.5: 均值回归  |  H ≈ 0.5: 随机游走  |  H > 0.5: 趋势性
    """
    series = np.asarray(series, dtype=float)
    series = series[~np.isnan(series)]
    n = len(series)
    if n < 50:
        return 0.5

    # 多个窗口大小
    windows = np.unique(np.logspace(
        np.log10(min_window), np.log10(n // 2), num=20, dtype=int
    ))
    windows = windows[windows >= min_window]

    rs_values = []
    for w in windows:
        segments = n // w
        rs_seg = []
        for s in range(segments):
            seg = series[s * w:(s + 1) * w]
            mean = np.mean(seg)
            dev = seg - mean
            cum_dev = np.cumsum(dev)
            r = np.max(cum_dev) - np.min(cum_dev)
            std = np.std(seg)
            if std > 0:
                rs_seg.append(r / std)
        if rs_seg:
            rs_values.append(np.mean(rs_seg))

    if len(rs_values) < 5:
        return 0.5

    log_windows = np.log(windows[:len(rs_values)])
    log_rs = np.log(rs_values)
    slope = np.polyfit(log_windows, log_rs, 1)[0]
    return float(np.clip(slope, 0.0, 1.0))


def _analyze_hurst(close_a, close_b):
    """Hurst 指数法 — 直接对 OLS 价差计算 Hurst 指数。

    H < 0.5 表示价差有均值回归倾向，适合配对交易。
    """
    n = len(close_a)

    # OLS 价差
    xm, ym = np.mean(close_b), np.mean(close_a)
    cov = np.sum((close_b - xm) * (close_a - ym))
    var = np.sum((close_b - xm) ** 2)
    beta = cov / var if var != 0 else 1.0
    spread = close_a - beta * close_b

    h = _compute_hurst(spread)

    result = {
        'method': 'hurst',
        'ols_beta': float(beta),
        'hurst_exponent': h,
        'hurst_interpretation': '均值回归' if h < 0.45 else ('随机游走' if h < 0.55 else '趋势性'),
    }
    result['score'] = 0.5 - h if h < 0.5 else 0.0
    result['is_suitable'] = h < 0.45  # 严格均值回归

    result['_plot_data'] = {
        'spread': spread, 'close_a': close_a, 'close_b': close_b,
    }
    return result


# ================================================================
#  方法实现: 5. 滚动窗口协整 (rolling_coint)
# ================================================================

def _analyze_rolling_coint(close_a, close_b, window=200, step=50, p_threshold=0.05):
    """滚动窗口协整比例法。

    在样本内滑动窗口反复做 Engle-Granger 检验，
    统计协整显著的窗口比例。
    比例越高，协整关系越稳定。
    """
    n = len(close_a)
    if n < window:
        raise ValueError(f"数据点数 ({n}) 小于滚动窗口 ({window})")

    coint_count = 0
    total_windows = 0
    p_values = []

    for start in range(0, n - window + 1, step):
        end = start + window
        try:
            cr = coint(close_b[start:end], close_a[start:end],
                       trend='c', maxlag=20, autolag='AIC')
            p_values.append(cr[1])
            if cr[1] < p_threshold:
                coint_count += 1
            total_windows += 1
        except Exception:
            continue

    if total_windows == 0:
        ratio = 0.0
        mean_p = 1.0
    else:
        ratio = coint_count / total_windows
        mean_p = float(np.mean(p_values))

    result = {
        'method': 'rolling_coint',
        'window': window,
        'step': step,
        'total_windows': total_windows,
        'coint_windows': coint_count,
        'coint_ratio': ratio,
        'mean_p_value': mean_p,
    }
    result['score'] = ratio
    result['is_suitable'] = ratio > 0.5

    result['_plot_data'] = {
        'close_a': close_a, 'close_b': close_b, 'p_values': p_values,
        'windows': total_windows,
    }
    return result


# ================================================================
#  方法实现: 6. Johansen 多变量协整 (johansen)
# ================================================================

def _analyze_johansen(close_a, close_b, det_order=0, k_ar_diff=1):
    """Johansen 多变量协整检验。

    检测两个序列之间是否存在协整关系，并给出协整向量。
    当 Engle-Granger 不显著时可用作替代。

    注意: Johansen 对样本量敏感，建议 n > 500。
    """
    n = len(close_a)
    data = np.column_stack([close_a, close_b])

    result = {
        'method': 'johansen',
    }

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            jres = coint_johansen(data, det_order, k_ar_diff)

        trace_stat = jres.lr1
        trace_crit = jres.cvt[:, 1]  # 5% 临界值
        eig_stat = jres.lr2
        eig_crit = jres.cvm[:, 1]

        # r=1: 存在至少 1 个协整关系
        result['trace_stat_r0'] = trace_stat[0]
        result['trace_crit_r0_5pct'] = trace_crit[0]
        result['trace_stat_r1'] = trace_stat[1] if len(trace_stat) > 1 else np.nan
        result['trace_crit_r1_5pct'] = trace_crit[1] if len(trace_crit) > 1 else np.nan
        result['eig_stat_r0'] = eig_stat[0]
        result['eig_crit_r0_5pct'] = eig_crit[0]

        # 协整向量 (第一列)
        evec = jres.evec[:, 0]
        result['coint_vector'] = list(evec)
        result['hedge_ratio'] = float(evec[0] / evec[1]) if evec[1] != 0 else np.nan

        is_coint_5pct = trace_stat[0] > trace_crit[0]
        result['is_cointegrated_johansen'] = bool(is_coint_5pct)

    except Exception as e:
        result['error'] = str(e)
        result['is_cointegrated_johansen'] = False
        result['score'] = 0.0
        result['is_suitable'] = False
        result['_plot_data'] = {'close_a': close_a, 'close_b': close_b}
        return result

    result['score'] = 1.0 if is_coint_5pct else 0.0
    result['is_suitable'] = bool(is_coint_5pct)
    result['_plot_data'] = {'close_a': close_a, 'close_b': close_b}
    return result


# ================================================================
#  统一入口: analyze_pair
# ================================================================

_METHOD_DISPATCH = {
    'coint': _analyze_coint,
    'distance': _analyze_distance,
    'halflife': _analyze_halflife,
    'half_life': _analyze_halflife,
    'hurst': _analyze_hurst,
    'rolling_coint': _analyze_rolling_coint,
    'johansen': _analyze_johansen,
}

_METHOD_PRINTERS = {}  # 在下方注册


def analyze_pair(
    leg_a: Union[str, object, pd.DataFrame],
    leg_b: Union[str, object, pd.DataFrame],
    method: Literal['coint', 'distance', 'halflife', 'hurst', 'rolling_coint', 'johansen'] = 'coint',
    save_plot: bool = False,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    datas=None,
    leg_a_label: str = "",
    leg_b_label: str = "",
    **kwargs,
) -> dict:
    """统一配对分析入口，通过 method 参数切换 6 种筛选方法。

    方法说明:
      - coint:        Engle-Granger 协整 + 卡尔曼滤波 (最全面)
      - distance:     归一化价格 SSD 距离法 (最快，不依赖统计检验)
      - halflife:     相关性 + OU 半衰期 (直观的均值回归速度)
      - hurst:        Hurst 指数检测价差均值回归倾向
      - rolling_coint: 滚动窗口协整比例 (判断协整稳定性)
      - johansen:     Johansen 多变量协整 (EG 不显著时的替代)

    Args:
        leg_a / leg_b: 合约数据，支持 str / DataString / pd.DataFrame。
        method:        筛选方法，默认 'coint'。
        save_plot:     是否保存分析图表。
        output_dir:    图表保存目录，默认 "./coint_analysis"。
        datas:         LocalDatas 实例。
        leg_a_label / leg_b_label: 显示标签。
        **kwargs:      传递给具体方法的参数 (如 zscore_window, p_threshold 等)。

    Returns:
        dict: 分析结果，通用字段:
            - method:   使用的方法
            - score:    综合评分 (越高越好)
            - is_suitable: 是否适合配对交易
            - n_points: 共同时间点数
    """
    method = method.lower().replace('-', '_')
    if method not in _METHOD_DISPATCH:
        raise ValueError(
            f"未知方法: '{method}'。可用: {list(_METHOD_DISPATCH.keys())}"
        )

    # 数据准备
    name_a, name_b, label_a, label_b, close_a, close_b, n_raw_a, n_raw_b = \
        _prepare_data(leg_a, leg_b, datas, leg_a_label, leg_b_label)

    n = len(close_a)
    result = _base_result(name_a, name_b, label_a, label_b, n, n_raw_a, n_raw_b)

    # 过滤 kwargs 传递给具体方法
    analyze_fn = _METHOD_DISPATCH[method]
    import inspect
    sig_params = set(inspect.signature(analyze_fn).parameters.keys())
    filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig_params}
    method_result = analyze_fn(close_a, close_b, **filtered_kwargs)

    # 提取绘图数据（保留一份在 result 中供 find_pairs 复用）
    plot_data = method_result.pop('_plot_data', {})
    result['_plot_data'] = plot_data
    result.update(method_result)

    # 打印报告
    printer = _METHOD_PRINTERS.get(method, _print_generic_report)
    printer(label_a, label_b, result)

    # 保存图表（仅保存符合条件的配对）
    if save_plot and result.get('is_suitable', False):
        os.makedirs(output_dir, exist_ok=True)
        filename = os.path.join(
            output_dir,
            f"{method}_OK_{name_a}_{name_b}.png"
        )
        _save_generic_plot(method, label_a, label_b, result, plot_data, filename)
        print(f"  图表已保存: {os.path.abspath(filename)}")

    sys.stdout.flush()
    return result


# ---- 报告打印器注册 ----

def _print_coint_report(label_a, label_b, result):
    """coint 方法专用报告。"""
    print(f"\n{'=' * 60}")
    print(f"  [{result['method']}] {label_a} x {label_b}")
    print(f"{'=' * 60}")
    print(f"  数据点数: {result['n_points']} (原始: {result.get('n_raw_a','?')}, {result.get('n_raw_b','?')})")
    print(f"  {label_a} ADF(level): {result['adf_a_level_stat']:.3f} "
          f"(p={result['adf_a_level_pval']:.4f}) "
          f"-> {'平稳' if result['adf_a_level_is_stationary'] else '非平稳'}")
    print(f"  {label_b} ADF(level): {result['adf_b_level_stat']:.3f} "
          f"(p={result['adf_b_level_pval']:.4f}) "
          f"-> {'平稳' if result['adf_b_level_is_stationary'] else '非平稳'}")
    print(f"  Engle-Granger: t={result['coint_t']:.3f}, p={result['coint_p']:.4f}")
    print(f"    1%={result['coint_crit_1pct']:.3f}, 5%={result['coint_crit_5pct']:.3f}, 10%={result['coint_crit_10pct']:.3f}")
    status = "存在协整关系 [OK]" if result['is_cointegrated'] else "不存在显著协整关系 [FAIL]"
    print(f"    -> {status}")
    print(f"  OLS beta: {result.get('ols_beta_mean', 'N/A')}")
    print(f"  Kalman beta: {result.get('kalman_beta_mean','N/A')} +/- {result.get('kalman_beta_std','N/A')}")
    spread_p = result.get('spread_adf_p', 1.0)
    print(f"  价差 ADF: p={spread_p:.4f} "
          f"-> {'平稳 [OK]' if result.get('spread_is_stationary') else '不平稳 [FAIL]'}")
    s = result.get('signals', {})
    print(f"  信号: +2s={s.get('cross_up_2sigma',0)}, -2s={s.get('cross_down_2sigma',0)}, mid={s.get('cross_mid',0)}")
    print(f"  综合评分: {result.get('score', 0):.4f}  |  适用: {result.get('is_suitable', False)}")


def _print_generic_report(label_a, label_b, result):
    """通用报告（distance / halflife / hurst / rolling_coint / johansen）。"""
    method = result.get('method', '?')
    print(f"\n{'=' * 60}")
    print(f"  [{method}] {label_a} x {label_b}")
    print(f"{'=' * 60}")
    print(f"  数据点数: {result.get('n_points', '?')} "
          f"(原始: {result.get('n_raw_a','?')}, {result.get('n_raw_b','?')})")

    # 打印该方法的特有字段
    skip_keys = {'method', 'leg_a', 'leg_b', 'label_a', 'label_b',
                 'n_points', 'n_raw_a', 'n_raw_b', 'is_suitable', 'score',
                 '_plot_data', 'signals'}
    for k, v in result.items():
        if k in skip_keys or k.startswith('_'):
            continue
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        elif isinstance(v, list):
            print(f"  {k}: {[f'{x:.4f}' if isinstance(x, float) else x for x in v]}")
        else:
            print(f"  {k}: {v}")

    print(f"  综合评分: {result.get('score', 0):.4f}  |  适用: {result.get('is_suitable', False)}")


_METHOD_PRINTERS['coint'] = _print_coint_report


# ---- 通用图表保存 ----

def _save_generic_plot(method, label_a, label_b, result, plot_data, filepath):
    """根据 method 绘制对应的分析图表。"""
    if method == 'coint':
        _save_coint_plot(
            plot_data.get('close_a'), plot_data.get('close_b'),
            plot_data.get('state_mean'), plot_data.get('rolling_beta'),
            plot_data.get('spread_series'), plot_data.get('zscore_series'),
            label_a, label_b, 60, result, filepath,
        )
    else:
        _save_simple_plot(method, label_a, label_b, result, plot_data, filepath)


def _save_coint_plot(close_a, close_b, state_mean, rolling_beta,
                     spread_series, zscore_series,
                     label_a, label_b, window, result, filepath):
    """协整分析三面板图表。"""
    fig, axes = plt.subplots(3, 1, figsize=(12, 10))
    status = "[ OK ] COINTEGRATED" if result.get('is_cointegrated') else "[FAIL] NOT COINTEGRATED"
    fig.suptitle(f"{label_a} x {label_b}  |  {status}  |  t={result.get('coint_t',0):.3f} p={result.get('coint_p',0):.4f}",
                 fontsize=12, fontweight='bold')

    ax0 = axes[0]
    if close_a is not None and close_b is not None:
        ax0.plot(close_a, label=label_a, alpha=0.8, linewidth=0.8)
        ax0.plot(close_b, label=label_b, alpha=0.8, linewidth=0.8)
    ax0.set_title("Price")
    ax0.legend(fontsize=8)
    ax0.tick_params(labelsize=7)

    ax1 = axes[1]
    if state_mean is not None:
        ax1.plot(state_mean, label='Kalman beta', linewidth=0.8)
    if rolling_beta is not None:
        ax1.plot(rolling_beta, label=f'Rolling OLS (w={window})', linewidth=0.8, alpha=0.7)
    ax1.axhline(y=result.get('kalman_beta_mean', 0), color='red', linestyle='--',
                linewidth=0.8, label=f"Mean={result.get('kalman_beta_mean',0):.3f}")
    ax1.set_title(f"Hedge Ratio  |  std={result.get('kalman_beta_std',0):.4f}")
    ax1.legend(fontsize=7)
    ax1.tick_params(labelsize=7)

    ax2 = axes[2]
    ax2_twin = ax2.twinx()
    if spread_series is not None:
        ax2.plot(np.asarray(spread_series, dtype=float), label='Spread', linewidth=0.6, color='steelblue')
    ax2.set_ylabel('Spread', fontsize=8, color='steelblue')
    ax2.tick_params(axis='y', labelsize=7, colors='steelblue')
    if zscore_series is not None:
        zs = np.asarray(zscore_series, dtype=float)
        ax2_twin.plot(zs, label='Z-Score', linewidth=0.6, color='darkorange', alpha=0.7)
        ax2_twin.axhline(y=2, color='red', linestyle='--', linewidth=0.6, alpha=0.5)
        ax2_twin.axhline(y=-2, color='red', linestyle='--', linewidth=0.6, alpha=0.5)
        ax2_twin.axhline(y=0, color='gray', linestyle='--', linewidth=0.5, alpha=0.5)
    ax2_twin.set_ylabel('Z-Score', fontsize=8, color='darkorange')
    ax2_twin.tick_params(axis='y', labelsize=7, colors='darkorange')

    s = result.get('signals', {})
    ax2.set_title(f"Spread & Z-Score  |  "
                  f"ADF p={result.get('spread_adf_p',0):.4f}  |  "
                  f"up={s.get('cross_up_2sigma',0)} down={s.get('cross_down_2sigma',0)} mid={s.get('cross_mid',0)}")
    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2_twin.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2, fontsize=7, loc='upper left')

    plt.tight_layout()
    plt.savefig(filepath, dpi=120)
    plt.close(fig)


def _save_simple_plot(method, label_a, label_b, result, plot_data, filepath):
    """其他方法的通用两面板图表。"""
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    status = "[ OK ] SUITABLE" if result.get('is_suitable') else "[FAIL] NOT SUITABLE"
    fig.suptitle(f"[{method}] {label_a} x {label_b}  |  {status}  |  score={result.get('score',0):.4f}",
                 fontsize=12, fontweight='bold')

    # Panel 1: 价格走势
    ax0 = axes[0]
    close_a = plot_data.get('close_a')
    close_b = plot_data.get('close_b')
    if close_a is not None and close_b is not None:
        ax0.plot(close_a, label=label_a, alpha=0.8, linewidth=0.8)
        ax0.plot(close_b, label=label_b, alpha=0.8, linewidth=0.8)
    ax0.set_title("Price")
    ax0.legend(fontsize=8)
    ax0.tick_params(labelsize=7)

    # Panel 2: 价差或归一化序列
    ax1 = axes[1]
    spread = plot_data.get('spread')
    norm_a = plot_data.get('norm_a')
    norm_b = plot_data.get('norm_b')

    if spread is not None:
        ax1.plot(spread, label='Spread', linewidth=0.6, color='steelblue')
        ax1.axhline(y=0, color='gray', linestyle='--', linewidth=0.5)
        ax1.set_title(f"Spread  |  method={method}")
        ax1.legend(fontsize=8)
    elif norm_a is not None and norm_b is not None:
        ax1.plot(norm_a, label=f'{label_a} (norm)', alpha=0.8, linewidth=0.6)
        ax1.plot(norm_b, label=f'{label_b} (norm)', alpha=0.8, linewidth=0.6)
        ax1.set_title(f"Normalized Price  |  SSD={result.get('ssd',0):.1f}")
        ax1.legend(fontsize=8)
    ax1.tick_params(labelsize=7)

    plt.tight_layout()
    plt.savefig(filepath, dpi=120)
    plt.close(fig)


# ================================================================
#  统一批量筛选: find_pairs
# ================================================================

def find_pairs(
    contracts: List[Union[str, object, pd.DataFrame]],
    method: Literal['coint', 'distance', 'halflife', 'hurst', 'rolling_coint', 'johansen'] = 'coint',
    save_plot: bool = False,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    datas=None,
    top_n: int = 0,
    **kwargs,
) -> List[dict]:
    """统一批量配对筛选入口。

    遍历所有两两组合，按指定方法评分排序，返回适用的配对列表。

    Args:
        contracts: 合约列表 (str / DataString / pd.DataFrame)。
        method:    筛选方法，默认 'coint'。
        save_plot: 是否保存所有配对的分析图表。
        output_dir: 图表目录。
        datas:     LocalDatas 实例。
        top_n:     返回前 N 对 (0=返回所有适用的)。
        **kwargs:  传递给 analyze_pair 的额外参数。

    Returns:
        List[dict]: 按评分降序排列的配对结果列表。
    """
    method = method.lower().replace('-', '_')
    if method not in _METHOD_DISPATCH:
        raise ValueError(f"未知方法: '{method}'。可用: {list(_METHOD_DISPATCH.keys())}")

    n = len(contracts)
    if n < 2:
        print("[警告] 合约数量不足，至少需要2个合约")
        return []

    print(f"\n{'=' * 60}")
    print(f"  [{method}] 批量配对筛选: {n} 个合约 -> {n * (n - 1) // 2} 对")
    print(f"{'=' * 60}")

    all_results = []

    for i, j in combinations(range(n), 2):
        c_a, c_b = contracts[i], contracts[j]

        try:
            result = analyze_pair(
                leg_a=c_a, leg_b=c_b,
                method=method,
                save_plot=False,  # 暂不单独保存，统一处理
                output_dir=output_dir,
                datas=datas,
                **kwargs,
            )
        except (ValueError, np.linalg.LinAlgError) as e:
            na, _ = _get_close(c_a, datas)
            nb, _ = _get_close(c_b, datas)
            print(f"  {na} x {nb}: 分析失败 ({e}), 跳过")
            continue

        all_results.append(result)

        # 如果需要保存图表，只保存符合条件的配对
        if save_plot and result.get('is_suitable', False):
            os.makedirs(output_dir, exist_ok=True)
            filename = os.path.join(
                output_dir,
                f"{method}_OK_{result['leg_a']}_{result['leg_b']}.png"
            )
            plot_data = result.get('_plot_data', {})
            _save_generic_plot(method, result['label_a'], result['label_b'],
                               result, plot_data, filename)

    # 按评分降序
    all_results.sort(key=lambda r: r.get('score', 0), reverse=True)

    suitable = [r for r in all_results if r.get('is_suitable')]

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"  筛选结果: {len(suitable)}/{len(all_results)} 对适用")
    if top_n > 0:
        suitable = suitable[:top_n]
        print(f"  (取前 {top_n} 对)")
    print(f"{'=' * 60}")
    for r in suitable:
        print(f"  [{r['method']}] {r['label_a']} x {r['label_b']}: score={r.get('score',0):.4f}")
    if not suitable:
        print("  (无适用配对)")
    print(f"{'=' * 60}")
    sys.stdout.flush()

    # 保存结果到文件
    if suitable and save_plot:
        os.makedirs(output_dir, exist_ok=True)
        # 转换为 DataFrame 格式
        records = []
        for r in suitable:
            record = {
                'method': r.get('method', ''),
                'leg_a': r.get('leg_a', ''),
                'leg_b': r.get('leg_b', ''),
                'label_a': r.get('label_a', ''),
                'label_b': r.get('label_b', ''),
                'score': r.get('score', 0),
                'is_suitable': r.get('is_suitable', False),
                'n_points': r.get('n_points', 0),
            }
            # 添加方法特定字段
            for k, v in r.items():
                if k not in record and not k.startswith('_'):
                    if isinstance(v, (bool, int, float, str)):
                        record[k] = v
                    elif isinstance(v, dict):
                        # 将字典展开为单独字段
                        for sk, sv in v.items():
                            record[f'{k}_{sk}'] = sv
            records.append(record)
        
        df = pd.DataFrame(records)
        
        # 保存为 CSV
        csv_path = os.path.join(output_dir, f"{method}_suitable_pairs.csv")
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"\n  结果已保存: {os.path.abspath(csv_path)}")
        
        # 保存为 JSON（保留完整信息）
        json_path = os.path.join(output_dir, f"{method}_suitable_pairs.json")
        # 清理不适合 JSON 序列化的字段
        json_records = []
        for r in suitable:
            jr = {k: v for k, v in r.items() if not k.startswith('_')}
            # 处理 numpy 类型
            for k, v in jr.items():
                if hasattr(v, 'item'):  # numpy 类型
                    jr[k] = v.item()
                elif isinstance(v, dict):
                    for sk, sv in v.items():
                        if hasattr(sv, 'item'):
                            v[sk] = sv.item()
            json_records.append(jr)
        
        import json
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_records, f, ensure_ascii=False, indent=2)
        print(f"  结果已保存: {os.path.abspath(json_path)}")

    return suitable


# ================================================================
#  向后兼容：保留旧接口
# ================================================================

def analyze_cointegration(
    leg_a, leg_b,
    save_plot=False, output_dir=DEFAULT_OUTPUT_DIR, datas=None,
    leg_a_label="", leg_b_label="",
    kalman_state_var=0.0001, kalman_obs_var=0.01,
    zscore_window=60, coint_p_threshold=0.05,
) -> dict:
    """旧接口: 等同于 analyze_pair(..., method='coint')。"""
    return analyze_pair(
        leg_a=leg_a, leg_b=leg_b, method='coint',
        save_plot=save_plot, output_dir=output_dir, datas=datas,
        leg_a_label=leg_a_label, leg_b_label=leg_b_label,
        kalman_state_var=kalman_state_var, kalman_obs_var=kalman_obs_var,
        zscore_window=zscore_window, coint_p_threshold=coint_p_threshold,
    )


def find_cointegrated_pairs(
    contracts, save_plot=False, output_dir=DEFAULT_OUTPUT_DIR,
    datas=None, p_threshold=0.05,
) -> List[dict]:
    """旧接口: 等同于 find_pairs(..., method='coint')。"""
    return find_pairs(
        contracts=contracts, method='coint',
        save_plot=save_plot, output_dir=output_dir,
        datas=datas, coint_p_threshold=p_threshold,
    )


# ================================================================
#  PairAnalyzer — 面向对象的配对交易分析工具类
# ================================================================

class PairAnalyzer:
    """配对交易分析工具类。

    封装所有 6 种筛选方法，提供统一的面向对象接口。

    使用示例::

        from minibt import LocalDatas
        from minibt.tools import PairAnalyzer

        pa = PairAnalyzer(datas=LocalDatas, output_dir="my_analysis")

        # 单个配对分析
        r = pa.coint("pp2601_60", "l2601_60")
        print(r['is_suitable'])

        # 批量筛选
        pairs = pa.find(["pp2601_60", "l2601_60", "v2601_60"], method='coint')

        # 多方法综合对比
        df = pa.summary("pp2601_60", "l2601_60")
        print(df)

        # 全方法批量筛选
        df_all = pa.find_all(["pp2601_60", "l2601_60", "v2601_60"])

    可用方法一览:

    ================ ======================================================
    方法名            说明
    ================ ======================================================
    .coint()         Engle-Granger 协整 + 卡尔曼滤波（最全面）
    .distance()      归一化价格 SSD 距离法（最快）
    .halflife()      相关性 + OU 半衰期（均值回归速度）
    .hurst()         Hurst 指数检测（均值回归倾向）
    .rolling_coint() 滚动窗口协整比例（协整稳定性）
    .johansen()      Johansen 多变量协整（EG 替代方案）
    .find()          单方法批量筛选
    .summary()       多方法综合对比（单对）
    .find_all()      全方法批量筛选（全对）
    ================ ======================================================

    筛选方法与交易策略对应关系:

    =========== ============ ======================== =============================
    筛选方法      指标类        返回值                    计算逻辑
    =========== ============ ======================== =============================
    coint        CointPair    (zscores, state_mean)    Kalman 滤波动态对冲比率 +
                                                      价差 Z-Score
    distance     DistancePair (zscores)                归一化价格差值 + 滚动布林带
                                                      Z-Score
    halflife     HalflifePair (zscores, ols_beta)     滚动 OLS β + OU 过程计算半
                                                      衰期 + 价差 Z-Score
    hurst        HurstPair    (zscores, ols_beta)     滚动 OLS β + 价差 Z-Score
                                                      (结合筛选阶段的 Hurst 值)
    rolling_cointRollingCointP(zscores, ols_beta,      滚动 OLS β + 滚动 EG 检验
                 air          in_coint_window)         + 协整窗口标记
    johansen     JohansenPair (zscores, johansen_beta) 滚动 Johansen 协整向量 β +
                                                      价差 Z-Score
    =========== ============ ======================== =============================
    """

    _METHOD_MAP = {
        'coint': _analyze_coint,
        'distance': _analyze_distance,
        'halflife': _analyze_halflife,
        'hurst': _analyze_hurst,
        'rolling_coint': _analyze_rolling_coint,
        'johansen': _analyze_johansen,
    }

    def __init__(
        self,
        datas=None,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        save_plot: bool = False,
    ):
        """初始化分析器。

        Args:
            datas: LocalDatas 实例，使用字符串合约名时必须提供。
            output_dir: 图表保存目录（仅在 save_plot=True 时使用）。
            save_plot: 全局默认是否保存图表，各方法可单独覆盖。
        """
        self.datas = datas
        self.output_dir = output_dir
        self.save_plot = save_plot

    def _run_single(self, method_name, leg_a, leg_b, **kwargs):
        """调用统一的 analyze_pair，注入 datas / output_dir。"""
        sp = kwargs.pop('save_plot', self.save_plot)
        od = kwargs.pop('output_dir', self.output_dir)
        return analyze_pair(
            leg_a=leg_a, leg_b=leg_b,
            method=method_name,
            save_plot=sp,
            output_dir=od,
            datas=self.datas,
            **kwargs,
        )

    # ---- 单对分析方法 ----

    def coint(self, leg_a, leg_b, **kwargs) -> dict:
        """Engle-Granger 协整分析 + 卡尔曼滤波动态对冲。

        可传额外参数: kalman_state_var, kalman_obs_var, zscore_window, coint_p_threshold。
        """
        return self._run_single('coint', leg_a, leg_b, **kwargs)

    def distance(self, leg_a, leg_b, **kwargs) -> dict:
        """归一化价格 SSD 距离法。无额外参数。"""
        return self._run_single('distance', leg_a, leg_b, **kwargs)

    def halflife(self, leg_a, leg_b, **kwargs) -> dict:
        """相关性 + OU 半衰期法。

        可传额外参数: max_half_life (最大半衰期K线数, 默认 60)。
        """
        return self._run_single('halflife', leg_a, leg_b, **kwargs)

    def hurst(self, leg_a, leg_b, **kwargs) -> dict:
        """Hurst 指数法 — 对 OLS 价差计算 R/S Hurst 指数。"""
        return self._run_single('hurst', leg_a, leg_b, **kwargs)

    def rolling_coint(self, leg_a, leg_b, **kwargs) -> dict:
        """滚动窗口协整比例法。

        可传额外参数: window (默认200), step (默认50), p_threshold (默认0.05)。
        """
        return self._run_single('rolling_coint', leg_a, leg_b, **kwargs)

    def johansen(self, leg_a, leg_b, **kwargs) -> dict:
        """Johansen 多变量协整检验。

        可传额外参数: det_order (默认0), k_ar_diff (默认1)。
        """
        return self._run_single('johansen', leg_a, leg_b, **kwargs)

    # ---- 批量筛选 ----

    def find(
        self,
        contracts,
        method: str = 'coint',
        top_n: int = 0,
        **kwargs,
    ) -> List[dict]:
        """单方法批量配对筛选。

        Args:
            contracts: 合约列表 (str / DataString / pd.DataFrame)。
            method:    筛选方法，默认 'coint'。
            top_n:     返回前 N 对 (0 = 全部适用)。
            **kwargs:  传递给具体方法的额外参数。

        Returns:
            List[dict]: 按评分降序排列的适用配对列表。
        """
        sp = kwargs.pop('save_plot', self.save_plot)
        od = kwargs.pop('output_dir', self.output_dir)
        return find_pairs(
            contracts=contracts, method=method,
            save_plot=sp, output_dir=od,
            datas=self.datas, top_n=top_n,
            **kwargs,
        )

    # ---- 综合对比 ----

    def summary(
        self,
        leg_a,
        leg_b,
        methods: Optional[list] = None,
        **kwargs,
    ) -> pd.DataFrame:
        """对同一配对运行多种方法，返回综合对比 DataFrame。

        Args:
            leg_a / leg_b: 合约数据。
            methods: 要运行的方法列表，None 表示全部 6 种。
            **kwargs: 额外参数（所有方法共享）。

        Returns:
            pd.DataFrame: 行=方法，列包含 score / suitable / 各方法关键指标。

        示例::

            pa.summary("pp2601_60", "l2601_60", methods=['coint', 'distance', 'halflife'])
        """
        if methods is None:
            methods = ['coint', 'distance', 'halflife', 'hurst', 'rolling_coint', 'johansen']

        rows = []
        for m in methods:
            try:
                r = self._run_single(m, leg_a, leg_b, save_plot=False, **kwargs)
                if not labels:
                    labels = {
                        'label_a': r.get('label_a', str(leg_a)),
                        'label_b': r.get('label_b', str(leg_b)),
                    }
                row = {
                    'method': m,
                    'score': round(r.get('score', 0), 4),
                    'suitable': r.get('is_suitable', False),
                }
                if m == 'coint':
                    row['coint_p'] = round(r.get('coint_p', 1), 4)
                    row['beta_std'] = round(r.get('kalman_beta_std', 1), 4)
                elif m == 'distance':
                    row['ssd_pp'] = round(r.get('ssd_per_point', 0), 2)
                    row['corr'] = round(r.get('correlation_mean', 0), 4)
                elif m == 'halflife':
                    row['half_life'] = round(r.get('half_life', 0), 1)
                    row['corr'] = round(r.get('correlation', 0), 4)
                elif m == 'hurst':
                    row['hurst'] = round(r.get('hurst_exponent', 0.5), 4)
                elif m == 'rolling_coint':
                    row['ratio'] = round(r.get('coint_ratio', 0), 4)
                elif m == 'johansen':
                    row['joh_coint'] = r.get('is_cointegrated_johansen', False)
                rows.append(row)
            except Exception as e:
                rows.append({'method': m, 'score': 0, 'suitable': False, 'error': str(e)})

        df = pd.DataFrame(rows).set_index('method')
        print(f"\n=== {leg_a} x {leg_b} 多方法对比 ===")
        print(df.to_string())
        sys.stdout.flush()

        # 保存 summary 表格
        if self.save_plot and not df.empty:
            la = labels.get('label_a', str(leg_a))
            lb = labels.get('label_b', str(leg_b))
            safe_a = str(la).replace('/', '_').replace('\\', '_').replace(':', '_')
            safe_b = str(lb).replace('/', '_').replace('\\', '_').replace(':', '_')
            os.makedirs(self.output_dir, exist_ok=True)
            csv_path = os.path.join(self.output_dir, f"summary_{safe_a}_{safe_b}.csv")
            df.to_csv(csv_path, encoding='utf-8-sig')
            print(f"  summary 表格已保存: {os.path.abspath(csv_path)}")
            sys.stdout.flush()

        return df

    def find_all(
        self,
        contracts,
        methods: Optional[list] = None,
        **kwargs,
    ) -> pd.DataFrame:
        """全方法批量筛选：对全部合约对运行所有方法，汇总为 DataFrame。

        Args:
            contracts: 合约列表。
            methods: 要运行的方法列表，None = 全部 6 种。
            **kwargs: 额外参数。

        Returns:
            pd.DataFrame: 列包含 pair / method / score / suitable。
        """
        if methods is None:
            methods = ['coint', 'distance', 'halflife', 'hurst', 'rolling_coint', 'johansen']

        all_rows = []
        for m in methods:
            try:
                results = self.find(contracts, method=m, top_n=0, save_plot=False, **kwargs)
                for r in results:
                    row = {
                        'pair': f"{r.get('label_a','?')}  x  {r.get('label_b','?')}",
                        'method': m,
                        'score': round(r.get('score', 0), 4),
                        'suitable': r.get('is_suitable', False),
                    }
                    all_rows.append(row)
            except Exception as e:
                print(f"  [{m}] 批量筛选出错: {e}")

        if not all_rows:
            print("\n未找到任何适用配对")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df = df.sort_values(['suitable', 'score'], ascending=[False, False])
        print(f"\n{'=' * 60}")
        print(f"  find_all 汇总 ({len(contracts)} 合约, {len(methods)} 方法)")
        print(f"{'=' * 60}")
        print(df.to_string(index=False))
        sys.stdout.flush()

        # 保存 find_all 汇总表格
        if self.save_plot:
            os.makedirs(self.output_dir, exist_ok=True)
            csv_path = os.path.join(self.output_dir, "find_all_summary.csv")
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            print(f"\n  find_all 汇总表格已保存: {os.path.abspath(csv_path)}")
            sys.stdout.flush()

        return df
