---
name: quantdb-data-structure
description: "QuantDB 服务器数据结构与读取口径 — 数据目录组织（1_kline_data~6_ml_datasets）、Hive 分区规律（dt=YYYYMMDD 整数）、单文件 {symbol}.parquet、parquet 后缀式代码 600519.SH 与 PG 前缀式 SH600519 的转换口径、quantdb_hub.py 单一读取入口与 DuckDB 视图清单、服务器路径映射（/opt/quantmind/data/quantdb → 容器 /data/quantdb）。凡需要读写/探查/补数/验证 QuantDB 本地 parquet 数据、查目录结构、写 DuckDB 查询、排查查不到数据问题时使用。触发词：quantdb 结构、数据目录、dt 分区、hive 分区、parquet 路径、数据在哪里、600519.SH、代码格式、qdb_ 视图、quantdb_hub、增量同步、数据缺失排查"
---

> ⚙️ 本技能遵循公共运行环境契约（最高优先级，先于本文其余内容执行）：
> 详见 [_shared/env-contract.md](../_shared/env-contract.md)，执行前先读它。

# quantdb-data-structure — QuantDB 数据结构与读取口径

> 写任何触碰 QuantDB 本地数据的代码/查询前必读。字段单位与口径陷阱另见
> `skills/quantdb-fields/SKILL.md`（本技能管「数据在哪、怎么组织、怎么读」）。

## 一、路径映射（先定位数据）

| 位置 | 路径 |
|---|---|
| 生产服务器宿主机 | `/opt/quantmind/data/quantdb` |
| 容器内（`./data:/data` bind mount） | `/data/quantdb` |
| 本地仓库 | `<项目根>/data/quantdb` |
| **用户自定义数据集（产出区）** | `/data/quantcustom`（宿主机 `/opt/quantmind/data/quantcustom`） |

> ⚠️ `quantdb/` 是**官方只读数据**；`quantcustom/` 是**用户/挖掘产出**（因子挖掘、因子工厂的落盘区，`QM_QUANTCUSTOM_DATA_DIR`）。两者结构同为 6 大类，但写入一律进 `quantcustom`，**不要污染 quantdb**。

数据目录解析优先级（`quantdb_hub.py._resolve_data_dir`）：
环境变量 `QM_QUANTDB_DATA_DIR` → `/data/quantdb` → `/app/data/quantdb` → `D:/quant_data` → 项目根 `data/quantdb`。

## 二、顶层数据集目录

| 目录 | 内容 | 组织方式 |
|---|---|---|
| `1_kline_data/` | 日K（daily_forward/backward/unadjusted 三种复权）、index_daily、min1_kline、min5_kline、tick | 日K/指数=分区；分钟线=单文件 |
| `2_base_sector/` | instrument_detail、sector_concept、trading_calendar、index_weights、margin_trading、hsgt_north | 混合 |
| `3_financial_data/` | balance/income/cashflow/股本/分红因子等财务报表 | 单文件为主 |
| `4_bond_etf/` | 债券 / ETF | 单文件 |
| `5_technical_derived/` | valuation（估值）、technical_indicators、market_sentiment | 分区 |
| `6_ml_datasets/` | features_daily、l1_factors、l1_l2_factors、alpha_library（Alpha101+GTJA191+Alpha158 三库因子） | 分区 |

辅助文件：`releases/`（数据包版本）、`.sync_state` / `quantdb_sync.sqlite`（增量同步状态）、`.qlib_cache`、`_meta`。

### 用户自定义数据集 `quantcustom/6_ml_datasets/`

挖掘产物统一落 `data/quantcustom/6_ml_datasets/<数据集>/`（结构与 `quantdb` 一致，按 `dt=YYYYMMDD/` 分区）：

| 数据集 | 来源 | 附加文件 |
|---|---|---|
| `l1_factors` | ① RD-Agent 因子 `export` ② **因子工厂**（`backend/scripts/factor_factory.py`） | `MANIFEST.csv`（factor_name/expression/ic/icir/coverage/kept）、`PROPOSALS.json` |

- 读取入口：`QuantDBFactorReader(mode="CUSTOM")`（`QM_QUANTCUSTOM_DATA_DIR`，默认 `/data/quantcustom`）。
- 只读展示：`GET /api/v1/alpha-agent/factory-factors`（读 `l1_factors/MANIFEST.csv`，只回显不回测）。
- 历史补全/落库路径：`backend/services/api/routers/admin/alpha_factor_pipeline.py`。

## 三、文件组织规律（决定查询写法）

