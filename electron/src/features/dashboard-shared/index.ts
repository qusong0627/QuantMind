/**
 * 首页六宫格共享层（dashboard-shared）
 *
 * 出口只有四类：市场内容规格、市场徽标、数据态占位、开通动作。
 * 卡片组件从这里取「该市场这一格显示什么」，不要各自写市场分支。
 */

export type { AppMarket, BoxContent, BoxDataState, BoxId, MarketContent } from './types';
export { BOX_IDS } from './types';
export { MARKET_CONTENT, getMarketContent, formatBoxTitle } from './marketContent';
export { MarketChip } from './components/MarketChip';
export { BoxPlaceholder } from './components/BoxPlaceholder';
export { useBoxContent, useMarketContent, useOpenSimAccount } from './hooks/useMarketBoxes';
export { splitNotificationsByMarket } from './notificationScope';
export type { NotificationBucket, ScopedNotification } from './notificationScope';