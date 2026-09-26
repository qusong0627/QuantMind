import { useState, useEffect, useCallback, useRef } from 'react';
import { FundData } from '../services/userService';
import { portfolioService } from '../services/portfolioService';
import { shouldUpdateByFingerprint } from '../utils/dataChange';
import { refreshOrchestrator } from '../services/refreshOrchestrator';
import { useAppSelector } from '../store';
import { authService } from '../features/auth/services/authService';

export interface UseFundDataOptions {
  autoRefresh?: boolean;
  refreshInterval?: number;
  userId?: string;
  tenantId?: string;
}

export interface UseFundDataReturn {
  data: FundData | null;
  loading: boolean;
  error: string | null;
  lastUpdate: string | null;
  isSimulated: boolean;
  tradingMode: 'real' | 'simulation';
  refresh: () => Promise<void>;
}

export const useFundData = (options: UseFundDataOptions = {}): UseFundDataReturn => {
  const {
    autoRefresh = true,
    refreshInterval = 30000,
    userId,
    tenantId,
  } = options;

  const tradingMode = useAppSelector((state) => state.ui.tradingMode);
  const [data, setData] = useState<FundData | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);
  const [isSimulated, setIsSimulated] = useState<boolean>(tradingMode === 'simulation');
  const fingerprintRef = useRef<string | null>(null);
  const dataRef = useRef<FundData | null>(null);
  // 请求序号：只有最新一次请求的响应才允许写入状态。交易模式/账户切换会并发多次请求，
  // 旧响应若晚到会把新数据覆盖回旧值（典型表现：仪表盘首次打开先按默认模式取到空数据，
  // 随后模式被纠正为 simulation 并发出新请求，但旧的 real 响应后到，把正确金额覆盖成 0）。
  const requestSeqRef = useRef(0);

  const storedUser = authService.getStoredUser() as { id?: string; user_id?: string; tenant_id?: string } | null;
  const resolvedUserId = String(
    userId ||
    storedUser?.user_id ||
    storedUser?.id ||
    ''
  ).trim();
  const resolvedTenantId = String(
    tenantId ||
    storedUser?.tenant_id ||
    localStorage.getItem('tenant_id') ||
    (import.meta as any).env?.VITE_TENANT_ID ||
    'default'
  ).trim() || 'default';

  const fetchData = useCallback(async (params?: { silent?: boolean }) => {
    const silent = params?.silent ?? true;
    const seq = ++requestSeqRef.current;

    try {
      // 静默刷新不打断已有展示，避免大盘「加载慢 / 闪回 100 万」
      if (!silent || !dataRef.current) {
        setLoading(true);
      }
      setError(null);

      const result = await portfolioService.getFundOverview(resolvedUserId, tradingMode, resolvedTenantId);

      // 已有更新的请求发出，丢弃本次过期响应（否则会把新数据覆盖回旧值）
      if (seq !== requestSeqRef.current) {
        return;
      }

      const nextSnapshot = {
        data: result.data,
        isSimulated: result.isSimulated,
        mode: tradingMode
      };

      const { changed, fingerprint } = shouldUpdateByFingerprint(fingerprintRef.current, nextSnapshot);

      if (!changed) {
        return;
      }

      dataRef.current = result.data;
      setData(result.data);
      setIsSimulated(result.isSimulated);
      setLastUpdate(result.data.lastUpdate);
      fingerprintRef.current = fingerprint;
    } catch (err) {
      // 过期请求的失败不应污染最新状态
      if (seq !== requestSeqRef.current) {
        return;
      }
      const errorMessage = err instanceof Error ? err.message : '未知错误';
      setError(errorMessage);
      console.error('获取资金数据失败:', errorMessage);

      // 已有成功数据时保留，绝不降级成假 100 万
      if (!dataRef.current) {
        setData(null);
        setLastUpdate(null);
        fingerprintRef.current = null;
      }
    } finally {
      // 仅最新请求可以收尾 loading，否则会把仍在进行中的新请求标记为已结束
      if (seq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, [resolvedUserId, resolvedTenantId, tradingMode]);

  const refresh = useCallback(async () => {
    await fetchData({ silent: true });
  }, [fetchData]);

  useEffect(() => {
    // 切模式时作废指纹强制刷新，但保留上一帧数据作占位，避免总资产骨架空等。
    fingerprintRef.current = null;
    fetchData({ silent: Boolean(dataRef.current) });
  }, [fetchData]);

  useEffect(() => {
    if (!autoRefresh) {
      return;
    }

    const unregister = refreshOrchestrator.register(
      'fund',
      async () => {
        await fetchData({ silent: true });
      },
      { minIntervalMs: Math.min(Math.max(refreshInterval, 800), 5000) },
    );

    return unregister;
  }, [autoRefresh, refreshInterval, fetchData]);

  return {
    data,
    loading,
    error,
    lastUpdate,
    isSimulated,
    tradingMode,
    refresh,
  };
};
