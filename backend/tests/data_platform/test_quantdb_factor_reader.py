from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorError,
    QuantDBFactorReader,
    split_features_by_availability,
)


def _write_factor_partition(root, source: str, frame: pd.DataFrame, dt: str) -> None:
    target = root / "6_ml_datasets" / source / f"dt={dt}"
    target.mkdir(parents=True)
    frame.to_parquet(target / "data.parquet", index=False)


def _write_daily_backward_partition(root, frame: pd.DataFrame, dt: str) -> None:
    target = root / "1_kline_data" / "daily_backward" / f"dt={dt}"
    target.mkdir(parents=True)
    frame.to_parquet(target / "data.parquet", index=False)


def _frame(day: str, close: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ"],
            "date": [day, day],
            "open": [close - 1, close - 1],
            "high": [close + 1, close + 1],
            "low": [close - 2, close - 2],
            "close": [close, close],
            "volume": [100, 100],
            "amount": [1000, 1000],
            "l1_alpha": [0.1, 0.2],
        }
    )


def test_reads_single_source_with_logical_field_alias(tmp_path):
    _write_factor_partition(
        tmp_path, "l1_l2_factors", _frame("2024-01-02", 10), "20240102"
    )
    reader = QuantDBFactorReader(tmp_path)

    status = reader.assert_ready("l1_l2_factors", start="2024-01-02", end="2024-01-02")
    assert status.ready
    assert status.min_date == "2024-01-02"
    data = reader.read_day(
        "l1_l2_factors",
        features=["alpha"],
        feature_sources={"alpha": "l1_alpha"},
        trade_date="2024-01-02",
    )

    assert set(data["symbol"]) == {"SH600001", "SZ000001"}
    # 读端在 DuckDB 侧 CAST AS FLOAT（float32），0.1/0.2 非精确可表示 → 近似比较
    assert data["alpha"].tolist() == pytest.approx([0.1, 0.2], rel=1e-6)


def test_rejects_source_without_common_ohlcv(tmp_path):
    _write_factor_partition(
        tmp_path,
        "l2_factors",
        pd.DataFrame(
            {"symbol": ["600001.SH"], "date": ["2024-01-02"], "l2_alpha": [1.0]}
        ),
        "20240102",
    )
    with pytest.raises(QuantDBFactorError, match="not ready"):
        QuantDBFactorReader(tmp_path).assert_ready("l2_factors")


def test_forward_labels_use_future_close_without_snapshot(tmp_path):
    for day, close in [("2024-01-02", 10), ("2024-01-03", 11), ("2024-01-04", 12)]:
        _write_factor_partition(
            tmp_path, "l1_factors", _frame(day, close), day.replace("-", "")
        )
    reader = QuantDBFactorReader(tmp_path)
    frame = reader.read_range(
        "l1_factors", features=["l1_alpha"], start="2024-01-02", end="2024-01-04"
    )
    labels = reader.forward_labels(frame, horizon=1, signal_lag_days=0)
    first = labels[
        (labels["symbol"] == "SH600001")
        & (labels["trade_date"] == pd.Timestamp("2024-01-02"))
    ]
    assert first.iloc[0]["label"] == pytest.approx(0.1)


def test_read_range_backfills_null_factor_ohlcv_from_daily_backward(tmp_path):
    factor_frame = _frame("2024-01-02", 10)
    factor_frame[["open", "high", "low", "close", "volume", "amount"]] = None
    _write_factor_partition(tmp_path, "l1_factors", factor_frame, "20240102")
    _write_daily_backward_partition(
        tmp_path,
        _frame("2024-01-02", 12).drop(columns=["date", "l1_alpha"]),
        "20240102",
    )

    frame = QuantDBFactorReader(tmp_path).read_day(
        "l1_factors", features=["l1_alpha"], trade_date="2024-01-02"
    )

    assert frame["close"].tolist() == [12, 12]
    assert frame["volume"].tolist() == [100, 100]


def test_include_ohlcv_false_emits_no_ohlcv_columns(tmp_path):
    """include_ohlcv=False 时输出不得带行情列。

    它们既没进 SELECT，也就不可能被写进预分配的 float32 块 —— 名单若只看
    `ohlcv_join`（补给表存在即真）而不管 include_ohlcv，就会把 6 个从未写入的列
    交出去，值来自 `np.empty` 的未初始化内存。
    """
    _write_factor_partition(tmp_path, "l1_factors", _frame("2024-01-02", 10), "20240102")
    # 补给表在 → daily_relation 为真 → ohlcv_join 为真（旧实现正是在这里误判名单）
    _write_daily_backward_partition(
        tmp_path,
        _frame("2024-01-02", 12).drop(columns=["date", "l1_alpha"]),
        "20240102",
    )

    frame = QuantDBFactorReader(tmp_path).read_range(
        "l1_factors",
        features=["l1_alpha"],
        start="2024-01-02",
        end="2024-01-02",
        include_ohlcv=False,
    )

    assert set(frame.columns) == {"symbol", "trade_date", "l1_alpha"}


