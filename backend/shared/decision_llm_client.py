"""决策 LLM 的 IO 适配：env 配置 → HTTP 调用 → ``(原文, usage)``。

对应相邻系统的 ``live_hourly_analysis.call_llm``（但它有两个口径问题，本模块刻意
**不跟随**，见「三处刻意分叉」）。纯状态机在 :mod:`backend.shared.decision.llm_call`，
本模块只负责把它要的 ``call`` 真做出来。

**⚠️ 同步阻塞**：一次调用最长 ``QM_DECISION_LLM_TIMEOUT`` 秒。在 asyncio 环境里必须
``await asyncio.to_thread(call_with_usage, prompt, ...)``——直接在事件循环里调会把
整个 trade 服务冻住这么久（相邻系统是纯 cron 脚本，没有这个问题）。

**配置（三个都必须显式给，没有兜底链）**：

======================  ==================================================
``QM_DECISION_LLM_BASE_URL``  到 ``/v1`` 为止，例 ``https://api.deepseek.com/v1``
``QM_DECISION_LLM_API_KEY``   占位符（``your-...-key`` 等）视同没配
``QM_DECISION_LLM_MODEL``     模型名；它同时进系统提示词（见下）
======================  ==================================================

不做「``QM_DECISION_LLM_*`` 缺了就用 ``OPENAI_*`` 顶」的兜底：那会演成「以为在跑 A
模型、账单上是 B 模型」。缺哪个直接 :class:`~backend.shared.decision.llm_call.LLMNotConfigured`
点名哪个（**不打印 key 本身**）。

**三处刻意分叉（相邻系统的问题，本仓不跟随）**：

1. **缺配置返回空串**（``if not base or not key: return "", None``）→ 一路滑到
   ``empty_output``，把「去配 key」报成「模型返回空响应」。本仓抛
   :class:`LLMNotConfigured`。
2. **按模型名前缀分流供应商**（``model.startswith("glm")`` → GLM_*，其余 → OPENAI_*）
   → 供应商成了模型名的一部分，换一家就得改代码。本仓由 ``BASE_URL`` 决定端点。
3. **``usage`` 缺失键记 0**（``int(x or 0)``）→ 累计用量永远偏低且看不出来。本仓缺席
   的键**不出现**在结果里（见 :func:`~backend.shared.decision.llm_call.normalize_usage`）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.shared.decision.llm_call import LLMNotConfigured, normalize_usage

logger = logging.getLogger(__name__)

#: 供应商级重试的总尝试次数（相邻系统 ``for attempt in range(2)``：初发 + 重发 1 次）。
#: 与解析级重试**不是一回事**（那一层在 ``decision.llm_call``）。
PROVIDER_RETRY_ATTEMPTS = 2

#: 决策调用的默认参数（逐字对齐相邻系统：``temperature=0.3`` / ``max_tokens=4000`` /
#: ``timeout=120``）。温度不为 0 是**有意**的：同一提示词重跑要有采样噪声基线，
#: 否则「换提示词的收益」与「这一轮运气好」分不开。
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TIMEOUT_S = 120.0

#: 系统提示词模板（**逐字**取自相邻系统 ``call_llm`` 的 ``base_prompt``）。
#: ``{model}`` 是唯一插值点——模型名进提示词不是装饰，它让模型知道自己是哪一档。
#: 与 ``{model}`` 的差异会记进金样（``fixtures/decision_prompt_golden.json``）。
SYSTEM_PROMPT_TEMPLATE = (
    "你是 {model} 模型驱动的 A股 实盘交易助手盘中持仓分析师。"
    "分析冷静客观，给可执行的操作建议，注意 A股 T+1 规则与风险。输出中文 markdown。"
)

#: 配置占位符（``.env.example`` 里那套）。落在这上面 = 没配——否则会拿着假 key
#: 打到真端点，用 401 冒充「模型坏了」。
_PLACEHOLDER_MARKERS = ("your-", "mock-", "xxx", "在此", "changeme", "<", ">")

_ENV_BASE = "QM_DECISION_LLM_BASE_URL"
_ENV_KEY = "QM_DECISION_LLM_API_KEY"
_ENV_MODEL = "QM_DECISION_LLM_MODEL"

#: 错误文案里响应体的截断上限（网关 200 + HTML 错误页是常态，别把整页塞进日志）。
_BODY_LIMIT = 400


def _looks_like_placeholder(value: str) -> bool:
    low = value.strip().lower()
    return (not low) or any(m in low for m in _PLACEHOLDER_MARKERS)


@dataclass(frozen=True)
class DecisionLLMConfig:
    """一次决策调用的供应商配置。``base_url`` **含** ``/v1``（我们只接 ``/chat/completions``）。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT_TEMPLATE.format(model=self.model)


