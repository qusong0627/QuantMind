# QuantMind Qlib 策略开发规范 (V2)

本规范面向当前的 QuantMind 云端回测中心与 AI-IDE 专家模式。  
V2 的目标不是要求开发者手写一套“固定模板”，而是明确当前系统真实生效的契约：

- 前端可以补参，但**显式传入的参数优先**
- 后端会做**参数补全、路径修复与兼容适配**
- `STRATEGY_CONFIG` / `get_strategy_config()` 仍是主入口
- 专家模式不再依赖固定函数名或固定 `_rule_based_policy` 结构
- 当前系统是**云端执行 + 自动适配**，不是旧版“本地半手工专家模式”

---

## 1. 适用范围

V2 适用于以下入口：

- 回测中心「快速回测」
- 回测中心「专家模式」
- 云端 AI-IDE 中的策略生成、修改与保存
- 通过后端 `/api/v1/qlib/backtest`、`/api/v1/qlib/optimization` 直接发起的回测任务

---

## 2. 核心策略契约

### 2.1 推荐入口

当前系统支持以下三种策略入口，优先级从高到低：

1. `STRATEGY_CONFIG` 字典
2. `get_strategy_config()` 函数
3. `get_strategy_instance()` 对象返回

其中 `STRATEGY_CONFIG` / `get_strategy_config()` 是专家模式最稳定的入口。

### 2.2 推荐结构

```python
def get_strategy_config():
    return {
        "class": "RedisTopkStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
        "kwargs": {
            "signal": "<PRED>",
            "topk": 50,
            "n_drop": 5,
            "rebalance_days": 3,
            "account_stop_loss": 0.1,
            "max_leverage": 1.0,
            "only_tradable": True,
        }
    }

STRATEGY_CONFIG = get_strategy_config()
```

### 2.3 允许的策略形态

V2 推荐优先使用以下策略类：

- `RedisTopkStrategy`
- `RedisRecordingStrategy`
- `RedisWeightStrategy`
- `RedisLongShortTopkStrategy`
- `RedisStopLossStrategy`
- `RedisVolatilityWeightedStrategy`
- `RedisFullAlphaStrategy`

对于自定义类，系统会通过 `StrategyBuilder`、`StrategyFormatterService` 和 `StrategyAdapter` 做自动适配。

---

## 3. 参数补全与优先级

### 3.1 参数优先级

最终生效的参数优先级如下：

1. 前端或 API 显式传入的参数
2. 后端 Schema 默认值
3. Builder / Adapter 的补全逻辑
4. 策略类自身默认值

### 3.2 当前关键默认值

V2 必须明确以下口径：

- `topk` 默认值：`50`
- `n_drop` 默认值：`5`
- `rebalance_days` 默认值：`3`
- `benchmark` 默认值：`SH000300`
- `deal_price` 后端默认值：`close`
- `initial_capital`
  - 回测中心专家模式 UI 默认：`1000000`
  - 后端回测 Schema 默认：`100000000`
  - 直接调用后端接口时，应显式传入，避免口径歧义

### 3.3 自动补全规则

后端会自动处理以下内容：

- `topk` 缺失时补默认值
- `n_drop = 0` 时按“全量调仓”处理
- `rebalance_days` 会被强制透传到支持的策略
- `riskmodel_root`、`pred_path`、`model_path` 等路径会自动解析
- 对 `WeightStrategy` 类策略会自动补 `min_score`、`max_weight`
- 对 `CustomStrategy` 会根据构造函数签名补齐必要参数

这意味着：**专家模式不是纯手写契约，而是“前端显式参数 + 后端自动修复”的组合模型**。

---

## 4. 代码结构规范

### 4.1 `__init__` 约定

如果重写 `__init__`，必须先 `pop` 掉自定义参数，再调用 `super().__init__(**kwargs)`。

```python
class MyCustomStrategy(RedisRecordingStrategy):
    def __init__(self, **kwargs):
        self.my_param = kwargs.pop("my_param", 1.0)
        super().__init__(**kwargs)
```

