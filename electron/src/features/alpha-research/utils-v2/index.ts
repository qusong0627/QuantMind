import { type ClassValue, clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatNumber(num: number, decimals: number = 2): string {
  return num.toFixed(decimals);
}

export function formatPercent(num: number, decimals: number = 2): string {
  return `${(num * 100).toFixed(decimals)}%`;
}

export function formatDate(date: string | Date): string {
  const d = typeof date === 'string' ? new Date(date) : date;
  return d.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
}

export function formatDateTime(date: string | Date): string {
  const d = typeof date === 'string' ? new Date(date) : date;
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/**
 * 有方向的指标着色（A股口径：**红涨绿跌**）。
 * 只用于收益/IC 这类有正负方向的量；回撤/换手等中性量不要用本函数。
 * 质量分级（getQualityBadgeClass）是另一套色语言：绿=高质量，与红涨绿跌无关。
 */
export type MetricTone = 'up' | 'down' | 'flat';

export function metricTone(value: number | null | undefined): MetricTone {
  if (value == null || typeof value !== 'number' || !Number.isFinite(value) || value === 0) {
    return 'flat';
  }
  return value > 0 ? 'up' : 'down';
}

/** 与 metricTone 配套的文本色类（A股口径：正=红、负=绿）。 */
export function metricToneClass(value: number | null | undefined): string {
  switch (metricTone(value)) {
    case 'up':
      return 'text-rose-500';
    case 'down':
      return 'text-emerald-500';
    default:
      return '';
  }
}

export function getQualityColor(quality: 'high' | 'medium' | 'low' | 'unknown'): string {
  switch (quality) {
    case 'high':
      return 'text-success';
    case 'medium':
      return 'text-warning';
    case 'low':
      return 'text-destructive';
    case 'unknown':
      return 'text-muted-foreground';
  }
}

export function getQualityBadgeClass(quality: 'high' | 'medium' | 'low' | 'unknown'): string {
  switch (quality) {
    case 'high':
      return 'bg-success/20 text-success border-success/50';
    case 'medium':
      return 'bg-warning/20 text-warning border-warning/50';
    case 'low':
      return 'bg-destructive/20 text-destructive border-destructive/50';
    case 'unknown':
      return 'bg-muted/30 text-muted-foreground border-border/50';
  }
}

export function generateId(): string {
  return Math.random().toString(36).substring(2, 15);
}

export function debounce<T extends (...args: any[]) => any>(
  func: T,
  wait: number
): (...args: Parameters<T>) => void {
  let timeout: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    if (timeout) clearTimeout(timeout);
    timeout = setTimeout(() => func(...args), wait);
  };
}
