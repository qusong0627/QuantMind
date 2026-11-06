import type { Middleware } from '@reduxjs/toolkit';

export const sessionBoundaryActions = new Set([
  'auth/logout/pending',
  'auth/login/pending',
  'auth/loginWithSmsCode/pending',
  'auth/initialize/pending',
]);

let sessionGeneration = 0;
const resetters = new Set<() => void>();
export const getSessionGeneration = () => sessionGeneration;
export function registerSessionReset(reset: () => void): () => void {
  resetters.add(reset);
  return () => { resetters.delete(reset); };
}
export function invalidateSession(): void {
  sessionGeneration += 1;
  resetters.forEach((reset) => {
    try { reset(); } catch (error) { console.error('Failed to clear private session state', error); }
  });
  // Lazy feature stores might not have registered yet. Remove their old cache too.
  try {
    localStorage.removeItem('backtest-storage');
    localStorage.removeItem('backtest-center-storage');
    // The old AlphaResearch key was shared across every local user. Drop it
    // instead of assigning its preferences to whichever account logs in next.
    localStorage.removeItem('autoalpha_config');
    // 品牌更名前遗留的旧键：清掉，避免把旧偏好算到下一个登录账号头上。
    localStorage.removeItem('quantaalpha_config');
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index);
      if (key?.startsWith('alpha_explain_')) localStorage.removeItem(key);
    }
  } catch { /* Storage can be unavailable in restricted browser contexts. */ }
}

/** Ignore thunk completions belonging to a previous login session. */
export function createSessionBoundaryMiddleware(): Middleware {
  const requests = new Map<string, number>();
  return () => (next) => (unknownAction) => {
    const action = unknownAction as { type?: string; meta?: { requestId?: string } };
    const type = action?.type || '';
    if (sessionBoundaryActions.has(type)) invalidateSession();
    const requestId = action.meta?.requestId;
    if (requestId && type.endsWith('/pending')) {
      requests.set(requestId, sessionGeneration);
    } else if (requestId && (type.endsWith('/fulfilled') || type.endsWith('/rejected'))) {
      const started = requests.get(requestId);
      requests.delete(requestId);
      if (started !== undefined && started !== sessionGeneration) return unknownAction;
    }
    return next(unknownAction);
  };
}
