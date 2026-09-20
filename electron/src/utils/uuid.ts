/**
 * 生成 UUID v4 的唯一入口。
 *
 * `crypto.randomUUID` **只在安全上下文**（https / localhost）存在：用户从局域网
 * `http://192.168.x.x:3080` 打开时它是 `undefined`，裸调直接 `TypeError` 把整个页面
 * 崩进 ErrorBoundary（2026-09-20 实测：持仓监控点「卖出 / 清仓」必崩，因为预检面板
 * 打开时的 effect 要生成 `batch_id`）。
 *
 * 这些 id 只做前端幂等键 / 本地会话号，不参与加密，所以没有 `randomUUID` 时按 v4 的
 * 位规则用 `Math.random` 拼一个同格式串即可（服务端按字符串收，格式必须是合法 v4）。
 */
export function newUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const bytes = Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
  const hex = bytes.map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
