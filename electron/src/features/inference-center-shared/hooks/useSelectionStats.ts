/**
 * 单模型 Top50 历史统计状态机。
 *
 * - 进入工作区即按默认参数算一次全测试区间（默认 top_k=50、持有期取模型周期）；
 * - 换模型立即清空旧结果并重算，旧请求的晚到响应按序号丢弃；
 * - 首次「不带区间」的成功结果记作测试集全区间 `bounds`，供快捷区间使用；
 * - StrictMode 双挂载不重复发请求（同一模型只自动算一次）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  selectionStatsErrorMessage,
  selectionStatsService,
} from '../../../services/selectionStatsService';
import type {
  SelectionStatsQuery,
  SelectionStatsResponse,
} from '../../../services/selectionStatsTypes';

export interface SelectionBounds {
  start: string;
  end: string;
}

export interface SelectionStatsState {
  stats: SelectionStatsResponse | null;
  /** 测试集全区间的生效区间（由首次不带区间的请求确定） */
  bounds: SelectionBounds | null;
  loading: boolean;
  error: string | null;
  load: (query: SelectionStatsQuery) => void;
  reload: () => void;
}

export function useSelectionStats(
  modelId: string | null,
  excludeBlacklist = false,
): SelectionStatsState {
  const [stats, setStats] = useState<SelectionStatsResponse | null>(null);
  const [bounds, setBounds] = useState<SelectionBounds | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seqRef = useRef(0);
  const autoRef = useRef<string | null>(null);
  const lastQueryRef = useRef<SelectionStatsQuery>({});

  const load = useCallback(
    (query: SelectionStatsQuery) => {
      if (!modelId) return;
      const seq = ++seqRef.current;
      lastQueryRef.current = query;
      setLoading(true);
      setError(null);
      void selectionStatsService
        .fetch(modelId, { ...query, exclude_blacklist: excludeBlacklist })
        .then((data) => {
          if (seq !== seqRef.current) return;
          setStats(data);
          setLoading(false);
          if (!query.start && !query.end) {
            setBounds({ start: data.effective.start, end: data.effective.end });
          }
        })
        .catch((err: unknown) => {
          if (seq !== seqRef.current) return;
          setError(selectionStatsErrorMessage(err));
          setLoading(false);
        });
    },
    [modelId, excludeBlacklist],
  );

  const reload = useCallback(() => {
    load(lastQueryRef.current);
  }, [load]);

  useEffect(() => {
    const key = `${modelId ?? ''}|${excludeBlacklist ? 'excl' : 'all'}`;
    if (!modelId) {
      autoRef.current = null;
      seqRef.current += 1;
      setStats(null);
      setBounds(null);
      setError(null);
      setLoading(false);
      return;
    }
    if (autoRef.current === key) return;
    autoRef.current = key;
    // 换模型/换口径：旧结果与旧请求一并作废
    seqRef.current += 1;
    setStats(null);
    setBounds(null);
    setError(null);
    lastQueryRef.current = {};
    load({});
  }, [modelId, excludeBlacklist, load]);

  return { stats, bounds, loading, error, load, reload };
}
