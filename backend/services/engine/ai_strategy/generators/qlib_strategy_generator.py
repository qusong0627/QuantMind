"""
Qlib策略代码生成器
基于LLM生成符合Qlib框架规范的策略代码
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class QlibStrategyConfig(BaseModel):
    """Qlib策略配置"""

    strategy_name: str
    strategy_description: str
    strategy_type: str  # topk_dropout, weight_based, custom

    # 策略参数
    parameters: dict[str, Any]

    # 交易逻辑
    entry_logic: dict[str, Any]
    exit_logic: dict[str, Any]

    # 风控参数
    risk_degree: float = 0.95
    max_position_per_stock: float = 0.1

    # 生成信息
    llm_model: str
    user_input: str


class QlibStrategyCodeGenerator:
    """Qlib策略代码生成器"""

    def __init__(self, llm_client):
        """
        初始化生成器

        Args:
            llm_client: LLM客户端（支持Gemini/OpenAI/DeepSeek等）
        """
        self.llm_client = llm_client
        self.templates = QlibStrategyTemplates()

    async def generate_strategy(self, user_input: str, strategy_type: str = "auto") -> dict[str, Any]:
        """
        生成Qlib策略代码

        Args:
            user_input: 用户自然语言需求
            strategy_type: 策略类型（auto/topk_dropout/weight_based/custom）

        Returns:
            {
                "code": "策略Python代码",
                "config": "策略配置",
                "documentation": "策略文档",
                "validation": "验证结果"
            }
        """

        # 1. 解析需求
        requirement = await self._parse_requirement(user_input, strategy_type)

        # 2. 设计策略逻辑
        design = await self._design_strategy(requirement)

        # 3. 生成代码
        code = self._generate_code(design, requirement)

        # 4. 生成配置
        config = self._generate_config(design, requirement)

        # 5. 生成文档
        documentation = self._generate_documentation(design, requirement)

        # 6. 验证代码
        validation = self._validate_code(code)

        return {
            "code": code,
            "config": config,
            "documentation": documentation,
            "validation": validation,
            "metadata": {
                "strategy_name": design.get("strategy_name"),
                "strategy_type": requirement.get("strategy_type"),
                "generated_at": datetime.now().isoformat(),
                "llm_model": getattr(self.llm_client, "model_name", "unknown"),
            },
        }

    async def convert_strategy(
        self,
        source_code: str,
        source_platform: str,
        user_requirements: str | None = None,
    ) -> dict[str, Any]:
        """将第三方平台策略转换为Qlib格式"""
        prompt = f"""
你是一个量化策略转换专家。请将以下来自 {source_platform} 平台的 Python 策略代码转换为 Microsoft Qlib 框架格式。

**源代码:**
```python
{source_code}
```

**用户额外要求:**
{user_requirements or "无"}

**转换目标:**
1. 目标框架: Microsoft Qlib (使用 TopkDropoutStrategy 或 BaseSignalStrategy)
2. 提取原策略的核心信号计算逻辑，并将其转化为 Qlib 的信号表达式 (表达式语法需符合 Qlib 数据接口)
3. 保留原策略的风险控制逻辑 (止损、止盈、仓位限制)
4. 生成完整的、可执行的 Qlib 策略类代码

**输出格式要求:**
请以 JSON 格式返回（只返回 JSON），包含以下字段：
{{
    "success": true,
    "converted_code": "完整的 Qlib Python 代码",
    "conversion_notes": ["转换要点1", "转换要点2"],
    "warnings": ["警告1：某某逻辑在Qlib中实现有差异"],
    "suggestions": ["建议1：建议调整某某参数以适配Qlib执行器"],
    "estimated_compatibility": 85,
    "platform_differences": [
        {{
            "category": "API差异",
            "source_feature": "原平台特性描述",
            "target_equivalent": "Qlib对应实现描述",
            "notes": "详细说明",
            "manual_review_required": true
        }}
    ]
}}
"""

        response = await self.llm_client.chat(
            messages=[
                {"role": "system", "content": "你是Qlib量化专家，擅长多平台策略重构。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )

        # 提取并解析 JSON
        content = response.content if hasattr(response, "content") else str(response)
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = json.loads(content)

        return result

    async def _parse_requirement(self, user_input: str, strategy_type: str) -> dict[str, Any]:
        """解析用户需求"""

        prompt = f"""
