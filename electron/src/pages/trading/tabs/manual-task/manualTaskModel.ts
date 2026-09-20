/**
 * 手动任务向导第 4/5 步（生成调仓预案 / 确认提交）的纯函数模型。
 *
 * 这两步的界面信息密度最高：一份预案里可能带着几十条委托和几百条风控拦截，
 * 之前的实现把每条拦截都渲染成一张卡片，一屏塞不下也看不出「到底被什么拦了」。
 * 所以把「聚合 / 判定 / 按钮状态」从 JSX 里抽出来，放在这里单独测试：
 * 拦截按原因归组、资金是否够、主操作按钮此刻该是什么，都能脱离 DOM 断言。
 */

import type {
    ManualExecutionPreviewOrder,
    ManualExecutionPreviewSkippedItem,
} from '../../../../services/realTradingService';

/** 资金「偏紧」判定比例：剩余现金不足买入总额的 5% 时提示可能不够 */
export const CASH_TIGHT_RATIO = 0.05;

export type TradeSide = 'buy' | 'sell';

/** 归一化买卖方向：除 SELL 外一律按买入处理（预览接口只会给 BUY/SELL） */
export const sideOf = (side?: string | null): TradeSide =>
    String(side || '').trim().toUpperCase() === 'SELL' ? 'sell' : 'buy';

