"""市场真实性验收的回归测试（TDD：先于实现编写）。

这三类缺陷的共同后果是「离线好看、实盘翻车」，且都不会自己报错：

1. **年代指示器** —— schema 断点让某列在老分区全空、新分区全有，
   树模型第一个学的就是它。实测 `features_daily` 30 列 NULL 率 99.744%，
   有值的 27,806 行恰好等于 20260914 起的 5 个交易日。
2. **缺失率越界** —— 高缺失列未剔除就进模型。
3. **回测窗口过短仍报年化** —— `backtest_l2_top20.py` 的窗口只有 12 个交易日，
   却按 `(1+r)^(1/years)-1` 年化。

判据一律用**「是不是构成时间分隔」**，不用「像不像异常值」——后者会误杀。
"""

from __future__ import annotations

import importlib.util
from dataclasses import FrozenInstanceError
from pathlib import Path

import pandas as pd
import pytest

from backend.shared.market_fidelity import (
    EraIndicator,
    FidelityError,
    FidelityReport,
    Finding,
    availability_island,
    check_backtest_window,
    era_switch,
    scan_era_indicators,
    scan_hardcoded_limit_thresholds,
    scan_missing_rates,
)

DATES = ["20260101", "20260102", "20260103", "20260104", "20260105"]

#: 门禁 CLI。按路径加载（`backend/scripts/audit/` 不是包），
#: 与 test_factor_quality.py 同一模式。
CLI_PY = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "audit"
    / "check_market_fidelity.py"
)


def _panel(**cols_by_date):
    """按日期展开成 (dt, symbol) 长表。cols_by_date: 列名 -> {日期: 值}。"""
    rows = []
    for d in DATES:
        for sym in ("A", "B"):
            row = {"dt": d, "symbol": sym}
            for col, by_date in cols_by_date.items():
                row[col] = by_date.get(d)
            rows.append(row)
    return pd.DataFrame(rows)


# ─────────────────────── 年代指示器 ───────────────────────


def test_era_indicator_detects_column_that_starts_halfway():
    # Arrange: f_new 从 20260104 起才有值
    df = _panel(
        f_old=dict.fromkeys(DATES, 1.0),
        f_new={"20260104": 1.0, "20260105": 2.0},
    )

    # Act
    found = scan_era_indicators(df, date_col="dt", feature_cols=["f_old", "f_new"])

    # Assert
    assert [e.column for e in found] == ["f_new"]
    assert found[0].switch_date == "20260104"
    assert found[0].missing_side == "before"
    assert found[0].n_dates == len(DATES)


def test_era_indicator_detects_column_that_ends_halfway():
    """反向同样成立：某列在断点后被删掉（features_daily 的 Symbol_val 就是这种）。"""
    # Arrange
    df = _panel(f_gone={"20260101": 1.0, "20260102": 2.0})

    # Act
    found = scan_era_indicators(df, date_col="dt", feature_cols=["f_gone"])

    # Assert
    assert [e.column for e in found] == ["f_gone"]
    assert found[0].switch_date == "20260103"
    assert found[0].missing_side == "after"


def test_era_indicator_ignores_column_present_throughout():
    # Arrange
    df = _panel(f_ok={d: float(i) for i, d in enumerate(DATES)})

    # Act / Assert
    assert scan_era_indicators(df, date_col="dt", feature_cols=["f_ok"]) == []


def test_era_indicator_ignores_scattered_missing_values():
    """零散缺失（真·随机缺失）不是年代指示器，不能误报。"""
    # Arrange: 只有 20260103 全空，两侧都有值
    df = _panel(f_sparse={d: 1.0 for d in DATES if d != "20260103"})

    # Act / Assert
    assert scan_era_indicators(df, date_col="dt", feature_cols=["f_sparse"]) == []


