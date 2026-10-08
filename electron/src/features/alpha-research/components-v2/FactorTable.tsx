/**
 * FactorTable — 机构级密集因子表（挖掘结果区与因子库共用）。
 *
 * 数据纪律：
 * - 数值列一律走 `metricRegistry.formatMetricValue`：缺失 → `—`，**真 0 显示 0**；
 * - 有方向的 IC/收益族按 A 股口径红涨绿跌（metricToneClass）；
 *   MDD/换手/RRE/PFS 是中性量，单色不加色；
 * - 行 key = factorId（旧实现 key=index，实时更新时选中/状态串行）；
 * - 客户端排序：点表头 desc → asc → 默认（给什么顺序是什么顺序）；
 *   undefined 恒排最后（缺失不是「小」）；tie → createdAt desc → factorId。
 */

import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronsUpDown, Loader2, SlidersHorizontal } from 'lucide-react';
import type { Factor } from '../types-v2';
import type { BacktestRunEntry } from '../context-v2/RunQueueContext';
import { formatMetricValue } from '../services-v2/metricRegistry';
import { cn, getQualityBadgeClass, metricToneClass } from '../utils-v2';

/** 扩展列开关持久化（两个页面共享一个开关） */
const EXTENDED_COLS_STORAGE_KEY = 'qa_factor_cols_ext';
/** 初始渲染行数；更多走「显示全部」 */
const DEFAULT_VISIBLE_LIMIT = 200;

const QUALITY_LABELS: Record<string, { text: string; title: string }> = {
  high: { text: '高', title: '高质量：|IC| ≥ 0.05' },
  medium: { text: '中', title: '中等：0.02 ≤ |IC| < 0.05' },
  low: { text: '低', title: '低质量：|IC| < 0.02' },
  unknown: { text: '—', title: 'IC 缺失，质量无从分级' },
};

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  crypto: '加密货币',
  hong_kong: '港股',
  us_stock: '美股',
  futures: '期货',
};

// ========================== 列定义 ==========================

type SortKey =
  | 'name'
  | 'rank_ic'
  | 'ic'
  | 'icir'
  | 'rank_icir'
  | 'annual_return'
  | 'max_drawdown'
  | 'sharpe_ratio'
  | 'ann_return_net'
  | 'ann_turnover'
  | 'quality.pfs'
  | 'rre';

interface FactorColumn {
  key: SortKey;
  label: string;
  /** 注册表键（formatMetricValue 用） */
  metricKey: string;
  accessor: (f: Factor) => number | undefined;
  /** 有方向的量：红涨绿跌着色 */
  toned?: boolean;
  extended?: boolean;
}

const COLUMNS: FactorColumn[] = [
  { key: 'rank_ic', label: 'RankIC', metricKey: 'rank_ic', accessor: (f) => f.rankIc, toned: true },
  { key: 'ic', label: 'IC', metricKey: 'ic', accessor: (f) => f.ic, toned: true },
  { key: 'rank_icir', label: 'RankICIR', metricKey: 'rank_icir', accessor: (f) => f.rankIcir, toned: true },
  { key: 'icir', label: 'ICIR', metricKey: 'icir', accessor: (f) => f.icir, toned: true },
  { key: 'annual_return', label: 'ARR', metricKey: 'annual_return', accessor: (f) => f.annualReturn, toned: true },
  { key: 'max_drawdown', label: 'MDD', metricKey: 'max_drawdown', accessor: (f) => f.maxDrawdown },
  { key: 'sharpe_ratio', label: 'Sharpe', metricKey: 'sharpe_ratio', accessor: (f) => f.sharpeRatio, toned: true },
  { key: 'ann_return_net', label: '净ARR', metricKey: 'ann_return_net', accessor: (f) => f.annReturnNet, toned: true, extended: true },
  { key: 'ann_turnover', label: '年化换手', metricKey: 'ann_turnover', accessor: (f) => f.annTurnover, extended: true },
  { key: 'quality.pfs', label: 'PFS', metricKey: 'quality.pfs', accessor: (f) => f.pfsQuality?.pfs, extended: true },
  { key: 'rre', label: 'RRE', metricKey: 'rre', accessor: (f) => f.rre, extended: true },
];

