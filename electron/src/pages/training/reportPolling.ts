/**
 * 严格评估 / 数据审计这类后台异步报告的轮询参数与错误提取。
 * 两处报告都按 8s 轮询、最多 12 分钟（90 次），口径必须一致。
 */
export const REPORT_POLL_INTERVAL_MS = 8000;
export const REPORT_POLL_MAX_ATTEMPTS = 90;

export function extractHttpStatus(err: unknown): number | null {
  const status = (err as { response?: { status?: number } })?.response?.status;
  return typeof status === 'number' ? status : null;
}
