import { useCallback, useState } from 'react';

import { useAppSelector } from '../../../store';
import { selectCurrentMarket, type AppMarket } from '../../../store/slices/uiSlice';
import { getMarketContent } from '../marketContent';
import type { BoxContent, BoxId, MarketContent } from '../types';

/** 当前市场的内容规格（六宫格卡片统一从这里取标题/空态/数据源标注） */
export function useMarketContent(): MarketContent {
  const market = useAppSelector(selectCurrentMarket);
  return getMarketContent(market);
}

/** 某个框在当前市场下的规格 */
export function useBoxContent(boxId: BoxId): { market: AppMarket; marketContent: MarketContent; content: BoxContent } {
  const marketContent = useMarketContent();
  return { market: marketContent.market, marketContent, content: marketContent.boxes[boxId] };
}

interface OpenSimAccountResult {
  ok: boolean;
  message: string;
}

/**
 * 空态「开通模拟盘」动作：按当前市场创建/重置模拟账户。
 *
 * 后端 `POST /api/v1/simulation/reset` 已支持 market 维度（账户键规范见
 * backend/shared/simulation_account_keys.py），这里只负责带市场与默认资金调用。
 */
export function useOpenSimAccount() {
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const openAccount = useCallback(
    async (market: AppMarket | string, initialCash: number): Promise<OpenSimAccountResult> => {
      setOpening(true);
      setError(null);
      try {
        const { realTradingService } = await import('../../../services/realTradingService');
        const account = await realTradingService.resetSimulationAccount('current', initialCash, undefined, String(market));
        if (!account) {
          const message = '开通失败：后端未返回账户信息';
          setError(message);
          return { ok: false, message };
        }
        return { ok: true, message: '模拟盘已开通' };
      } catch (e) {
        const message = e instanceof Error ? e.message : '开通失败';
        setError(message);
        return { ok: false, message };
      } finally {
        setOpening(false);
      }
    },
    [],
  );

  return { openAccount, opening, error };
}
