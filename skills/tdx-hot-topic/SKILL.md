---
name: tdx-hot-topic
description: （原名：查询个股热点题材）使用 `tdx_api_data` 查询个股热点题材信息。适用于按股票代码获取热点题材板块族谱、主题库、事件驱动和信息面概览，并根据 `fixedTag` 选择对应结构化结果。
---

# TDX Hot Topic

## 何时使用

当用户想查询个股热点题材相关信息时使用本 skill，典型场景包括：

- 热点板块归属、关联板块、关联度
- 热点题材主题库、题材内容
- 事件驱动、催化因素、触发原因
- 信息面栏目摘要、热点概览

这个 skill 不直接发起 HTTP 请求，统一使用 `tdx_api_data`。

## 入参规则

- `entry` 固定为 `TdxSharePCCW.tdxf10_gg_rdtc`
- 标准结构化路由的 `mode` 是 `code-fixed-tag`
- `code` 必须是 6 位股票代码字符串
- 如果显式传 `responseTransform`，要保证它与 `fixedTag` 对应一致；当前代码不会自动纠正不匹配的 preset
- 如果用户明确要求原始上游响应，使用 `mode="raw"` 并按上游顺序传 `params`

## 查询类型映射

| 查询类型 | 适用场景 | `fixedTag` | 默认 `preset` | 结果表名 |
|---|---|---|---|---|
| `board_family` | 查询热点题材板块族谱、关联板块、关联度、收录时间 | `zttzbkz` | `hot_topic_board_family` | `board_family` |
| `theme_library` | 查询热点题材主题库、主题归类、题材内容 | `zttzztk` | `hot_topic_theme_library` | `theme_library` |
| `event_driven` | 查询热点题材事件驱动、事件类型 | `sjcd` | `hot_topic_event_driven` | `event_driven` |
| `info_overview` | 查询热点信息面概览、栏目内容摘要 | `xxmmg` | `hot_topic_info_overview` | `info_overview` |

## 选择规则

- 用户想看“属于哪些热点板块、板块关系、关联度”，选 `board_family`
- 用户想看“题材库、主题内容、题材归类”，选 `theme_library`
- 用户想看“催化事件、触发因素、事件类型”，选 `event_driven`
- 用户想看“栏目摘要、信息面概览”，选 `info_overview`
- 如果用户只说“查热点题材”但没细分，优先用 `board_family`

## 调用方式

### 推荐写法：依赖自动推导

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_rdtc" code="000001" fixedTag="zttzbkz"