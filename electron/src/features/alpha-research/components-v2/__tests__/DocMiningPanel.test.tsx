/**
 * 文档挖掘面板 —— 四步 Stepper 的状态机与三条纪律：
 *
 * - **状态只信后端**：解析进度由轮询驱动，失败与超限都显示后端 error 原文；
 * - **人工确认是默认**：整理完落进可编辑草稿，用户点「开始挖掘」才提交；
 *   「整理后直通」打开才跳过确认步；
 * - **提交携带血统**：onStartMining 必须带 {userInput: 草稿, docId}，
 *   docId 丢了历史页就再也连不回文档。
 *
 * 时间用 fake timers：轮询间隔 3s 的推进必须是显式的，测试不许等真实秒数。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { DocMiningPanel, DOC_POLL_INTERVAL_MS } from '../DocMiningPanel';
import type { DocRow } from '../../services-v2/docMiningApi';

const { uploadDocsMock, getDocMock, getDocQuotaMock, organizeDocMock, getDocFileTextMock } =
  vi.hoisted(() => ({
    uploadDocsMock: vi.fn(),
    getDocMock: vi.fn(),
    getDocQuotaMock: vi.fn(),
    organizeDocMock: vi.fn(),
    getDocFileTextMock: vi.fn(),
  }));

vi.mock('../../services-v2/docMiningApi', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../../services-v2/docMiningApi')>();
  return {
    ...actual,
    uploadDocs: uploadDocsMock,
    getDoc: getDocMock,
    getDocQuota: getDocQuotaMock,
    organizeDoc: organizeDocMock,
    getDocFileText: getDocFileTextMock,
  };
});

function mkDoc(over: Partial<DocRow> = {}): DocRow {
  return {
    doc_id: 'd-1',
    filename: 'paper.pdf',
    ext: '.pdf',
    size_bytes: 1000,
    parse_state: null,
    page_count: null,
    status: 'parsing',
    organize_kind: null,
    organize_prompt_version: null,
    organized_at: null,
    task_id: null,
    error: null,
    created_at: '2026-10-09T00:00:00Z',
    updated_at: '2026-10-09T00:00:00Z',
    ...over,
  };
}

const QUOTA = {
  day: '20261009',
  user_id: 'u-1',
  user_used: 10,
  user_limit: 200,
  platform_used: 100,
  platform_budget: 10000,
  user_remaining: 190,
  platform_remaining: 9900,
  exhausted: false,
  warning: false,
  token_configured: true,
};

/** 冲掉一次事件触发的 promise 链（每次 setState 一跳，多给几跳） */
async function flush() {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function mkFile(name: string, size = 1000): File {
  const f = new File(['x'.repeat(Math.min(size, 64))], name);
  if (size > 64) Object.defineProperty(f, 'size', { value: size });
  return f;
}

function renderPanel(props: {
  onStartMining?: ReturnType<typeof vi.fn>;
  isRunning?: boolean;
  resume?: { key: number; docId: string; filename: string } | null;
} = {}) {
  const onStartMining = props.onStartMining ?? vi.fn();
  const utils = render(
    <DocMiningPanel
      onStartMining={onStartMining}
      isRunning={props.isRunning ?? false}
      resume={props.resume}
    />,
  );
  const input = utils.container.querySelector('#doc-mining-file') as HTMLInputElement;
  return { onStartMining, input, ...utils };
}

/** upload(parsing) → tick(parsed) 的标准铺垫：返回后即处于整理步 */
function primeUploadThenParsed(parsedOver: Partial<DocRow> = {}) {
  uploadDocsMock.mockResolvedValue({ doc: mkDoc({ status: 'parsing' }), reused: false });
  getDocMock.mockResolvedValue(mkDoc({ status: 'parsed', page_count: 12, ...parsedOver }));
}

beforeEach(() => {
  vi.useFakeTimers();
  uploadDocsMock.mockReset();
  getDocMock.mockReset();
  getDocQuotaMock.mockReset();
  organizeDocMock.mockReset();
  getDocFileTextMock.mockReset();
  getDocQuotaMock.mockResolvedValue(QUOTA);
  getDocFileTextMock.mockResolvedValue('原文内容');
});

afterEach(() => {
  vi.useRealTimers();
});

describe('上传闸：白名单与大小在前端先拦（省一次白传）', () => {
  test('不支持的扩展名显式报错，不发请求', async () => {
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('report.txt')] } });
    await flush();

    expect(screen.getByText(/不支持的文件类型 .txt/)).toBeTruthy();
    expect(uploadDocsMock).not.toHaveBeenCalled();
  });

  test('超过 200MB 显式报错，不发请求', async () => {
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('big.pdf', 201 * 1024 * 1024)] } });
    await flush();

    expect(screen.getByText(/文件超过 200MB 上限/)).toBeTruthy();
    expect(uploadDocsMock).not.toHaveBeenCalled();
  });

  test('后端拒绝（如额度用尽）时显示 detail 原文', async () => {
    uploadDocsMock.mockRejectedValue({
      response: { data: { detail: '今日上传额度已用尽' } },
    });
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();

    expect(screen.getByText('今日上传额度已用尽')).toBeTruthy();
  });
});

