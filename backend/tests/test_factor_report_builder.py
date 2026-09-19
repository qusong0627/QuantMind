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


# ═══════════ E. 截面附加口径（半/中性化/分域 IC、数据质量、风格相关）═══════════
#
# 这一组锁的是「构建期只加需要当日截面矩阵的东西」这条分工：
# 附加列的**数值口径**由 test_factor_report_metrics.py / _neutralize.py 锁定，
# 这里只锁**接线**——列有没有真的落进 parquet、快照与序列是否同源、
# 取数降级时是显式 None 还是冒充的 0。


def test_快照带结构版本与风格供给状态(run_labels_table):
    report, _, _, _ = run_labels_table
    meta = report["meta"]
    assert meta["schema_version"] == bfr.SCHEMA_VERSION >= 2
    # 未构建风格产物时必须显式记 missing（前端据此降级），而不是假装有数据
    assert meta["style_model"] in ("ok", "missing")
    assert isinstance(meta["style_names"], list)


def test_附加列全部落进明细_parquet_且不加行(run_labels_table):
    _, series, _, _ = run_labels_table
    for col in ("ic_top", "ic_bot", "ic_neutral", "ic_large", "ic_mid",
                "ic_small", "clip_frac", "n_valid"):
        assert col in series.columns, f"明细 parquet 缺列 {col}"
    # 附加列是加列不是加行：3 因子 × 3 天
    assert len(series) == len(DAYS) * 3


def test_数据质量列有已知答案(run_labels_table):
    """合成因子严格等距（无极端值）→ clip_frac≈0；有效样本恒为全部 200 只。"""
    _, series, _, _ = run_labels_table
    assert (series["n_valid"] == N_SYMBOLS).all()
    assert float(np.nanmax(series["clip_frac"])) < 0.02


def test_半截面IC_在主前瞻期上算且符号随因子(run_labels_table):
    """f_inverse 与主期 fwd_ret_5 完全同序 → 上下半 IC 都 = +1；
    f_rank 与之反向 → 上下半 IC 都 = −1。**取错前瞻期会得到反号**（其余四期斜率同号）。"""
    report, _, _, _ = run_labels_table
    inv = _factor(report, "f_inverse")
    rnk = _factor(report, "f_rank")
    assert inv["ic_top_mean"] == pytest.approx(1.0, abs=RND)
    assert inv["ic_bot_mean"] == pytest.approx(1.0, abs=RND)
    assert rnk["ic_top_mean"] == pytest.approx(-1.0, abs=RND)
    assert rnk["ic_bot_mean"] == pytest.approx(-1.0, abs=RND)
    # 噪声因子的半 IC 必须**有值**（不是 NaN）—— 有值才说明它真的算过
    assert np.isfinite(_factor(report, "f_noise")["ic_top_mean"])


def test_快照的附加均值与明细列逐因子同源(run_labels_table):
    """快照 *_mean 必须等于明细列按因子的均值 —— 两处不同源是本项目反复踩的坑。"""
    report, series, _, _ = run_labels_table
    for name, key, col in (("f_inverse", "ic_top_mean", "ic_top"),
                           ("f_rank", "ic_bot_mean", "ic_bot"),
                           ("f_noise", "ic_top_mean", "ic_top")):
        want = float(series.loc[series["factor"] == name, col].mean())
        assert _factor(report, name)[key] == pytest.approx(want, abs=RND)


# ── 取数降级 / 已知答案（直接调 compute_cross_section，不落盘）──

def _cs_inputs(n: int = 600):
    syms = np.array([f"S{i:03d}" for i in range(n)])
    i = np.arange(n, dtype=np.float64)
    return syms, i.reshape(-1, 1), np.ones(n, dtype=bool)