你是Qlib量化策略专家。分析以下需求，提取策略关键信息。

用户需求: {user_input}

请分析并返回JSON格式（只返回JSON，不要其他文字）:
{{
    "strategy_type": "策略类型：topk_dropout（Top K选股换仓）/ weight_based（权重分配）/ custom（自定义逻辑）",
    "strategy_name": "策略名称（英文，如DoubleMAStrategy）",
    "strategy_description": "策略简短描述",

    "indicators": ["需要的技术指标，如MA5、MA20、MACD等"],
    "timeframe": "时间周期：daily/weekly/monthly",
    "universe": "股票池：csi300/csi500/all",

    "entry_conditions": [
        "入场条件描述1",
        "入场条件描述2"
    ],
    "exit_conditions": [
        "出场条件描述1",
        "出场条件描述2"
    ],

    "parameters": {{
        "topk": {{
            "value": 30,
            "description": "持仓股票数量",
            "range": [10, 100]
        }},
        "n_drop": {{
            "value": 5,
            "description": "每次换仓数量",
            "range": [1, 20]
        }}
    }},

    "risk_management": {{
        "risk_degree": 0.95,
        "max_position_per_stock": 0.1,
        "stop_loss": 0.05,
        "take_profit": 0.15
    }}
}}

注意:
1. 如果是简单的TopK选股策略，使用topk_dropout类型
2. 如果需要精确控制每只股票权重，使用weight_based类型
3. 如果有复杂的自定义逻辑，使用custom类型
4. strategy_name必须是有效的Python类名
"""

        response = await self.llm_client.chat(
            messages=[
                {
                    "role": "system",
                    "content": "你是Qlib量化策略专家，专门设计符合Qlib框架的策略。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )

        # 提取JSON
        content = response.content if hasattr(response, "content") else str(response)
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            requirement = json.loads(json_match.group())
        else:
            requirement = json.loads(content)

        # 自动判断策略类型
        if strategy_type == "auto":
            requirement["strategy_type"] = self._auto_detect_strategy_type(requirement)
        else:
            requirement["strategy_type"] = strategy_type

        requirement["llm_model"] = getattr(self.llm_client, "model_name", "unknown")

        return requirement

    def _auto_detect_strategy_type(self, requirement: dict[str, Any]) -> str:
        """自动检测策略类型"""

        params = requirement.get("parameters", {})
        desc = requirement.get("strategy_description", "").lower()
        weight_param_names = {"min_score", "max_weight", "weight", "weights", "allocation"}

        # 权重类参数优先于通用 topk 参数，避免 TopkWeight 被误判为 TopkDropout。
        if any(name in params for name in weight_param_names):
            return "weight_based"

        # 如果提到权重、配置、仓位分配，使用weight_based
        if any(keyword in desc for keyword in ["权重", "weight", "配置", "allocation"]):
            return "weight_based"

        # 如果有明确的 TopK / n_drop 参数，使用 topk_dropout
        if "n_drop" in params or "topk" in params:
            return "topk_dropout"

        # 默认使用topk_dropout（最常用）
        return "topk_dropout"

    async def _design_strategy(self, requirement: dict[str, Any]) -> dict[str, Any]:
        """设计策略逻辑"""

        strategy_type = requirement["strategy_type"]

        prompt = f"""
基于以下需求，设计Qlib策略的详细实现逻辑。

策略类型: {strategy_type}
策略名称: {requirement.get("strategy_name")}
策略描述: {requirement.get("strategy_description")}

需求详情:
{json.dumps(requirement, indent=2, ensure_ascii=False)}

请设计策略并返回JSON格式（只返回JSON）:
{{
    "strategy_name": "{requirement.get("strategy_name")}",
    "base_class": "策略基类（TopkDropoutStrategy/BaseSignalStrategy/WeightStrategyBase）",

    "signal_calculation": {{
        "method": "信号计算方法描述",
        "indicators_used": ["使用的指标"],
        "formula": "信号计算公式或逻辑"
    }},

    "position_management": {{
        "method": "仓位管理方法",
        "sizing_logic": "仓位计算逻辑",
        "max_position": 0.1
    }},

    "trade_logic": {{
        "buy_condition": "买入条件的详细逻辑",
        "sell_condition": "卖出条件的详细逻辑",
        "rebalance_frequency": "换仓频率"
    }},

    "risk_control": {{
        "stop_loss_logic": "止损逻辑",
        "position_limit": "持仓限制",
        "drawdown_control": "回撤控制"
    }},

    "implementation_details": [
        "实现要点1",
        "实现要点2"
    ]
}}