def _hk_l1_frame(day: str, close: float) -> pd.DataFrame:
    """HK l1 风格：dt int 列（无 date 列）、suffix symbol。"""
    dt = int(day.replace("-", ""))
    return pd.DataFrame(
        {
            "symbol": ["0001.HK", "0002.HK"],
            "dt": [dt, dt],
            "open": [close - 1, close - 1],
            "high": [close + 1, close + 1],
            "low": [close - 2, close - 2],
            "close": [close, close],
            "volume": [100, 100],
            "amount": [1000, 1000],
            "mom_ret_1d": [0.01, 0.02],
        }
    )


def _hk_signal_frame(day: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["0001.HK", "0002.HK"],
            "date": [pd.Timestamp(day), pd.Timestamp(day)],
            "ca_n_pis": [50, 60],
            "ca_hhi_disc": [0.3, 0.4],
        }
    )


def test_market_source_mapping_and_defaults():
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        default_source_for,
        normalize_market,
        sources_for_market,
    )

    assert normalize_market("hk") == "HK"
    assert normalize_market("A_SHARE") == "CN"
    assert normalize_market(None) == "CN"
    assert normalize_market("custom") == "CUSTOM"
    assert sources_for_market("HK") == ["l1_factors", "ccass_factors", "south_factors"]
    assert sources_for_market("US") == ["l1_factors"]
    assert sources_for_market("CUSTOM") == ["l1_factors"]
    assert default_source_for("HK") == "l1_factors"
    assert default_source_for("CN") == "l1_factors"
    assert default_source_for("CUSTOM") == "l1_factors"


def test_hk_l1_dt_date_alias_ready_and_read(tmp_path):
    _write_factor_partition(
        tmp_path, "l1_factors", _hk_l1_frame("2024-01-02", 10), "20240102"
    )
    reader = QuantDBFactorReader(tmp_path)
    status = reader.assert_ready("l1_factors", start="2024-01-02", end="2024-01-02")
    assert status.ready

    data = reader.read_day("l1_factors", features=["mom_ret_1d"], trade_date="2024-01-02")
    assert set(data["symbol"]) == {"0001.HK", "0002.HK"}
    assert data["close"].tolist() == [10, 10]


def test_secondary_source_ohlcv_donor_from_l1(tmp_path):
    _write_factor_partition(tmp_path, "l1_factors", _hk_l1_frame("2024-01-02", 10), "20240102")
    _write_factor_partition(tmp_path, "ccass_factors", _hk_signal_frame("2024-01-02"), "20240102")

    reader = QuantDBFactorReader(tmp_path)
    status = reader.assert_ready("ccass_factors", start="2024-01-02", end="2024-01-02")
    assert status.ready, status.missing_required

    data = reader.read_day(
        "ccass_factors", features=["ca_n_pis"], trade_date="2024-01-02"
    )
    assert len(data) == 2
    assert data["close"].tolist() == [10, 10]  # OHLCV 由 l1_factors 补给
    assert data["ca_n_pis"].tolist() == [50, 60]


def test_secondary_source_without_donor_is_not_ready(tmp_path):
    _write_factor_partition(tmp_path, "ccass_factors", _hk_signal_frame("2024-01-02"), "20240102")
    status = QuantDBFactorReader(tmp_path).describe("ccass_factors")
    assert not status.ready
    assert set(status.missing_required) == {
        "open", "high", "low", "close", "volume", "amount",
    }


# ── 自定义市场：仅扫描因子，不强制 OHLCV ──────────────────────────────────────


def test_custom_market_scan_only_without_ohlcv(tmp_path):
    _write_factor_partition(
        tmp_path,
        "l1_factors",
        pd.DataFrame(
            {"symbol": ["MY001", "MY002"], "date": ["2024-01-02", "2024-01-02"], "my_alpha": [1.0, 2.0]}
        ),
        "20240102",
    )
    # 同一份无 OHLCV 数据：CN 口径拒绝，CUSTOM 口径仅扫描即 ready
    with pytest.raises(QuantDBFactorError, match="not ready"):
        QuantDBFactorReader(tmp_path).assert_ready("l1_factors")
    status = QuantDBFactorReader(tmp_path, market="CUSTOM").describe("l1_factors")
    assert status.ready
    assert status.missing_required == []
    assert "my_alpha" in status.columns


