# Qlib策略代码生成器

## 功能概述

自动生成符合Qlib框架规范的量化交易策略代码。

## 核心特性

✅ **智能解析**: 理解自然语言需求，提取策略关键要素
✅ **多种策略类型**: 支持TopK、权重分配、自定义策略
✅ **完整代码生成**: 生成可直接运行的Python代码
✅ **自动验证**: 语法检查、安全检查、最佳实践建议
✅ **文档齐全**: 自动生成策略文档和使用说明

## 快速开始

```python
from generators.qlib_strategy_generator import QlibStrategyCodeGenerator
from providers.gemini_provider import GeminiProvider

# 1. 初始化
llm_client = GeminiProvider()
generator = QlibStrategyCodeGenerator(llm_client)

# 2. 生成策略
result = await generator.generate_strategy(
    user_input="开发一个双均线策略，选择30只股票",
    strategy_type="auto"
)

# 3. 获取生成的代码
strategy_code = result['code']
strategy_config = result['config']
```

## 支持的策略类型

### 1. TopK Dropout Strategy
最常用的选股策略，选择信号最强的TopK只股票，定期换仓。

**适用场景**:
- 因子选股策略
- 动量策略
- 价值投资策略

**示例**:
```python
user_input = """
开发一个双均线策略：
- 选择MA5上穿MA20的股票
- 持仓30只
- 每周换仓5只
"""
```

### 2. Weight-Based Strategy
基于权重分配资金的策略，精确控制每只股票的仓位。

**适用场景**:
- 风险平价策略
- 最小方差策略
- 因子加权策略

**示例**:
```python
user_input = """
开发一个基于风险的权重分配策略：
- 根据波动率倒数分配权重
- 低波动股票配置更高权重
"""
```

### 3. Custom Strategy
完全自定义的策略逻辑，适合复杂策略。

**适用场景**:
- 多因子复合策略
- 条件触发策略
- 复杂的仓位管理

**示例**:
```python
user_input = """
开发一个MACD策略：
- MACD金叉买入
- MACD死叉卖出
- 根据MACD强度动态调整仓位
"""
```

## 生成结果结构

```python
result = {
    "code": "完整的Python策略代码",
    "config": {
        "class": "StrategyClassName",
        "kwargs": {"topk": 30, ...}
    },
    "documentation": "Markdown格式的策略文档",
    "validation": {
        "valid": True,
        "errors": [],
        "warnings": [],
        "suggestions": []
    },
    "metadata": {
        "strategy_name": "DoubleMAStrategy",
        "strategy_type": "topk_dropout",
        "generated_at": "2026-01-12T16:50:00Z",
        "llm_model": "gemini-2.0-flash"
    }
}
```

## 生成的代码示例

```python
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy

class DoubleMAStrategy(TopkDropoutStrategy):
    """
    双均线选股策略

    参数说明:
    - topk: 持仓股票数量 (默认: 30)
    - n_drop: 每次换仓数量 (默认: 5)
    """

    def __init__(self, topk=30, n_drop=5, **kwargs):
        kwargs.setdefault('topk', topk)
        kwargs.setdefault('n_drop', n_drop)
        super().__init__(**kwargs)

        self.topk = topk
        self.n_drop = n_drop

    def generate_trade_decision(self, execute_result=None):
        # 使用父类的TopK逻辑
        return super().generate_trade_decision(execute_result)
```

## 使用生成的策略

### 1. 保存策略代码

```python
# 保存到文件
with open('generated_strategies/DoubleMAStrategy.py', 'w') as f:
    f.write(result['code'])
```

### 2. 在Qlib中运行

```python
from qlib.backtest import backtest
from generated_strategies import DoubleMAStrategy

# 配置策略
strategy = {
    "class": "DoubleMAStrategy",
    "module_path": "generated_strategies",
    "kwargs": {
        "signal": <your_signal>,
        "topk": 30,
        "n_drop": 5
    }
}

# 运行回测
portfolio, indicators = backtest(
    strategy=strategy,
    executor={...},
    start_time="2023-01-01",
    end_time="2024-12-31"
)
```

## 最佳实践

### 1. 需求描述要清晰

✅ **好的描述**:
```
开发一个双均线策略：
- 使用5日和20日均线
- 当短期均线上穿长期均线时买入
- 选择信号最强的30只股票
- 每周换仓，更换5只表现最差的股票
```

❌ **不好的描述**:
```
做一个均线策略
```

### 2. 生成后要验证

```python
# 检查验证结果
if not result['validation']['valid']:
    print("代码有错误:", result['validation']['errors'])
else:
    print("代码验证通过!")
```

### 3. 审查生成的代码

- 检查策略逻辑是否符合预期
- 确认参数设置合理
- 测试边界情况

### 4. 回测验证

生成策略后，必须通过Qlib回测验证性能。

## 进阶功能

### 自定义LLM Prompt

```python
generator.templates.custom_prompt = """
你是Qlib策略专家...
<自定义指令>
"""
```

### 批量生成策略

```python
user_inputs = [
    "双均线策略",
    "MACD策略",
    "RSI策略"
]

for user_input in user_inputs:
    result = await generator.generate_strategy(user_input)
    # 保存策略...
```

## 故障排除

### 问题1: LLM返回格式错误

**解决**:
```python
# 使用更明确的提示词
result = await generator.generate_strategy(
    user_input=user_input,
    strategy_type="topk_dropout"  # 明确指定类型
)
```

### 问题2: 代码验证失败

**解决**:
- 检查 `result['validation']['errors']`
- 手动修复语法错误
- 重新生成

### 问题3: 生成的策略不符合预期

**解决**:
- 更详细地描述需求
- 提供具体参数
- 指定明确的策略类型

## API参考

### QlibStrategyCodeGenerator

```python
class QlibStrategyCodeGenerator:
    def __init__(self, llm_client):
        """初始化生成器"""

    async def generate_strategy(
        self,
        user_input: str,
        strategy_type: str = "auto"
    ) -> Dict[str, Any]:
        """生成策略"""
```

### 参数说明

- `user_input`: 自然语言策略需求描述
- `strategy_type`:
  - `"auto"`: 自动检测策略类型
  - `"topk_dropout"`: TopK选股策略
  - `"weight_based"`: 权重分配策略
  - `"custom"`: 自定义策略

### 返回值

返回包含以下键的字典:
- `code`: 策略Python代码
- `config`: Qlib配置字典
- `documentation`: 策略文档(Markdown)
- `validation`: 验证结果
- `metadata`: 元数据信息

## 集成到工作流

```
用户需求
  ↓
LLM生成策略代码
  ↓
代码验证
  ↓
Qlib回测
  ↓
RD-Agent优化
  ↓
部署到实盘
```

## 许可证

GNU Affero General Public License v3.0 (AGPL-3.0)

## 贡献

欢迎提交Issue和Pull Request!
