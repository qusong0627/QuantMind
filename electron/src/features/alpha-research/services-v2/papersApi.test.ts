import type { InternalAxiosRequestConfig } from 'axios';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/aiStrategyClients', async () => {
  const { default: axiosModule } = await import('axios');
  return { apiClient: axiosModule.create({ headers: { 'Content-Type': 'application/json' } }) };
});

import { apiClient } from '../../../services/aiStrategyClients';
import { uploadPersonalPaper } from './papersApi';

describe('personal paper upload', () => {
  it('preserves the PDF form through Axios request transformation', async () => {
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.data).toBeInstanceOf(FormData);
      expect((config.data as FormData).get('file')).toMatchObject({ name: 'paper.pdf' });
      expect(config.headers.getContentType()).not.toBe('application/json');
      return { data: { code: 200, data: { paper_id: 'p1' } }, status: 200, statusText: 'OK', headers: {}, config };
    });
    apiClient.defaults.adapter = adapter;
    const file = new File(['%PDF-1.7'], 'paper.pdf', { type: 'application/pdf' });
    await expect(uploadPersonalPaper(file)).resolves.toEqual({ paper_id: 'p1' });
    expect(adapter).toHaveBeenCalledOnce();
  });
});
