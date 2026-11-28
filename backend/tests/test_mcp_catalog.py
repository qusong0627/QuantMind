"""MCP 工具目录 / 权限码 / API Key 权限校验的纯函数测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.services.api.mcp_admin_service import effective_config
from backend.services.api.user_app.schemas.api_key import ApiKeyCreate, ApiKeyUpdate
from backend.shared.mcp_tool_catalog import (
    ALL_API_PERMISSIONS,
    DEFAULT_TOOL_FLAGS,
    PERMISSION_TEMPLATES,
    TOOLS,
    TOOLS_BY_NAME,
)


def test_tool_names_unique_and_permissions_known():
    names = [tool.name for tool in TOOLS]
    assert len(names) == len(set(names))
    for tool in TOOLS:
        assert tool.permission in ALL_API_PERMISSIONS
        assert tool.name in DEFAULT_TOOL_FLAGS


def test_write_tools_default_off_except_task_submit():
    for tool in TOOLS:
        if tool.destructive:
            assert DEFAULT_TOOL_FLAGS[tool.name] is False, tool.name
    # 任务提交是研究基线，默认开；模拟下单/撤单默认关
    assert DEFAULT_TOOL_FLAGS["qm_submit_task"] is True
    assert DEFAULT_TOOL_FLAGS["qm_sim_place_order"] is False
    assert DEFAULT_TOOL_FLAGS["qm_sim_cancel_order"] is False


def test_no_real_trading_tool_exists():
    assert not any("real" in tool.name for tool in TOOLS)
    assert not any("real" in tool.category for tool in TOOLS)


def test_permission_templates_have_no_real_trading():
    for name, perms in PERMISSION_TEMPLATES.items():
        assert "trade.real.write" not in perms, name
        assert set(perms) <= ALL_API_PERMISSIONS


def test_api_key_schema_rejects_unknown_permission():
    with pytest.raises(ValidationError):
        ApiKeyCreate(name="x", permissions=["not.a.permission"])
    with pytest.raises(ValidationError):
        ApiKeyUpdate(permissions=["mcp.use", "bogus"])


def test_api_key_schema_accepts_mcp_permissions():
    created = ApiKeyCreate(
        name="x",
        permissions=["mcp.use", "mcp.data.read", "trade.read", "mcp.use"],
    )
    assert created.permissions == ["mcp.use", "mcp.data.read", "trade.read"]


def test_effective_config_merges_key_overrides():
    global_cfg = {
        "enabled": True,
        "tool_flags": {**DEFAULT_TOOL_FLAGS, "qm_sim_place_order": True},
        "limits": {"calls_per_minute": 60, "query_max_rows": 5000},
    }
    key_cfg = {
        "enabled": False,
        "tool_flags": {"qm_sim_place_order": False},
        "limits": {"calls_per_minute": 10},
    }
    result = effective_config(
        global_config=global_cfg,
        key_settings=key_cfg,
        permissions=["mcp.use"],
    )
    assert result["enabled"] is False
    assert result["tool_flags"]["qm_sim_place_order"] is False
    assert result["limits"]["calls_per_minute"] == 10
    assert result["permissions"] == ["mcp.use"]


def test_catalog_contains_v1_and_v2_read_tools():
    assert "qm_query_dataset" in TOOLS_BY_NAME
    assert "qm_sim_place_order" in TOOLS_BY_NAME
    assert "qm_get_inference_rankings" in TOOLS_BY_NAME
    assert "qm_get_stock_score" in TOOLS_BY_NAME
