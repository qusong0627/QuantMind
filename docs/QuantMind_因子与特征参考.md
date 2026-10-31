# QuantMind 因子与特征参考

> **说明（2026）**：本文为历史特征参考。Alpha158/扩展微结构特征现由 QuantDB
> `6_ml_datasets/alpha_library`（Alpha101+GTJA191+Alpha158，429 列）与
> `l1_l2_factors` 直接提供；原 `Alpha158Ext` 处理器已移除。

## 1. Alpha158vwap 基础因子 (158 维)

Qlib 内置 Alpha158 处理器，默认输出 158 维特征，涵盖：

| 类别 | 示例特征 | 说明 |
|------|----------|------|
| **价格类** | OPEN, HIGH, LOW, CLOSE, VWAP | 日内价格数据 |
| **量价关系** | VOLUME, AMOUNT, TURNOVER | 成交量/金额/换手率 |
| **技术指标** | MACD, KDJ, RSI, BOLL | 经典技术指标 |
| **滚动统计** | MEAN/STD/MAX/MIN($close, N) | N 日滚动窗口统计 |
| **动量类** | CORR/COV/REF 衍生 | 价格动量与相关性 |
| **波动率** | STD($close, N), BETA | 波动率与风险指标 |

完整特征可通过 `Alpha158Ext` 处理器获取。

## 2. 扩展微结构因子 (24 维)

在 Alpha158 基础上追加 24 维市场微结构与高频特征：

| 类别 | 特征名 | 说明 |
|------|--------|------|
| **估值** | EXT_PB, EXT_PE | 市净率、市盈率 |
| **流动性** | EXT_TURNOVER, EXT_MONEY_NORM20 | 换手率、归一化成交额 |
| **资金流** | EXT_NET_BUY_AMT_RATIO, EXT_LARGE_ORDER_NET_RATIO | 净买入比例、大单净流入 |
| **买卖压力** | EXT_BUY_VOL_RATIO, EXT_SELL_VOL_RATIO, EXT_BUY_AMT_RATIO, EXT_SELL_AMT_RATIO | 买卖量/金额占比 |
| **订单分布** | EXT_BUY_ORDER_CNT_NORM20, EXT_SELL_ORDER_CNT_NORM20 | 买卖订单数归一化 |
| **已实现波动** | EXT_RV, EXT_RSK EW, EXT_RKURT, EXT_RRV, EXT_RJV | 已实现波动率/偏度/峰度/极差波动/跳空波动 |
| **跳空冲击** | EXT_JUMP_IMPACT, EXT_JUMP_VOL_RATIO, EXT_BIPOWER_VAR | 跳空冲击与连续波动分解 |
| **VPIN** | EXT_VPIN8, EXT_VPIN50 | 交易量知情交易概率 |
| **高频** | EXT_HF_RV, EXT_HF_RSK EW, EXT_HF_RKURT, EXT_HF_RRV, EXT_HF_VOL_NORM20 | 高频已实现波动系列 |

## 3. 基本面硬过滤因子 (88 维)

通过 `FundamentalFilterMixin` 提供，策略 kwargs 中传 `f_{field}_{op}` 即可使用：

| 类别 | 常用字段 | 说明 |
|------|----------|------|
| **市值** | total_mv, float_mv | 总市值、流通市值 |
| **盈利** | roe, roa, gross_margin, net_profit | ROE、ROA、毛利率、净利润 |
| **估值** | pe, pb, ps | PE、PB、PS |
| **成长** | revenue_yoy, net_profit_yoy | 营收同比、净利同比 |
| **质量** | is_st, audit_opinion | ST 标识、审计意见 |

操作符后缀：`_max` (<=), `_min` (>=), `_not` (!=), `_in` (包含)
示例：`f_total_mv_min: 1e9`, `f_is_st_not: 1`, `f_roe_min: 0.1`

## 4. RD-Agent 因子演化

通过 `python -m rdagent.app.qlib_rd_loop.factor` 自动挖掘因子：
- 输入：seed factors（基于 Alpha191 模板）+ 用户需求描述
- 输出：IC > 0.05、ICIR > 0.5 的新因子
- 产出存入 `rd_agent_factors` 表

## 5. 因子推荐场景

| 场景 | 推荐因子 |
|------|----------|
| **低波动策略** | EXT_RV, EXT_RRV, EXT_RJV + REF($close,5)/REF($close,20) |
| **价值因子** | EXT_PB, EXT_PE, $pb + roe 组合 |
| **动量因子** | REF($close,5)/REF($close,20) - 传统动量 |
| **资金流因子** | EXT_NET_BUY_AMT_RATIO, EXT_LARGE_ORDER_NET_RATIO |
| **质量因子** | f_roe_min + f_revenue_yoy_min 组合过滤 |
| **流动性因子** | EXT_TURNOVER + EXT_MONEY_NORM20 组合 |