1. **分区型**：`<数据集>/dt=YYYYMMDD/data.parquet`。`dt` 是 Hive 分区列，**整数**（如 `20260828`），DuckDB 过滤 `WHERE dt BETWEEN 20260101 AND 20260828` 可走谓词下推，**不要写字符串**。
2. **单文件型**：`<数据集>/{symbol}.parquet`（财务报表、分钟K）或整表单文件（`instrument_detail.parquet`），用 `pd.read_parquet` 直读。
3. **混合格式**：`6_ml_datasets/l1_factors/` 同时存在平铺 `l1_factors_YYYYMMDD.parquet` 与 `dt=YYYYMMDD/` 分区——只读 `dt=*` 分区目录，避免混入平铺文件。
4. **北向资金特殊**：`2_base_sector/hsgt_north/` 日频在 `daily_freq/*.parquet`（无分区），季度快照用 `quarter=YYYYQN` Hive 分区（2024-08 起季度披露）。

## 四、代码格式口径（最高频踩坑点）

| 存储位置 | 格式 | 示例 |
|---|---|---|
| QuantDB parquet 的 `symbol`/`wind_code` | **后缀式** | `600519.SH`、`000001.SZ` |
| PG 表 `stock_daily_latest` 等内部表 | **前缀式** | `SH600519`、`SZ000001` |

- 查 QuantDB parquet 前必须转换：后端用 `backend/shared/stock_utils.py` 的 `StockCodeUtil.to_suffix(code)`；前端 `normalizeStockCode`。
- **反面教训**：把前缀式代码原样传进 parquet 查询会**静默返回空、不报错**，快路径还会悄悄跌入兜底数据源（复权口径随之失效）。写完新链路必须实测两种复权参数下首/末根数值真正分化，并核对响应的 `source_used` 字段。

## 五、读取入口与 DuckDB 视图清单

**唯一推荐入口**：`backend/services/engine/data_platform/quantdb_hub.py`（QuantDBDataHub）——懒加载、线程安全、自动做列名映射（`time→trade_date`、`wind_code→symbol`、`volinstock/vol_in_stock→volume`）。不要绕过它自己拼 parquet 路径，除非做数据巡检。

分区数据集挂载的 DuckDB 视图（`hive_partitioning=1, union_by_name=true`）：

| 视图 | 数据 |
|---|---|
| `qdb_daily_forward` / `qdb_daily_backward` / `qdb_daily_unadjusted` | 前复权 / 后复权 / 不复权日K |
| `qdb_index_daily` | 指数日K |
| `qdb_valuation` | 估值 |
| `qdb_technical_indicators` | 技术指标 |
| `qdb_market_sentiment` | 市场情绪 |
| `qdb_features_daily` | 每日特征 |
| `qdb_margin_trading` | 融资融券 |
| `qdb_l1_l2_factors` | L1+L2 因子 |
| `qdb_l1_factors` | L1 因子（仅当存在 `dt=*` 分区时挂载） |
| `qdb_alpha_library` | 三库因子 429 列（训练直读） |
| `qdb_hsgt_north_daily` / `qdb_hsgt_north` | 北向资金日频 / 季度 |

临时探查可直接 DuckDB 查文件：

```sql
SELECT * FROM read_parquet('/data/quantdb/1_kline_data/daily_forward/dt=20260828/data.parquet')
WHERE symbol = '600519.SH';
```

## 六、服务器核查命令速查

```bash
# 目录与体量
ls /opt/quantmind/data/quantdb && du -sh /opt/quantmind/data/quantdb/*

# 容器内可见性
docker exec quantmind ls /data/quantdb

# 某交易日数据是否到位（以日K为例）
ls /opt/quantmind/data/quantdb/1_kline_data/daily_forward/dt=20260828/
```

数据更新后服务未感知时：`docker compose restart quantmind celery-worker`（数据走 bind mount，无需重建镜像）。增量同步需先在【个人中心】→【数据平台】绑定 `QUANTDB_API_KEY` 后 `docker exec` 触发。

## 七、写代码前的自查清单

- [ ] 用的是 `QM_QUANTDB_DATA_DIR`/默认目录解析，而不是写死路径？
- [ ] 分区过滤用的是整数 `dt`，范围合理（不跨年全表扫）？
- [ ] 查 parquet 的代码已转后缀式？查 PG 内部表保持前缀式？
- [ ] 查询结果为空时验证过不是「格式错配静默查空」，而是真的无数据？
- [ ] 涉及字段单位（成交量/成交额/市值/股息率）时已对照 `quantdb-fields` 技能？