describe('多文件：待上传清单（顺序=合并顺序）', () => {
  test('多选两件先进清单（先攒后传）；上传按清单顺序一次提交', async () => {
    uploadDocsMock.mockResolvedValue({ doc: mkDoc({ status: 'parsing' }), reused: false });
    getDocMock.mockResolvedValue(mkDoc({ status: 'parsed', page_count: 12 }));
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, {
      target: { files: [mkFile('正文.pdf'), mkFile('附录.pdf')] },
    });
    await flush();

    expect(uploadDocsMock).not.toHaveBeenCalled(); // 选完不立刻上传
    expect(screen.getByText(/待上传 2 个文件/)).toBeTruthy();
    expect(screen.getByText('正文.pdf')).toBeTruthy();
    expect(screen.getByText('附录.pdf')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();

    const files = uploadDocsMock.mock.calls[0][0] as File[];
    expect(files.map((f) => f.name)).toEqual(['正文.pdf', '附录.pdf']);
  });

  test('上移改变合并顺序（选反了就换回来）', async () => {
    uploadDocsMock.mockResolvedValue({ doc: mkDoc({ status: 'parsing' }), reused: false });
    getDocMock.mockResolvedValue(mkDoc({ status: 'parsed' }));
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, {
      target: { files: [mkFile('附录.pdf'), mkFile('正文.pdf')] },
    });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上移 正文.pdf/ }));
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();

    const files = uploadDocsMock.mock.calls[0][0] as File[];
    expect(files.map((f) => f.name)).toEqual(['正文.pdf', '附录.pdf']);
  });

  test('移除与清空：清单收缩，空清单不再显示上传按钮', async () => {
    const { input } = renderPanel();
    await flush();
    fireEvent.change(input, {
      target: { files: [mkFile('a.pdf'), mkFile('b.pdf')] },
    });
    await flush();

    fireEvent.click(screen.getByRole('button', { name: /移除 a.pdf/ }));
    expect(screen.getByText(/待上传 1 个文件/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '清空' }));
    expect(screen.queryByText(/待上传/)).toBeNull();
    expect(screen.queryByRole('button', { name: /上传并解析/ })).toBeNull();
  });

  test('合计超上限：显式报错、不入清单（不发请求）', async () => {
    const { input } = renderPanel();
    await flush();

    // 每件都在单件 200MB 内，两件合计 300MB 超 200MB 合计上限
    const heavy = 150 * 1024 * 1024;
    fireEvent.change(input, {
      target: { files: [mkFile('a.pdf', heavy), mkFile('b.pdf', heavy)] },
    });
    await flush();

    expect(screen.getByText(/文件合计超过 200MB 上限/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /上传并解析/ })).toBeNull();
    expect(uploadDocsMock).not.toHaveBeenCalled();
  });

  test('超过 20 件：显式报错、不入清单', async () => {
    const { input } = renderPanel();
    await flush();
    const many = Array.from({ length: 21 }, (_, i) => mkFile(`p${i}.pdf`));

    fireEvent.change(input, { target: { files: many } });
    await flush();

    expect(screen.getByText(/单次最多上传 20 个文件/)).toBeTruthy();
    expect(uploadDocsMock).not.toHaveBeenCalled();
  });
});

