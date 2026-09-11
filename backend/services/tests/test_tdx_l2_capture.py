"""TDX L2 采集任务单元测试：解析 / 13 因子计算 / 信号分 / 候选池。

纯函数级测试（不触网、不触库）：
- parse_exday_row / parse_snapshot: TQ 原始字段归一化
- compute_l2_factors: VPIN / 时段占比 / 开缺口 / 量价背离 / 流动性 / 冲击衰减
- build_signal_score: ICIR 加权原始分
- _resolve_watchlist: 候选池 + 持仓去重
"""
from backend.services.live_trading.services.tdx_l2_capture_task import (
    FACTOR_ICIR,
    L2SeriesState,
    _resolve_watchlist,
    build_signal_score,
    compute_l2_factors,
    parse_exday_row,
    parse_snapshot,
)


def _exday(**overrides) -> dict:
    """构造 get_exday_data 归一化行（Vol 4×4: 行=特大/大/中/小, 列=买/卖/主买/主卖）。"""
    data = {
        "trade_date": "20260825",
        "cjbs": 12000,
        "b_order": 8000.0,
        "b_cancel": 1000.0,
        "s_order": 5000.0,
        "s_cancel": 500.0,
        "buy_avp": 50.5,
        "sell_avp": 50.6,
        "total_b_order": 40000.0,
        "total_s_order": 30000.0,
        "vol_4x4": [
            [5000, 4000, 6000, 3000],
            [4000, 3500, 4500, 2500],
            [3000, 3000, 3000, 3000],
            [2000, 2500, 1500, 2000],
        ],
        "amo_4x4": [[v * 50 for v in r] for r in [
            [5000, 4000, 6000, 3000],
            [4000, 3500, 4500, 2500],
            [3000, 3000, 3000, 3000],
            [2000, 2500, 1500, 2000],
        ]],
        "vol_num": [],
        "l2_tic_num": 100000,
        "l2_order_num": 50000,
    }
    data.update(overrides)
    return data


def _snap(**overrides) -> dict:
    snap = {
        "now": 50.8,
        "open": 51.2,
        "pre_close": 52.5,
        "volume": 560000.0,
        "amount": 28500000.0,
        "bid5": [7.0, 4.0, 5.0, 3.0, 2.0],
        "ask5": [3.0, 2.0, 2.0, 1.0, 1.0],
        "inside": 300000.0,
        "outside": 260000.0,
    }
    snap.update(overrides)
    return snap


class TestParseExdayRow:
    def test_normalizes_raw_row(self):
        # Arrange
        row = {"Date": "2026-08-25", "CJBS": "12345", "BOrder": "8000", "BCancel": "1000",
               "SOrder": "5000", "SCancel": "500", "BuyAvp": "50.5", "SellAvp": "50.6",
               "TotalBOrder": "40000", "TotalSOrder": "30000", "Vol": [[1]], "Amo": [[2]], "VolNum": []}
        # Act
        d = parse_exday_row(row)
        # Assert
        assert d["trade_date"] == "20260825"
        assert d["cjbs"] == 12345
        assert d["b_order"] == 8000.0
        assert d["vol_4x4"] == [[1]]

    def test_handles_missing_fields(self):
        # Arrange
        row = {"Date": "20260825"}
        # Act
        d = parse_exday_row(row)
        # Assert
        assert d["cjbs"] == 0
        assert d["b_order"] == 0.0
        assert d["vol_4x4"] == []


class TestParseSnapshot:
    def test_extracts_depth_and_ohlc(self):
        # Act
        snap = parse_snapshot({"Value": [{
            "Now": "50.82", "Open": "51.21", "LastClose": "52.52", "Volume": "565567",
            "Amount": "29252831", "Buyv": ["7", "1", "4"], "Sellv": ["36", "63", "2"],
            "Inside": "313869", "Outside": "251699",
        }]})
        # Assert
        assert snap["now"] == 50.82
        assert snap["pre_close"] == 52.52
        assert snap["bid5"] == [7.0, 1.0, 4.0]
        assert snap["ask5"] == [36.0, 63.0, 2.0]
        assert snap["inside"] == 313869.0

    def test_extracts_flat_snapshot_from_bridge(self):
        """桥透传的快照是**平铺** dict（无 Value 包装，实测 2026-09-11；
        仓库文档《券商接入新手指南》亦记录「exday 包 Value，快照平铺」）。
        曾因只认 Value 包装导致快照全丢：now_price 与全部快照类因子为 null/0。"""
        # Act
        snap = parse_snapshot({
            "ErrorId": 0, "Now": "6.51", "Open": "6.60", "LastClose": "6.67",
            "Volume": "2315", "Amount": "15086", "Buyv": ["7", "1", "4"],
            "Sellv": ["36", "63", "2"], "Inside": "1200", "Outside": "1115",
        })
        # Assert：与 Value 包装形状解析结果一致
        assert snap["now"] == 6.51
        assert snap["open"] == 6.60
        assert snap["pre_close"] == 6.67
        assert snap["volume"] == 2315.0
        assert snap["bid5"] == [7.0, 1.0, 4.0]
        assert snap["ask5"] == [36.0, 63.0, 2.0]
        assert snap["inside"] == 1200.0
        assert snap["outside"] == 1115.0

    def test_empty_result(self):
        # Act
        snap = parse_snapshot({})
        # Assert
        assert snap == {}


