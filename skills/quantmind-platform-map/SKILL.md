---
name: quantmind-platform-map
description: "QuantMind 平台地图与角色指南 — 新用户问「这是哪儿 / 我该用哪个模块 / 我是什么角色 / 下一步做什么」时先读本技能。覆盖 12 个一级栏目（市场分析、RSS信息流、回测中心、模拟交易、实盘交易、模型训练、模型管理、推理中心、投研平台、因子挖掘、因子研究、QuantBot）的菜单路径、Web 路由、页面职责、上下游关系与对应技能，并给出研究者 / 模拟交易者 / 实盘交易者 / 管理员四种角色的使用链路。触发词：平台地图、栏目、菜单、路径、入口、在哪、怎么用、我该做什么、角色、新手、新手指引、市场分析、RSS信息流、回测中心、模拟交易、实盘交易、模型训练、模型管理、推理中心、投研平台、因子挖掘、因子研究、QuantBot"
---

# QuantMind 平台地图与角色指南

> 这是给 **QuantBot / AI 助手**看的平台导航。用户问「XX 在哪」「我该用哪个」「我是谁」时：
> **先按本表给出准确路径，再按角色给下一步，最后才决定要不要执行具体技能。**
> 不要凭空编造菜单或接口；本表与仓库路由一一对应（`electron/src/App.tsx`）。

## 一、先判断用户是谁（四种角色）

| 角色 | 典型问法 | 主链路 |
|---|---|---|
| **平台新用户 / 研究者** | 「这个平台能干什么」「我该从哪开始」 | 数据 → 市场分析 → 因子挖掘 → 因子研究 → 模型训练 → 推理中心 → 回测中心 |
| **策略 / 因子研究员** | 「挖因子」「训模型」「回测一下」「补推理」 | 因子挖掘 → 因子研究 → 模型训练 → 推理中心 → 回测中心 |
| **模拟交易者** | 「模拟买一手」「看持仓」「今天收益」 | 回测中心 → 模拟交易（默认） |
| **实盘交易者** | 「实盘下单」「连券商」 | 模拟交易 → 实盘交易（**默认关闭，需双开关**） |
| **管理员** | 「加用户」「同步数据」「看系统状态」 | 后台管理 → 数据管理 / 用户 / 模型 / 策略 |

> 研究口径统一：展示面只说**位置**（靠前/靠后/居中、0–100 截面分位），
> **买入/卖出**只出现在执行面（下单、委托、成交）。不要把研究评分读成买卖指令。

## 二、一级栏目路径表（底部导航 ⇄ Web 路由）

Web 端是 **HashRouter**：`http://<服务器IP>:3080/#/路由`；Electron 桌面端用底部导航点选。

| 一级栏目 | 底部导航 | Web 路由 | 页面 / 职责 | 先读技能 |
|---|---|---|---|---|
| **市场分析** | 市场分析 | `#/market-analysis`（跟随右上角市场切换器） | 大盘指数、广度与情绪、行业/概念热力图、板块资金流、个股主力资金；可生成市场分析报告 | [[market-analysis]]、[[daily-review]] |
| **RSS信息流** | RSS信息流 | `#/rss-news` | Huntly + RSSHub 聚合的 7x24 财经快讯、事件标签、利好/利空情绪、实体匹配 | [[news-sentiment-finbert]]、[[news-sentiment-research]] |
| **回测中心** | 回测中心 | 无独立路由（主控制台 `#/` 内切换 tab） | Qlib 快速/专家回测、策略对比、参数优化、回测历史、高级分析；另含**行情回测**（A股/港股/美股/期货/加密，日线/5分钟/1分钟，单标的与自定义/指数池批量，内置模板 + Pine 策略库） | [[backtest-center]] |
| **模拟交易** | 模拟交易 | `#/trading` | 模拟盘下单、持仓、委托/成交、资金曲线、T+1 撮合与风控；实盘栏目存在时本栏目固定为模拟盘 | [[simulation-trading]] |
| **实盘交易** | 实盘交易（默认不出现） | `#/live`（深链会落到说明页） | QMT / 通达信 / IBKR 等真实通道；**开源版默认关闭**：后端 `ENABLE_REAL_TRADING` + 前端 `VITE_ENABLE_REAL_TRADING` 都开才显示 | [[qmt-bridge-ops]]、[[tdx-live-trading]]、[[ibkr-cli]] |
| **模型训练** | 模型训练 | `#/model-training` | 13 种模型（LightGBM/XGBoost/CatBoost/GRU/LSTM/ALSTM/Transformer/TabNet/TCN/NativeTFT 等）、Target Contract 目标注册表与能力硬闸、Optuna 调参、WFA、本地 CPU/GPU 与 AutoDL 远程节点 | [[model-train-infer-backtest-report]]、[[factor-train-pipeline]] |
| **模型管理** | 模型管理 | `#/model-registry` | 模型台账、默认模型、A/B 对比、SHAP、漂移 PSI、滚动训练/月度重训、归档与恢复、推理覆盖；模型 metadata 带 target_id/version/hash | [[model-inference-backfill]]、[[batch-inference-analysis]] |
| **推理中心** | 推理中心 | `#/inference-center` | 全市场批量截面打分、Top-N 信号、组合选股（同市场同周期模型截面加权重合）、推理补推、模型滚动；横评按 target_hash 分组 | [[batch-inference-analysis]]、[[model-inference-backfill]]、[[model-portfolio-sim]] |
| **投研平台** | 投研平台 | `#/research` | 个股/行业深度投研、研报档案、每日复盘、候选池与研究工作区 | [[stock-research]]、[[daily-review]]、[[stock-picks]] |
| **因子挖掘** | 因子挖掘 | `#/alpha-research` | RD-Agent / LLM 因子演化、因子工厂、通达信指标量化、候选因子表达式与回测优选 | [[rd-agent-factor-mining]]、[[factor-materialize-catalog]] |
| **因子研究** | 因子研究 | `#/factor-research` | 因子 IC/Fitness 排行、正交化、分层、回测偏差审计（PBO/DSR/前视/成本）、物化与跨库训练载荷 | [[factor-materialize-catalog]]、[[factor-ic-decay]]、[[factor-orthogonalization]]、[[factor-quality-audit]]、[[backtesting-bias-avoidance]] |
| **QuantBot** | QuantBot | `#/quantbot`（独立控制台 `:8088`） | 量化大脑的自然语言总入口（dsh / DeepSeek Harness）：对话式取数、选股、回测、训练、推理、研报、模拟交易与运维 | 本技能 + `/root/.dsh/AGENTS.md` 路由表 |