describe('解析轮询：状态只信后端', () => {
  test('上传后进入解析步；轮询到 parsed 自动进整理步并拉原文', async () => {
    primeUploadThenParsed();
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();

    // 已是整理步：能看到原文入口与整理按钮
    expect(screen.getByRole('button', { name: /查看原文（4 字）/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /开始整理/ })).toBeTruthy();
    expect(getDocMock).toHaveBeenCalledWith('d-1');

    fireEvent.click(screen.getByRole('button', { name: /查看原文/ }));
    expect(screen.getByText('原文内容')).toBeTruthy();
  });

  test('复用命中的上传直接进整理步，不经过解析轮询', async () => {
    uploadDocsMock.mockResolvedValue({
      doc: mkDoc({ status: 'parsed', page_count: 3 }),
      reused: true,
    });
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();

    expect(screen.getByText(/该文件此前已解析过/)).toBeTruthy();
    expect(getDocMock).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /开始整理/ })).toBeTruthy();
  });

  test('解析失败：显示后端原因 + 重新上传可回到上传步', async () => {
    uploadDocsMock.mockResolvedValue({ doc: mkDoc({ status: 'parsing' }), reused: false });
    getDocMock.mockResolvedValue(
      mkDoc({ status: 'parse_failed', error: '文档页数超限（800 页）' }),
    );
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();

    expect(screen.getByText('文档页数超限（800 页）')).toBeTruthy();
    fireEvent.click(screen.getByText('重新上传'));
    expect(screen.getByText('点击选择文件，或拖入此区域')).toBeTruthy();
  });

  test('轮询瞬态失败挂提示不换步；下一拍恢复即照常推进', async () => {
    uploadDocsMock.mockResolvedValue({ doc: mkDoc({ status: 'parsing' }), reused: false });
    getDocMock
      .mockRejectedValueOnce(new TypeError('fetch failed'))
      .mockResolvedValue(mkDoc({ status: 'parsed' }));
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();

    expect(screen.getByText(/状态查询失败（自动重试中）：fetch failed/)).toBeTruthy();

    await advance(DOC_POLL_INTERVAL_MS);
    expect(screen.getByRole('button', { name: /开始整理/ })).toBeTruthy();
  });
});

describe('整理与确认：人工确认是默认', () => {
  test('整理成功落进可编辑草稿；「开始挖掘」携带 docId 血统', async () => {
    primeUploadThenParsed();
    organizeDocMock.mockResolvedValue({
      kind: 'free',
      prompt_version: 'v1',
      payload: {},
      markdown: '# 整理草稿',
      truncated: false,
      chunks_used: 1,
      doc: mkDoc({ status: 'organized' }),
    });
    const { input, onStartMining } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /开始整理/ }));
    await flush();

    expect(organizeDocMock).toHaveBeenCalledWith('d-1', {
      kind: 'free',
      extra: undefined,
    });
    const draft = screen.getByDisplayValue('# 整理草稿') as HTMLTextAreaElement;
    expect(draft.value).toBe('# 整理草稿');

    fireEvent.click(screen.getByRole('button', { name: /开始挖掘/ }));
    expect(onStartMining).toHaveBeenCalledWith({
      userInput: '# 整理草稿',
      docId: 'd-1',
    });
  });

  test('整理口径与补充关注点原样下发', async () => {
    primeUploadThenParsed();
    organizeDocMock.mockResolvedValue({
      kind: 'paper',
      prompt_version: 'v1',
      payload: {},
      markdown: 'x',
      truncated: false,
      chunks_used: 1,
      doc: mkDoc({ status: 'organized' }),
    });
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();
    fireEvent.click(screen.getByRole('radio', { name: /论文复现解读/ }));
    fireEvent.change(screen.getByPlaceholderText(/可选：补充关注点/), {
      target: { value: '只看动量类因子' },
    });
    fireEvent.click(screen.getByRole('button', { name: /开始整理/ }));
    await flush();

    expect(organizeDocMock).toHaveBeenCalledWith('d-1', {
      kind: 'paper',
      extra: '只看动量类因子',
    });
  });

  test('「整理后直通」跳过确认步直接开始挖掘', async () => {
    primeUploadThenParsed();
    organizeDocMock.mockResolvedValue({
      kind: 'free',
      prompt_version: 'v1',
      payload: {},
      markdown: '# 直通草稿',
      truncated: false,
      chunks_used: 1,
      doc: mkDoc({ status: 'organized' }),
    });
    const { input, onStartMining } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: /开始整理/ }));
    await flush();

    expect(onStartMining).toHaveBeenCalledWith({
      userInput: '# 直通草稿',
      docId: 'd-1',
    });
    expect(screen.queryByText('挖掘方向草稿（可直接编辑，将原样下发 RD-Agent）')).toBeNull();
  });

  test('长文采样整理挂提示（truncated）', async () => {
    primeUploadThenParsed();
    organizeDocMock.mockResolvedValue({
      kind: 'free',
      prompt_version: 'v1',
      payload: {},
      markdown: '# 采样草稿',
      truncated: true,
      chunks_used: 3,
      doc: mkDoc({ status: 'organized' }),
    });
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /开始整理/ }));
    await flush();

    expect(screen.getByText(/已按块采样整理/)).toBeTruthy();
  });

  test('草稿超过 8000 字：显式报错并锁提交', async () => {
    primeUploadThenParsed();
    organizeDocMock.mockResolvedValue({
      kind: 'free',
      prompt_version: 'v1',
      payload: {},
      markdown: 'draft',
      truncated: false,
      chunks_used: 1,
      doc: mkDoc({ status: 'organized' }),
    });
    const { input } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /开始整理/ }));
    await flush();

    const draft = screen.getByDisplayValue('draft') as HTMLTextAreaElement;
    fireEvent.change(draft, { target: { value: 'x'.repeat(8001) } });

    expect(screen.getByText(/超出 8000 字上限，请精简后再开始挖掘/)).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: /开始挖掘/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  test('直通模式下整理结果为空：报「方向草稿为空」而不是静默提交', async () => {
    primeUploadThenParsed();
    organizeDocMock.mockResolvedValue({
      kind: 'free',
      prompt_version: 'v1',
      payload: {},
      markdown: '',
      truncated: false,
      chunks_used: 0,
      doc: mkDoc({ status: 'organized' }),
    });
    const { input, onStartMining } = renderPanel();
    await flush();

    fireEvent.change(input, { target: { files: [mkFile('paper.pdf')] } });
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /上传并解析/ }));
    await flush();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: /开始整理/ }));
    await flush();

    expect(screen.getByText(/方向草稿为空/)).toBeTruthy();
    expect(onStartMining).not.toHaveBeenCalled();
  });
});

