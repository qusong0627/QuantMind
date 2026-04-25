# Trading Tabs

## 接口调用约定

`SettingsCenter.tsx` 中涉及后端调用（例如 QMT Agent 凭证初始化、在线状态查询、配置模板下载）统一使用 `SERVICE_URLS.API_GATEWAY` 作为基地址，再拼接 `/api/v1/...` 路径。

原因：

- Electron/Vite 开发环境默认运行在 `http://127.0.0.1:3000`
- 本地 dev server 未配置通用 `/api` 代理
- 使用相对路径（如 `/api/v1/...`）会命中前端 dev server 并返回 `404`

示例：

```ts
const apiGatewayBase = SERVICE_URLS.API_GATEWAY.replace(/\/+$/, '');
fetch(`${apiGatewayBase}/api/v1/api-keys/init`, { method: 'POST' });
```

## 近期更新

- `StrategyManagement.tsx` 里的“手动执行调试”已改名为“手动任务”，并迁移为左侧功能菜单中的独立页面；原页内 Drawer 仅保留兼容代码，不再作为主入口。
- 实盘策略管理的策略选择下拉已从原生 `<select>` 改为 `antd Select`，弹层圆角、阴影和高亮态已统一到项目视觉语言。
- `SettingsCenter.tsx` 已移除底部“独立部署说明”说明卡，避免与底部导航区域发生遮挡。
- `TradingHistory.tsx` 的“导出”按钮已接入真实订单导出，按当前时间范围和搜索条件导出 CSV，而不是 mock 数据。
- `TradingHistory.tsx` 的订单列表现在会按 `今日 / 本周 / 本月 / 全部` 直接向后端查询对应时间区间，并随 `tradingMode` 自动切换到实盘或模拟盘订单源。
- `TradingHistory.tsx` 的时间列与导出时间统一使用后端时间解析辅助函数：无时区时间按 UTC 解释后再转上海时区，避免页面与导出出现 8 小时偏移。
- `TradingHistory.tsx` 的 Excel 导出已补齐字段映射：保持界面与 CSV 导出的“委托/成交”细粒度列不变，同时在导出 `.xlsx` 时映射为 `数量/价格/金额` 标准列，避免类型不匹配导致构建失败。
- `PositionMonitor.tsx` / `positionMetrics.ts` 已修复持仓明细盈亏金额口径：兼容 `positions` 为数组或字典两种结构，优先读取 `symbol/stock_code`、`cost_price/avg_price`、`last_price/current_price`、`unrealized_pnl`，避免展示出股票代码 `0`、成本价 `0` 但盈亏金额异常偏大的问题。
- 当柜台上报 `last_price=0` 或 `cost_price=0` 时，前端会回退使用 `market_value / volume` 推导现价，并在成本缺失时以推导现价作为临时成本，避免“现价为 0、盈亏金额=持仓市值”的误导展示。
- `PositionOverview.tsx` 的持仓明细表格已启用横向滚动和最小宽度，列标题及金额列默认不换行，避免窗口较窄时“万”换行和列挤压。
- `PersonalCenter.tsx` 的模拟盘初始金额修改/重置提示已收敛为“已保存并重置，资金快照已更新”，与后端即时采集行为保持一致。
- `PersonalCenter.tsx` 的按钮提交态文案已同步为“保存并重置中... / 重置并采集中...”，避免用户误解当前操作仅是单纯改配置。
- `PersonalCenter.tsx` 在保存/重置成功后会短暂显示“今日快照已更新”，用于明确告诉用户历史资金口径已写入当日快照。
- `StrategyStatus.tsx` 与 `PositionMonitor.tsx` 的持仓分布/持仓明细已收敛到共享组件 `../components/PositionOverview.tsx`，避免两套页面分别维护图表和表格逻辑。
- `StrategyStatus.tsx` 的持仓分布已切换为真实账户快照驱动，不再使用固定 `10W+` / mock 饼图数据。
- `StrategyStatus.tsx`、`PersonalCenter.tsx` 与 `RealTradingPage.tsx` 现在统一通过运行态选择账户来源，不再在页面内分别判断“实盘优先 / 模拟兜底”；统一选择逻辑抽到了 `trading/utils/accountAdapter.ts` 与 `realTradingService.getRuntimeAccount()`。
- `trading/utils/accountAdapter.ts` 已按 `/account` 规范化字段统一顶部账户条语义：
  - 金额字段优先使用 `daily_pnl / total_pnl / floating_pnl`；
  - 收益率优先使用 `daily_return_ratio / total_return_ratio`，其次回退 `*_pct`；若与 `pnl + baseline` 推导结果明显冲突，则统一回退到推导值；
  - “初始权益”固定按 `total_asset - total_pnl` 计算，`day_open_equity / month_open_equity` 仍优先读取基线字段。
