"""中性化原语的**唯一实现**：行业去均值 + 对控制变量正交 + 残差 IC。

为什么要有这个模块：同样的秩空间投影在平台上已被抄了三遍 ——
``scripts/factor_deep_dive.py``（行业+市值，完整版）、
``scripts/evaluate_signal_factors.py``（仅市值，单变量版）；
本次因子报告若再写一份就是第四份。按项目既有铁律「单一实现」，
三处全部改为引用本模块，由 ``test_factor_report_neutralize.py`` 的
**冻结旧实现逐位对拍**保证重构不改口径。

两个纯函数与两个取数函数刻意分开：纯函数无 IO、可单测；
取数函数各自负责降级（读不到就返回空，由调用方决定是否跳过当日）。

## ⚠️ NaN 处理：本模块与三处旧实现的**有意分歧**

三处旧实现（`factor_deep_dive.neutralized_ic`、`evaluate_signal_factors --neutral`
的网格版、以及本次报告构建器原本要新写的那份）都是「全列同一套均值 / 同一个 beta /
同一个 std」，只要截面里有**一个**缺失值，该因子在该日就整列变 NaN 而被静默丢弃。
实测：alpha_library 抽样 22 个交易日，`a158_KMID` 的 NaN 数在 65–3213 之间
（分母 5568），结果 **每个因子的 `ic_neutral_days` 都等于 1** —— 报告里那一列
「中性化 IC」是单点噪声，不是统计量。

故本模块一律 **逐格 NaN 感知**：行业均值按 (行业, 列) 有效数算、beta 按 (列, 市值)
共同有效样本算、相关系数按 (列, 收益) 共同有效样本算；缺失位结果为 NaN 且**不参与**
求和。**在无缺失的完整数据上，本模块与旧实现逐位一致**（由
`test_factor_report_neutralize.py` 的冻结旧实现对拍锁定），所以三种口径的差异
只发生在「旧实现本来就出错」的输入上。
"""

from __future__ import annotations

import glob
import logging
import warnings
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

import numpy as np

from backend.shared.quantdb_paths import resolve_quantdb_subdir

log = logging.getLogger(__name__)

INSTRUMENT_GLOB = str(
    resolve_quantdb_subdir("2_base_sector", "instrument_detail") / "*.parquet"
)
VALUATION_DIR = resolve_quantdb_subdir("5_technical_derived", "valuation")

COLLINEAR_TOL = 1e-20
"""共线判定：正交后残差平方和 ≤ 该比例 × 原平方和 → 视为已被控制变量解释干净。

取的是**相对**判据（残差标准差 < 1e-10 × 原标准差），与因子量纲无关；**两侧都先去均值**
（截面信息只在离散度里，见 ``orthogonalize_to`` 内的注释）。必要性：``x = a·市值 + b``
这类数值共线输入，正交后剩下的是浮点残渣（或一个常数），既非零也无信息；没有这道守卫，
下游会在 ``残渣/残渣`` 上算出一个 ±1 量级的**任意**相关系数（报告里看起来像「极端强因子」）。

判据放在**正交这一步**而不是下游的相关计算里：只有这里同时掌握「残差多大」与
「原来多大」两个量纲 —— ``residual_ic`` 的容差按残差自身量级缩放，认不出残渣。
"""


# ═══════════════ 纯函数 ═══════════════


