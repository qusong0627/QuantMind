/**
 * 危险动作判定与后果文案（T-FE-18 默认安全）——唯一实现（quantbotIntents 再导出兼容）。
 *
 * 纪律（LLM 六铁律 + 产品化 §五）：写/破坏性动作在 UI 层强制两步确认；
 * 文案必须说清后果（不可逆性、涉及真实资金与否、如何撤回）。
 */

export interface DangerActionLike {
  label?: string;
  variant?: string;
}

/** 危险动作判定：显式 danger 或动作文案含写/执行类词 */
const _DANGER_WORDS = /执行|下单|买入|卖出|清仓|删除|停用|停止|上实盘|真实盘|关闭|归零/;

export function needsTwoStepConfirm(action: DangerActionLike | null | undefined): boolean {
  if (!action) return false;
  if (action.variant === 'danger') return true;
  return _DANGER_WORDS.test(String(action.label || ''));
}

export function confirmConsequence(label: string): string {
  return `「${label}」属于写操作：确认后将立即对实际状态生效（可能产生委托或不可逆变更）。请核对上下文与参数后确认。`;
}

/** 具名危险场景的后果文案（各接入点用同一措辞口径，可单测） */
export const DANGER_SCENARIOS = {
  switch_real: {
    title: '切换到实盘模式',
    consequences: [
      '仪表盘与交易入口将指向**真实资金账户**（不再显示模拟盘数字）',
      '后续经确认的执行动作可能产生真实委托与资金变动',
      '需要券商通道（TDX/QMT 桥）处于启用状态，否则下单会失败',
    ],
    confirmText: '我已知悉，切换实盘',
    cancelText: '保持模拟盘',
  },
  disable_stop_loss: {
    title: '关闭全局止损保护',
    consequences: [
      '持仓将**失去自动止损**——极端行情下亏损可能显著扩大',
      '消费者最常见的亏损来源正是"不止损"，平台不建议关闭',
      '区间/幅度仍可调整（如 -8%），关闭仅应急使用',
    ],
    confirmText: '确认关闭（后果自负）',
    cancelText: '保留默认 -5%',
  },
  bulk_execute: {
    title: '一键执行调仓',
    consequences: [
      '将按当前计划一次性产生多笔模拟委托',
      '行情变化可能使成交与预演略有出入',
      '退出规则单不可排除（风控动作不绕过）',
    ],
    confirmText: '确认执行',
    cancelText: '取消',
  },
} as const;

/** 场景文案 → 纯文本（测试与可访问性用） */
export function scenarioLines(scenario: { title: string; consequences: readonly string[] }): string[] {
  return [scenario.title, ...scenario.consequences.map((c) => c.replace(/\*\*/g, ''))];
}

// ---------------------------------------------------------------------------
// 大额调仓二次确认（产品化 §五：清仓/关风控/上实盘/大额调仓 四类危险操作）
// ---------------------------------------------------------------------------

/** 大额阈值（元）：买卖预估总额达到该值触发二次确认 */
export const LARGE_ORDER_THRESHOLD = 200000;

/** 大额判定（纯函数）：买 + 卖预估金额绝对值之和 ≥ 阈值 */
export function isLargeOrderAmount(
  buyAmount: number | null | undefined,
  sellAmount: number | null | undefined,
  threshold: number = LARGE_ORDER_THRESHOLD
): boolean {
  const total = Math.abs(Number(buyAmount) || 0) + Math.abs(Number(sellAmount) || 0);
  return total >= threshold;
}

export interface LargeOrderScenarioInput {
  buyAmount?: number | null;
  sellAmount?: number | null;
}

function fmtMoney(v: number): string {
  return `¥${Math.round(v).toLocaleString('zh-CN')}`;
}

/**
 * 大额调仓确认文案（随金额生成）：净卖出且无买入时按"清仓方向"提醒，
 * 其余按"大额调仓"提醒——金额、方向、撤回方式都说清。
 */
export function buildLargeOrderScenario({ buyAmount, sellAmount }: LargeOrderScenarioInput): {
  title: string;
  consequences: readonly string[];
  confirmText: string;
  cancelText: string;
} {
  const buy = Math.abs(Number(buyAmount) || 0);
  const sell = Math.abs(Number(sellAmount) || 0);
  const isNetSell = sell > 0 && buy === 0;
  const lines: string[] = [];
  if (buy > 0) lines.push(`本次将买入约 **${fmtMoney(buy)}**`);
  if (sell > 0) lines.push(`本次将卖出约 **${fmtMoney(sell)}**`);
  if (isNetSell) {
    lines.push('本次为**纯卖出**方向——若卖出列表覆盖全部持仓，等同清仓，仓位将归零');
  }
  lines.push('按当前预案口径估算；实际成交以提交时的行情与撮合结果为准');
  lines.push('提交前可回上一步调整标的与数量；提交后进入执行队列，未成交前可撤单');
  return {
    title: isNetSell ? '确认大额卖出（含清仓风险）' : '确认大额调仓提交',
    consequences: lines,
    confirmText: '核对无误，提交执行',
    cancelText: '回去再核对',
  };
}
