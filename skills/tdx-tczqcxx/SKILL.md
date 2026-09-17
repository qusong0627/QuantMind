---
name: tdx-tczqcxx
description: （原名：查询题材生命周期与持续性）用于题材生命周期与持续性判断，聚焦题材交易 / 市场周期 / 主题投资。Use when Codex needs to判断某个题材当前处于发酵、扩散、主升、分歧、退潮还是尾声阶段，题材是否具备持续性、是否仍是市场主线或只剩脉冲，哪个分支/个股是核心锚点，明日该看回流、扩散、分歧转一致还是退潮确认，或用户提到题材生命周期、主线持续性、热点轮动、题材退潮、题材强度、主升阶段、扩散强度等场景。
---

# TDX Theme Lifecycle

## Overview

只使用当前项目 `src/` 已有工具：

- `tdx_screener`
- `tdx_quotes`
- `tdx_kline`
- `tdx_lookup_stock`
- `tdx_api_data`
- `wenda_news_query`
- `wenda_notice_query`
- `wenda_report_query`

核心目标不是罗列新闻，而是回答 3 个问题：

1. 这个题材现在处于哪个生命周期阶段
2. 它还有没有持续性，持续性来自哪里
3. 明天更该看强化、分歧回流，还是退潮确认

优先判断“题材结构”，再判断“核心个股”。不要先入为主地把强势股走势等同于题材强度。

如需更细的阶段定义和打分规则，读取 [references/lifecycle-rules.md](references/lifecycle-rules.md)。

## Modes

### 市场模式

用户没有指定题材，只问“当前主线是什么”“哪个方向还有持续性”“市场在交易什么”时使用。

### 题材模式

用户明确给了题材、方向、板块或主题词，例如“算力”“深海科技”“低空经济”“可控核聚变”。

### 个股代理模式

用户给的是具体股票，但问题本质上是在问它背后题材还能不能做、是否仍在主升、是否已经退潮。先做简化题材判断，再落到个股地位。

## Tool Routing

### 1. 市场环境

先看指数和短线环境，不要脱离市场单看题材：

```bash
tdx_quotes code="000001" setcode="1" hasHQInfo="1" hasExtInfo="1"
tdx_quotes code="399001" setcode="0" hasHQInfo="1" hasExtInfo="1"
tdx_quotes code="399006" setcode="0" hasHQInfo="1" hasExtInfo="1"
tdx_screener message="涨停" pageSize="20"
tdx_screener message="跌停" pageSize="20"
tdx_screener message="2连板" pageSize="20"
tdx_screener message="3连板" pageSize="20"
```

至少回答：

- 市场是否支持题材博弈
- 连板高度和梯队是否抬升
- 高位负反馈是否扩散
- 当前更像主升、轮动还是退潮

### 2. 题材强度与扩散

用户给了题材名称时，优先用 `tdx_screener` 从题材角度抓结构，不要只看单一代表股：

```bash
tdx_screener message="<题材> 涨停" pageSize="20"
tdx_screener message="<题材> 主力净流入" pageSize="20"
tdx_screener message="<题材> 涨幅前列" pageSize="20"
tdx_screener message="<题材> 2连板" pageSize="20"
```

重点看：

- 有没有成片涨停，而不是只有 1 只独苗
- 有没有 2 板、3 板或趋势中军承接
- 强度集中在龙头，还是能向分支扩散
- 题材是否连续多日反复活跃

如果用户给了板块代码或你已经明确板块代码，可补 `tdx_api_data` 看板块阶段表现和市场统计：

```bash
tdx_api_data entry="TdxSharePCCW.skef10_bk_cpbd_jczl" branch="003" code="880976" timeType="1m"
tdx_api_data entry="TdxSharePCCW.skef10_bk_cpbd_jczl" branch="004" code="880976"
```

### 3. 个股与题材映射

用户只给股票时：

