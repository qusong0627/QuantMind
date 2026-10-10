/**
 * 收藏（自选）—— localStorage 持久化的最小工具，排行榜与因子报告左栏共用
 * （键由各自调用方构造：两个栏目是两套集合，互不串）。
 *
 * 读坏数据一律当空、写失败静默：收藏是便利功能，不该因为一条手改过的
 * localStorage 或隐私模式配额把整页打崩/弹错。
 */
export function loadFavs(key: string): string[] {
  try {
    const raw = localStorage.getItem(key);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

export function saveFavs(key: string, arr: string[]): void {
  try {
    localStorage.setItem(key, JSON.stringify(arr));
  } catch {
    /* 配额满 / 隐私模式：忽略 */
  }
}

/** 收藏集合的切换（纯函数，不改原数组） */
export function toggleInFavs(arr: string[], name: string): string[] {
  return arr.includes(name) ? arr.filter((x) => x !== name) : [...arr, name];
}