def industry_demean(M: np.ndarray, ind_codes: Sequence[str]) -> np.ndarray:
    """按行业去均值：``M − D @ (DᵀM / 计数)``，``D`` 是行业哑变量矩阵。

    用哑变量矩阵乘法而非 ``pandas.groupby().transform()``：后者对
    5000×429 的因子矩阵要 1-2 秒/日，本实现快一个数量级（实测）。

    ⚠️ **NaN 感知**（与三个调用方的旧实现不同，见模块 docstring 的分歧清单）：
    逐格按「该行业该列的有效样本」求均值，缺失位不参与也不被污染。旧实现直接
    ``Dᵀ @ M``，一个 NaN 就把**整个行业整列**的均值污染成 NaN，进而整列残差变
    NaN —— 真实面板里每个交易日都必然有缺失，结果是每个日期都被静默丢弃
    （实测 alpha_library 抽样 22 天，`ic_neutral_days` 全部 = 1）。

    Args:
        M: (n_samples, n_factors) 的截面矩阵（通常是秩）。
        ind_codes: 长度 n_samples 的行业代码。

    Returns:
        同形状的行业去均值矩阵；``M`` 中的 NaN 位置保持 NaN。样本数为 0 时原样返回。
    """
    arr = np.asarray(M, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] == 0:
        return arr
    finite = np.isfinite(arr)
    if not finite.all():
        return _nan_demean_by_group(arr, finite, np.asarray(ind_codes))
    codes, inv = np.unique(np.asarray(ind_codes), return_inverse=True)
    if codes.size <= 1:
        return arr - arr.mean(axis=0, keepdims=True)
    D = np.zeros((arr.shape[0], codes.size), dtype=np.float64)
    D[np.arange(arr.shape[0]), inv] = 1.0
    cnt = D.sum(axis=0)
    cnt[cnt == 0] = 1.0
    return arr - D @ ((D.T @ arr) / cnt[:, None])


