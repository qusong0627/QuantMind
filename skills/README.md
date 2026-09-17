# QuantMind Skills 索引

本项目所有 AI 编程工具技能（Skills）的统一目录。每个技能是一个独立文件夹，以 `SKILL.md` 为入口，通过自然语言触发词激活——AI 助手识别用户意图后自动调用对应技能完成操作。

目录结构与来源：

- `skills/<skill-name>/SKILL.md` — 全部技能的统一目录（本目录）
- 富途技能来源：https://openapi.futunn.com/skills/opend-skills.zip

## 安装方式

```bash
# Claude Code：一键安装全部技能到全局技能目录（~/.claude/skills，rsync 精确镜像）
bash scripts/install_skills.sh

# 或按需单个安装
cp -r skills/<skill-name> ~/.claude/skills/
```

QuantBot（dsh 容器 / DeepSeek Harness）**无需安装**：docker-compose 把本目录只读挂载为容器内 `/root/.dsh/skills`，技能随仓库更新即时生效。

其他工具（OpenCode / Codex / Trae / CodeBuddy 等）读取项目 `AGENTS.md`：把技能要点（部署检查表、API 端点清单等）并入 `AGENTS.md` 即可按流程执行。

> 注：部分技能（a-share-*、stock-universal-analysis、market-sentiment-dashboard、tdx 系等）导入自 dsh 技能库，其「运行环境契约」按原 dsh 宿主机环境编写；在 QuantBot 中由 `docker/dsh/AGENTS.md` 的运行环境映射统一转译（重依赖脚本到 quantmind 容器执行等）。

## 技能总览

### QuantMind 平台运营

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [quantmind-operations](quantmind-operations/) | 平台运营总指南：模型训练（5 步流程）、模型管理、后台数据更新、RSS 新闻对接 | 模型训练、数据更新、RSS、新闻分析 |
| [quantmind-deploy](quantmind-deploy/) | 部署运维：一键/快速/手动部署、数据库初始化、健康检查、问题排查、AutoDL 云端 GPU 训练 | 部署、一键部署、部署失败、装不上 |
| [quantdb-sdk](quantdb-sdk/) | QuantDB 数据 SDK：API Key 配置、数据集目录、字段查询、K线/财务/因子远程查询 | quantdb、数据key、数据集、查询K线 |
| [quantdb-fields](quantdb-fields/) | QuantDB 字段单位速查：各数据集实测单位、口径与陷阱（volume=股、amount=万元、L2 逐笔等） | 字段单位、成交量单位、数据口径、逐笔、十档盘口 |
| [quantdb-data-structure](quantdb-data-structure/) | QuantDB 数据结构：目录组织、Hive 分区、parquet 路径、代码格式、quantdb_hub 入口、quantdb vs quantcustom | quantdb 结构、数据目录、dt 分区、parquet 路径、数据在哪里 |
| [qwenpaw-migrate](qwenpaw-migrate/) | QwenPaw（千问）→ DSH 迁移向导：旧容器用户技能与 MCP 配置导入、核验平台技能库、停用并清理旧容器与数据卷（含整卷备份） | 迁移、QwenPaw、千问、旧数据导入、升级清理 |

### 研究与分析

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [daily-review](daily-review/) | A股每日复盘（专业版）：指数、涨停梯队、行业/概念轮动、资金面、L2 微观结构、推理信号复盘、次日方向研判，输出 MD+PDF | 复盘、每日复盘、复盘20260814 |
| [market-analysis](market-analysis/) | 市场分析报告（大盘快照版）：核心指数、广度情绪、行业热力图、板块资金流、个股主力 Top20，输出研报风 MD+PDF | 市场分析、大盘分析、行情分析、今天市场怎么样 |
| [stock-market-analysis](stock-market-analysis/) | 股票市场深度数据分析与导出：全市场扫描、行业轮动、个股六维深度分析、CSV/Excel 导出 | 分析市场、全市场扫描、导出CSV、个股研报 |
| [stock-picks](stock-picks/) | 复盘后的每日股票推荐：多维打分（L2微观/模型融合分/仓位/板块/情绪）从全市场挑强势股，输出候选榜 + Top 个股深分 PDF | 选股、推荐股票、每日推荐、明日看好 |
| [stock-research](stock-research/) | 个股深度研究（多 Agent 版）：5 分析师并行 → 多空辩论 → 汇总报告 → PDF，数据走本地 QuantDB + 新闻 | 深度研究、研究600519、多角度分析 |
| [trading-agents](trading-agents/) | 个股投研分析（智能体自主版）：本地数据 → 多空子代理辩论 → 综合研判 → PDF 报告，不依赖容器投研管线 | 投研分析、深度分析、多空分析、生成报告 |
| [smart-strategy-stock-picking](smart-strategy-stock-picking/) | 智能策略选股：基于 QuantDB 的条件选股，自然语言或结构化条件构建股票池 | 选股、筛选股票、股票池、条件选股 |
| [batch-inference-analysis](batch-inference-analysis/) | 批量推理结果分析：每日信号、行业轮动、个股分数区间、负分参考 | 分析批量推理、解读信号、每日选股 |
| [news-sentiment-research](news-sentiment-research/) | 新闻情绪研究方法论：42万篇 RSS 新闻 → FinBERT+词典情绪 → 事件研究 + 七维分析 → 优化回测 → 研报 MD+PDF | 新闻情绪、新闻规律、情绪回测、消息面 |
| [news-sentiment-finbert](news-sentiment-finbert/) | RSS 新闻情绪识别（FinBERT）安装与运维：管线架构、权重下载、字典扩充、全量重算 | 新闻情绪、FinBERT、情绪不生效、新闻重算 |

