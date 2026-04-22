"""
Qwen (千问) LLM - 同步客户端 + 异步 Provider

合并自:

- providers/qwen_provider.py (QwenLLMProvider, 异步)
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any, Dict, Tuple

import requests

if TYPE_CHECKING:
    from ..models import StrategyConversionRequest, StrategyConversionResponse

from ..models import StrategyGenerationRequest, StrategyGenerationResult
from .base import BaseLLMProvider, normalize_name

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class QwenLLM:
    """Qwen LLM - 使用 OpenAI 兼容模式"""

    def __init__(self):
        from ..ai_strategy_config import get_config as _gc

        ai_strategy_config = _gc()

        self._config = ai_strategy_config
        # 使用 DASHSCOPE_API_KEY（官方推荐）或兼容的 QWEN_API_KEY
        self.api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY")
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or QWEN_API_KEY not set in environment")

        # 使用官方 OpenAI 兼容模式 base_url
        base_url = os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self.base_url = str(base_url).rstrip("/")
        self.endpoint = f"{self.base_url}/chat/completions"

        # 模型配置
        self.model = os.getenv("QWEN_MODEL", "qwen3.6-plus")

        # 生成参数
        self.max_tokens = self._config.LLM_MAX_TOKENS
        self.temperature = self._config.LLM_TEMPERATURE

    def generate_code(self, prompt: str, mode: str = "simple", api_key: str | None = None) -> tuple[str, dict[str, Any]]:
        """使用 OpenAI 兼容模式生成代码"""
        # Use provided api_key or fall back to instance api_key
        effective_api_key = api_key or self.api_key
        if not effective_api_key:
            raise RuntimeError("No API key available (neither provided nor in environment)")

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert AI coding assistant specialized in quantitative trading strategies. Generate clean, efficient Python code.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {effective_api_key}",
            "Content-Type": "application/json",
        }

        last_exc = None
        for attempt in range(3):
            try:
                r = requests.post(self.endpoint, json=payload, headers=headers, timeout=180)
                r.raise_for_status()
                data = r.json()

                # OpenAI 兼容模式返回格式
                content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""

                meta = {
                    "model_used": data.get("model", self.model),
                    "usage": data.get("usage", {}),
                    "request_id": data.get("id"),
                }
                return content, meta
            except Exception as e:
                last_exc = e
                time.sleep(0.5 * (2**attempt))
                continue

        raise RuntimeError(f"QWEN request failed after retries: {last_exc}")


# ---------------------------------------------------------------------------
# 异步 Provider (使用 OpenAI 兼容模式)
# ---------------------------------------------------------------------------


class QwenProvider(BaseLLMProvider):
    """千问 LLM Provider - 使用 OpenAI 兼容模式"""

    name: str = "qwen"

    def __init__(self):
        # 使用 DASHSCOPE_API_KEY（官方推荐）或兼容的 QWEN_API_KEY
        self.api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY", "")
        self.model = os.getenv("QWEN_MODEL", "qwen3.6-plus")

        if not self.api_key:
            import logging
            logging.getLogger(__name__).warning("DASHSCOPE_API_KEY or QWEN_API_KEY not set in environment")
            return

        # 使用官方 OpenAI 兼容模式 base_url
        self.base_url = os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self.endpoint = f"{self.base_url.rstrip('/')}/chat/completions"

    async def generate(self, req: StrategyGenerationRequest) -> StrategyGenerationResult:
        """使用千问生成策略代码"""

        strategy_name = normalize_name(req.description)

        # 构建提示词
        prompt = self._build_prompt(req)

        try:
            # 使用 OpenAI 兼容模式调用
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是一个量化交易策略专家，擅长生成高质量的Python策略代码。",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 4000,
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            import asyncio
            def _call_api():
                r = requests.post(self.endpoint, json=payload, headers=headers, timeout=180)
                r.raise_for_status()
                return r.json()

            data = await asyncio.to_thread(_call_api)

            # OpenAI 兼容模式返回格式
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""

            # 解析代码
            code = self._extract_code(content)

            return StrategyGenerationResult(
                strategy_name=strategy_name,
                rationale=f"使用千问 {self.model} 生成的策略",
                artifacts=[
                    {
                        "filename": f"{strategy_name}.py",
                        "language": "python",
                        "code": code,
                    }
                ],
                metadata={
                    "factors": ["千问生成"],
                    "risk_controls": [],
                    "assumptions": ["使用千问API生成"],
                    "notes": f"model={self.model}, request_id={data.get('id')}",
                },
                provider="qwen",
            )

        except Exception as e:
            raise RuntimeError(f"千问API调用失败: {str(e)}")

    async def chat(self, messages: list, **kwargs) -> Any:
        """通用聊天接口"""
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": kwargs.get("temperature", 0.3),
                "max_tokens": kwargs.get("max_tokens", 4000),
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            import asyncio
            def _call_api():
                r = requests.post(self.endpoint, json=payload, headers=headers, timeout=180)
                r.raise_for_status()
                return r.json()

            data = await asyncio.to_thread(_call_api)
            return (((data.get("choices") or [{}])[0]).get("message") or {})
        except Exception as e:
            raise RuntimeError(f"Qwen聊天失败: {str(e)}")

    async def convert(self, req: StrategyConversionRequest) -> StrategyConversionResponse:
        """转换第三方策略到Qlib格式"""
        from ..generators.qlib_strategy_generator import QlibStrategyCodeGenerator

        try:
            generator = QlibStrategyCodeGenerator(self)
            result = await generator.convert_strategy(
                source_code=req.source_code,
                source_platform=req.source_platform,
                user_requirements=req.user_requirements,
            )

            from ..models import StrategyConversionResponse

            return StrategyConversionResponse(**result)
        except Exception as e:
            from ..models import StrategyConversionResponse

            return StrategyConversionResponse(
                success=False,
                converted_code=f"# 转换失败: {str(e)}",
                conversion_notes=[],
                warnings=[f"发生异常: {str(e)}"],
                suggestions=[],
                platform_differences=[],
                estimated_compatibility=0,
            )

    def _build_prompt(self, req: StrategyGenerationRequest) -> str:
        """构建提示词"""
        strategy_name = normalize_name(req.description)
        prompt = f"""请生成一个量化交易策略。

