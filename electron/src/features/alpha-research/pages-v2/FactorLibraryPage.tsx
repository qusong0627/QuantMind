import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { isAxiosError } from 'axios';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';
import { Button } from '../components-v2/ui/Button';
import { Badge } from '../components-v2/ui/Badge';
import { FactorRecoveryDialog } from '../components-v2/FactorRecoveryDialog';
import { PoolCleanupDialog } from '../components-v2/PoolCleanupDialog';
import { PoolResearchDialog } from '../components-v2/PoolResearchDialog';
import { Factor, FactorQuality, UniverseInfo } from '../types-v2';
import { formatNumber, formatPercent, getQualityBadgeClass } from '../utils-v2';
import { getFactors, getFactorDetail, getUniverses, getFactoryFactors, classifyQuality, UNIVERSE_LABELS } from '../services-v2/api';
import {
  getFactorValidationHistory,
  getFactorSourceRevisions,
  getFactorPoolPanel,
  addFactorToPool,
  listFactorPool,
  removeFactorFromPool,
  listFactorPoolEdges,
  rebuildFactorPoolEdges,
  rebuildFactorPoolTaskRoundEdges,
  rebuildFactorPoolScores,
  rebuildFactorPoolValueEdges,
  submitFactorMaterialization,
  submitFactorCombo,
  submitFactorExport,
  downloadFactorExport,
  type FactorPoolEdge,
  type FactorPoolEntry,
  type FactorPoolPanelSummary,
  type FactorValidationAttempt,
  type FactorSourceRevision,
  evaluateFactorLifecycle,
  getFactorLifecycleHistory,
  restoreFactorLifecycle,
  setFactorArchived,
  type FactorLifecycleEvent,
  type FactorLifecycleEvaluation,
} from '../services-v2/factorJobsApi';
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
  BookmarkPlus,
  BookmarkCheck,
  GitBranch,
  Archive,
  ArchiveRestore,
} from 'lucide-react';

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
};

const FACTOR_PAGE_SIZE = 10;
const EMPTY_FACTOR_COUNTS = { total: 0, high: 0, medium: 0, low: 0 };

const MARKET_COLORS: Record<string, string> = {
  a_share: 'bg-red-500/15 text-red-400 border-red-500/30',
};

const poolMembershipKey = (factorId: string, universe = '') => `${factorId}\u0000${universe}`;

type FactorDetail = Partial<Factor> & {
  factor_name?: string;
  factor_expression?: string;
  factor_formulation?: string;
  factorFormulation?: string;
  factor_description?: string;
  factor_id?: string;
  added_at?: string;
  metadata?: { market?: string; universe?: string };
  backtestResults?: Record<string, unknown>;
  backtest_results?: Record<string, unknown>;
};

