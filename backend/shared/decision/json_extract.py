"""LLM 输出 → JSON 对象：**决策链唯一抽取器**（纯函数，无 I/O）。

为什么单独一层（事故依据，逐条）
-------------------------------
隔壁 BayMax 有两条决策解析链各写一份抽取器，能力还不一样——那条链只认「整段 /
```json 围栏」，这条链多一个「括号平衡块」回退。2026-09-11 09:35 `deepseek-v4-pro`
输出**散文夹 JSON**，调仓链的抽取器直接失败 → 当天该 agent **0 买 0 卖**；
同一份文本喂另一条链就能取出来。抽取能力不一致的代价是全天零交易，且当天无人察觉。

本仓还有第三个实现（`services/engine/ai_strategy/core/json_utils.py`）——它用
`re.search(r"\\{[\\s\\S]*\\}")` 贪婪匹配，在「回复里有两个 JSON 对象」或「字符串里带
花括号」时会截出畸形片段；且它直接返回 `json.loads` 的结果，非 dict 的合法 JSON
（数组/字符串）也会被当成命中。**决策链不用它**：本模块是决策链的唯一入口。

抽取顺序（与既有实现一致，多块取**首个通过校验的**）
----------------------------------------------------
1. 整段即 JSON；
2. ```json 围栏（``` 无语言标记也认）；
3. 括号平衡块——按字符串状态机扫描，**字符串内的花括号不参与计数**。朴素计数在
   ``{"reason": "涨到 17.9 } 减半"}`` 这类输出上会截断失败。

``want(obj) -> bool`` 让调用方保留自己的语义校验（如「必须至少有一条合法决策」）；
校验不过的块继续往后找，全部失败返回 ``None``——**不抛异常**，由调用方决定
重试/留痕（见 ``contract.parse_decisions`` 与 P2.3 的重试段）。
"""

from __future__ import annotations

import json
import re

__all__ = ["extract_json"]

#: 围栏正则（```json / ``` 都认；`re.S` 让 `.` 跨行）。
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str | None, want=None) -> dict | None:
    """从 LLM 输出里抽出第一个满足 ``want`` 的 JSON 对象；找不到返回 None。

    ``want=None`` 时接受任意 dict（非 dict 的合法 JSON 如数组/字符串不算命中——
    调用方要的是「一个带字段的对象」，数组当命中只会把错误推迟到取值那一刻）。
    """

    def _accept(obj: object) -> dict | None:
        if not isinstance(obj, dict):
            return None
        if want is not None and not want(obj):
            return None
        return obj

    def _try(payload: str) -> dict | None:
        try:
            return _accept(json.loads(payload))
        except (json.JSONDecodeError, ValueError):
            return None

    if not text:
        return None

    hit = _try(text.strip())
    if hit is not None:
        return hit

    match = _FENCE.search(text)
    if match:
        hit = _try(match.group(1).strip())
        if hit is not None:
            return hit

    # 括号平衡块：从每个 `{` 起按字符串状态机扫描，字符串内的花括号不计数
    # （朴素计数会在 `{"reason": "涨到 17.9 } 减半"}` 上截断）。
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth, in_str, escaped = 0, False, False
        for end in range(start, len(text)):
            c = text[end]
            if in_str:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    hit = _try(text[start : end + 1])
                    if hit is not None:
                        return hit
                    break
    return None
