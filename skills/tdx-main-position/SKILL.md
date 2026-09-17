---
name: tdx-main-position
description: （原名：主力资金）使用 `tdx_api_data` 查询机构持股、北向资金和持仓对比数据，优先依赖 `entry + 选择参数` 自动推导结构化结果。
---

# TDX Main Position

## 何时使用

当用户想看机构持股日期、机构持仓明细、机构持股汇总、机构类型分布、北向资金或机构持仓与股价对比时使用本 skill。

## 入参规则

- 当前这些查询类型都已纳入自动映射
- `code` 必须是 6 位股票代码字符串
- 涉及报告期或日期时，统一使用 `YYYYMMDD`
- `institutional_holding_dates` 使用 `entry="TdxSharePCCW.tdxf10_gg_comreq"` + `fixedTag="jgcg"`
- `institutional_holding_summary` 使用 `entry="TdxSharePCCW.tdxf10_gg_gdyj"` + `fixedTag="jgcg"`
- `institutional_holding_overview` 使用 `entry="TdxSharePCCW.tdxf10_gg_gdyj"` + `fixedTag="jgcgqk"`
- `institutional_holding_detail` 使用 `entry="TdxSharePCCW.tdxf10_gg_gdyj_jgcgmx"`
- `northbound_funds` 使用 `entry="TdxSharePCCW.tdxf10_gg_zlcc"` + `fixedTag="bszj"` + `date="YYYYMMDD"`
- `northbound_funds` 的实际上游参数会组装为 `[code, fixedTag, date, "0", "0", "0"]`
- `institutional_holding_price_compare` 使用 `entry="TdxShareCW.ph_agf10_gbgd_jgcc"` + `queryKey="00101"`
- 如果显式传 `responseTransform`，要保证它与查询类型对应一致；当前代码不会自动纠正不匹配的 preset

## 查询类型映射

| 查询类型 | 适用场景 | `entry` | 选择参数 | 默认 `preset` |
|---|---|---|---|---|
| `institutional_holding_dates` | 可用报告期列表 | `TdxSharePCCW.tdxf10_gg_comreq` | `fixedTag="jgcg"` | `institutional_holding_dates` |
| `institutional_holding_summary` | 历次报告期机构家数、持仓比例 | `TdxSharePCCW.tdxf10_gg_gdyj` | `fixedTag="jgcg"`、分页参数 | `institutional_holding_summary` |
| `institutional_holding_overview` | 单报告期机构类型整体分布 | `TdxSharePCCW.tdxf10_gg_gdyj` | `fixedTag="jgcgqk"`、`reportDate`、分页参数 | `institutional_holding_overview` |
| `institutional_holding_detail` | 某报告期机构持股明细 | `TdxSharePCCW.tdxf10_gg_gdyj_jgcgmx` | `reportDate`、`sortType`、`typeValue`、`pageNo`、`pageSize` | `institutional_holding_detail` |
| `northbound_funds` | 北向资金持股数量、市值和占比 | `TdxSharePCCW.tdxf10_gg_zlcc` | `fixedTag="bszj"`、`date=<YYYYMMDD>` | `northbound_funds` |
| `institutional_holding_price_compare` | 机构持仓与股价对比 | `TdxShareCW.ph_agf10_gbgd_jgcc` | `queryKey="00101"`、`compareFlag` | `institutional_holding_price_compare` |

## 选择规则

- 用户要“有哪些报告期可查”时，选 `institutional_holding_dates`
- 用户要“历次机构持股汇总”时，选 `institutional_holding_summary`
- 用户要“某一期各机构类型分布”时，选 `institutional_holding_overview`
- 用户要“某一期机构持仓明细”时，选 `institutional_holding_detail`
- 用户要“北向资金持股”时，选 `northbound_funds`
- 用户要“机构持仓和股价对比”时，选 `institutional_holding_price_compare`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_comreq" fixedTag="jgcg" code="000002"
```

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gdyj" code="000002" fixedTag="jgcg" pageNo="1" pageSize="20"
```

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_zlcc" code="000002" fixedTag="bszj" date="20250630"
```

```bash
tdx_api_data entry="TdxShareCW.ph_agf10_gbgd_jgcc" queryKey="00101" code="000002" compareFlag="0"
```

## 结果处理
优先读取 response.transformed.tables
dates 看可用报告期范围
summary 看机构家数、持仓比例和占流通股比例
overview 看不同机构类型的分布差异
detail 看机构名称、类型、持股数量和占比
northbound_funds 看持股数量、市值和占比
price_compare 看资金类型与价格走势对应关系
如果用户要求原始字段，再补充 response.data

## 输出要求
先说明查询对象、查询类型和调用参数
再总结报告期、持股比例、集中度或价格对比关系
最后补充分页情况、明细表或异常说明