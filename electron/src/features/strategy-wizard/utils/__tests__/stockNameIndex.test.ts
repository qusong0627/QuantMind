import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const fetchMock = vi.fn();

describe('stockNameIndex', () => {
  beforeEach(() => {
    vi.resetModules();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('应该为缺少名称的 qlib symbol 补全简称', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [
          { symbol: '000001.SZ', code: '000001', exchange: 'SZ', name: '平安银行' },
        ],
      }),
    });

    const { patchMissingStockNames } = await import('../stockNameIndex');
    const patched = await patchMissingStockNames([
      { symbol: 'SZ000001', name: '', metrics: { market_cap: 1 } },
    ]);

    expect(patched[0].name).toBe('平安银行');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('应该兼容 code 格式并复用缓存', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [
          { symbol: '600519.SH', code: '600519', exchange: 'SH', name: '贵州茅台' },
        ],
      }),
    });

    const { patchMissingStockNames } = await import('../stockNameIndex');

    const patchedByCode = await patchMissingStockNames([
      { symbol: '600519', name: '' },
    ]);
    const patchedByDotSymbol = await patchMissingStockNames([
      { symbol: '600519.SH', name: '' },
    ]);

    expect(patchedByCode[0].name).toBe('贵州茅台');
    expect(patchedByDotSymbol[0].name).toBe('贵州茅台');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('已有名称时不应触发本地索引加载', async () => {
    const { patchMissingStockNames } = await import('../stockNameIndex');
    const items = [{ symbol: 'SZ000001', name: '已有名称' }];

    const patched = await patchMissingStockNames(items);

    expect(patched).toEqual(items);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});