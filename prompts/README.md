# QuantMind 提示词库（技能中心数据源）

本目录存放面向 QuantBot（dsh / DeepSeek Harness）的**用户可复制提示词**，每个文件对应一个技能。前端「技能中心」页面的提示词卡片即来自本目录（构建时打包，无需后端接口）。

## 文件格式

```markdown
---
name: <技能名，与 skills/ 目录同名>
title: <卡片标题>
category: <分类：平台运营/研究分析/策略·因子·模型·回测/交易/券商 SDK>
description: <一句话功能描述>
outputs: <产出物与落盘目录>
---

（正文 = 可直接复制给 QuantBot 的提示词，{占位符} 由用户替换）
```

## 与 skills/ 的关系

- `skills/<name>/SKILL.md` — 技能本体：QuantBot 执行时读取的**完整操作细节**（端点、脚本、参数、铁律）；
- `prompts/<name>.md` — 用户入口：**一段短提示词**，指示 QuantBot 读取对应 SKILL.md 并按流程执行。

两者同名对应。新增技能时：先写 `skills/<name>/SKILL.md`（含运行环境契约），再补一个 `prompts/<name>.md`，并更新下方索引。可用 `python scripts/gen_prompts.py` 重新生成全部提示词。

## 提示词索引

### 环境初始化

| 提示词 | 用途 |
|--------|------|
| [quantbot-init](quantbot-init.md) | 首次使用：检查技能池/人格/环境连通性并补齐 |

### 平台运营

| 提示词 | 用途 |
|--------|------|
| [quantmind-operations](quantmind-operations.md) | 模型训练、数据更新、RSS 新闻对接 |
| [quantmind-deploy](quantmind-deploy.md) | 部署、问题排查、服务健康检查 |
| [quantdb-sdk](quantdb-sdk.md) | QuantDB 数据集与 K线/财务/因子查询 |
| [quantdb-fields](quantdb-fields.md) | 字段单位与口径速查 |
| [quantdb-data-structure](quantdb-data-structure.md) | QuantDB 目录结构、分区、parquet 路径、quantdb vs quantcustom |
| [news-sentiment-finbert](news-sentiment-finbert.md) | FinBERT 情绪管线安装与运维 |

### 研究分析

| 提示词 | 用途 |
|--------|------|
| [daily-review](daily-review.md) | A股每日复盘（专业版） |
| [market-analysis](market-analysis.md) | 市场分析报告（大盘快照版） |
| [stock-market-analysis](stock-market-analysis.md) | 个股/全市场深度分析与 CSV 导出 |
| [stock-picks](stock-picks.md) | 每日股票推荐（多维打分） |
| [stock-research](stock-research.md) | 个股深度研究（多 Agent） |
| [trading-agents](trading-agents.md) | 个股投研分析（智能体自主版） |
| [smart-strategy-stock-picking](smart-strategy-stock-picking.md) | 条件选股 |
| [batch-inference-analysis](batch-inference-analysis.md) | 批量推理信号分析 |
| [news-sentiment-research](news-sentiment-research.md) | 新闻情绪研究方法论 |

### 策略 · 因子 · 模型 · 回测

| 提示词 | 用途 |
|--------|------|
| [ai-ide-strategy-writing](ai-ide-strategy-writing.md) | AI 写量化策略 |
| [backtest-center](backtest-center.md) | Qlib 回测中心 |
| [rd-agent-factor-mining](rd-agent-factor-mining.md) | 因子挖掘（RD-Agent） |
| [model-train-infer-backtest-report](model-train-infer-backtest-report.md) | 训练-推理-回测-报告全流程 |

### 交易

| 提示词 | 用途 |
|--------|------|
| [simulation-trading](simulation-trading.md) | 模拟交易下单与查询 |
| [tdx-live-trading](tdx-live-trading.md) | TDX 实盘监控与交易 |
| [ibkr-cli](ibkr-cli.md) | IBKR 盈透证券操作 |

> 富途 / 老虎等券商 OpenAPI 技能仍保留在 `skills/` 供 QuantBot 按需读取，**不在技能中心展示**。
