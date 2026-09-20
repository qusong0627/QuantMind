/**
 * 持仓预警投递（桌面通知 + 声音）——挂在 **App 根**上，**常驻**。
 *
 * 挂根不挂 DashboardLayout：`/trading`、`/desk` 是独立路由，不在那个外壳里，
 * 而它们恰恰是用户盯持仓时开着的页面——挂外壳上就成了「盯盘那页收不到提醒」。
 *
 * 为什么不复用 `useNotifications`：它是通知中心的列表 hook，只挂在一个仪表盘卡片里
 * （`ModuleGrid → NotificationQuickCard`）。用户盯持仓/看行情时那一页未必开着，
 * 靠它投递就会「切走了就不响」。这里自己拉 `holding-alerts` 列表：
 * 渠道独立、去重由 `services/alertDelivery` 按 id 单调做（跨调用方共享，不会重复播报）。
 *
 * 节奏：哨兵 60s 扫一轮，这里 30s 拉一次 → 最坏多等 30s，换来的是不依赖任何页面。
 */

import { useEffect } from 'react';
import { authService } from '../features/auth/services/authService';
import { holdingAlertService } from '../services/holdingAlertService';
import { deliverNewAlerts } from '../services/alertDelivery';

/** 轮询间隔：哨兵周期 60s 的一半，避免恰好卡在两轮之间 */
export const DELIVERY_POLL_MS = 30_000;

export function useHoldingAlertDelivery(enabled: boolean = true): void {
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number | null = null;

    const tick = async () => {
      // 未登录不拉：否则每次轮询都吃一个 401，还会走 handle401Error 的登出分支
      if (!authService.getAccessToken()) return;
      try {
        const result = await holdingAlertService.listAlerts({ status: 'active', limit: 20 });
        if (cancelled) return;
        deliverNewAlerts(result.items, result.config);
      } catch (err) {
        // 投递是尽力而为：网络抖动不该冒泡成页面错误，但也不静默——留一条可查的日志
        console.warn('[useHoldingAlertDelivery] 预警轮询失败', err);
      }
    };

    void tick();
    timer = window.setInterval(() => { void tick(); }, DELIVERY_POLL_MS);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [enabled]);
}

export default useHoldingAlertDelivery;
