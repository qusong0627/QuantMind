"""港股聪明钱数据面集成测试（直连真实 parquet/产物，无网络依赖）。

照 test_quanthk_institutional.py 同款直连模式。两层：

1. **源侧对账（无产物依赖，永远跑）**：CCASS A 席（南向结算席）最新分区
   持股量 vs hsgt_south 同日总量 ±2%——独立重算，防「财务双计」口径漂移；
2. **产物面（依赖手动首建，未建 skip）**：港股聪明钱产物为手动首建（OSS
   首装不排调度），未首建时 skip 而非红——首建后自动纳入：
   - 最新分区事件冒烟（量级/键唯一/方向与类别域/counts 对账一致）；
   - 封存前视列成熟度（远日成熟、尾日 NaN——绝不填 0）；
   - 类型覆盖：事件与持仓 = CCASS 5 类，flows = 6 类（含南向）；
   - 席位榜三窗口形状与单调（all 窗口事件数 ≥ 20 窗口）；
   - 产物自洽：as_of ↔ evidence.eval_end ↔ seats.last_event_date ↔ 对账比；
3. **v2 面（技能/下界/月序/持续性，依赖 v2 产物，缺则 skip）**：
   - 30 列契约 + 下界纪律（lb ⇔ n_exc10 ≥ SEAT_MIN_EVENTS，假 90% 不入榜）；
   - 默认榜（n_events≥50）榜首必须显著（skill_t ≥ 2）；
   - seat_monthly/seat_symbols 与席位榜 n_exc10 口径对账（可测样本两面同源）；
   - latest.categories 技能列 + seat_persistence 弱持续区间护栏（0 < h < 0.3）。
"""

from __future__ import annotations

import duckdb
import pytest

from backend.services.api.market_analysis_hk import quanthk_feed as feed
from backend.services.api.market_analysis_shared import market_days
from backend.shared.hk_smart_money import (
    CATEGORIES,
    CCASS_CATEGORIES,
    SEAT_MIN_EVENTS,
    SOUTHBOUND_SEAT_IDS,
)
from backend.shared.hk_smart_money import datasets as ds
from backend.shared.hk_smart_money import events as ev

SEAT_WINDOWS = ("20", "60", "all")

# v2 席位榜 30 列契约（stats.seat_metrics 输出，按窗口成套）
SEAT_V2_COLUMNS = frozenset(
    {
        "participant_id",
        "participant_name",
        "category",
        "kind",
        "window",
        "n_events",
        "n_buy",
        "n_sell",
        "n_exc10",
        "avg_exc_bp",
        "hit",
        "buy_hit",
        "sell_hit",
        "t",
        "shrunk_exc_bp",
        "shrunk_hit",
        "stability",
        "eligible",
        "first_event_date",
        "last_event_date",
        "skill_bp",
        "skill_t",
        "lb_skill_bp",
        "vw_skill_bp",
        "exc5_bp",
        "exc20_bp",
        "buy_exc_bp",
        "sell_exc_bp",
        "n_symbols",
        "avg_value_m",
    }
)


def _common_date(rel_a: str, rel_b: str) -> str:
    """两 rel 的最新公共分区日（各自原始字串）；无交集则 fail。"""
    da = {
        d.replace("-", ""): d
        for d in market_days.list_partition_dates(rel_a, feed.DATA_DIR)
    }
    db = {
        d.replace("-", ""): d
        for d in market_days.list_partition_dates(rel_b, feed.DATA_DIR)
    }
    assert da, f"{rel_a} 无分区"
    assert db, f"{rel_b} 无分区"
    common = sorted(set(da) & set(db), reverse=True)
    assert common, f"{rel_a} 与 {rel_b} 无公共日"
    return common[0]


@pytest.fixture(scope="module")
def product():
    if not (ds.product_dir() / "latest.json").is_file():
        pytest.skip("聪明钱产物未首建（build_hk_smart_money.py）——首建后自动纳入")
    return ds.product_dir()


# ---------------------------------------------------------------- 源侧对账


