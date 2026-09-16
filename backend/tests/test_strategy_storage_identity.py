"""strategy_storage user_id 双空间解析回归（2026-09-16 事故固化）。

背景：`strategies.user_id` 存的是 **users.id 主键空间**（admin=1），而业务 user_id 是
8 位串（'10000001'）；两空间在 admin 改名期间错位，导致「用户策略不存在」/
改名为失败/模拟回测读不到策略。本测试钉死解析链的跨形态一致性。

机构级要求：真库断言（拿真实 admin users.id 比对），不做魔法常量。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


def _admin_users_id() -> int:
    from sqlalchemy import text

    from backend.shared.strategy_storage import get_db

    with get_db() as session:
        return int(
            session.execute(
                text("SELECT id FROM users WHERE username = 'admin' LIMIT 1")
            ).scalar_one()
        )


@pytest.mark.integration
def test_resolver_admin_family_forms_unify_real_db():
    from backend.shared.strategy_storage import _ensure_int_user_id

    admin_id = _admin_users_id()
    # 四种历史形态（规范业务形/旧业务形/用户名/主键形）→ 同一个 users.id 主键
    for form in ("10000001", "00000001", "admin", "1"):
        assert _ensure_int_user_id(form) == admin_id, form


@pytest.mark.integration
def test_resolver_unknown_inputs_fail_explicitly():
    from backend.shared.strategy_storage import _ensure_int_user_id

    # 非数字且不在 users：显式报错（绝不猜测映射）
    with pytest.raises(ValueError):
        _ensure_int_user_id("definitely-not-a-user")
    # 数字但不存在：落入纯数字兜底（记录既有语义——调用方查询自然得空集）
    assert _ensure_int_user_id("98765432") == 98765432


@pytest.mark.unit
def test_backfill_script_excludes_strategy_main_pk_space():
    """守卫：整型迁移清单不得包含 strategies（users.id 主键空间，迁移即事故）。"""
    src = (
        _BACKEND / "scripts" / "fix_admin_identity_full.py"
    ).read_text(encoding="utf-8")
    assert 'INT_TABLES = ("sim_orders", "sim_trades", "replay_sessions")' in src
    assert '"strategies"' not in src.split("INT_TABLES")[1].split("\n")[0]
