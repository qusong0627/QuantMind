import { useCallback, useEffect, useRef, useState } from 'react';

/** 轮询数据 Hook：自动刷新 + 手动 refresh。
 *  生产加固（2026-09-07 卡顿复盘）：
 *  1. in-flight 防护——上一轮请求未返回时不叠发（慢响应不再雪球）；
 *  2. 失败指数退避——后端暂挂时 1s/2s/4s…/≤60s 退避，恢复后回到正常间隔；
 *  3. 请求错峰——首轮延迟加固定相位，避免全站 ~24 个轮询同时发请求风暴
 *     （浏览器 6 并发限制下排队造成整页周期性卡顿）。
 *  过渡期先轮询，未来换 SSE 时只改这一处。 */
const MAX_BACKOFF_MS = 60000;

export function usePolling<T>(fetcher: () => Promise<T>, deps: unknown[],
                              intervalMs = 30000, phaseMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    try {
      setError(null);
      setData(await fetcherRef.current());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const inflight = { busy: false };
    let errStreak = 0;
    let stopped = false;

    const schedule = (delayMs: number) => {
      if (stopped) return;
      // 页面切后台（document.hidden）→ 至少降到 60s 一次：多 tab/切走时不再 24 路空转烧网
      if (typeof document !== 'undefined' && document.hidden) {
        delayMs = Math.max(delayMs, 60000);
      }
      timer = setTimeout(() => void run(), delayMs);
    };

    const run = async () => {
      if (inflight.busy) return; // 上一轮未返回：跳过本次（不叠发）
      inflight.busy = true;
      try {
        const d = await fetcherRef.current();
        if (!alive) return;
        errStreak = 0;
        setError(null);
        setData(d);
        schedule(intervalMs);
      } catch (e) {
        if (!alive) return;
        errStreak += 1;
        setError(e instanceof Error ? e.message : String(e));
        // 指数退避：1s→2s→4s…封顶 60s；连续 3 次失败后不再烧网络
        const backoff = Math.min(1000 * 2 ** (errStreak - 1), MAX_BACKOFF_MS);
        schedule(errStreak >= 3 ? MAX_BACKOFF_MS : backoff);
      } finally {
        inflight.busy = false;
        if (alive) setLoading(false);
      }
    };

    timer = setTimeout(() => void run(), phaseMs % Math.max(intervalMs, 1000)); // 首轮错峰
    return () => {
      alive = false;
      stopped = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, refresh };
}
