# QuantMind 2.0 功能指南（完整版）

> 本文档面向 LLM / 开发者，系统描述 QuantMind 2.0 版本的完整功能结构与实现要点，便于快速理解本项目的模块边界、数据流与关键接口。

---

## 0. 文档使用说明（阅读前必读）

- **项目定位**：QuantMind（量化大脑）是面向个人量化研究者、投研团队与机构的一体化 **AI 原生量化交易平台**，开源版（OSS）采用单容器部署。
- **技术栈**：后端 Python FastAPI（微服务单入口 `backend/main_oss.py`），前端 Electron + React + TypeScript。
- **核心能力闭环**：
  ```text
  数据底座 -> 因子挖掘 -> 模型训练 -> 批量推理 -> 组合回测 -> QMT/通达信实盘 -> 生产监控
  ```
- **股票代码规范（CRITICAL）**：全项目强制使用**前缀格式**（如 `SH600036`），禁止后缀格式（`600036.SH`）。后端统一走 `backend/shared/stock_utils.py -> StockCodeUtil.to_prefix()`，前端走 `electron/src/utils/portfolioUtils.ts -> normalizeStockCode()`。
- **Redis 库分配**：0=general、1=auth、2=trade、3=market、4=backtest、5=cache。

---

## 1. 版本总览

QuantMind 2.0 围绕五大方向升级：

| # | 方向 | 一句话说明 |
| --- | --- | --- |
| 1 | 分析类工具 | 新增市场分析与投研分析工具矩阵，全面分析资金走向与未来趋势 |
| 2 | 模型工场 | 模型支持增至 **13 种**，覆盖树模型/线性/DL 全类型 |
| 3 | 数据同步 | 增量更新客户端 + 自动更新，数据获取更便捷 |
| 4 | 一键部署 | 后端打包完整镜像，offline/online/update 三脚本一键部署 |
| 5 | 模型广场 | 新增 Model Hub，支持模型免费共享与下载 |

---

## 2. 功能一：分析类工具（资金走向 + 未来趋势）

### 2.1 市场分析平台（Market Analysis）

**前端页面**：`electron/src/features/market-analysis/pages/MarketAnalysisPage.tsx`
**后端模块**：`backend/services/api/market_analysis/`
**API 前缀**：`/api/v1/market-analysis`（见 `backend/services/api/market_analysis/router.py`）

包含以下可视化与分析能力：

| 子能力 | 说明 | 关键组件/接口 |
| --- | --- | --- |
| 大盘全景看板 | 核心指数快照、涨跌家数、涨停/跌停、赚钱效应、市场情绪温度计 | `MarketBreadthCard`；`GET /breadth`、`GET /indices/overview` |
| 多周期资金流向 | 1/3/5/10/20 日可切换，板块与个股主力资金净流入/净流出排行 | `CapitalFlowHorizontalBarChart`；`GET /money-flow/stocks` 等 |
| 资金流动全景图 | 主力 vs 散户资金流动桑基图（Sankey），呈现资金传导链条 | `CapitalFlowSankeyChart` |
| 行业热力矩形图谱 | 申万一级行业 Treemap 热力图，捕捉行业强度与轮动 | `ShenwanHeatmapChart`；`GET /heatmap` |
| 个股资金流拆解 | 超大单/大单/中单/小单明细 + 主力净占比 | `StockMoneyFlowTable` |
| 概念/行业标签查询 | 双向查询面板，快速定位主题机会 | `TagLookupPanel` |

**资金流数据模型**（`backend/services/api/market_analysis/money_flow.py`）：板块/个股维度主力资金流入/流出/净流向记录，存储于 `qm_sector_daily_metrics.details` JSON 字段（`money_flow` 键），支持桑基图所需结构，不新增数据表。

### 2.2 投研聚合平台（Research Platform）

**前端页面**：`electron/src/pages/ResearchPlatformPage.tsx`
**服务层**：`electron/src/services/researchService.ts`（`/api/v1/research/overview`）

- 全市场截面扫描与多因子组合筛选，覆盖维度：
  - **资金流向**（主力净流入区间过滤）
  - **风格因子**（β20、特质波动、价值、规模、市值排名）
  - **行业因子**（行业强度、相对动量、拥挤度、行业轮动速度）
  - **筹码因子**（获利盘 20/60/120、成本 90 宽）
  - **技术/量价因子**（ATR、KDJ、MACD、量比、OBV 等）
- 输出用于研判未来趋势的候选标的池。

