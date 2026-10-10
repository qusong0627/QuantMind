/**
 * 因子池页（P1/P2）：池总览 / 池因子 / 谱系图 / 门禁状态 / 组合实验室 + 刷新运维面板。
 *
 * 数据纪律：
 * - 所有数字缺失一律显 `—`（MISSING_METRIC_TEXT），绝不伪造 0；
 * - 「无面板」是显式状态（只参与公式/任务边），表格与谱系图都如实标注；
 * - 刷新是后台子进程（单飞）：409=已有刷新在跑，按钮置灰靠 running 位；
 *   预演与执行严格两键，执行键带 confirm——刷新会重写池行与谱系边；
 * - 组合实验室（ComboLabTab）自持状态与轮询，作用域随页头市场/股票池走。
 * - 因子分类（总览概览块 / 页签类别 chips / 行内徽章）来自后端单源
 *   （描述标签归一 + 名字兜底，见 factor_classify.py）；前端只展示与下钻，
 *   不另维护类别映射；归类不明 = 「其他」如实呈现。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';
import { Button } from '../components-v2/ui/Button';
import { Badge } from '../components-v2/ui/Badge';
import { ComboLabTab } from '../components-v2/ComboLabTab';
import { FactorPoolGraph } from '../components-v2/FactorPoolGraph';
import { PoolCleanupTab } from '../components-v2/PoolCleanupTab';
import { MISSING_METRIC_TEXT } from '../services-v2/metricRegistry';
import { alphaAgentService } from '../services/alphaAgentService';
import {
  getGateDescriptors,
  getPoolFactors,
  getPoolGraph,
  getPoolOverview,
  getPoolRefreshStatus,
  getUniverses,
  refreshPool,
  unarchivePoolFactors,
  type MiningGateDescriptor,
  type PoolCategoryStat,
  type PoolFactorList,
  type PoolFactorRow,
  type PoolGateOutcome,
  type PoolGraph,
  type PoolOverview,
  type PoolRefreshStatus,
  type PoolSortKey,
} from '../services-v2/api';
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  Eye,
  FlaskConical,
  LayoutGrid,
  Network,
  Play,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Table2,
  X,
} from 'lucide-react';

type PoolTab = 'overview' | 'factors' | 'graph' | 'gates' | 'cleanup' | 'combo';

const TAB_ITEMS: { id: PoolTab; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: 'overview', label: '池总览', icon: LayoutGrid },
  { id: 'factors', label: '池因子', icon: Table2 },
  { id: 'graph', label: '谱系图', icon: Network },
  { id: 'gates', label: '门禁状态', icon: ShieldCheck },
  { id: 'cleanup', label: '清理建议', icon: Archive },
  { id: 'combo', label: '组合实验室', icon: FlaskConical },
];

const DEFAULT_MARKETS = [
  { id: 'a_share', label: 'A股' },
  { id: 'hong_kong', label: '港股' },
  { id: 'us_stock', label: '美股' },
  { id: 'crypto', label: '区块链' },
  { id: 'futures', label: '期货' },
];

const SORT_OPTIONS: { key: PoolSortKey; label: string }[] = [
  { key: 'pool_score', label: '池评分' },
  { key: 'novelty', label: '新颖度' },
  { key: 'ic', label: 'IC' },
  { key: 'times_retrieved', label: '被检索次数' },
  { key: 'updated_at', label: '更新时间' },
  { key: 'created_at', label: '创建时间' },
];

const PAGE_SIZE = 20;
const STATUS_POLL_MS = 5000;

/** 分类色（克制的语义点缀：同 hue 用于总览色点/进度条与行内徽章）。 */
const CATEGORY_TONES: Record<string, { bar: string; chip: string }> = {
  momentum: { bar: 'bg-violet-500', chip: 'border-violet-500/30 bg-violet-500/10 text-violet-700' },
  reversal: { bar: 'bg-amber-500', chip: 'border-amber-500/30 bg-amber-500/10 text-amber-700' },
  volatility: { bar: 'bg-rose-500', chip: 'border-rose-500/30 bg-rose-500/10 text-rose-700' },
  volume: { bar: 'bg-sky-500', chip: 'border-sky-500/30 bg-sky-500/10 text-sky-700' },
  price_volume: { bar: 'bg-cyan-500', chip: 'border-cyan-500/30 bg-cyan-500/10 text-cyan-700' },
  liquidity: { bar: 'bg-teal-500', chip: 'border-teal-500/30 bg-teal-500/10 text-teal-700' },
  overnight: { bar: 'bg-indigo-500', chip: 'border-indigo-500/30 bg-indigo-500/10 text-indigo-700' },
  position: { bar: 'bg-fuchsia-500', chip: 'border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-700' },
  moneyflow: { bar: 'bg-emerald-500', chip: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700' },
  sentiment: { bar: 'bg-orange-500', chip: 'border-orange-500/30 bg-orange-500/10 text-orange-700' },
  interaction: { bar: 'bg-lime-500', chip: 'border-lime-500/30 bg-lime-500/10 text-lime-700' },
  return_dist: { bar: 'bg-blue-500', chip: 'border-blue-500/30 bg-blue-500/10 text-blue-700' },
  valuation: { bar: 'bg-slate-500', chip: 'border-slate-500/30 bg-slate-500/10 text-slate-600' },
  other: { bar: 'bg-slate-400', chip: 'border-slate-400/30 bg-slate-400/10 text-slate-500' },
};

function categoryTone(id: string | null | undefined): { bar: string; chip: string } {
  return (id ? CATEGORY_TONES[id] : undefined) ?? CATEGORY_TONES.other;
}

function fmtShare(share: number): string {
  const pct = share * 100;
  return `${pct >= 10 ? pct.toFixed(0) : pct.toFixed(1)}%`;
}

function categoryChipClass(active: boolean): string {
  return `inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors cursor-pointer ${
    active
      ? 'border-violet-500/40 bg-violet-500/15 text-violet-700'
      : 'border-border/60 bg-secondary/20 text-muted-foreground hover:text-foreground hover:bg-secondary/40'
  }`;
}

function fmtNum(value: number | null | undefined, digits = 4): string {
  return value == null || !Number.isFinite(value) ? MISSING_METRIC_TEXT : value.toFixed(digits);
}

function fmtTime(value: string | null | undefined): string {
  if (!value) return MISSING_METRIC_TEXT;
  const dt = new Date(value);
  return Number.isNaN(dt.getTime()) ? String(value) : dt.toLocaleString('zh-CN');
}

function gateStatusClass(outcome: PoolGateOutcome): string {
  if (outcome.status === 'pass') return 'bg-emerald-500/15 text-emerald-600 border-emerald-500/30';
  if (outcome.status === 'fail') {
    return outcome.mode === 'hard'
      ? 'bg-red-500/15 text-red-600 border-red-500/30'
      : 'bg-amber-500/15 text-amber-600 border-amber-500/30';
  }
  return 'bg-slate-400/15 text-slate-500 border-slate-400/30';
}

const GateChip: React.FC<{ outcome: PoolGateOutcome }> = ({ outcome }) => {
  const icon =
    outcome.status === 'pass' ? '✓' : outcome.status === 'fail' ? '✗' : '–';
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${gateStatusClass(outcome)}`}
      title={`${outcome.label}（${outcome.mode === 'hard' ? '硬门禁' : '软告警'}）：${outcome.message}`}
    >
      {icon} {outcome.label}
      {outcome.observed != null && (
        <span className="font-mono opacity-80">{outcome.observed.toFixed(3)}</span>
      )}
    </span>
  );
};

const KpiTile: React.FC<{ label: string; value: string; hint?: string }> = ({
  label,
  value,
  hint,
}) => (
  <Card className="glass card-hover">
    <CardContent className="p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-bold font-mono text-foreground">{value}</div>
      {hint && <div className="mt-1 text-[11px] text-muted-foreground">{hint}</div>}
    </CardContent>
  </Card>
);

export const FactorPoolPage: React.FC = () => {
  const [market, setMarket] = useState('a_share');
  const [universe, setUniverse] = useState('');
  const [tab, setTab] = useState<PoolTab>('overview');

  const [markets, setMarkets] = useState(DEFAULT_MARKETS);
  const [universes, setUniverses] = useState<{ id: string; name: string }[]>([]);

  const [overview, setOverview] = useState<PoolOverview | null>(null);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [factors, setFactors] = useState<PoolFactorList | null>(null);
  const [factorsError, setFactorsError] = useState<string | null>(null);
  const [sort, setSort] = useState<PoolSortKey>('pool_score');
  const [page, setPage] = useState(0);
  const [includeArchived, setIncludeArchived] = useState(false);
  /** 因子大类过滤（'' = 全部；总览点类别卡/池因子页签 chip 都在改它） */
  const [categoryFilter, setCategoryFilter] = useState('');
  const [restorePending, setRestorePending] = useState<string | null>(null);
  const [graph, setGraph] = useState<PoolGraph | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [gateDescriptors, setGateDescriptors] = useState<MiningGateDescriptor[]>([]);
  const [gateRows, setGateRows] = useState<PoolFactorRow[]>([]);

  const [refreshState, setRefreshState] = useState<PoolRefreshStatus | null>(null);
  const [refreshBusy, setRefreshBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const statusTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  /** 池因子列表请求序号：切类别/市场会在同一次 commit 里连发多个请求，
   *  只有最后一次的响应允许落表（迟到响应会把旧页/旧类别的切片画进新视图）。 */
  const loadSeqRef = useRef(0);

  // ── 静态选项（市场/股票池/门禁描述符）：进页一次 ──
  useEffect(() => {
    alphaAgentService
      .listMarkets()
      .then((ms) =>
        setMarkets(
          ms.length > 0
            ? ms.map((m) => ({ id: m.market_id, label: m.market_name }))
            : DEFAULT_MARKETS,
        ),
      )
      .catch(() => {});
    getUniverses()
      .then((res) => setUniverses(res.data?.universes ?? []))
      .catch(() => {});
    getGateDescriptors()
      .then((res) => setGateDescriptors(res.data?.gates ?? []))
      .catch(() => {});
  }, []);

  // ── 刷新状态轮询：常驻（状态是全局单份，便宜） ──
  const loadRefreshStatus = useCallback(async () => {
    const res = await getPoolRefreshStatus();
    if (res.success && res.data) setRefreshState(res.data);
  }, []);

  useEffect(() => {
    loadRefreshStatus();
    statusTimer.current = setInterval(loadRefreshStatus, STATUS_POLL_MS);
    return () => {
      if (statusTimer.current) clearInterval(statusTimer.current);
      statusTimer.current = null;
    };
  }, [loadRefreshStatus]);

  // ── 作用域数据：总览 + 因子列表（切市场/池都刷新） ──
  const loadOverview = useCallback(async () => {
    const res = await getPoolOverview({ market, universe });
    if (res.success && res.data) {
      setOverview(res.data);
      setOverviewError(null);
    } else {
      setOverviewError(res.error ?? '总览加载失败');
    }
  }, [market, universe]);

  const loadFactors = useCallback(
    async (targetPage: number, targetSort: PoolSortKey) => {
      const seq = ++loadSeqRef.current;
      const res = await getPoolFactors({
        market,
        universe,
        limit: PAGE_SIZE,
        offset: targetPage * PAGE_SIZE,
        sort: targetSort,
        includeArchived,
        category: categoryFilter || undefined,
      });
      if (seq !== loadSeqRef.current) return; // 迟到的旧响应：丢弃，不覆盖新结果
      if (res.success && res.data) {
        setFactors(res.data);
        setFactorsError(null);
      } else {
        setFactorsError(res.error ?? '池因子列表加载失败');
      }
    },
    [market, universe, includeArchived, categoryFilter],
  );

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    loadFactors(page, sort);
  }, [loadFactors, page, sort]);

  useEffect(() => {
    setPage(0);
  }, [market, universe, includeArchived, categoryFilter]);

  // 作用域切换后旧类别过滤无意义（类别计数随市场/池变）→ 清掉
  useEffect(() => {
    setCategoryFilter('');
  }, [market, universe]);

  // ── 归档行恢复（池因子表内联动作；确认动作在「清理建议」tab） ──
  const handleRestoreRow = useCallback(
    async (factorId: string) => {
      setRestorePending(factorId);
      try {
        await unarchivePoolFactors([factorId]);
        await Promise.all([loadFactors(page, sort), loadOverview()]);
      } finally {
        setRestorePending(null);
      }
    },
    [loadFactors, loadOverview, page, sort],
  );

  // ── 谱系图：进入 tab 拉取（避免每个筛选都拖 200 点图） ──
  const loadGraph = useCallback(async () => {
    const res = await getPoolGraph({ market, universe, maxNodes: 200 });
    if (res.success && res.data) {
      setGraph(res.data);
      setGraphError(null);
    } else {
      setGraphError(res.error ?? '谱系图加载失败');
    }
  }, [market, universe]);

  useEffect(() => {
    if (tab === 'graph') loadGraph();
  }, [tab, loadGraph]);

  // ── 门禁 tab：拉一页较大列表做裁决汇总 ──
  const loadGateRows = useCallback(async () => {
    const res = await getPoolFactors({
      market,
      universe,
      limit: 200,
      offset: 0,
      sort: 'updated_at',
    });
    if (res.success && res.data) setGateRows(res.data.items);
  }, [market, universe]);

  useEffect(() => {
    if (tab === 'gates') loadGateRows();
  }, [tab, loadGateRows]);

  // ── 刷新动作：预演/执行两键 ──
  const handleRefresh = useCallback(
    async (dryRun: boolean) => {
      if (
        !dryRun &&
        !window.confirm('执行刷新会重算池状态与谱系边（可能耗时数分钟），确认执行？')
      ) {
        return;
      }
      setRefreshBusy(true);
      setNotice(null);
      try {
        const res = await refreshPool({ market, universe, dryRun });
        if (res.success && res.data?.started) {
          setNotice(dryRun ? '已启动预演刷新（不改库）' : '已启动刷新，完成后自动生效');
        } else {
          setNotice(res.error ?? '刷新启动失败');
        }
      } finally {
        setRefreshBusy(false);
        loadRefreshStatus();
      }
    },
    [market, universe, loadRefreshStatus],
  );

  const totalPages = useMemo(
    () => (factors ? Math.max(1, Math.ceil(factors.total / PAGE_SIZE)) : 1),
    [factors],
  );

  const selectedNode = useMemo(
    () => graph?.nodes.find((n) => n.factorId === selectedNodeId) ?? null,
    [graph, selectedNodeId],
  );

  // ── 因子分类（总览概览块 ↔ 池因子页签的类别过滤共用一份口径） ──
  const categoryStats = useMemo(() => overview?.categoryBreakdown ?? [], [overview]);
  const activeCategoryStat = useMemo(
    () => categoryStats.find((c) => c.category === categoryFilter) ?? null,
    [categoryStats, categoryFilter],
  );
  // 进度条按最大类归一（other 垫底但计数可能最大，不能拿 categoryStats[0] 当上限）
  const maxCategoryCount = useMemo(
    () => Math.max(...categoryStats.map((c) => c.count), 1),
    [categoryStats],
  );
  const handleCategoryDrillDown = useCallback((category: string) => {
    setCategoryFilter(category);
    setTab('factors');
  }, []);

  const refreshRunning = refreshState?.running === true;
  const refreshIsDry = (refreshState?.args as { dry_run?: unknown } | undefined)?.dry_run === true;
  const refreshLabel = useMemo(() => {
    if (!refreshState) return '刷新状态未知';
    if (refreshState.status === 'other_user') return '最近一次刷新由其他用户执行';
    if (refreshState.running) return refreshIsDry ? '预演刷新进行中…' : '刷新执行中…';
    switch (refreshState.status) {
      case 'done':
        return `最近刷新完成（${fmtTime(refreshState.finished_at)}）`;
      case 'failed':
        return `最近刷新失败：${refreshState.error ?? '详见日志'}`;
      case 'running':
        return '刷新进行中…';
      default:
        return '尚未运行过刷新';
    }
  }, [refreshState, refreshIsDry]);

  const gateSummary = useMemo(() => {
    let rejected = 0;
    let softFail = 0;
    let allPass = 0;
    let unevaluated = 0;
    for (const row of gateRows) {
      if (!row.gates) {
        unevaluated += 1;
        continue;
      }
      if (row.gates.rejected) {
        rejected += 1;
        continue;
      }
      if (row.gates.gates.some((g) => g.status === 'fail')) softFail += 1;
      else allPass += 1;
    }
    return { rejected, softFail, allPass, unevaluated };
  }, [gateRows]);

  return (
    <div className="space-y-4">
      {/* 页头：作用域 + 刷新动作 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 mr-auto">
          <div className="relative flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-violet-600 via-purple-600 to-fuchsia-600 text-white shadow-xs">
            <Network className="h-3.5 w-3.5" />
          </div>
          <div>
            <h2 className="text-base font-black text-slate-800 m-0 tracking-tight leading-none">因子池</h2>
            <p className="text-[10px] font-bold text-slate-400 m-0 leading-none mt-1">
              跨任务因子记忆 · 谱系 · 物化门禁 · 组合实验室
            </p>
          </div>
        </div>

        <select
          aria-label="市场"
          value={market}
          onChange={(e) => {
            setMarket(e.target.value);
            setSelectedNodeId(null);
          }}
          className="h-8 rounded-md border border-input bg-background px-2 text-xs"
        >
          {markets.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>

        <select
          aria-label="股票池"
          value={universe}
          onChange={(e) => {
            setUniverse(e.target.value);
            setSelectedNodeId(null);
          }}
          className="h-8 rounded-md border border-input bg-background px-2 text-xs"
        >
          <option value="">全部股票池</option>
          {universes.map((u) => (
            <option key={u.id} value={u.id}>
              {u.name}
            </option>
          ))}
        </select>

        <Button
          variant="outline"
          size="sm"
          disabled={refreshBusy || refreshRunning}
          onClick={() => handleRefresh(true)}
          title="预演：只统计不改库"
        >
          <Eye className="h-3.5 w-3.5 mr-1" /> 预演刷新
        </Button>
        <Button
          variant="primary"
          size="sm"
          disabled={refreshBusy || refreshRunning}
          onClick={() => handleRefresh(false)}
          title="执行：重算池状态与谱系边"
        >
          <RefreshCw className={`h-3.5 w-3.5 mr-1 ${refreshRunning ? 'animate-spin' : ''}`} /> 执行刷新
        </Button>
      </div>

      {/* 刷新状态条 */}
      <Card className="glass">
        <CardContent className="p-3 flex flex-wrap items-center gap-3 text-xs">
          <span
            className={`inline-flex h-2 w-2 rounded-full ${
              refreshRunning ? 'bg-amber-500 animate-pulse' : refreshState?.status === 'failed' ? 'bg-red-500' : 'bg-emerald-500'
            }`}
          />
          <span className="font-medium text-foreground">{refreshLabel}</span>
          {refreshState?.status === 'done' && refreshState.summary ? (
            <span className="text-muted-foreground font-mono">
              {Object.entries(refreshState.summary)
                .filter(([, v]) => typeof v === 'number')
                .map(([k, v]) => `${k}=${v}`)
                .join('  ')}
            </span>
          ) : null}
          {notice && <span className="text-primary font-medium ml-auto">{notice}</span>}
          {refreshState?.log && refreshState.log.lines.length > 0 && (
            <details className="w-full">
              <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
                查看刷新日志（{refreshState.log.lines.length} 行{refreshState.log.truncated ? '，已截断' : ''}）
              </summary>
              <pre className="mt-2 max-h-64 overflow-auto rounded-lg bg-slate-900/95 p-3 text-[11px] leading-relaxed text-slate-200">
                {refreshState.log.lines.join('\n')}
              </pre>
            </details>
          )}
        </CardContent>
      </Card>

      {/* Tab 切换 */}
      <div className="flex items-center gap-1.5">
        {TAB_ITEMS.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setTab(item.id)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold transition-all cursor-pointer ${
                tab === item.id
                  ? 'bg-gradient-to-r from-violet-600 to-purple-600 text-white shadow-xs'
                  : 'text-slate-500 hover:text-slate-800 hover:bg-slate-100/70'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {item.label}
            </button>
          );
        })}
      </div>

      {/* ── 池总览 ── */}
      {tab === 'overview' && (
        <div className="space-y-4">
          {overviewError ? (
            <Card className="glass">
              <CardContent className="p-6 text-sm text-destructive">{overviewError}</CardContent>
            </Card>
          ) : (
            <>
              <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
                <KpiTile
                  label="池内因子"
                  value={overview ? String(overview.total) : MISSING_METRIC_TEXT}
                  hint={
                    overview
                      ? `有面板 ${overview.withPanel}${
                          overview.archivedCount > 0 ? ` · 已归档 ${overview.archivedCount}` : ''
                        }`
                      : undefined
                  }
                />
                <KpiTile
                  label="被检索过的因子"
                  value={overview ? String(overview.retrievedFactors) : MISSING_METRIC_TEXT}
                  hint={overview ? `累计注入 ${overview.retrievedTotal} 次` : undefined}
                />
                <KpiTile
                  label="平均池评分"
                  value={fmtNum(overview?.avgPoolScore)}
                />
                <KpiTile
                  label="池多样性熵"
                  value={fmtNum(overview?.poolDiversity, 3)}
                  hint={overview?.nEff != null ? `有效因子数 ${overview.nEff.toFixed(1)}` : '未算过（跑一次池刷新）'}
                />
                <KpiTile label="平均新颖度" value={fmtNum(overview?.avgNovelty, 3)} />
                <KpiTile
                  label="平均池内相关"
                  value={fmtNum(overview?.avgMaxCorr, 3)}
                  hint="与池内最相似因子的 |ρ| 均值"
                />
              </div>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <KpiTile label="平均 IC" value={fmtNum(overview?.avgIc)} />
                <KpiTile label="平均 ICIR" value={fmtNum(overview?.avgIcir, 3)} />
                <KpiTile label="平均 PFS" value={fmtNum(overview?.avgPfs)} />
              </div>

              {/* 因子分类概览：按大类聚合（计数/占比/均值/代表因子），点类别下钻 */}
              {categoryStats.length > 0 && (
                <Card className="glass">
                  <CardHeader className="pb-2">
                    <div className="flex flex-wrap items-baseline justify-between gap-1">
                      <CardTitle className="text-sm">因子分类概览</CardTitle>
                      <span className="text-[11px] text-muted-foreground">
                        按挖掘描述标签归一（无描述回退因子名）；点击类别下钻到池因子列表
                      </span>
                    </div>
                  </CardHeader>
                  <CardContent>
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
                      {categoryStats.map((cat) => {
                        const tone = categoryTone(cat.category);
                        return (
                          <button
                            key={cat.category}
                            type="button"
                            title={`查看「${cat.label}」因子`}
                            onClick={() => handleCategoryDrillDown(cat.category)}
                            className="group rounded-lg border border-border/60 bg-secondary/20 p-3 text-left transition-all cursor-pointer hover:border-violet-400/50 hover:bg-secondary/40 hover:shadow-xs"
                          >
                            <div className="flex items-center gap-2">
                              <span className={`h-2 w-2 shrink-0 rounded-full ${tone.bar}`} />
                              <span className="text-sm font-bold text-foreground">
                                {cat.label}
                              </span>
                              <span className="ml-auto font-mono text-xs text-muted-foreground">
                                {cat.count} · {fmtShare(cat.share)}
                              </span>
                            </div>
                            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200/80">
                              <div
                                className={`h-full rounded-full ${tone.bar}`}
                                style={{
                                  width: `${Math.max(4, (cat.count / maxCategoryCount) * 100)}%`,
                                }}
                              />
                            </div>
                            <div className="mt-2 flex items-center gap-3 font-mono text-[11px] text-muted-foreground">
                              <span title={`IC 均值（有值样本 ${cat.nIc}/${cat.count}）`}>
                                IC <b className="text-foreground">{fmtNum(cat.avgIc)}</b>
                              </span>
                              <span title={`ICIR 均值（有值样本 ${cat.nIcir}/${cat.count}）`}>
                                ICIR <b className="text-foreground">{fmtNum(cat.avgIcir, 3)}</b>
                              </span>
                              <span title="池评分均值">
                                池分 <b className="text-foreground">{fmtNum(cat.avgPoolScore, 3)}</b>
                              </span>
                            </div>
                            <div className="mt-1.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                              <span className="truncate">
                                代表：
                                {cat.topFactors.length > 0
                                  ? cat.topFactors.join('、')
                                  : MISSING_METRIC_TEXT}
                              </span>
                              <span className="ml-auto shrink-0 inline-flex items-center gap-0.5 font-medium transition-colors group-hover:text-violet-600">
                                下钻 <ChevronRight className="h-3 w-3" />
                              </span>
                            </div>
                          </button>
                        );
                      })}
                    </div>
                  </CardContent>
                </Card>
              )}
              {overview && overview.total === 0 && (
                <Card className="glass">
                  <CardContent className="p-8 text-center text-sm text-muted-foreground">
                    因子池还是空的。回测完成的因子会自动登记进池；历史因子用「执行刷新」批量登记。
                  </CardContent>
                </Card>
              )}
            </>
          )}
        </div>
      )}

      {/* ── 池因子表 ── */}
      {tab === 'factors' && (
        <Card className="glass">
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm">
                池内因子 {factors ? `（${factors.total}）` : ''}
                {activeCategoryStat && (
                  <span className="ml-1.5 font-normal text-muted-foreground">
                    · 类别「{activeCategoryStat.label}」
                  </span>
                )}
              </CardTitle>
              <div className="flex items-center gap-2 text-xs">
                <label className="flex items-center gap-1.5 cursor-pointer text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={includeArchived}
                    onChange={(e) => setIncludeArchived(e.target.checked)}
                  />
                  含已归档
                </label>
                <span className="text-muted-foreground">排序</span>
                <select
                  aria-label="排序"
                  value={sort}
                  onChange={(e) => {
                    setSort(e.target.value as PoolSortKey);
                    setPage(0);
                  }}
                  className="h-7 rounded-md border border-input bg-background px-2 text-xs"
                >
                  {SORT_OPTIONS.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            {categoryStats.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-muted-foreground">类别</span>
                <button
                  type="button"
                  onClick={() => setCategoryFilter('')}
                  className={categoryChipClass(categoryFilter === '')}
                >
                  全部
                </button>
                {categoryStats.map((cat) => (
                  <button
                    key={cat.category}
                    type="button"
                    title={`${cat.count} 个因子 · 占比 ${fmtShare(cat.share)}${includeArchived ? '（不含已归档）' : ''}`}
                    onClick={() =>
                      setCategoryFilter(categoryFilter === cat.category ? '' : cat.category)
                    }
                    className={categoryChipClass(categoryFilter === cat.category)}
                  >
                    <span className={`h-1.5 w-1.5 rounded-full ${categoryTone(cat.category).bar}`} />
                    {cat.label}
                    <span className="font-mono opacity-70">{cat.count}</span>
                  </button>
                ))}
              </div>
            )}
          </CardHeader>
          <CardContent>
            {factorsError ? (
              <div className="p-6 text-sm text-destructive">{factorsError}</div>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/50 text-xs text-muted-foreground">
                        <th className="py-2 px-2 text-left font-medium">因子名</th>
                        <th className="py-2 px-2 text-center font-medium">IC</th>
                        <th className="py-2 px-2 text-center font-medium">ICIR</th>
                        <th className="py-2 px-2 text-center font-medium">PFS</th>
                        <th className="py-2 px-2 text-center font-medium">新颖度</th>
                        <th className="py-2 px-2 text-center font-medium">最大相关</th>
                        <th className="py-2 px-2 text-center font-medium">池评分</th>
                        <th className="py-2 px-2 text-center font-medium">被检索</th>
                        <th className="py-2 px-2 text-center font-medium">面板</th>
                        <th className="py-2 px-2 text-center font-medium">门禁</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(factors?.items ?? []).map((row) => (
                        <tr
                          key={row.factorId}
                          className="border-b border-border/40 last:border-0 hover:bg-muted/40 transition-colors"
                        >
                          <td className="py-2 px-2 max-w-[220px]">
                            <div className="truncate font-medium" title={row.factorFormulation || row.factorName}>
                              {row.factorName}
                            </div>
                            <div className="flex items-center gap-1.5 mt-0.5">
                              <span className="truncate font-mono text-[10px] text-muted-foreground">
                                {row.factorId.slice(0, 12)}
                              </span>
                              {row.categoryLabel && (
                                <span
                                  className={`shrink-0 rounded border px-1 py-0 text-[9px] font-medium ${categoryTone(row.category).chip}`}
                                  title={
                                    row.rawCategoryLabel
                                      ? `原始标签：${row.rawCategoryLabel}`
                                      : '按因子名归类（无描述标签）'
                                  }
                                >
                                  {row.categoryLabel}
                                </span>
                              )}
                              {row.archivedAt != null && (
                                <>
                                  <Badge variant="outline" className="shrink-0 text-[9px] px-1 py-0">
                                    已归档
                                  </Badge>
                                  <button
                                    type="button"
                                    className="shrink-0 text-[10px] font-medium text-primary hover:underline disabled:opacity-50"
                                    disabled={restorePending === row.factorId}
                                    title="恢复：重新参与提示词注入与池视图"
                                    onClick={() => handleRestoreRow(row.factorId)}
                                  >
                                    恢复
                                  </button>
                                </>
                              )}
                            </div>
                          </td>
                          <td className="py-2 px-2 text-center font-mono">{fmtNum(row.ic)}</td>
                          <td className="py-2 px-2 text-center font-mono">{fmtNum(row.icir, 3)}</td>
                          <td className="py-2 px-2 text-center font-mono">{fmtNum(row.pfs)}</td>
                          <td className="py-2 px-2 text-center font-mono">{fmtNum(row.novelty, 3)}</td>
                          <td
                            className="py-2 px-2 text-center font-mono"
                            title={row.maxPoolCorrWith ? `最相似：${row.maxPoolCorrWith}` : undefined}
                          >
                            {fmtNum(row.maxPoolCorr, 3)}
                          </td>
                          <td className="py-2 px-2 text-center font-mono font-bold text-primary">
                            {fmtNum(row.poolScore)}
                          </td>
                          <td className="py-2 px-2 text-center font-mono">
                            {row.timesRetrieved > 0 ? `${row.timesRetrieved} 次` : '—'}
                          </td>
                          <td className="py-2 px-2 text-center">
                            {row.hasPanel ? (
                              <span className="text-emerald-600 text-xs">有</span>
                            ) : (
                              <span className="text-muted-foreground text-xs" title="无面板：仅参与公式/任务边">
                                无
                              </span>
                            )}
                          </td>
                          <td className="py-2 px-2">
                            {row.gates ? (
                              <div className="flex flex-wrap gap-1 justify-center">
                                {row.gates.gates.map((g) => (
                                  <GateChip key={g.key} outcome={g} />
                                ))}
                              </div>
                            ) : (
                              <div className="text-center text-[10px] text-muted-foreground">未物化</div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {(factors?.items ?? []).length === 0 && (
                  <div className="p-8 text-center text-sm text-muted-foreground">
                    {categoryFilter
                      ? `「${activeCategoryStat?.label ?? categoryFilter}」类别下没有符合当前条件的因子`
                      : '没有符合条件的池因子'}
                    {categoryFilter && (
                      <button
                        type="button"
                        className="ml-2 text-primary hover:underline"
                        onClick={() => setCategoryFilter('')}
                      >
                        清除类别过滤
                      </button>
                    )}
                  </div>
                )}
                <div className="flex items-center justify-end gap-2 mt-3 text-xs">
                  <span className="text-muted-foreground">
                    第 {page + 1} / {totalPages} 页
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={page <= 0}
                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                  >
                    <ChevronLeft className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={page + 1 >= totalPages}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    <ChevronRight className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {/* ── 谱系图 ── */}
      {tab === 'graph' && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-3">
          <Card className="glass lg:col-span-3">
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm">因子谱系（节点大小=池评分，颜色=新颖度）</CardTitle>
                <Button variant="ghost" size="sm" onClick={loadGraph}>
                  <RefreshCw className="h-3.5 w-3.5" />
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {graphError ? (
                <div className="p-6 text-sm text-destructive">{graphError}</div>
              ) : (
                <FactorPoolGraph
                  nodes={graph?.nodes ?? []}
                  edges={graph?.edges ?? []}
                  onSelectNode={setSelectedNodeId}
                />
              )}
            </CardContent>
          </Card>
          <Card className="glass">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">图例与说明</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs text-muted-foreground">
              {selectedNode ? (
                <div className="rounded-lg bg-secondary/40 p-3 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-foreground text-sm">{selectedNode.factorName}</span>
                    <button type="button" onClick={() => setSelectedNodeId(null)} className="text-muted-foreground hover:text-foreground">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="font-mono text-[10px]">{selectedNode.factorId}</div>
                  <div className="grid grid-cols-2 gap-1.5 font-mono">
                    <span>池评分</span>
                    <span className="text-right">{fmtNum(selectedNode.poolScore)}</span>
                    <span>新颖度</span>
                    <span className="text-right">{fmtNum(selectedNode.novelty, 3)}</span>
                    <span>ICIR</span>
                    <span className="text-right">{fmtNum(selectedNode.icir, 3)}</span>
                    <span>被检索</span>
                    <span className="text-right">{selectedNode.timesRetrieved} 次</span>
                    <span>面板</span>
                    <span className="text-right">{selectedNode.hasPanel ? '有' : '无'}</span>
                  </div>
                </div>
              ) : (
                <p>点击节点查看因子详情。</p>
              )}
              <div className="space-y-1.5">
                <div className="flex items-center gap-2">
                  <span className="inline-block h-0.5 w-6 rounded bg-amber-500" />
                  值级相关（|ρ|≥0.8，线宽∝|ρ|）
                </div>
                <div className="flex items-center gap-2">
                  <span className="inline-block h-0.5 w-6 rounded bg-violet-500 border-t border-dashed" />
                  公式/语义相似
                </div>
                <div className="flex items-center gap-2">
                  <span className="inline-block h-0.5 w-6 rounded bg-slate-500 border-t border-dotted" />
                  同任务同轮
                </div>
                <div className="flex items-center gap-2">
                  <span className="inline-block h-2.5 w-2.5 rotate-45 rounded-[2px] bg-slate-400" />
                  菱形 = 无面板（仅公式/任务边）
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* ── 门禁状态 ── */}
      {tab === 'gates' && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <KpiTile label="全部通过" value={String(gateSummary.allPass)} />
            <KpiTile label="软告警（记录不拦）" value={String(gateSummary.softFail)} />
            <KpiTile label="硬拒（未入账）" value={String(gateSummary.rejected)} />
            <KpiTile label="未物化（无裁决）" value={String(gateSummary.unevaluated)} hint={`统计范围：最近 ${gateRows.length} 个因子`} />
          </div>

          <Card className="glass">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">门禁规则（物化准入）</CardTitle>
            </CardHeader>
            <CardContent>
              {gateDescriptors.length === 0 ? (
                <div className="text-sm text-muted-foreground">门禁描述符不可用（后端未升级或离线）</div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {gateDescriptors.map((g) => (
                    <div key={g.key} className="rounded-lg bg-secondary/30 p-3 space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm">{g.label}</span>
                        <Badge variant={g.default_mode === 'hard' ? 'destructive' : 'warning'}>
                          {g.default_mode === 'hard' ? '硬门禁' : '软告警'}
                        </Badge>
                        {g.default_threshold != null && (
                          <span className="font-mono text-[11px] text-muted-foreground">
                            阈值 {g.default_threshold}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">{g.description}</p>
                    </div>
                  ))}
                </div>
              )}
              <p className="mt-3 text-[11px] text-muted-foreground flex items-center gap-1">
                <ShieldAlert className="h-3 w-3" />
                软告警只记录并展示；硬门禁失败会拒绝入账（--force 可重做）。阈值调整走 config/factor_mining/plugins.yaml 或环境变量。
              </p>
            </CardContent>
          </Card>

          <Card className="glass">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">最近物化裁决</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {gateRows.filter((r) => r.gates).length === 0 ? (
                <div className="text-sm text-muted-foreground p-4 text-center">
                  还没有物化过的因子（门禁裁决在 rd_mined 物化时产生）
                </div>
              ) : (
                gateRows
                  .filter((r) => r.gates)
                  .slice(0, 30)
                  .map((row) => (
                    <div key={row.factorId} className="rounded-lg bg-secondary/30 p-3">
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="text-sm font-medium truncate max-w-[50%]">{row.factorName}</span>
                        <span className="text-[10px] text-muted-foreground font-mono">
                          {fmtTime(row.updatedAt)}
                        </span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {row.gates!.gates.map((g) => (
                          <GateChip key={g.key} outcome={g} />
                        ))}
                        {row.gates!.rejected && (
                          <Badge variant="destructive">已拒入账</Badge>
                        )}
                      </div>
                    </div>
                  ))
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* ── 清理建议（P3）：只建议不自动删；归档 ≠ 删除 ── */}
      {tab === 'cleanup' && <PoolCleanupTab market={market} universe={universe} />}

      {/* ── 组合实验室 ── */}
      {tab === 'combo' && <ComboLabTab market={market} universe={universe} />}
    </div>
  );
};

export default FactorPoolPage;
