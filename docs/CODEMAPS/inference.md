# CODEMAP: inference（信号 / 推理）

> 用途：从「今天有没有信号」到「信号怎么生成」的定位地图。

## 职责
模型推理执行 → 信号方向判定（三道闸门）→ 落库 `engine_signal_scores` + 候选快照 + Redis 发布。

## 入口文件
| 文件 | 职责 |
|---|---|
| `services/engine/inference/script_runner.py` | **核心**：推理脚本执行、`_resolve_signal_sides`（BUY/SELL/HOLD 判定）、`_persist_and_publish` |
| `services/engine/inference/position_signal.py` | 仓位分（凯利）写 `quality.position` |
| `services/engine/services/fusion_config.py` | 融合配置（LGBM+TFT+风控三层） |
| `services/engine/routers/inference*.py` / `ai_ide/` | REST 触发入口 / AI-IDE 回测 |
| `services/engine/tasks/` + `qlib_app/tasks.py` | Celery 异步（beat 调度） |
| `services/engine/routers/selection.py` | 旧三层过滤选股 API（**P4 起由 Scanner 替代**） |
| `services/engine/inference/inference_backtest_service.py` | 选股核心逻辑 + 回测复用（`run_inference_backtest` 曾无调用方） |

## 对外契约
- 写 `engine_signal_scores`（`trade_date` = T+1 生效日；`signal_side`；`fusion_score`；`quality` JSONB）
- 写 `qm_research_candidate_snapshot`（用户候选池快照）
- Redis：`qm:inference:completed:{date}`（完成标记）、`qm:signal:latest:*`（db0 发布）

## 依赖
模型目录（`models/users/{tenant}/{user}/{model_id}/`，非 CN 多一层市场段）· 特征快照 parquet（滞后是推理断更头号根因）· QuantDB（回测/指数 MA20）

## 数据流
```
调度/Celery → script_runner 执行推理脚本 → pred（分数）
  → _resolve_signal_sides（百分位 + 归一化强度 + 共识 + 置信度 四闸门）
  → 落库 + 快照 + Redis 标记/发布 → 下游（模拟盘 signal_loader / 选股 / TDX 推送）
```

## 常见故障 top5
| 症状 | 先跑 | 根因 |
|---|---|---|
| 今日无信号 | health C01/C02 | 推理未跑 / 残 run / 就绪标记缺失 |
| 全 HOLD | 搜 `[RULE:SIGNAL-GATE]` 日志 | 闸门与量纲不匹配（2026-08 事故形态） |
| 分数全 0 / 空预测 | 容器日志 `neutralizer` | 元数据未加载（已 raise 守卫） |
| 推理断更 | 特征 parquet 时间戳 | 特征滞后（见 daily-review 记忆） |
| 信号与选股对不上 | `selection.py` 阈值 | 量纲硬编码（P4 分位化前已知） |

## 禁区
- 不许在推理层做「选股过滤」（那是策略层的事；信号 ≠ 选股，见主文档 §4.1 解耦）；
- 阈值一律分位口径（铁律三），禁止新增绝对分数硬编码；
- 改动 `_resolve_signal_sides` 必须跑 `scripts/backfill_signal_side.py --dry-run` 对照。