def test_南向对账A席_vs_hsgt_south_独立重算():
    """±2% 数据质量门：按**两边都可见的股票交集**独立重算。

    CCASS 只披露前 50 大参与者；部分股票的中国结算席位排在第 50 名之外
    （2026-11-12 有 86 只），用全量南向做分母会凭空少 ~3%。对账必须在同一
    股票集合上做，并保留覆盖率门防止代码映射大面积断裂。
    """
    dt = _common_date(feed.CCASS_TOP50_REL, feed.SOUTH_REL)
    seats = ",".join(f"'{s}'" for s in SOUTHBOUND_SEAT_IDS)
    a_path = feed.DATA_DIR / feed.CCASS_TOP50_REL / f"dt={dt}" / "data.parquet"
    s_path = feed.DATA_DIR / feed.SOUTH_REL / f"dt={dt}" / "data.parquet"
    row = (
        duckdb.connect()
        .execute(
            f"""
            WITH a AS (
              SELECT stock_code AS symbol, sum(holding_quantity) AS qty
              FROM read_parquet('{a_path}')
              WHERE participant_id IN ({seats})
              GROUP BY stock_code
            ), s AS (
              SELECT symbol, sum(holding_quantity) AS qty
              FROM read_parquet('{s_path}')
              GROUP BY symbol
            )
            SELECT sum(a.qty), sum(s.qty), count(*) AS n,
                   (SELECT count(*) FROM s) AS south_n
            FROM a JOIN s USING(symbol)
            """
        )
        .fetchone()
    )
    a_qty, s_qty, n, south_n = row
    assert a_qty and s_qty and n
    # 覆盖率门：两库交集至少 90% 南向股票（防代码映射断裂/整库缺失）
    assert n >= 0.9 * south_n, f"CCASS/南向代码交集仅 {n}/{south_n}"
    ratio = a_qty / s_qty
    assert abs(ratio - 1.0) < 0.02, (
        f"A 席/南向 {dt} 交集比值 {ratio:.4f} 偏离 1.0"
        f"（A={a_qty} S={s_qty} n={n}/{south_n}）"
    )


# ---------------------------------------------------------------- 事件仓冒烟


def test_事件仓最新分区冒烟(product):
    dates = ds.event_dates()
    assert dates, "事件仓为空"
    assert dates == tuple(sorted(dates))
    assert all(len(d) == 8 and d.isdigit() for d in dates)
    # 链条存活：事件尾日跟随 CCASS（一周容差=防周末/假期边界）
    assert dates[-1] in ds.ccass_dates()[-5:], (
        "事件仓落后 CCASS 超 5 个分区——链条停摆？"
    )

    latest = dates[-1]
    df = ds.load_events(dates=[latest])
    assert 200 < len(df) < 50_000, f"最新分区 {latest} 事件数 {len(df)} 量级异常"
    assert int(df[["symbol", "participant_id"]].duplicated().sum()) == 0
    assert set(df["direction"].unique()) <= {1, -1}
    assert set(df["category"].unique()) <= set(CCASS_CATEGORIES)
    assert bool((df["delta_value"] != 0).all())
    assert bool(df["symbol"].str.fullmatch(r"\d{4}\.HK").all())
    assert bool(df["delta_qty"].astype("int64").ne(0).all())

    # counts.json 与数据对账（同源见证）
    counts = ev.read_counts(latest)
    assert counts.get("priced") is True
    assert counts.get("events_n") == len(df)
    assert counts.get("suspect_n", 0) <= counts.get("events_n", 0)
    assert counts.get("a_seat_qty_sum", 0) > 0
    assert counts.get("south_qty_sum", 0) > 0


def test_封存前视列成熟度(product):
    """远日 fwd 超额应成熟（非空）；尾日未成熟应 NaN——绝不填 0 冒充。"""
    dates = ds.event_dates()
    old = dates[-40]
    df_old = ds.load_events(dates=[old], columns=["fwd_exc_10"])
    ripe = float(df_old["fwd_exc_10"].notna().mean())
    assert ripe > 0.9, f"{old} 的 h10 事后超额成熟率仅 {ripe:.2f}"
    latest = ds.load_events(dates=[dates[-1]], columns=["fwd_exc_10", "fwd_exc_20"])
    any_fwd = latest["fwd_exc_10"].notna() | latest["fwd_exc_20"].notna()
    assert float(any_fwd.mean()) < 0.5, "尾日事后超额不应大量成熟（未到期应 NaN）"
    assert float((latest["fwd_exc_20"].fillna(0) == 0).mean()) > 0.5  # NaN≠0


def test_类型覆盖6类(product):
    """事件与持仓 = CCASS 5 类；flows = 6 类（含南向）——南向不入事件仓防双重计数。"""
    dates = ds.event_dates()
    win = list(dates[-20:])
    evs = ds.load_events(dates=win, columns=["symbol", "category"])
    assert set(evs["category"].unique()) == set(CCASS_CATEGORIES)
    # flows 须 20 日窗覆盖 6 类：单日不稳（'other' 极稀疏；南向上游分区复制
    # 快照日 Δ 全 0 → 当日 0 行，是数据事实而非链路故障）
    flows = ds.load_flows(dates=win, columns=["dt", "category"])
    assert set(flows["category"].unique()) == set(CATEGORIES)
    hold = ds.load_holdings(dates=[dates[-1]])
    assert set(hold["category"].unique()) == set(CCASS_CATEGORIES)
    assert "southbound" not in set(hold["category"].unique())


