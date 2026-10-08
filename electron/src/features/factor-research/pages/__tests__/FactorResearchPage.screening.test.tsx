/**
 * 因子研究页 —— 筛选页签的接线契约。
 *
 * 筛选清单的名字横跨**两个**数据集目录（实测 362 个里 331 私人库 / 31 经典库），
 * 而页面主流程只持有当前数据集那一份目录。这里守的就是这道缝上的四件事，
 * 每一件坏了都不会报错、只会静默做错：
 *
 * 1. 缺的那份目录**只在进筛选页签时才补**——它是 39 KB，但没必要每次开页都拉；
 * 2. 点经典库的因子要**顺带切库**。不切的话 `SingleFactorTab` 会拿经典库的 code
 *    去问私人库的快照，界面只会显示「该因子不可用」；
 * 3. 切库时 `[dataset]` 那个 effect 会清空 activeCode（它分不清「用户手动切库」和
 *    「从筛选页点名跳过去」），清掉之后「没选中就补第一个因子」会立刻把用户点的
 *    东西换成榜单第一名——**这是本条最阴的失败模式**，因为界面看起来完全正常；
 * 4. 从筛选页注册的目标数据集恒为私人库，不跟当前展示的数据集走。在经典库下
 *    勾选再把目标定成经典库，后端会把每一条都 skipped。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import FactorResearchPage from '../FactorResearchPage';

const { catalogMock, leaderboardMock, isAdminRef } = vi.hoisted(() => ({
  catalogMock: vi.fn(),
  leaderboardMock: vi.fn(),
  isAdminRef: { value: true },
}));

vi.mock('../../services/factorResearchService', async () => {
  const actual = await vi.importActual<typeof import('../../services/factorResearchService')>(
    '../../services/factorResearchService',
  );
  return { ...actual, getCatalog: catalogMock, getLeaderboard: leaderboardMock };
});

vi.mock('../../../../store', () => ({ useAppSelector: () => isAdminRef.value }));

// 子组件桩：把页面传下去的 props 摊在 DOM 上，本文件测的就是这些 props
vi.mock('../../components/CatalogSidebar', () => ({ CatalogSidebar: () => <div data-testid="sidebar" /> }));
vi.mock('../../components/LeaderboardTab', () => ({ LeaderboardTab: () => <div data-testid="tab-leaderboard" /> }));
vi.mock('../../components/CompareTab', () => ({ CompareTab: () => null }));
vi.mock('../../components/ComposeTab', () => ({ ComposeTab: () => null }));
vi.mock('../../components/factor-report/FactorReportPanel', () => ({
  FactorReportPanel: (p: { initialDataset?: string; initialCode?: string }) => (
    <div data-testid="factor-report" data-dataset={p.initialDataset} data-code={p.initialCode} />
  ),
}));
vi.mock('../../components/SnapshotPanel', () => ({ SnapshotPanel: () => <div data-testid="panel-snapshot" /> }));
vi.mock('../../components/ScanPanel', () => ({ ScanPanel: () => <div data-testid="panel-scan" /> }));

vi.mock('../../components/SingleFactorTab', () => ({
  SingleFactorTab: (p: { code: string | null; dataset: string }) => (
    <div data-testid="single" data-code={p.code} data-dataset={p.dataset} />
  ),
}));

vi.mock('../../components/RegisterToTrainingModal', () => ({
  RegisterToTrainingModal: (p: { codes: string[]; factors: Array<{ code: string }>; dataset: string }) => (
    <div
      data-testid="register"
      data-codes={p.codes.join(',')}
      data-dataset={p.dataset}
      data-factors={p.factors.map((f) => f.code).join(',')}
    />
  ),
}));

vi.mock('../../components/ScreeningTab', () => ({
  ScreeningTab: (p: {
    index: Map<string, unknown>;
    onOpenSingle: (code: string, ds: string) => void;
    onToggle: (code: string) => void;
    onRegister: () => void;
  }) => (
    <div data-testid="screening" data-index-size={p.index.size}>
      {/* 经典库的因子：私人库目录里没有它 */}
      <button onClick={() => p.onOpenSingle('ABTURN', 'classic')}>桩-点经典因子</button>
      {/* 私人库的因子 */}
      <button onClick={() => p.onOpenSingle('a101_001', 'private')}>桩-点私人因子</button>
      <button onClick={() => p.onToggle('a101_001')}>桩-勾选</button>
      <button onClick={() => p.onRegister()}>桩-注册</button>
    </div>
  ),
}));

