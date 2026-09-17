"""一次性生成 prompts/ 提示词库（技能中心数据源）。"""
import os

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts"))

# (name, title, category, description, outputs, body)
P = []

P.append(("quantbot-init", "QuantBot 环境初始化", "平台运营",
    "首次使用 QuantBot 时，检查技能、人格与平台对接是否就绪",
    "dsh 技能/人格就绪清单",
    """请完成 QuantBot（dsh / DeepSeek Harness）与 QuantMind 平台的对接检查：

1. 检查技能：列出你能看到的技能（应来自 /root/.dsh/skills，含 daily-review、stock-research、quantdb-fields 等）；
2. 检查人格与规则：确认 /root/.dsh/AGENTS.md（技能路由表、平台 API、挂载地图、术语映射）已生效，复述你的身份与关键挂载路径；
3. 环境自检：确认你能访问后端 API（http://quantmind:8000，内部认证见 AGENTS.md）、能读写 /data/reports/、能 `docker exec quantmind` 跑重依赖脚本；
4. 输出一份环境就绪清单：哪些能力可用、哪些缺失、如何补齐（缺失时给出修复命令，如 `docker compose restart dsh`）。"""))

# ---- 平台运营 ----
P.append(("quantmind-operations", "平台运营总指南", "平台运营",
    "模型训练（5 步流程、特征字典、AutoDL 节点）、模型管理、数据同步、RSS 新闻",
    "视具体操作而定",
    """我需要执行平台运营操作：{操作内容，如：训练 lightgbm / 查特征字典 / 更新今日数据 / 对接 RSS}。

请读取 skills/quantmind-operations/SKILL.md，按对应章节执行（训练走 5 步：特征选择→训练目标→参数→执行→入库；特征类别以 /api/v1/models/feature-catalog 动态返回为准，勿硬编码）。遵守顶部运行环境契约；涉及数据同步请确认脚本执行结果；完成后给我操作结果速览。"""))

P.append(("quantmind-deploy", "部署与运维", "平台运营",
    "一键部署、快速部署、数据库初始化、部署问题排查、服务健康检查",
    "服务状态报告",
    """我需要部署或排查 QuantMind 平台：{部署新服务器 / 排查部署失败 / 检查服务健康}。

请读取 skills/quantmind-deploy/SKILL.md，按其中对应章节执行；服务健康检查可用 docker compose ps 与各服务 /health 端点。发现问题给出根因和修复步骤，重大变更操作前先向我确认。"""))

P.append(("quantdb-sdk", "QuantDB 数据查询", "平台运营",
    "QuantDB API Key 配置、数据集目录、K线/财务/因子远程查询",
    "查询结果",
    """我需要查询量化数据：{查询内容，如：600519 最近 60 个交易日的日K / 某财报字段}。

请读取 skills/quantdb-sdk/SKILL.md 获取数据集目录与查询方式，并配合 skills/quantdb-fields/SKILL.md 确认字段单位口径（volume=股、amount=万元等）。查询结果用表格给我，标注单位。"""))

P.append(("quantdb-fields", "字段单位与口径速查", "平台运营",
    "QuantDB 各数据集实测单位、口径与陷阱速查手册",
    "口径说明",
    """我在做数据分析/回测/报告时需要确认数据口径：{字段疑问，如：成交量单位是股还是手 / 股息率是百分数还是小数 / L2 逐笔数据怎么读}。

请读取 skills/quantdb-fields/SKILL.md，直接给我该字段的单位、口径和已知陷阱，必要时给出验证方法。"""))

