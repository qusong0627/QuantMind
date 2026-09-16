#!/usr/bin/env python3
"""
JSON处理工具模块
提供JSON解析、修复和处理功能
"""

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def fix_incomplete_json(json_str: str) -> str:
    """尝试修复不完整的JSON字符串

    Args:
        json_str: 可能不完整的JSON字符串

    Returns:
        修复后的JSON字符串
    """
    if not json_str:
        return json_str

        # 第一步：修复常见的截断问题
        # 如果在字符串中间被截断（以反斜杠结尾但不是有效的转义序列）
    if json_str.endswith("\\") and not json_str.endswith("\\\\"):
        # 如果以单个反斜杠结尾，可能是截断的转义序列，移除它
        json_str = json_str[:-1]
        logger.info("移除了末尾的反斜杠")

        # 第二步：检查是否在Python代码字符串中被截断
        # 查找python_code字段的开始
    python_code_start = json_str.find('"python_code":')
    if python_code_start != -1:
        # 找到python_code值的开始（第一个引号）
        code_value_start = json_str.find('"', python_code_start)
        if code_value_start != -1:
            # 从这里开始检查引号平衡
            code_section = json_str[code_value_start + 1 :]
            in_string = True
            escape_count = 0

            # 计算字符串内有效的引号数量（排除转义的）
            i = 0
            while i < len(code_section):
                if code_section[i] == "\\":
                    escape_count += 1
                    i += 1  # 跳过下一个字符
                elif code_section[i] == '"':
                    if escape_count % 2 == 0:  # 偶数个转义符，这个引号没有转义
                        if in_string:
                            in_string = False
                            break
                    escape_count = 0
                else:
                    pass
                i += 1

                # 如果还在字符串中，说明代码被截断了
            if in_string:
                # 找到截断点
                cutoff_point = code_value_start + 1 + i
                json_str = json_str[:cutoff_point] + '"'  # 闭合字符串
                logger.info("修复了截断的python_code字符串")

                # 第三步：常规的括号和引号平衡检查
    open_braces = json_str.count("{")
    close_braces = json_str.count("}")
    open_quotes = json_str.count('"')

    # 如果缺少闭合括号，添加它们
    if open_braces > close_braces:
        missing_braces = open_braces - close_braces
        json_str += "}" * missing_braces
        logger.info(f"添加了{missing_braces}个闭合括号")

        # 如果引号数量是奇数，说明字符串未闭合（在python_code修复后不应该发生）
    if open_quotes % 2 != 0:
        json_str += '"'
        logger.info("添加了缺失的引号")

        # 第四步：修复其他截断的键值对
    if json_str and json_str[-1] not in ',}]"':
        # 尝试找到最后一个完整的键值对
        last_colon = json_str.rfind(":")
        if last_colon > 0:
            # 查看是否在字符串值中被截断
            key_part = json_str[:last_colon]
            last_quote = key_part.rfind('"')
            if last_quote > 0:
                key = key_part[last_quote:].strip('" ')
                if key:
                    json_str = json_str[:last_colon]
                    json_str += ': "..."'
                    logger.info(f"修复了截断的键值对: {key}")

    return json_str


def extract_json_from_content(content: str) -> dict[str, Any] | None:
    """从文本内容中提取JSON对象

    Args:
        content: 包含JSON的文本内容

    Returns:
        解析出的JSON对象，如果失败返回None
    """
    if not content:
        return None

        # 首先尝试直接解析整个内容
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

        # 尝试提取JSON代码块
    json_match = re.search(r"```json\s*\n(.*?)\n```", content, re.DOTALL)
    if json_match:
        try:
            json_content = json_match.group(1)
            logger.info(f"找到JSON代码块，长度: {len(json_content)}")
            return json.loads(json_content)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON代码块解析失败: {e}")
            # 尝试修复
            try:
                fixed_json = fix_incomplete_json(json_content)
                return json.loads(fixed_json)
            except Exception as fix_e:
                logger.warning(f"修复JSON也失败: {fix_e}")

                # 如果没有代码块，查找任意JSON对象
    json_match = re.search(r"\{[\s\S]*\}", content)
    if json_match:
        try:
            json_content = json_match.group()
            logger.info(f"找到JSON对象，长度: {len(json_content)}")
            return json.loads(json_content)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON对象解析失败: {e}")

    return None


