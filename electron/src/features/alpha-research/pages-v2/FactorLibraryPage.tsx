import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';
import { Button } from '../components-v2/ui/Button';
import { Badge } from '../components-v2/ui/Badge';
import { Factor, FactorQuality, UniverseInfo } from '../types-v2';
import type { PageId } from '../components-v2/layout/Layout';
import { formatNumber, getQualityBadgeClass, metricToneClass } from '../utils-v2';
import { formatMetricValue } from '../services-v2/metricRegistry';
import {
  getFactors,
  getFactorDetail,
  getUniverses,
  getFactoryFactors,
  classifyQuality,
  UNIVERSE_LABELS,
  startFactorRecovery,
  getFactorRecoveryStatus,
  type FactorQualityCounts,
  type FactorRecoveryStatus,
} from '../services-v2/api';
import { alphaAgentService, MarketInfo } from '../services/alphaAgentService';
import {
  Database,
  Search,
  Download,
  RefreshCw,
  TrendingUp,
  Code,
  Calendar,
  BarChart3,
  AlertCircle,
  Play,
  X,
  Copy,
  Check,
  List,
  ListFilter,
  LayoutGrid,
} from 'lucide-react';
import { useTaskContext } from '../context-v2/TaskContext';
import { useBacktestQueue, useMaterializeRun } from '../context-v2/RunQueueContext';
import { FactorTable, backtestChip, materializationChip } from '../components-v2/FactorTable';
import { MaterializeBar } from '../components-v2/MaterializeBar';
import { PageHeader } from '../components-v2/layout/PageHeader';

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  crypto: '加密货币',
  hong_kong: '港股',
  us_stock: '美股',
};

const MARKET_COLORS: Record<string, string> = {
  a_share: 'bg-red-500/15 text-red-400 border-red-500/30',
  crypto: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/30',
  hong_kong: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  us_stock: 'bg-green-500/15 text-green-400 border-green-500/30',
};

const QUALITY_SHORT: Record<string, string> = { high: '高', medium: '中', low: '低', unknown: '—' };
const QUALITY_FULL: Record<string, string> = { high: '高质量', medium: '中等质量', low: '低质量', unknown: '质量未知' };

/** 视图持久化：默认列表（用户反馈「现在是方块的、列表不能显示吗」） */
const VIEW_STORAGE_KEY = 'qa_factor_lib_view';
type LibraryView = 'list' | 'cards';

/**
 * 清单单页上限（与后端 Query(le=500) 对齐）。超出单页时由「加载更多」
 * 按服务端 offset 向前翻页；总数/质量统计恒用服务端**全量**口径（apiScope），
 * 不随单页大小漂移——旧实现 200 条硬窗口把「最新 200」当成全库，
 * 既是「为啥就显示 200」的直接原因，也是「越挖、中等因子越少」的假象来源。
 */
const LIBRARY_LIST_LIMIT = 500;

/**
 * 补码评估批次轮询间隔（ms）。批次要跑 45 次 LLM 调用 + 45 次回测（串行、
 * 每秒级到分钟级一条），4s 足够让「当前因子 / done 计数」动起来又不churn；
 * 状态查询是内存快照，开销可忽略。
 */
const RECOVERY_POLL_MS = 4000;

/** /alpha-agent/factors 行 → 列表 Factor（回测指标优先级链；缺失保持
 *  undefined，界面显「—」，禁止 `|| 0` 把「没算过」伪造成「算出来是 0」）。 */
function normalizeLibraryFactor(f: any): Factor {
  const bt = f.backtestResults || {};
  return {
    // 先透传 normalizeAgentFactor 产出的全部键（rre/pfsQuality/annTurnover…），
    // 下面的显式赋值再覆盖需要归一化的字段——漏字段=新指标在列表页静默消失。
    ...f,
    factorId: f.factorId || '',
    factorName: f.factorName || 'Unknown',
    factorExpression: f.factorExpression || '',
    factorDescription: f.factorDescription || '',
    quality: (f.quality || classifyQuality(f.ic)) as FactorQuality,
    market: f.market || f.metadata?.market || undefined,
    universe: f.universe || f.metadata?.universe || undefined,
    ic: (typeof bt['IC'] === 'number' ? bt['IC'] : (f.ic ?? bt['1day.excess_return_without_cost.information_coefficient'])),
    icir: (typeof bt['ICIR'] === 'number' ? bt['ICIR'] : (f.icir ?? bt['1day.excess_return_without_cost.information_coefficient_ir'])),
    rankIc: (typeof bt['Rank IC'] === 'number' ? bt['Rank IC'] : (f.rankIc ?? bt['rank_ic'] ?? bt['1day.excess_return_without_cost.rank_ic'])),
    rankIcir: (typeof bt['Rank ICIR'] === 'number' ? bt['Rank ICIR'] : (f.rankIcir ?? bt['rank_ic_ir'] ?? bt['1day.excess_return_without_cost.rank_ic_ir'])),
    round: f.round || 0,
    direction: String(f.direction ?? ''),
    createdAt: f.createdAt || new Date().toISOString(),
    // Extra fields from API
    backtestResults: f.backtestResults,
    factorFormulation: f.factorFormulation,
    annualReturn: f.annualReturn,
    maxDrawdown: f.maxDrawdown,
    sharpeRatio: f.sharpeRatio,
  };
}

