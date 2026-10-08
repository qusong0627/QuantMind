/**
 * MaterializeBar — 结果区/因子库的粘性操作条（选中 → 物化 / 回测）。
 *
 * 状态与队列走 RunQueueContext（跨页共享同一份服务端真相）：
 * - 「物化运行中」只在服务端响应点亮（RunQueueContext 纪律），显示别人的
 *   运行也如实（可能是管理员在跑）；
 * - 跳过/拒绝明细直接摊开（不猜、不吞）：already_materialized / no_code /
 *   market_unsupported / rejected_duplicate / rejected_gate……
 */

import React, { useState } from 'react';
import { FlaskConical, Loader2, PlayCircle, X } from 'lucide-react';
import type { Factor } from '../types-v2';
import { useBacktestQueue, useMaterializeRun } from '../context-v2/RunQueueContext';
import { extractApiDetail } from '../services-v2/materialize';
import { cn } from '../utils-v2';

const SKIP_REASON_LABELS: Record<string, string> = {
  already_materialized: '已物化',
  rejected_duplicate: '值级重复被拒（需 force 重跑）',
  rejected_gate: '门禁拒入（需 force 重跑）',
  no_code: '无代码',
  market_unsupported: '非 A 股市场',
  not_found: '不存在或无权访问',
};

export function describeSkipReasons(skipped: Record<string, string>): Array<{ label: string; count: number }> {
  const counts = new Map<string, number>();
  for (const reason of Object.values(skipped)) {
    counts.set(reason, (counts.get(reason) ?? 0) + 1);
  }
  return [...counts.entries()].map(([reason, count]) => ({
    label: SKIP_REASON_LABELS[reason] ?? reason,
    count,
  }));
}

export interface MaterializeBarProps {
  /** 已勾选的因子（调用方已滤掉 ownerless/readOnly） */
  selected: Factor[];
  onClearSelection: () => void;
  /** 物化/回测任一动作有行终结时回调（宿主页刷新清单） */
  onSettledRefresh?: () => void;
}

export const MaterializeBar: React.FC<MaterializeBarProps> = ({
  selected,
  onClearSelection,
  onSettledRefresh,
}) => {
  const backtestQueue = useBacktestQueue();
  const materialize = useMaterializeRun();
  const [busy, setBusy] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const selectedIds = selected.map((f) => f.factorId);
  const hasSelection = selectedIds.length > 0;

  const handleMaterialize = async () => {
    if (!hasSelection || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await materialize.start(selectedIds, { onCompleted: onSettledRefresh });
      setDetailsOpen(true);
    } catch (err) {
      // 409（锁忙）/400（非法 id）detail 原文必须上屏
      setActionError(extractApiDetail(err, '启动物化失败'));
    } finally {
      setBusy(false);
    }
  };

  const handleBacktest = () => {
    if (!hasSelection) return;
    backtestQueue.enqueue(selectedIds, {
      onSettled: () => onSettledRefresh?.(),
    });
    onClearSelection();
  };

  const result = materialize.lastResult;
  const skipSummary = result ? describeSkipReasons(result.skipped) : [];
  const rejectedCount = result?.rejected.length ?? 0;

  return (
    <div className="sticky top-0 z-20 mb-2 rounded-lg border border-border/60 bg-card/95 px-3 py-2 shadow-sm backdrop-blur">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">
          已选 <span className="font-mono font-semibold text-foreground">{selected.length}</span>
        </span>
        <button
          type="button"
          onClick={handleMaterialize}
          disabled={!hasSelection || busy || materialize.running}
          title={
            materialize.running
              ? '物化运行中（独占训练库），结束后才能再启动'
              : '把选中因子送入 rd_mined 训练库物化'
          }
          className="inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-2 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="h-3.5 w-3.5" />}
          物化选中
        </button>
        <button
          type="button"
          onClick={handleBacktest}
          disabled={!hasSelection}
          title="对选中因子逐个发起轻量回测（并发 2）"
          className="inline-flex items-center gap-1 rounded-md border border-border/60 px-2 py-1 text-xs font-medium text-foreground/80 transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <PlayCircle className="h-3.5 w-3.5" />
          回测选中
        </button>
        <button
          type="button"
          onClick={onClearSelection}
          disabled={!hasSelection}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted/50 disabled:opacity-40"
        >
          <X className="h-3.5 w-3.5" />
          清除
        </button>

        <div className="ml-auto flex items-center gap-3">
          {backtestQueue.activeCount > 0 && (
            <span className="text-[11px] text-muted-foreground">
              回测队列 {backtestQueue.activeCount} 个进行中
            </span>
          )}
          {materialize.running && (
            <span className="inline-flex items-center gap-1 text-[11px] font-medium text-blue-500">
              <Loader2 className="h-3 w-3 animate-spin" />
              物化运行中
            </span>
          )}
        </div>
      </div>

      {(actionError || materialize.warning) && (
        <div className="mt-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-700">
          {actionError ?? materialize.warning}
        </div>
      )}

      {result && (skipSummary.length > 0 || rejectedCount > 0 || result.materializable.length > 0) && (
        <div className="mt-1.5 text-[11px] text-muted-foreground">
          {result.started ? (
            <>
              本轮送入 <span className="font-mono text-foreground">{result.materializable.length}</span> 个
            </>
          ) : (
            <>{result.message || '没有可物化的因子'}</>
          )}
          {skipSummary.length > 0 && (
            <>
              {' · 跳过 '}
              <button
                type="button"
                className="text-amber-600 underline decoration-dotted hover:text-amber-700"
                onClick={() => setDetailsOpen((v) => !v)}
              >
                {skipSummary.reduce((n, s) => n + s.count, 0)} 个
              </button>
            </>
          )}
          {rejectedCount > 0 && <>{` · 拒绝 ${rejectedCount} 个（不存在或无权访问）`}</>}
          {skipSummary.length > 0 && detailsOpen && (
            <ul className="mt-1 space-y-0.5 rounded border border-border/50 bg-muted/20 px-2 py-1">
              {skipSummary.map((s) => (
                <li key={s.label} className="flex justify-between gap-3">
                  <span>{s.label}</span>
                  <span className="font-mono">{s.count}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
};

export default MaterializeBar;
