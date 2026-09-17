---
name: tdx-financials
description: （原名：查询财务分析数据）使用 `tdx_api_data` 查询财务分析数据，优先依赖 `entry + 选择参数` 自动推导结构化结果。
---

# TDX Financials

## 何时使用

当用户想看利润表、现金流量表、资产负债表、行业排名、主营构成、估值历史或员工指标时使用本 skill。

## 入参规则

- 当前下列财务查询类型都已纳入自动映射
- `code` 必须保持字符串类型
- `reportDate` 统一使用 `YYYYMMDD`
- `valuationMetric` 仅允许 `PE`、`PB`、`PS`、`PCF`
- `timeRange` 仅允许 `1Y`、`3Y`、`5Y`、`MAX`
- 如果显式传 `responseTransform`，要保证它与查询类型对应一致；当前代码不会自动纠正不匹配的 preset

## 查询类型映射

| 查询类型 | 适用场景 | `entry` | 选择参数 | 默认 `preset` |
|---|---|---|---|---|
| `income_statement` | 利润表 | `TdxShareCW.ph_agf10_cw_lyb` | `fixedTag="00101"` 或 `"00102"` | `income_statement` |
| `cashflow_statement` | 现金流量表 | `TdxShareCW.ph_agf10_cw_xjllb` | `fixedTag="00101"` 或 `"00102"` | `cashflow_statement` |
| `balance_sheet` | 资产负债表 | `TdxShareCW.ph_agf10_cw_zcfzb` | `code` | `balance_sheet` |
| `industry_rank` | 行业财务排名 | `TdxShareCW.ph_agf10_hypm` | `queryKey="00102"`、`extra=<reportDate>` | `industry_rank` |
| `valuation_rank` | 行业估值排名 | `TdxShareCW.ph_agf10_hypm` | `queryKey="00105"`、`extra=<reportDate>` | `valuation_rank` |
| `financial_sector_indicators` | 金融行业专项指标 | `TdxShareCW.ph_agf10_cw_zxzbxq` | `code`、`extra=<reportDate>` | `financial_sector_indicators` |
| `business_composition` | 主营构成 | `TdxShareCW.ph_agf10_jyfx` | `fixedTag="00202"`、`extra=<reportDate>` | `business_composition` |
| `valuation_history` | 历史估值走势 | `TdxShareCW.ph_agf10_gzfx` | `code`、`extraOne=timeRange`、`extraTwo=valuationMetric` | `valuation_history` |
| `employee_structure` | 员工构成 | `TdxSharePCCW.tdxf10_gg_gsgk` | `fixedTag="4"` | `employee_structure` |
| `employee_efficiency` | 员工效益 | `TdxSharePCCW.tdxf10_gg_gsgk` | `fixedTag="5"` | `employee_efficiency` |

## 选择规则

- 用户要“利润表”时，选 `income_statement`
- 用户要“现金流量表”时，选 `cashflow_statement`
- 用户要“资产负债表”时，选 `balance_sheet`
- 用户要“行业排名”时，按问题选择 `industry_rank` 或 `valuation_rank`
- 用户要“主营构成”时，选 `business_composition`
- 用户要“历史估值”时，选 `valuation_history`
- 用户要“员工结构/效率”时，选对应员工类查询
- 不要默认把所有财务子类型都打一遍

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxShareCW.ph_agf10_cw_lyb" fixedTag="00101" code="600036"
```

```bash
tdx_api_data entry="TdxShareCW.ph_agf10_cw_xjllb" fixedTag="00101" code="300750"
```

```bash
tdx_api_data entry="TdxShareCW.ph_agf10_cw_zcfzb" code="600519"
```

```bash
tdx_api_data entry="TdxShareCW.ph_agf10_hypm" queryKey="00102" code="688318" extra="20250930"
```

```bash
tdx_api_data entry="TdxShareCW.ph_agf10_jyfx" fixedTag="00202" code="600519" extra="20241231"
```

```bash
tdx_api_data entry="TdxShareCW.ph_agf10_gzfx" code="000002" extraOne="1Y" extraTwo="PE"
```

## 结果处理

- 优先读取 `response.transformed.tables`
- 报表类先看最新报告期和核心指标
- 排名类先看公司值、行业平均和排序位置
- 主营构成先看分类方式，再看明细
- 估值历史看最新值、区间变化和样本点数
- 员工类看人数、结构占比或人均指标
- 如果字段语义不够明确，保留原字段名，不要自行杜撰解释

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结报告期、关键指标和对比结论
- 最后补充明细表、空结果或接口异常说明
