"""``decision_round_feeds``（Stage 2 第二批取数席）单测：文件/序列/台账/唤醒上下文。

口径纪律（模块 docstring）：文件不存在与坏文件都 → ``None``（级别区分）；序列
逐码尽力、绝不零值冒充、任何一环失败不抛；台账读失败**上抛**（装配层防谎报
「今日首轮」）；唤醒上下文读写器由调用方传入（同库契约）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.services.trade.services import decision_round_feeds as F
from backend.services.trade.services.decision_round_core import TENANT_ID

DAY = date(2026, 9, 24)
CN_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(CN_TZ).strftime("%Y%m%d")


# ── 文件面（news_brief / 风险三文件）──────────────────────────────────
def _write(root: Path, name: str, payload: object) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_news_files_parses_both_and_missing_is_none(tmp_path):
    _write(tmp_path, "news_brief/latest.json", {"ts": "2026-09-24T09:30:00+08:00"})
    brief, watch = F.read_news_files(root=tmp_path)
    assert brief == {"ts": "2026-09-24T09:30:00+08:00"}
    assert watch is None  # 观察文件缺席：缺谁谁 None，与「坏文件」同形

    _write(tmp_path, "news_brief/intraday_watch.json", {"items": []})
    brief, watch = F.read_news_files(root=tmp_path)
    assert watch == {"items": []}


def test_read_news_files_bad_or_non_object_json_is_none(tmp_path):
    (tmp_path / "news_brief").mkdir()
    (tmp_path / "news_brief/latest.json").write_text("{不是JSON", encoding="utf-8")
    _write(tmp_path, "news_brief/intraday_watch.json", ["不是对象"])
    brief, watch = F.read_news_files(root=tmp_path)
    assert brief is None and watch is None


def test_read_news_files_merges_fast_decisions(tmp_path):
    _write(tmp_path, "news_brief/latest.json", {"ts": "2026-09-24T09:30:00+08:00"})
    _write(tmp_path, "news_brief/news_decisions.json", {
        "as_of": "2026-09-24T10:05:00+08:00",
        "items": [{"decision_id": f"{TODAY}-600519.SH-1", "code": "600519.SH", "decision": "P0"}],
    })
    brief, _watch = F.read_news_files(root=tmp_path)
    assert brief["decisions"][0]["decision"] == "P0"
    assert brief["ts"] == "2026-09-24T09:30:00+08:00"


def test_read_news_files_fast_decisions_without_latest(tmp_path):
    _write(tmp_path, "news_brief/news_decisions.json", {
        "as_of": "2026-09-24T10:05:00+08:00",
        "items": [{"decision_id": f"{TODAY}-600519.SH-1", "code": "600519.SH", "decision": "P0"}],
    })
    brief, _watch = F.read_news_files(root=tmp_path)
    assert brief["decisions"][0]["code"] == "600519.SH"
    assert brief["ts"] == "2026-09-24T10:05:00+08:00"


def test_read_news_files_drops_stale_fast_decisions(tmp_path):
    _write(tmp_path, "news_brief/latest.json", {"ts": "2026-09-24T09:30:00+08:00"})
    _write(tmp_path, "news_brief/news_decisions.json", {
        "as_of": "2026-01-01T10:05:00+08:00",
        "items": [{"decision_id": "20260101-600519.SH-1", "code": "600519.SH", "decision": "P0"}],
    })
    brief, _watch = F.read_news_files(root=tmp_path)
    assert brief.get("decisions") == []


def test_read_risk_files_returns_the_three_slots(tmp_path):
    _write(tmp_path, "risk_block.json", {"asof": "2026-09-24", "items": {}})
    _write(tmp_path, "industry_risk.json", {"asof": "2026-09-24", "items": []})
    risk, industry, fund = F.read_risk_files(root=tmp_path)
    assert risk == {"asof": "2026-09-24", "items": {}}
    assert industry == {"asof": "2026-09-24", "items": []}
    assert fund is None  # 第三个文件缺席

    risk, industry, fund = F.read_risk_files(root=tmp_path / "空空如也")
    assert (risk, industry, fund) == (None, None, None)


def test_data_root_prefers_env_then_reports_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_DATA_DIR", str(tmp_path / "qm"))
    assert F.data_root() == tmp_path / "qm"
    monkeypatch.delenv("QM_DATA_DIR")
    monkeypatch.setattr(F, "reports_dir", lambda: tmp_path / "root/reports")
    assert F.data_root() == tmp_path / "root"


# ── 序列面（market:series → SeriesSummary）────────────────────────────
class FakePipeline:
    def __init__(self, results):
        self.results = results
        self.calls: list[tuple] = []

    def zrangebyscore(self, key, lo, hi, withscores=False):  # noqa: A002
        self.calls.append((key, lo, hi, withscores))

    def execute(self):
        return self.results


class FakeSeriesClient:
    def __init__(self, results=None, *, boom=False):
        self.pipeline_obj = FakePipeline(results or [])
        self.boom = boom
        self.closed = False

    def pipeline(self):
        if self.boom:
            raise RuntimeError("远端行情连不上")
        return self.pipeline_obj

    def close(self):
        self.closed = True


def _member(price, volume=None, source="tdx_bridge") -> str:
    payload: dict = {"price": price}
    if volume is not None:
        payload["volume"] = volume
    if source is not None:
        payload["source"] = source
    return json.dumps(payload)


#: 五条成员（score 升序，与 zrangebyscore withscores 返回序一致；ts 均在 1h 窗内）。
#: 成员在前、score 在后——zrangebyscore 的 (member, score) 契约。
ROWS_600036 = [
    (_member(9.0, volume=280), 997_000.0),
    (_member(9.5, volume=290), 998_000.0),
    (_member(10.2, volume=295), 999_300.0),
    (_member(10.4, volume=300), 999_800.0),
    (_member(10.5, volume=500), 1_000_000.0),
]
NOW_TS = 1_100_000.0


def test_series_summaries_momentum_windows_and_same_source_volume():
    client = FakeSeriesClient([list(ROWS_600036)])
    out = F.series_summaries(["600036.SH"], now_ts=NOW_TS, client=client)
    key, lo, hi, withscores = client.pipeline_obj.calls[0]
    assert key == "market:series:SH600036"
    assert lo == NOW_TS - F._SERIES_WINDOW_S and hi == "+inf" and withscores is True
    s = out["600036.SH"]
    # 30 分钟参考 = (last_ts − 30min) 前最近一条：999_300 在窗外的 30 分钟线内
    # （998_000 ≤ last_ts−1800=998_200 的最后一条是 998_000，价 9.5）
    assert s.mom30m_pct == round((10.5 - 9.5) / 9.5 * 100.0, 4)
    # 5 分钟参考 = 999_300（10.2）
    assert s.mom5m_pct == round((10.5 - 10.2) / 10.2 * 100.0, 4)
    # 5 分钟量 = 同源最近两条之差（500−300 手 → ×100 = 股）
    assert s.vol5m_shares == 20_000.0
    assert s.last_ts == 1_000_000.0 and s.last_price == 10.5
    assert s.source == "tdx_bridge"
    assert client.closed is False  # 调用方传入的客户端不归本函数关


def test_series_summaries_unreadable_codes_stay_out_of_the_map():
    rows_bad_json = [("{坏成员", 1_000_000.0)]
    rows_no_price = [(_member(None), 1_000_000.0)]
    rows_zero_price = [(_member(0.0), 1_000_000.0)]
    client = FakeSeriesClient([rows_bad_json, rows_no_price, rows_zero_price])
    out = F.series_summaries(
        ["600036.SH", "600519.SH", "601988.SH"], now_ts=NOW_TS, client=client
    )
    assert out == {}  # 逐码尽力：不可验的码**不进表**（绝不零值摘要冒充）


def test_series_summaries_empty_window_and_bad_code_never_touch_redis():
    client = FakeSeriesClient([[]])
    assert F.series_summaries(["600036.SH"], now_ts=NOW_TS, client=client) == {}
    # 归一不出通道键的码（series_key_for=None）→ 连 Redis 都不碰
    untouched = FakeSeriesClient([])
    assert F.series_summaries(["???"], now_ts=NOW_TS, client=untouched) == {}
    assert untouched.pipeline_obj.calls == []


def test_series_summaries_degrades_on_every_failure_and_closes_own_client(
    monkeypatch,
):
    # ① 管道构造/执行失败 → 空表（增益面降级），自造客户端照关
    owned = FakeSeriesClient(boom=True)
    monkeypatch.setattr(F, "make_sync_client", lambda: owned)
    assert F.series_summaries(["600036.SH"], now_ts=NOW_TS) == {}
    assert owned.closed is True

    # ② 客户端构造失败 → 空表
    def boom():
        raise RuntimeError("远端行情未配置")

    monkeypatch.setattr(F, "make_sync_client", boom)
    assert F.series_summaries(["600036.SH"], now_ts=NOW_TS) == {}
    # ③ make_sync_client 返回 None（未配置远端行情）→ 空表
    monkeypatch.setattr(F, "make_sync_client", lambda: None)
    assert F.series_summaries(["600036.SH"], now_ts=NOW_TS) == {}


# ── 台账面（load_decision_rows：读主库，失败上抛）────────────────────
@pytest.mark.asyncio
async def test_load_decision_rows_reads_primary_and_forwards_scope(monkeypatch):
    from backend.shared import database_manager_v2 as db
    from backend.shared import decision_ledger_store as store

    seen: dict = {}

    class _Session:
        async def __aenter__(self):
            return "SESSION"

        async def __aexit__(self, *exc):
            return False

    def fake_get_session(*, read_only=False):
        seen["read_only"] = read_only
        return _Session()

    async def fake_load_day(session, trade_date, *, tenant_id, user_id):
        seen["args"] = (session, trade_date, tenant_id, user_id)
        return ("ROW",)

    monkeypatch.setattr(db, "get_session", fake_get_session)
    monkeypatch.setattr(store, "load_day", fake_load_day)
    rows = await F.load_decision_rows(DAY, "10000001")
    assert rows == ("ROW",)
    # 读主库：从库滞后会把上一轮藏成「今日首轮」（谎报比缺块危险）
    assert seen["read_only"] is False
    assert seen["args"] == ("SESSION", DAY, TENANT_ID, "10000001")


@pytest.mark.asyncio
async def test_load_decision_rows_reraises_so_the_assembly_layer_can_degrade(
    monkeypatch,
):
    from backend.shared import database_manager_v2 as db

    def boom(*, read_only=False):
        raise RuntimeError("主库连接炸了")

    monkeypatch.setattr(db, "get_session", boom)
    with pytest.raises(RuntimeError, match="主库连接炸了"):
        await F.load_decision_rows(DAY, "10000001")


# ── 通达信 L2 快照面（丁2：付费 L2 因子进决策提示词）─────────────────
# 口径：读**主库**（采集任务写 PG 后下一轮就要看见）；symbol 是前缀码、
# trade_date 是 YYYYMMDD；factors 可能是 JSONB(已解析 dict) 或驱动给的 JSON 串，
# 两种都要能吃。读失败上抛，由装配层降级留痕。
class _L2Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _L2Session:
    def __init__(self, rows, seen):
        self._rows = rows
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params):
        self._seen["sql"] = str(stmt)
        self._seen["params"] = dict(params)
        return _L2Result(self._rows)


def _patch_l2_session(monkeypatch, rows, seen, *, boom=False):
    from backend.shared import database_manager_v2 as db

    def fake_get_session(*, read_only=True):
        seen["read_only"] = read_only
        if boom:
            raise RuntimeError("主库连接炸了")
        return _L2Session(rows, seen)

    monkeypatch.setattr(db, "get_session", fake_get_session)


@pytest.mark.asyncio
async def test_l2_snapshots_read_primary_and_parse_factors(monkeypatch):
    seen: dict = {}
    rows = [
        {
            "symbol": "SH600036",
            "ts": "T0",
            "signal_score": 52.3,
            "factors": '{"micro_vpin_vol_ratio": 0.21}',
            "cjbs": 13604,
        }
    ]
    _patch_l2_session(monkeypatch, rows, seen)

    out = await F.l2_snapshots(["600036.SH", "600036.SH", ""], DAY)

    # 读主库：采集刚写的快照必须当轮可见（从库滞后会把 L2 块整段降级）
    assert seen["read_only"] is False
    assert seen["params"]["day"] == "20260924"
    assert seen["params"]["codes"] == ["SH600036"]  # 后缀→前缀、去重、空码剔除
    assert "DISTINCT ON (symbol)" in seen["sql"]
    assert out == {
        "SH600036": {
            "symbol": "SH600036",
            "ts": "T0",
            "signal_score": 52.3,
            "factors": {"micro_vpin_vol_ratio": 0.21},
            "cjbs": 13604,
        }
    }


@pytest.mark.asyncio
async def test_l2_snapshots_accepts_an_already_parsed_factor_mapping(monkeypatch):
    seen: dict = {}
    _patch_l2_session(
        monkeypatch,
        [{"symbol": "SH600036", "factors": {"vol_price_divergence": 0.3}}],
        seen,
    )
    out = await F.l2_snapshots(["600036.SH"], DAY)
    assert out["SH600036"]["factors"] == {"vol_price_divergence": 0.3}


@pytest.mark.asyncio
async def test_l2_snapshots_bad_factors_degrade_to_empty_mapping(monkeypatch):
    seen: dict = {}
    _patch_l2_session(monkeypatch, [{"symbol": "SH600036", "factors": "{坏JSON"}], seen)
    out = await F.l2_snapshots(["600036.SH"], DAY)
    # 因子坏了只丢因子，快照行本身（分/价/委撤）仍可渲染。
    assert out["SH600036"]["factors"] == {}


@pytest.mark.asyncio
async def test_l2_snapshots_without_codes_never_touches_the_db(monkeypatch):
    def boom(*, read_only=True):
        raise AssertionError("空码表不应开库")

    from backend.shared import database_manager_v2 as db

    monkeypatch.setattr(db, "get_session", boom)
    assert await F.l2_snapshots([], DAY) == {}
    assert await F.l2_snapshots(["", "???"], DAY) == {}


@pytest.mark.asyncio
async def test_l2_snapshots_reraises_so_the_assembly_layer_can_degrade(monkeypatch):
    _patch_l2_session(monkeypatch, [], {}, boom=True)
    with pytest.raises(RuntimeError, match="主库连接炸了"):
        await F.l2_snapshots(["600036.SH"], DAY)


# ── 唤醒上下文（read_wake_ctx：无记录/读失败 → ""，绝不抛）───────────
class FakeCtxClient:
    def __init__(self, value=None, *, boom=False):
        self.value = value
        self.boom = boom
        self.asked: list[str] = []

    def get(self, key):
        self.asked.append(key)
        if self.boom:
            raise RuntimeError("redis 读不下来")
        return self.value


def test_read_wake_ctx_round_trip_key_shape_and_degrades_to_empty():
    client = FakeCtxClient("【本轮为触发唤醒】…")
    assert F.read_wake_ctx(client, "rnd-20260924-1052") == "【本轮为触发唤醒】…"
    assert client.asked == [
        "trade:decision-round:wake:ctx:rnd-20260924-1052"  # 键形与写侧同源
    ]
    # 无记录（非唤醒轮）/ 空 round_id / 无客户端 → 空串，且空 round_id 不留查询
    quiet = FakeCtxClient(None)
    assert F.read_wake_ctx(quiet, "rnd-20260924-0935") == ""
    fresh = FakeCtxClient(None)
    assert F.read_wake_ctx(fresh, "") == "" and fresh.asked == []
    assert F.read_wake_ctx(None, "rnd-20260924-1052") == ""
    # 读失败 → 空串（退化为普通轮），绝不抛
    assert F.read_wake_ctx(FakeCtxClient(boom=True), "rnd-1") == ""
