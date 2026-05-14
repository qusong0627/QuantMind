"""
DeepSeek LLM - 同步客户端 + 异步 Provider

合并自:

- providers/deepseek_provider.py (DeepseekLLMProvider, 异步)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Dict, Tuple

import requests

if TYPE_CHECKING:
    from ..models import StrategyConversionRequest, StrategyConversionResponse

from ..models import StrategyGenerationRequest, StrategyGenerationResult
from .base import BaseLLMProvider, normalize_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class DeepseekLLM:
    """
    DeepSeek OpenAI-compatible client (sync) used by the API layer.

    Expected env:
    - DEEPSEEK_API_KEY
    - DEEPSEEK_API_URL (preferred) or DEEPSEEK_BASE_URL (compat)
    - DEEPSEEK_MODEL
    """

    def __init__(self, api_key: str | None = None) -> None:
        from ..ai_strategy_config import get_config as _gc

        ai_strategy_config = _gc()

        # 优先使用传入的 api_key，否则从环境变量读取
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set in environment")

        # Prefer DEEPSEEK_API_URL, fallback to DEEPSEEK_BASE_URL for compatibility with existing .env.
        base_url = (
            os.getenv("DEEPSEEK_API_URL")
            or os.getenv("DEEPSEEK_BASE_URL")
            or getattr(ai_strategy_config, "DEEPSEEK_API_URL", None)
            or "https://api.deepseek.com"
        )
        self.base_url = str(base_url).rstrip("/")
        self.endpoint = f"{self.base_url}/chat/completions"

        self.api_key = api_key
        self.model = (
            os.getenv("DEEPSEEK_MODEL") or getattr(ai_strategy_config, "DEEPSEEK_MODEL", None) or "deepseek-chat"
        )

        # Reuse common generation knobs if present.
        self.max_tokens = int(getattr(ai_strategy_config, "DEEPSEEK_MAX_TOKENS", 2000))
        self.temperature = float(getattr(ai_strategy_config, "DEEPSEEK_TEMPERATURE", 0.7))

    def generate_code(self, prompt: str, mode: str = "simple") -> tuple[str, dict[str, Any]]:
        messages = [
            {
                "role": "system",
                "content": "You are an expert AI coding assistant specialized in quantitative trading strategies. Generate clean, efficient Python code.",
            },
            {"role": "user", "content": prompt},
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.post(self.endpoint, json=payload, headers=headers, timeout=180)
                r.raise_for_status()
                data = r.json()
                content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
                meta = {
                    "model_used": data.get("model", self.model),
                    "usage": data.get("usage", {}),
                    "request_id": data.get("id") or data.get("request_id"),
                    "provider": "deepseek",
                    "mode": mode,
                }
                return content, meta
            except Exception as e:  # noqa: BLE001
                last_exc = e
                time.sleep(0.5 * (2**attempt))
                continue

        raise RuntimeError(f"DEEPSEEK request failed after retries: {last_exc}")


# ---------------------------------------------------------------------------
# 异步 Provider (原 providers/deepseek_provider.py)
# ---------------------------------------------------------------------------


class DeepseekProvider(BaseLLMProvider):
    """DeepSeek LLM Provider"""

    name: str = "deepseek"

    def __init__(self):
        try:
            self.llm = DeepseekLLM()
        except Exception as e:
            logger.warning(f"Failed to initialize DeepseekLLM: {e}")
            self.llm = None

    async def generate(self, req: StrategyGenerationRequest) -> StrategyGenerationResult:
        """使用 DeepSeek 生成策略代码"""
        if not self.llm:
            raise RuntimeError("DeepSeek provider not properly initialized (check API key)")

        strategy_name = normalize_name(req.description)
        prompt = self._build_prompt(req)

        # Reuse existing logic from DeepseekLLM
        code, meta = await asyncio.to_thread(self.llm.generate_code, prompt)

        # Parse code (ensure markdown blocks are stripped)
        code = self._extract_code(code)

        return StrategyGenerationResult(
            strategy_name=strategy_name,
            rationale=f"使用 DeepSeek {self.llm.model} 生成的策略",
            artifacts=[
                {
                    "filename": f"{strategy_name}.py",
                    "language": "python",
                    "code": code,
                }
            ],
            metadata={
                "factors": ["DeepSeek生成"],
                "risk_controls": [],
                "assumptions": ["使用DeepSeek API生成"],
                "notes": f"model={self.llm.model}, request_id={meta.get('request_id')}",
            },
            provider="deepseek",
        )

    async def chat(self, messages: list, **kwargs) -> Any:
        """通用聊天接口"""
        if not self.llm:
            raise RuntimeError("DeepSeek provider not properly initialized")

        # Simplistic implementation matching Qwen interface
        payload = {
            "model": self.llm.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 2000),
        }

        r = requests.post(
            self.llm.endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.llm.api_key}",
                "Content-Type": "application/json",
            },
            timeout=180,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]

    async def convert(self, req: StrategyConversionRequest) -> StrategyConversionResponse:
        """转换第三方策略到Qlib格式"""
        # Reuse QlibStrategyCodeGenerator
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
        """构建提示词 (Mirror Qwen logic for consistency)"""
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
        """从响应中提取代码 (Mirror Qwen logic)"""
        if "```python" in content:
            start = content.find("```python") + 9
            end = content.find("```", start)
            if end != -1:
                return content[start:end].strip()

        if "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            if end != -1:
                return content[start:end].strip()

        return content.strip()
