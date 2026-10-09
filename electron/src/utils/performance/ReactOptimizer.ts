/**
 * React性能优化工具
 */

import { useEffect, useRef, DependencyList } from 'react';

/**
 * 防抖Hook
 */
export function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = React.useState<T>(value);

  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    return () => {
      clearTimeout(handler);
    };
  }, [value, delay]);

  return debouncedValue;
}

/**
 * 节流Hook
 */
export function useThrottle<T>(value: T, limit: number): T {
  const [throttledValue, setThrottledValue] = React.useState<T>(value);
  const lastRan = useRef(Date.now());

  useEffect(() => {
    const handler = setTimeout(() => {
      if (Date.now() - lastRan.current >= limit) {
        setThrottledValue(value);
        lastRan.current = Date.now();
      }
    }, limit - (Date.now() - lastRan.current));

    return () => {
      clearTimeout(handler);
    };
  }, [value, limit]);

  return throttledValue;
}

/**
 * 深度比较Hook
 */
export function useDeepCompareEffect(
  callback: () => void | (() => void),
  dependencies: DependencyList
): void {
  const ref = useRef<DependencyList>(dependencies);

  if (!deepEqual(dependencies, ref.current)) {
    ref.current = dependencies;
  }

  useEffect(callback, ref.current);
}

/**
 * 深度相等比较
 */
function deepEqual(a: any, b: any): boolean {
  if (a === b) return true;
  if (a == null || b == null) return false;
  if (typeof a !== 'object' || typeof b !== 'object') return false;

  const keysA = Object.keys(a);
  const keysB = Object.keys(b);

  if (keysA.length !== keysB.length) return false;

  for (const key of keysA) {
    if (!keysB.includes(key)) return false;
    if (!deepEqual(a[key], b[key])) return false;
  }

  return true;
}

/**
 * 性能监控Hook
 */
export function usePerformanceMonitor(componentName: string): void {
  const renderCount = useRef(0);
  const renderTimes = useRef<number[]>([]);
  const startTime = useRef<number>(0);

  // 记录渲染开始时间
  startTime.current = performance.now();

  useEffect(() => {
    // 记录渲染结束时间
    const endTime = performance.now();
    const renderTime = endTime - startTime.current;

    renderCount.current++;
    renderTimes.current.push(renderTime);

    // 只保留最近10次渲染时间
    if (renderTimes.current.length > 10) {
      renderTimes.current.shift();
    }

    // 每10次渲染输出统计
    if (renderCount.current % 10 === 0) {
      const avgTime = renderTimes.current.reduce((a, b) => a + b, 0) / renderTimes.current.length;
      console.log(`[Performance] ${componentName}: ${renderCount.current} renders, avg: ${avgTime.toFixed(2)}ms`);
    }
  });
}

/**
 * 懒加载Hook
 */
export function useLazyLoad<T>(
  loadFn: () => Promise<T>,
  deps: DependencyList = []
): { data: T | null; loading: boolean; error: Error | null } {
  const [data, setData] = React.useState<T | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;

    setLoading(true);
    setError(null);

    loadFn()
      .then(result => {
        if (!cancelled) {
          setData(result);
          setLoading(false);
        }
      })
      .catch(err => {
        if (!cancelled) {
          setError(err);
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, deps);

  return { data, loading, error };
}

/**
 * 虚拟滚动配置
 */
export interface VirtualScrollConfig {
  itemHeight: number;
  containerHeight: number;
  overscan?: number;
}

/**
 * 计算虚拟滚动范围
 */
export function calculateVirtualRange(
  scrollTop: number,
  config: VirtualScrollConfig,
  itemCount: number
): { start: number; end: number; offsetY: number } {
  const { itemHeight, containerHeight, overscan = 3 } = config;

  const start = Math.max(0, Math.floor(scrollTop / itemHeight) - overscan);
  const visibleCount = Math.ceil(containerHeight / itemHeight);
  const end = Math.min(itemCount, start + visibleCount + overscan * 2);
  const offsetY = start * itemHeight;

  return { start, end, offsetY };
}

/**
 * 性能分析器
 */
export class PerformanceProfiler {
  private marks: Map<string, number> = new Map();
  private measures: Map<string, number[]> = new Map();

  /**
   * 开始标记
   */
  mark(name: string): void {
    this.marks.set(name, performance.now());
  }

  /**
   * 结束标记并记录
   */
  measure(name: string, startMark: string): number {
    const startTime = this.marks.get(startMark);
    if (!startTime) {
      console.warn(`Start mark "${startMark}" not found`);
      return 0;
    }

    const duration = performance.now() - startTime;

    if (!this.measures.has(name)) {
      this.measures.set(name, []);
    }
    this.measures.get(name)!.push(duration);

    return duration;
  }

  /**
   * 获取统计信息
   */
  getStats(name: string): { count: number; avg: number; min: number; max: number } | null {
    const measures = this.measures.get(name);
    if (!measures || measures.length === 0) {
      return null;
    }

    return {
      count: measures.length,
      avg: measures.reduce((a, b) => a + b, 0) / measures.length,
      min: Math.min(...measures),
      max: Math.max(...measures)
    };
  }

  /**
   * 清除数据
   */
  clear(): void {
    this.marks.clear();
    this.measures.clear();
  }
}

// 全局性能分析器
export const globalProfiler = new PerformanceProfiler();

// 曾有过一段 `declare global { namespace React { function useState<T>(...) } }`，
// 号称「React 命名空间修复」。它不是修复：`@types/react` 已经导出 `useState`，
// 这段只是往全局 React 命名空间里**再叠一个重载**，而它的 setter 签名是
// `(value: T) => void` —— 没有函数式更新。全局重载一旦胜出，全仓的
// `setX(prev => ...)` 都会报 TS2345，而 `setX(true)` 照样通过，所以它能一直潜伏：
// 删掉之前整个仓库**一处函数式 setState 都写不出来**（grep 实测 0 处）。
import * as React from 'react';
