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

// ---------------------------------------------------------------------------
// 停止策略二次确认（T-RC-19：用户硬要求「停止必须二次确认并留痕」）
// ---------------------------------------------------------------------------

/** 停止原因：随 `/stop` 请求落审计，供事后回溯「这次为什么停」。 */
export const STOP_REASONS = [
  { value: 'manual', label: '人工干预（临时停一下）' },
  { value: 'switch', label: '更换策略（准备启动新策略）' },
  { value: 'risk', label: '风控告警（主动避险）' },
  { value: 'debug', label: '调试排查（参数或代码异常）' },
] as const;

export type StopReasonValue = (typeof STOP_REASONS)[number]['value'];

export interface StopStrategyInput {
  /** 'SIMULATION' | 'REAL'；未知按模拟口径（与后端 Form 默认值一致） */
  mode?: unknown;
  strategyName?: string;
  /** 当前持仓只数；给出时写入文案，`undefined`/非数则不提 */
  positionCount?: number | null;
}

/**
 * 停止确认文案（随模式生成）。
 *
 * 两种模式**必须分开写**，因为用户最需要知道的那件事不同：
 * - 模拟盘：钱是虚拟的，「停止是否影响台账」是唯一悬念
 * - 实盘：停止**不等于撤单**——在途委托仍可能成交，这是真实亏损来源，
 *   不写清楚等于让用户以为「点了停止就安全了」
 */
export function buildStopStrategyScenario({ mode, strategyName, positionCount }: StopStrategyInput): {
  title: string;
  consequences: readonly string[];
  confirmText: string;
  cancelText: string;
} {
  const name = String(strategyName || '').trim();
  const isReal = String(mode ?? '').trim().toLowerCase() === 'real';
  const count = Number(positionCount);
  // 0 只 = 没有持仓，走「无持仓」话术；只有正数才报笔数
  const hasCount = Number.isFinite(count) && count > 0;
  const lines: string[] = [];

  lines.push('停止后**不再产生新的委托**；已在途的执行轮次跑完即止，不会中途砍断');

  if (isReal) {
    lines.push('⚠️ 停止**不会自动撤回**已提交至券商的委托——它们仍可能成交，请到券商端核对');
    lines.push(
      hasCount
        ? `当前 ${count} 只持仓**保留在券商账户**中，停止后不再有策略托管（止损/调仓均停止）`
        : '**当前无持仓**；已有委托与成交记录保留在券商账户'
    );
  } else {
    lines.push(
      hasCount
        ? `当前 ${count} 只模拟持仓与台账**全部保留**，停止后不再自动调仓`
        : '**当前无持仓**；已有模拟台账保留，可随时回看'
    );
  }

  lines.push(`想继续跑时，回到本页**重新启动**即可，历史运行记录不丢`);

  return {
    title: isReal
      ? `停止实盘策略${name ? `「${name}」` : ''}`
      : `停止模拟策略${name ? `「${name}」` : ''}`,
    consequences: lines,
    confirmText: '确认停止',
    cancelText: '继续运行',
  };
}