def test_era_indicator_ignores_partial_null_within_a_date():
    """某日只有一半行缺 → 不是整列缺失，构不成年代分隔。"""
    # Arrange
    df = _panel(f_half=dict.fromkeys(DATES, 1.0))
    df.loc[(df["dt"] == "20260105") & (df["symbol"] == "A"), "f_half"] = None

    # Act / Assert
    assert scan_era_indicators(df, date_col="dt", feature_cols=["f_half"]) == []


def test_era_indicator_requires_min_dates_to_avoid_single_date_noise():
    """只有一个日期时无法判定分隔，必须要求最少日期数。"""
    # Arrange
    df = pd.DataFrame({"dt": ["20260101"] * 2, "symbol": ["A", "B"], "f": [1.0, None]})

    # Act / Assert
    assert scan_era_indicators(df, date_col="dt", feature_cols=["f"], min_dates=3) == []


def test_era_indicator_raises_on_missing_date_column():
    # Arrange
    df = pd.DataFrame({"f": [1.0, 2.0]})

    # Act / Assert
    with pytest.raises(ValueError, match="date_col"):
        scan_era_indicators(df, date_col="dt", feature_cols=["f"])


def test_era_switch_reports_date_and_side():
    """逐日缺失率 → (切换日, 空在哪一侧)。这是 DataFrame 与 parquet 元数据两条
    取数路径的公共判据，单独测，避免两边实现漂移。"""
    assert era_switch(["d1", "d2", "d3"], [1.0, 0.0, 0.0]) == ("d2", "before")
    assert era_switch(["d1", "d2", "d3"], [0.0, 0.0, 1.0]) == ("d3", "after")


def test_era_switch_is_none_without_exactly_one_transition():
    # 两次切换（零散缺失）
    assert era_switch(["d1", "d2", "d3"], [1.0, 0.0, 1.0]) is None
    # 全程有值
    assert era_switch(["d1", "d2", "d3"], [0.0, 0.0, 0.0]) is None
    # 单日内部分缺失 —— 构不成整列缺失
    assert era_switch(["d1", "d2", "d3"], [0.0, 0.5, 0.0]) is None
    # 日期退化：少于两个日期无法定义「切换」
    assert era_switch(["d1"], [1.0]) is None


def test_era_switch_misses_data_islands_by_design():
    """**已知边界**：`era_switch` 只认「恰好一次切换」，中段孤岛会被它漏掉 ——
    所以另有 `availability_island` 兜底。这条测试是把这个分工钉死，别让后人以为
    「era_switch 返回 None」等于「这列没问题」。"""
    # Arrange: 全空 → 中段有值 → 又全空
    fracs = [1.0] * 100 + [0.0] * 10 + [1.0] * 10

    # Act / Assert
    assert era_switch([f"d{i:03d}" for i in range(120)], fracs) is None


# ─────────────────────── 数据孤岛 ───────────────────────


def _long_series(n=120):
    return [f"d{i:03d}" for i in range(n)]


def test_availability_island_detects_mid_range_block():
    """实测 l1_factors.ind_netflow_rank_20：全空 2430 天 → 有值 34 天 → 全空 137 天。

    两次切换，era_switch 判不出来，但对模型同样致命 —— 该列的全部信息都锁在
    2026 年初那 34 天里，任何用到它的模型都被锚定到那个时段。
    """
    # Arrange
    dates = _long_series()
    fracs = [1.0] * 100 + [0.0] * 10 + [1.0] * 10

    # Act
    hit = availability_island(dates, fracs)

    # Assert
    assert hit is not None
    start, span, total = hit
    assert start == "d100" and span == 10 and total == 120


def test_availability_island_ignores_scattered_gaps():
    """散布的单日空洞：最长可用段只占可用日期的极小部分 → 不是孤岛。"""
    # Arrange
    dates = _long_series()
    fracs = [0.0] * 120
    for i in (10, 30, 50, 70, 90):
        fracs[i] = 1.0

    # Act / Assert
    assert availability_island(dates, fracs) is None