### 2.3 高级分析服务

- `electron/src/services/advancedAnalysisService.ts`：高级分析接口封装，含直方图边界保留等数据修复（避免图表丢失 bin 边界）。

---

## 3. 功能二：13 种模型工场

### 3.1 模型清单

| 类别 | 模型 | 定位 |
| --- | --- | --- |
| 树模型 | LightGBM | 首选基线，IC 最稳定，3 分钟出基准 |
| 树模型 | XGBoost | 与 LGB 异构，Stacking 互补 |
| 树模型 | CatBoost | 原生支持类别特征，行业特征场景优势最大 |
| 树模型 | RandomForest | Bagging 对照基线，判断非线性价值 |
| 线性 | Ridge | 线性 sanity check，必跑诊断基线 |
| DL | GRU | DL 性价比最高，入门首选 |
| DL | LSTM | 长程记忆，实测提升有限 |
| DL | ALSTM | 注意力 LSTM，捕捉事件驱动行情 |
| DL | Transformer | 自注意力全局长程依赖，需大数据量 |
| DL | TabNet | 表格专用 DL，自带特征选择，吃扁平特征 |
| DL | TCN | 因果卷积，训练快 ~50%，捕捉波动率突变 |
| DL | NativeTFT | QuantMind 自研轻量 TFT（GRU 编码+注意力+门控残差） |
| DL | MLP | 神经网络基线，判断时序建模是否值得 |

### 3.2 模型训练流程能力

- **可视化训练配置**：`electron/src/pages/training/`（训练参数、特征选择、WFA 滚动切分）。
- **Optuna 自动化超参寻优**：自动搜索最优参数。
- **Stacking 多模型集成**：基模型 + 元学习器融合（`backend/services/engine/inference/model_loader.py` 支持 `is_ensemble` + `stacking`）。
- **算力调度**：本地 CPU/GPU；一键推送到 **AutoDL 远程 GPU 集群**（`backend/services/engine/training/remote_ssh_orchestrator.py`、`local_docker_orchestrator.py`）。
- **模型注册中心**：`backend/shared/model_registry.py`（`model_registry_service`），统一模型元数据、默认模型、策略绑定、启停/归档、集成模型注册。
- **推理引擎**：`backend/services/engine/inference/`（`model_loader.py` 支持 LightGBM/XGBoost/CatBoost/sklearn/PyTorch 多框架加载）。
- **训练/推理 API**：`backend/services/api/routers/model_training.py`（训练任务、模型 CRUD、默认模型、策略绑定、集成模型）。

---

## 4. 功能三：数据同步与自动更新

### 4.1 开源版数据更新客户端

**脚本**：`scripts/data/update_client.py`
**文档**：`docs/开源版数据更新客户端使用指南.md`

- **作用**：增量更新本地 `db/qlib_data`、`db/feature_snapshots`、`stock_daily_latest` 等离线数据，无需手动搬运全量包。
- **更新机制**（客户端不持 COS 密钥）：
  ```text
  客户端 -> X-Access-Key/X-Secret-Key 请求 quantmind-api
  -> 校验 API Key 与订阅 data_access 权限
  -> 返回短时效 COS signed URL
  -> 客户端下载更新包 -> sha256 校验 -> 解包到 db/ -> 记录已应用版本
  ```
- **幂等**：状态文件 `.quantmind_data_update_state.json` 记录已应用版本，重复运行自动跳过。
- **安全**：解包前校验 tar 成员路径，拒绝绝对路径与 `../`。
- **自动更新**：支持 crontab/launchd 定时任务，每日自动同步最新数据。
- **常用命令**：
  ```bash
  python scripts/data/update_client.py --dry-run        # 预览
  python scripts/data/update_client.py                  # 应用最新
  python scripts/data/update_client.py --version 20260504  # 补装指定版本
  ```

### 4.2 QuantDB 在线同步

- **入口**：登录系统后在【个人中心】➔【数据平台】填入 QuantDB API Key 一键绑定与在线同步。
- **脚本**：`backend/scripts/quantdb_daily_sync.py`（在线增量同步）；另有 `sync_quantdb_data.py`、`sync_stocks_from_quantdb.py`、北向资金相关 `quantdb_north_*.py` 等。
- **市场 Hub 适配**：`backend/services/engine/data_platform/` 按市场注册数据 Hub（CN/HK/US/CRYPTO/FUTURES），K 线查询统一走 `quantdb_hub.py -> QuantDBDataHub` 等。