### 策略 · 因子 · 模型 · 回测

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [ai-ide-strategy-writing](ai-ide-strategy-writing/) | AI 生成 Qlib 量化策略代码，Docker runner 执行策略/回测，策略落库 | 写策略、生成策略、运行策略 |
| [backtest-center](backtest-center/) | 回测中心：快速回测、专家模式、策略对比、参数优化、向量化极速回测 | 回测、策略对比、参数优化 |
| [rd-agent-factor-mining](rd-agent-factor-mining/) | RD-Agent 因子挖掘端到端流水线：preflight → 演化 → 回测评估 → IC/Sharpe 排序 → 入库，支持五市场 | 挖因子、因子挖掘、RD-Agent、一键挖因子 |
| [model-train-infer-backtest-report](model-train-infer-backtest-report/) | 训练-推理-组合回测-专业报告全流程：13 种模型类型、批量推理全年、自定义组合回测（阈值+大盘MA+止损）、研报 MD+PDF | 训练模型、推理全年、T+3、止损、出报告 |
| [factor-train-pipeline](factor-train-pipeline/) | 因子训练链路：因子研究筛选保留集（可按库剔除，如去 L2）→ 合并自定义市场数据集 → 发布训练目录 → 生产级 LightGBM 训练全流程与实战坑清单 | 因子训练、筛选因子拿去训练、去L2训练、合并因子训练、自定义市场训练 |

### 交易

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [simulation-trading](simulation-trading/) | 模拟交易：下单买卖、持仓管理、成交查询、资金快照、模拟盘启动、实盘/模拟盘切换 | 模拟交易、买入股票、查持仓、查账户 |
| [tdx-live-trading](tdx-live-trading/) | TDX 通达信实盘交易 + 全链路监控：L2 实时推理、自动买卖、挂单/撤单、交易记录、持仓、桥健康巡检 | 实盘下单、自动买卖、挂单、撤单、TDX、链路状态 |
| [ibkr-cli](ibkr-cli/) | Interactive Brokers CLI：IB Gateway/TWS 配置、下单交易、订单管理、账户/持仓/盈亏、行情、期权链、扫描器、基本面 | IBKR、TWS、IB Gateway、brokerage CLI |

### 券商 OpenAPI SDK

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [futuapi](futuapi/) | 富途 OpenAPI（Python）：行情/K线/下单/持仓/资金 | 富途、futu、行情、下单 |
| [install-futu-opend](install-futu-opend/) | 富途 OpenD 安装助手：下载/安装/启动 OpenD，升级 futu-api SDK | 安装 OpenD |
| [tigeropen](tigeropen/) | 老虎证券 OpenAPI Python SDK：行情、股票/期货/期权交易、实时推送、CLI、MCP Server 集成 | tigeropen、tiger API、期权、订阅 |
| [tigeropen-java](tigeropen-java/) | 老虎证券 OpenAPI Java SDK | tigeropen Java SDK |
| [tigeropen-cpp](tigeropen-cpp/) | 老虎证券 OpenAPI C++ SDK | tigeropen C++ SDK |
| [tigeropen-csharp](tigeropen-csharp/) | 老虎证券 OpenAPI C#/.NET SDK | tigeropen C# SDK |
| [tigeropen-go](tigeropen-go/) | 老虎证券 OpenAPI Go SDK | tigeropen Go SDK |
| [tigeropen-rust](tigeropen-rust/) | 老虎证券 OpenAPI Rust SDK（异步） | tigeropen Rust SDK |
| [tigeropen-typescript](tigeropen-typescript/) | 老虎证券 OpenAPI TypeScript/Node.js SDK | tigeropen TypeScript SDK |

