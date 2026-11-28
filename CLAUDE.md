# CLAUDE.md

本文件为 Claude Code（claude.ai/code）在此仓库中工作时提供指导。

## ⚠️ 免责声明

本项目仅供学习研究与技术演示，不构成任何投资建议。本系统产出的所有分析报告和交易信号均由 AI 自动生成，可能存在错误或偏差。投资决策请咨询持有中国证监会颁发资质的专业机构。作者不对使用本工具产生的任何投资损失承担责任。股市有风险，投资需谨慎。

## 项目概述

QuantMind 是一个量化交易平台，后端为 Python（FastAPI），前端为 Electron/React/TypeScript。开源版（OSS）采用单容器部署，所有后端服务运行在同一个容器中。

## 后端服务（统一入口 `backend/main_oss.py`）

| 服务 | 端口 | 职责 |
|------|------|------|
| api | 8000 | 用户认证、策略管理、社区、新闻代理 |
| engine | 8001 | Qlib 回测、AI 策略生成、模型推理、Alpha Agent |
| trade | 8002 | 订单管理、持仓、风控 |
| stream | 8003 | 实时行情、WebSocket 推送 |

## 常用命令

### 后端
```bash
# 启动全部服务（Docker）
docker compose up -d

# 本地运行单个服务
SERVICE_MODE=api python backend/main_oss.py

# 测试（在项目根目录执行）
python backend/run_tests.py unit        # 单元测试
python backend/run_tests.py integration # 集成测试
python backend/run_tests.py all         # 全部测试
python backend/run_tests.py trade-long-short  # QMT MVP 链路测试

# 代码检查与格式化
ruff check backend/
ruff format backend/
```

### 前端（Electron 应用，位于 `electron/`）
```bash
npm install              # 安装依赖
npm run dev              # 开发模式（Electron 桌面端）
npm run dev:web          # 开发模式（Web 浏览器）
npm run typecheck        # 类型检查
npm run dashboard:build  # 生产环境构建
```

## 架构要点

