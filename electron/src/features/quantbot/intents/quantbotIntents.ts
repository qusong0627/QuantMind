/**
 * QuantBot 意图模型（T-FE-13）：四类意图示例 + 危险动作两步确认判定（纯函数，可单测）。
 *
 * 纪律（LLM 接入六铁律）：助手**永不直达写操作**——危险动作在 UI 层强制两步确认，
 * 涉及实际委托的动作只给"去交易台执行"的深链（人工作业），聊天界面不提供直接下单按钮。
 */

export interface IntentExample {
  label: string;
  prompt: string;
}

export interface QuantbotIntent {
  key: 'write_strategy' | 'screen_stocks' | 'analyze' | 'help';
  label: string;
  description: string;
  examples: IntentExample[];
}

export const QUANTBOT_INTENTS: QuantbotIntent[] = [
  {
    key: 'write_strategy',
    label: '写策略',
    description: '用自然语言描述思路，生成可回测的策略代码',
    examples: [
      { label: '双均线择时', prompt: '帮我写一个 A 股双均线择时策略：日线，快线 5 日、慢线 20 日，金叉买入死叉卖出，每次满仓。' },
      { label: '周频轮动', prompt: '写一个每周调仓的 TopK 轮动策略，从模型信号里选前 10 只等权持有。' },
      { label: '止盈止损', prompt: '在双均线策略上加硬止损 -8% 和止盈 +20%，写完整代码。' },
      { label: '涨停回踩低吸', prompt: '写一个涨停回踩策略：昨日涨停、今日不破昨日高点的票低吸，跌破涨停日最低价止损。' },
      { label: '小市值轮动', prompt: '写一个小市值轮动策略：每周选流通市值最小的 10 只（剔除 ST 和上市不足 60 天的新股），等权持有。' },
      { label: '动量强势股', prompt: '写一个动量策略：每周买入过去 20 日涨幅前 10 且当日未涨停的股票，持有一周后调仓。' },
    ],
  },
  {
    key: 'screen_stocks',
    label: '选股筛选',
    description: '按分位/行业/量价/事件条件筛选当日机会',
    examples: [
      { label: '高分位强势股', prompt: '今天的信号里，rank_pct 前 100 中最近 5 日没有涨停的股票有哪些？' },
      { label: '行业分散选取', prompt: '给我 rank_pct 前 200 里行业内只取最强一只、最多 8 个行业的选股清单。' },
      { label: '缩量回踩', prompt: '从我关注的票里筛出缩量回踩 20 日线的标的，给出量能比和乖离率。' },
      { label: '涨停梯队', prompt: '今天涨停和连板梯队怎么构成的？按题材归类，谁是高度龙头、谁是补涨？' },
      { label: '龙虎榜游资', prompt: '今天的龙虎榜有哪些活跃游资席位？净买入前列的票给我一份清单。' },
      { label: '财务排雷', prompt: '从我的持仓里筛出有财务风险的票：商誉占比高、股权质押高、业绩预告下滑的都要标出来。' },
    ],
  },
  {
    key: 'analyze',
    label: '分析问答',
    description: '个股/行业/市场的本地数据深度分析',
    examples: [
      { label: '个股快速体检', prompt: '用本地数据帮我快速分析 600036.SH：估值分位、资金流、近期信号变化。' },
      { label: '今日复盘', prompt: '帮我做今天的 A 股复盘：指数表现、涨停梯队、行业轮动、资金面，输出一份复盘报告。' },
      { label: '市场情绪', prompt: '现在市场情绪温度怎么样？涨跌家数、量能、位置分别处在什么状态？' },
      { label: '深度研究报告', prompt: '对 600519.SH 做一次深度研究：基本面、估值、资金面、消息面，输出一份研报。' },
      { label: '事件雷达', prompt: '我持仓的股票最近有没有解禁、股权质押、回购这类事件？按影响排个序。' },
      { label: '行业轮动', prompt: '最近的行业轮动在往哪些方向走？列出申万行业近 20 日强弱变化前五。' },
      { label: '持仓复盘', prompt: '帮我复盘模拟盘本周持仓的表现，哪几笔贡献最大、哪几笔拖后腿。' },
    ],
  },
  {
    key: 'help',
    label: '操作帮助',
    description: '平台功能怎么用、门槛/口径怎么理解',
    examples: [
      { label: '怎么晋级模拟盘', prompt: '策略怎么从回测晋级到模拟盘？体检结论要达到什么才有资格？' },
      { label: '体检结论怎么看', prompt: '体检报告的 A/B/L/E 分别是什么意思？我这个策略是 E，应该先做什么？' },
      { label: '同步数据失败', prompt: '今天的数据同步失败了，我应该按什么顺序排查？' },
      { label: '旧版数据迁移', prompt: '升级后，我旧 QwenPaw 里的技能和 MCP 配置怎么迁移到现在的 QuantBot？' },
      { label: '你有哪些技能', prompt: '你现在都有哪些技能可以用？按类别列一下，并说说各自适合什么场景。' },
      { label: '数据在哪怎么查', prompt: 'QuantDB 的数据都存在哪里？怎么查某只股票的历史行情和财务数据？' },
    ],
  },
];

/** 危险动作判定与后果文案：唯一实现在 shared/compliance/dangerAction（T-FE-18 收敛），此处再导出兼容旧引用 */
export { needsTwoStepConfirm, confirmConsequence } from '../../../components/shared/compliance/dangerAction';
