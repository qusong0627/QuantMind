"""挖掘任务中心（rd_agent_mining_tasks）不变量 —— 机构级 P0 / T-FM-01。

盯的核心问题：每次挖掘「挖了什么」必须活过进程重启。此前任务状态只存在于
进程内存 + `/tmp/alpha_agent_logs/<task_id>/task_state.json`——engine 一重启，
历史即失忆，前端「挖掘历史」无从谈起。

本文件锁四件事：

1. **行 ↔ API 字典**：时间戳一律 ISO-8601 UTC（带 `Z`），缺失就是 `None`——
   绝不回落 `now()`（那会把几小时前的僵尸任务显示成刚提交的）。
2. **列表过滤在进 SQL 前解析干净**：limit/offset 收敛、状态白名单——未知状态
   必须显式报错而非静默查空：静默查空把「挖了却没显示」变成无从定位的失忆。
   注意任务状态与**因子**状态不同名（任务有 cancelled / 没有 backtesting），
   白名单必须拦下这种跨域混用。
3. **真库往返**：create → progress → terminal 全字段落位、按 user 收口。
4. **重启对账**：pending/running 的孤儿行翻 failed（completed 行绝不碰）。

真库用例租户前缀 `t-`（与真账隔离），用完必删、按用例 `close_database()`
（asyncpg 池绑定创建它的 event loop，pytest 每用例新 loop——不收池会把下一个
用例的探活绑定错误变成静默 skip），DB 不可用整体 skip。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.task_store import (  # noqa: E402
    MAX_DIRECTION_CHARS,
    MAX_HISTORY_LIMIT,
    clamp_direction,
    get_mining_task_store,
    resolve_history_filters,
    row_to_dict,
)


# ── 纯函数 ───────────────────────────────────────────────────────────


def test_row_to_dict_serializes_timestamps_as_utc_z() -> None:
    """TIMESTAMPTZ 出来是 aware datetime → 必须带 Z 的 ISO；前端 `new Date(s)` 直接可用。"""
    row = {
        "task_id": "t-1",
        "user_id": "10000001",
        "direction": "动量 × 波动率",
        "created_at": datetime.fromisoformat("2026-10-09T00:00:00+00:00"),
        "updated_at": datetime.fromisoformat("2026-10-09T01:02:03.500000+00:00"),
        "completed_at": None,
    }

    out = row_to_dict(row)

    assert out["created_at"] == "2026-10-09T00:00:00Z"
    assert out["updated_at"] == "2026-10-09T01:02:03.500000Z"
    assert out["completed_at"] is None, "未完成就是 None，不许伪造时间"
    assert out["direction"] == "动量 × 波动率"
    assert out["task_id"] == "t-1"


def test_row_to_dict_passes_through_additive_columns() -> None:
    """未来加列不该让序列化层静默吃掉字段（前向兼容）。"""
    out = row_to_dict({"task_id": "t-1", "future_column": "v"})
    assert out["future_column"] == "v"


def test_resolve_history_filters_defaults_and_clamps() -> None:
    out = resolve_history_filters(market=None, status=None, limit=50, offset=0)
    assert out == {"market": None, "status": None, "limit": 50, "offset": 0}

    out = resolve_history_filters(
        market="  a_share ", status=" running ", limit=9999, offset=-5
    )
    assert out["market"] == "a_share", "空白串等于没过滤"
    assert out["status"] == "running"
    assert out["limit"] == MAX_HISTORY_LIMIT, "超大 limit 收敛到上限而不是拖垮查询"
    assert out["offset"] == 0, "负 offset 收敛到 0"

    assert resolve_history_filters(market=" ", status="", limit=0, offset=0) == {
        "market": None,
        "status": None,
        "limit": 1,
        "offset": 0,
    }


def test_resolve_history_filters_rejects_unknown_status() -> None:
    """未知状态必须显式报错：静默查空会让「挖了但没显示」永远定不了位。

    `backtesting` 是**因子**状态不是任务状态——跨域混用要在这里被拦下。
    """
    with pytest.raises(ValueError):
        resolve_history_filters(market=None, status="backtesting", limit=50, offset=0)


def test_clamp_direction_caps_length_and_normalizes_none() -> None:
    assert clamp_direction(None) == ""
    assert clamp_direction("   ") == ""
    assert clamp_direction(" 动量 ") == "动量"

    long_text = "挖" * (MAX_DIRECTION_CHARS + 500)
    out = clamp_direction(long_text)
    assert len(out) == MAX_DIRECTION_CHARS
    assert out.startswith("挖")


# ── 真库 ─────────────────────────────────────────────────────────────


def _scope() -> str:
    """测试用户（前缀 `t-`，与真账隔离；用完必删）。"""
    return f"t-mining-{uuid.uuid4().hex[:10]}"


async def _ready() -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")
    await get_mining_task_store().ensure_tables()


async def _cleanup(user: str) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text("DELETE FROM rd_agent_mining_tasks WHERE user_id = :u"), {"u": user}
        )


async def _close() -> None:
    """asyncpg 池绑定创建它的 event loop；pytest 每用例新 loop，必须按用例收池。"""
    from backend.shared.database_manager_v2 import close_database

    await close_database()


async def _create(store, task_id: str, user: str, **overrides) -> None:
    kwargs = {
        "market": "a_share",
        "universe": "csi300",
        "data_source": "parquet",
        "direction": "测试方向",
        "loop_n": 3,
    }
    kwargs.update(overrides)
    await store.create_task(task_id=task_id, user_id=user, **kwargs)


@pytest.mark.asyncio
async def test_real_db_create_get_roundtrip_is_user_scoped() -> None:
    await _ready()
    user = _scope()
    tid = uuid.uuid4().hex[:16]
    try:
        store = get_mining_task_store()
        await _create(
            store,
            tid,
            user,
            direction="动量反转 × 波动率过滤",
            source="doc",
            doc_id="doc-abc",
        )

        row = await store.get_task(tid)
        assert row is not None
        assert row["task_id"] == tid and row["user_id"] == user
        assert row["direction"] == "动量反转 × 波动率过滤"
        assert row["source"] == "doc" and row["doc_id"] == "doc-abc"
        assert row["status"] == "pending" and row["progress_pct"] == 0
        assert row["created_at"].endswith("Z") and row["updated_at"].endswith("Z")
        assert row["completed_at"] is None

        assert await store.get_task(tid, user_id="someone-else") is None, (
            "跨用户读必须查空——不是 404 文案问题，是数据面隔离"
        )
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_create_is_idempotent_on_task_id() -> None:
    """task_id 撞键（重放/重试）不该炸——重复创建是无操作而非 500。"""
    await _ready()
    user = _scope()
    tid = uuid.uuid4().hex[:16]
    try:
        store = get_mining_task_store()
        await _create(store, tid, user)
        await _create(store, tid, user, direction="迟到的一次重放")

        row = await store.get_task(tid)
        assert row is not None
        assert row["direction"] == "测试方向", "首次写入赢，重放不覆盖已有记录"
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_update_progress_moves_row_forward() -> None:
    await _ready()
    user = _scope()
    tid = uuid.uuid4().hex[:16]
    try:
        store = get_mining_task_store()
        await _create(store, tid, user)
        before = await store.get_task(tid)

        await store.update_progress(
            tid, status="running", progress_pct=42, current_loop=2
        )

        after = await store.get_task(tid)
        assert after["status"] == "running"
        assert after["progress_pct"] == 42 and after["current_loop"] == 2
        assert datetime.fromisoformat(after["updated_at"].replace("Z", "+00:00")) >= (
            datetime.fromisoformat(before["updated_at"].replace("Z", "+00:00"))
        )
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_mark_terminal_sets_completed_at_and_error() -> None:
    await _ready()
    user = _scope()
    tid_ok = uuid.uuid4().hex[:16]
    tid_bad = uuid.uuid4().hex[:16]
    try:
        store = get_mining_task_store()
        await _create(store, tid_ok, user)
        await _create(store, tid_bad, user)

        await store.mark_terminal(tid_ok, status="completed", factor_count=7)
        await store.mark_terminal(
            tid_bad, status="failed", error="Process exited with code 1"
        )

        ok = await store.get_task(tid_ok)
        assert ok["status"] == "completed" and ok["factor_count"] == 7
        assert ok["completed_at"] is not None and ok["error"] is None

        bad = await store.get_task(tid_bad)
        assert bad["status"] == "failed"
        assert bad["error"] == "Process exited with code 1"
        assert bad["completed_at"] is not None
    finally:
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_mark_terminal_rejects_non_terminal_status() -> None:
    """终态入口只收终态：把 running 从这条写进去会绕过 completed_at 的维护。

    校验必须在开 session 之前发生（本用例在没有 DB 的环境里也要红/绿分明）。
    """
    store = get_mining_task_store()
    with pytest.raises(ValueError):
        await store.mark_terminal("t-x", status="running")


@pytest.mark.asyncio
async def test_real_db_count_factors_by_task_id() -> None:
    await _ready()
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    user = _scope()
    tid = uuid.uuid4().hex[:16]
    fid = f"t-msf-{uuid.uuid4().hex[:12]}"
    try:
        store = get_mining_task_store()
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO rd_agent_factors
                      (factor_id, factor_name, status, user_id, metadata_json, created_at, updated_at)
                    VALUES (:fid, '测试因子', 'completed', :u,
                            jsonb_build_object('task_id', CAST(:tid AS text)), now(), now())
                    """),
                {"fid": fid, "u": user, "tid": tid},
            )

        assert await store.count_factors(tid) == 1
        assert await store.count_factors("no-such-task") == 0
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM rd_agent_factors WHERE factor_id = :fid"),
                {"fid": fid},
            )
        await _cleanup(user)
        await _close()