- **特征工程**：48 维特征由外部服务写入 `market_data_daily` 表
- **交易服务**：外部报单前强制「本地优先」落库持久化
- **真单成败判据 = 订单终态**（2026-09-24 修）：`TradingEngine.submit_order` 只在终态 `REJECTED` 时返回 `success: False` —— 券商不抛异常直接拒、执行异常两条路径**都由 `_execute_via_broker` 吞掉并置终态**，所以调用方**不能**按「有没有异常」判成败（旧实现把拒单报成 success，止损链因此记 `ST_SUBMITTED` 当日终态：不重试、无告警）。超时保持 `submitted` + `success: True`（单**可能已在柜台**，绝不可当失败重试）。告警面的拒因出口 = 派发信封**顶层** `message`（`internal_strategy_dispatcher` 的 `lot_blocked`/`risk_blocked`/`direct` 三个失败分支都抬出；嵌套 `result.message` 是原始出处），消费方**不要**拿 `str(信封)` 当文案
- **Redis 库分配**：0=通用，1=认证，2=交易，3=行情，4=回测，5=缓存
- **共享模块**：`backend/shared/` 存放跨服务代码（DB 管理器、Redis 客户端、配置、日志）
- **MCP 服务（2026-11-28）**：独立容器 `quantmind-mcp`（镜像 `quantmind-mcp:latest`，容器内 8765 / 宿主仅 `127.0.0.1:18765`，对外经 web nginx `/mcp`），代码 `mcp_server/`；只经 `/api/ext/v1` 访问平台，不直连 DB。认证复用 SDK Access Key/Secret Key（`POST /api/ext/v1/auth/session` 换 1h token），工具权限码与目录唯一事实源 `backend/shared/mcp_tool_catalog.py`；后台入口「系统设置 → MCP 服务」（开关/工具权限/配额/客户端签发/审计），表由 `backend/shared/db_migrations/upgrade_v1.3.0.sql` 建（API 启动自愈）。**v1** 覆盖数据/清单/任务/模拟盘（含下单/撤单，写工具默认关）；**v2 只读**覆盖推理排名/单票评分。**实盘永不接入 MCP**。对外 API 需配置 `EXTERNAL_API_SECRET`（`.env` 或 `config/runtime.env`），未配置时 `/api/ext/v1` 全部 503；MCP 健康 `GET /health`，管理页健康经 `/api/v1/admin/mcp/health`。
- **瞬时时间（成交/委托）**：`sim_trades.executed_at` 等瞬时列一律 `TIMESTAMPTZ` + aware UTC。写入走 `backend/shared/utc_datetime.py` 的 `utc_now()` / `UtcDateTime`，JSON 输出带 `Z`。禁止 naive UTC 与上海墙钟混用。存量库由 `data/upgrade_v1.0.7.sql` 对齐。
- **策略存储**：`backend/shared/strategy_storage.py` 是所有策略增删改查的唯一入口
- **实盘双开关（开源版默认全关）**：后端 `ENABLE_REAL_TRADING`（`backend/shared/live_trading_gate.py` 中间件按**端点**拦，非按前缀——`/api/v1/tdx/*` 下行情与交易同前缀，按前缀拦会误杀盘中行情主源）+ 前端 `VITE_ENABLE_REAL_TRADING`（`electron/src/config/tradingFlags.ts` 唯一读取点，三态同 `marketFlags.ts`）。两个都开才显示实盘 UI；任一为 false 都不渲染 / 都 403（`detail=real_trading_disabled`）。代码全留，仅收敛入口。**栏目源码已入仓**（2026-10-08）：`electron/src/features/local-live/`（入口 `LiveTradingPage.tsx`），底部导航项由 `isLocalLiveAvailable && isLiveTradingEnabled()` 收敛——flag 关时整栏不渲染（含顶栏「实盘」标识与真实账户概览），深链 `#/live` 落 `LiveDisabledPage` 说明页；部署闸门 `scripts/deploy_frontend.sh --allow-local-live` 改为拦「实盘 UI 可见的构建」（判据=构建开关本身）。启用流程(TDX 桥 / QMT Windows 端)见 `docs/实盘模块_启用与通道指南.md`。
- **因子挖掘文档链（Docker 出厂默认开启，2026-10-24）**：因子挖掘「上传文档」= 前端构建期 `VITE_ENABLE_DOC_MINING`（`docker/Dockerfile.web` 的 ARG 与 `scripts/deploy_frontend.sh` 都缺省 `true`）+ 后端 `ENABLE_DOC_MINING`（`docker-compose.yml` 与 `.env.example` 缺省 `true`）。Docker 服务器版**第一次安装、第一次启动就要能打开**，不允许"重启后才有"——前端开关是 Vite 构建期内联，重启补不出来。便携包 / Windows 桌面客户端仍默认关。关闭需两端显式 `false`；端点 403 标记 `doc_mining_disabled`。原件送 MinerU 云端（数据出网，界面固定披露）。回归护栏 `backend/tests/test_doc_mining_release_default.py`，流程见 `docs/文档挖掘_启用与通道指南.md`。
- **展示面/执行面用词边界**：展示面（研究评分卡、信号点下钻、K线标记、日历、筛选器、desk 信号卡）只说**位置**——`electron/src/features/shared/signalVocabulary.ts`（靠前/靠后/居中）与 `researchScore.ts`（0–100 截面分位）；执行面（下单表单、委托台账、成交回报、调仓计划单）保留「买入/卖出」，含糊化会让人下错单。**不要写「前 20%/后 20%」**：`signal_side` 有分位与绝对阈值两条生成路径，百分比标签在其中一条上是假的。
- **QQ 推送面分级（2026-10-12 用户拍板重规划）**：P0 即时（成交回执/风控/止损）→ **持仓情报**即时摘要（`qq_digest`，120s 窗口，header `📌 持仓情报 · {市场}`）→ **非持仓/大盘情报只进速览档期**（`qq_briefing`：每交易日 08:35/11:35/15:15 三档卡片合集，任务 `intel_briefing` 常驻 trade；只认最近一档不补发、读失败不落 done 键下周期重试、20h 新鲜度上限掐周末旧闻）→ 日报（15:10 收盘报表、15:40 值班摘要不动）。路由在 `sentinel_alert_service._decide_push`：市场级/范围外标的 → 速览缓冲；小时配额只拦即时面（速览不吃配额）。**陷阱：速览写侧必须走 `_briefing_redis()`（trade 库 db2，与冲刷侧 `trade_shared.deps.get_redis` 同库）——哨兵自身 client 走 db0，拿它写缓冲=结构性空转（卡片永远不发且全链无报错）**。排版=卡片式纯文本（`━` 分隔线；QQ 拒 markdown），行=`方向箭头 名称(前缀码) 事实 · 利空/利好/风险`（`alert_text.format_sentinel_digest_line`）。
- **研究评分口径**：`rank_pct`（`engine_signal_scores` 契约列）的分母是**同一次推理的那批标的**，不是全市场，跨模型/跨市场不可直接比较。前后端同一公式（`research_score.py` / `researchScore.ts`，金样 `backend/tests/fixtures/researchScoreGolden.json`，两侧测试都读它）。**缺失一律 `—`，绝不显示成 0**（`position_score=0` 的语义是「引擎明确不入场」）。
- **评估体系机构口径（2026-11-24）**：评分卡缺失核心维/缺失权重 >20% → 最高 B（`eval_scoring.combine_dimension_scores` 的 `core_dims`/`grade_cap`，禁止靠剩余权重归一造 A）；模型 Tier0 审计失败 → 封顶 D，walk-forward <3 vintage → 最高 B；回测体检只认 `policy="strict"`（DSR/HAC/PBO），PBO 缺失最高 B，旧报告不得晋级。`eval_scores` 写入时由 `eval_contract.save_eval_score` 统一调用 `eval_governance.decide_action` 生成 `action`（promote/hold/watch/de_risk/block），调用方禁止自传；治理表/事件由 `upgrade_v1.2.0.sql` 建、事件 append-only。自动降险只写治理状态与 Redis `qm:eval:derisk:{type}:{id}`，**不直接改交易参数**。细案 `docs/评估体系_P1-P2_机构级实施细案_20261124.md`。
- **组合选股（推理中心「组合选股」Tab，2026-10-21）**：同市场、同预测周期模型在**推理时**按截面分位加权合成，**不是融合模型**（不训练、不改写成员 `pred`，blend≠fusion）。服务 `backend/services/engine/services/model_blend_service.py` + 纯函数 `backend/services/engine/inference/blend_engine.py`；路由 `/api/v1/models/blends/*` **必须注册在 `model_training` 的 `GET /{model_id}` 兜底之前**（否则整组 404）。展示面只说位置（靠前/靠后/居中、新进/留存/退出），**禁止买入/卖出字眼**；调仓计划确认只写纸面台账（「仅纸面记录」，不产生委托）。滚动视图 `held_days` 只算窗口内连续在榜，断榜重进按「新进」计。窗口分有进程内 `_window_cache`（键含成员/权重/参数/截止日）。
- **训练目标契约（Target Contract，2026-11-24）**：训练目标 = 注册表里版本不可变的 `LabelTargetSpec`（`backend/shared/training/target_spec.py`），训练请求用 `target_ref {id, version}` + 白名单 `label_overrides`；API 将冻结 spec + sha256 写进 `config.label`，训练容器 `target_contract.py` 重算 hash/校验平铺字段后才训练。注册表 `qm_label_targets`/审计表 `qm_label_target_audit` 由 `backend/shared/db_migrations/upgrade_v1.1.9.sql` 建表，触发器禁止改 spec/删行；内置 29 个目标（CN 8 / HK 7 / US 7 / FUTURES 7；T+1/3/5/10 rank + T+5 raw/top20/updown，CN 另有 quantile）。13 模型能力矩阵单一事实源 `backend/shared/training/capabilities.py`：classification 仅 6 个非 DL 模型；quantile 仅 CN 单 LightGBM；排序损失仅三棵 GBDT；sample_weight 支持 lgb/xgb/cat/linear/rf。提交/编排/容器三层硬闸，classification×DL、quantile×非 LGB、非法 objective、open 价无直读源、size_excess 缺 total_cap 一律 422/abort；标签诊断 NaN>50%、正类率<1%/>99%、每日截面<50、top-tail 偏离>50% 训练前失败。legacy 请求 config 键集合与训练行为不变，metadata 记 `target_id=legacy` + 合成 hash；重训/滚动配方携带 `target_ref/label_overrides/expected_target_hash`，覆盖后 hash 不一致即 `target_hash_drift`。说明 `docs/训练目标契约_使用说明_20261124.md`，细案 `docs/训练目标体系_W0-W6_机构级实施细案_20261124.md`。
- **多模型串行补推（模型治理「一键推理至最新日期」）**：`model_inference_jobs` 单活跃任务 + heartbeat 回收；前端 `BackfillJobModal` 轮询（页面隐藏暂停），`localStorage` 存 job_id 可关窗恢复，取消=「当前交易日完成后停止」。逐日合并 `pred` 走 `pred_merge` 文件锁，单日失败隔离计入「部分成功」，并发合并绝不丢行。
- **程序化交易报告义务**：`backend/shared/programmatic_trading_disclosure.py` 是法规常量（高频阈值 300 笔/秒、20000 笔/日）与待填报信息（软件名称/版本号/开发者）的唯一出处；`GET /api/v1/system/programmatic-trading-disclosure` 供「设置→系统信息」复制填报。风控 `l3.order_frequency.max_per_minute` 撞线时**只告警不拦单**。合规边界见根目录 `DISCLAIMER.md`。
- **盘中结构/量能（2026-11-16）**：`backend/services/live_trading/services/intraday_market_watch.py`（trade 常驻，免费 TDX 桥，只读行情不下单）→ `qm:intraday:market:{day}`；决策轮【盘中结构】块由 `backend/shared/intraday_live_blocks.py` 渲染、`decision_round_{feeds,blocks,io}.py` 接线。全市场 `get_pricevol` 60s；持仓+池 `get_more_info`（**必须带 `_nc` 绕 86400s 缓存**，否则 L2 聚合/量比盘中全 0）120s 轮转；板块成分 15:05 后日缓存。**免费 TDX 桥盘口只回 1 档**（渲染按实际档位写「一档失衡」）；持仓+池真五档走 **QMT 伴生桥 `get_full_tick`（`source=qmt`，免费 5 档）** → `market:snapshot:qmt:{suffix}`，`decision_context_source.read_snapshots` 优先读。
- **Celery worker 必须唯一**：`SERVICE_MODE=all` 下 `main_oss.py` 默认**不启动**内嵌 worker（需 `EMBEDDED_CELERY_WORKER=true`），消费队列的只有 `celery-worker` 容器。重复 worker 会瓜分 `qlib_backtest_srv` 队列消息，表现为定时任务随机「不执行」；排查看 `redis-cli client list | grep cmd=brpop` 应只有 1 个。
- **市场数据同步不内置默认调度**：是否开启、何时触发一律以用户在前端「同步调度」保存的 Redis 配置为准（`quantmind:sync_schedule:{market}`），未配置时 5 个市场全部 `enabled=false`；`MARKET_SUGGESTED_TIMES` 只是前端时间预填建议值（次日 00:00 以后错峰），不参与触发。详见 `backend/services/engine/README.md` →「定时调度与市场数据同步」。
- **Alpha Agent**：`backend/services/engine/alpha_agent/` - 因子演化启动器，经 RD-Agent 支持多市场
- **RD-Agent 集成**：`backend/services/engine/rd_agent/` - 封装微软 RD-Agent 的多市场因子挖掘框架
  - `market_adapters/` - MarketAdapter 模式：a_share（A股）、crypto（区块链）、hong_kong（港股）、us_stock（美股）
  - `rd_loop_wrapper.py` - 将 RD-Agent 的 FactorRDLoop 封装为 QuantMind 接口
  - `data_pipeline/` - 各市场专属的数据下载与格式转换
