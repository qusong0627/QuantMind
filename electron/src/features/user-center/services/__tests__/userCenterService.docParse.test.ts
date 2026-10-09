/**
 * MinerU 文档解析 Token 服务桥（userCenterService.getDocParseConfig /
 * saveDocParseConfig）—— 与 /api/v1/ai-ide/config/doc-parse 的契约面。
 *
 * 钉死的边：
 * - 走 AI 服务配置同一前缀（/ai-ide/config），不新开网关；
 * - 清除 = 显式空串（后端据此回落服务器 MINERU_API_TOKEN），不是省略字段；
 * - profile_readable 缺省为 true：旧后端没有该字段时按「可读但未配置」处理，
 *   不能因缺字段就渲染成「状态读取失败」。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => ({
      defaults: {},
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
      get: getMock,
      post: postMock,
    })),
  },
}));

vi.mock('../../../auth/services/authService', () => ({
  authService: { handle401Error: vi.fn() },
}));

import { userCenterService } from '../userCenterService';

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
});

describe('getDocParseConfig', () => {
  test('取 /ai-ide/config/doc-parse 并原样映射状态字段', async () => {
    getMock.mockResolvedValue({
      data: {
        success: true,
        profile_readable: true,
        has_user_token: true,
        masked_token: 'abc****wxyz',
        env_configured: false,
        effective_source: 'user',
      },
    });

    const result = await userCenterService.getDocParseConfig();

    expect(getMock).toHaveBeenCalledWith('/ai-ide/config/doc-parse', undefined);
    expect(result).toEqual({
      profile_readable: true,
      has_user_token: true,
      masked_token: 'abc****wxyz',
      env_configured: false,
      effective_source: 'user',
    });
  });

  test('响应残缺时安全退化：profile_readable 缺省 true，其余为空值', async () => {
    getMock.mockResolvedValue({ data: {} });

    const result = await userCenterService.getDocParseConfig();

    expect(result).toEqual({
      profile_readable: true,
      has_user_token: false,
      masked_token: '',
      env_configured: false,
      effective_source: 'none',
    });
  });
});

describe('saveDocParseConfig', () => {
  test('保存：Token 原样进 mineru_api_token 字段', async () => {
    postMock.mockResolvedValue({ data: { success: true, message: '已保存' } });

    const result = await userCenterService.saveDocParseConfig('tok-123');

    expect(postMock).toHaveBeenCalledWith(
      '/ai-ide/config/doc-parse',
      { mineru_api_token: 'tok-123' },
      undefined,
    );
    expect(result.message).toBe('已保存');
  });

  test('清除：显式空串必须真的发出（省略字段 = 不动，语义不同）', async () => {
    postMock.mockResolvedValue({ data: { success: true, message: '已清除' } });

    await userCenterService.saveDocParseConfig('');

    expect(postMock).toHaveBeenCalledWith(
      '/ai-ide/config/doc-parse',
      { mineru_api_token: '' },
      undefined,
    );
  });
});