# ── 跨库组合读：库=包，读时拼接（factor_sources 值可限定 "库:列"）─────────────


def _alpha_frame(day: str, a101: float, a102: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ"],
            "date": [day, day],
            "a101_x": [a101, a101 + 1],
            "a102_y": [a102, a102 + 1],
        }
    )


def test_split_qualified_source_parses_library_and_column():
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        split_qualified_source,
    )

    assert split_qualified_source("alpha_library:a101_x") == ("alpha_library", "a101_x")
    assert split_qualified_source("plain_col") == (None, "plain_col")
    assert split_qualified_source(" lib : col ") == ("lib", "col")


def test_secondary_library_feature_joined_by_symbol_and_date(tmp_path):
    for day, close in [("2024-01-02", 10), ("2024-01-03", 11)]:
        _write_factor_partition(
            tmp_path, "l1_factors", _frame(day, close), day.replace("-", "")
        )
    _write_factor_partition(tmp_path, "alpha_library", _alpha_frame("2024-01-02", 1.5, 9.0), "20240102")
    _write_factor_partition(tmp_path, "alpha_library", _alpha_frame("2024-01-03", 3.5, 8.0), "20240103")

    frame = QuantDBFactorReader(tmp_path).read_range(
        "l1_factors",
        features=["l1_alpha", "alpha_library:a101_x"],
        start="2024-01-02",
        end="2024-01-03",
    )

    assert len(frame) == 4  # 行集仍由锚库决定
    ordered = frame.sort_values(["trade_date", "symbol"])
    assert ordered["a101_x"].tolist() == [1.5, 2.5, 3.5, 4.5]
    assert ordered["l1_alpha"].tolist() == pytest.approx([0.1, 0.2, 0.1, 0.2], rel=1e-6)
    assert ordered["close"].tolist() == [10, 10, 11, 11]  # OHLCV 仍来自锚库


def test_duplicate_alias_from_two_libraries_is_rejected(tmp_path):
    """同一输出列名（别名）来自两个不同来源时必须报错，不得悄悄留一个。

    锚库与副库各有 `a101_x`：裸名取锚库、`alpha_library:a101_x` 取副库，两者
    输出列名都是 `a101_x`。若只留一个，用户点名的那个特征会在模型里静默消失。
    """
    anchor_frame = _frame("2024-01-02", 10)
    anchor_frame["a101_x"] = [7.0, 7.5]  # 锚库确有同名列，冲突才是**唯一**失败原因
    _write_factor_partition(tmp_path, "l1_l2_factors", anchor_frame, "20240102")
    _write_factor_partition(
        tmp_path, "alpha_library", _alpha_frame("2024-01-02", 1.5, 9.0), "20240102"
    )

    with pytest.raises(QuantDBFactorError, match="Duplicate factor alias"):
        QuantDBFactorReader(tmp_path).read_range(
            "l1_l2_factors",
            features=["a101_x", "alpha_library:a101_x"],
            start="2024-01-02",
            end="2024-01-02",
        )


def test_qualified_alias_cannot_shadow_a_reserved_column(tmp_path):
    """限定写法的 alias 也必须过保留名闸门，否则静默取到**锚库行情**的值。

    副库真有一列叫 close 时（jq110/alpha360/cand_factors/tdxgs 都带 close），SELECT
    会产出两个 close：DuckDB 把第二个改名 close_1，而回填按 `chunk["close"]` 取值
    —— 拿到的是锚库 OHLCV 的 close，请求的特征被换成另一个数列，不报错、无日志。
    """
    _write_factor_partition(tmp_path, "l1_factors", _frame("2024-01-02", 10), "20240102")
    secondary = _alpha_frame("2024-01-02", 1.5, 9.0)
    secondary["close"] = [77.0, 78.0]  # 与锚库行情同名、不同值：撞了才看得出来
    _write_factor_partition(tmp_path, "alpha_library", secondary, "20240102")

    reader = QuantDBFactorReader(tmp_path)
    with pytest.raises(QuantDBFactorError, match="overwrite key or OHLCV"):
        reader.read_day("l1_factors", features=["alpha_library:close"], trade_date="2024-01-02")
    # 形式② 的裸键同样不得借道映射挤进保留列名
    with pytest.raises(QuantDBFactorError, match="overwrite key or OHLCV"):
        reader.read_day("l1_factors", features=["close"], trade_date="2024-01-02")


