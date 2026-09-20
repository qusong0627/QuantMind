/**
 * 策略市场归一与页签过滤（T-RC-15，修 D4）。
 *
 * 背景：策略管理页的策略下拉此前调用 `loadStrategies(userId)` **漏传 market**，
 * 于是港股策略混进了 A 股视图——用户以为在给 A 股策略配参数，实际配的是港股策略，
 * 启动后行情/信号/账户口径全错。
 *
 * 口径必须与后端一致（`backend/shared/strategy_storage.py` 的市场过滤）：
 * - `parameters->>'market'` 为空 → **一律按 A 股计**（历史策略没有该字段）
 * - 其余按 `UPPER(market)` 精确匹配
 *
 * 注意与「运行快照的市场判定」区分：那里是「载荷没写市场就不判定页签」（见
 * `backend/shared/active_strategy_market.py`），这里是「策略列表归属哪个市场」，
 * 后者有明确的后端契约，故可以判定。
 */

import type { AppMarket } from '../../../store/slices/uiSlice';

export type StrategyMarket = AppMarket;

/** 别名 → 规范市场。`A`/`a股` 是历史写法，必须与 CN 视为同一市场。 */
const MARKET_ALIASES: Record<string, StrategyMarket> = {
    A: 'CN',
    CN: 'CN',
    'A股': 'CN',
    'A 股': 'CN',
    ASHARE: 'CN',
    HK: 'HK',
    HONGKONG: 'HK',
    '港股': 'HK',
    US: 'US',
    USA: 'US',
    '美股': 'US',
    CRYPTO: 'CRYPTO',
    '区块链': 'CRYPTO',
    FUTURES: 'FUTURES',
    '期货': 'FUTURES',
};

const KNOWN_MARKETS: StrategyMarket[] = ['CN', 'US', 'HK', 'CRYPTO', 'FUTURES'];

interface StrategyLike {
    parameters?: { market?: unknown } | null;
    market?: unknown;
}

/**
 * 声明情况的三种结果——**必须区分「没写」和「写了但看不懂」**：
 *
 * - `absent`：字段缺失/空串 → 历史策略，后端契约按 A 股计
 * - `unknown`：写了但不在别名表内 → 脏数据，任何页签都不认领
 * - 具体市场：正常归一
 *
 * 早先把二者都归成 `null`，于是脏数据被当成历史策略落进 A 股视图——
 * 这正是本次要消除的「张冠李戴」。
 */
type DeclaredMarket =
    | { kind: 'absent' }
    | { kind: 'unknown'; raw: string }
    | { kind: 'market'; market: StrategyMarket };

function readDeclaredMarket(strategy: unknown): DeclaredMarket {
    if (!strategy || typeof strategy !== 'object') return { kind: 'absent' };
    const item = strategy as StrategyLike;
    const raw = item.parameters?.market ?? item.market;
    const text = String(raw ?? '').trim().toUpperCase();
    if (!text) return { kind: 'absent' };
    const market = MARKET_ALIASES[text];
    if (!market) return { kind: 'unknown', raw: text };
    return { kind: 'market', market };
}

/**
 * 策略声明的市场；未声明或无法识别返回 `null`（不猜）。
 *
 * 优先 `parameters.market`（后端权威字段），兼容部分接口把 market 提到顶层的形态。
 * 需要区分「未声明」与「脏数据」时用 `strategyBelongsToMarket`。
 */
export function normalizeStrategyMarket(strategy: unknown): StrategyMarket | null {
    const declared = readDeclaredMarket(strategy);
    return declared.kind === 'market' ? declared.market : null;
}

/**
 * 策略是否属于某市场视图。
 *
 * 未声明市场 → 计入 A 股（后端契约）；声明了但无法识别 → **不属于任何市场**
 * （脏数据不该被任何页签认领，宁可少显示也不能张冠李戴）。
 */
export function strategyBelongsToMarket(strategy: unknown, market: unknown): boolean {
    const want = normalizeMarketKey(market);
    if (!want) return false;
    const declared = readDeclaredMarket(strategy);
    if (declared.kind === 'market') return declared.market === want;
    if (declared.kind === 'unknown') return false;
    return want === 'CN';
}

function normalizeMarketKey(market: unknown): StrategyMarket | null {
    const text = String(market ?? '').trim().toUpperCase();
    if (!text) return null;
    if (KNOWN_MARKETS.includes(text as StrategyMarket)) return text as StrategyMarket;
    return MARKET_ALIASES[text] ?? null;
}

/**
 * 按市场过滤策略列表。
 *
 * 未知页签市场 → 原样返回：宁可多显示（列表本身仍是用户的策略），也不因为一个
 * 前台传参问题把下拉清空——清空会让用户以为「策略丢了」。
 */
export function filterStrategiesByMarket<T>(strategies: T[] | null | undefined, market: unknown): T[] {
    const list = Array.isArray(strategies) ? strategies : [];
    if (!normalizeMarketKey(market)) return list;
    return list.filter((item) => strategyBelongsToMarket(item, market));
}

/**
 * 页签市场 vs 运行策略市场不一致时的话术；一致或未声明则返回空串。
 *
 * 与后端 `market_gate` 的措辞同口径：说清「按哪个市场在执行」，而不是只说「不匹配」。
 */
export function describeMarketMismatch(declared: unknown, active: unknown): string {
    const declaredKey = normalizeMarketKey(declared);
    if (!declaredKey) return '';
    const activeKey = normalizeMarketKey(active) ?? 'CN';
    if (declaredKey === activeKey) return '';
    return `当前运行策略为 ${activeKey} 市场，与所选页签 ${declaredKey} 不一致，交易按 ${activeKey} 口径执行`;
}