/** 因子工厂产出（只读、共享）：并入列表，禁用回测/物化操作；
 *  工厂只评估了 ic/icir，其余指标**不存在**（旧实现补 0 是伪造）。 */
function normalizeFactoryFactor(f: any, generatedAt: string): Factor {
  return {
    factorId: f.factorId,
    factorName: f.factorName,
    factorExpression: f.factorExpression,
    factorDescription: `因子工厂产出 · 字段 ${f.field || '—'} · 覆盖率 ${(f.coverage * 100).toFixed(0)}%`,
    quality: classifyQuality(f.ic),
    market: 'a_share',
    universe: 'all_a',
    ic: f.ic,
    icir: f.icir,
    round: 0,
    direction: f.ic != null ? (f.ic >= 0 ? '正向' : '反向') : '',
    createdAt: generatedAt || new Date().toISOString(),
    readOnly: true,
    source: 'factor_factory',
    coverage: f.coverage,
  };
}

interface QualityTally {
  total: number;
  high: number;
  medium: number;
  low: number;
  unknown: number;
}

function tallyQuality(rows: Factor[]): QualityTally {
  const counts: QualityTally = { total: rows.length, high: 0, medium: 0, low: 0, unknown: 0 };
  for (const f of rows) {
    const q = f.quality;
    if (q === 'high' || q === 'medium' || q === 'low' || q === 'unknown') counts[q] += 1;
  }
  return counts;
}

// `onNavigate` 收 `PageId` 而不是 `string`：同级的 HomePage / MiningDashboardPage /
// Layout 都是这么写的，只有这里松了一格。松的代价是实打实的——回调最终落到
// `setCurrentPage`，收 `string` 就意味着任何拼错的页面名都能编译通过，
// 然后静默切到一个不存在的页（`currentPage === 'xxx'` 全不命中，白屏）。
export interface FactorLibraryPageProps {
  onNavigate?: (page: PageId) => void;
  /** 「挖掘历史 → 查看结果」带过来的任务过滤：只列该挖掘任务产出的因子 */
  taskFilter?: { taskId: string; label: string } | null;
  /** 清除任务过滤（回到全量清单） */
  onClearTaskFilter?: () => void;
}