def test_availability_island_ignores_full_coverage():
    dates = _long_series()

    assert availability_island(dates, [0.0] * 120) is None
    # 全空由缺失率规则负责，孤岛规则不重复记账
    assert availability_island(dates, [1.0] * 120) is None


def test_availability_island_ignores_leading_warmup():
    """把孤岛规则从 2230 列假阳性里救回来的那条判据。

    长预热期的列（前 40 天不可用、其后一直可用）**不是孤岛** ——
    孤岛必须两端都被不可用日包住。少了这一条，alpha360 会 366/368 全红、
    factor_defs 1243/1338 全红，门禁直接失去意义。
    """
    # Arrange: 前 40 天不可用，其后一直可用（可用段接到序列末尾）
    dates = _long_series()
    fracs = [1.0] * 40 + [0.0] * 80

    # Act / Assert
    assert availability_island(dates, fracs) is None


def test_availability_island_ignores_trailing_only_coverage():
    """反向同理：可用段从序列头开始 → 不是孤岛，是「中途没了」。"""
    dates = _long_series()
    fracs = [0.0] * 80 + [1.0] * 40

    assert availability_island(dates, fracs) is None


def test_availability_island_ignores_short_series():
    """短序列不判孤岛：5 个日期里「缺 1 天」占 20%，
    与「一个月的断档」在比例上无法区分。"""
    assert (
        availability_island(["d1", "d2", "d3", "d4", "d5"], [1.0, 1.0, 0.0, 1.0, 1.0])
        is None
    )


def test_availability_island_tolerates_a_few_nulls_inside_the_block():
    """孤岛内部有几行正常缺失（如 0.0002）不该让整块被判为「不可用」。"""
    # Arrange
    dates = _long_series()
    fracs = [1.0] * 100 + [0.0] * 10 + [1.0] * 10
    fracs[105] = 0.0002

    # Act
    hit = availability_island(dates, fracs)

    # Assert
    assert hit is not None and hit[1] == 10


# ─────────────────────── 缺失率 ───────────────────────


def test_missing_rate_flags_column_over_threshold():
    # Arrange: f_bad 缺 3/4 行
    df = _panel(
        f_bad={
            "20260101": 1.0,
            "20260102": 2.0,
            "20260103": None,
            "20260104": None,
            "20260105": None,
        },
        f_ok=dict.fromkeys(DATES, 1.0),
    )

    # Act
    out = scan_missing_rates(df, feature_cols=["f_bad", "f_ok"], max_rate=0.5)

    # Assert
    assert [f.subject for f in out] == ["f_bad"]
    assert out[0].severity == "HIGH"
    assert "60.0%" in out[0].detail


def test_missing_rate_is_silent_when_under_threshold():
    # Arrange: 缺 1/5 = 20% < 50%
    df = _panel(f_ok={d: 1.0 for d in DATES if d != "20260101"})

    # Act / Assert
    assert scan_missing_rates(df, feature_cols=["f_ok"], max_rate=0.5) == []


# ─────────────────────── 回测窗口 ───────────────────────


def test_backtest_window_rejects_twelve_trading_days():
    """12 个交易日 → 拒绝。`(1+r)^(1/0.04)-1` 会把 2% 放大成 64% 年化。

    天数取自 `backtest_l2_top20.py` 的真实窗口（2026-08-04 ~ 08-19，跳过周末）。
    """
    # Arrange
    days = [
        "20260804",
        "20260805",
        "20260806",
        "20260807",
        "20260810",
        "20260811",
        "20260812",
        "20260813",
        "20260814",
        "20260817",
        "20260818",
        "20260819",
    ]

    # Act
    out = check_backtest_window(days, min_trading_days=60, context="L2 Top20")

    # Assert
    assert len(out) == 1
    assert out[0].severity == "CRITICAL"
    assert "12" in out[0].detail
    assert "L2 Top20" in out[0].detail