@pytest.mark.asyncio
async def test_real_db_count_history_matches_filters_and_scope() -> None:
    """总数必须是真的 COUNT：分页「共 N 条」若用本页行数会永远显示 ≤ 一页。"""
    await _ready()
    user = _scope()
    other = _scope()
    tid_a = f"t-ms-{uuid.uuid4().hex[:12]}"
    tid_b = f"t-ms-{uuid.uuid4().hex[:12]}"
    tid_other = f"t-ms-{uuid.uuid4().hex[:12]}"
    try:
        store = get_mining_task_store()
        await _create(store, tid_a, user, market="a_share")
        await _create(store, tid_b, user, market="crypto")
        await _create(store, tid_other, other, market="a_share")
        await store.mark_terminal(tid_a, status="completed", factor_count=2)

        assert await store.count_history(user_id=user) == 2
        assert await store.count_history(user_id=user, market="crypto") == 1
        assert await store.count_history(user_id=user, status="completed") == 1
        assert await store.count_history(user_id=other) == 1, "别人的行不计入"

        with pytest.raises(ValueError):
            await store.count_history(user_id=user, status="backtesting")
    finally:
        await _cleanup(user)
        await _cleanup(other)
        await _close()


@pytest.mark.asyncio
async def test_real_db_list_history_filters_scope_market_status_order_paging() -> None:
    await _ready()
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    user = _scope()
    other = _scope()
    try:
        store = get_mining_task_store()
        await _create(store, "t-ms-h1", user, market="a_share")
        await _create(store, "t-ms-h2", user, market="a_share")
        await _create(store, "t-ms-h3", user, market="crypto")
        await _create(store, "t-ms-h4", other, market="a_share")

        # 固定 created_at 顺序：h1 最早 → h3 最新（避免同 ms 抖动）
        async with get_session() as session:
            for tid, expr in (
                ("t-ms-h1", "now() - interval '3 minutes'"),
                ("t-ms-h2", "now() - interval '2 minutes'"),
                ("t-ms-h3", "now() - interval '1 minute'"),
            ):
                await session.execute(
                    text(
                        f"UPDATE rd_agent_mining_tasks SET created_at = {expr} WHERE task_id = :t"
                    ),
                    {"t": tid},
                )
        await store.mark_terminal("t-ms-h1", status="completed", factor_count=3)

        rows = await store.list_history(user_id=user)
        assert [r["task_id"] for r in rows] == ["t-ms-h3", "t-ms-h2", "t-ms-h1"], (
            "按创建时间倒序；别人的任务（t-ms-h4）不得出现"
        )

        assert [
            r["task_id"]
            for r in await store.list_history(user_id=user, market="a_share")
        ] == [
            "t-ms-h2",
            "t-ms-h1",
        ]
        assert [
            r["task_id"]
            for r in await store.list_history(user_id=user, status="completed")
        ] == ["t-ms-h1"]

        page = await store.list_history(user_id=user, limit=1, offset=1)
        assert [r["task_id"] for r in page] == ["t-ms-h2"]

        # 列表行同样带 ISO Z 时间与方向——历史页「挖了什么」直接从这份数据渲染
        assert rows[0]["created_at"].endswith("Z")
        assert rows[0]["direction"] == "测试方向"

        with pytest.raises(ValueError):
            await store.list_history(user_id=user, status="backtesting")
    finally:
        await _cleanup(user)
        await _cleanup(other)
        await _close()


