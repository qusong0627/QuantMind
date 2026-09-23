"""T-P1-05 测试：今日交易台聚合 API。

覆盖：
1. build_pipeline 纯函数（体检结果 → 管线步骤；缺项 unknown）；
2. 接线源断言（health 同源导入 / to_thread 不阻塞事件循环 / 路由已注册 / 下钻 source 字段）；
3. 数据采集函数对空库的容错形态（fake session 不便，改用源码断言 SQL 口径：rank_pct 排序、
   归一 uid、REAL 原始 sub 双口径）。
"""

from pathlib import Path

from backend.services.api.routers.desk import build_evidence_rings, build_pipeline

_BACKEND = Path(__file__).resolve().parents[1]


def test_build_pipeline_maps_health_levels():
    items = {
        "C08": {"level": "ok", "detail": "A 股 2026-09-15 已同步"},
        "C02": {"level": "warn", "detail": "无标记"},
        "C01": {"level": "fail", "detail": "全 HOLD"},
        "C05": {"level": "ok", "detail": "台账 ok"},
    }
    pipeline = build_pipeline(items)
    by_key = {p["key"]: p for p in pipeline}
    assert by_key["data_sync"]["status"] == "ok"
    assert by_key["inference"]["status"] == "warn"
    assert by_key["signals"]["status"] == "fail"
    assert by_key["settlement"]["status"] == "ok"
    assert all(p["source"].startswith("health:C") for p in pipeline)
    assert all(p["detail"] for p in pipeline)


def test_build_pipeline_unknown_when_health_skipped():
    pipeline = build_pipeline({})
    assert len(pipeline) == 4
    assert all(p["status"] == "unknown" for p in pipeline)


def test_system_ring_surfaces_calendar_coverage_check():
    """C13（真日历覆盖年限）必须落进「系统」环节的下钻里。

    这条钉的是**接线**，不是判定（判定单测在 ``test_health_checks.py``）：判定写了、
    体检跑了，但没人把结果送进界面，等于没有——C13 说的又是「决策轮从哪天起一轮都不出」
    这种停摆级事件，界面看不见就等于没有。
    """
    rings = build_evidence_rings(
        {
            "health_items": {
                "C13": {
                    "name": "真日历覆盖年限",
                    "level": "warn",
                    "detail": "XSHG 覆盖到 2026-12-31（剩 77 天）",
                    "suggestion": "升级 exchange_calendars 或落 DB override",
                }
            }
        }
    )
    system = next(r for r in rings if r["key"] == "system")
    item = next(i for i in system["items"] if i["id"] == "C13")
    assert item["level"] == "warn"
    assert item["source"] == "scripts/diagnose/health.py:C13"
    assert "2026-12-31" in item["detail"]


def test_desk_wiring_source_assertions():
    src = (_BACKEND / "services/api/routers/desk.py").read_text(encoding="utf-8")
    # 与体检脚本同源（唯一实现复用，不重复造判定）
    assert "from backend.scripts.diagnose import health as health_mod" in src
    assert "health_mod.CHECKS" in src
    # 同步 IO 走线程池（不阻塞事件循环——DuckDB/psycopg2 阻塞单 worker 的历史教训）
    assert "asyncio.to_thread(_sync)" in src
    # 身份双口径：快照/模拟单用归一 uid；REAL 订单用原始 sub
    assert "require_sim_user_id" in src
    # 信号分位口径
    assert "rank_pct" in src and "ORDER BY rank_pct DESC" in src


def test_desk_route_registered():
    main_src = (_BACKEND / "services/api/main.py").read_text(encoding="utf-8")
    assert "from backend.services.api.routers.desk import router as desk_router" in main_src
    assert "app.include_router(desk_router)" in main_src


def test_drill_down_source_fields_present():
    src = (_BACKEND / "services/api/routers/desk.py").read_text(encoding="utf-8")
    for needle in (
        '"source": "db:engine_signal_scores"',
        '"source": "db:sim_orders+orders"',
        '"source": "db:simulation_fund_snapshots"',
        "scripts/diagnose/health.py（与体检命令行同源）",
    ):
        assert needle in src, needle


def test_desk_shadow_block_wired():
    """T-P2-06：交易台影子对照块接线（读日报不重算；不可用态不伪造数字）。"""
    src = (_BACKEND / "services/api/routers/desk.py").read_text(encoding="utf-8")
    assert "_collect_shadow" in src
    assert "load_latest_report" in src
    assert '"shadow": shadow' in src
    assert '"available": False' in src  # 无日报时如实不可用
    assert "redis:mirror:shadow:{date}" in src


def test_desk_shadow_collect_unavailable_without_redis(monkeypatch):
    """get_redis 异常时 shadow 块降级为不可用（不炸整个交易台）。"""
    import asyncio

    from backend.services.api.routers import desk as desk_mod

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(
        "backend.services.trade_shared.deps.get_redis", _boom, raising=True
    )
    data = asyncio.run(desk_mod._collect_shadow())
    assert data["available"] is False
