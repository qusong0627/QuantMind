"""策略评分卡的成交口径测试（`scripts/eval/strategy_card.py`，设计 §2.3）。

被测的是「成本 / 容量」两维的取数与评分纯函数。盯四件事：
- `trades[]` 缺字段/空**不许编**：如实 `insufficient` 并写明缺的是什么；
- 单位：`daily_forward.amount` 是**万元**，`trades.totalAmount` 是**元**，换算错会让容量差 1e4 倍；
- 佣金只是成本**下界**（无印花税/过户费/滑点），detail 必须写清这条口径；
- 容量维沿用 `metrics_eval.capacity_estimate` 的假设模型，其假设说明必须原样带出。
"""

from __future__ import annotations

import json

import pytest

from backend.scripts.eval.strategy_card import (
    curve_scalars,
    parse_equity_curve,
    save_curve_sidecar,
    strategy_dims,
)
from backend.scripts.eval.strategy_realized import (
    capacity_dim,
    cost_dim,
    trade_stats,
)


def _capacity_import_error() -> str | None:
    """容量维要 `metrics_eval.capacity_estimate`（经 engine 包 → fastapi）；
    本地轻量 .venv 没装 → 这两个用例在容器内跑（`docker exec quantmind …`）。"""
    try:
        from backend.services.engine.factor_report.metrics_eval import (  # noqa: F401
            capacity_estimate,
        )
    except ModuleNotFoundError as exc:
        return str(exc.name)
    return None


_MISSING_DEP = _capacity_import_error()
needs_engine = pytest.mark.skipif(
    _MISSING_DEP is not None,
    reason=f"本地 .venv 缺依赖（{_MISSING_DEP}）：容量维用例在容器内跑",
)

TRADES = [
    # 两天各一买一卖，单边成交额 = (20000+30000)/2 = 25000 元
    {
        "date": "2025-10-16",
        "symbol": "600036.SH",
        "action": "buy",
        "price": 39.91,
        "quantity": 1000,
        "totalAmount": 39910.0,
        "commission": 9.9775,
    },
    {
        "date": "2025-10-16",
        "symbol": "000001.SZ",
        "action": "buy",
        "price": 11.0,
        "quantity": 2000,
        "totalAmount": 22000.0,
        "commission": 5.5,
    },
    {
        "date": "2025-10-17",
        "symbol": "600036.SH",
        "action": "sell",
        "price": 40.0,
        "quantity": 1000,
        "totalAmount": 40000.0,
        "commission": 10.0,
    },
    {
        "date": "2025-10-17",
        "symbol": "000001.SZ",
        "action": "sell",
        "price": 11.2,
        "quantity": 2000,
        "totalAmount": 22400.0,
        "commission": 5.6,
    },
]


def _curve(values: list[float]) -> list[float]:
    return values


# ── 成交统计 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_trade_stats_is_insufficient_when_no_trades():
    """空成交流水：不允许按 0 换手算分——如实缺省并说明原因。"""
    stats = trade_stats([], _curve([1e6, 1.01e6]), trading_days=252)

    assert stats["insufficient"] is True
    assert stats["n_trades"] == 0
    assert "成交明细" in stats["note"]


@pytest.mark.unit
def test_trade_stats_computes_one_way_amount_and_annual_turnover():
    """单边口径 = Σ|totalAmount|/2（买卖各半），年化换手 = 单边额/均净值/天数×252。"""
    stats = trade_stats(
        TRADES, _curve([100000.0, 100000.0, 100000.0]), trading_days=252
    )

    assert stats["n_trades"] == 4
    assert stats["n_symbols"] == 2
    assert stats["one_way_amount"] == pytest.approx(
        62155.0
    )  # (39910+22000+40000+22400)/2
    assert stats["avg_equity"] == pytest.approx(100000.0)
    # 3 天窗口，单边 62155 / 均净值 100000 / 3 天
    assert stats["daily_turnover"] == pytest.approx(62155.0 / 100000.0 / 3)
    assert stats["annual_turnover"] == pytest.approx(62155.0 / 100000.0 / 3 * 252)
    assert stats["commission_total"] == pytest.approx(31.0775)


