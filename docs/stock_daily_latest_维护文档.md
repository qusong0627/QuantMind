# stock_daily_latest 数据表维护文档

> 生成时间：2026-05-04
> 数据范围：2026-01-05 ~ 2026-04-30
> 总记录数：415,014 行
> 股票数量：5,208 只（已剔除B股、北交所股票）
> 股票简称填充率：100%
> 最新复权示例：SH600519 贵州茅台 2026-04-30 收盘价 12,014.32 / 复权因子 8.2297 = 实际价格 1,461.72 元

---

## 一、表结构概览

> 生产库 `public.stock_daily_latest` 已为全部 90 个字段写入 `COMMENT ON COLUMN` 注释；维护字段时请同步更新数据库注释与本文档。

| 序号 | 字段名 | 数据类型 | 说明 |
|------|--------|----------|------|
| 1 | trade_date | date | 交易日期，日频数据所属交易日 |
| 2 | symbol | varchar | 股票代码，统一使用后缀格式，如 `600000.SH`、`000001.SZ` |
| 3 | stock_name | text | 股票简称 |
| 4 | open | double | 开盘价，后复权价格口径 |
| 5 | high | double | 最高价，后复权价格口径 |
| 6 | low | double | 最低价，后复权价格口径 |
| 7 | close | double | 收盘价，后复权价格口径 |
| 8 | volume | double | 成交量，原始数据源口径 |
| 9 | amount | double | 成交额；投研接口按亿元展示，消费端需兼容元/亿元历史口径 |
| 10 | pct_change | double | 当日涨跌幅，百分比数值口径 |
| 11 | turnover_rate | double | 换手率，百分比数值口径 |
| 12 | pe_ttm | double | 市盈率 TTM |
| 13 | pb | double | 市净率 PB |
| 14 | total_mv | double | 总市值；投研接口按亿元展示，消费端需兼容元/亿元历史口径 |
| 15 | float_mv | double | 流通市值；投研接口按亿元展示，消费端需兼容元/亿元历史口径 |
| 16 | listed_days | integer | 上市天数 |
| 17 | is_st | smallint | ST 标记，1 表示 ST 或退市风险，0 表示非 ST |
| 18 | listing_market | varchar | 上市市场/板块 |
| 19 | industry | text | 所属行业名称 |
| 20 | province | text | 注册地或所属省份 |
| 21 | consecutive_limit_up_days | integer | 连续涨停天数 |
| 22 | limit_up_today | smallint | 当日涨停标记，1 表示涨停 |
| 23 | limit_down_today | smallint | 当日跌停标记，1 表示跌停 |
| 24 | return_1d | double | 近 1 个交易日收益率 |
| 25 | return_3d | double | 近 3 个交易日收益率 |
| 26 | return_5d | double | 近 5 个交易日收益率 |
| 27 | return_10d | double | 近 10 个交易日收益率 |
| 28 | return_20d | double | 近 20 个交易日收益率 |
| 29 | return_60d | double | 近 60 个交易日收益率 |
| 30 | ma5 | double | 5 日移动平均价，后复权价格口径 |
| 31 | ma10 | double | 10 日移动平均价，后复权价格口径 |
| 32 | ma20 | double | 20 日移动平均价，后复权价格口径 |
| 33 | ma60 | double | 60 日移动平均价，后复权价格口径 |
| 34 | ma_gap_5 | double | 收盘价相对 5 日均线偏离度，通常为小数比例 |
| 35 | ma_gap_10 | double | 收盘价相对 10 日均线偏离度，通常为小数比例 |
| 36 | ma_gap_20 | double | 收盘价相对 20 日均线偏离度，通常为小数比例 |
| 37 | rsi_6 | double | RSI 6 日指标，通常 0-100 |
| 38 | rsi_14 | double | RSI 14 日指标，通常 0-100 |
| 39 | kdj_k | double | KDJ 指标 K 值 |
| 40 | kdj_d | double | KDJ 指标 D 值 |
| 41 | kdj_j | double | KDJ 指标 J 值 |
| 42 | macd_dif | double | MACD DIF 线，使用标准 EMA 方法计算 |
| 43 | macd_dea | double | MACD DEA 线，使用标准 EMA 方法计算 |
| 44 | macd_hist | double | MACD 柱状值，DIF - DEA 的 2 倍 |
| 45 | vol_std_5 | double | 近 5 日波动率指标 |
| 46 | vol_std_20 | double | 近 20 日波动率指标 |
| 47 | vol_std_60 | double | 近 60 日波动率指标 |
| 48 | vol_atr_14 | double | 14 日 ATR 平均真实波幅 |
| 49 | volume_ratio_5 | double | 5 日量比 |
| 50 | volume_ratio_20 | double | 20 日量比 |
| 51 | volume_ma_5 | double | 5 日平均成交量 |
| 52 | amount_ma_5 | double | 5 日平均成交额 |
| 53 | bp | double | 账面市值比或 PB 倒数，依因子生成口径 |
| 54 | ep_ttm | double | 盈利收益率 TTM，通常为 PE(TTM) 倒数 |
| 55 | ln_mv_total | double | 总市值对数因子 |
| 56 | beta_20 | double | 20 日市场 Beta |
| 57 | label | double | 模型训练/评估标签（未来5日收益率） |
| 58 | ind_code_l1 | text | 一级行业代码 |
| 59 | ind_code_l2 | text | 二级行业代码 |
| 60 | micro_effective_spread | double | 微观结构有效价差指标 |
| 61 | micro_imbalance_volume | double | 微观结构成交量不平衡指标 |
| 62 | micro_jump_flag | smallint | 微观结构跳空/跳跃标记 |
| 63 | roe | double | 净资产收益率 ROE |
| 64 | volume_trend_3d | boolean | 3 日量能趋势标记，TRUE 表示量能增强 |
| 65 | adj_factor | double | 复权因子 |
| 66 | volume_ma_3 | double | 3 日平均成交量 |
| 67 | idx_all | integer | 指数筛选列：全市场标记 |
| 68 | idx_hs300 | integer | 指数筛选列：沪深300成分标记 |
| 69 | idx_zz500 | integer | 指数筛选列：中证500成分标记 |
| 70 | idx_zz1000 | integer | 指数筛选列：中证1000成分标记 |
| 71 | idx_margin | integer | 指数筛选列：两融标的标记 |
| 72 | concept_ai | integer | 概念标签列：AI 概念 |
| 73 | concept_chip | integer | 概念标签列：芯片概念 |
| 74 | concept_new_energy | integer | 概念标签列：新能源概念 |
| 75 | concept_ev | integer | 概念标签列：电动车概念 |
| 76 | concept_pv | integer | 概念标签列：光伏概念 |
| 77 | concept_lithium | integer | 概念标签列：锂电概念 |
| 78 | concept_semiconductor | integer | 概念标签列：半导体概念 |
| 79 | concept_military | integer | 概念标签列：军工概念 |
| 80 | concept_medical | integer | 概念标签列：医药概念 |
| 81 | concept_cyber | integer | 概念标签列：网络安全概念 |
| 82 | concept_fintech | integer | 概念标签列：金融科技概念 |
| 83 | concept_consumption | integer | 概念标签列：消费概念 |
| 84 | concept_real_estate | integer | 概念标签列：地产概念 |
| 85 | concept_infrastructure | integer | 概念标签列：基建概念 |
| 86 | concept_state_owned | integer | 概念标签列：国企改革概念 |
| 87 | main_flow | double | 主力净流入 |
| 88 | inst_ownership | double | 机构持股比例 |
| 89 | profit_growth | double | 利润增长率 |
| 90 | idx_chinext | integer | 指数筛选列：创业板指数成分标记；当前按测试口径默认填充为 1 |