### A股研究增强（导入自 dsh 技能库，2026-09）

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [stock-universal-analysis](stock-universal-analysis/) | 个股全维度数据一次拉齐（QuantDB 直读）：历史日线、板块归属、财务三表、估值/技术指标、L1/L2 因子 | 个股数据、任意股票分析、财报、板块归属 |
| [stock-news-sentiment](stock-news-sentiment/) | 个股实时新闻与情绪分析：新闻时间线 + 逐条情感标注 + 当日情绪聚合（PG 新闻库/RSS） | 个股新闻、消息面、舆情、利好利空 |
| [market-sentiment-dashboard](market-sentiment-dashboard/) | 市场情绪温度报告：盘面状态（大盘三态/量能/位置）+ 情绪温度（涨跌家数/动量/买卖压力） | 市场情绪、情绪温度、大盘温度 |
| [a-share-event-radar](a-share-event-radar/) | A股事件雷达：解禁/股权质押/回购采集缓存与标的标签（东财数据源） | 解禁、质押、回购、事件风险 |
| [a-share-hypothesis-lab](a-share-hypothesis-lab/) | 假设库实验室：定性认知 → 带胜率的可验证假设（walk-forward OOS 纪律） | 验证想法、假设库、胜率复测 |
| [a-share-pit-financial](a-share-pit-financial/) | 财务 PIT 快照与防前视审计：按公告日还原历史时点财务，naive-vs-PIT 泄漏审计 | 财务因子回测、前视偏差 |
| [a-share-tradability-audit](a-share-tradability-audit/) | 可交易性约束审计：涨停买不进/跌停卖不出/停牌/T+1/碎股逐笔判定 | 回测能成交吗、涨停买不进 |

### 通达信（TDX）数据研究套件（45 个，导入自 dsh 技能库）

> 依赖通达信数据通道（`tdx_api_data`/`tdx_screener`/`tdx_quotes` 工具或 TdxQuant 平台），在 QuantBot（dsh）中按 `docker/dsh/AGENTS.md` 的运行环境映射执行。

