/**
 * 因子研究 — 独立栏目页（factor-lib-demo 完整复刻 + 因子工具页签）
 *
 * 布局：左侧因子目录（分类树 + 搜索 + 标签筛选）· 顶部区间选择 · 两组页签：
 * 研究：排行榜 / 单因子分析 / 多因子对比 / 多因子合成 / 筛选
 * 工具：因子报告（2026-09-17 由技能中心迁入，全宽渲染；策略模板已迁至回测中心·策略管理右侧，评估中心已迁至模拟交易页签）
 * 数据：/api/v1/factor-research（引擎服务，快照由 build_factor_research.py 构建）
 *
 * 市场分层（2026-10）：跟随全局市场选择器（CN/HK/US/FUTURES），market 进全部
 * 请求与依赖数组；切换市场即清空目录缓存与选择态、数据集重置为私人库、全量重取。
 * CRYPTO 未接入（工作台无基准表）→ 诚实阻断卡片，**不发起任何请求**、绝不回退
 * CN 数据（拿沪深300 给加密因子算超额是数据污染）。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowRightLeft, Award, BarChart3, ChevronsLeft, ChevronsRight, Database, Filter, Layers, LineChart, ScanSearch, Sigma, TableProperties } from 'lucide-react';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import { getMarketConfig } from '../../../config/marketConfig';
import { ApiError, getCatalog, getLeaderboard } from '../services/factorResearchService';
import type { FactorDataset, RangeParams } from '../services/factorResearchService';
import type { CategoryFilter, FactorMeta, LeaderboardRow, RangeMeta, ResearchMarket } from '../types/factorResearch';
import { RangePicker } from '../components/common';
import type { RangeValue } from '../components/common';
import { CatalogSidebar } from '../components/CatalogSidebar';
import { LeaderboardTab } from '../components/LeaderboardTab';
import { SingleFactorTab } from '../components/SingleFactorTab';
import { CompareTab } from '../components/CompareTab';
import { ComposeTab } from '../components/ComposeTab';
import { ScreeningTab } from '../components/ScreeningTab';
import type { FactorLocation } from '../components/ScreeningTab';
import { SnapshotPanel } from '../components/SnapshotPanel';
import { ScanPanel } from '../components/ScanPanel';
import { RegisterToTrainingModal } from '../components/RegisterToTrainingModal';
import { FactorReportPanel } from '../components/factor-report/FactorReportPanel';
import { MarketSelector } from '../../../components/layout/MarketSelector';

/** 排行榜去重阈值（与筛选页默认 |ρ| ≥ 0.9 同口径；改这里须同步筛选页语义） */
const LEADERBOARD_DEDUP_RHO = 0.9;
import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';

/**
 * 因子研究已接入的市场（= ResearchMarket 全集）。全局 AppMarket 里 CRYPTO 不在内：
 * 工作台未接基准表（后端 422「未接入市场」）。判断只在这一个地方做。
 */
const RESEARCH_MARKETS: readonly string[] = ['CN', 'HK', 'US', 'FUTURES'];
const isResearchMarket = (m: string): m is ResearchMarket => RESEARCH_MARKETS.includes(m);

/** 在途目录请求的键（见 `catalogInflightRef` 的注释）。 */
const inflightKey = (market: ResearchMarket, dataset: FactorDataset): string => `${market}:${dataset}`;

/** 数据集胶囊文案（`catalogDatasets` 决定当前市场渲染哪几枚） */
const DATASET_LABELS: Record<FactorDataset, { label: string; title: string }> = {
  classic: { label: '经典因子', title: 'factor-lib-demo 复刻的 82 因子（仅 A 股供给）' },
  private: { label: '私人因子库', title: '本市场 L1/L2 等因子库筛选保留的私人因子' },
};

type Tab = 'leaderboard' | 'single' | 'compare' | 'compose' | 'screening' | 'factor-report';

const TABS: Array<{ key: Tab; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { key: 'leaderboard', label: '排行榜', icon: BarChart3 },
  { key: 'single', label: '单因子分析', icon: TableProperties },
  { key: 'compare', label: '多因子对比', icon: ArrowRightLeft },
  { key: 'compose', label: '多因子合成', icon: Layers },
  { key: 'screening', label: '筛选', icon: Filter },
  { key: 'factor-report', label: '因子报告', icon: LineChart },
];

/** 工具页签：不依赖因子目录/区间工具条，整页全宽渲染 */
const TOOL_TABS: Tab[] = ['factor-report'];
const isToolTab = (t: Tab): boolean => TOOL_TABS.includes(t);
const isValidTab = (v: string | null): v is Tab => TABS.some((t) => t.key === v);

