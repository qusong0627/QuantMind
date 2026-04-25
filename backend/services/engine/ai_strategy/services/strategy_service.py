#!/usr/bin/env python3
"""
AI策略生成服务模块
处理与AI模型的交互和策略生成逻辑
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from ..core import (
    create_fallback_strategy_result,
    extract_json_from_content,
    extract_python_code_from_json,
)

logger = logging.getLogger(__name__)


class StrategyService:
    """AI策略生成服务"""

    def __init__(
        self,
        api_key: str,
        api_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "qwen-max",
    ):
        """
        初始化策略服务

        Args:
            api_key: API密钥
            api_url: API基础URL
            model: 模型名称
        """
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.client = httpx.AsyncClient(timeout=120.0)

    def is_available(self) -> bool:
        """检查服务是否可用"""
        return self.api_key and len(self.api_key) > 5

    async def generate_strategy(
        self,
        description: str,
        market: str = "CN",
        risk_level: str = "medium",
        style: str = "simple",
        user_id: str = "desktop-user",
    ) -> dict[str, Any]:
        """
        生成AI策略

        Args:
            description: 策略描述
            market: 目标市场
            risk_level: 风险级别
            style: 策略风格
            user_id: 用户ID

        Returns:
            生成的策略结果
        """
        if not description.strip():
            raise ValueError("描述不能为空")

        if not self.is_available():
            raise RuntimeError(f"AI Service ({self.model}) 未配置可用的 API Key，请配置真实模型服务后再试")

        # 记录策略生成请求
        logger.info(
            "Strategy generation request",
            extra={
                "user_id": user_id,
                "description": description[:100],
                "market": market,
                "risk_level": risk_level,
                "style": style,
                "model": self.model,
            },
        )

        # 构建提示词
        prompt = self._build_strategy_prompt(description, market, risk_level)

        try:
            # 调用AI API
            response = await self._call_ai_api(prompt, user_id)

            # 解析响应
            result = self._parse_strategy_response(response, description, user_id)

            return result

        except Exception as e:
            logger.error(
                "AI API call failed",
                extra={
                    "error": str(e),
                    "user_id": user_id,
                    "description": description[:100],
                    "model": self.model,
                },
            )
            raise RuntimeError(f"策略生成失败: {str(e)}")

    async def generate_strategy_direct(self, prompt: str, user_id: str = "desktop-user") -> str:
        """
        直接通过 Prompt 生成/修复代码

        Args:
            prompt: 完整 Prompt
            user_id: 用户 ID

        Returns:
            生成的代码字符串
        """
        system_prompt = "你是一个精通 Qlib 和 Python 的量化交易专家。请只返回代码，不要包含任何解释说明。"
        content = await self._call_ai_api(prompt, user_id, system_prompt=system_prompt)

        # 尝试提取 Python 代码块
        if "```python" in content:
            parts = content.split("```python")
            if len(parts) > 1:
                return parts[1].split("```")[0].strip()

        # 兜底：如果直接返回的就是代码（无 Markdown）
        return content.strip()

    async def generate_strategy_stream(self, prompt: str) -> str:
        """
        流式生成策略内容

        Args:
            prompt: 策略生成提示

        Yields:
            生成的策略内容片段
        """
        if not self.is_available():
            yield 'data: {"type": "error", "error": "AI 服务未配置可用的 API Key"}\n\n'
            yield "data: [DONE]\n\n"
            return

        try:
            async with self.client.stream(
                "POST",
                f"{self.api_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一个专业的量化交易专家，请详细生成策略代码",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "stream": True,
                    "temperature": 0.7,
                    "max_tokens": 4000,
                },
                timeout=60.0,
            ) as response:
                if response.status_code != 200:
                    raise Exception(f"AI API错误: {response.status_code}")

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            if "choices" in chunk and chunk["choices"]:
                                delta = chunk["choices"][0].get("delta", {})
                                if "content" in delta:
                                    yield f"data: {json.dumps({'type': 'progress', 'content': delta['content']})}\n\n"
                        except json.JSONDecodeError:
                            continue

                yield f"data: {json.dumps({'type': 'complete', 'message': '策略生成完成'})}\n\n"

        except Exception as e:
            logger.error(f"策略生成失败: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    async def chat_stream(self, message: str) -> str:
        """
        流式对话

        Args:
            message: 用户消息

        Yields:
            对话回复内容片段
        """
        if not message.strip():
            raise ValueError("消息不能为空")

        if not self.is_available():
            yield 'data: {"type": "error", "error": "AI 服务未配置可用的 API Key"}\n\n'
            yield "data: [DONE]\n\n"
            return

        try:
            async with self.client.stream(
                "POST",
                f"{self.api_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "你是一个专业的量化交易助手"},
                        {"role": "user", "content": message},
                    ],
                    "stream": True,
                    "temperature": 0.7,
                    "max_tokens": 1000,
                },
                timeout=60.0,
            ) as response:
                if response.status_code != 200:
                    raise Exception(f"AI API错误: {response.status_code}")

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            if "choices" in chunk and chunk["choices"]:
                                delta = chunk["choices"][0].get("delta", {})
                                if "content" in delta:
                                    yield f"data: {json.dumps({'type': 'text', 'content': delta['content']})}\n\n"
                        except json.JSONDecodeError:
                            continue

        except Exception as e:
            logger.error(f"对话生成失败: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    def _build_strategy_prompt(self, description: str, market: str, risk_level: str) -> str:
        """构建策略生成提示词"""
        return f"""请基于以下描述生成 QuantMind + Qlib 可运行的量化交易策略:

