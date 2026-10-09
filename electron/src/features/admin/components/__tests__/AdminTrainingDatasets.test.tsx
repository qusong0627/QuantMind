/**
 * 训练数据集页 —— 草稿发现契约（2026-10-07）。
 *
 * 这页此前**只认自己当场新建的草稿**：`load()` 里取草稿那一支挂着 `if (draft)`，
 * 而首屏 `draft` 状态恒为 null，那一支永远不进。别处写进来的草稿（因子研究页的
 * 「注册到训练目录」就是典型）在这页上因此完全不可见——而发布按钮只在这页，
 * 于是整条链死掉：注册进去了 → 看不见 → 发布不了 → 训练永远用不上。
 *
 * 锁三件事，每件都对应一个会静默把人领错的分支：
 * 1) 首屏要认领该来源库现有的草稿；
 * 2) 没有草稿时不许凭空认一个（要保持「未创建」，否则用户会以为有东西可发布）；
 * 3) 认领只发生在「手上没有草稿」时——正在编辑的那份不能被一个后来者顶掉，
 *    顶掉意味着用户在左侧目录做的分类/中文解释**静默失去落点**。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Modal } from 'antd';

import { AdminTrainingDatasets } from '../AdminTrainingDatasets';
import { adminService } from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  adminService: {
    getQuantDBFactorSources: vi.fn(),
    getQuantDBFactorFields: vi.fn(),
    getQuantDBFactorCatalog: vi.fn(),
    listQuantDBFactorVersions: vi.fn(),
    publishQuantDBFactorDraft: vi.fn(),
  },
}));

// RD 物化面板自带轮询，与本文件无关；换桩，免得它的定时器混进断言。
vi.mock('../RdMinedMaterializePanel', () => ({ RdMinedMaterializePanel: () => null }));

const sourcesMock = vi.mocked(adminService.getQuantDBFactorSources);
const fieldsMock = vi.mocked(adminService.getQuantDBFactorFields);
const catalogMock = vi.mocked(adminService.getQuantDBFactorCatalog);
const versionsMock = vi.mocked(adminService.listQuantDBFactorVersions);
const publishMock = vi.mocked(adminService.publishQuantDBFactorDraft);

/** 后端 /catalog 的形状：categories[].features[] 会被摊平成 mappings。 */
const draftPayload = (versionId: string, versionName: string, featureCount = 0) => ({
  version_id: versionId,
  version_name: versionName,
  status: 'draft',
  source_dataset: 'l1_factors',
  market: 'CN',
  feature_count: featureCount,
  categories: featureCount
    ? [{ category_id: 'other', category_name: '其他因子', features: [{ mapping_id: 'm1', key: 'VOLUME48', feature_name: 'VOLUME48', source_column: 'VOLUME48', enabled: true }] }]
    : [],
});

beforeEach(async () => {
  // `Modal.confirm` 渲染在 antd 自建的静态容器里，不是本测试 render() 的树，
  // 所以 testing-library 的自动 cleanup **收不走它**：上一用例的确认框会留在
  // document.body 上，下一个用例再弹一个，`findByText('发布这份草稿？')`
  // 就撞上「Found multiple elements」。
  //
  // 收干净必须走 antd 自己的卸载路径：`destroyAll()` 只是**开始**退场，退场动画
  // 结束才摘节点。**不要**图快手动 `node.remove()`——那是把节点从 React 眼皮底下
  // 拿走，它随后自己去删同一个节点就抛 `NotFoundError: The node to be removed is
  // not a child of this node`（实测 4 次 Unhandled Rejection，挂在每个用例尾巴上）。
  // 这里等它走完：jsdom 不触发 transitionend，rc-motion 有 setTimeout 兜底。
  Modal.destroyAll();
  await waitFor(() => expect(document.querySelectorAll('.ant-modal-root')).toHaveLength(0));

  sourcesMock.mockReset();
  fieldsMock.mockReset();
  catalogMock.mockReset();
  versionsMock.mockReset();
  publishMock.mockReset();
  publishMock.mockResolvedValue({} as any);

  sourcesMock.mockResolvedValue({
    sources: { l1_factors: { ready: true, files: 10, column_count: 33 } },
    labels: { l1_factors: 'L1 因子（默认）' },
    default_source: 'l1_factors',
  });
  fieldsMock.mockResolvedValue({ fields: [] });
  // 未发布：后端以 catalog: null 表示，service 把它折成 null
  catalogMock.mockResolvedValue(null);
  versionsMock.mockResolvedValue({ versions: [] });
});