def test_backtest_window_passes_when_long_enough():
    # Arrange
    days = [f"2026{i:04d}" for i in range(101, 200)]

    # Act / Assert
    assert check_backtest_window(days, min_trading_days=60) == []


def test_backtest_window_rejects_empty_input():
    # Arrange / Act
    out = check_backtest_window([], min_trading_days=60)

    # Assert
    assert len(out) == 1 and out[0].severity == "CRITICAL"


# ─────────────────────── 源码扫描：自写阈值 ───────────────────────


def test_hardcoded_limit_scanner_finds_bare_threshold(tmp_path):
    # Arrange
    src = tmp_path / "bt.py"
    src.write_text(
        "def f(pct):\n    return abs(pct) >= 9.8  # 接近涨停\n", encoding="utf-8"
    )

    # Act
    out = scan_hardcoded_limit_thresholds([src])

    # Assert
    assert len(out) == 1
    assert out[0].severity == "HIGH"
    assert "9.8" in out[0].detail


def test_hardcoded_limit_scanner_finds_hardcoded_st_false(tmp_path):
    """is_st=False 写死 → ST 的 5% 涨跌停永不生效。"""
    # Arrange
    src = tmp_path / "bt.py"
    src.write_text(
        "compute_limits(code, pre, is_st=False, trade_date=d)\n", encoding="utf-8"
    )

    # Act
    out = scan_hardcoded_limit_thresholds([src])

    # Assert
    assert len(out) == 1
    assert "is_st=False" in out[0].detail


def test_hardcoded_limit_scanner_honours_allow_pragma(tmp_path):
    """显式豁免（附理由）应当放行，避免把合法用例钉死。"""
    # Arrange
    src = tmp_path / "bt.py"
    src.write_text(
        "x = 9.8  # fidelity: allow-limit-threshold — 此处是年化系数\n",
        encoding="utf-8",
    )

    # Act / Assert
    assert scan_hardcoded_limit_thresholds([src]) == []


def test_hardcoded_limit_scanner_does_not_honour_ruff_noqa(tmp_path):
    """**刻意与 ruff 分家**：`# noqa` 是 ruff 的语法，拿它当本门禁的豁免会让
    ruff 对每处合法豁免报「非法指令」。豁免只认 `# fidelity: allow-limit-threshold`。"""
    # Arrange
    src = tmp_path / "bt.py"
    src.write_text("x = 9.8  # noqa: limit-threshold\n", encoding="utf-8")

    # Act
    out = scan_hardcoded_limit_thresholds([src])

    # Assert
    assert len(out) == 1


def test_hardcoded_limit_scanner_ignores_authoritative_module(tmp_path):
    """权威实现自身持有制度常量，不得被自扫误伤。

    文件里故意放一个**会被匹配**的字面量，确保豁免来自文件名判定而非巧合。
    """
    # Arrange
    # 夹具字面量单独成行：它**必须**保持可被匹配（否则本用例退化为恒真），
    # 所以按门禁的豁免语法就地声明理由。反过来把 `# fidelity: ...` 塞进被写入
    # 的字符串里是错的 —— 豁免会跟着字符串进到临时文件，测试就成了「因为豁免
    # 所以通过」，恰好放掉它要证明的那件事。
    legacy = "_LEGACY = 0.098"  # fidelity: allow-limit-threshold — 夹具字面量
    src = tmp_path / "local_market_data.py"
    src.write_text(
        f'_PCT_MAIN = Decimal("0.10")\n{legacy}  # 权威模块内部的制度常量\n',
        encoding="utf-8",
    )

    # Act / Assert
    assert scan_hardcoded_limit_thresholds([src]) == []


# ─────────────────────── 总闸门 ───────────────────────


