/**
 * 本文件由 scripts/gen_prompts.py 自动生成，请勿手工编辑。
 * 源数据：仓库根目录 prompts/*.md（人类可读版本）。
 */

export interface PromptMeta {
  name: string;
  title: string;
  category: string;
  description: string;
  outputs: string;
  body: string;
}

export const PROMPTS: PromptMeta[] = [
  {
    name: 'quantbot-init',
    title: 'QuantBot 环境初始化',
    category: '平台运营',
    description: '首次使用 QuantBot 时，检查并安装 QuantMind 技能与人格配置',
    outputs: 'QwenPaw 技能池 + default 工作区',
    body: `请完成 QuantBot（QwenPaw）与 QuantMind 平台的对接检查：\n\n1. 检查技能池：确认 QuantMind 技能（daily-review、stock-research 等）是否已安装到我的工作区；\n2. 如缺失：读取 /quantmind/scripts/quantbot_init.sh 了解安装流程，或提示用户在服务器执行 \`bash scripts/quantbot_init.sh --skills-only\`（装技能）和 \`--persona-only\`（装人格）；\n3. 环境自检：确认你能访问后端 API（http://quantmind:8000，内部认证见 AGENTS.md）、能读写 /data/reports/、能 docker exec 到 quantmind 容器；\n4. 输出一份环境就绪清单：哪些能力可用、哪些缺失、如何补齐。`,
  },
  {
    name: 'quantmind-operations',
    title: '平台运营总指南',
    category: '平台运营',
    description: '模型训练（5 步流程、特征字典、AutoDL 节点）、模型管理、数据同步、RSS 新闻',
    outputs: '视具体操作而定',
    body: `我需要执行平台运营操作：{操作内容，如：训练 lightgbm / 查特征字典 / 更新今日数据 / 对接 RSS}。\n\n请读取 skills/quantmind-operations/SKILL.md，按对应章节执行（训练走 5 步：特征选择→训练目标→参数→执行→入库；特征类别以 /api/v1/models/feature-catalog 动态返回为准，勿硬编码）。遵守顶部运行环境契约；涉及数据同步请确认脚本执行结果；完成后给我操作结果速览。`,
  },
  {
    name: 'quantmind-deploy',
    title: '部署与运维',
    category: '平台运营',
    description: '一键部署、快速部署、数据库初始化、部署问题排查、服务健康检查',
    outputs: '服务状态报告',
    body: `我需要部署或排查 QuantMind 平台：{部署新服务器 / 排查部署失败 / 检查服务健康}。\n\n请读取 skills/quantmind-deploy/SKILL.md，按其中对应章节执行；服务健康检查可用 docker compose ps 与各服务 /health 端点。发现问题给出根因和修复步骤，重大变更操作前先向我确认。`,
  },
  {
    name: 'quantdb-sdk',
    title: 'QuantDB 数据查询',
    category: '平台运营',
    description: 'QuantDB API Key 配置、数据集目录、K线/财务/因子远程查询',
    outputs: '查询结果',
    body: `我需要查询量化数据：{查询内容，如：600519 最近 60 个交易日的日K / 某财报字段}。\n\n请读取 skills/quantdb-sdk/SKILL.md 获取数据集目录与查询方式，并配合 skills/quantdb-fields/SKILL.md 确认字段单位口径（volume=股、amount=万元等）。查询结果用表格给我，标注单位。`,
  },
  {
    name: 'quantdb-fields',
    title: '字段单位与口径速查',
    category: '平台运营',
    description: 'QuantDB 各数据集实测单位、口径与陷阱速查手册',
    outputs: '口径说明',
    body: `我在做数据分析/回测/报告时需要确认数据口径：{字段疑问，如：成交量单位是股还是手 / 股息率是百分数还是小数 / L2 逐笔数据怎么读}。\n\n请读取 skills/quantdb-fields/SKILL.md，直接给我该字段的单位、口径和已知陷阱，必要时给出验证方法。`,
  },
  {
    name: 'quantdb-data-structure',
    title: 'QuantDB 数据结构',
    category: '平台运营',
    description: '数据目录组织、Hive 分区、parquet 路径、代码格式、quantdb_hub 读取入口、quantdb vs quantcustom',
    outputs: '结构说明 / 查询路径',
    body: `我需要了解 QuantDB 本地数据结构或排查查不到数据：{问题，如：l1_factors 在哪 / dt 分区怎么写 / 600519.SH 还是 SH600519 / 因子挖掘产物落哪}。\n\n请读取 skills/quantdb-data-structure/SKILL.md（数据在哪、怎么组织、怎么读）；字段单位另查 skills/quantdb-fields/SKILL.md。说明 quantdb（官方只读）与 quantcustom（用户/挖掘产出）边界，给出可直接用的路径或 DuckDB 视图名。`,
  },
  {
    name: 'daily-review',
    title: 'A股每日复盘',
    category: '研究分析',
    description: '盘后专业复盘：指数、涨停梯队、行业轮动、资金面、L2 微观结构、推理信号复盘、次日方向研判',
    outputs: 'data/reports/daily_review/ + PDF 报告',
    body: `今天是 {日期，如 2026-08-29}，请给我做 A 股每日复盘。\n\n请读取 skills/daily-review/SKILL.md 并严格按照其固定流程执行（取数脚本 → 按模板写复盘 → 转 PDF → 落盘）：报告以 facts 数据为准，facts 里没有的数字不要写。最后给我 200 字以内的速览：市场方向、最强板块、明日关键位。`,
  },
  {
    name: 'market-analysis',
    title: '市场分析报告',
    category: '研究分析',
    description: '大盘快照版市场分析：核心指数、广度情绪、行业热力图、板块资金流、个股主力 Top20',
    outputs: 'data/reports/market_analysis/ + PDF 报告',
    body: `请给我做一份今日市场分析报告（大盘快照版）。\n\n请读取 skills/market-analysis/SKILL.md 并按其流程执行：跑取数脚本（market_analysis.py）→ 基于 facts 撰写解读 → Markdown → PDF（研报风）→ 落盘 data/reports/market_analysis/。最后给我核心结论速览：大盘状态、资金主线、值得关注的 3 个板块。`,
  },
  {
    name: 'stock-market-analysis',
    title: '个股/全市场深度分析',
    category: '研究分析',
    description: '全市场信号扫描、行业轮动、个股六维深度分析（基本面/估值/技术/资金筹码/情绪/风险）、CSV 导出',
    outputs: '分析报告 + CSV 导出',
    body: `我需要市场/个股深度分析：{全市场信号扫描 / 行业轮动分析 / 深度分析某只股票，如 600519 / 导出选股数据 CSV}。\n\n请读取 skills/stock-market-analysis/SKILL.md 并按对应章节执行，个股分析覆盖基本面/估值/技术/资金筹码/情绪/风险六个维度。报告数据必须来自接口真实返回，最后给出可操作结论。`,
  },
  {
    name: 'stock-picks',
    title: '每日股票推荐',
    category: '研究分析',
    description: '复盘后多维打分选股：L2 微观结构 + 模型融合分 + 仓位信号 + 板块强度 + 新闻情绪',
    outputs: 'data/reports/stock_picks/ + PDF 报告',
    body: `请基于最新复盘数据给我做今日股票推荐。\n\n请读取 skills/stock-picks/SKILL.md 并按其流程执行：先确认 data/reports/daily_review/ 有当日 stats（没有先跑复盘取数）→ 跑 pick_candidates.py 多维打分 → Top N 个股深度分析 → 综合报告 → PDF 落盘 data/reports/stock_picks/。最后给我候选榜和每只股票的一句入选理由。`,
  },
  {
    name: 'stock-research',
    title: '个股深度研究（多Agent）',
    category: '研究分析',
    description: '5 分析师并行（技术/新闻/资金情绪/基本面/市场）→ 多空辩论 → 研究经理汇总 → 研报 PDF',
    outputs: 'data/reports/stock_research/ + data/reports/trading_agents/ PDF',
    body: `请对 {股票名称及代码，如：贵州茅台 600519} 做一次个股深度研究。\n\n请读取 skills/stock-research/SKILL.md 并严格按多 Agent 流程执行：跑 research_data.py 取数 → 5 个分析师并行（用 prompts/ 下的角色提示词）→ 多空辩论 → 研究经理汇总 → MD 转 PDF → 落盘 data/reports/trading_agents/{市场}/{股票名}/（平台股票报告页可见）。最后给我结论速览：核心逻辑、多空关键分歧、风险点。`,
  },
  {
    name: 'trading-agents',
    title: '个股投研分析（智能体自主版）',
    category: '研究分析',
    description: '本地数据 → 多空子代理辩论 → 综合研判 → PDF 报告，不依赖容器投研管线',
    outputs: 'data/reports/trading_agents/ PDF 报告',
    body: `请用智能体自主模式深度分析 {股票名称及代码}。\n\n请读取 skills/trading-agents/SKILL.md 并按其流程执行：拉取本地数据（特征快照/风险初筛/推理分数/新闻）→ 组织多空子代理辩论 → 综合研判 → 生成 MD 报告 → 转 PDF 落盘 data/reports/trading_agents/{市场}/{股票名}/。最后给我投资论点摘要和主要风险。`,
  },
  {
    name: 'smart-strategy-stock-picking',
    title: '条件选股',
    category: '研究分析',
    description: 'QuantDB 条件选股：自然语言/结构化/DSL 三种方式，可选登记为全局股票池',
    outputs: '股票池列表',
    body: `请帮我选股，条件：{自然语言条件，如：市值 100-500 亿、PE < 30、近 20 日主力资金净流入、行业为半导体}。\n\n请读取 skills/smart-strategy-stock-picking/SKILL.md：优先 parse-text 或 query-pool 执行 DSL 筛选。结果按市值/涨跌幅排序给表格，注明单位与数据截止日期；超出 50 只只展示前 50。若用户要求持久化股票池，提示在管理后台「全局股票池」保存，或通过 legacy 保存桥接到 v2（pool:code）供回测/训练/推理使用。`,
  },
  {
    name: 'batch-inference-analysis',
    title: '批量推理信号分析',
    category: '研究分析',
    description: '每日推理信号解读：行业轮动、个股分数区间、负分参考、市场状态判断',
    outputs: '信号解读报告',
    body: `请分析最新一批模型推理信号：{指定日期或批次，留空则取最新}。\n\n请读取 skills/batch-inference-analysis/SKILL.md 并按其方法论执行：读取推理信号数据 → 分析行业分布与轮动 → 个股分数区间解读 → 负分参考。最后给我：市场状态判断、信号最集中的 3 个行业、Top 5 高分股与风险提示。`,
  },
  {
    name: 'news-sentiment-research',
    title: '新闻情绪研究',
    category: '研究分析',
    description: 'RSS 历史新闻情绪研究：事件研究、七维深度分析、融合规律优化回测、研报输出',
    outputs: '研报 MD + PDF',
    body: `我想研究新闻情绪对股价的规律：{研究主题，如：利好新闻后 5 日收益分布 / 情绪强度与后续涨幅关系}。\n\n请读取 skills/news-sentiment-research/SKILL.md 并按方法论执行（数据源为 Huntly RSS 历史新闻 + FinBERT 情绪），跑对应 backtest_news_*.py 脚本，输出研报级 MD + PDF。结论必须基于数据，样本量不足时明确说明。`,
  },
  {
    name: 'news-sentiment-finbert',
    title: '新闻情绪管线运维',
    category: '平台运营',
    description: 'FinBERT 中文金融情绪识别：安装、权重下载、字典扩充、全量重算、排查',
    outputs: '运维结果',
    body: `新闻情绪功能需要运维：{情绪不生效 / 情绪都是中性 / 重新安装 FinBERT / 扩充情绪词典 / 触发全量重算}。\n\n请读取 skills/news-sentiment-finbert/SKILL.md，按对应章节排查或执行。涉及全量重算的操作先告诉我预计耗时，经我确认后再执行。`,
  },
  {
    name: 'ai-ide-strategy-writing',
    title: 'AI 写量化策略',
    category: '策略·因子·模型·回测',
    description: 'AI 生成 Qlib 量化策略代码、Docker 容器执行、策略落库',
    outputs: '策略代码 + 落库结果',
    body: `请帮我写一个量化策略：{策略想法，如：低波动+高股息双因子选股，每周调仓}。\n\n请读取 skills/ai-ide-strategy-writing/SKILL.md，按其规范生成 Qlib 策略代码，在 Docker runner 中执行验证可运行，然后落库保存。给我策略逻辑说明、代码位置和执行结果。`,
  },
  {
    name: 'backtest-center',
    title: '回测中心',
    category: '策略·因子·模型·回测',
    description: 'Qlib 回测：快速/专家模式、向量化极速回测、策略对比、参数优化、全局股票池 pool_id',
    outputs: '回测结果报告',
    body: `我需要回测：{快速回测 / 专家模式 / 对比策略 / 参数优化 / 查历史}，市场 {CN/HK/US/CRYPTO/FUTURES}，股票池 {csi300 / pool:自定义池code / 留空全市场}。\n\n请读取 skills/backtest-center/SKILL.md 按对应模式操作。选股范围优先用 pool_id（如 pool:csi1000），与前端全局股票池一致；纯 TopK 策略可试 use_vectorized=true 极速引擎（不安全策略会自动退回 step 模式）。按市场切换 provider_uri/基准。结果给我年化收益、最大回撤、夏普比率，并说明结论是否稳健。`,
  },
  {
    name: 'rd-agent-factor-mining',
    title: '因子挖掘（RD-Agent）',
    category: '策略·因子·模型·回测',
    description: 'factor_pipeline 一键管线：preflight → 演化 → 回测 → IC 排序 → explain → export 至 quantcustom',
    outputs: '因子报告 + quantcustom 入库',
    body: `请帮我挖掘新因子：方向「{挖掘假设，如：筹码集中度上行伴随低位换手放大}」，股票池 {csi300 / 自定义全局池 code}，市场 {a_share 等}。\n\n请读取 skills/rd-agent-factor-mining/SKILL.md：先跑 scripts/alpha_agent/factor_pipeline.py --check-env；再用 --direction / --universe / --loops 走一键管线（演化→回测→排名→可选 --explain-top / --export）。universe 支持内置指数与全局自定义池 code。产物落 /data/quantcustom（勿写 quantdb）。耗时长，分段汇报；最后给 Top 因子 IC/Sharpe 与是否 export 成功。`,
  },
  {
    name: 'model-train-infer-backtest-report',
    title: '训练-推理-回测-报告全流程',
    category: '策略·因子·模型·回测',
    description: 'T+N 周期模型训练（13 种模型）→ 批量推理 → 组合回测（阈值+大盘MA+止损）→ 研报输出',
    outputs: '研报 MD + PDF',
    body: `请走完「训练 → 推理 → 回测 → 报告」全流程：模型类型 {lightgbm/xgboost/lstm/transformer 等 13 选 1}，周期 T+{N}，市场 {CN/HK/US/CRYPTO/FUTURES}。\n\n请读取 skills/model-train-infer-backtest-report/SKILL.md 并按流程执行：提交训练 → 等待完成 → 批量推理 → 自定义组合回测（分数阈值 + 大盘 MA 过滤{+止损}）→ 导出研报 MD+PDF。训练耗时长，分段汇报；最后给我 T+N 周期对比与效益分析结论。`,
  },
  {
    name: 'simulation-trading',
    title: '模拟交易',
    category: '交易',
    description: '模拟盘下单买卖、持仓管理、成交查询、资金快照',
    outputs: '交易结果',
    body: `请帮我操作模拟交易：{买入/卖出 某股票及数量 / 查持仓 / 查账户与资金 / 查成交记录}。\n\n请读取 skills/simulation-trading/SKILL.md，通过 /api/v1/simulation/* 接口执行。下单前把订单要素（代码、方向、数量、价格）列给我确认后再提交；完成后返回成交结果与最新持仓。`,
  },
];
