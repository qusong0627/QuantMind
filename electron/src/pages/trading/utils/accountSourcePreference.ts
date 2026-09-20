/**
 * 实盘账户「按源看」偏好（本地）：页面看哪个券商账户的快照。
 *
 * 与「设为交易券商」是**两件事**：本偏好只改**看**（GET /account?source=），
 * 不改下单路由（那是 PUT /broker-config/selected/CN，需二次确认）。
 * 分开的原因：多券商并行上报时（QMT 50 只 / 通达信 8 只，是两个真实账户），
 * 用户经常只想瞄一眼另一个账户，而不是把自己的交易券商切过去。
 *
 * 按市场分键（当前只有 CN 有实盘快照源）；切换时广播，让正在轮询的页面**立即**重取，
 * 而不是等下一个 5s 周期（否则点了 chip 数字要愣 5 秒才变，像没生效）。
 */

const STORAGE_PREFIX = 'qm:trading:accountSource';
/** null = 跟随当前实盘券商（后端仲裁），是默认态 */
type PreferredSource = string | null;

const listeners = new Set<() => void>();

function storageKey(market?: string | null): string {
    return `${STORAGE_PREFIX}:${(market || 'CN').toUpperCase()}`;
}

export function getPreferredAccountSource(market?: string | null): PreferredSource {
    try {
        const raw = localStorage.getItem(storageKey(market));
        const value = (raw || '').trim();
        return value || null;
    } catch {
        return null;
    }
}

export function setPreferredAccountSource(source: PreferredSource, market?: string | null): void {
    const key = storageKey(market);
    try {
        const value = String(source || '').trim();
        if (value) localStorage.setItem(key, value);
        else localStorage.removeItem(key);
    } catch {
        /* 存不下就只在本次广播里生效，不阻断页面 */
    }
    listeners.forEach(listener => {
        try {
            listener();
        } catch {
            /* 单个订阅者异常不影响其它订阅者 */
        }
    });
}

export function subscribeAccountSourceChange(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}