- **数据平台**：`backend/services/engine/data_platform/` - 多市场数据聚合（A股/港股/美股），适配不同数据源
  - **QuantDB 数据中枢**：`quantdb_hub.py` - 全部 A 股 parquet 读取的统一入口（DuckDB + pd.read_parquet）
  - **QuantDB 本地适配器**：`adapters/quantdb_local_adapter.py` - A 股主数据源（本地 parquet）
  - **QuantDB 远程适配器**：`adapters/quantdb_adapter.py` - 远程 SDK 实时查询（兜底）
  - **Qlib 数据构建器**：`qlib_data_builder.py` - 由 QuantDB parquet 生成 Qlib 二进制缓存（派生产物）
  - **字段路由**：`config/data_sources/field_routing.yaml` - quantdb_local 优先，旧适配器兜底
- **投研分析（自主模式）**：`skills/trading-agents/` - 智能体自主版个股深度投研（拉本地数据 → 子代理辩论 → md/PDF 报告落「股票报告」档案）
  - 容器投研管线（`TradingAgents-astock` 包 + `/analyze`、`/progress`、`/report`、`/stop`）已于 **2026-10-09 完整退役**；历史实现见 `git show bb1b38a0:TradingAgents-astock/`
  - `backend/services/engine/trading_agents/progress.py` - 进度跟踪器（报告导出复用的阶段表）
  - `backend/services/engine/routers/trading_agents.py` - 仅存报告档案文件面（files/list、upload、pdf、move、delete）
