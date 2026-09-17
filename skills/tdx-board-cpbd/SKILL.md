---
name: tdx-board-cpbd
description: （原名：板块操盘必读）使用 `tdx_api_data` 查询板块操盘必读，优先依赖 `entry + branch` 自动推导结构化结果，统一覆盖基础资料、板块详解、阶段涨幅和市场统计。
---

# TDX Board CPBD

## 何时使用

当用户想看板块基础资料、板块说明、阶段表现或市场统计时使用本 skill。

## 入参规则

- `entry` 固定为 `TdxSharePCCW.skef10_bk_cpbd_jczl`
- 结构化路由会自动推导到 `mode="branch-code-time"`
- `code` 必须是板块或指数代码字符串
- `branch` 决定查询类型
- `timeType` 主要用于 `stage_return`
- `stage_return` 默认 `timeType="1m"`
- 如果显式传 `responseTransform`，要保证它与 `branch` 对应一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `branch` | `timeType` | 默认 `preset` | 结果表名 |
|---|---|---|---|---|---|
| `basic_info` | 总市值、PE、PB、成分股数量等基础快照 | `001` | `""` | `board_cpbd_basic_info` | `basic_info` |
| `detail` | 创建日期、板块分类、解析和关联证券 | `002` | `""` | `board_cpbd_detail` | `board_detail`、`board_asset` |
| `stage_return` | 阶段涨幅、与基准对比、板块排名 | `003` | 默认 `1m` | `board_cpbd_stage_return` | `stage_return` |
| `market_stats` | 收盘点位、成交额、涨跌家数和区间收益 | `004` | `""` | `board_cpbd_market_stats` | `market_stats` |

## 选择规则

- 用户要“板块快照”“板块基础资料”时，选 `basic_info`
- 用户要“板块介绍”“板块解读”时，选 `detail`
- 用户要“阶段涨幅”“近 1 月/3 月表现”时，选 `stage_return`
- 用户要“成交额”“涨跌家数”“市场统计”时，选 `market_stats`
- 用户只说“查板块操盘必读”但未细分时，优先使用 `basic_info`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.skef10_bk_cpbd_jczl" branch="003" code="880976" timeType="1m"
```

### 显式覆盖默认转换

```bash
tdx_api_data entry="TdxSharePCCW.skef10_bk_cpbd_jczl" branch="002" code="880976" responseTransform={"kind":"preset","preset":"board_cpbd_detail"}
```

### 原始上游结果

```bash
tdx_api_data mode="raw" entry="TdxSharePCCW.skef10_bk_cpbd_jczl" params=["003","880976","1m"]
```

## 结果处理

- 优先读取 `response.transformed.tables`
- `basic_info` 看估值、总市值和成分股概况
- `detail` 看板块属性和关联资产
- `stage_return` 看区间收益和排名
- `market_stats` 看成交、涨跌家数和市场热度
- 如果用户要求原始字段，再补充 `response.data`

## 输出要求

- 先说明查询对象、查询类型和调用参数
- 再总结关键指标、最新日期和排名信息
- 最后补充明细表、空结果或接口异常说明
