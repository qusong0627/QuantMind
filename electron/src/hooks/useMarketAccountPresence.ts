/**
 * 市场账户活跃度门控：判断当前账户在指定市场是否有持仓或成交。
 *
 * 首页仪表盘策略监控/智能图表/信息通知三卡没有市场维度的数据模型，
 * 切市场时用本 hook 统一门控——无活动则显空，有活动才展示账户级内容，
 * 避免一直显示 A 股数据。
 */
import { useEffect, useRef, useState } from 'react';
import { useAppSelector } from '../store';
import { tradingService } from '../services/tradingService';
import { realTradingService } from '../services/realTradingService';
import { authService } from '../features/auth/services/authService';
import { symbolToAppMarket } from '../utils/portfolioUtils';

interface PresenceCacheEntry {
  at: number;
  present: boolean;
}

const CACHE_TTL_MS = 30_000;
const cache = new Map<string, PresenceCacheEntry>();

function countActivePositions(account: any): number {
  const positions = account?.positions;
  if (!positions) return 0;
  const list = Array.isArray(positions) ? positions : Object.entries(positions).map(([symbol, pos]) => ({ ...(pos as object), symbol }));
  return list.filter((p: any) => Number(p?.volume) > 0).length;
}

function positionMarket(symbol: string | undefined): string {
  return symbolToAppMarket(symbol || '');
}

export function useMarketAccountPresence(market: string): { present: boolean; loading: boolean } {
  const tradingMode = useAppSelector((state) => state.ui.tradingMode);
  const [present, setPresent] = useState(true);
  const [loading, setLoading] = useState(true);
  const seqRef = useRef(0);

  useEffect(() => {
    const storedUser = authService.getStoredUser() as { id?: string; user_id?: string; tenant_id?: string } | null;
    const userId = String(storedUser?.user_id || storedUser?.id || '').trim();
    const tenantId = String(
      storedUser?.tenant_id || localStorage.getItem('tenant_id') || 'default',
    ).trim() || 'default';
    const target = String(market || 'CN').toUpperCase();
    const key = `${tenantId}|${userId}|${tradingMode}|${target}`;
    const seq = ++seqRef.current;

    const cached = cache.get(key);
    if (cached && Date.now() - cached.at < CACHE_TTL_MS) {
      setPresent(cached.present);
      setLoading(false);
      return;
    }

    setLoading(true);
    (async () => {
      try {
        let posCount = 0;
        if (tradingMode === 'simulation') {
          // 分市场账户：直接取该市场账本持仓
          const account = await realTradingService.getSimulationAccount(userId, tenantId, target).catch(() => null);
          posCount = countActivePositions(account);
        } else {
          const account = await realTradingService.getAccount(userId, tenantId).catch(() => null);
          const positions = account?.positions;
          const list = Array.isArray(positions)
            ? positions
            : Object.entries(positions || {}).map(([symbol, pos]) => ({ ...(pos as object), symbol }));
          posCount = list.filter((p: any) => Number(p?.volume) > 0 && positionMarket(p?.symbol) === target).length;
        }
        let tradeCount = 0;
        try {
          const result = await tradingService.getRecentTrades(100, tradingMode);
          tradeCount = (result.records || []).filter((r) => positionMarket(r.symbol) === target).length;
        } catch {
          tradeCount = 0;
        }
        const next = posCount > 0 || tradeCount > 0;
        cache.set(key, { at: Date.now(), present: next });
        if (seq === seqRef.current) {
          setPresent(next);
          setLoading(false);
        }
      } catch {
        if (seq === seqRef.current) {
          // 查不到按有活动处理，避免误清空（保持现有展示）。
          setPresent(true);
          setLoading(false);
        }
      }
    })();
  }, [market, tradingMode]);

  return { present, loading };
}