- **数据管线**：`backend/scripts/` - 统一的每日数据同步
  - `quantdb_daily_sync.py` - 主同步链路：sync_dataset() → parquet → PG 回填 → Qlib 缓存
  - `daily_data_sync.py` - 全量同步：QuantDB parquet → baostock → akshare → eltdx → PG → Qlib 缓存 → 指标 → parquet
  - `sync_investment_data.py` - 从 GitHub releases 下载并解压 qlib_bin（遗留兜底）
  - `update_feature_parquet.py` - 151 维特征计算（动量/波动率/流动性/资金流/风格）
- **新闻/RSS**：Huntly + RSSHub 聚合财经新闻，经 API 服务代理访问

## 股票代码标准化（分层口径，禁止跨层混用）

- **QuantDB parquet / Qlib / 行情数据层**：后缀式（如 `600036.SH`，Qlib 桥接用全小写 `sh600036`），否则静默查空
- **PG 数据库字段 / Redis 键 / 前端 / Strategy Lab SDK / 大多数 API**：前缀式（如 `SH600036`）
- **标准化工具**：
  - 后端：`backend/shared/stock_utils.py` → `StockCodeUtil.to_suffix(code)` / `.to_prefix(code)` / `.to_qlib(code)`
  - 前端：`electron/src/utils/portfolioUtils.ts` → `normalizeStockCode(code)`（输出前缀式）