@pytest.mark.unit
def test_trade_stats_median_holdings_replays_buy_sell():
    """持仓数由成交流水回放（trades 无 positions 快照）：两日各持 2 只 → 中位 2。

    第二日收盘已全部卖出（空仓日）——空仓日不计入中位但另报 ``flat_days``，
    否则「清仓后一天」会把典型持仓数读成一半。
    """
    stats = trade_stats(TRADES, _curve([1e5, 1e5, 1e5]))

    assert stats["n_days_with_trades"] == 2
    assert stats["median_holdings"] == pytest.approx(2.0)
    assert stats["max_holdings"] == 2
    assert stats["flat_days"] == 1


@pytest.mark.unit
def test_trade_stats_counts_unreadable_rows_instead_of_dropping_silently():
    """缺 totalAmount 的行要计数并写进 note，不能悄悄丢掉后当完整数据算。"""
    trades = [*TRADES, {"date": "2025-10-17", "symbol": "X.SH", "action": "buy"}]

    stats = trade_stats(trades, _curve([1e5, 1e5, 1e5]))

    assert stats["n_trades"] == 4
    assert stats["n_unreadable"] == 1
    assert "1 行" in stats["note"]


# ── 成本维 ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cost_dim_is_insufficient_without_trades():
    stats = trade_stats([], _curve([1e6, 1.1e6]))

    dim = cost_dim(stats, gross_pnl=1e5, ann_return=0.2, round_trip_cost=0.002)

    assert dim.score is None
    assert dim.detail["insufficient"] is True
    assert "成交明细" in str(dim.detail["note"])


@pytest.mark.unit
def test_cost_dim_fails_red_line_when_cost_eats_half_the_profit():
    """设计 §2.3 红线：成本吃掉 > 50% 毛利。佣金实测超半 → 红线。"""
    stats = trade_stats(TRADES, _curve([1e5, 1e5, 1e5]))
    stats = {**stats, "commission_total": 600.0, "one_way_amount": 1000.0}

    dim = cost_dim(stats, gross_pnl=1000.0, ann_return=None, round_trip_cost=0.002)

    assert dim.red_line_failed is True
    assert "毛利" in str(dim.detail["red_line"])
    assert dim.detail["cost_over_gross"] == pytest.approx(0.6)


@pytest.mark.unit
def test_cost_dim_states_commission_is_only_a_lower_bound():
    """佣金不含印花税/过户费/滑点 → 实测口径是下界，另给模型口径的上界。"""
    stats = trade_stats(TRADES, _curve([1e5, 1e5, 1e5]))

    dim = cost_dim(stats, gross_pnl=1000.0, ann_return=0.30, round_trip_cost=0.002)

    scope = str(dim.detail["cost_scope_note"])
    assert "不含印花税" in scope
    assert dim.detail["round_trip_cost"] == pytest.approx(0.002)
    # 模型口径：年化换手 × 双边成本 = 磨损掉的年化收益
    assert dim.detail["model_cost_over_return"] is not None


@pytest.mark.unit
def test_cost_dim_falls_back_to_turnover_when_profit_is_negative():
    """毛利与年化收益都 ≤0 时「成本吃掉毛利」无定义：只按换手计分并写明。"""
    stats = trade_stats(TRADES, _curve([1e5, 1e5, 1e5]))

    dim = cost_dim(stats, gross_pnl=-5000.0, ann_return=-0.2, round_trip_cost=0.002)

    assert dim.score is not None
    assert dim.red_line_failed is False
    assert "无定义" in str(dim.detail["note"])
    assert dim.detail["cost_over_gross"] is None


# ── 容量维 ───────────────────────────────────────────────────────────


