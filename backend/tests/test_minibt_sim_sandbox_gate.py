# -*- coding: utf-8 -*-
"""沙箱拒入闸：minibt 策略不得进入 on_tick 沙箱（否则启动成功、零信号）。"""

from __future__ import annotations

import pytest

from backend.services.trade.sandbox.manager import SandboxPlatformManager

MINIBT_CODE = """
from minibt import Bt, Strategy

class DualMA(Strategy):
    params = dict(fast=10, slow=20)

    def next(self):
        pass
"""


def test_minibt策略被沙箱显式拒绝() -> None:
    manager = SandboxPlatformManager(pool_size=1)
    with pytest.raises(ValueError, match="minibt"):
        manager.submit_strategy(
            tenant_id="default",
            user_id="10000001",
            strategy_id="minibt_dual_ma",
            code_str=MINIBT_CODE,
            exec_config={},
        )
