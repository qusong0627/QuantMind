import json
import logging
import os
from typing import Any, Dict

from openai import AsyncOpenAI

from .prompts import PARSER_SYSTEM_PROMPT
from .schema_retriever import get_schema_retriever
from .vector_parser import get_strategy_vector_parser

logger = logging.getLogger(__name__)


class IntentParser:
    MOCK_KEY_PATTERNS = ["mock-api-key", "not-configured", "placeholder"]

    def __init__(self):
        # 兼容 ai_strategy 的配置读取方式
        api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY")
        # 检测 mock key
        if api_key and any(pattern in api_key for pattern in self.MOCK_KEY_PATTERNS):
            raise RuntimeError(
                "API Key 未配置真实密钥。请在个人中心配置您的 API Key。"
            )
        base_url = os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = os.getenv("DASHSCOPE_MODEL", "qwen-max")

    async def parse(self, query: str) -> dict[str, Any]:
        try:
            # Stage 1: 向量语义解析
            vector_parser = await get_strategy_vector_parser()
            category, confidence = vector_parser.match_strategy_prototype(query)

            semantic_context = f"用户此需求在语义上最接近 '{category}' 类策略（匹配度: {confidence:.2f}）。"
            if category == "value_investing":
                semantic_context += " 重点关注低估值因子指标。"
            elif category == "growth_investing":
                semantic_context += " 重点关注成长性及盈利质量指标。"
            elif category == "technical_analysis":
                semantic_context += " 重点处理指标交叉、背离等时序逻辑。"

            logger.info(f"Semantic routing result: {category} ({confidence:.2f})")

            # Stage 1.5: Schema RAG（字段/表检索）
            retriever = await get_schema_retriever()
            schema_info = await retriever.retrieve(query, top_k=12)
            target_table = schema_info["target_table"]
            candidate_fields = schema_info["candidate_fields"]
            allowed_fields = set(schema_info["allowed_fields"])

            # Stage 2: Qwen-max 生成结构化过滤条件
            formatted_system_prompt = PARSER_SYSTEM_PROMPT.format(
                semantic_context=semantic_context,
                target_table=target_table,
                candidate_fields="\n".join(f"- {f['name']}: {f['description']}" for f in candidate_fields)
                or "（无候选字段）",
            )

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": formatted_system_prompt},
                    {"role": "user", "content": query},
                ],
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            logger.info(f"Raw Qwen-max intent response: {content}")

            if not content or not content.strip():
                logger.error("Empty content from LLM")
                return {
                    "filters": [],
                    "complex_logic": "Empty response",
                    "date_context": "2026-02-01",
                }

            result = json.loads(content)
            # 注入语义分类信息供后续使用
            result["semantic_category"] = category
            result["query"] = query
            result["target_table"] = result.get("target_table") or target_table

            # 过滤非法字段
            filters = result.get("filters") or []
            sanitized_filters = []
            for f in filters:
                field = f.get("field")
                if field in allowed_fields:
                    sanitized_filters.append(f)
            result["filters"] = sanitized_filters
            result["fields_used"] = [f.get("field") for f in sanitized_filters if f.get("field")]
            result["candidate_fields"] = candidate_fields
            result["allowed_fields"] = list(allowed_fields)
            return result

        except Exception as e:
            logger.error(f"Selection intent parsing failed: {e}")
            return {
                "filters": [],
                "complex_logic": str(e),
                "date_context": "2026-02-01",
            }


_parser = None


def get_intent_parser():
    global _parser
    if _parser is None:
        _parser = IntentParser()
    return _parser