describe('AdminTrainingDatasets：草稿发现', () => {
  test('首屏认领该来源库现有的草稿（别处注册进来的也要看得见）', async () => {
    versionsMock.mockResolvedValue({
      versions: [
        { version_id: 'qdb-cn-l1_factors-3f1467cddd58', version_name: '因子研究注册 private 2026-10-07', status: 'draft', mapping_count: 17 },
      ],
    });
    catalogMock.mockImplementation(async (_s: string, vid?: string) =>
      vid === 'qdb-cn-l1_factors-3f1467cddd58'
        ? draftPayload('qdb-cn-l1_factors-3f1467cddd58', '因子研究注册 private 2026-10-07', 1)
        : null,
    );

    render(<AdminTrainingDatasets />);

    // 认领到位：右侧「分类映射草稿」变成编辑态，且发布按钮可用
    expect(await screen.findByText('因子研究注册 private 2026-10-07')).toBeTruthy();
    expect(screen.getByText('编辑中')).toBeTruthy();
    expect(screen.getByRole('button', { name: /发布此草稿/ })).toBeTruthy();
    // 必须按**那个草稿的 id** 去取，不是「取活动发布版本」
    expect(catalogMock).toHaveBeenCalledWith('l1_factors', 'qdb-cn-l1_factors-3f1467cddd58', 'CN');
  });

  test('只有已发布版本时保持「未创建」，不凭空认一个草稿', async () => {
    versionsMock.mockResolvedValue({
      versions: [
        { version_id: 'v-pub', version_name: '早就发布的那份', status: 'published', mapping_count: 33 },
      ],
    });

    render(<AdminTrainingDatasets />);

    expect(await screen.findByRole('button', { name: /新建草稿/ })).toBeTruthy();
    expect(screen.queryByText('编辑中')).toBeNull();
    // 已发布版本只该走不带 version_id 的那次查询
    expect(catalogMock).not.toHaveBeenCalledWith('l1_factors', 'v-pub', 'CN');
  });

  test('刷新时不改认别的草稿：正在编辑的那份不能被抢走', async () => {
    versionsMock
      .mockResolvedValueOnce({
        versions: [{ version_id: 'D', version_name: '草稿 D', status: 'draft' }],
      })
      // 第二次若真被问到，会答一个更新的——认领若无条件，用户就会被静默换走
      .mockResolvedValue({
        versions: [{ version_id: 'NEWER', version_name: '草稿 NEWER', status: 'draft' }],
      });
    catalogMock.mockImplementation(async (_s: string, vid?: string) =>
      vid ? draftPayload(vid, vid === 'D' ? '草稿 D' : '草稿 NEWER') : null,
    );

    render(<AdminTrainingDatasets />);
    expect(await screen.findByText('草稿 D')).toBeTruthy();

    // 用正则而不是精确串：antd 的图标会以 role=img + aria-label 计入可访问名，
    // 精确匹配 '刷新' 匹配不上真实的 "reload 刷新"
    fireEvent.click(screen.getByRole('button', { name: /刷新/ }));

    await waitFor(() => expect(screen.getByText('草稿 D')).toBeTruthy());
    expect(screen.queryByText('草稿 NEWER')).toBeNull();
    // 认领只该发生一次：手上已经有草稿了，就不再去问「有哪些版本」
    expect(versionsMock).toHaveBeenCalledTimes(1);
  });
});

/**
 * 发布确认（2026-10-07）。
 *
 * 发布**当场**把线上口径换掉（后端把当前 published 转 archived、把草稿扶正）。
 * 这个按钮在草稿能被本页显示之前根本够不着，所以确认框是随草稿发现修复一起
 * 补上的，不是独立需求。锁三件事：
 *
 * 1. 点发布**先弹确认**，不是直接 POST——误点一次就换了线上口径；
 * 2. 口径变小时要**把两个数都摆出来**并给危险按钮；
 * 3. 比较用的必须是**启用数**，不是后端 `feature_count`。
 */
const featuresPayload = (
  versionId: string,
  versionName: string,
  status: string,
  counts: { enabled: number; disabled?: number },
) => ({
  version_id: versionId,
  version_name: versionName,
  status,
  source_dataset: 'l1_factors',
  market: 'CN',
  // 后端就是这么算的：**不过滤 enabled**（quantdb_factor_catalog.py:471）。
  // 故意让它在下面第三个用例里和启用数反向，好把「拿它比大小」钉死。
  feature_count: counts.enabled + (counts.disabled ?? 0),
  categories: [{
    id: 'other',
    name: '其他因子',
    features: [
      ...Array.from({ length: counts.enabled }, (_, i) => ({
        feature_id: `en${i}`, key: `EN${i}`, feature_name: `EN${i}`,
        source_column: `EN${i}`, enabled: true,
      })),
      ...Array.from({ length: counts.disabled ?? 0 }, (_, i) => ({
        feature_id: `dis${i}`, key: `DIS${i}`, feature_name: `DIS${i}`,
        source_column: `DIS${i}`, enabled: false,
      })),
    ],
  }],
});

