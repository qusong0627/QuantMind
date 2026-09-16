"""T-P3-03 测试：策略代码格式分类 + 声明式骨架 + 兜底路径下架 + 修复脚本。

覆盖：
1. classify_strategy_code 五形态（含残壳/桩边界）；
2. build_scaffold_strategy_code：合法 STRATEGY_CONFIG、可过 AST 闸门、描述注入安全；
3. 兜底生成路径源断言（两处 handle_data 伪代码已下架）；
4. 修复脚本：dry-run 默认不动数据、真库 E2E（回填空 code 行按名称匹配模板）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_classify_five_formats():
    from backend.shared.strategy_format import (
        FORMAT_EMPTY,
        FORMAT_HANDLE_DATA,
        FORMAT_MINIBT,
        FORMAT_SCRIPT,
        FORMAT_STRATEGY_CONFIG,
        classify_strategy_code,
    )

    assert (
        classify_strategy_code('STRATEGY_CONFIG = {"class": "RedisTopkStrategy"}')
        == FORMAT_STRATEGY_CONFIG
    )
    assert (
        classify_strategy_code("import minibt\nfrom minibt import Bt") == FORMAT_MINIBT
    )
    assert (
        classify_strategy_code(
            "def initialize(c):\n    pass\ndef handle_data(c, d):\n    pass"
        )
        == FORMAT_HANDLE_DATA
    )
    assert (
        classify_strategy_code("if __name__ == '__main__':\n    run()") == FORMAT_SCRIPT
    )
    assert classify_strategy_code("def main():\n    pass") == FORMAT_SCRIPT
    assert classify_strategy_code("def on_tick(ctx):\n    pass") == FORMAT_SCRIPT
    assert classify_strategy_code("") == FORMAT_EMPTY
    assert classify_strategy_code("# New Strategy\n") == FORMAT_EMPTY
    assert classify_strategy_code(None) == FORMAT_EMPTY


@pytest.mark.unit
def test_executable_formats_gate():
    from backend.shared.strategy_format import (
        FORMAT_HANDLE_DATA,
        FORMAT_MINIBT,
        FORMAT_SCRIPT,
        FORMAT_STRATEGY_CONFIG,
        is_executable_format,
    )

    assert is_executable_format(FORMAT_STRATEGY_CONFIG)
    assert is_executable_format(FORMAT_MINIBT)
    assert not is_executable_format(FORMAT_HANDLE_DATA)
    assert not is_executable_format(FORMAT_SCRIPT)


@pytest.mark.unit
def test_scaffold_is_valid_executable_and_gate_clean():
    from backend.shared.strategy_code_gate import validate_strategy_code
    from backend.shared.strategy_format import (
        FORMAT_STRATEGY_CONFIG,
        build_scaffold_strategy_code,
        classify_strategy_code,
    )

    code = build_scaffold_strategy_code('动量"策略"\n第二行')
    assert classify_strategy_code(code) == FORMAT_STRATEGY_CONFIG
    validate_strategy_code(code)  # 不抛即过 AST 闸门
    assert '"""' not in code.split('"""')[1]  # 描述注入未破坏 docstring
    ns: dict = {}
    exec(compile(code, "<scaffold>", "exec"), ns)  # noqa: S102 - 平台自产代码
    assert ns["STRATEGY_CONFIG"]["class"] == "RedisRecordingStrategy"


@pytest.mark.unit
def test_ai_fallback_paths_no_longer_emit_handle_data():
    """T-P3-03 下架：两处 AI 兜底生成不再产出 handle_data 伪代码。"""
    svc_src = (
        _BACKEND / "services/engine/ai_strategy/services/strategy_service.py"
    ).read_text(encoding="utf-8")
    utils_src = (_BACKEND / "services/engine/ai_strategy/core/json_utils.py").read_text(
        encoding="utf-8"
    )
    for src in (svc_src, utils_src):
        assert "build_scaffold_strategy_code" in src
        assert "def handle_data(context, data):" not in src, "伪代码模板残留"


@pytest.mark.unit
def test_repair_script_dry_run_default():
    """修复脚本默认 dry-run：不传 --apply 不写库（源码级断言 + 入口契约）。"""
    src = (_BACKEND / "scripts/repair_strategy_code_formats.py").read_text(
        encoding="utf-8"
    )
    assert "--apply" in src
    assert "DRY-RUN" in src or "dry_run" in src


@pytest.mark.asyncio
async def test_repair_backfills_empty_code_from_template():
    """真库 E2E：空 code 的策略行按名称匹配模板 → 回填 .py + code_hash（幂等）。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    # DB 可用性预检：全套混跑时 asyncpg 连接池/事件循环抖动（套件既有环境问题）
    # → 降级 skip 而非假红
    try:
        async with get_session(read_only=True) as _probe:
            await _probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 连接抖动: {exc}")

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "repair_strategy_code_formats",
        _BACKEND / "scripts/repair_strategy_code_formats.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import json as _json

    tmpl_dir = _BACKEND.parent / "strategy_templates"
    tmpl = _json.loads((tmpl_dir / "risk_guard_topk.json").read_text(encoding="utf-8"))

    # 造一行空 code 的克隆（测试名前缀，用后清理）
    async with get_session(read_only=False) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO strategies (user_id, name, status, code, code_hash, "
                    "strategy_type) VALUES (1, :n, 'DRAFT', '', '', 'CUSTOM') "
                    "RETURNING id"
                ),
                {"n": tmpl["name"]},
            )
        ).scalar()
        await session.commit()
    try:
        result = await mod.repair_formats(apply=True, only_ids=[int(row)])
        assert result["filled"] >= 1
        async with get_session(read_only=True) as session:
            got = (
                await session.execute(
                    text("SELECT code, code_hash FROM strategies WHERE id=:i"),
                    {"i": int(row)},
                )
            ).one()
        assert "STRATEGY_CONFIG" in got.code
        assert got.code_hash and len(got.code_hash) == 64
        # 幂等：再跑不重复回填
        result2 = await mod.repair_formats(apply=True, only_ids=[int(row)])
        async with get_session(read_only=True) as session:
            got2 = (
                await session.execute(
                    text("SELECT code FROM strategies WHERE id=:i"),
                    {"i": int(row)},
                )
            ).one()
        assert got2.code == got.code
        assert result2["filled"] == 0
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                text("DELETE FROM strategies WHERE id=:i"), {"i": int(row)}
            )
            await session.commit()
