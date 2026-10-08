import { describe, test, expect } from 'vitest';
import { extractApiError } from '../apiError';

const FALLBACK = '操作失败';

/** 造一个 axios 风格的错误对象 */
const axiosErr = (detail: unknown, message = 'Request failed with status code 422') => ({
  message,
  response: { data: { detail } },
});

describe('extractApiError', () => {
  test('detail 是字符串时直接用后端中文（优先于 axios 英文 message）', () => {
    expect(extractApiError(axiosErr('未知市场：XX'), FALLBACK)).toBe('未知市场：XX');
  });

  test('FastAPI 422 的 detail 数组被拼成一条字符串，而不是丢给 React 渲染', () => {
    // 这是本函数存在的理由：数组直接进 JSX 会抛
    // "Objects are not valid as a React child" → 白屏。
    const err = axiosErr([
      { loc: ['body', 'codes', 0], msg: 'String should have at most 128 characters', type: 'string_too_long' },
      { loc: ['body', 'dataset'], msg: 'Input should be classic or private', type: 'literal_error' },
    ]);

    const out = extractApiError(err, FALLBACK);

    expect(typeof out).toBe('string');
    // loc 的 'body' 前缀对用户无意义，应被剥掉
    expect(out).toBe(
      'codes.0: String should have at most 128 characters；dataset: Input should be classic or private',
    );
    expect(out).not.toContain('body');
  });

  test('detail 为空数组时回落到 message，不返回空串', () => {
    expect(extractApiError(axiosErr([]), FALLBACK)).toBe('Request failed with status code 422');
  });

  test('数组元素缺 msg 时跳过该项，其余照常展示', () => {
    const err = axiosErr([{ loc: ['body', 'x'] }, { loc: ['body', 'y'], msg: '必填' }]);
    expect(extractApiError(err, FALLBACK)).toBe('y: 必填');
  });

  test('detail 是 {message} / {detail} 形态的对象时也能取出来', () => {
    expect(extractApiError(axiosErr({ message: '额度不足' }), FALLBACK)).toBe('额度不足');
    expect(extractApiError(axiosErr({ detail: '版本已发布' }), FALLBACK)).toBe('版本已发布');
  });

  test('管理员重新登录提示优先于后端 detail（它更能指导用户动作）', () => {
    const err = { ...axiosErr('Forbidden'), _adminReauthHint: '管理员权限验证失败，请退出并重新登录' };
    expect(extractApiError(err, FALLBACK)).toBe('管理员权限验证失败，请退出并重新登录');
  });

  test('无 response（网络错误/超时）时用 axios message', () => {
    expect(extractApiError({ message: 'Network Error' }, FALLBACK)).toBe('Network Error');
  });

  test('全部落空时用兜底文案', () => {
    expect(extractApiError(undefined, FALLBACK)).toBe(FALLBACK);
    expect(extractApiError(null, FALLBACK)).toBe(FALLBACK);
    expect(extractApiError({}, FALLBACK)).toBe(FALLBACK);
    expect(extractApiError(new Error(''), FALLBACK)).toBe(FALLBACK);
  });

  test('异常值是数字/布尔等非 Error 时也不炸', () => {
    expect(extractApiError(42, FALLBACK)).toBe(FALLBACK);
    expect(extractApiError(true, FALLBACK)).toBe(FALLBACK);
  });

  test('超长 detail 被截断，避免把整段回显塞进 DOM', () => {
    const out = extractApiError(axiosErr('长'.repeat(2000)), FALLBACK);
    expect(out.length).toBe(501); // 500 + 省略号
    expect(out.endsWith('…')).toBe(true);
  });

  test('返回值恒为字符串——调用点会把它直接渲染进 JSX', () => {
    const weird: unknown[] = [
      axiosErr([{ loc: ['body'], msg: 'x' }]),
      axiosErr({ nested: { deep: 1 } }),
      { response: { data: null } },
      '字符串错误',
    ];
    for (const w of weird) {
      expect(typeof extractApiError(w, FALLBACK)).toBe('string');
    }
  });
});