// ========================== 排序 ==========================

type SortState = { key: SortKey; dir: 1 | -1 } | null;

function tieBreak(a: Factor, b: Factor): number {
  const ta = a.createdAt || '';
  const tb = b.createdAt || '';
  if (ta !== tb) return ta < tb ? 1 : -1; // createdAt desc
  return a.factorId < b.factorId ? -1 : a.factorId > b.factorId ? 1 : 0;
}

function compareByColumn(a: Factor, b: Factor, key: SortKey, dir: 1 | -1): number {
  if (key === 'name') {
    const va = a.factorName || '';
    const vb = b.factorName || '';
    if (va === vb) return tieBreak(a, b);
    return dir * (va < vb ? -1 : 1);
  }
  const col = COLUMNS.find((c) => c.key === key);
  if (!col) return tieBreak(a, b);
  const va = col.accessor(a);
  const vb = col.accessor(b);
  // 缺失恒排最后（与方向无关）——「没算过」不是「小」
  if (va == null && vb == null) return tieBreak(a, b);
  if (va == null) return 1;
  if (vb == null) return -1;
  if (va === vb) return tieBreak(a, b);
  return dir * (va < vb ? -1 : 1);
}

// ========================== 行内状态 chip ==========================

type ChipSpec = { text: string; className: string; title?: string } | null;

export function materializationChip(factor: Factor, isMaterializing: boolean): ChipSpec {
  if (isMaterializing) {
    return {
      text: '物化中',
      className: 'bg-blue-500/10 text-blue-500 border-blue-500/30',
      title: '本轮物化包含此因子，等待服务端确认完成',
    };
  }
  const mat = factor.materialization;
  if (!mat || mat.status === 'none') return null;
  switch (mat.status) {
    case 'materialized':
      return {
        text: '已物化',
        className: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30',
        title: `训练库列：${mat.column ?? '—'}${mat.at ? `（${mat.at}）` : ''}`,
      };
    case 'rejected_duplicate':
      return {
        text: '重复被拒',
        className: 'bg-amber-500/10 text-amber-600 border-amber-500/30',
        title:
          mat.corr != null
            ? `与库内 ${mat.corrAgainst ?? '既有因子'} 的 |ρ|=${mat.corr.toFixed(4)}，需 force 重跑才会再送`
            : '与库内既有因子值级重复，需 force 重跑',
      };
    case 'rejected_gate':
      return {
        text: '门禁拒入',
        className: 'bg-amber-500/10 text-amber-600 border-amber-500/30',
        title: '质量门禁未过（hard 项），需 force 重跑',
      };
    case 'error':
      return {
        text: '物化失败',
        className: 'bg-rose-500/10 text-rose-500 border-rose-500/30',
        title: mat.error || '物化失败（可在下次物化自动重试）',
      };
    default:
      return null;
  }
}

export function backtestChip(entry: BacktestRunEntry | undefined): ChipSpec {
  if (!entry || entry.status === 'idle') return null;
  switch (entry.status) {
    case 'queued':
      return { text: '排队中', className: 'bg-muted/40 text-muted-foreground border-border/60' };
    case 'running':
      return { text: '回测中', className: 'bg-blue-500/10 text-blue-500 border-blue-500/30' };
    case 'completed':
      return {
        text: '已完成',
        className: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30',
        title: '点击「看图表」进入回测页查看曲线',
      };
    case 'failed':
      return {
        text: '失败',
        className: 'bg-rose-500/10 text-rose-500 border-rose-500/30',
        title: entry.error || '回测失败',
      };
    case 'cancelled':
      return { text: '已取消', className: 'bg-muted/40 text-muted-foreground border-border/60' };
    default:
      return null;
  }
}

// ========================== 行 ==========================

interface FactorRowProps {
  factor: Factor;
  index: number;
  selected: boolean;
  btEntry?: BacktestRunEntry;
  isMaterializing: boolean;
  showExtended: boolean;
  onToggleSelect: (factorId: string) => void;
  onOpenDetail: (factorId: string) => void;
  onBacktest: (factorId: string) => void;
  onMaterialize: (factorId: string) => void;
  onViewBacktest: (factorId: string) => void;
}