# ---------------------------------------------------------------- 席位榜


def test_席位榜三窗口形状与单调(product):
    seats = ds.load_seats()
    assert seats is not None and len(seats) > 100
    assert set(seats["window"].unique()) == set(SEAT_WINDOWS)
    assert seats.groupby("window").size().nunique() == 1  # 三窗同席位数
    # 席位宇宙 = 全历史有事件者；20/60 窗可含 0 事件行（原值披露不删行，
    # 由 API min_events 过滤），'all' 窗必 ≥1
    zero_windows = set(seats.loc[seats["n_events"] == 0, "window"].unique())
    assert zero_windows <= {"20", "60"}
    assert int(seats.loc[seats["window"] == "all", "n_events"].min()) >= 1
    assert int(seats["eligible"].astype(bool).sum()) > 0
    assert bool(seats["shrunk_hit"].dropna().between(0.0, 1.0).all())
    latest = ds.event_dates()[-1]
    assert str(seats["last_event_date"].max()) <= latest
    # all 窗口事件数 ≥ 20 窗口（同席位口径单调）
    m = seats[seats["window"] == "20"].merge(
        seats[seats["window"] == "all"], on="participant_id", suffixes=("_20", "_all")
    )
    assert len(m) > 50
    assert bool((m["n_events_all"] >= m["n_events_20"]).all())


# ---------------------------------------------------------------- 产物自洽


def test_产物三面自洽(product):
    latest = ds.load_product_json("latest")
    consensus = ds.load_product_json("consensus")
    evidence = ds.load_product_json("evidence")
    seats = ds.load_seats()
    iso = latest["as_of"]
    compact = iso.replace("-", "")
    assert evidence["window"]["eval_end"] == iso
    assert str(seats["last_event_date"].max()) == compact
    assert evidence["admitted"] == latest["admitted"] == consensus["adopted"]
    # 产物内对账比与源侧同门限（±2%）
    ratio = evidence["reconciliation"]["ratio"]
    assert abs(ratio - 1.0) <= 0.02, f"产物对账比 {ratio} 超 ±2%"
    # 零入柱 honesty：无入柱则共识空态、evidence_only=true（真裁定非故障）
    if not latest["admitted"]:
        assert latest["evidence_only"] is True
        assert consensus["items"] == []


# ---------------------------------------------------------------- v2 面


@pytest.fixture(scope="module")
def v2_seats(product):
    seats = ds.load_seats()
    if seats is None or "lb_skill_bp" not in seats.columns:
        pytest.skip("席位榜 v2 产物未重建——重建后自动纳入")
    return seats


def test_席位榜v2技能列与下界纪律(v2_seats):
    """30 列契约；下界 ⇔ n_exc10 ≥ SEAT_MIN_EVENTS（假标 90% 绝不入榜）；榜首必须显著。"""
    seats = v2_seats
    assert set(seats.columns) == SEAT_V2_COLUMNS
    has_lb = seats["lb_skill_bp"].notna()
    assert bool(has_lb.any()) and bool((~has_lb).any())  # 两态都存在（小样本确沉底）
    assert bool((seats.loc[has_lb, "n_exc10"] >= SEAT_MIN_EVENTS).all())
    assert bool((seats.loc[~has_lb, "n_exc10"] < SEAT_MIN_EVENTS).all())
    # 下界 ≤ 技能点值（同源减惩罚项；容 0.05 舍入）；技能 t 与技能同号
    both = seats["lb_skill_bp"].notna() & seats["skill_bp"].notna()
    assert bool(
        (seats.loc[both, "lb_skill_bp"] <= seats.loc[both, "skill_bp"] + 0.05).all()
    )
    # 技能 t 与技能点值同号（两边都过舍入死区才参与：±0.1bp/±0.005 以下会被圆成 0）
    st = seats["skill_t"].abs().gt(0.005) & seats["skill_bp"].abs().gt(0.05)
    assert bool((seats.loc[st, "skill_t"] * seats.loc[st, "skill_bp"] > 0).all())
    # 默认榜（前端 n_events ≥ 50）榜首必须显著：弱席位不得靠噪声/未成熟小样本上位
    top50 = seats[(seats["window"] == "all") & (seats["n_events"] >= 50)].sort_values(
        "lb_skill_bp", ascending=False
    )
    assert len(top50) >= 10
    head = top50.iloc[0]
    assert float(head["skill_t"]) >= 2.0, (
        f"默认榜榜首 {head['participant_name']} skill_t={head['skill_t']}——"
        "榜首不再显著，机构优势叙事须人工复核"
    )
    assert int(head["n_exc10"]) >= 50