**字段总数：90 个**（已移除 `return_120d`）

### 1.1 投研平台消费字段映射

投研平台 BFF（`backend/services/api/routers/research.py`）默认以 `stock_daily_latest` 作为行情与技术指标来源，并将生产库 snake_case 字段映射为前端 camelCase 字段。维护该表或投研接口时，应优先参考本表字段名，避免使用不存在的历史字段（如 `change_pct`、`sdl.pe`、`is_hs300`）。

| 前端字段 | stock_daily_latest 字段 | 说明 |
|----------|--------------------------|------|
| latestChange | pct_change | 当日涨跌幅，百分比数值口径 |
| turnoverRate | turnover_rate | 换手率 |
| amount | amount | 成交额，投研接口统一转换为亿元展示 |
| pe | pe_ttm | PE(TTM)，不要读取不存在的 `pe` 字段 |
| pb | pb | PB |
| roe | roe | 后端返回前会转换为百分比口径 |
| ma5 / ma10 | ma5 / ma10 | 移动均线 |
| maGap5 / maGap10 / maGap20 | ma_gap_5 / ma_gap_10 / ma_gap_20 | 均线乖离 |
| rsi / rsi14 | COALESCE(rsi_14, rsi_6) / rsi_14 | RSI 指标 |
| atr | vol_atr_14 | 14 日 ATR |
| macdHist | macd_hist | MACD 柱状值 |
| volRatio5 / volRatio20 | volume_ratio_5 / volume_ratio_20 | 量比 |
| return1d / return3d | return_1d / return_3d | 近期收益率；库内为小数比例，投研接口返回前转换为百分比数值 |
| nextDayReturn / day3Return | return_1d / return_3d | 投研候选表格兼容字段，用于“次日收益/3日收益”列；返回百分比数值 |
| totalMv / floatMv | total_mv / float_mv | 市值，投研接口统一转换为亿元展示 |
| listedDays | listed_days | 上市天数 |
| volumeTrend3d | volume_trend_3d | 3 日量能趋势 |
| mainFlow / instOwnership / profitGrowth | main_flow / inst_ownership / profit_growth | 资金与财务扩展字段，当前填充率可能较低 |
| indexTags | idx_hs300 / idx_zz500 / idx_zz1000 / idx_chinext / idx_margin / idx_all | 后端聚合为标签数组 |
| conceptTags | concept_ai ~ concept_state_owned | 后端聚合为标签数组 |