- `StrategyManagement.tsx` 的“运行环境 / 传输连接”已改为真实预检与 WebSocket 状态驱动，前端不再展示静态的容器镜像、K8s Cluster Active、Redis Connected 等占位值。
- `StrategyManagement.tsx` 的“运行环境”已改成“标题 + 状态 + 下方原因”结构；“传输连接”仍保持原有左右对齐版式，仅将静态占位值替换为真实预检与 WebSocket 状态。
- `StrategyManagement.tsx` 的运行环境原因文案已统一为短状态词（如 `已配置 / 已就绪 / 已连接 / 已上报`），完整后端提示保留在悬停 `title` 中，避免卡片被长文本拉高；其中 `K8s 状态` 已调整为更准确的 `运行容器状态`。
- `RealTradingPage.tsx` 顶部资产概览已增加当前运行模式徽标，`StrategyManagement.tsx` 策略控制卡也会直接显示 `影子运行中 / 实盘运行中 / 模拟运行中`，用于区分影子模式和实盘模式。
- `TopBar.tsx` 顶部徽标已细分为 `当前运行模式` 与 `部署通道` 两项，便于区分影子模式、实盘和模拟盘的部署路径。
- `TopBar.tsx` 的徽标颜色已按运行语义区分：影子偏紫、实盘偏蓝、模拟偏橙。
- `TopBar.tsx` 的顶部徽标尺寸与间距已收紧，默认保持单行并排显示。
- `TopBar.tsx` 的头部标题、徽标和右侧状态块间距已进一步压缩。
- `TopBar.tsx` 的 8 个指标格已统一标签为 `总资产 / 初始权益 / 可用资金 / 持仓市值 / 总盈亏 / 今日盈亏 / 浮动盈亏 / 持仓数量`，其中盈亏类副标签分别固定展示 `总收益率 / 日收益率 / 持仓收益率 / 仓位占比`，避免不同页面出现“累计盈亏/总收益/今日实时”等混杂命名。
- `TopBar.tsx` 运行态文案新增 `策略启动中`，避免后端 `status=starting` 时被误显示为 `策略已停止`。
- `RealTradingPage.tsx` 新增运行模式兜底：当 `/real-trading/status` 未返回 `mode` 但状态为 `running/starting` 时，按当前交易模式推断 `REAL/SIMULATION`，避免顶部徽标显示 `未启动/未识别`。
- `TopBar.tsx` 的“部署通道”改为读取 `/real-trading/status.orchestration_mode` 动态显示 `Docker/Kubernetes`；若后端暂未返回该字段，则以 `容器` 兜底，避免误导性写死 `K8s`。
- `RealTradingPage.tsx` 的启动前自检新增二次确认步骤：`REAL/SHADOW/SIMULATION` 在自检通过后不会自动调用启动接口，需用户点击底部“确认并启动…”按钮后才真正拉起运行容器/沙箱。
- `RealTradingPage.tsx` 的交易记录页已改为透传当前 `tradingMode`，不再硬编码 `real`，避免影子/模拟场景下因模式错筛导致“数据库有数据但列表为空”。
- `StrategyManagement.tsx` 的运行态徽标也已跟随同一套语义色，便于和顶部概览一致识别。
- `StrategyManagement.tsx` 的 `QMT Agent` 状态文案已细分为 `已上报 / 已过期 / 未上报 / 检测异常`，并在过期场景显示心跳与账户快照秒数（例如 `已过期（账户1038s，心跳1036s）`），避免将“过期”误显示为“未上报”。
- `StrategyManagement.tsx` 已新增“当前生效推理批次”状态卡，直接展示 `/api/v1/models/inference/latest` 返回的 `run_id / prediction_trade_date / status / updated_at`，并标注当前模型是否与最新生效批次匹配，便于在实盘页确认正在消费的推理结果。
- `StrategyManagement.tsx` 已新增“最新任务汇报”卡，直接消费 `/api/v1/real-trading/status.latest_hosted_task / latest_signal_run_id / signal_source_status`，展示最近一轮托管任务的 `task_id / run_id / 状态 / 触发上下文 / 完成度 / 成功失败跳过统计`，并可直接在控制台内查看任务日志流；无任务时会展示最新信号状态与未触发原因。
- `StrategyManagement.tsx` 的“默认模型推理反馈与诊断”已按状态来源拆分文案：`missing / window_pending / expired / fallback / mismatch / ready`，避免把“窗口未到”“已过期”“兜底拦截”都混成“未检测到最新完成推理”。
- `StrategyManagement.tsx` 的“最新任务汇报”视觉已调整为浅色风格，任务状态卡、统计卡与操作按钮统一改为浅底+浅边框，避免与深色日志流区域混淆。
- `StrategyManagement.tsx` 的日志流框也已切换为浅色样式，保持与任务汇报卡一致的浅色视觉层级。
- `StrategyManagement.tsx` 的“链路质量看板”和“自动化状态”已改成“上方总览 + 下方指标网格”的一致结构，减少纵向空洞与卡片层级跳变，让两张卡在同一行内更协调。
- `StrategyManagement.tsx` 的“链路质量看板”左侧状态已改为纯色圆点图标，字段名统一为 `Redis Signal / PostgreSQL / Data Feed / WebSocket` 这类标准大小写命名，并收紧字距与行距，避免状态文案挤占换行。
- `StrategyManagement.tsx` 的“环境监控”指标行已改为左侧信息、右侧状态的双栏布局，`正常/异常/未获取` 状态徽标在小卡片中线居中对齐。
- `StrategyManagement.tsx` 的“自动托管就绪度”结论卡已把状态徽标限制在标题行，说明正文独占整行宽度；包含截止日期的文案会把 `截止日期=...` 单独下沉到第二行，并保护“可执行窗口”短语不被拆行。
- `StrategyManagement.tsx` 顶部“影子模式”已改为开关样式的 `role="switch"` 控件，保留原逻辑但让控制条更紧凑，不再像传统 checkbox。
- `StrategyManagement.tsx` 的“核心决策结果”卡已收敛为“默认模型最新推理”，只展示当前默认模型最近一次可用于托管的完成推理批次；空态改为“暂无可用推理批次”，并回显信号来源诊断。
- `StrategyManagement.tsx` 的默认模型最新推理卡在 `run_id` 变化时会短暂显示 `NEW` 标签，帮助识别刚刷新出来的新推理批次。
- `ManualTaskPage.tsx` 已重构为 5 步引导式执行向导：
  - 第 1 步选择模型；
  - 第 2 步只加载当前模型下的 `completed` 推理批次，并支持查看信号排序；
  - 第 3 步选择已验证策略；
  - 第 4 步调用 `/real-trading/manual-executions/preview` 生成结构化调仓预案，展示账户快照、卖出清单、买入清单和跳过原因；
  - 第 5 步确认后再调用 `/real-trading/manual-executions` 创建正式任务，并继续展示任务级日志流。
