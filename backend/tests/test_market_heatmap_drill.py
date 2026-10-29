"""板块 → 成分个股热力矩形图下钻（/heatmap/drill）的集成测试。

真实数据口径（QuantDB 本地 parquet），与 test_quantdb_market_analysis.py 同风格：
不 mock 数据层，直接验证三类板块体系（二级/一级/概念）的下钻结果自洽性：
- 面积口径 = 个股总市值（亿），与板块层 total_mv 同源，父子面积可对齐；
- 主力净额 = flow_super_net + flow_large_net（元→亿），与 flow_panorama 的「主力」口径一致；
- 截断门控 MAX_DRILL_ITEMS：概念板块（可 >1000 只）必须如实披露 truncated；
- 非法入参（不存在板块/空名/非法分类）一律返回 None → 路由层 404。
"""

import math

import pytest

from backend.services.api.market_analysis import quantdb_feed

# 三类体系各取一个成分规模适中的板块：既能验证有值，又不至于让测试读取过慢
DRILL_CASES = [
    ("shenwan", "半导体"),
    ("industry1", "化学制品"),
    ("concept", "人工智能"),
]


@pytest.fixture(autouse=True)
def _clear_cache():
    """下钻走 _cached（30min TTL），用例间必须清缓存，避免拿到上条用例的结果。"""
    quantdb_feed.clear_cache()
    yield
    quantdb_feed.clear_cache()


@pytest.mark.parametrize("category,name", DRILL_CASES)
def test_sector_drill_shape_and_self_consistency(category, name):
    """下钻返回结构完整，且 summary 与 items 逐项自洽（无凭空数字）。"""
    data = quantdb_feed.get_sector_drill(category=category, name=name)
    assert data is not None, f"{category}/{name} 无下钻数据"

    assert data["category"] == category
    assert data["sector_name"] == name
    assert data["category_label"]
    # 交易日格式 YYYY-MM-DD
    assert len(data["trade_date"]) == 10 and data["trade_date"][4] == "-"

    items = data["items"]
    assert isinstance(items, list) and items, "成分股不应为空"
    for it in items:
        assert set(it) >= {
            "symbol",
            "name",
            "value",
            "pct_change",
            "flow_net",
            "is_leader",
        }
        # 前端规范：symbol 为前缀式（如 SH600036），面积规模为正
        assert it["symbol"][:2] in {"SH", "SZ", "BJ"}
        assert it["value"] > 0
        assert math.isfinite(it["pct_change"])
        assert math.isfinite(it["flow_net"])

    # is_leader 恰好一个，且是面积（市值）第一档
    leaders = [it for it in items if it["is_leader"]]
    assert len(leaders) == 1
    assert items[0]["is_leader"], "面积最大者才是领头权重的标注对象"

    summary = data["summary"]
    assert summary["shown"] == len(items)
    assert summary["shown"] <= quantdb_feed.MAX_DRILL_ITEMS
    assert summary["total"] >= summary["shown"]
    assert summary["truncated"] == (summary["total"] > quantdb_feed.MAX_DRILL_ITEMS)
    # 涨跌家数计入 shown（渲染范围内），三者相加 = shown
    assert summary["advance"] + summary["decline"] + summary["flat"] == summary["shown"]
    # total_mv_yi 等于 items 面积求和（口径自洽，非另算一份）
    assert summary["total_mv_yi"] == pytest.approx(
        round(sum(it["value"] for it in items), 1), abs=0.5
    )


def test_sector_drill_concept_truncation_is_disclosed():
    """概念板块成员可达千只：必须按 MAX_DRILL_ITEMS 截断并如实披露，而不是静默截断。"""
    data = quantdb_feed.get_sector_drill(category="concept", name="人工智能")
    assert data is not None
    s = data["summary"]
    assert s["total"] > quantdb_feed.MAX_DRILL_ITEMS, (
        "该概念应超过截断阈值，否则本用例失效"
    )
    assert s["truncated"] is True
    assert s["shown"] == quantdb_feed.MAX_DRILL_ITEMS
    # 截断即按市值降序：items 应单调不增
    vals = [it["value"] for it in data["items"]]
    assert vals == sorted(vals, reverse=True)