def extract_python_code_from_json(result: dict[str, Any], content: str = "") -> str:
    """从JSON结果中提取Python代码

    Args:
        result: 解析的JSON结果
        content: 原始内容（备用）

    Returns:
        提取的Python代码字符串
    """
    # 首先尝试直接获取代码
    python_code = result.get("python_code", result.get("code", ""))

    if python_code:
        return python_code

        # 如果没有直接找到代码，尝试从rationale中提取
    if "rationale" in result:
        rationale_content = result["rationale"]

        # 尝试提取JSON代码块
        json_match = re.search(r"```json\n(.*?)\n```", rationale_content, re.DOTALL)
        if json_match:
            try:
                json_data = json.loads(json_match.group(1))
                python_code = json_data.get("python_code", "")
                if python_code:
                    logger.info("从rationale中的JSON代码块提取到Python代码")
                    return python_code
            except json.JSONDecodeError:
                logger.warning("解析rationale中的JSON代码块失败")

                # 尝试查找JSON对象
        if not python_code:
            json_obj_match = re.search(r'\{[\s\S]*?"python_code"[\s\S]*?\}', rationale_content)
            if json_obj_match:
                try:
                    json_data = json.loads(json_obj_match.group())
                    python_code = json_data.get("python_code", "")
                    if python_code:
                        logger.info("从rationale中的JSON对象提取到Python代码")
                        return python_code
                except json.JSONDecodeError:
                    logger.warning("解析rationale中的JSON对象失败")

                    # 尝试提取Python代码块
        if not python_code:
            code_match = re.search(r"```python\n(.*?)\n```", rationale_content, re.DOTALL)
            if code_match:
                python_code = code_match.group(1)
                logger.info("从rationale中的Python代码块提取到代码")
                return python_code

                # 如果还是没有，尝试从原始内容中提取
    if not python_code and content:
        python_code_match = re.search(r'"python_code":\s*"(.*?)(?:"\s*(?:,|}))', content, re.DOTALL)
        if not python_code_match:
            # 尝试另一种模式
            python_code_match = re.search(
                r'"python_code"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
                content,
                re.DOTALL,
            )

        if python_code_match:
            try:
                # 解码转义的Python代码
                raw_code = python_code_match.group(1)
                python_code = decode_python_code(raw_code)
                logger.info(f"从内容中提取到Python代码，长度: {len(python_code)}")
                return python_code
            except Exception as e:
                logger.warning(f"提取部分代码失败: {e}")

    return ""


def decode_python_code(encoded_code: str) -> str:
    """解码转义的Python代码

    Args:
        encoded_code: 转义的Python代码字符串

    Returns:
        解码后的Python代码
    """
    # 处理常见的转义序列，按优先级顺序
    python_code = encoded_code.replace('\\"', '"')  # 必须先处理转义的引号
    python_code = python_code.replace("\\\\", "\\")  # 然后处理双反斜杠
    python_code = python_code.replace("\\n", "\n")  # 处理换行符
    python_code = python_code.replace("\\t", "\t")  # 处理制表符
    python_code = python_code.replace("\\r", "\r")  # 处理回车符

    # 尝试解码unicode转义
    try:
        if "\\u" in python_code:
            python_code = python_code.encode("utf-8").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeError):
        logger.warning("unicode_escape解码失败，使用原始编码")

        # 清理残留的转义序列
    python_code = re.sub(r'\\(?!["\\nrt])', "", python_code)

    return python_code


def create_fallback_strategy_result(description: str, content: str = "") -> dict[str, Any]:
    """创建回退策略结果

    Args:
        description: 策略描述
        content: 原始内容

    Returns:
        回退策略结果
    """
    from backend.shared.strategy_format import build_scaffold_strategy_code

    return {
        "strategy_name": f"{description[:10]}策略",
        "rationale": content if content else "AI生成的策略",
        "artifacts": [
            {
                "filename": "strategy.py",
                "language": "python",
                # T-P3-03：原 handle_data 伪代码无执行器，改产声明式占位骨架
                "code": build_scaffold_strategy_code(description),
            }
        ],
        "metadata": {
            "factors": ["自定义指标"],
            "risk_controls": ["止损", "仓位管理"],
            "assumptions": ["市场有效性"],
            "notes": "AI生成策略，请谨慎使用",
        },
        "provider": "deepseek",
        "generated_at": None,
    }