- 当前引导式执行首版只支持 `REAL`，不在该页面内触发新推理。
- “提交完成”在页面中明确解释为“已完成派发尝试”，不等价于柜台最终成交；用户需要继续到订单/成交历史页确认最终受理与成交状态。
- 实盘策略管理引导增强（2026-03-13）：
  - `StrategyManagement.tsx` 不再在页内直接编辑零散风控参数，改为在点击“启动实盘/开启影子运行/开启实时模拟”后拉起执行参数向导；
  - 向导统一采集 `execution_config + live_trade_config`，包含调仓周期、执行时段、买卖时间点、委托方式和风险保护；
  - 运行中状态会在策略页回显当前生效的调仓周期、买卖时点与执行风控快照。
- QMT Agent 接入收敛（2026-03-14）：
  - `SettingsCenter.tsx` 已下线 PTrade 入口，只保留 QMT Agent；
  - 新增 `Access Key / Secret Key` 双密钥初始化与重置；
  - 新增 `QMT Agent 在线状态` 卡片，展示心跳、资金账号、终端名称与 Agent 版本；
  - 下载入口当前固定绑定为 `https://cos.quantmind.cloud/update/QMTAgent.zip`（文件名 `QMTAgent.zip`）；
  - 布局已收敛为单主容器：左侧集中展示接入凭证与客户端下载，右侧集中展示在线状态和更多信息；
  - 左右两列已统一为等宽双列、等高主容器，左右外层背景均为白底；
  - 无数据时不再使用 `-` 作为占位，统一显示 `无数据上报`；
  - 门禁状态码已翻译为中文，例如 `heartbeat_stale -> 心跳已过期`、`account_snapshot_stale -> 账户快照已过期`；
  - 当前前端下载的是独立 QMT Agent 部署包，不是单独的配置模板，也不是和 Electron 绑定的本机控制模块；
  - `SettingsCenter.tsx` 当前只负责用户维护、远端绑定状态和独立安装包下载，不直接控制客户侧 Agent。

## 下次开发提示

- 若继续完善客户端交付，优先补齐 Windows `.exe` / 安装器产物与自动升级；
- 若继续调 UI，保持“左侧接入、右侧状态”的双列结构，不再新增额外彩色说明容器；
- 若右侧显示 `无数据上报`，优先从服务端和 Redis 校验 QMT Agent 是否仍在上报账户快照与心跳。