def test_sector_drill_historical_date():
    """date 参数应锚定历史交易日（下钻可回溯），而不是永远返回最新。"""
    latest = quantdb_feed.get_sector_drill(category="shenwan", name="半导体")
    assert latest is not None
    latest_date = latest["trade_date"]

    days = quantdb_feed._get_partition_dates("1_kline_data/daily_unadjusted")
    if len(days) < 2:
        pytest.skip("分区不足两个交易日，无法验证历史回溯")
    prev = days[1]
    prev_iso = f"{prev[:4]}-{prev[4:6]}-{prev[6:]}"

    hist = quantdb_feed.get_sector_drill(
        category="shenwan", name="半导体", date=prev_iso
    )
    assert hist is not None
    assert hist["trade_date"] == prev_iso
    assert hist["trade_date"] != latest_date
    # 同一板块成分数不随交易日变化（成分表是截面静态的），但涨跌幅应不同
    assert hist["summary"]["total"] == latest["summary"]["total"]


def test_sector_drill_invalid_inputs_return_none():
    """不存在板块 / 空名 / 非法分类 → None（路由层据此 404，前端回退顶层）。"""
    assert (
        quantdb_feed.get_sector_drill(category="shenwan", name="不存在的板块XYZ")
        is None
    )
    assert quantdb_feed.get_sector_drill(category="shenwan", name="   ") is None
    assert quantdb_feed.get_sector_drill(category="bogus", name="半导体") is None


def test_sector_drill_flow_matches_super_plus_large():
    """主力净额口径 = flow_super_net + flow_large_net（与 flow_panorama 一致），
    并非 flow_net_amount（该列是东财口径，与主力定义不同）。"""
    data = quantdb_feed.get_sector_drill(category="shenwan", name="半导体")
    assert data is not None
    items = data["items"]
    # 板块级主力净额应等于成分求和（都是 super+large 的当日值，单位同为亿元）
    total_flow = sum(it["flow_net"] for it in data["items"])
    assert data["summary"]["total_flow_yi"] == pytest.approx(
        round(total_flow, 2), abs=0.5
    )
    # 至少存在非零主力净额的成分（否则数据链路可能整体为空）
    assert any(abs(it["flow_net"]) > 0 for it in items)


def test_sector_heatmap_layer_exposes_flow_net():
    """板块层（/heatmap 的实时聚合结果）必须带 flow_net：前端「主力净额」着色
    套在板块层与下钻层同一份数据上，板块层缺这一列会让切换着色后整图无净额。

    回归点：``_load_l2_main_flow`` 少读 amount/flow_net_amount/flow_net_ratio
    时，单位判别器静默退回「已是元」，万元分区被当元读 → 板块净额整列 0.00。
    """
    items = quantdb_feed.get_sector_heatmap("shenwan")
    assert items, "板块层无数据"
    assert all("flow_net" in it for it in items), "板块层缺 flow_net 字段"
    # 至少有一个板块净额非零——全零即上述单位误判回归
    assert any(abs(it["flow_net"] or 0.0) > 0 for it in items), (
        "板块层主力净额整列 0，疑似 L2 单位判别退化为「已是元」"
    )


def test_sector_heatmap_period_speed_fields():
    """周期口径：5 日净额/日均/加速度/周期涨幅必须齐全，且加速度有非零样本。"""
    items = quantdb_feed.get_sector_heatmap("shenwan", "5d")
    assert items
    for key in ("flow_net", "flow_daily", "flow_accel", "pct_period", "period_days"):
        assert all(key in it for it in items), f"板块层缺 {key} 字段"
    assert {it["period_days"] for it in items} == {5}
    assert any(it["flow_accel"] not in (None, 0) for it in items)
    assert all(
        it["flow_daily"] is None
        or abs(it["flow_daily"] - (it["flow_net"] or 0.0) / 5) < 0.01
        for it in items
    )