const PRIVATE_CATALOG = {
  factors: [{ code: 'a101_001', name_cn: 'a101_001', l2: 'alpha_library', available: true }],
  l1_order: ['Alpha 因子库'],
  l2_order: {},
  meta: {},
};
const CLASSIC_CATALOG = {
  factors: [{ code: 'ABTURN', name_cn: '换手率', l2: '动量', available: true }],
  l1_order: ['动量'],
  l2_order: {},
  meta: {},
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <FactorResearchPage />
    </MemoryRouter>,
  );

const gotoScreening = async () => {
  fireEvent.click(await screen.findByRole('button', { name: '筛选' }));
  return screen.findByTestId('screening');
};

beforeEach(() => {
  catalogMock.mockReset();
  leaderboardMock.mockReset();
  isAdminRef.value = true;
  catalogMock.mockImplementation((ds: string) =>
    Promise.resolve(ds === 'private' ? PRIVATE_CATALOG : CLASSIC_CATALOG),
  );
  // 榜单永远有一行：若 activeCode 被误清空，「补第一个因子」会把它顶上来
  leaderboardMock.mockResolvedValue({ leaderboard: [{ code: 'AUTO_FIRST', rank: 1 }], meta: {} });
});

describe('FactorResearchPage：筛选页签的目录索引', () => {
  test('进页面只拉当前数据集，进筛选页签才补另一个', async () => {
    renderPage();
    await waitFor(() => expect(catalogMock).toHaveBeenCalledWith('private'));
    expect(catalogMock).not.toHaveBeenCalledWith('classic');

    await gotoScreening();

    await waitFor(() => expect(catalogMock).toHaveBeenCalledWith('classic'));
  });

  test('索引键是目录的 code（不是 name_cn），两个数据集都在里面', async () => {
    renderPage();
    const tab = await gotoScreening();

    // 私人库 a101_001 + 经典库 ABTURN —— 经典库的 name_cn「换手率」不该进索引
    await waitFor(() => expect(tab.getAttribute('data-index-size')).toBe('2'));
  });

  test('私人库目录还在路上时进筛选页签：不把同一份目录拉两遍', async () => {
    // 私人库卡住不放行，模拟「进页面就立刻点筛选」——上面两条用例的 mock 会在
    // 点击前就 resolve，走不到这条路。
    let releasePrivate: (v: unknown) => void = () => undefined;
    catalogMock.mockImplementation((ds: string) =>
      ds === 'private' ? new Promise((r) => { releasePrivate = r; }) : Promise.resolve(CLASSIC_CATALOG),
    );

    renderPage();
    await waitFor(() => expect(catalogMock).toHaveBeenCalledWith('private'));

    await gotoScreening();
    await waitFor(() => expect(catalogMock).toHaveBeenCalledWith('classic'));
    // 私人库那条还在途，补缺的路必须跳过它：1.1 MB 拉两遍是白烧引擎（它被并发压垮过）
    expect(catalogMock.mock.calls.filter((c) => c[0] === 'private')).toHaveLength(1);

    // 跳过 ≠ 永远缺：在途那条回来之后索引仍要补齐两个数据集
    releasePrivate(PRIVATE_CATALOG);
    await waitFor(() =>
      expect(screen.getByTestId('screening').getAttribute('data-index-size')).toBe('2'),
    );
  });
});