@pytest.mark.unit
@needs_engine
def test_capacity_dim_converts_wan_to_yuan_before_estimating():
    """`daily_forward.amount` 单位是万元：漏乘 1e4 会把容量低估 1 万倍。"""
    stats = {**trade_stats(TRADES, _curve([1e5, 1e5, 1e5]))}

    dim = capacity_dim(stats, median_amount_wan=5000.0, n_positions=50)

    # est = 参与率 10% × 5000 万元(5e7 元) × 50 / 日换手
    t = stats["daily_turnover"]
    assert dim.detail["est_aum"] == pytest.approx(0.10 * 5e7 * 50 / t)
    assert dim.detail["median_amount_yuan"] == pytest.approx(5e7)
    assert dim.detail["median_amount_wan"] == pytest.approx(5000.0)
    assert dim.score is not None


@pytest.mark.unit
@needs_engine
def test_capacity_dim_carries_assumption_note_verbatim():
    """容量是假设模型不是实测：`capacity_estimate` 的假设说明必须原样带出。"""
    stats = trade_stats(TRADES, _curve([1e5, 1e5, 1e5]))

    dim = capacity_dim(stats, median_amount_wan=5000.0, n_positions=50)

    assert "参与率" in str(dim.detail["note"]) or "假设" in str(dim.detail["note"])
    assert dim.detail["assumed_participation"] == pytest.approx(0.10)


@pytest.mark.unit
def test_capacity_dim_is_insufficient_without_amount_or_holdings():
    """取不到日成交额 / 无持仓 → 缺省，且 note 说明缺的是哪一项。"""
    stats = trade_stats(TRADES, _curve([1e5, 1e5, 1e5]))

    missing_amount = capacity_dim(stats, median_amount_wan=None, n_positions=50)
    missing_pos = capacity_dim(stats, median_amount_wan=5000.0, n_positions=0)

    assert missing_amount.score is None
    assert "成交额" in str(missing_amount.detail["note"])
    assert missing_pos.score is None
    assert "持仓" in str(missing_pos.detail["note"])


# ── 整体接线（不读库：曲线与成交全由入参给） ──────────────────────────


@pytest.mark.unit
def test_strategy_dims_uses_trades_for_cost_and_reports_amount_gap_honestly():
    """有成交 → 成本维真实算分；日成交额取数失败 → 容量维缺省带出失败原因。"""
    loaded = {
        "equity_curve": [1e5 * (1 + 0.001 * i) for i in range(30)],
        "dates": [f"2025-10-{i + 1:02d}" for i in range(30)],
        "drawdown_curve": [0.0] * 30,
        "trades": TRADES,
    }

    dims, evidence = strategy_dims(
        loaded,
        bench_ann=0.05,
        amount_stats={"note": "daily_forward 取数失败：Catalog Error"},
        round_trip_cost=0.002,
    )
    by_key = {d.key: d for d in dims}

    assert by_key["cost"].score is not None
    assert by_key["capacity"].score is None
    assert "Catalog Error" in str(by_key["capacity"].detail["note"])
    assert evidence["trades"]["n_trades"] == 4


@pytest.mark.unit
def test_strategy_dims_consistency_note_points_at_the_real_gap():
    """一致性维保持缺省，但 note 必须指向真实缺口（不是「无模拟盘」这种不实说法）。"""
    loaded = {
        "equity_curve": [1e5, 1e5, 1e5],
        "dates": ["2025-10-16", "2025-10-17", "2025-10-20"],
        "drawdown_curve": None,
        "trades": [],
    }

    dims, _ = strategy_dims(
        loaded, bench_ann=None, amount_stats=None, round_trip_cost=0.002
    )
    by_key = {d.key: d for d in dims}

    note = str(by_key["consistency"].detail["note"])
    assert "未接入评估侧" in note
    assert "无同期模拟曲线" not in note


# ── 代码口径（实盘数据暴露的坑） ──────────────────────────────────────


