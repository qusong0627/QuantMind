/**
 * FactorList — 本轮挖掘结果区（机构级密集表）。
 *
 * 组成：粘性操作条（MaterializeBar）+ 密集因子表（FactorTable）+ 详情抽屉。
 * 数据纪律：
 * - 挖到多少显示多少：清单来自 metrics.factors（TaskContext 经 /factors?task_id=…
 *   limit=500 全量合并，不再截 Top10）；
 * - 指标缺失一律「—」、真 0 显 0（formatMetricValue），红涨绿跌只用于有方向的量；
 * - 行级回测/物化走 RunQueueContext（跨页共享）；「看图表」经 attachBacktestTask
 *   把既有回测载入回测页（不重跑）。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';
import type { Factor, RealtimeMetrics } from '../types-v2';
import { alphaAgentService } from '../services/alphaAgentService';
import { FACTOR_LIST_MAX_LIMIT } from '../services-v2/api';
import { useTaskContext } from '../context-v2/TaskContext';
import { useBacktestQueue, useMaterializeRun } from '../context-v2/RunQueueContext';
import { FactorTable } from './FactorTable';
import { MaterializeBar } from './MaterializeBar';
import { FactorDetailDrawer } from './FactorDetailDrawer';
import type { BacktestRunEntry } from '../context-v2/RunQueueContext';

interface FactorListProps {
  metrics: RealtimeMetrics | null;
  onNavigate?: (page: string) => void;
}

type PromoteState = 'loading' | 'done' | 'error';

/**
 * 默认排序：RankIC 降序、缺失恒排最后（缺失不是「小」）；tie → createdAt desc
 * → factorId。用户点表头后由 FactorTable 接管排序。
 */
function sortByRankIc(factors: Factor[]): Factor[] {
  const next = [...factors];
  next.sort((a, b) => {
    const va = a.rankIc;
    const vb = b.rankIc;
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (va !== vb) return vb - va;
    return 0;
  });
  return next;
}

export const FactorList: React.FC<FactorListProps> = ({ metrics, onNavigate }) => {
  const { refreshMiningFactors, attachBacktestTask } = useTaskContext();
  const backtestQueue = useBacktestQueue();
  const materialize = useMaterializeRun();

  const rawFactors = metrics?.factors;
  const factors = useMemo(() => sortByRankIc(rawFactors ?? []), [rawFactors]);

  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [promoting, setPromoting] = useState<Record<string, PromoteState>>({});

  // 清单变化时清掉已消失的勾选（因子被并发刷新移除/换任务）
  useEffect(() => {
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      const present = new Set(factors.map((f) => f.factorId));
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (present.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [factors]);

  const selectableIds = useMemo(
    () => factors.filter((f) => !f.ownerless && !f.readOnly).map((f) => f.factorId),
    [factors],
  );
  const selected = useMemo(
    () => factors.filter((f) => selectedIds.has(f.factorId)),
    [factors, selectedIds],
  );
  const detailFactor = useMemo(
    () => (detailId ? factors.find((f) => f.factorId === detailId) ?? null : null),
    [factors, detailId],
  );

  // 物化/回测任一行动终结 → 拉一次权威清单（物化 chip、回测指标都是服务端口径）
  const handleSettledRefresh = useCallback(() => {
    void refreshMiningFactors();
  }, [refreshMiningFactors]);

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

  const handleBacktest = useCallback(
    (factorId: string) => {
      backtestQueue.enqueue([factorId], { onSettled: handleSettledRefresh });
    },
    [backtestQueue, handleSettledRefresh],
  );

  const handleMaterialize = useCallback(
    (factorId: string) => {
      // 失败原文由 RunQueueContext 置入 materialize.warning（MaterializeBar 告警行上屏）
      void materialize
        .start([factorId], { onCompleted: handleSettledRefresh })
        .catch(() => {});
    },
    [materialize, handleSettledRefresh],
  );

  const handleViewBacktest = useCallback(
    (factorId: string) => {
      void attachBacktestTask(factorId).catch((err) => {
        console.error('[alpha-research] attach backtest failed:', err);
      });
      onNavigate?.('backtest');
    },
    [attachBacktestTask, onNavigate],
  );

  // 训练 = promoteByExpression 进训练特征集（保留的既有出口；物化是另一条链路）
  const handlePromote = useCallback(async (factor: Factor) => {
    setPromoting((prev) => ({ ...prev, [factor.factorId]: 'loading' }));
    try {
      const result = await alphaAgentService.promoteByExpression([
        { name: factor.factorName, expression: factor.factorExpression },
      ]);
      setPromoting((prev) => ({
        ...prev,
        [factor.factorId]:
          result.success && result.promoted.length > 0 ? 'done' : 'error',
      }));
    } catch {
      setPromoting((prev) => ({ ...prev, [factor.factorId]: 'error' }));
    }
  }, []);

  const btEntriesForDetail: BacktestRunEntry | undefined = detailFactor
    ? backtestQueue.entries[detailFactor.factorId]
    : undefined;

  return (
    <>
      <Card className="glass animate-fade-in-up w-full">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <div className="h-2 w-2 rounded-full bg-purple-500 animate-pulse" />
            本轮挖掘因子（{factors.length}）
            <span className="text-[11px] font-normal text-muted-foreground">
              · 按 RankIC 降序
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <MaterializeBar
            selected={selected}
            onClearSelection={handleClearSelection}
            onSettledRefresh={handleSettledRefresh}
          />
          <FactorTable
            factors={factors}
            selectedIds={selectedIds}
            onToggleSelect={handleToggleSelect}
            onToggleSelectAll={handleToggleSelectAll}
            backtestEntries={backtestQueue.entries}
            materializingIds={materialize.runningIds}
            materializeRunning={materialize.running}
            onOpenDetail={setDetailId}
            onBacktest={handleBacktest}
            onMaterialize={handleMaterialize}
            onViewBacktest={handleViewBacktest}
            emptyText="暂无因子数据（挖掘产出落库后会自动出现）"
            serverLimit={FACTOR_LIST_MAX_LIMIT}
          />
        </CardContent>
      </Card>

      <FactorDetailDrawer
        factor={detailFactor}
        onClose={() => setDetailId(null)}
        onBacktest={handleBacktest}
        onMaterialize={handleMaterialize}
        onViewBacktest={handleViewBacktest}
        btEntry={btEntriesForDetail}
        isMaterializing={Boolean(
          detailFactor &&
            materialize.running &&
            materialize.runningIds.has(detailFactor.factorId),
        )}
        onPromote={handlePromote}
        promoteState={detailFactor ? promoting[detailFactor.factorId] : undefined}
      />
    </>
  );
};

export default FactorList;