def _nan_demean_by_group(
    arr: np.ndarray, finite: np.ndarray, ind_codes: np.ndarray
) -> np.ndarray:
    """``industry_demean`` 的 NaN 分支：按 (行业, 列) 的有效样本数求均值。

    与无缺失分支走同一公式，只是把「计数」换成逐格有效数、把「求和」换成把缺失
    当 0 的求和；某格全缺时均值为 NaN，但那些位置本来就都是 NaN，减完仍是 NaN。
    """
    codes, inv = np.unique(ind_codes, return_inverse=True)
    D = np.zeros((arr.shape[0], codes.size), dtype=np.float64)
    D[np.arange(arr.shape[0]), inv] = 1.0
    cnt = D.T @ finite.astype(np.float64)  # n_ind × K 逐格有效数
    sums = D.T @ np.where(finite, arr, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = sums / np.where(cnt > 0, cnt, np.nan)
    return arr - mean[inv]  # 缺失位 NaN − 有限 = NaN


def orthogonalize_to(M: np.ndarray, control: np.ndarray) -> np.ndarray:
    """对控制变量（如 rank(总市值)）做一次最小二乘正交，返回残差。

    一次投影拿下所有列（``beta_k = Σ(x_k·cc)/Σ(cc²)``），避免逐因子循环。
    控制变量零方差时原样返回（无信息可正交，不是错误）。

    ⚠️ **NaN 感知**：beta 逐列按「该列与市值同时有效」的样本算（旧实现全列共用
    一个 beta，任一 NaN 即让 beta 变 NaN → 残差整片 NaN → 该日被静默丢弃）。
    缺失位残差保持 NaN，不参与下游。

    与旧实现在**完整数据**上逐位一致（见 test_factor_report_neutralize.py 的冻结对拍）。
    """
    arr = np.asarray(M, dtype=np.float64)
    c = np.asarray(control, dtype=np.float64).ravel()
    if arr.ndim != 2 or arr.shape[0] != c.size or c.size == 0:
        return arr
    c_ok = np.isfinite(c)
    cc = np.where(c_ok, c - (c[c_ok].mean() if c_ok.any() else 0.0), 0.0)
    ok = np.isfinite(arr) & c_ok[:, None]
    den = ((cc**2)[:, None] * ok).sum(axis=0)  # 逐列有效平方和
    num = (cc[:, None] * np.where(ok, arr, 0.0)).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        beta = np.where(den > 0, num / np.where(den > 0, den, 1.0), 0.0)
    # 因子或市值任一侧缺失 → 该股的残差无定义（拿不到残差的股票不能算进中性化 IC），
    # 与 orthogonalize_rows 的 `resid[~both] = nan` 取同一口径。
    resid = np.where(ok, arr - cc[:, None] * beta, np.nan)
    # 共线守卫：残差相对**原列**方差可忽略 → 该列已被控制变量解释干净（见 COLLINEAR_TOL）。
    # 判据放在这里而不是 residual_ic：只有这一步同时掌握「残差多大」与「原来多大」。
    n_ok = ok.sum(axis=0)
    n_safe = np.where(n_ok > 0, n_ok, 1.0)
    arrw = np.where(ok, arr, 0.0)
    am = arrw.sum(axis=0) / n_safe
    ss_tot = (np.where(ok, arrw - am, 0.0) ** 2).sum(axis=0)
    # 残差也要**先去均值**再比：因子写成 `a·mv + b`（b≠0）时，正交后剩下的是常数 b + 3·mean
    # —— 未去均值时两侧比的是「噪声 vs 噪声」（比值 ~1），守卫不触发；截面信息只在离散度里。
    rw = np.where(ok, resid, 0.0)
    rm = rw.sum(axis=0) / n_safe
    ss_res = (np.where(ok, rw - rm, 0.0) ** 2).sum(axis=0)
    degenerate = ss_res <= COLLINEAR_TOL * ss_tot
    return np.where(ok & ~degenerate[None, :], resid, np.nan)


def orthogonalize_rows(
    M: np.ndarray,
    control: np.ndarray,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """``orthogonalize_to`` 的**向量化形态**：逐行（逐日）各做一次最小二乘正交。

    与逐日调用 ``orthogonalize_to`` 是同一口径、同一结果（差一个不影响相关性的
    常数），只是把 2588 天的循环写成一次矩阵运算 —— 批量脚本靠这个把分钟级降到
    秒级，故保留两种形态而不是强行统一调用形状。等价性由
    ``test_factor_report_neutralize.py::test_向量化正交与逐日正交_IC_一致`` 断言。

    Args:
        M: (T, N) 因子网格，缺失为 NaN。
        control: (T, N) 控制变量网格（如 rank(市值)），缺失为 NaN。
        valid: (T, N) 布尔有效性掩码；None 表示「非 NaN 即有效」。

    Returns:
        (T, N) 残差网格；**因子与控制变量任一侧缺失的位置一律 NaN**（无法正交即无语义
        上的残差，不能拿 0 冒充 —— 0 是截面均值位置，会被下游当成一个真实观测计入 IC）。

    ⚠️ **与旧网格实现的两处有意差异**（旧代码见 ``evaluate_signal_factors.py`` 原
    ``--neutral`` 分支，两处都是静默出错）：
      1. 旧式 ``(f - mean_f) * both`` 在缺失位得到 ``NaN * 0 = NaN``，污染整行点积
         → ``slope`` 变 NaN → ``nan_to_num`` 归 0 → **正交化被整行静默跳过**，只剩
         截面去均值。真实面板里几乎每行都有缺失，等于 ``--neutral`` 从未生效过。
      2. 旧式把「控制变量缺失」的位置留成残差 ``0.0`` 并计为有效观测，把 IC 往 0 拉。
    """
    arr = np.asarray(M, dtype=np.float64)
    ctl = np.asarray(control, dtype=np.float64)
    if arr.ndim != 2 or ctl.shape != arr.shape:
        return np.full_like(arr, np.nan)
    m = np.isfinite(arr) if valid is None else (np.asarray(valid) & np.isfinite(arr))
    both = m & np.isfinite(ctl)

    # 空行（当日该股无有效控制变量）会触发 numpy 的 "Mean of empty slice" 警告 —— 那是
    # 预期输入，不是异常；结果位由下面的 NaN 掩码兜住，不必污染构建日志。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean_f = np.nanmean(np.where(both, arr, np.nan), axis=1, keepdims=True)
        mean_c = np.nanmean(np.where(both, ctl, np.nan), axis=1, keepdims=True)
    # np.where 而非乘法掩码：缺失位取精确 0，NaN 不会污染整行求和
    fd = np.where(both, arr - mean_f, 0.0)
    cd = np.where(both, ctl - mean_c, 0.0)
    var_c = (cd**2).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = np.where(
            var_c > 0, (fd * cd).sum(axis=1) / np.where(var_c > 0, var_c, 1.0), np.nan
        )
    resid = fd - np.nan_to_num(slope)[:, None] * cd
    # 逐行共线守卫（与 orthogonalize_to 同一判据、同一常量）：该行因子被控制变量
    # 解释干净时整行作废 —— 否则残差是浮点残渣，下游会在「残渣/残渣」上算出 ±1 的假 IC。
    ss_tot = (fd**2).sum(axis=1)
    ss_res = (np.where(both, resid, 0.0) ** 2).sum(axis=1)
    degenerate = (ss_res <= COLLINEAR_TOL * ss_tot)[:, None]
    return np.where(both & ~degenerate, resid, np.nan)


def residual_ic(resid: np.ndarray, y: Sequence[float]) -> np.ndarray:
    """残差矩阵各列与 ``y`` 的横截面相关（逐列，向量化）。

    与 ``factor_deep_dive.neutralized_ic`` 口径一致：总体标准差（ddof=0）、
    对去均值后的 ``y`` 做内积 —— 即残差空间里的皮尔逊相关
    （残差秩与 ``y`` 的 Spearman 在数学上等价于此式，见模块原始注释）。

    ⚠️ **两处与旧实现的有意差异**（均在退化输入上触发；完整数据上逐位一致）：
      1. **NaN 感知**：逐列按「残差与收益同时有效」的样本算相关。旧实现用全列
         ``std``/``mean``，一个 NaN 就让该因子整列变 NaN —— 真实面板每列都有缺失，
         等价于整列作废（这就是 ``ic_neutral_days`` 恒为 1 的根因）。
      2. **零方差守卫**：残差方差低于容差时返回 NaN。旧实现没有这道守卫，
         「因子 = a·市值 + b」这类数值共线输入会走进 ``噪声/噪声``，输出 ±1 量级的
         **任意**相关系数 —— 报告里看起来像「极端强因子」，实则是浮点残渣。
         ⚠️ 这道守卫只管「方差精确为 0」；**共线的正经守卫在正交那一步**
         （``orthogonalize_to`` / ``orthogonalize_rows`` 的 ``COLLINEAR_TOL``）——
         残差是 1e-13 的浮点残渣时，本函数的容差按残差自身量级缩放，认不出来。

    总体标准差（ddof=0）与旧实现口径相同。
    """
    R = np.asarray(resid, dtype=np.float64)
    yy = np.asarray(y, dtype=np.float64).ravel()
    if R.ndim != 2 or R.shape[1] == 0 or R.shape[0] == 0 or R.shape[0] != yy.size:
        return np.full(R.shape[1] if R.ndim == 2 else 0, np.nan)
    ok = np.isfinite(R) & np.isfinite(yy)[:, None]
    n = ok.sum(axis=0)
    if not n.any():
        return np.full(R.shape[1], np.nan)
    nn = np.where(n > 0, n, 1)
    # 均值只取有效样本：np.nanmean 会把 ±inf（如 close[t]=0 算出的收益）算进均值，
    # 整个 y 随之变 inf，逐列相减出 NaN —— 一条脏数据废掉当天全部因子。
    y_fin = yy[np.isfinite(yy)]
    yv = np.where(np.isfinite(yy), yy, 0.0) - (
        float(y_fin.mean()) if y_fin.size else 0.0
    )
    Rw = np.where(ok, R, 0.0)
    yw = np.where(ok, yv[:, None], 0.0)
    dr = Rw - Rw.sum(axis=0) / nn
    dy = yw - yw.sum(axis=0) / nn
    num = (dr * dy).sum(axis=0)
    den = np.sqrt((dr**2).sum(axis=0) * (dy**2).sum(axis=0))
    # 相对容差（量纲 = 残差 × 收益 × 样本数）：残差量级 × 收益量级 × n × 1e-12。
    # 低于此即认为「无截面方差」而非「方差很小」。
    s_r = float(np.max(np.abs(Rw))) if Rw.size else 0.0
    s_y = float(np.max(np.abs(yw))) if yw.size else 0.0
    tol = 1e-12 * n * max(s_r, 1e-12) * max(s_y, 1e-12)
    with np.errstate(invalid="ignore", divide="ignore"):
        ic = np.where(den > tol, num / np.where(den > 0, den, 1.0), np.nan)
    return np.where(n > 0, ic, np.nan)


def neutralize_ic(
    M: np.ndarray,
    y: Sequence[float],
    *,
    ind_codes: Sequence[str] | None = None,
    control: Sequence[float] | None = None,
) -> np.ndarray:
    """``行业去均值 → 对控制变量正交 → 残差 IC`` 三步的标准流水线。

    任一步的输入为 None 即跳过该步（支持「只做行业中性」「只做市值中性」）。
    """
    R = np.asarray(M, dtype=np.float64)
    if ind_codes is not None:
        R = industry_demean(R, ind_codes)
    if control is not None:
        R = orthogonalize_to(R, control)
    return residual_ic(R, y)


def rank_pct(M: np.ndarray) -> np.ndarray:
    """按列转百分位秩（0..1），与 ``pandas.rank(pct=True)`` 同口径。

    每列的**有效样本数**（有限值个数）作分母，缺失位保持 NaN —— 与 pandas 一致：
    ``rank`` 不给 NaN 计分母，把 NaN 排到最大是**错的**（argsort 会把 NaN 排到末尾，
    直接照抄就会得到「最缺失 = 最强因子」这种反向结论）。
    """
    arr = np.asarray(M, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] == 0:
        return arr
    finite = np.isfinite(arr)
    n = finite.sum(axis=0)
    safe = np.where(finite, arr, np.inf)  # inf 排到最后，不影响有效值名次
    order = np.argsort(safe, axis=0, kind="stable")
    ranks = np.empty_like(arr)
    col = np.arange(arr.shape[0], dtype=np.float64)[:, None]
    np.put_along_axis(ranks, order, np.broadcast_to(col, arr.shape), axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (ranks + 1.0) / np.where(n > 0, n, 1.0)
    return np.where(finite, out, np.nan)


# ═══════════════ 取数（各自降级，返回空表示不可用）═══════════════


@lru_cache(maxsize=1)
def load_industry_map() -> Any:
    """行业映射（Symbol → rs_hycode_sim）。取不到时返回空 Series，由调用方跳过。

    ``lru_cache(maxsize=1)``：行业映射是**静态快照**（`instrument_detail`），
    构建器逐日调用它时若每次都重读 parquet，2588 天就是 2588 次全量读。
    调用方只做 ``reindex`` 读取，不改动返回的 Series。
    """
    import pandas as pd
    import pyarrow.parquet as pq

    files = glob.glob(INSTRUMENT_GLOB)  # pyarrow 不展开 glob（DuckDB 才展开）
    if not files:
        log.warning("行业映射缺失：%s 无文件", INSTRUMENT_GLOB)
        return pd.Series(dtype=str)
    try:
        df = pq.read_table(files, columns=["Symbol", "rs_hycode_sim"]).to_pandas()
    except Exception as e:  # noqa: BLE001 — 缺列/损坏都不应让整条报告链挂掉
        log.warning("行业映射读取失败: %s", e)
        return pd.Series(dtype=str)
    df = df.dropna(subset=["rs_hycode_sim"]).drop_duplicates("Symbol")
    return df.set_index("Symbol")["rs_hycode_sim"].astype(str)


def load_mv(dt: str, symbols: Sequence[str] | None = None) -> Any:
    """某日总市值**原始值**（Series，index=symbol）。文件缺失返回空 Series。

    原始值而非秩：分市值域 IC 要按市值分位切子域、容量估算要用成交额量纲，
    两者都依赖可比的数量级；秩只在需要正交化时才排一次（见 :func:`load_mv_rank`）。
    """
    import pandas as pd
    import pyarrow.parquet as pq

    path = VALUATION_DIR / f"dt={dt}" / "data.parquet"
    if not path.exists():
        return pd.Series(dtype=float)
    try:
        s = (
            pq.read_table(path, columns=["symbol", "total_mv"])
            .to_pandas()
            .set_index("symbol")["total_mv"]
        )
    except Exception as e:  # noqa: BLE001
        log.warning("市值读取失败(%s): %s", dt, e)
        return pd.Series(dtype=float)
    s = s.where(np.isfinite(s)).astype(float)
    if symbols is not None:
        s = s.reindex(pd.Index(list(symbols)))
    return s


def load_mv_rank(dt: str, symbols: Sequence[str] | None = None) -> Any:
    """某日总市值的百分位秩（Series，index=symbol）。文件缺失返回空 Series。"""
    s = load_mv(dt, symbols)
    return s.rank(pct=True) if len(s) else s
