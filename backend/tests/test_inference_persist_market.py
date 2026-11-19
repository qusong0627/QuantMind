"""推理落库 market 口径：写**交易市场**，不写数据集选择器（CUSTOM）。

2026-11-19 事故：模型磁盘 metadata 为 ``market=CUSTOM``（DB 注册表已是 CN），
推理把 3277 条批量信号写成 ``engine_signal_scores.market='CUSTOM'``；健康 C01/C02
与今日交易台一律按 ``COALESCE(market,'CN')='CN'`` 取数 → 信号「写了但全链看不见」，
证据矩阵信号环判 fail。回归要求：CUSTOM 必须解析成真实交易市场后再落库。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("model_market", "meta", "expected"),
    [
        # 常规市场原样透传
        ("CN", {}, "CN"),
        ("HK", {}, "HK"),
        ("US", {}, "US"),
        # 训练侧习惯用 A 表示 A 股，落库统一成 CN
        ("A", {}, "CN"),
        # 空值按基准指数推断
        ("", {"context": {"benchmark": "SH000300"}}, "CN"),
        # CUSTOM（自定义合并因子库）不是交易市场：CUSTOM 模型落 CN
        ("CUSTOM", {"context": {"benchmark": "SH000300"}}, "CN"),
        # 显式声明优先于推断
        ("CUSTOM", {"context": {"universe_market": "HK"}}, "HK"),
        ("CUSTOM", {"trading_market": "US"}, "US"),
    ],
)
def test_resolve_persist_market(model_market, meta, expected):
    # Arrange
    from backend.services.engine.inference.script_runner import _resolve_persist_market

    # Act
    got = _resolve_persist_market(model_market, meta)

    # Assert
    assert got == expected
