"""决策层纯核心（P2）：LLM 决策的契约、解析、闸门与守护意图映射。

分层（各层只依赖上一层，全部纯函数，无网络无 DB）：

* :mod:`~backend.shared.decision.json_extract` —— LLM 文本 → JSON 对象（唯一抽取器）；
* :mod:`~backend.shared.decision.contract` —— JSON → 类型化决策（三态、两套 schema）；
* :mod:`~backend.shared.decision.gates` —— 决策 → 放行/否决（买入侧四条缺失闸）；
* :mod:`~backend.shared.decision.watch_map` —— `watch` 决策 → 守护单规则。

记分卡侧的纯函数在 :mod:`~backend.shared.decision.tags`（入场前形态标签），
它不在决策链路上——只服务于「这批决策在什么模式下失灵」的归因。

IO 与编排在别处：审计表 ``backend/shared/decision_ledger_store.py``、
轮次调度 ``backend/services/trade/services/decision_round.py``、
运维 CLI ``backend/scripts/decision_ledger.py``。
"""

from __future__ import annotations

__all__: list[str] = []
