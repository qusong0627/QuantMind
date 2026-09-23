"""决策 LLM 调用链：解析重试 + usage 合并（**纯状态机**，网络由注入的 caller 承担）。

对应相邻系统的 ``live_llm_trade.decide_with_retry`` / ``_merge_usage``。分层与
:mod:`~backend.shared.decision.context` 同一条线：本模块不 import httpx、不读 env、
不写日志——``call`` 由调用方注入，故**全部分支都能在无网络下断言**（含「第一次炸、
第二次成」这类真线上很难等到的路径）。真调用在
``backend/shared/decision_llm_client.py``。

**两层重试不是一回事，别合并**：

* 供应商级（网络/超时/5xx）→ **同一个提示词**再发一次，在 IO 适配层（见
  ``decision_llm_client.PROVIDER_RETRY_ATTEMPTS``）；
* 解析级（有输出但取不出决策）→ 追加 :data:`~backend.shared.decision.contract.JSON_ONLY_HINT`
  再问一次，在本模块（``PARSE_RETRY_ATTEMPTS``）。

每一次尝试都是**真实开销**，故 ``DecisionAttempt.usage`` 是两次调用的合并值、
``calls`` 是实际发出次数——「这一轮花了多少 token / 打了几次」在审计表里要能对上账。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.shared.decision.contract import (
    JSON_ONLY_HINT,
    RAW_LIMIT,
    STATUS_API_FAILED,
    STATUS_EMPTY_OUTPUT,
    STATUS_NOT_CONFIGURED,
    STATUS_PARSE_FAILED,
    Decision,
    DecisionBatch,
    parse_decisions,
)

#: 调用器契约：提示词 → ``(原文, usage)``。**同步阻塞**——异步环境里由调用方
#: 用 ``asyncio.to_thread`` 包（见 ``decision_llm_client`` 的模块 docstring）。
Caller = Callable[[str], "tuple[str, Mapping[str, Any] | None]"]

#: 解析失败后追加纠正语重问的次数（相邻系统是 1，事故复盘也支持「再问一次就够」：
#: 第二次仍取不出 JSON 的，几乎都是提示词/schema 串了，重试解决不了）。
PARSE_RETRY_ATTEMPTS = 1

#: usage 的规范键（OpenAI 兼容协议）。别的键不进合并结果——键集合固定才能跨轮求和。
USAGE_KEYS: tuple[str, ...] = ("prompt_tokens", "completion_tokens", "total_tokens")

#: ``DecisionAttempt.errors`` 里单条错误的截断上限（留痕要有限度）。
ERROR_LIMIT = 200

#: 非 str 原文在错误文案里的显示上限。
_REPR_LIMIT = 80


class LLMNotConfigured(RuntimeError):
    """供应商没配（缺 key/base 或落在占位符上）。

    必须是独立异常类型而不是「返回空串 + 一个 bool」：空串会一路滑到
    ``empty_output``，把「去配 key」报成「模型抽风」——相邻系统就是这么混的。
    """


def _as_int(value: object) -> int | None:
    """provider 报的 token 数 → int；**取不到一律 ``None``**（不是 0）。

    分界线是「值是什么」而不是「类型对不对」：

    * ``"12"`` **收**（网关把 usage 字符串化是常态，``int("12")`` 就是真值 12）；
    * ``None`` / 键缺席 / 非数字串 **不收**——真值**未知**，记 0 是编数据；
    * ``bool`` 不收：``True`` 是 ``int`` 的子类，``{"prompt_tokens": True}`` 记成 1
      比记成未知更糟（假数据看起来像真数据）；
    * ``nan`` / ``inf`` 不收（``int(nan)`` 直接抛）。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        try:
            num = float(text)
        except ValueError:
            return None
        return int(num) if math.isfinite(num) else None
    return None


def normalize_usage(raw: Mapping[str, Any] | None) -> dict[str, int] | None:
    """provider 原始 usage → 规范键字典；**一个规范键都没报到就返回 ``None``**。

    ``None`` 与 ``{}`` 的区别不是洁癖：``None`` 是「这次调用没报用量」，``{}`` 会
    被下游当成「报了个空」。相邻系统在这里写 ``int(x or 0)``，把缺席的键变成了
    0——累计用量看上去精确，实际永远偏低。
    """
    if not isinstance(raw, Mapping):
        return None
    out = {k: v for k in USAGE_KEYS if (v := _as_int(raw.get(k))) is not None}
    return out or None


def merge_usage(
    a: Mapping[str, Any] | None, b: Mapping[str, Any] | None
) -> dict[str, int] | None:
    """两次调用的 usage 合并（解析重试也是真实开销，必须计入）。

    **键缺席 = 双方都没报**：只出现在一侧的键照常保留单侧值（那次调用消耗了、
    这次没有该明细），两侧都没有的键不出现在结果里——审计里「没有 total_tokens
    这一项」与「total_tokens 是 0」是两件事。两侧都没报 → ``None``。
    """
    left, right = normalize_usage(a), normalize_usage(b)
    if left is None:
        return right
    if right is None:
        return left
    out: dict[str, int] = {}
    for key in USAGE_KEYS:
        x, y = left.get(key), right.get(key)
        if x is None and y is None:
            continue
        out[key] = (x or 0) + (y or 0)
    return out or None