def test_gate_raises_when_critical_finding_present():
    # Arrange
    rep = FidelityReport(
        findings=(
            Finding(
                rule="backtest_window",
                severity="CRITICAL",
                subject="L2",
                detail="12 天",
            ),
        )
    )

    # Act / Assert
    with pytest.raises(FidelityError, match="12 天"):
        rep.gate()


def test_gate_passes_with_only_high_findings():
    """HIGH 不阻断（但要点名），只有 CRITICAL 才拦 —— 否则会被绕过。"""
    # Arrange
    rep = FidelityReport(
        findings=(
            Finding(rule="missing_rate", severity="HIGH", subject="f", detail="60%"),
        )
    )

    # Act / Assert
    rep.gate()
    assert "f" in rep.summary()


def test_gate_raises_on_empty_findings_when_required():
    """零项参与即失败：没扫描出任何东西 ≠ 数据是干净的。"""
    # Arrange
    rep = FidelityReport(findings=(), n_checked=0)

    # Act / Assert
    with pytest.raises(FidelityError, match="未检查"):
        rep.gate(require_checks=True)


def test_era_indicator_is_exported_as_frozen_dataclass():
    # Arrange / Act
    e = EraIndicator(
        column="f", switch_date="20260104", missing_side="before", n_dates=5
    )

    # Assert: frozen 是契约的一部分（发现一旦产生就不该被就地改写）
    with pytest.raises(FrozenInstanceError):
        e.column = "g"  # type: ignore[misc]


# ─────────────────── 数据集取数（门禁 CLI） ───────────────────


