"""`GET /api/v1/system/programmatic-trading-disclosure` 契约测试。

这个端点的存在意义是「用户抄信息不抄错」，所以断言重点在**内容对得上单一事实源**，
而不是状态码。阈值单位（每秒 vs 每分钟）也要在响应里都给出——只给一个数，
用户拿 300 去填「每分钟」的栏位就是 60 倍误差。
"""

from __future__ import annotations

import pytest

from backend.services.api.routers import system as system_router
from backend.shared.programmatic_trading_disclosure import (
    HFT_ORDERS_PER_DAY,
    HFT_ORDERS_PER_MINUTE,
    HFT_ORDERS_PER_SECOND,
    SOFTWARE_DEVELOPER,
    SOFTWARE_NAME,
    disclosure_text,
    software_version,
)


@pytest.mark.asyncio
async def test_payload_matches_single_source_of_truth():
    # Act
    payload = await system_router.programmatic_trading_disclosure()

    # Assert
    assert payload["software_name"] == SOFTWARE_NAME
    assert payload["version"] == software_version()
    assert payload["developer"] == SOFTWARE_DEVELOPER
    assert payload["text"] == disclosure_text()
    assert payload["lines"] == disclosure_text().split("\n")


@pytest.mark.asyncio
async def test_thresholds_carry_both_units():
    """每秒与每分钟两个口径都要给：只给一个必然被填错单位。"""
    # Act
    hf = (await system_router.programmatic_trading_disclosure())["high_frequency"]

    # Assert
    assert hf["orders_per_second"] == HFT_ORDERS_PER_SECOND
    assert hf["orders_per_minute"] == HFT_ORDERS_PER_MINUTE
    assert hf["orders_per_day"] == HFT_ORDERS_PER_DAY
    assert hf["orders_per_minute"] == hf["orders_per_second"] * 60


@pytest.mark.asyncio
async def test_note_says_tool_does_not_file_for_user():
    """说明里必须写明「不代为报告」，否则会被读成开了这个开关就履行完义务了。"""
    # Act
    hf = (await system_router.programmatic_trading_disclosure())["high_frequency"]

    # Assert
    assert "不代为报告" in hf["note"]
    assert "高频交易" in hf["note"]


@pytest.mark.asyncio
async def test_payload_is_json_serializable():
    """响应体要能过 FastAPI 的序列化（别把 Path/自定义对象塞进来）。"""
    import json

    payload = await system_router.programmatic_trading_disclosure()

    assert (
        json.loads(json.dumps(payload, ensure_ascii=False))["text"] == disclosure_text()
    )
