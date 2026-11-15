import { describe, it, expect } from 'vitest';
import { unwrapEnvelope } from '../envelope';

describe('unwrapEnvelope', () => {
  it('returns data when success is true', async () => {
    // Arrange
    const promise = Promise.resolve({ data: { success: true, data: { id: 7 } } });

    // Act
    const result = await unwrapEnvelope(promise);

    // Assert
    expect(result).toEqual({ id: 7 });
  });

  it('rejects with the backend error when success is false', async () => {
    // Arrange：后端业务错误是 HTTP 200 + success:false
    const promise = Promise.resolve({
      data: { success: false, error: '回测失败: PyneCore 运行时未安装' },
    });

    // Act / Assert
    await expect(unwrapEnvelope(promise)).rejects.toThrow('回测失败: PyneCore 运行时未安装');
  });

  it('rejects instead of resolving undefined when data is missing', async () => {
    // Arrange：旧实现会返回 undefined，调用方当成功继续走（静默失败）
    const promise = Promise.resolve({ data: {} as { success?: boolean; data?: unknown } });

    // Act / Assert
    await expect(unwrapEnvelope(promise)).rejects.toThrow('请求失败');
  });

  it('falls back to detail when error is absent', async () => {
    // Arrange
    const promise = Promise.resolve({ data: { success: false, detail: '入参不合法' } });

    // Act / Assert
    await expect(unwrapEnvelope(promise)).rejects.toThrow('入参不合法');
  });
});