describe('「继续挖掘」带回：按 key 代次恢复', () => {
  test('已整理的文档：预填草稿、kind 回填，可进确认步编辑', async () => {
    getDocMock.mockResolvedValue(
      mkDoc({
        status: 'organized',
        organized_text: '先前整理的草稿',
        organize_kind: 'paper',
        page_count: 8,
      }),
    );
    const { rerender } = renderPanel({
      resume: { key: 1, docId: 'd-9', filename: 'old.pdf' },
    });
    await flush();

    expect(getDocMock).toHaveBeenCalledWith('d-9');
    expect(screen.getByText(/本文档已整理过/)).toBeTruthy();
    expect(
      (screen.getByRole('radio', { name: /论文复现解读/ }) as HTMLInputElement).checked,
    ).toBe(true);

    fireEvent.click(screen.getByText('继续'));
    const draft = screen.getByDisplayValue('先前整理的草稿') as HTMLTextAreaElement;
    expect(draft.value).toBe('先前整理的草稿');

    // 同一代次重渲染不重拉（key 是唯一代次信号）
    getDocMock.mockClear();
    rerender(
      <DocMiningPanel
        onStartMining={vi.fn()}
        isRunning={false}
        resume={{ key: 1, docId: 'd-9', filename: 'old.pdf' }}
      />,
    );
    await flush();
    expect(getDocMock).not.toHaveBeenCalled();
  });

  test('key 递增才重新应用（连点两次「继续挖掘」同一文档也要恢复）', async () => {
    getDocMock.mockResolvedValue(mkDoc({ status: 'parsed' }));
    const onStartMining = vi.fn();
    const { rerender } = render(
      <DocMiningPanel
        onStartMining={onStartMining}
        isRunning={false}
        resume={{ key: 1, docId: 'd-1', filename: 'a.pdf' }}
      />,
    );
    await flush();

    rerender(
      <DocMiningPanel
        onStartMining={onStartMining}
        isRunning={false}
        resume={{ key: 2, docId: 'd-2', filename: 'b.pdf' }}
      />,
    );
    await flush();

    expect(getDocMock).toHaveBeenCalledWith('d-1');
    expect(getDocMock).toHaveBeenCalledWith('d-2');
  });

  test('带回的文档解析失败：显示原因，可重新上传', async () => {
    getDocMock.mockResolvedValue(
      mkDoc({ status: 'parse_failed', error: 'MinerU 解析失败（页数超限）' }),
    );
    renderPanel({ resume: { key: 1, docId: 'd-3', filename: 'bad.pdf' } });
    await flush();

    expect(screen.getByText('MinerU 解析失败（页数超限）')).toBeTruthy();
    expect(screen.getByText('重新上传')).toBeTruthy();
  });

  test('带回的文档还在解析：进解析步继续轮询', async () => {
    getDocMock
      // 第 1 拍是 resume 自己的取数，第 2 拍是进解析步后的立即轮询；都还在解析中
      .mockResolvedValueOnce(mkDoc({ status: 'parsing' }))
      .mockResolvedValueOnce(mkDoc({ status: 'parsing' }))
      .mockResolvedValue(mkDoc({ status: 'parsed' }));
    renderPanel({ resume: { key: 1, docId: 'd-4', filename: 'wait.pdf' } });
    await flush();

    expect(screen.getByText(/MinerU 云端解析通常需要 10~60 秒/)).toBeTruthy();

    await advance(DOC_POLL_INTERVAL_MS);
    expect(screen.getByRole('button', { name: /开始整理/ })).toBeTruthy();
  });
});