class TestComputeL2Factors:
    def test_vpin_vol_and_amount_ratio(self):
        # Arrange: 主买占优（列2 和 9000+4500+3000+1500=18000, 列3 和 3000+2500+3000+2000=10500）
        data = _exday()
        state = L2SeriesState()
        snap = _snap()
        # Act: 喂三帧采样（第三次量增加、主买更占优）
        compute_l2_factors(data, snap, state)
        compute_l2_factors(data, snap, state)
        data3 = _exday(
            vol_4x4=[
                [6000, 4000, 9000, 4000],
                [5000, 3500, 7500, 3000],
                [4000, 3000, 5000, 3500],
                [3000, 2500, 3000, 2500],
            ],
            amo_4x4=[[v * 50 for v in r] for r in [
                [6000, 4000, 9000, 4000],
                [5000, 3500, 7500, 3000],
                [4000, 3000, 5000, 3500],
                [3000, 2500, 3000, 2500],
            ]],
        )
        factors = compute_l2_factors(data3, _snap(), state)
        # Assert: Δ买 9500 / Δ卖 2500 → VPIN = 7000/12000 ≈ 0.58，量/额口径一致
        assert factors["micro_vpin_vol_ratio"] is not None
        assert 0.55 < factors["micro_vpin_vol_ratio"] < 0.65
        assert abs(factors["micro_vpin_amount_ratio"] - factors["micro_vpin_vol_ratio"]) < 0.01

    def test_zone_distribution_positive_when_bid_depth_dominates(self):
        # Arrange: 买档深度 > 卖档深度
        data = _exday()
        snap = _snap(bid5=[100.0, 50.0, 30.0, 10.0, 5.0], ask5=[10.0, 5.0, 3.0, 2.0, 1.0])
        state = L2SeriesState()
        # Act
        factors = compute_l2_factors(data, snap, state)
        # Assert
        assert factors["micro_zone_distribution"] > 0.0

    def test_open_gap_from_snapshot(self):
        # Arrange: 开盘 51.2 / 昨收 52.5 → 低开 -2.48%
        factors = compute_l2_factors(_exday(), _snap(), L2SeriesState())
        # Assert
        assert factors["micro_open_gap"] == round((51.2 - 52.5) / 52.5, 6)
        assert factors["micro_open_gap"] < 0

    def test_zone_vol_ratio_after_zone_baseline(self):
        # Arrange: 盘中 10:05（T3 时段内）记录基准，之后总量翻倍 → 时段占比 ≈ 0.5
        state = L2SeriesState()
        low = [[5000, 4000, 6000, 3000]] * 4
        high = [[5000, 4000, 6000, 3000], [5000, 4000, 6000, 3000], [5000, 4000, 6000, 3000], [5000, 4000, 15000, 3000]]
        compute_l2_factors(_exday(vol_4x4=low), _snap(), state, minute_of_day=605)
        assert len(state.zone_baselines) == 1  # T3 基准已记录
        factors = compute_l2_factors(_exday(vol_4x4=high), _snap(), state, minute_of_day=620)
        # Assert: 时段内增量 ≈ 新买入 9000 → 占比 > 0
        assert factors["micro_zone_vol_ratio_T3"] is not None
        assert factors["micro_zone_vol_ratio_T3"] > 0.1

    def test_liquidity_and_rv_ratio_with_price_movement(self):
        # Arrange: 价格序列有波动
        state = L2SeriesState()
        for price in (50.0, 50.2, 50.1, 50.4, 50.3, 50.6, 50.5, 50.8, 50.7, 51.0):
            compute_l2_factors(_exday(), _snap(now=price, pre_close=50.0, open=50.0), state)
        factors = compute_l2_factors(_exday(), _snap(now=51.0, pre_close=50.0, open=50.0), state)
        # Assert: 波动/流动性因子有值且在 [0,10] 截断内
        assert 0.0 <= factors["micro_zone_rv_ratio_close"] <= 10.0
        assert 0.0 <= factors["micro_liquidity_daily_pattern"] <= 10.0

    def test_rv_ratio_returns_none_when_no_valid_prices(self):
        """快照拿不到价（停牌/无行情，now=None → price=0）时不得除零：
        零价样本满 6 帧后 rets 为空列表，min(len)//2 的 half 兜到 1 但
        day_rv = sum(rets)/len(rets) 会 ZeroDivisionError，曾致整轮采集中止
        （2026-09-11 实盘：SZ002227 一只无价票让 26 只候选每分钟全被跳过）。"""
        # Arrange: 全程无有效价（now=None），累计 ≥6 帧
        state = L2SeriesState()
        snap = _snap(now=None)
        for _ in range(8):
            factors = compute_l2_factors(_exday(), snap, state)
        # Assert: 不抛异常，因子保守返回 None（样本不足不误导）
        assert factors["micro_zone_rv_ratio_close"] is None

    def test_flow_revert_speed_and_impact_decay(self):
        # Arrange: 固定两帧采样不足以测自相关 → None（标准化时跳过，不误导）
        state = L2SeriesState()
        compute_l2_factors(_exday(), _snap(), state)
        factors = compute_l2_factors(_exday(), _snap(), state)
        # Assert: 样本不足保守返回 None
        assert factors["flow_imbalance_revert_speed"] is None
        assert factors["micro_impact_decay_half_life"] is None

    def test_divergence_positive_when_price_rises_on_shrinking_volume(self):
        # Arrange: 价涨 + 量缩 → 负相关 → 背离为正
        state = L2SeriesState()
        base = _exday()
        for i in range(10):
            vol_shrink = [[5000, 4000, max(6000 - i * 400, 500), 3000]] * 4
            compute_l2_factors(
                _exday(vol_4x4=vol_shrink), _snap(now=50.0 + i * 0.1), state
            )
        factors = compute_l2_factors(
            _exday(vol_4x4=[[500, 4000, 500, 3000]] * 4), _snap(now=51.0), state
        )
        # Assert: 量价背离为正
        assert factors["vol_price_divergence"] is not None
        assert factors["vol_price_divergence"] > 0.0