设计要求:
1. 必须符合Qlib框架规范
2. generate_trade_decision()方法是核心
3. 考虑交易成本和滑点
4. 包含完整的风险控制
"""

        response = await self.llm_client.chat(
            messages=[
                {"role": "system", "content": "你是Qlib策略架构设计专家。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )

        # 提取JSON
        content = response.content if hasattr(response, "content") else str(response)
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            design = json.loads(json_match.group())
        else:
            design = json.loads(content)

        return design

    def _generate_code(self, design: dict[str, Any], requirement: dict[str, Any]) -> str:
        """生成策略代码 (包含动态仓位逻辑)"""

        strategy_type = requirement["strategy_type"]

        # 检查是否启用了动态仓位
        is_dynamic = requirement.get("risk_management", {}).get("enable_dynamic_position", False)

        # 如果启用，在 Prompt 中增加指令，或者在代码拼接时注入
        if is_dynamic:
            design["dynamic_sizing_logic"] = (
                "接入系统提供的市场热度算子，根据 (近5日均量/近120日均量) 动态调整总目标仓位乘数。"
            )

        if strategy_type == "topk_dropout":
            return self.templates.generate_topk_strategy(design, requirement, is_dynamic=is_dynamic)
        elif strategy_type == "weight_based":
            return self.templates.generate_weight_strategy(design, requirement, is_dynamic=is_dynamic)
        else:  # custom
            return self.templates.generate_custom_strategy(design, requirement, is_dynamic=is_dynamic)

    def _generate_config(self, design: dict[str, Any], requirement: dict[str, Any]) -> dict[str, Any]:
        """生成Qlib配置"""

        strategy_name = requirement.get("strategy_name", "CustomStrategy")
        parameters = requirement.get("parameters", {})

        # 提取参数默认值
        param_defaults = {}
        for param_name, param_info in parameters.items():
            if isinstance(param_info, dict):
                param_defaults[param_name] = param_info.get("value")
            else:
                param_defaults[param_name] = param_info

        config = {
            "class": strategy_name,
            "module_path": "generated_strategies",  # 自定义模块路径
            "kwargs": {
                "signal": "<signal_placeholder>",  # 需要在运行时替换
                **param_defaults,
            },
        }

        return config

    def _generate_documentation(self, design: dict[str, Any], requirement: dict[str, Any]) -> str:
        """生成策略文档"""

        doc = f"""# {requirement.get("strategy_name")} 策略文档

## 策略概述

**策略类型**: {requirement.get("strategy_type")}
**策略描述**: {requirement.get("strategy_description")}

## 策略逻辑

### 信号计算
{design.get("signal_calculation", {}).get("method", "N/A")}

使用指标: {", ".join(design.get("signal_calculation", {}).get("indicators_used", []))}

### 交易逻辑

**买入条件**:
{design.get("trade_logic", {}).get("buy_condition", "N/A")}

**卖出条件**:
{design.get("trade_logic", {}).get("sell_condition", "N/A")}

**换仓频率**: {design.get("trade_logic", {}).get("rebalance_frequency", "N/A")}

## 策略参数

"""

        parameters = requirement.get("parameters", {})
        for param_name, param_info in parameters.items():
            if isinstance(param_info, dict):
                doc += f"- **{param_name}**: {param_info.get('description', 'N/A')}\n"
                doc += f"  - 默认值: {param_info.get('value')}\n"
                doc += f"  - 范围: {param_info.get('range', 'N/A')}\n\n"

        doc += f"""
## 风险控制

{json.dumps(requirement.get("risk_management", {}), indent=2, ensure_ascii=False)}

## 使用示例

```python
from qlib.backtest import backtest
from qlib.backtest.executor import SimulatorExecutor
from qlib.data import D
from generated_strategies import {requirement.get("strategy_name")}

# 创建策略实例
strategy = {{
    "class": "{requirement.get("strategy_name")}",
    "module_path": "generated_strategies",
    "kwargs": {{
        "signal": <your_signal>,
        {", ".join([f'"{k}": {v.get("value") if isinstance(v, dict) else v}' for k, v in parameters.items()])}
    }}
}}

# 运行回测
portfolio_dict, indicator_dict = backtest(
    strategy=strategy,
    executor={{...}},
    start_time="2023-01-01",
    end_time="2024-12-31"
)
```