@pytest.mark.unit
def test_normalize_symbols_maps_qlib_lowercase_to_suffix():
    """实测回测 trades 里同时存在 `sh600007`（Qlib 小写）与 `600036.SH`：
    两种都必须归一到 QuantDB 的后缀式，否则 `daily_forward` 一行都匹配不上、
    `median()` 返回 NULL 静默变 NaN。"""
    from backend.scripts.eval.strategy_card import normalize_symbols

    out = normalize_symbols(
        ["sh600007", "600036.SH", "SH600000", "300750", "", "  ", "乱码$$"]
    )

    assert out == ["300750.SZ", "600000.SH", "600007.SH", "600036.SH"]


# ── 曲线装载 + 长序列侧车（设计 §1.6）───────────────────────────────


@pytest.mark.unit
def test_parse_equity_curve_drops_missing_values_and_keeps_dates_aligned():
    """缺测点连日期一起丢（两个列表必须等长，调用方 zip 时才不错位）；
    真 0 净值是有效点，不能被当成缺测剔除。"""
    # Arrange
    curve = [
        {"date": "2025-10-16", "value": 1.0},
        {"date": "2025-10-17", "value": None},
        {"date": "2025-10-20"},
        {"date": "2025-10-21", "value": float("nan")},
        {"date": "2025-10-22", "value": 0.0},
    ]

    # Act
    dates, values, dropped = parse_equity_curve(curve)

    # Assert
    assert dates == ["2025-10-16", "2025-10-22"]
    assert values == [1.0, 0.0]
    assert dropped == 3


@pytest.mark.unit
def test_parse_equity_curve_handles_empty_and_non_mapping_rows():
    """裸数字/字符串行不是净值点（老形态没有这种行），一律丢弃并计数。"""
    assert parse_equity_curve([]) == ([], [], 0)

    dates, values, dropped = parse_equity_curve([1.5, "x", {}, {"value": "2.5"}])

    assert values == [2.5]
    assert dates == [""]  # 数字形态的 value 可解析，但没有日期
    assert dropped == 3
    assert len(dates) == len(values)


@pytest.mark.unit
def test_curve_scalars_reads_numbers_already_computed_by_the_card():
    """图上标量必须与卡上分数同源：年化/最大回撤只从维度 detail 里取，不重算。"""
    # Arrange
    combined = {
        "score": 61.5,
        "grade": "C",
        "dimensions": {
            "return": {
                "detail": {
                    "annual_return": 0.1834,
                    "benchmark_annual": 0.09,
                    "excess_annual": 0.0934,
                }
            },
            "risk": {"detail": {"max_drawdown": -0.2146, "annual_vol": 0.31}},
            "stability": {"detail": {"months": 12, "monthly_win_rate": 0.5833}},
        },
    }

    # Act
    scalars = curve_scalars(combined)

    # Assert
    assert scalars["score"] == 61.5
    assert scalars["annual_return"] == 0.1834
    assert scalars["max_drawdown"] == -0.2146
    assert scalars["n_months"] == 12


@pytest.mark.unit
def test_curve_scalars_on_empty_combined_gives_none_not_zero():
    """维度全缺时不编数：None 让前端走缺省分支，0 会被读成「回撤为 0」。"""
    scalars = curve_scalars({})

    assert scalars["score"] is None
    assert scalars["max_drawdown"] is None
    assert scalars["annual_return"] is None


