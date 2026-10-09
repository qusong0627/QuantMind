/**
 * evolve 提交的两条形态（文档链）——最容易悄悄回归的一段：
 *
 * - **不带 docId**：query 形态一字不动（断言逐条键值对的顺序与内容，而非
 *   只看「包含某个参数」——顺序变了老前端缓存/日志对不上，也算改动）；
 * - **带 docId**：改走 JSON body 变体，`doc_id` 必须原样下发（后端据此落
 *   source=doc 并回写文档 task_id），且不再拼 query（同一端点，后端按
 *   payload 非 None 覆盖 query，两条路径各自完整）；
 * - 两条路径的返回归一化一致：taskId 来自信封 data.task_id。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { apiPostMock } = vi.hoisted(() => ({ apiPostMock: vi.fn() }));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { post: apiPostMock, get: vi.fn(), delete: vi.fn() },
}));

import { startMining } from '../api';

beforeEach(() => {
  apiPostMock.mockReset();
  apiPostMock.mockResolvedValue({
    data: { data: { task_id: 't-99', status: 'pending' } },
  });
});

/** 拆出查询串的键值对（保持顺序），编码无关地断言 */
function queryPairs(url: string): Array<[string, string]> {
  const qs = url.split('?')[1] ?? '';
  return [...new URLSearchParams(qs).entries()];
}

describe('startMining：无 docId 时 query 形态零回归', () => {
  test('键值逐条一致、无第二参数（body）', async () => {
    await startMining({
      direction: '动量因子',
      market: 'a_share',
      universe: 'csi300',
      dataSource: 'qlib_bin',
      maxRounds: 3,
      directions: ['动量', '波动率'],
      directionMode: 'random',
    });

    expect(apiPostMock).toHaveBeenCalledTimes(1);
    const [url, body] = apiPostMock.mock.calls[0];
    expect(body).toBeUndefined();
    expect(String(url).startsWith('/alpha-agent/evolve?')).toBe(true);
    expect(queryPairs(String(url))).toEqual([
      ['loop_n', '3'],
      ['direction', '动量因子'],
      ['market', 'a_share'],
      ['universe', 'csi300'],
      ['data_source', 'qlib_bin'],
      ['directions', '动量'],
      ['directions', '波动率'],
      ['direction_mode', 'random'],
    ]);
  });

  test('可选项缺省时不出现；maxLoops 兜底 loop_n；空方向也带 direction=', async () => {
    await startMining({ direction: '', maxLoops: 7 });

    const [url] = apiPostMock.mock.calls[0];
    expect(queryPairs(String(url))).toEqual([
      ['loop_n', '7'],
      ['direction', ''],
    ]);
  });

  test('directions 逐条 trim、空串跳过', async () => {
    await startMining({ direction: 'd', directions: ['  动量  ', '', '  '] });

    const [url] = apiPostMock.mock.calls[0];
    expect(queryPairs(String(url)).filter(([k]) => k === 'directions')).toEqual([
      ['directions', '动量'],
    ]);
  });
});

describe('startMining：带 docId 时 JSON body 变体', () => {
  test('完整字段 + doc_id 进 body，URL 不带查询串', async () => {
    const resp = await startMining({
      direction: '按论文复现动量因子',
      market: 'a_share',
      universe: 'csi500',
      dataSource: 'parquet',
      maxRounds: 4,
      directions: ['动量'],
      directionMode: 'selected',
      docId: 'd0c1234567890abc',
    });

    expect(apiPostMock).toHaveBeenCalledTimes(1);
    const [url, body] = apiPostMock.mock.calls[0];
    expect(url).toBe('/alpha-agent/evolve');
    expect(body).toEqual({
      direction: '按论文复现动量因子',
      market: 'a_share',
      universe: 'csi500',
      data_source: 'parquet',
      loop_n: 4,
      directions: ['动量'],
      direction_mode: 'selected',
      doc_id: 'd0c1234567890abc',
    });
    expect(resp.success).toBe(true);
    expect(resp.data?.taskId).toBe('t-99');
    expect(resp.data?.task.config.userInput).toBe('按论文复现动量因子');
  });

  test('缺省字段在 body 里补齐后端默认值（请求仍完整）', async () => {
    await startMining({ direction: 'd', docId: 'd-1' });

    const [, body] = apiPostMock.mock.calls[0];
    expect(body).toEqual({
      direction: 'd',
      market: 'a_share',
      universe: 'csi300',
      data_source: '',
      loop_n: 3,
      directions: [],
      direction_mode: 'selected',
      doc_id: 'd-1',
    });
  });
});
