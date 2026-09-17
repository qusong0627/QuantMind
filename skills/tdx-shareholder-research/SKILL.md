---
name: tdx-shareholder-research
description: （原名：查询股东信息）使用 `tdx_api_data` 查询个股股东研究数据，优先依赖 `entry + fixedTag` 自动推导结构化结果，统一覆盖控股股东、股东人数、排名和前十大股东信息。
---

# TDX Shareholder Research

## 何时使用

当用户想看控股股东与实控人、股东人数变化、股东人数排名、十大流通股东或十大股东持股时使用本 skill。

## 入参规则

- `entry` 固定为 `TdxSharePCCW.tdxf10_gg_gdyj`
- 这组股东研究路由会自动推导到 `mode="code-fixed-tag-report-cursor-page"`
- `code` 必须是 6 位股票代码字符串
- `fixedTag` 决定查询类型
- 可选参数主要是 `reportDate`、`cursor`、`pageNo`、`pageSize`
- 如果不传 `reportDate`，通常读取上游默认可用报告期
- 如果显式传 `responseTransform`，要保证它与 `fixedTag` 对应一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `fixedTag` | 默认 `preset` | 结果表名 |
|---|---|---|---|---|
| `controlling_shareholder` | 控股股东、实控人、最终控制人 | `kggd` | `controlling_shareholder` | `controlling_shareholder` |
| `shareholder_count` | 股东人数、户均流通股、户数变化 | `gdrs` | `shareholder_count` | `shareholder_count` |
| `shareholder_count_rank` | 股东人数增减排名 | `thygdrs` | `shareholder_count_rank` | `shareholder_count_rank` |
| `top_float_shareholders` | 十大流通股东概览与明细 | `ltgd` | `top_float_shareholders` | `top_float_shareholders` |
| `top_shareholders` | 十大股东持股和一致行动信息 | `sdgdbgq` | `top_shareholders` | `top_shareholders` |

## 选择规则

- 用户要“控股股东”“实控人”时，选 `controlling_shareholder`
- 用户要“股东人数”“户数变化”时，选 `shareholder_count`
- 用户要“股东人数排名”时，选 `shareholder_count_rank`
- 用户要“十大流通股东”时，选 `top_float_shareholders`
- 用户要“十大股东”时，选 `top_shareholders`
- 用户只说“查股东结构”但未细分时，优先使用 `controlling_shareholder`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gdyj" code="000001" fixedTag="gdrs" pageNo="1" pageSize="20"
```

### 带报告期查询

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gdyj" code="000001" fixedTag="ltgd" reportDate="20241231" pageNo="1" pageSize="20"
```

### 原始上游结果

```bash
tdx_api_data mode="raw" entry="TdxSharePCCW.tdxf10_gg_gdyj" params=["000001","gdrs","","20241231","0","1","20"]
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `controlling_shareholder` 看控制关系和持股比例
- `shareholder_count` 与 `shareholder_count_rank` 看变化方向和幅度
- `top_float_shareholders`、`top_shareholders` 看集中度、持股比例和报告期
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结最新报告期、关键比例和变化趋势
- 最后补充明细表、分页情况或异常说明
