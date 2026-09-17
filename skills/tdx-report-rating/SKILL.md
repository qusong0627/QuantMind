---
name: tdx-report-rating
description: （原名：查询个股研报评级一致预期）使用 `tdx_api_data` 查询 A 股个股的研报评级一致预期，优先依赖 `entry + fixedTag` 自动推导结构化结果。
---

# TDX Report Rating

## 何时使用

当用户想看个股研报评级一致预期、目标价预期、时间序列变化或分析师一致预期概览时使用本 skill。

## 入参规则

- `entry` 固定为 `TdxSharePCCW.tdxf10_gg_ybpj`
- 结构化路由会自动推导到 `mode="code-fixed-tag"`
- `code` 必须是 6 位股票代码字符串
- `fixedTag` 固定为 `yzyq`
- 默认 `preset` 为 `report_rating_consensus`
- 如果显式传 `responseTransform`，要保证它与 `fixedTag=yzyq` 对应一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `fixedTag` | 默认 `preset` | 结果表名 |
|---|---|---|---|---|
| `consensus` | 研报评级一致预期、目标价预期、时间序列变化 | `yzyq` | `report_rating_consensus` | `overview`、`consensus_timeline`、`adjustment_factors`、`price_series` |

## 选择规则

- 用户问“评级一致预期”“目标价一致预期”“分析师一致预期”时，使用 `consensus`
- 如果用户没有细分，只要问题仍属于研报评级预期，默认使用 `consensus`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_ybpj" code="000001" fixedTag="yzyq"
```

### 显式覆盖默认转换

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_ybpj" code="000001" fixedTag="yzyq" responseTransform={"kind":"preset","preset":"report_rating_consensus"}
```

### 原始上游结果

```bash
tdx_api_data mode="raw" entry="TdxSharePCCW.tdxf10_gg_ybpj" params=["000001","yzyq"]
```

## 结果处理

- 优先读取 `response.transformed.tables`
- 重点关注 `overview` 中的预测截止期、基准报告期、最近更新时间
- 需要看趋势时，再展开 `consensus_timeline`、`adjustment_factors`、`price_series`
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象和调用参数
- 再总结最新预期、时间序列条数和关键变化
- 最后补充需要展开的明细表或异常说明