def test_席位v2月度标的产物自洽(v2_seats):
    """月序/标的聚合金样口径=只用 fwd_exc_10 可测样本 → 对账基线 n_exc10（非 n_events）。"""
    monthly = ds.load_seat_monthly()
    symbols = ds.load_seat_symbols()
    if monthly is None or symbols is None:
        pytest.skip("v2 月序/标的产物未重建——重建后自动纳入")
    assert list(monthly.columns) == [
        "participant_id",
        "month",
        "n",
        "skill_bp",
        "vw_skill_bp",
        "hit",
    ]
    assert list(symbols.columns) == [
        "participant_id",
        "symbol",
        "stock_name",
        "n",
        "skill_bp",
        "vw_skill_bp",
        "last_dt",
    ]
    assert bool(monthly["n"].ge(1).all()) and bool(symbols["n"].ge(1).all())
    assert bool(monthly["month"].str.fullmatch(r"\d{4}-\d{2}").all())
    # 4/5 位皆合法：双柜台人民币柜位（80xxx/81xxx）为历史五位码，未强制统一
    assert bool(symbols["symbol"].str.fullmatch(r"\d{4,5}\.HK").all())
    assert float(symbols["stock_name"].notna().mean()) > 0.8  # 缺名允许，大面积缺即异常
    # 可测事件集两面同源：按席求和 == all 窗 n_exc10（总量亦相等）
    allw = v2_seats[v2_seats["window"] == "all"].set_index("participant_id")
    for agg in (
        monthly.groupby("participant_id")["n"].sum(),
        symbols.groupby("participant_id")["n"].sum(),
    ):
        base = allw["n_exc10"].reindex(agg.index)
        assert bool((agg == base).all()), "聚合计数与 n_exc10 口径漂移"
    assert int(monthly["n"].sum()) == int(allw["n_exc10"].sum())
    # 月序技能按 n 加权回算 = 席位全窗技能（仅 0.1bp 舍入差）
    m = monthly.merge(
        allw["skill_bp"].rename("seat_skill_bp"),
        left_on="participant_id",
        right_index=True,
    )
    m["w"] = m["skill_bp"] * m["n"]
    tot = m.groupby("participant_id")[["w", "n"]].sum()
    wmean = tot["w"] / tot["n"]
    base = allw["skill_bp"].reindex(wmean.index)
    assert float((wmean - base).abs().max()) < 1.0
    # 标的 last_dt 顶日 ≤ 席位 last_event_date 顶日（可测样本 ⊆ 全事件）
    assert str(symbols["last_dt"].max()) <= str(v2_seats["last_event_date"].max())


def test_类别技能与持续性审计产物(product):
    """latest.categories 技能列四类非空/南向空；persistence 弱持续护栏（0 < h < 0.3）。"""
    latest = ds.load_product_json("latest")
    by = {c["category"]: c for c in latest["categories"]}
    for cat in CCASS_CATEGORIES:
        if cat == "other":  # 'other' 极稀疏（实测 0 可测事件）→ 技能空是数据事实
            continue
        assert by[cat]["skill_bp"] is not None, f"{cat} 类别技能缺失"
        assert by[cat]["vw_skill_bp"] is not None
        assert by[cat]["n_lead_events"] > 0
    assert by["southbound"]["skill_bp"] is None
    assert by["southbound"]["n_lead_events"] is None

    pj = ds.load_product_json("seat_persistence")
    if pj is None:
        pytest.skip("持续性审计产物未重建——重建后自动纳入")
    assert pj["method"]["note"], "弱持续叙事必须随产物出墙（防止被当可跟随信号）"
    sp = pj["spearman"]
    # 区间护栏：弱正但 < 0.3——转负/走强都须人工复核（细案 §2 已知事实背板）
    assert 0.0 < sp["h10"] < 0.3 and 0.0 < sp["h20"] < 0.3
    assert abs(sp["hit"]) < 0.3
    assert len(pj["quartiles"]) == 4
    assert {h["category"] for h in pj["category_halves"]} == set(CCASS_CATEGORIES)
    assert pj["scatter"] and all(s["n1"] >= 20 and s["n2"] >= 10 for s in pj["scatter"])
