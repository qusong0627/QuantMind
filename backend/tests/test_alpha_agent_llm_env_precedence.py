"""AlphaAgent 因子挖掘：子进程 LLM 环境变量优先级回归测试。

背景（线上实测故障）：用户在个人中心「AI 服务配置」填了可用的 LLM
（deepseek-flash + 有效 key），但因子挖掘子进程仍用容器 env 的占位符
（OPENAI_API_KEY=mock-api-key-not-configured / CHAT_MODEL=container-default-model）
发起调用，最终 401 Invalid API Key、30 次重试后任务失败。

根因：market_adapters 的 get_env_overrides() 只读 os.getenv，且在
launcher 中于用户配置之后 env.update(...)，把用户配置整个盖掉。

本测试锁定：
1. _build_subprocess_env 的最终结果必须让「用户配置」胜出；
2. 各市场 adapter 不得再下发 LLM 凭证/模型键；
3. 失败原因提取要在缺 common_logs.log 时回退 subprocess_stdout.log。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.engine.alpha_agent.launcher import (
    AlphaAgentLauncher,
    EvolutionTask,
)

# 容器 env 里的占位符（docker-compose 默认值）
_CONTAINER_ENV = {
    "AI_IDE_LLM_API_KEY": "mock-api-key-not-configured",
    "OPENAI_API_KEY": "mock-api-key-not-configured",
    "OPENAI_BASE_URL": "https://container.example.invalid/v1",
    "OPENAI_API_BASE": "https://container.example.invalid/v1",
    "CHAT_MODEL": "container-default-model",
}

# 用户个人中心「AI 服务配置」（LLMConfig.llm_env_overrides() 的产物）
_USER_OVERRIDES = {
    "LITELLM_OPENAI_API_KEY": "sk-user-deepseek-key",
    "LITELLM_OPENAI_API_BASE": "https://api.deepseek.com/v1",
    "OPENAI_API_KEY": "sk-user-deepseek-key",
    "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
    "CHAT_MODEL": "deepseek-flash",
    "REASONING_MODEL": "deepseek-flash",
}

_LLM_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "CHAT_MODEL",
    "REASONING_MODEL",
    "LITELLM_OPENAI_API_KEY",
    "LITELLM_OPENAI_API_BASE",
    "LITELLM_CHAT_MODEL",
)


@pytest.fixture
def container_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟容器全局 env：全是占位符，且没有 DEEPSEEK_API_KEY。"""
    for key, value in _CONTAINER_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("DEEPSEEK_API_KEY", "AI_IDE_API_KEY", "ALPHA_AGENT_SYSTEM_PROMPT"):
        monkeypatch.delenv(key, raising=False)


def test_user_profile_llm_wins_over_container_placeholder(
    container_env: None, tmp_path: Path
) -> None:
    """用户配置必须覆盖容器占位符（本用例即线上故障的回归）。"""
    task = EvolutionTask(task_id="t1", user_id="10000001", market="a_share")
    env = AlphaAgentLauncher._build_subprocess_env(
        task=task,
        task_log_dir=tmp_path,
        provider_uri="/tmp/qlib_provider",
        llm_overrides=_USER_OVERRIDES,
    )

    # 子进程真正用来发请求的三个值都必须是用户的
    assert env["OPENAI_API_KEY"] == "sk-user-deepseek-key"
    assert env["OPENAI_BASE_URL"] == "https://api.deepseek.com/v1"
    assert env["CHAT_MODEL"] == "deepseek-flash"
    # litellm / RD-Agent settings 侧同样不能被占位符链覆盖
    assert env["LITELLM_OPENAI_API_KEY"] == "sk-user-deepseek-key"
    assert env["LITELLM_OPENAI_API_BASE"] == "https://api.deepseek.com/v1"
    assert env["LITELLM_CHAT_MODEL"] == "openai/deepseek-flash"
    # 兜底：任何 LLM 相关变量都不允许残留占位符 / 容器默认模型
    for key in _LLM_KEYS:
        assert "mock-api-key" not in str(env.get(key, "")), key
        assert env.get(key) != "container-default-model", key
    # 非 LLM 的市场适配器变量仍要生效
    assert env["QLIB_FACTOR_UNIVERSE"] == "csi300"
    assert env["CHAT_STREAM"] == "false"


def test_adapter_env_applied_before_llm_overrides(
    container_env: None, tmp_path: Path
) -> None:
    """adapter 的 QLIB_PROVIDER_URI 仍生效，但不得影响 LLM 键。"""
    task = EvolutionTask(task_id="t2", user_id="10000001", market="a_share")
    env = AlphaAgentLauncher._build_subprocess_env(
        task=task,
        task_log_dir=tmp_path,
        provider_uri="/tmp/qlib_provider",
        llm_overrides=_USER_OVERRIDES,
    )
    from backend.services.engine.rd_agent.market_adapters import get_adapter

    assert env["QLIB_PROVIDER_URI"] == get_adapter("a_share").get_qlib_provider_uri()


@pytest.mark.parametrize(
    "market", ["a_share", "futures", "crypto", "hong_kong", "us_stock"]
)
def test_market_adapters_do_not_emit_llm_keys(market: str) -> None:
    """市场适配器只负责数据/市场参数，不得下发 LLM 凭证或模型。"""
    from backend.services.engine.rd_agent.market_adapters import get_adapter

    overrides = get_adapter(market).get_env_overrides()
    leaked = sorted(set(overrides) & set(_LLM_KEYS))
    assert leaked == [], f"{market} adapter 不应下发 LLM 变量: {leaked}"


def test_tail_error_log_falls_back_to_subprocess_stdout(tmp_path: Path) -> None:
    """没有 common_logs.log 时要回退 subprocess_stdout.log，否则前端只看到空错误。"""
    (tmp_path / "subprocess_stdout.log").write_text(
        "line-1\nRuntimeError: Failed to create chat completion after 30 retries.\n",
        encoding="utf-8",
    )
    tail = AlphaAgentLauncher._tail_error_log(tmp_path)
    assert "Failed to create chat completion" in tail


def test_tail_error_log_prefers_common_logs(tmp_path: Path) -> None:
    """有 common_logs.log 时仍以它为准。"""
    nested = tmp_path / "Loop_0"
    nested.mkdir()
    (nested / "common_logs.log").write_text("rdagent 自己的错误日志", encoding="utf-8")
    (tmp_path / "subprocess_stdout.log").write_text("stdout 兜底", encoding="utf-8")
    assert AlphaAgentLauncher._tail_error_log(tmp_path) == "rdagent 自己的错误日志"