## 注意事项

{chr(10).join(["- " + note for note in design.get("implementation_details", [])])}

---

**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
**生成模型**: {requirement.get("llm_model", "N/A")}
"""

        return doc

    def _validate_code(self, code: str) -> dict[str, Any]:
        """验证生成的代码"""

        validation = {"valid": True, "errors": [], "warnings": [], "suggestions": []}

        # 1. 语法检查
        try:
            compile(code, "<string>", "exec")
        except SyntaxError as e:
            validation["valid"] = False
            validation["errors"].append(f"语法错误: {e}")

        # 2. 必要导入检查
        required_imports = ["from qlib", "BaseSignalStrategy", "TradeDecisionWO"]

        for req_import in required_imports:
            if req_import not in code:
                validation["warnings"].append(f"缺少导入: {req_import}")

        # 3. 必要方法检查
        if "generate_trade_decision" not in code:
            validation["errors"].append("缺少核心方法: generate_trade_decision()")
            validation["valid"] = False

        # 4. 安全性检查
        dangerous_keywords = ["eval", "exec", "compile", "__import__", "os.system"]
        for keyword in dangerous_keywords:
            if keyword in code:
                validation["errors"].append(f"发现危险操作: {keyword}")
                validation["valid"] = False

        # 5. 最佳实践建议
        if "self.signal.get_signal" not in code:
            validation["suggestions"].append("建议使用 self.signal.get_signal() 获取信号")

        if "self.trade_calendar.get_trade_step" not in code:
            validation["suggestions"].append("建议使用 self.trade_calendar 获取交易时间")

        return validation


class QlibStrategyTemplates:
    """Qlib策略代码模板"""

    def generate_topk_strategy(
        self,
        design: dict[str, Any],
        requirement: dict[str, Any],
        is_dynamic: bool = False,
    ) -> str:
        """生成TopK Dropout策略"""

        strategy_name = requirement.get("strategy_name", "CustomTopkStrategy")
        strategy_desc = requirement.get("strategy_description", "")
        parameters = requirement.get("parameters", {})

        # 动态逻辑段
        dynamic_code_init = (
            "        self.dynamic_sizing = True" if is_dynamic else "        self.dynamic_sizing = False"
        )
        dynamic_code_method = ""
        if is_dynamic:
            dynamic_code_method = """
    def _get_market_heat_multiplier(self):
        \"\"\"从系统生成的历史热度特征文件中获取当前日期的乘数\"\"\"
        try:
            import os
            import pandas as pd
            # 路径约定：db/qlib_data/market_intelligence/global_heat.csv
            heat_file = os.path.join(os.getcwd(), "db/qlib_data/market_intelligence/global_heat.csv")
            if not os.path.exists(heat_file):
                return 1.0
            
            heat_df = pd.read_csv(heat_file)
            current_date = str(self.trade_calendar.get_trade_step().date())
            
            # 匹配当前日期
            match = heat_df[heat_df['trade_date'] == current_date]
            if not match.empty:
                return float(match.iloc[0]['market_heat'])
        except Exception:
            pass
        return 1.0

    def generate_trade_decision(self, execute_result=None):
        \"\"\"基于全市场热度动态调整仓位的核心决策逻辑\"\"\"
        # 1. 获取基础 TopK 信号决策
        decision = super().generate_trade_decision(execute_result)
        
        # 2. 获取动态热度乘数 (5d均量/120d均量)
        heat_multiplier = self._get_market_heat_multiplier()
        
        # 3. 按比例动态缩减下单金额 (实现择时风控)
        if heat_multiplier < 0.95: # 仅在市场走弱时介入缩减
            for order in decision.order_list:
                order.amount *= heat_multiplier
            
        return decision
"""

        # 提取参数
        topk = parameters.get("topk", {}).get("value", 30) if isinstance(parameters.get("topk"), dict) else 30
        n_drop = parameters.get("n_drop", {}).get("value", 5) if isinstance(parameters.get("n_drop"), dict) else 5

        # 生成参数文档
        self._generate_param_docs(parameters)

        # 生成初始化参数
        init_params = self._generate_init_params(parameters)

        code = f'''"""