P.append(("quantdb-data-structure", "QuantDB 数据结构", "平台运营",
    "数据目录组织、Hive 分区、parquet 路径、代码格式、quantdb_hub 读取入口、quantdb vs quantcustom",
    "结构说明 / 查询路径",
    """我需要了解 QuantDB 本地数据结构或排查查不到数据：{问题，如：l1_factors 在哪 / dt 分区怎么写 / 600519.SH 还是 SH600519 / 因子挖掘产物落哪}。

请读取 skills/quantdb-data-structure/SKILL.md（数据在哪、怎么组织、怎么读）；字段单位另查 skills/quantdb-fields/SKILL.md。说明 quantdb（官方只读）与 quantcustom（用户/挖掘产出）边界，给出可直接用的路径或 DuckDB 视图名。"""))

# ---- 研究分析 ----
P.append(("daily-review", "A股每日复盘", "研究分析",
    "盘后专业复盘：指数、涨停梯队、行业轮动、资金面、L2 微观结构、推理信号复盘、次日方向研判",
    "data/reports/daily_review/ + PDF 报告",
    """今天是 {日期，如 2026-08-29}，请给我做 A 股每日复盘。

请读取 skills/daily-review/SKILL.md 并严格按照其固定流程执行（取数脚本 → 按模板写复盘 → 转 PDF → 落盘）：报告以 facts 数据为准，facts 里没有的数字不要写。最后给我 200 字以内的速览：市场方向、最强板块、明日关键位。"""))

P.append(("market-analysis", "市场分析报告", "研究分析",
    "大盘快照版市场分析：核心指数、广度情绪、行业热力图、板块资金流、个股主力 Top20",
    "data/reports/market_analysis/ + PDF 报告",
    """请给我做一份今日市场分析报告（大盘快照版）。

请读取 skills/market-analysis/SKILL.md 并按其流程执行：跑取数脚本（market_analysis.py）→ 基于 facts 撰写解读 → Markdown → PDF（研报风）→ 落盘 data/reports/market_analysis/。最后给我核心结论速览：大盘状态、资金主线、值得关注的 3 个板块。"""))

P.append(("stock-market-analysis", "个股/全市场深度分析", "研究分析",
    "全市场信号扫描、行业轮动、个股六维深度分析（基本面/估值/技术/资金筹码/情绪/风险）、CSV 导出",
    "分析报告 + CSV 导出",
    """我需要市场/个股深度分析：{全市场信号扫描 / 行业轮动分析 / 深度分析某只股票，如 600519 / 导出选股数据 CSV}。

请读取 skills/stock-market-analysis/SKILL.md 并按对应章节执行，个股分析覆盖基本面/估值/技术/资金筹码/情绪/风险六个维度。报告数据必须来自接口真实返回，最后给出可操作结论。"""))

P.append(("stock-picks", "每日股票推荐", "研究分析",
    "复盘后多维打分选股：L2 微观结构 + 模型融合分 + 仓位信号 + 板块强度 + 新闻情绪",
    "data/reports/stock_picks/ + PDF 报告",
    """请基于最新复盘数据给我做今日股票推荐。

请读取 skills/stock-picks/SKILL.md 并按其流程执行：先确认 data/reports/daily_review/ 有当日 stats（没有先跑复盘取数）→ 跑 pick_candidates.py 多维打分 → Top N 个股深度分析 → 综合报告 → PDF 落盘 data/reports/stock_picks/。最后给我候选榜和每只股票的一句入选理由。"""))

P.append(("stock-research", "个股深度研究（多Agent）", "研究分析",
    "5 分析师并行（技术/新闻/资金情绪/基本面/市场）→ 多空辩论 → 研究经理汇总 → 研报 PDF",
    "data/reports/stock_research/ + data/reports/trading_agents/ PDF",
    """请对 {股票名称及代码，如：贵州茅台 600519} 做一次个股深度研究。

请读取 skills/stock-research/SKILL.md 并严格按多 Agent 流程执行：跑 research_data.py 取数 → 5 个分析师并行（用 prompts/ 下的角色提示词）→ 多空辩论 → 研究经理汇总 → MD 转 PDF → 落盘 data/reports/trading_agents/{市场}/{股票名}/（平台股票报告页可见）。最后给我结论速览：核心逻辑、多空关键分歧、风险点。"""))