### 4.2 `reset` 兼容约定

Qlib 不同版本可能传入不同的 `reset` 参数。若重写 `reset`，必须兼容可变参数：

```python
def reset(self, *args, **kwargs):
    try:
        return super().reset(*args, **kwargs)
    except TypeError:
        filtered = dict(kwargs)
        filtered.pop("level_infra", None)
        filtered.pop("common_infra", None)
        filtered.pop("trade_exchange", None)
        try:
            return super().reset(*args, **filtered)
        except TypeError:
            return super().reset()
```

### 4.3 策略入口不再要求固定函数名

V1 时代常见的“必须修改 `_rule_based_policy`”并不是当前系统的强制要求。  
在 V2 中，只要你的策略能够被构造成合法的 Qlib 策略实例，后端就会尝试解析和修复。

---

## 5. 信号与数据

### 5.1 信号来源

- `"<PRED>"`：使用平台默认生产模型预测结果
- `.pkl` 文件：通过 `SimpleSignal` 包装后使用

示例：

```python
"signal": {
    "class": "SimpleSignal",
    "module_path": "backend.services.engine.qlib_app.utils.simple_signal",
    "kwargs": {
        "pred_path": "models/my_model/pred.pkl",
        "universe": "csi300"
    }
}
```

### 5.2 动态行情

需要实时行情时，使用 `D.features`，但要避免在循环中频繁调用。

---

## 6. 交易与回测口径

### 6.1 成交价

- 后端默认 `deal_price = close`
- 若要尽量降低前视偏差，建议在回测配置中显式指定 `open`

### 6.2 初始资金

- 回测中心专家模式 UI 默认值：`1000000`
- 后端 Schema 默认值：`100000000`
- 若你要保证不同入口一致，必须显式传入 `initial_capital`

### 6.3 基准指数

默认基准为 `SH000300`。  
若策略面向其他市场，应显式调整 `benchmark_symbol`。

---

## 7. 安全与隔离

### 7.1 禁用项

后端会对自定义代码做静态检查，严禁使用：

- `os`, `sys`, `subprocess`, `shutil`
- `requests`, `urllib`, `socket`
- `__subclasses__`, `__globals__`, `__builtins__`
- 非 `/tmp` 目录的任意写文件行为

### 7.2 多租户要求

所有回测与策略保存请求必须携带：

- `user_id`
- `tenant_id`

严禁跨租户读取、保存或复用策略结果。

---

## 8. 前端专家模式口径

V2 时代的前端专家模式应遵循以下交互：

- 选中代码后，**先填入输入框**，用户再补充需求后发送
- 右侧 AI 助手遵循固定开发规范，不直接吞掉用户的上下文
- 专家模式更偏向“代码编辑 + 参数调整 + 云端执行”，而不是旧版本地脚本式操作

---

## 9. 推荐模板

```python
from backend.services.engine.qlib_app.utils.extended_strategies import RedisTopkStrategy

def get_strategy_config():
    return {
        "class": "RedisTopkStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
        "kwargs": {
            "signal": "<PRED>",
            "topk": 50,
            "n_drop": 5,
            "rebalance_days": 3,
            "max_leverage": 1.0,
            "account_stop_loss": 0.1,
            "only_tradable": True,
        }
    }

STRATEGY_CONFIG = get_strategy_config()
```

---

## 10. 常见问题

- `ModuleNotFoundError`
  - 检查 `module_path` 是否指向 `backend.services.engine.qlib_app...`

- `TypeError: __init__() got an unexpected keyword argument`
  - 检查是否已经 `pop` 掉自定义参数

- `TypeError: reset() got an unexpected keyword argument 'level_infra'`
  - 检查 `reset(*args, **kwargs)` 的回退逻辑

- `Empty module name`
  - 不要把 `module_path` 传成 `None` 或空字符串

- 指标显示异常
  - 优先检查结果是否来自有效的后端摘要，而不是前端临时拼接值

---

*QuantMind 研发团队 2026-04-10*