自动生成的Qlib策略: {strategy_name}
... (省略部分 doc)
"""

from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
from qlib.backtest.decision import TradeDecisionWO, Order, OrderDir
import pandas as pd
import numpy as np


class {strategy_name}(TopkDropoutStrategy):
    """
    {strategy_desc}
    (动态仓位支持: {"已启用" if is_dynamic else "未启用"})
    """

    def __init__(
        self,
{init_params},
        **kwargs
    ):
        # 设置默认参数
        kwargs.setdefault('topk', {topk})
        kwargs.setdefault('n_drop', {n_drop})
        
        super().__init__(**kwargs)
{dynamic_code_init}
{self._generate_param_assignments(parameters)}
{dynamic_code_method}
'''
        return code

    def generate_weight_strategy(self, design: dict[str, Any], requirement: dict[str, Any]) -> str:
        """生成权重策略"""

        strategy_name = requirement.get("strategy_name", "CustomWeightStrategy")
        strategy_desc = requirement.get("strategy_description", "")
        parameters = requirement.get("parameters", {})

        param_docs = self._generate_param_docs(parameters)
        init_params = self._generate_init_params(parameters)

        code = f'''"""
自动生成的Qlib策略: {strategy_name}

策略描述: {strategy_desc}

生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""

from qlib.contrib.strategy.signal_strategy import WeightStrategyBase
from qlib.backtest.decision import TradeDecisionWO
from qlib.backtest.position import Position
import pandas as pd
import numpy as np


class {strategy_name}(WeightStrategyBase):
    """
    {strategy_desc}

    策略类型: Weight-Based Strategy

    参数说明:
{param_docs}
    """

    def __init__(
        self,
{init_params},
        **kwargs
    ):
        super().__init__(**kwargs)
{self._generate_param_assignments(parameters)}

    def generate_target_weight_position(
        self,
        score: pd.Series,
        current: Position,
        trade_start_time,
        trade_end_time
    ) -> pd.Series:
        """
        根据信号生成目标权重

        Args:
            score: 预测信号
            current: 当前持仓
            trade_start_time: 交易开始时间
            trade_end_time: 交易结束时间

        Returns:
            pd.Series: 目标权重 (stock_id -> weight)
        """
        # 1. 过滤信号
        valid_scores = score.dropna()

        if len(valid_scores) == 0:
            return pd.Series(dtype=float)

        # 2. 计算权重（示例：等权重）
        # 可以根据信号强度、风险等因素调整权重
        top_stocks = valid_scores.nlargest(getattr(self, 'topk', 30))

        # 等权重分配
        weights = pd.Series(1.0 / len(top_stocks), index=top_stocks.index)

        # 或者根据信号强度分配权重
        # weights = top_stocks / top_stocks.sum()

        return weights