def test_secondary_library_partial_coverage_leaves_nan(tmp_path):
    for day, close in [("2024-01-02", 10), ("2024-01-03", 11)]:
        _write_factor_partition(
            tmp_path, "l1_factors", _frame(day, close), day.replace("-", "")
        )
    # 副库只有第一天
    _write_factor_partition(tmp_path, "alpha_library", _alpha_frame("2024-01-02", 1.5, 9.0), "20240102")

    frame = QuantDBFactorReader(tmp_path).read_range(
        "l1_factors",
        features=["alpha_library:a101_x"],
        start="2024-01-02",
        end="2024-01-03",
    )

    # 行数先断：日期谓词若从 ON 挪进 WHERE，LEFT JOIN 退化成 INNER，day2 的**整行**
    # 都会被丢掉——那时 day2 是空集，而空列的 `.isna().all()` 恒为 True，下面那条
    # 覆盖率断言照样绿（2026-09-23 审查实测）。丢行是这条契约唯一会坏的方式。
    assert len(frame) == 4
    day1 = frame[frame["trade_date"] == pd.Timestamp("2024-01-02")]
    day2 = frame[frame["trade_date"] == pd.Timestamp("2024-01-03")]
    assert len(day1) == 2 and len(day2) == 2
    assert day1["a101_x"].notna().all()
    assert day2["a101_x"].isna().all()


def test_secondary_library_does_not_expand_row_universe(tmp_path):
    _write_factor_partition(tmp_path, "l1_factors", _frame("2024-01-02", 10), "20240102")
    # 副库多出一只锚库没有的股票：不得进入行集
    _write_factor_partition(
        tmp_path,
        "alpha_library",
        pd.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ", "300001.SZ"],
                "date": ["2024-01-02"] * 3,
                "a101_x": [1.0, 2.0, 3.0],
            }
        ),
        "20240102",
    )

    frame = QuantDBFactorReader(tmp_path).read_day(
        "l1_factors",
        features=["alpha_library:a101_x"],
        trade_date="2024-01-02",
    )

    assert set(frame["symbol"]) == {"SH600001", "SZ000001"}
    assert frame["a101_x"].tolist() == [1.0, 2.0]


def test_secondary_library_missing_column_reports_qualified_name(tmp_path):
    _write_factor_partition(tmp_path, "l1_factors", _frame("2024-01-02", 10), "20240102")
    _write_factor_partition(tmp_path, "alpha_library", _alpha_frame("2024-01-02", 1.5, 9.0), "20240102")

    with pytest.raises(QuantDBFactorError, match="alpha_library:missing_col"):
        QuantDBFactorReader(tmp_path).read_day(
            "l1_factors",
            features=["alpha_library:missing_col"],
            trade_date="2024-01-02",
        )


def test_unknown_secondary_library_raises(tmp_path):
    _write_factor_partition(tmp_path, "l1_factors", _frame("2024-01-02", 10), "20240102")

    with pytest.raises(QuantDBFactorError):
        QuantDBFactorReader(tmp_path).read_day(
            "l1_factors",
            features=["nope_lib:col"],
            trade_date="2024-01-02",
        )


def test_label_libraries_blocked_as_secondary_source(tmp_path):
    _write_factor_partition(tmp_path, "l1_factors", _frame("2024-01-02", 10), "20240102")
    _write_factor_partition(
        tmp_path,
        "alpha_library_labels",
        pd.DataFrame(
            {"symbol": ["600001.SH"], "date": ["2024-01-02"], "fwd_ret_5": [0.01]}
        ),
        "20240102",
    )
    _write_factor_partition(
        tmp_path,
        "features_daily",
        pd.DataFrame(
            {"symbol": ["600001.SH"], "date": ["2024-01-02"], "return_1d": [0.02]}
        ),
        "20240102",
    )

    reader = QuantDBFactorReader(tmp_path)
    for lib in ("alpha_library_labels", "features_daily"):
        with pytest.raises(QuantDBFactorError, match="excluded"):
            reader.read_day(
                "l1_factors",
                features=[f"{lib}:fwd_ret_5" if lib == "alpha_library_labels" else f"{lib}:return_1d"],
                trade_date="2024-01-02",
            )


