---
name: tdx-company-info
description: （原名：查询公司信息）使用 `tdx_api_data` 查询公司信息，优先依赖 `entry + fixedTag` 自动推导结构化结果，统一覆盖公司概要、基本情况、发行交易、董监高和参控股公司。
---

# TDX Company Info

## 何时使用

当用户想看公司概要、主营业务、基础资料、发行交易信息、董监高或参控股公司结构时使用本 skill。

## 入参规则

- `overview` 使用 `entry="TdxSharePCCW.tdxf10_gg_zxts"`，自动推导到 `mode="code-fixed-tag-extra"`
- 其余类型使用 `entry="TdxSharePCCW.tdxf10_gg_gsgk"`，自动推导到 `mode="fixed-tag-code-extra"`
- `code` 必须是 6 位股票代码字符串
- `fixedTag` 决定查询类型
- `extra` 一般留空，除非上游接口明确需要
- 如果显式传 `responseTransform`，要保证它与 `entry + fixedTag` 对应一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `entry` | `fixedTag` | 默认 `preset` | 结果表名 |
|---|---|---|---|---|---|
| `overview` | 公司概要、主营业务、关联主题、标签、财务摘要 | `TdxSharePCCW.tdxf10_gg_zxts` | `gsgy` | `company_overview` | `basic_overview`、`business_overview`、`related_themes` |
| `basic_info` | 基础资料、业务分类、ESG 报告 | `TdxSharePCCW.tdxf10_gg_gsgk` | `0` | `company_basic_info` | `basic_info`、`business_breakdown`、`esg_reports` |
| `issuance_trading` | 上市、发行、募资、首日交易信息 | `TdxSharePCCW.tdxf10_gg_gsgk` | `8` | `company_issuance_trading` | `issuance_and_trading` |
| `executives` | 董监高名单、职务和结构汇总 | `TdxSharePCCW.tdxf10_gg_gsgk` | `20` | `company_executives` | `executives`、`position_summary` |
| `affiliates` | 参股控股公司、关联结构和汇率快照 | `TdxSharePCCW.tdxf10_gg_gsgk` | `3` | `company_affiliates` | `affiliates`、`exchange_rates` |

## 选择规则

- 用户要“公司概览”“主营业务”“公司标签”时，选 `overview`
- 用户要“基础资料”“经营范围”“ESG”时，选 `basic_info`
- 用户要“上市发行”“募资历史”时，选 `issuance_trading`
- 用户要“董监高”“高管团队”时，选 `executives`
- 用户要“参股公司”“控股结构”时，选 `affiliates`
- 用户只说“查公司信息”但未细分时，优先使用 `overview`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_zxts" code="000001" fixedTag="gsgy"
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gsgk" fixedTag="0" code="000001"
```

### 显式覆盖默认转换

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_gsgk" fixedTag="20" code="000001" responseTransform={"kind":"preset","preset":"company_executives"}
```

### 原始上游结果

```bash
tdx_api_data mode="raw" entry="TdxSharePCCW.tdxf10_gg_gsgk" params=["0","000001",""]
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `overview` 重点看公司定位、主营业务、财务摘要和关联主题
- `basic_info` 重点看基础资料、业务拆分和 ESG
- `issuance_trading`、`executives`、`affiliates` 按用户问题展开对应表
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结最关键的结构化结论
- 最后补充明细表、空结果或接口异常说明
