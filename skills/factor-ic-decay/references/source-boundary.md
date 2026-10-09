# 数据边界与不做的事（source-boundary，本地版）

本技能只对**已存在的因子面板**做 IC 衰减与稳定性统计，不预测、不喊单。

## 允许的数据来源

- **本地 QuantDB（quantdb 模式，主路径）**：只读本地 parquet——
  - 因子列：`quantdb/6_ml_datasets/features_daily`（CN 默认）、`l1_factors`/`l2_factors`；HK/US 为 `quanthk|quantus/6_ml_datasets/l1_factors`。
  - 收益：`quantdb|quanthk|quantus/1_kline_data/daily_forward`（CN 前复权；HK/US 不复权，口径差异写入报告）。
  - 不连任何远程服务、不连 PG/Redis、不触发同步任务。
- **用户自备 CSV（csv 模式）**：长面板 `date, symbol, factor, fwd_ret`（可选 `fwd_ret_{n}` 多周期列）；列名别名自动识别（含 日期/股票代码/因子/收益 等中文列名）。

## 需要用户权利与明确提供才可用的

- 付费墙内 / 会员专享行情或因子库的未授权抓取。
- 在本技能内写券商 / 私有库连接器、SQL 适配器、自动爬取——**不内置**。

## 本技能不做的事

- 不构造因子表达式、不计算新因子（只诊断既有列）。
- 不做组合回测、不算费用/换手、不输出买卖指令与仓位建议。
- 不做季节性、归因、同业对标（属于其他技能）。
- 不负责因子的时点正确性（因子列是否只用截至当日可知信息，由数据生产方保证）；脚本仅硬拒绝明显的前视标签列（`label_return` / `return_{n}d` / `future_return_{n}d`）。

## 输出边界

- 只输出：日度 IC 汇总、ICIR、NW-t、滚动/分段稳定性、多周期衰减与半衰期拟合（JSON + stdout 表格）。
- 每个结论须标注：因子名、数据窗口、IC 观测数、是事实还是推断；数据缺口（如 HK/US 空壳列）如实列为失败因子/警告，不补齐、不猜测。
- 必须声明：半衰期与强度标签是历史证据描述，**不是保质期、不是交易信号**。