---

## 5. 功能四：完整镜像与一键部署

**部署脚本**：`deploy/`（README 见 `deploy/README.md`）

| 方式 | 脚本 | 场景 |
| --- | --- | --- |
| 完整部署 | `full-deploy.sh` | 从 CDN 下载完整业务数据/模型/Qlib 数据包，开箱即用 |
| 在线源码部署 | `deploy.sh` | 新服务器可稳定访问代码与镜像仓库 |
| 一键更新 | `update.sh` | 已部署服务器更新代码与核心服务 |

**完整部署**（一条命令）：
```bash
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/full-deploy.sh | sudo bash
```
离线包内置：预构建镜像（`images.tar.zst`）、业务数据（`data-system.tar.zst`）、PostgreSQL 初始化备份（`postgres-all.sql.zst`）、QwenPaw 模型卷。

**部署完成后访问**：
- Web 控制台 `http://<IP>:3000`
- API 网关 `http://<IP>:8000`、Swagger `http://<IP>:8000/docs`
- 默认管理员 `admin` / `admin123`

**一键更新**：`sudo bash deploy/update.sh`（保留 PostgreSQL、Redis、`data/`、`models/`、`db/qlib_data/`，不丢失数据库与模型资产）。

**服务端口**：api 8000 / engine 8001 / trade 8002 / stream 8003 / data gateway 8004。

---

## 6. 功能五：模型广场（Model Hub）

### 6.1 定位

模型广场（Model Hub）是 **QuantDB 开放生态**的共享能力：OSS 后端通过统一网关代理远程 QuantDB 广场服务，前端提供完整的浏览、发布、下载体验。

**前端页面**：`electron/src/pages/ModelHubPage.tsx`
**服务层**：`electron/src/services/modelHubService.ts`
**API**：`/api/v1/hub/models`（本地网关失败时自动回退远程 QuantDB 网关）

### 6.2 核心能力

| 能力 | 说明 |
| --- | --- |
| 浏览与检索 | 按市场、算法、关键词、作者、排序（默认按 Sharpe）多维度筛选，分页浏览 |
| 一键下载导入 | 广场发现模型 → 下载模型包 → 一键导入本地验证（`ModelHubCard` 下载按钮） |
| 一键发布共享 | 选择本地已训练模型 → 上传模型文件 → 发布；模型元数据与回测指标同步至广场（`PublishModelModal`） |
| 可见性控制 | `public`（公开全社区可见）/ `unlisted`（凭分享码可见） |
| 社交互动 | 模型点赞 |
| 详情抽屉 | 模型详情、回测指标、导入操作（`ModelHubDetailDrawer`） |

### 6.3 发布流程（前端链路）

1. 选择本地模型（`listUserModels`）→ 2. 获取上传 ticket（`upload_url`）→ 3. 直传模型包 → 4. 调用 `publishModel` 激活发布 → 5. 广场可见。
- 入口：模型管理页（`electron/src/pages/ModelRegistryPage.tsx`）「发布到广场」按钮，或广场页「发布我的模型」。

### 6.4 后期规划

模型广场将持续完善，逐步开放模型**免费共享、下载与评价体系**，让优质策略模型在社区流动。

---

## 7. 相关模块索引（快速定位）

| 功能 | 后端 | 前端 |
| --- | --- | --- |
| 市场分析 | `backend/services/api/market_analysis/` | `electron/src/features/market-analysis/` |
| 投研聚合 | `backend/services/api/routers/research_service.py`、`research_features_service.py` | `electron/src/pages/ResearchPlatformPage.tsx` |
| 模型训练/推理 | `backend/services/api/routers/model_training.py`、`backend/services/engine/inference/` | `electron/src/pages/training/`、`electron/src/pages/ModelRegistryPage.tsx` |
| 模型注册中心 | `backend/shared/model_registry.py` | — |
| 数据同步客户端 | `scripts/data/update_client.py` | 【个人中心】➔【数据平台】 |
| 一键部署 | `deploy/full-deploy.sh`、`deploy.sh`、`update.sh` | — |
| 模型广场 | 网关代理 `/api/v1/hub/models` | `electron/src/pages/ModelHubPage.tsx` |

---

## 8. 免责声明

> 本系统产出的所有分析报告与交易信号均由 AI 算法自动生成，可能存在误差或失效风险；实际投资决策请结合自身风险承受能力或咨询合规专业机构。**股市有风险，入市需谨慎。**
