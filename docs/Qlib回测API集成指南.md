# Qlib 后端回测策略开发指南（兼容 QuantMind）

## 1. 文档目标

本文指导你开发并接入 **可在 QuantMind 后端直接运行** 的 Qlib 策略，覆盖：

- 请求协议与字段口径
- 可用策略类型与参数
- 自定义策略代码规范（`CustomStrategy`）
- 真实费用模型与成交口径
- 异步回测联调流程与排障

适用入口（统一经 API 网关转发）：

- `POST /api/v1/qlib/backtest`
- `GET /api/v1/qlib/backtest/{backtest_id}/status`
- `GET /api/v1/qlib/results/{backtest_id}`
- `GET /api/v1/qlib/health`

## 2. 回测请求协议

核心请求模型为 `QlibBacktestRequest`，关键字段如下。

### 2.1 必填字段

- `start_date`: `YYYY-MM-DD`
- `end_date`: `YYYY-MM-DD`

### 2.2 强烈建议显式传入字段

- `strategy_type`: 策略类型（见第 3 节）
- `strategy_params`: 策略参数对象
- `initial_capital`: 初始资金（默认值较大，建议业务侧显式传入）
- `benchmark`: 基准（默认 `SH000300`）
- `universe`: 股票池（默认 `csi300`）
- `user_id` / `tenant_id`: 多租户隔离字段（生产必须真实传入）

### 2.3 费用字段（真实费率口径）

后端默认按分项费用计算，字段如下：

- `commission`（默认 `0.00025`）
- `min_commission`（默认 `5.0`）
- `stamp_duty`（默认 `0.0005`，仅卖出）
- `transfer_fee`（默认 `0.00001`）
- `min_transfer_fee`（默认 `0.01`）
- `impact_cost_coefficient`（默认 `0.0005`）

兼容字段：

- `open_cost` -> `buy_cost`
- `close_cost` -> `sell_cost`

如果传 `open_cost/close_cost`，后端会映射为综合买卖费率逻辑；新接入建议统一使用分项费率字段。

## 3. 支持的策略类型（strategy_type）

当前工厂映射支持以下主要类型：

- 原生：`TopkDropout`、`WeightStrategy`、`CustomStrategy`
- 前端模板映射：`standard_topk`、`alpha_cross_section`、`deep_time_series`、`adaptive_drift`、`score_weighted`

说明：

- 未识别的 `strategy_type` 会自动降级到 `TopkDropout`，不会直接报错。
- 若你需要可控行为，务必在请求侧校验 `strategy_type` 合法性，避免“静默降级”。

## 4. 策略参数规范（strategy_params）

常用参数：

- `topk`: 选股数（5~200）
- `n_drop`: 每期替换数（0~20）
- `signal`: 信号来源，默认 `<PRED>`
- `min_score`: 权重策略最小分数阈值
- `max_weight`: 单标的最大权重（0~1）
- `lookback_days`: 回看窗口

兼容建议：

- `TopkDropout` / `standard_topk`: 至少传 `topk`、`n_drop`
- `WeightStrategy` / `score_weighted`: 至少传 `topk`、`min_score`、`max_weight`
- `deep_time_series`: `signal` 建议传 `.pkl` 预测文件路径

## 5. 信号机制（signal）与 `<PRED>` 语义

`strategy_params.signal` 支持三种模式：

1. `<PRED>`
- 后端尝试读取 `QLIB_PRED_PATH` 指向的预测文件（`pred.pkl`）。
- 文件不存在则降级为 `$close`。

2. 因子表达式或列名
- 如 `$close`、`close`（后者会自动补 `$`）。
- 后端会拉取 `universe` 对应行情并构造信号。

3. `.pkl` 文件路径
- 可传绝对路径或项目相对路径。
- 需满足 MultiIndex（`datetime`、`instrument`），且包含可映射到 `score` 的列。

## 6. 自定义策略开发（CustomStrategy）

当 `strategy_type = "CustomStrategy"` 时，策略代码放在 `strategy_content`。

### 6.1 返回值要求（重要）

当前实现要求最终返回 **dict 配置**（不是策略对象实例），可通过以下任一方式提供：

