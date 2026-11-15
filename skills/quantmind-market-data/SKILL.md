---
name: quantmind-market-data
description: "QuantMind 多市场数据地图 — 回答「数据在哪 / 怎么同步 / 为什么查不到 / 用哪个数据集」时先读本技能。覆盖 A股 QuantDB、港股 QuantHK、美股 QuantUS、区块链 QuantBC、期货 QuantFutures、用户自传 QuantCustom 六个数据中枢：宿主机/容器目录、环境变量、同步脚本、目录结构、代码格式、读取入口、UI 入口与常见坑，并指向 quantdb-data-structure / quantdb-fields / quantdb-sdk 等深技能。触发词：数据在哪、数据目录、数据平台、数据同步、初始化数据、QuantDB、QuantUS、QuantHK、QuantBC、QuantFutures、QuantCustom、parquet、dt 分区、美股数据、港股数据、期货数据、加密数据、查不到数据、数据没更新"
---

# QuantMind 多市场数据地图

> 面向 **QuantBot / AI 助手**：先确认「哪个市场、哪类数据、什么代码格式」，再给目录/脚本/读取入口。
> 数据目录的细节口径以 [[quantdb-data-structure]] 与 [[quantdb-fields]] 为准；本技能负责**跨市场总览与分流**。

## 一、六个数据中枢总表

| 市场 | 数据中枢 | 环境变量（容器内默认） | 宿主机目录 | 同步脚本 | 读取入口 |
|---|---|---|---|---|---|
| **A 股** | QuantDB | `QM_QUANTDB_DATA_DIR=/data/quantdb` | `data/quantdb/` | `backend/scripts/quantdb_daily_sync.py` | `quantdb_hub.py` |
| **港股** | QuantHK | `QM_QUANTHK_DATA_DIR=/data/quanthk` | `data/quanthk/` | `backend/scripts/quanthk_daily_sync.py`（CCASS/南向另有 `quanthk_ccass_sync.py`、`quanthk_south_sync.py`） | `quanthk_hub.py` |
| **美股** | QuantUS | `QM_QUANTUS_DATA_DIR=/data/quantus` | `data/quantus/` | `backend/scripts/quantus_daily_sync.py` | `quantus_hub.py` |
| **区块链** | QuantBC | `QM_QUANTBC_DATA_DIR=/data/quantbc` | `data/quantbc/` | `backend/scripts/quantbc_daily_sync.py` | `quantbc_hub.py` |
| **期货/贵金属** | QuantFutures | `QM_QUANTFUTURES_DATA_DIR=/data/quantfutures` | `data/quantfutures/` | `backend/scripts/quantfutures_daily_sync.py` | `quantfutures_hub.py` |
| **用户自传/挖掘产出** | QuantCustom | `QM_QUANTCUSTOM_DATA_DIR=/data/quantcustom` | `data/quantcustom/` | 前端上传 / 因子工厂 / 物化流水线 | `QuantDBFactorReader(mode="CUSTOM")` |

> 所有 hub 都在 `backend/services/engine/data_platform/`；容器内路径统一 `/data/<市场>`，宿主机是仓库 `data/<市场>/`。
> 新增市场或自定义数据集时，不要绕过 hub 手拼 parquet 路径。

## 二、UI 入口（用户问「在哪点」）

| 操作 | 路径 |
|---|---|
| 配置 QuantDB API Key / 触发在线同步 | **个人中心 → 数据平台** |
| 按市场初始化 / 查看同步状态 / 补历史 | **后台管理 → 数据管理**：QuantDB A股 / QuantUS 美股 / QuantHK 港股 / QuantBC 区块链 / QuantFutures 期货 |
| ModelScope 全量离线数据（约 56GB） | 后台管理 → 数据管理 → **初始化数据**（数据集 `qusong0627/LightGBM_Alpha300`） |
| 数据健康/字段/覆盖 | 后台管理 → 数据管理各 tab（状态、最新分区、失败原因） |

CLI 增量同步（容器内执行，默认最新交易日）：

```bash
docker exec quantmind python backend/scripts/quantdb_daily_sync.py
docker exec quantmind python backend/scripts/quanthk_daily_sync.py
docker exec quantmind python backend/scripts/quantus_daily_sync.py
docker exec quantmind python backend/scripts/quantbc_daily_sync.py
docker exec quantmind python backend/scripts/quantfutures_daily_sync.py
```

> 长任务：先告诉用户「已启动」，轮询状态/日志，不要干等；同步失败要报告具体市场与错误，不要假装成功。

## 三、目录结构（各市场基本同构）

### 3.1 A 股 QuantDB（最完整，其他市场可参照）

| 目录 | 内容 |
|---|---|
| `1_kline_data/` | 日K（前复权/后复权/不复权）、指数、1分钟/5分钟、tick |
| `2_base_sector/` | 标的详情、行业/概念、交易日历、指数权重、融资融券、北向 |
| `3_financial_data/` | 资产负债表 / 利润表 / 现金流 / 股本 / 分红 |
| `4_bond_etf/` | 债券、ETF |
| `5_technical_derived/` | 估值、技术指标、市场情绪 |
| `6_ml_datasets/` | `features_daily`、`l1_factors`、`l2_factors`、`l1_l2_factors`、`alpha_library` |
| `9_hithink_fuyao/` | 同花顺/问财补充数据（按市场情况） |
| `factor_research/`、`factor_research_private/` | 因子研究注册与私有产物 |
| `quantdb_sync.sqlite`、`.sync_state/` | 增量同步状态，**不要删** |

