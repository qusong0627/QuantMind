/**
 * 持仓监控「来源条」的纯逻辑：账户源 chips 的选中语义 + 行情源聚合的展示口径。
 *
 * 抽出来的原因和 deskModel/alertModel 一样：这些判断（看的是哪个源、有没有多写席、
 * 数字什么单位）本来是散在 JSX 里的三元表达式，改一处漏一处；纯函数可以直接盯住。
 */

export type FreshnessLevel = 'fresh' | 'stale' | 'unavailable';

export interface QuoteSourceItem {
    source: string;
    label: string;
    count: number;
    newest_age_sec: number | null;
    level: string;
}

/** 后端 `/tdx/quote-feed/status` 的 quote_sources 段 */
export interface QuoteSourcesPayload {
    requested?: number;
    missing?: number;
    dominant?: string | null;
    dominant_label?: string | null;
    level?: FreshnessLevel;
    newest_age_sec?: number | null;
    sources?: QuoteSourceItem[];
    error?: string | null;
}

export interface FreshnessStyle {
    dot: string;
    text: string;
    label: string;
}

const FRESHNESS_STYLES: Record<FreshnessLevel, FreshnessStyle> = {
    fresh: { dot: 'bg-emerald-500', text: 'text-emerald-600', label: '实时' },
    stale: { dot: 'bg-amber-400', text: 'text-amber-600', label: '滞后' },
    unavailable: { dot: 'bg-rose-400', text: 'text-rose-500', label: '停更' },
};

export function freshnessStyle(level?: string | null): FreshnessStyle {
    return FRESHNESS_STYLES[(level as FreshnessLevel) || 'unavailable'] || FRESHNESS_STYLES.unavailable;
}

/**
 * 点 chip 的下一状态：正在看它 → 取消（回到「跟随交易券商」）；否则 → 看它。
 * 只改「看」，不改下单路由——两件事分开，见 accountSourcePreference 的模块注释。
 */
export function nextPreferredSource(current: string | null, clicked: string): string | null {
    return current === clicked ? null : clicked;
}

/** 当前实际在看哪个源：有显式偏好用它，否则跟随交易券商源 */
export function resolveViewedSource(
    preferred: string | null,
    selectedSource?: string | null,
): string | null {
    return preferred || selectedSource || null;
}

/** 同池其它写源（除主供数方外 count>0 的）：>0 即说明同一批键正被两席轮流写 */
export function extraQuoteWriters(quote?: QuoteSourcesPayload | null): QuoteSourceItem[] {
    if (!quote || quote.error) return [];
    return (quote.sources || []).filter(item => item.count > 0 && item.source !== quote.dominant);
}

/** 采样上限：URL 别超长（50 只持仓 ≈ 500 字符），后端默认上限 300 */
export const QUOTE_SAMPLE_LIMIT = 100;

/**
 * 行情源采样集 = **当前账户正在显示的持仓**（不是馈送自己的监控名单）。
 * 后端 `?symbols=` 缺省时才用馈送名单；馈送名单常为空（那行就会显示「采样 0 只」，
 * 看着像坏了）。这里显式把我们真正关心的标的给它。
 */
export function quoteSampleSymbols(codes?: string[] | null, limit = QUOTE_SAMPLE_LIMIT): string | null {
    if (!codes || codes.length === 0) return null;
    const cleaned = codes.map(c => String(c || '').trim()).filter(Boolean);
    if (cleaned.length === 0) return null;
    return Array.from(new Set(cleaned)).slice(0, limit).join(',');
}

export function formatAge(sec?: number | null): string {
    if (sec == null || !Number.isFinite(sec)) return '—';
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return `${s}s 前`;
    if (s < 3600) return `${Math.floor(s / 60)}m 前`;
    return `${Math.floor(s / 3600)}h 前`;
}

/** 金额简写（亿/万）；0 或非数 → 「—」（0 当占位不冒充真实资产） */
export function formatMoney(value?: number | null): string {
    const num = Number(value ?? 0);
    if (!Number.isFinite(num) || num === 0) return '—';
    if (Math.abs(num) >= 1e8) return `${(num / 1e8).toFixed(2)} 亿`;
    if (Math.abs(num) >= 1e4) return `${(num / 1e4).toFixed(1)} 万`;
    return num.toFixed(0);
}
