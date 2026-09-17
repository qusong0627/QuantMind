---
name: tdx-lhbxwfg
description: （原名：查询龙虎榜席位风格）用于龙虎榜席位风格、资金行为、游资研究与短线交易分析，聚焦营业部席位买卖明细、净额、席位标签和短线博弈结构。适用于回答“这只票龙虎榜怎么看”“哪些席位在主导”“更像接力还是兑现”“能否整理成龙虎榜复盘、资金行为报告、游资观察或金融文章”等问题。优先使用本仓库已实现的 `tdx_api_data`、`tdx_lookup_stock`、`tdx_quotes`、`tdx_kline`、`wenda_news_query`、`wenda_notice_query`，必要时补充 `tdx_screener`、`tdx_indicator_select`。
---

# 龙虎榜席位风格

**Skill 分类**  
龙虎榜 / 资金行为 / 游资研究 / 短线交易

**适用场景**  
用户希望基于龙虎榜数据判断席位主导结构、资金进攻或兑现特征、游资线索、次日博弈价值，或需要把这些结论写成问答、报告、复盘、专栏文章。

**输出结构**  
1. 查询对象与日期  
2. 龙虎榜核心事实  
3. 席位主导结构  
4. 资金行为与风格判断  
5. 结合股价位置与题材催化的解释  
6. 风险与局限  
7. 结论或写作成稿

## 可用工具

- `tdx_lookup_stock`
  - 当用户只给股票名称、简称或别名时，先查代码与 `setcode`
- `tdx_api_data`
  - 核心工具，用于查询龙虎榜日期和当日明细
- `tdx_quotes`
  - 补当前价格、涨跌幅、换手率、盘口和成交强弱
- `tdx_kline`
  - 补日线位置、趋势阶段、量价配合
- `wenda_news_query`
  - 验证是否有事件、题材、新闻催化支持
- `wenda_notice_query`
  - 验证公告、异动、停复牌、回购、减持等信息
- `tdx_screener`
  - 仅在用户想看同题材扩散、涨停梯队、市场热度时补充
- `tdx_indicator_select`
  - 仅在用户需要公司属性、概念板块、产业链映射时补充

不要引用本仓库 `src/` 中不存在的工具名。

## 核心查询规则

### 1. 先确认标的

- 用户未给 6 位代码时，先用 `tdx_lookup_stock`
- 后续凡是调用 `tdx_quotes`、`tdx_kline`，都要带正确的 `setcode`

### 2. 先查龙虎榜可用日期

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_comreq" fixedTag="jglhb" code="000001"
```

- 这条路由在本仓库里已映射为 `dragon_tiger_dates`
- 优先读取 `response.transformed.tables`
- 没给日期时，默认先看最近可查日期

### 3. 再查指定日期龙虎榜明细

```bash
tdx_api_data entry="TdxSharePCCW.tdxf10_gg_jyds" code="000001" fixedTag="jglhb" extra="20221129"
```

- 这条路由在本仓库里已映射为 `dragon_tiger_list`
- 重点读取以下表：
  - `summary`
  - `details`
  - `seat_profiles`
  - `extra`

### 4. 需要价格语境时再补行情

```bash
tdx_quotes code="000001" setcode="0" hasHQInfo="1" hasExtInfo="1" bspNum="5"
tdx_kline code="000001" setcode="0" period="4" wantNum="20" tqFlag="11"
```

- `tdx_quotes` 用来确认涨跌幅、换手率、盘口和成交状态
- `tdx_kline` 用来判断是低位启动、中位加速、高位分歧还是高位兑现

### 5. 需要催化解释时再补资讯或公告

```bash
wenda_news_query name="平安银行" bdate="20260401" edate="20260409" keywords="异动,题材,利好"
wenda_notice_query name="平安银行" bdate="20260401" edate="20260409" keywords="公告,回购,减持"
```

## 数据解读顺序

1. 先看 `summary`，确认信息类型编码、总买入、总卖出、净额和总成交额  
2. 再看 `details`，识别买一到买五、卖一到卖五的席位结构  
3. 再看 `seat_profiles`，读取营业部标签代码和标签名称  
4. 最后看 `extra`，确认交易类型、排名、买卖标签等补充字段  

如果结构化结果不可用，再退回 `response.data` 原始字段，不要凭空改字段名。

## 分析框架

### 第一步：确认事实层

- 查清楚是哪一天的龙虎榜，不要把不同日期混写
- 说明是单日复盘还是结合多日连续上榜
- 说明信息类型、净买额、总成交额和核心席位名单

### 第二步：识别主导席位

- 看买方是否集中在 1 至 2 个核心席位
- 看卖方是否更强，是否出现明显兑现压力
- 看是否存在同一席位同时大额买卖
- 看席位标签是否提示机构、量化、游资或普通营业部特征

### 第三步：判断资金行为

- 更像主动做多、情绪接力、套利博弈、冲高兑现还是高位换手
- 把“事实”与“推断”分开写
- 风格判断只写“更像”“倾向于”“可能”，不要写成确定事实

### 第四步：结合走势与催化

- 把龙虎榜放回日线位置、量价状态和题材热度里解释
- 如果没有事件催化，只能解释成交易型博弈，不要硬写基本面变化
- 如果新闻或公告与上榜时间接近，再讨论持续性

### 第五步：转成适合场景的输出

- 问答场景：先给结论，再给 3 至 5 条证据
- 报告场景：按“事实、解读、风险、结论”展开
- 文章场景：先写市场背景，再写席位博弈，再写交易含义

需要更细的风格信号时，读取 [analysis-framework.md](./references/analysis-framework.md)。  
需要直接成稿时，读取 [output-templates.md](./references/output-templates.md)。

## 输出要求

- 必须写清查询日期、代码、工具和关键参数
- 必须区分“数据事实”和“风格推断”
- 必须说明龙虎榜只反映上榜席位，不等于全市场全部资金流向
- 必须避免把营业部名称直接等同于某个知名游资，除非名称本身已清晰且用户明确要求
- 必须避免把单日龙虎榜直接推导成未来涨跌结论
- 如果数据不足、无可用日期、接口异常或字段缺失，要直接说清楚
