/**
 * 因子研究 — 独立栏目页（factor-lib-demo 完整复刻 + 因子工具页签）
 *
 * 布局：左侧因子目录（分类树 + 搜索 + 标签筛选）· 顶部区间选择 · 两组页签：
 * 研究：排行榜 / 单因子分析 / 多因子对比 / 多因子合成 / 筛选
 * 工具：因子报告 / 评估中心（2026-09-17 由技能中心迁入，全宽渲染；策略模板已迁至回测中心·策略管理右侧）
 * 数据：/api/v1/factor-research（引擎服务，快照由 build_factor_research.py 构建）
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ArrowRightLeft, Award, BarChart3, Database, Filter, Layers, LineChart, Sigma, TableProperties } from 'lucide-react';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import { ApiError, getCatalog, getLeaderboard } from '../services/factorResearchService';
import type { FactorDataset, RangeParams } from '../services/factorResearchService';
import type { FactorMeta, LeaderboardRow } from '../types/factorResearch';
import { RangePicker } from '../components/common';
import type { RangeValue } from '../components/common';
import { CatalogSidebar } from '../components/CatalogSidebar';
import { LeaderboardTab } from '../components/LeaderboardTab';
import { SingleFactorTab } from '../components/SingleFactorTab';
import { CompareTab } from '../components/CompareTab';
import { ComposeTab } from '../components/ComposeTab';
import { ScreeningTab } from '../components/ScreeningTab';
import { SnapshotPanel } from '../components/SnapshotPanel';
import { FactorReportPanel } from '../components/factor-report/FactorReportPanel';
import { EvalCenterPanel } from '../components/eval-center/EvalCenterPanel';

type Tab = 'leaderboard' | 'single' | 'compare' | 'compose' | 'screening' | 'factor-report' | 'eval';

const TABS: Array<{ key: Tab; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { key: 'leaderboard', label: '排行榜', icon: BarChart3 },
  { key: 'single', label: '单因子分析', icon: TableProperties },
  { key: 'compare', label: '多因子对比', icon: ArrowRightLeft },
  { key: 'compose', label: '多因子合成', icon: Layers },
  { key: 'screening', label: '筛选', icon: Filter },
  { key: 'factor-report', label: '因子报告', icon: LineChart },
  { key: 'eval', label: '评估中心', icon: Award },
];

/** 工具页签：不依赖因子目录/区间工具条，整页全宽渲染 */
const TOOL_TABS: Tab[] = ['factor-report', 'eval'];
const isToolTab = (t: Tab): boolean => TOOL_TABS.includes(t);
const isValidTab = (v: string | null): v is Tab => TABS.some((t) => t.key === v);

const FactorResearchPage: React.FC = () => {
  // 页签支持深链（如评估徽章跳 /factor-research?tab=eval）；切换时同步回 URL
  const [searchParams, setSearchParams] = useSearchParams();
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

  // URL 为准：外部导航（徽章深链等）带 ?tab= 时同步切换页签
  useEffect(() => {
    const fromUrl = searchParams.get('tab');
    if (isValidTab(fromUrl) && fromUrl !== tab) setTabState(fromUrl);
  }, [searchParams, tab]);
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [showSnapshot, setShowSnapshot] = useState(false);
  const [dataset, setDataset] = useState<FactorDataset>('private'); // 默认以 L1+L2 私人因子库为主
  const [reloadKey, setReloadKey] = useState(0);

  const rangeParams: RangeParams = useMemo(
    () => ({ start: range.start, end: range.end }),
    [range.start, range.end],
  );

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getCatalog(dataset)
      .then((cat) => {
        if (!alive) return;
        setFactors(cat.factors);
        setL1Order(cat.l1_order);
        setL2Order(cat.l2_order || {});
        setCatalogMeta(cat.meta);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [dataset]);

  // 切换数据集：清空选择态，避免跨库代码混选
  useEffect(() => {
    setSelected([]);
    setActiveCode(null);
    setTagFilter([]);
  }, [dataset]);

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
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
        setErrorStatus(e instanceof ApiError ? e.status : null);
        if (e instanceof ApiError && e.status === 503) setShowSnapshot(true);
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

  const openSingle = (code: string) => {
    setActiveCode(code);
    setTab('single');
  };

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
              <span className="text-sm font-medium text-slate-500 truncate">因子排行榜 · 单因子体检 · 对比 · 合成 · 因子报告 · 评估中心</span>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <div className="flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5">
              {TABS.map((t) => {
                const Icon = t.icon;
                return (
                  <button
                    key={t.key}
                    onClick={() => setTab(t.key)}
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

        {/* 工具页签：整页全宽（原技能中心的因子报告/评估中心） */}
        {isTool && tab === 'factor-report' && (
          <div className="flex-1 min-h-0 min-w-0 overflow-hidden">
            <FactorReportPanel />
          </div>
        )}

        {isTool && tab !== 'factor-report' && (
          <div className="flex-1 min-h-0 min-w-0 overflow-y-auto p-4">
            <EvalCenterPanel />
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
              <button
                onClick={() => setShowSnapshot(!showSnapshot)}
                className={`flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[10px] font-bold ${
                  showSnapshot
                    ? 'border-indigo-200 bg-indigo-50 text-indigo-600'
                    : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'
                }`}
                title="查看/重建因子快照（本地计算）"
              >
                <Database className="w-3 h-3" /> 快照
              </button>
            </div>

            {showSnapshot ? (
              <SnapshotPanel
                dataset={dataset}
                onReady={() => {
                  setShowSnapshot(false);
                  setReloadKey(reloadKey + 1);
                }}
              />
            ) : error ? (
              <div className="flex-1 flex flex-col items-center justify-center gap-2">
                <span className="text-sm text-rose-500">{error.slice(0, 240)}</span>
                {errorStatus === 503 ? (
                  <button
                    onClick={() => setShowSnapshot(true)}
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
                n={lbN}
                onNChange={setLbN}
                onToggle={toggleSelected}
                onToggleTag={toggleTag}
                onSendCompare={() => setTab('compare')}
                onSendCompose={() => setTab('compose')}
                onOpenSingle={openSingle}
                meta={meta}
              />
            ) : tab === 'single' ? (
              <SingleFactorTab code={activeCode} range={rangeParams} dataset={dataset} />
            ) : tab === 'compare' ? (
              <CompareTab codes={selected} onRemove={toggleSelected} nameOf={nameOf} range={rangeParams} dataset={dataset} />
            ) : tab === 'screening' ? (
              <ScreeningTab />
            ) : (
              <ComposeTab factors={factors} codes={selected} onChangeCodes={setSelected} range={rangeParams} dataset={dataset} />
            )}
          </div>
        </div>
        )}
      </div>
    </div>
  );
};

export default FactorResearchPage;