def resolve_config(env: Mapping[str, str] | None = None) -> DecisionLLMConfig:
    """读三个环境变量 → :class:`DecisionLLMConfig`；缺/占位 → :class:`LLMNotConfigured`。

    ``env`` 可注入（测试用），默认 ``os.environ``。**错误文案只点名变量名，不带值**——
    异常会被写进日志与审计表，key 不能跟着进去。
    """
    src = os.environ if env is None else env
    missing = [
        name
        for name in (_ENV_BASE, _ENV_KEY, _ENV_MODEL)
        if _looks_like_placeholder(src.get(name, ""))
    ]
    if missing:
        raise LLMNotConfigured(
            f"决策 LLM 未配置：{', '.join(missing)} 为空或仍是占位符"
            f"（需要 {_ENV_BASE} / {_ENV_KEY} / {_ENV_MODEL} 三件套，见模块 docstring）"
        )
    return DecisionLLMConfig(
        base_url=src[_ENV_BASE].strip(),
        api_key=src[_ENV_KEY].strip(),
        model=src[_ENV_MODEL].strip(),
        timeout=float(src.get("QM_DECISION_LLM_TIMEOUT", "") or DEFAULT_TIMEOUT_S),
        max_tokens=int(src.get("QM_DECISION_LLM_MAX_TOKENS", "") or DEFAULT_MAX_TOKENS),
        temperature=float(
            src.get("QM_DECISION_LLM_TEMPERATURE", "") or DEFAULT_TEMPERATURE
        ),
    )


def _post(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict:
    """唯一的网络点（测试 monkeypatch 这里）。**头里带 key，别把它写进异常。**"""
    import httpx

    resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"LLM 响应不是 JSON 对象：{str(data)[:_BODY_LIMIT]}")
    return data


def _extract(data: dict[str, Any]) -> tuple[str, dict[str, int] | None]:
    """响应体 → ``(原文, usage)``。形状不对就抛（带截断的响应体，便于定位网关问题）。

    ``reasoning_content`` 兜底：推理模型（deepseek-r1 一类）的 ``content`` 可能是空串、
    真正的话在 ``reasoning_content`` 里——不兜底会把「模型答了」记成 ``empty_output``。
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"LLM 响应没有 choices：{str(data)[:_BODY_LIMIT]}")
    msg = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        raise RuntimeError(
            f"LLM 响应的 choices[0].message 不是对象：{str(choices[0])[:_BODY_LIMIT]}"
        )
    content = str(msg.get("content") or "").strip()
    if not content:
        content = str(msg.get("reasoning_content") or "").strip()
    return content, normalize_usage(data.get("usage"))


def call_with_usage(
    prompt: str,
    *,
    config: DecisionLLMConfig | None = None,
    model: str | None = None,
) -> tuple[str, dict[str, int] | None]:
    """真调一次 LLM（供应商级重试 ``PROVIDER_RETRY_ATTEMPTS`` 次）→ ``(原文, usage)``。

    可直接当 :data:`backend.shared.decision.llm_call.Caller` 用
    （``partial(call_with_usage, config=cfg)``）——接口刻意同形，省掉一层包装。

    ``model`` 覆盖 ``config.model``：多模型竞争（一个账户几个 agent 各跑各的模型）时
    用同一个端点、不同模型名，系统提示词跟着换。
    """
    cfg = config or resolve_config()
    if model and model != cfg.model:
        cfg = DecisionLLMConfig(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=model,
            timeout=cfg.timeout,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
        )
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": cfg.system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    last: BaseException | None = None
    for attempt in range(PROVIDER_RETRY_ATTEMPTS):
        try:
            return _extract(_post(cfg.chat_url, payload, headers, cfg.timeout))
        except Exception as exc:  # noqa: BLE001 供应商抖动（超时/5xx/网关 200 坏体）都重发
            last = exc
            logger.warning(
                "决策 LLM 调用失败（第 %d/%d 次）：%s: %s",
                attempt + 1,
                PROVIDER_RETRY_ATTEMPTS,
                type(exc).__name__,
                exc,
            )
    if last is None:  # 只有 PROVIDER_RETRY_ATTEMPTS<=0 才可能走到（常量被改坏的信号）
        raise RuntimeError("决策 LLM 未发起任何调用：PROVIDER_RETRY_ATTEMPTS 必须 ≥ 1")
    raise last  # → 由 decision.llm_call 收成 api_failed；未配置则在此之前就抛了


def make_caller(
    *,
    config: DecisionLLMConfig | None = None,
    model: str | None = None,
) -> Callable[[str], tuple[str, dict[str, int] | None]]:
    """绑定配置的 ``Caller``（可直接交给 :func:`decision.llm_call.decide_with_retry`）。

    ``config`` 不传就在**每次调用时**解析 env——env 是进程级的，一轮调仓里不会变；
    不想让它碰 env 就显式传 ``config``。多模型竞争的写法是同一个 config 配不同
    ``model``（端点相同、模型名与系统提示词不同），一轮里每个 agent 建一个 caller。
    """

    def _call(prompt: str) -> tuple[str, dict[str, int] | None]:
        return call_with_usage(prompt, config=config, model=model)

    return _call
