import copyreg
import re
from typing import Any, Literal, Optional, Type, TypedDict, Union, cast

import numpy as np
from litellm import (
    completion,
    completion_cost,
    embedding,
    get_model_info,
    supports_function_calling,
    supports_response_schema,
    token_counter,
)
from litellm.exceptions import BadRequestError, Timeout
from pydantic import BaseModel

from rdagent.log import LogColors
from rdagent.log import rdagent_logger as logger
from rdagent.oai.backend.base import APIBackend
from rdagent.oai.llm_conf import LLMSettings
from rdagent.oai.utils.embedding import (
    embedding_channel_is_explicit,
    resolve_embedding_channel,
)


# NOTE: Patching! Otherwise, the exception will call the constructor and with following error:
# `BadRequestError.__init__() missing 2 required positional arguments: 'model' and 'llm_provider'`
def _reduce_no_init(exc: Exception) -> tuple:
    cls = exc.__class__
    return (cls.__new__, (cls,), exc.__dict__)


# suppose you want to apply this to MyError
for cls in [BadRequestError, Timeout]:
    copyreg.pickle(cls, _reduce_no_init)


class LiteLLMSettings(LLMSettings):

    class Config:
        env_prefix = "LITELLM_"
        """Use `LITELLM_` as prefix for environment variables"""

    # Placeholder for LiteLLM specific settings, so far it's empty


LITELLM_SETTINGS = LiteLLMSettings()
ACC_COST = 0.0

_SECRET_FIELD_PATTERN = re.compile(r"(?:^|_)(?:key|token|secret|password)$", re.IGNORECASE)
"""settings 里以这些后缀结尾的字段一律脱敏后才准进日志。

上游把整个 ``LITELLM_SETTINGS`` 原样打印（repr 与 ``model_dump()`` 各一次），
``openai_api_key`` / ``chat_openai_api_key`` / ``embedding_openai_api_key``
等字段因此**明文落日志**。实测确认：讯飞 MaaS 的 key 会出现在容器 stdout，
而 ``log_object`` 还会把它写进落盘的 log artifact。
"""


def _redact_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """把 settings dump 里的密钥字段替换成 ``***``，其余原样保留。

    保留结构而非整条丢弃：这份 dump 是排查配置问题的唯一快照，
    去掉密钥后仍能看出模型、端点、重试策略是否如预期。
    """
    return {
        key: ("***" if _SECRET_FIELD_PATTERN.search(key) else value)
        for key, value in settings.items()
    }


