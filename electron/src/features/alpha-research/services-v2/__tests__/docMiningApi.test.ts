/**
 * 文档链 API 桥 —— 与后端 alpha_agent_docs 的契约面。
 *
 * 钉死的边：
 * - **错误文案优先取后端 detail 原文**（用户看到的拒绝理由不是「请求失败」）；
 * - 上传是 multipart：请求必须显式声明 `Content-Type: multipart/form-data`。
 *   apiClient 实例默认 application/json，axios≥1 的 transformRequest 会把
 *   FormData 直接 JSON.stringify（formDataToJSON）——那是 2026-10-09 线上
 *   上传 400「缺少文件（multipart 字段名 file）」的根因，别再把显式头删掉；
 * - 响应缺 doc / 缺 markdown 时显式抛错，不许编一个空壳让界面假成功；
 * - 常量与后端同字面量（8000 字方向闸 / 扩展名白名单），改一处必须改两处。
 */
import axios, { type AxiosResponse } from 'axios';
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { apiGetMock, apiPostMock, apiDeleteMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  apiDeleteMock: vi.fn(),
}));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: apiGetMock, post: apiPostMock, delete: apiDeleteMock },
}));

import {
  DOC_MAX_DIRECTION_CHARS,
  DOC_MAX_FILES,
  DOC_MAX_TOTAL_UPLOAD_BYTES,
  DOC_MAX_UPLOAD_BYTES,
  DOC_UNSUPPORTED_IMAGE_EXTS,
  DOC_UPLOAD_ACCEPT,
  deleteDoc,
  extractDetail,
  getDoc,
  getDocDetail,
  getDocFileText,
  organizeDoc,
  uploadDoc,
  uploadDocs,
} from '../docMiningApi';

beforeEach(() => {
  apiGetMock.mockReset();
  apiPostMock.mockReset();
  apiDeleteMock.mockReset();
});

const DOC = {
  doc_id: 'd-1',
  filename: 'paper.pdf',
  ext: '.pdf',
  size_bytes: 100,
  parse_state: null,
  page_count: 12,
  status: 'parsed',
  organize_kind: null,
  organize_prompt_version: null,
  organized_at: null,
  task_id: null,
  error: null,
  created_at: '2026-10-09T00:00:00Z',
  updated_at: '2026-10-09T00:00:00Z',
};

describe('常量：与后端同字面量', () => {
  test('方向上限 8000 字、单件 200MB、合计 200MB（链同口径）、件数 20', () => {
    expect(DOC_MAX_DIRECTION_CHARS).toBe(8000);
    expect(DOC_MAX_UPLOAD_BYTES).toBe(200 * 1024 * 1024);
    expect(DOC_MAX_TOTAL_UPLOAD_BYTES).toBe(200 * 1024 * 1024);
    expect(DOC_MAX_FILES).toBe(20);
  });

  test('扩展名白名单覆盖后端 ACCEPTED_EXTENSIONS', () => {
    for (const ext of [
      '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif',
      '.docx', '.pptx', '.doc', '.ppt',
    ]) {
      expect(DOC_UPLOAD_ACCEPT.split(',')).toContain(ext);
    }
  });

  test('HEIC 不在接受词表（后端无解码器），走单独引导常量', () => {
    for (const ext of DOC_UNSUPPORTED_IMAGE_EXTS) {
      expect(DOC_UPLOAD_ACCEPT.split(',')).not.toContain(ext);
    }
    expect(DOC_UNSUPPORTED_IMAGE_EXTS).toContain('.heic');
    expect(DOC_UNSUPPORTED_IMAGE_EXTS).toContain('.heif');
  });
});

