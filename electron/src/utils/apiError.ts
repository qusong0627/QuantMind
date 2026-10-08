/**
 * 把 axios / FastAPI 的报错对象提炼成**一条可展示的字符串**。
 *
 * 为什么需要一个函数而不是到处写 `err?.response?.data?.detail || err?.message`：
 * FastAPI 的 `detail` **不保证是字符串**。请求体校验失败（422）时它是
 * `[{loc, msg, type}, ...]` 数组，直接丢进 JSX 会抛
 * `Objects are not valid as a React child` —— 于是「后端明确告诉你哪里填错了」
 * 变成「页面白屏」。本站所有带 Pydantic 值域约束的端点都有这条路径。
 *
 * 取值优先级：管理员鉴权提示 → 后端 detail → axios message → 兜底文案。
 */

/** 展示长度上限：detail 里可能塞着整段回显（如超长请求体），不设限会拖垮渲染。 */
const MAX_LEN = 500;

/** 兜底：任何情况下都返回字符串，绝不把异常抛给调用方（它跑在 catch 里）。 */
function safeString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

/** FastAPI 校验错误项：`{loc: ['body','codes',0], msg: '...'}` */
function formatValidationItem(item: unknown): string | null {
  if (typeof item === 'string') return safeString(item);
  if (!item || typeof item !== 'object') return null;

  const rec = item as { loc?: unknown; msg?: unknown };
  const msg = safeString(rec.msg);
  if (!msg) return null;

  // loc[0] 恒为 'body'/'query' 这类来源前缀，对用户无意义，去掉。
  const loc = Array.isArray(rec.loc) ? rec.loc.slice(1).map(String).join('.') : '';
  return loc ? `${loc}: ${msg}` : msg;
}

function formatDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return safeString(detail);
  if (Array.isArray(detail)) {
    const parts = detail.map(formatValidationItem).filter((s): s is string => !!s);
    return parts.length ? parts.join('；') : null;
  }
  if (detail && typeof detail === 'object') {
    // 少数处理器返回 {message: '...'} / {detail: '...'} 形态的对象。
    const rec = detail as { message?: unknown; detail?: unknown };
    return safeString(rec.message) || safeString(rec.detail);
  }
  return null;
}

/**
 * @param err           catch 到的任意值
 * @param fallback      全部落空时的文案（必传，避免把 undefined 渲染出来）
 */
export function extractApiError(err: unknown, fallback: string): string {
  try {
    const rec = (err ?? {}) as {
      _adminReauthHint?: unknown;
      message?: unknown;
      response?: { data?: { detail?: unknown } };
    };

    const text =
      safeString(rec._adminReauthHint) ||
      formatDetail(rec.response?.data?.detail) ||
      safeString(rec.message) ||
      fallback;

    return text.length > MAX_LEN ? `${text.slice(0, MAX_LEN)}…` : text;
  } catch {
    // 展示层不该再抛：这里再炸一次，用户看到的就是白屏而不是错误原因。
    return fallback;
  }
}
