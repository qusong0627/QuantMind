---
name: tdx-trading-info
description: （原名：查询个股交易相关数据）使用 `tdx_api_data` 查询交易相关数据，优先依赖 `entry + fixedTag` 自动推导结构化结果。
---

# TDX Trading Info

## 何时使用

当用户想看大宗交易意向申报、融资融券、转融券、资金流向、涨停分析或跌停分析时使用本 skill。

## 入参规则

- 这些交易类查询都已纳入自动映射
- `block_trade_intention` 使用 `entry="TdxSharePCCW.tdxf10_gg_iyds"`
- 其余类型使用 `entry="TdxSharePCCW.tdxf10_gg_jyds"`
- `code` 必须是 6 位股票代码字符串
- `fixedTag` 决定查询类型
- `extra` 一般留空，只有上游明确需要第三参数时再传
- 如果显式传 `responseTransform`，要保证它与 `fixedTag` 对应一致

## 查询类型映射

| 查询类型 | 适用场景 | `entry` | `fixedTag` | 默认 `preset` |
|---|---|---|---|---|
| `block_trade_intention` | 大宗交易意向申报 | `TdxSharePCCW.tdxf10_gg_iyds` | `yxsbxx` | `block_trade_intention` |
| `margin_trading` | 融资融券数据 | `TdxSharePCCW.tdxf10_gg_jyds` | `rzrq` | `margin_trading` |
| `refinancing` | 转融券数据 | `TdxSharePCCW.tdxf10_gg_jyds` | `zrq` | `refinancing` |
| `capital_flow` | 资金流向数据 | `TdxSharePCCW.tdxf10_gg_jyds` | `zjlx` | `capital_flow` |
| `limit_up_analysis` | 涨停分析 | `TdxSharePCCW.tdxf10_gg_jyds` | `ztfx` | `limit_up_analysis` |
| `limit_down_analysis` | 跌停分析 | `TdxSharePCCW.tdxf10_gg_jyds` | `dtfx` | `limit_down_analysis` |

## 选择规则

- 用户要“大宗交易意向申报”时，选 `block_trade_intention`
- 用户要“融资融券”时，选 `margin_trading`
- 用户要“转融券”时，选 `refinancing`
- 用户要“资金流向”时，选 `capital_flow`
- 用户要“涨停分析”时，选 `limit_up_analysis`
- 用户要“跌停分析”时，选 `limit_down_analysis`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_iyds" code="000001" fixedTag="yxsbxx"
```

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_jyds" code="000001" fixedTag="rzrq"
```

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_jyds" code="000001" fixedTag="zjlx"
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `block_trade_intention` 看申报信息和时间
- `margin_trading`、`refinancing` 看融资融券余额、变动和相关指标
- `capital_flow` 看主力/大单/中单/小单资金方向
- `limit_up_analysis`、`limit_down_analysis` 看封板原因、强度和持续性
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结资金方向、交易特征或涨跌停成因
- 最后补充明细表、空结果或接口异常说明
