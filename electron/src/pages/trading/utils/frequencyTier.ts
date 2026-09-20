/**
 * 频率档位与调仓节奏（T-RC-18）。
 *
 * 用户要的是「低频/高频」这种操盘手语言，而系统里只有 `rebalance_days` 这类
 * 配置项。本模块把配置翻译成档位，且**不新增后端字段**——档位由现有配置推导，
 * 避免为了一个显示概念做数据库迁移。
 *
 * 诚实边界：本平台没有逐笔撮合链路，**日内高频（tick 级）不存在**。与其在界面上
 * 做一个点不动的选项，不如显式标注「未开放」并说明原因（见 `INTRADAY_TIER`）。
 */

export type FrequencyTierKey = 'low' | 'medium' | 'daily' | 'unknown';

export interface FrequencyTier {
    key: FrequencyTierKey | 'intraday';
    label: string;
    /** 一句话说明这一档的调仓节奏 */
    detail: string;
    supported: boolean;
    note: string;
    /** 判定依据（weekly / rebalance_days=N），用于「为什么判成这一档」的可解释性 */
    basis: string;
}

/** 已开放的档位（按节奏由慢到快）。 */
export const FREQUENCY_TIERS: FrequencyTier[] = [
    {
        key: 'low',
        label: '低频',
        detail: '10 / 20 个交易日调仓一次',
        supported: true,
        note: '换手低，适合容量大的组合',
        basis: 'rebalance_days∈{10,20}',
    },
    {
        key: 'medium',
        label: '中频',
        detail: '3 / 5 个交易日调仓一次',
        supported: true,
        note: '平台默认档位',
        basis: 'rebalance_days∈{3,5}',
    },
    {
        key: 'daily',
        label: '日频',
        detail: '每个交易日调仓一次（可配多个执行时段）',
        supported: true,
        note: '在上午盘/下午盘各跑一轮即多时段，但仍非 tick 级',
        basis: 'rebalance_days=1',
    },
];

/** 未开放的档位：显式标注，避免界面假装支持。 */
export const INTRADAY_TIER: FrequencyTier = {
    key: 'intraday',
    label: '日内高频',
    detail: '实时 tick 级调仓',
    supported: false,
    note: '本平台暂无逐笔撮合链路，未开放（信号仍为日级批次）',
    basis: '不支持',
};

const UNKNOWN_TIER: FrequencyTier = {
    key: 'unknown',
    label: '未配置',
    detail: '调仓节奏未配置或取值不在白名单内',
    supported: false,
    note: '请在参数向导中选择调仓周期',
    basis: '',
};

/** 与 `LiveTradeConfigSchema` 的 rebalance_days 白名单一致。 */
const ALLOWED_REBALANCE_DAYS = [1, 3, 5, 10, 20];

interface RhythmConfig {
    rebalance_days?: unknown;
    schedule_type?: unknown;
    trade_weekdays?: unknown;
    enabled_sessions?: unknown;
    sell_time?: unknown;
    buy_time?: unknown;
    sell_first?: unknown;
    max_orders_per_cycle?: unknown;
}

const SESSION_LABELS: Record<string, string> = {
    AM: '上午盘',
    PM: '下午盘',
    AFTER_HOURS: '盘后固定价格',
    NIGHT: '夜盘',
};

function tierFor(rebalanceDays: number): FrequencyTier {
    if (rebalanceDays >= 10) return FREQUENCY_TIERS[0];
    if (rebalanceDays >= 3) return FREQUENCY_TIERS[1];
    return FREQUENCY_TIERS[2];
}

/**
 * 由生效配置推导频率档位。
 *
 * - `weekly` 调度与 `rebalance_days` 无关，恒为低频（每周一次）；
 * - 取值不在白名单 → `unknown`（不四舍五入到最近的档位，那会谎报节奏）。
 */
export function deriveFrequencyTier(config: RhythmConfig | null | undefined): FrequencyTier {
    if (!config || typeof config !== 'object') return UNKNOWN_TIER;
    const scheduleType = String(config.schedule_type ?? 'interval').trim().toLowerCase();
    if (scheduleType === 'weekly') {
        return { ...FREQUENCY_TIERS[0], basis: 'weekly（按周内日触发）' };
    }
    const raw = Number(config.rebalance_days);
    if (!Number.isFinite(raw) || !ALLOWED_REBALANCE_DAYS.includes(raw)) return UNKNOWN_TIER;
    return { ...tierFor(raw), basis: `rebalance_days=${raw}` };
}

function timeLabel(raw: unknown): string {
    const text = String(raw ?? '').trim();
    return /^\d{2}:\d{2}$/.test(text) ? text : '';
}

function sessionLabel(raw: unknown): string {
    const list = Array.isArray(raw) ? raw : [];
    return list
        .map((item) => SESSION_LABELS[String(item ?? '').trim().toUpperCase()] ?? '')
        .filter(Boolean)
        .join('+');
}

/**
 * 把节奏配置说成一句人话（时段 / 先后 / 时点 / 单轮上限）。
 *
 * 只描述配置里**确实写了**的东西：缺失项不补默认值，否则界面会把后端默认值
 * 显示成「用户的选择」。
 */
export function describeRhythm(config: RhythmConfig | null | undefined): string {
    if (!config || typeof config !== 'object') return '';
    const parts: string[] = [];

    const sessions = sessionLabel(config.enabled_sessions);
    if (sessions) parts.push(sessions);

    if (typeof config.sell_first === 'boolean') {
        parts.push(config.sell_first ? '先卖后买' : '先买后卖');
    }

    const sell = timeLabel(config.sell_time);
    const buy = timeLabel(config.buy_time);
    if (sell && buy) parts.push(`卖 ${sell} / 买 ${buy}`);
    else if (sell) parts.push(`卖 ${sell}`);
    else if (buy) parts.push(`买 ${buy}`);

    const cap = Number(config.max_orders_per_cycle);
    if (Number.isFinite(cap) && cap > 0) parts.push(`单轮最多 ${cap} 笔`);

    return parts.join(' · ');
}