P.append(("trading-agents", "个股投研分析（智能体自主版）", "研究分析",
    "本地数据 → 多空子代理辩论 → 综合研判 → PDF 报告，不依赖容器投研管线",
    "data/reports/trading_agents/ PDF 报告",
    """请用智能体自主模式深度分析 {股票名称及代码}。

请读取 skills/trading-agents/SKILL.md 并按其流程执行：拉取本地数据（特征/风险评分/推理分数/新闻）→ 组织多空子代理辩论 → 综合研判 → 生成 MD 报告 → 转 PDF 落盘 data/reports/trading_agents/{市场}/{股票名}/。最后给我投资论点摘要和主要风险。"""))

P.append(("smart-strategy-stock-picking", "条件选股", "研究分析",
    "QuantDB 条件选股：自然语言/结构化/DSL 三种方式，可选登记为全局股票池",
    "股票池列表",
    """请帮我选股，条件：{自然语言条件，如：市值 100-500 亿、PE < 30、近 20 日主力资金净流入、行业为半导体}。

请读取 skills/smart-strategy-stock-picking/SKILL.md：优先 parse-text 或 query-pool 执行 DSL 筛选。结果按市值/涨跌幅排序给表格，注明单位与数据截止日期；超出 50 只只展示前 50。若用户要求持久化股票池，提示在管理后台「全局股票池」保存，或通过 legacy 保存桥接到 v2（pool:code）供回测/训练/推理使用。"""))

P.append(("batch-inference-analysis", "批量推理信号分析", "研究分析",
    "每日推理信号解读：行业轮动、个股分数区间、负分参考、市场状态判断",
    "信号解读报告",
    """请分析最新一批模型推理信号：{指定日期或批次，留空则取最新}。

请读取 skills/batch-inference-analysis/SKILL.md 并按其方法论执行：读取推理信号数据 → 分析行业分布与轮动 → 个股分数区间解读 → 负分参考。最后给我：市场状态判断、信号最集中的 3 个行业、Top 5 高分股与风险提示。"""))

P.append(("news-sentiment-research", "新闻情绪研究", "研究分析",
    "RSS 历史新闻情绪研究：事件研究、七维深度分析、融合规律优化回测、研报输出",
    "研报 MD + PDF",
    """我想研究新闻情绪对股价的规律：{研究主题，如：利好新闻后 5 日收益分布 / 情绪强度与后续涨幅关系}。

请读取 skills/news-sentiment-research/SKILL.md 并按方法论执行（数据源为 Huntly RSS 历史新闻 + FinBERT 情绪），跑对应 backtest_news_*.py 脚本，输出研报级 MD + PDF。结论必须基于数据，样本量不足时明确说明。"""))

P.append(("news-sentiment-finbert", "新闻情绪管线运维", "平台运营",
    "FinBERT 中文金融情绪识别：安装、权重下载、字典扩充、全量重算、排查",
    "运维结果",
    """新闻情绪功能需要运维：{情绪不生效 / 情绪都是中性 / 重新安装 FinBERT / 扩充情绪词典 / 触发全量重算}。

请读取 skills/news-sentiment-finbert/SKILL.md，按对应章节排查或执行。涉及全量重算的操作先告诉我预计耗时，经我确认后再执行。"""))

# ---- 策略·因子·模型·回测 ----
P.append(("ai-ide-strategy-writing", "AI 写量化策略", "策略·因子·模型·回测",
    "AI 生成 Qlib 量化策略代码、Docker 容器执行、策略落库",
    "策略代码 + 落库结果",
    """请帮我写一个量化策略：{策略想法，如：低波动+高股息双因子选股，每周调仓}。

请读取 skills/ai-ide-strategy-writing/SKILL.md，按其规范生成 Qlib 策略代码，在 Docker runner 中执行验证可运行，然后落库保存。给我策略逻辑说明、代码位置和执行结果。"""))

