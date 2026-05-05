import os
import json
import time
import logging
import httpx
from typing import List, Optional, Dict, Any
from collections.abc import AsyncGenerator
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .skill_engine import SkillEngine

logger = logging.getLogger(__name__)
router = APIRouter()

# --- Core Logic (Ported from AI-IDE Agent) ---


class KnowledgeBase:
    """缓存 TTL 5 分钟，过期后自动重新加载文档。"""

    _CACHE_TTL_SEC = 300

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.doc_paths = [
            "docs/Qlib内部策略开发规范.md",
            "docs/QuantMind_152维特征方案规范.md",
            "docs/Qlib回测API集成指南.md",
        ]
        self._cached_context = ""
        self._cached_at = 0.0

    def get_context_summary(self) -> str:
        now = time.monotonic()
        if self._cached_context and (now - self._cached_at) < self._CACHE_TTL_SEC:
            return self._cached_context
        summary = "### QuantMind Project Standards & API Reference:\n"
        for doc_rel_path in self.doc_paths:
            full_path = os.path.join(self.project_root, doc_rel_path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, encoding="utf-8") as f:
                        content = f.read()
                        summary += f"\n-- From {doc_rel_path} --\n{content[:3000]}\n"
                except Exception as e:
                    summary += f"\n-- Error reading {doc_rel_path}: {str(e)} --\n"
            else:
                summary += f"\n-- File not found: {doc_rel_path} --\n"
        self._cached_context = summary
        self._cached_at = now
        return summary