describe('extractDetail：后端拒绝理由原样带给用户', () => {
  test('字符串 detail 取 trim 后的原文', () => {
    expect(extractDetail({ response: { data: { detail: '  文档尚未解析完成  ' } } })).toBe(
      '文档尚未解析完成',
    );
  });

  test('detail 缺失或非字符串 → Error.message 兜底', () => {
    expect(extractDetail(new Error('Network Error'))).toBe('Network Error');
    expect(extractDetail({ response: { data: { detail: { nested: true } } } })).toBe(
      '请求失败',
    );
    expect(extractDetail(undefined)).toBe('请求失败');
  });
});

describe('uploadDoc', () => {
  test('multipart FormData 打到 /docs/upload；进度回调按整数百分比', async () => {
    apiPostMock.mockResolvedValue({
      data: { data: { doc: DOC, reused: true } },
    });
    const progress: number[] = [];

    const out = await uploadDoc(new File(['abc'], 'paper.pdf'), (p) =>
      progress.push(p),
    );

    const [url, form, opts] = apiPostMock.mock.calls[0];
    expect(url).toBe('/alpha-agent/docs/upload');
    expect(form).toBeInstanceOf(FormData);
    expect((form as FormData).get('file')).toBeInstanceOf(File);
    // 显式 multipart 头是刚需（实例默认 application/json 会让 FormData 被
    // JSON.stringify——见文件头注释），谁删谁把上传打回 400
    expect(opts.headers['Content-Type']).toBe('multipart/form-data');
    expect(out.doc.doc_id).toBe('d-1');
    expect(out.reused).toBe(true);

    // 模拟 axios 进度事件（total 缺失时不回调——算不出百分比就不猜）
    opts.onUploadProgress({ loaded: 1, total: 4 });
    opts.onUploadProgress({ loaded: 3, total: undefined });
    expect(progress).toEqual([25]);
  });

  test('响应缺 doc → 抛错（不许编空壳让界面假成功）', async () => {
    apiPostMock.mockResolvedValue({ data: { data: {} } });
    await expect(uploadDoc(new File(['a'], 'a.pdf'))).rejects.toThrow(
      '上传响应缺少文档信息',
    );
  });

  test('uploadDocs：同字段 file 重复提交，数组顺序=合并顺序', async () => {
    apiPostMock.mockResolvedValue({
      data: { data: { doc: { ...DOC, files_count: 2 }, reused: false } },
    });

    const out = await uploadDocs([
      new File(['a'], '正文.pdf'),
      new File(['b'], '附录.pdf'),
    ]);

    const [url, form] = apiPostMock.mock.calls[0];
    expect(url).toBe('/alpha-agent/docs/upload');
    const sent = (form as FormData).getAll('file');
    expect(sent.map((f) => (f as File).name)).toEqual(['正文.pdf', '附录.pdf']);
    expect(out.doc.files_count).toBe(2);
  });
});

