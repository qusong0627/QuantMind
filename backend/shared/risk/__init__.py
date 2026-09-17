"""风控引擎公共 API（T-RC-01）。

    from backend.shared.risk import RiskGateCore, RiskContext, next_state

接线纪律（RC-02 起）：五条下单路径（托管/手动/沙箱/镜像/实时事件）一律经
``RiskGateCore.evaluate`` 判定；配置（PG 版本 + Redis 热副本）与决策落库
（risk_events）在适配器层，本包保持纯函数。
"""

from backend.shared.risk.contracts import (
    ACTION_HALT,
    ACTION_PASS,
    ACTION_REJECT,
    ACTION_WARN,
    Decision,
    LEVELS,
    RiskContext,
    RiskVerdict,
)
from backend.shared.risk.engine import RiskGateCore
from backend.shared.risk.registry import RuleSpec, all_rules, get_rule, register, rule
from backend.shared.risk.state_machine import (
    DEFAULT_POSITION_CAP,
    STATES,
    allows_buy,
    infer_target,
    next_state,
    position_cap_pct,
)

__all__ = [
    "ACTION_HALT",
    "ACTION_PASS",
    "ACTION_REJECT",
    "ACTION_WARN",
    "DEFAULT_POSITION_CAP",
    "Decision",
    "LEVELS",
    "RiskContext",
    "RiskGateCore",
    "RiskVerdict",
    "RuleSpec",
    "STATES",
    "all_rules",
    "allows_buy",
    "get_rule",
    "infer_target",
    "next_state",
    "position_cap_pct",
    "register",
    "rule",
]
