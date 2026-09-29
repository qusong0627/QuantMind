/**
 * RD 挖掘因子物化面板 —— 完成判定与清单词表的契约测试。
 *
 * 这里锁的是两个已经踩过的真坑：
 * 1) 「假完成」：子进程冷启 ~4s 才拿锁，POST 回包后第一次探测恒为 running=false。
 *    只要前端自己造一个「刚才在跑」的记忆，这次 false 就会被当成「运行→结束」
 *    的边——刚点开始就弹「已结束」，且轮询定时器（依赖 running 真值为真）再也
 *    装不上，整轮物化从面板上消失。所以：宽限期内的 false 不许完成、不许提示，
 *    完成必须等到「服务端确认过 running=true」之后的第一次 false。
 * 2) 「失败磁贴恒为 0」：物化清单只写 materialized/rejected_duplicate/error
 *    三个状态，页面若读 `failed` 键，红磁贴结构上永远红不起来（后端夹具曾与
 *    前端一起错到同一个不存在的键上）。
 *
 * 轮询不用 fake timers：直接捕获 window.setInterval 注册的回调手动触发，
 * 这样「定时器到底有没有装上」本身就是可断言的（坑 1 的原始症状），
 * 也不必和 antd/React 的计时器行为纠缠。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { message, Modal } from 'antd';

import { RdMinedMaterializePanel } from '../RdMinedMaterializePanel';
import { adminService } from '../../services/adminService';
import type { RdMinedMaterializeStatus } from '../../types';

vi.mock('../../services/adminService', () => ({
  adminService: {
    getRdMinedMaterializeStatus: vi.fn(),
    startRdMinedMaterialize: vi.fn(),
  },
}));

const statusMock = vi.mocked(adminService.getRdMinedMaterializeStatus);
const startMock = vi.mocked(adminService.startRdMinedMaterialize);

/** 生产形状的清单统计：只可能出现这三个状态键。 */
const mkStatus = (over: {
  running: boolean;
  pending?: number;
  byStatus?: Record<string, number>;
  upToDate?: boolean;
}): RdMinedMaterializeStatus => ({
  running: over.running,
  overview: {
    candidates: {
      total: 12,
      pending: over.pending ?? 0,
      pending_reasons: (over.pending ?? 0) > 0 ? { new: over.pending ?? 0 } : {},
      skipped: { already_materialized: 8 },
    },
    manifest: {
      total: 10,
      by_status: over.byStatus ?? { materialized: 8, rejected_duplicate: 1, error: 1 },
      last_at: '2026-09-29T08:22:00Z',
    },
    library: { ready: true, factor_columns: 33, partitions: 1500, min_date: '2018-01-02', max_date: '2026-09-25' },
    catalog: { published_version: 'v3', published_columns: 33, up_to_date: over.upToDate ?? true },
  },
  log: { path: '/data/logs/rd_mined_materialize_web.log', exists: true, size: 4096, lines: ['[08:22] 物化开始'] },
});

/** 手动触发所有已注册的 setInterval 回调一轮（即「过了一个轮询周期」）。 */
const pollOnce = async (cbs: Array<() => void>) => {
  await act(async () => {
    cbs.forEach((cb) => cb());
  });
};

const flushMount = async () => {
  await act(async () => {});
};

/** 读某个 Statistic 磁贴的显示值（antd 结构：.ant-statistic > title + content-value）。 */
const statValue = (title: string): string => {
  const stat = screen.getByText(title).closest('.ant-statistic');
  expect(stat, `未找到「${title}」磁贴`).not.toBeNull();
  return stat!.querySelector('.ant-statistic-content-value')?.textContent || '';
};

let intervals: Array<() => void>;

beforeEach(() => {
  vi.clearAllMocks();
  intervals = [];
  // 捕获轮询定时器回调：既能手动推进，又能断言「定时器装没装上」
  vi.spyOn(window, 'setInterval').mockImplementation(((cb: () => void) => {
    intervals.push(cb);
    return intervals.length;
  }) as typeof window.setInterval);
  vi.spyOn(window, 'clearInterval').mockImplementation(() => undefined);
  vi.spyOn(message, 'success').mockImplementation((() => undefined) as never);
  vi.spyOn(message, 'warning').mockImplementation((() => undefined) as never);
  vi.spyOn(message, 'info').mockImplementation((() => undefined) as never);
  vi.spyOn(message, 'error').mockImplementation((() => undefined) as never);
});

afterEach(() => {
  vi.restoreAllMocks();
});

const clickStartAndConfirm = async () => {
  let captured: { onOk?: () => Promise<void> | void } | null = null;
  vi.spyOn(Modal, 'confirm').mockImplementation(((cfg: { onOk?: () => Promise<void> | void }) => {
    captured = cfg;
    return { destroy: vi.fn(), update: vi.fn() } as never;
  }) as never);

  await act(async () => {
    screen.getByRole('button', { name: /开始物化/ }).click();
  });
  expect(captured, 'Modal.confirm 未被调用').not.toBeNull();
  await act(async () => {
    await captured!.onOk?.();
  });
};

