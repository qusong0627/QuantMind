# QuantMind 数据文件使用说明

本目录包含 QuantMind 投研平台所需的全部离线数据文件，部署前请按以下说明完成数据准备。

---

## 目录结构

```
db/
├── feature_snapshots/    # 年度特征快照（2016-2026）
├── qlib_data/            # Qlib 回测数据（日历/因子/标的）
├── custom/               # 核心对齐数据集 (Single Source of Truth)
│   └── fundamental_aligned.parquet  # 投研/实盘统一对齐产物
├── backups/              # 数据库备份
└── README.md
```

---

## 1. custom/ — 投研对齐产物 (Single Source of Truth)

**内容**：`fundamental_aligned.parquet` 是 QuantMind 的核心高维数据集。它深度整合了 2016-2026 年全市场所有标的的行情（OHLCV）、89 维深度因子（估值、质量、技术指标）、资金流向、微观结构以及行业概念标签。

**核心价值**：
- **快捷带入参数**：专为 Qlib 回测和实盘策略设计。用户在编写策略逻辑时，无需自行编写复杂的特征计算代码或进行多表关联（Join），直接调用数据即可。
- **开箱即用**：所有关键字段（如 `pe_ttm`, `roe`, `rsi_14`, `main_flow`, `idx_hs300` 等）均已预计算完成并进行了严格的时间轴对齐。
- **高性能查询**：采用 Parquet 列式存储，支持分钟级的全量数据读取与过滤，极大地降低了量化研究的门槛。

---

## 2. feature_snapshots/ — 特征快照

**内容**：2016-2026 年每年的模型特征 Parquet 文件 + 元数据 JSON，共 152 维特征，主要用于 AI 模型的训练与推理。

---

## 3. qlib_data/ — Qlib 回测数据

**内容**：Qlib 原生二进制格式数据，供 Qlib 引擎直接调用进行回测计算。主要包含 `calendars/` (交易日历) 和 `features/` (基础 OHLCV 二进制)。

**复权方式**：
- **后复权价格**：用于计算收益率、技术指标等，确保价格序列连续可比
- **不复权价格**：用于获取实际成交价格，反映真实交易成本

> **说明**：后复权价格适合策略回测中的收益计算，不复权价格适合模拟实盘交易时的下单价格。

---

## 4. backups/ — 数据库备份与导入

### 4.1 核心备份文件
- `stock_daily_latest.sql`: 最新导出的 PostgreSQL 备份文件，包含 `stock_daily_latest` 表结构与全量数据。

### 4.2 导入方法 (PostgreSQL)

#### 方法 A：使用导出的 SQL 脚本还原
如果您需要通过新导出的 `.sql` 文件恢复数据，可直接执行：
```bash
docker exec -i quantmind-db psql -U quantmind -d quantmind < db/backups/stock_daily_latest.sql
```

#### 方法 B：自动化同步脚本
直接运行项目内置的同步工具，无需手动操作 CSV：
```bash
python scripts/sync_parquet_to_pg.py
```
> **说明**：该脚本会自动读取 `db/custom/fundamental_aligned.parquet` 并原子性地更新数据库中的 `stock_daily_latest` 表。

---

## 数据逻辑结构

```mermaid
graph TD
    A[CSMAR/DuckDB/AkShare] --> B(fundamental_aligned.parquet)
    B --> |参数带入| C[Qlib 回测引擎]
    B --> |秒级过滤| D[实盘交易引擎]
    B --> |增量更新| E[PostgreSQL stock_daily_latest]
    B --> |转换| F[qlib_data 二进制]
```

**重要提醒**：`fundamental_aligned.parquet` 是整个平台的“数据心脏”，在进行任何策略研发前，请务必确保该文件已通过 `sync_qlib_from_parquet.py` 等脚本完成最新日期的同步。