describe('FactorResearchPage：从筛选页点进单因子分析', () => {
  test('私人库的因子：不切库，直接带 private', async () => {
    renderPage();
    await gotoScreening();

    fireEvent.click(screen.getByText('桩-点私人因子'));

    const single = await screen.findByTestId('single');
    expect(single.getAttribute('data-code')).toBe('a101_001');
    expect(single.getAttribute('data-dataset')).toBe('private');
  });

  test('经典库的因子：切到经典库，且**不能**被「补第一个因子」顶掉', async () => {
    renderPage();
    await gotoScreening();

    fireEvent.click(screen.getByText('桩-点经典因子'));

    const single = await screen.findByTestId('single');
    await waitFor(() => expect(single.getAttribute('data-dataset')).toBe('classic'));
    // AUTO_FIRST 是桩榜单的第一名。这里若变成它，说明 activeCode 被切库 effect 清掉了。
    expect(single.getAttribute('data-code')).toBe('ABTURN');
  });
});

describe('FactorResearchPage：用户自己切库', () => {
  /**
   * 与上面那条**互补**的一路：那里是「从筛选页点名跳过去」，这里是用户点数据集按钮。
   * 后者没有 keepActiveRef 护着，走的是「清空 activeCode → 回头补第一个」的正路，
   * 而「回头」那一步读的 rows 如果没有随切库清掉，读到的就是**上一库**的榜单。
   */
  test('切库后 activeCode 必须来自新库的榜单，不能是上一库的第一名', async () => {
    leaderboardMock.mockImplementation((_r: unknown, _n: unknown, ds: string) =>
      Promise.resolve({
        leaderboard: [{ code: ds === 'private' ? 'PRIV_FIRST' : 'CLS_FIRST', rank: 1 }],
        meta: {},
      }),
    );

    renderPage();
    // 先让私人库的榜单落地，把 activeCode 撑起来（否则切库时它本来就是 null，测不到）
    fireEvent.click(await screen.findByRole('button', { name: '单因子分析' }));
    await waitFor(() =>
      expect(screen.getByTestId('single').getAttribute('data-code')).toBe('PRIV_FIRST'),
    );

    fireEvent.click(await screen.findByRole('button', { name: '经典因子' }));

    const single = screen.getByTestId('single');
    // 等新库的榜单落地再断言：这里若停在 PRIV_FIRST，就是 rows 没清、旧第一名
    // 被写回 activeCode 并且因为非空而永远不再纠正。
    await waitFor(() => expect(single.getAttribute('data-code')).toBe('CLS_FIRST'));
    expect(single.getAttribute('data-dataset')).toBe('classic');
  });
});

describe('FactorResearchPage：从筛选页注册到训练目录', () => {
  test('目标数据集恒为私人库，且用私人库目录解析来源库', async () => {
    renderPage();
    await gotoScreening();

    fireEvent.click(screen.getByText('桩-勾选'));
    fireEvent.click(screen.getByText('桩-注册'));

    const modal = await screen.findByTestId('register');
    expect(modal.getAttribute('data-codes')).toBe('a101_001');
    expect(modal.getAttribute('data-dataset')).toBe('private');
    // 来源库（l2）取自私人库目录；经典库那份是「动量」这类中文字面量，过不了校验
    expect(modal.getAttribute('data-factors')).toBe('a101_001');
  });

  test('页面停在经典库上时，注册目标也不跟着走', async () => {
    renderPage();
    await waitFor(() => expect(catalogMock).toHaveBeenCalledWith('private'));

    fireEvent.click(await screen.findByRole('button', { name: '经典因子' }));
    await gotoScreening();

    fireEvent.click(screen.getByText('桩-勾选'));
    fireEvent.click(screen.getByText('桩-注册'));

    const modal = await screen.findByTestId('register');
    expect(modal.getAttribute('data-dataset')).toBe('private');
    expect(modal.getAttribute('data-factors')).toBe('a101_001');
  });
});
