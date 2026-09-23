"""决策层纯核心（P2）：LLM 决策的契约、解析、闸门、提示词渲染与守护意图映射。

分层（各层只依赖上一层，全部纯函数，无网络无 DB）：

* :mod:`~backend.shared.decision.json_extract` —— LLM 文本 → JSON 对象（唯一抽取器）；
* :mod:`~backend.shared.decision.contract` —— JSON → 类型化决策（三态、两套 schema）；
* :mod:`~backend.shared.decision.gates` —— 决策 → 放行/否决（买入侧四条缺失闸）；
* :mod:`~backend.shared.decision.context` —— 一轮上下文 → **提示词文本**（纯渲染，
  输入是已取好的快照，**不读 Redis/不读文件**）；
* :mod:`~backend.shared.decision.llm_call` —— 提示词 → 决策 + usage（解析重试与失败
  分类的**纯状态机**，网络由注入的 caller 承担）；
* :mod:`~backend.shared.decision.execution` —— 一条决策批次 → **执行计划**（能不能
  变成一张单、多少股、什么价；纯函数，不读行情快照/不碰 Redis）；
* :mod:`~backend.shared.decision.watch_map` —— `watch` 决策 → 守护单规则。

记分卡侧的纯函数在 :mod:`~backend.shared.decision.tags`（入场前形态标签），
它不在决策链路上——只服务于「这批决策在什么模式下失灵」的归因。

IO 与编排在别处：提示词取数 ``backend/shared/decision_context_source.py``、
LLM 真调用 ``backend/shared/decision_llm_client.py``、
执行段取数与提交 ``backend/services/trade/services/decision_executor.py``、
审计表 ``backend/shared/decision_ledger_store.py``、
止盈止损规则表写入 ``backend/shared/decision/watch_writer.py``（`watch` → 规则表，
整组替换 + 写后回读）、
轮次调度 ``backend/services/trade/services/decision_round.py``、
运维 CLI ``backend/scripts/decision_ledger.py``。

``context`` 刻意只做渲染、``llm_call`` 刻意只做状态机：两者因此都能拿隔壁
（quant-Trader）的独立实现做**金样**（``backend/tests/fixtures/decision_prompt_golden.json``，
真语料差分见 ``docs/local/diff_decision_prompt_vs_baymax.py``）——逻辑里一旦混进
取数或 HTTP，这套差分就退化成了自拍照。
"""

from __future__ import annotations

__all__: list[str] = []