const FactorRow = memo<FactorRowProps>(function FactorRow({
  factor,
  index,
  selected,
  btEntry,
  isMaterializing,
  showExtended,
  onToggleSelect,
  onOpenDetail,
  onBacktest,
  onMaterialize,
  onViewBacktest,
}) {
  const quality = QUALITY_LABELS[factor.quality] ?? QUALITY_LABELS.unknown;
  const matChip = materializationChip(factor, isMaterializing);
  const btChip = backtestChip(btEntry);
  const readOnly = Boolean(factor.ownerless || factor.readOnly);
  const readOnlyTitle = factor.ownerless ? '历史因子（无归属），只读' : '共享因子，只读';
  const runningBt = btEntry?.status === 'running' || btEntry?.status === 'queued';

  const cellTone = (value: number | undefined, toned?: boolean) =>
    toned ? metricToneClass(value) : undefined;

  return (
    <tr
      className={cn(
        'h-8 border-b border-border/40 transition-colors hover:bg-muted/40',
        selected && 'bg-primary/[0.06]',
      )}
    >
      <td className="w-8 px-2 text-center">
        <input
          type="checkbox"
          className="h-3.5 w-3.5 accent-primary align-middle"
          checked={selected}
          disabled={readOnly}
          title={readOnly ? readOnlyTitle : '选中后可批量物化/回测'}
          onChange={() => onToggleSelect(factor.factorId)}
        />
      </td>
      <td className="w-10 px-1 text-right font-mono text-[10px] text-muted-foreground/70">
        {index + 1}
      </td>
      <td className="max-w-[220px] px-2">
        <button
          type="button"
          onClick={() => onOpenDetail(factor.factorId)}
          className="flex w-full min-w-0 items-center gap-1.5 text-left"
          title={`${factor.factorName}（点击查看详情）`}
        >
          <span className="truncate text-[11px] font-medium">{factor.factorName}</span>
          <span
            className={cn(
              'shrink-0 rounded border px-1 text-[9px] leading-4',
              getQualityBadgeClass(factor.quality),
            )}
            title={quality.title}
          >
            {quality.text}
          </span>
          {readOnly && (
            <span className="shrink-0 text-[9px] text-muted-foreground/60" title={readOnlyTitle}>
              只读
            </span>
          )}
        </button>
      </td>
      <td className="px-2 text-[10px] text-muted-foreground">
        {factor.market ? MARKET_LABELS[factor.market] || factor.market : '—'}
      </td>
      {COLUMNS.filter((c) => showExtended || !c.extended).map((col) => {
        const value = col.accessor(factor);
        return (
          <td
            key={col.key}
            className={cn(
              'px-2 text-right font-mono tabular-nums',
              value == null && 'text-muted-foreground/50',
              cellTone(value, col.toned),
            )}
          >
            {formatMetricValue(col.metricKey, value)}
          </td>
        );
      })}
      {showExtended && (
        <td
          className="max-w-[220px] truncate px-2 font-mono text-[10px] text-muted-foreground"
          title={factor.factorExpression}
        >
          {factor.factorExpression || '—'}
        </td>
      )}
      <td className="px-2 text-center">
        <span className="inline-flex items-center gap-1">
          {matChip && (
            <span
              className={cn('rounded border px-1 text-[9px] leading-4', matChip.className)}
              title={matChip.title}
            >
              {matChip.text}
            </span>
          )}
          {btChip && (
            <span
              className={cn(
                'inline-flex items-center gap-0.5 rounded border px-1 text-[9px] leading-4',
                btChip.className,
              )}
              title={btChip.title}
            >
              {btEntry?.status === 'running' && <Loader2 className="h-2.5 w-2.5 animate-spin" />}
              {btChip.text}
            </span>
          )}
          {!matChip && !btChip && <span className="text-[10px] text-muted-foreground/40">—</span>}
        </span>
      </td>
      <td className="px-2">
        <div className="flex items-center justify-end gap-1">
          {btEntry?.status === 'completed' ? (
            <button
              type="button"
              onClick={() => onViewBacktest(factor.factorId)}
              className="rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-600 transition-colors hover:bg-emerald-500/20"
              title="进入回测页查看曲线"
            >
              看图表
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onBacktest(factor.factorId)}
              disabled={readOnly || runningBt}
              title={readOnly ? readOnlyTitle : runningBt ? '已在回测队列中' : '对因子发起轻量回测'}
              className="rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-foreground/70 transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-40"
            >
              回测
            </button>
          )}
          {!readOnly && (
            <button
              type="button"
              onClick={() => onMaterialize(factor.factorId)}
              disabled={isMaterializing}
              title={
                factor.materialization?.status === 'materialized'
                  ? '已物化（重复物化会被跳过；如需重算用 force）'
                  : '送入 rd_mined 训练库物化'
              }
              className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              物化
            </button>
          )}
        </div>
      </td>
    </tr>
  );
});

