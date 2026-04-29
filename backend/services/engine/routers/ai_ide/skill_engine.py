"""
Skill Engine - AI-IDE 策略生成模板路由引擎

功能：
1. 按用户意图自动路由模板（模型策略 / 传统指标 / 调试防护）
2. 多模板叠加，既给主模板又给防错守卫
3. 注入历史报错，减少"同错重犯"
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SkillEngine:
    """策略生成模板路由引擎"""

    # 意图检测关键词
    TRADITIONAL_KEYWORDS = [
        "MACD", "KDJ", "RSI", "BOLL", "布林", "均线", "MA", "EMA", "SMA",
        "指标", "技术指标", "传统", "backtest", "回测", "信号", "买入", "卖出",
        "cross", "金叉", "死叉", "突破", "支撑", "压力", "趋势",
    ]

    MODEL_KEYWORDS = [
        "模型", "预测", "机器学习", "ML", "AI", "深度学习", "神经网络",
        "RedisTopkStrategy", "TopK", "选股", "因子", "alpha", "分数", "score",
        "STRATEGY_CONFIG", "get_strategy_config", "策略配置",
    ]

    def __init__(self, templates_dir: Optional[str] = None):
        if templates_dir is None:
            templates_dir = os.path.join(os.path.dirname(__file__), "skill_templates")
        self.templates_dir = templates_dir
        self._cache: dict[str, str] = {}

    def detect_intent(self, user_input: str, context: dict) -> list[str]:
        """检测用户意图，返回模板名称列表

        返回列表中可能包含多个模板名，按优先级排序：
        - 主模板（traditional_indicator_backtest 或 qlib_model_strategy_config）
        - 防护模板（debug_guardrail，当有错误信息时）
        """
        templates = []
        user_lower = user_input.lower()
        error_msg = context.get("error_msg", "")

        # 检测主意图
        traditional_score = sum(1 for kw in self.TRADITIONAL_KEYWORDS if kw.lower() in user_lower)
        model_score = sum(1 for kw in self.MODEL_KEYWORDS if kw.lower() in user_lower)

        if model_score > traditional_score:
            templates.append("qlib_model_strategy_config")
        else:
            templates.append("traditional_indicator_backtest")

        # 如果有错误信息，叠加调试防护模板
        if error_msg:
            templates.append("debug_guardrail")

        return templates

    def load_template(self, template_name: str) -> str:
        """加载模板内容（带缓存）"""
        if template_name in self._cache:
            return self._cache[template_name]

        template_path = os.path.join(self.templates_dir, f"{template_name}.md")
        if not os.path.exists(template_path):
            logger.warning(f"Template not found: {template_path}")
            return ""

        try:
            with open(template_path, encoding="utf-8") as f:
                content = f.read()
            self._cache[template_name] = content
            return content
        except Exception as e:
            logger.error(f"Failed to load template {template_name}: {e}")
            return ""

    def build_skill_prompt(self, user_input: str, context: dict) -> str:
        """构建 skill 提示词

        将检测到的模板内容组合成一个完整的约束提示。
        """
        templates = self.detect_intent(user_input, context)
        if not templates:
            return ""

        parts = []
        for template_name in templates:
            content = self.load_template(template_name)
            if content:
                parts.append(f"### {template_name}\n{content}")

        if not parts:
            return ""

        return "\n\n---\n\n".join(parts)

    def get_error_injection(self, error_msg: str) -> str:
        """根据错误信息生成修复建议注入

        针对常见错误模式，生成具体的修复指导。
        """
        if not error_msg:
            return ""

        error_patterns = {
            "NameError: name 'qlib' is not defined": (
                "检测到 qlib 未定义错误。修复方案：\n"
                "1. 在文件顶部添加 `import qlib`\n"
                "2. 在使用前调用 `qlib.init(provider_uri='/app/db/qlib_data', region='cn')`"
            ),
            "ModuleNotFoundError: No module named 'quantmind'": (
                "检测到 quantmind 模块不存在。修复方案：\n"
                "删除 `from quantmind.api import ...` 或 `import quantmind`，"
                "改用 qlib 或 pandas 方案。"
            ),
            "ModuleNotFoundError: No module named 'qlib.contrib.signal'": (
                "检测到 qlib.contrib.signal 模块不存在。修复方案：\n"
                "删除该导入，使用 pandas/ta 库自行计算指标，或使用 Qlib 已验证接口。"
            ),
            "ImportError: cannot import name 'backtest' from qlib.contrib.evaluate": (
                "检测到 qlib.contrib.evaluate.backtest 导入失败。修复方案：\n"
                "删除该导入，使用项目内统一回测封装或自定义轻量回测统计。"
            ),
            "FileNotFoundError": (
                "检测到文件路径错误。修复方案：\n"
                "1. 禁止使用占位路径如 `path/to/your/data.csv`\n"
                "2. 使用 qlib 数据：`qlib.init(provider_uri='/app/db/qlib_data', region='cn')` + `D.features(...)`"
            ),
        }

        for pattern, fix in error_patterns.items():
            if pattern in error_msg:
                return f"\n\n[错误修复指导]:\n{fix}"

        # 通用错误注入
        return (
            f"\n\n[历史错误信息]:\n{error_msg}\n"
            "请分析上述错误并修复代码，确保：\n"
            "1. 不引入新的依赖问题\n"
            "2. 保持原有策略意图\n"
            "3. 输出完整可运行的代码"
        )