class LiteLLMAPIBackend(APIBackend):
    """LiteLLM implementation of APIBackend interface"""

    _has_logged_settings: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not self.__class__._has_logged_settings:
            # 上游这里是 `logger.info(f"{LITELLM_SETTINGS}")` + 原样 model_dump，
            # 会把 openai_api_key 明文写进 stdout 与落盘日志。改为脱敏后打印：
            # 排查配置所需的模型/端点/重试策略全部保留，只有密钥变 `***`。
            settings_dump = _redact_settings(LITELLM_SETTINGS.model_dump())
            logger.info(f"LITELLM_SETTINGS={settings_dump}")
            logger.log_object(settings_dump, tag="LITELLM_SETTINGS")
            self.__class__._has_logged_settings = True
        super().__init__(*args, **kwargs)

    def _calculate_token_from_messages(self, messages: list[dict[str, Any]]) -> int:
        """
        Calculate the token count from messages
        """
        num_tokens = token_counter(
            model=LITELLM_SETTINGS.chat_model,
            messages=messages,
        )
        logger.info(f"{LogColors.CYAN}Token count: {LogColors.END} {num_tokens}", tag="debug_litellm_token")
        return num_tokens

    def _embedding_channel_ready(self) -> tuple[bool, str]:
        """本后端是否真的能产出 embedding。

        在 `base.py` 的重试循环**之前**检查：embedding 通道没配好是配置错误，不是瞬时
        故障，必须快速失败并给出可操作的提示，而不是被 30×5s 的退避吞掉。

        ⚠️ 不能只判 ``model_name`` 非空：``LLM_SETTINGS.embedding_model`` 的默认值
        ``text-embedding-3-small``（``oai/llm_conf.py:16``）就是非空的，只看它会让
        「完全没配」被误判为就绪——然后静默回落到 chat 供应商的凭证。
        """
        model_name, _, _ = resolve_embedding_channel()
        if model_name and embedding_channel_is_explicit():
            return True, ""
        return False, (
            "Embedding channel is not configured. Set EMBEDDING_MODEL + EMBEDDING_BASE_URL "
            "(plus EMBEDDING_API_KEY for hosted / OpenAI-compatible providers) before enabling "
            "the knowledge-base retrieval path. Without an explicit channel the request would "
            "silently fall back to the chat provider's credentials"
            + (f", using the default model {model_name!r}" if model_name else "")
            + "."
        )

    def _create_embedding_inner_function(self, input_content_list: list[str]) -> list[list[float]]:
        """
        Call the embedding function
        """
        model_name, api_base, api_key = resolve_embedding_channel()
        # 自定义端点（本地 embedding 服务 / 第三方 OpenAI 兼容网关）需要显式传 api_base + api_key；
        # 不传时 litellm 只会去读 OPENAI_* 全局变量，指不到 embedding 供应商。
        extra: dict[str, Any] = {}
        if api_base:
            extra["api_base"] = api_base
        if api_key:
            extra["api_key"] = api_key
        logger.info(
            f"{LogColors.GREEN}Using emb model{LogColors.END} {model_name} "
            f"(api_base={api_base or '<litellm default>'})",
            tag="debug_litellm_emb",
        )
        if LITELLM_SETTINGS.log_llm_chat_content:
            logger.info(
                f"{LogColors.MAGENTA}Creating embedding{LogColors.END} for: {input_content_list}",
                tag="debug_litellm_emb",
            )
        response = embedding(
            model=model_name,
            input=input_content_list,
            **extra,
        )
        response_list = [data["embedding"] for data in response.data]
        return response_list

    class CompleteKwargs(TypedDict):
        model: str
        temperature: float
        max_tokens: int | None
        reasoning_effort: Literal["low", "medium", "high"] | None

    def get_complete_kwargs(self) -> CompleteKwargs:
        """
        return several key settings for completion
        getting these values from settings makes it easier to adapt to backend calls in agent systems.
        """
        # Call LiteLLM completion
        model = LITELLM_SETTINGS.chat_model
        temperature = LITELLM_SETTINGS.chat_temperature
        max_tokens = LITELLM_SETTINGS.chat_max_tokens
        reasoning_effort = LITELLM_SETTINGS.reasoning_effort

        if LITELLM_SETTINGS.chat_model_map:
            for t, mc in LITELLM_SETTINGS.chat_model_map.items():
                if t in logger._tag:
                    model = mc["model"]
                    if "temperature" in mc:
                        temperature = float(mc["temperature"])
                    if "max_tokens" in mc:
                        max_tokens = int(mc["max_tokens"])
                    if "reasoning_effort" in mc:
                        if mc["reasoning_effort"] in ["low", "medium", "high"]:
                            reasoning_effort = cast(Literal["low", "medium", "high"], mc["reasoning_effort"])
                        else:
                            reasoning_effort = None
                    break
        return self.CompleteKwargs(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    def _create_chat_completion_inner_function(  # type: ignore[no-untyped-def] # noqa: C901, PLR0912, PLR0915
        self,
        messages: list[dict[str, Any]],
        response_format: Optional[Union[dict, Type[BaseModel]]] = None,
        *args,
        **kwargs,
    ) -> tuple[str, str | None]:
        """
        Call the chat completion function
        """

        if response_format and not supports_response_schema(model=LITELLM_SETTINGS.chat_model):
            # Deepseek will enter this branch
            logger.warning(
                f"{LogColors.YELLOW}Model {LITELLM_SETTINGS.chat_model} does not support response schema, ignoring response_format argument.{LogColors.END}",
                tag="llm_messages",
            )
            response_format = None

        if response_format:
            kwargs["response_format"] = response_format

        if LITELLM_SETTINGS.log_llm_chat_content:
            logger.info(self._build_log_messages(messages), tag="llm_messages")

        complete_kwargs = self.get_complete_kwargs()
        model = complete_kwargs["model"]

        response = completion(
            messages=messages,
            stream=LITELLM_SETTINGS.chat_stream,
            max_retries=0,
            **complete_kwargs,
            **kwargs,
        )
        if LITELLM_SETTINGS.log_llm_chat_content:
            logger.info(f"{LogColors.GREEN}Using chat model{LogColors.END} {model}", tag="llm_messages")

        if LITELLM_SETTINGS.chat_stream:
            if LITELLM_SETTINGS.log_llm_chat_content:
                logger.info(f"{LogColors.BLUE}assistant:{LogColors.END}", tag="llm_messages")
            content = ""
            finish_reason = None
            for message in response:
                if message["choices"][0]["finish_reason"]:
                    finish_reason = message["choices"][0]["finish_reason"]
                if "content" in message["choices"][0]["delta"]:
                    chunk = (
                        message["choices"][0]["delta"]["content"] or ""
                    )  # when finish_reason is "stop", content is None
                    content += chunk
                    if LITELLM_SETTINGS.log_llm_chat_content:
                        logger.info(LogColors.CYAN + chunk + LogColors.END, raw=True, tag="llm_messages")
            if LITELLM_SETTINGS.log_llm_chat_content:
                logger.info("\n", raw=True, tag="llm_messages")
        else:
            content = str(response.choices[0].message.content)
            finish_reason = response.choices[0].finish_reason
            finish_reason_str = (
                f"({LogColors.RED}Finish reason: {finish_reason}{LogColors.END})"
                if finish_reason and finish_reason != "stop"
                else ""
            )
            if LITELLM_SETTINGS.log_llm_chat_content:
                logger.info(
                    f"{LogColors.BLUE}assistant:{LogColors.END} {finish_reason_str}\n{content}", tag="llm_messages"
                )

        global ACC_COST
        try:
            cost = completion_cost(model=model, messages=messages, completion=content)
        except Exception as e:
            logger.warning(f"Cost calculation failed for model {model}: {e}. Skip cost statistics.")
            cost = np.nan
        else:
            ACC_COST += cost
            if LITELLM_SETTINGS.log_llm_chat_content:
                logger.info(
                    f"Current Cost: ${float(cost):.10f}; Accumulated Cost: ${float(ACC_COST):.10f}; {finish_reason=}",
                )
        try:
            prompt_tokens = token_counter(model=model, messages=messages)
            completion_tokens = token_counter(model=model, text=content)
        except ValueError as e:
            logger.warning(f"Token counting failed for model {model}: {e}. Skip token statistics.")
            prompt_tokens = 0
            completion_tokens = 0
        logger.log_object(
            {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost": cost,
                "accumulated_cost": ACC_COST,
            },
            tag="token_cost",
        )
        return content, finish_reason

    def supports_response_schema(self) -> bool:
        """
        Check if the backend supports function calling
        """
        return supports_response_schema(model=LITELLM_SETTINGS.chat_model) and LITELLM_SETTINGS.enable_response_schema

    @property
    def chat_token_limit(self) -> int:
        """Suggest an input token limit, ensuring enough space in the context window for the maximum output tokens."""
        try:
            model_info = get_model_info(LITELLM_SETTINGS.chat_model)
            if model_info is None:
                return super().chat_token_limit

            max_input = model_info.get("max_input_tokens")
            max_output = model_info.get("max_output_tokens")

            if max_input is None or max_output is None:
                return super().chat_token_limit

            max_input_tokens = max_input - max_output
            return max_input_tokens
        except Exception as e:
            return super().chat_token_limit
