"""FE-E 后端测试：评估读 API（`/api/v1/eval/*`）。

覆盖：
1. `_row_to_score` / `build_gate_preview` / `_validate_object_type` 纯函数；
2. **门禁预演与执行点同源**断言（promotion_gate 唯一实现，不复制判定）；
3. 路由注册与可见性口径源断言；
4. 真库 E2E：合成 eval_scores 行 → list_scores（latest_only 网格）/ score_history（升序）/
   strategy_health（最新+历史+门禁预演）→ 清理。
"""

from __future__ import annotations

import uuid
from datetime import date as _date
from pathlib import Path

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[1]


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_row_to_score_shape():
    from datetime import date, datetime

    from backend.services.api.routers.eval_scores import _row_to_score

    row = {
        "object_type": "model",
        "object_id": "model_qlib",
        "snapshot_date": date(2026, 9, 16),
        "score": 83.7,
        "grade": "B",
        "low_confidence": False,
        "red_line_failed": [],
        "dimensions": {"oos_predictive": {"score": 83.7}},
        "inputs_version": {"metrics_source": "metrics"},
        "created_at": datetime(2026, 9, 16, 13, 0, 0),
    }
    out = _row_to_score(row)
    assert out["snapshot_date"] == "2026-09-16"
    assert out["grade"] == "B" and out["score"] == 83.7
    assert out["created_at"].startswith("2026-09-16")
    assert out["red_line_failed"] == []

    # 空值容错（未评级/未落日期）
    row_min = dict(row, snapshot_date=None, created_at=None, red_line_failed=None)
    out_min = _row_to_score(row_min)
    assert out_min["snapshot_date"] is None and out_min["created_at"] is None
    assert out_min["red_line_failed"] == []


@pytest.mark.unit
def test_gate_preview_same_source_as_promotion_gate():
    from backend.services.api.routers.eval_scores import build_gate_preview

    # 无记录 → 拒绝（与执行点一致）
    preview = build_gate_preview(None)
    assert preview["allowed"] is False and "体检" in preview["note"]
    # A → 放行；L → 拒绝且带理由
    assert build_gate_preview({"verdict": "A", "confidence": 88})["allowed"] is True
    rejected = build_gate_preview(
        {"verdict": "L", "confidence": 40, "reasons": ["DSR 0.4 < 0.95"]}
    )
    assert rejected["allowed"] is False and "DSR 0.4 < 0.95" in rejected["note"]

    src = (_BACKEND / "services/api/routers/eval_scores.py").read_text(encoding="utf-8")
    assert "from backend.shared.backtest_health import promotion_gate" in src


@pytest.mark.unit
def test_object_type_validation():
    from backend.services.api.routers.eval_scores import (
        ALLOWED_OBJECT_TYPES,
        _validate_object_type,
    )

    assert _validate_object_type("model") == "model"
    assert _validate_object_type(" strategy_health ") == "strategy_health"
    with pytest.raises(HTTPException) as exc:
        _validate_object_type("nonsense")
    assert exc.value.status_code == 400
    assert "factor" in exc.value.detail  # 提示允许集合
    assert set(ALLOWED_OBJECT_TYPES) == {
        "factor", "model", "strategy", "account", "daily_selection", "strategy_health",
    }


@pytest.mark.unit
def test_eval_router_registered_and_visibility_clause():
    main_src = (_BACKEND / "services/api/main.py").read_text(encoding="utf-8")
    assert "from backend.services.api.routers.eval_scores import router as eval_router" in main_src
    assert "app.include_router(eval_router)" in main_src

    src = (_BACKEND / "services/api/routers/eval_scores.py").read_text(encoding="utf-8")
    # 可见性：租户内共享行（user_id=''）+ 本人私有行
    assert "(user_id = :u OR user_id = '')" in src
    # 只读纪律：无 INSERT/UPDATE/DELETE
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in src