describe('完成判定：宽限期内的 running=false 不是「结束」', () => {
  it('起完立刻探测到 false 不许弹完成，轮询照装；真运行→真结束才提示一次', async () => {
    // Arrange：初始空闲、有待办
    const onCompleted = vi.fn();
    statusMock.mockResolvedValue(mkStatus({ running: false, pending: 3 }));
    render(<RdMinedMaterializePanel onCompleted={onCompleted} />);
    await flushMount();
    expect(screen.getByText('空闲')).toBeInTheDocument();

    // Act 1：启动。后端回包后前端会立刻探测一次——mock 仍返回 false
    //（旧实现正是在这里把 false 读成「刚跑完」的）。这是最坏的一拍。
    startMock.mockResolvedValue({
      started: true, pid: 4242, log_path: '/data/logs/rd_mined_materialize_web.log',
      message: '物化已确认在后台运行；完成后会自动刷新字段注册并发布训练目录',
    });
    await clickStartAndConfirm();

    // Assert 1：没有任何完成口径的提示，onCompleted 未触发，且按钮被宽限锁住
    const allMessages = [
      ...vi.mocked(message.success).mock.calls.flat(),
      ...vi.mocked(message.warning).mock.calls.flat(),
      ...vi.mocked(message.info).mock.calls.flat(),
    ].map((arg) => String(arg));
    expect(allMessages.some((text) => text.includes('已结束') || text.includes('物化完成'))).toBe(false);
    expect(onCompleted).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /开始物化/ })).toBeDisabled();
    // 轮询必须已装（旧实现此时定时器根本不存在，整轮运行从面板消失）
    expect(intervals.length).toBeGreaterThan(0);

    // Act 2：下一拍服务端确认 running=true——这才是「确认过运行」
    statusMock.mockResolvedValue(mkStatus({ running: true, pending: 3 }));
    await pollOnce(intervals);

    // Assert 2：运行态可见，按钮保持禁用
    expect(screen.getByText('物化运行中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /开始物化/ })).toBeDisabled();
    expect(onCompleted).not.toHaveBeenCalled();

    // Act 3：再下一拍 running=false 且待办清零、目录已最新——真正的结束边
    statusMock.mockResolvedValue(mkStatus({ running: false, pending: 0, upToDate: true }));
    await pollOnce(intervals);

    // Assert 3：恰好一次完成提示 + 一次宿主回调，回到空闲
    expect(vi.mocked(message.success).mock.calls.some(
      (call) => String(call[0]).includes('物化完成'),
    )).toBe(true);
    expect(onCompleted).toHaveBeenCalledTimes(1);
    expect(screen.getByText('空闲')).toBeInTheDocument();
  });

  it('结束但仍有待办：提示按「未完成」口径，不报成功', async () => {
    // Arrange：先处于确认过的运行态
    const onCompleted = vi.fn();
    statusMock.mockResolvedValue(mkStatus({ running: true, pending: 2 }));
    render(<RdMinedMaterializePanel onCompleted={onCompleted} />);
    await flushMount();

    // Act：转为结束且有剩余待办
    statusMock.mockResolvedValue(mkStatus({ running: false, pending: 2 }));
    await pollOnce(intervals);

    // Assert：完成回调仍触发（宿主可刷新），但文案是剩余待办而非「完成」
    expect(onCompleted).toHaveBeenCalledTimes(1);
    const warnings = vi.mocked(message.warning).mock.calls.map((call) => String(call[0]));
    expect(warnings.some((text) => text.includes('仍有 2 个因子待物化'))).toBe(true);
    expect(vi.mocked(message.success).mock.calls.some(
      (call) => String(call[0]).includes('物化完成'),
    )).toBe(false);
  });
});

describe('清单词表：失败磁贴读 error，不读不存在的 failed', () => {
  it('生产形状 {materialized, rejected_duplicate, error} 如实显示', async () => {
    // Arrange & Act
    statusMock.mockResolvedValue(mkStatus({
      running: false,
      byStatus: { materialized: 5, rejected_duplicate: 1, error: 2 },
    }));
    render(<RdMinedMaterializePanel />);
    await flushMount();

    // Assert
    expect(statValue('失败')).toBe('2');
    expect(statValue('已物化')).toBe('5');
    expect(statValue('值级重复被拒')).toBe('1');
  });

  it('清单混入非词表键（failed）时仍以 error 为准', async () => {
    // Arrange & Act：failed 是历史夹具的错键，生产从不写；它不许顶替 error
    statusMock.mockResolvedValue(mkStatus({
      running: false,
      byStatus: { materialized: 5, error: 2, failed: 7 },
    }));
    render(<RdMinedMaterializePanel />);
    await flushMount();

    // Assert
    expect(statValue('失败')).toBe('2');
  });
});

describe('轮询失败的可见性', () => {
  it('静默轮询连续失败只告警一次，恢复后再次失败重新告警', async () => {
    // Arrange：运行中，轮询已装
    statusMock.mockResolvedValue(mkStatus({ running: true, pending: 1 }));
    render(<RdMinedMaterializePanel />);
    await flushMount();
    expect(intervals.length).toBeGreaterThan(0);

    // Act & Assert 1：第一次失败可见
    statusMock.mockRejectedValue(new Error('boom'));
    await pollOnce(intervals);
    const warnCount = () => vi.mocked(message.warning).mock.calls
      .filter((call) => String(call[0]).includes('刷新失败')).length;
    expect(warnCount()).toBe(1);

    // Act & Assert 2：连续失败不刷屏
    await pollOnce(intervals);
    expect(warnCount()).toBe(1);

    // Act 3：恢复成功后再失败——新的失败段重新可见
    statusMock.mockResolvedValue(mkStatus({ running: true, pending: 1 }));
    await pollOnce(intervals);
    statusMock.mockRejectedValue(new Error('boom'));
    await pollOnce(intervals);
    expect(warnCount()).toBe(2);
  });
});
