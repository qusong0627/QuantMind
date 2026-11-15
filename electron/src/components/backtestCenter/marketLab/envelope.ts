/** QuantMind 后端统一信封 `{success, data}` / `{success:false, error}` 的解包。
 *
 * ⚠️ 行情实验室（`/api/v1/agent-arena/market-lab/*`）的业务错误是
 * **HTTP 200 + `{success:false, error}`**（只有入参类走 4xx）。
 * 旧实现只看 `.data.data`：`success:false` 时拿到 `undefined`，调用方当成功继续走，
 * 表现为「点了没反应 / 回测跑完没有任何结果」——错误被整条吞掉。
 * 这里统一把 `success:false` 变成 reject，让 `errText` 能把后端原因显示出来。
 */

export interface ApiEnvelope<T> {
  success?: boolean;
  data?: T;
  error?: string;
  detail?: string;
}

export async function unwrapEnvelope<T>(
  promise: Promise<{ data: ApiEnvelope<T> }>,
): Promise<T> {
  const body = (await promise).data;
  // fail-closed：只有显式 success:true 才认。缺 success 的畸形响应也按失败处理——
  // 否则 `data` 为 undefined 被当成功继续走，又回到静默失败。
  if (!body || body.success !== true) {
    throw new Error(body?.error || body?.detail || '请求失败');
  }
  return body.data as T;
}
