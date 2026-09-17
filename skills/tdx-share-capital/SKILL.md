---
name: tdx-share-capital
description: （原名：查询股本信息）使用 `tdx_api_data` 查询个股股本信息，优先依赖 `entry + fixedTag` 自动推导结构化结果，统一覆盖股本结构、股本变动、限售解禁和股票回购。
---

# TDX Share Capital

## 何时使用

当用户想看股本结构、历史股本变动、限售解禁安排或股票回购时使用本 skill。

## 入参规则

- `entry` 固定为 `TdxSharePCCW.tdxf10_gg_gbjg`
- 结构化路由会自动推导到 `mode="code-fixed-tag"`
- `code` 必须是 6 位股票代码字符串
- `fixedTag` 决定查询类型
- 默认 `preset` 会随 `fixedTag` 自动选择
- 如果显式传 `responseTransform`，要保证它与 `fixedTag` 对应一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `fixedTag` | 默认 `preset` | 结果表名 |
|---|---|---|---|---|
| `structure` | 股本结构、流通股、限售股构成 | `gbjg` | `share_capital_structure` | `capital_structure`、`latest_change_meta` |
| `changes` | 历次股本变化与原因 | `gbbd` | `share_capital_changes` | `capital_changes`、`change_reasons` |
| `restricted_unlocks` | 限售股解禁安排与调整 | `xslt` | `restricted_share_unlocks` | `restricted_share_unlocks`、`unlock_adjustments` |
| `stock_buyback` | 股票回购预案与实施进度 | `gphg` | `stock_buyback` | `stock_buyback` |

## 选择规则

- 用户要“股本结构”“总股本”“流通盘”时，选 `structure`
- 用户要“股本变动”“扩股缩股原因”时，选 `changes`
- 用户要“限售解禁”时，选 `restricted_unlocks`
- 用户要“股票回购”时，选 `stock_buyback`
- 用户只说“查股本信息”但未细分时，优先使用 `structure`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gbjg" code="688318" fixedTag="gbjg"
```

### 显式覆盖默认转换

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gbjg" code="688318" fixedTag="xslt" responseTransform={"kind":"preset","preset":"restricted_share_unlocks"}
```

### 原始上游结果

```bash
tdx_api_data mode="raw" entry="TdxSharePCCW.tdxf10_gg_gbjg" params=["688318","gbjg"]
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `structure` 重点看最新股本结构和最近变更日期
- `changes` 重点看历史变动节奏和原因编码
- `restricted_unlocks`、`stock_buyback` 按用户关心的日期和规模展开
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结关键日期、规模和变化趋势
- 最后补充明细表、空结果或接口异常说明
