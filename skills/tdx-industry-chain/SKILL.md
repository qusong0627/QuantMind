---
name: tdx-industry-chain
description: （原名：查询行业产业链）使用 `tdx_api_data` 查询行业产业链和行业重要事件，优先依赖 `entry + 选择参数` 自动推导结构化结果。
---

# TDX Industry Chain

## 何时使用

当用户想看行业产业链图谱、上下游节点关系，或行业重要事件和最新动态时使用本 skill。

## 入参规则

- `industry_chain` 使用 `entry="TdxSharePCCW.cfg_tk_gethy"`，自动推导到 `mode="industry-code"`
- `important_events` 使用 `entry="TdxSharePCCW.skef10_hy_zxdt_hyzysj"`，自动推导到 `mode="industry-title"`
- `industryCode` 必须保持字符串类型
- `title` 仅用于行业事件标题筛选；不筛选时可留空
- 如果显式传 `responseTransform`，要保证它与对应 `entry` 一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `entry` | 选择参数 | 默认 `preset` | 结果表名 |
|---|---|---|---|---|---|
| `industry_chain` | 产业链图谱、上下游节点、连线关系 | `TdxSharePCCW.cfg_tk_gethy` | `industryCode` | `industry_chain` | `metadata`、`graph_elements` |
| `important_events` | 行业重要事件、最新动态、标题过滤 | `TdxSharePCCW.skef10_hy_zxdt_hyzysj` | `industryCode`、`title` | `industry_important_events` | `important_events` |

## 选择规则

- 用户要“产业链图谱”“上下游关系”“行业链路”时，选 `industry_chain`
- 用户要“行业事件”“行业动态”“按标题筛选事件”时，选 `important_events`
- 用户只说“查行业链”但未细分时，优先使用 `industry_chain`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.cfg_tk_gethy" industryCode="881426"
```

```bash
tdx_api_data entry="TdxSharePCCW.skef10_hy_zxdt_hyzysj" industryCode="881430" title="机器人"
```

### 原始上游结果

```bash
tdx_api_data mode="raw" entry="TdxSharePCCW.cfg_tk_gethy" params=["881426"]
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `industry_chain` 先看 `metadata`，再整理 `graph_elements` 中的节点和连线关系
- `important_events` 重点看最新日期、标题和事件摘要
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结链路结构或事件重点
- 最后补充节点/事件明细、空结果或接口异常说明