export const FactorLibraryPage: React.FC<FactorLibraryPageProps> = ({
  onNavigate,
  taskFilter,
  onClearTaskFilter,
}) => {
  const { attachBacktestTask } = useTaskContext();
  const backtestQueue = useBacktestQueue();
  const materialize = useMaterializeRun();

  const [view, setView] = useState<LibraryView>(() =>
    localStorage.getItem(VIEW_STORAGE_KEY) === 'cards' ? 'cards' : 'list',
  );
  useEffect(() => {
    localStorage.setItem(VIEW_STORAGE_KEY, view);
  }, [view]);

  const [factors, setFactors] = useState<Factor[]>([]);
  const [filteredFactors, setFilteredFactors] = useState<Factor[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [qualityFilter, setQualityFilter] = useState<FactorQuality | 'all'>('all');
  const [marketFilter, setMarketFilter] = useState<string>('all');
  const [universeFilter, setUniverseFilter] = useState<string>('all');
  const [universes, setUniverses] = useState<UniverseInfo[]>([]);
  const [markets, setMarkets] = useState<MarketInfo[]>([]);
  const [selectedFactor, setSelectedFactor] = useState<any | null>(null);
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(new Set());
  const [copiedExpr, setCopiedExpr] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 服务端全量口径：总数 + 四档质量计数（与已加载窗口解耦）；null = 旧后端未提供
  const [apiScope, setApiScope] = useState<{
    total: number;
    qualityCounts: FactorQualityCounts | null;
  } | null>(null);
  // 已取回的挖掘因子行数（= 下一页的 offset；按取回行数推进，不按去重后计数）
  const [apiNextOffset, setApiNextOffset] = useState(0);
  // 补码评估（待评估存量因子：旧批次半成品没有实现代码）
  const [recovery, setRecovery] = useState<FactorRecoveryStatus | null>(null);
  const [isStartingRecovery, setIsStartingRecovery] = useState(false);
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  // 批次 running→终态的那一刻要刷新列表（把新生效的 IC 拉进来）——用 ref 记
  // 上一次状态，避免把副作用写进 setState 更新函数（StrictMode 会双调）。
  const recoveryWasRunning = useRef(false);

  useEffect(() => {
    alphaAgentService.listMarkets().then(setMarkets).catch(() => {});
    getUniverses()
      .then((res) => setUniverses(res.data?.universes ?? []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadFactors();
  }, [marketFilter, universeFilter, taskFilter?.taskId]);

  useEffect(() => {
    filterFactors();
  }, [factors, searchQuery, qualityFilter, marketFilter, universeFilter]);

  const loadFactors = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [resp, factoryResp] = await Promise.all([
        getFactors({
          market: marketFilter !== 'all' ? marketFilter : undefined,
          universe: universeFilter !== 'all' ? universeFilter : undefined,
          // 任务过滤是服务端口径（metadata_json->>'task_id'），不是本地筛
          taskId: taskFilter?.taskId,
          limit: LIBRARY_LIST_LIMIT,
        }),
        // 工厂因子是全库批量产出、不带 task_id——任务过滤下并入会破坏
        // 「只列该任务产出」的语义，所以过滤态不拉工厂清单
        taskFilter?.taskId ? Promise.resolve(null) : getFactoryFactors().catch(() => null),
      ]);
      if (resp.success && resp.data) {
        const apiFactors: Factor[] = resp.data.factors.map(normalizeLibraryFactor);
        const generatedAt = factoryResp?.data?.generatedAt ?? '';
        const factoryFactors: Factor[] = (factoryResp?.data?.factors ?? []).map((f) =>
          normalizeFactoryFactor(f, generatedAt),
        );
        setFactors([...factoryFactors, ...apiFactors]);
        setApiNextOffset(apiFactors.length);
        setApiScope({
          total: resp.data.total,
          qualityCounts: resp.data.qualityCounts ?? null,
        });
      }
    } catch (err: any) {
      console.error('Failed to load factors from API:', err);
      const status = err?.response?.status;
      if (status === 401 || status === 403) {
        setError('登录已过期，请刷新页面重新登录。');
      } else if (status) {
        setError(`后端返回错误 (HTTP ${status})。请稍后重试。`);
      } else {
        setError('无法连接 AlphaAgent 接口，请检查网络或登录状态。');
      }
      // Show empty state with error message instead of mock data
      setFactors([]);
      setApiScope(null);
      setApiNextOffset(0);
    } finally {
      setIsLoading(false);
    }
  }, [marketFilter, universeFilter, taskFilter?.taskId]);

  // 「加载更多」：按创建时间倒序向前翻页（服务端 offset），追加并按 factorId
  // 去重（并发新挖掘会让窗口边界轻微漂移，重复行在这里吞掉）。失败不清列表
  // ——已加载内容是用户正在看的，清掉等于把失败代价翻倍；按钮可重试。
  const loadMoreFactors = useCallback(async () => {
    if (isLoadingMore) return;
    setIsLoadingMore(true);
    try {
      const resp = await getFactors({
        market: marketFilter !== 'all' ? marketFilter : undefined,
        universe: universeFilter !== 'all' ? universeFilter : undefined,
        taskId: taskFilter?.taskId,
        limit: LIBRARY_LIST_LIMIT,
        offset: apiNextOffset,
      });
      if (resp.success && resp.data) {
        const rows = resp.data.factors.map(normalizeLibraryFactor);
        setFactors((prev) => {
          const seen = new Set(prev.map((f) => f.factorId));
          return [...prev, ...rows.filter((f) => !seen.has(f.factorId))];
        });
        setApiNextOffset((c) => c + rows.length);
        setApiScope({
          total: resp.data.total,
          qualityCounts: resp.data.qualityCounts ?? null,
        });
      }
    } catch (err) {
      console.error('[alpha-research] load more factors failed:', err);
    } finally {
      setIsLoadingMore(false);
    }
  }, [marketFilter, universeFilter, taskFilter?.taskId, apiNextOffset, isLoadingMore]);

  // 补码评估进度轮询：挂载先主动拉一次（页面切走再回来也能接上已在跑的批次），
  // running 时每 4s 跟一次；转终态那一刻刷新列表（新 IC 进瓦片/页签计数）。
  const recoveryRunning = recovery?.running ?? false;
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      const res = await getFactorRecoveryStatus();
      if (cancelled) return;
      if (!res.success || !res.data) {
        if (recoveryRunning) setRecoveryError(res.error ?? '查询补码评估进度失败');
        return;
      }
      setRecoveryError(null);
      setRecovery(res.data);
      if (recoveryWasRunning.current && !res.data.running) {
        void loadFactors();
      }
      recoveryWasRunning.current = res.data.running;
    };
    void tick();
    const timer = recoveryRunning
      ? window.setInterval(() => {
          void tick();
        }, RECOVERY_POLL_MS)
      : undefined;
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [recoveryRunning, loadFactors]);

  const filterFactors = () => {
    let filtered = factors;
    if (marketFilter !== 'all') {
      filtered = filtered.filter((f) => f.market === marketFilter);
    }
    if (universeFilter !== 'all') {
      filtered = filtered.filter((f) => f.universe === universeFilter);
    }
    if (qualityFilter !== 'all') {
      filtered = filtered.filter((f) => f.quality === qualityFilter);
    }
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (f) =>
          f.factorName.toLowerCase().includes(query) ||
          f.factorExpression.toLowerCase().includes(query) ||
          f.factorDescription.toLowerCase().includes(query)
      );
    }
    setFilteredFactors(filtered);
  };

  // 清单变化时清掉已消失的勾选
  useEffect(() => {
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      const present = new Set(filteredFactors.map((f) => f.factorId));
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (present.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [filteredFactors]);

  const selectableIds = useMemo(
    () => filteredFactors.filter((f) => !f.ownerless && !f.readOnly).map((f) => f.factorId),
    [filteredFactors],
  );
  const selectedInLibrary = useMemo(
    () => filteredFactors.filter((f) => selectedIds.has(f.factorId)),
    [filteredFactors, selectedIds],
  );

  const handleSettledRefresh = useCallback(() => {
    void loadFactors();
  }, [loadFactors]);

  const handleToggleSelect = useCallback((factorId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(factorId)) next.delete(factorId);
      else next.add(factorId);
      return next;
    });
  }, []);

  const handleToggleSelectAll = useCallback(
    (checked: boolean) => {
      setSelectedIds(checked ? new Set(selectableIds) : new Set());
    },
    [selectableIds],
  );

  const handleClearSelection = useCallback(() => setSelectedIds(new Set()), []);

  // 行/卡片级回测：真入队（并发 2），行内状态在表/卡片上就地显示
  const handleBacktest = useCallback(
    (factorId: string) => {
      backtestQueue.enqueue([factorId], { onSettled: handleSettledRefresh });
    },
    [backtestQueue, handleSettledRefresh],
  );

  // 「看图表」：把既有回测载入回测页（不重跑），然后导航
  const handleViewBacktest = useCallback(
    (factorId: string) => {
      void attachBacktestTask(factorId).catch((err) => {
        console.error('[alpha-research] attach backtest failed:', err);
      });
      onNavigate?.('backtest');
    },
    [attachBacktestTask, onNavigate],
  );

  const handleMaterialize = useCallback(
    (factorId: string) => {
      void materialize
        .start([factorId], { onCompleted: handleSettledRefresh })
        .catch(() => {});
    },
    [materialize, handleSettledRefresh],
  );

  const copyToClipboard = async (text: string): Promise<boolean> => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      return true;
    } catch {
      return false;
    }
  };

  const handleCopyExpression = async () => {
    const expr = selectedFactor?.factorExpression || selectedFactor?.factor_expression || '';
    if (!expr) return;
    if (await copyToClipboard(expr)) {
      setCopiedExpr(true);
      setTimeout(() => setCopiedExpr(false), 1500);
    }
  };

  const handleExport = () => {
    const dataStr = JSON.stringify(factors, null, 2);
    const blob = new Blob([dataStr], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `factors_${new Date().toISOString().split('T')[0]}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const handleSelectFactor = async (factor: Factor) => {
    // 工厂因子只读、无明细接口，直接用列表数据
    if (factor.readOnly) {
      setSelectedFactor(factor);
      return;
    }
    // Try to load full detail from API
    try {
      const resp = await getFactorDetail(factor.factorId);
      if (resp.success && resp.data?.factor) {
        setSelectedFactor({ ...factor, ...resp.data.factor });
        return;
      }
    } catch {
      // fallback
    }
    setSelectedFactor(factor);
  };

  // 统计瓦片/页签计数恒用「全量口径」：服务端同一过滤域的 quality_counts
  // （与列表窗口解耦）+ 工厂清单本地计数（按当前市场/股票池取子集，与列表
  // 口径一致）。拿窗口长度当总数会随挖掘进度越挖越「少」——那是老因子被
  // 挤出可视窗口，不是质量下降。服务端字段缺失（旧后端/异常）才退回窗口
  // 计数——宁可退回旧口径，不编造数字。
  const stats = useMemo(() => {
    const factoryRows = factors.filter(
      (f) =>
        f.source === 'factor_factory' &&
        (marketFilter === 'all' || f.market === marketFilter) &&
        (universeFilter === 'all' || f.universe === universeFilter),
    );
    const factory = tallyQuality(factoryRows);
    const base = apiScope?.qualityCounts
      ? { ...apiScope.qualityCounts, total: apiScope.total }
      : tallyQuality(factors.filter((f) => f.source !== 'factor_factory'));
    return {
      total: base.total + factory.total,
      high: base.high + factory.high,
      medium: base.medium + factory.medium,
      low: base.low + factory.low,
      unknown: base.unknown + factory.unknown,
    };
  }, [apiScope, factors, marketFilter, universeFilter]);

  // 发起补码评估批次（stats 之后声明：确认文案要带「待评估 N」，而 stats 是
  // 块级 const——回调放前面会触发 TS 的 use-before-declaration）。
  const handleStartRecovery = useCallback(async () => {
    if (isStartingRecovery || recoveryRunning) return;
    // 45 条 LLM 调用 + 回测不是小动作，先让用户确认（文案带清数量与去向）
    const confirmed = window.confirm(
      `将为 ${stats.unknown} 个「待评估」因子逐条用 AI 补全实现代码，并自动回测补 IC。\n` +
        '后台串行执行（每条约 1-2 分钟，可离开本页），完成后自动归入高/中/低档，可用于物化与训练。',
    );
    if (!confirmed) return;
    setIsStartingRecovery(true);
    setRecoveryError(null);
    try {
      const res = await startFactorRecovery();
      if (res.success && res.data) {
        setRecovery(res.data);
        recoveryWasRunning.current = res.data.running;
      } else {
        // 412（未配置 LLM Key）等 detail 原文上屏——补 Key 入口就在提示里
        setRecoveryError(res.error ?? '发起补码评估失败');
      }
    } finally {
      setIsStartingRecovery(false);
    }
  }, [isStartingRecovery, recoveryRunning, stats.unknown]);

  const StatTile = ({
    icon: Icon,
    label,
    value,
    tone,
    iconTone,
    sub,
  }: {
    icon: typeof BarChart3;
    label: string;
    value: number;
    tone: string;
    iconTone: string;
    /** 补充说明（如「含待评估 N」）——让四档与总数对得上账 */
    sub?: string;
  }) => (
    <Card className="glass card-hover">
      <CardContent className="flex h-[96px] flex-col items-center justify-center gap-0.5 p-3 text-center">
        <div className={`rounded-lg p-1.5 ${iconTone}`}>
          <Icon className="h-4 w-4" />
        </div>
        <div className="text-[11px] text-muted-foreground">{label}</div>
        <div className={`text-lg font-bold leading-tight ${tone}`}>{value}</div>
        {sub && <div className="text-[10px] text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );

  const emptyListText = isLoading
    ? '加载中…'
    : searchQuery || qualityFilter !== 'all'
      ? '没有符合筛选条件的因子'
      : taskFilter
        ? '该任务没有已落库的因子（可能尚未物化或被门禁拒绝）'
        : '开始挖掘因子后，结果将显示在这里';

  return (
    <div className="space-y-4 animate-fade-in-up">
      <PageHeader
        icon={Database}
        title="因子库"
        subtitle="浏览与管理挖掘因子（含因子工厂批量产出，只读）"
        actions={
          <>
            <div
              className="inline-flex items-center rounded-md border border-input p-0.5"
              role="group"
              aria-label="视图切换"
            >
              <button
                type="button"
                onClick={() => setView('list')}
                aria-pressed={view === 'list'}
                title="列表视图"
                className={`flex h-6 w-6 items-center justify-center rounded ${
                  view === 'list'
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-muted/60'
                }`}
              >
                <List className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => setView('cards')}
                aria-pressed={view === 'cards'}
                title="卡片视图"
                className={`flex h-6 w-6 items-center justify-center rounded ${
                  view === 'cards'
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-muted/60'
                }`}
              >
                <LayoutGrid className="h-3.5 w-3.5" />
              </button>
            </div>
            <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={loadFactors} disabled={isLoading}>
              <RefreshCw className={`h-3.5 w-3.5 mr-1 ${isLoading ? 'animate-spin' : ''}`} />
              刷新
            </Button>
            <Button variant="primary" size="sm" className="h-7 px-2 text-xs" onClick={handleExport}>
              <Download className="h-3.5 w-3.5 mr-1" />
              导出 JSON
            </Button>
          </>
        }
      />

      {/* Error Banner */}
      {error && (
        <div className="glass rounded-lg p-3 flex items-center gap-3 bg-warning/10 border-warning/50">
          <AlertCircle className="h-4 w-4 text-warning flex-shrink-0" />
          <span className="text-xs text-warning">{error}</span>
        </div>
      )}

      {/* 任务过滤横幅（挖掘历史 → 查看结果） */}
      {taskFilter && (
        <div className="glass rounded-lg p-3 flex items-center gap-3 bg-primary/5 border-primary/30">
          <ListFilter className="h-4 w-4 text-primary flex-shrink-0" />
          <span className="min-w-0 flex-1 truncate text-xs text-foreground" title={`任务 ${taskFilter.taskId}`}>
            只显示挖掘任务「{taskFilter.label}」产出的因子
            <span className="ml-2 font-mono text-[10px] text-muted-foreground">
              {taskFilter.taskId.slice(0, 8)}
            </span>
          </span>
          <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={onClearTaskFilter}>
            <X className="h-3 w-3 mr-1" />
            清除过滤
          </Button>
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatTile
          icon={BarChart3}
          label="总因子数"
          value={stats.total}
          tone=""
          iconTone="bg-primary/20 text-primary"
          sub={stats.unknown > 0 ? `含待评估 ${stats.unknown}` : undefined}
        />
        <StatTile icon={TrendingUp} label="高质量" value={stats.high} tone="text-success" iconTone="bg-success/20 text-success" />
        <StatTile icon={BarChart3} label="中等质量" value={stats.medium} tone="text-warning" iconTone="bg-warning/20 text-warning" />
        <StatTile icon={BarChart3} label="低质量" value={stats.low} tone="text-destructive" iconTone="bg-destructive/20 text-destructive" />
      </div>

      {/* Filters */}
      <Card className="glass">
        <CardContent className="p-3">
          <div className="flex flex-col gap-2.5">
            {/* Market filter */}
            <div className="flex flex-wrap gap-1.5">
              <Button
                variant={marketFilter === 'all' ? 'primary' : 'outline'}
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => setMarketFilter('all')}
              >
                全部市场
              </Button>
              {markets.map((m) => (
                <Button
                  key={m.market_id}
                  variant={marketFilter === m.market_id ? 'primary' : 'outline'}
                  size="sm"
                  className="h-7 px-2 text-xs"
                  onClick={() => setMarketFilter(m.market_id)}
                >
                  {m.market_name}
                </Button>
              ))}
            </div>
            {/* Universe filter — A-share stock pools */}
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-muted-foreground mr-1">股票池</span>
              <Button
                variant={universeFilter === 'all' ? 'primary' : 'outline'}
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => setUniverseFilter('all')}
              >
                全部
              </Button>
              {(universes.length > 0
                ? universes
                : (Object.keys(UNIVERSE_LABELS) as Array<keyof typeof UNIVERSE_LABELS>).map(
                    (id) => ({ id, name: UNIVERSE_LABELS[id], stockCount: 0, indexSymbol: null }),
                  )
              ).map((u) => (
                <Button
                  key={u.id}
                  variant={universeFilter === u.id ? 'primary' : 'outline'}
                  size="sm"
                  className="h-7 px-2 text-xs"
                  onClick={() => setUniverseFilter(u.id)}
                >
                  {u.name}
                </Button>
              ))}
            </div>
            {/* Search + quality filter */}
            <div className="flex flex-col md:flex-row gap-3">
              <div className="flex-1">
                <div className="relative">
                  <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="搜索因子名称、表达式或描述..."
                    className="h-8 w-full pl-8 pr-3 rounded-md border border-input bg-background text-xs focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                  />
                </div>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {(
                  [
                    ['all', `全部 (${stats.total})`],
                    ['high', `高质量 (${stats.high})`],
                    ['medium', `中等 (${stats.medium})`],
                    ['low', `低质量 (${stats.low})`],
                    ['unknown', `待评估 (${stats.unknown})`],
                  ] as Array<[FactorQuality | 'all', string]>
                ).map(([value, label]) => (
                  <Button
                    key={value}
                    variant={qualityFilter === value ? 'primary' : 'outline'}
                    size="sm"
                    className="h-7 px-2 text-xs"
                    onClick={() => setQualityFilter(value)}
                  >
                    {label}
                  </Button>
                ))}
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 窗口提示：单页装不下时给出口——「最新 N 条」不是全部 */}
      {apiScope && apiScope.total > apiNextOffset && (
        <div className="glass rounded-lg p-3 flex items-center gap-3 bg-warning/10 border-warning/50">
          <AlertCircle className="h-4 w-4 text-warning flex-shrink-0" />
          <span className="min-w-0 flex-1 text-xs text-foreground">
            共 {apiScope.total} 个因子，列表当前加载最新 {apiNextOffset} 个（按创建时间倒序）
          </span>
          <Button
            variant="outline"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={loadMoreFactors}
            disabled={isLoadingMore}
          >
            {isLoadingMore ? '加载中…' : `加载更多（还有 ${apiScope.total - apiNextOffset} 个）`}
          </Button>
        </div>
      )}

      {/* 补码评估：待评估因子多是旧批次半成品（无实现代码），行内「回测」用不了——
          这里给出批次入口与进度（服务端串行补码+回测，完成后归入高/中/低档） */}
      {(recoveryError ||
        recoveryRunning ||
        recovery?.message ||
        stats.unknown > 0) && (
        <div className="glass rounded-lg p-3 flex flex-wrap items-center gap-3 bg-primary/5 border-primary/30">
          <Play className="h-4 w-4 text-primary flex-shrink-0" />
          <span className="min-w-0 flex-1 text-xs text-foreground">
            {recoveryRunning && recovery ? (
              <>
                补码评估进行中 {recovery.done + recovery.failed}/{recovery.total}
                {recovery.currentFactorName ? ` · 当前：${recovery.currentFactorName}` : ''}
                {recovery.failed > 0 ? `（失败 ${recovery.failed}，原因见各行）` : ''}
                {recovery.skipped > 0 ? `（跳过 ${recovery.skipped}）` : ''}
              </>
            ) : recoveryError ? (
              <span className="text-warning">{recoveryError}</span>
            ) : recovery?.message ? (
              recovery.message
            ) : (
              <>
                {stats.unknown} 个因子从未评估：多为旧批次半成品（只存了公式、没有实现代码），
                行内「回测」无法使用。将按公式用 AI 补全代码并自动回测，完成后归入高/中/低档、可用于物化与训练。
              </>
            )}
          </span>
          {recoveryRunning ? (
            <RefreshCw className="h-3.5 w-3.5 animate-spin text-primary" />
          ) : stats.unknown > 0 ? (
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={handleStartRecovery}
              disabled={isStartingRecovery}
            >
              {isStartingRecovery ? '发起中…' : '补全代码并评估'}
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              aria-label="关闭"
              onClick={() => {
                setRecovery(null);
                setRecoveryError(null);
              }}
            >
              <X className="h-3 w-3" />
            </Button>
          )}
        </div>
      )}

      {/* Factor List */}
      {view === 'list' ? (
        <Card className="glass">
          <CardContent className="p-0">
            <MaterializeBar
              selected={selectedInLibrary}
              onClearSelection={handleClearSelection}
              onSettledRefresh={handleSettledRefresh}
            />
            <FactorTable
              factors={filteredFactors}
              selectedIds={selectedIds}
              onToggleSelect={handleToggleSelect}
              onToggleSelectAll={handleToggleSelectAll}
              backtestEntries={backtestQueue.entries}
              materializingIds={materialize.runningIds}
              materializeRunning={materialize.running}
              onOpenDetail={(factorId) => {
                const f = filteredFactors.find((x) => x.factorId === factorId);
                if (f) void handleSelectFactor(f);
              }}
              onBacktest={handleBacktest}
              onMaterialize={handleMaterialize}
              onViewBacktest={handleViewBacktest}
              emptyText={emptyListText}
            />
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3 gap-3">
            {filteredFactors.map((factor) => {
              const btEntry = backtestQueue.entries[factor.factorId];
              const btChip = backtestChip(btEntry);
              const matChip = materializationChip(
                factor,
                Boolean(materialize.running && materialize.runningIds.has(factor.factorId)),
              );
              return (
                <Card
                  key={factor.factorId}
                  className="glass card-hover cursor-pointer"
                  onClick={() => handleSelectFactor(factor)}
                >
                  <CardHeader className="px-3 pb-2 pt-3">
                    <div className="flex items-start justify-between gap-2">
                      <CardTitle className="text-sm">{factor.factorName}</CardTitle>
                      <span className="flex shrink-0 items-center gap-1">
                        {matChip && (
                          <span
                            className={`rounded border px-1 text-[9px] leading-4 ${matChip.className}`}
                            title={matChip.title}
                          >
                            {matChip.text}
                          </span>
                        )}
                        {btChip && (
                          <span
                            className={`rounded border px-1 text-[9px] leading-4 ${btChip.className}`}
                            title={btChip.title}
                          >
                            {btChip.text}
                          </span>
                        )}
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                      <Badge className={getQualityBadgeClass(factor.quality)}>
                        {QUALITY_SHORT[factor.quality] ?? factor.quality}
                      </Badge>
                      {factor.readOnly && (
                        <span className="inline-flex items-center rounded-md border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                          工厂
                        </span>
                      )}
                      {factor.market && (
                        <span className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${MARKET_COLORS[factor.market] || 'bg-secondary text-muted-foreground'}`}>
                          {MARKET_LABELS[factor.market] || factor.market}
                        </span>
                      )}
                      {factor.round > 0 && (
                        <span className="text-[10px] text-muted-foreground">Round {factor.round}</span>
                      )}
                      {factor.direction && (
                        <span className="text-[10px] text-muted-foreground">方向 {factor.direction}</span>
                      )}
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2 px-3 pb-3">
                    <p className="text-[11px] text-muted-foreground line-clamp-2">
                      {factor.factorDescription || '—'}
                    </p>
                    <div className="rounded-md bg-secondary/30 p-2">
                      <div className="flex items-center gap-1.5 mb-1">
                        <Code className="h-3 w-3 text-muted-foreground" />
                        <span className="text-[10px] text-muted-foreground">表达式</span>
                      </div>
                      <code className="text-[11px] font-mono line-clamp-2 break-all">
                        {factor.factorExpression || '—'}
                      </code>
                    </div>
                    <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
                      <div className="flex items-baseline justify-between gap-1">
                        <span className="text-muted-foreground">IC</span>
                        <span className={`font-mono tabular-nums ${metricToneClass(factor.ic)}`}>
                          {formatMetricValue('ic', factor.ic)}
                        </span>
                      </div>
                      <div className="flex items-baseline justify-between gap-1">
                        <span className="text-muted-foreground">RankIC</span>
                        <span className={`font-mono tabular-nums ${metricToneClass(factor.rankIc)}`}>
                          {formatMetricValue('rank_ic', factor.rankIc)}
                        </span>
                      </div>
                      <div className="flex items-baseline justify-between gap-1">
                        <span className="text-muted-foreground">ICIR</span>
                        <span className={`font-mono tabular-nums ${metricToneClass(factor.icir)}`}>
                          {formatMetricValue('icir', factor.icir)}
                        </span>
                      </div>
                      <div className="flex items-baseline justify-between gap-1">
                        <span className="text-muted-foreground">RankICIR</span>
                        <span className={`font-mono tabular-nums ${metricToneClass(factor.rankIcir)}`}>
                          {formatMetricValue('rank_icir', factor.rankIcir)}
                        </span>
                      </div>
                    </div>
                    {/* Action buttons */}
                    <div className="flex items-center gap-1 pt-1 border-t border-border/30">
                      {factor.readOnly ? (
                        <span className="text-[10px] text-muted-foreground flex-1">
                          工厂因子为批量产出（只读），请在训练/特征目录中使用
                        </span>
                      ) : btEntry?.status === 'completed' ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-6 text-[11px] flex-1 px-2 text-emerald-600"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleViewBacktest(factor.factorId);
                          }}
                        >
                          <Play className="h-3 w-3 mr-1" /> 看图表
                        </Button>
                      ) : (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-6 text-[11px] flex-1 px-2"
                          disabled={btEntry?.status === 'running' || btEntry?.status === 'queued'}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleBacktest(factor.factorId);
                          }}
                        >
                          <Play className="h-3 w-3 mr-1" />
                          {btEntry?.status === 'running' ? '回测中…' : '回测'}
                        </Button>
                      )}
                    </div>
                    {factor.createdAt && (
                      <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                        <Calendar className="h-3 w-3" />
                        {new Date(factor.createdAt).toLocaleString('zh-CN')}
                      </div>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>

          {/* Empty State（列表视图由表格自带的空态承担） */}
          {filteredFactors.length === 0 && !isLoading && (
            <Card className="glass">
              <CardContent className="p-12 text-center">
                <Database className="h-16 w-16 mx-auto text-muted-foreground mb-4" />
                <h3 className="text-lg font-medium mb-2">暂无因子</h3>
                <p className="text-sm text-muted-foreground">{emptyListText}</p>
              </CardContent>
            </Card>
          )}
        </>
      )}

      {/* Factor Detail Modal */}
      {selectedFactor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-6"
          onClick={() => setSelectedFactor(null)}
        >
          <Card
            className="glass-strong max-w-3xl w-full max-h-[80vh] overflow-y-auto animate-scale-in"
            onClick={(e: React.MouseEvent) => e.stopPropagation()}
          >
            <CardHeader>
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <CardTitle className="text-xl">
                    {selectedFactor.factorName || selectedFactor.factor_name}
                  </CardTitle>
                  <div className="flex items-center gap-2 mt-2">
                    <Badge className={getQualityBadgeClass(selectedFactor.quality || 'unknown')}>
                      {QUALITY_FULL[selectedFactor.quality] ?? '质量未知'}
                    </Badge>
                    {(selectedFactor.market || selectedFactor.metadata?.market) && (
                      <span className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${MARKET_COLORS[selectedFactor.market || selectedFactor.metadata?.market] || 'bg-secondary text-muted-foreground'}`}>
                        {MARKET_LABELS[selectedFactor.market || selectedFactor.metadata?.market] || selectedFactor.market || selectedFactor.metadata?.market}
                      </span>
                    )}
                    {(selectedFactor.universe || selectedFactor.metadata?.universe) && (
                      <span className="inline-flex items-center rounded-md border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                        {UNIVERSE_LABELS[
                          (selectedFactor.universe || selectedFactor.metadata?.universe) as keyof typeof UNIVERSE_LABELS
                        ] || selectedFactor.universe || selectedFactor.metadata?.universe}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-2">
                    质量按 |IC| 分级（≥0.05 高 / ≥0.02 中）；IC 为负表示因子与收益负相关，可反向使用。
                  </p>
                </div>
                <Button variant="ghost" onClick={() => setSelectedFactor(null)}>
                  <X className="w-4 h-4" />
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Description */}
              <div>
                <h4 className="text-sm font-medium mb-2">因子描述</h4>
                <p className="text-sm text-muted-foreground">
                  {selectedFactor.factorDescription || selectedFactor.factor_description || '无描述'}
                </p>
              </div>

              {/* Expression */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-sm font-medium">因子表达式</h4>
                  <button
                    type="button"
                    onClick={handleCopyExpression}
                    className="inline-flex items-center gap-1 rounded-md border border-border/60 px-2 py-1 text-xs text-muted-foreground hover:text-primary hover:bg-secondary/40 transition-colors"
                    title="拷贝因子表达式"
                  >
                    {copiedExpr ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
                    {copiedExpr ? '已拷贝' : '拷贝'}
                  </button>
                </div>
                <div className="rounded-lg bg-secondary/30 p-4">
                  <code className="text-sm font-mono break-all">
                    {selectedFactor.factorExpression || selectedFactor.factor_expression || ''}
                  </code>
                </div>
              </div>

              {/* Formulation */}
              {(selectedFactor.factorFormulation || selectedFactor.factor_formulation) && (
                <div>
                  <h4 className="text-sm font-medium mb-2">数学公式</h4>
                  <div className="rounded-lg bg-secondary/30 p-4">
                    <code className="text-sm font-mono break-all">
                      {selectedFactor.factorFormulation || selectedFactor.factor_formulation}
                    </code>
                  </div>
                </div>
              )}

              {/* Backtest Results */}
              {(selectedFactor.backtestResults || selectedFactor.backtest_results) && (
                <div>
                  <h4 className="text-sm font-medium mb-2">回测指标</h4>
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                    {Object.entries(
                      selectedFactor.backtestResults || selectedFactor.backtest_results || {}
                    ).map(([key, val]) => (
                      <div key={key} className="rounded-lg bg-secondary/30 p-3">
                        <div className="text-xs text-muted-foreground truncate" title={key}>
                          {key}
                        </div>
                        <div className="text-sm font-bold font-mono mt-1">
                          {typeof val === 'number' ? formatNumber(val, 4) : String(val)}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Meta */}
              <div>
                <h4 className="text-sm font-medium mb-2">元信息</h4>
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">因子ID:</span>
                    <span className="font-mono">
                      {selectedFactor.factorId || selectedFactor.factor_id || ''}
                    </span>
                  </div>
                  {(selectedFactor.createdAt || selectedFactor.added_at) && (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">创建时间:</span>
                      <span>
                        {new Date(
                          selectedFactor.createdAt || selectedFactor.added_at
                        ).toLocaleString('zh-CN')}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
};

export default FactorLibraryPage;