describe('axios 契约（实证）：FORMData 与 JSON 默认头的相互陷害', () => {
  /** 复刻 apiClient 的实例默认值（services/aiStrategyClients.ts）。 */
  const makeInstance = () => {
    const inst = axios.create({ headers: { 'Content-Type': 'application/json' } });
    const seen: { data?: unknown } = {};
    inst.defaults.adapter = async (config) => {
      seen.data = config.data;
      return {
        data: { ok: true },
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      } as AxiosResponse;
    };
    return { inst, seen };
  };
  const makeForm = () => {
    const f = new FormData();
    f.append('file', new File(['abc'], 'paper.pdf'));
    return f;
  };

  test('旧写法（不声明 Content-Type）：FormData 被 JSON.stringify 成字符串体', async () => {
    const { inst, seen } = makeInstance();
    await inst.post('/u', makeForm());
    // 陷阱实证：到达适配器的是 JSON 字符串而不是 FormData——multipart 解析
    // 出空表单，后端只能回 400「缺少文件（multipart 字段名 file）」。
    // 若某天 axios 改了 transformRequest 使此断言失败：说明陷阱消失，
    // 可移除 uploadDocs 的显式头与本用例（届时头注释一并更新）。
    expect(typeof seen.data).toBe('string');
  });

  test('修复写法（显式 multipart/form-data）：FormData 原样到达适配器', async () => {
    const { inst, seen } = makeInstance();
    await inst.post('/u', makeForm(), {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    expect(seen.data).toBeInstanceOf(FormData);
  });
});

describe('getDocDetail：文档 + 关联挖掘任务（一文档多方向）', () => {
  test('缺 doc → 抛「文档不存在或已删除」；tasks 非数组 → undefined（未知 ≠ 空）', async () => {
    apiGetMock.mockResolvedValue({ data: { data: {} } });
    await expect(getDocDetail('d-1')).rejects.toThrow('文档不存在或已删除');

    apiGetMock.mockResolvedValue({ data: { data: { doc: DOC } } });
    const out = await getDocDetail('d-1');
    expect(out.doc.doc_id).toBe('d-1');
    expect(out.tasks).toBeUndefined();
  });

  test('带 tasks 时原样透出（顺序/字段由后端保证）；getDoc 委托取 doc', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          doc: { ...DOC, task_count: 2 },
          tasks: [
            {
              task_id: 't-2',
              status: 'running',
              direction: '方向二',
              created_at: '2026-10-09T01:00:00Z',
            },
            {
              task_id: 't-1',
              status: 'completed',
              direction: '方向一',
              created_at: '2026-10-09T00:00:00Z',
            },
          ],
        },
      },
    });

    const out = await getDocDetail('d-1');
    expect(out.doc.task_count).toBe(2);
    expect(out.tasks?.map((t) => t.task_id)).toEqual(['t-2', 't-1']);

    const doc = await getDoc('d-1');
    expect(doc.doc_id).toBe('d-1');
  });
});

describe('getDoc / getDocFileText / organizeDoc / deleteDoc', () => {
  test('getDoc 缺 doc → 抛「文档不存在或已删除」', async () => {
    apiGetMock.mockResolvedValue({ data: { data: {} } });
    await expect(getDoc('d-1')).rejects.toThrow('文档不存在或已删除');
  });

  test('getDocFileText 默认 full.md，responseType=text 且不做 JSON 反序列化', async () => {
    apiGetMock.mockResolvedValue({ data: '# 标题\n正文' });

    const text = await getDocFileText('d-1');

    const [url, opts] = apiGetMock.mock.calls[0];
    expect(url).toBe('/alpha-agent/docs/d-1/file');
    expect(opts.params).toEqual({ path: 'full.md' });
    expect(opts.responseType).toBe('text');
    expect(opts.transformResponse).toHaveLength(1);
    expect(text).toBe('# 标题\n正文');
  });

  test('organizeDoc：kind 必带、extra 空串省略（不把空串当补充要求下发）', async () => {
    apiPostMock.mockResolvedValue({
      data: {
        data: { kind: 'paper', prompt_version: 'v1', markdown: '# 草稿', doc: DOC },
      },
    });

    const out = await organizeDoc('d-1', { kind: 'paper', extra: '' });

    expect(apiPostMock.mock.calls[0][0]).toBe('/alpha-agent/docs/d-1/organize');
    expect(apiPostMock.mock.calls[0][1]).toEqual({ kind: 'paper', extra: undefined });
    expect(out.markdown).toBe('# 草稿');
  });

  test('organizeDoc 缺 markdown → 抛「整理响应缺少结果」', async () => {
    apiPostMock.mockResolvedValue({ data: { data: { kind: 'free' } } });
    await expect(organizeDoc('d-1', { kind: 'free' })).rejects.toThrow(
      '整理响应缺少结果',
    );
  });

  test('deleteDoc 打 DELETE /docs/{id}', async () => {
    apiDeleteMock.mockResolvedValue({ data: { data: {} } });
    await deleteDoc('d-9');
    expect(apiDeleteMock).toHaveBeenCalledWith('/alpha-agent/docs/d-9');
  });
});
