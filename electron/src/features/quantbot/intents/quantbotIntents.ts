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
    ],
  },
  {
    key: 'screen_stocks',
    label: '选股筛选',
    description: '按分位/行业/量价条件筛选当日机会',
    examples: [
      { label: '高分位强势股', prompt: '今天的信号里，rank_pct 前 100 中最近 5 日没有涨停的股票有哪些？' },
      { label: '行业分散选取', prompt: '给我 rank_pct 前 200 里行业内只取最强一只、最多 8 个行业的选股清单。' },
      { label: '缩量回踩', prompt: '从我关注的票里筛出缩量回踩 20 日线的标的，给出量能比和乖离率。' },
    ],
  },
  {
    key: 'analyze',
    label: '分析问答',
    description: '个股/行业/市场的本地数据深度分析',
    examples: [
      { label: '个股快速体检', prompt: '用本地数据帮我快速分析 600036.SH：估值分位、资金流、近期信号变化。' },
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
    ],
  },
];

/** 危险动作判定（两步确认卡触发）：显式 danger 或动作文案含写/执行类词 */
const _DANGER_WORDS = /执行|下单|买入|卖出|清仓|删除|停用|停止|上实盘|真实盘/;

export function needsTwoStepConfirm(action: { label?: string; variant?: string } | null | undefined): boolean {
  if (!action) return false;
  if (action.variant === 'danger') return true;
  return _DANGER_WORDS.test(String(action.label || ''));
}

export function confirmConsequence(label: string): string {
  return `「${label}」属于写操作：确认后将立即对实际状态生效（可能产生委托或不可逆变更）。请核对上下文与参数后确认。`;
}