策略名称: {strategy_name}
策略描述: {req.description or "无描述"}
市场: {req.market}
风险等级: {req.risk_level}
时间周期: {req.timeframe}
初始资金: {req.initial_capital}
单次仓位%: {req.position_size}
最大持仓数: {req.max_positions}
止损%: {req.stop_loss}
止盈%: {req.take_profit}
回测周期: {req.backtest_period}
"""

        if req.symbols:
            prompt += f"\n股票池: {', '.join(req.symbols)}"
        if req.style:
            prompt += f"\n策略风格: {req.style}"
        if req.max_drawdown is not None:
            prompt += f"\n最大回撤%: {req.max_drawdown}"
        if req.commission_rate is not None:
            prompt += f"\n手续费率: {req.commission_rate}"
        if req.slippage is not None:
            prompt += f"\n滑点: {req.slippage}"
        if req.benchmark:
            prompt += f"\n基准指数: {req.benchmark}"

        prompt += """

请生成完整的Python策略代码，包括:
1. 策略类定义
2. 初始化方法
3. 技术指标计算
4. 信号生成逻辑
5. 仓位管理
6. 风险控制

代码要求:
- 使用 pandas, numpy, talib 等标准库
- 包含完整的文档字符串
- 符合 PEP 8 规范
- 包含错误处理
- 参数可配置

请用 ```python 代码块包裹代码。
"""
        return prompt

    def _extract_code(self, content: str) -> str:
        """从响应中提取代码"""
        # 尝试提取 ```python 代码块
        if "```python" in content:
            start = content.find("```python") + 9
            end = content.find("```", start)
            if end != -1:
                return content[start:end].strip()

        # 尝试提取 ``` 代码块
        if "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            if end != -1:
                return content[start:end].strip()

        # 如果没有代码块标记，返回全部内容
        return content.strip()