const FactorResearchPage: React.FC = () => {
  // 页签支持深链（如评估徽章跳 /factor-research?tab=eval）；切换时同步回 URL
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const [tab, setTabState] = useState<Tab>(() => {
    const fromUrl = searchParams.get('tab');
    return isValidTab(fromUrl) ? fromUrl : 'leaderboard';
  });
  const setTab = useCallback(
    (t: Tab) => {
      setTabState(t);
      setSearchParams(t === 'leaderboard' ? {} : { tab: t }, { replace: true });
    },
    [setSearchParams],
  );
  const isTool = isToolTab(tab);

  // 全局市场（顶栏及各切面页头部共用 uiSlice.currentMarket；**本页头部左侧也挂了
  // 一枚** MarketSelector，页内直切即生效——换市场由 marketResetRef effect 清旧重取）。
  // `researchMarket` 为 null = 该市场未接入 → 渲染诚实阻断面，一切取数 effect
  // 直接短路（CRYPTO 下一个请求都不发）。
  const currentMarket = useAppSelector(selectCurrentMarket);
  const researchMarket: ResearchMarket | null = isResearchMarket(currentMarket) ? currentMarket : null;
  const marketLabel = getMarketConfig(currentMarket).label;
  /** 目录数据集清单：经典库是 CN demo 口径的复刻，仅 CN 供给；非 CN 只有私人库 */
  const catalogDatasets: FactorDataset[] = useMemo(
    () => (researchMarket === 'CN' ? ['classic', 'private'] : ['private']),
    [researchMarket],
  );

  // URL 为准：外部导航（徽章深链等）带 ?tab= 时同步切换页签。
  // 依赖里**只放 searchParams、不放 tab**：本页自己的 setTab 是「先改 state、后跳
  // URL」，中间那一版渲染 searchParams 还停在旧值（如 ?tab=single），effect 若跟着
  // tab 一起重跑，就会拿这个滞后 URL 把刚切过去的页签拽回去。回排行榜的跳转会把
  // ?tab= 清成空串，没有第二次纠正机会 —— 表现为「点排行榜却停在单因子」卡死
  // （2026-10-09 由分类接线测试实测暴露；切去 *别的* 页签时 URL 最后会带上新
  // ?tab= 再纠正一次，所以这个坑只在回排行榜时显形）。
  useEffect(() => {
    const fromUrl = searchParams.get('tab');
    if (isValidTab(fromUrl)) setTabState(fromUrl);
  }, [searchParams]);
  const [factors, setFactors] = useState<FactorMeta[]>([]);
  const [l1Order, setL1Order] = useState<string[]>([]);
  const [l2Order, setL2Order] = useState<Record<string, string[]>>({});
  const [rows, setRows] = useState<LeaderboardRow[]>([]);
  const [catalogMeta, setCatalogMeta] = useState<Record<string, unknown>>({});
  const [lbMeta, setLbMeta] = useState<Record<string, unknown>>({});
  const meta = useMemo(() => ({ ...catalogMeta, ...lbMeta }), [catalogMeta, lbMeta]);
  /** 排行榜去重开关（口径同筛选页 |ρ| ≥ 0.9；切换触发一次后端重算 + 重排名） */
  const [lbDedup, setLbDedup] = useState(false);
  const [range, setRange] = useState<RangeValue>({ preset: 'all', start: null, end: null });
  const [tagFilter, setTagFilter] = useState<string[]>([]);
  /** 左侧目录可收起：目录固定 248px，榜单列多时让它给表格让宽度（会话内记忆即可） */
  const [catalogOpen, setCatalogOpen] = useState(true);
  const [lbN, setLbN] = useState(30);
  const [selected, setSelected] = useState<string[]>([]);
  const [activeCode, setActiveCode] = useState<string | null>(null);
  /**
   * 左侧目录选中的分类限定。只作用于排行榜的过滤视图（榜单内的名次/综合分
   * 本就是全库截面口径）；`l2: null` = 整个大类。切数据集时清空 —— l1/l2 是
   * 各库自己的命名空间（经典库 l2 是「动量」这类中文名），跨库残留只会筛出空榜。
   */
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter | null>(null);
  /** 目录（factors）拉取中。与共享的 `loading` 分开：那个还盖着榜单重算， */
  /** 而左侧目录的空态文案（「正在加载因子目录…」vs「无匹配因子」）只跟这一件事有关。 */
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  // 主区右上角的两块「工具面板」：快照（状态/构建）与扫描（差异）。同一时刻只开一块。
  const [panel, setPanel] = useState<'none' | 'snapshot' | 'scan'>('none');
  // 快照就绪后要不要**自动收起**快照面板，取决于它是怎么打开的：
  // - 榜单 503（快照缺失）时自动弹出 → 建完就该收起并把数据带回来；
  // - 用户自己点开（「快照」按钮 / 扫描面板的「重算快照」）→ **绝不能自动收起**。
  //   快照存在时 SnapshotPanel 首轮轮询就会回调 onReady，自动收起会让面板一闪而过，
  //   里面那个真正发起构建的「重算快照」按钮永远点不到 —— 入口变成死胡同。
  const [snapshotAutoClose, setSnapshotAutoClose] = useState(true);
  // 本轮 503 是否已经自动弹过一次面板。**必须**有这个闸门：`/snapshot-status` 说
  // 「就绪」而榜单仍 503 时（快照文件在但不完整），「弹出 → onReady → 收起 → 重拉榜单
  // → 又 503 → 再弹出」会自转成一个请求循环，把引擎打满 —— 2026-09-19 就有过并发
  // 因子研究请求把引擎健康检查压到失败、被看门狗重启的先例。榜单一旦成功即复位，
  // 真正的下一次快照缺失照样能自动弹。
  const autoOpenedRef = useRef(false);
  const [dataset, setDataset] = useState<FactorDataset>('private'); // 默认以 L1+L2 私人因子库为主
  const [reloadKey, setReloadKey] = useState(0);
  // 注册弹窗的入参在**打开那一刻**固定下来。主选（selected）与筛选页签的选择
  // 是两套互不相干的状态，且目标数据集可能不同（筛选选中恒为私人库），
  // 让弹窗自己去读 state 会在切页签/切数据集时错位到另一批因子。
  const [registerTarget, setRegisterTarget] = useState<{ codes: string[]; dataset: FactorDataset } | null>(null);
  // 因子报告的深链目标。只有从筛选页签点「报告」才会被设置；用户自己点页签会清掉。
  const [reportTarget, setReportTarget] = useState<{ dataset: string; code: string } | null>(null);
  /**
   * 两个数据集目录的累积缓存。页面主流程只消费 `factors`（当前数据集），
   * 但筛选页签的名字横跨两个目录，需要一份完整的 name→落点索引才行。
   * 私人库 1.1 MB / 经典库 39 KB，缓存下来比每次现拉划算得多。
   */
  const [catalogCache, setCatalogCache] = useState<Partial<Record<FactorDataset, FactorMeta[]>>>({});
  /**
   * 在途的目录请求，键 = `市场:数据集`。主流程与「补缺的那份」两条路可能同时想要
   * 同一个数据集——进页面就立刻点筛选页签最典型（私人库目录 1.1 MB 还在路上，
   * cache 仍是空的）。没有这个闸门就会把同一份目录拉两遍，而引擎被并发因子研究
   * 请求压垮过一次（2026-09-19，看门狗重启），这里不该再添一份。
   *
   * 键**必须带市场**：换市场时上一市场那条请求的 `.finally` 仍会销号，若只按数据集
   * 记，它会把新市场同名数据集的在途登记一并抹掉，补缺那条路就会再拉一遍。
   */
  const catalogInflightRef = useRef(new Set<string>());
  /**
   * 每个目录的取回结果。筛选页要靠它区分两件**界面表现必须不同**的事：
   * 「这份目录还没到手 / 没取回来」与「目录到手了、里面确实没这个因子」。
   * 只有后者才该建议去重算快照 —— 把取回失败说成「新挖到的因子还没进快照」，
   * 等于把人指去跑一个十分钟的昂贵错路，而真正的问题是引擎那一下没答理。
   */
  const [catalogOutcome, setCatalogOutcome] = useState<Partial<Record<FactorDataset, 'ok' | 'fail'>>>({});
  /** 筛选页签里勾中的因子。**不并入 `selected`**：那一个是当前数据集的 code 空间
   *  （切库即清空、排行榜/对比/合成共用），而筛选的勾选天然跨库且只服务于注册。 */
  const [screeningSelected, setScreeningSelected] = useState<string[]>([]);
  /** 跨库跳转时把目标 code 带过 `[dataset]` 那次清理（见下面的 effect）。 */
  const keepActiveRef = useRef<string | null>(null);
  // 注册会写 `qm_training_factor_mapping`，后端是 require_admin；非管理员不渲染入口。
  const isAdmin = useAppSelector((state) => state.auth.user?.is_admin) || false;

  // 经典因子库**不能**注册：`store.factors_meta("classic")` 恒为 None（只有
  // `build_factor_panel_private.py` 写 factors.json），且 classic 的 l2 是
  // 「动量」这类中文字面量，过不了后端的来源库标识符校验。所以放行条件必须
  // 带上数据集，否则按钮点了只会全量 skipped，还把用户指向永远刷不出来的
  // 「刷新字段」。
  const canRegister = isAdmin && dataset === 'private' && researchMarket === 'CN';

  const rangeParams: RangeParams = useMemo(
    () => ({ start: range.start, end: range.end }),
    [range.start, range.end],
  );

  useEffect(() => {
    if (!researchMarket) return; // 未接入市场：不发请求（面板由阻断卡片代替）
    // 非 CN 市场只有私人库一份目录（见 catalogDatasets）。换市场那一帧 dataset 还是
    // 旧市场的值（'classic'），不挡就会白拉一次本市场不存在的经典库。
    if (!catalogDatasets.includes(dataset)) return;
    let alive = true;
    setLoading(true);
    setCatalogLoading(true);
    catalogInflightRef.current.add(inflightKey(researchMarket, dataset));
    getCatalog(dataset, researchMarket)
      .then((cat) => {
        if (!alive) return;
        setFactors(cat.factors);
        setL1Order(cat.l1_order);
        setL2Order(cat.l2_order || {});
        setCatalogMeta(cat.meta);
        setCatalogCache((prev) => ({ ...prev, [dataset]: cat.factors }));
        setCatalogOutcome((prev) => ({ ...prev, [dataset]: 'ok' }));
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
        setCatalogOutcome((prev) => ({ ...prev, [dataset]: 'fail' }));
      })
      .finally(() => {
        // 不论组件还在不在都要销号：请求已经结束了，留在册只会让补缺那条路永远跳过它。
        catalogInflightRef.current.delete(inflightKey(researchMarket, dataset));
        if (alive) {
          setLoading(false);
          setCatalogLoading(false);
        }
      });
    return () => {
      alive = false;
    };
  }, [dataset, researchMarket, catalogDatasets]);

  // 切换市场：目录缓存/回执全清（它们此前都是按 market+dataset 的内容，
  // cache 只按 dataset 键存，不清就会把上一市场的因子清单喂给新市场），
  // 选择态与面板一并复位，数据集回私人库（非 CN 唯一可选）。
  //
  // 在途登记**不清**：它按 `市场:数据集` 记，上一市场那条与新市场无关，且各自
  // `.finally` 会销号。清掉反而是错的——本 effect 在挂载时排在「拉目录」那条之后，
  // 清一次就等于把刚登记的那条抹掉，补缺那条路会把 1.1 MB 的目录再拉一遍。
  // 首装也跳过：此刻各项状态本来就是下面这些默认值，跑一遍只多一轮白渲染。
  const marketResetRef = useRef(currentMarket);
  useEffect(() => {
    if (marketResetRef.current === currentMarket) return;
    marketResetRef.current = currentMarket;
    setCatalogCache({});
    setCatalogOutcome({});
    catalogInflightRef.current.clear();
    setFactors([]);
    setL1Order([]);
    setL2Order({});
    setCatalogMeta({});
    setLbMeta({});
    setRows([]);
    setSelected([]);
    setActiveCode(null);
    setTagFilter([]);
    setCategoryFilter(null);
    setRegisterTarget(null);
    setReportTarget(null);
    setScreeningSelected([]);
    setPanel('none');
    setError(null);
    setErrorStatus(null);
    setDataset('private');
    keepActiveRef.current = null;
  }, [currentMarket]);

  // 切换数据集：清空选择态，避免跨库代码混选。
  // 注册弹窗也要关——它按 dataset 决定目标库，挂着不关会用旧库的 codes 去写新库。
  // 工具面板同理：扫描只对私人库有意义（经典库目录来自内置清单，扫不出东西），
  // 停在扫描面板切到经典会留下一块讲不通的空结果。
  useEffect(() => {
    setSelected([]);
    // 榜单行也要清空。**不清就是个静默错配**：rows 在切库的这一刻还是上一库的，
    // 而下面「没选中就补第一个因子」那个 effect 会在 activeCode 被置空后重跑一次
    // （它的依赖就是 activeCode），条件成立 → 把上一库的第一名写回 activeCode。
    // 之后新榜单落地时 activeCode 已经非空，effect 不会再纠正，于是单因子分析
    // 拿私人库的 code 去问经典库的快照，只显示「该因子不可用」——用户从没选过它。
    // 清空不会闪：同一批更新里 loading 也被置 true，表格位渲染的是骨架屏。
    setRows([]);
    // 跨库跳转（筛选页点经典库的因子）会在同一次事件里改 dataset 和 activeCode，
    // 而这里会把 activeCode 清掉 —— 它是为「用户自己切库」写的，分不清两种情况。
    // keepActiveRef 把转移中的目标带过这一次清理；「没选中就补第一个因子」的那个
    // effect 因为 activeCode 非空，也就不会把用户点到的东西替换掉。
    setActiveCode(keepActiveRef.current);
    keepActiveRef.current = null;
    setTagFilter([]);
    // 分类限定同样清空：l1/l2 是各库自己的命名空间（经典库 l2 是「动量」这类
    // 中文名），残留私人库的分类名去筛经典库只会得到一块空榜。
    setCategoryFilter(null);
    setRegisterTarget(null);
    setPanel('none');
  }, [dataset]);

  // 筛选页签与因子报告页签都需要**两个**数据集的目录才建得出 name→落点索引
  // （前者给筛选清单定位；后者给反跳入口判「这个因子在单因子页打得开吗」），
  // 而主流程只拉当前那一个。缺的那份在这里补，且只补进 cache：factors/l1Order/
  // l2Order 是「当前数据集」的展示态，不能被一次后台补数改掉。
  // 依赖里的 catalogCache 会让它在补完后重跑一次并立刻空转返回，不会自转。
  useEffect(() => {
    if (!researchMarket) return;
    if (tab !== 'screening' && tab !== 'factor-report') return;
    // 在途的跳过：主流程可能正在拉同一个数据集（进页面就点筛选页签），
    // 不跳就会把 1.1 MB 的私人库目录拉两遍。
    const missing = catalogDatasets.filter(
      (d) => !catalogCache[d] && !catalogInflightRef.current.has(inflightKey(researchMarket, d)),
    );
    if (missing.length === 0) return;
    let alive = true;
    missing.forEach((d) => catalogInflightRef.current.add(inflightKey(researchMarket, d)));
    Promise.all(
      missing.map((d) =>
        getCatalog(d, researchMarket)
          .then((cat) => [d, cat.factors] as const)
          .catch(() => null)
          .finally(() => catalogInflightRef.current.delete(inflightKey(researchMarket, d))),
      ),
    ).then((out) => {
      if (!alive) return;
      const patch: Partial<Record<FactorDataset, FactorMeta[]>> = {};
      out.forEach((o) => {
        if (o) patch[o[0]] = o[1];
      });
      // 全部失败时一个字都不写：写了会改变 catalogCache 的引用、白触发一轮重渲染，
      // 而缺的那份还是缺的。空转返回即止（依赖没变，effect 不会再跑）。
      if (Object.keys(patch).length) setCatalogCache((prev) => ({ ...prev, ...patch }));
      // 成败逐个记账：cache 里没有不等于没取回来，筛选页要凭这个说实话。
      setCatalogOutcome((prev) => {
        const next = { ...prev };
        out.forEach((o, i) => { next[missing[i]] = o ? 'ok' : 'fail'; });
        return next;
      });
    });
    return () => {
      alive = false;
    };
  }, [tab, catalogCache, catalogDatasets, researchMarket]);

  const catalogIndex = useMemo(() => {
    const idx = new Map<string, FactorLocation>();
    // 索引键必须是目录的 **code**，不是 name_cn：筛选清单里的名字与 code 逐字
    // 相同（实测 362/362），而经典库的 name_cn 是「动量」这类中文名，用它建索引
    // 会一个都对不上。私人库后写：两份目录实测零重叠，万一将来重名，让主库赢。
    catalogDatasets.forEach((ds) => {
      (catalogCache[ds] || []).forEach((f) => idx.set(f.code, { dataset: ds, code: f.code }));
    });
    return idx;
  }, [catalogCache, catalogDatasets]);

  /** 目录的可信度，交给筛选页决定怎么解释「这行点不进去」。 */
  const catalogStatus: 'loading' | 'degraded' | 'ready' = useMemo(() => {
    if (catalogDatasets.some((d) => catalogOutcome[d] === 'fail')) return 'degraded';
    return catalogDatasets.every((d) => catalogOutcome[d] === 'ok') ? 'ready' : 'loading';
  }, [catalogOutcome, catalogDatasets]);

  useEffect(() => {
    if (!researchMarket) return;
    let alive = true;
    setLoading(true);
    getLeaderboard(rangeParams, lbN, dataset, lbDedup ? LEADERBOARD_DEDUP_RHO : 0, researchMarket)
      .then((lb) => {
        if (!alive) return;
        setRows(lb.leaderboard);
        setLbMeta(lb.meta);
        setError(null);
        setErrorStatus(null);
        autoOpenedRef.current = false; // 榜单恢复 → 允许下一次快照缺失再自动弹
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
        setErrorStatus(e instanceof ApiError ? e.status : null);
        if (e instanceof ApiError && e.status === 503 && !autoOpenedRef.current) {
          autoOpenedRef.current = true;
          setSnapshotAutoClose(true);
          setPanel('snapshot');
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [rangeParams, lbN, dataset, lbDedup, reloadKey, researchMarket]);

  useEffect(() => {
    if (!activeCode && rows.length) setActiveCode(rows[0].code);
  }, [rows, activeCode]);

  const rowsByCode = useMemo(() => new Map(rows.map((r) => [r.code, r])), [rows]);
  const nameOf = useCallback((c: string) => factors.find((f) => f.code === c)?.name_cn || c, [factors]);

  const toggleSelected = (code: string) =>
    setSelected(selected.includes(code) ? selected.filter((c) => c !== code) : [...selected, code]);
  const toggleTag = (tag: string) =>
    setTagFilter(tagFilter.includes(tag) ? tagFilter.filter((t) => t !== tag) : [...tagFilter, tag]);

  /**
   * 左侧目录点分类名：把右侧排行榜限定到该类（再点一次取消）。选中时切回排行榜 ——
   * 限定只作用于榜单视图，停在单因子/合成等页签上点了等于没反应。
   */
  const handleSelectCategory = useCallback(
    (f: CategoryFilter | null) => {
      setCategoryFilter(f);
      if (f) setTab('leaderboard');
    },
    [setTab],
  );

  /**
   * 打开单因子分析。`target` 是该因子所属的数据集（筛选页签会带过来）——
   * 与当前数据集不同就顺带切库：拿经典库的 code 去问私人库的快照，
   * 只会得到一屏「该因子不可用」，而界面上没有任何地方说明是库选错了。
   */
  const openSingle = useCallback(
    (code: string, target?: FactorDataset) => {
      if (target && target !== dataset) {
        keepActiveRef.current = code;
        setDataset(target);
      }
      setActiveCode(code);
      setTab('single');
    },
    [dataset, setTab],
  );

  const toggleScreening = useCallback((code: string) => {
    setScreeningSelected((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]));
  }, []);

  const toggleScreeningMany = useCallback((codes: string[], select: boolean) => {
    setScreeningSelected((prev) =>
      select ? [...new Set([...prev, ...codes])] : prev.filter((c) => !codes.includes(c)),
    );
  }, []);

  const openReport = useCallback(
    (reportDataset: string, code: string) => {
      setReportTarget({ dataset: reportDataset, code });
      setTab('factor-report');
    },
    [setTab],
  );

  /** 用户主动打开快照面板（「快照」按钮 / 扫描面板的重算）：就绪后**不**自动收起 */
  const openSnapshotManually = useCallback(() => {
    setSnapshotAutoClose(false);
    setPanel('snapshot');
  }, []);

  // 必须 useCallback：SnapshotPanel 的轮询 effect 依赖 onReady，而它每轮 setSt 都会
  // 重渲染。onReady 每次渲染换新 → effect 重订阅 → 立刻再 tick 一次……
  // 于是「5 秒轮询」退化成不受控的请求风暴（快照缺失、面板不被收起时最明显）。
  const handleSnapshotReady = useCallback(() => {
    setReloadKey((k) => k + 1);
    if (snapshotAutoClose) setPanel('none');
  }, [snapshotAutoClose]);

  const window_ = meta?.window as string[] | undefined;
  const available = factors.filter((f) => f.available).length;
  const rangeMeta = meta?.range as RangeMeta | undefined;

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 bg-gradient-to-br from-indigo-500 to-violet-500 rounded-2xl flex items-center justify-center shadow-lg shrink-0">
              <Sigma className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center gap-2.5 ml-1 min-w-0">
              {/* h1 不许被压缩换行：切换器撑宽后（1280 宽实测）标题会被挤成两行顶破头部 */}
              <h1 className="text-xl font-bold text-slate-800 tracking-tight whitespace-nowrap shrink-0">因子研究</h1>
              {/* 市场切换器：就地复挂全局 MarketSelector（与顶栏/回测中心同一实现，
                  不另造第三份市场 UI）。点即 dispatch(setMarket) → 页面 marketResetRef
                  effect 清旧市场缓存并带新市场重取。包 testid：探针要能确定性地锁定
                  **本页这枚**，而不是拿全页 role 计数去猜（旧版静态标签已由它取代）。 */}
              <div data-testid="research-market-switcher" className="shrink-0">
                <MarketSelector />
              </div>
              <div className="h-4 w-[1px] bg-slate-200 self-center shrink-0" />
              <span className="text-sm font-medium text-slate-500 truncate">因子排行榜 · 单因子体检 · 对比 · 合成 · 因子报告</span>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <div className="flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5">
              {TABS.map((t) => {
                const Icon = t.icon;
                return (
                  <button
                    key={t.key}
                    onClick={() => {
                      // 用户自己点页签 = 一次全新访问，丢掉上次从筛选页带过来的深链目标，
                      // 否则离开「因子报告」再回来会被拽回那个因子。
                      setReportTarget(null);
                      setTab(t.key);
                    }}
                    className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-bold transition-colors ${
                      tab === t.key ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                    }`}
                  >
                    <Icon className="w-3 h-3" />
                    {t.label}
                  </button>
                );
              })}
            </div>
            {!isTool && (
              <>
                <span className="hidden lg:inline-flex items-center gap-1.5 rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-500">
                  <span className="h-1.5 w-1.5 rounded-full bg-indigo-500" />
                  {/* 目录未到时不写死数字：82 是经典库的口径，非 CN 没有经典库 */}
                  {factors.length ? `${available}/${factors.length} 因子可用` : '因子目录加载中'}
                </span>
                {/* 大屏专属：头部左簇加了市场切换器（~240px）后，1280 宽下与页签组
                    相挤（左簇压到 min-content 之下会溢出叠页签）。样本窗字样升到 2xl
                    再显示，与「因子可用」chip 的 lg 档形成两段渐进披露。 */}
                {window_ && (
                  <span className="hidden 2xl:inline-flex items-center rounded-full bg-indigo-50 border border-indigo-100 px-2.5 py-1 text-[11px] font-bold text-indigo-600">
                    样本 {window_[0]} ~ {window_[1]}
                  </span>
                )}
              </>
            )}
          </div>
        </header>

        {/* 未接入市场（当前仅 CRYPTO）：诚实阻断 —— 不发起任何请求、不回退 CN 数据。
            拿沪深300/申万行业这类 A 股口径去给别的市场算超额/IC 是数据污染。 */}
        {!researchMarket && (
          <div className="flex-1 min-h-0 min-w-0 flex items-center justify-center p-6" data-testid="research-market-blocked">
            <div className="max-w-md w-full rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
              <div className="flex items-center gap-2.5 mb-3">
                <div className="w-9 h-9 rounded-xl bg-slate-100 flex items-center justify-center">
                  <Sigma className="w-4.5 h-4.5 text-slate-400" />
                </div>
                <div>
                  <div className="text-sm font-bold text-slate-800">因子研究未接入{marketLabel}市场</div>
                  <div className="text-[11px] text-slate-400">当前工作台只服务 A股 / 港股 / 美股 / 期货</div>
                </div>
              </div>
              <p className="text-xs leading-relaxed text-slate-500">
                本页的排行榜、超额、IC 与因子报告都以<strong className="text-slate-600">该市场自身的基准指数表</strong>为口径；
                该市场尚未接入基准与因子快照，这里不会展示任何数据，也不会用其它市场的数据代填。
              </p>
              <p className="mt-3 text-[11px] text-slate-400">
                请用本页标题旁的市场切换器切到已接入的市场（A股 / 港股 / 美股 / 期货）后再使用本页。
              </p>
            </div>
          </div>
        )}

        {/* 工具页签：整页全宽（原技能中心的因子报告） */}
        {researchMarket && isTool && tab === 'factor-report' && (
          <div className="flex-1 min-h-0 min-w-0 overflow-hidden">
            <FactorReportPanel
              // key 换掉即重挂载：面板只在挂载时读一次深链目标（见它的 props 注释），
              // 用 key 而不是「监听 props 变化」来保证这个约定不被父级的不卸载破坏。
              key={reportTarget ? `${reportTarget.dataset}|${reportTarget.code}` : 'default'}
              initialDataset={reportTarget?.dataset}
              initialCode={reportTarget?.code}
              market={researchMarket}
              // 反跳回单因子分析：资格判据 = catalogIndex（= 单因子页真实打得开的
              // 集合），代码级查表，不按报告数据集名硬映射（两个命名空间不对齐）。
              locateSingle={(code) => catalogIndex.get(code) ?? null}
              openSingle={(loc) => openSingle(loc.code, loc.dataset)}
            />
          </div>
        )}

        {researchMarket && !isTool && (
        <div className="flex-1 min-h-0 min-w-0 flex gap-2 p-3">
          {/* 左侧目录（可收起：给榜单让宽度） */}
          {catalogOpen && (
            <CatalogSidebar
              factors={factors}
              l1Order={l1Order}
              rowsByCode={rowsByCode}
              selected={selected}
              activeCode={activeCode}
              tagFilter={tagFilter}
              loading={catalogLoading}
              categoryFilter={categoryFilter}
              onSelectCategory={handleSelectCategory}
              onToggle={toggleSelected}
              onOpen={openSingle}
            />
          )}

          {/* 主区 */}
          <div className="flex-1 min-w-0 min-h-0 flex flex-col gap-2">
            {/* 数据集 + 区间工具条 */}
            <div className="shrink-0 flex items-center gap-2 flex-wrap rounded-xl border border-slate-200/80 bg-white px-3 py-1.5">
              <button
                data-testid="toggle-catalog"
                onClick={() => setCatalogOpen(!catalogOpen)}
                title={catalogOpen ? '收起目录，给榜单让出宽度' : '展开因子目录'}
                aria-pressed={!catalogOpen}
                className="flex items-center gap-0.5 rounded-lg border border-slate-200 px-1.5 py-0.5 text-[10px] font-bold text-slate-500 hover:border-slate-300 hover:text-slate-700"
              >
                {catalogOpen ? <ChevronsLeft className="w-3 h-3" /> : <ChevronsRight className="w-3 h-3" />}
                目录
              </button>
              <div className="flex items-center gap-0.5 rounded-full bg-slate-100 border border-slate-200 p-0.5">
                {catalogDatasets.map((key) => (
                  <button
                    key={key}
                    onClick={() => setDataset(key)}
                    title={DATASET_LABELS[key].title}
                    className={`rounded-full px-2.5 py-0.5 text-[10px] font-bold transition-colors ${
                      dataset === key ? 'bg-white text-indigo-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                    }`}
                  >
                    {DATASET_LABELS[key].label}
                  </button>
                ))}
              </div>
              <RangePicker windowStr={window_} value={range} onChange={setRange} />
              <div className="flex-1" />
              {tagFilter.length > 0 && (
                <button
                  onClick={() => setTagFilter([])}
                  className="text-[10px] font-bold text-indigo-500 hover:text-indigo-600"
                >
                  清空标签筛选（{tagFilter.length}）
                </button>
              )}
              {rangeMeta?.n_months !== undefined && (
                <span className="text-[10px] font-mono text-slate-400">
                  {rangeMeta.n_months} 个月末
                </span>
              )}
              {rangeMeta?.window && (
                <span
                  className="text-[10px] font-mono text-slate-400"
                  title={
                    rangeMeta.direction_calibration
                      ? `主库 ${rangeMeta.window[0]} ~ ${rangeMeta.window[1]}；方向校准窗 ${rangeMeta.direction_calibration.start} ~ ${rangeMeta.direction_calibration.end}（评估窗口不回算符号）`
                      : `主库 ${rangeMeta.window[0]} ~ ${rangeMeta.window[1]}`
                  }
                >
                  主库 {rangeMeta.window[0]} ~ {rangeMeta.window[1]}
                </span>
              )}
              {rangeMeta?.survivorship_bias && (
                <span
                  className="rounded-full border border-amber-200 bg-amber-50 px-1.5 py-[1px] text-[9px] font-bold text-amber-600"
                  title="该窗口有退市股因子值缺口（>5% 行情池）；只能做研究，不得作为验收依据。详见 coverage.json。"
                >
                  退市覆盖缺口
                </span>
              )}
              {/* 扫描只对私人库有意义：经典库的目录来自内置 catalog.py，
                  没有「盘上有什么」可对，后端也会 400。故按数据集隐藏而不是禁用。 */}
              {dataset === 'private' && (
                <button
                  onClick={() => setPanel(panel === 'scan' ? 'none' : 'scan')}
                  data-testid="open-scan"
                  className={`flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[10px] font-bold ${
                    panel === 'scan'
                      ? 'border-emerald-200 bg-emerald-50 text-emerald-600'
                      : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'
                  }`}
                  title="扫描 quantdb：列出新挖到、还没进快照的因子（只读，不触发重算）"
                >
                  <ScanSearch className="w-3 h-3" /> 扫描
                </button>
              )}
              <button
                onClick={() => (panel === 'snapshot' ? setPanel('none') : openSnapshotManually())}
                className={`flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[10px] font-bold ${
                  panel === 'snapshot'
                    ? 'border-indigo-200 bg-indigo-50 text-indigo-600'
                    : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'
                }`}
                title="查看/重建因子快照（本地计算）"
              >
                <Database className="w-3 h-3" /> 快照
              </button>
            </div>

            {panel === 'snapshot' ? (
              <SnapshotPanel dataset={dataset} market={researchMarket} onReady={handleSnapshotReady} />
            ) : panel === 'scan' ? (
              <ScanPanel
                dataset={dataset}
                market={researchMarket}
                // 重算交给快照面板：进度、日志、轮询都只在那一边实现。
                // 走「手动」入口：用户是冲着「重算」来的，面板不能自己收起来。
                onRebuild={openSnapshotManually}
                onClose={() => setPanel('none')}
              />
            ) : error ? (
              <div className="flex-1 flex flex-col items-center justify-center gap-2">
                <span className="text-sm text-rose-500">{error.slice(0, 240)}</span>
                {errorStatus === 503 ? (
                  <button
                    // 这条链接只出现在「快照缺失」的 503 分支上，与自动弹出同路：
                    // 建完就收起并把榜单带回来。
                    onClick={() => {
                      setSnapshotAutoClose(true);
                      setPanel('snapshot');
                    }}
                    className="text-[11px] font-bold text-indigo-500 hover:text-indigo-600"
                  >
                    → 打开快照计算面板
                  </button>
                ) : (
                  <span className="text-[11px] text-slate-400">
                    若为快照缺失：点击右上角「快照」查看状态并在本地计算
                  </span>
                )}
              </div>
            ) : tab === 'leaderboard' ? (
              <LeaderboardTab
                rows={rows}
                loading={loading}
                error={null}
                selected={selected}
                tagFilter={tagFilter}
                categoryFilter={categoryFilter}
                onClearCategoryFilter={() => setCategoryFilter(null)}
                onClearTagFilter={() => setTagFilter([])}
                dataset={dataset}
                n={lbN}
                onNChange={setLbN}
                onToggle={toggleSelected}
                onToggleTag={toggleTag}
                onSendCompare={() => setTab('compare')}
                onSendCompose={() => setTab('compose')}
                onOpenSingle={openSingle}
                meta={meta}
                dedup={lbDedup}
                market={researchMarket}
                onToggleDedup={() => setLbDedup((v) => !v)}
                onRegisterToTraining={canRegister ? () => setRegisterTarget({ codes: selected, dataset }) : undefined}
              />
            ) : tab === 'single' ? (
              <SingleFactorTab
                code={activeCode}
                range={rangeParams}
                dataset={dataset}
                market={researchMarket}
                // 单因子详情的「因子报告」入口复用筛选页的深链通道（同一个 openReport）。
                onOpenReport={openReport}
              />
            ) : tab === 'compare' ? (
              <CompareTab codes={selected} onRemove={toggleSelected} nameOf={nameOf} range={rangeParams} dataset={dataset} market={researchMarket} />
            ) : tab === 'screening' ? (
              <ScreeningTab
                index={catalogIndex}
                catalogStatus={catalogStatus}
                market={researchMarket}
                selected={screeningSelected}
                onToggle={toggleScreening}
                onToggleMany={toggleScreeningMany}
                onOpenSingle={openSingle}
                onOpenReport={openReport}
                canRegisterToTraining={isAdmin && researchMarket === 'CN'}
                // 筛选的勾选恒为私人库 code（经典库的行根本勾不上），所以目标
                // 数据集写死不跟 dataset 走——否则用户在经典库下勾选会写错库。
                onRegister={() => setRegisterTarget({ codes: screeningSelected, dataset: 'private' })}
              />
            ) : (
              <ComposeTab factors={factors} codes={selected} onChangeCodes={setSelected} range={rangeParams} dataset={dataset} market={researchMarket} />
            )}
          </div>
        </div>
        )}

        {registerTarget && (
          <RegisterToTrainingModal
            codes={registerTarget.codes}
            // 来源库（弹窗按它分组）取自**目标数据集**的目录，不是当前展示的那个：
            // 从筛选页注册时页面可能正停在经典库上。
            factors={catalogCache[registerTarget.dataset] || []}
            dataset={registerTarget.dataset}
            onClose={() => setRegisterTarget(null)}
            // 「去发布」只导航：跳到训练数据集页并预选 market/source，
            // 发布本身仍是那一页的人工闸门（先关弹窗，避免跨页残留）。
            onGoPublish={({ market, source }) => {
              setRegisterTarget(null);
              navigate(
                `/admin/training-datasets?market=${encodeURIComponent(market)}&source=${encodeURIComponent(source)}`,
              );
            }}
          />
        )}
      </div>
    </div>
  );
};

export default FactorResearchPage;