| 主题 | 技能 |
|------|------|
| 平台总览 | [tdx-quant](tdx-quant/)（TdxQuant 平台）、[realtime-quotes-tdx](realtime-quotes-tdx/)（实时行情/五档/桥健康）、[ths-fuyao](ths-fuyao/)（同花顺 Fuyao 行情/涨停池/连板天梯/龙虎榜） |
| 市场与板块 | [tdx-agzxsb](tdx-agzxsb/)（市场主线）、[tdx-mrtyjb](tdx-mrtyjb/)（每日投研简报）、[tdx-bkbj](tdx-bkbj/)（板块比较）、[tdx-board-cpbd](tdx-board-cpbd/)（板块操盘必读）、[tdx-board-valuation](tdx-board-valuation/)（板块估值）、[tdx-bxzjxw](tdx-bxzjxw/)（北向资金行为）、[tdx-main-position](tdx-main-position/)（主力资金）、[tdx-jjzcyjd](tdx-jjzcyjd/)（基金重仓拥挤度）、[tdx-tczqcxx](tdx-tczqcxx/)（题材生命周期） |
| 短线情绪与打板 | [tdx-ztltby](tdx-ztltby/)（龙头博弈/连板梯队）、[tdx-dragon-tiger](tdx-dragon-tiger/)（龙虎榜）、[tdx-lhbxwfg](tdx-lhbxwfg/)（席位风格/游资）、[tdx-event-driven-short-term-catalyst](tdx-event-driven-short-term-catalyst/)（事件驱动短线催化）、[tdx-yjygby](tdx-yjygby/)（业绩预告博弈） |
| 智能选股器 | [tdx-wxd-a](tdx-wxd-a/)（选A股）、[tdx-wxd-bk](tdx-wxd-bk/)（选板块）、[tdx-wxd-etf](tdx-wxd-etf/)（选ETF）、[tdx-wxd-jj](tdx-wxd-jj/)（选基金） |
| 个股数据查询 | [tdx-financials](tdx-financials/)（财务分析）、[tdx-company-info](tdx-company-info/)（公司信息）、[tdx-earnings-warning](tdx-earnings-warning/)（业绩预警）、[tdx-report-rating](tdx-report-rating/)（研报评级一致预期）、[tdx-shareholder-research](tdx-shareholder-research/)（股东研究）、[tdx-share-capital](tdx-share-capital/)（股本信息）、[tdx-dividend-financing](tdx-dividend-financing/)（分红融资）、[tdx-stock-events](tdx-stock-events/)（股票事件）、[tdx-trading-info](tdx-trading-info/)（交易数据）、[tdx-hot-topic](tdx-hot-topic/)（热点题材） |
| 研究框架 | [tdx-ggwdzk](tdx-ggwdzk/)（个股问答总控）、[tdx-ggtzljyj](tdx-ggtzljyj/)（投资逻辑研究）、[tdx-ggycbfx](tdx-ggycbfx/)（公告与财报分析）、[tdx-gszddf](tdx-gszddf/)（公司质地打分）、[tdx-valuation-pricing-framework](tdx-valuation-pricing-framework/)（估值定价框架）、[tdx-fhgdhb](tdx-fhgdhb/)（分红与股东回报）、[tdx-fsxypmsb](tdx-fsxypmsb/)（反身性与泡沫识别）、[tdx-chltz](tdx-chltz/)（出海链投资）、[tdx-zzjdysyfx](tdx-zzjdysyfx/)（政策解读与受益分析）、[tdx-industry-chain](tdx-industry-chain/)（行业产业链）、[tdx-industry-chain-mapping](tdx-industry-chain-mapping/)（产业链映射）、[tdx-jgccgdfx](tdx-jgccgdfx/)（机构持仓股东分析） |
| 交易决策 | [tdx-position-decision](tdx-position-decision/)（仓位决策）、[tdx-trade-plan](tdx-trade-plan/)（交易计划生成）、[tdx-czzdxfxjs](tdx-czzdxfxjs/)（持仓诊断与风险检视）、[tdx-zjftjytl](tdx-zjftjytl/)（专家访谈纪要提炼） |

### 通用工具与文档（导入自 dsh 技能库）

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [tavily-search](tavily-search/) | Tavily 联网搜索（LLM 优化结果） | 搜一下、查网页 |
| [web-search](web-search/) | 网页搜索（排名结果 + 摘要/缩略图，支持时效过滤） | 网络搜索、搜新闻 |
| [find-skills](find-skills/) | 技能发现与安装助手（按需求找可装技能） | 有没有技能能做 X |
| [self-improving-agent](self-improving-agent/) | 自改进代理：把错误/纠正沉淀为学习记录 | 总结经验、记录教训 |
| [docx](docx/) | Word 文档创建/读取/编辑 | Word、docx、文档 |
| [pdf](pdf/) | PDF 读取/提取/合并/拆分 | PDF、提取文字 |
| [pptx](pptx/) | PowerPoint 创建/编辑 | PPT、幻灯片 |
| [xlsx](xlsx/) | Excel 表格读写 | Excel、表格、xlsx |


## 关联网关容器

- 富途 OpenD 网关：compose **未内置**该服务，按 [install-futu-opend](install-futu-opend/) 在本地/服务器安装启动（API 端口 11111）
- IB Gateway：`docker compose up -d ib-gateway`（.env 配置 IB_ACCOUNT/IB_PASSWORD，端口 4001=实盘 / 4002=模拟）
- QuantBot（dsh / DeepSeek Harness）：`docker compose up -d dsh`，控制台端口 8088（前端 QuantBot 页直连 `<宿主>:8088`；用 IP/域名访问前在 `.env` 配 `DSH_TRUSTED_HOSTS`）。技能经 `./skills` 只读挂载（dsh 容器 `/root/.dsh/skills`），改仓库即生效、无需上传。旧 QwenPaw 已退役（服务/镜像已删除，存量迁移见 [qwenpaw-migrate](qwenpaw-migrate/)）

## 技能开发约定

新增技能时：

1. 在 `skills/<skill-name>/` 下创建 `SKILL.md`
2. `SKILL.md` 顶部 YAML frontmatter 必须包含 `name` 和 `description`（description 含触发词，供 AI 意图识别）
3. 内容按「认证 → 端点 → 示例」组织，所有 API 统一走 `/api/v1` 前缀
4. 涉及市场的操作，标注市场参数（CN/HK/US/CRYPTO/FUTURES）
5. 更新本 README 的技能总览表
