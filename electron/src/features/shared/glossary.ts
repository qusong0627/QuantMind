/**
 * 术语翻译层（T-FE-01）——**唯一来源**：术语 key → 人话 + 专业说明（前端设计 §一.3）。
 *
 * 纪律：
 * - 对外指标必须由本表翻译（简单模式展示 plain，专业模式追加 detail）；
 * - 组件里出现 `term="xxx"` 字面量时，key 必须在本表存在（glossary 覆盖度单测强校验，漏翻即红）；
 * - 术语是"解释"不是"美化"：plain 用一句人话，detail 保留机构口径（公式/阈值/来源）。
 */

export interface GlossaryEntry {
  /** 简单模式：一句人话 */
  plain: string;
  /** 专业模式：机构口径（定义/阈值/公式要点） */
  detail: string;
  category?: string;
}

export const GLOSSARY: Record<string, GlossaryEntry> = {
  // ── 信号/选股 ─────────────────────────────────────────────
  rank_pct: {
    plain: '研究评分：当日截面分位（越高越靠前）',
    detail:
      '同一次推理、同一交易日内该标的的截面百分位（0~1，乘 100 即 0–100 研究评分）。**分母是这一次推理跑出的那批标的**，不是整个市场，所以跨模型/跨市场不可直接比较。选股阈值按分位口径（如 p98）而非原始分数，因为原始分量纲随模型变。',
    category: '信号',
  },
  fusion_score: {
    plain: '模型打分（只用于同日内排序）',
    detail: '模型融合原始分。不同模型量纲不可比，仅日内排序有意义；对外展示以 rank_pct 为准。',
    category: '信号',
  },
  signal_side: {
    plain: '当日截面位置：靠前 / 靠后 / 居中',
    detail:
      'BUY/SELL/HOLD 三态，是模型在**当日截面中的相对位置**，不是买卖指令。推理链路按分位 + 共识 + 置信归一生成（pred.parquet 回退路线按绝对阈值，故两条来源的阈值不同、仅同源可比）。展示面只说位置；执行面（模拟盘下单、委托台账）仍按买入/卖出表述。',
    category: '信号',
  },

  // ── 回测体检九项 ──────────────────────────────────────────
  dsr: {
    plain: '扣掉"我试了很多参数"之后，业绩还显著吗',
    detail: 'Deflated Sharpe Ratio（Bailey & López de Prado）：按试验次数 N 去胀后的夏普显著性。≥0.95 视为通过；N 自动取自参数扫描记录。',
    category: '体检',
  },
  psr: {
    plain: '夏普比率大于 0 的概率',
    detail: 'Probabilistic Sharpe Ratio：偏度/峰度校正后的 P(SR>0)。样本越短、收益越偏态，所需的夏普越高。',
    category: '体检',
  },
  min_trl: {
    plain: '要证明这不是运气，需要多长的样本',
    detail: 'Minimum Track Record Length：在目标置信度（95%）下，写出该夏普所需的最短样本。观测年限 < 该值 → 证据不足（E 区）。',
    category: '体检',
  },
  pbo: {
    plain: '参数优化的过拟合概率',
    detail: 'PBO/CSCV（Bailey 等）：把参数扫描的 T×N 收益矩阵分块做 IS/OOS，IS 最优组合在 OOS 落到中位数以下的占比。<0.5 才算稳。',
    category: '体检',
  },
  bootstrap: {
    plain: '收益置信区间（保留自相关）',
    detail: 'Block Bootstrap（块长 √n，B=1000）：年化收益的重采样置信区间。区间跨 0 视为不可区分于噪声（L 嫌疑）。',
    category: '体检',
  },
  concentration: {
    plain: '收益是不是靠少数几天',
    detail: '收益集中度：剔除表现最好的 Top5 交易日后复利重算，若超额消失（杀 alpha）→ 归入运气嫌疑（L）。',
    category: '体检',
  },
  regime: {
    plain: '牛市/熊市/震荡下都活着吗',
    detail: '以指数 60 日趋势为代理分段（牛/熊/震荡），逐段统计存活与累计收益；覆盖段数 <2 判不稳。',
    category: '体检',
  },
  cost_sensitivity: {
    plain: '成本上浮后还剩多少',
    detail: '换手 × 费率上浮（默认 +X bps）复算年化：成本后仍为正才算真超额。',
    category: '体检',
  },
  factor_regression: {
    plain: '收益来自选股能力（alpha）还是跟大盘（beta）',
    detail: '对基准（+风格）回归：alpha 年化/t 值/决定系数 R²。t≤2 或 alpha 占收益 <30% 判 beta 主导（B）。',
    category: '体检',
  },

  // ── 体检结论（四分类）─────────────────────────────────────
  verdict_a: {
    plain: '真本事：选股/择时能力统计上站得住',
    detail: 'A：alpha 显著（t>2）且 DSR 通过、跨 regime 稳健、成本后仍显著、样本 ≥ MinTRL。可晋级。',
    category: '体检',
  },
  verdict_b: {
    plain: '跟着市场涨的：收益主要来自大盘/风格暴露',
    detail: 'B：alpha 不显著或占比 <30%。可晋级但须标注收益来源（门禁放行信息会写明）。',
    category: '体检',
  },
  verdict_l: {
    plain: '运气嫌疑：统计上站不住',
    detail: 'L：DSR<0.95 / 收益 CI 跨 0 / 剔除 Top5 日后 alpha 消失。不得晋级（T-P3-05 门槛）。',
    category: '体检',
  },
  verdict_e: {
    plain: '证据不足：样本/数据不够下结论',
    detail: 'E：样本 < MinTRL 或缺基准回归证据。不得晋级；正确解法是延长回测区间/补数据，而不是绕过门禁。',
    category: '体检',
  },
  confidence_score: {
    plain: '可信度分（0-100，越大证据越硬）',
    detail: '体检可信度 = 显著性30 + DSR25 + 集中度15 + regime15 + 样本充分度15 分量合成。',
    category: '体检',
  },
  low_confidence: {
    plain: '低置信（样本不足的评分，先看趋势）',
    detail: '带 † 标注：评分或体检在证据不充分时产出，仅作方向参考，不得作为晋级/清仓依据。',
    category: '评分',
  },
  red_line: {
    plain: '红线（触发即封顶/不得晋级）',
    detail: '维度红线（如单票>50%、MDD≤-50%、ICIR<0.2）触发后总分封顶 59；体检 L/E 直接拦截晋级。',
    category: '评分',
  },
  score_grade: {
    plain: '综合评级 A/B/C/D',
    detail: 'A≥85 / B 70-84 / C 60-69 / D<60；红线封顶 59；低置信附 †。',
    category: '评分',
  },

  max_drawdown: {
    plain: '最大回撤（从最高点最多跌过多少）',
    detail: '区间内净值相对历史峰值的最大跌幅（水下曲线取最小值）。负值越大风险越高；策略红线 MDD ≤ -50%。',
    category: '通用',
  },
  sharpe: {
    plain: '夏普比率（每承担一分波动赚多少）',
    detail: '年化：均值/波动 × √252（rf=0 口径）。样本 <2 或波动为 0 → 不计算（—）。',
    category: '通用',
  },

  kline_adjust: {
    plain: '复权口径（分红送股后价格是否折算）',
    detail: '前复权（qfq）：历史价按复权因子折算，均线/形态可比；不复权（none）：真实成交价，事件跳变会失真。口径变更会改变价格可比性，图内角标提示当前口径。',
    category: '行情',
  },
  trade_mark: {
    plain: 'K 线上的买卖点（模拟成交）',
    detail: '来自 sim_trades（含理由 remarks 与订单号）；点击标记可下钻到该笔成交的来源与理由。',
    category: '行情',
  },

  // ── 执行/交易 ─────────────────────────────────────────────
  dry_run: {
    plain: '计划预演：按同样规则算给你看，不会真下单',
    detail: '调仓计划预演：与执行共用同一 RebalanceCalculator（退出规则/池过滤/风控买锁同源），但不撮合、不落单、不写快照。',
    category: '执行',
  },
  exit_rule: {
    plain: '止盈止损等自动退出规则触发的单',
    detail: '持仓退出规则（硬止损/止盈/最大持有天数）与调仓共用幂等键；计划卡中以「退出规则」类别标出。',
    category: '执行',
  },
  rebalance: {
    plain: '定期调仓（按目标权重换股）',
    detail: 'RebalanceCalculator：TopK 目标权重 → 目标股数 → 先卖后买；受调仓周期（rebalance_days）与涨跌停/停牌约束。',
    category: '执行',
  },
  slippage: {
    plain: '滑点：成交价与理论价的差',
    detail: '影子对照口径：模拟↔真单成交价偏差（bps）与滑点实现率；越大说明执行损耗越高。',
    category: '执行',
  },
  fill_rate: {
    plain: '成交率：下的单有多少真的成交了',
    detail: '影子对照：配对订单中真实成交的比例；低成交率常见于涨跌停/流动性不足。',
    category: '执行',
  },
  tracking_error: {
    plain: '模拟盘与真单的偏差（越小越一致）',
    detail: '共同交易日日收益差（模拟−真单）的标准差年化（te_ann_bps）。LIVE 晋级门槛：≤15%。',
    category: '执行',
  },

  // ── 通用 ─────────────────────────────────────────────────
  source: {
    plain: '这个数字从哪来（可下钻核对）',
    detail: '下钻来源标识：如 db:engine_signal_scores / redis:mirror:shadow:{date} / scripts/diagnose/health.py。全链证据矩阵要求每个数字带 source。',
    category: '通用',
  },
  pipeline: {
    plain: '当日闭环：数据→推理→信号→计划→执行→结算',
    detail: '管线状态与体检脚本同源映射（C08/C02/C01/C05），不暴露内部 worker 拓扑。',
    category: '通用',
  },
};

export type GlossaryKey = keyof typeof GLOSSARY;

export function getTerm(key: string | undefined | null): GlossaryEntry | null {
  if (!key) return null;
  return GLOSSARY[key] || null;
}

export function hasTerm(key: string): boolean {
  return Object.prototype.hasOwnProperty.call(GLOSSARY, key);
}