- **市场自动识别**：
  - `SH`：6xxxxx、9xxxxx
  - `SZ`：0xxxxx、3xxxxx、2xxxxx
  - `BJ`：4xxxxx、8xxxxx
  - `HK`：5 位代码或 `.HK` 后缀
  - `US`：无数字前缀的股票代码（Ticker）

## 环境变量

必需的 `.env` 键（默认值见 `docker-compose.yml`）：
- `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`
- `REDIS_HOST`、`REDIS_PORT`
- `SECRET_KEY`、`JWT_SECRET_KEY`
- `STORAGE_MODE=local`（OSS 版必需）
- 本地数据目录（由 `docker-compose.yml` 设置，经 `./data:/data` 卷持久化，默认即容器内 `/data/xxx`）:
  - `QM_QUANTDB_DATA_DIR` - QuantDB A股本地 parquet（默认 `data/quantdb/`）
  - `QM_QUANTUS_DATA_DIR` - QuantUS 美股（默认 `data/quantus/`，Yahoo Finance 摄取）
  - `QM_QUANTHK_DATA_DIR` - QuantHK 港股（默认 `data/quanthk/`）
  - `QM_QUANTBC_DATA_DIR` - QuantBC 区块链/加密货币（默认 `data/quantbc/`，Binance 摄取；生产默认 `ENABLE_CRYPTO=false` 隐藏该市场）
  - `QM_QUANTFUTURES_DATA_DIR` - QuantFutures 期货/贵金属（默认 `data/quantfutures/`，akshare 摄取）