P.append(("backtest-center", "回测中心", "策略·因子·模型·回测",
    "Qlib 回测：快速/专家模式、向量化极速回测、策略对比、参数优化、全局股票池 pool_id",
    "回测结果报告",
    """我需要回测：{快速回测 / 专家模式 / 对比策略 / 参数优化 / 查历史}，市场 {CN/HK/US/CRYPTO/FUTURES}，股票池 {csi300 / pool:自定义池code / 留空全市场}。

请读取 skills/backtest-center/SKILL.md 按对应模式操作。选股范围优先用 pool_id（如 pool:csi1000），与前端全局股票池一致；纯 TopK 策略可试 use_vectorized=true 极速引擎（不安全策略会自动退回 step 模式）。按市场切换 provider_uri/基准。结果给我年化收益、最大回撤、夏普比率，并说明结论是否稳健。"""))

P.append(("rd-agent-factor-mining", "因子挖掘（RD-Agent）", "策略·因子·模型·回测",
    "factor_pipeline 一键管线：preflight → 演化 → 回测 → IC 排序 → explain → export 至 quantcustom",
    "因子报告 + quantcustom 入库",
    """请帮我挖掘新因子：方向「{挖掘假设，如：筹码集中度上行伴随低位换手放大}」，股票池 {csi300 / 自定义全局池 code}，市场 {a_share 等}。

请读取 skills/rd-agent-factor-mining/SKILL.md：先跑 scripts/alpha_agent/factor_pipeline.py --check-env；再用 --direction / --universe / --loops 走一键管线（演化→回测→排名→可选 --explain-top / --export）。universe 支持内置指数与全局自定义池 code。产物落 /data/quantcustom（勿写 quantdb）。耗时长，分段汇报；最后给 Top 因子 IC/Sharpe 与是否 export 成功。"""))

P.append(("model-train-infer-backtest-report", "训练-推理-回测-报告全流程", "策略·因子·模型·回测",
    "T+N 周期模型训练（13 种模型）→ 批量推理 → 组合回测（阈值+大盘MA+止损）→ 研报输出",
    "研报 MD + PDF",
    """请走完「训练 → 推理 → 回测 → 报告」全流程：模型类型 {lightgbm/xgboost/lstm/transformer 等 13 选 1}，周期 T+{N}，市场 {CN/HK/US/CRYPTO/FUTURES}。

请读取 skills/model-train-infer-backtest-report/SKILL.md 并按流程执行：提交训练 → 等待完成 → 批量推理 → 自定义组合回测（分数阈值 + 大盘 MA 过滤{+止损}）→ 导出研报 MD+PDF。训练耗时长，分段汇报；最后给我 T+N 周期对比与效益分析结论。"""))

P.append(("factor-report", "因子体检报告（分位·换手·相关性）", "策略·因子·模型·回测",
    "Alpha 库 429 因子的 Alphalens 式体检：分位收益单调性、换手率、相关性去重",
    "因子体检结论 + 入选/剔除清单",
    """请给因子做体检：范围 {全部 429 个 / 指定因子如 a158_ROC20 / 某个库 alpha101}，前瞻期 T+{1/2/5/10/20}。

数据源是技能中心「因子报告」页签背后的接口 /api/v1/factor-report（快照文件由 backend/scripts/build_factor_report.py 生成）。请按下面四步走：

1. 先看快照元数据（窗口起止、样本天数、生成时间）：快照缺失或超过一周，先在服务器执行 `docker exec quantmind python3 backend/scripts/build_factor_report.py`；
2. 按 |IC| / ICIR 列出候选因子，逐个核对三件事：分位收益是否单调（单调性接近 ±1）、多空价差 Q10−Q1 有多大、单边换手率多少（换手 >50% 的因子要把交易成本算进去）；
3. 用相关性矩阵找出 |ρ|>0.9 的重复因子（同一簇只保留 ICIR 最高的一个，其余剔除并说明与谁重复）；
4. 输出 Markdown 体检报告：入选因子清单（每个附 IC/ICIR/多空/换手/相关性证据）、剔除理由、以及建议的组合权重思路，落到 /data/reports/。"""))

