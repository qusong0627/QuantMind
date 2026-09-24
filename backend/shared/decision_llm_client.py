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

**名册（P2.9，一个账户几家模型）**：``QM_DECISION_LLM_ROSTER`` 是一段 JSON 数组，
一家一项；不配 = 上面那套单家配置（逐字同旧行为）。解析与校验见
:func:`resolve_roster`。**agent 身份 = 归一后的模型名**（:func:`~backend.shared.
order_contract.normalize_agent`）：它进槽位键、分账账本段、订单幂等键与审计行，
所以名册按归一后的身份查重——同一家写两遍不是「跑两遍」，是把两家并成一本账。

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

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.shared.decision.llm_call import LLMNotConfigured, normalize_usage
from backend.shared.order_contract import normalize_agent

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
_ENV_TIMEOUT = "QM_DECISION_LLM_TIMEOUT"
_ENV_MAX_TOKENS = "QM_DECISION_LLM_MAX_TOKENS"
_ENV_TEMPERATURE = "QM_DECISION_LLM_TEMPERATURE"
#: 名册（P2.9）：一个账户几家模型。不配 = 单家三件套。
ENV_ROSTER = "QM_DECISION_LLM_ROSTER"

#: 名册上限（家）。一轮里**逐家串行**，每家最长 :data:`DEFAULT_TIMEOUT_S` 秒、
#: 最坏两次调用：8 家 × 2 × 120s = 32min，还在 45min 宽限窗以内。再多家就会挤掉
#: 排在后面的家，而「跑不完」的表现是**那家当天不决策**（账本上看起来像它没意见）。
AGENT_LIMIT = 8

#: 错误文案里响应体的截断上限（网关 200 + HTML 错误页是常态，别把整页塞进日志）。
_BODY_LIMIT = 400


def looks_like_placeholder(value: str) -> bool:
    """占位符判据（**公开**：trade 的名册配置面用同一套，不另写一份）。"""
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


def _tuning(src: Mapping[str, str]) -> tuple[float, int, float]:
    """全局调参三件（``timeout`` / ``max_tokens`` / ``temperature``）。

    单家与名册**共用这一处**：两家各写一份解析，迟早会出现「单家改了、名册没改」
    这类只在一半路径上生效的差异。值不合法照旧抛 ``ValueError``（配置错，不是运行态）。
    """
    return (
        float(src.get(_ENV_TIMEOUT, "") or DEFAULT_TIMEOUT_S),
        int(src.get(_ENV_MAX_TOKENS, "") or DEFAULT_MAX_TOKENS),
        float(src.get(_ENV_TEMPERATURE, "") or DEFAULT_TEMPERATURE),
    )