# ── 真库 E2E ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eval_api_endpoints_real_db():
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        # 跨事件循环池自愈：前序用例可能留下绑定旧循环的池（asyncpg 陷阱）——
        # 关池重试一次，避免整条真库 E2E 被环境抖动跳过（用后关池纪律的受害者侧补丁）。
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.services.api.routers.eval_scores import (
        list_scores,
        score_history,
        strategy_health,
    )

    tag = uuid.uuid4().hex[:8]
    model_id = f"pytest_model_{tag}"
    sid = f"77{uuid.uuid4().int % 10**6:06d}"
    user = {"tenant_id": "default", "user_id": "00000001"}

    try:
        async with get_session() as session:
            # 模型卡两天历史（共享行 user_id=''），策略体检两期（私有行）
            for day, score in (("2026-09-15", 60.0), ("2026-09-16", 72.0)):
                await session.execute(
                    text(
                        "INSERT INTO eval_scores (object_type, object_id, snapshot_date, score, "
                        "grade, low_confidence, red_line_failed, dimensions, inputs_version) "
                        "VALUES ('model', :oid, :d, :s, 'B', FALSE, '[]'::jsonb, "
                        "CAST(:dims AS jsonb), '{}'::jsonb)"
                    ),
                    {
                        "oid": model_id,
                        "d": _date.fromisoformat(day),
                        "s": score,
                        "dims": '{"oos_predictive": {"score": %s}}' % score,
                    },
                )
            for day, verdict in (("2026-08-01", "E"), ("2026-09-16", "A")):
                await session.execute(
                    text(
                        "INSERT INTO eval_scores (object_type, object_id, snapshot_date, tenant_id, "
                        "user_id, score, grade, low_confidence, red_line_failed, dimensions, inputs_version) "
                        "VALUES ('strategy_health', :sid, :d, 'default', :u, 80, :v, FALSE, '[]'::jsonb, "
                        "CAST(:dims AS jsonb), CAST(:ver AS jsonb))"
                    ),
                    {
                        "sid": sid,
                        "d": _date.fromisoformat(day),
                        "u": user["user_id"],
                        "v": verdict,
                        "dims": '{"verdict_label": "标签%s", "reasons": ["r"], "suggestions": []}' % verdict,
                        "ver": '{"backtest_id": "bt-%s", "evidence_source": "backtest"}' % day,
                    },
                )
            await session.commit()

        # 1) 网格：latest_only → 每对象一条（取最新 9/16 的 72）
        listing = await list_scores(object_type="model", object_id=model_id, latest_only=True, limit=10, current_user=user)
        assert listing["success"] is True
        assert listing["meta"]["count"] == 1
        assert listing["data"][0]["score"] == 72.0

        # 2) 历史：升序两根
        hist = await score_history(object_type="model", object_id=model_id, limit=10, current_user=user)
        scores = [row["score"] for row in hist["data"]]
        assert scores == [60.0, 72.0]

        # 3) 体检档案：最新 A + 历史两期 + 门禁放行（同源预演）
        health = await strategy_health(strategy_id=sid, limit=10, current_user=user)
        assert health["data"]["latest"]["verdict"] == "A"
        assert [h["verdict"] for h in health["data"]["history"]] == ["A", "E"]
        assert health["data"]["gate"]["allowed"] is True
        assert health["data"]["latest"]["backtest_id"].startswith("bt-")

        # 4) 可见性：他人 user_id 读不到该策略体检（私有行）
        other = await strategy_health(
            strategy_id=sid, limit=10, current_user={"tenant_id": "default", "user_id": "99999999"}
        )
        assert other["data"]["latest"] is None
    finally:
        from backend.shared.database_manager_v2 import get_session as _gs

        async with _gs() as session:
            await session.execute(
                text("DELETE FROM eval_scores WHERE object_id IN (:m, :s)"),
                {"m": model_id, "s": sid},
            )
            await session.commit()
        from backend.shared.database_manager_v2 import close_database

        await close_database()


# ── T-FE-15 自助体检上传 ────────────────────────────────────────────


@pytest.mark.unit
def test_parse_nav_payload_formats():
    from backend.services.api.routers.eval_scores import parse_nav_payload

    # JSON 纯数组
    rows = parse_nav_payload("[" + ",".join(str(100 + i) for i in range(40)) + "]")
    assert len(rows) == 40 and rows[0]["date"] is None

    # JSON 回测结果文件形态（{"equity_curve":[{date,value}]}）
    import json as _json

    payload = {"equity_curve": [{"date": f"2026-01-{i:02d}", "value": 100 + i} for i in range(1, 32)]}
    rows = parse_nav_payload(_json.dumps(payload))
    assert len(rows) == 31 and rows[0]["date"] == "2026-01-01"

    # CSV：日期+数值（表头跳过）；YYYYMMDD 归一为 ISO
    csv = "date,close\n" + "\n".join(f"202601{i:02d},{100 + i}" for i in range(1, 32))
    rows = parse_nav_payload(csv)
    assert len(rows) == 31 and rows[0]["date"] == "2026-01-01"
    assert rows[-1]["value"] == 131

    # CSV：纯数值
    rows = parse_nav_payload("\n".join(str(100 + i) for i in range(35)))
    assert len(rows) == 35


@pytest.mark.unit
def test_parse_nav_payload_guards():
    from backend.services.api.routers.eval_scores import parse_nav_payload

    with pytest.raises(ValueError, match="内容为空"):
        parse_nav_payload("  ")
    with pytest.raises(ValueError, match="不足"):
        parse_nav_payload("1,2,3")  # <30 点
    with pytest.raises(ValueError, match="过多"):
        parse_nav_payload("\n".join(str(100 + (i % 7)) for i in range(5001)))
    # 非法值（0/负）被过滤后不足
    with pytest.raises(ValueError, match="不足"):
        parse_nav_payload("\n".join(["0"] * 40))


@pytest.mark.asyncio
async def test_upload_health_check_real_report():
    """合成强势曲线 → 真实九项体检报告（A/B/L/E 皆可，但必须结构完整）；口径不足 400。"""
    import json as _json

    import numpy as np
    from fastapi import HTTPException

    from backend.services.api.routers.eval_scores import upload_health_check

    rng = np.random.default_rng(7)
    bench = rng.normal(0.0003, 0.006, 750)
    strat = 0.3 * bench + 0.002 + rng.normal(0, 0.004, 750)
    vals = list(np.cumprod(1 + strat) * 100)
    start = __import__("datetime").date(2024, 1, 2)
    curve = [
        {"date": (start + __import__("datetime").timedelta(days=i)).isoformat(), "value": float(v)}
        for i, v in enumerate(vals[:300])
    ]
    resp = await upload_health_check(
        payload={"content": _json.dumps({"equity_curve": curve})},
        current_user={"tenant_id": "default", "user_id": "00000001"},
    )
    data = resp["data"]
    assert data["report"]["verdict"] in {"A", "B", "L", "E"}
    assert "结论标签" in data["report_text"]
    assert data["points"] == 300
    assert "不参与" in data["disclaimer"]

    with pytest.raises(HTTPException) as exc:
        await upload_health_check(payload={"content": "1,2"}, current_user={"tenant_id": "default", "user_id": "1"})
    assert exc.value.status_code == 400