/** 让页面左手拿到草稿、右手拿到已发布版本。published 传 null 表示首次发布。 */
const stagePublish = (
  published: ReturnType<typeof featuresPayload> | null,
  draftCounts: { enabled: number; disabled?: number } = { enabled: 2 },
) => {
  versionsMock.mockResolvedValue({
    versions: [{ version_id: 'DRAFT', version_name: '草稿 v2', status: 'draft' }],
  });
  catalogMock.mockImplementation(async (_s: string, vid?: string) =>
    vid === 'DRAFT' ? featuresPayload('DRAFT', '草稿 v2', 'draft', draftCounts) : published,
  );
};

describe('AdminTrainingDatasets：发布前确认口径增减', () => {
  test('点发布先弹确认，确认之前不发 POST', async () => {
    stagePublish(featuresPayload('PUB', '线上 v1', 'published', { enabled: 5 }));

    render(<AdminTrainingDatasets />);
    fireEvent.click(await screen.findByRole('button', { name: /发布此草稿/ }));

    // 弹了确认，且**还没**写库——误点一次就是换线上口径
    await screen.findByRole('dialog', { name: '发布这份草稿？' });
    expect(publishMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /^发\s*布$/ }));

    await waitFor(() => expect(publishMock).toHaveBeenCalledWith('DRAFT'));
  });

  test('口径缩小时两个数都摆出来，并给危险按钮', async () => {
    stagePublish(featuresPayload('PUB', '线上 v1', 'published', { enabled: 5 }));

    render(<AdminTrainingDatasets />);
    fireEvent.click(await screen.findByRole('button', { name: /发布此草稿/ }));

    const dialog = await screen.findByText(/线上启用特征将从/);
    expect(dialog.textContent).toContain('5');
    expect(dialog.textContent).toContain('2');
    // 缩小是破坏性动作，确认键要显眼
    expect(screen.getByRole('button', { name: /^发\s*布$/ }).className).toContain('dangerous');
  });

  test('口径没变小就不吓人：不报「减少」，也不给危险按钮', async () => {
    // 草稿 7 启用 > 线上 5 启用：是**扩**，不该报警
    stagePublish(
      featuresPayload('PUB', '线上 v1', 'published', { enabled: 5 }),
      { enabled: 7 },
    );

    render(<AdminTrainingDatasets />);
    fireEvent.click(await screen.findByRole('button', { name: /发布此草稿/ }));
    await screen.findByRole('dialog', { name: '发布这份草稿？' });

    // 对照上一条：同样的入口，这里必须**没有**告警行
    expect(screen.queryByText(/线上启用特征将从/)).toBeNull();
    expect(screen.getByRole('button', { name: /^发\s*布$/ }).className).not.toContain('dangerous');
  });

  test('比的是启用数，不是 feature_count——后者的「缩小」方向是假的', async () => {
    // 草稿：启用 10、禁用 0 → feature_count=10
    // 线上：启用 5、禁用 400 → feature_count=405
    // 用 feature_count 比会得出 10<405「减少了」；用启用数比是 10>5，**是扩**。
    stagePublish(
      featuresPayload('PUB', '线上 v1', 'published', { enabled: 5, disabled: 400 }),
      { enabled: 10 },
    );

    render(<AdminTrainingDatasets />);
    fireEvent.click(await screen.findByRole('button', { name: /发布此草稿/ }));
    await screen.findByRole('dialog', { name: '发布这份草稿？' });

    expect(screen.queryByText(/线上启用特征将从/)).toBeNull();
  });

  test('首次发布不算缩小：没有旧版本可比', async () => {
    stagePublish(null); // 该源从未发布过

    render(<AdminTrainingDatasets />);
    fireEvent.click(await screen.findByRole('button', { name: /发布此草稿/ }));

    await screen.findByRole('dialog', { name: '发布这份草稿？' });
    expect(screen.queryByText(/线上启用特征将从/)).toBeNull();
    expect(screen.queryByText(/替换当前/)).toBeNull();
    expect(screen.getByRole('button', { name: /^发\s*布$/ }).className).not.toContain('dangerous');
  });
});
