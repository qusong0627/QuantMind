"""滚动 campaign 台账测试（P1）：幂等裁决纯函数 + 真库全生命周期。

- 纯函数部分锁「同一窗口绝不盲目重训」的裁决矩阵（busy-409 可重试、
  失败已消耗计算不自动重试、registered 不可被终态污染）；
- 真库部分跑一遍 planned → dispatched → registered / failed / skipped →
  reopen 的重试闭环（测试行随用随建随删，前缀 rc_cn_unittest_recipe_）。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from backend.shared.rolling_campaigns import (
    DEFAULT_MAX_ATTEMPTS,
    build_campaign_id,
    decide_redispatch,
)

TEST_MARKET = "CN"
TEST_RECIPE = "unittest_recipe"


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_campaign_id_schedule_vs_manual():
    anchor = date(2026, 10, 9)
    scheduled = build_campaign_id("CN", "cn_nativetft_base", anchor, "schedule")
    manual = build_campaign_id("CN", "cn_nativetft_base", anchor, "manual")
    assert scheduled == "rc_cn_cn_nativetft_base_20261009"
    assert manual == "rc_cn_cn_nativetft_base_20261009_manual"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("existing", "expected_action", "expected_reason"),
    [
        (None, "create", "no_campaign"),
        ({"status": "planned", "attempts": 0}, "reuse", "planned_in_flight"),
        ({"status": "dispatched", "attempts": 1}, "reuse", "in_flight"),
        ({"status": "registered", "attempts": 1}, "reuse", "already_registered"),
        ({"status": "skipped", "attempts": 0}, "redispatch", "retry_after_skip"),
        # busy-409：进入 failed 但 attempts=0（未消耗计算）→ 允许重试
        ({"status": "failed", "attempts": 0}, "redispatch", "retry_after_failure"),
        # 真提交过且失败（attempts=1 ≥ max）→ 不自动重训，留人工
        ({"status": "failed", "attempts": 1}, "reuse", "max_attempts_reached"),
        ({"status": "weird", "attempts": 0}, "reuse", "unknown_status:weird"),
    ],
)
def test_decide_redispatch_matrix(existing, expected_action, expected_reason):
    decision = decide_redispatch(existing)
    assert decision == {"action": expected_action, "reason": expected_reason}


@pytest.mark.unit
def test_decide_redispatch_respects_max_attempts_override():
    existing = {"status": "failed", "attempts": 1}
    assert decide_redispatch(existing)["action"] == "reuse"
    assert (
        decide_redispatch(existing, max_attempts=2)["action"] == "redispatch"
    )
    assert DEFAULT_MAX_ATTEMPTS == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_outcome_rejects_unknown_status_without_db():
    from backend.shared.rolling_campaigns import mark_outcome_by_run

    with pytest.raises(ValueError):
        await mark_outcome_by_run("run_x", status="weird")


@pytest.mark.unit
def test_ddl_mirrored_in_db_init_and_non_destructive():
    """两份手抄件防漂移：ensure 语句必须原样出现在 db_init.sql；且不可含破坏性语句
    （启动期自愈会整份跳过含 DROP/DELETE/TRUNCATE 的 SQL）。"""
    import re
    from pathlib import Path

    from backend.shared import rolling_campaigns as rc

    def _norm(sql: str) -> str:
        return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()

    db_init = _norm(
        (Path(__file__).resolve().parents[1] / "shared" / "db_init.sql").read_text(
            encoding="utf-8"
        )
    )
    missing = [s for s in rc._DDL_STATEMENTS if _norm(s) not in db_init]
    assert not missing, f"db_init.sql 缺少以下语句（两份 DDL 已漂移）: {missing}"
    for statement in rc._DDL_STATEMENTS:
        lowered = statement.lower()
        for banned in ("drop ", "delete ", "truncate "):
            assert banned not in lowered


# ---------------------------------------------------------------------------
# 真库全生命周期（integration）
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_campaign_lifecycle_real_db():
    from sqlalchemy import text

    from backend.shared import rolling_campaigns as rc
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    await rc.ensure_tables()

    unique = uuid.uuid4().hex[:6]
    anchor_a = date(2099, 1, 5)
    anchor_b = date(2099, 1, 6)
    campaign_a = build_campaign_id(TEST_MARKET, TEST_RECIPE, anchor_a, "schedule")
    campaign_b = build_campaign_id(TEST_MARKET, TEST_RECIPE, anchor_b, "schedule")
    run_id = f"run_unittest_{unique}"

    async def _cleanup() -> None:
        async with get_session() as session:
            await session.execute(
                text(
                    "DELETE FROM qm_rolling_campaigns WHERE campaign_id IN (:a, :b)"
                ),
                {"a": campaign_a, "b": campaign_b},
            )

    await _cleanup()
    try:
        # 1) 落 planned；同窗口第二次插入被唯一键挡住
        inserted = await rc.insert_campaign(
            campaign_id=campaign_a,
            market=TEST_MARKET,
            recipe_id=TEST_RECIPE,
            recipe_hash="deadbeef",
            trigger="schedule",
            anchor_date=anchor_a,
            window_index=1188,
            purge_days=6,
            window_policy={"train_days": 756, "purge_days": None},
            window_plan={"anchor_date": anchor_a.isoformat()},
        )
        assert inserted is not None and inserted["status"] == "planned"
        assert inserted["window_policy"]["train_days"] == 756
        duplicate = await rc.insert_campaign(
            campaign_id=campaign_a,
            market=TEST_MARKET,
            recipe_id=TEST_RECIPE,
            recipe_hash="deadbeef",
            trigger="schedule",
            anchor_date=anchor_a,
            window_index=1188,
            purge_days=6,
            window_policy=None,
            window_plan=None,
        )
        assert duplicate is None

        existing = await rc.get_campaign_by_window(
            TEST_MARKET, TEST_RECIPE, anchor_a, "schedule"
        )
        assert existing is not None
        assert decide_redispatch(existing)["action"] == "reuse"

        # 2) 提交成功 → dispatched（attempts=1）
        dispatched = await rc.mark_dispatched(campaign_a, run_id)
        assert dispatched is not None and dispatched["status"] == "dispatched"
        assert dispatched["attempts"] == 1
        assert dispatched["run_id"] == run_id

        # 3) run 终态回流 → registered + model_id
        assert (
            await rc.mark_outcome_by_run(
                run_id, status="registered", model_id="mdl_test_x"
            )
            is True
        )
        final = await rc.get_campaign(campaign_a)
        assert final["status"] == "registered"
        assert final["model_id"] == "mdl_test_x"
        assert final["finished_at"]
        # registered 不可被后续 failed 覆盖
        assert await rc.mark_failed(campaign_a, "late_failure") is False

        # 4) busy-409 路径：failed(attempts=0) → 可重试 → reopen → skipped → 仍可重试
        await rc.insert_campaign(
            campaign_id=campaign_b,
            market=TEST_MARKET,
            recipe_id=TEST_RECIPE,
            recipe_hash="deadbeef",
            trigger="schedule",
            anchor_date=anchor_b,
            window_index=1188,
            purge_days=6,
            window_policy=None,
            window_plan=None,
        )
        assert await rc.mark_failed(campaign_b, "busy_409", {"http": 409}) is True
        busy = await rc.get_campaign(campaign_b)
        assert busy["status"] == "failed"
        assert busy["attempts"] == 0
        assert busy["detail"]["reason"] == "busy_409"
        assert decide_redispatch(busy)["action"] == "redispatch"

        reopened = await rc.reopen_campaign(campaign_b)
        assert reopened is not None and reopened["status"] == "planned"
        assert reopened["finished_at"] is None
        assert await rc.mark_skipped(campaign_b, "data_lag", {"missing": 3}) is True
        skipped = await rc.get_campaign(campaign_b)
        assert skipped["status"] == "skipped"
        assert decide_redispatch(skipped) == {
            "action": "redispatch",
            "reason": "retry_after_skip",
        }
        assert (await rc.reopen_campaign(campaign_b))["status"] == "planned"

        # 4b) 崩溃残留：planned 行同样可被 reopen 重开（旧 SQL 只认 failed/skipped，
        # 「提交前进程死亡」的窗口会永久卡死在 planned——审查 F1 的自愈闭环）
        crash_left = await rc.reopen_campaign(campaign_b)  # campaign_b 此刻即 planned
        assert crash_left is not None and crash_left["status"] == "planned"
        assert crash_left["finished_at"] is None

        # 5) 列表可见
        listed_ids = {
            item["campaign_id"]
            for item in await rc.list_campaigns(market=TEST_MARKET, limit=200)
        }
        assert {campaign_a, campaign_b} <= listed_ids

        # 6) run 终态回流对未知 run 是 no-op（不报错）
        assert (
            await rc.mark_outcome_by_run("run_not_tracked", status="failed") is False
        )
    finally:
        await _cleanup()