class QuantAgent:
    def __init__(self, api_key: str, base_url: str, model: str, project_root: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.project_root = project_root
        self.kb = KnowledgeBase(project_root)
        self.skill_engine = SkillEngine()
        self._kb_context_cached = None
        self._system_prompt_base = self._build_system_prompt_base()

    def _build_system_prompt_base(self):
        """构建不包含知识库的 system prompt 基础部分"""
        strategy_classes = """
### 可用策略类及参数规范：

**1. RedisTopkStrategy** - TopK 选股策略（最常用）
```python
def get_strategy_config():
    return {
        "class": "RedisTopkStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
        "kwargs": {
            "signal": "<PRED>",  # 使用平台默认模型预测
            "topk": 50,          # 持仓股票数量
            "n_drop": 5,         # 每次调仓剔除数量
            "rebalance_days": 3, # 调仓周期（天）
            "account_stop_loss": 0.1,  # 账户级止损（10%）
            "max_leverage": 1.0,       # 最大杠杆
            "only_tradable": True,     # 剔除停牌/涨跌停
        }
    }
```

**2. RedisLongShortTopkStrategy** - 多空策略
- 参数：topk, short_topk, long_exposure(1.0), short_exposure(1.0), max_weight, min_score
- 多头取预测分最高的 topk 只，空头取最低的 short_topk 只
- 自动过滤非两融标的（空头侧）

**3. RedisWeightStrategy** - 分数权重策略
- 参数：signal, min_score(0.0), max_weight(0.05)
- 按预测分数归一化分配权重

**4. RedisVolatilityWeightedStrategy** - 波动率加权策略
- 参数：topk, vol_lookback(20), max_weight(0.10), min_score
- 低波动标的获得更高权重

**5. RedisAdvancedAlphaStrategy** - 高级截面 Alpha 策略
- 继承 RedisTopkStrategy，结合分数权重与 TopK-Dropout
- 参数：max_weight(0.05), min_score(0.0)

**6. RedisStopLossStrategy** - 止损止盈策略
- 参数：stop_loss(-0.08), take_profit(0.15)
- 持仓成本追踪，触发阈值强制卖出

**7. RedisFullAlphaStrategy** - 全量截面策略
- 参数：topk, max_weight(0.05)
- 跌出 TopK 全部卖出，不可买入标的自动顺延补位

### 策略开发规范：
1. 必须使用 `get_strategy_config()` 或 `STRATEGY_CONFIG` 作为入口
2. 自定义参数必须在 `__init__` 中 `pop` 后再调用 `super().__init__(**kwargs)`
3. `reset` 方法必须兼容可变参数：`def reset(self, *args, **kwargs)`
4. 禁用：os, sys, subprocess, requests, socket 等危险模块
"""
        return (
            "你是 QuantMind 的智能助手，用自然、友好的方式与用户交流。\n\n"
            "## 核心对话规则（必须严格遵守）\n"
            "1. **绝对禁止主动生成策略代码**：除非用户明确要求编写/修改策略，否则绝不要输出任何策略代码、回测代码或技术指标计算代码。\n"
            "2. 对于问候、闲聊、感谢、自我介绍等非技术类消息，用简洁、友好的方式回应即可（1-2句话），不要展开技术话题，不要提供代码示例。\n"
            "3. 当用户提出具体的策略开发、代码编写、回测分析等技术需求时，再给出专业建议和可执行代码。\n"
            "4. 先确认用户意图再行动，不要假设用户需要生成策略。\n"
            "5. 代码修改优先输出最小改动，明确标注文件路径。\n"
            "6. 涉及策略时，优先推荐使用平台内置的模型驱动策略（RedisTopkStrategy 等），传统指标策略仅在用户明确要求时使用。\n"
            "7. 使用简体中文回答，结论优先，步骤清晰。\n\n"
            "## 重要提醒\n"
            "- 用户说'你好'、'在吗'、'你是谁'等问候语时，只需简单问候回应\n"
            "- 只有当用户明确说'帮我写一个...策略'、'修改代码'、'回测'等技术指令时，才使用下面的技术规范\n"
            "- 下面的技术文档仅供参考，不要在用户未要求时主动使用\n\n"
            f"{strategy_classes}\n\n"
            "FORMATTING RULES:\n"
            "1. 使用标准 Markdown 输出。\n"
            "2. 涉及代码修改时，优先使用 SEARCH/REPLACE：\n"
            "<<<< SEARCH\n原始代码\n====\n修改后代码\n>>>>\n"
            "3. 新增代码使用 ```python ... ``` 代码块。"
        )

    def _get_system_prompt(self, user_input: str, context: dict) -> str:
        """根据用户输入动态构建 system prompt

        仅在用户提出技术问题时才注入知识库文档。
        """
        # 判断是否是技术性问题
        is_technical = self._is_technical_query(user_input, context)

        if is_technical:
            # 技术性问题：注入知识库
            if self._kb_context_cached is None:
                self._kb_context_cached = self.kb.get_context_summary()
            return f"{self._system_prompt_base}\n\n## 技术参考文档\n{self._kb_context_cached}"
        else:
            # 非技术性问题：不注入知识库
            return self._system_prompt_base

    def _is_technical_query(self, user_input: str, context: dict) -> bool:
        """判断用户输入是否是技术性查询"""
        # 有代码上下文或错误信息，肯定是技术问题
        if (
            context.get("current_code")
            or context.get("error_msg")
            or context.get("selection")
        ):
            return True

        # 检查历史消息是否有技术上下文
        history = context.get("history", [])
        for msg in history:
            if msg.get("role") == "user" and any(
                kw in msg.get("content", "").lower()
                for kw in [
                    "策略",
                    "代码",
                    "回测",
                    "指标",
                    "macd",
                    "kdj",
                    "rsi",
                    "boll",
                    "买入",
                    "卖出",
                    "因子",
                    "模型",
                    "预测",
                    "机器学习",
                    "ai",
                    "选股",
                    "topk",
                    "分数",
                    "score",
                    "config",
                    "配置",
                ]
            ):
                return True

        # 检查当前输入是否包含技术关键词
        user_lower = user_input.lower()
        technical_keywords = [
            "策略",
            "代码",
            "回测",
            "指标",
            "macd",
            "kdj",
            "rsi",
            "boll",
            "布林",
            "均线",
            "ma",
            "ema",
            "sma",
            "技术指标",
            "传统",
            "backtest",
            "信号",
            "买入",
            "卖出",
            "cross",
            "金叉",
            "死叉",
            "突破",
            "支撑",
            "压力",
            "趋势",
            "模型",
            "预测",
            "机器学习",
            "ml",
            "ai",
            "深度学习",
            "神经网络",
            "redistopkstrategy",
            "topk",
            "选股",
            "因子",
            "alpha",
            "分数",
            "score",
            "strategy_config",
            "get_strategy_config",
            "策略配置",
            "修改",
            "优化",
            "调试",
            "error",
            "bug",
            "报错",
            "异常",
            "fix",
            "修复",
        ]
        return any(kw in user_lower for kw in technical_keywords)

    async def chat_stream(
        self, prompt: str, context: dict
    ) -> AsyncGenerator[str, None]:
        # 动态生成 system prompt（根据用户输入决定是否注入知识库）
        system_prompt = self._get_system_prompt(prompt, context)
        messages = [{"role": "system", "content": system_prompt}]
        history = context.get("history", [])
        for msg in history:
            messages.append(msg)
        messages.append(
            {"role": "user", "content": self._format_user_prompt(prompt, context)}
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.1,
        }

        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code != 200:
                    try:
                        err_body = await response.aread()
                        logger.error(
                            f"LLM API Error: Status={response.status_code}, Body={err_body.decode('utf-8', 'ignore')}"
                        )
                    except:
                        pass
                    yield f"**Error:** AI 服务返回状态码 {response.status_code}。请确认设置中的 API Key 是否正确有效。"
                    return

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    if line.startswith("data: [DONE]"):
                        break
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            delta = data["choices"][0]["delta"]
                            if "content" in delta:
                                yield delta["content"]
                        except:
                            continue

    def _format_user_prompt(self, user_input: str, context: dict) -> str:
        assistant_rules = str(context.get("assistant_rules", "") or "").strip()
        current_code = context.get("current_code", "")
        error_msg = context.get("error_msg", "")
        selection = context.get("selection", "")
        file_path = str(context.get("file_path", "") or "").strip()
        prompt = f"User Request: {user_input}\n"
        if assistant_rules:
            prompt += f"\n[Development Rules]:\n{assistant_rules}\n"
        if file_path:
            prompt += f"\n[File Path]:\n{file_path}\n"
        if current_code:
            prompt += f"\n[Current Code]:\n```python\n{current_code}\n```\n"
        if selection:
            prompt += f"\n[Selection]:\n```python\n{selection}\n```\n"
        if error_msg:
            prompt += f"\n[Error]:\n{error_msg}\n"

        # 注入 Skill 模板约束
        skill_prompt = self.skill_engine.build_skill_prompt(user_input, context)
        if skill_prompt:
            prompt += f"\n\n[Skill Constraints]:\n{skill_prompt}\n"

        # 注入错误修复指导
        if error_msg:
            error_injection = self.skill_engine.get_error_injection(error_msg)
            if error_injection:
                prompt += error_injection

        return prompt


# --- API Router Logic ---


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    current_code: str | None = None
    selection: str | None = None
    error_msg: str | None = None
    file_path: str | None = None
    history: list[ChatMessage] | None = []
    extra_context: dict[str, Any] | None = None


@router.post("/chat")
async def chat_completions(request: Request, item: ChatRequest):
    # 1. 解析基础配置 (支持多种环境变量命名方式以对齐服务器 .env)
    base_url = (
        os.getenv("AI_IDE_LLM_BASE_URL")
        or os.getenv("AI_IDE_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = os.getenv("AI_IDE_LLM_MODEL") or os.getenv("AI_IDE_MODEL") or "qwen-max"
    api_key = (
        os.getenv("AI_IDE_LLM_API_KEY")
        or os.getenv("AI_IDE_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )

    # 2. 尝试从网关透传的用户身份获取私有 API Key (个人 Key 优先级最高)
    user_context = getattr(request.state, "user", None)
    if user_context:
        user_id = user_context.get("user_id")
        tenant_id = user_context.get("tenant_id", "default")
        # 尝试从 User Service 动态获取当前用户的私有 Key
        try:
            from backend.shared.auth import get_internal_call_secret

            # OSS 单容器模式使用 127.0.0.1
            api_gateway = os.getenv("INTERNAL_API_GATEWAY_URL", "http://127.0.0.1:8000")
            async with httpx.AsyncClient(timeout=3.0) as client:
                headers = {
                    "X-Internal-Call": get_internal_call_secret(),
                    "X-User-Id": user_id,
                    "X-Tenant-Id": tenant_id,
                }
                resp = await client.get(
                    f"{api_gateway}/api/v1/profiles/{user_id}", headers=headers
                )
                if resp.status_code == 200:
                    profile_key = resp.json().get("data", {}).get("ai_ide_api_key")
                    if profile_key:
                        api_key = profile_key
        except Exception as e:
            logger.warning(
                f"Could not fetch individual API key for user {user_id}: {e}"
            )

    # 获取项目根目录，以便读取文档
    project_root = os.getcwd()

    # 打印非敏感初始化参数以便诊断
    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"
    logger.info(
        f"Initializing QuantAgent: model={model}, base_url={base_url}, api_key={masked_key}"
    )

    # 检测 mock key
    mock_key_patterns = ["mock-api-key", "not-configured", "placeholder"]
    if not api_key or any(pattern in api_key for pattern in mock_key_patterns):
        logger.error("Chat failed: API Key is empty or mock key")
        raise HTTPException(
            status_code=500, detail="API Key 未配置。请在个人中心配置您的 API Key。"
        )

    agent = QuantAgent(api_key, base_url, model, project_root)

    context = {
        "current_code": item.current_code,
        "selection": item.selection,
        "error_msg": item.error_msg,
        "file_path": item.file_path,
        "history": [m.model_dump() for m in (item.history or [])],
        **(item.extra_context or {}),
    }

    async def event_generator():
        try:
            async for chunk in agent.chat_stream(item.message, context):
                if chunk:
                    data = json.dumps({"delta": chunk}, ensure_ascii=False)
                    yield f"data: {data}\n\n"
        except Exception as e:
            logger.error(f"Chat stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/stop")
async def stop_chat():
    return {"status": "stopped"}
