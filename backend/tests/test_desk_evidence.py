"""T-FE-16 测试：全链证据矩阵（十环）——纯函数矩阵 + 真库采集 + 验收口径守卫。

验收口径（设计 §七）：每格红黄绿 + 可下钻；**任一环节"无证据"必须可见**
（无证据 ≠ 绿——空源一律 no_evidence，不粉饰）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

_DESIGN_RINGS = ["数据", "特征", "模型", "信号", "回测", "模拟", "执行", "账本", "策略", "系统"]


@pytest.mark.unit
def test_evidence_rings_match_design_ten():
    from backend.services.api.routers.desk import EVIDENCE_RINGS

    assert [r[1] for r in EVIDENCE_RINGS] == _DESIGN_RINGS
    # 证据产物与频率齐备（下钻展示用）
    for _key, _label, artifact, frequency in EVIDENCE_RINGS:
        assert artifact and frequency


@pytest.mark.unit
def test_build_rings_mapping_and_worst_rollup():
    from backend.services.api.routers.desk import build_evidence_rings

    sources = {
        "health_items": {
            "C08": {"name": "数据同步", "level": "ok", "detail": "A股 9/15 已同步"},
            "C04": {"name": "快照同源", "level": "ok", "detail": "一致"},
            "C05": {"name": "台账写入", "level": "fail", "detail": "1 笔无台账", "suggestion": "查 ledger"},
            "C01": {"name": "信号分布", "level": "ok", "detail": "BUY 1040"},
            "C03": {"name": "账户键一致性", "level": "ok", "detail": "一致"},
            "C07": {"name": "调度心跳", "level": "warn", "detail": "1 项缺失"},
        },
        "eval_summary": {
            "model": {"count": 2, "latest_date": "2026-09-16", "worst_grade": "C"},
            "strategy": {"count": 5, "latest_date": "2026-09-16"},
            "strategy_health": {"count": 1, "latest_date": "2026-09-16", "latest_grade": "A"},
        },
        "features_latest": "20260915",
        "signals": {"trade_date": "2026-09-15"},
        "execution": {"sim_count": 3, "real_count": 0, "filled": 2, "rejected": 1},
        "shadow": {"available": True, "date": "20260916", "fill": {"fill_rate": 1.0}},
    }
    rings = {r["key"]: r for r in build_evidence_rings(sources)}

    # 映射正确（体检断言落入对应环）
    assert [i["id"] for i in rings["data"]["items"]] == ["C08"]
    assert rings["ledger"]["level"] == "fail"  # C05 fail 主导（最差滚动）
    assert rings["system"]["level"] == "warn"  # C07 warn + C03 ok → warn
    assert rings["signal"]["level"] == "ok"
    # 特征同日 → ok；跨期 → warn
    assert rings["feature"]["level"] == "ok"
    lagging = build_evidence_rings({**sources, "features_latest": "20260910"})
    assert {r["key"]: r for r in lagging}["feature"]["level"] == "warn"
    # 评估留档类
    assert rings["model"]["level"] == "ok" and "最差 C" in rings["model"]["items"][0]["detail"]
    assert rings["backtest"]["level"] == "ok" and "结论 A" in rings["backtest"]["items"][0]["detail"]
    assert rings["strategy"]["level"] == "ok"
    # 执行：拒单 → warn 且给建议
    assert rings["execution"]["level"] == "warn"
    assert rings["execution"]["items"][0]["suggestion"]
    # 模拟：日报可用 → ok
    assert rings["simulation"]["level"] == "ok"


@pytest.mark.unit
def test_no_evidence_is_visible_not_green():
    """空源一律 no_evidence（验收要求：任一环节"无证据"可见）。"""
    from backend.services.api.routers.desk import build_evidence_rings

    rings = build_evidence_rings(
        {
            "health_items": {},
            "eval_summary": {},
            "features_latest": None,
            "signals": {},
            "execution": {},
            "shadow": {},
        }
    )
    by_key = {r["key"]: r for r in rings}
    # 无评估/日报源 → no_evidence（不冒充 ok）
    for key in ("model", "backtest", "strategy"):
        assert by_key[key]["level"] == "no_evidence", key
    # 健康断言类完全缺失 → no_evidence（"暂无证据源接入"）
    assert by_key["data"]["level"] == "no_evidence"
    assert "暂无证据源" in by_key["data"]["summary"]
    # 执行环在完全空数据下仍可如实给出"今日无委托"（0 委托是事实而非无证据）
    assert by_key["execution"]["items"][0]["level"] == "ok"
    # 10 环齐备
    assert len(rings) == 10


@pytest.mark.asyncio
async def test_collect_evidence_real_env():
    """真环境采集：十环齐备、等级枚举合法、留档类环在盘即有分、特征环带分区日期。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.services.api.routers.desk import _collect_evidence

    try:
        evidence = await _collect_evidence(
            health_items={},
            signals={"trade_date": "2026-09-15"},
            execution={"sim_count": 0, "real_count": 0},
            shadow={"available": False, "reason": "无日报"},
        )
    finally:
        from backend.shared.database_manager_v2 import close_database

        await close_database()

    rings = evidence["rings"]
    assert len(rings) == 10
    assert all(r["level"] in {"ok", "warn", "fail", "no_evidence"} for r in rings)
    by_key = {r["key"]: r for r in rings}
    # 本机已有模型卡/策略卡留档（EOD 已跑）→ 对应环不应 no_evidence
    assert by_key["model"]["level"] != "no_evidence"
    assert by_key["strategy"]["level"] != "no_evidence"
    # 特征环在盘（features_daily 存在）→ detail 带分区日期
    feature_items = by_key["feature"]["items"]
    if feature_items:
        assert "特征分区" in feature_items[0]["detail"]


@pytest.mark.unit
def test_evidence_wired_into_today_payload():
    src = (_BACKEND / "services/api/routers/desk.py").read_text(encoding="utf-8")
    assert '"evidence": await _collect_evidence(' in src
    assert "EVIDENCE_RINGS" in src
