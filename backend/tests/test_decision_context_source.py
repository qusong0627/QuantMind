"""P2.2 IO 适配：QM 四个数据面 → RebalanceContext 的边界与口径。

重点是**别把「没采到」和「采到了但馊了」混成一个数字**、以及**别把缺失渲染成 0**
（本仓口径）。真语料冒烟在 ``docs/local/diff_decision_prompt_vs_baymax.py``。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.shared.decision.context import (
    MISSING,
    DirectionBlock,
    PoolRow,
    render_prompt,
)
from backend.shared.decision_context_source import (
    SNAPSHOT_TTL_S,
    build_context,
    day_change_pct,
    load_pool_doc,
    pool_path,
    positions_to_holding_rows,
    quotes_from_snapshots,
    read_snapshots,
    snapshot_age_min,
    snapshot_key,
)

CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 23, 9, 35, 12, tzinfo=CN)


def _snap(now_px: float, pre: float, ts: float | None, **extra: object) -> dict:
    d: dict = {"Now": now_px, "PreClose": pre}
    if ts is not None:
        d["timestamp"] = ts
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 池文件
# ---------------------------------------------------------------------------


@pytest.fixture
def pool_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "stock_picks"
    d.mkdir(parents=True)
    monkeypatch.setenv("QM_REPORTS_DIR", str(tmp_path))
    return d


def test_pool_path_prefers_agent_picks_then_legacy(pool_env: Path) -> None:
    """两代命名都要认：``{d}_agent_picks.json``（现行）优先于 ``{d}_picks.json``。"""
    (pool_env / "20260101_picks.json").write_text("{}", encoding="utf-8")
    assert pool_path("20260101").name == "20260101_picks.json"
    (pool_env / "20260101_agent_picks.json").write_text("{}", encoding="utf-8")
    assert pool_path("20260101").name == "20260101_agent_picks.json"
    assert pool_path("20991231") is None


def _write_pool(d: Path, day: str, doc: object) -> None:
    (d / f"{day}_agent_picks.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )


def test_load_pool_doc_records_missing_columns_instead_of_inventing(
    pool_env: Path,
) -> None:
    """QM 产物没有 rank/industry/fusion —— 登记缺列，**不编**行业名。"""
    _write_pool(
        pool_env,
        "20260101",
        {
            "date": "20260101",
            "market_direction": {"direction": "偏多"},
            "picks": [
                {
                    "code": "SH600237",
                    "name": "铜峰电子",
                    "score": 29.77,
                    "reason": "薄膜电容",
                },
                {
                    "code": "002636.SZ",
                    "name": "金安国纪",
                    "score": 29.57,
                    "reason": "覆铜板",
                },
            ],
        },
    )
    doc = load_pool_doc("20260101")
    assert doc is not None
    assert doc.missing_columns == ("fusion", "industry", "rank")
    assert [r.code for r in doc.rows] == ["600237.SH", "002636.SZ"]  # 出口一律 suffix
    assert [r.rank for r in doc.rows] == [1, 2]  # 无 rank → 按出现顺序编号
    assert doc.rows[0].industry == "" and doc.rows[0].fusion is None
    assert doc.rows[0].score == 29.77
    assert doc.rows[0].remark == "薄膜电容"
    assert doc.direction.direction == "偏多"
    assert doc.direction.total_score is None


def test_load_pool_doc_marks_columns_present_when_producer_supplies_them(
    pool_env: Path,
) -> None:
    """池产物补齐这三列的那天，缺列清单必须自动变空（否则告警会永远响）。"""
    _write_pool(
        pool_env,
        "20260102",
        {
            "market_direction": {"direction": "偏空", "total_score": 3},
            "picks": [
                {
                    "code": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "酿酒",
                    "score": 0.9,
                    "fusion": 0.8,
                    "rank": 7,
                    "reason": "x",
                },
            ],
        },
    )
    doc = load_pool_doc("20260102")
    assert doc is not None and doc.missing_columns == ()
    assert doc.rows[0].rank == 7 and doc.rows[0].industry == "酿酒"
    assert doc.direction.total_score == 3


def test_load_pool_doc_top_and_absent_day(pool_env: Path) -> None:
    _write_pool(
        pool_env,
        "20260103",
        {
            "picks": [
                {"code": f"60000{i}.SH", "name": f"n{i}", "score": 1.0}
                for i in range(5)
            ]
        },
    )
    doc = load_pool_doc("20260103", top=2)
    assert doc is not None and len(doc.rows) == 2
    assert load_pool_doc("20991231") is None


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ("{not json", "坏 JSON"),
        ({"picks": "不是列表"}, "picks 类型坏"),
    ],
)
def test_load_pool_doc_bad_file_returns_none_not_crash(
    pool_env: Path, payload: object, why: str
) -> None:
    """池读不动是**当天还没跑出池**的常态，不该炸穿调度；但也绝不返回假池。"""
    p = pool_env / "20260104_agent_picks.json"
    p.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    assert load_pool_doc("20260104") is None, why


def test_load_pool_doc_skips_rows_without_code(pool_env: Path) -> None:
    _write_pool(
        pool_env,
        "20260105",
        {
            "picks": [
                {"name": "无码"},
                {"code": "600519.SH", "name": "贵州茅台", "score": 1.0},
            ]
        },
    )
    doc = load_pool_doc("20260105")
    assert doc is not None and [r.code for r in doc.rows] == ["600519.SH"]


# ---------------------------------------------------------------------------
# 快照键与字段
# ---------------------------------------------------------------------------


def test_snapshot_key_is_lowercase_prefix_like_the_writer() -> None:
    """写侧契约是 ``market:snapshot:sh600036``（小写）。写成大写读到的是空。"""
    assert snapshot_key("600036.SH") == "market:snapshot:sh600036"
    assert snapshot_key("SH600036") == "market:snapshot:sh600036"
    assert snapshot_key("000001.SZ") == "market:snapshot:sz000001"
    assert snapshot_key("BJ920950") == "market:snapshot:bj920950"


def test_snapshot_ttl_matches_writer() -> None:
    """键 TTL 与采集侧同值——不一致会让「键还在」不再等于「5 分钟内有帧」。"""
    assert SNAPSHOT_TTL_S == 300


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        ({"Now": 11.0, "PreClose": 10.0}, 10.0),
        ({"now": 11.0, "pre_close": 10.0}, 10.0),  # 原始推送字段那套
        ({"Now": 9.0, "PreClose": 10.0}, -10.0),
        ({"Now": 11.0}, None),  # 缺昨收 → 不给涨跌（不臆造）
        ({"PreClose": 10.0}, None),
        ({"Now": 11.0, "PreClose": 0}, None),  # 昨收 0 是脏值，不是「涨无穷」
        ({"Now": "x", "PreClose": 10.0}, None),
        ({}, None),
        (None, None),
    ],
)
def test_day_change_pct(snap: object, expected: float | None) -> None:
    assert day_change_pct(snap) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_day_change_pct_rejects_nonfinite(bad: float) -> None:
    """非有限值不能混进提示词（隔壁曾在成本列渲染出 +nan%）。"""
    assert day_change_pct({"Now": bad, "PreClose": 10.0}) is None
    assert day_change_pct({"Now": 11.0, "PreClose": bad}) is None


def test_snapshot_age_min() -> None:
    ts = NOW.timestamp() - 90
    assert snapshot_age_min({"timestamp": ts}, NOW) == pytest.approx(1.5)
    assert snapshot_age_min({"ts": ts}, NOW) == pytest.approx(1.5)
    assert snapshot_age_min({}, NOW) is None
    assert snapshot_age_min({"timestamp": 0}, NOW) is None


class _FakePipeline:
    def __init__(self, client: _FakeRedis) -> None:
        self._c = client
        self._keys: list[str] = []

    def hgetall(self, key: str) -> None:
        self._keys.append(key)

    def execute(self) -> list[dict]:
        return [self._c.store.get(k, {}) for k in self._keys]


class _FakeRedis:
    def __init__(self, store: dict[str, dict]) -> None:
        self.store = store

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)


def test_read_snapshots_returns_only_present_keys() -> None:
    """缺键就是缺键——不能补一个 ``{}`` 让下游以为「读到了但价格是空」。"""
    store = {"market:snapshot:sh600036": {"Now": "40.82", "PreClose": "40.10"}}
    out = read_snapshots(_FakeRedis(store), ["600036.SH", "000001.SZ"])
    assert set(out) == {"600036.SH"}
    assert out["600036.SH"]["Now"] == "40.82"


# ---------------------------------------------------------------------------
# 池内行情块
# ---------------------------------------------------------------------------


def _pool(*codes: str) -> tuple[PoolRow, ...]:
    return tuple(PoolRow(code=c, name=f"名{c}") for c in codes)


def test_quotes_only_for_pool_codes_not_holdings() -> None:
    """行情块只列**池内**标的：否则它会变成第二个持仓表，模型读到两套价。"""
    snaps = {
        "600036.SH": _snap(40.82, 40.10, NOW.timestamp() - 60),
        "600519.SH": _snap(1523.4, 1501.0, NOW.timestamp() - 60),
    }
    quotes, stale = quotes_from_snapshots(snaps, _pool("600519.SH"), NOW)
    assert [q.code for q in quotes] == ["600519.SH"]
    assert stale == 0


def test_stale_counted_but_missing_not_counted() -> None:
    """「采到了但馊了」计数，「压根没采到」不计数——混在一起就查不出是哪种断。"""
    snaps = {
        "600519.SH": _snap(1523.4, 1501.0, NOW.timestamp() - 60),
        "000858.SZ": _snap(128.6, 130.1, NOW.timestamp() - 46 * 60),  # 过期
        # 002594.SZ 完全没有快照 → 静默跳过
    }
    quotes, stale = quotes_from_snapshots(
        snaps, _pool("600519.SH", "000858.SZ", "002594.SZ"), NOW
    )
    assert [q.code for q in quotes] == ["600519.SH"]
    assert stale == 1


def test_snapshot_without_timestamp_is_silently_skipped() -> None:
    """有价无时间戳 = 无法判断新鲜度 → 不展示（也不计入过期）。"""
    snaps = {"600519.SH": _snap(1523.4, 1501.0, None)}
    quotes, stale = quotes_from_snapshots(snaps, _pool("600519.SH"), NOW)
    assert quotes == () and stale == 0


def test_nonpositive_preclose_renders_no_change_rather_than_a_lie() -> None:
    snaps = {"600519.SH": _snap(1523.4, 0.0, NOW.timestamp() - 60)}
    (q,) = quotes_from_snapshots(snaps, _pool("600519.SH"), NOW)[0]
    assert q.pre_close is None
    assert "- 名600519.SH 600519.SH 现价 ¥1523.40" in render_prompt(
        build_context(
            agent="flash",
            holdings=(),
            pool=_pool("600519.SH"),
            direction=DirectionBlock("偏多"),
            now=NOW,
            quotes=(q,),
        )
    )


# ---------------------------------------------------------------------------
# 持仓
# ---------------------------------------------------------------------------


def test_positions_normalized_prefix_to_suffix_and_float_volume() -> None:
    """落库是 ``SH600036`` + ``volume: 200.0``；提示词要 ``600036.SH`` + 整数股。"""
    (row,) = positions_to_holding_rows(
        [
            {
                "symbol": "SH600036",
                "name": "招商银行",
                "volume": 200.0,
                "available_volume": 200.0,
                "cost_price": 41.6549,
                "price": 40.82,
            }
        ]
    )
    assert row.code == "600036.SH"
    assert row.volume == 200 and row.avail == 200
    assert row.cost == 41.65 and row.price == 40.82
    assert row.pnl_pct == pytest.approx(round((40.82 - 41.6549) / 41.6549 * 100, 2))


def test_positions_day_chg_none_when_no_snapshot() -> None:
    """没有快照 → ``day_chg`` 是 ``None``（渲染 ``—``），**不是 0.0**。

    0.0 的意思是「今天没动」，None 才是「不知道」。这条差别决定了模型会不会
    因为一个假的「今天没动」而继续持有。
    """
    (row,) = positions_to_holding_rows(
        [
            {
                "symbol": "SH600036",
                "name": "招商银行",
                "volume": 200,
                "available_volume": 200,
                "cost_price": 41.65,
                "price": 40.82,
            }
        ]
    )
    assert row.day_chg is None
    out = render_prompt(
        build_context(
            agent="flash",
            holdings=(row,),
            pool=(),
            now=NOW,
            direction=DirectionBlock("偏多"),
        )
    )
    # 盈亏是 −0.83/41.65 = −1.99%（手算过，别照抄旁边的直觉数字）
    assert "| 600036.SH | 招商银行 | 200 | 41.65* | 40.82 | -1.99% | — | 200 |" in out


def test_positions_empty_name_falls_back_to_index_or_code() -> None:
    """实测有个账户 50 只全空名——空名喂给模型等于没给标的身份。"""
    rows = positions_to_holding_rows(
        [
            {
                "symbol": "SH600036",
                "name": "",
                "volume": 100,
                "available_volume": 100,
                "cost_price": 40.0,
                "price": 40.0,
            }
        ]
    )
    assert rows[0].name  # 非空即可（索引里有什么用什么，最差回退代码）


def test_positions_missing_price_is_zero_not_a_crash() -> None:
    (row,) = positions_to_holding_rows(
        [{"symbol": "SH600036", "name": "x", "volume": 100, "available_volume": 0}]
    )
    assert row.price == 0.0 and row.cost == 0.0 and row.pnl_pct == 0.0


def test_positions_rows_without_symbol_are_skipped() -> None:
    rows = positions_to_holding_rows([{"name": "无码"}, {"symbol": "", "name": "空"}])
    assert rows == ()


def test_negative_broker_cost_survives_to_be_flagged() -> None:
    """真语料里桥给过 −11.02 —— 适配层照实带出，由渲染层判它不可信（显示 ``—``）。"""
    (row,) = positions_to_holding_rows(
        [
            {
                "symbol": "SZ002141",
                "name": "贤丰控股",
                "volume": 100,
                "available_volume": 100,
                "cost_price": -11.02,
                "price": 7.08,
            }
        ]
    )
    assert row.cost == -11.02
    out = render_prompt(
        build_context(
            agent="flash",
            holdings=(row,),
            pool=(),
            now=NOW,
            direction=DirectionBlock("偏多"),
        )
    )
    assert "| 002141.SZ | 贤丰控股 | 100 | — | 7.08 | — | — | 100 |" in out


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def test_build_context_lets_risk_tier_override_limits() -> None:
    """风控档位的单票比例 / 新开仓上限必须能传进来（defensive：10% / 1 只）。"""
    ctx = build_context(
        agent="flash",
        holdings=(),
        pool=(),
        direction=DirectionBlock("偏多"),
        now=NOW,
        quota_used=38_760.0,
        per_stock_pct=0.10,
        max_new_buys=1,
    )
    assert ctx.quota_remaining == 61_240.0
    out = render_prompt(ctx)
    assert "每票 ≤10%，当日新开仓 ≤1 只" in out
    assert "剩余 ¥61,240" in out


def test_build_context_defaults_are_the_baymax_ones() -> None:
    ctx = build_context(
        agent="flash", holdings=(), pool=(), direction=DirectionBlock("偏多"), now=NOW
    )
    assert (ctx.per_stock_pct, ctx.max_new_buys) == (0.20, 3)


def test_build_context_empty_pool_and_direction_missing_marker() -> None:
    """池为空 / 方向缺失：整段不出现 + 方向渲染 ``—``，提示词仍然成立。"""
    ctx = build_context(
        agent="flash", holdings=(), pool=(), direction=DirectionBlock(MISSING), now=NOW
    )
    out = render_prompt(ctx)
    assert "【候选池】" not in out
    assert (
        out.splitlines()[
            out.splitlines().index("【今日大盘方向】（最新研究产出）：") + 1
        ]
        == MISSING
    )
