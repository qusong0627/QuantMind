---
name: tdx-board-valuation
description: （原名：板块估值）使用 `tdx_api_data` 查询板块估值对比和历史估值走势，优先依赖 `entry + queryType` 自动推导结构化结果。
---

# TDX Board Valuation

## 何时使用

当用户想看个股在板块中的估值定位、板块与沪深 300 对比，或板块/指数历史估值走势时使用本 skill。

## 入参规则

- `entry` 固定为 `TdxSharePCCW.skef10_hy_hydw_gzsppm`
- 结构化路由会自动推导到 `mode="query-type-target-stock"`
- `queryType` 决定查询类型
- `targetCode` 是板块或指数代码
- `queryType="01"` 时，`stockCode` 代表个股代码；如果只看板块与沪深 300，可留空
- `queryType="02"` 时，`stockCode` 应留空
- 如果显式传 `responseTransform`，要保证它与 `queryType` 对应一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `queryType` | 默认 `preset` | 结果表名 |
|---|---|---|---|---|
| `relative_valuation` | 个股在板块中的估值对比、排名、与沪深 300 对比 | `01` | `board_valuation_query_type_01` | `individual_stock`、`industry_board`、`hs300` |
| `history_valuation` | 板块或指数历史估值走势 | `02` | `board_valuation_query_type_02` | `components`、`industry_board_snapshot`、`hs300_snapshot`、`market_average_snapshot` |

## 选择规则

- 用户问“个股在板块里贵不贵”“板块内估值排名”时，选 `relative_valuation`
- 用户问“板块历史 PE/PB 水平”“指数估值走势”时，选 `history_valuation`
- 用户未细分但明显是在问个股相对板块估值时，优先选 `relative_valuation`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.skef10_hy_hydw_gzsppm" queryType="01" targetCode="881430" stockCode="301073"
```

```bash
tdx_api_data entry="TdxSharePCCW.skef10_hy_hydw_gzsppm" queryType="02" targetCode="881430" stockCode=""
```

### 原始上游结果

```bash
tdx_api_data mode="raw" entry="TdxSharePCCW.skef10_hy_hydw_gzsppm" params=["01","881430","301073"]
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `relative_valuation` 重点看个股、板块、沪深 300 的相对位置
- `history_valuation` 重点看最新值、区间高低和样本点数
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结估值位置、对比关系和最新时间点
- 最后补充明细表、空结果或接口异常说明