def _load_cli():
    """按路径加载门禁 CLI（它自身会 sys.path.insert 到仓库根）。"""
    spec = importlib.util.spec_from_file_location(
        "check_market_fidelity_under_test", CLI_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_partitions(root, dates):
    """每个日期写一个 dt= 分区，行数固定 2，便于手算缺失率。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    for d in dates:
        p = root / f"dt={d}"
        p.mkdir(parents=True)
        pq.write_table(
            pa.table({"symbol": ["A", "B"], "f": [1.0, None]}), p / "part-0.parquet"
        )


def _write_dataset(root, dates, cols_by_date):
    """cols_by_date: 列名 -> {日期: 该列取值}。某日期不在内层字典里 = 该列在这天不存在。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    for d in dates:
        p = root / f"dt={d}"
        p.mkdir(parents=True)
        data = {"symbol": ["A", "B", "C", "D"]}
        for col, by_date in cols_by_date.items():
            if d in by_date:
                data[col] = by_date[d]
        pq.write_table(pa.table(data), p / "part-0.parquet")


def _long_dates(n):
    return [f"2026{i:04d}" for i in range(1, n + 1)]


def test_scan_dataset_separates_warmup_from_era_indicator(tmp_path):
    """同一份数据里两种「前段全空」必须分开判 —— 这正是门禁最容易被绕过的地方。

    - `f_warmup` 仅头 3 天缺席 → **预热期 MEDIUM**（数学必然，不是缺陷）
    - `f_late`   头 35 天缺席   → **年代指示器 CRITICAL**（真·schema 断点）

    两者形态完全一样（老段全空、之后全有），只有**缺席天数**能区分。
    少了这条区分，长预热期的列会被全库误杀；有了它却设错阈值，真断点又会被放过。
    """
    # Arrange
    dates = _long_dates(40)
    root = tmp_path / "ds"
    _write_dataset(
        root,
        dates,
        {
            "f_warmup": {d: [1.0] * 4 for d in dates[3:]},
            "f_late": {d: [1.0] * 4 for d in dates[35:]},
        },
    )
    cli = _load_cli()

    # Act
    report = cli._scan_dataset(root, "", "", cli.DEFAULT_WARMUP_MAX, 0.5)

    # Assert
    by_rule = {f.subject: f for f in report.findings}
    assert by_rule["f_warmup"].rule == "series_warmup"
    assert by_rule["f_warmup"].severity == "MEDIUM"
    assert by_rule["f_late"].rule == "era_indicator"
    assert by_rule["f_late"].severity == "CRITICAL"

    # 门禁只拦 CRITICAL：预热期不该阻断发布
    with pytest.raises(FidelityError, match="f_late"):
        report.gate()


def test_scan_dataset_flags_high_missing_rate_as_high(tmp_path):
    """高缺失列报 HIGH（点名但不阻断）—— 它降低模型质量，却不构成时间分隔。"""
    # Arrange: f_sparse 每天 4 行里 3 行是空 = 75%
    dates = _long_dates(5)
    root = tmp_path / "ds"
    _write_dataset(
        root, dates, {"f_sparse": {d: [1.0, None, None, None] for d in dates}}
    )
    cli = _load_cli()

    # Act
    report = cli._scan_dataset(root, "", "", cli.DEFAULT_WARMUP_MAX, 0.5)

    # Assert
    assert [f.severity for f in report.findings] == ["HIGH"]
    assert report.findings[0].rule == "missing_rate"
    assert "75.0%" in report.findings[0].detail
    report.gate()  # HIGH 不阻断


def test_cli_exit_code_is_the_gate_contract(tmp_path):
    """门禁的对外契约是**退出码**。同目录的 `audit_*.py` 是分析师（打印完
    `return 0`），这个必须真失败，否则挂进 CI 也拦不住任何东西。"""
    # Arrange: 一个只有 5 天的数据集（窗口过短，另有 era 列）
    root = tmp_path / "ds"
    _write_dataset(
        root, _long_dates(5), {"f_late": {d: [1.0] * 4 for d in _long_dates(5)[3:]}}
    )
    cli = _load_cli()

    # Act / Assert: 数据集体检有 CRITICAL → 退出 1
    assert cli.main(["dataset", "--root", str(root), "--warmup-max", "0"]) == 1
    # 把断点判成预热期后无 CRITICAL → 退出 0
    assert cli.main(["dataset", "--root", str(root)]) == 0
    # 12 个交易日 < 60 → 退出 1
    assert cli.main(["window", "--days", "12"]) == 1
    # 足够长的窗口 → 退出 0
    assert cli.main(["window", "--days", "200"]) == 0


def test_fail_on_works_on_either_side_of_the_subcommand(tmp_path):
    """`--fail-on` 必须**两个位置都能写**。

    只挂顶层解析器时，`source --fail-on high`（本模块文档示例的写法）会被
    argparse 判为未知参数、以退出码 2 失败 —— 而且失败得「看起来像」门禁生效，
    最难发现。子命令侧用 `default=SUPPRESS`，否则顶层解析到的取值会被
    子命令的默认值覆盖回 "critical"。
    """
    # Arrange: 一个只有 HIGH 发现的源码目录（区分 critical/high 才有意义）
    src = tmp_path / "src"
    src.mkdir()
    (src / "bt.py").write_text("y = abs(pct) >= 9.8\n", encoding="utf-8")
    cli = _load_cli()

    # Act / Assert: 默认门槛 critical → HIGH 不阻断
    assert cli.main(["source", str(src)]) == 0
    # 子命令后置
    assert cli.main(["source", str(src), "--fail-on", "high"]) == 1
    # 顶层前置 —— 若子命令默认值发生覆盖，这里会退回 0
    assert cli.main(["--fail-on", "high", "source", str(src)]) == 1


def test_collect_fractions_honours_from_and_to_together(tmp_path):
    """`--from` 与 `--to` 同时给出时必须取**交集**。

    曾经用两个平行列表各做一次 zip 过滤：第二次过滤把已过滤的目录配上
    未过滤的日期，交集被算成空集 —— 用户同时给两端区间就静默 `SystemExit`，
    而合法分区明明存在。
    """
    # Arrange
    root = tmp_path / "ds"
    _write_partitions(root, ["20260101", "20260102", "20260103", "20260104"])
    cli = _load_cli()

    # Act
    dates, fractions = cli._collect_fractions(root, "20260102", "20260103")

    # Assert
    assert dates == ["20260102", "20260103"]
    assert set(fractions) == {"symbol", "f"}


def test_collect_fractions_treats_missing_column_as_fully_absent(tmp_path):
    """某日文件里根本没有这一列 → 记 1.0，与「整列全空」同规则。

    对模型而言两者是同一种伤害，判据上不该分家（否则后加的列会因为
    「老分区没这列」被判成正常）。
    """
    # Arrange: 前两天只有 f，后两天多了 g
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "ds"
    for d in ("20260101", "20260102"):
        p = root / f"dt={d}"
        p.mkdir(parents=True)
        pq.write_table(pa.table({"f": [1.0]}), p / "part-0.parquet")
    for d in ("20260103", "20260104"):
        p = root / f"dt={d}"
        p.mkdir(parents=True)
        pq.write_table(pa.table({"f": [1.0], "g": [2.0]}), p / "part-0.parquet")
    cli = _load_cli()

    # Act
    dates, fractions = cli._collect_fractions(root, "", "")

    # Assert
    assert [fractions["g"][d] for d in dates] == [1.0, 1.0, 0.0, 0.0]


def test_hardcoded_limit_scanner_ignores_docstring_prose(tmp_path):
    """docstring 是散文，不是实现。

    讲清一个缺陷往往**必须**引用被废弃的旧常量（「旧实现返回
    0.095/0.195/0.295」）。若不排除 docstring，门禁会惩罚「把理由写下来」——
    方向正好反了，且会逼着后来者删注释来换绿灯。
    """
    # Arrange
    src = tmp_path / "bt.py"
    src.write_text(
        '"""旧实现按前缀返回 0.095/0.195/0.295，不认创业板改革。"""\n'
        "\n"
        "\n"
        "def f(pct):\n"
        '    """阈值 0.098 是史前遗留。"""\n'
        "    return pct\n",
        encoding="utf-8",
    )

    # Act / Assert
    assert scan_hardcoded_limit_thresholds([src]) == []


def test_hardcoded_limit_scanner_still_finds_threshold_after_docstring(tmp_path):
    """排除 docstring 不得顺带放过整个函数体。"""
    # Arrange
    src = tmp_path / "bt.py"
    src.write_text(
        'def f(pct):\n    """阈值 0.098 是史前遗留。"""\n    return abs(pct) >= 9.8\n',
        encoding="utf-8",
    )

    # Act
    out = scan_hardcoded_limit_thresholds([src])

    # Assert
    assert len(out) == 1
    assert out[0].subject.endswith(":3")


def test_hardcoded_limit_scanner_still_finds_threshold_in_plain_string(tmp_path):
    """只豁免 docstring —— 普通字符串里的阈值是**活的**。

    策略模板/DSL 就是把阈值写成字符串再喂给引擎的
    （`gen_ashare_strategy_templates.py` 即此类），豁免它等于开一个真实盲区。
    """
    # Arrange
    src = tmp_path / "bt.py"
    src.write_text(
        'TPL = "if pct_change >= 0.098: skip"\n',
        encoding="utf-8",
    )

    # Act
    out = scan_hardcoded_limit_thresholds([src])

    # Assert
    assert len(out) == 1


def test_hardcoded_limit_scanner_fails_safe_on_unparseable_source(tmp_path):
    """语法错误时不做任何豁免：宁可多报，不可漏报。"""
    # Arrange
    src = tmp_path / "broken.py"
    src.write_text(
        '"""docstring 里的 0.098"""\ndef f(:\n    return 9.8\n',
        encoding="utf-8",
    )

    # Act
    out = scan_hardcoded_limit_thresholds([src])

    # Assert — 两行都要报：docstring 那行不获豁免，才是 fail-safe 的含义
    assert sorted(f.subject.rsplit(":", 1)[1] for f in out) == ["1", "3"]