export const FactorLibraryPage: React.FC<{
  onBacktestFactor?: (factorId: string) => void;
}> = ({ onBacktestFactor }) => {
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const [sessionEpoch, setSessionEpoch] = useState(() => getSessionGeneration());
  const sessionGenerationRef = useRef(getSessionGeneration());
  const [factors, setFactors] = useState<Factor[]>([]);
  const [factoryFactors, setFactoryFactors] = useState<Factor[]>([]);
  const [factorSource, setFactorSource] = useState<'mined' | 'factory'>('mined');
  const [factorOffset, setFactorOffset] = useState(0);
  const [factorTotal, setFactorTotal] = useState(0);
  const [factorCounts, setFactorCounts] = useState(EMPTY_FACTOR_COUNTS);
  const [archivedView, setArchivedView] = useState(false);
  const [archiveSupported, setArchiveSupported] = useState(false);
  const [visibilityBusy, setVisibilityBusy] = useState<string | null>(null);
  const [visibilityMessage, setVisibilityMessage] = useState('');
  const [factorRefreshSeq, setFactorRefreshSeq] = useState(0);
  const [filteredFactors, setFilteredFactors] = useState<Factor[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [qualityFilter, setQualityFilter] = useState<FactorQuality | 'all'>('all');
  const marketFilter = 'a_share';
  const [universeFilter, setUniverseFilter] = useState<string>('all');
  const [universes, setUniverses] = useState<UniverseInfo[]>([]);
  const [selectedFactor, setSelectedFactor] = useState<FactorDetail | null>(null);
  const [recoveryFactors, setRecoveryFactors] = useState<Array<{ factorId: string; factorName: string }> | null>(null);
  const closeRecovery = useCallback(() => setRecoveryFactors(null), []);
  const [poolCleanupOpen, setPoolCleanupOpen] = useState(false);
  const [poolResearchOpen, setPoolResearchOpen] = useState(false);
  const closePoolResearch = useCallback(() => setPoolResearchOpen(false), []);
  const [poolMembershipRefresh, setPoolMembershipRefresh] = useState(0);
  const closePoolCleanup = useCallback(() => { setPoolCleanupOpen(false); setPoolMembershipRefresh((value) => value + 1); }, []);
  const [validationAttempts, setValidationAttempts] = useState<FactorValidationAttempt[]>([]);
  const [sourceRevisions, setSourceRevisions] = useState<FactorSourceRevision[]>([]);
  const [validationHistoryLoading, setValidationHistoryLoading] = useState(false);
  const [validationHistoryError, setValidationHistoryError] = useState('');
  const [factorPoolIds, setFactorPoolIds] = useState<Set<string>>(() => new Set());
  const [factorPoolEntries, setFactorPoolEntries] = useState<FactorPoolEntry[]>([]);
  const [factorPanel, setFactorPanel] = useState<FactorPoolPanelSummary | null>(null);
  const [factorPanelLoading, setFactorPanelLoading] = useState(false);
  const [factorPanelError, setFactorPanelError] = useState('');
  const [poolActionFactorId, setPoolActionFactorId] = useState<string | null>(null);
  const [poolEdges, setPoolEdges] = useState<FactorPoolEdge[]>([]);
  const [poolGraphLoading, setPoolGraphLoading] = useState(false);
  const [poolGraphSummary, setPoolGraphSummary] = useState('');
  const [poolScoreSummary, setPoolScoreSummary] = useState('');
  const selectionRequestRef = useRef(0);
  const factorLoadSequence = useRef(0);
  const factorLoadController = useRef<AbortController | null>(null);
  const visibilityController = useRef<AbortController | null>(null);
  const [copiedExpr, setCopiedExpr] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [materializingFactorId, setMaterializingFactorId] = useState<string | null>(null);
  const [materializationMessage, setMaterializationMessage] = useState('');
  const [comboSelection, setComboSelection] = useState<string[]>([]);
  const [comboName, setComboName] = useState('');
  const [comboBusy, setComboBusy] = useState(false);
  const [comboMessage, setComboMessage] = useState('');
  const [exportSelection, setExportSelection] = useState<string[]>([]);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportMessage, setExportMessage] = useState('');
  const [batchMaterializing, setBatchMaterializing] = useState(false);
  const [lifecycleEvaluation, setLifecycleEvaluation] = useState<FactorLifecycleEvaluation | null>(null);
  const [lifecycleEvaluationBusy, setLifecycleEvaluationBusy] = useState(false);
  const [lifecycleEvaluationError, setLifecycleEvaluationError] = useState('');
  const [lifecycleEvents, setLifecycleEvents] = useState<FactorLifecycleEvent[]>([]);
  const [lifecycleHistoryCursor, setLifecycleHistoryCursor] = useState<number | null>(null);
  const [lifecycleHistoryLoading, setLifecycleHistoryLoading] = useState(false);
  const [lifecycleHistoryError, setLifecycleHistoryError] = useState('');

  const onPoolCleanupRemoved = useCallback((factorId: string, universe: string) => {
    const key = poolMembershipKey(factorId, universe);
    setFactorPoolEntries((current) => current.filter((entry) => poolMembershipKey(entry.factor_id, entry.universe) !== key));
    setFactorPoolIds((current) => { const next = new Set(current); next.delete(key); return next; });
    setComboSelection((current) => current.filter((value) => value !== key));
    setPoolEdges((current) => current.filter((edge) => edge.universe !== universe || (edge.src_factor_id !== factorId && edge.dst_factor_id !== factorId)));
    setPoolScoreSummary(''); setPoolGraphSummary('');
  }, []);

  const resetPrivateFactorState = useCallback(() => {
    const generation = getSessionGeneration();
    sessionGenerationRef.current = generation;
    setFactors([]);
    setFactoryFactors([]);
    setFactorSource('mined');
    setFactorOffset(0);
    setFactorTotal(0);
    setFactorCounts(EMPTY_FACTOR_COUNTS);
    setArchivedView(false);
    setArchiveSupported(false);
    setVisibilityBusy(null);
    setVisibilityMessage('');
    setFactorRefreshSeq(0);
    visibilityController.current?.abort();
    factorLoadSequence.current += 1;
    factorLoadController.current?.abort();
    setFilteredFactors([]);
    setUniverses([]);
    setSelectedFactor(null);
    setRecoveryFactors(null);
    setPoolCleanupOpen(false);
    setPoolResearchOpen(false);
    setPoolMembershipRefresh(0);
    setValidationAttempts([]);
    setSourceRevisions([]);
    setValidationHistoryLoading(false);
    setValidationHistoryError('');
    setFactorPoolIds(new Set());
    setFactorPoolEntries([]);
    setFactorPanel(null);
    setFactorPanelLoading(false);
    setFactorPanelError('');
    setPoolActionFactorId(null);
    setPoolEdges([]);
    setPoolGraphLoading(false);
    setPoolGraphSummary('');
    setPoolScoreSummary('');
    selectionRequestRef.current += 1;
    setSearchQuery('');
    setAppliedSearch('');
    setQualityFilter('all');
    setUniverseFilter('all');
    setError(null);
    setMaterializingFactorId(null);
    setMaterializationMessage('');
    setComboSelection([]);
    setComboName('');
    setComboBusy(false);
    setComboMessage('');
    setExportSelection([]);
    setExportBusy(false);
    setExportMessage('');
    setBatchMaterializing(false);
    setLifecycleEvaluation(null);
    setLifecycleEvaluationBusy(false);
    setLifecycleEvaluationError('');
    setLifecycleEvents([]);
    setLifecycleHistoryCursor(null);
    setLifecycleHistoryLoading(false);
    setLifecycleHistoryError('');
    setIsLoading(false);
    setSessionEpoch(generation);
  }, []);

  useEffect(() => {
    sessionGenerationRef.current = getSessionGeneration();
    const unregister = registerSessionReset(resetPrivateFactorState);
    return () => {
      unregister();
      factorLoadSequence.current += 1;
      factorLoadController.current?.abort();
      visibilityController.current?.abort();
    };
  }, [resetPrivateFactorState]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setAppliedSearch(searchQuery.trim());
      setFactorOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    if (!isAuthenticated || isAuthenticating) {
      setUniverses([]);
      return;
    }
    const generation = sessionGenerationRef.current;
    getUniverses()
      .then((res) => {
        if (generation === sessionGenerationRef.current) {
          setUniverses(res.data?.universes ?? []);
        }
      })
      .catch(() => {});
  }, [isAuthenticated, isAuthenticating, sessionEpoch]);

  const selectedPoolFactorId = selectedFactor?.factorId || selectedFactor?.factor_id || '';
  const selectedPoolUniverse = selectedFactor?.universe || selectedFactor?.metadata?.universe || '';
  const selectedIsPoolMember = selectedPoolFactorId
    ? factorPoolIds.has(poolMembershipKey(selectedPoolFactorId, selectedPoolUniverse))
    : false;
  const selectedPoolEntry = factorPoolEntries.find((entry) =>
    entry.factor_id === selectedPoolFactorId && entry.universe === selectedPoolUniverse,
  );
  const selectedComboEntries = factorPoolEntries.filter((entry) =>
    comboSelection.includes(poolMembershipKey(entry.factor_id, entry.universe))
      && Boolean(entry.panel_version),
  );
  const comboUniverses = new Set(selectedComboEntries.map((entry) => entry.universe));
  const comboSelectionValid = selectedComboEntries.length >= 2
    && selectedComboEntries.length <= 12
    && comboUniverses.size === 1
    && selectedComboEntries.length === comboSelection.length;

  useEffect(() => {
    if (!selectedIsPoolMember || !isAuthenticated || isAuthenticating) {
      setFactorPanel(null);
      setFactorPanelLoading(false);
      setFactorPanelError('');
      return;
    }
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    setFactorPanel(null);
    setFactorPanelError('');
    setFactorPanelLoading(true);
    getFactorPoolPanel(
      selectedPoolFactorId,
      { market: 'a_share', universe: selectedPoolUniverse },
      controller.signal,
    )
      .then((panel) => {
        if (generation === sessionGenerationRef.current) setFactorPanel(panel);
      })
      .catch(() => {
        if (generation === sessionGenerationRef.current && !controller.signal.aborted) {
          setFactorPanelError('本人值级面板暂时无法读取');
        }
      })
      .finally(() => {
        if (generation === sessionGenerationRef.current) setFactorPanelLoading(false);
      });
    return () => controller.abort();
  }, [selectedIsPoolMember, selectedPoolFactorId, selectedPoolUniverse, isAuthenticated, isAuthenticating, sessionEpoch]);

  useEffect(() => {
    if (!isAuthenticated || isAuthenticating) {
      setFactorPoolIds(new Set());
      return;
    }
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    listFactorPool({ limit: 500 }, controller.signal)
      .then((items) => {
        if (!controller.signal.aborted && generation === sessionGenerationRef.current) {
          setFactorPoolEntries(items);
          setFactorPoolIds(new Set(items.map((item) => poolMembershipKey(item.factor_id, item.universe))));
        }
      })
      .catch(() => {
        if (generation === sessionGenerationRef.current && !controller.signal.aborted) {
          setError('本人因子池暂时无法读取');
        }
      });
    return () => controller.abort();
  }, [isAuthenticated, isAuthenticating, sessionEpoch, poolMembershipRefresh]);

  const toggleFactorPool = async (factor: Factor) => {
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    const membershipKey = poolMembershipKey(factor.factorId, factor.universe || '');
    setPoolActionFactorId(factor.factorId);
    try {
      if (factorPoolIds.has(membershipKey)) {
        await removeFactorFromPool(
          factor.factorId,
          { market: 'a_share', universe: factor.universe || '' },
          controller.signal,
        );
        if (generation === sessionGenerationRef.current) {
          const next = new Set<string>(factorPoolIds);
          next.delete(membershipKey);
          setFactorPoolIds(next);
        }
      } else {
        await addFactorToPool(
          factor.factorId,
          { market: 'a_share', universe: factor.universe || '' },
          controller.signal,
        );
        if (generation === sessionGenerationRef.current) {
          setFactorPoolIds(new Set<string>([...factorPoolIds, membershipKey]));
        }
      }
      const entries = await listFactorPool({ limit: 500 }, controller.signal);
      if (generation === sessionGenerationRef.current) setFactorPoolEntries(entries);
    } catch {
      if (generation === sessionGenerationRef.current) setError('更新本人因子池失败，请重试');
    } finally {
      if (generation === sessionGenerationRef.current) setPoolActionFactorId(null);
    }
  };

  const materializeFactor = async (factor: Factor) => {
    const generation = sessionGenerationRef.current;
    setMaterializingFactorId(factor.factorId);
    setMaterializationMessage('');
    try {
      const job = await submitFactorMaterialization({
        factor_id: factor.factorId,
        universe: factor.universe || 'csi300',
      });
      if (generation === sessionGenerationRef.current) {
        setMaterializationMessage(`已提交私有因子物化任务 ${job.job_id}，可在任务中心查看。`);
      }
    } catch (cause) {
      if (generation === sessionGenerationRef.current) {
        setError(cause instanceof Error ? cause.message : '提交因子物化失败，请重试');
      }
    } finally {
      if (generation === sessionGenerationRef.current) setMaterializingFactorId(null);
    }
  };

  const toggleComboFactor = (factor: Factor) => {
    const key = poolMembershipKey(factor.factorId, factor.universe || '');
    setComboMessage('');
    if (comboSelection.includes(key)) {
      setComboSelection(comboSelection.filter((item) => item !== key));
      return;
    }
    if (comboSelection.length >= 12) {
      setComboMessage('组合最多选择 12 个因子');
      return;
    }
    setComboSelection([...comboSelection, key]);
  };

  const submitCombo = async () => {
    if (!comboSelectionValid || comboBusy) return;
    const generation = sessionGenerationRef.current;
    setComboBusy(true);
    setComboMessage('');
    try {
      const job = await submitFactorCombo({
        factor_ids: selectedComboEntries.map((entry) => entry.factor_id),
        universe: selectedComboEntries[0].universe,
        name: comboName.trim(),
      });
      if (generation === sessionGenerationRef.current) {
        setComboMessage(`组合优化已进入本人作业队列：${job.job_id}，可在任务中心查看。`);
      }
    } catch (cause) {
      if (generation === sessionGenerationRef.current) {
        setComboMessage(cause instanceof Error ? cause.message : '组合优化提交失败');
      }
    } finally {
      if (generation === sessionGenerationRef.current) setComboBusy(false);
    }
  };

  const toggleExportFactor = (factor: Factor) => {
    setExportMessage('');
    if (exportSelection.includes(factor.factorId)) {
      setExportSelection(exportSelection.filter((item) => item !== factor.factorId));
      return;
    }
    if (exportSelection.length >= 12) {
      setExportMessage('合并导出最多选择 12 个因子');
      return;
    }
    setExportSelection([...exportSelection, factor.factorId]);
  };

  const submitBatchMaterialization = async () => {
    if (exportSelection.length === 0 || batchMaterializing) return;
    const generation = sessionGenerationRef.current;
    const byId = new Map<string, Factor>();
    [...factors, ...factoryFactors, ...filteredFactors].forEach((item) => {
      if (!byId.has(item.factorId)) byId.set(item.factorId, item);
    });
    const ids = exportSelection.slice(0, 12);
    setBatchMaterializing(true);
    setExportMessage('');
    try {
      let succeeded = 0;
      let failed = 0;
      for (let index = 0; index < ids.length; index += 3) {
        const chunk = ids.slice(index, index + 3);
        const outcomes = await Promise.allSettled(chunk.map((factorId) => submitFactorMaterialization({
          factor_id: factorId,
          universe: byId.get(factorId)?.universe || 'csi300',
        })));
        outcomes.forEach((outcome) => {
          if (outcome.status === 'fulfilled') succeeded += 1;
          else failed += 1;
        });
      }
      if (generation === sessionGenerationRef.current) {
        setExportMessage(`批量物化已提交 ${succeeded} 个，提交失败 ${failed} 个；最终结果请在任务中心查看。`);
      }
    } finally {
      if (generation === sessionGenerationRef.current) setBatchMaterializing(false);
    }
  };

  const submitMergedExport = async () => {
    if (exportSelection.length === 0 || exportBusy) return;
    const generation = sessionGenerationRef.current;
    setExportBusy(true);
    setExportMessage('');
    try {
      const summary = await submitFactorExport({ factor_ids: exportSelection.slice(0, 12) });
      const blob = await downloadFactorExport(summary.export_id);
      if (generation !== sessionGenerationRef.current) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `factors_${new Date().toISOString().slice(0, 10).replace(/-/g, '')}.parquet`;
      link.click();
      URL.revokeObjectURL(url);
      setExportMessage(`已导出 ${summary.column_count} 列 · ${summary.row_count} 行（${summary.date_min} 至 ${summary.date_max}，${summary.symbol_count} 标的）。`);
    } catch (cause) {
      if (generation === sessionGenerationRef.current) {
        const detail = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setExportMessage(typeof detail === 'string' ? detail : (cause instanceof Error ? cause.message : '合并导出失败，请稍后重试'));
      }
    } finally {
      if (generation === sessionGenerationRef.current) setExportBusy(false);
    }
  };

  const refreshPoolGraph = async () => {
    if (!isAuthenticated || isAuthenticating) return;
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    setPoolGraphLoading(true);
    try {
      const universe = universeFilter === 'all' ? undefined : universeFilter;
      const summary = await rebuildFactorPoolEdges({ market: 'a_share', universe }, controller.signal);
      const edges = await listFactorPoolEdges({ universe, limit: 1000 }, controller.signal);
      if (generation !== sessionGenerationRef.current) return;
      setPoolEdges(edges);
      setPoolGraphSummary(`检查 ${summary.factor_count} 个本人池因子，发现 ${summary.edge_count} 条公式相似关系`);
    } catch {
      if (generation === sessionGenerationRef.current && !controller.signal.aborted) {
        setError('重算本人因子谱系失败，请稍后重试');
      }
    } finally {
      if (generation === sessionGenerationRef.current) setPoolGraphLoading(false);
    }
  };

  const refreshPoolValueGraph = async () => {
    if (!isAuthenticated || isAuthenticating) return;
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    setPoolGraphLoading(true);
    try {
      const universe = universeFilter === 'all' ? undefined : universeFilter;
      const summary = await rebuildFactorPoolValueEdges({ market: 'a_share', universe }, controller.signal);
      const edges = await listFactorPoolEdges({ universe, limit: 1000 }, controller.signal);
      if (generation !== sessionGenerationRef.current) return;
      setPoolEdges(edges);
      setPoolGraphSummary(`检查 ${summary.panel_count} 个本人面板，发现 ${summary.edge_count} 条值相关关系（|ρ| ≥ 0.50）`);
    } catch {
      if (generation === sessionGenerationRef.current) setError('本人因子值相关性重算失败，请重试');
    } finally {
      if (generation === sessionGenerationRef.current) setPoolGraphLoading(false);
    }
  };

  const refreshPoolTaskRoundGraph = async () => {
    if (!isAuthenticated || isAuthenticating) return;
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    setPoolGraphLoading(true);
    try {
      const universe = universeFilter === 'all' ? undefined : universeFilter;
      const summary = await rebuildFactorPoolTaskRoundEdges({ market: 'a_share', universe }, controller.signal);
      const edges = await listFactorPoolEdges({ universe, limit: 1000 }, controller.signal);
      if (generation !== sessionGenerationRef.current) return;
      setPoolEdges(edges);
      setPoolGraphSummary(`检查 ${summary.factor_count} 个本人池因子、${summary.task_count} 个本人挖掘任务，建立 ${summary.edge_count} 条相邻轮次关系`);
    } catch {
      if (generation === sessionGenerationRef.current && !controller.signal.aborted) {
        setError('重算本人任务轮次谱系失败，请稍后重试');
      }
    } finally {
      if (generation === sessionGenerationRef.current) setPoolGraphLoading(false);
    }
  };

  const refreshPoolScores = async () => {
    if (!isAuthenticated || isAuthenticating) return;
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    setPoolGraphLoading(true);
    try {
      const universe = universeFilter === 'all' ? undefined : universeFilter;
      const summary = await rebuildFactorPoolScores({ market: 'a_share', universe }, controller.signal);
      const entries = await listFactorPool({ limit: 500 }, controller.signal);
      if (generation !== sessionGenerationRef.current) return;
      setFactorPoolEntries(entries);
      setPoolScoreSummary(
        `已刷新 ${summary.updated_count} 条本人池记录；${summary.scored_count} 条有 ICIR，${summary.missing_icir_count} 条因缺少 ICIR 不参与排序`,
      );
    } catch {
      if (generation === sessionGenerationRef.current && !controller.signal.aborted) {
        setError('重算本人因子池评分失败，请稍后重试');
      }
    } finally {
      if (generation === sessionGenerationRef.current) setPoolGraphLoading(false);
    }
  };

  const loadFactors = useCallback(async () => {
    const requestSequence = ++factorLoadSequence.current;
    factorLoadController.current?.abort();
    if (!isAuthenticated || isAuthenticating) {
      setFactors([]);
      setFactoryFactors([]);
      setFactorTotal(0);
      setFactorCounts(EMPTY_FACTOR_COUNTS);
      setFilteredFactors([]);
      setSelectedFactor(null);
      setIsLoading(false);
      return;
    }
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    factorLoadController.current = controller;
    const isCurrent = () => !controller.signal.aborted && generation === sessionGenerationRef.current && requestSequence === factorLoadSequence.current;
    setIsLoading(true);
    setError(null);
    try {
      const [resp, factoryResp] = await Promise.all([
        factorSource === 'mined' ? getFactors({
          market: marketFilter,
          universe: universeFilter !== 'all' ? universeFilter : undefined,
          search: appliedSearch || undefined,
          quality: qualityFilter === 'all' ? undefined : qualityFilter,
          limit: FACTOR_PAGE_SIZE,
          offset: factorOffset,
          archived: archivedView,
        }, controller.signal) : Promise.resolve(null),
        getFactoryFactors(controller.signal).catch(() => null),
      ]);
      if (!isCurrent()) return;
      if (resp?.success && resp.data) {
        setFactors(resp.data.factors.filter((factor) => factor.market === marketFilter));
        setFactorTotal(resp.data.total);
        setArchiveSupported(resp.data.archiveSupported === true);
        if (archivedView && !resp.data.archiveSupported) setArchivedView(false);
        setFactorCounts(resp.data.counts ?? {
          total: resp.data.total,
          high: resp.data.factors.filter((factor) => factor.quality === 'high').length,
          medium: resp.data.factors.filter((factor) => factor.quality === 'medium').length,
          low: resp.data.factors.filter((factor) => factor.quality === 'low').length,
        });
        if (factorOffset > 0 && factorOffset >= resp.data.total) {
          setFactorOffset(Math.max(0, Math.floor((resp.data.total - 1) / FACTOR_PAGE_SIZE) * FACTOR_PAGE_SIZE));
        }
      }
      if (factoryResp?.data) {
        // Factory factors are private, read-only assets with their own view.
        const generatedAt = factoryResp?.data?.generatedAt ?? '';
        const factoryRows: Factor[] = (factoryResp?.data?.factors ?? []).map((f) => ({
          factorId: f.factorId,
          factorName: f.factorName,
          factorExpression: f.factorExpression,
          factorDescription: `因子工厂产出 · 字段 ${f.field || '—'} · 覆盖率 ${formatPercent(f.coverage, 0)}`,
          quality: classifyQuality(f.ic),
          market: 'a_share',
          universe: 'all_a',
          ic: f.ic,
          icir: f.icir,
          rankIc: null,
          rankIcir: null,
          sharpeRatio: null,
          annualReturn: null,
          maxDrawdown: null,
          round: 0,
          direction: f.ic == null ? '未知' : f.ic >= 0 ? '正向' : '反向',
          createdAt: generatedAt,
          readOnly: true,
          source: 'factor_factory',
          coverage: f.coverage,
        }));
        setFactoryFactors(factoryRows);
      } else {
        setFactoryFactors([]);
        if (factorSource === 'factory') setError('本人工厂因子暂时无法读取，请稍后刷新。');
      }
    } catch (err: unknown) {
      if (!isCurrent()) return;
      console.error('Failed to load factors from API:', err);
      const status = isAxiosError(err) ? err.response?.status : undefined;
      if (status === 401 || status === 403) {
        setError('登录已过期，请刷新页面重新登录。');
      } else if (status) {
        setError(`后端返回错误 (HTTP ${status})。请稍后重试。`);
      } else {
        setError('无法连接 AlphaAgent 接口，请检查网络或登录状态。');
      }
      // Show empty state with error message instead of mock data
      setFactors([]);
      setFactorTotal(0);
      setFactorCounts(EMPTY_FACTOR_COUNTS);
    } finally {
      if (isCurrent()) setIsLoading(false);
    }
  }, [isAuthenticated, isAuthenticating, marketFilter, universeFilter, qualityFilter, appliedSearch, factorOffset, factorSource, archivedView]);

  const changeFactorArchive = async (factor: Factor) => {
    if (factor.readOnly || visibilityBusy || poolActionFactorId === factor.factorId || materializingFactorId === factor.factorId || !archiveSupported || !isAuthenticated || isAuthenticating) return;
    const generation = sessionGenerationRef.current;
    const controller = new AbortController();
    visibilityController.current = controller;
    const archived = !factor.archivedAt;
    setVisibilityBusy(factor.factorId);
    setVisibilityMessage('');
    setError(null);
    try {
      await setFactorArchived(factor.factorId, archived, controller.signal);
      if (controller.signal.aborted || generation !== sessionGenerationRef.current) return;
      if (archived) {
        const removedKeys = new Set(factorPoolEntries.filter((entry) => entry.factor_id === factor.factorId).map((entry) => poolMembershipKey(entry.factor_id, entry.universe)));
        setFactorPoolEntries((current) => current.filter((entry) => entry.factor_id !== factor.factorId));
        setFactorPoolIds((current) => new Set([...current].filter((key) => !removedKeys.has(key))));
        setComboSelection((current) => current.filter((key) => !removedKeys.has(key)));
        setExportSelection((current) => current.filter((id) => id !== factor.factorId));
      }
      setSelectedFactor((current) => current?.factorId === factor.factorId ? null : current);
      setVisibilityMessage(archived ? '因子已归档并退出研究池，历史回测、版本和训练引用保留。' : '因子已恢复到在库列表；需要继续研究时请重新加入本人因子池。');
      setFactorRefreshSeq((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted && generation === sessionGenerationRef.current) {
        const detail = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setError(typeof detail === 'string' ? detail : '因子归档状态修改失败，请刷新核对后重试。');
      }
    } finally {
      if (generation === sessionGenerationRef.current) setVisibilityBusy(null);
    }
  };

  const factoryMatches = useMemo(() => {
    let filtered = factoryFactors;
    filtered = filtered.filter((f) => f.market === marketFilter);
    if (universeFilter !== 'all') {
      filtered = filtered.filter((f) => f.universe === universeFilter);
    }
    if (appliedSearch) {
      const query = appliedSearch.toLowerCase();
      filtered = filtered.filter(
        (f) =>
          f.factorName.toLowerCase().includes(query) ||
          f.factorExpression.toLowerCase().includes(query) ||
          f.factorDescription.toLowerCase().includes(query)
      );
    }
    return filtered;
  }, [factoryFactors, marketFilter, appliedSearch, universeFilter]);

  const filterFactors = useCallback(() => {
    if (factorSource === 'mined') {
      setFilteredFactors(factors);
      return;
    }
    const filtered = qualityFilter === 'all' ? factoryMatches : factoryMatches.filter((factor) => factor.quality === qualityFilter);
    if (factorOffset > 0 && factorOffset >= filtered.length) {
      setFactorOffset(Math.max(0, Math.floor((filtered.length - 1) / FACTOR_PAGE_SIZE) * FACTOR_PAGE_SIZE));
      return;
    }
    setFilteredFactors(filtered.slice(factorOffset, factorOffset + FACTOR_PAGE_SIZE));
  }, [factors, factorSource, factoryMatches, qualityFilter, factorOffset]);

  useEffect(() => {
    void loadFactors();
  }, [loadFactors, sessionEpoch, factorRefreshSeq]);

  useEffect(() => {
    filterFactors();
  }, [filterFactors]);

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
    const dataStr = JSON.stringify(filteredFactors, null, 2);
    const blob = new Blob([dataStr], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `factors_${new Date().toISOString().split('T')[0]}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const handleSelectFactor = async (factor: Factor) => {
    const generation = sessionGenerationRef.current;
    const requestId = ++selectionRequestRef.current;
    setSelectedFactor(factor);
    setLifecycleEvaluation(null);
    setLifecycleEvaluationError('');
    setLifecycleEvents([]);
    setLifecycleHistoryCursor(null);
    setLifecycleHistoryError('');
    setValidationAttempts([]);
    setSourceRevisions([]);
    setValidationHistoryError('');
    // 工厂因子只读、无明细接口，直接用列表数据
    if (factor.readOnly) {
      setValidationHistoryLoading(false);
      return;
    }
    setValidationHistoryLoading(true);
    setLifecycleHistoryLoading(true);
    // Try to load full detail from API
    const [detailResult, validationResult, revisionResult, lifecycleResult] = await Promise.allSettled([
      getFactorDetail(factor.factorId),
      getFactorValidationHistory(factor.factorId),
      getFactorSourceRevisions(factor.factorId),
      getFactorLifecycleHistory(factor.factorId, { limit: 10 }),
    ]);
    if (generation !== sessionGenerationRef.current || requestId !== selectionRequestRef.current) return;
    if (detailResult.status === 'fulfilled') {
      const resp = detailResult.value;
      if (resp.success && resp.data?.factor) {
        setSelectedFactor({ ...factor, ...resp.data.factor });
      }
    }
    if (validationResult.status === 'fulfilled') {
      setValidationAttempts(validationResult.value.attempts);
    } else {
      setValidationHistoryError('验证记录暂时无法读取');
    }
    if (revisionResult.status === 'fulfilled') {
      setSourceRevisions(revisionResult.value);
    }
    if (lifecycleResult.status === 'fulfilled') {
      setLifecycleEvents(lifecycleResult.value.events);
      setLifecycleHistoryCursor(lifecycleResult.value.next_before_event_id);
    } else {
      setLifecycleHistoryError('生命周期历史暂时无法读取');
    }
    setLifecycleHistoryLoading(false);
    setValidationHistoryLoading(false);
  };

  const loadMoreLifecycleHistory = async (factorId: string, beforeEventId?: number) => {
    const generation = sessionGenerationRef.current;
    const requestId = selectionRequestRef.current;
    setLifecycleHistoryLoading(true);
    setLifecycleHistoryError('');
    try {
      const page = await getFactorLifecycleHistory(
        factorId,
        { limit: 10, beforeEventId },
      );
      if (
        generation !== sessionGenerationRef.current
        || requestId !== selectionRequestRef.current
      ) return;
      setLifecycleEvents(beforeEventId
        ? [...lifecycleEvents, ...page.events]
        : page.events);
      setLifecycleHistoryCursor(page.next_before_event_id);
    } catch {
      if (generation === sessionGenerationRef.current) {
        setLifecycleHistoryError('生命周期历史暂时无法读取');
      }
    } finally {
      if (generation === sessionGenerationRef.current) setLifecycleHistoryLoading(false);
    }
  };

  const runLifecycleEvaluation = async (apply: boolean) => {
    const factorId = selectedFactor?.factorId || selectedFactor?.factor_id || '';
    if (!factorId || selectedFactor?.readOnly || lifecycleEvaluationBusy) return;
    const generation = sessionGenerationRef.current;
    const selectionRequest = selectionRequestRef.current;
    const selectedFactorSnapshot = selectedFactor;
    const controller = new AbortController();
    setLifecycleEvaluationBusy(true);
    setLifecycleEvaluationError('');
    try {
      const result = await evaluateFactorLifecycle(factorId, apply, controller.signal);
      if (
        generation !== sessionGenerationRef.current
        || selectionRequest !== selectionRequestRef.current
      ) return;
      setLifecycleEvaluation(result);
      if (apply) {
        const detail = await getFactorDetail(factorId);
        if (
          generation !== sessionGenerationRef.current
          || selectionRequest !== selectionRequestRef.current
        ) return;
        if (detail.success && detail.data?.factor) {
          setSelectedFactor({ ...selectedFactorSnapshot, ...detail.data.factor });
        }
        await loadMoreLifecycleHistory(factorId);
        void loadFactors();
      }
    } catch (cause) {
      if (generation === sessionGenerationRef.current) {
        setLifecycleEvaluationError(
          cause instanceof Error ? cause.message : '生命周期评估失败，请稍后重试。',
        );
      }
    } finally {
      if (generation === sessionGenerationRef.current) setLifecycleEvaluationBusy(false);
    }
  };

  const restoreSelectedLifecycle = async () => {
    const factorId = selectedFactor?.factorId || selectedFactor?.factor_id || '';
    if (!factorId || selectedFactor?.readOnly || lifecycleEvaluationBusy) return;
    const reason = window.prompt('填写人工恢复原因（必填，最多 500 字）')?.trim();
    if (!reason) return;
    const generation = sessionGenerationRef.current;
    const selectionRequest = selectionRequestRef.current;
    setLifecycleEvaluationBusy(true);
    setLifecycleEvaluationError('');
    try {
      await restoreFactorLifecycle(factorId, reason);
      if (
        generation !== sessionGenerationRef.current
        || selectionRequest !== selectionRequestRef.current
      ) return;
      const detail = await getFactorDetail(factorId);
      if (
        generation !== sessionGenerationRef.current
        || selectionRequest !== selectionRequestRef.current
      ) return;
      if (detail.success && detail.data?.factor) {
        setSelectedFactor({ ...selectedFactor, ...detail.data.factor });
      }
      setLifecycleEvaluation(null);
      await loadMoreLifecycleHistory(factorId);
      void loadFactors();
    } catch (cause) {
      if (generation === sessionGenerationRef.current) {
        setLifecycleEvaluationError(
          cause instanceof Error ? cause.message : '人工恢复失败，请稍后重试。',
        );
      }
    } finally {
      if (generation === sessionGenerationRef.current) setLifecycleEvaluationBusy(false);
    }
  };

  const stats = factorSource === 'mined' ? factorCounts : {
    total: factoryMatches.length,
    high: factoryMatches.filter((f) => f.quality === 'high').length,
    medium: factoryMatches.filter((f) => f.quality === 'medium').length,
    low: factoryMatches.filter((f) => f.quality === 'low').length,
  };
  const visibleTotal = factorSource === 'mined' ? factorTotal : factoryMatches.filter((factor) => qualityFilter === 'all' || factor.quality === qualityFilter).length;

  return (
    <div className="space-y-4 animate-fade-in-up">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <Database className="h-5 w-5 text-primary" />
            因子库
          </h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            浏览和管理挖掘的因子（含因子工厂批量产出，只读）
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" className="text-xs" onClick={() => void refreshPoolGraph()} disabled={poolGraphLoading || !isAuthenticated || isAuthenticating}>
            <GitBranch className={`h-3.5 w-3.5 mr-1.5 ${poolGraphLoading ? 'animate-pulse' : ''}`} />
            重算公式相似
          </Button>
          <Button variant="outline" size="sm" className="text-xs" onClick={() => void refreshPoolValueGraph()} disabled={poolGraphLoading || !isAuthenticated || isAuthenticating}>
            <GitBranch className={`h-3.5 w-3.5 mr-1.5 ${poolGraphLoading ? 'animate-pulse' : ''}`} />
            重算值相关
          </Button>
          <Button variant="outline" size="sm" className="text-xs" onClick={() => void refreshPoolTaskRoundGraph()} disabled={poolGraphLoading || !isAuthenticated || isAuthenticating}>
            <GitBranch className={`h-3.5 w-3.5 mr-1.5 ${poolGraphLoading ? 'animate-pulse' : ''}`} />
            重算任务轮次
          </Button>
          <Button variant="outline" size="sm" className="text-xs" onClick={() => void refreshPoolScores()} disabled={poolGraphLoading || !isAuthenticated || isAuthenticating}>
            <TrendingUp className={`h-3.5 w-3.5 mr-1.5 ${poolGraphLoading ? 'animate-pulse' : ''}`} />
            重算池评分
          </Button>
          <Button variant="outline" size="sm" className="text-xs" onClick={loadFactors} disabled={isLoading}>
            <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${isLoading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
          <Button variant="primary" size="sm" className="text-xs" onClick={handleExport} disabled={isLoading}>
            <Download className="h-3.5 w-3.5 mr-1.5" />
            导出当前页
          </Button>
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-warning/10 border-warning/50">
          <AlertCircle className="h-5 w-5 text-warning flex-shrink-0" />
          <span className="text-sm text-warning">{error}</span>
        </div>
      )}
      {materializationMessage && (
        <div className="rounded-lg border border-primary/30 bg-primary/5 px-3 py-2 text-sm" role="status">
          {materializationMessage}
        </div>
      )}
      {visibilityMessage && <div role="status" className="rounded-lg border border-primary/20 bg-primary/5 p-3 text-sm">{visibilityMessage}</div>}
      <Card className="glass">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <GitBranch className="h-4 w-4 text-primary" /> 组合实验
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            仅使用本人因子池中有同股票池、带前瞻收益值级面板的因子；按时间先后切分训练与验证窗口。
          </p>
        </CardHeader>
        <CardContent className="flex flex-col md:flex-row gap-3">
          <input
            value={comboName}
            onChange={(event) => setComboName(event.target.value.slice(0, 128))}
            maxLength={128}
            placeholder="组合名称（可选）"
            aria-label="组合名称"
            className="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm"
          />
          <Button
            variant="primary"
            disabled={!comboSelectionValid || comboBusy || !isAuthenticated || isAuthenticating}
            onClick={() => void submitCombo()}
          >
            {comboBusy ? '提交中…' : `优化所选因子 (${comboSelection.length}/12)`}
          </Button>
        </CardContent>
        {(comboMessage || comboSelection.length > 0) && (
          <div className="px-4 pb-4 text-xs text-muted-foreground" role="status">
            {comboMessage || (comboSelectionValid
              ? `已选择 ${selectedComboEntries.length} 个同股票池因子`
              : '请选 2–12 个同一股票池的本人面板因子')}
          </div>
        )}
      </Card>

      {(poolGraphSummary || poolEdges.length > 0) && (
        <Card className="glass">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <GitBranch className="h-4 w-4 text-primary" /> 本人因子研究关系
            </CardTitle>
            {poolGraphSummary && <p className="text-xs text-muted-foreground">{poolGraphSummary}；公式边使用 Jaccard ≥ 0.60，值相关边使用逐日截面秩相关（每天至少 20 个重叠标的、至少 5 个有效日，|ρ| ≥ 0.50），每个因子最多保留 3 个邻居。</p>}
          </CardHeader>
          <CardContent className="space-y-2">
            {poolEdges.length === 0 ? (
              <p className="text-xs text-muted-foreground">当前股票池没有达到阈值的关系边。值相关边需要本人因子先完成回测并保存面板。</p>
            ) : poolEdges.map((edge) => (
              <div key={`${edge.universe}:${edge.method}:${edge.relation}:${edge.src_factor_id}:${edge.dst_factor_id}`} className="flex items-center justify-between gap-3 rounded-md bg-secondary/30 px-3 py-2 text-sm">
                <span className="truncate">
                  {edge.src_factor_name} ↔ {edge.dst_factor_name}
                  <span className="text-muted-foreground">
                    ({edge.method === 'task_round'
                      ? '同一挖掘任务的相邻因子'
                      : edge.method === 'value'
                      ? `值相关 ρ=${(edge.signed_correlation ?? 0).toFixed(2)}`
                      : '公式相似'})
                  </span>
                </span>
                <Badge className="shrink-0">{((edge.weight ?? 0) * 100).toFixed(0)}%</Badge>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
      {poolScoreSummary && <p className="text-xs text-muted-foreground">{poolScoreSummary}</p>}

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="glass card-hover">
          <CardContent className="p-3 flex flex-col items-center justify-center text-center gap-0.5">
            <div className="p-2 rounded-md bg-primary/20">
              <BarChart3 className="h-4 w-4 text-primary" />
            </div>
            <div className="text-xs text-muted-foreground">总因子数</div>
            <div className="text-lg font-bold">{stats.total}</div>
          </CardContent>
        </Card>

        <Card className="glass card-hover">
          <CardContent className="p-3 flex flex-col items-center justify-center text-center gap-0.5">
            <div className="p-2 rounded-md bg-success/20">
              <TrendingUp className="h-4 w-4 text-success" />
            </div>
            <div className="text-xs text-muted-foreground">高质量</div>
            <div className="text-lg font-bold text-success">{stats.high}</div>
          </CardContent>
        </Card>

        <Card className="glass card-hover">
          <CardContent className="p-3 flex flex-col items-center justify-center text-center gap-0.5">
            <div className="p-2 rounded-md bg-warning/20">
              <BarChart3 className="h-4 w-4 text-warning" />
            </div>
            <div className="text-xs text-muted-foreground">中等质量</div>
            <div className="text-lg font-bold text-warning">{stats.medium}</div>
          </CardContent>
        </Card>

        <Card className="glass card-hover">
          <CardContent className="p-3 flex flex-col items-center justify-center text-center gap-0.5">
            <div className="p-2 rounded-md bg-destructive/20">
              <BarChart3 className="h-4 w-4 text-destructive" />
            </div>
            <div className="text-xs text-muted-foreground">低质量</div>
            <div className="text-lg font-bold text-destructive">{stats.low}</div>
          </CardContent>
        </Card>
      </div>

      {/* Filters */}
      <Card className="glass">
        <CardContent className="p-4">
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-2">
              <Button variant={factorSource === 'mined' ? 'primary' : 'outline'} size="sm" onClick={() => { setFactorSource('mined'); setFactorOffset(0); }}>
                本人挖掘
              </Button>
              <Button variant={factorSource === 'factory' ? 'primary' : 'outline'} size="sm" onClick={() => { setFactorSource('factory'); setArchivedView(false); setFactorOffset(0); }}>
                本人工厂（只读）
              </Button>
              {factorSource === 'mined' && archiveSupported && <>
                <Button variant={!archivedView ? 'primary' : 'outline'} size="sm" onClick={() => { setArchivedView(false); setFactorOffset(0); }}>在库</Button>
                <Button variant={archivedView ? 'primary' : 'outline'} size="sm" onClick={() => { setArchivedView(true); setFactorOffset(0); }}><Archive className="mr-1 h-3 w-3" />已归档</Button>
              </>}
            </div>
            {factorSource === 'mined' && archivedView && <p className="text-xs text-muted-foreground">归档保留代码、历史回测及训练引用，不删除文件或释放存储空间；恢复后可重新加入研究池。</p>}
            {factorSource === 'mined' && !archivedView && <Button variant="outline" size="sm" disabled={!isAuthenticated || isAuthenticating} onClick={() => setPoolCleanupOpen(true)}>研究池清理建议</Button>}
            {factorSource === 'mined' && !archivedView && <Button variant="outline" size="sm" disabled={!isAuthenticated || isAuthenticating || universeFilter === 'all'} title="选择一个股票池后评估其完整池成员" onClick={() => setPoolResearchOpen(true)}>{universeFilter === 'all' ? '池正交与多样性证据（先选股票池）' : '池正交与多样性证据'}</Button>}
            {factorSource === 'mined' && !archivedView && factors.some((factor) => factor.hasCode === false && !factor.readOnly && !factor.archivedAt) && <Button variant="outline" size="sm" onClick={() => setRecoveryFactors(factors.filter((factor) => factor.hasCode === false && !factor.readOnly && !factor.archivedAt).slice(0, 20))}>补码评估当前页无码因子（最多 20 个）</Button>}
            {/* QuantMind 当前仅开放 A 股因子 */}
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" size="sm" disabled>
                A股
              </Button>
            </div>
            {/* Universe filter — A-share stock pools */}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted-foreground mr-1">股票池</span>
              <Button
                variant={universeFilter === 'all' ? 'primary' : 'outline'}
                size="sm"
                onClick={() => { setUniverseFilter('all'); setFactorOffset(0); }}
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
                  onClick={() => { setUniverseFilter(u.id); setFactorOffset(0); }}
                >
                  {u.name}
                </Button>
              ))}
            </div>
            {/* Search + quality filter */}
            <div className="flex flex-col md:flex-row gap-4">
              <div className="flex-1">
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    maxLength={200}
                    placeholder="搜索因子名称、表达式或描述..."
                    className="w-full pl-10 pr-4 py-2 rounded-lg border border-input bg-background text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                </div>
              </div>
              <div className="flex gap-2">
                <Button
                  variant={qualityFilter === 'all' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => { setQualityFilter('all'); setFactorOffset(0); }}
                >
                  全部 ({stats.total})
                </Button>
                <Button
                  variant={qualityFilter === 'high' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => { setQualityFilter('high'); setFactorOffset(0); }}
                >
                  高质量 ({stats.high})
                </Button>
                <Button
                  variant={qualityFilter === 'medium' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => { setQualityFilter('medium'); setFactorOffset(0); }}
                >
                  中等 ({stats.medium})
                </Button>
                <Button
                  variant={qualityFilter === 'low' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => { setQualityFilter('low'); setFactorOffset(0); }}
                >
                  低质量 ({stats.low})
                </Button>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Factor List */}
      {isLoading && <div className="py-6 text-center text-xs text-muted-foreground">正在读取本人因子…</div>}
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="text-[11px]">已选 {exportSelection.length} 个</span>
        <Button variant="outline" size="sm" className="h-7 text-[11px]" disabled={exportSelection.length === 0 || batchMaterializing} onClick={() => void submitBatchMaterialization()}>
          {batchMaterializing ? '批量物化中…' : `批量物化 (${exportSelection.length}/12)`}
        </Button>
        <Button variant="outline" size="sm" className="h-7 text-[11px]" disabled={exportSelection.length === 0 || exportBusy} onClick={() => void submitMergedExport()}>
          {exportBusy ? '导出中…' : `合并导出 (${exportSelection.length}/12)`}
        </Button>
        {exportSelection.length > 0 && (
          <button type="button" className="text-[11px] text-muted-foreground underline" onClick={() => { setExportSelection([]); setExportMessage(''); }}>
            清空选择
          </button>
        )}
      </div>
      {exportMessage && <div className="text-[11px] text-muted-foreground" role="status">{exportMessage}</div>}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {!isLoading && filteredFactors.map((factor) => (
          <Card
            key={factor.factorId}
            className="glass card-hover cursor-pointer"
            onClick={() => handleSelectFactor(factor)}
          >
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between gap-2">
                {!factor.readOnly && (
                  <input
                    type="checkbox"
                    checked={exportSelection.includes(factor.factorId)}
                    onChange={() => toggleExportFactor(factor)}
                    onClick={(event) => event.stopPropagation()}
                    aria-label={`选择导出 ${factor.factorName}`}
                    title="选择后可批量物化或合并导出"
                    className="mt-1 h-3.5 w-3.5 shrink-0 accent-primary"
                  />
                )}
                <div className="flex-1">
                  <CardTitle className="text-sm">{factor.factorName}</CardTitle>
                  <div className="flex items-center gap-2 mt-2">
                    <Badge className={getQualityBadgeClass(factor.quality)}>
                      {factor.quality === 'high' ? '高' : factor.quality === 'medium' ? '中' : '低'}
                    </Badge>
                    {factor.archivedAt && <Badge variant="outline">已归档</Badge>}
                    {factor.readOnly && (
                      <span className="inline-flex items-center rounded-md border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                        工厂
                      </span>
                    )}
                    {factor.lifecycle?.maturity && factor.lifecycle.maturity !== 'unknown' && (
                      <Badge title={factor.lifecycle.blind_until ? `盲窗截至 ${factor.lifecycle.blind_until}` : undefined}>
                        {factor.lifecycle.maturity}
                      </Badge>
                    )}
                    {factor.tags?.family && factor.tags.family !== 'unknown' && (
                      <span className="text-xs text-muted-foreground">{factor.tags.family}</span>
                    )}
                    {factor.market && (
                      <span className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${MARKET_COLORS[factor.market] || 'bg-secondary text-muted-foreground'}`}>
                        {MARKET_LABELS[factor.market] || factor.market}
                      </span>
                    )}
                    {factor.round > 0 && (
                      <span className="text-xs text-muted-foreground">
                        Round {factor.round}
                      </span>
                    )}
                    {factor.direction && (
                      <span className="text-xs text-muted-foreground">
                        方向 {factor.direction}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-xs text-muted-foreground line-clamp-2">
                {factor.factorDescription}
              </p>
              <div className="rounded-lg bg-secondary/30 p-3">
                <div className="flex items-center gap-2 mb-2">
                  <Code className="h-3 w-3 text-muted-foreground" />
                  <span className="text-xs text-muted-foreground">表达式</span>
                </div>
                <code className="text-xs font-mono line-clamp-2">
                  {factor.factorExpression}
                </code>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <span className="text-muted-foreground">IC: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.ic, 4)}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">RankIC: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.rankIc, 4)}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">ICIR: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.icir, 3)}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">RankICIR: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.rankIcir, 3)}</span>
                </div>
              </div>
              {/* Action buttons */}
              <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-border/30">
                {factor.readOnly ? (
                  <span className="text-xs text-muted-foreground flex-1">
                    工厂因子为批量产出记录（只读），当前仅提供查看
                  </span>
                ) : factor.archivedAt ? (
                  <>
                    <span className="flex-1 text-xs text-muted-foreground">已归档，保留历史与代码</span>
                    <Button variant="outline" size="sm" disabled={Boolean(visibilityBusy)} onClick={(event) => { event.stopPropagation(); void changeFactorArchive(factor); }}>
                      <ArchiveRestore className="mr-1 h-3 w-3" />{visibilityBusy === factor.factorId ? '恢复中…' : '恢复'}
                    </Button>
                  </>
                ) : (
                  <>
                  <Button variant="outline" size="sm" className="h-7 text-xs" onClick={(event) => { event.stopPropagation(); setRecoveryFactors([factor]); }}><Code className="h-3 w-3 mr-1" />{factor.hasCode === false ? '补码并评估' : '恢复记录 / 评估'}</Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 text-xs flex-1"
                    disabled={!onBacktestFactor}
                    onClick={(e) => {
                      e.stopPropagation();
                      onBacktestFactor?.(factor.factorId);
                    }}
                  >
                    <Play className="h-3 w-3 mr-1" /> 回测
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    disabled={materializingFactorId === factor.factorId}
                    onClick={(e) => {
                      e.stopPropagation();
                      void materializeFactor(factor);
                    }}
                    title="将本人 Qlib Factor 类物化为训练可用的私有特征版本"
                  >
                    <Database className="h-3 w-3 mr-1" />
                    {materializingFactorId === factor.factorId ? '提交中' : '物化训练特征'}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    disabled={poolActionFactorId === factor.factorId}
                    onClick={(e) => {
                      e.stopPropagation();
                      void toggleFactorPool(factor);
                    }}
                    aria-label={factorPoolIds.has(poolMembershipKey(factor.factorId, factor.universe || '')) ? '从本人因子池移除' : '加入本人因子池'}
                  >
                    {factorPoolIds.has(poolMembershipKey(factor.factorId, factor.universe || '')) ? (
                      <><BookmarkCheck className="h-3 w-3 mr-1" />已入池</>
                    ) : (
                      <><BookmarkPlus className="h-3 w-3 mr-1" />入池</>
                    )}
                  </Button>
                  {factorPoolEntries.some((entry) =>
                    entry.factor_id === factor.factorId
                      && entry.universe === (factor.universe || '')
                      && Boolean(entry.panel_version),
                  ) && (
                    <Button
                      variant={comboSelection.includes(poolMembershipKey(factor.factorId, factor.universe || '')) ? 'primary' : 'outline'}
                      size="sm"
                      className="h-7 text-xs"
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleComboFactor(factor);
                      }}
                      aria-pressed={comboSelection.includes(poolMembershipKey(factor.factorId, factor.universe || ''))}
                    >
                      {comboSelection.includes(poolMembershipKey(factor.factorId, factor.universe || '')) ? '已选组合' : '选入组合'}
                    </Button>
                  )}
                  {archiveSupported && <Button variant="ghost" size="sm" className="h-7 text-xs" disabled={Boolean(visibilityBusy) || poolActionFactorId === factor.factorId || materializingFactorId === factor.factorId} title="归档后退出研究池，保留历史产物与训练引用" onClick={(event) => { event.stopPropagation(); void changeFactorArchive(factor); }}>
                    <Archive className="mr-1 h-3 w-3" />{visibilityBusy === factor.factorId ? '归档中…' : '归档'}
                  </Button>}
                  </>
                )}
              </div>
              {factor.createdAt && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Calendar className="h-3 w-3" />
                  {new Date(factor.createdAt).toLocaleString('zh-CN')}
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
        <span>{factorSource === 'mined' ? '本人挖掘' : '本人工厂'}共 {visibleTotal} 个因子，当前显示 {visibleTotal === 0 ? 0 : factorOffset + 1}–{Math.min(visibleTotal, factorOffset + filteredFactors.length)}；第 {Math.floor(factorOffset / FACTOR_PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(visibleTotal / FACTOR_PAGE_SIZE))} 页</span>
        <span className="text-[11px]">已选 {exportSelection.length} 个</span>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-7 text-[11px]" disabled={exportSelection.length === 0 || batchMaterializing} onClick={() => void submitBatchMaterialization()}>
            {batchMaterializing ? '批量物化中…' : '批量物化'}
          </Button>
          <Button variant="outline" size="sm" className="h-7 text-[11px]" disabled={exportSelection.length === 0 || exportBusy} onClick={() => void submitMergedExport()}>
            {exportBusy ? '导出中…' : '合并导出'}
          </Button>
          <Button variant="outline" size="sm" disabled={isLoading || factorOffset === 0} onClick={() => setFactorOffset(Math.max(0, factorOffset - FACTOR_PAGE_SIZE))}>上一页</Button>
          <Button variant="outline" size="sm" disabled={isLoading || factorOffset + FACTOR_PAGE_SIZE >= visibleTotal} onClick={() => setFactorOffset(factorOffset + FACTOR_PAGE_SIZE)}>下一页</Button>
        </div>
      </div>

      {/* Empty State */}
      {filteredFactors.length === 0 && !isLoading && (
        <Card className="glass">
          <CardContent className="p-8 text-center">
            <Database className="h-10 w-10 mx-auto text-muted-foreground mb-3" />
            <h3 className="text-sm font-medium mb-2">暂无因子</h3>
            <p className="text-xs text-muted-foreground">
              {searchQuery || qualityFilter !== 'all'
                ? '没有符合筛选条件的因子'
                : '开始挖掘因子后，结果将显示在这里'}
            </p>
          </CardContent>
        </Card>
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
                  <CardTitle className="text-lg">
                    {selectedFactor.factorName || selectedFactor.factor_name}
                  </CardTitle>
                  <div className="flex items-center gap-2 mt-2">
                    <Badge className={getQualityBadgeClass(selectedFactor.quality || 'medium')}>
                      {selectedFactor.quality === 'high'
                        ? '高质量'
                        : selectedFactor.quality === 'medium'
                        ? '中等质量'
                        : '低质量'}
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
                <h4 className="text-xs font-medium mb-2">因子描述</h4>
                <p className="text-xs text-muted-foreground">
                  {selectedFactor.factorDescription || selectedFactor.factor_description || '无描述'}
                </p>
              </div>

              {/* Expression */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-xs font-medium">因子表达式</h4>
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
                <div className="rounded-lg bg-secondary/30 p-3">
                  <code className="text-xs font-mono break-all">
                    {selectedFactor.factorExpression || selectedFactor.factor_expression || ''}
                  </code>
                </div>
              </div>

              {/* Formulation */}
              {(selectedFactor.factorFormulation || selectedFactor.factor_formulation) && (
                <div>
                  <h4 className="text-xs font-medium mb-2">数学公式</h4>
                  <div className="rounded-lg bg-secondary/30 p-3">
                    <code className="text-xs font-mono break-all">
                      {selectedFactor.factorFormulation || selectedFactor.factor_formulation}
                    </code>
                  </div>
                </div>
              )}

              <div>
                <h4 className="text-xs font-medium mb-2">不可变代码版本</h4>
                {sourceRevisions.length === 0 ? (
                  <p className="text-xs text-muted-foreground">暂无版本账本记录；旧因子将在迁移后登记当前代码。</p>
                ) : (
                  <div className="space-y-2">
                    {sourceRevisions.map((revision) => (
                      <details key={revision.revision_no} className="rounded-lg bg-secondary/30 p-3">
                        <summary className="cursor-pointer text-sm">
                          v{revision.revision_no} · {new Date(revision.created_at).toLocaleString('zh-CN')}
                          <span className="ml-2 font-mono text-xs text-muted-foreground" title={revision.factor_code_sha256}>
                            SHA-256 {revision.factor_code_sha256.slice(0, 12)}…
                          </span>
                        </summary>
                        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs font-mono">
                          {revision.factor_code}
                        </pre>
                      </details>
                    ))}
                  </div>
                )}
              </div>

              {/* Backtest Results */}
              {(selectedFactor.backtestResults || selectedFactor.backtest_results) && (
                <div>
                  <h4 className="text-xs font-medium mb-2">回测指标</h4>
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

              {selectedFactor.archivedAt && <div className="rounded-lg border border-border/60 bg-secondary/30 p-3 text-sm">
                <p>此因子已于 {new Date(selectedFactor.archivedAt).toLocaleString('zh-CN')} 归档，当前仅供历史查看。</p>
                {selectedFactor.archiveReason && <p className="mt-1 break-words text-xs text-muted-foreground">原因：{selectedFactor.archiveReason}</p>}
              </div>}
              {!selectedFactor.readOnly && (
                <div className="rounded-lg border border-border/60 bg-secondary/20 p-4 space-y-3">
                  <div>
                    <h4 className="text-xs font-medium">生命周期评估</h4>
                    <p className="mt-1 text-xs text-muted-foreground">
                      使用本人最新完成回测中的精确日度 IC。预览不会写入；确认后才保存评估，并按连续月末规则降权。
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      disabled={lifecycleEvaluationBusy || Boolean(selectedFactor.archivedAt)}
                      onClick={() => void runLifecycleEvaluation(false)}
                    >
                      {lifecycleEvaluationBusy ? '评估中…' : '预览评估'}
                    </Button>
                    {lifecycleEvaluation && !lifecycleEvaluation.applied && (
                      <Button
                        disabled={lifecycleEvaluationBusy || Boolean(selectedFactor.archivedAt)}
                        onClick={() => void runLifecycleEvaluation(true)}
                      >
                        {lifecycleEvaluation.action === 'downweight'
                          ? '确认降权并保存'
                          : '保存本次评估'}
                      </Button>
                    )}
                    {selectedFactor.lifecycle?.state === 'downweighted' && (
                      <Button
                        variant="outline"
                        disabled={lifecycleEvaluationBusy || Boolean(selectedFactor.archivedAt)}
                        onClick={() => void restoreSelectedLifecycle()}
                      >
                        人工恢复
                      </Button>
                    )}
                  </div>
                  {lifecycleEvaluationError && (
                    <p className="text-sm text-destructive">{lifecycleEvaluationError}</p>
                  )}
                  {lifecycleEvaluation && (
                    <div className="text-sm space-y-1" aria-live="polite">
                      {lifecycleEvaluation.eval_status === 'blind_window' ? (
                        <p>观察盲窗内，评估读数已隐藏且不会触发降权。</p>
                      ) : lifecycleEvaluation.eval_status === 'no_lifecycle_series'
                        || lifecycleEvaluation.eval_status === 'no_series' ? (
                        <p className="text-muted-foreground">没有可用的精确日度 IC 快照；完成新的回测后可重新评估。</p>
                      ) : (
                        <>
                          <p>
                            {lifecycleEvaluation.action === 'downweight'
                              ? '最近三个以上月末连续低于 ICIR 0.2，建议降权。'
                              : lifecycleEvaluation.action === 'already'
                                ? '该因子已降权，当前证据仍满足降权条件。'
                                : '当前证据未达到降权条件。'}
                          </p>
                          <p className="text-xs text-muted-foreground">
                            最新月末 {lifecycleEvaluation.latest?.month_end || '—'}
                            {' · '}ICIR {typeof lifecycleEvaluation.latest?.icir === 'number'
                              ? formatNumber(lifecycleEvaluation.latest.icir, 3)
                              : '—'}
                            {' · '}连续弱读数 {lifecycleEvaluation.streak ?? 0}
                          </p>
                        </>
                      )}
                      {lifecycleEvaluation.applied && <p className="text-xs text-success">评估结果已保存。</p>}
                    </div>
                  )}
                  {lifecycleHistoryError && (
                    <p className="text-xs text-destructive">{lifecycleHistoryError}</p>
                  )}
                  {lifecycleEvents.length > 0 && (
                    <div className="border-t border-border/60 pt-3 space-y-2">
                      <h5 className="text-xs font-medium">评估与恢复历史</h5>
                      <div className="space-y-2">
                        {lifecycleEvents.map((event) => {
                          const snapshot = event.event_snapshot || {};
                          const evaluation = snapshot.evaluation as Record<string, unknown> | undefined;
                          const label = event.redacted
                            ? '观察盲窗内，历史读数已隐藏'
                            : event.event_type === 'manual_restore'
                              ? `人工恢复：${String(snapshot.reason || '已恢复')}`
                              : `评估：${String(evaluation?.action || '无状态变更')}`;
                          return (
                            <div key={event.event_id} className="text-xs text-muted-foreground flex flex-wrap justify-between gap-x-3 gap-y-1">
                              <span>{label}</span>
                              <span>
                                {new Date(event.evaluated_at).toLocaleString('zh-CN')}
                                {!event.redacted && event.factor_revision_no
                                  ? ` · v${event.factor_revision_no}`
                                  : ''}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                      {lifecycleHistoryCursor && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={lifecycleHistoryLoading}
                          onClick={() => void loadMoreLifecycleHistory(
                            selectedFactor.factorId || selectedFactor.factor_id || '',
                            lifecycleHistoryCursor,
                          )}
                        >
                          {lifecycleHistoryLoading ? '读取中…' : '加载更早记录'}
                        </Button>
                      )}
                    </div>
                  )}
                </div>
              )}

              {selectedIsPoolMember && (
                <div>
                  <h4 className="text-xs font-medium mb-2">本人因子池评分</h4>
                  {selectedPoolEntry ? (
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4 text-sm">
                      <div className="rounded-lg bg-secondary/30 p-3">检索分：{selectedPoolEntry.pool_score == null ? '—' : selectedPoolEntry.pool_score.toFixed(4)}</div>
                      <div className="rounded-lg bg-secondary/30 p-3">新颖度：{selectedPoolEntry.novelty == null ? '—' : selectedPoolEntry.novelty.toFixed(3)}</div>
                      <div className="rounded-lg bg-secondary/30 p-3">池内最大相关：{selectedPoolEntry.max_pool_corr == null ? '—' : selectedPoolEntry.max_pool_corr.toFixed(3)}</div>
                      <div className="rounded-lg bg-secondary/30 p-3">检索次数：{selectedPoolEntry.times_retrieved}</div>
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground mb-4">暂无池评分；可在页面顶部重算本人池评分。</p>
                  )}
                  <h4 className="text-xs font-medium mb-2">本人值级面板</h4>
                  {factorPanelLoading ? (
                    <p className="text-xs text-muted-foreground">读取本人面板摘要中…</p>
                  ) : factorPanelError ? (
                    <p className="text-sm text-destructive">{factorPanelError}</p>
                  ) : !factorPanel || factorPanel.points.length === 0 ? (
                    <p className="text-xs text-muted-foreground">暂无面板。完成该因子的本人回测后可生成面板。</p>
                  ) : (
                    <div className="rounded-lg bg-secondary/30 p-3">
                      <p className="text-xs text-muted-foreground mb-2">
                        最多 60 个日期；展示日均截面秩分位、z-score 与覆盖标的数。
                      </p>
                      <div className="max-h-48 overflow-y-auto">
                        <table className="w-full text-xs">
                          <thead className="text-muted-foreground">
                            <tr>
                              <th className="text-left py-1">日期</th>
                              <th className="text-right py-1">覆盖</th>
                              <th className="text-right py-1">均值秩分位</th>
                              <th className="text-right py-1">均值 z-score</th>
                            </tr>
                          </thead>
                          <tbody>
                            {factorPanel.points.slice(-10).reverse().map((point) => (
                              <tr key={point.trade_date} className="border-t border-border/40">
                                <td className="py-1">{point.trade_date}</td>
                                <td className="text-right py-1">{point.coverage}</td>
                                <td className="text-right py-1">{point.mean_rank_pct.toFixed(3)}</td>
                                <td className="text-right py-1">{point.mean_zscore.toFixed(3)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}
                </div>
              )}

              <div>
                <h4 className="text-xs font-medium mb-2">样本外验证记录</h4>
                {validationHistoryLoading ? (
                  <p className="text-xs text-muted-foreground">读取本人验证记录中…</p>
                ) : validationHistoryError ? (
                  <p className="text-sm text-destructive">{validationHistoryError}</p>
                ) : validationAttempts.length === 0 ? (
                  <p className="text-xs text-muted-foreground">暂无样本外验证证据</p>
                ) : (
                  <div className="space-y-2">
                    {validationAttempts.map((attempt) => {
                      const metrics = attempt.result_snapshot?.metrics ?? {};
                      const validationWindow = attempt.validation_context?.validation_window;
                      return (
                        <div key={attempt.validation_id} className="rounded-lg bg-secondary/30 p-3 space-y-1">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex items-center gap-2">
                              <Badge variant={attempt.status === 'completed' ? 'success' : 'destructive'}>
                                {attempt.status === 'completed' ? '验证完成' : '验证失败'}
                              </Badge>
                              {attempt.is_latest && <Badge variant="outline">最新尝试</Badge>}
                            </div>
                            <span className="text-xs text-muted-foreground">
                              {attempt.created_at ? new Date(attempt.created_at).toLocaleString('zh-CN') : ''}
                            </span>
                          </div>
                          {validationWindow && (
                            <p className="text-xs text-muted-foreground">
                              验证区间 {validationWindow.start_date} 至 {validationWindow.end_date}
                              {attempt.validation_context?.non_overlapping ? ' · 与采矿区间不重叠' : ''}
                            </p>
                          )}
                          {attempt.status === 'completed' ? (
                            <p className="text-xs">
                              IC {typeof metrics.ic_mean === 'number' ? formatNumber(metrics.ic_mean, 4) : '—'}
                              {' · '}Rank IC 中位数 {typeof metrics.rank_ic_median === 'number' ? formatNumber(metrics.rank_ic_median, 4) : '—'}
                            </p>
                          ) : (
                            <p className="text-xs text-destructive">{attempt.error_code || '验证任务失败'}</p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Meta */}
              <div>
                <h4 className="text-xs font-medium mb-2">元信息</h4>
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
      {recoveryFactors && <FactorRecoveryDialog factors={recoveryFactors} universe={universeFilter === 'all' ? 'csi300' : universeFilter} onClose={closeRecovery} onChanged={() => setFactorRefreshSeq((value) => value + 1)} />}
      {poolCleanupOpen && <PoolCleanupDialog universe={universeFilter === 'all' ? undefined : universeFilter} onClose={closePoolCleanup} onRemoved={onPoolCleanupRemoved} />}
      {poolResearchOpen && universeFilter !== 'all' && <PoolResearchDialog universe={universeFilter} onClose={closePoolResearch} />}
    </div>
  );
};
