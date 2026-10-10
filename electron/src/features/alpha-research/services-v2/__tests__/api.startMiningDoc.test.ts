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

import { dispatchMiningBatch, startMining } from '../api';

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
    expect(resp.data?.task?.config.userInput).toBe('按论文复现动量因子');
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

describe('startMining：并行方向数（T-MV-04）', () => {
  test('N>1 才下发 num_directions（追加在 direction_mode 之后），N=1 不下发', async () => {
    await startMining({
      direction: '',
      directions: ['动量', '波动率', '流动性'],
      directionMode: 'random',
      numDirections: 2,
    });

    const [url] = apiPostMock.mock.calls[0];
    expect(queryPairs(String(url))).toEqual([
      ['loop_n', '3'],
      ['direction', ''],
      ['directions', '动量'],
      ['directions', '波动率'],
      ['directions', '流动性'],
      ['direction_mode', 'random'],
      ['num_directions', '2'],
    ]);

    apiPostMock.mockClear();
    await startMining({ direction: 'd', numDirections: 1 });
    const [url1] = apiPostMock.mock.calls[0];
    expect(queryPairs(String(url1)).some(([k]) => k === 'num_directions')).toBe(false);
  });

  test('N>1 回执逐条归一：成功进 tasks（各自方向）、失败进 failures、摘要原样', async () => {
    apiPostMock.mockResolvedValueOnce({
      data: {
        data: {
          task_id: 't-0',
          items: [
            {
              index: 0,
              task_id: 't-0',
              status: 'running',
              queue_position: null,
              direction: '动量因子',
              direction_meta: '{"mode":"random"}',
              error: null,
            },
            {
              index: 1,
              task_id: 't-1',
              status: 'queued',
              queue_position: 2,
              direction: '波动率因子',
              direction_meta: null,
              error: null,
            },
            {
              index: 2,
              task_id: null,
              status: 'failed',
              queue_position: null,
              direction: '流动性因子',
              direction_meta: null,
              error: '队列已满（上限 8）',
            },
          ],
          started: 1,
          queued: 1,
          failed: 1,
          message: 'A股 已派发 2 条方向任务（启动 1 / 排队 1 / 失败 1）',
        },
      },
    });

    const resp = await startMining({
      direction: '',
      directions: ['动量因子', '波动率因子', '流动性因子'],
      directionMode: 'random',
      numDirections: 3,
    });

    expect(resp.success).toBe(true);
    expect(resp.data?.taskId).toBe('t-0');
    expect(resp.data?.tasks?.map((t) => t.taskId)).toEqual(['t-0', 't-1']);
    // 每条任务带自己的方向（不是首条方向的回声）
    expect(resp.data?.tasks?.[0].config.userInput).toBe('动量因子');
    expect(resp.data?.tasks?.[1].config.userInput).toBe('波动率因子');
    // 排队条目保留位次语义
    expect(resp.data?.tasks?.[1].status).toBe('queued');
    expect(resp.data?.tasks?.[1].queuePosition).toBe(2);
    expect(resp.data?.failures).toEqual([
      { direction: '流动性因子', error: '队列已满（上限 8）' },
    ]);
    expect(resp.data?.message).toContain('失败 1');
  });

  test('N>1 全部失败：无任务可展示——taskId 空串、tasks 空、failures 全量、task 缺席', async () => {
    apiPostMock.mockResolvedValueOnce({
      data: {
        data: {
          task_id: null,
          items: [
            {
              index: 0,
              task_id: null,
              status: 'failed',
              direction: '方向一',
              error: '队列已满',
            },
            {
              index: 1,
              task_id: null,
              status: 'failed',
              direction: '方向二',
              error: '硬件锁被占用',
            },
          ],
          started: 0,
          queued: 0,
          failed: 2,
        },
      },
    });

    const resp = await startMining({ direction: '', directions: ['方向一', '方向二'], numDirections: 2 });

    expect(resp.data?.taskId).toBe('');
    expect(resp.data?.task).toBeUndefined();
    expect(resp.data?.tasks).toEqual([]);
    expect(resp.data?.failures).toEqual([
      { direction: '方向一', error: '队列已满' },
      { direction: '方向二', error: '硬件锁被占用' },
    ]);
  });

  test('文档血统不下发 num_directions（body 形态逐字段不变）', async () => {
    await startMining({
      direction: 'd',
      docId: 'd-1',
      directions: ['动量'],
      numDirections: 3,
    });

    const [url, body] = apiPostMock.mock.calls[0];
    expect(url).toBe('/alpha-agent/evolve');
    expect(body).not.toHaveProperty('num_directions');
  });
});

describe('startMining / dispatchMiningBatch：入库闸门模式（T-MV-05）', () => {
  test('未指定或开启 → 两形态都不下发（任务行 NULL，生效模式归后端 env 兜底）', async () => {
    await startMining({ direction: 'd' });
    let [url] = apiPostMock.mock.calls[0];
    expect(queryPairs(String(url)).some(([k]) => k === 'quality_gate_mode')).toBe(
      false,
    );

    apiPostMock.mockClear();
    await startMining({ direction: 'd', qualityGateEnabled: true });
    [url] = apiPostMock.mock.calls[0];
    expect(queryPairs(String(url)).some(([k]) => k === 'quality_gate_mode')).toBe(
      false,
    );

    apiPostMock.mockClear();
    await startMining({ direction: 'd', docId: 'd-1', qualityGateEnabled: true });
    const [, body] = apiPostMock.mock.calls[0];
    expect(body).not.toHaveProperty('quality_gate_mode');
  });

  test('关闭 → query 形态追加 quality_gate_mode=off（在 num_directions 之后）', async () => {
    await startMining({
      direction: '',
      directions: ['动量', '波动率'],
      directionMode: 'random',
      numDirections: 2,
      qualityGateEnabled: false,
    });

    const [url] = apiPostMock.mock.calls[0];
    expect(queryPairs(String(url))).toEqual([
      ['loop_n', '3'],
      ['direction', ''],
      ['directions', '动量'],
      ['directions', '波动率'],
      ['direction_mode', 'random'],
      ['num_directions', '2'],
      ['quality_gate_mode', 'off'],
    ]);
  });

  test('关闭 + docId → body 带 quality_gate_mode=off，其余字段逐条不变', async () => {
    await startMining({
      direction: 'd',
      docId: 'd-1',
      qualityGateEnabled: false,
    });

    const [url, body] = apiPostMock.mock.calls[0];
    expect(url).toBe('/alpha-agent/evolve');
    expect(body).toEqual({
      direction: 'd',
      market: 'a_share',
      universe: 'csi300',
      data_source: '',
      loop_n: 3,
      directions: [],
      direction_mode: 'selected',
      doc_id: 'd-1',
      quality_gate_mode: 'off',
    });
  });

  test('dispatchMiningBatch：关闭 → body 带 off；未指定 → 不携带', async () => {
    await dispatchMiningBatch({
      directions: ['方向一'],
      qualityGateEnabled: false,
    });
    let body = apiPostMock.mock.calls[0][1];
    expect(body.quality_gate_mode).toBe('off');

    apiPostMock.mockClear();
    await dispatchMiningBatch({ directions: ['方向一'] });
    body = apiPostMock.mock.calls[0][1];
    expect(body).not.toHaveProperty('quality_gate_mode');
  });
});