@pytest.mark.asyncio
async def test_real_db_reconcile_orphans_flips_only_pending_running() -> None:
    """重启对账：孤儿行（pending/running）翻 failed 并补 completed_at；completed 行原样。"""
    await _ready()
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    user = _scope()
    try:
        store = get_mining_task_store()
        await _create(store, "t-ms-r1", user)  # → 下面置 running
        await _create(store, "t-ms-r2", user)  # → pending（不动）
        await _create(store, "t-ms-r3", user)  # → completed（不许碰）
        await store.mark_terminal("t-ms-r3", status="completed", factor_count=1)
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE rd_agent_mining_tasks SET status = 'running' WHERE task_id = 't-ms-r1'"
                )
            )

        flipped = await store.reconcile_orphans(user_id=user)
        assert flipped == 2

        r1 = await store.get_task("t-ms-r1")
        r2 = await store.get_task("t-ms-r2")
        r3 = await store.get_task("t-ms-r3")
        assert r1["status"] == "failed" and r1["completed_at"] is not None
        assert r1["error"], "对账翻掉的行必须留下原因，面板才知道为什么停了"
        assert r2["status"] == "failed" and r2["completed_at"] is not None
        assert r3["status"] == "completed" and r3["error"] is None

        assert await store.reconcile_orphans(user_id=user) == 0, "幂等：没有孤儿时是 0"
    finally:
        await _cleanup(user)
        await _close()
