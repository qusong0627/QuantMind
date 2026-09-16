"""T-P4-05b 测试：评分引擎（§三）+ eval_scores 契约 + 每日选股评分卡。

覆盖：打分方法纯函数（winsorize/分位/阈值/合成/红线封顶/评级/低置信）+ 契约安全纪律 +
评分卡五维纯函数 + 真库 E2E（9/8 有完整 T+5 前向数据 → 事后验证可评；落表幂等 + 清理）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


# ── 评分引擎纯函数 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_winsorize_clamps_outliers():
    from backend.shared.eval_scoring import winsorize

    vals = [1.0] * 98 + [-1000.0, 1000.0]
    out = winsorize(vals)
    assert max(out) <= 1.0 and min(out) >= 1.0  # 极端值被压到分位边界
    assert winsorize([]) == []


@pytest.mark.unit
def test_score_from_quantile_and_thresholds():
    from backend.shared.eval_scoring import score_from_quantile, score_from_thresholds

    vals = list(range(100))
    assert score_from_quantile(vals, 99) > 95
    assert score_from_quantile(vals, 0) < 5
    # 阈值线性 + 两端截断
    t = [(0.0, 0.0), (0.5, 50.0), (1.0, 100.0)]
    assert score_from_thresholds(0.25, t) == pytest.approx(25.0)
    assert score_from_thresholds(-1, t) == 0.0
    assert score_from_thresholds(9, t) == 100.0
    assert score_from_thresholds(None, t) is None


@pytest.mark.unit
def test_combine_renormalizes_missing_dims():
    from backend.shared.eval_scoring import DimensionScore, combine_dimension_scores

    dims = [
        DimensionScore("a", "A", 50, 80.0),
        DimensionScore("b", "B", 50, None, detail={"note": "缺省"}),
    ]
    out = combine_dimension_scores(dims)
    assert out["score"] == pytest.approx(80.0)  # 仅 a 可评 → 权重重归一到 a
    assert out["missing_dims"] == ["b"]


@pytest.mark.unit
def test_combine_red_line_cap_and_grades():
    from backend.shared.eval_scoring import DimensionScore, combine_dimension_scores

    dims = [
        DimensionScore("a", "A", 50, 95.0, red_line_failed=True),
        DimensionScore("b", "B", 50, 95.0),
    ]
    out = combine_dimension_scores(dims)
    assert out["score"] == 59.0 and out["capped"] is True
    assert out["raw_score"] == 95.0

    for total, expected in ((90, "A"), (75, "B"), (65, "C"), (50, "D")):
        d = [DimensionScore("x", "X", 100, float(total))]
        assert combine_dimension_scores(d)["grade"] == expected
    # 低置信 †
    d = [DimensionScore("x", "X", 100, 90.0)]
    assert combine_dimension_scores(d, low_confidence=True)["grade"] == "A†"


# ── eval_scores 契约 ────────────────────────────────────────────────


@pytest.mark.unit
def test_eval_contract_safety_pattern():
    src = (_BACKEND / "shared/eval_contract.py").read_text(encoding="utf-8")
    assert "to_regclass" in src  # 存在性预检 → 零 DDL 快路径
    assert "lock_timeout" in src
    assert "不阻断" in src


@pytest.mark.asyncio
async def test_eval_scores_table_ensure_idempotent():
    try:
        from backend.shared.eval_contract import ensure_eval_scores_table_async
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    assert await ensure_eval_scores_table_async() is True
    assert await ensure_eval_scores_table_async() is True  # 第二次走零 DDL 快路径
    # 用后关池：asyncpg 连接绑定事件循环，避免同进程后续真库用例跨循环 skip
    from backend.shared.database_manager_v2 import close_database

    await close_database()


# ── 评分卡五维纯函数 ────────────────────────────────────────────────


@pytest.mark.unit
def test_quality_dimension_variants():
    from backend.scripts.eval.daily_selection import score_quality

    opps = [
        {"symbol": "600036.SH", "strength": 0.999, "evidence": {"industry": "银行"}},
        {"symbol": "000001.SZ", "strength": 0.998, "evidence": {"industry": "电子"}},
    ]
    good = score_quality(opps, {"entry_gate": {"ma20_ok": True, "entry_ok": True}})
    assert good.score is not None and good.score > 70
    # 空仓合规（门关）
    empty_ok = score_quality([], {"entry_gate": {"ma20_ok": False, "entry_ok": True}})
    assert empty_ok.score == 70.0
    empty_bad = score_quality([], {"entry_gate": {"ma20_ok": True, "entry_ok": True}})
    assert empty_bad.score == 30.0


@pytest.mark.unit
def test_realized_dimension_pending_and_scored():
    from backend.scripts.eval.daily_selection import score_realized

    pending = score_realized(
        [{"symbol": "x", "realized_return": None}], None, horizon=5
    )
    assert pending.score is None and pending.detail["pending"] is True

    scored = score_realized(
        [
            {"symbol": "a", "realized_return": 0.06},
            {"symbol": "b", "realized_return": 0.04},
            {"symbol": "c", "realized_return": -0.01},
        ],
        benchmark_return=0.01,
        horizon=5,
    )
    assert scored.score is not None and scored.score > 60
    assert scored.detail["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)


@pytest.mark.unit
def test_coverage_matrix():
    from backend.scripts.eval.daily_selection import score_coverage

    assert (
        score_coverage(
            [{"symbol": "a"}], {"entry_gate": {"ma20_ok": True, "entry_ok": True}}
        ).score
        == 100
    )
    assert (
        score_coverage([], {"entry_gate": {"ma20_ok": True, "entry_ok": True}}).score
        == 40
    )
    assert (
        score_coverage([], {"entry_gate": {"ma20_ok": False, "entry_ok": True}}).score
        == 100
    )
    assert (
        score_coverage(
            [{"symbol": "a"}], {"entry_gate": {"ma20_ok": False, "entry_ok": True}}
        ).score
        == 20
    )


# ── 真库 E2E ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_scorecard_real_e2e_with_save():
    """9/8（有完整 T+5 前向数据）：事后验证应可评；落表幂等；用后清理。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 连接抖动: {exc}")

    from backend.scripts.eval.daily_selection import score_daily_selection

    try:
        result = await score_daily_selection("2026-09-08", horizon=5, save=True)
    finally:
        from backend.shared.database_manager_v2 import close_database

        await close_database()

    assert result["object_type"] == "daily_selection"
    assert result["trade_date"] == "2026-09-08"
    assert result["score"] is not None and result["grade"]
    realized = result["dimensions"]["realized"]
    # 9/8 + T+5 落在 9/15 前（前向数据齐）→ 事后验证不应 pending
    assert realized["score"] is not None, realized
    assert realized["detail"].get("pending") is not True

    # 落表核对 + 幂等重写 + 清理
    try:
        from backend.shared.database_manager_v2 import get_session as _gs

        async with _gs(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT score, grade FROM eval_scores WHERE object_type='daily_selection' "
                        "AND object_id='2026-09-08'"
                    )
                )
            ).first()
        assert row is not None and row[0] == result["score"]
        await score_daily_selection("2026-09-08", horizon=5, save=True)  # 幂等重写
    finally:
        async with _gs(read_only=False) as session:
            await session.execute(
                text(
                    "DELETE FROM eval_scores WHERE object_type='daily_selection' "
                    "AND object_id='2026-09-08'"
                )
            )
            await session.commit()