- 各市场的后台管理界面在「数据管理」页的 tab：QuantDB A股 / QuantUS 美股 / QuantHK 港股 / QuantBC 区块链 / QuantFutures 期货；同步入口为各 `backend/scripts/*_daily_sync.py`

## 代码风格

- Python：行宽 88，使用 ruff 做检查与格式化
- TypeScript：提交前端改动前必须运行 `npm run typecheck`
- **颜色语义（两套，不许混用）**：数值涨跌按 A 股口径 **涨=红、跌=绿**（`text-rose-600`/`#e11d48` vs `text-emerald-600`/`#10b981`）；状态/评级另按 **绿=合格、黄=警告、红=严重**。中性数值（MDD/波动率/分位）不染色；图表多序列靠线型+图例区分，不靠红绿对立。首例 `electron/src/features/market-analysis/components/MarketTimingV2Panel.tsx` 的 `retTone`/`UP_COLOR`/`DOWN_COLOR`。
- **浏览器支持策略（`modern-web-guidance` skill 的 fallback 判据）**：Web 构建按 Baseline Widely available（该 skill 的默认口径）；Electron 43 桌面端内置 Chromium 更新，可放宽到该版本已实现的 API，但 Electron/Web 共享代码必须保留 Web 端的 fallback，不引入 polyfill
- **前端任务先查指南**：改 `electron/src` 的 HTML/CSS/客户端交互（dialog/popover、表格、暗色模式、表单与校验、动画/滚动、a11y、CWV 性能）前，先调用 `modern-web-guidance` skill 检索再写码；纯后端、构建工具、测试类改动不查。npx 被工具闸拦时，直接读 `~/.claude/skills/modern-web-guidance/guides/<分类>/<指南>.md`

## 部署工作流

代码改动后务必：
1. **提交 git**：写描述清晰的提交信息
2. **部署到服务器**：在目标服务器上使用受控更新脚本

```bash
# 本地：提交改动
git add <changed-files>
git commit -m "descriptive message"
git push gitee master

# 注意：目标服务器的 SSH 别名/主机与项目目录因人而异，部署前先向用户询问确认。
# 用 ${SSH_TARGET} 和 ${PROJECT_DIR} 表示用户提供的具体值。
ssh ${SSH_TARGET} "cd ${PROJECT_DIR} && sudo bash deploy/update.sh"
```

Electron 前端在本地开发时使用 Vite HMR；修改 `electron/src` 后运行 `npm run typecheck` 即可，不需要复制构建产物到服务器的 `web` 容器。

### Web 前端部署（独立 nginx 容器 `quantmind-web`）
- **⚠️ `docker-compose.yml` 里没有 `web` 服务**（实测 grep 确认；服务只有 db/redis/quantmind/celery-worker/celery-beat/data-gateway/huntly/rsshub/dsh/ib-gateway）。`quantmind-web` 是独立容器（镜像 `quantmind-web:latest`），**不受 `docker compose` 管理**，`docker compose up -d web` / `restart web` 都是无效命令。
- **唯一部署入口**：`bash scripts/deploy_frontend.sh`（先 `cd electron && npm run build`，再 `docker cp dist-react/. quantmind-web:/usr/share/nginx/html/`，带旧 chunk 清理 + main 文件校验 + assets 数量比对 + curl 健康检查）
- **改前端后的完整流程**：`npm run typecheck` → `bash scripts/deploy_frontend.sh` → 浏览器强刷 `Ctrl+Shift+R`
- **`web/dist` 是遗留产物**：曾经由 `web` 服务的 Volume 挂载伺服，该服务已不在 compose 中；`cp -r electron/dist-react/* web/dist/` 现在不产生任何效果，不要走这条路
- **注意容器重建即丢失**：`docker cp` 的产物在容器重建后消失（回到镜像内旧版本），需重新跑一次 `deploy_frontend.sh`

## 关键文件

