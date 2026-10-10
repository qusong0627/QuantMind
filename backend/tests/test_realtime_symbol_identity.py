"""T4-1（审计 M2）：000001 撞键——身份=后缀式，全链不得跨市场折叠。

缺陷：热集常驻上证指数 ``000001.SH``（REGIME_INDEXES 不占 cap 永不截断），一旦平安
银行 ``000001.SZ`` 也进热集，旧实现 ``digits()`` 把两者折叠成同一键——指数吃股票的
基线/历史/窗口帧；账本 ``symbols`` 两行不分；``engine_signal_scores`` 冲突键
(tenant,user,day,symbol,model,feature,run) 把两行并成一行；回放 ``group_frames`` 把
指数与股票的归档帧混进同一指针列表。修复：``identity()``（后缀式）是唯一身份空间
（热集→矩阵行序→账本→回放指针→落库冲突键）。

本文件四处钉住：① ``identity()`` 归一语义；② ``compute_cycle`` 两行且指数不吃
股票基线行；③ 归档分组/窗口加载不跨市场串帧；④ 全链 ``build_cycle``→真库
两行 + 回放 diff=0（同批含 000001.SH+000001.SZ 的验收口径）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from backend.services.engine.inference import realtime_core as rc

_CST = timezone(timedelta(hours=8))


class _SumSession:
    """伪 ONNX 会话：原样收下输入矩阵，按行求和当分数。"""

    def __init__(self) -> None:
        self.seen: np.ndarray | None = None

    def get_inputs(self):
        class _I:
            name = "features"

        return [_I()]

    def run(self, _outputs, feeds):
        x = feeds["features"]
        self.seen = x
        return [x.reshape(x.shape[0], -1).sum(axis=1)]


# ── 1. identity() 归一语义 ──────────────────────────────────────────


@pytest.mark.unit
def test_identity_normalizes_to_suffix_and_never_folds_market():
    from backend.services.engine.inference.realtime_core import digits, identity

    assert identity("600036") == "600036.SH"
    assert identity("SH600036") == "600036.SH"
    assert identity("600036.SH") == "600036.SH"
    assert identity("830001") == "830001.BJ"
    # 身份不折叠：指数与同数字股票必须各是各的
    assert identity("000001.SH") != identity("000001.SZ")
    assert identity("000001.SH") == "000001.SH" and identity("000001.SZ") == "000001.SZ"
    # 记录 digits 的危险折叠：它只允许用于连源行键（因子源/快照表是股票表）
    assert digits("000001.SH") == digits("000001.SZ")
    # 场内基金（热集实测含 159518.SZ、501018.SH）：股票前缀规则不覆盖，
    # 15/16/18→SZ、5→SH；号段收窄不猜可转债（11/12 原样）
    assert identity("159518") == "159518.SZ"
    assert identity("501018") == "501018.SH"
    assert identity("123456") == "123456"  # 12xxxx=深市可转债号段，不猜
    # 识别不了的形态原样返回（实时链仅 CN；不因归一失败丢符号）
    assert identity("BTCUSDT") == "BTCUSDT"
    assert identity("") == ""


# ── 2. compute_cycle：同批两行 + 指数不吃股票基线 ───────────────────


@pytest.mark.unit
def test_cycle_keeps_index_and_stock_apart_same_digits():
    """因子源里只有平安银行一行（000001 → 000001.SZ）。指数 000001.SH 必须
    查不到基线（走 fill），而不是经 digits 折叠串吃股票行——旧实现 ready 会算 2。"""
    session = _SumSession()
    result = rc.compute_cycle(
        session=session,
        input_name="features",
        cols=["a"],
        fill={"a": -7.0},
        model_version="t",
        hot=["000001.SH", "000001.SZ"],
        snapshots={},
        baseline={"000001.SZ": {"a": 1.0}},
        histories={},
        override=set(),
        engine=None,
        bootstrapped=set(),
    )

    assert result.symbols == ["000001.SH", "000001.SZ"]  # 两行身份，不折叠
    assert session.seen is not None
    assert session.seen[0, 0] == pytest.approx(-7.0)  # 指数：无基线行 → fill
    assert session.seen[1, 0] == pytest.approx(1.0)  # 平安银行：吃到自己的行
    assert result.ready == 1  # 只有股票有真实基线


# ── 3. 归档分组 / 窗口加载：不跨市场串帧 ────────────────────────────


@pytest.mark.unit
def test_group_frames_keeps_market_identity():
    import pandas as pd

    from backend.services.engine.inference.replay_verifier import group_frames

    df = pd.DataFrame(
        [
            {"symbol": "000001.SH", "ts": 1, "price": 3000.0},
            {"symbol": "000001.SH", "ts": 2, "price": 3001.0},
            {"symbol": "000001.SZ", "ts": 1, "price": 10.5},
        ]
    )

    got = group_frames(df)

    assert set(got) == {"000001.SH", "000001.SZ"}  # 旧实现折叠成单键 "000001"
    assert [f["price"] for f in got["000001.SH"]] == [3000.0, 3001.0]
    assert [f["price"] for f in got["000001.SZ"]] == [10.5]


@pytest.mark.unit
def test_window_loader_does_not_cross_feed_index_and_stock():
    import pandas as pd

    from backend.tests.test_realtime_sequence_window import _FakeReader, _meta_quantdb

    df = pd.DataFrame(
        {
            "symbol": ["000001"],  # 因子源只有平安银行（纯数字行键）
            "trade_date": ["2026-09-28"],
            "a": [9.0],
            "b": [90.0],
        }
    )
    reader = _FakeReader(["2026-09-28", "2026-09-29"], df)

    got = rc.load_window_quantdb(
        ["000001.SH", "000001.SZ"],
        date(2026, 10, 1),
        meta=_meta_quantdb(),
        cols=["a", "b"],
        step_len=3,
        reader=reader,
    )

    # 帧键=后缀身份：股票拿到自己的帧；指数同数字但不串帧（整帧缺席→None）
    assert got["frames"]["000001.SZ"] == [None, {"a": 9.0, "b": 90.0}]
    assert got["frames"]["000001.SH"] == [None, None]


# ── 4. 全链：真库两行 + 回放 diff=0 ─────────────────────────────────


def _history(sym: str, closes: list[float]):
    """价格历史（增量引擎引导用；symbol 列=传入身份，逐标的各一份）。"""
    import pandas as pd

    n = len(closes)
    return pd.DataFrame(
        {
            "symbol": [sym] * n,
            "trade_date": pd.date_range("2026-08-01", periods=n, freq="B"),
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1e6] * n,
            "amount": [1e7] * n,
        }
    )


def _collision_fixtures(ts: float | None = None):
    """热集含指数+同数字股票；快照两串（同 ts）；历史各是各的，基线行只有股票。

    现实口径：因子源（l1/l2）是**股票表**——指数 000001.SH 在其中忠实缺席
    （旧实现 digits 折叠后指数会串吃平安银行的基线行，这正是 M2 的主实害）。
    """
    from backend.tests.test_replay_verifier import _recent_ts, _snap

    ts = _recent_ts(30.0) if ts is None else float(ts)
    snaps = {"000001.SH": _snap(3000.0, ts), "000001.SZ": _snap(10.5, ts)}
    rows = {
        "000001.SZ": {
            "mom_ret_1d": -0.02,
            "f1": 0.1,
            "f2": 0.4,
            "f3": -0.2,
            "f4": 0.0,
            "f5": 0.2,
        },
    }
    history = {
        "000001.SH": _history("000001.SH", [3000.0 + i for i in range(25)]),
        "000001.SZ": _history("000001.SZ", [10.4] * 25),
    }
    return ["000001.SH", "000001.SZ"], snaps, {"rows": rows, "history": history}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_chain_writes_two_rows_for_colliding_digits(tmp_path):
    """验收口径（T4-1）：同批含 000001.SH+000001.SZ → engine_signal_scores **两行**。

    旧实现两行在冲突键上折叠成一行（指数行存成 000001 被股票行覆盖/互踩）。"""
    import uuid

    from sqlalchemy import text

    from backend.services.engine.inference.realtime_service import (
        RealtimeInferenceService,
    )
    from backend.shared.database_manager_v2 import get_session
    from backend.tests.test_realtime_inference import _cfg, _make_model_dir

    model_dir = _make_model_dir(tmp_path)
    hot, snaps, bundle = _collision_fixtures()
    suffix = uuid.uuid4().hex[:8]
    svc = RealtimeInferenceService(
        config_loader=lambda: _cfg(model_dir),
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: snaps,
        baseline_loader=lambda syms, day: bundle,
        ledger_sink=lambda _e: None,  # 测试绝不写生产账本（Redis）
    )
    session = _SumSession()
    svc._ensure_session = lambda model_dir_, n_features, seq_len=1: session  # type: ignore[method-assign]
    payload = svc.build_cycle()
    assert payload is not None
    assert [s["symbol"] for s in payload["scores"]] == ["000001.SH", "000001.SZ"]
    # 唯一化 run_id/model_version，避免与生产数据互相污染
    payload["run_id"] = f"rt-test-{suffix}"
    payload["model_version"] = f"rt-test-{suffix}"

    await svc._default_publish(payload, _cfg(model_dir))
    try:
        async with get_session(read_only=True) as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT symbol FROM engine_signal_scores "
                        "WHERE run_id=:r ORDER BY symbol"
                    ),
                    {"r": payload["run_id"]},
                )
            ).all()
        assert {r[0] for r in rows} == {"000001.SH", "000001.SZ"}, (
            f"指数与同数字股票必须是两行（实际 {rows}）"
        )
    finally:
        async with get_session(read_only=False) as db:
            await db.execute(
                text("DELETE FROM engine_signal_scores WHERE run_id=:r"),
                {"r": payload["run_id"]},
            )
            await db.execute(
                text("DELETE FROM engine_feature_runs WHERE run_id=:r"),
                {"r": payload["run_id"]},
            )
        # 关连接池：pytest-asyncio 每个用例一个新事件循环，池里的连接绑在**上一个**
        # 循环上——不关的话同一个会话里后面的真库用例会 "attached to a different
        # loop"（test_agent_ledger_fill 同款纪律）。
        from backend.shared.database_manager_v2 import close_database

        await close_database()


@pytest.mark.integration
def test_collision_replay_diff_zero(tmp_path):
    """同批指数+股票两周期 → 归档（两串各自）→ 回放 diff=0。

    旧实现在两处同时坏：账本 symbols 折叠成两个 "000001"，group_frames 又把两串
    归档帧并成一个指针列表——帧序/水位全乱，回放必然对不上。覆盖白名单开
    mom_ret_1d：只有 live 覆盖真吃到了逐帧快照，对账才咬得住帧的归属。
    """
    from backend.services.engine.inference.realtime_service import (
        RealtimeInferenceService,
    )
    from backend.services.engine.inference.replay_verifier import verify_day
    from backend.shared.l05_store import read_day, write_records
    from backend.tests.test_realtime_inference import _cfg, _make_model_dir
    from backend.tests.test_replay_verifier import _archive_record, _recent_ts, _snap
    from backend.tests.test_realtime_sequence_window import _FakeSession

    model_dir = _make_model_dir(tmp_path)
    ts1, ts2 = _recent_ts(30.0), _recent_ts(15.0)
    hot, snaps1, bundle = _collision_fixtures(ts1)
    # 第二周期快照必须整串重建（High/Low 等字段与归档 _archive_record 默认逐字段一致）
    snaps2 = {"000001.SH": _snap(3001.0, ts2), "000001.SZ": _snap(10.6, ts2)}

    ledger: list[dict] = []
    current = [snaps1]
    cfg = _cfg(model_dir, whitelist=("mom_ret_1d",))
    svc = RealtimeInferenceService(
        config_loader=lambda: cfg,
        hot_set_fetcher=lambda: hot,
        snapshot_fetcher=lambda syms: current[0],
        baseline_loader=lambda syms, day: bundle,
        ledger_sink=ledger.append,
    )
    svc._ensure_session = lambda model_dir_, n_features, seq_len=1: _FakeSession()  # type: ignore[method-assign]
    assert svc.build_cycle() is not None
    current = [snaps2]
    assert svc.build_cycle() is not None
    assert len(ledger) == 2
    assert ledger[0]["cuts"] and ledger[1]["cuts"]  # 两周期快照水印都落账
    assert ledger[1]["symbols"] == ["000001.SH", "000001.SZ"]  # 账本两行身份

    l05_base = tmp_path / "l05"
    day = datetime.now(tz=_CST).date()
    records = []
    for sym, price in (("000001.SH", 3000.0), ("000001.SZ", 10.5)):
        records.append(_archive_record(sym, price, ts1))
    for sym, price in (("000001.SH", 3001.0), ("000001.SZ", 10.6)):
        records.append(_archive_record(sym, price, ts2))
    write_records(records, base_dir=str(l05_base))

    frames = read_day(day, base_dir=str(l05_base))
    assert len(frames) == 4
    report = verify_day(
        day=day,
        model_dir=model_dir,
        ledger=ledger,
        frames=frames,
        baseline_bundle=bundle,
        session_factory=lambda _p, _n: _FakeSession(),
    )

    assert report["diff_zero"] is True, report.get("mismatch_details")
    assert report["entries"] == 2
