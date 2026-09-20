/**
 * 交易模式文案唯一事实源（T-RC-17）。
 *
 * 背景（本次修复的三个真实缺陷）：控制台里「模拟/实盘」的字面量散落在三处手写
 * 三元表达式中，其中一处分支写反——实盘部署的启动按钮写着「启动模拟交易」，
 * 参数向导标题硬编码「模拟执行参数」，横幅还写着自相矛盾的「全自动实盘模拟控制台」。
 * 对交易台来说这不是文案瑕疵：用户会据此判断自己下的单进了哪个账户。
 *
 * 因此本模块是模式文案的**唯一**出口：任何界面要显示模式相关文字，都从这里取；
 * 并由 `containsSimulationWording` 提供机器可判定的卫生检查（探针据此断言
 * 「实盘界面不出现『模拟』」），而不是靠人眼审。
 */

export type TradingMode = 'SIMULATION' | 'REAL';

/** 判定「这句话在说模拟盘」的词族。新增文案若含这些词，实盘界面就会被探针拦下。 */
export const SIMULATION_WORDING = ['模拟', 'simulation', 'paper'] as const;

export interface ModeCopy {
    mode: TradingMode;
    isReal: boolean;
    /** 极短标签：徽章、按钮内嵌 */
    short: string;
    /** 完整称呼：正文、确认弹窗 */
    full: string;
    /** 徽章/横幅样式（模式色调的唯一来源） */
    badgeClass: string;
    bannerClass: string;
    startButton: string;
    stopButton: string;
    wizardTitle: string;
    wizardSubtitle: string;
    bannerTitle: string;
    bannerSubtitle: string;
    /** 停止二次确认正文：要说清账户影响面，不只是一句「确定吗」 */
    stopConfirm: string;
    /** 启动成功后的提示语 */
    startedToast: string;
}

const COPY: Record<TradingMode, ModeCopy> = {
    SIMULATION: {
        mode: 'SIMULATION',
        isReal: false,
        short: '模拟',
        full: '模拟交易',
        badgeClass: 'bg-sky-50 text-sky-700 border-sky-200',
        bannerClass: 'bg-sky-50/70 border-sky-200 text-sky-900',
        startButton: '启动模拟交易',
        stopButton: '停止模拟交易',
        wizardTitle: '模拟执行参数',
        wizardSubtitle: '仅作用于模拟账户，不会发出真实委托',
        bannerTitle: '模拟交易运行台',
        bannerSubtitle: '信号 → 模拟撮合 → 模拟账户，全部资金为虚拟资金',
        stopConfirm: '停止后将不再产生新的模拟委托；已有模拟持仓与台账保留，可随时重新启动。',
        startedToast: '模拟策略已启动',
    },
    REAL: {
        mode: 'REAL',
        isReal: true,
        short: '实盘',
        full: '实盘交易',
        badgeClass: 'bg-rose-50 text-rose-700 border-rose-200',
        bannerClass: 'bg-rose-50/70 border-rose-200 text-rose-900',
        startButton: '启动实盘交易',
        stopButton: '停止实盘交易',
        wizardTitle: '实盘执行参数',
        wizardSubtitle: '将向券商通道发出真实委托，请逐项核对',
        bannerTitle: '实盘交易运行台',
        bannerSubtitle: '信号 → 风控闸门 → 券商通道，委托真实成交',
        stopConfirm: '停止后将不再产生新的真实委托；已提交至券商的委托是否撤回取决于券商状态，持仓保留在账户中。',
        startedToast: '实盘策略已启动',
    },
};

/**
 * 归一化模式写法。**未知一律判为模拟**——与后端 `Form("SIMULATION")` 的默认值
 * 一致：宁可少认一个实盘，不可把一个模拟部署显示成实盘（反向误判会让用户以为
 * 真金白银在下单）。
 */
export function normalizeTradingMode(raw: unknown): TradingMode {
    const text = String(raw ?? '').trim().toLowerCase();
    if (!text) return 'SIMULATION';
    if (['real', 'live', '实盘', '真实'].includes(text)) return 'REAL';
    if (['simulation', 'sim', 'paper', '模拟', '模拟盘'].includes(text)) return 'SIMULATION';
    // 后端交易模式的规范枚举
    if (text === 'real' || text === 'shadow') return 'REAL';
    return 'SIMULATION';
}

export function modeCopy(mode: unknown): ModeCopy {
    return COPY[normalizeTradingMode(mode)];
}

/** 文本卫生：这句话是否含「模拟」词族（实盘界面禁止出现）。 */
export function containsSimulationWording(text: unknown): boolean {
    const body = String(text ?? '').toLowerCase();
    if (!body) return false;
    return SIMULATION_WORDING.some((word) => body.includes(word));
}