class TestBuildSignalScore:
    def test_weights_are_backtest_icir(self):
        # Assert: 13 因子 + 回测 ICIR 权重
        assert len(FACTOR_ICIR) == 13
        assert FACTOR_ICIR["micro_vpin_vol_ratio"] == 0.562

    def test_signal_score_positive_when_factors_bullish(self):
        # Arrange: 构造偏多因子
        factors = {
            "micro_vpin_vol_ratio": 0.35,
            "micro_vpin_amount_ratio": 0.30,
            "micro_zone_distribution": 0.20,
            "micro_zone_vol_ratio_T4": 0.15,
            "micro_zone_vol_ratio_T6": 0.12,
            "vol_price_divergence": 0.10,
            "micro_zone_vol_ratio_T5": 0.12,
            "micro_open_gap": 0.02,
            "micro_impact_decay_half_life": 0.30,
            "micro_liquidity_daily_pattern": 1.0,
            "micro_zone_vol_ratio_T3": 0.10,
            "flow_imbalance_revert_speed": 0.25,
            "micro_zone_rv_ratio_close": 1.2,
        }
        # Act
        score = build_signal_score(factors)
        # Assert
        assert score > 10.0
        assert score < 100.0

    def test_signal_score_zero_on_empty(self):
        # Act
        score = build_signal_score({})
        # Assert
        assert score == 0.0


class TestResolveWatchlist:
    def test_ranks_pool_then_appends_positions(self):
        # Arrange
        scores = {"SH600000": 3.0, "SH600519": 2.5, "SZ000001": 1.0}
        positions = [{"symbol": "SZ300750"}, {"symbol": "SH600519"}]
        # Act
        watch = _resolve_watchlist(scores, positions, [], pool_size=2)
        # Assert
        assert watch[:2] == ["SH600000", "SH600519"]  # Top2
        assert "SZ300750" in watch  # 持仓补充
        assert watch.count("SH600519") == 1  # 去重

    def test_respects_max_watchlist(self):
        # Arrange
        scores = {f"SH{s:06d}": 3.0 for s in range(60)}
        # Act
        watch = _resolve_watchlist(scores, [], [], pool_size=50)
        # Assert
        assert len(watch) <= 50