- `backend/main_oss.py` - 全部后端服务的统一入口
- `backend/run_tests.py` - 多模式测试运行器
- `backend/shared/` - 跨服务共享模块
- `minibt/` - vendored 裁剪版 minibt(20 行策略 DSL + 指标库 + 双引擎回测;补丁与口径差异见 `minibt/PATCHES.md`,运行时镜像 `docker/Dockerfile.minibt-runner`)
- `backend/shared/minibt_qdb.py` - QuantDB → minibt KLine 数据适配器(AI-IDE minibt 策略用)
- `backend/shared/minibt_result.py` - minibt 回测结果提取(修复浮盈/手续费口径,[RESULT] 行 + result.json)
- `backend/services/engine/routers/ai_ide/executor.py` - AI-IDE 代码执行器(minibt 自动路由 /execute/result 端点)
- `backend/services/engine/alpha_agent/launcher.py` - 因子演化启动器（支持 market 参数）
- `backend/services/engine/rd_agent/market_adapters/` - 市场适配器注册表（a_share、crypto、hong_kong、us_stock）
- `backend/services/engine/rd_agent/rd_loop_wrapper.py` - 桥接 RD-Agent 与 QuantMind 的 RDLoop 封装
- `backend/services/engine/routers/alpha_agent.py` - Alpha Agent API（含 /markets、带 market 参数的 /evolve）
- `backend/services/engine/routers/trading_agents.py` - 投研报告档案文件面（报告列表/上传/PDF/移动/删除；执行面端点已退役）
- `backend/services/engine/trading_agents/progress.py` - 投研进度跟踪器（阶段表；报告导出复用）
- `scripts/alpha_agent/run_rd_agent.py` - RD-Agent 多市场运行脚本（子进程入口）
- `backend/services/engine/data_platform/` - 多市场数据平台
- `backend/services/engine/data_platform/quantdb_hub.py` - QuantDB 数据中枢（A 股 parquet 读取统一入口）
- `backend/services/engine/qlib_data_builder.py` - 由 QuantDB parquet 构建 Qlib 二进制缓存
- `backend/services/engine/data_platform/adapters/quantdb_local_adapter.py` - A 股主数据适配器（本地 parquet）
- `backend/scripts/quantdb_daily_sync.py` - 主每日同步（sync_dataset → parquet → PG → Qlib 缓存）
- `backend/scripts/daily_data_sync.py` - 全量同步（QuantDB parquet → 多源兜底 → PG → Qlib 缓存 → 指标）
- `backend/scripts/sync_investment_data.py` - GitHub investment_data 下载与解压（遗留）
- `backend/scripts/update_feature_parquet.py` - 151 维特征 parquet 计算
- `backend/services/api/routers/admin/data_platform.py` - 数据平台管理端点（同步、parquet、健康）
- `backend/services/api/routers/news.py` - 新闻代理路由
- `backend/services/api/routers/market_kline.py` - K 线行情路由
- `electron/src/features/trading-agents/pages/ReportManagerPage.tsx` - 报告档案管理页（QuantBot ReportsModal 复用；执行面页面已退役）
- `backend/services/api/routers/model_rolling.py` + `rolling_shared.py` - 滚动训练用户态端点（JWT `/api/v1/models/rolling/*`：recipes/campaigns/schedule/dispatch/derive；调度校验与内部端点共享 `apply_schedule_update` 一份实现，前端不持内部密钥）
- `backend/shared/training/model_recipe.py` - 模型→滚动配方派生（读模型目录 `metadata.json`+`config.yaml`；云端导入模型同路径可用；缺因子源/特征/可训练模型类型即拒派生；`deploy_to_production` 恒 False、`auto_feature_filter` 恒关）
- `backend/shared/training/recipe_registry.py` - 滚动配方注册表（内建 + 用户配方目录 `QM_ROLLING_RECIPE_DIR`〔默认 `/data/rolling_recipes`〕双源装载；`save_user_recipe` 原子落盘、内容未变幂等）
- `electron/src/pages/ModelRollingPanel.tsx` + `services/modelRollingService.ts` - 模型管理「滚动训练」面板（派生配方 / 手动派发 / 月度重训调度 / 滚动台账）
- `backend/shared/tdx_aidata/` - TdxAiData 实时行情通道（worker 唯一 SDK 引入口 + 订阅引擎写 `market:snapshot`/`market:series` 标准键，P6 盘中主源；**SDK 单次订阅上限 100 只**——超限整批拒绝且只打印不抛，>100 必须分片集群 `TdxAiDataCluster`，分片数在 Redis `qm:market:tdx_aidata:config.shard_count`）
- `backend/shared/l05_store.py` - L0.5 热集快照按日落盘（`data/l05_snapshots/date=YYYYMMDD/` + 质检 + 降冷 + 订阅写侧归档器，T-P6-04）
- `backend/shared/freshness.py` - 行情新鲜度分级唯一谓词（fresh/stale/unavailable；阈值 env 唯一读取点，T-P6-05）
- `backend/shared/feature_defs.py` - 批量特征定义唯一实现（151 维 OHLCV 因子；原 update_feature_parquet 纯迁移，改算法必须过金样）
- `backend/shared/feature_incremental.py` + `backend/services/engine/inference/incremental_features.py` - 增量特征引擎（32 列快车道与批量 ε=1e-9 金样锁定 + 快照状态机/分钟桶/provenance，T-P6-07）
- `backend/shared/market_regime.py` + `backend/services/engine/realtime_regime.py` - 市场状态（regime）口径单源（日频/日内共用）+ 日内服务（指数 live + 热集广度 → 快照/总线，T-P6-13）
- `backend/shared/intel_events.py` + `backend/services/stream/ws_core/intel_pusher.py` - 盘中情报总线（Redis Stream `intel:events`，schema 校验/生产消费 SDK/WS `intel.{tenant}.market.*` 鉴权，T-P6-11）
- `backend/services/engine/inference/realtime_{core,service}.py` + `replay_verifier.py` - 热集实时推理（ONNX、账本 `qm:realtime:infer:ledger:*`）与回放复现验收（同数据同特征同模型 → 信号 diff=0；CLI `scripts/p6_replay_verify.py`，T-P6-08/09）；**基线取数按模型 `metadata.data_source` 分派**（`load_baseline_for_model`：`quantdb_factors` → `QuantDBFactorReader` 直读最近可用因子日，其余 → 遗留 `model_features_{year}.parquet` 不可变快照；在线/回放共用，日级缓存）
- `backend/shared/latency_metrics.py` - 端到端时延打点（`intel:latency` 滚动统计；报表 `backend/scripts/latency_report.py`，T-P6-05）
- `backend/scripts/l05_maintenance.py` - L0.5 留存运维（质检报告 `report` / 容量 `capacity` / 降冷 `prune` 默认 dry-run）
- `backend/services/live_trading/services/qmt_quote_backup.py` - 大 QMT 备源行情席（全推订阅 → 主源陈旧超 30s 时旁路写 `market:snapshot/series`，source=qmt_big；状态 `qm:qmt:quote:backup:status`）
- `backend/services/live_trading/services/tdx_hot_set_feed.py` - TDX 桥热集行情轮询（trade 任务，env `TDX_HOTSET_FEED_ENABLED` 默认开）：热集 529 只含指数按 ~0.18s/只（桥限流 600/min 内）轮转取 `get_market_snapshot`（五档 Buyp/Buyv/Sellp/Sellv→bid1-5/ask1-5）→ 标准键 `market:snapshot/series`（source=tdx_bridge）；状态 `GET /tdx/quote-feed/status` → `hot_set` 段；节拍 `TDX_HOTSET_PACING_S` 可调
- `backend/services/live_trading/services/tdx_ingest/` - 通达信桥数据面采集（trade 任务，批次 1 已交付）：盘后档位 15:31 pricevol / 15:45 universe / 16:45 more_info 全A扫描 / 17:20 go / 17:30 gp / 23:50+06:30 补采（预算是桥侧 600/min 共享中的 180/min 令牌桶，Redis db2）；PG+parquet 双写（`data/tdx_bridge/`）；GP/BK/SC/GO 家族缺客户端数据包时状态置 blocked 零重试，数据包就位自动转正；历史回填 `backend/scripts/tdx_ingest_backfill.py`（水位断点续跑）；状态面 `GET /api/v1/tdx/ingest/status`；设计全文 `docs/通达信桥数据面全量接入_实施细案_20261015.md`
- `docker-compose.yml` - 本地部署配置