分区规律：`<数据集>/dt=YYYYMMDD/data.parquet`，`dt` 是**整数**。
单文件型：`{symbol}.parquet`（财务、分钟K）。细节见 [[quantdb-data-structure]]。

### 3.2 港股 / 美股 / 期货 / 区块链

| 市场 | 顶层目录（实测） | 备注 |
|---|---|---|
| QuantHK | `1_kline_data/`、`2_base_sector/`、`3_financial_data/`、`4_analyst/`、`5_technical_derived/`、`6_ml_datasets/`、`factor_research_private/` | 另有 CCASS、南向、港交所事件等专用脚本 |
| QuantUS | `1_kline_data/`、`2_base_sector/`、`3_financial_data/`、`4_analyst/`、`4_options/`、`5_technical_derived/`、`6_ml_datasets/`、`factor_research_private/` | Yahoo Finance 摄取；含分析师与期权数据 |
| QuantFutures | `1_kline_data/`、`2_base_sector/`、`6_ml_datasets/`、`9_hithink_fuyao/`、`factor_research_private/` | akshare 摄取，期货/贵金属 |
| QuantBC | `1_kline_data/`、`5_technical_derived/`、`6_ml_datasets/` | Binance 摄取；生产默认 `ENABLE_CRYPTO=false`，市场入口隐藏 |

## 四、代码格式（跨市场最高频踩坑）

| 市场 | 规范格式 | 示例 | 说明 |
|---|---|---|---|
| A 股 parquet | 后缀式 | `600036.SH`、`000001.SZ` | Qlib 桥接用小写 `sh600036`；PG/前端用前缀式 `SH600036`，必须经 `StockCodeUtil` 转换 |
| 港股 parquet | 4 位 + `.HK`（8 开头真 5 位保留） | `0700.HK`、`2057.HK`、`80001.HK` | 统一走 `StockCodeUtil.to_hk_suffix()`；`00700`/`700.HK`/`02057.HK` 都会被归一 |
| 美股 | 原生 ticker | `AAPL`、`MSFT`；指数 `IXIC.US` | 不做前后缀转换 |
| 期货/加密 | 数据源原生合约/交易对 | 以对应 hub/同步脚本为准 | 用 `quantfutures_hub.py` / `quantbc_hub.py` 读取，不要手写路径 |

> 前缀式代码直接查 parquet 会**静默返回空、不报错**——这是最危险的坑。查不到先核对格式。

## 五、快速体检命令

```bash
# 各市场体量
docker exec quantmind du -sh /data/quantdb /data/quanthk /data/quantus /data/quantfutures /data/quantbc

# 最新分区（以 A 股日K为例；其他市场换目录）
docker exec quantmind sh -c 'ls -d /data/quantdb/1_kline_data/daily_forward/dt=* | tail -1'

# 某市场数据集清单
docker exec quantmind ls /data/quanthk/6_ml_datasets
docker exec quantmind ls /data/quantus/6_ml_datasets
```

数据是否「新鲜」：看最新 `dt` 分区与交易日历；行情新鲜度分级统一谓词在 `backend/shared/freshness.py`（fresh/stale/unavailable）。
**不要用「文件存在」代替「最新分区是今天」**。

## 六、训练与因子链路里的数据入口

| 场景 | 用什么 |
|---|---|
| 模型训练选特征源 | 模型训练页「直读因子」→ `6_ml_datasets/l1_factors`、`l1_l2_factors`、`alpha_library` 等 |
| 因子挖掘产物落盘 | `data/quantcustom/6_ml_datasets/<数据集>/`（**不要写 quantdb**） |
| 因子研究/物化 | [[factor-materialize-catalog]]、[[factor-train-pipeline]] |
| 行情回测 | 回测中心 → 行情回测（A股/港股/美股/期货/加密；日线/5分钟/1分钟；无分钟数据自动回落日线） |
| 实时行情 | 不是本技能的 parquet 数据；见 [[realtime-quotes-tdx]] / [[tdx-aidata-ops]] |

## 七、常见问题

| 现象 | 优先排查 |
|---|---|
| 查询返回空但不报错 | 代码格式（前缀 vs 后缀、港股 `.HK` 归一、美股 ticker）；`dt` 是否字符串 |
| 最新日期不是今天 | 同步未跑/失败；看后台数据管理状态与同步日志；不要拿旧分区当今日 |
| 模型训练缺数据 | 训练数据集是否已发布/物化；`6_ml_datasets` 是否有对应 `dt` 分区 |
| 港股 CCASS/南向缺失 | 对应专用脚本（`quanthk_ccass_sync.py`、`quanthk_south_sync.py`），不是主日线同步 |
| 加密市场看不到 | 生产默认 `ENABLE_CRYPTO=false`；需显式开启 |
| 自定义因子没进训练 | 物化 → 字段发现 → 发布（[[factor-materialize-catalog]]），只在 `quantcustom` 落盘不等于已发布 |

## 八、相关技能

- [[quantdb-data-structure]] — 目录/分区/路径/quantdb_hub 细节
- [[quantdb-fields]] — 字段单位与口径陷阱（成交量、成交额、L2、估值）
- [[quantdb-sdk]] — QuantDB API Key、远程 SDK、数据集预览
- [[factor-materialize-catalog]] — 因子物化、字段发现、发布、跨库训练
- [[factor-train-pipeline]] — 从因子库到训练载荷
- [[model-inference-backfill]] — 推理覆盖与补推
- [[quantmind-platform-map]] — 平台模块路径与角色
