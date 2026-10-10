# arena → QuantMind 实盘栏 移植清单（自动生成，勿手改）

- 源：`/home/zbox/quant-Trader/arena/src`（arena 仓库 bd228b7，2026-10-08T12:42:46+09:00，工作区 干净）
- 生成时间：2026-10-10T04:25:27.847Z
- 重新同步：`node electron/src/features/local-live/agent-arena/tools/port-from-arena.mjs`

## 搬运的文件（68 个）

- `api/client.ts`
- `components/ChannelStatus.css`
- `components/ChannelStatus.tsx`
- `components/ChatStream.tsx`
- `components/CompConfigPanel.tsx`
- `components/CompletedFeed.css`
- `components/CompletedFeed.tsx`
- `components/EquityChart.tsx`
- `components/KLineChart.tsx`
- `components/LiveDetails.css`
- `components/LiveDetails.tsx`
- `components/ModelCard.tsx`
- `components/ModelChat.css`
- `components/ModelChat.tsx`
- `components/Navbar.css`
- `components/Navbar.tsx`（替身）
- `components/NewsAgentChat.css`
- `components/NewsAgentChat.tsx`
- `components/NewsProtocolView.css`
- `components/NewsProtocolView.tsx`
- `components/NewsStream.css`
- `components/NewsStream.tsx`
- `components/RealAccountPanel.tsx`
- `components/Tables.tsx`
- `components/lab/AgentChatPanel.tsx`
- `components/lab/BatchPanel.tsx`
- `components/lab/BatchRankTable.tsx`
- `components/lab/BatchScreener.tsx`
- `components/lab/NotePanel.tsx`
- `components/lab/ResultView.tsx`
- `components/lab/StrategyList.tsx`
- `components/lab/StrategyPanel.tsx`
- `components/lab/Workbench.tsx`
- `components/lab/format.ts`
- `components/lab/indicators.ts`
- `components/lab/labResult.ts`
- `components/lab/useWorkbench.ts`
- `hooks/usePolling.ts`
- `pages/About.css`
- `pages/About.tsx`
- `pages/Control.css`
- `pages/Control.tsx`
- `pages/DataPlatform.css`
- `pages/DataPlatform.tsx`
- `pages/Live.css`
- `pages/Live.tsx`
- `pages/MarketLab.css`
- `pages/MarketLab.tsx`
- `pages/ModelDetail.css`
- `pages/ModelDetail.tsx`
- `pages/TradingSettings.css`
- `pages/TradingSettings.tsx`
- `styles/globals.css`
- `utils/actionTags.tsx`
- `utils/agents.ts`
- `utils/buyTime.ts`
- `utils/channelStatus.ts`
- `utils/datetime.ts`
- `utils/dayFilter.ts`
- `utils/equity.ts`
- `utils/format.ts`
- `utils/liveFills.ts`
- `utils/markdown.tsx`
- `utils/modeTag.tsx`
- `utils/newsProtocol.ts`
- `utils/parseAnalysis.ts`
- `utils/positions.ts`
- `utils/symbols.ts`

## 本地改写

