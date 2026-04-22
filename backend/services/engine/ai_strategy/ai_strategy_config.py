"""
AI策略生成服务配置
"""

import json
import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_ide_config_api_key() -> str:
    """从 AI-IDE 的 config.json 读取用户配置的 API Key"""
    # 尝试多个可能的数据目录
    data_dirs = [
        os.getenv("AI_IDE_DATA_DIR"),
        "/app/data",  # Docker 容器内
        os.path.join(os.path.dirname(__file__), "data"),
    ]
    for data_dir in data_dirs:
        if not data_dir:
            continue
        config_path = os.path.join(data_dir, "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                    if config.get("qwen_api_key"):
                        return config["qwen_api_key"]
            except Exception:
                pass
    return ""


class AIStrategyConfig(BaseSettings):
    """AI策略生成配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )

    # ============ 服务配置 ============
    SERVICE_NAME: str = "AI Strategy Generator"
    SERVICE_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # ============ LLM配置 ============
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "qwen")
    LLM_MODEL: str = os.getenv("QWEN_MODEL", "qwen3.6-plus")
    # 支持 DASHSCOPE_API_KEY（官方推荐）或 QWEN_API_KEY
    LLM_API_KEY: str = os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY", "")
    # 使用官方 OpenAI 兼容模式 base_url
    LLM_API_BASE: str = os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    LLM_TEMPERATURE: float = 0.3  # 降低随机性，提高代码质量
    LLM_MAX_TOKENS: int = 4000
    LLM_TIMEOUT: int = 60  # 秒

    # ============ DashScope Embedding配置 ============
    DASHSCOPE_EMBEDDING_MODEL: str = os.getenv("DASHSCOPE_EMBEDDING_MODEL", "text-embedding-v4")
    DASHSCOPE_EMBEDDING_TIMEOUT: int = int(os.getenv("DASHSCOPE_EMBEDDING_TIMEOUT", "60"))

    # ============ DeepSeek配置 ============
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_API_URL: str = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    DEEPSEEK_MAX_TOKENS: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4000"))
    DEEPSEEK_TEMPERATURE: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.7"))

    # ============ Tushare配置 ============
    TUSHARE_TOKEN: str = os.getenv("TUSHARE_TOKEN", "")
    TUSHARE_DATA_CACHE_TTL: int = 3600  # 数据缓存1小时
    TUSHARE_MAX_RETRIES: int = 3
    TUSHARE_RETRY_DELAY: int = 1  # 秒

    # ============ 策略生成配置 ============
    MAX_GENERATION_TIME: int = 300  # 最长生成时间5分钟
    ENABLE_CODE_VALIDATION: bool = True
    ENABLE_AUTO_BACKTEST: bool = True
    MAX_CONCURRENT_TASKS: int = 10

    # ============ 数据查询配置 ============
    DEFAULT_DATA_PERIOD: str = "2y"  # 默认2年数据
    MAX_STOCKS_PER_QUERY: int = 30  # 单次最多查询30只股票
    MIN_DATA_POINTS: int = 60  # 最少60个数据点

    # ============ 代码安全配置 ============
    FORBIDDEN_MODULES: list = [
        "os",
        "sys",
        "subprocess",
        "shutil",
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "file",
        "input",
    ]
    FORBIDDEN_KEYWORDS: list = [
        "os.",
        "sys.",
        "subprocess.",
        "eval(",
        "exec(",
        "__import__",
        "globals(",
        "locals(",
    ]
    MAX_CODE_LENGTH: int = 10000  # 最大代码长度
    MAX_CODE_LINES: int = 500  # 最大代码行数

    # ============ 回测配置 ============
    BACKTEST_INITIAL_CASH: float = 1_000_000  # 初始资金100万
    BACKTEST_COMMISSION: float = 0.0003  # 佣金0.03%
    BACKTEST_SLIPPAGE: float = 0.001  # 滑点0.1%
    BACKTEST_BENCHMARK: str = "000300.SH"  # 沪深300基准

    # ============ Redis配置 (缓存) ============
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB: int = int(os.getenv("REDIS_DB_ENGINE", os.getenv("REDIS_DB", "5")))
    REDIS_PASSWORD: str = os.getenv("REDIS_PASSWORD", "")

    # ============ 数据库配置 ============
    # 统一优先使用 DATABASE_URL，兼容 AI_STRATEGY_DB_URL
    DATABASE_URL: str = os.getenv("DATABASE_URL", os.getenv("AI_STRATEGY_DB_URL", ""))

    # ============ 监控配置 ============
    ENABLE_METRICS: bool = True
    METRICS_PORT: int = 9095

    # ============ OpenClaw 选股服务配置 ============
    OPENCLAW_BASE_URL: str = os.getenv("OPENCLAW_BASE_URL", "http://127.0.0.1:8015")

    # ============ Strategy Service 同步配置 ============
    STRATEGY_SERVICE_URL: str = os.getenv("STRATEGY_SERVICE_URL", "http://127.0.0.1:8001")
    STRATEGY_SYNC_ENABLED: bool = os.getenv("STRATEGY_SYNC_ENABLED", "true").lower() == "true"

    # ============ 日志配置 ============
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class LLMProviderConfig:
    """LLM提供商配置 - 仅支持Qwen"""

    @staticmethod
    def get_qwen_config(base_config: AIStrategyConfig):
        """千问配置（使用官方 OpenAI 兼容模式）"""
        # 优先级：config.json > DASHSCOPE_API_KEY > QWEN_API_KEY
        api_key = _load_ide_config_api_key() or os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY", "")
        return {
            "api_key": api_key,
            "api_url": os.getenv(
                "DASHSCOPE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            "model": os.getenv("QWEN_MODEL", "qwen3.6-plus"),
            "temperature": base_config.LLM_TEMPERATURE,
            "max_tokens": base_config.LLM_MAX_TOKENS,
            "timeout": base_config.LLM_TIMEOUT,
        }


# Prompt模板
class PromptTemplates:
    """Prompt模板集合"""

    # 需求解析Prompt
    REQUIREMENT_PARSE = """
你是一个专业的量化交易策略分析师。请分析用户的策略需求，提取关键信息。

用户输入: {user_input}

请按照以下JSON格式返回:
{{
    "strategy_type": "策略类型 (trend_following/mean_reversion/arbitrage/breakout/momentum/other)",
    "indicators": ["需要的技术指标列表，如: MA, MACD, RSI, BOLL, KDJ, ATR等"],
    "timeframe": "时间周期 (1min/5min/15min/30min/1h/daily/weekly/monthly)",
    "symbols": ["股票范围，如: hs300, sz50, 具体股票代码等"],
    "entry_conditions": ["入场条件的自然语言描述"],
    "exit_conditions": ["出场条件的自然语言描述"],
    "risk_params": {{
        "stop_loss": "止损比例或描述",
        "take_profit": "止盈比例或描述",
        "max_position": "最大仓位比例"
    }},
    "data_requirements": {{
        "start_date": "数据开始日期 (YYYY-MM-DD)",
        "end_date": "数据结束日期 (YYYY-MM-DD)",
        "min_bars": "最少需要的K线数量"
    }}
}}

分析要点:
1. 准确识别策略类型和交易逻辑
2. 列出所有需要的技术指标
3. 明确时间周期和股票范围
4. 提取风险管理参数
5. 确定数据需求，确保有足够的历史数据

只返回JSON，不要其他内容。
"""

    # 策略设计Prompt
    STRATEGY_DESIGN = """
你是一个量化策略设计专家。基于以下信息设计一个完整的交易策略。

需求: {requirement}

数据分析结果:
- 数据范围: {data_range}
- 统计特征: {statistics}
- 趋势分析: {trend_analysis}
- 波动率: {volatility}

请设计策略并返回JSON格式:
{{
    "strategy_name": "策略名称",
    "strategy_description": "策略详细描述，包括策略原理、适用市场等",
    "entry_logic": {{
        "conditions": ["具体的入场条件，如: MACD金叉且价格突破MA20"],
        "logic": "条件组合逻辑 (AND表示所有条件满足, OR表示任一条件满足)"
    }},
    "exit_logic": {{
        "conditions": ["具体的出场条件"],
        "logic": "条件组合逻辑"
    }},
    "position_sizing": {{
        "method": "仓位计算方法 (fixed/kelly/volatility_based等)",
        "formula": "计算公式或说明"
    }},
    "risk_management": {{
        "stop_loss": "止损策略，包括止损点位计算方法",
        "take_profit": "止盈策略",
        "max_drawdown": "最大回撤限制",
        "position_limit": "单只股票仓位限制"
    }},
    "parameters": {{
        "param_name": {{
            "default": 默认值,
            "range": [最小值, 最大值],
            "step": 步长,
            "description": "参数说明"
        }}
    }},
    "implementation_notes": [
        "实现要点1: 如数据预处理方法",
        "实现要点2: 如信号过滤规则",
        "实现要点3: 如特殊情况处理"
    ]
}}

设计要求:
1. 策略逻辑清晰、可实现
2. 参数设置合理，有优化空间
3. 风险控制完善
4. 考虑交易成本和滑点
5. 避免过度拟合

只返回JSON，不要其他内容。
"""

    CODE_GENERATION = """
你是一个精简高效的 Python 量化代码专家。请根据提供的策略设计生成完整但【极简】的 Python 代码。

代码要求:
1. 继承 BaseStrategy 基类。
2. 保持极致简洁：仅包含核心逻辑，【严禁】添加无用的参数、过度设计的配置项或冗余的注释。
3. 参数化建议：除非核心逻辑必需（如周期），否则尽量硬编码合理默认值，不要暴露过多不常用的 UI 配置项。
4. 核心方法实现：
   - __init__(self, params): 初始化最简配置。
   - on_bar(self, bar): 处理每根K线数据。
   - generate_signals(self, data): 计算信号并输出 DataFrame (列包含 'signal')。
5. 依赖项：仅使用必要的 pandas, numpy, talib。

BaseStrategy 基类定义参考:
```python
class BaseStrategy:
    def __init__(self, params):
        self.params = params
```

请只返回 Python 代码，使用 ```python 代码块包裹。代码应当直接可运行，逻辑清晰，无废话。
"""


@lru_cache
def get_config() -> AIStrategyConfig:
    """获取配置单例并验证必需配置"""
    config = AIStrategyConfig()
    validate_required_config(config)
    return config


@lru_cache
def get_prompts() -> PromptTemplates:
    """获取Prompt模板"""
    return PromptTemplates()


def validate_required_config(config: AIStrategyConfig) -> None:
    """验证必需的配置项是否存在

    Args:
        config: 配置对象

    Raises:
        ValueError: 当必需配置缺失时
    """
    required_configs = {
        "LLM_API_KEY": config.LLM_API_KEY,
        "DATABASE_URL": config.DATABASE_URL,
    }

    missing = [key for key, value in required_configs.items() if not value or not value.strip()]

    if missing:
        error_msg = f"缺少必需的配置项: {', '.join(missing)}. 请检查环境变量或.env文件。"
        raise ValueError(error_msg)

    # 验证数据库URL格式
    if config.DATABASE_URL and "password" in config.DATABASE_URL.lower():
        # 检查是否包含默认密码（不安全）
        if "postgres:password@" in config.DATABASE_URL.lower():
            raise ValueError("检测到默认数据库密码，请修改为实际密码")

    # 验证LLM提供商
    if config.LLM_PROVIDER not in ["qwen", "deepseek"]:
        raise ValueError(f"不支持的LLM提供商: {config.LLM_PROVIDER}. 支持: qwen, deepseek")