描述: {description}
市场: {market}
风险级别: {risk_level}

平台规范（V1.1，必须遵守）:
1. 代码中必须提供 get_strategy_config() 或 STRATEGY_CONFIG 入口
2. 推荐基类:
   - 选股策略: RedisRecordingStrategy
   - 权重策略: RedisWeightStrategy
   导入路径:
   from backend.services.engine.qlib_app.utils.recording_strategy import RedisRecordingStrategy, RedisWeightStrategy
3. signal 默认使用 "<PRED>"
4. 若重写 __init__，必须使用 kwargs.pop() 消费自定义参数，避免 unexpected keyword argument
5. 若重写 reset，必须兼容:
   def reset(self, *args, **kwargs)
   并对 level_infra/common_infra/trade_exchange 做参数剔除回退
6. generate_trade_decision 必须返回 TradeDecisionWO（不能返回 dict）
7. 禁止使用 os/sys/subprocess/requests/urllib/socket

请返回JSON格式的策略代码，包含以下字段:
- strategy_name: 策略名称
- rationale: 策略说明（中文）
- python_code: Python策略代码（使用简体中文注释）
- factors: 使用的技术指标
- risk_controls: 风险控制措施
- assumptions: 策略假设
- notes: 重要说明

要求:
1. 代码中使用简体中文注释，便于理解
2. 生成完整交易逻辑，包含买入和卖出信号
3. 确保代码语法正确且可运行（QuantMind 环境）
4. 必须符合上面的 V1.1 兼容约束
5. 注释清晰易懂，解释关键交易逻辑

