"""Alpha Agent 因子回测链路单测：日度 IC 指标口径（含 Rank ICIR）。

alpha_agent 路由依赖 fastapi/DB，本地轻量环境 import 失败时整体跳过
（与 test_alpha_agent_quality_gate.py 同策略）；在 OSS 容器内
运行 `python -m pytest backend/tests/` 时全量生效。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - 环境相关
    from backend.services.engine.routers import alpha_agent as aa
except Exception as _exc:  # noqa: BLE001
    aa = None
    _IMPORT_ERR = _exc


pytestmark = pytest.mark.skipif(
    aa is None, reason="alpha_agent 依赖不可用（需容器环境）"
)


def _mining_order_frame(dates, instruments, col="f"):
    """挖掘侧层序的因子产出：index=[datetime, instrument]，值 = 行号。

    层序不是随便定的——RD-Agent 挖出来的因子代码里**自己断言**并要求
    ``set_index(['datetime', 'instrument'])``，所以回测拿到的就是这个顺序。
    """
    idx = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"]
    )
    return pd.DataFrame({col: np.arange(len(idx), dtype="float64")}, index=idx)


def test_factor_output_in_mining_order_aligns_with_qlib_returns() -> None:
    """回测对齐回归（2026-10-07）：层序相反曾让对齐结果**恰好 0 行**。

    因子产出是 (datetime, instrument)（挖掘侧层序），Qlib ``D.features`` 是
    (instrument, datetime)。旧实现 ``s.index.names = ["instrument", "datetime"]``
    按下标改名、不换层，名字与值就对不上了；而 ``Index.intersection`` 比对的是
    **值**（元组），于是 (日期, 标的) 去撞 (标的, 日期)，交集恒为 0，最终报
    「因子与价格对齐后数据不足 (共 0 行)」。
    """
    dates = pd.date_range("2024-01-01", periods=8)
    instruments = ["sz000001", "sz000002", "sz000003", "sz000004"]
    factor = _mining_order_frame(dates, instruments)

    # Qlib 的 close：层序与因子相反，且只有这几只票
    qlib_idx = pd.MultiIndex.from_product(
        [instruments, dates], names=["instrument", "datetime"]
    )
    close = pd.Series(np.linspace(10.0, 20.0, len(qlib_idx)), index=qlib_idx)

    f, r = aa._align_factor_returns(factor, close)

    assert len(f) > 0, "层序相反不该让对齐结果变成 0 行"
    assert len(f) == len(r)
    assert f.index.equals(r.index)
    assert f.index.names == ["datetime", "instrument"]
    # 对齐后的日期真的落在 datetime 层上（不是把标的当成了日期）
    assert set(f.index.get_level_values("datetime")) <= set(dates)
    # 对齐口径的 instrument 是大写（与挖掘侧一致：两侧统一转大写再取交集）
    assert set(f.index.get_level_values("instrument")) == {
        s.upper() for s in instruments
    }


def test_factor_values_stay_on_their_own_rows() -> None:
    """规整层序时值必须跟着自己那行走，不能被重排打乱。"""
    dates = pd.date_range("2024-01-01", periods=6)
    instruments = ["sz000001", "sz000002", "sz000003"]
    factor = _mining_order_frame(dates, instruments)

    s = aa._canonicalize_factor_series(factor)

    assert s.index.names == ["datetime", "instrument"]
    # product 展开顺序：第 n 行 = (dates[n // 3], instruments[n % 3])
    for n in (0, 1, 5, 9, 17):
        assert s.loc[(dates[n // 3], instruments[n % 3])] == float(n)


def test_level_order_is_decided_by_value_not_by_name() -> None:
    """名字可能已经被上游改错（level0 叫 instrument、装的却是日期）。

    所以层序必须按**值**判定：取一层出来看是不是 datetime，而不是信 names。
    """
    dates = pd.date_range("2024-01-01", periods=4)
    instruments = ["sz000001", "sz000002"]
    idx = pd.MultiIndex.from_product(
        [dates, instruments], names=["instrument", "datetime"]
    )
    factor = pd.DataFrame({"f": np.arange(len(idx), dtype="float64")}, index=idx)

    s = aa._canonicalize_factor_series(factor)

    assert s.index.names == ["datetime", "instrument"]
    assert isinstance(s.index.get_level_values("datetime")[0], pd.Timestamp)
    assert s.index.get_level_values("instrument")[0] == "sz000001"


def test_qlib_level_order_is_reordered_to_mining_order() -> None:
    """Qlib 原生层序进来也要归位到挖掘侧层序，值同样不能乱。"""
    instruments = ["sz000001", "sz000002"]
    dates = pd.date_range("2024-01-01", periods=4)
    idx = pd.MultiIndex.from_product(
        [instruments, dates], names=["instrument", "datetime"]
    )
    factor = pd.DataFrame({"f": np.arange(len(idx), dtype="float64")}, index=idx)

    s = aa._canonicalize_factor_series(factor)

    assert s.index.names == ["datetime", "instrument"]
    # Qlib 序里 (i0, d2) 是第 2 行
    assert s.loc[(dates[2], "sz000001")] == 2.0
    # (i1, d0) 是第 4 行
    assert s.loc[(dates[0], "sz000002")] == 4.0


def test_forward_return_does_not_bleed_across_instruments() -> None:
    """次日收益要按标的**组内**前移。

    旧写法 ``close.groupby(level=0).pct_change().shift(-1)`` 的 shift 落在
    groupby **之外**，是整表位移：上一只股票的末日会拿到下一只的首日收益。
    层序换成 (datetime, instrument) 后 groupby(level=0) 更是直接按**日期**分组。
    """
    dates = pd.date_range("2024-01-01", periods=3)
    idx = pd.MultiIndex.from_product(
        [dates, ["A", "B"]], names=["datetime", "instrument"]
    )
    close = pd.Series([10.0, 100.0, 11.0, 110.0, 12.0, 120.0], index=idx)

    r = aa._forward_return(close)

    assert r.loc[(dates[2], "A")] != r.loc[(dates[2], "A")]  # NaN：A 没有次日
    assert r.loc[(dates[0], "A")] == pytest.approx(0.1)
    assert r.loc[(dates[1], "A")] == pytest.approx(12.0 / 11.0 - 1.0)
    assert r.loc[(dates[0], "B")] == pytest.approx(0.1)
    assert r.loc[(dates[2], "B")] != r.loc[(dates[2], "B")]


def test_mining_input_keeps_enriched_columns_and_layer_order(tmp_path) -> None:
    """回测喂给因子的数据必须与挖掘**同列、同层序**。

    因子代码是照着挖掘时那份 39 列写的（``calculate_*`` 里直接取 ``$netflow_5``），
    回测若从 Qlib 二进制现拼 6 列量价，一读到富化列就 KeyError。层序同理：
    挖掘侧是 (datetime, instrument)，归位错了交集恒为 0。
    """
    dates = pd.date_range("2024-01-01", periods=5)
    instruments = ["sz000001", "sz000002", "sz000003"]
    idx = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"]
    )
    enriched = pd.DataFrame(
        {
            "$close": np.linspace(10.0, 20.0, len(idx)),
            "$volume": np.arange(len(idx), dtype="float64"),
            # 富化列：Qlib 二进制里根本没有，正是回退路径缺的东西
            "$netflow_5": np.arange(len(idx), dtype="float64") * 2.0,
        },
        index=idx,
    )
    src = tmp_path / "daily_pv_all.h5"
    enriched.to_hdf(src, key="data", mode="w")

    # 回测侧 Qlib df：层序相反，且只覆盖 2 只票 / 前 3 天
    qlib_idx = pd.MultiIndex.from_product(
        [["sz000001", "sz000002"], dates[:3]], names=["instrument", "datetime"]
    )
    qlib_df = pd.DataFrame({"$close": 1.0}, index=qlib_idx)

    dest = tmp_path / "daily_pv.h5"
    aa._write_mining_input(str(src), str(dest), qlib_df)  # noqa: SLF001

    out = pd.read_hdf(dest, key="data")
    assert "$netflow_5" in out.columns, "富化列不能在回测侧丢掉"
    assert out.index.names == ["datetime", "instrument"], "层序要照挖掘的来"
    assert set(out.index.get_level_values("instrument")) == {"sz000001", "sz000002"}
    # 窗口收窄到回测区间：第 4、5 天不该出现
    assert out.index.get_level_values("datetime").max() == dates[2]
    # 值没有被重排打乱
    got = out.loc[(dates[0], "sz000002"), "$netflow_5"]
    assert got == enriched.loc[(dates[0], "sz000002"), "$netflow_5"]


def test_case_difference_is_reconciled_like_mining() -> None:
    """因子产出大写、价格小写 → 必须**对齐成功**，与挖掘侧同款处置。

    这一条曾经被我判反过，所以把依据钉在这里：挖掘侧算 IC 前对因子与收益**两侧**
    都统一转大写（``scripts/alpha_agent/run_rd_agent.py``，注释原文「因子代码可能
    假设大写（SH600036），而 daily_pv.h5 用小写（sh600036），不统一会导致对齐交集
    为空、IC 无法计算」）。也就是说「因子把代码大写」是挖掘侧容许的写法，不是笔误。

    回测若不照做，同一个因子会在挖掘阶段算出 IC、却在回测阶段撞空——正是
    「回测要喂挖掘同源数据」要补的那条缝。
    """
    dates = pd.date_range("2024-01-01", periods=3)
    instruments = ["sz000001", "sz000002"]
    # 因子侧：挖掘产出的层序 (datetime, instrument)，且代码被因子自己大写
    idx_f = pd.MultiIndex.from_product(
        [dates, [s.upper() for s in instruments]], names=["datetime", "instrument"]
    )
    factor = pd.Series(np.arange(len(idx_f), dtype="float64"), index=idx_f)
    # 价格侧：Qlib 的相反层序 + 小写代码
    idx_c = pd.MultiIndex.from_product(
        [instruments, dates], names=["instrument", "datetime"]
    )
    close = pd.Series(np.linspace(10.0, 20.0, len(idx_c)), index=idx_c)

    f, r = aa._align_factor_returns(factor, close)  # noqa: SLF001

    assert len(f) == len(factor), "大小写与层序都不该让对齐缩水"
    assert f.index.equals(r.index)
    assert set(f.index.get_level_values("instrument")) == {"SZ000001", "SZ000002"}
    # 值必须跟着自己那行走：product 展开第 n 行 = (dates[n // 2], instruments[n % 2])
    for n in (0, 1, 4, 5):
        assert f.loc[(dates[n // 2], instruments[n % 2].upper())] == float(n)


def test_diagnostics_name_samples_when_alignment_is_genuinely_empty() -> None:
    """真的对不上时（标的池毫无交集），报错要能指出两边样本不同。

    大小写现在会被对齐掉，但「层序/口径之外的第三种错位」以后还会有，
    这个指纹是留给那时的——只报「数据不足」不足以定位。
    """
    dates = pd.date_range("2024-01-01", periods=3)
    idx_f = pd.MultiIndex.from_product(
        [dates, ["sz000001", "sz000002"]], names=["datetime", "instrument"]
    )
    factor = pd.Series(np.arange(len(idx_f), dtype="float64"), index=idx_f)
    idx_c = pd.MultiIndex.from_product(
        [["sz300001", "sz300002"], dates], names=["instrument", "datetime"]
    )
    close = pd.Series(10.0, index=idx_c)

    f, r = aa._align_factor_returns(factor, close)  # noqa: SLF001
    assert len(f) == 0

    msg = aa._alignment_failure_message(factor, close, len(f))  # noqa: SLF001
    assert "SZ000001" in msg and "SZ300001" in msg
    assert "共 6 行" in msg  # 3 天 × 2 只
    assert "datetime" in msg and "instrument" in msg


def _small_pv() -> pd.DataFrame:
    """最小可用的 daily_pv：层序照挖掘侧，带 $close。"""
    dates = pd.date_range("2024-01-01", periods=5)
    instruments = ["sz000001", "sz000002"]
    idx = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"]
    )
    return pd.DataFrame({"$close": np.linspace(10.0, 20.0, len(idx))}, index=idx)


# 自执行式：入口是 main()，靠 __main__ 守卫触发，自己读 daily_pv.h5、写 result.h5。
# 这是挖掘侧容许的第一种（也是**优先**的）入口样式。
SELF_EXECUTING_FACTOR = """
import pandas as pd


