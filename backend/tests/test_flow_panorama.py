"""资金流全景 v2 契约测试：panorama / sector / stock 三端点 + 证据产物 IO。

分两类：
- 纯逻辑单测（合成数据，随处可跑）：占位剔除、方向筛选、链接符号、徽章选取、
  原子写与 schema 校验；
- 契约集成测（容器内真实 QuantDB）：4 层结构、topK、value≥0、窗口求和与独立重算
  一致、证据产物 新鲜/陈旧/缺失/体系不适配 四态；
- 证据端点契约（monkeypatch 证据目录与分区表，不依赖真实数据）：unbuilt / 仅档案 /
  stale / 新鲜命中榜 / 快照回溯 / no_rows。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.services.api.market_analysis import flow_panorama, quantdb_feed
from backend.shared import flow_evidence


@pytest.fixture(autouse=True)
def _clear_feed_cache():
    """每个用例起止都清缓存：证据用例 monkeypatch 证据目录，防止结果串味。"""
    quantdb_feed.clear_cache()
    yield
    quantdb_feed.clear_cache()


# ---------------------------------------------------------------------------
# 纯逻辑单测
# ---------------------------------------------------------------------------


def test_placeholder_symbols_by_dt_only_flags_sharing_day():
    """占位常量只在共享发生的当天被剔除，跨日巧合不误伤。"""
    rows = []
    for i in range(30):
        rows.append(
            {"symbol": f"{i:06d}.SZ", "dt": 20260101, "flow_net_amount": 12345.6789}
        )
    rows.append({"symbol": "999999.SZ", "dt": 20260102, "flow_net_amount": 98765.4321})
    hist = pd.DataFrame(rows)
    bad = quantdb_feed.placeholder_symbols_by_dt(hist)
    assert set(bad.keys()) == {"20260101"}
    assert len(bad["20260101"]) == 30
    assert "999999.SZ" not in bad["20260101"]


def test_select_sectors_split_6_4_and_filters():
    agg = pd.DataFrame(
        {"main": [10.0, 8.0, 6.0, 4.0, 2.0, 1.0, -3.0, -7.0, -9.0]},
        index=[f"涨{i}" for i in range(6)] + [f"跌{i}" for i in range(3)],
    )
    # all = 流入 6 名额 + 流出 4 名额；负板块只 3 个 → 共 9 个，流出按 main 升序（最负在前）
    all_sel = flow_panorama._select_sectors(agg, "all", 10)
    assert all_sel == [f"涨{i}" for i in range(6)] + ["跌2", "跌1", "跌0"]

    inflow = flow_panorama._select_sectors(agg, "inflow", 3)
    assert inflow == ["涨0", "涨1", "涨2"]
    outflow = flow_panorama._select_sectors(agg, "outflow", 2)
    assert outflow == ["跌2", "跌1"]  # 最负优先


def _synthetic_sankey_frames(mains: dict[str, float], groups: dict[str, list[str]]):
    """合成 _sankey_graph 入参：sym_sums / member_df / agg / names（口径同 _symbol_sums）。"""
    idx = list(mains)
    sym_sums = pd.DataFrame(
        {
            "super": [mains[s] / 2 for s in idx],
            "large": [mains[s] / 2 for s in idx],
            "medium": [-mains[s] for s in idx],
            "small": [0.0 for _ in idx],
            "buy": [abs(mains[s]) for s in idx],
            "sell": [abs(mains[s]) / 2 for s in idx],
            "main": [mains[s] for s in idx],
            "retail": [-mains[s] for s in idx],
            "amount_y": [abs(mains[s]) * 100 for s in idx],
        },
        index=idx,
    )
    member_df = pd.DataFrame(
        {
            "symbol": [s for syms in groups.values() for s in syms],
            "sector_name": [g for g, syms in groups.items() for _ in syms],
            "sector_type": ["行业板块(二级)"] * sum(len(v) for v in groups.values()),
        }
    )
    agg = pd.DataFrame(
        {
            "main": [sum(mains[s] for s in syms) for syms in groups.values()],
            "retail": [-sum(mains[s] for s in syms) for syms in groups.values()],
            "members": [len(syms) for syms in groups.values()],
            "amount_y": [10000.0 for _ in groups],
            "super": [sum(mains[s] for s in syms) / 2 for syms in groups.values()],
            "large": [sum(mains[s] for s in syms) / 2 for syms in groups.values()],
            "medium": [-sum(mains[s] for s in syms) for syms in groups.values()],
            "small": [0.0 for _ in groups],
        },
        index=list(groups),
    )
    names = {s: f"票{s[:6]}" for s in idx}  # 注意：桑基按 name 去重，测试名必须逐码唯一
    return sym_sums, member_df, agg, names


def _stock_ranks(graph: dict, sector: str) -> list[int]:
    return sorted(
        n["inner_rank"] for n in graph["nodes"] if n["kind"] == "stock" and n["sector"] == sector
    )


def test_sankey_stock_layer_bilateral_6_4_and_direction_single_side():
    """个股层选取（v4）：all=每板块流入前 3 + 流出最深 2（K=5）；单向=TopK/末位 K。

    回归背景：旧实现按「板块主方向」单侧取 head/tail——强流入板块看不到流出排名、
    强流出板块看不到流入排名；v4 起两翼并取（短板块自动收敛）。
    """
    syms_a = [f"{i:06d}.SZ" for i in range(1, 7)]
    syms_b = [f"{i:06d}.SZ" for i in range(7, 13)]
    mains = dict(zip(syms_a, [10, 9, 8, 7, 6, -5], strict=True)) | dict(
        zip(syms_b, [-10, -9, -8, -7, -6, 5], strict=True)
    )
    sym_sums, member_df, agg, names = _synthetic_sankey_frames(
        mains, {"A": syms_a, "B": syms_b}
    )

    g_all = flow_panorama._sankey_graph(
        agg, member_df, sym_sums, names, {}, {}, {"A": 1.0, "B": -1.0},
        {}, None, False, "all", 2, 5,
    )
    # 每板块 rank = {1,2,3（流入头翼）, 5,6（流出尾翼，末位起）}；inner_total=6
    assert _stock_ranks(g_all, "A") == [1, 2, 3, 5, 6]
    assert _stock_ranks(g_all, "B") == [1, 2, 3, 5, 6]
    totals = {n["inner_total"] for n in g_all["nodes"] if n["kind"] == "stock"}
    assert totals == {6}

    g_in = flow_panorama._sankey_graph(
        agg, member_df, sym_sums, names, {}, {}, {}, {}, None, False, "inflow", 2, 5,
    )
    # 单向流入：只展示 A，取头 5（rank 1..5）
    assert _stock_ranks(g_in, "A") == [1, 2, 3, 4, 5]

    g_out = flow_panorama._sankey_graph(
        agg, member_df, sym_sums, names, {}, {}, {}, {}, None, False, "outflow", 2, 5,
    )
    # 单向流出：只展示 B，取末位 5（rank 2..6）
    assert _stock_ranks(g_out, "B") == [2, 3, 4, 5, 6]


def test_sankey_stock_layer_short_sector_converges_with_dedupe():
    """短板块（成员 4 < K=5）双侧收敛为全量且头/尾翼重叠去重（不重复出行）。"""
    syms = [f"{i:06d}.SZ" for i in range(20, 24)]
    mains = dict(zip(syms, [4, 3, 2, -1.5], strict=True))
    sym_sums, member_df, agg, names = _synthetic_sankey_frames(mains, {"C": syms})

    g = flow_panorama._sankey_graph(
        agg, member_df, sym_sums, names, {}, {}, {}, {}, None, False, "all", 2, 5,
    )
    # head(3) = {1,2,3}，tail(2) = {4,3} → 3 已在头翼 → 去重后全量 4 行且不重复
    assert _stock_ranks(g, "C") == [1, 2, 3, 4]
    rank_dupes = [
        n["inner_rank"] for n in g["nodes"] if n["kind"] == "stock"
    ]
    assert len(rank_dupes) == len(set(rank_dupes))


def test_mk_link_non_negative_value_and_sign():
    up = flow_panorama._mk_link("a", "b", 123.45)
    assert (
        up["value"] == pytest.approx(123.45)
        and up["sign"] == 1
        and up["net"] == pytest.approx(123.45)
    )
    down = flow_panorama._mk_link("a", "b", -99.0)
    assert (
        down["value"] == pytest.approx(99.0)
        and down["sign"] == -1
        and down["net"] == pytest.approx(-99.0)
    )
    assert flow_panorama._mk_link("a", "b", 0.0) is None  # 零头边不画


def _evidence_doc(min_n=500):
    stats = {
        "n": 6235,
        "excess_bp": 28.4,
        "excess_daily_bp": 3.5,
        "hit": 0.561,
        "base_hit": 0.54,
        "yearly": {"2024": {"n": 100, "excess_bp": -9.0}},
    }
    exec_stats = dict(stats, n=6120, excess_bp=22.1)
    return {
        "schema": flow_evidence.EVIDENCE_SCHEMA,
        "window": ["20240806", "20261009"],
        "min_cond_n": min_n,
        "conditions": {
            "streak_5": {
                "level": 5,
                "gate": {"pass": True},
                "fwd": {"3": dict(stats, n=2119)},
                "exec": {"3": exec_stats},
            },
            "streak_3": {
                "level": 3,
                "gate": {"pass": True},
                "fwd": {"3": stats},
                "exec": {"3": exec_stats},
            },
            "streak_2": {"level": 2, "gate": {"pass": False}, "fwd": {"3": stats}},
        },
    }


def test_pick_badge_levels_gating_and_min_n():
    doc = _evidence_doc()
    # streak=4 → 取最高满足且过闸的档位（5 不满足 → 3）
    badge = flow_evidence.pick_badge(4, doc)
    assert badge is not None and badge["level"] == 3
    assert badge["fwd"]["n"] == 6235 and badge["exec"]["n"] == 6120
    assert badge["window"] == ["20240806", "20261009"]
    # streak=2 → 仅剩的 streak_2 未过闸 → None
    assert flow_evidence.pick_badge(2, doc) is None
    # streak=6 → 档位 5
    assert flow_evidence.pick_badge(6, doc)["level"] == 5
    # 样本不足 → None
    assert flow_evidence.pick_badge(4, _evidence_doc(min_n=100000)) is None
    # 非 int / 缺失 → None
    assert flow_evidence.pick_badge(None, doc) is None
    assert flow_evidence.pick_badge(4, None) is None


def test_pick_badge_requires_gate_and_stats_no_fabrication():
    doc = _evidence_doc()
    doc["conditions"]["streak_3"]["gate"]["pass"] = False
    badge = flow_evidence.pick_badge(3, doc)
    assert badge is None or badge["level"] != 3  # 未过闸的档位不得上屏


def test_write_json_atomic_and_schema_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_FLOW_EVIDENCE_DIR", str(tmp_path))
    flow_evidence.write_json_atomic(
        tmp_path / "latest.json",
        {
            "schema": flow_evidence.LATEST_SCHEMA,
            "trade_date": "2026-10-09",
            "sectors": {"半导体": {"streak": 4}},
        },
    )
    doc = flow_evidence.load_latest()
    assert (
        doc is not None
        and flow_evidence.norm_trade_date(doc["trade_date"]) == "20261009"
    )
    assert flow_evidence.streak_map(doc)["半导体"]["streak"] == 4
    # schema 不符 → None
    flow_evidence.write_json_atomic(tmp_path / "evidence.json", {"schema": "bogus"})
    assert flow_evidence.load_evidence() is None
    assert flow_evidence.resolve_evidence_dir() == tmp_path


# ---------------------------------------------------------------------------
# 契约集成测（真实 QuantDB，容器内运行）
# ---------------------------------------------------------------------------


def test_panorama_structure_four_layers_and_contract():
    pan = flow_panorama.get_panorama(period="1d", stocks_per_sector=3)
    assert pan["available"] is True, pan.get("meta")
    assert pan["trade_date"] and pan["trade_date_raw"]

    nodes = {n["name"]: n for n in pan["nodes"]}
    kinds = {n["kind"] for n in pan["nodes"]}
    assert {"entity", "size", "sector", "stock"} <= kinds
    layer_of = {"entity": 1, "size": 2, "sector": 3, "stock": 4}
    for n in pan["nodes"]:
        assert n["layer"] == layer_of[n["kind"]], n

    assert flow_panorama.MAIN_NODE in nodes and flow_panorama.RETAIL_NODE in nodes

    # 链接契约：端点必须存在、value=|net|≥0、sign 与 net 同号
    for link in pan["links"]:
        assert link["source"] in nodes and link["target"] in nodes, link
        assert link["value"] >= 0
        assert link["value"] == pytest.approx(abs(link["net"]))
        if link["net"] > 0:
            assert link["sign"] == 1
        elif link["net"] < 0:
            assert link["sign"] == -1

    # 每板块个股数 ≤ stocks_per_sector
    per_sector: dict[str, int] = {}
    for n in pan["nodes"]:
        if n["kind"] == "stock":
            per_sector[n["sector"]] = per_sector.get(n["sector"], 0) + 1
    assert per_sector, "桑基应包含个股层"
    assert all(v <= 3 for v in per_sector.values())

    # 个股节点带板块内 rank（C 级），不带资金流预测徽章
    stock_nodes = [n for n in pan["nodes"] if n["kind"] == "stock"]
    for n in stock_nodes:
        assert 1 <= n["inner_rank"] <= n["inner_total"]
        assert n["key"].startswith(("SH", "SZ", "BJ"))
        assert "evidence" not in n

    # 板块 master 榜排序 = 延续状态驱动：命中(≥2)在前，其后按净额降序
    rows = pan["sectors"]
    assert len(rows) >= 20
    keys = [
        (0 if (r["streak"] or 0) >= 2 else 1, -(r["streak"] or 0), -r["net_main"])
        for r in rows
    ]
    assert keys == sorted(keys)


def test_panorama_direction_filters_sign():
    inflow = flow_panorama.get_panorama(period="1d", direction="inflow", top_sectors=5)
    assert inflow["available"] is True
    sector_nodes = [n for n in inflow["nodes"] if n["kind"] == "sector"]
    assert sector_nodes and all(n["sign"] == 1 for n in sector_nodes)

    outflow = flow_panorama.get_panorama(
        period="1d", direction="outflow", top_sectors=5
    )
    sector_nodes = [n for n in outflow["nodes"] if n["kind"] == "sector"]
    assert sector_nodes and all(n["sign"] == -1 for n in sector_nodes)


def test_panorama_window_sum_matches_independent_recompute():
    """5 日窗口：任选一板块，用同源取数独立重算成分主力净额之和应精确一致。"""
    pan = flow_panorama.get_panorama(period="5d")
    assert pan["available"] is True
    row = pan["sectors"][0]

    days = flow_panorama._l2_dates_upto(None, 5)
    assert days and days[0] == pan["trade_date_raw"]
    flow = flow_panorama._clean_flow(quantdb_feed._load_l2_flow(days))
    member_df = flow_panorama._member_frame(
        quantdb_feed._sector_members(), flow_panorama.SECTOR_SET_TYPE["industry2"]
    )
    syms = set(member_df[member_df["sector_name"] == row["name"]]["symbol"])
    expected = float(flow[flow["symbol"].isin(syms)]["main"].sum())
    assert row["net_main"] == pytest.approx(
        expected, abs=max(1.0, abs(expected) * 1e-9)
    )


def test_sector_detail_members_contract():
    pan = flow_panorama.get_panorama(period="1d")
    name = pan["sectors"][0]["name"]
    detail = flow_panorama.get_sector_detail(name=name, top=30)
    assert detail is not None and detail["available"] is True
    assert detail["name"] == name
    summary = detail["summary"]
    for field in (
        "net_main",
        "net_ratio",
        "super",
        "large",
        "medium",
        "small",
        "breadth",
        "pct_change",
        "members",
        "streak",
        "evidence",
    ):
        assert field in summary
    assert summary["net_main"] == pytest.approx(pan["sectors"][0]["net_main"], abs=1.0)

    rows = detail["members"]
    assert rows and len(rows) <= 30
    totals = [r["inner_total"] for r in rows]
    assert len(set(totals)) == 1 and totals[0] >= len(rows)
    ranks = [r["inner_rank"] for r in rows]
    # rank 为窗口净额全序名次：截断后仍唯一，且落在 1..total
    assert len(set(ranks)) == len(ranks)
    assert all(1 <= r <= totals[0] for r in ranks)
    abs_vals = [abs(r["net_main"]) for r in rows]
    assert abs_vals == sorted(abs_vals, reverse=True)
    for r in rows:
        assert r["symbol"].startswith(("SH", "SZ", "BJ"))
        # 20 日累计涨幅：数值或 null（缺价显 —，绝不落 0）
        assert r["pct_20d"] is None or isinstance(r["pct_20d"], (int, float))
        if r["share"] is not None:
            # share = 成员净额 / 板块净额（两值皆 2 位小数舍入，容差放宽）
            assert r["share"] == pytest.approx(
                r["net_main"] / summary["net_main"], abs=2e-3
            )
    assert len(detail["trend"]) >= 1 and len(detail["trend"]) <= 20
    assert detail["trend"] == sorted(detail["trend"], key=lambda x: x["date"])


def test_sector_detail_members_recent_pct_window():
    """成分行 recent：统一升序日期组（近 ≤5 个 L2 分区日），值为官方涨跌幅或 null。"""
    from backend.shared.stock_utils import StockCodeUtil

    pan = flow_panorama.get_panorama(period="1d")
    name = pan["sectors"][0]["name"]
    detail = flow_panorama.get_sector_detail(name=name, top=5)
    assert detail is not None and detail["available"] is True
    days = sorted(flow_panorama._l2_dates_upto(None, flow_panorama.SECTOR_RECENT_DAYS))
    expect_dates = [flow_panorama._dash(d) for d in days]
    rows = detail["members"]
    assert rows
    for r in rows:
        recent = r["recent"]
        assert isinstance(recent, list) and len(recent) == len(expect_dates)
        # 统一日期组：所有成分行逐日对齐，缺价显 null 而非漏行
        assert [p["date"] for p in recent] == expect_dates
        for p in recent:
            assert isinstance(p["date"], str) and len(p["date"]) == 10
            assert p["pct"] is None or isinstance(p["pct"], (int, float))
    # 与同源价格独立对齐：最新日 pct 应等于官方涨跌幅（同值源 _load_prices）
    raw = StockCodeUtil.to_suffix(rows[0]["symbol"])
    today_px = quantdb_feed._load_prices([days[-1]])
    hit = today_px[today_px["symbol"] == raw]
    if not hit.empty:
        val = hit.iloc[0]["pct_change"]
        got = rows[0]["recent"][-1]["pct"]
        if pd.isna(val):
            assert got is None
        else:
            assert got == pytest.approx(float(val), abs=1e-6)


def test_sector_detail_member_pct_20d_compound():
    """成员行 pct_20d = 近 20 交易日官方逐日涨跌幅复利（独立重算逐行对齐）。

    复利连乘等价于区间收盘比（官方口径已含除权调整）；有效日 < 10 必须显 null
    而非零值——零会把「停牌/次新无数据」伪装成「20 日零涨幅」。
    """
    from backend.shared.stock_utils import StockCodeUtil

    pan = flow_panorama.get_panorama(period="1d")
    name = pan["sectors"][0]["name"]
    detail = flow_panorama.get_sector_detail(name=name, top=5)
    assert detail is not None and detail["available"] is True
    rows = detail["members"]
    assert rows
    days20 = sorted(
        flow_panorama._l2_dates_upto(None, flow_panorama.SECTOR_TREND_DAYS)
    )
    px = quantdb_feed._load_prices(days20)
    checked = 0
    for r in rows:
        assert "pct_20d" in r
        got = r["pct_20d"]
        assert got is None or isinstance(got, (int, float))
        if got is None:
            continue
        raw = StockCodeUtil.to_suffix(r["symbol"])
        sub = px[px["symbol"] == raw].sort_values("dt")
        acc = 1.0
        n_ok = 0
        for v in sub["pct_change"]:
            if pd.isna(v):
                continue
            acc *= 1.0 + float(v) / 100.0
            n_ok += 1
        assert n_ok >= flow_panorama.SECTOR_PCT_MIN_DAYS, (
            f"{raw} 有效日仅 {n_ok} 却给了 pct_20d"
        )
        assert got == pytest.approx((acc - 1.0) * 100.0, abs=0.01)
        checked += 1
    if checked == 0:
        pytest.skip("实机数据该板块无 20 日有效价（全 null），跳过复利对齐断言")


def test_sector_detail_day_stats_only_latest_day(monkeypatch):
    """多日价格必须只取当日切片：成分均涨幅不得跨日放大（回归防线）。

    合成 2 日价格（前日 +10/−5，当日 0/+1）：当日均涨幅应 = 0.5；
    若调用方把多日价格整体 merge，均值会被放大到 1.5。
    """
    members = pd.DataFrame(
        {
            "symbol": ["600036.SH", "000001.SZ"],
            "sector_name": ["板块A", "板块A"],
            "sector_type": [flow_panorama.SECTOR_SET_TYPE["industry2"]] * 2,
        }
    )
    monkeypatch.setattr(flow_panorama, "_available", lambda: True)
    monkeypatch.setattr(
        flow_panorama, "_l2_dates_upto", lambda target, n: ["20260102", "20251231"][:n]
    )
    monkeypatch.setattr(flow_panorama, "_sector_members", lambda: members)
    monkeypatch.setattr(
        flow_panorama,
        "_load_l2_flow",
        lambda days: pd.DataFrame(
            {
                "dt": ["20260102", "20251231"],
                "symbol": ["600036.SH", "000001.SZ"],
                "flow_super_net": [3.0e8, 1.0e8],
                "flow_large_net": [1.0e8, 1.0e8],
                "flow_medium_net": [0.0, 0.0],
                "flow_small_net": [0.0, 0.0],
                "flow_buy_amount": [5.0e8, 2.0e8],
                "flow_sell_amount": [1.0e8, 0.0],
                "amount": [100000.0, 80000.0],
            }
        ),
    )
    monkeypatch.setattr(
        flow_panorama,
        "_load_prices",
        lambda days: pd.DataFrame(
            {
                "symbol": ["600036.SH", "000001.SZ", "600036.SH", "000001.SZ"],
                "dt": ["20251231", "20251231", "20260102", "20260102"],
                "close": [10.0, 20.0, 11.0, 19.0],
                "pct_change": [10.0, -5.0, 0.0, 1.0],
            }
        ),
    )
    monkeypatch.setattr(
        flow_panorama,
        "_evidence_context",
        lambda sector_set, day: ({}, None, {"available": False}),
    )
    monkeypatch.setattr(flow_panorama, "_instrument_names", lambda: {})

    detail = flow_panorama._sector_detail_impl("板块A", "industry2", "1d", 30, None)
    assert detail is not None and detail["available"] is True
    assert detail["summary"]["pct_change"] == pytest.approx(0.5, abs=1e-6)
    row = detail["members"][0]
    assert [p["date"] for p in row["recent"]] == ["2025-12-31", "2026-01-02"]
    assert [p["pct"] for p in row["recent"]] == [10.0, 0.0]


def test_stock_detail_series_and_attribution_roundtrip():
    pan = flow_panorama.get_panorama(period="1d", stocks_per_sector=5)
    stock_node = next(n for n in pan["nodes"] if n["kind"] == "stock")
    detail = flow_panorama.get_stock_detail(symbol=stock_node["key"], days=20)
    assert detail is not None and detail["available"] is True
    assert detail["symbol"] == stock_node["key"]
    assert isinstance(detail["name"], str)

    series = detail["series"]
    assert 1 <= len(series) <= 20
    assert [s["date"] for s in series] == sorted(s["date"] for s in series)
    for s in series:
        if s["main"] is not None:
            # main/retail 由五档求和派生，各自 4 位小数舍入 → 容差 2e-4（亿元）
            assert s["main"] == pytest.approx(s["super"] + s["large"], abs=2e-4)
            assert s["retail"] == pytest.approx(s["medium"] + s["small"], abs=2e-4)

    # windows 与 series 自洽：1d 窗口 = 序列最后一行（亿元 → 元 容差 1 万元）
    w1 = detail["windows"]["1d"]
    assert w1 is not None
    assert w1["net_main"] == pytest.approx(series[-1]["main"] * 1e8, abs=1e4)

    # 板块归因：能「返回板块」——含该股当前所在板块且 rank/total 合法
    attrib = detail["attribution"]
    assert attrib, "个股应至少归属一个板块"
    for a in attrib:
        assert 1 <= a["inner_rank"] <= a["inner_total"]
        assert "stock_net_main" in a
    assert any(a["type"] == "行业板块(二级)" for a in attrib)


def test_stock_detail_invalid_symbol_returns_none():
    assert flow_panorama.get_stock_detail(symbol="NOTACODE") is None
    assert flow_panorama.get_stock_detail(symbol="") is None


def test_stock_detail_factor_block_missing_present_stale_uncovered(
    tmp_path, monkeypatch
):
    """因子板共振态四态：板缺失→None / 在板→行业二级+象限 / 陈旧→stale / 未收录。

    板目录用 QM_FACTOR_BOARD_DIR 切到 tmp（members 伴生件由 board_io 原子写），
    个股侧走真实 L2 分区（与本节其余契约测同款依赖）。
    """
    from backend.shared.factor_board import board_io as fb_io

    # 0) 板目录缺失 → factor=None（个股资金流主路径不因因子板缺席而失败）
    monkeypatch.setenv("QM_FACTOR_BOARD_DIR", str(tmp_path / "empty"))
    pan = flow_panorama.get_panorama(period="1d", stocks_per_sector=5)
    stock_node = next(n for n in pan["nodes"] if n["kind"] == "stock")
    detail = flow_panorama.get_stock_detail(symbol=stock_node["key"], days=5)
    assert detail is not None and detail["available"] is True
    assert detail["factor"] is None

    suffix = detail["suffix"]
    board_dir = tmp_path / "board"

    def _write_members(decision_date: str, include: bool) -> None:
        rows = (
            [
                {
                    "symbol": suffix,
                    "name": detail["name"],
                    "composite": 0.66,
                    "model_rank": 0.83,
                    "quadrant": "HH",
                }
            ]
            if include
            else [
                {"symbol": "SH000000", "name": "占位", "composite": 0.1,
                 "model_rank": 0.1, "quadrant": "LL"}
            ]
        )
        fb_io.write_members_index(
            {
                "decision_date": decision_date,
                "trade_date": decision_date,
                "sectors": {"industry_l2": {"测试行业": rows}},
            },
            board_dir,
        )

    # 1) 板与资金流数据日同日 → available + 行业二级定位 + 象限原样 + 不陈旧
    _write_members(detail["trade_date"], include=True)
    monkeypatch.setenv("QM_FACTOR_BOARD_DIR", str(board_dir))
    quantdb_feed.clear_cache()  # 步骤 0 已缓存同一 key，换板后须重算
    f = flow_panorama.get_stock_detail(symbol=stock_node["key"], days=5)["factor"]
    assert f is not None
    assert f["available"] is True
    assert f["sector_l2"] == "测试行业"
    assert f["quadrant"] == "HH"
    assert f["composite"] == pytest.approx(0.66)
    assert f["model_rank"] == pytest.approx(0.83)
    assert f["decision_date"] == detail["trade_date"]
    assert f["stale_days"] == 0 and f["stale"] is False

    # 2) 板决策日远落后于资金流数据日 → 只显形不冒充当日（stale=True）
    _write_members("2020-01-01", include=True)
    quantdb_feed.clear_cache()
    f_old = flow_panorama.get_stock_detail(symbol=stock_node["key"], days=5)["factor"]
    assert f_old["available"] is True and f_old["stale"] is True
    assert f_old["stale_days"] is not None and f_old["stale_days"] > 3

    # 3) 板在但该股未收录 → available=False（不许翻成 None 或编造象限）
    _write_members(detail["trade_date"], include=False)
    quantdb_feed.clear_cache()
    f_miss = flow_panorama.get_stock_detail(symbol=stock_node["key"], days=5)["factor"]
    assert f_miss["available"] is False
    assert f_miss["quadrant"] is None and f_miss["sector_l2"] is None


def test_panorama_evidence_gating_fresh_stale_missing(tmp_path, monkeypatch):
    """证据四态：缺失(not_built) / 陈旧(stale) / 新鲜(available) / 体系不适配(not_applicable)。"""
    monkeypatch.setenv("QM_FLOW_EVIDENCE_DIR", str(tmp_path))
    latest_day = quantdb_feed._latest_l2_date()
    assert latest_day
    dash = f"{latest_day[:4]}-{latest_day[4:6]}-{latest_day[6:]}"

    # 1) 缺失
    pan = flow_panorama.get_panorama(period="1d")
    assert pan["meta"]["evidence"]["reason"] == "not_built"
    assert all(r["streak"] is None and r["evidence"] is None for r in pan["sectors"])

    # 2) 体系不适配（概念体系不适用行业二级证据）
    pan_c = flow_panorama.get_panorama(period="1d", sector_set="concept")
    assert pan_c["meta"]["evidence"]["reason"] == "not_applicable"
    assert all(r["streak"] is None for r in pan_c["sectors"])

    # 3) 陈旧：产物 trade_date 落后 → 不得冒充当日
    flow_evidence.write_json_atomic(
        tmp_path / "latest.json",
        {
            "schema": flow_evidence.LATEST_SCHEMA,
            "trade_date": "2020-01-01",
            "sectors": {"半导体": {"streak": 4}},
        },
    )
    quantdb_feed.clear_cache()  # 步骤 1 已缓存同 key 结果，写产物后须重算
    assert (
        flow_panorama.get_panorama(period="1d")["meta"]["evidence"]["reason"] == "stale"
    )

    # 4) 新鲜：匹配合法 streak 落到板块行与桑基节点，徽章带 n 与分年
    target_name = pan["sectors"][0]["name"]
    flow_evidence.write_json_atomic(
        tmp_path / "latest.json",
        {
            "schema": flow_evidence.LATEST_SCHEMA,
            "trade_date": dash,
            "sectors": {target_name: {"streak": 4}},
        },
    )
    flow_evidence.write_json_atomic(tmp_path / "evidence.json", _evidence_doc())

    quantdb_feed.clear_cache()
    pan2 = flow_panorama.get_panorama(period="1d")
    assert pan2["meta"]["evidence"]["available"] is True
    row = next(r for r in pan2["sectors"] if r["name"] == target_name)
    assert row["streak"] == 4
    assert row["evidence"] is not None and row["evidence"]["level"] == 3
    assert row["evidence"]["fwd"]["n"] == 6235
    assert "yearly" in row["evidence"]["fwd"]

    detail = flow_panorama.get_sector_detail(name=target_name)
    assert detail["summary"]["streak"] == 4
    assert detail["summary"]["evidence"]["level"] == 3

    # 其他板块无 streak 数据 → 保持 null（不编造）
    others = [r for r in pan2["sectors"] if r["name"] != target_name]
    assert all(r["streak"] is None for r in others)


# ---------------------------------------------------------------------------
# 证据端点契约（get_evidence；目录与分区表 monkeypatch，不依赖真实数据）
# ---------------------------------------------------------------------------


def _archive_doc() -> dict:
    """带全字段的信号档案（_evidence_doc 的富化副本，供端点直传断言）。"""
    return dict(
        _evidence_doc(),
        trade_date="2099-10-10",
        built_at="2099-10-10T01:02:03Z",
        badge_levels=list(flow_evidence.BADGE_LEVELS),
        badge_horizon=flow_evidence.BADGE_HORIZON,
        gate_criteria="n(h=3)≥min_cond_n ∧ exec>0 ∧ fwd>0 ∧ 环境双正",
        coverage={"days": 520, "sectors": 80, "panel_rows": 41600},
        conventions={"price_source": "daily_forward(qfq)"},
    )


def test_evidence_endpoint_unbuilt_stale_fresh_and_archive(tmp_path, monkeypatch):
    """端点五路径：产物全缺 / 仅档案 / 陈旧 / 新鲜命中榜；档案与新鲜度解耦。"""
    monkeypatch.setenv("QM_FLOW_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setattr(flow_panorama, "_get_partition_dates", lambda rel: ["20991010"])

    # ① 产物全缺 → not_built（不编造命中榜与档案）
    doc = flow_panorama.get_evidence()
    assert doc["available"] is False and doc["reason"] == "not_built"
    assert doc["hits_state"] == "unbuilt" and doc["hits"] == []
    assert doc["conditions"] == {} and doc["rejected"] == []
    assert doc["min_cond_n"] == flow_evidence.MIN_COND_N
    assert doc["meta"]["disclosure"] == flow_panorama.EVIDENCE_DISCLOSURE
    assert doc["meta"]["disclosure"], "披露清单不得为空"

    # ② 档案在、latest 缺 → available 但命中榜 unbuilt；档案字段原样直传
    flow_evidence.write_json_atomic(tmp_path / "evidence.json", _archive_doc())
    quantdb_feed.clear_cache()
    doc = flow_panorama.get_evidence()
    assert doc["available"] is True and doc["reason"] is None
    assert doc["hits_state"] == "unbuilt" and doc["hits"] == []
    assert doc["trade_date"] == "2099-10-10"
    assert doc["trade_date_raw"] == "20991010"
    assert doc["built_at"] == "2099-10-10T01:02:03Z"
    assert doc["gate_criteria"].startswith("n(h=3)")
    assert doc["coverage"] == {"days": 520, "sectors": 80, "panel_rows": 41600}
    assert doc["conventions"] == {"price_source": "daily_forward(qfq)"}
    assert doc["conditions"]["streak_3"]["gate"]["pass"] is True
    assert doc["badge_horizon"] == flow_evidence.BADGE_HORIZON
    assert doc["meta"]["evidence_trade_date"] == "2099-10-10"

    # ③ 陈旧：latest 落后最新 L2 分区 → stale，不用陈旧 streak 冒充当日；
    #    档案为全窗统计，与其新鲜度无关，仍随行返回
    flow_evidence.write_json_atomic(
        tmp_path / "latest.json",
        {
            "schema": flow_evidence.LATEST_SCHEMA,
            "trade_date": "2099-01-01",
            "sectors": {"半导体": {"streak": 4}},
        },
    )
    quantdb_feed.clear_cache()
    doc = flow_panorama.get_evidence()
    assert doc["hits_state"] == "stale" and doc["hits"] == []
    assert doc["hits_trade_date"] == "2099-01-01"
    assert doc["conditions"], "档案不得随命中榜新鲜度一起消失"

    # ④ 新鲜：命中榜装配（streak≥2 过滤、streak→净额双键排序、逐行徽章）
    flow_evidence.write_json_atomic(
        tmp_path / "latest.json",
        {
            "schema": flow_evidence.LATEST_SCHEMA,
            "trade_date": "2099-10-10",
            "sectors": {
                "医药": {
                    "streak": 4,
                    "net_main": 1.1e9,
                    "net_ratio": 0.021,
                    "breadth": 0.7,
                    "members": 300,
                },
                "银行": {
                    "streak": 3,
                    "net_main": 2.0e9,
                    "net_ratio": 0.05,
                    "breadth": 0.9,
                    "members": 40,
                },
                "半导体": {
                    "streak": 3,
                    "net_main": 1.25e9,
                    "net_ratio": 0.0333,
                    "breadth": 0.75,
                    "members": 120,
                },
                "煤炭": {"streak": 1, "net_main": 9e9},
            },
        },
    )
    quantdb_feed.clear_cache()
    doc = flow_panorama.get_evidence()
    assert doc["available"] is True and doc["hits_state"] == "ok"
    assert doc["hits_trade_date"] == "2099-10-10"
    # 煤炭 streak=1 剔除；先按 streak 降序，同 streak 再按净额降序
    assert [h["name"] for h in doc["hits"]] == ["医药", "银行", "半导体"]
    top = doc["hits"][0]
    assert top["net_main"] == 1.1e9 and top["members"] == 300
    assert top["evidence"]["level"] == 3  # streak 4 → 最高过闸档 3
    assert top["evidence"]["fwd"]["n"] == 6235


def test_evidence_endpoint_snapshot_backtrack_and_no_rows(tmp_path, monkeypatch):
    """date=快照日：命中榜从 panel 回溯（2/4 位圆整）；无该日行或无面板 → no_rows。"""
    monkeypatch.setenv("QM_FLOW_EVIDENCE_DIR", str(tmp_path))
    flow_evidence.write_json_atomic(tmp_path / "evidence.json", _archive_doc())
    pd.DataFrame(
        [
            {
                "sector": "半导体",
                "dt": "20990930",
                "streak": 3,
                "net_main": 66263022.51999999,
                "net_ratio": 0.03333333,
                "breadth": 0.83333333,
                "members": 120,
            },
            {
                "sector": "煤炭",
                "dt": "20990930",
                "streak": 1,
                "net_main": -1.0,
                "net_ratio": -0.001,
                "breadth": 0.1,
                "members": 30,
            },
            {
                "sector": "半导体",
                "dt": "20990929",
                "streak": 2,
                "net_main": 1.0,
                "net_ratio": 0.0,
                "breadth": 0.5,
                "members": 120,
            },
        ]
    ).to_parquet(tmp_path / "panel_sector.parquet", index=False)

    doc = flow_panorama.get_evidence(date="2099-09-30")
    assert doc["available"] is True and doc["hits_state"] == "ok"
    assert doc["requested_date"] == "2099-09-30"
    assert doc["hits_trade_date"] == "2099-09-30"
    assert len(doc["hits"]) == 1
    hit = doc["hits"][0]
    assert hit["name"] == "半导体" and hit["streak"] == 3
    # 面板路径 2/4 位圆整：无浮点尾巴
    assert hit["net_main"] == 66263022.52
    assert hit["net_ratio"] == 0.0333
    assert hit["breadth"] == 0.8333
    assert hit["members"] == 120
    assert hit["evidence"]["level"] == 3

    # 该日无行 → no_rows
    doc = flow_panorama.get_evidence(date="2099-09-28")
    assert doc["hits_state"] == "no_rows" and doc["hits"] == []
    assert doc["requested_date"] == "2099-09-28"

    # 面板缺失 → 仍 no_rows（不抛不编造）
    (tmp_path / "panel_sector.parquet").unlink()
    quantdb_feed.clear_cache()
    doc = flow_panorama.get_evidence(date="2099-09-30")
    assert doc["hits_state"] == "no_rows" and doc["hits"] == []


def test_period_map_has_2d_and_speed_windows():
    from backend.services.api.market_analysis.quantdb_feed import PERIOD_DAYS

    assert PERIOD_DAYS["2d"] == 2
    assert {"1d", "2d", "3d", "5d", "10d", "20d"} <= set(PERIOD_DAYS)