请确保代码可直接用于 QuantMind 的 Qlib 回测链路。"""

    async def _call_ai_api(self, prompt: str, user_id: str, system_prompt: str | None = None) -> str:
        """调用AI API"""
        if system_prompt is None:
            system_prompt = "你是一个专业的量化交易专家。请返回JSON格式的策略代码，包含完整的交易逻辑。代码中使用简体中文注释，确保注释清晰易懂。"

        # 简单 token 估算：中文约 1.5 char/token，英文约 4 char/token，取 2 char/token 作为上限估算
        estimated_tokens = (len(system_prompt) + len(prompt)) // 2
        max_tokens = 8000

        # Qwen-max 上下文窗口约 32K tokens，Qwen-turbo 约 131K tokens
        # 保守按 28K 输入上限估算，预留安全余量
        input_token_limit = 25000
        if estimated_tokens > input_token_limit:
            logger.warning(
                "Prompt may exceed model context window",
                extra={
                    "estimated_tokens": estimated_tokens,
                    "input_limit": input_token_limit,
                    "prompt_length": len(prompt),
                    "system_prompt_length": len(system_prompt),
                    "user_id": user_id,
                },
            )
            # 截断用户 prompt 而非系统 prompt（规范约束在系统 prompt 中）
            max_prompt_chars = max(500, (input_token_limit * 2) - len(system_prompt))
            if len(prompt) > max_prompt_chars:
                logger.warning(
                    f"Truncating user prompt from {len(prompt)} to {max_prompt_chars} chars"
                )
                prompt = prompt[:max_prompt_chars] + "\n\n[提示内容因过长已被截断，核心约束已通过系统提示词注入]"

        response = await self.client.post(
            f"{self.api_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 8000,
            },
            timeout=120.0,
        )

        if response.status_code != 200:
            logger.error(
                "AI API error",
                extra={
                    "status_code": response.status_code,
                    "response_text": response.text[:500],
                    "user_id": user_id,
                    "model": self.model,
                },
            )
            raise Exception(f"AI API错误: {response.status_code} - {response.text[:200]}")

        data = response.json()
        content = data["choices"][0]["message"]["content"]

        logger.info(
            "AI API response received",
            extra={
                "content_length": len(content),
                "user_id": user_id,
                "model": data.get("model", "unknown"),
            },
        )

        return content

    def _parse_strategy_response(self, content: str, description: str, user_id: str) -> dict[str, Any]:
        """解析策略生成响应"""
        # 尝试解析JSON响应
        result = extract_json_from_content(content)

        if not result:
            logger.warning("未找到有效JSON，创建回退结果")
            return create_fallback_strategy_result(description, content)

        logger.info(
            "Successfully parsed AI JSON response",
            extra={
                "user_id": user_id,
                "has_strategy_name": bool(result.get("strategy_name")),
                "has_python_code": bool(result.get("python_code")),
                "factors_count": len(result.get("factors", [])),
            },
        )

        # 提取Python代码
        python_code = extract_python_code_from_json(result, content)

        # 如果仍然没有有效代码，使用默认结构
        if not python_code:
            logger.warning("未找到有效Python代码，使用默认模板")
            python_code = f"# {description}\n# 基于AI生成\n\ndef initialize(context):\n    # 初始化策略参数\n    pass\n\ndef handle_data(context, data):\n    # 处理数据并生成交易信号\n    pass"

        # 清理rationale，移除代码块
        if "rationale" in result:
            import re

            result["rationale"] = re.sub(
                r"```(?:json|python)\n.*?\n```",
                "",
                result["rationale"],
                flags=re.DOTALL,
            ).strip()

        final_result = {
            "strategy_name": result.get("strategy_name", f"{description[:10]}策略"),
            "rationale": result.get("rationale", "AI生成的策略"),
            "artifacts": [
                {
                    "filename": "strategy.py",
                    "language": "python",
                    "code": python_code,
                }
            ],
            "metadata": {
                "factors": result.get("factors", []),
                "risk_controls": result.get("risk_controls", []),
                "assumptions": result.get("assumptions", []),
                "notes": result.get("notes", ""),
            },
            "provider": getattr(self, "model", "unknown"),
            "generated_at": datetime.now().isoformat(),
        }

        logger.info(
            "Strategy generation completed successfully",
            extra={
                "user_id": user_id,
                "strategy_name": final_result["strategy_name"],
                "code_length": len(final_result["artifacts"][0]["code"]),
                "factors_count": len(final_result["metadata"]["factors"]),
                "provider": final_result["provider"],
            },
        )

        return final_result

    async def close(self):
        """关闭客户端连接"""
        await self.client.aclose()


# 全局策略服务实例
_strategy_service: StrategyService | None = None


def get_strategy_service() -> StrategyService:
    """获取全局策略服务实例"""
    global _strategy_service
    if _strategy_service is None:
        from ..ai_strategy_config import LLMProviderConfig, get_config

        config = get_config()
        # 优先使用 Qwen 配置
        qwen_config = LLMProviderConfig.get_qwen_config(config)

        _strategy_service = StrategyService(
            api_key=qwen_config["api_key"],
            api_url=qwen_config["api_url"],
            model=qwen_config["model"],
        )
    return _strategy_service


async def close_strategy_service():
    """关闭全局策略服务"""
    global _strategy_service
    if _strategy_service:
        await _strategy_service.close()
        _strategy_service = None