---

## 二、数据填充率明细（2026-05-04 更新）

### 2.1 填充率 100% (28 个字段) ✅

| 类别 | 字段列表 |
|------|----------|
| 主键/名称 | trade_date, symbol, stock_name |
| 基础行情 | open, high, low, close, volume |
| 均线系统 | ma5, ma10, ma20, ma60 |
| 均线偏离度 | ma_gap_5, ma_gap_10, ma_gap_20 |
| MACD | macd_dif, macd_dea, macd_hist (已修正为标准 EMA 方法) |
| 均量/均额 | volume_ma_3, volume_ma_5, amount_ma_5 |
| 涨跌停 | limit_up_today, consecutive_limit_up_days |
| 其他 | listing_market, adj_factor, vol_atr_14, idx_all, idx_hs300, idx_zz500, idx_zz1000, idx_margin, idx_chinext |
| 概念标签 | concept_ai ~ concept_state_owned (16个) |

### 2.2 填充率 95%~99.99% (11 个字段)

| 字段 | 填充率 | 说明 |
|------|--------|------|
| amount | 99.83% | 成交额 |
| float_mv, turnover_rate | 99.83% | 流通市值/换手率 |
| total_mv | 99.78% | 总市值 |
| bp | 99.39% | 账面市值比 |
| volume_ratio_5 | 99.93% | 5日量比 |
| volume_ratio_20 | 99.98% | 20日量比 |
| amount_ma_5 | 99.93% | 5日均额 |
| kdj_k, kdj_d, kdj_j | 99.98% | KDJ指标 |
| pct_change, return_1d | 98.70% | 涨跌幅/1日收益率 |

### 2.3 填充率 90%~95% (7 个字段)

| 字段 | 填充率 | 说明 |
|------|--------|------|
| rsi_6, rsi_14 | 96.69% | RSI指标 |
| vol_std_5/20/60, beta_20 | 97.39% | 波动率/Beta |
| return_3d | 96.09% | 3日收益率 |
| return_5d, label | 93.49% | 5日收益率/目标变量 |
| return_10d | 86.98% | 10日收益率 |

### 2.4 填充率 < 90% (10 个字段) ⚠️