### 辅助入口（用户常问，但不在 12 个一级栏目里）

| 入口 | 路由 | 用途 |
|---|---|---|
| 市场看板 | `#/` | 市场监控、自选股、资产看板、持仓预警（默认首页） |
| 个股终端 | `#/stock-terminal` | K线/财务/估值/筹码/融资/L2/模型分数一站式个股页 |
| AI-IDE | `#/ai-ide` | 写 Qlib 策略、AI Copilot、一键回测、策略落库（原 Strategy Lab） |
| 策略对比 | `#/strategy-comparison` | 多策略/多模型回测结果对比 |
| 今日交易台 | `#/desk` | 当日信号、模型观点、决策轮与执行台 |
| 用户中心 | `#/user-center` | 个人中心 → 数据平台（QuantDB API Key、市场数据同步） |
| 后台管理 | `#/admin` | 数据管理（QuantDB/QuantUS/QuantHK/QuantBC/QuantFutures）、用户、模型、策略模板、系统状态 |

## 三、第一次使用：标准链路（给新用户）

```text
① 数据就绪：个人中心 → 数据平台（QuantDB API Key）
             或 后台管理 → 数据管理 → 对应市场「初始化/同步」
② 看市场：市场分析（指数/广度/行业/资金流）
③ 找因子：因子挖掘（RD-Agent / 因子工厂）→ 因子研究（IC/Fitness/正交化）
④ 训模型：模型训练（选物化因子库 / 直接 QuantDB 数据集）→ 模型管理（默认模型）
⑤ 出信号：推理中心（批量推理 / 组合选股 / 补推到最新交易日）
⑥ 验策略：回测中心（快速回测 / 策略对比 / 行情回测）
⑦ 练交易：模拟交易（默认）；确需实盘再按双开关启用实盘交易
```

> 数据在哪、每个市场目录/脚本/代码格式 → 见 [[quantmind-market-data]]。

## 四、按问题路由（最常见的新手问题）

| 用户问 | 回答要点 |
|---|---|
| 「我该从哪开始？」 | 先确认数据是否就绪（数据平台/后台数据管理），再按第三节链路；不要一上来就训模型 |
| 「XX 在哪？」 | 直接查第二节表：菜单名 + `#/路由`；回测中心无独立路由，说明从底部导航进 |
| 「我是研究者还是交易者？」 | 没有下单需求 = 研究者/策略角色；要下单 = 模拟交易者；要真实资金 = 实盘（默认关） |
| 「怎么训自己的因子？」 | 因子挖掘 → 物化（[[factor-materialize-catalog]]）→ 模型训练选「直读因子」数据集 |
| 「怎么回测？」 | 回测中心（[[backtest-center]]）；组合/模型回测与行情回测是两条入口 |
| 「怎么模拟买？」 | 模拟交易 `#/trading`（[[simulation-trading]]）；先确认账户与资金是模拟盘 |
| 「实盘怎么开？」 | 默认关闭；需同时开 `ENABLE_REAL_TRADING` 与 `VITE_ENABLE_REAL_TRADING`，并自行履行程序化交易报告义务；不要替用户直接开启 |
| 「QuantBot 是什么？」 | 平台自然语言总入口（dsh），可以查数据/跑回测/训模型/出报告；具体技能见 `skills/README.md` |
| 「数据在哪 / 为什么查不到？」 | 见 [[quantmind-market-data]]；先查代码格式（A股 `600519.SH`、港股 `0700.HK`、美股 ticker）与 dt 分区 |

## 五、安全与合规边界

- **实盘默认关闭**：`ENABLE_REAL_TRADING`（后端按端点拦）+ `VITE_ENABLE_REAL_TRADING`（前端构建期）。任一为 false 都不渲染/403（`detail=real_trading_disabled`）。
- **展示面/执行面用词分离**：研究评分卡、信号下钻、K线标记、筛选用「靠前/靠后/居中」；下单、委托、成交回报保留「买入/卖出」。
- **不构成投资建议**：任何报告/信号都要附免责；不承诺收益，不预测「明天必涨」。
- **危险操作先说明影响**：真实下单、删数据、重启服务、改 `.env`、开实盘开关，必须先向用户确认。

## 六、相关技能

- 数据侧：[[quantdb-data-structure]]、[[quantdb-fields]]、[[quantdb-sdk]]、[[quantmind-market-data]]
- 研究侧：[[market-analysis]]、[[daily-review]]、[[stock-research]]、[[trading-agents]]、[[stock-picks]]
- 因子/模型：[[rd-agent-factor-mining]]、[[factor-materialize-catalog]]、[[model-train-infer-backtest-report]]、[[model-inference-backfill]]
- 交易侧：[[simulation-trading]]、[[tdx-live-trading]]、[[qmt-bridge-ops]]、[[ibkr-cli]]
- 运维：[[quantmind-deploy]]、[[quantmind-operations]]、[[portable-pack-update]]
