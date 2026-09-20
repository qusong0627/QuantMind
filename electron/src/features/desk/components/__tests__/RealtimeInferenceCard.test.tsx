/**
 * 交易台「实时推理」卡。
 *
 * 用户原话：「实时推理 · T-P6-08  后面的T后面不显示啊、这个内容和功能有些简陋啊
 * 不像是专业的实时推理」——两句都要在测试里钉住：
 * ① 卡头**不得出现内部工单号**（T-… 对使用者是噪音，长标题在小卡里还会被截断）；
 * ② 运行面（心跳/时延/治理/台账/可复现线索）必须在卡上看得见。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import type { Mock } from 'vitest';

vi.mock('../../services/realtimeInferService', () => ({
  getInferConfig: vi.fn(),
  listInferModels: vi.fn(() => Promise.resolve([])),
  setInferConfig: vi.fn(),
  exportOnnx: vi.fn(),
}));

import { getInferConfig } from '../../services/realtimeInferService';
import { RealtimeInferenceCard } from '../RealtimeInferenceCard';

const mockConfig = getInferConfig as unknown as Mock;

/** 一份「运行中但已在重度降载」的真实形态镜像 */
function runningPayload() {
  return {
    config: {
      enabled: 'true',
      model_dir: '/app/models/users/1/CN/mdl_cn_train_x',
      cadence_s: '15',
      min_live_coverage: '0.5',
    },
    status: {
      updated_at: new Date().toISOString(),
      counters: {
        cycles: 143,
        published: 19,
        scores: 10032,
        skipped_no_live: 124,
        last_live_coverage: 0.62,
        last_ms: 486.6,
        ledger_entries: 19,
        ledger_errors: 2,
        last_run_id: 'rt-mdl_cn-20260920',
      },
      governor: { level: 2, effective_cadence_s: 30, p95_ms: 26000 },
    },
    model_display_name: '远程测试9',
    baseline_source: 'quantdb_factors 直读（与批量推理同一取数面）',
  };
}

describe('RealtimeInferenceCard', () => {
  beforeEach(() => {
    mockConfig.mockReset();
  });

  it('卡头与正文都不出现内部工单号（T-…），标题就是「实时推理」', async () => {
    mockConfig.mockResolvedValue(runningPayload());
    const { container } = render(<RealtimeInferenceCard />);

    await waitFor(() => expect(screen.getByText('实时推理')).toBeTruthy());
    expect(screen.getByTestId('realtime-infer-card')).toBeTruthy();
    expect(container.textContent || '').not.toMatch(/T-P\d/);
  });

  it('运行面齐全：心跳 / 生效节拍 / 治理档位 / 时延 / 台账 / run_id / 基线取数面', async () => {
    mockConfig.mockResolvedValue(runningPayload());
    const { container } = render(<RealtimeInferenceCard />);
    await waitFor(() => expect(screen.getByText('运行面')).toBeTruthy());
    const text = container.textContent || '';

    expect(text).toContain('心跳');           // 心跳一行
    expect(text).toContain('节拍 30s');       // 降载后的**生效**节拍（不是配置里的 15s）
    expect(text).toContain('重度降载');        // 治理档位如实报
    expect(text).toContain('487 ms');         // 本周期时延（counters.last_ms）
    expect(text).toContain('26.0 s');         // p95
    expect(text).toContain('逼近节拍上限');     // 26s > 30s×80% → 串轮预警
    expect(text).toContain('19 条 · 2 错');    // 台账写错数必须可见
    expect(text).toContain('rt-mdl_cn-20260920');
    expect(text).toContain('quantdb_factors');
    expect(text).toContain('19/143 周期');    // 发布率（不是只摆累计数）
  });

  it('停用态不报心跳警（关了就是关了，别扮成故障）', async () => {
    const payload = runningPayload();
    payload.config.enabled = 'false';
    // 停用久了镜像必然陈旧——此时不该让卡头显示「心跳停更」
    payload.status.updated_at = new Date(Date.now() - 3600_000).toISOString();
    mockConfig.mockResolvedValue(payload);

    render(<RealtimeInferenceCard />);
    await waitFor(() => expect(screen.getByText('已停用')).toBeTruthy());
    expect(screen.queryByText('心跳停更')).toBeNull();
  });

  it('启用但镜像停更 → 卡头报「心跳停更」且说明可能原因（不写死 5 分钟）', async () => {
    const payload = runningPayload();
    payload.status.updated_at = new Date(Date.now() - 90_000).toISOString();
    mockConfig.mockResolvedValue(payload);

    const { container } = render(<RealtimeInferenceCard />);
    await waitFor(() => expect(screen.getByText('心跳停更')).toBeTruthy());
    expect(container.textContent || '').toMatch(/循环可能已停/);
  });

  it('p95 无样本时如实说「样本不足」，不写 0', async () => {
    const payload = runningPayload();
    payload.status.governor = { level: 0, effective_cadence_s: 15, p95_ms: null } as never;
    mockConfig.mockResolvedValue(payload);

    const { container } = render(<RealtimeInferenceCard />);
    await waitFor(() => expect(screen.getByText('运行面')).toBeTruthy());
    expect(container.textContent || '').toContain('样本不足');
    expect(container.textContent || '').not.toContain('逼近节拍上限');
  });

  it('非管理员：诚实降级，且同样不暴露工单号', async () => {
    mockConfig.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }));
    const { container } = render(<RealtimeInferenceCard />);

    await waitFor(() => expect(screen.getByText(/需管理员权限/)).toBeTruthy());
    expect(container.textContent || '').not.toMatch(/T-P\d/);
  });
});
