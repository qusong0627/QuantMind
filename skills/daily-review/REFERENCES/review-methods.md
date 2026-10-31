# daily-review 计算口径细则（REFERENCES）

本文档定义 daily_review.py 各统计项的确切口径。改脚本时必须同步更新这里。

## 1. 涨跌停判定（核心口径）

规则源：`backend/services/trade/simulation/services/local_market_data.py` 的
`compute_limits(symbol, pre_close, is_st, trade_date)` —— 本 skill **只允许复用，禁止另写一份**。

- **涨停价/跌停价**：由昨日收盘价（不复权）+ 板块涨跌幅限制计算。
  - SH/SZ 主板 ±10%、创业板(300/301/302)/科创板(688/689) ±20%、北交所(43/83/87/88/92 或 .BJ) ±30%
  - 主板 ST：2026-07-06 **之前** ±5%，**2026-07-06 起放宽为 ±10%**（`_ST_LIMIT_RELAXED_FROM`）；创业板/科创板/北交所 ST 不折减
  - 舍入：沪深深四舍五入到分（ROUND_HALF_UP + Decimal，禁用浮点乘法）；北交所涨停**截尾**（ROUND_DOWN）、跌停**进位**（ROUND_UP）
  - 新股首日（无昨收）视为无涨跌幅限制 → 不参与涨停/跌停统计
- **收盘涨停**：`close >= 涨停价 - 0.004`；**炸板**：`high >= 涨停价 - 0.004` 且收盘未涨停；**跌停**：`close <= 跌停价 + 0.004`
- **除权除息日兜底**：官方 `pct_change` 与自算 `(close/prev_close-1)×100` 差 > 0.5% 判定为除权日（昨收不可靠），改用 pct 容差法：`|pct_change| >= 板块限制 - 容差`（SH/SZ 容差 0.5%、BJ 1.0%）
- **停牌**：当日 volume=0 计为停牌，剔除出广度统计

## 2. ST 集合

取自 `instrument_detail.parquet` 的 `Name` 含 "ST"（当前约 209 只）。**注意**：这是当前快照，
复盘历史日期时 ST 状态可能有偏差；instrument_detail 的 HqDate 停滞 20260720，长期不变。

## 3. 连板梯队

- 取当日收盘涨停的股票，向历史回看连续涨停天数（阈值 = 板块限制 − 容差）
- 连板高度 = 当日涨停个股中的最大连续涨停天数；梯队 = {连板数: 股票列表}（仅统计 ≥2 板）
- 回看窗口 12 个交易日；除权日个股涨停判定用官方 pct_change（口径 1 的兜底法）

## 4. 涨跌分布直方图

13 桶：涨停 / >7 / 5~7 / 3~5 / 1~3 / 0~1 / 平盘 / -1~0 / -3~-1 / -5~-3 / -7~-5 / <-7 / 跌停。
涨停/跌停桶**用精确分类覆盖**（口径 1）；精确涨停/跌停的股票从区间桶剔除，
被替换掉的近似阈值桶（|pct|≥9.7% 的非精确涨停/跌停）并入 >7 / <-7 桶。
不变量：直方图之和 = 总股票数 − 公司行为 − 停牌。

## 5. 板块聚合

- 成员表：`sector_concept/sector_members.parquet`（行业一级 48、行业二级 80、概念 420）
- 同一 (板块, 股票) 去重；无当日 pct_change 的成员剔除
- **等权平均涨跌幅**：恒有
- **市值加权涨跌幅**：用 valuation.float_mv（取最近 10 个交易日内该股最新值）；板块内流通市值覆盖 ≥60% 才输出，否则列 "—"
- 输出 Top15 + Bottom5

## 6. 量能指标

- 量比 = 当日成交额 / 前 5 个交易日成交额均值
- 两市成交额 = 全部个股 amount 求和（万元 → 亿元 ÷1e4）；指数成交额同理
- 指数涨跌幅：`index_daily.preClose` 全 NULL，**用 close 序列上一交易日收盘自算**

## 7. 资金面

- **两融**：`margin_trading` 按 dt 分组求和，取 ≤ 复盘日的最新两个交易日做余额与环比；**常滞后 1 个交易日**，报告必须标注截至日
- **北向**：`hsgt_north` 季度分区（quarter=YYYYQN），取最新季度快照；
  无市值列 → 市值 = holding_quantity × 该股最新不复权收盘价（≤ report_date）估算，报告必须写「估算」二字
- **L2 主力资金**（因子家族叫法保留，数据源为 l1_l2 合并宽表）：l2 独立目录已下线，读 l1_l2；若 l1_l2 当日缺数，仅输出缺数声明，不得伪造当日资金流

## 8. 个股榜

- 剔除：ST（`--include-st` 可保留）、新股首日（无 prev_close）、无 pct_change
- 涨幅榜/跌幅榜 Top20、成交额榜 Top20（万元→亿元）、换手率榜 Top10（volume / circulating_capital ×100，市值取最新 ≤ 复盘日）
- 状态列（category）：limit_up / broke_up / limit_down / up / down / flat

## 9. 已知限制（写报告时如实标注，禁止掩盖）

1. `index_daily.preClose` 全 NULL（自算替代）
2. 两融滞后、北向季度、l1_l2 缺数时见 §7
3. ST 集合为当前快照（见 2）
4. 除权日涨跌停判定使用 pct 容差法，极端低股价个股可能有 ±0.5% 误判
5. 板块涨跌幅含停牌股之外的全部成员；行业一级成员覆盖约 1429 只（全量 5000+），二级覆盖 4121 只——行业复盘以二级为主，一级做参考

## 10. 输出文件

- `data/reports/daily_review/{YYYY-MM-DD}_stats.json`：全部结构化数据（金额 `*_yi` 字段单位为亿元）
- `data/reports/daily_review/{YYYY-MM-DD}_facts.md`：事实清单（写报告的素材库）
- 最终交付：`data/reports/stock_reports/每日复盘/每日复盘_{YYYY-MM-DD}.{md,pdf}`