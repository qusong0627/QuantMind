"""增量正交闸（T-FE-01）：新因子对库的**残差 IC_IR** 口径。

台账 §1.2：「新因子对库（或基准模型）回归取残差，残差 IC_IR>0.3 才入库
（施密特逐次；库大后换 PCA）」。本模块是该口径的**纯函数单源**，供物化器
（``backend/scripts/rd_mined_materialize.py``）在入库闸上调用；闸门本体见
``mining_plugins/gates/builtin.py`` 的 ``incremental_ic_ir_floor``。

与既有值级查重（逐日 rank |ρ| 超过硬拒阈值即拒；阈值见物化器
``rd_mined_materialize`` 常量）是**两个机制**：查重管「近乎重复」，增量闸
管「有冗余基时，残差还剩多少独立信息」。口径要点：

- **对照集 = basis**：与库列的 |日均 Spearman ρ| ≥ ``DEFAULT_BASIS_ABS_CORR``
  （0.3）的列；**无对照列或全列 < 阈值 → 状态 no_basis**——无冗余基 = 残差
  即原值，语义上当然"通过"（本模块不判通过与否，只报事实，由闸决定）；
  但**有对照列却算不出 ρ（全 NaN）→ unavailable**，绝不冒充 no_basis。
- **施密特逐次、确定性**：basis 按 |ρ| 降序（并列按列名）逐个正交；先吃
  相关性最强的列。顺序影响逐次残差，故必须与输入列序无关。
- **秩空间回归**：因子与基列各自逐日 rank→z（std 用 ddof=1），逐日无截距
  OLS（z 日均值≈0 已吸收截距）；对位数 < ``min_pairs`` 的日子系数置 NaN
  → 该日残差不可信、进不了 IC（与值级查重的样本量门槛同思路）。
- **残差 IC_IR** = ``|mean(rank_ic)| / std(rank_ic, ddof=1)``，与
  ``ic_stats.summarize_ic`` 完全同式；**取绝对值**——方向由 signal_side /
  执行面管，闸只问「有没有独立信息」（与 |ρ| 口径一致）。
- **前向收益必须走全交易日**：``forward_return_series`` 在**未截断**的对照
  帧上 shift(-h)。先按采样日截断再 shift 会静默变成「下一个采样日」收益
  （≈2 个交易日跳步），IC 口径整体位移而不报错。
- **宁缺勿凑**：算不出的情形一律 ``ic_ir=None`` + 明确 status
  （``insufficient_days`` / ``unavailable``），调用方据此判「不判不拦」；
  绝不把 None 当 0 或当 fail。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import ic_stats

DEFAULT_HORIZON = 1
DEFAULT_BASIS_ABS_CORR = 0.3
DEFAULT_MIN_PAIRS_PER_DAY = 20
DEFAULT_MIN_SAMPLE_DAYS = 5
DEFAULT_MIN_IC_DAYS = 20
DEFAULT_MIN_NAMES = 30


@dataclass(frozen=True)
class IncrementalResult:
    """增量正交口径的冻结结果。

    ``status``：``ok`` / ``no_basis`` / ``insufficient_days`` / ``unavailable``。
    ``ic_ir`` 仅在 ``ok`` 时非 None（已取绝对值）；``basis`` 是参与正交的
    列名（|ρ| 降序）；``n_days`` 是残差 IC 的有效日数。
    """

    status: str
    ic_ir: float | None
    ic_mean: float | None
    n_days: int
    basis: tuple[str, ...]
    max_basis_abs_corr: float | None

    @property
    def n_basis(self) -> int:
        return len(self.basis)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "ic_ir": self.ic_ir,
            "ic_mean": self.ic_mean,
            "n_days": int(self.n_days),
            "basis": list(self.basis),
            "n_basis": self.n_basis,
            "max_basis_abs_corr": self.max_basis_abs_corr,
        }


def _finite_or_none(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


def forward_return_series(
    panel: pd.DataFrame, *, horizon: int = DEFAULT_HORIZON
) -> pd.Series | None:
    """对照帧的 close-to-close 前向收益；缺 close 列/空帧 → ``None``。

    **必须在按交易日全量对齐的帧上调用**（而不是采样日切片）——否则
    ``shift(-h)`` 跨到"下一个采样日"，标签口径整体位移。缺 close 时宁可
    判不可得，绝不拿别的价格列顶替（静默降级）。
    """
    if panel is None or panel.empty or "close" not in panel.columns:
        return None
    return ic_stats.forward_returns(panel[["close"]], horizon)


def mean_rho_by_column(
    values: pd.Series,
    controls: pd.DataFrame,
    *,
    sample_days: set[str] | None = None,
    exclude: tuple[str, ...] | set[str] = (),
    min_pairs: int = DEFAULT_MIN_PAIRS_PER_DAY,
    min_sample_days: int = DEFAULT_MIN_SAMPLE_DAYS,
) -> pd.Series:
    """新因子与每个对照列的**有符号**日均 Spearman ρ。

    实现 = 逐日截面 rank 后按日求皮尔逊相关、再对日取均值（抓「逐日排序
    相同的换皮因子」）；**成对相关按逐列对交集域（pairwise-complete）
    计算**——对照列的缺失行不参与该列的协方差与两侧方差，覆盖域窄的列
    绝不被系统性缩水。要求每天至少 ``min_pairs`` 对、至少
    ``min_sample_days`` 个有效日，否则该列为 NaN。索引须为
    ``(trade_date, symbol)`` 两层。``exclude`` 列（该因子自己的旧列，
    或替换受理时被顶替的列）不参与。

    无重叠/样本不足/无对照列 → **空 Series**（调用方自行回落，不误报 0）。
    本函数是既有值级查重（取 |ρ| 最大者）与增量闸（取 |ρ|≥0.3 的 basis）
    的共同底层——两者口径必须同源。
    """
    empty = pd.Series(dtype=float)
    if values is None or controls is None or len(values) == 0 or controls.empty:
        return empty
    new_col = values.rename("__new__")
    if isinstance(new_col, pd.DataFrame):
        new_col = new_col.iloc[:, 0]
    frame = pd.concat([new_col, controls], axis=1, join="inner")
    frame = frame[frame["__new__"].notna()]
    if exclude:
        drop = [c for c in exclude if c in frame.columns]
        if drop:
            frame = frame.drop(columns=drop)
    if sample_days is not None:
        frame = frame[frame.index.get_level_values(0).isin(sample_days)]
    if frame.empty:
        return empty
    ranks = frame.groupby(level=0).rank()
    new_rank = ranks["__new__"]
    other = ranks.drop(columns="__new__")
    if other.shape[1] == 0:
        return empty
    # 逐列按「与 __new__ 的对交集」域算原始和（pairwise-complete）：协方差
    # 与两侧方差必须同域。曾有实现让 new 方差取「任一对照列非空」的宽域
    # （pair_ok.any(axis=1)）——对照列覆盖域窄于 values 时（如 l1 只覆盖
    # 部分标的、帧是全票并集）分母被抬高，ρ 系统性缩水
    # sqrt(k_pair/k_wide)（实测 0.773），真重复列会静默漏过硬拒闸。
    pair_ok = other.notna()
    # values 已全帧非 NaN（上方过滤），other 的 NaN 即 pair 缺失；masked 为 0
    # 处恰是 pair 缺失（rank≥1，非零），平方后 0² 仍 0，可复用算 Σn²。
    masked_new = pair_ok.mul(new_rank, axis=0)
    sum_n = masked_new.groupby(level=0).sum()
    masked_new **= 2
    sum_nn = masked_new.groupby(level=0).sum()
    del masked_new
    sum_o = other.groupby(level=0).sum()
    sum_oo = (other * other).groupby(level=0).sum()
    sum_no = other.mul(new_rank, axis=0).groupby(level=0).sum()
    n_pairs = pair_ok.groupby(level=0).sum()
    cov = n_pairs * sum_no - sum_n * sum_o
    var_new = n_pairs * sum_nn - sum_n * sum_n
    var_other = n_pairs * sum_oo - sum_o * sum_o
    denom = (var_new * var_other) ** 0.5
    rho_day = cov / denom.where(denom > 0)
    rho_day = rho_day.where(n_pairs >= min_pairs)
    rho_mean = rho_day.mean()
    return rho_mean.where(rho_day.notna().sum() >= min_sample_days)


def mean_abs_rank_ic(
    series: pd.Series,
    fwd_ret: pd.Series,
    *,
    sample_days: set[str] | None = None,
    min_names: int = DEFAULT_MIN_NAMES,
    min_days: int = DEFAULT_MIN_IC_DAYS,
) -> tuple[float | None, int]:
    """单列在采样窗口上的 ``mean(|逐日 rank IC|)`` → ``(值, 有效日数)``。

    替换判据（T-FM-02）用：新因子与被顶替列必须在**同一窗口、同一
    fwd 口径**上比 |IC|。有效日 < ``min_days`` → ``(None, n)``——
    宁缺勿凑，替换判据 fail-closed。
    """
    if series is None or len(series) == 0 or fwd_ret is None or len(fwd_ret) == 0:
        return None, 0
    s = series
    if sample_days is not None:
        s = s[s.index.get_level_values(0).isin(sample_days)]
    daily = ic_stats.daily_ic(s, fwd_ret, min_names=min_names)
    n = int(len(daily))
    if n < min_days:
        return None, n
    mean = abs(float(daily["rank_ic"].mean()))
    return _finite_or_none(mean), n


def _daily_rank_z(series: pd.Series) -> pd.Series:
    """逐日截面 rank → z（ddof=1；单点/零方差日 → NaN）。"""
    s = series.astype(float)
    ranks = s.groupby(level=0).rank()
    mean = ranks.groupby(level=0).transform("mean")
    std = ranks.groupby(level=0).transform("std")
    return (ranks - mean) / std.where(std > 0)


def _sequential_residual(
    factor: pd.Series,
    basis: pd.DataFrame,
    *,
    days: set[str] | None,
    min_pairs: int,
) -> pd.Series:
    """factor 对 basis 各列（调用方已按 |ρ| 降序排好）的施密特残差。

    逐日秩空间无截距 OLS（z 日均值≈0 吸收截距）：``res -= z_b · beta``，
    ``beta = Σ(res·z_b) / Σ(z_b²)``，仅在对位数 ≥ ``min_pairs`` 的日子生效
    （否则该日残差置 NaN——系数不可信的日子不许投票）。
    """
    f = factor
    if days is not None:
        f = f[f.index.get_level_values(0).isin(days)]
    res = _daily_rank_z(f)
    for col in basis.columns:
        b = basis[col]
        if days is not None:
            b = b[b.index.get_level_values(0).isin(days)]
        z_b = _daily_rank_z(b)
        pair = res.notna() & z_b.notna()
        cov = (res * z_b).where(pair).groupby(level=0).sum()
        var = (z_b**2).where(pair).groupby(level=0).sum()
        n_pairs = pair.groupby(level=0).sum()
        beta = (cov / var.where(var > 0)).where(n_pairs >= min_pairs)
        res = res - z_b.mul(beta, level=0)
    return res


def residual_ic_ir(
    values: pd.Series,
    basis_values: pd.Series,
    *,
    basis_name: str,
    fwd_ret: pd.Series | None,
    sample_days: set[str] | None = None,
    min_pairs: int = DEFAULT_MIN_PAIRS_PER_DAY,
    min_sample_days: int = DEFAULT_MIN_SAMPLE_DAYS,
    min_ic_days: int = DEFAULT_MIN_IC_DAYS,
    min_names: int = DEFAULT_MIN_NAMES,
) -> IncrementalResult:
    """单基残差 IC_IR（T-FE-09）：对**指定父本**正交后的残差口径。

    与 :func:`incremental_ic_ir` 是同一残差口径的两个视图，差别只有两点：

    - **不做 |ρ|≥0.3 筛选**：变体台账记录的是「家族×窗口/变换」子变体对
      父本的残差——父子（如 rev5×rev20）秩相关常 < 0.3，走增量闸口径会
      判 ``no_basis`` 拿不到残差；这里**无条件**以传入的父本为基。
    - ``max_basis_abs_corr`` 记**真实** ρ（可为 0~1 任意值）——绝不为了
      复用筛选路径伪造 1.0 的审计字段。

    ``fwd_ret`` **必传**（调用方持全交易日帧的预计算前向收益）：缺省/None
    响亮拒绝（ValueError）——绝不回退到按 values 帧自己 shift（采样截断
    陷阱的另一条静默入口）。

    ``status``：``ok`` / ``insufficient_days`` / ``unavailable``（单基无
    ``no_basis`` 概念；父本列全 NaN / 对域不足算不出 ρ → unavailable，
    宁缺勿凑）。
    """
    if values is None or len(values) == 0 or basis_values is None:
        return IncrementalResult("unavailable", None, None, 0, (), None)
    if fwd_ret is None:
        raise ValueError("residual_ic_ir 要求显式 fwd_ret（全交易日帧预计算）")
    if len(fwd_ret) == 0:
        return IncrementalResult("unavailable", None, None, 0, (), None)
    days = set(sample_days) if sample_days is not None else None
    controls = pd.DataFrame({str(basis_name): basis_values})
    rho = mean_rho_by_column(
        values,
        controls,
        sample_days=days,
        min_pairs=min_pairs,
        min_sample_days=min_sample_days,
    )
    # 算不出 ρ（父本全 NaN / 对域不足）→ 残差同样不可信：unavailable。
    # 单基下不存在「无对照列」情形（基恒被传入），唯一诚实出口是「不可算」。
    if str(basis_name) not in rho.index or not math.isfinite(
        float(rho[str(basis_name)])
    ):
        return IncrementalResult("unavailable", None, None, 0, (), None)
    abs_corr = abs(float(rho[str(basis_name)]))
    basis = (str(basis_name),)
    res = _sequential_residual(values, controls, days=days, min_pairs=min_pairs)
    daily = ic_stats.daily_ic(res, fwd_ret, min_names=min_names)
    summary = ic_stats.summarize_ic(daily, min_days=min_ic_days)
    if summary is None or not math.isfinite(summary.icir):
        return IncrementalResult(
            "insufficient_days", None, None, int(len(daily)), basis, abs_corr
        )
    return IncrementalResult(
        "ok",
        abs(float(summary.icir)),
        _finite_or_none(summary.rank_ic_mean),
        int(summary.n_days),
        basis,
        abs_corr,
    )


def incremental_ic_ir(
    values: pd.Series,
    controls: pd.DataFrame,
    *,
    sample_days: set[str] | None = None,
    basis_exclude: tuple[str, ...] | set[str] = (),
    horizon: int = DEFAULT_HORIZON,
    fwd_ret: pd.Series | None = None,
    rho: pd.Series | None = None,
    basis_screen_abs_corr: float = DEFAULT_BASIS_ABS_CORR,
    min_pairs: int = DEFAULT_MIN_PAIRS_PER_DAY,
    min_sample_days: int = DEFAULT_MIN_SAMPLE_DAYS,
    min_ic_days: int = DEFAULT_MIN_IC_DAYS,
    min_names: int = DEFAULT_MIN_NAMES,
) -> IncrementalResult:
    """残差 IC_IR 主入口（T-FE-01）。``controls`` 须含 ``close``（除非显式
    传 ``fwd_ret``）；``sample_days`` 只影响 ρ 筛选与残差 IC 的取日，
    **前向收益永远在全交易日帧上计算**。

    ``rho`` 允许调用方复用**同一帧、同一 exclude、同一门槛**已算好的
    筛选列（物化器与值级查重共用最重的逐日秩相关计算，避免翻倍）；
    传入时必须 ``basis_exclude`` 为空——两者混用会得出与任一机制都不
    一致的口径，响亮拒绝而不是静默取其一种。
    """
    if values is None or len(values) == 0 or controls is None or controls.empty:
        return IncrementalResult("unavailable", None, None, 0, (), None)
    if rho is not None and basis_exclude:
        raise ValueError("rho 与 basis_exclude 互斥（预筛口径不得再改排除集）")
    fwd = (
        fwd_ret
        if fwd_ret is not None
        else forward_return_series(controls, horizon=horizon)
    )
    if fwd is None or len(fwd) == 0:
        return IncrementalResult("unavailable", None, None, 0, (), None)
    days = set(sample_days) if sample_days is not None else None

    if rho is None:
        rho = mean_rho_by_column(
            values,
            controls,
            sample_days=days,
            exclude=tuple(basis_exclude),
            min_pairs=min_pairs,
            min_sample_days=min_sample_days,
        )
    abs_rho = rho.abs().dropna()
    if abs_rho.empty:
        # 空筛选结果有两种性质完全不同的来源，必须分开（评审 HIGH-1）：
        # - ``len(rho)==0``：候选对照列本就没有（全被 exclude / 库为空）→
        #   「无冗余基」是事实，no_basis 诚实；
        # - ``len(rho)>0`` 但全 NaN：有候选列却算不出 ρ（每日截面 < min_pairs、
        #   对照列全 NaN 等）→ 判 unavailable，由闸走 skipped——绝不冒充
        #   「无冗余基」绿票（soft 下是审计假绿灯，升 hard 即假放过）。
        if len(rho) == 0:
            return IncrementalResult("no_basis", None, None, 0, (), None)
        return IncrementalResult("unavailable", None, None, 0, (), None)
    screened = abs_rho[abs_rho >= basis_screen_abs_corr]
    if screened.empty:
        return IncrementalResult("no_basis", None, None, 0, (), None)
    basis = sorted(
        (str(c) for c in screened.index), key=lambda c: (-float(screened[c]), c)
    )

    res = _sequential_residual(values, controls[basis], days=days, min_pairs=min_pairs)
    daily = ic_stats.daily_ic(res, fwd, min_names=min_names)
    summary = ic_stats.summarize_ic(daily, min_days=min_ic_days)
    max_corr = float(screened.max())
    if summary is None or not math.isfinite(summary.icir):
        return IncrementalResult(
            "insufficient_days", None, None, int(len(daily)), tuple(basis), max_corr
        )
    return IncrementalResult(
        "ok",
        abs(float(summary.icir)),
        _finite_or_none(summary.rank_ic_mean),
        int(summary.n_days),
        tuple(basis),
        max_corr,
    )