- api/client.ts：baseURL 由 nginx 同源反代 /api 改为 QuantMind 后端代理 /api/v1/agent-arena；前端补 QuantMind Bearer
- api/client.ts：A6-③ 头注释去「FastAPI 8091 / nginx token 注入」（原独立 API 服务已退役）
- api/client.ts：富途恢复（2026-10-08）：注释改「自建 OpenD 容器」口径（原 BayMax 出处已退役）
- api/client.ts：富途恢复：reshape 抽成导出纯函数（单卡/双卡共用；边界口径对齐 reshapeQmtAccount——price=0 不给盈亏段、cost<0 只留绝对盈亏）
- api/client.ts：富途恢复：双卡 fetch 改调导出的 reshapeFutuAccount（原内部闭包删除，单卡/双卡同一口径）
- api/client.ts：A6-③ 迅投 QMT 注释去 BayMax（QMT 通道本身在，经 /qmt/account 直连 Redis 桥）
- api/client.ts：富途恢复（2026-10-08）：市场感知账户路由注释（hk 已恢复、us 仍下线）
- api/client.ts：富途恢复：订单历史注释回「直连」口径（去 BayMax 出处）
- api/client.ts：富途恢复：市场感知成交路由注释（hk 已恢复、us 仍下线）
- api/client.ts：富途恢复：已平仓注释回「直连」口径（去 BayMax 出处）
- api/client.ts：下单面板：FutuOrderRaw 导出（面板「当前委托」列表复用同一形状）
- api/client.ts：富途恢复 + 下单面板（2026-10-08）：closed 双形状容错；追加 place/cancel/orders 三个写读函数（QM 本地新增，后端 /futu/* 契约）
- api/client.ts：分析触发复活（2026-10-08）：注释改走 QM 原生 /analysis/* 台账
- api/client.ts：triggerLiveAnalysis → QM 原生 /analysis/trigger（type=live）
- api/client.ts：triggerNewsAnalysis → QM 原生 /analysis/trigger（type=news）
- api/client.ts：fetchAnalysisJobs → QM 原生 /analysis/jobs
- api/client.ts：A6-① 美股实盘注释 → 通道已下线
- api/client.ts：A6-① fetchIbkrAccount 函数体 → 直接失败
- api/client.ts：A6-① fetchIbkrOrders → 直接失败（不发请求）
- api/client.ts：T3-2（审计 C5）：LogLine 增 data_gaps —— 数据缺口标记随日志条目走，横幅判据=字段存在（不靠正文刮擦）
- components/ChatStream.tsx：T3-2：合规尾注引入宿主 compliance 单一来源组件（AI 生成提示 + 统一免责横条，勿抄字面量）
- components/ChatStream.tsx：T3-2（审计 C5）：MixedRound 增 dataGaps 字段（跨行日志把缺口标记并进回合）+ McDisclaimer 合规尾注组件
- components/ChatStream.tsx：T3-2（审计 C5）：缺口标记按日志行并进当前回合（去重）——A股单行含 user+assistant，标记挂在同一行也要能落到回合上
- components/ChatStream.tsx：T3-2：空态也挂免责尾注（列表空时同样是 AI 展示面口径）
- components/ChatStream.tsx：T3-2（审计 C5）：桥不可达轮挂「无实盘账户数据」横幅（判据=data_gaps 字段；常显不折叠），防 LLM 幻觉持仓点评被当事实读
- components/ChatStream.tsx：T3-2：列表尾部挂 McDisclaimer（有分析记录时也出免责）
- components/EquityChart.tsx：日终账本线（real-ledger-*）的 tooltip 尾注（2026-10-08）：该线是实盘账户金额序列，不带前缀会落到「虚拟净值（¥10万起步）」兜底文案（对 ¥91.8 万实账是假的）；dollar 口径下 ±% 是「自首个记录日」，把窗口写清楚
- components/EquityChart.tsx：E1 成交标记 x 先换算到联合轴序号（nearestIdxOfTime 给的是本线序号）
- components/EquityChart.tsx：E1 对账台阶标注的可见窗口判断同样换算联合轴序号
- components/LiveDetails.tsx：A6-① 美股执行通道说明 → 通道已下线
- components/LiveDetails.tsx：富途恢复（2026-10-08）：账户快照时效行 hk 换回「未读通」，只留 us「通道已下线」
- components/ModelCard.tsx：品牌色 --model-accent 由「仅选中时注入」改为**恒注入**：宿主打磨层（qm-arena-theme.css）要靠它给每张卡上左侧竖条/logo 底色（未选中时也得有值）。选中态的 inline 边框/底色/投影逻辑不变
- components/ModelChat.tsx：A6-③ 工具调用行注释去 baymax_memory 字面量
- components/Navbar.tsx ← tools/overrides/Navbar.tsx.tpl（整文件替身）
- components/RealAccountPanel.tsx：A6-① 美股 IBKR 卡头状态 →「通道已下线」
- components/RealAccountPanel.tsx：A6-① 美股 IBKR 卡体降级文案 →「通道已下线」
- components/RealAccountPanel.tsx：A6-① 美股说明块 → 通道已下线口径
- components/RealAccountPanel.tsx：A6-③ 通达信桥通道说明去 BayMax
- components/RealAccountPanel.tsx：日终账本曲线改金额口径（2026-10-08）。pct 以「首个记录日」=100 归一，而首日 08-13 是中途起点（账户在建账前已在交易）：曲线读成「自 08-13 亏 6.1%」，与同屏卡片（总资产 ¥918,398 / 日收益 +0.05%）、桥「累计盈亏」互相打架。实账总资产本身就是唯一连续的真实金额序列，改 dollar 后金额轴直接可读、曲线右端=卡片总资产（虚账曲线的 ¥10 万起点归一保留在净值图，两边口径不再混淆）
- pages/About.tsx：A6-③ 展示层架构块去「FastAPI 8091 / Arena 8092 / nginx token」
- pages/Control.tsx：useNavigate 指向 arena 自己的路由（/model/:market/:agent），改成宿主的栏内下钻
- pages/Control.tsx：同上：nav(...) 由宿主 context 接管（调用点一行不改）
- pages/Control.tsx：三市场表格的 agent 列改用应用自己的短名（shortName，实况页/台账/详情页同款）：宿主里三列并排后每列只有 437px，全名 deepseek-v4-flash 单列要吃 125px，会把「收益率/回放截止」挤出卡外（见 qm-arena-overrides.css 顶部注释）。全名挂 title 保留
- pages/Control.tsx：同上：该列渲染短名 + title 全名
- pages/Live.tsx：useSearchParams 改走宿主内存态（不写交易台的 URL，避免与 ?tab= 深链互相踩）
- pages/Live.tsx：A6-③ 全局错误横幅去「baymax-api(8091) / ui-arena(8092)」
- pages/Live.tsx：富途恢复（2026-10-08）：实盘账户区块注释（hk 已恢复、us 仍下线）
- pages/Live.tsx：下单面板（QM 本地新增 2026-10-08）：import HkOrderPanel（生成物外的本地组件，勿由 arena 覆盖）
- pages/Live.tsx：下单面板：实盘 tab 港股分支在账户双卡后追加最小下单/撤单面板
- pages/Live.tsx：对话 tab「立即分析」按钮提示语改 QM 口径（复活后只出观点，不下单）
- pages/ModelDetail.tsx：详情页由路由参数改为宿主传入的 props（栏内下钻，不再注册路由）
- pages/ModelDetail.tsx：同上：签名收 props
- pages/ModelDetail.tsx：同上：useParams 取值删掉（改由 props）
- pages/ModelDetail.tsx：返回按钮：原先跳排行榜（未移植），改为回本栏
- pages/TradingSettings.tsx：A6-③ 交易所设置头注释去 BayMax（改 QuantMind trade 服务口径）
- pages/TradingSettings.tsx：A6-③ 实时交易状态卡描述去 BayMax
- pages/TradingSettings.tsx：A6-③ 用户信息单元格去 BayMax-Trader
- utils/channelStatus.ts：A6-③ 通道状态注释去 BayMax
- utils/channelStatus.ts：A6-① ibkrChannel 注释与文案 →「已下线」
- 函数式 setState 共 24 处裹 `asUpdater()`（本仓 tsc 的类型简化，运行时无影响）：
  - `components/ChatStream.tsx:121 setOpen`
  - `components/ChatStream.tsx:132 setSections`
  - `components/ChatStream.tsx:232 setExp`
  - `components/ChatStream.tsx:269 setExp`
  - `components/ChatStream.tsx:336 setExp`
  - `components/CompConfigPanel.tsx:43 setDraft`
  - `components/ModelChat.tsx:166 setOpen`
  - `components/ModelChat.tsx:385 setFolded`
  - `components/lab/BatchPanel.tsx:32 setRunId`
  - `components/lab/BatchScreener.tsx:50 setSid`
  - `components/lab/StrategyPanel.tsx:68 setParams`
  - `components/lab/useWorkbench.ts:382 setSel`
  - `components/lab/useWorkbench.ts:383 setParams`
  - `components/lab/useWorkbench.ts:446 setList`
  - `components/lab/useWorkbench.ts:611 setFullSpan`
  - `components/lab/useWorkbench.ts:613 setShowMarkers`
  - `components/lab/useWorkbench.ts:615 setShowIndicators`
  - `pages/DataPlatform.tsx:544 setSelected`
  - `pages/Live.tsx:352 setCompletedDates`
  - `pages/Live.tsx:1404 setSelectedModel`
  - `pages/TradingSettings.tsx:231 setBrokerCfgs`
  - `pages/TradingSettings.tsx:766 setValues`
  - `pages/TradingSettings.tsx:804 setValues`
  - `pages/TradingSettings.tsx:860 setValues`

## 与 arena 原版的行为差异（有意的）

1. **CSS 全部作用域化**到 `.qm-arena-root`：arena 的 neo-brutalism 自绘样式表直接进
   QuantMind 会污染 antd/tailwind 全局；`@keyframes` 同步改名（`spin` 等与宿主重名）。
2. **后端入口换成 QuantMind 代理**：`/api/v1/agent-arena/*`（后端注入上游 X-API-Token），
   前端只带 QuantMind 的 Bearer；arena 的 `/api/quantmind/*` 原样经代理透传（该路径在
   arena 后端本来就是反代回 QuantMind 的，行为不变）。
3. **导航由宿主接管**：arena 的 Navbar（整站路由）换成只保留 `MarketSwitcher` 的替身；
   `/model/:market/:agent` 由路由改为栏内下钻（`arenaNav` context + ModelDetail props）。
4. **宿主尺寸修正写在 `qm-arena-overrides.css`**（手写、不参与生成、在 globals.css 之后加载）：
   arena 在独立窗口里按整屏宽度排版，搬进交易台后内容区窄得多（1680 视口下约 1400px），
   按整屏写死的尺寸会撑破容器。目前一条：总控三市场卡片的表格改定宽列
   （`table-layout: fixed` + 实测百分比列宽 + 字号收到 10px），否则表比卡宽，
   最右一列被卡片的横向滚动吃掉（且字号必须写在 th/td 上 —— globals.css 有一条
   `table.data td { font-size: 11px }` 的元素选择器，写在 table 上不生效）。
   同一处还有个**代码侧**配套改写：该表首列改渲染 `shortName()` 短名（见 PATCHES），
   否则全名 `deepseek-v4-flash` 一列就要 125px。
5. **富途已恢复（2026-10-08），IBKR 仍下线，分析触发已复活**：港股富途账户通道改为
   **自建 futu-opend 容器**（docker/futu-opend，共享 rsa.key）+ QM 原生
   `/api/v1/agent-arena/futu/*`（account/account-both/orders/closed/snapshot 读面 +
   place/cancel 写面；REAL 写面 fail-closed：闸门关 403、未解锁 409）。最小下单/撤单
   面板是**生成物外的本地组件**（`agent-arena/hk-order-panel/HkOrderPanel.tsx`，
   由 Live.tsx 补丁挂到实盘 tab 港股分支）——不要把它挪进 `arena/**`。
   ibkr 账户通道（IB Gateway 未迁移）取数函数仍为**直接失败**（不发请求、不产生
   404 噪声），面板保留并显示「通道已下线」。
   「立即分析」两枚按钮**已复活**：改走 QM 原生 `/api/v1/agent-arena/analysis/*`
   （任务台账 `/data/logs/analysis_jobs.jsonl` + 宿主 cron
   `scripts/analysis_trigger_worker.py` 消费）；对话按钮提示语改为
   「只出观点，不下单」。行情回测的转写/对话/写库端点（runPineBacktest、
   savePineSource、sendChat、apply/revert 等）**已全量迁移（同批 2026-10-08）**：
   客户端函数恢复为上游原样（不再有「直接失败」补丁），API 面已挂
   `market_lab_family` 写端点；执行在宿主——cron 跑
   `scripts/pine_transpile_worker.py` / `scripts/pine_chat_worker.py` 消费
   `data/pine_*/queue/`。**宿主 cron 未装时按钮会一直排队中**（不产生假活）。
   **不要手改 `arena/**` 下的文件**，重跑本脚本即覆盖。
