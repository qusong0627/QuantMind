/**
 * newUuid 测试。
 *
 * 盯的是一个真实事故：局域网 http（非安全上下文）下 `crypto.randomUUID` 是 undefined，
 * 持仓监控点「卖出 / 清仓」→ 预检面板 effect 生成 batch_id → TypeError → 整页崩进
 * ErrorBoundary。所以这里必须覆盖「没有 randomUUID 也要产出合法 v4」这条路径。
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { newUuid } from '../uuid';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('newUuid', () => {
  it('安全上下文：直接用 crypto.randomUUID', () => {
    const spy = vi.fn(() => '11111111-2222-4333-8444-555555555555');
    vi.stubGlobal('crypto', { randomUUID: spy });

    expect(newUuid()).toBe('11111111-2222-4333-8444-555555555555');
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('非安全上下文（http 局域网）：randomUUID 不存在也不抛错，产出合法 v4', () => {
    // Arrange：模拟 http://192.168.x.x 下 window.crypto 存在但没有 randomUUID
    vi.stubGlobal('crypto', {});

    // Act
    const id = newUuid();

    // Assert
    expect(id).toMatch(UUID_V4);
  });

  it('完全连 crypto 都没有（老 WebView）也能兜底', () => {
    vi.stubGlobal('crypto', undefined);

    expect(newUuid()).toMatch(UUID_V4);
  });

  it('兜底路径每次不同（幂等键不能撞车）', () => {
    vi.stubGlobal('crypto', {});

    const ids = new Set(Array.from({ length: 50 }, () => newUuid()));

    expect(ids.size).toBe(50);
  });
});