def resolve_config(env: Mapping[str, str] | None = None) -> DecisionLLMConfig:
    """读三个环境变量 → :class:`DecisionLLMConfig`；缺/占位 → :class:`LLMNotConfigured`。

    ``env`` 可注入（测试用），默认 ``os.environ``。**错误文案只点名变量名，不带值**——
    异常会被写进日志与审计表，key 不能跟着进去。
    """
    src = os.environ if env is None else env
    missing = [
        name
        for name in (_ENV_BASE, _ENV_KEY, _ENV_MODEL)
        if looks_like_placeholder(src.get(name, ""))
    ]
    if missing:
        raise LLMNotConfigured(
            f"决策 LLM 未配置：{', '.join(missing)} 为空或仍是占位符"
            f"（需要 {_ENV_BASE} / {_ENV_KEY} / {_ENV_MODEL} 三件套，见模块 docstring）"
        )
    timeout, max_tokens, temperature = _tuning(src)
    return DecisionLLMConfig(
        base_url=src[_ENV_BASE].strip(),
        api_key=src[_ENV_KEY].strip(),
        model=src[_ENV_MODEL].strip(),
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _entry_text(entry: Mapping[str, Any], key: str) -> str:
    """项里的字符串字段（去空白）；缺席/非字符串 → ``""``（= 没给）。"""
    raw = entry.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _entry_credential(
    entry: Mapping[str, Any],
    field: str,
    *,
    index: int,
    model: str,
    src: Mapping[str, str],
    global_value: str | None,
) -> str:
    """一家模型的 ``base_url`` / ``api_key``：**变量名 > 字面值 > 全局三件套**。

    中间那层（变量名）不是洁癖：名册串会被打进日志、``docker inspect`` 与工单，
    key 只能以**变量名**的形式出现在里面（``api_key_env``）——隔壁 ``${GLM_API_KEY}``
    那套间接写法同源。字面值仍允许（本机调试），但不推荐。
    """
    name = _entry_text(entry, f"{field}_env")
    if name:
        value = str(src.get(name, "")).strip()
        if looks_like_placeholder(value):
            raise LLMNotConfigured(
                f"{ENV_ROSTER} 第 {index + 1} 项（{model}）的 {field}_env 指向 {name}，"
                f"但 {name} 没配或仍是占位符"
            )
        return value
    literal = _entry_text(entry, field)
    if literal:
        if looks_like_placeholder(literal):
            raise LLMNotConfigured(
                f"{ENV_ROSTER} 第 {index + 1} 项（{model}）的 {field} 是占位符"
            )
        return literal
    if global_value is None:
        # 回落全局但全局没配：抛**单家那条**报错（同一故障不该有两套排查话术）
        raise _global_error(src, field)
    return global_value


def _global_error(src: Mapping[str, str], field: str) -> LLMNotConfigured:
    """全局三件套缺件时的报错（逐字由 :func:`resolve_config` 造出来）。"""
    try:
        resolve_config(src)
    except LLMNotConfigured as exc:
        return exc
    # 全局是好的：能走到这里说明调用点判错了「全局缺不缺」，如实说清而不是编一句
    return LLMNotConfigured(
        f"{ENV_ROSTER} 的某一项缺 {field}，且回落全局也取不到值（见模块 docstring）"
    )


def _entry_tuning(
    entry: Mapping[str, Any],
    *,
    index: int,
    model: str,
    fallback: tuple[float, int, float],
) -> tuple[float, int, float]:
    """一家模型的调用参数：项里给了就用项里的，否则用全局那套。"""
    out: list[Any] = []
    for key, cast, base in (
        ("timeout", float, fallback[0]),
        ("max_tokens", int, fallback[1]),
        ("temperature", float, fallback[2]),
    ):
        raw = entry.get(key)
        if raw is None or raw == "":
            out.append(base)
            continue
        try:
            out.append(cast(raw))
        except (TypeError, ValueError):
            raise LLMNotConfigured(
                f"{ENV_ROSTER} 第 {index + 1} 项（{model}）的 {key}={raw!r} 不是数字"
            ) from None
    return float(out[0]), int(out[1]), float(out[2])


def resolve_roster(
    env: Mapping[str, str] | None = None,
) -> tuple[DecisionLLMConfig, ...]:
    """``QM_DECISION_LLM_ROSTER`` → 每家一份 :class:`DecisionLLMConfig`（**顺序即执行顺序**）。

    不配名册 = ``(resolve_config(),)``：单家路径逐字不变。配了就是名册路径，字段：

    ==================  ====================================================
    ``model``           **必填**。模型名，同时是这个 agent 的**身份**（见下）
    ``base_url_env``   变量名，指向这家用的端点（例 ``GLM_API_BASE``）
    ``api_key_env``    变量名，指向这家用的 key（例 ``GLM_API_KEY``）
    ``base_url``/``api_key``  字面值（本机调试用；key 会进名册串，不推荐）
    ``timeout``/``max_tokens``/``temperature``  逐家调参，缺省用全局那套
    ==================  ====================================================

    **agent 身份 = ``normalize_agent(model)``**：它进槽位键（认领/done）、分账账本段、
    订单幂等键段与审计行。本函数按**归一后**的身份查重并要求唯一——两家写同一个身份
    不是「跑两遍」，是把两家的持仓并成一本账（各自的虚拟现金与名义持仓会互相吃掉）。
    归一后仍然不同、但模型名不同的两家，是两本账，天经地义。

    **错误一律 :class:`LLMNotConfigured`**（名册是配置，坏了就是没配好）：要点名的
    东西三样——第几项、哪家模型、哪个变量名；**绝不打印变量值**（异常会进日志、
    状态键与审计表）。
    """
    src = os.environ if env is None else env
    raw = str(src.get(ENV_ROSTER, "")).strip()
    if not raw:
        return (resolve_config(src),)

    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise LLMNotConfigured(
            f"{ENV_ROSTER} 不是合法 JSON（{type(exc).__name__}）："
            "它要么不配（走单家三件套），要么是一段 JSON 数组"
        ) from None
    if not isinstance(doc, list):
        raise LLMNotConfigured(
            f"{ENV_ROSTER} 不是 JSON 数组（收到 {type(doc).__name__}）："
            '一家一项，例 [{"model": "deepseek-v4-pro"}]'
        )
    if not doc:
        raise LLMNotConfigured(
            f"{ENV_ROSTER} 是空名册：一轮都不会跑。它是一条**没有痕迹**的停机"
            "（心跳照写、状态键照写空结果）——要停决策轮请关 QM_DECISION_ROUND_ENABLED"
        )
    if len(doc) > AGENT_LIMIT:
        raise LLMNotConfigured(
            f"{ENV_ROSTER} 配了 {len(doc)} 家，超过上限 {AGENT_LIMIT} 家："
            "一轮里逐家串行跑，排在后面的家会跑不完（表现为那家当天不决策）"
        )

    # 全局三件套只在**真被回落到**时才要求配齐：整段名册自带端点与 key 时，
    # 全局那三个变量一个都不需要（否则「换供应商」还得养一套用不上的假配置）。
    try:
        global_cfg: DecisionLLMConfig | None = resolve_config(src)
    except LLMNotConfigured:
        global_cfg = None
    tuning = _tuning(src)
    fallback = (
        (global_cfg.timeout, global_cfg.max_tokens, global_cfg.temperature)
        if global_cfg is not None
        else (tuning[0], tuning[1], tuning[2])
    )

    out: list[DecisionLLMConfig] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(doc):
        if not isinstance(entry, Mapping):
            raise LLMNotConfigured(
                f"{ENV_ROSTER} 第 {index + 1} 项不是对象（收到 "
                f'{type(entry).__name__}）：一家一项，形如 {{"model": "…"}}'
            )
        model = _entry_text(entry, "model")
        if looks_like_placeholder(model):
            raise LLMNotConfigured(
                f"{ENV_ROSTER} 第 {index + 1} 项缺 model（或仍是占位符）："
                "模型名是必填的，它同时是这个 agent 的身份"
            )
        agent = normalize_agent(model)
        if agent in seen:
            raise LLMNotConfigured(
                f"{ENV_ROSTER} 第 {index + 1} 项（{model}）与第 {seen[agent]} 项归一后"
                f"重名（agent={agent}）：agent 进分账账本段/幂等键/审计行，"
                "同名会把两家并成一本账"
            )
        seen[agent] = index + 1
        timeout, max_tokens, temperature = _entry_tuning(
            entry, index=index, model=model, fallback=fallback
        )
        out.append(
            DecisionLLMConfig(
                base_url=_entry_credential(
                    entry,
                    "base_url",
                    index=index,
                    model=model,
                    src=src,
                    global_value=global_cfg.base_url if global_cfg else None,
                ),
                api_key=_entry_credential(
                    entry,
                    "api_key",
                    index=index,
                    model=model,
                    src=src,
                    global_value=global_cfg.api_key if global_cfg else None,
                ),
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
    return tuple(out)


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
