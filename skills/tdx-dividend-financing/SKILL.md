---
name: tdx-dividend-financing
description: （原名：查询分红融资）使用 `tdx_api_data` 查询个股分红融资、分红历史走势、分红排名，以及已接入的分红视界和股东进出详情相关接口。适用于用户提到分红、派现融资比、股息率、配股、增发获配明细等场景，并需要根据 `fixedTag` 选择对应结构化结果时。
---

# TDX Dividend Financing

## 何时使用

当用户想查询以下信息时使用本 skill：

- 分红与募资概览
- 分红图、分红历史走势
- 分红排名、股息率、派现融资比
- 配股方案、增发方案、增发获配明细
- 分红视界股票筛选、对比数据
- 股东进出详情和类别

统一通过 `tdx_api_data` 调用，不直接拼 HTTP 请求。

## 主入口

- 主入口 `entry` 为 `TdxSharePCCW.tdxf10_gg_fhrz`
- 自动推导 `mode` 为 `code-fixed-tag`
- 基本参数形态为 `[code, fixedTag]`

## `fixedTag` 映射

| 场景 | `fixedTag` | 默认 `preset` |
|---|---|---|
| 分红与募资概览 | `pxmz` | `dividend_financing_overview` |
| 分红图 | `fh` | `dividend_chart` |
| 配股已实施方案与预案 | `pf` | `rights_issue_plan` |
| 增发获配明细 | `zfpg` | `placement_detail` |
| 增发方案与实施 | `zf` | `refinancing_plan` |
| 分红历史走势-股利支付率 | `fhlszs_glzfl` | `dividend_history_payout` |
| 分红历史走势-股息率 | `fhlszs_gxl` | `dividend_history_yield` |
| 分红排名-股利支付率 | `fhpm_glzfl` | `dividend_rank_payout` |
| 分红排名-股息率 | `fhpm_gxl` | `dividend_rank_yield` |
| 分红排名-派现融资比 | `fhpm_pxrzb` | `dividend_rank_cashfin_ratio` |

优先依赖自动推导，不必显式传 `mode`。

## 其他已接入入口

### 分红表分页

- `entry="TdxSharePCCW.tdxf10_gg_fhrz_fh"`
- 参数形态 `[code, fixedTag, extra]`
- `fixedTag="fh"`
- `extra` 表示页码
- 默认 `preset="dividend_table"`

### 分红视界

- `entry="TdxSharePCCW.tdxf10_gg_sj"`
- 参数形态 `[fixedTag, code, extraOne, extraTwo]`

映射：

- `fixedTag="qhgp"` -> `dividend_viewer_filter`
- `fixedTag="fh_sj"` -> `dividend_viewer_compare`

### 股东进出详情

- `entry="TdxSharePCCW.tdxf10_gg_gdyjcgmx"`
- 参数形态 `[fixedTag, code, extraOne, extraTwo, extraThree]`

映射：

- `fixedTag="gdjc"` -> `holder_change_detail`
- `fixedTag="gdjcmxrq"` -> `holder_change_type`

其中：

- `extraOne` 是机构代码
- `extraTwo` 是股东 id
- `extraThree` 是页码
- `gdjcmxrq` 允许 `code` 为空字符串

## 调用示例

### 分红与募资概览

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_fhrz" code="000001" fixedTag="pxmz"
```

### 分红历史走势-股息率

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_fhrz" code="601086" fixedTag="fhlszs_gxl"
```

### 分红排名-派现融资比

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_fhrz" code="601086" fixedTag="fhpm_pxrzb"
```

### 分红表分页

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_fhrz_fh" code="000001" fixedTag="fh" extra="1"
```

### 分红视界-股票筛选

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_sj" fixedTag="qhgp" code="000001" extraOne="0" extraTwo=""
```

## 已知状态

- `TdxSharePCCW.tdxf10_gg_fhrz` 已实测可用，`fixedTag="pxmz"` 返回正常
- `TdxSharePCCW.tdxf10_gg_fhrz_fh` 当前实测返回上游 `功能未注册`
- 其余新接入 dividend 相关入口已完成本地自动推导和组包接线，但是否可用仍建议按需实测

## 处理原则

- 用户只说“查分红融资”但没细分时，优先使用 `fixedTag="pxmz"`
- 用户明确要原始响应时，改用 `mode="raw"` 并按上游顺序显式传 `params`
- 用户要求分页明细时，先提醒 `TdxSharePCCW.tdxf10_gg_fhrz_fh` 当前上游可能未注册
