"""策略版本历史契约（T-FE-10）：每次内容变更留快照，支撑版本 diff 与参数锁审计。

设计要点：
- 快照 = 变更后的**完整生效状态**（name/code/parameters/execution_config/status/version），
  UNIQUE(strategy_id, version) 幂等（同版本重放只留一条）；
- 写入与 strategies 更新**同事务**（读到的版本与内容必然一致）；
- 自愈迁移沿用安全三纪律（to_regclass 预检零 DDL 快路径 + lock_timeout + **失败不阻断**）——
  版本历史是审计增强，落库失败只告警，绝不影响策略保存本身（P1 事故铁律）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS strategy_versions (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT,
    code TEXT,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    execution_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32),
    code_hash VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, version)
)
"""


def ensure_strategy_versions_table(session: Any) -> bool:
    """自愈建表（同步 session）：存在即零 DDL 快路径；失败仅告警返回 False。"""
    try:
        from sqlalchemy import text as _text

        exists = session.execute(_text("SELECT to_regclass('public.strategy_versions')")).scalar()
        if exists is not None:
            return True
        session.execute(_text("SET LOCAL lock_timeout = '3s'"))
        session.execute(_text(_CREATE_SQL))
        logger.info("[StrategyVersions] strategy_versions 表已创建")
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断策略保存
        logger.warning("[StrategyVersions] 自愈失败（不阻断）: %s", exc)
        return False


def record_strategy_version(
    session: Any,
    *,
    strategy_id: int,
    version: int,
    user_id: int,
    name: str,
    code: str,
    parameters: dict[str, Any] | None,
    execution_config: dict[str, Any] | None,
    status: str | None,
    code_hash: str | None,
) -> bool:
    """幂等写入版本快照（与 strategies 更新同事务）；失败仅告警返回 False。"""
    if not ensure_strategy_versions_table(session):
        return False
    try:
        import json

        from sqlalchemy import text as _text

        session.execute(
            _text(
                "INSERT INTO strategy_versions (strategy_id, version, user_id, name, code, "
                "parameters, execution_config, status, code_hash) VALUES "
                "(:sid, :v, :uid, :name, :code, CAST(:params AS jsonb), CAST(:exec AS jsonb), "
                ":status, :hash) ON CONFLICT (strategy_id, version) DO NOTHING"
            ),
            {
                "sid": int(strategy_id),
                "v": int(version),
                "uid": int(user_id),
                "name": name or "",
                "code": code or "",
                "params": json.dumps(parameters or {}, ensure_ascii=False),
                "exec": json.dumps(execution_config or {}, ensure_ascii=False),
                "status": (status or "")[:32] or None,
                "hash": (code_hash or "")[:64] or None,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断策略保存
        logger.warning("[StrategyVersions] 版本快照写入失败（不阻断）: %s", exc)
        return False