def test_纯行业因子_中性化IC_无定义而非_0(monkeypatch):
    """因子在每个行业内取常数 → 行业去均值后无截面信息。

    正确输出是 **NaN（无定义）**，不是 0（0 会被读成「测过，没效果」）。
    同时原始 IC 很高 —— 这一对比正是「中性化 IC」要回答的问题。
    """
    n = 600
    syms, _, ok_y = _cs_inputs(n)
    ind = np.array(["A"] * (n // 2) + ["B"] * (n - n // 2))
    level = {"A": 0.0, "B": 1.0}
    X = np.array([[level[c]] for c in ind], dtype=np.float64)
    y = np.where(ind == "B", 0.01, -0.01)          # 收益完全由行业解释（有噪声才非退化）
    y = y + np.random.default_rng(11).normal(0, 1e-4, n)
    monkeypatch.setattr(bfr, "load_mv", lambda dt, symbols=None: pd.Series(
        np.linspace(1.0, 1000.0, n), index=pd.Index(syms)))
    monkeypatch.setattr(bfr, "load_industry_map", lambda: pd.Series(ind, index=pd.Index(syms)))
    out = bfr.compute_cross_section(X, X, y, ok_y, syms, "20240102",
                                    {"neutral": True, "style_dir": None})
    # 原始 IC 很强（pandas 的 Spearman 独立重算）—— 「行业上有效、中性化后无定义」
    # 正是这组对比要说的。（注：分域 IC 在这份样例里同样无定义 —— 行业边界恰好与
    # 市值三分位重合，每个子域内因子取值恒定，零方差；这是样例构造使然，非缺陷。）
    raw_ic = float(pd.Series(X[:, 0]).corr(pd.Series(y), method="spearman"))
    assert raw_ic > 0.5, f"原始 IC 应当很强，实测 {raw_ic:.3f}"
    assert not np.isfinite(out["ic_neutral"][0]), "纯行业因子不该有中性化 IC"


def test_与市值共线的因子_中性化IC_无定义而非_0(monkeypatch):
    """因子 = a·市值 + b → 对 rank(市值) 正交后残差无方差 → NaN。"""
    n = 600
    syms, _, ok_y = _cs_inputs(n)
    mv = np.linspace(1.0, 1000.0, n)
    X = (3.0 * mv + 7.0).reshape(-1, 1)
    y = np.random.default_rng(13).normal(0, 0.01, n)
    monkeypatch.setattr(bfr, "load_mv", lambda dt, symbols=None: pd.Series(mv, index=pd.Index(syms)))
    monkeypatch.setattr(bfr, "load_industry_map", lambda: pd.Series(
        ["UNKNOWN"] * n, index=pd.Index(syms)))
    out = bfr.compute_cross_section(X, X, y, ok_y, syms, "20240102",
                                    {"neutral": True, "style_dir": None})
    assert not np.isfinite(out["ic_neutral"][0]), "与市值共线不该有中性化 IC"


def test_分域IC_大小盘各一段(monkeypatch):
    """市值递增、因子与收益在**小盘段**完全同序、大盘段是噪声 →
    小盘 IC ≈ +1、大盘 IC ≈ 0。分域切点跨因子一致（用当日全体市值定分位）。"""
    n = 900
    syms, R, ok_y = _cs_inputs(n)
    mv = np.arange(1.0, n + 1.0)
    rng = np.random.default_rng(17)
    y = np.where(mv < n / 3.0, R[:, 0], rng.normal(0, 1, n))
    monkeypatch.setattr(bfr, "load_mv", lambda dt, symbols=None: pd.Series(mv, index=pd.Index(syms)))
    monkeypatch.setattr(bfr, "load_industry_map", lambda: pd.Series(
        ["UNKNOWN"] * n, index=pd.Index(syms)))
    out = bfr.compute_cross_section(R, R, y, ok_y, syms, "20240102",
                                    {"neutral": True, "style_dir": None})
    assert out["ic_small"][0] > 0.9
    assert abs(out["ic_large"][0]) < 0.2


def test_取数降级_中性化块留_NaN_而不是_0(monkeypatch):
    """市值取不到（旧库/未同步）→ 中性化与分域 IC 全 NaN，半 IC 与数据质量照常。"""
    n = 300
    syms, R, ok_y = _cs_inputs(n)
    y = R[:, 0]
    monkeypatch.setattr(bfr, "load_mv", lambda dt, symbols=None: pd.Series(dtype=float))
    out = bfr.compute_cross_section(R, R, y, ok_y, syms, "20240102",
                                    {"neutral": True, "style_dir": None})
    for key in ("ic_neutral", "ic_large", "ic_mid", "ic_small"):
        assert not np.isfinite(out[key]).any(), f"{key} 在取数失败时应为 NaN"
    assert np.isfinite(out["ic_top"][0])            # 半 IC 不依赖外部取数
    assert np.isfinite(out["clip_frac"][0])


def test_skip_neutral_只关掉依赖取数的块(monkeypatch):
    n = 300
    syms, R, ok_y = _cs_inputs(n)
    out = bfr.compute_cross_section(R, R, R[:, 0], ok_y, syms, "20240102",
                                    {"neutral": False, "style_dir": None})
    assert not np.isfinite(out["ic_neutral"]).any()
    assert np.isfinite(out["ic_top"][0])


def test_风格产物缺失_返回_None_而不是_0(monkeypatch, tmp_path):
    n = 300
    syms, R, ok_y = _cs_inputs(n)
    out = bfr.compute_cross_section(R, R, R[:, 0], ok_y, syms, "20240102",
                                    {"neutral": False, "style_dir": str(tmp_path / "nope")})
    assert out["style_corr"] is None and out["style_names"] == ()


def test_风格产物存在时给出因子与各风格的秩相关(monkeypatch, tmp_path):
    """风格名从产物 schema 读（不硬编码）；size 与因子同序 → ρ=+1。"""
    n = 300
    syms, R, ok_y = _cs_inputs(n)
    sdir = tmp_path / "style"
    _write(sdir, "20240102", pd.DataFrame({
        "symbol": syms,
        "size": np.arange(1.0, n + 1.0),          # 与 R 同序
        "beta": -np.arange(1.0, n + 1.0),         # 与 R 反序
    }))
    out = bfr.compute_cross_section(R, R, R[:, 0], ok_y, syms, "20240102",
                                    {"neutral": False, "style_dir": str(sdir)})
    assert out["style_names"] == ("size", "beta")
    assert out["style_corr"].shape == (1, 2)
    assert out["style_corr"][0, 0] == pytest.approx(1.0)
    assert out["style_corr"][0, 1] == pytest.approx(-1.0)


# ── 逐组换手（组合换手 vs 全截面换组比例）──


def _gt(a: list[int], b: list[int], k: int) -> np.ndarray:
    """测试夹具：按 day-a → day-b 的组号名单直接调被测函数（n 只 × k 因子）。"""
    av = np.array(a, dtype=np.int16).reshape(-1, 1)
    bv = np.array(b, dtype=np.int16).reshape(-1, 1)
    valid = (av >= 0) & (bv >= 0)
    return bfr._group_turnover(av, bv, valid, k)


def test_逐组换手_分母是昨日该组只数():
    """3 只昨日在 G1（组号 0），今日 1 只留、2 只走 → G1 换手 = 2/3。

    「分母写成今日只数」或「分子写成双向变动」都会给出别的数，故这里用**不等量**的
    进出来锁死语义：只有 2 只进去、0 只出来时，换出比例必须是 0 而不是 2/5。
    """
    gt = _gt([0, 0, 0, 1, 1], [0, 1, 2, 1, 1], 1)
    assert gt[0, 0] == pytest.approx(2.0 / 3.0)
    assert gt[1, 0] == pytest.approx(0.0), "昨日 G2 全员留任 → 换手 0"
    assert np.isnan(gt[2, 0]), "昨日空仓的组换手无定义（NaN），不是 0"


def test_逐组换手_低于全截面换组比例():
    """组合换手必须能与全截面数字分开：一只从 G5 挪到 G6，全截面记 1 次变动，
    但 G3/G9 两条腿**完全没动** —— 把两者混为一谈就会凭空多出成本。"""
    a = [9, 9, 4, 4]
    b = [9, 9, 5, 4]
    gt = _gt(a, b, 1)
    cross = float(np.mean(np.array(a) != np.array(b)))
    assert cross == pytest.approx(0.25)
    # 组号 9 = G10、组号 4 = G5（下标 1-based 展开成 0-based）
    assert gt[9, 0] == pytest.approx(0.0), "G10 腿没动"
    assert gt[4, 0] == pytest.approx(0.5), "G5 走掉一只"


def test_逐组换手_无效样本不进任何组():
    """-1（当日该因子无效）必须整只剔除，不能落进某组污染分母。"""
    gt = _gt([-1, -1, 0, 0], [0, 1, 0, 0], 1)
    assert gt[0, 0] == pytest.approx(0.0)
    assert float(np.nansum(gt[:, 0])) == pytest.approx(0.0)


def test_逐组换手_形状不符即报错而不是静默广播():
    """`a` 给 (n,1) 而 k=2 时，`a*k + cols` 会广播成 (n,2) —— 凭空把第一列复印一份。

    广播在本函数的写法里永远不是想要的行为，故必须显式失败。
    """
    av = np.array([[0], [0], [0], [0]], dtype=np.int16)
    with pytest.raises(ValueError):
        bfr._group_turnover(av, av, np.ones((4, 1), bool), 2)


def test_逐组换手_逐因子独立不串列():
    """两列的分组各不相同：列偏移漏乘就会让两列共用一组 bin，数字互相串味。

    构造：列 0 全部留在 G1（换手 0）；列 1 整组从 G2 挪到 G3（G2 换手 1、G3 昨日空仓）。
    串列时列 1 会读成 0 或列 0 会读成 1，两种都能被下面四条断言抓住。
    """
    av = np.array([[0, 1], [0, 1], [0, 1], [0, 1]], dtype=np.int16)
    bv = np.array([[0, 2], [0, 2], [0, 2], [0, 2]], dtype=np.int16)
    gt = bfr._group_turnover(av, bv, np.ones((4, 2), bool), 2)
    assert gt.shape == (10, 2)
    assert gt[0, 0] == pytest.approx(0.0), "列 0 的 G1 全员留任"
    assert gt[1, 1] == pytest.approx(1.0), "列 1 的 G2 整组换出"
    assert np.isnan(gt[0, 1]), "列 1 的 G1 昨日空仓 → 无定义（不得借来列 0 的 0）"
    assert np.isnan(gt[1, 0]), "列 0 的 G2 昨日空仓 → 无定义"


def test_逐组换手落进明细_parquet(run_labels_table):
    _, series, _, _ = run_labels_table
    cols = [f"gt{i}" for i in range(1, 11)]
    for col in cols:
        assert col in series.columns, f"明细 parquet 缺列 {col}"
    # 逐组换手是「与日期无关」的汇总量：同一因子的值在每一天都相同
    sub = series[series["factor"] == "f_rank"]
    assert sub["gt3"].nunique(dropna=False) == 1


# ─────────────── 快照 headline（7 指标环的全库分位基准）───────────────
#
# 存在的理由：环的弧长是**全库百分位**而不是裸值，故全库每个因子都要有同一口径的
# Returns/IR/Turnover/Fitness/Margin。此前快照只有 IC/ICIR，其余五项无从比较。
# 本组测试的**核心**不是「算得对」，而是「构建期与读时算的是同一件事」——
# 两处各写一份公式，改了一处就会静默产出「环上一个值、页签里另一个值」。

def _q_gt(k: int = 3, t: int = 60):
    """合成 (T, 10, K) 分位收益与 (10, K) 逐组换手。

    噪声刻意取 1%/日（真实多空组合的量级）而不是极小值：σ 太小会把 IR 推到几百，
    此时恒等式两边的**落盘舍入**被放大，容差再也说不清「多少才算口径差异」。
    """
    rng = np.random.default_rng(7)
    q = np.zeros((t, 10, k), dtype=np.float64)
    for j in range(k):
        base = (np.arange(10, dtype=np.float64) - 8.5) * 0.001 * (j + 1)   # G3>G9 的单调梯度
        q[:, :, j] = base + rng.normal(0.0, 0.01, size=(t, 10))
    gt = np.full((10, k), 0.2, dtype=np.float64)
    gt[2, :] = 0.3
    gt[8, :] = 0.5          # 两条腿均值 = 0.4
    return q, gt


def test_headline_与读时_blocks_逐位一致():
    """**本条是这组测试的全部意义**：构建期写进快照的 headline，必须与读时从
    明细 parquet 现算的 headline_block 一致。两处公式一旦漂移，页面上就是
    「指标环一个数、分组回测页签另一个数」，而且都不报错。"""
    from backend.services.engine.factor_report import blocks as B

    q, gt = _q_gt()
    snap = bfr.build_headline_snapshot(q, gt, k_main=5, n_factors=3)

    df = pd.DataFrame({f"gt{i + 1}": gt[i] for i in range(10)})
    for j in range(3):
        read = B.headline_block(
            df, q[:, :, j], np.zeros(q.shape[0]), long_group=3, short_group=9, cost_bps=20.0, k=5,
        )
        for key in ("returns", "ir", "turnover", "fitness", "margin"):
            # 容差 = 落盘的四舍五入（_r6 存 6 位小数），不是公式差异：
            # round(x, 6) 与 x 的偏差上界恰为 5e-7，超出即说明两处口径真的不同。
            assert snap[j][key] == pytest.approx(read[key], abs=5.1e-7), (
                f"因子 {j} 的 {key}：快照 {snap[j][key]} vs 读时 {read[key]}"
            )


def test_headline_缺少逐组换手时_fitness_margin_为_None_而_returns_照常():
    """旧快照没有 gt 列。此时 Fitness/Margin 无定义（它们的分母就是换手），
    写 0 会让人以为「盈亏比为零」；Returns/IR 不受影响，必须照常给。"""
    q, _ = _q_gt()
    snap = bfr.build_headline_snapshot(q, None, k_main=5, n_factors=3)
    assert snap[0]["turnover"] is None
    assert snap[0]["fitness"] is None and snap[0]["margin"] is None
    assert snap[0]["returns"] is not None and snap[0]["ir"] is not None


def test_headline_满足_brain_恒等式():
    """恒等式在**落盘精度内**成立。

    容差取相对 1e-5：三个量各自 round 到 6 位小数后，误差会被 Div/sqrt 放大 ——
    这个量级（~1e-6 相对）与任何真实口径差异（动辄百分之几十）差四个数量级，
    既能过、又抓得住错。同时先断言量级非退化：否则「两边都是 0」也满足恒等式
    （零项假通过）。
    """
    q, gt = _q_gt(k=1)
    h = bfr.build_headline_snapshot(q, gt, k_main=5, n_factors=1)[0]
    assert h["turnover"] == pytest.approx(0.4), "取两条腿的均值，不是某个单组"
    assert abs(h["returns"]) > 1e-4 and abs(h["ir"]) > 0.1 and abs(h["fitness"]) > 0.1, (
        f"合成序列退化，恒等式会空成立：{h}"
    )
    tol = {"rel": 1e-5}
    assert h["margin"] == pytest.approx(h["returns"] / h["turnover"], **tol)
    expect_fit = h["ir"] * np.sqrt(abs(h["returns"]) / max(h["turnover"], 0.125))
    assert h["fitness"] == pytest.approx(expect_fit, **tol)


def test_headline_记录所用分组_便于前端标注():
    """环上的数字是「默认 G3/G9 口径」。用户把页面切到 G1/G10 后，环不能再声称
    自己还是默认口径 —— 故分组必须随值一起存下来。"""
    q, gt = _q_gt(k=1)
    h = bfr.build_headline_snapshot(q, gt, k_main=5, n_factors=1)[0]
    assert h["long_group"] == bfr.DEF_LONG and h["short_group"] == bfr.DEF_SHORT


def test_headline_形状不符即抛_而不是静默错位():
    """(T,10,K) 与 (10,K) 都是三维/二维的近似形状，串列后不会抛、只会算错。"""
    q, gt = _q_gt(k=3)
    with pytest.raises(ValueError, match="分位序列形状"):
        bfr.build_headline_snapshot(q, gt, k_main=5, n_factors=99)
    with pytest.raises(ValueError, match="逐组换手形状"):
        bfr.build_headline_snapshot(q, gt[:, :2], k_main=5, n_factors=3)


def test_headline_空序列时全为_None_而不是_NaN():
    """JSON 里出现 NaN 会让前端 JSON.parse 直接抛 —— 必须落成 null。"""
    q = np.full((0, 10, 1), np.nan)
    h = bfr.build_headline_snapshot(q, None, k_main=5, n_factors=1)[0]
    assert h["n_days"] == 0
    for key in ("returns", "ir", "turnover", "fitness", "margin"):
        assert h[key] is None, f"{key} 应为 None，实得 {h[key]!r}"
        assert isinstance(h[key], type(None)), "不得是 NaN/Inf"
