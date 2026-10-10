/**
 * 因子研究 — 独立栏目页（factor-lib-demo 完整复刻 + 因子工具页签）
 *
 * 布局：左侧因子目录（分类树 + 搜索 + 标签筛选）· 顶部区间选择 · 两组页签：
 * 研究：排行榜 / 单因子分析 / 多因子对比 / 多因子合成 / 筛选
 * 工具：因子报告（2026-09-17 由技能中心迁入，全宽渲染；策略模板已迁至回测中心·策略管理右侧，评估中心已迁至模拟交易页签）
 * 数据：/api/v1/factor-research（引擎服务，快照由 build_factor_research.py 构建）
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowRightLeft, Award, BarChart3, Database, Filter, Layers, LineChart, ScanSearch, Sigma, TableProperties } from 'lucide-react';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import { ApiError, getCatalog, getLeaderboard } from '../services/factorResearchService';
import type { FactorDataset, RangeParams } from '../services/factorResearchService';
import type { CategoryFilter, FactorMeta, LeaderboardRow } from '../types/factorResearch';
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
import { useAppSelector } from '../../../store';

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
  const [range, setRange] = useState<RangeValue>({ preset: 'all', start: null, end: null });
  const [tagFilter, setTagFilter] = useState<string[]>([]);
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
   * 在途的目录请求。主流程与「补缺的那份」两条路可能同时想要同一个数据集——
   * 进页面就立刻点筛选页签最典型（私人库目录 1.1 MB 还在路上，cache 仍是空的）。
   * 没有这个闸门就会把同一份目录拉两遍，而引擎被并发因子研究请求压垮过一次
   * （2026-09-19，看门狗重启），这里不该再添一份。
   */
  const catalogInflightRef = useRef(new Set<FactorDataset>());
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
  const canRegister = isAdmin && dataset === 'private';

  const rangeParams: RangeParams = useMemo(
    () => ({ start: range.start, end: range.end }),
    [range.start, range.end],
  );

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setCatalogLoading(true);
    catalogInflightRef.current.add(dataset);
    getCatalog(dataset)
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
        catalogInflightRef.current.delete(dataset);
        if (alive) {
          setLoading(false);
          setCatalogLoading(false);
        }
      });
    return () => {
      alive = false;
    };
  }, [dataset]);

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

  // 筛选页签需要**两个**数据集的目录才建得出 name→落点索引，而主流程只拉当前
  // 那一个。缺的那份在这里补，且只补进 cache：factors/l1Order/l2Order 是
  // 「当前数据集」的展示态，不能被一次后台补数改掉。
  // 依赖里的 catalogCache 会让它在补完后重跑一次并立刻空转返回，不会自转。
  useEffect(() => {
    if (tab !== 'screening') return;
    // 在途的跳过：主流程可能正在拉同一个数据集（进页面就点筛选页签），
    // 不跳就会把 1.1 MB 的私人库目录拉两遍。
    const missing = (['private', 'classic'] as FactorDataset[]).filter(
      (d) => !catalogCache[d] && !catalogInflightRef.current.has(d),
    );
    if (missing.length === 0) return;
    let alive = true;
    missing.forEach((d) => catalogInflightRef.current.add(d));
    Promise.all(
      missing.map((d) =>
        getCatalog(d)
          .then((cat) => [d, cat.factors] as const)
          .catch(() => null)
          .finally(() => catalogInflightRef.current.delete(d)),
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
  }, [tab, catalogCache]);

  const catalogIndex = useMemo(() => {
    const idx = new Map<string, FactorLocation>();
    // 索引键必须是目录的 **code**，不是 name_cn：筛选清单里的名字与 code 逐字
    // 相同（实测 362/362），而经典库的 name_cn 是「动量」这类中文名，用它建索引
    // 会一个都对不上。私人库后写：两份目录实测零重叠，万一将来重名，让主库赢。
    (['classic', 'private'] as FactorDataset[]).forEach((ds) => {
      (catalogCache[ds] || []).forEach((f) => idx.set(f.code, { dataset: ds, code: f.code }));
    });
    return idx;
  }, [catalogCache]);

  /** 目录的可信度，交给筛选页决定怎么解释「这行点不进去」。 */
  const catalogStatus: 'loading' | 'degraded' | 'ready' = useMemo(() => {
    const both: FactorDataset[] = ['private', 'classic'];
    if (both.some((d) => catalogOutcome[d] === 'fail')) return 'degraded';
    return both.every((d) => catalogOutcome[d] === 'ok') ? 'ready' : 'loading';
  }, [catalogOutcome]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getLeaderboard(rangeParams, lbN, dataset)
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
  }, [rangeParams, lbN, dataset, reloadKey]);

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
  const rangeMeta = meta?.range as { start?: string; end?: string; n_months?: number } | undefined;

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 bg-gradient-to-br from-indigo-500 to-violet-500 rounded-2xl flex items-center justify-center shadow-lg shrink-0">
              <Sigma className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center gap-2.5 ml-1 min-w-0">
              <h1 className="text-xl font-bold text-slate-800 tracking-tight">因子研究</h1>
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
                  {available}/{factors.length || 82} 因子可用
                </span>
                {window_ && (
                  <span className="hidden xl:inline-flex items-center rounded-full bg-indigo-50 border border-indigo-100 px-2.5 py-1 text-[11px] font-bold text-indigo-600">
                    样本 {window_[0]} ~ {window_[1]}
                  </span>
                )}
              </>
            )}
          </div>
        </header>

        {/* 工具页签：整页全宽（原技能中心的因子报告） */}
        {isTool && tab === 'factor-report' && (
          <div className="flex-1 min-h-0 min-w-0 overflow-hidden">
            <FactorReportPanel
              // key 换掉即重挂载：面板只在挂载时读一次深链目标（见它的 props 注释），
              // 用 key 而不是「监听 props 变化」来保证这个约定不被父级的不卸载破坏。
              key={reportTarget ? `${reportTarget.dataset}|${reportTarget.code}` : 'default'}
              initialDataset={reportTarget?.dataset}
              initialCode={reportTarget?.code}
            />
          </div>
        )}

        {!isTool && (
        <div className="flex-1 min-h-0 min-w-0 flex gap-2 p-3">
          {/* 左侧目录 */}
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

          {/* 主区 */}
          <div className="flex-1 min-w-0 min-h-0 flex flex-col gap-2">
            {/* 数据集 + 区间工具条 */}
            <div className="shrink-0 flex items-center gap-2 flex-wrap rounded-xl border border-slate-200/80 bg-white px-3 py-1.5">
              <div className="flex items-center gap-0.5 rounded-full bg-slate-100 border border-slate-200 p-0.5">
                {([
                  { key: 'classic', label: '经典因子' },
                  { key: 'private', label: '私人因子库' },
                ] as Array<{ key: FactorDataset; label: string }>).map((d) => (
                  <button
                    key={d.key}
                    onClick={() => setDataset(d.key)}
                    className={`rounded-full px-2.5 py-0.5 text-[10px] font-bold transition-colors ${
                      dataset === d.key ? 'bg-white text-indigo-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                    }`}
                  >
                    {d.label}
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
              <SnapshotPanel dataset={dataset} onReady={handleSnapshotReady} />
            ) : panel === 'scan' ? (
              <ScanPanel
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
                n={lbN}
                onNChange={setLbN}
                onToggle={toggleSelected}
                onToggleTag={toggleTag}
                onSendCompare={() => setTab('compare')}
                onSendCompose={() => setTab('compose')}
                onOpenSingle={openSingle}
                meta={meta}
                onRegisterToTraining={canRegister ? () => setRegisterTarget({ codes: selected, dataset }) : undefined}
              />
            ) : tab === 'single' ? (
              <SingleFactorTab code={activeCode} range={rangeParams} dataset={dataset} />
            ) : tab === 'compare' ? (
              <CompareTab codes={selected} onRemove={toggleSelected} nameOf={nameOf} range={rangeParams} dataset={dataset} />
            ) : tab === 'screening' ? (
              <ScreeningTab
                index={catalogIndex}
                catalogStatus={catalogStatus}
                selected={screeningSelected}
                onToggle={toggleScreening}
                onToggleMany={toggleScreeningMany}
                onOpenSingle={openSingle}
                onOpenReport={openReport}
                canRegisterToTraining={isAdmin}
                // 筛选的勾选恒为私人库 code（经典库的行根本勾不上），所以目标
                // 数据集写死不跟 dataset 走——否则用户在经典库下勾选会写错库。
                onRegister={() => setRegisterTarget({ codes: screeningSelected, dataset: 'private' })}
              />
            ) : (
              <ComposeTab factors={factors} codes={selected} onChangeCodes={setSelected} range={rangeParams} dataset={dataset} />
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