| 字段 | 填充率 | 缺口原因 |
|------|--------|----------|
| return_20d | 73.97% | 需要20天历史数据 |
| pe_ttm | 73.34% | CSMAR估值数据覆盖不全 |
| ep_ttm | 73.34% | EP计算依赖PE数据 |
| micro_jump_flag | 94.81% | 微观结构数据缺失 |
| micro_imbalance_volume, micro_effective_spread | 94.46% | 微观结构数据缺失 |
| listed_days, province | 71.27% | 公司信息缺失 |
| industry | 100% | 行业信息完整 |
| return_60d | 22.02% | 需要60天历史数据，新股不足 |
| volume_trend_3d | 98.70% | 3日量能趋势 |
| is_st | 0% | 未填充 |
| limit_down_today | 0% | 未填充 |
| ln_mv_total | 99.78% | 总市值对数 |
| roe | 26.87% | 财务数据缺失 |
| main_flow | 0% | 主力资金流向未填充 |
| inst_ownership | 0% | 机构持股比例未填充 |
| profit_growth | 0% | 利润增长率未填充 |

---

## 三、数据缺口分析

### 3.1 缺口原因汇总

| 原因分类 | 影响字段 | 影响程度 |
|----------|----------|----------|
| 历史数据不足 | return_60d (22%), return_20d (74%) | 中等 |
| CSMAR数据源限制 | turnover_rate, is_st | 严重 |
| 财务数据缺失 | roe (27%), profit_growth (0%) | 严重 |
| 估值数据覆盖不全 | pe_ttm (73%), ep_ttm (73%) | 中等 |
| 公司信息缺失 | listed_days, province (71%) | 中等 |
| 资金流向数据 | main_flow, inst_ownership (0%) | 严重 |

### 3.2 CSMAR数据源说明

CSMAR（中国股票市场会计研究数据库）当前提供的数据存在以下限制：

1. **换手率/ST标识**：仅覆盖 2026 年数据
2. **公司基本信息**：约 1,299 只股票的 industry/province 信息缺失
3. **估值指标**：PE/PB 数据覆盖不完整
4. **财务数据**：ROE、利润增长率等财务指标缺失严重

---

## 四、指标补全工具

### 4.1 补全脚本

项目提供自动化指标补全脚本：`scripts/data/processing/backfill_stock_daily_latest.py`

**功能**：
- 从数据库拉取历史数据到本地
- 计算缺失的技术指标（均线/RSI/KDJ/MACD/收益率等）
- 批量同步回数据库

**使用方法**：

```bash
# 全量重算所有指标
DATABASE_URL="postgresql://user:pass@host:5432/db" \
python scripts/data/processing/backfill_stock_daily_latest.py --mode full

# 仅补全 P0 核心字段（均线/量能/衍生字段）
python scripts/data/processing/backfill_stock_daily_latest.py --mode p0

# 仅补全 P1 技术指标（RSI/KDJ/MACD等）
python scripts/data/processing/backfill_stock_daily_latest.py --mode p1

# 仅补全 P2 收益率序列（return_1d~60d）
python scripts/data/processing/backfill_stock_daily_latest.py --mode p2

# Dry-run 模式（预览不写入）
python scripts/data/processing/backfill_stock_daily_latest.py --mode full --dry-run
```

**补全效果**：

| 指标类别 | 补全前 | 补全后 |
|---------|--------|--------|
| 均线系统 (ma5/10/20/60) | 0% | 100% ✅ |
| MACD (dif/dea/hist) | 95% | 100% ✅ |
| 均线偏离度 (ma_gap) | 95% | 100% ✅ |
| 量能指标 (volume_ma) | 95% | 100% ✅ |
| KDJ (k/d/j) | 95% | 99.98% ✅ |
| RSI (6/14) | 90% | 96.69% ✅ |
| 收益率 (return_1d~60d) | 22-99% | 22-99%* |

> *收益率字段缺失主要因历史数据不足（仅77个交易日），新股无法计算长周期收益率。

### 4.2 验证脚本

```bash
# 验证补全效果
DATABASE_URL="postgresql://user:pass@host:5432/db" \
python scripts/data/processing/verify_backfill.py
```

---

## 五、维护建议

### 5.1 短期优化

