/**
 * 向量检索配置的请求组装：三态语义（undefined = 不动 / '' = 清除 / 有值 = 设置）。
 *
 * 这是整块 UI 里最容易悄悄坏掉的一环：改错了不会报错、不会白屏，只会让
 * 「改个模型名」顺手清掉 Key，然后子进程静默换供应商。故逐条锁住。
 */

import { describe, expect, it } from 'vitest';
import { buildEmbeddingSavePayload, type EmbeddingStatus } from '../embeddingPayload';

const SERVER: EmbeddingStatus = {
  model: 'BAAI/bge-m3',
  base_url: 'https://api.siliconflow.cn/v1',
  has_key: true,
  key_masked: 'sk-****cdef',
};

const form = (over: Partial<{ model: string; baseUrl: string; apiKey: string }> = {}) => ({
  model: SERVER.model,
  baseUrl: SERVER.base_url,
  apiKey: '',
  ...over,
});

describe('buildEmbeddingSavePayload', () => {
  it('没有改动时返回空对象（调用方据此提示「没有需要保存的改动」）', () => {
    expect(buildEmbeddingSavePayload(form(), SERVER)).toEqual({});
  });

  it('只改模型名时，不得带出 baseUrl / apiKey', () => {
    // 曾经的失败模式：全量提交让 model 的修改顺手清掉已存的 Key
    const payload = buildEmbeddingSavePayload(form({ model: 'BAAI/bge-large' }), SERVER);

    expect(payload).toEqual({ model: 'BAAI/bge-large' });
    expect('apiKey' in payload).toBe(false);
    expect('baseUrl' in payload).toBe(false);
  });

  it('只补 Key 时，不得带出 model / baseUrl', () => {
    const payload = buildEmbeddingSavePayload(form({ apiKey: 'sk-new-key-1234' }), SERVER);

    expect(payload).toEqual({ apiKey: 'sk-new-key-1234' });
  });

  it('Key 留空 = 不动，而不是清空', () => {
    const payload = buildEmbeddingSavePayload(form({ model: 'x', apiKey: '   ' }), SERVER);

    expect('apiKey' in payload).toBe(false);
  });

  it('把模型改成空串 = 清除该字段（显式清空要能发出去）', () => {
    const payload = buildEmbeddingSavePayload(form({ model: '   ' }), SERVER);

    expect(payload).toEqual({ model: '' });
  });

  it('把接口地址改成空串 = 清除该字段', () => {
    const payload = buildEmbeddingSavePayload(form({ baseUrl: '' }), SERVER);

    expect(payload).toEqual({ baseUrl: '' });
  });

  it('三项都改时三项都提交', () => {
    const payload = buildEmbeddingSavePayload(
      { model: 'm2', baseUrl: 'https://other.example/v1', apiKey: 'sk-k-1234567890' },
      SERVER,
    );

    expect(payload).toEqual({
      model: 'm2',
      baseUrl: 'https://other.example/v1',
      apiKey: 'sk-k-1234567890',
    });
  });

  it('首尾空白先 trim 再比对：只加空格不算改动', () => {
    const payload = buildEmbeddingSavePayload(
      form({ model: `  ${SERVER.model}  `, baseUrl: `\t${SERVER.base_url} ` }),
      SERVER,
    );

    expect(payload).toEqual({});
  });

  it('提交的值也是 trim 过的', () => {
    const payload = buildEmbeddingSavePayload(form({ model: '  bge-large  ' }), SERVER);

    expect(payload).toEqual({ model: 'bge-large' });
  });

  it('状态没拉到（undefined）时不提交 model/baseUrl —— 无从比对，提交即误清', () => {
    // 调用方另有「状态没到就不许保存」的守卫；这里是第二道防线：
    // 万一守卫被绕过，也不能把服务端已存的 model/baseUrl 用空表覆盖掉。
    const payload = buildEmbeddingSavePayload({ model: '', baseUrl: '', apiKey: 'sk-x' }, undefined);

    expect(payload).toEqual({});
  });

  it('服务端未配置（空初值）时，填了就是新增', () => {
    const empty: EmbeddingStatus = { model: '', base_url: '', has_key: false, key_masked: '' };
    const payload = buildEmbeddingSavePayload(
      { model: 'BAAI/bge-m3', baseUrl: 'https://api.siliconflow.cn/v1', apiKey: 'sk-k-1234567890' },
      empty,
    );

    expect(payload).toEqual({
      model: 'BAAI/bge-m3',
      baseUrl: 'https://api.siliconflow.cn/v1',
      apiKey: 'sk-k-1234567890',
    });
  });
});