# ---- 交易 ----
P.append(("simulation-trading", "模拟交易", "交易",
    "模拟盘下单买卖、持仓管理、成交查询、资金快照",
    "交易结果",
    """请帮我操作模拟交易：{买入/卖出 某股票及数量 / 查持仓 / 查账户与资金 / 查成交记录}。

请读取 skills/simulation-trading/SKILL.md，通过 /api/v1/simulation/* 接口执行。下单前把订单要素（代码、方向、数量、价格）列给我确认后再提交；完成后返回成交结果与最新持仓。"""))

P.append(("tdx-live-trading", "TDX 实盘监控与交易", "交易",
    "通达信实盘链路：实时推理、自动买卖、挂单/撤单、交易记录、持仓、桥健康巡检",
    "链路状态/交易结果",
    """我需要处理 TDX 实盘链路：{查看链路健康状态 / 查今日交易记录与持仓 / 配置实时推理 / 下单、撤单操作}。

请读取 skills/tdx-live-trading/SKILL.md：先跑 tdx_live_status.py 状态快照并按异常判定表巡检；涉及实盘下单/撤单的操作必须先列出订单要素经我确认。实盘资金安全第一，任何异常先停止操作并报告。"""))

P.append(("ibkr-cli", "IBKR 盈透证券操作", "交易",
    "Interactive Brokers CLI：行情、下单、订单管理、账户/持仓/盈亏、期权链、扫描器",
    "操作结果",
    """我需要通过 Interactive Brokers 操作：{行情查询 / 下单 / 账户持仓盈亏 / 期权链 / 基本面数据}。

请读取 skills/ibkr-cli/SKILL.md 获取 ibkr-cli 用法并执行。涉及真实订单的操作先与我确认要素；输出用表格，注明币种与数据时点。"""))


def write(name, title, category, desc, outputs, body):
    content = "---\n"
    content += "name: " + name + "\n"
    content += "title: " + title + "\n"
    content += "category: " + category + "\n"
    content += "description: " + desc + "\n"
    content += "outputs: " + outputs + "\n"
    content += "---\n\n"
    content += "> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。\n\n"
    content += body + "\n"
    with open(name + ".md", "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


for item in P:
    write(*item)

# 同步生成前端数据模块（QuantBot 顶栏提示词库数据源，构建期打包进 bundle）
TS_OUT = os.path.join(os.getcwd(), "..", "electron", "src", "features", "quantbot", "data",
                      "prompts.generated.ts")
lines = [
    "/**",
    " * 本文件由 scripts/gen_prompts.py 自动生成，请勿手工编辑。",
    " * 源数据：仓库根目录 prompts/*.md（人类可读版本）。",
    " */",
    "",
    "export interface PromptMeta {",
    "  name: string;",
    "  title: string;",
    "  category: string;",
    "  description: string;",
    "  outputs: string;",
    "  body: string;",
    "}",
    "",
    "export const PROMPTS: PromptMeta[] = [",
]
for name, title, category, desc, outputs, body in P:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${").replace("\n", "\\n")
    lines.append("  {")
    lines.append(f"    name: '{name}',")
    lines.append(f"    title: '{title}',")
    lines.append(f"    category: '{category}',")
    lines.append(f"    description: '{esc(desc)}',")
    lines.append(f"    outputs: '{esc(outputs)}',")
    lines.append(f"    body: `{esc(body)}`,")
    lines.append("  },")
lines.append("];")
lines.append("")
with open(TS_OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(lines))

print("written", len(P), "prompts; ts module ->", TS_OUT)