- [x] **补全技术指标**：使用 backfill 脚本补全均线/MACD/KDJ/RSI 等指标 ✅
- [ ] **补充历史数据**：导入更早期的行情数据，解决 return_60d 填充率低的问题
- [ ] **完善CSMAR数据**：联系CSMAR获取完整的历史换手率和ST标识数据
- [ ] **补全公司信息**：通过其他数据源（如tushare、akshare）补全缺失的 industry/province

### 5.2 中期优化

- [ ] **增加数据源**：引入东方财富、同花顺等数据源作为补充
- [ ] **优化计算逻辑**：对新股/次新股的技术指标计算逻辑进行优化
- [ ] **建立数据校验**：每日任务增加填充率监控告警
- [ ] **补全资金流向**：接入主力资金流向、机构持股等数据

### 5.3 长期规划

- [ ] **构建数据仓库**：整合多个数据源，建立统一的数据清洗流程
- [ ] **回溯填充机制**：定期执行回溯任务，补充历史缺口数据
- [ ] **建立质量报告**：自动生成每日/每周数据质量报告

---

## 六、数据更新日志

| 日期 | 操作 | 说明 |
|------|------|------|
| 2026-05-04 | 股票简称补全 | 从 akshare 拉取最新股票快照，补全股票简称至 100% |
| 2026-05-04 | 清理无效数据 | 删除 B股（78只）、北交所（312只）数据，共 14,346 条记录 |
| 2026-05-04 | MACD 算法修正 | 将 MACD 指标从简化 SMA 方法修正为标准 EMA 方法，更新 211,620 条记录 |
| 2026-05-04 | 技术指标重算 | 从 DuckDB 重新导出完整 OHLCV 数据（含正确复权因子），重算并更新 2026 年全部技术指标 |
| 2026-05-04 | 数据导出 | 导出 stock_daily_latest 表为 CSV 压缩文件 |
| 2026-05-02 | 移除 return_120d | 删除实用性不大的120日收益率字段 |
| 2026-05-02 | 指标补全 | 使用 backfill 脚本补全 399,244 条记录的技术指标 |
| 2026-05-02 | 字段注释补全 | 为生产库 `stock_daily_latest` 全部 90 个字段写入数据库注释 |
| 2026-05-02 | 指数列扩展 | 新增 `idx_chinext` 创业板指数标记 |
| 2026-05-01 | 表重建 | 重建 stock_daily_latest 表并填充数据 |

---

## 七、数据库连接信息

```
Host: <your_database_host>
Port: 5432
Database: quantmind
User: <your_database_user>
Table: stock_daily_latest
```

---

## 八、快速查询

```sql
-- 查看总数据量
SELECT COUNT(*) as total,
       COUNT(DISTINCT symbol) as stocks,
       MIN(trade_date) as start_dt,
       MAX(trade_date) as end_dt
FROM stock_daily_latest;

-- 查询实际前复权价格（示例：贵州茅台）
SELECT symbol, stock_name, trade_date, close, adj_factor,
       ROUND(close / adj_factor, 2) as actual_close
FROM stock_daily_latest
WHERE symbol = 'SH600519'
ORDER BY trade_date DESC LIMIT 5;

-- 查看各字段填充率
SELECT 
    column_name,
    COUNT(*) FILTER (WHERE column_name IS NOT NULL) as non_null,
    COUNT(*) as total,
    ROUND(100.0 * COUNT(*) FILTER (WHERE column_name IS NOT NULL) / COUNT(*), 2) as fill_rate
FROM stock_daily_latest
GROUP BY column_name
ORDER BY fill_rate DESC;

-- 查看字段注释覆盖情况
SELECT
  COUNT(*) AS total_columns,
  COUNT(pgd.description) AS commented_columns,
  COUNT(*) FILTER (WHERE pgd.description IS NULL OR btrim(pgd.description) = '') AS missing_comments
FROM information_schema.columns c
LEFT JOIN pg_catalog.pg_class cls ON cls.relname = c.table_name
LEFT JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace AND ns.nspname = c.table_schema
LEFT JOIN pg_catalog.pg_description pgd ON pgd.objoid = cls.oid AND pgd.objsubid = c.ordinal_position
WHERE c.table_schema = 'public'
  AND c.table_name = 'stock_daily_latest';
```