- `STRATEGY_CONFIG = {...}`
- `def get_strategy_config(): return {...}`

字典必须包含键：

- `class`
- `module_path`
- `kwargs`

### 6.2 最小可运行模板

```python
def get_strategy_config():
    return {
        "class": "RedisRecordingStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
        "kwargs": {
            "signal": "<PRED>",
            "topk": 50,
            "n_drop": 5
        }
    }
```

### 6.3 安全限制

`strategy_content` 会经过 AST 检查，禁止导入高风险模块（如 `os`、`subprocess`、`requests`、`socket` 等），禁止访问危险 dunder 属性。上线前请按白名单思维写策略逻辑。

## 7. 成交与费用口径

### 7.1 成交价基准

回测交易所使用自定义 `CnExchange`，当前 `exchange` 配置中：

- `deal_price = "close"`

即默认按日频收盘价成交（再叠加费用与冲击成本）。

### 7.2 费用计算

后端按真实分项费用计算：

- 买入：佣金 + 过户费 + 冲击成本
- 卖出：佣金 + 过户费 + 印花税 + 冲击成本

详见：`docs/回测费用配置说明.md`

### 7.3 交易明细字段

回测结果中的 `trades` 典型字段：

- `date`, `symbol`, `action`, `price`, `quantity`
- `commission`（手续费）
- `totalAmount`（成交金额）
- `cash_after`（成交后现金）
- `position_value_after`（成交后持仓市值）
- `equity_after`（成交后总权益）
- `balance`（兼容旧字段，等价总权益）

## 8. 异步回测联调流程

### 8.1 提交任务

`POST /api/v1/qlib/backtest?async_mode=true`

返回 `backtest_id`（和可能的 `task_id`）。

### 8.2 查询状态

`GET /api/v1/qlib/backtest/{backtest_id}/status?tenant_id=...`

### 8.3 拉取结果

`GET /api/v1/qlib/results/{backtest_id}?tenant_id=...`

说明：

- Celery 可用时走任务队列。
- Celery 不可用时，后端会回退到 `BackgroundTasks` 异步执行，接口仍可轮询状态。

## 9. 最小联调请求示例

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/qlib/backtest?async_mode=true' \
  -H 'Content-Type: application/json' \
  -d '{
    "strategy_type": "standard_topk",
    "strategy_params": {
      "topk": 50,
      "n_drop": 5,
      "signal": "<PRED>"
    },
    "start_date": "2024-01-01",
    "end_date": "2024-12-31",
    "initial_capital": 1000000,
    "benchmark": "SH000300",
    "universe": "csi300",
    "commission": 0.00025,
    "min_commission": 5.0,
    "stamp_duty": 0.0005,
    "transfer_fee": 0.00001,
    "min_transfer_fee": 0.01,
    "impact_cost_coefficient": 0.0005,
    "user_id": "u_demo",
    "tenant_id": "t_demo"
  }'
```

## 10. 常见问题与排查

1. `422 Request validation failed`
- 检查日期格式、数值边界、必填字段。
- 检查 `tenant_id` 是否传入（查询状态/结果接口通常要求）。

2. `ImportError` 或策略加载失败
- 检查 `module_path` 是否可被引擎进程导入。
- 避免在 `strategy_content` 中导入受限模块。

3. 回测卡在高进度（如 98%）
- 优先检查 Celery worker 与 Redis 连通性。
- 拉取 `/status`、`/results` 与后端日志确认是否进入结果落库阶段。

4. 回测结果与预期差异大
- 核对 `initial_capital`、费用参数、`deal_price` 口径（当前为 `close`）。
- 核对 `signal` 来源是否一致（`<PRED>` 可能降级到 `$close`）。

## 11. 开发建议（兼容性最佳实践）

1. 请求侧固定传 `strategy_type` 与完整 `strategy_params`，避免依赖后端默认值漂移。
2. 生产必传真实 `user_id/tenant_id`，并在日志中带上 `backtest_id` 做链路追踪。
3. 自定义策略先用最小模板跑通，再逐步增加参数与复杂逻辑。
4. 统一使用分项费率字段，避免新旧费率口径混用。

