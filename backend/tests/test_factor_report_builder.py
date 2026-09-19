"""build_factor_report 的口径回归测试（2026-09-19）。

背景 —— 三个**静默错口径**缺陷，报告页已在线上跑了两批、共 1700+ 条因子却没人发现，
因为每个缺陷都只让数字变小、不报错：

1. **主前瞻期取错**：``compute_one_date`` 里写的是 ``horizon = horizons[0]``
   ——列表第一个，不是 ``--horizon`` 指定的那个。``--horizons`` 默认首项是
   ``fwd_ret_1``，于是 ``meta.horizon`` 记 ``fwd_ret_5``，而
   ``ic_mean/icir/t_value/win_rate/quantiles/ls_mean/monotonicity`` 全是
   **fwd_ret_1** 的。（实测旧产物：``ic_mean`` 与 ``ic_by_horizon["fwd_ret_1"]``
   吻合 100.0%，与 fwd_ret_5 吻合 0.1%。）
2. **分位均值分母错**：``q_cnt`` 累加的是「当日**有值的分位个数**」，满分位因子
   一天贡献 10 而不是 1 —— ``quantiles``/``ls_mean`` 被压成真值的 ~1/10
   （实测：报告 ``quantiles[0]``=+0.00009 vs 明细 parquet 的 ``q1`` 日均 +0.00089）。
3. **两个多空口径不自洽**：headline ``ls_mean`` 取「跨日先平均分位再相减」，而
   ``ls_by_horizon``/parquet 的 ``ls_*`` 列是「逐日 q10−q1 再平均」，实测差 ~32×。

测试锁死三类不变量：
  A. 主前瞻期显式生效（不随 horizon 列表顺序漂移）；
  B. 分位均值 = **逐分位**归一（等价于「各分位均值的均值 == 全体均值」这条恒等式）；
  C. headline / by_horizon / 明细 parquet 三处同源自洽。

外加**独立复算**：报告里的 IC 用 pandas 的 Spearman 另一条实现路径重算，
以及多空价差的闭式解对照 —— 不是把被测代码抄第二遍。

⚠️ JSON 里的 headline 指标都 ``round(..., 5)``，故比较容差取 1e-5。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.scripts import build_factor_report as bfr

# 200 只股票 → 十分位每档恰好 20 只（等分位是下面恒等式的成立前提）
N_SYMBOLS = 200
DAYS = ["20240102", "20240103", "20240104"]
PRIMARY = "fwd_ret_5"
# 故意用**默认**前瞻期列表：首项 fwd_ret_1 ≠ 主前瞻期，正是当年踩坑的形状
DEFAULT_HORIZONS = "fwd_ret_1,fwd_ret_2,fwd_ret_5,fwd_ret_10,fwd_ret_20"
RND = 1e-5          # JSON 四舍五入到 5 位小数后的容差

# 各前瞻期标签的斜率：只有主期（fwd_ret_5）为负，且各期量级互不相同 —— 拿错期一眼可见
_LABEL_SLOPES = {
    "fwd_ret_1": 0.010,
    "fwd_ret_2": 0.020,
    "fwd_ret_5": -0.010,
    "fwd_ret_10": 0.005,
    "fwd_ret_20": 0.040,
}
# i ∈ [0,199] 上等分位（每档 20 只）的多空闭式价差 = slope × (189.5 − 9.5)
_LS_CLOSED_FORM = {h: s * 180.0 for h, s in _LABEL_SLOPES.items()}


# ─────────────────────────── 合成数据 ───────────────────────────

def _symbols() -> list[str]:
    return [f"S{i:03d}" for i in range(N_SYMBOLS)]


def _rank_grid() -> np.ndarray:
    """因子取值 = 0..N-1（严格递增 → 秩就是序号本身，无并列）。"""
    return np.arange(N_SYMBOLS, dtype=np.float64)


def _write(root: Path, dt: str, df: pd.DataFrame) -> None:
    d = root / f"dt={dt}"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), d / "data.parquet")


def _labels_table_fixture(root: Path) -> tuple[Path, Path]:
    """因子表 + 独立标签表：五期标签**斜率各不相同且只有 fwd_ret_5 为负**。

    标签表必须含 --horizons 请求的**每一列**（``pq.read_table(columns=...)`` 缺列直接抛
    ArrowInvalid），所以五期都造出来 —— 顺带成了四个诱饵：

      fwd_ret_1  +0.010·i   → f_rank 的 IC = +1
      fwd_ret_2  +0.020·i   → IC = +1（斜率是 1 期的 2 倍）
      fwd_ret_5  −0.010·i   → IC = **−1**  ← 主前瞻期（唯一的负号）
      fwd_ret_10 +0.005·i   → IC = +1（斜率的一半）
      fwd_ret_20 +0.040·i   → IC = +1

    斜率不同 → 各期多空价差各不相同（±1.8 / ±3.6 / ±0.9 / ±7.2），所以**除了 IC 符号，
    ``ls_by_horizon`` 的数值也能逐个对上号**：拿错期不是「差一点」，是差整数倍。
    噪声 1e-4 远小于步长 1e-2，不会打乱次序（无并列、无翻转）。
    """
    fac_dir, lab_dir = root / "factor", root / "labels"
    i = _rank_grid()
    for dt in DAYS:
        rng = np.random.default_rng(int(dt))
        _write(fac_dir, dt, pd.DataFrame({
            "symbol": _symbols(),
            "f_rank": i,                                  # 与 fwd_ret_1/2/10/20 同序
            "f_inverse": -i,                              # 与 fwd_ret_5 同序
            "f_noise": rng.standard_normal(N_SYMBOLS),    # 与哪期都无关
        }))
        lab = {"symbol": _symbols()}
        for name, slope in _LABEL_SLOPES.items():
            lab[name] = slope * i + 1e-4 * rng.standard_normal(N_SYMBOLS)
        _write(lab_dir, dt, pd.DataFrame(lab))
    return fac_dir, lab_dir


def _close_fwd_fixture(root: Path) -> Path:
    """close_fwd 路径：日收益恒为 0.001·i（与 f_rank 完全同序），T+k 对齐错了必露馅。"""
    fac_dir = root / "factor"
    i = _rank_grid()
    close = np.full(N_SYMBOLS, 100.0)
    for dt in DAYS:
        _write(fac_dir, dt, pd.DataFrame({
            "symbol": _symbols(),
            "f_rank": i,
            "f_noise": np.random.default_rng(int(dt)).standard_normal(N_SYMBOLS),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(N_SYMBOLS, 1000.0),
        }))
        close = close * (1.0 + 0.001 * i)   # 次日收盘 → 日收益严格单调递增
    return fac_dir


def _labels(fac_dir: Path, lab_dir: Path, dt: str) -> pd.DataFrame:
    """按 symbol 合并后的原始因子+标签 —— 独立复算用的输入。"""
    fac = pq.read_table(fac_dir / f"dt={dt}" / "data.parquet").to_pandas()
    lab = pq.read_table(lab_dir / f"dt={dt}" / "data.parquet").to_pandas()
    return fac.merge(lab, on="symbol")


# ─────────────────────────── 运行器 ───────────────────────────

def _run(monkeypatch, tmp_path: Path, name: str, fac_dir: Path, lab_dir: Path | None,
         label_mode: str, extra: list[str] | None = None) -> tuple[dict, pd.DataFrame]:
    """按数据集注册表的口径跑一遍 main()，返回 (报告 JSON, 明细序列 DataFrame)。"""
    monkeypatch.setitem(bfr.DATASETS, name, {
        "label": "pytest 合成数据集",
        "dir_parts": ("pytest", name),
        "label_mode": label_mode,
        "label_parts": ("pytest", "labels"),
        "meta_cols": ("symbol", "date", "time", "dt", "open", "high", "low", "close", "volume", "amount"),
        "library_rule": "fixed:pytest",
        "universe": "pytest",
    })
    monkeypatch.setattr(bfr, "dataset_dir", lambda _ds: fac_dir)
    if lab_dir is not None:
        monkeypatch.setattr(bfr, "dataset_label_dir", lambda _ds: lab_dir)

    out = tmp_path / "report" / "factor_report.json"
    series = tmp_path / "report" / "factor_series.parquet"
    monkeypatch.setattr(sys, "argv", [
        "build_factor_report.py", "--dataset", name, "--years", "0", "--workers", "1",
        "--out", str(out), "--series-out", str(series), *(extra or []),
    ])
    assert bfr.main() == 0, "构建器返回非 0"
    return json.loads(out.read_text(encoding="utf-8")), pq.read_table(series).to_pandas()


def _factor(report: dict, name: str) -> dict:
    return next(f for f in report["factors"] if f["name"] == name)


def _mk_partial(dt: str, *, ic: float, q_ret: np.ndarray | None = None,
                q_cnt: np.ndarray | None = None, n_factors: int = 1,
                n_symbols: int = 2) -> dict:
    """构造一天的最小 partial —— merge_partials 的输入契约（纯单测用，不碰磁盘）。"""
    q = np.full((10, n_factors), np.nan) if q_ret is None else q_ret
    c = np.zeros((10, n_factors)) if q_cnt is None else q_cnt
    return {
        "dt": dt,
        "symbols": np.array([f"S{i}" for i in range(n_symbols)]),
        "n_rows": n_symbols,
        "gram": np.eye(n_factors),
        "col_sum": np.zeros(n_factors),
        "q_ret": q,
        "q_cnt": c,
        "ic": np.array([ic]),
        "ic_by_h": {"fwd_ret_5": np.array([ic])},
        "q_by_h": {"fwd_ret_5": q},
        "qcnt_by_h": {"fwd_ret_5": c},
        "group": np.zeros((n_symbols, n_factors), dtype=np.int16),
        "coverage": np.zeros(n_factors, dtype=np.float32),
    }


@pytest.fixture()
def run_labels_table(monkeypatch, tmp_path):
    """主用例：独立标签表模式，主前瞻期 fwd_ret_5，而 horizon 列表首项是 fwd_ret_1。"""
    fac_dir, lab_dir = _labels_table_fixture(tmp_path)
    report, series = _run(monkeypatch, tmp_path, "pytest_labels_table", fac_dir, lab_dir,
                          "labels_table", ["--horizon", PRIMARY, "--horizons", DEFAULT_HORIZONS])
    return report, series, fac_dir, lab_dir


# ─────────────────────────── A. 主前瞻期 ───────────────────────────

def test_primary_horizon_is_honoured_not_the_first_of_the_list(run_labels_table):
    """A1（缺陷 1 回归）：headline 指标必须来自 --horizon，而非 --horizons 首项。"""
    report, _, _, _ = run_labels_table

    # Arrange：确认这个用例真的踩在坑上 —— 列表首项不是主前瞻期
    assert report["meta"]["horizons"][0] == "fwd_ret_1"
    assert report["meta"]["horizon"] == PRIMARY

    # Act
    rank_ic = _factor(report, "f_rank")["ic_mean"]

    # Assert：f_rank 与 fwd_ret_5 完全反序（−1）、与 fwd_ret_1 完全同序（+1）。
    # 旧的 horizons[0] 会给 +1 —— 符号直接翻过来。
    assert rank_ic == pytest.approx(-1.0, abs=RND), "headline IC 取的是列表首项 fwd_ret_1"
    assert _factor(report, "f_inverse")["ic_mean"] == pytest.approx(1.0, abs=RND)


def test_every_horizon_is_computed_from_its_own_label(run_labels_table):
    """A2：五期同趟计算时，**每一期各自对上自己的标签**（不串期）。

    各期标签斜率不同 → 多空价差是 slope × 180，逐个对上号；主期是唯一负号。
    """
    report, _, _, _ = run_labels_table
    r = _factor(report, "f_rank")

    # Act
    by_h = r["ls_by_horizon"]

    # Assert
    for h, expect in _LS_CLOSED_FORM.items():
        assert by_h[h] == pytest.approx(expect, abs=1e-3), f"{h} 的多空价差不是它自己的标签算的"
    assert r["ic_by_horizon"][PRIMARY] == pytest.approx(-1.0, abs=RND)
    for other in ("fwd_ret_1", "fwd_ret_2", "fwd_ret_10", "fwd_ret_20"):
        assert r["ic_by_horizon"][other] == pytest.approx(1.0, abs=RND)
    # headline 只与主期等值 —— 防止「换个位置再犯」
    assert r["ic_mean"] == pytest.approx(r["ic_by_horizon"][PRIMARY], abs=RND)
    assert r["ls_mean"] == pytest.approx(_LS_CLOSED_FORM[PRIMARY], abs=1e-3)


def test_win_rate_follows_the_primary_horizon_too(run_labels_table):
    """A3：同为头条口径的 win_rate 也必须跟着主前瞻期走。

    主期（fwd_ret_5）三日 IC 全为 −1 → 胜率 0；若错取任一正号期则是 1.0。
    """
    report, _, _, _ = run_labels_table

    # Act
    r = _factor(report, "f_rank")

    # Assert
    assert r["win_rate"] == pytest.approx(0.0, abs=1e-9)


def test_tvalue_and_icir_are_derived_from_the_merged_ic_series():
    """A4：t_value / icir 的**定义**锁死（纯单测，不受夹具噪声影响）。

    icir = mean(IC)/std(IC)，t = icir × √天数，win_rate = IC>0 的占比。
    """
    # Arrange：三日 IC 全负且有方差 → icir 与 t 都为负，胜率 0
    ics = [-0.5, -0.3, -0.1]
    partials = [_mk_partial(dt, ic=ic) for dt, ic in zip(DAYS, ics, strict=True)]
    ic = np.array(ics)

    # Act
    out = bfr.merge_partials(iter(partials), n_factors=1, horizons=["fwd_ret_5"])

    # Assert
    assert out["ic_mean"][0] == pytest.approx(ic.mean())
    assert out["icir"][0] == pytest.approx(ic.mean() / ic.std())
    assert out["t_value"][0] == pytest.approx(ic.mean() / ic.std() * np.sqrt(len(ic)))
    assert out["win_rate"][0] == pytest.approx(0.0)


def test_tvalue_is_guarded_when_ic_has_no_dispersion():
    """A5：IC 完全无离散（std=0）时走守卫分支落 0，而不是 inf/nan。

    真实数据不会出现，但完美一致的三天夹具会 —— 这个分支必须有确定行为。
    """
    # Arrange
    partials = [_mk_partial(dt, ic=-1.0) for dt in DAYS]

    # Act
    out = bfr.merge_partials(iter(partials), n_factors=1, horizons=["fwd_ret_5"])

    # Assert
    assert out["icir"][0] == 0.0
    assert out["t_value"][0] == 0.0


# ─────────────────────────── B. 分位均值分母 ───────────────────────────

def test_decile_means_average_to_the_universe_mean(run_labels_table):
    """B1（缺陷 2 回归）：等分位下 mean(各分位均值) == 全体均值，是个恒等式。

    旧代码把分母写成「当日有值的分位个数」（满分位因子每天 +10），
    这条恒等式会**恰好差 10 倍**。真值从写盘的标签表里现读，不靠手算。
    """
    report, _, fac_dir, lab_dir = run_labels_table

    # Arrange：三日 fwd_ret_5 的全体均值（与构建器读的是同一份文件）
    truth = float(np.mean([
        _labels(fac_dir, lab_dir, dt)["fwd_ret_5"].mean() for dt in DAYS
    ]))

    for name in ("f_rank", "f_inverse", "f_noise"):
        # Act
        q = _factor(report, name)["quantiles"]

        # Assert
        assert len(q) == 10
        assert float(np.mean(q)) == pytest.approx(truth, abs=RND), f"{name} 分位均值分母错"


def test_decile_ordering_follows_the_factor(run_labels_table):
    """B2：分位收益的方向由因子决定 —— f_rank/f_inverse 单调且互为镜像。"""
    report, _, _, _ = run_labels_table

    # Act
    q_rank = _factor(report, "f_rank")["quantiles"]
    q_inv = _factor(report, "f_inverse")["quantiles"]

    # Assert：f_rank 与 fwd_ret_5 反序 → 分位收益递减
    # strict=False 是故意的：这里就是拿序列与自己的错位副本做滑动配对
    assert all(a > b for a, b in zip(q_rank, q_rank[1:], strict=False))
    assert q_rank == pytest.approx(list(reversed(q_inv)), abs=RND)
    assert _factor(report, "f_rank")["monotonicity"] < -0.99


def test_merge_partials_normalises_each_decile_by_its_own_day_count():
    """B3：纯单测 —— 覆盖不均时，分母必须是「该分位自己的有效天数」。

    构造（单因子）：
      day1: q1=1.0，q2 **缺**       → 计数 (1, 0)
      day2: q1=3.0，q2=2.0          → 计数 (1, 1)
    逐分位归一 → q_mean = (2.0, 2.0)；旧的按列求和 → q_sum[0]/4 = 1.0（压成 1/2）。
    """
    # Arrange：day1 只有 q1；day2 q1、q2 都有
    nan = float("nan")
    d1 = np.full((10, 1), nan)
    d1[0, 0] = 1.0
    c1 = np.zeros((10, 1))
    c1[0, 0] = 1.0
    d2 = np.full((10, 1), nan)
    d2[:2, 0] = [3.0, 2.0]
    c2 = np.zeros((10, 1))
    c2[:2, 0] = 1.0
    partials = [
        _mk_partial(DAYS[0], ic=0.5, q_ret=d1, q_cnt=c1),
        _mk_partial(DAYS[1], ic=0.5, q_ret=d2, q_cnt=c2),
    ]

    # Act
    out = bfr.merge_partials(iter(partials), n_factors=1, horizons=["fwd_ret_5"])

    # Assert：q1 = (1+3)/2，q2 = 2/1；旧口径会把 q1 算成 (1+3)/4 = 1.0
    assert out["q_mean"][:2, 0].tolist() == pytest.approx([2.0, 2.0])
    assert out["series"]["q"][:, 0, 0].tolist() == pytest.approx([1.0, 3.0])


def test_merge_partials_ignores_missing_deciles_in_the_denominator():
    """B4：只有部分分位有值时，缺失分位不能被算进分母（旧的列求和会）。"""
    # Arrange：单日、单因子，只有 q1 有值
    q = np.full((10, 1), np.nan)
    q[0, 0] = 6.0
    cnt = np.zeros((10, 1))
    cnt[0, 0] = 1.0
    p = _mk_partial(DAYS[0], ic=float("nan"), q_ret=q, q_cnt=cnt, n_symbols=1)

    # Act
    out = bfr.merge_partials(iter([p]), n_factors=1, horizons=["fwd_ret_5"])

    # Assert：有值的档位按自己的天数归一；无值的档位落 **0 哨兵**
    # （故意不用 NaN：nan 经 json.dumps 会写出非法 JSON，前端 JSON.parse 直接抛）
    assert out["q_mean"][0, 0] == pytest.approx(6.0)
    assert out["q_mean"][1, 0] == 0.0


# ─────────────────────────── C. 三处口径自洽 ───────────────────────────

def test_ls_mean_equals_the_daily_ls_of_the_primary_horizon(run_labels_table):
    """C1（缺陷 3 回归）：headline ls_mean 与 ls_by_horizon[主] 必须同源等值。"""
    report, _, _, _ = run_labels_table

    for name in ("f_rank", "f_inverse", "f_noise"):
        # Act
        f = _factor(report, name)

        # Assert：同一口径 → 同值（两边都 round 到 5 位）
        assert f["ls_mean"] == pytest.approx(f["ls_by_horizon"][PRIMARY], abs=1e-9)


def test_headline_quantiles_match_the_series_parquet(run_labels_table):
    """C2：排行页（JSON）与明细页（parquet）必须给出同一组分位均值。

    这正是当初暴露缺陷的那条检查：JSON quantiles[0]=+0.00009 vs
    parquet 的 q1 日均 +0.00089（差 10 倍）。
    """
    report, series, _, _ = run_labels_table

    for f in report["factors"]:
        # Act
        name = f["name"]
        sub = series[series["factor"] == name]
        q_parquet = [sub[f"q{k + 1}"].mean() for k in range(10)]

        # Assert
        assert f["quantiles"] == pytest.approx(q_parquet, abs=RND), f"{name} 排行页与明细页不一致"


def test_ls_series_column_matches_the_horizon_suffix(run_labels_table):
    """C3：parquet 的 ls_5 列就是主前瞻期的「逐日 q10−q1」，且与 ls_1 符号相反。"""
    report, series, _, _ = run_labels_table
    sub = series[series["factor"] == "f_rank"]

    # Act + Assert
    assert _factor(report, "f_rank")["ls_by_horizon"][PRIMARY] == pytest.approx(
        sub["ls_5"].mean(), abs=RND)
    assert sub["ls_5"].mean() < 0 < sub["ls_1"].mean()


# ─────────────────────────── D. 独立复算 ───────────────────────────

def test_ic_recomputed_independently_with_pandas_spearman(run_labels_table):
    """D1：用 pandas 的 Spearman 重算 IC（**另一条实现路径**）。

    被测实现是「argsort 名次 + Pearson」；这里是 pandas 的
    rank(method='average') + 相关系数。连续取值无并列，两者应逐位吻合。
    """
    report, _, fac_dir, lab_dir = run_labels_table

    # Act：逐日独立复算再取均值（与构建器的日度 IC 均值同定义）
    expect = {
        col: float(np.mean([
            _labels(fac_dir, lab_dir, dt)[col].corr(
                _labels(fac_dir, lab_dir, dt)[PRIMARY], method="spearman")
            for dt in DAYS
        ]))
        for col in ("f_rank", "f_inverse", "f_noise")
    }

    # Assert
    for col, want in expect.items():
        assert _factor(report, col)["ic_mean"] == pytest.approx(want, abs=RND), f"{col} 与独立复算不符"


def test_ls_matches_the_closed_form_on_a_linear_label(run_labels_table):
    """D2：闭式解对照 —— y = −0.01·i 上，十分位 q10 取 i∈[180,199]、q1 取 i∈[0,19]：
      mean(q10) − mean(q1) = −0.01 × (189.5 − 9.5) = **−1.8**
    """
    report, _, _, _ = run_labels_table

    # Act
    ls = _factor(report, "f_rank")["ls_by_horizon"][PRIMARY]

    # Assert（噪声 1e-4 经 20 只平均后 ~1e-5，容差留 1e-3）
    assert ls == pytest.approx(-1.8, abs=1e-3)


# ─────────────────────────── E. 另一条标签路径 ───────────────────────────

def test_close_fwd_labels_use_the_t_plus_k_close(monkeypatch, tmp_path):
    """E1：close_fwd 模式（L1/L2/alpha360 走这条）的 T+k 对齐同样生效。

    日收益恒为 0.001·i，故 f_rank 的 IC 必须恰好 = +1；若 T+k 取成 T（同分区自比），
    收益恒为 0 → IC 变 0 或 NaN。
    """
    # Arrange
    fac_dir = _close_fwd_fixture(tmp_path)

    # Act
    report, _ = _run(monkeypatch, tmp_path, "pytest_close_fwd", fac_dir, None,
                     "close_fwd", ["--horizon", "fwd_ret_1", "--horizons", "fwd_ret_1"])

    # Assert
    assert report["meta"]["label_mode"] == "close_fwd"
    assert _factor(report, "f_rank")["ic_mean"] == pytest.approx(1.0, abs=RND)
    # 分位均值恒等式在 close_fwd 下同样成立（y 均值为 0.001 × 均秩）
    q = _factor(report, "f_rank")["quantiles"]
    assert float(np.mean(q)) == pytest.approx(0.001 * 99.5, abs=RND)


# ─────────────────────────── F. 流式合并的既有约束 ───────────────────────────

def test_dates_are_emitted_in_ascending_order(run_labels_table):
    """F1：明细序列必须按 dt 升序 —— 换手拿相邻两日比，乱序会把换手算成噪声。"""
    report, series, _, _ = run_labels_table

    # Act
    dates = series["date"].drop_duplicates().tolist()

    # Assert
    assert dates == sorted(dates)
    assert [str(int(d)) for d in dates] == DAYS
    assert report["meta"]["start"] == DAYS[0] and report["meta"]["end"] == DAYS[-1]
    assert report["meta"]["n_dates"] == len(DAYS)


def test_turnover_is_zero_when_decile_membership_never_moves(run_labels_table):
    """F2：因子取值逐日不变 → 分位成员不动 → 单边换手 0（而不是「无数据」）。"""
    report, series, _, _ = run_labels_table

    # Act
    sub = series[series["factor"] == "f_rank"]

    # Assert
    assert _factor(report, "f_rank")["turnover"] == pytest.approx(0.0, abs=1e-12)
    assert sub["turnover"].tail(2).tolist() == pytest.approx([0.0, 0.0])


def test_parallel_and_serial_runs_agree(monkeypatch, tmp_path):
    """F3：workers>1 的进程池路径（生产就用它）与串行结果逐位一致。"""
    # Arrange
    fac_dir, lab_dir = _labels_table_fixture(tmp_path)
    common = ["--horizon", PRIMARY, "--horizons", DEFAULT_HORIZONS]
    serial, _ = _run(monkeypatch, tmp_path, "pytest_serial", fac_dir, lab_dir,
                     "labels_table", common)

    # Act
    work = tmp_path / "parallel"
    work.mkdir()
    parallel, _ = _run(monkeypatch, work, "pytest_parallel", fac_dir, lab_dir,
                       "labels_table", [*common, "--workers", "2"])

    # Assert
    assert [f["name"] for f in parallel["factors"]] == [f["name"] for f in serial["factors"]]
    for a, b in zip(parallel["factors"], serial["factors"], strict=True):
        assert a["ic_mean"] == pytest.approx(b["ic_mean"], abs=0)
        assert a["quantiles"] == pytest.approx(b["quantiles"], abs=0)
        assert a["turnover"] == pytest.approx(b["turnover"], abs=0)
