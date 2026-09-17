---
name: tdx-earnings-warning
description: （原名：查询个股业绩预警）使用 `tdx_api_data` 查询个股业绩预警数据。适用于用户提到业绩预警、业绩预告类型、预告净利润、净利润变动幅度等场景，并且已知股票代码和证券 id 时。
---

# TDX Earnings Warning

## 何时使用

当用户想查询个股业绩预警相关信息时使用本 skill，典型场景包括：

- 预告类型
- 预告净利润区间
- 净利润变化幅度区间
- 预告次数
- 最新预告日

统一通过 `tdx_api_data` 调用。

## 接口规则

- `entry` 固定为 `TdxSharePCCW.tdxf9_ag_cwsj_yjyj`
- 自动推导 `mode` 为 `code-extra`
- 参数形态为 `[code, extra]`
- `code` 是 6 位股票代码
- `extra` 是证券 id，例如 `gssz0000526`
- 默认 `preset` 为 `earnings_warning`

## 返回字段

主要字段包括：

- `reportPeriod`
- `forecastType`
- `forecastProfit10k`
- `profitLower10k`
- `profitUpper10k`
- `profitChangePct`
- `changeLowerPct`
- `changeUpperPct`
- `forecastCount`
- `isWarning`
- `latestForecastDate`

## 调用示例

```bash
tdx_api_data entry="TdxSharePCCW.tdxf9_ag_cwsj_yjyj" code="000526" extra="gssz0000526"
```

## 处理原则

- 如果用户只给股票代码，没有证券 id，先提示缺少第二个参数，不能安全发起调用
- 如果用户要求原始响应，使用 `mode="raw"` 并显式传 `params=["000526","gssz0000526"]`
- 如果用户想看结构化结果，直接使用自动推导写法

## 已知状态

- 本地自动推导、请求组包和 preset 转换已经接通
- 当前实测上游返回 `功能未注册`
- 因此这个 skill 更适合在代码接线、参数确认、手动联调时使用；真正查数前要先确认上游服务端已注册对应功能
