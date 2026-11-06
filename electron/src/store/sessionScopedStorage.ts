/** Build a localStorage key that is private to the active tenant and user. */
export function getSessionScopedStorageKey(
  baseKey: string,
  expectedUserId?: string | number,
): string | null {
  try {
    const user = JSON.parse(localStorage.getItem('user') || '{}') as {
      id?: string | number;
      user_id?: string | number;
      tenant_id?: string;
    };
    const userId = user.user_id ?? user.id;
    if (!user.tenant_id || userId === undefined || userId === null || !String(userId)) {
      return null;
    }
    if (expectedUserId !== undefined && String(userId) !== String(expectedUserId)) {
      return null;
    }
    return `${baseKey}:v2:${encodeURIComponent(user.tenant_id)}:${encodeURIComponent(String(userId))}`;
  } catch {
    return null;
  }
}

/** 一次性把旧 baseKey 下的条目搬到新 baseKey（键名品牌更名时用），幂等。 */
export function migrateSessionScopedStorageKey(oldBaseKey: string, newBaseKey: string): void {
  if (oldBaseKey === newBaseKey) return;
  try {
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index);
      if (!key || (key !== oldBaseKey && !key.startsWith(`${oldBaseKey}:`))) continue;
      const target = `${newBaseKey}${key.slice(oldBaseKey.length)}`;
      if (localStorage.getItem(target) === null) {
        const value = localStorage.getItem(key);
        if (value !== null) localStorage.setItem(target, value);
      }
      localStorage.removeItem(key);
    }
  } catch {
    /* Storage can be unavailable in restricted browser contexts. */
  }
}