'''

        return code

    def generate_custom_strategy(self, design: dict[str, Any], requirement: dict[str, Any]) -> str:
        """生成自定义策略"""

        strategy_name = requirement.get("strategy_name", "CustomStrategy")
        strategy_desc = requirement.get("strategy_description", "")
        parameters = requirement.get("parameters", {})

        param_docs = self._generate_param_docs(parameters)
        init_params = self._generate_init_params(parameters)

        code = f'''"""
自动生成的Qlib策略: {strategy_name}

策略描述: {strategy_desc}

生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""

from qlib.contrib.strategy.signal_strategy import BaseSignalStrategy
from qlib.backtest.decision import TradeDecisionWO, Order, OrderDir
from qlib.backtest.position import Position
import pandas as pd
import numpy as np


class {strategy_name}(BaseSignalStrategy):
    """
    {strategy_desc}

    策略类型: Custom Strategy

    参数说明:
{param_docs}
    """

    def __init__(
        self,
{init_params},
        **kwargs
    ):
        super().__init__(**kwargs)
{self._generate_param_assignments(parameters)}

    def generate_trade_decision(self, execute_result=None):
        """
        生成交易决策 - 核心方法

        Returns:
            TradeDecisionWO: 交易决策对象
        """
        # 1. 获取交易时间
        trade_step = self.trade_calendar.get_trade_step()
        trade_start_time, trade_end_time = self.trade_calendar.get_step_time(trade_step)
        pred_start_time, pred_end_time = self.trade_calendar.get_step_time(trade_step, shift=1)

        # 2. 获取预测信号
        pred_score = self.signal.get_signal(start_time=pred_start_time, end_time=pred_end_time)

        if pred_score is None:
            return TradeDecisionWO([], self)

        # 如果信号是DataFrame，取第一列
        if isinstance(pred_score, pd.DataFrame):
            pred_score = pred_score.iloc[:, 0]

        # 3. 计算交易信号
        signals = self.calculate_signals(pred_score)

        # 4. 生成订单
        order_list = self.generate_orders(signals)

        return TradeDecisionWO(order_list, self)

    def calculate_signals(self, pred_score: pd.Series) -> pd.Series:
        """
        计算交易信号

        Args:
            pred_score: 预测信号

        Returns:
            pd.Series: 交易信号 (1: 买入, -1: 卖出, 0: 持有)
        """
        # 示例逻辑：选择信号最强的前 topk 只股票
        signals = pd.Series(0, index=pred_score.index)

        topk = getattr(self, 'topk', 30)
        top_stocks = pred_score.nlargest(topk)
        signals.loc[top_stocks.index] = 1

        return signals

    def generate_orders(self, signals: pd.Series) -> list:
        """
        根据信号生成订单列表

        Args:
            signals: 交易信号

        Returns:
            list: 订单列表
        """
        order_list = []
        current_position = self.trade_position

        # 获取当前持仓股票
        current_stocks = set(current_position.get_stock_list())

        # 获取目标持仓股票
        target_stocks = set(signals[signals > 0].index)

        # 卖出不在目标中的股票
        stocks_to_sell = current_stocks - target_stocks
        for stock_id in stocks_to_sell:
            amount = current_position.get_stock_amount(stock_id)
            if amount > 0:
                order = Order(
                    stock_id=stock_id,
                    amount=amount,
                    direction=OrderDir.SELL,
                    factor=1.0
                )
                order_list.append(order)

        # 买入新股票（等权重）
        stocks_to_buy = target_stocks - current_stocks
        if len(stocks_to_buy) > 0:
            # 计算每只股票的目标金额
            risk_degree = self.get_risk_degree()
            total_value = current_position.get_cash() * risk_degree
            target_value_per_stock = total_value / len(target_stocks)

            for stock_id in stocks_to_buy:
                order = Order(
                    stock_id=stock_id,
                    amount=target_value_per_stock,
                    direction=OrderDir.BUY,
                    factor=1.0
                )
                order_list.append(order)

        return order_list
'''

        return code

    def _generate_param_docs(self, parameters: dict[str, Any]) -> str:
        """生成参数文档"""
        docs = []
        for param_name, param_info in parameters.items():
            if isinstance(param_info, dict):
                desc = param_info.get("description", "参数")
                value = param_info.get("value", "N/A")
                range_info = param_info.get("range", [])

                doc = f"    - {param_name}: {desc} (默认: {value}"
                if range_info:
                    doc += f", 范围: {range_info[0]}-{range_info[1]}"
                doc += ")"
                docs.append(doc)

        return "\n".join(docs) if docs else "    无特殊参数"

    def _generate_init_params(self, parameters: dict[str, Any]) -> str:
        """生成初始化参数"""
        params = []
        for param_name, param_info in parameters.items():
            if isinstance(param_info, dict):
                default_value = param_info.get("value")
                if isinstance(default_value, str):
                    params.append(f'        {param_name}: str = "{default_value}"')
                else:
                    params.append(f"        {param_name}: float = {default_value}")
            else:
                params.append(f"        {param_name} = {param_info}")

        return ",\n".join(params) if params else "        # 无额外参数"

    def _generate_init_docs(self, parameters: dict[str, Any]) -> str:
        """生成初始化方法文档"""
        docs = []
        for param_name, param_info in parameters.items():
            if isinstance(param_info, dict):
                desc = param_info.get("description", "参数")
                docs.append(f"            {param_name}: {desc}")

        return "\n".join(docs) if docs else "            # 无额外参数"

    def _generate_param_assignments(self, parameters: dict[str, Any]) -> str:
        """生成参数赋值代码"""
        assignments = []
        for param_name in parameters.keys():
            assignments.append(f"        self.{param_name} = {param_name}")

        return "\n".join(assignments) if assignments else "        pass"
