---
name: tdx-dragon-tiger
description: （原名：查询个股龙虎榜）使用 `tdx_api_data` 查询个股龙虎榜数据，优先依赖 `entry + 选择参数` 自动推导结构化结果。
---

# TDX Dragon Tiger

## 何时使用

当用户想看某只股票的龙虎榜可用日期、指定日期明细、席位买卖额或营业部画像时使用本 skill。

## 入参规则

- `dates` 使用 `entry="TdxSharePCCW.tdxf10_gg_comreq"` + `fixedTag="jglhb"`
- `list` 使用 `entry="TdxSharePCCW.tdxf10_gg_jyds"` + `fixedTag="jglhb"`
- 两类都已纳入自动映射
- `code` 必须是 6 位股票代码字符串
- `date` 统一转成 `YYYYMMDD` 后放在 `extra`
- 如果显式传 `responseTransform`，要保证它与查询类型对应一致

## 查询类型映射

| 查询类型 | 适用场景 | `entry` | 选择参数 | 默认 `preset` |
|---|---|---|---|---|
| `dates` | 龙虎榜可用日期列表 | `TdxSharePCCW.tdxf10_gg_comreq` | `fixedTag="jglhb"` | `dragon_tiger_dates` |
| `list` | 指定日期龙虎榜明细、净买入和席位画像 | `TdxSharePCCW.tdxf10_gg_jyds` | `fixedTag="jglhb"`、`extra=<date>` | `dragon_tiger_list` |

## 选择规则

- 用户没有给日期时，先查 `dates`
- 用户给了明确日期，或已经知道最新可查日期时，再查 `list`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_comreq" fixedTag="jglhb" code="000001"
```

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_jyds" code="000001" fixedTag="jglhb" extra="20221129"
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `dates` 先确认有哪些日期可查
- `list` 先看 `summary`，再看 `details` 和 `seat_profiles`
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结可用日期，或当日净买入、成交额和关键席位
- 最后补充席位画像、空结果或接口异常说明
