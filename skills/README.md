# QuantMind Skills 索引

本项目所有 AI 编程工具技能（Skills）的统一目录。每个技能是一个独立文件夹，以 `SKILL.md` 为入口，通过自然语言触发词激活——AI 助手识别用户意图后自动调用对应技能完成操作。

目录结构与来源：

- `skills/<skill-name>/SKILL.md` — 全部技能的统一目录（本目录）
- 富途技能来源：https://openapi.futunn.com/skills/opend-skills.zip

## 安装方式

```bash
# Claude Code / QuantBot：复制到全局技能目录
cp -r skills/<skill-name> ~/.claude/skills/
```

其他工具（OpenCode / Codex / Trae / CodeBuddy 等）读取项目 `AGENTS.md`：把技能要点（部署检查表、API 端点清单等）并入 `AGENTS.md` 即可按流程执行。

## 技能总览

### QuantMind 平台运营

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [quantmind-operations](quantmind-operations/) | 平台运营总指南：模型训练（5 步流程）、模型管理、后台数据更新、RSS 新闻对接 | 模型训练、数据更新、RSS、新闻分析 |
| [quantmind-deploy](quantmind-deploy/) | 部署运维：一键/快速/手动部署、数据库初始化、健康检查、问题排查、AutoDL 云端 GPU 训练 | 部署、一键部署、部署失败、装不上 |
| [quantdb-sdk](quantdb-sdk/) | QuantDB 数据 SDK：API Key 配置、数据集目录、字段查询、K线/财务/因子远程查询 | quantdb、数据key、数据集、查询K线 |
| [quantdb-fields](quantdb-fields/) | QuantDB 字段单位速查：各数据集实测单位、口径与陷阱（volume=股、amount=万元、L2 逐笔等） | 字段单位、成交量单位、数据口径、逐笔、十档盘口 |
| [quantdb-data-structure](quantdb-data-structure/) | QuantDB 数据结构：目录组织、Hive 分区、parquet 路径、代码格式、quantdb_hub 入口、quantdb vs quantcustom | quantdb 结构、数据目录、dt 分区、parquet 路径、数据在哪里 |

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
| [model-training-config](model-training-config/) | 训练配置文件生成器：自然语言 → 可直接导入的 quantmind-model-training-config（YAML/.yml），带 schema 校验脚本与 4 个演示配置 | 训练配置文件、生成训练配置、导入配置、训练配置模板 |

### 交易

| 技能 | 功能 | 触发词示例 |
|------|------|-----------|
| [simulation-trading](simulation-trading/) | 模拟交易：下单买卖、持仓管理、成交查询、资金快照、模拟盘启动、实盘/模拟盘切换 | 模拟交易、买入股票、查持仓、查账户 |


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

## 关联网关容器

- 富途 OpenD 网关：compose **未内置**该服务，按 [install-futu-opend](install-futu-opend/) 在本地/服务器安装启动（API 端口 11111）
- IB Gateway：`docker compose up -d ib-gateway`（.env 配置 IB_ACCOUNT/IB_PASSWORD，端口 4001=实盘 / 4002=模拟）
- QwenPaw（QuantBot）：`docker compose up -d qwenpaw`，端口 8088 绑定地址由 `.env` 的 `QWENPAW_BIND` 控制（默认 `0.0.0.0`，仅本机访问则设 `127.0.0.1`），配合云安全组/防火墙限制来源 IP

## 技能开发约定

新增/改版技能时：

1. 在 `skills/<skill-name>/` 下创建 `SKILL.md`（模板见 `_shared/SKILL_TEMPLATE.md`）
2. `SKILL.md` 顶部 YAML frontmatter 必须包含 `name`（=目录名）和 `description`（含触发词，供 AI 意图识别；触发词避免与其他技能重复）
3. **不要粘贴运行环境契约全文**：只保留 `_shared/env-contract.md` 的一行引用（契约改一处全局生效）
4. 内容按「认证 → 端点 → 示例」组织，所有 API 统一走 `/api/v1` 前缀；股票代码用前缀式（如 `SH600036`）
5. 涉及市场的操作，标注市场参数（CN/HK/US/CRYPTO/FUTURES）
6. 提交前必须跑通：`python scripts/lint_skills.py --strict`（规范）与 `python scripts/check_skill_endpoints.py`（端点对照，0 死链）
7. 确认是历史提及才允许进 `_shared/endpoint_allowlist.txt` 豁免；正常漂移一律修技能正文
8. 更新本 README 的技能总览表

## 同步与校验（唯一入口）

- 日常更新走部署链路自动同步：`deploy/update.sh` / `deploy.sh` / `full-deploy.sh` 统一调用 `deploy/sync-qwenpaw-skills.sh`（经 `docker exec qwenpaw` 跑 `quantbot_init`，避免 Web updater 容器打不到宿主 8088）；`--skip-skills` / `QUANTMIND_SKIP_SKILLS=true` 可跳过
- 手动同步：服务器上 `bash scripts/quantbot_init.sh`（或 `--skills-only`），再 `docker restart qwenpaw`
- 更新技能后走 API 进技能池，**禁止手工拷贝**
- 验证：`docker exec qwenpaw qwenpaw skills list`（技能数与启用数一致）+ `docker exec qwenpaw qwenpaw skills test <name>`
- 漂移基线：`skills/_shared/drift_report.md`（自动生成，勿手改）