def calculate_probe(data: pd.DataFrame) -> pd.DataFrame:
    raise AssertionError("自执行因子不该被零参调用：本因子的入口是 main()")


def main():
    df = pd.read_hdf("daily_pv.h5")
    (df["$close"] * 2.0).to_frame("probe").to_hdf("result.h5", key="data", mode="w")


if __name__ == "__main__":
    main()
"""

# 零参函数式：RD-Agent 挖出来的绝大多数因子长这样，必须不受影响。
ZERO_ARG_FACTOR = """
import pandas as pd


def calculate_probe():
    df = pd.read_hdf("daily_pv.h5")
    return (df["$close"] + 1.0).to_frame("probe")
"""


def test_self_executing_factor_with_main_guard_runs() -> None:
    """自执行式因子（main() + ``__main__`` 守卫）在回测侧也必须能跑。

    挖掘侧对因子有**两种**入口样式，且**优先自执行**——``run_rd_agent.py`` 原注释：
    「若因子代码未自执行（无 ``__main__`` 守卫）或未产出 result.h5，则显式调用
    ``calculate_*()``」。回测侧只实现了第二种，而且是**无条件**零参调用，于是
    ``def calculate_X(data: pd.DataFrame)`` 这类自执行因子一律撞
    「missing 1 required positional argument: 'data'」（线上 6 个因子卡在这，
    2026-10-07）。

    机理：子进程用 ``exec(code, {})``，命名空间里没有 ``__name__``，守卫取到的是
    ``'builtins'`` 而不是 ``'__main__'``，``main()`` 根本不触发——所以不能只改调用
    顺序，必须把 ``__name__`` 显式给成 ``'__main__'``。
    """
    df = _small_pv()

    s = asyncio.run(
        aa._run_functional_factor_subprocess(
            "t_selfexec_0000", SELF_EXECUTING_FACTOR, df
        )
    )

    assert s is not None, "自执行因子没跑出结果"
    assert s.index.names == ["datetime", "instrument"]
    assert len(s) == len(df)
    # main() 里是 $close * 2；若被零参调用会抛 AssertionError，不会走到这里
    assert np.allclose(s.to_numpy(dtype="float64"), df["$close"].to_numpy() * 2.0)


def test_zero_arg_calculate_factor_still_runs() -> None:
    """零参 ``calculate_*`` 式因子必须原样能跑——新契约是**增加**一种入口，不是替换。"""
    df = _small_pv()

    s = asyncio.run(
        aa._run_functional_factor_subprocess("t_zeroarg_0000", ZERO_ARG_FACTOR, df)
    )

    assert s is not None
    assert s.index.names == ["datetime", "instrument"]
    assert np.allclose(s.to_numpy(dtype="float64"), df["$close"].to_numpy() + 1.0)


# 位置型取 instrument：挖掘出来的因子普遍按**位置**取 level1 当 instrument
# （老契约里它就是 instrument 层），pv_sync_10 的
# ``get_level_values(1).str.upper()`` 是线上实测样张。
POSITIONAL_INSTRUMENT_FACTOR = """
import pandas as pd


