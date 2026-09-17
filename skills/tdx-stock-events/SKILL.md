---
name: tdx-stock-events
description: （原名：查询股票事件信息）使用 `tdx_api_data` 查询股票事件信息；优先使用已纳入自动映射的查询类型，对参数无法自动区分的类型保留显式调用。
---

# TDX Stock Events

## 何时使用

当用户想看股东增减持、大宗交易或十大股东持股明细时使用本 skill。

## 入参规则

- `shareholder_change` 已纳入自动映射：`entry="TdxSharePCCW.tdxf10_gg_gdyj"` + `fixedTag="cgbd"`
- `block_trade` 已纳入自动映射：`entry="TdxSharePCCW.tdxf10_gg_jyds"` + `fixedTag="dzjy"`
- `top_shareholder_detail` 仍需显式调用，因为它与 `institutional_holding_detail` 共用同一个 `entry + 参数形状`，工具无法仅凭参数自动判断该走哪个 preset
- `code` 必须是 6 位股票代码字符串
- 涉及日期时，统一使用 `YYYYMMDD`
- 如果显式传 `responseTransform`，要保证它与查询类型对应一致；当前代码不会自动纠正不匹配的 preset

## 查询类型映射

| 查询类型 | 适用场景 | 自动映射 | `entry` | 选择参数 | 默认/建议 `preset` |
|---|---|---|---|---|---|
| `shareholder_change` | 股东增减持时间范围、分页结果、股本历史 | 是 | `TdxSharePCCW.tdxf10_gg_gdyj` | `fixedTag="cgbd"`、`beginDate`、`endDate`、分页参数 | `shareholder_change` |
| `block_trade` | 大宗交易日期、价格、成交额和买卖营业部 | 是 | `TdxSharePCCW.tdxf10_gg_jyds` | `fixedTag="dzjy"`、`extra=<date>` | `block_trade` |
| `top_shareholder_detail` | 十大股东持股明细、变动和市值 | 否 | `TdxSharePCCW.tdxf10_gg_gdyj_jgcgmx` | `reportDate`、`sortType`、`typeValue`、分页参数 | `top_shareholder_detail` |

## 选择规则

- 用户要“增减持”“时间范围内股东变动”时，选 `shareholder_change`
- 用户要“大宗交易”时，选 `block_trade`
- 用户要“十大股东持股明细”时，选 `top_shareholder_detail`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gdyj" code="000001" fixedTag="cgbd" beginDate="20250101" endDate="20251231" pageNo="1" pageSize="20"
```

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_jyds" code="000001" fixedTag="dzjy" extra="20260107"
```

### 例外：当前仍需显式调用

```bash
tdx_api_data mode="code-sort-report-type-click-page" entry="TdxSharePCCW.tdxf10_gg_gdyj_jgcgmx" code="000001" reportDate="20251231" sortType="000" typeValue="99" clickIndex="1" pageNo="1" pageSize="10" responseTransform={"kind":"preset","preset":"top_shareholder_detail"}
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `shareholder_change` 重点看增减持区间、记录数和股本历史
- `block_trade` 重点看成交日期、价格、成交额和买卖席位
- `top_shareholder_detail` 重点看持股市值、比例和变动
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结时间范围、关键金额或持股变化
- 最后补充分页情况、明细表或异常说明
