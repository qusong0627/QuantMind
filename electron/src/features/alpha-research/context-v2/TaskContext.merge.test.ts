import { describe, expect, it } from 'vitest';

import { mergeMetrics, mergePolledTask } from './TaskContext';
import type { RealtimeMetrics, Task } from '../types-v2';

function metrics(overrides: Partial<RealtimeMetrics> = {}): RealtimeMetrics {
  return {
    ic: null,
    icir: null,
    rankIc: null,
    rankIcir: null,
    annualReturn: null,
    sharpeRatio: null,
    maxDrawdown: null,
    totalFactors: 0,
    highQualityFactors: 0,
    mediumQualityFactors: 0,
    lowQualityFactors: 0,
    top10Factors: [],
    ...overrides,
  };
}

function task(overrides: Partial<Task> = {}): Task {
  return {
    taskId: 'task-a',
    status: 'running',
    config: { userInput: '' } as Task['config'],
    progress: {
      phase: 'evolving',
      currentRound: 0,
      totalRounds: 3,
      progress: 40,
      message: '运行中',
      timestamp: new Date().toISOString(),
    },
    metrics: metrics(),
    logs: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  };
}

function factor(index: number, rankIc: number | null) {
  return {
    factorId: `f-${index}`,
    factorName: `因子${index}`,
    factorExpression: 'close / open',
    rankIc,
    rankIcir: null,
    ic: null,
    icir: null,
    annualReturn: null,
    sharpeRatio: null,
    maxDrawdown: null,
  };
}

describe('mergePolledTask', () => {
  it('does not wipe accumulated factors when the poll returns empty metrics', () => {
    // 状态接口没有 metrics 时会给出空模板（normalizeAgentTask 的 emptyMetrics），
    // 每 10 秒一次的轮询不能把已产出的因子清空。
    const accumulated = metrics({
      totalFactors: 11,
      top10Factors: [factor(1, 0.035), factor(2, 0.021)],
      rankIc: 0.035,
      factorName: '因子1',
    });
    const prev = task({ metrics: accumulated });
    const merged = mergePolledTask(prev, task({ metrics: metrics() }));

    expect(merged.metrics?.totalFactors).toBe(11);
    expect(merged.metrics?.top10Factors).toHaveLength(2);
    expect(merged.metrics?.rankIc).toBe(0.035);
    expect(merged.metrics?.factorName).toBe('因子1');
  });

  it('adopts the authoritative factor list returned by the status endpoint', () => {
    const prev = task({
      metrics: metrics({
        totalFactors: 2,
        top10Factors: [factor(1, 0.01)],
      }),
    });
    const polled = task({
      metrics: metrics(),
      factors: [
        { ...factor(1, 0.02), factorDescription: '', quality: 'high', round: 1, direction: '', createdAt: '', archivedAt: null, hasCode: true, archiveReason: null },
        { ...factor(2, 0.05), factorDescription: '', quality: 'high', round: 1, direction: '', createdAt: '', archivedAt: null, hasCode: true, archiveReason: null },
      ],
    });

    const merged = mergePolledTask(prev, polled);

    expect(merged.metrics?.totalFactors).toBe(2);
    expect(merged.metrics?.top10Factors?.[0].factorName).toBe('因子2');
    expect(merged.metrics?.rankIc).toBe(0.05);
  });

  it('keeps at most ten factors and orders them by RankIC', () => {
    const prev = task({ metrics: metrics({ totalFactors: 12, top10Factors: [factor(1, 0.01)] }) });
    const polled = task({
      metrics: metrics(),
      factors: Array.from({ length: 12 }, (_, index) => ({
        ...factor(index, index === 3 ? 0.9 : index / 100),
        factorDescription: '', quality: 'medium', round: 1, direction: '',
        createdAt: '', archivedAt: null, hasCode: true, archiveReason: null,
      })),
    });

    const merged = mergePolledTask(prev, polled);

    expect(merged.metrics?.top10Factors).toHaveLength(10);
    expect(merged.metrics?.top10Factors?.[0].factorName).toBe('因子3');
    expect(merged.metrics?.totalFactors).toBe(12);
  });
});

describe('mergeMetrics', () => {
  it('never regresses the factor count and keeps non-null scalars', () => {
    const merged = mergeMetrics(
      metrics({ totalFactors: 7, rankIc: 0.04, icir: 0.2, top10Factors: [factor(1, 0.04)] }),
      metrics({ totalFactors: 0, rankIc: null, icir: 0.3, top10Factors: [] }),
    );

    expect(merged?.totalFactors).toBe(7);
    expect(merged?.rankIc).toBe(0.04);
    expect(merged?.icir).toBe(0.3);
    expect(merged?.top10Factors).toHaveLength(1);
  });
});
