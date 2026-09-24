"""执行损耗（TCA）口径的单元测试——**符号约定是这份文件的核心契约**。

背景：本仓此前只能回答"模型选得对不对"（研究评分卡）与"模拟盘撮合得像不像"
（``fill_quality.fidelity_metrics``，F2 保真度），**没有**一层回答"真单实际花了
多少执行成本"。补这一层的第一件事不是写报告，是**把符号约定钉死并只钉一次**。

本文件的每一条断言都在钉这个约定，不是形式主义：

* 买入：成交价高于基准 = 差（+）；
* 卖出：成交价低于基准 = 差（+）——**两侧正号同义**（都表示吃亏），
  报告因此可以直接说"这批单平均 +12.3 bps"而不必分方向解释；
* 缓冲用尽度：0 = 按基准成交，100 = 成交在限价上（把缓冲吃满），负 = 优于基准；
* 任何缺项/脏值 → ``None``（报告按"不可定价"计数，**绝不吐 NaN，也绝不冒充 0**）。

``0 bps`` 与 ``None`` 在机构语境里是两件事：前者说"这笔执行与基准分毫不差"，
后者说"这笔我们不知道"。合并二者 = 报告撒谎。

**与 F2 保真度的关系**：``fill_quality`` 里那条 ``direction×(成交/参考−1)×1e4``
与本模块的 :func:`slip_bps` 是**同一个公式**（差别只在基准价取谁：F2 取当日收盘、
TCA 取决策时点参考价）。所以公式只实现在这里一处，F2 反向引用本模块——见
``test_slip_bps_is_the_only_implementation_of_the_sign_convention``。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------- slip_bps


def test_buy_paying_more_than_reference_is_positive():
    """买入成交价高于基准 = 比基准差 = 正号（不管"价格涨了"本身是好事还是坏事）。"""
    from backend.shared.exec_cost import slip_bps

    assert slip_bps("buy", 10.0, 10.1) == pytest.approx(100.0)
    assert slip_bps("buy", 10.0, 9.9) == pytest.approx(-100.0)


def test_sell_selling_lower_than_reference_is_positive():
    """卖出成交价低于基准 = 比基准差 = 正号。**两侧正号同义**（都表示吃亏）。"""
    from backend.shared.exec_cost import slip_bps

    assert slip_bps("sell", 10.0, 9.9) == pytest.approx(100.0)
    assert slip_bps("sell", 10.0, 10.1) == pytest.approx(-100.0)


def test_slip_bps_is_scale_invariant():
    """同样的相对偏差，价格量级不同（5 元 vs 130 元）bps 必须一致。"""
    from backend.shared.exec_cost import slip_bps

    a = slip_bps("buy", 5.0, 5.05)
    b = slip_bps("buy", 130.0, 131.3)
    assert a is not None and b is not None
    assert abs(a - b) < 1e-9


def test_slip_bps_rejects_bad_input():
    """缺项/零价/非数字/未知方向 → None（不是 0，不是异常）。"""
    from backend.shared.exec_cost import slip_bps

    assert slip_bps("buy", None, 10.0) is None
    assert slip_bps("buy", 10.0, None) is None
    assert slip_bps("buy", 0, 10.0) is None
    assert slip_bps("buy", 10.0, 0) is None
    assert slip_bps("buy", "abc", 10.0) is None
    assert slip_bps("short", 10.0, 10.0) is None
    assert slip_bps("buy", float("nan"), 10.0) is None
    assert slip_bps("buy", 10.0, float("inf")) is None
    assert slip_bps("", 10.0, 10.0) is None


def test_slip_bps_accepts_uppercase_and_padded_side():
    """方向大小写/空白不该让一笔真实成交掉出样本（归一化在这一处做）。"""
    from backend.shared.exec_cost import slip_bps

    assert slip_bps("BUY", 10.0, 10.1) == pytest.approx(100.0)
    assert slip_bps(" Sell ", 10.0, 9.9) == pytest.approx(100.0)


# ---------------------------------------------------------------- cushion_used_bps


def test_cushion_used_is_zero_at_reference_and_100_at_limit():
    """缓冲用尽度：按基准成交 0，成交在限价上 100——买卖两侧同式。"""
    from backend.shared.exec_cost import cushion_used_bps

    assert cushion_used_bps("buy", 10.0, 10.1, 10.0) == pytest.approx(0.0)
    assert cushion_used_bps("buy", 10.0, 10.1, 10.1) == pytest.approx(100.0)
    assert cushion_used_bps("sell", 10.0, 9.9, 10.0) == pytest.approx(0.0)
    assert cushion_used_bps("sell", 10.0, 9.9, 9.9) == pytest.approx(100.0)


def test_cushion_used_can_be_negative_when_fill_beats_reference():
    """优于基准成交 → 负值（买入成交在基准下方 / 卖出成交在基准上方）。"""
    from backend.shared.exec_cost import cushion_used_bps

    assert cushion_used_bps("buy", 10.0, 10.1, 9.95) == pytest.approx(-50.0)
    assert cushion_used_bps("sell", 10.0, 9.9, 10.05) == pytest.approx(-50.0)


def test_cushion_used_rejects_no_buffer():
    """零缓冲（限价==基准）不可定义用尽度 → None，不是除零崩。"""
    from backend.shared.exec_cost import cushion_used_bps

    assert cushion_used_bps("buy", 10.0, 10.0, 10.0) is None
    assert cushion_used_bps("buy", None, 10.1, 10.0) is None
    assert cushion_used_bps("buy", 10.0, None, 10.0) is None
    assert cushion_used_bps("buy", 10.0, 10.1, None) is None


def test_cushion_used_has_opposite_sign_to_slip_when_inside_the_buffer():
    """**缓冲内的成交**：滑点为正（比基准差）时，缓冲用尽度必为正（吃掉了缓冲）。

    两条口径必须能互相校验——这正是把两个函数放在一个模块里的理由：任何一处
    符号写反，这条断言会红。
    """
    from backend.shared.exec_cost import cushion_used_bps, slip_bps

    ref, limit, fill = 10.0, 10.2, 10.1        # 买：成交在基准与限价之间
    assert slip_bps("buy", ref, fill) > 0
    assert cushion_used_bps("buy", ref, limit, fill) > 0


# ---------------------------------------------------------------- 唯一出处


def test_slip_bps_is_the_only_implementation_of_the_sign_convention():
    """F2 保真度（``fill_quality``）必须引用本模块的公式，不得各写一份。

    同一条"方向 × (成交/参考 − 1) × 1e4"在两处各写一遍，就是两处可以**分别**
    改错的地方：F2 那条喂"模拟保真度"看板、TCA 这条喂"执行成本"报告，符号一旦
    分叉，两块面板会同时给出相反的故事，且各自的自测都还是绿的。

    这条断言同时钉住**数值一致性**（两处对同一输入给同一答案）。
    """
    from backend.shared import fill_quality
    from backend.shared.exec_cost import slip_bps

    assert fill_quality.slip_bps is slip_bps, (
        "fill_quality 必须从 exec_cost 导入 slip_bps（唯一出处），不得自带实现"
    )

    rows = [
        # 两侧各 +100 bps：买在基准上方 1%、卖在基准下方 1%（都表示吃亏）
        {"symbol": "600036.SH", "side": "buy", "filled_quantity": 100,
         "fill_price": 10.1, "quantity": 100, "status": "filled"},
        {"symbol": "000001.SZ", "side": "sell", "filled_quantity": 100,
         "fill_price": 11.88, "quantity": 100, "status": "filled"},
    ]
    out = fill_quality.fidelity_metrics(
        rows, reference_prices={"600036.SH": 10.0, "000001.SZ": 12.0}
    )
    dev = out["price_deviation_bps"]
    assert dev["n"] == 2
    assert dev["median"] == pytest.approx(100.0)   # 买 +100bps、卖 +100bps → 中位 +100


# ---------------------------------------------------------------- 路径归属


def test_tca_path_reads_the_client_order_id_first():
    """路径归属**首看幂等号前缀**：它不会被成交回报改写。"""
    from backend.shared.exec_cost import tca_path

    assert tca_path("mir-600036.SH-20260924", None, None) == "mirror"
    assert tca_path("sltp-600036.SH-20260924-g1", None, None) == "sltp"
    assert tca_path("trim-600036.SH-20260924-g1", None, None) == "trim"
    assert tca_path("flat-600036.SH-20260924", None, None) == "flatten"
    assert tca_path("flatten-600036.SH-20260924", None, None) == "flatten"
    assert tca_path("manual-600036.SH-1", None, None) == "manual"
    assert tca_path("auto-600036.SH-1", None, None) == "hosted"


def test_tca_path_matches_the_real_builders():
    """前缀表必须与**真正造幂等键的那几个函数**对得上——手抄一份前缀表，
    迟早有一次是抄着旧版抄的，而错法无声：那批单会静默落进 unknown 分组。"""
    from backend.shared.exec_cost import tca_path
    from backend.shared.order_contract import (
        build_candidate_client_order_id,
        build_copilot_client_order_id,
        build_llm_decision_client_order_id,
    )

    assert tca_path(
        build_llm_decision_client_order_id("rnd-20260924-am", "600036.SH", "buy"),
        None, None,
    ) == "llm_decision"
    assert tca_path(
        build_candidate_client_order_id("b1", "600036.SH", "buy"), None, None
    ) == "candidate_push"
    assert tca_path(
        build_copilot_client_order_id("a1", "600036.SH", "sell"), None, None
    ) == "co_pilot"


def test_tca_path_falls_back_to_remarks_then_agent():
    """幂等号认不出时的降级链：备注前缀 → agent → unknown。

    **备注不可依赖**：``apply_execution_report`` 会拿券商回报的消息**覆盖**
    ``orders.remarks``（成交回报到达后原始备注就没了），所以备注只能是次选，
    而它的失效方向必须是"掉进 unknown"——绝不是"猜成另一条腿"。
    """
    from backend.shared.exec_cost import tca_path

    # 老单/无幂等号：备注还在时认得出
    assert tca_path(None, "sltp:保护性止损", None) == "sltp"
    assert tca_path("", "trim:减仓执行器", None) == "trim"
    assert tca_path("", "forced-exit:平仓清单", None) == "flatten"
    assert tca_path("", "mirror:submit", None) == "mirror"
    # 备注已被成交回报覆盖、只剩 agent（只有 LLM 腿有 agent）
    assert tca_path("", "成交回报", "deepseek-v4-flash") == "llm_decision"
    # 三样都认不出
    assert tca_path(None, "", None) == "unknown"
    assert tca_path(None, "成交回报", None) == "unknown"


def test_tca_path_is_not_fooled_by_overwritten_remarks():
    """**回归钉**：备注被回报覆盖后，路径仍由幂等号定死。

    这是本仓特有的坑：隔壁的路径写在成交流水行的 ``tca_path`` 字段里，永远不变；
    本仓从 ``orders`` 行反推，而 ``remarks`` 是**会被改写的活字段**。若哪天有人
    把优先级反过来（备注优先），一条 ``mir-`` 单在成交后会从 mirror 组跳到 unknown，
    同一笔单在两个口径下各出现一次，报告的分组统计当场失真。
    """
    from backend.shared.exec_cost import tca_path

    assert tca_path("mir-600036.SH-20260924", "成交回报", None) == "mirror"
    assert tca_path("lld-rnd-20260924-am-600036.SH-buy", "已成交", "x") == "llm_decision"


# ---------------------------------------------------------------- 合并同单多笔


def test_merge_partial_fills_of_same_order_weight_by_volume():
    """同委托号的多笔部分成交 = 同一笔单：按量加权合成一个样本。

    分部成交（100@10.0 + 200@10.3）合成 300@10.2，而不是算成两笔再取平均 10.15——
    后者把小单的口径放大成了与大单同权。
    """
    from backend.shared.exec_cost import merge_orders

    rows = [
        {"order_id": "o1", "side": "buy", "fill_px": 10.0, "filled": 100, "ref_px": 10.0,
         "limit_px": 10.1, "ts": "2026-09-24T09:40:00+08:00", "path": "mirror"},
        {"order_id": "o1", "side": "buy", "fill_px": 10.3, "filled": 200, "ref_px": 10.0,
         "limit_px": 10.1, "ts": "2026-09-24T09:45:00+08:00", "path": "mirror"},
    ]
    merged = merge_orders(rows)
    assert len(merged) == 1
    assert merged[0]["filled"] == 300
    assert merged[0]["fill_px"] == pytest.approx(10.2)
    assert merged[0]["ts"] == "2026-09-24T09:40:00+08:00"      # 取首次成交
    assert merged[0]["limit_px"] == 10.1                        # 身份字段随首笔


def test_merge_keeps_rows_without_order_id_separate():
    """对不上委托号的成交（桥没给号）不能互相合并——那是不同的单，只是都没号。"""
    from backend.shared.exec_cost import merge_orders

    rows = [{"order_id": "", "side": "buy", "fill_px": 10.0, "filled": 100, "ts": "t1"},
            {"order_id": "", "side": "buy", "fill_px": 9.0, "filled": 100, "ts": "t2"}]
    assert len(merge_orders(rows)) == 2


def test_merge_sums_fees_like_it_sums_quantity():
    """手续费**可加**：分部成交的各笔费用必须并进同一行。

    回归钉：``fees`` 一度既不在 ``_IDENTITY_FIELDS`` 里也不求和——合并时**静默丢掉**
    第一笔之外的所有费用，报告里"带费用的笔数/费用合计"随之少算，而少算的方向恰好是
    "看起来更便宜"（与成交量同类，必须按加和合并，不是取首笔）。
    """
    from backend.shared.exec_cost import merge_orders

    rows = [
        {"order_id": "o1", "side": "buy", "fill_px": 10.0, "filled": 100, "fees": 1.5},
        {"order_id": "o1", "side": "buy", "fill_px": 10.3, "filled": 200, "fees": 2.25},
    ]
    merged = merge_orders(rows)
    assert merged[0]["fees"] == pytest.approx(3.75)
    # 缺 fees 键的行不被凭空补出费用（0 与 "没这个字段" 是两件事）
    assert "fees" not in merge_orders([{"order_id": "o2", "filled": 100}])[0]


def test_merge_is_stable_on_single_rows_and_empty():
    """单笔/空输入是恒等变换：本函数在读数链上每跑一次，行数不该无故变。"""
    from backend.shared.exec_cost import merge_orders

    assert merge_orders([]) == []
    one = [{"order_id": "o1", "side": "buy", "fill_px": 10.0, "filled": 100}]
    assert merge_orders(one) == one


# ---------------------------------------------------------------- summarize


def _sample(side="buy", ref=10.0, fill=10.1, filled=100, limit=None,
            path="mirror", order_id="o1", code="600036.SH"):
    return {
        "order_id": order_id, "side": side, "code": code, "ref_px": ref,
        "fill_px": fill, "limit_px": limit, "filled": filled, "wanted": filled,
        "path": path, "ts": "2026-09-24T09:45:00+08:00", "date": "2026-09-24",
    }


def test_summarize_weights_by_notional_not_by_count():
    """一笔 1 万股的大单 与 一笔 100 股的小单：大单必须主导加权均值。

    "钱花在哪"问的是钱：小单滑点再大，也不该和一个仓位级别的单等权。
    """
    from backend.shared.exec_cost import summarize

    rows = [_sample(fill=10.1, filled=10000),      # +100 bps，名义 10.1 万
            _sample(fill=9.0, filled=100, order_id="o2")]   # −1000 bps，名义 900
    s = summarize(rows)
    assert s["n"] == 2
    assert s["slip_bps_w"] > 0                        # 大单方向说话
    assert s["slip_bps_mean"] == pytest.approx(-450.0)   # 简单均值仍是两者中点


def test_summarize_reports_n_only_for_priced_samples():
    """没有基准价的行不进滑点样本，但要在覆盖里能被看见（n_unpriced）。"""
    from backend.shared.exec_cost import summarize

    s = summarize([_sample(), _sample(ref=None, order_id="o2")])
    assert s["n"] == 1 and s["n_unpriced"] == 1
    assert s["slip_bps_w"] == pytest.approx(100.0)
    assert s["n_rows"] == 2


def test_summarize_median_and_percentiles_are_robust_to_one_outlier():
    """中位数/p10/p90 用来说明分布：一笔极端值不许把中位拽走。"""
    from backend.shared.exec_cost import summarize

    rows = [_sample(fill=10.0, order_id=f"o{i}") for i in range(3)]
    rows.append(_sample(fill=12.0, order_id="o9"))    # +2000 bps 一笔
    s = summarize(rows)
    assert s["slip_bps_med"] == 0.0
    assert s["slip_bps_p90"] > 0


def test_summarize_empty_is_all_none_not_zero():
    """空样本 = 全部 None（报告据此印"无样本"），不许印 0.00 bps。"""
    from backend.shared.exec_cost import summarize

    s = summarize([])
    assert s["n"] == 0 and s["slip_bps_w"] is None and s["slip_bps_med"] is None
    assert s["fill_rate"] is None


def test_summarize_reports_latency_and_notional_without_any_priced_sample():
    """**回归钉**：一笔都不可定价时，延迟与成交额照样要出数。

    这两项与"有没有基准价"无关。挂在可定价样本上算的后果是静默丢数：某条腿没记
    基准价（或某个窗口的基准价整体缺失）时，那一组的延迟与成交额一起变空白，
    而空白读起来像"没采到"，不像"没算"。
    """
    from backend.shared.exec_cost import summarize

    row = _sample(ref=None)
    row["decided_ts"] = "2026-09-24T01:35:01+00:00"
    row["submit_ts"] = "2026-09-24T01:41:10+00:00"
    row["fill_ts"] = "2026-09-24T01:41:14+00:00"
    s = summarize([row])
    assert s["n"] == 0 and s["n_unpriced"] == 1
    assert s["decide_to_submit_min_med"] == pytest.approx(6.15, abs=0.01)
    assert s["submit_to_fill_min_med"] == pytest.approx(0.07, abs=0.01)
    assert s["notional"] == pytest.approx(10.1 * 100)


def test_summarize_counts_fill_rate_denominator():
    """成交率：成交笔数 / (成交笔数 + 零成交终态笔数)，口径写在这一个函数里。"""
    from backend.shared.exec_cost import summarize

    s = summarize([_sample(order_id="o1"), _sample(order_id="o2")], zero_fill_orders=2)
    assert s["n_orders"] == 4
    assert s["fill_rate"] == pytest.approx(0.5)


def test_summarize_latency_from_round_level_decided_ts():
    """决策→提交延迟：决策时刻是轮级的（同轮各单共用），报告必须说清粒度。"""
    from backend.shared.exec_cost import summarize

    row = _sample()
    row["decided_ts"] = "2026-09-24T09:35:01+08:00"
    row["submit_ts"] = "2026-09-24T09:41:10+08:00"
    s = summarize([row])
    assert s["decide_to_submit_min_med"] == pytest.approx(6.15, abs=0.01)


def test_summarize_groups_by_path_and_side():
    """按路径/方向分组：两条腿的成交机制不同（镜像走盘口、止损走保护价），
    混算会把差异洗掉。分组是**递归同式**的（组内不再分组），故键形态可预测。"""
    from backend.shared.exec_cost import summarize

    rows = [_sample(path="mirror"),
            _sample(side="sell", path="sltp", order_id="o2"),
            _sample(side="sell", path="sltp", order_id="o3")]
    g = summarize(rows)["groups"]
    assert set(g) == {"mirror/buy", "sltp/sell"}
    assert g["sltp/sell"]["n"] == 2
    assert "groups" not in g["sltp/sell"]


def test_summarize_latency_accepts_z_suffixed_stamps():
    """延迟统计必须认 ``Z`` 结尾的时刻——本仓 JSON 出口（``to_utc_iso``）印的就是 Z，
    而 Python 3.10 的 ``fromisoformat`` 不认它。不认的后果是**静默全空**：
    报告里"延迟"那一格没有数，读的人只会以为"没采到"，不会想到是解析失败。"""
    from backend.shared.exec_cost import summarize

    row = _sample()
    row["decided_ts"] = "2026-09-24T01:35:01Z"      # = 北京 09:35
    row["submit_ts"] = "2026-09-24T01:41:10Z"       # = 北京 09:41
    assert summarize([row])["decide_to_submit_min_med"] == pytest.approx(6.15, abs=0.01)


# ---------------------------------------------------------------- 取数层（纯部分）


def test_cst_day_bounds_are_naive_beijing_days():
    """窗口是**北京日**的零点到零点（naive）。

    本仓有两套 naive 时刻：``sim_trades`` 是 naive UTC、``trades/orders`` 是 naive
    北京时间。窗口取错一种，报告就会把早盘头半小时（北京 09:30 = UTC 01:30）算进
    前一天——而那正是买单最集中的时段。
    """
    from datetime import date

    from backend.shared.exec_cost_source import cst_day_bounds

    start, end = cst_day_bounds(1, date(2026, 9, 24))
    assert start == datetime(2026, 9, 24, 0, 0)
    assert end == datetime(2026, 9, 25, 0, 0)
    assert start.tzinfo is None and end.tzinfo is None

    start7, _ = cst_day_bounds(7, date(2026, 9, 24))
    assert start7 == datetime(2026, 9, 18, 0, 0)          # 含今天共 7 天

    earliest, _ = cst_day_bounds(0, date(2026, 9, 24))    # 0 = 不设下界
    assert earliest == datetime.min


def test_sample_reads_fill_at_half_past_midnight_as_the_same_beijing_day():
    """跨零点那半小时：北京 00:30 的成交属于**当天**，不是前一天。

    这是 naive-CST→aware-UTC 转换最容易错的地方（00:30 CST = 前一天 16:30 UTC）：
    按 UTC 归日，一笔凌晨成交会被算进前一天的日报，而当天的日报少一笔。
    """
    from backend.shared.exec_cost_source import sample_from_report_row

    sample = sample_from_report_row(
        {
            "order_id": "o1", "symbol": "600036.SH", "side": "BUY",
            "quantity": 100, "price": 10.0,
            "executed_at": datetime(2026, 9, 24, 0, 30, 0),
            "exchange_trade_id": "T1",
        }
    )
    assert sample["date"] == "2026-09-24"
    assert sample["ts"].startswith("2026-09-23T16:30:00")   # aware UTC 串
    assert sample["side"] == "buy"                           # 方向归一小写


def test_sample_maps_order_side_fields_and_counts_synth_prices():
    """订单侧字段逐项落位；合成成交（柜面均价）单独标记，不剔除也不冒充明细。"""
    from backend.shared.exec_cost_source import TCA_SAMPLE_FIELDS, sample_from_report_row

    sample = sample_from_report_row(
        {
            "order_id": "o1", "symbol": "600036.SH", "side": "buy",
            "quantity": 300, "price": 33.0, "commission": 5.0, "stamp_duty": 0.0,
            "executed_at": datetime(2026, 9, 24, 9, 45, 0),
            "exchange_trade_id": "qmt-synth-123",
            "client_order_id": "mir-600036.SH-20260924",
            "remarks": "成交回报", "agent": None,
            "limit_price": 33.4, "wanted": 300,
            "submitted_at": datetime(2026, 9, 24, 9, 41, 10),
            "decided_at": datetime(2026, 9, 24, 1, 35, 1, tzinfo=timezone.utc),
            "ref_price": 33.069,
        }
    )
    assert set(sample) == set(TCA_SAMPLE_FIELDS), "样本键变了就要同步改报告口径"
    assert sample["limit_px"] == 33.4 and sample["wanted"] == 300
    assert sample["ref_px"] == pytest.approx(33.069)
    assert sample["path"] == "mirror"                         # 幂等号说话
    assert sample["price_source"] == "synth"
    assert sample["fees"] == pytest.approx(5.0)
    assert sample["submit_ts"].startswith("2026-09-24T01:41:10")
    assert sample["decided_ts"].startswith("2026-09-24T01:35:01")


def test_sample_labels_full_detail_prices_and_missing_ref():
    """明细成交标 detail；缺基准价的样本如实留 None（聚合层据此计入不可定价）。"""
    from backend.shared.exec_cost_source import sample_from_report_row

    sample = sample_from_report_row(
        {
            "order_id": "o2", "symbol": "000001.SZ", "side": "sell",
            "quantity": 100, "price": 9.9, "executed_at": datetime(2026, 9, 24, 10, 0, 0),
            "exchange_trade_id": "REAL-9",
            "client_order_id": "sltp-000001.SZ-20260924-g1",
            "limit_price": None, "ref_price": None,
        }
    )
    assert sample["price_source"] == "detail"
    assert sample["ref_px"] is None and sample["limit_px"] is None
    assert sample["path"] == "sltp"
    assert sample["decided_ts"] == ""      # 拼不上决策账 → 空串，不是 None


def test_samples_sql_actually_selects_the_reference_price():
    """基准价必须出现在取数 SQL 的 SELECT 里。

    回归钉：``sample_from_report_row`` 读 ``row["ref_price"]``，而 SQL 少选一列**不会
    报任何错**——只会让每一笔都落进"不可定价"：报告照常出、数字永远空着，读起来像
    "还没采到"而不是"代码少了一列"。本仓真实发生过（契约列与 SELECT 都还没补时，
    73/73 全是缺基准价）。
    """
    import re

    from backend.shared.exec_cost_source import _SQL_SAMPLES

    assert re.search(r"\bo\.ref_price\b", _SQL_SAMPLES), "取数 SQL 没选 orders.ref_price"


def test_reference_price_is_wired_through_every_hop():
    """TCA 基准价的**接线**逐段点名（源断言）：契约链五段 + **每一条真单腿**一行。

    这是一个**静默失败**型字段：任何一段断了都不报错，只是执行损耗报告永远"不可
    定价"（或更坏——镜像腿把昨收当成了决策价，滑点里混进隔夜跳空）。段与段之间没有
    类型/运行时约束，故把这条链写成一份可点名的清单：哪段被删，测试就指出哪段。

    1. 契约列（启动期自愈的 DDL + 新装 db_init.sql）
    2. ORM 模型 + ``OrderCreate`` schema（create_all 路径与 API 入口）
    3. ``OrderService.create_order`` 入库
    4. 派发器透传（所有内部腿的唯一 REAL 漏斗）
    5. 镜像腿赋值（决策腿的价，不是昨收）
    6. 其余四条腿各自的赋值 —— 每条腿的"决策价"是哪个变量都不一样，删掉任何一条
       都只表现为该腿的成交永远不可定价，故逐腿点名（字面量在各自文件里唯一，
       grep 得到 1 处才算这条腿还在）。

    行为层的覆盖：``_positive_or_none`` 的用例在
    ``test_internal_strategy_dispatcher_sim.py``；映射在
    ``test_sample_maps_order_side_fields_and_counts_synth_prices``。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    def src(rel: str) -> str:
        return (root / rel).read_text(encoding="utf-8")

    from backend.shared.order_contract import ORDER_COLUMNS

    assert ("ref_price", "DOUBLE PRECISION") in ORDER_COLUMNS, "契约列缺 ref_price"

    ddl = src("shared/db_init.sql")
    orders_block = ddl.split("CREATE TABLE IF NOT EXISTS orders (", 1)[1].split(");", 1)[0]
    assert re.search(r"\bref_price\s+DOUBLE PRECISION", orders_block), (
        "db_init.sql 的 orders 建表段缺 ref_price（新装库不会有这一列）"
    )

    assert re.search(r"ref_price\s*=\s*Column\(Float", src("services/trade_shared/models/order.py")), (
        "trade Order 模型缺 ref_price"
    )
    assert re.search(r"ref_price\s*:\s*float \| None", src("services/trade_shared/schemas/order.py")), (
        "OrderCreate 缺 ref_price（API 入口传不进来）"
    )
    assert "ref_price=order_data.ref_price" in src(
        "services/trade_shared/services/order_service.py"
    ), "OrderService.create_order 没把 ref_price 写进 Order"
    assert "ref_price=_positive_or_none(" in src(
        "services/live_trading/services/internal_strategy_dispatcher.py"
    ), "派发器没透传 ref_price（镜像/强平/人工腿全部落空）"
    mirror = src("services/live_trading/services/real_mirror_service.py")
    assert '"ref_price": tca_ref_price' in mirror, "镜像腿没给真单带基准价"
    assert "tca_ref_price = live_price if live_price > 0 else None" in mirror, (
        "镜像腿的基准价必须是**决策腿当时的价**（载荷 price）；改成昨收 ref_price "
        "会把隔夜跳空算进滑点"
    )

    #: 每条腿：(腿名, 相对路径, 那一行的字面量, 为什么是**这个**变量而不是隔壁那个)
    legs = [
        (
            "止损-触发",
            "services/live_trading/services/sltp_executor.py",
            '"ref_price": price,',
            "触发时现价（`price` 就是触发那一刻读到的价）",
        ),
        (
            "止损-重挂",
            "services/live_trading/services/sltp_executor.py",
            '"ref_price": ref,',
            "本次重挂依据的价 `ref`（不是 `order_price`——那是派生出的保护价）",
        ),
        (
            "减仓",
            "services/trade/services/leverage_trim_submit.py",
            '"ref_price": float(plan_leg.price),',
            "计划时看到的价 `plan_leg.price`（不是喂给保护价函数的产物 `price`）",
        ),
        (
            "人工",
            "services/live_trading/services/manual_execution_service.py",
            '"ref_price": preview_price,',
            "预览价（不是 `price`——人工腿的 `price` 同样是保护限价）",
        ),
        (
            "强平脚本",
            "scripts/qmt_flatten_positions.py",
            '"ref_price": ref_price,',
            "QuantDB 最近收盘（不是派生限价 `limit = 收盘 × (1 − limit_pct)`）",
        ),
    ]
    for name, rel, literal, why in legs:
        body = src(rel)
        assert body.count(literal) == 1, f"{name}腿的基准价赋值不见了（{rel}）：{why}"



@pytest.mark.integration
def test_load_samples_runs_against_the_real_ledger():
    """真库冒烟：SQL 能跑、覆盖计数齐、窗口天数不超界。

    这条**不是**在断言某个具体数字（账本每天在变），而是在断言这份 SQL 与真表结构
    对得上：列名、枚举转型（``status::text``）、以及决策账 LEFT JOIN 都不炸。
    拿不到数据时（空库）也必须返回一个结构完整的 ``LoadResult``。
    """
    import asyncio

    from backend.shared.exec_cost_source import TCA_SAMPLE_FIELDS, load_samples

    result = asyncio.run(
        load_samples(days=0, tenant_id="default", user_id="10000001")
    )
    assert isinstance(result.coverage, dict)
    assert set(result.coverage) >= {
        "n_trades", "n_orphan", "n_not_real", "n_synth", "n_missing_ref", "n_orders",
    }
    assert result.coverage["n_trades"] >= len(result.samples)
    for sample in result.samples:
        assert set(sample) == set(TCA_SAMPLE_FIELDS)
        assert sample["date"], "每笔成交都要能归到某个北京日"
        assert sample["filled"] > 0