def test_previously_excluded_feature_library_allowed_as_secondary(tmp_path):
    """factor_defs 曾因"清单库"性质排除在训练直读外——现只拦发现面，训练面放行。"""
    _write_factor_partition(tmp_path, "l1_factors", _frame("2024-01-02", 10), "20240102")
    _write_factor_partition(
        tmp_path,
        "factor_defs",
        pd.DataFrame(
            {"symbol": ["600001.SH", "000001.SZ"], "date": ["2024-01-02"] * 2, "def_x": [7.0, 8.0]}
        ),
        "20240102",
    )

    frame = QuantDBFactorReader(tmp_path).read_day(
        "l1_factors", features=["factor_defs:def_x"], trade_date="2024-01-02"
    )
    assert frame["def_x"].tolist() == [7.0, 8.0]


class _FakeReader:
    """只实现 describe 的假读端：按库返回列集，记录被问过的库。"""

    def __init__(self, libs: dict[str, list[str]]) -> None:
        self._libs = libs
        self.described: list[str] = []

    def describe(self, lib: str):
        self.described.append(lib)
        if lib not in self._libs:
            raise QuantDBFactorError(f"dataset not found: {lib}")
        return SimpleNamespace(columns=list(self._libs[lib]))


def test_split_features_whole_and_qualified_libs():
    """整表三种写法：锚库裸列名 / 限定库:列 / 映射值限定库，逐库判定、缺列指名。"""
    reader = _FakeReader({"anchor": ["a1", "a2"], "sec": ["s1"]})
    valid, missing = split_features_by_availability(
        reader,
        ["a1", "s1key", "s2key", "plain_key"],
        {"s1key": "sec:s1", "s2key": "other:s2", "plain_key": "a2"},
        anchor="anchor",
    )
    assert valid == ["a1", "s1key", "plain_key"]
    assert missing == ["s2key"], "未知副库按缺列处理，不得静默放行"


def test_split_features_qualified_key_wins_over_mapping():
    """特征键自带库前缀时以键为准：映射表不该能把它改指到别的库。"""
    reader = _FakeReader({"anchor": ["a1"], "sec": ["s1"]})
    valid, missing = split_features_by_availability(
        reader,
        ["sec:s1", "sec:zz"],
        {"sec:s1": "anchor:a1", "sec:zz": "anchor:a1"},
        anchor="anchor",
    )
    assert valid == ["sec:s1"]
    assert missing == ["sec:zz"], "键说 sec、表说 anchor，应按键查 sec 并在 sec 里判缺"


def test_split_features_describes_each_library_once():
    """同库多列只 describe 一次：describe 是全量扫描（含 min/max），逐特征问会拖垮预检。"""
    reader = _FakeReader({"anchor": ["a1", "a2", "a3"], "sec": ["s1", "s2"]})
    split_features_by_availability(
        reader,
        ["a1", "a2", "a3", "x1", "x2"],
        {"x1": "sec:s1", "x2": "sec:s2"},
        anchor="anchor",
    )
    assert reader.described == ["anchor", "sec"], "每库各 describe 一次，且不重复"



def test_two_secondary_libraries_each_keep_their_own_coverage(tmp_path):
    """两个副库各有各的覆盖日：各自按自己的 dt 取到值，不得互相串范围。

    两个副库的 `dt BETWEEN ? AND ?` 占位符必须按 SQL 文本顺序（库名排序：
    alpha_library < jq110）绑定。绑错**不会报错**：某一库的区间会落到另一库身上，
    表现为整列静默变 NaN。这里把两库的覆盖日错开，绑错必然露馅。
    """
    for day, close in [("2024-01-02", 10), ("2024-01-03", 11)]:
        _write_factor_partition(
            tmp_path, "l1_factors", _frame(day, close), day.replace("-", "")
        )
    _write_factor_partition(
        tmp_path, "alpha_library", _alpha_frame("2024-01-02", 1.5, 9.0), "20240102"
    )
    _write_factor_partition(
        tmp_path,
        "jq110",
        pd.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ"],
                "date": ["2024-01-03", "2024-01-03"],
                "jq_x": [5.0, 6.0],
            }
        ),
        "20240103",
    )

    frame = QuantDBFactorReader(tmp_path).read_range(
        "l1_factors",
        features=["alpha_library:a101_x", "jq110:jq_x"],
        start="2024-01-02",
        end="2024-01-03",
    )

    assert len(frame) == 4  # 行集仍由锚库决定
    ordered = frame.sort_values(["trade_date", "symbol"])
    assert ordered["a101_x"].tolist()[:2] == pytest.approx([1.5, 2.5], rel=1e-6)
    assert ordered["a101_x"].isna().tolist()[2:] == [True, True]
    assert ordered["jq_x"].isna().tolist()[:2] == [True, True]
    assert ordered["jq_x"].tolist()[2:] == pytest.approx([5.0, 6.0], rel=1e-6)