def calculate_probe():
    df = pd.read_hdf("daily_pv.h5")
    out = df["$close"].to_frame("probe")
    out.index = pd.MultiIndex.from_arrays(
        [out.index.get_level_values(0), out.index.get_level_values(1).str.upper()],
        names=out.index.names,
    )
    return out
"""


def test_fallback_input_is_written_in_mining_layer_order(tmp_path) -> None:
    """非 CN 市场走 fallback：``D.features`` 的 (instrument, datetime) 帧在写盘前
    必须归位成挖掘契约 (datetime, instrument)。

    挖掘因子按位置取 level1 当 instrument；层序不归位时拿到的是 datetime64，
    ``.str`` 访问器直接在 datetime64 上炸——2026-10-09 美股首跑实测：
    ``AttributeError: Can only use .str accessor with string values``（pv_sync_10）。
    """
    dates = pd.date_range("2024-01-01", periods=5)
    instruments = ["us_aapl", "us_msft"]
    idx = pd.MultiIndex.from_product(
        [instruments, dates], names=["instrument", "datetime"]
    )
    qlib_df = pd.DataFrame({"$close": np.linspace(10.0, 20.0, len(idx))}, index=idx)

    s = asyncio.run(
        aa._run_functional_factor_subprocess(  # noqa: SLF001
            "t_qliblayer_0000", POSITIONAL_INSTRUMENT_FACTOR, qlib_df
        )
    )

    assert s is not None, "qlib 层序的帧走 fallback 必须能跑出结果"
    assert len(s) == len(qlib_df)
    assert set(s.index.get_level_values("instrument")) == {"US_AAPL", "US_MSFT"}


def test_mining_source_only_for_a_share() -> None:
    """富化缓存目前只有 A 股（QuantDB parquet）；其他市场必须老实返回 None 走回退。

    真返回一个不存在的路径或港股不存在的文件，回测会在准备阶段炸掉。
    """
    assert aa._resolve_mining_source_h5("hong_kong") is None  # noqa: SLF001
    assert aa._resolve_mining_source_h5("us_stock") is None  # noqa: SLF001
    assert aa._resolve_mining_source_h5("crypto") is None  # noqa: SLF001


def test_vectorized_ic_returns_rank_icir() -> None:
    """回测要落库 ICIR / Rank ICIR（口径与挖掘阶段一致：同除日度 IC 标准差）。"""
    rng = np.random.default_rng(0)
    n_stocks, n_days = 40, 60
    index = pd.MultiIndex.from_product(
        [range(n_stocks), pd.date_range("2024-01-01", periods=n_days)],
        names=["instrument", "datetime"],
    )
    f = pd.Series(rng.normal(size=len(index)), index=index)
    r = pd.Series(rng.normal(size=len(index)), index=index)

    ic_mean, rank_ic_median, icir, rank_icir, n_obs = aa._vectorized_daily_spearman_ic(  # noqa: SLF001
        f, r
    )

    assert n_obs > 0
    assert np.isfinite(icir) and np.isfinite(rank_icir)
    # icir 与 rank_icir 同分母（日度 IC 标准差），符号分别与 ic_mean / rank_ic_median 一致
    assert (icir > 0) == (ic_mean > 0)
    assert (rank_icir > 0) == (rank_ic_median > 0)


# ——— 评估器插件接线（P0）：EVAL_ 协议解析 + Qlib 路径 helper ———

_GOLDEN = json.loads(
    (
        Path(__file__).resolve().parent / "fixtures" / "miningMetricsGolden.json"
    ).read_text(encoding="utf-8")
)


def test_parse_eval_metrics_reads_protocol_lines() -> None:
    """``EVAL_<key>=<float>`` 协议：只认行首、跳过非法值、过滤 NaN。"""
    out = "\n".join(
        [
            "IC=0.0123",
            "EVAL_rre=0.9374384380505406",
            "EVAL_ann_turnover=63.0",
            "EVAL_icir=-1.5",  # 负值合法
            "EVAL_broken=abc",  # 非法 → 跳过
            "EVAL_",  # 无键 → 跳过
            "EVAL_nan=nan",  # NaN → 过滤
            "some log line EVAL_rre=9.9",  # 非行首 → 不算
            "MiningEvalError: boom",  # 错误行不进指标命名空间
        ]
    )

    metrics = aa._parse_eval_metrics(out)  # noqa: SLF001

    assert metrics == {
        "rre": 0.9374384380505406,
        "ann_turnover": 63.0,
        "icir": -1.5,
    }


def test_parse_eval_metrics_empty_output_is_empty_dict() -> None:
    assert aa._parse_eval_metrics("") == {}  # noqa: SLF001
    assert aa._parse_eval_metrics("IC=0.1\nSHARPE=1.2\n") == {}  # noqa: SLF001


def _golden_panel(case: dict) -> tuple[pd.Series, pd.Series]:
    """金样换手用例 → (f_clean, r_clean) 一对 MultiIndex(datetime, instrument)。"""
    dates = pd.date_range("2024-01-01", periods=len(case["factors"]))
    idx = pd.MultiIndex.from_product(
        [dates, case["symbols"]], names=["datetime", "instrument"]
    )
    f = pd.Series(np.asarray(case["factors"], dtype="float64").reshape(-1), index=idx)
    r = pd.Series(np.asarray(case["returns"], dtype="float64").reshape(-1), index=idx)
    return f, r


def test_run_mining_evaluators_matches_golden_through_full_chain() -> None:
    """Qlib 路径 helper → evaluate_paired → 插件链，端到端对金样钉值。

    这条把「两条回测路径接同一评估器」的接线也一起锁住：helper 的输入约定
    （MultiIndex 名为 datetime/instrument 的 f_clean/r_clean）如果漂了，
    这里会先在 get_level_values 上炸掉。
    """
    case = _GOLDEN["turnover_cases"][0]  # three_day_rotation
    f, r = _golden_panel(case)

    metrics = aa._run_mining_evaluators(  # noqa: SLF001
        f, r, market="a_share", universe="csi300", factor_id="t_eval_0000"
    )

    for key, expected in case["expected"].items():
        assert metrics[key] == pytest.approx(expected, rel=1e-6), key
    assert 0.0 < metrics["rre"] <= 1.0
    # 与毛指标同名的键不该被评估器顶掉（gross 路径继续自己算）
    assert "annual_return" not in metrics and "sharpe_ratio" not in metrics


def test_run_mining_evaluators_degrades_to_empty_on_bad_input() -> None:
    """评估器是可加层：输入不合约定（无 datetime/instrument 层）只降级为空 dict。"""
    bad = pd.Series([1.0, 2.0, 3.0])  # RangeIndex

    assert (
        aa._run_mining_evaluators(  # noqa: SLF001
            bad, bad, market="a_share", universe="csi300", factor_id="t_eval_bad"
        )
        == {}
    )