@dataclass(frozen=True)
class DecisionAttempt:
    """一轮「取决策」的完整结果（成功与失败同构，含重试过程）。

    ``batch`` 与 :class:`~backend.shared.decision.contract.DecisionBatch` 同形，
    ``usage``/``calls``/``errors`` 是本层补的**过程**信息——审计要回答的不只是
    「模型说了什么」，还有「问了几次、花了多少、中间错在哪」。
    """

    batch: DecisionBatch
    usage: dict[str, int] | None = None
    calls: int = 0
    errors: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return self.batch.status

    @property
    def ok(self) -> bool:
        return self.batch.ok

    @property
    def decisions(self) -> tuple[Decision, ...]:
        return self.batch.decisions

    @property
    def retried(self) -> bool:
        """是否发生过解析级重试（供应商级重试算在 ``calls`` 里，不在本属性上）。"""
        return self.calls > 1

    @property
    def raw(self) -> str:
        return self.batch.raw

    def error_text(self) -> str:
        """全部错误的单行摘要（写事件/告警用）。"""
        return "；".join(self.errors)


@dataclass(frozen=True)
class _Call:
    """单次调用的结果（成功/失败同构，避免在主流里写 try/except）。"""

    content: str = ""
    usage: dict[str, int] | None = None
    status: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.status


def _brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]


def _one_call(call: Caller, prompt: str) -> _Call:
    """发一次调用并把**任何**异常收成三态之一（本模块不抛异常）。"""
    try:
        content, usage = call(prompt)
    except LLMNotConfigured as exc:
        return _Call(status=STATUS_NOT_CONFIGURED, error=_brief(exc))
    except Exception as exc:  # noqa: BLE001 调用链任何异常都归 api_failed——排查方向一致
        return _Call(status=STATUS_API_FAILED, error=_brief(exc))
    if not isinstance(content, str):
        # caller 契约坏了：当成功继续走会一路演成「解析失败」，把契约问题报成模型问题
        described = f"{type(content).__name__}：{content!r}"[:_REPR_LIMIT]
        return _Call(
            status=STATUS_API_FAILED,
            error=f"caller 返回的原文不是 str（{described}）",
        )
    return _Call(content=content, usage=normalize_usage(usage))


def decide_with_retry(
    call: Caller,
    prompt: str,
    *,
    schema: str,
) -> DecisionAttempt:
    """调 LLM 拿决策；解析失败追加「只输出 JSON」重试（``PARSE_RETRY_ATTEMPTS`` 次）。

    分支与相邻系统逐条对齐（差异只在**分类**上，见下）：

    * 调用抛异常 → ``api_failed``，**不重试**（网络问题重发同一提示词属于供应商层的
      事；这里再发一次只会把账单翻倍，且供应商层已经发过了）；
    * 未配置 → ``not_configured``，不重试（本仓新增：相邻系统把这一态混进
      ``empty_output``）；
    * 调用成功但原文为空 → ``empty_output``，不重试（空响应多为接口异常而非格式
      问题，追加纠正语没有对象可纠正）；
    * 有原文但取不出决策 → 追加 :data:`JSON_ONLY_HINT` 重问一次；
    * 重试也取不出 → ``parse_failed``，原文取**第二次**的（更接近模型最终想说的），
      第二次为空则退回第一次。

    ``schema`` 必须显式传：它决定 action 白名单，给个默认值会让「盘中轮误用调仓
    schema」这类事故变成静默丢决策。
    """
    first = _one_call(call, prompt)
    calls = 1
    errors: tuple[str, ...] = (first.error,) if first.error else ()
    if not first.ok:
        return DecisionAttempt(
            DecisionBatch(status=first.status, schema=schema),
            first.usage,
            calls,
            errors,
        )

    batch = parse_decisions(first.content, schema=schema)
    if batch.ok or batch.status == STATUS_EMPTY_OUTPUT:
        return DecisionAttempt(batch, first.usage, calls, errors)

    second = _one_call(call, prompt + JSON_ONLY_HINT)
    calls += 1
    if second.error:
        errors += (second.error,)
    usage = merge_usage(first.usage, second.usage)
    last = batch
    if second.ok:
        last = parse_decisions(second.content, schema=schema)
        if last.ok:
            return DecisionAttempt(last, usage, calls, errors)
        # 与相邻系统同形（`content2 or content`）：第二次的原文更近，空则退回第一次
        raw = second.content or first.content
    else:
        raw = first.content
    return DecisionAttempt(
        DecisionBatch(
            status=STATUS_PARSE_FAILED,
            schema=schema,
            skipped_rows=last.skipped_rows,
            ignored_actions=last.ignored_actions,
            raw=raw[:RAW_LIMIT],
        ),
        usage,
        calls,
        errors,
    )