// ========================== 表 ==========================

export interface FactorTableProps {
  factors: Factor[];
  selectedIds: ReadonlySet<string>;
  onToggleSelect: (factorId: string) => void;
  /** 全选/清空可勾选行（readOnly/ownerless 不在内） */
  onToggleSelectAll: (checked: boolean) => void;
  backtestEntries?: Record<string, BacktestRunEntry>;
  /** 物化运行叠加层（materialize.runningIds） */
  materializingIds?: ReadonlySet<string>;
  materializeRunning?: boolean;
  onOpenDetail: (factorId: string) => void;
  onBacktest: (factorId: string) => void;
  onMaterialize: (factorId: string) => void;
  onViewBacktest: (factorId: string) => void;
  emptyText?: string;
  /** 服务端单次上限（清单长度触顶时给出诚实提示） */
  serverLimit?: number;
}

export const FactorTable: React.FC<FactorTableProps> = ({
  factors,
  selectedIds,
  onToggleSelect,
  onToggleSelectAll,
  backtestEntries,
  materializingIds,
  materializeRunning,
  onOpenDetail,
  onBacktest,
  onMaterialize,
  onViewBacktest,
  emptyText = '暂无因子数据',
  serverLimit,
}) => {
  const [showExtended, setShowExtended] = useState<boolean>(
    () => localStorage.getItem(EXTENDED_COLS_STORAGE_KEY) === '1',
  );
  const [sort, setSort] = useState<SortState>(null);
  const [visibleLimit, setVisibleLimit] = useState(DEFAULT_VISIBLE_LIMIT);

  useEffect(() => {
    localStorage.setItem(EXTENDED_COLS_STORAGE_KEY, showExtended ? '1' : '0');
  }, [showExtended]);

  const handleSort = useCallback((key: SortKey) => {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: -1 };
      if (prev.dir === -1) return { key, dir: 1 };
      return null;
    });
  }, []);

  const sorted = useMemo(() => {
    if (!sort) return factors;
    const next = [...factors];
    next.sort((a, b) => compareByColumn(a, b, sort.key, sort.dir));
    return next;
  }, [factors, sort]);

  const visible = useMemo(() => sorted.slice(0, visibleLimit), [sorted, visibleLimit]);

  const selectable = useMemo(
    () => factors.filter((f) => !f.ownerless && !f.readOnly),
    [factors],
  );
  const selectedSelectableCount = useMemo(
    () => selectable.filter((f) => selectedIds.has(f.factorId)).length,
    [selectable, selectedIds],
  );
  const allSelected = selectable.length > 0 && selectedSelectableCount === selectable.length;

  const selectAllRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate =
        selectedSelectableCount > 0 && selectedSelectableCount < selectable.length;
    }
  }, [selectedSelectableCount, selectable.length]);

  const visibleColumns = COLUMNS.filter((c) => showExtended || !c.extended);
  const colCount = 5 + visibleColumns.length + (showExtended ? 1 : 0) + 2;

  const sortIcon = (key: SortKey) => {
    if (sort?.key !== key) return <ChevronsUpDown className="h-2.5 w-2.5 opacity-40" />;
    return sort.dir === -1 ? <ArrowDown className="h-2.5 w-2.5" /> : <ArrowUp className="h-2.5 w-2.5" />;
  };

  return (
    <div className="flex flex-col">
      {/* 工具条：计数 / 扩展列开关 */}
      <div className="flex items-center justify-between border-b border-border/50 px-2 py-1.5">
        <div className="text-[10px] text-muted-foreground">
          共 {factors.length} 行
          {sorted.length > visibleLimit && `（显示前 ${visibleLimit}）`}
          {serverLimit != null && factors.length >= serverLimit && (
            <span className="ml-2 text-amber-600">已达单次上限 {serverLimit}，仅列最近 {serverLimit} 条</span>
          )}
        </div>
        <button
          type="button"
          onClick={() => setShowExtended((v) => !v)}
          className={cn(
            'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] transition-colors',
            showExtended
              ? 'border-primary/40 bg-primary/10 text-primary'
              : 'border-border/60 text-muted-foreground hover:bg-muted/50',
          )}
          title="净ARR / 年化换手 / PFS / RRE / 公式"
        >
          <SlidersHorizontal className="h-3 w-3" />
          扩展列
        </button>
      </div>

      <div className="max-h-[min(70vh,640px)] overflow-auto">
        <table className="w-full border-collapse text-[11px]">
          <thead className="sticky top-0 z-10">
            <tr className="border-b border-border/60 bg-card/95 backdrop-blur">
              <th className="w-8 px-2 py-1.5">
                <input
                  ref={selectAllRef}
                  type="checkbox"
                  className="h-3.5 w-3.5 align-middle accent-primary"
                  checked={allSelected}
                  disabled={selectable.length === 0}
                  title={selectable.length === 0 ? '没有可勾选的因子' : '全选/清空当前可操作因子'}
                  onChange={(e) => onToggleSelectAll(e.target.checked)}
                />
              </th>
              <th className="w-10 px-1 py-1.5 text-right text-[10px] font-medium text-muted-foreground">
                #
              </th>
              <th className="px-2 py-1.5 text-left text-[10px] font-medium text-muted-foreground">
                <button
                  type="button"
                  className="inline-flex items-center gap-1 hover:text-foreground"
                  onClick={() => handleSort('name')}
                >
                  因子名 {sortIcon('name')}
                </button>
              </th>
              <th className="px-2 py-1.5 text-left text-[10px] font-medium text-muted-foreground">
                市场
              </th>
              {visibleColumns.map((col) => (
                <th
                  key={col.key}
                  className="px-2 py-1.5 text-right text-[10px] font-medium text-muted-foreground"
                >
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 hover:text-foreground"
                    onClick={() => handleSort(col.key)}
                    title={`按 ${col.label} 排序`}
                  >
                    {col.label} {sortIcon(col.key)}
                  </button>
                </th>
              ))}
              {showExtended && (
                <th className="px-2 py-1.5 text-left text-[10px] font-medium text-muted-foreground">
                  公式
                </th>
              )}
              <th className="px-2 py-1.5 text-center text-[10px] font-medium text-muted-foreground">
                状态
              </th>
              <th className="px-2 py-1.5 text-right text-[10px] font-medium text-muted-foreground">
                操作
              </th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="py-10 text-center text-xs text-muted-foreground">
                  {emptyText}
                </td>
              </tr>
            ) : (
              visible.map((factor, index) => (
                <FactorRow
                  key={factor.factorId}
                  factor={factor}
                  index={index}
                  selected={selectedIds.has(factor.factorId)}
                  btEntry={backtestEntries?.[factor.factorId]}
                  isMaterializing={Boolean(
                    materializeRunning && materializingIds?.has(factor.factorId),
                  )}
                  showExtended={showExtended}
                  onToggleSelect={onToggleSelect}
                  onOpenDetail={onOpenDetail}
                  onBacktest={onBacktest}
                  onMaterialize={onMaterialize}
                  onViewBacktest={onViewBacktest}
                />
              ))
            )}
          </tbody>
        </table>
        {sorted.length > visibleLimit && (
          <div className="border-t border-border/40 py-1.5 text-center">
            <button
              type="button"
              onClick={() => setVisibleLimit(sorted.length)}
              className="text-[11px] text-primary hover:underline"
            >
              显示全部（{sorted.length}）
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default FactorTable;