export const formatMoney = (value?: number | null): string => {
    if (!Number.isFinite(Number(value))) return '--';
    return `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export interface RiskSymbolLabel {
    symbol: string;
    /** 股票名（后端补名；缺失为空串，此时只显示代码） */
    name: string;
}

export interface RiskGroup {
    /** 拦截原因原文（后端给的中文短句，同上同因归一组） */
    reason: string;
    /** BUY / SELL / FILTER，用于给组行配买卖色 */
    action: string;
    count: number;
    symbols: string[];
    /** 与 symbols 一一对应的展示标签（名称），展开区 chip 用 */
    labels: RiskSymbolLabel[];
}

export interface RiskSummary {
    total: number;
    groups: RiskGroup[];
}

/**
 * 把 skipped_items 按「原因 + 方向」归组，按条数降序。
 * 653 条「当前无可卖持仓」应当收敛成一行，而不是 653 张卡片。
 */
export function summarizeSkipped(items?: ManualExecutionPreviewSkippedItem[] | null): RiskSummary {
    const list = Array.isArray(items) ? items : [];
    const buckets = new Map<string, {
        reason: string;
        action: string;
        count: number;
        symbols: string[];
        labels: RiskSymbolLabel[];
    }>();

    for (const item of list) {
        const reason = String(item?.reason || '').trim() || '未标注原因';
        const action = String(item?.action || '').trim().toUpperCase() || 'FILTER';
        const symbol = String(item?.symbol || '').trim();
        const name = String(item?.name || '').trim();
        const key = `${reason}|${action}`;
        const bucket = buckets.get(key);
        if (bucket) {
            bucket.count += 1;
            if (symbol) {
                bucket.symbols.push(symbol);
                bucket.labels.push({ symbol, name });
            }
        } else {
            buckets.set(key, {
                reason,
                action,
                count: 1,
                symbols: symbol ? [symbol] : [],
                labels: symbol ? [{ symbol, name }] : [],
            });
        }
    }

    const groups: RiskGroup[] = Array.from(buckets.values()).map((bucket) => ({
        reason: bucket.reason,
        action: bucket.action,
        count: bucket.count,
        symbols: bucket.symbols,
        labels: bucket.labels,
    }));

    groups.sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason, 'zh-Hans-CN'));
    return { total: list.length, groups };
}

export type CashTone = 'ok' | 'tight' | 'short';

export interface CashVerdict {
    tone: CashTone;
    label: string;
    hint: string;
}

/** 资金裁定：剩余现金 < 0 = 缺口；不足买入总额 5% = 偏紧；否则充足 */
export function cashVerdict(
    remaining?: number | null,
    buyAmount?: number | null,
): CashVerdict {
    const rest = Number(remaining);
    if (!Number.isFinite(rest)) {
        return { tone: 'ok', label: '--', hint: '预案未提供资金测算' };
    }
    if (rest < 0) {
        return {
            tone: 'short',
            label: '资金缺口',
            hint: `预估缺口 ${formatMoney(Math.abs(rest))}，请调低买入或先卖出回款`,
        };
    }
    const buy = Number(buyAmount);
    if (Number.isFinite(buy) && buy > 0 && rest < buy * CASH_TIGHT_RATIO) {
        return {
            tone: 'tight',
            label: '资金偏紧',
            hint: '剩余现金不足买入总额 5%，成交价上浮可能触发资金不足',
        };
    }
    return { tone: 'ok', label: '资金充足', hint: '剩余现金可覆盖本次买入' };
}

export type RailStep = 'preview' | 'submit';

export interface RailPrimaryAction {
    kind: 'generate' | 'advance' | 'submit';
    label: string;
    disabled: boolean;
}

/**
 * 右侧「风控 · 操作」栏此刻的主操作。
 *
 * 主操作必须唯一：第 4 步要么「计算预案」要么「进入确认」；第 5 步未提交时是
 * 「推送执行」，已提交（taskId 存在）则没有主操作 —— 此时按钮区改为执行状态块。
 */
export function railPrimaryAction(
    step: RailStep,
    state: {
        hasPreview: boolean;
        previewLoading: boolean;
        submitting: boolean;
        hasTask: boolean;
    },
): RailPrimaryAction | null {
    if (step === 'preview') {
        if (!state.hasPreview) {
            return { kind: 'generate', label: '立即计算调仓预案', disabled: state.previewLoading };
        }
        return { kind: 'advance', label: '下一步：确认提交', disabled: state.previewLoading };
    }
    if (state.hasTask) return null;
    return { kind: 'submit', label: '推送执行', disabled: state.submitting || !state.hasPreview };
}

/**
 * 同一批委托往往共享同一句原因（实测 50 笔买入全是「按预估可用资金等额分配买入预算」），
 * 逐行重复就是噪音。整列同因时由列头统一说明，行内不再重复；不同因才逐行标注。
 */
export function commonReason(orders?: ManualExecutionPreviewOrder[] | null): string | null {
    const list = Array.isArray(orders) ? orders : [];
    if (list.length === 0) return null;
    const first = String(list[0]?.reason || '').trim();
    if (!first) return null;
    return list.every((order) => String(order?.reason || '').trim() === first) ? first : null;
}

export interface OrderRowView {
    symbol: string;
    name: string;
    /** 申万行业；为空表示后端未补到（渲染时隐藏该标签） */
    industry: string;
    /** 上市板；「其他」= 非 A 股或未识别，渲染时隐藏该标签 */
    board: string;
    side: TradeSide;
    sideLabel: '买' | '卖';
    quantityText: string;
    orderTypeText: string;
    priceText: string;
    amountText: string;
    positionText: string;
    referenceText: string;
    reason: string;
    hasPrice: boolean;
}

/** 委托卡 → 单行表格视图（跨列对齐用固定字段，不再是逐张卡片） */
export function orderRowView(order: ManualExecutionPreviewOrder): OrderRowView {
    const side = sideOf(order?.side);
    const price = Number(order?.price);
    const hasPrice = Number.isFinite(price) && price > 0;
    const quantity = Number(order?.quantity);
    const currentVolume = Number(order?.current_volume);
    const referencePrice = Number(order?.reference_price);

    // 卖单没有可卖持仓是真问题，买单没有持仓只是常态，两者不能写同一句
    const positionText = currentVolume > 0
        ? `持仓 ${currentVolume.toLocaleString('zh-CN')}`
        : side === 'sell'
          ? '无可卖持仓'
          : '当前无持仓';

    return {
        symbol: String(order?.symbol || ''),
        name: String(order?.name || ''),
        industry: String(order?.industry || ''),
        board: String(order?.board || ''),
        side,
        sideLabel: side === 'sell' ? '卖' : '买',
        quantityText: Number.isFinite(quantity) ? quantity.toLocaleString('zh-CN') : '--',
        orderTypeText: String(order?.order_type || 'MARKET'),
        priceText: hasPrice ? formatMoney(price) : '未获取',
        amountText: hasPrice ? formatMoney(order?.estimated_notional) : '--',
        positionText,
        referenceText: referencePrice > 0 ? `Ref ${formatMoney(referencePrice)}` : 'Ref 未获取',
        reason: String(order?.reason || ''),
        hasPrice,
    };
}