1. 先用 `tdx_lookup_stock` 确认代码与 `setcode`
2. 用 `tdx_api_data` 查它属于什么热点链路
3. 再用 `tdx_quotes`、`tdx_kline`、`tdx_api_data` 判断它是龙头、中军、补涨还是后排

常用查询：

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_rdtc" code="000001" fixedTag="zttzbkz"
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_rdtc" code="000001" fixedTag="zttzztk"
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_rdtc" code="000001" fixedTag="sjcd"
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_jyds" code="000001" fixedTag="ztfx"
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_jyds" code="000001" fixedTag="zjlx"
tdx_quotes code="000001" setcode="0" hasHQInfo="1" hasExtInfo="1" hasProInfo="1" bspNum="5"
tdx_kline code="000001" setcode="0" period="4" wantNum="30" tqFlag="11"
```

### 4. 催化与信息面验证

只有当题材驱动逻辑存在争议，或需要判断持续性来源时，再补资讯与公告：

- `wenda_news_query`：验证政策、产业、海外映射、事件催化是否仍在发酵
- `wenda_notice_query`：验证公告、订单、回购、减持、停复牌等是否改变预期
- `wenda_report_query`：验证券商是否开始集中覆盖或一致预期是否已过高

不要把所有新闻都算作“新催化”。要区分新增催化、旧消息重复发酵、以及纯情绪扩散。

## Workflow

### 第一步：先定义分析对象

必须先说清楚当前分析的是：

- 市场主线候选
- 指定题材
- 某只个股所代理的题材周期

如果题材边界本身不清晰，要先指出边界模糊，不要把多个相邻概念硬合并成一个周期。

### 第二步：判断市场是否支持题材持续

题材持续性先服从市场环境。至少回答：

- 指数和风险偏好是配合还是拖累
- 涨停/跌停结构对题材接力是否友好
- 高位股是正反馈、震荡，还是亏钱效应扩散

市场不支持时，即使题材本身有逻辑，也要下调持续性预期。

### 第三步：判断题材所处阶段

至少区分以下阶段：

- 发酵试探
- 主线确认
- 扩散主升
- 高位分歧
- 退潮出清
- 尾声脉冲

阶段判断必须用结构证据支撑，不要只凭单日涨跌幅。

### 第四步：识别题材内部层次

必须把题材里的角色拆开：

- 情绪龙头
- 趋势中军
- 补涨分支
- 后排跟风
- 掉队或负反馈标的

不要只说“题材很强”，要回答是谁带队、谁承接、谁掉队。

### 第五步：评估持续性

持续性至少从以下 5 个维度判断：

1. 催化是否持续
2. 梯队是否完整
3. 资金是否回流
4. 是否具备中军和扩散能力
5. 负反馈是否开始压制

可按“弱 / 一般 / 较强 / 强”给结论；如需细分分值，按 [references/lifecycle-rules.md](references/lifecycle-rules.md) 执行。

### 第六步：给出明日观察点

不要只给静态结论。必须明确下一交易日应看什么：

- 龙头弱转强还是继续分歧
- 中军是否放量新高或继续承接
- 分支是否扩散，还是回流只剩核心
- 后排是否集中掉队
- 负反馈是否扩大到同题材其他个股

## Output Rules

固定输出以下 7 段：

1. 题材对象与催化定义
2. 当前生命周期阶段
3. 结构证据
4. 核心锚点与分层
5. 持续性判断
6. 明日观察点
7. 一句话交易结论

如果用户问的是个股，在第 4 段必须单列“该股地位”。

## Hard Rules

- 不要把单日大涨直接定义为新周期启动。
- 不要把消息热度高但无资金承接的方向说成主线。
- 不要把所有同概念个股视为同一强度，必须区分核心与后排。
- 不要臆造炸板率、封板率、板块成交额等工具没有直接给出的数字。
- 不要只复述新闻，必须解释“结构是否支持持续”。
- 市场处于退潮或高位强分歧时，要明确写出“降低预期、谨慎接力”。
- 如果题材边界不清、数据不足或无法稳定映射，必须说明局限，再给保守结论。