@pytest.mark.unit
def test_save_curve_sidecar_writes_series_named_by_backtest_id(tmp_path, monkeypatch):
    """侧车落在 `data/eval_series/strategy/<backtest_id>.json`，两条曲线与月度柱齐全。"""
    # Arrange
    monkeypatch.setenv("QM_EVAL_SERIES_DIR", str(tmp_path))
    loaded = {
        "dates": ["2025-10-16", "2025-10-17", "2025-10-20"],
        "equity_curve": [1.0, 1.1, 1.2],
        "drawdown_entries": [
            ("2025-10-16", 0.0),
            ("2025-10-17", -0.02),
            ("2025-10-20", 0.0),
        ],
        "drawdown_dropped": 0,
        "equity_dropped": 1,
        "path": "data/backtest_results/bt_abc.json",
    }
    combined = {"score": 61.5, "grade": "C", "dimensions": {}}

    # Act
    status = save_curve_sidecar(
        loaded,
        {"monthly": [("2025-10", 0.2)]},
        combined,
        object_id="bt_abc",
        backtest_id="bt_abc",
    )

    # Assert
    assert status["written"] is True
    assert status["note"] is None
    raw = json.loads((tmp_path / "strategy" / "bt_abc.json").read_text("utf-8"))
    assert [p["value"] for p in raw["series"]["equity"]] == [1.0, 1.1, 1.2]
    assert [p["value"] for p in raw["series"]["drawdown"]] == [0.0, -0.02, 0.0]
    assert raw["series"]["monthly_return"] == [{"label": "2025-10", "value": 0.2}]
    assert raw["scalars"]["score"] == 61.5
    # 装载层丢的点也要进 notes（否则「读进来时丢了几个点」无人知晓）
    assert "1 个净值点值非法已剔除" in raw["notes"]["equity"]


@pytest.mark.unit
def test_save_curve_sidecar_flags_id_mismatch_for_cli_file_runs(tmp_path, monkeypatch):
    """CLI `--file` 直跑：卡片 object_id 是结果文件路径，侧车只能用文件主名落盘。
    两者不一致时必须在 note 里说出来（详情页按 object_id 取不到这张图）。"""
    # Arrange
    monkeypatch.setenv("QM_EVAL_SERIES_DIR", str(tmp_path))
    loaded = {
        "dates": ["2025-10-16", "2025-10-17"],
        "equity_curve": [1.0, 1.1],
        "path": "data/backtest_results/bt_abc.json",
    }

    # Act
    status = save_curve_sidecar(
        loaded, {}, {}, object_id="data/backtest_results/bt_abc.json"
    )

    # Assert
    assert status["written"] is True
    assert status["object_id"] == "bt_abc"
    assert "侧车 id bt_abc" in str(status["note"])
    assert "取不到这张图" in str(status["note"])
    assert (tmp_path / "strategy" / "bt_abc.json").is_file()


@pytest.mark.unit
def test_save_curve_sidecar_reports_instead_of_raising_without_a_safe_id(
    tmp_path, monkeypatch
):
    """无回测 ID 也无文件路径 → 如实记 note，而不是抛异常把整张卡带垮。"""
    # Arrange
    monkeypatch.setenv("QM_EVAL_SERIES_DIR", str(tmp_path))

    # Act
    status = save_curve_sidecar({}, {}, {}, object_id="")

    # Assert
    assert status["written"] is False
    assert status["bytes"] == 0
    assert "不能作为侧车文件名" in str(status["note"])
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.unit
def test_normalize_symbols_dedupes_and_keeps_order_stable():
    from backend.scripts.eval.strategy_card import normalize_symbols

    assert normalize_symbols(["SH600000", "600000.SH", "sh600000"]) == ["600000.SH"]


@pytest.mark.unit
def test_sample_evenly_spreads_over_the_whole_list():
    """超上限时等距抽样：按代码序取前 N 只等于只查沪市（代码前缀成块）。"""
    from backend.scripts.eval.strategy_card import _sample_evenly

    items = [f"{i:06d}.SZ" for i in range(1000)]

    picked = _sample_evenly(items, 10)

    assert len(picked) == 10
    assert picked[0] == items[0] and picked[-1] == items[-1]
    assert all(p in items for p in picked)
    # 覆盖到列表后半段（前 N 只截断会全部落在前半段）
    assert sum(1 for p in picked if int(p[:6]) >= 500) >= 4
