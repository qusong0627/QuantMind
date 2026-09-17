---
name: tdx-ztltby
description: （原名：查询龙头博弈分析）用于涨停结构、连板梯队、短线情绪周期与龙头博弈分析，聚焦短线交易 / 情绪周期 / 龙头战法。Use when Codex needs to判断今天市场在打什么板、连板高度和梯队是否健康、谁是情绪龙头/趋势中军/补涨/后排、某只股票是否具备龙头博弈价值、明日该看弱转强还是分歧转一致，或用户提到涨停结构、连板、首板、换手板、封板质量、炸板、卡位、打板、接力、龙头战法、情绪冰点/修复/主升/退潮等场景。
---

# TDX Limit-Up Dragon Cycle

## Overview

只使用当前项目 `src/` 已有工具：

- `tdx_screener`
- `tdx_lookup_stock`
- `tdx_quotes`
- `tdx_kline`
- `tdx_api_data`
- `wenda_news_query`
- `wenda_notice_query`
- `wenda_report_query`

优先做“结构判断”，再做“个股判断”。先回答市场情绪和涨停结构是否支持博弈，再回答具体龙头能不能做、该博弈什么信号、什么情况下不该做。

如果用户没有指定个股，默认进入“市场模式”。如果用户给了具体股票、题材或“某股是不是龙头”之类的问题，先做一遍简化市场判断，再进入“个股模式”。

如需更细的情绪周期分层和角色划分，读取 [references/decision-rules.md](references/decision-rules.md)。

## Tool Routing

- 用 `tdx_screener` 看全市场结构，常用查询：
  - `涨停`
  - `跌停`
  - `2连板`
  - `3连板`
  - `主力净流入`
- 用 `tdx_quotes` 看指数、板块和个股强弱；个股模式优先打开 `hasHQInfo="1" hasExtInfo="1"`，需要更完整盘口时加 `hasProInfo="1" bspNum="5"`。
- 用 `tdx_kline` 看 5 日和 30 日结构，确认是主升、分歧、修复还是高位走弱。
- 用 `tdx_api_data` 拉个股交易结构：
  - `entry="TdxSharePCCW.tdxf10_gg_jyds" fixedTag="ztfx"` 看涨停分析
  - `entry="TdxSharePCCW.tdxf10_gg_jyds" fixedTag="zjlx"` 看资金流向
  - `entry="TdxSharePCCW.tdxf10_gg_comreq" fixedTag="jglhb"` 看龙虎榜可用日期
  - `entry="TdxSharePCCW.tdxf10_gg_jyds" fixedTag="jglhb"` 看龙虎榜明细
  - `entry="TdxSharePCCW.tdxf10_gg_rdtc" fixedTag="zttzbkz"` 看热点题材板块族谱
  - `entry="TdxSharePCCW.tdxf10_gg_rdtc" fixedTag="sjcd"` 看事件驱动
- 用 `wenda_news_query`、`wenda_notice_query`、`wenda_report_query` 验证催化、公告和拥挤度；只在事件驱动或逻辑存在争议时调用，不要无差别堆工具。

## Workflow

### 市场模式

1. 先判断市场是否支持短线博弈。至少查看上证、深成指、创业板指，再看 `涨停 / 跌停 / 2连板 / 3连板` 的分布。
2. 再判断涨停结构。重点回答：
   - 涨停家数是扩散还是收缩
   - 连板高度是否抬升
   - 2 板、3 板以上是否形成有效梯队
   - 高位股反馈是修复、分歧还是负反馈
3. 再区分主线和杂线。不要把一日脉冲误判为主线；优先识别“有梯队、有核心锚点、有资金回流”的方向。
4. 再判断情绪周期。必须给证据，不要只下结论。证据优先级：
   - 连板高度和梯队完整度
   - 高位股承接与负反馈
   - 低位首板扩散力度
   - 跌停压力和亏钱效应
5. 最后输出“明日博弈点”。明确说明明天应该盯：
   - 龙头弱转强
   - 分歧后的回流
   - 中军放量新高
   - 后排掉队
   - 是否出现卡位成功

### 个股模式

1. 用户只给名称时，先用 `tdx_lookup_stock` 解析证券代码和市场。
2. 用 `tdx_quotes` + `tdx_kline(period="4", wantNum="5")` 判断当下强弱，再用 `tdx_kline(period="4", wantNum="30")` 判断位置高低。
3. 用 `tdx_api_data fixedTag="ztfx"` 看该股涨停原因、首次涨停时间、封单金额和主题。
4. 用 `tdx_api_data fixedTag="zjlx"` 看主力净额是否持续支持。
5. 需要验证席位博弈时，先查 `jglhb` 可用日期，再查对应日期龙虎榜明细。
6. 需要判断该股属于哪个热点链路时，再补 `zttzbkz` 或 `sjcd`。
7. 按 [references/decision-rules.md](references/decision-rules.md) 把它归类为：
   - 情绪龙头
   - 趋势中军
   - 补涨
   - 后排跟风
   - 伪强
8. 最终只给“可执行的博弈结论”，例如：
   - 只适合分歧低吸，不适合追涨
   - 只看弱转强，不接受低开走弱
   - 只能当情绪观察标，不作为核心交易标的
   - 高位兑现风险大，放弃博弈

## Output Rules

固定输出以下 7 段，不要省略关键结论：

1. 市场环境
2. 涨停结构
3. 核心梯队与角色分层
4. 情绪周期判断
5. 龙头博弈结论
6. 明日观察点
7. 一句话交易结论

如果是个股模式，在第 3 段里必须单列“该股地位”，说清它是龙头、中军、补涨还是后排。

## Hard Rules

- 不要臆造工具没有直接提供的数据。比如没有可靠炸板率时，不要编造炸板率数字。
- 不要把所有涨停都说成机会。先判断结构，再谈参与。
- 不要只复述题材新闻，要说明“谁带队、谁跟风、谁掉队”。
- 市场处于退潮或高位强分歧时，要明确写“不适合无脑打板/接力”。
- 事实、推断、交易动作分开写。事实不足时，明确说证据不够。
- 当市场结构混乱、主线不清、梯队断层时，允许直接给出“降低预期、少做或不做接力”的结论。
