/**
 * 模型对比弹窗 —— 批量选中后横向对比「最近 3 个月实盘力」与「测试集 IC」。
 *
 * 数据口径：
 * - 测试集 IC / IR：训练产物 metadata 的 test 段（`resolveSplitMetrics` 唯一出处）；
 * - 近 3 个月：模型管理同一份 `getModelSignalEval`（滚动 test 切分，窗口随推理前移）。
 *
 * 交互：
 * - 窗口可拖动（标题栏左侧手柄，方向键也可移动）、可缩放（右边/下边/右下角），可最大化；
 *   默认宽度按选中模型数推算，模型多时表格横向滚动而不是把列挤窄；
 * - 表格是 Excel 式：表头与指标列冻结，模型列可「冻结」到左侧（最多 3 列）；
 * - 点指标行或顶部下拉都能排序；收藏 / 置顶直接写页面同一份 listPrefs，关掉弹窗即生效。
 *
 * 缺失一律显示「—」，不折算成 0。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Modal, Select, Tooltip } from 'antd';
import {
  ArrowDownWideNarrow,
  ArrowUpWideNarrow,
  GripHorizontal,
  Maximize2,
  Minimize2,
  MoveDiagonal2,
  RefreshCw,
  X,
} from 'lucide-react';
import type { UserModelRecord } from '../services/modelTrainingService';
import { modelTrainingService } from '../services/modelTrainingService';
import { ModelCompareTable } from './modelCompare/ModelCompareTable';
import { useFloatingWindow } from './modelCompare/useFloatingWindow';
import {
  MAX_FROZEN_COLUMNS,
  METRICS,
  metricValue,
  reportOf,
  type EvalEntry,
  type MetricKey,
} from './modelCompare/modelCompareMetrics';

interface ModelCompareModalProps {
  open: boolean;
  models: UserModelRecord[];
  favorites: string[];
  pinned: string[];
  onClose: () => void;
  onToggleFavorite: (modelId: string) => void;
  onTogglePin: (modelId: string) => void;
}

export const ModelCompareModal: React.FC<ModelCompareModalProps> = ({
  open,
  models,
  favorites,
  pinned,
  onClose,
  onToggleFavorite,
  onTogglePin,
}) => {
  const [evals, setEvals] = useState<Record<string, EvalEntry>>({});
  const [sortKey, setSortKey] = useState<MetricKey>('rank_ic');
  const [sortDir, setSortDir] = useState<'desc' | 'asc'>('desc');
  const [reloadNonce, setReloadNonce] = useState(0);
  const [frozenIds, setFrozenIds] = useState<string[]>([]);

  const modelKey = models.map((model) => model.model_id).join('|');
  const modelIdSet = useMemo(
    () => new Set(models.map((model) => model.model_id)),
    // modelKey 已覆盖 ID 集合变化；models 数组每次筛选都会新建。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [modelKey],
  );

  const {
    rect,
    maximized,
    announcement,
    beginInteraction,
    handlePointerMove,
    endInteraction,
    handleMoveKeyDown,
    handleResizeKeyDown,
    toggleMaximize,
  } = useFloatingWindow(open, models.length);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setEvals((prev) => {
      const next: Record<string, EvalEntry> = {};
      for (const model of models) {
        next[model.model_id] = prev[model.model_id] ?? {
          state: null,
          loading: true,
          error: null,
        };
      }
      return next;
    });
    for (const model of models) {
      if (evals[model.model_id]?.state) continue;
      void modelTrainingService
        .getModelSignalEval(model.model_id)
        .then((state) => {
          if (!alive) return;
          setEvals((prev) => ({
            ...prev,
            [model.model_id]: { state, loading: false, error: null },
          }));
        })
        .catch((err: unknown) => {
          if (!alive) return;
          setEvals((prev) => ({
            ...prev,
            [model.model_id]: {
              state: null,
              loading: false,
              error: err instanceof Error ? err.message : '请求失败',
            },
          }));
        });
    }
    return () => {
      alive = false;
    };
    // evals 只用于「已有缓存就跳过」，不能进依赖：否则每回填一条就整批重发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, modelKey, reloadNonce]);

  // 选中的模型变化后，清掉已不在列表里的冻结列。
  useEffect(() => {
    setFrozenIds((prev) => {
      const next = prev.filter((id) => modelIdSet.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [modelIdSet]);

  const orderedModels = useMemo(() => {
    const withValue = models.map((model, index) => ({
      model,
      index,
      value: metricValue(model, evals[model.model_id], sortKey),
    }));
    withValue.sort((a, b) => {
      if (a.value === null && b.value === null) return a.index - b.index;
      if (a.value === null) return 1;
      if (b.value === null) return -1;
      if (a.value === b.value) return a.index - b.index;
      return sortDir === 'desc' ? b.value - a.value : a.value - b.value;
    });
    return withValue.map((item) => item.model);
  }, [models, evals, sortKey, sortDir]);

  // 冻结列固定排在最前（按冻结顺序），其余按当前排序指标排列。
  const { displayModels, frozenCount } = useMemo(() => {
    const byId = new Map(orderedModels.map((model) => [model.model_id, model] as const));
    const frozen = frozenIds
      .map((id) => byId.get(id))
      .filter((model): model is UserModelRecord => Boolean(model));
    const frozenSet = new Set(frozen.map((model) => model.model_id));
    return {
      displayModels: [...frozen, ...orderedModels.filter((model) => !frozenSet.has(model.model_id))],
      frozenCount: frozen.length,
    };
  }, [orderedModels, frozenIds]);

  const onClickMetric = useCallback(
    (key: MetricKey) => {
      if (key === sortKey) {
        setSortDir((prev) => (prev === 'desc' ? 'asc' : 'desc'));
      } else {
        setSortKey(key);
        setSortDir('desc');
      }
    },
    [sortKey],
  );

  const handleToggleFreeze = useCallback((modelId: string) => {
    setFrozenIds((prev) => {
      if (prev.includes(modelId)) return prev.filter((id) => id !== modelId);
      if (prev.length >= MAX_FROZEN_COLUMNS) return prev;
      return [...prev, modelId];
    });
  }, []);

  const windowText = useMemo(() => {
    for (const model of models) {
      const report = reportOf(evals[model.model_id]?.state ?? null);
      if (report?.window) {
        return `${report.window.start} ~ ${report.window.end} · ${report.window.signal_days} 个交易日`;
      }
    }
    return '窗口随推理与行情前移';
  }, [models, evals]);

  // 每项指标的最优值（并列都算）。只有一个模型有数时不算「领先」，避免假优势。
  const bestByMetric = useMemo(() => {
    const best = new Map<MetricKey, number>();
    for (const metric of METRICS) {
      let max: number | null = null;
      let scored = 0;
      for (const model of displayModels) {
        const current = metricValue(model, evals[model.model_id], metric.key);
        if (current === null) continue;
        scored += 1;
        if (max === null || current > max) max = current;
      }
      if (max !== null && scored >= 2) best.set(metric.key, max);
    }
    return best;
  }, [displayModels, evals]);

  const leadCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const model of displayModels) counts.set(model.model_id, 0);
    for (const metric of METRICS) {
      const best = bestByMetric.get(metric.key);
      if (best === undefined) continue;
      for (const model of displayModels) {
        if (metricValue(model, evals[model.model_id], metric.key) === best) {
          counts.set(model.model_id, (counts.get(model.model_id) ?? 0) + 1);
        }
      }
    }
    return counts;
  }, [displayModels, evals, bestByMetric]);

  const resetAndReload = useCallback(() => {
    setEvals({});
    setReloadNonce((prev) => prev + 1);
  }, []);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      title={null}
      closable={false}
      centered={false}
      width={rect.width}
      height={rect.height}
      style={{ top: 0, margin: 0, paddingBottom: 0, maxWidth: 'none' }}
      className="model-compare-modal"
      destroyOnHidden={false}
      styles={{
        wrapper: { padding: 0, overflow: 'hidden' },
        mask: {
          backdropFilter: 'blur(3px)',
          backgroundColor: 'rgba(15, 23, 42, 0.45)',
        },
        content: { height: '100%', padding: 0, overflow: 'hidden', borderRadius: 16 },
        body: { height: '100%', padding: 0 },
      }}
      modalRender={(node) => (
        <div
          data-testid="model-compare-window"
          style={{
            position: 'absolute',
            left: rect.x,
            top: rect.y,
            width: rect.width,
            height: rect.height,
          }}
        >
          {node}
        </div>
      )}
    >
      <div className="relative flex h-full flex-col overflow-hidden rounded-2xl bg-white">
        <header className="relative shrink-0 overflow-hidden border-b border-slate-200 bg-white px-4 py-3">
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0"
            style={{
              background:
                'radial-gradient(circle at 10% 0%, rgba(99,102,241,0.10), transparent 30%), radial-gradient(circle at 92% 0%, rgba(14,165,233,0.08), transparent 28%)',
            }}
          />
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-indigo-400 via-sky-400 to-amber-300"
          />
          <div className="relative flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2.5">
              <Tooltip title="拖动窗口（双击最大化 / 方向键移动，Shift 加速）">
                <button
                  type="button"
                  aria-label="拖动对比窗口，方向键可移动"
                  title="拖动窗口（双击最大化 / 方向键移动）"
                  onPointerDown={beginInteraction('move')}
                  onPointerMove={handlePointerMove}
                  onPointerUp={endInteraction}
                  onPointerCancel={endInteraction}
                  onLostPointerCapture={endInteraction}
                  onKeyDown={handleMoveKeyDown}
                  onDoubleClick={toggleMaximize}
                  className="flex h-8 w-8 shrink-0 cursor-grab touch-none select-none items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-500 shadow-sm transition-colors hover:bg-slate-50 hover:text-slate-800 active:cursor-grabbing"
                >
                  <GripHorizontal size={16} />
                </button>
              </Tooltip>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[9px] font-black tracking-[0.18em] text-indigo-600">
                    MODEL COMPARE
                  </span>
                  <span className="text-[10px] font-bold text-slate-500">
                    {models.length} 个模型
                  </span>
                  {frozenCount > 0 && (
                    <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[9px] font-bold text-sky-700">
                      冻结 {frozenCount} 列
                    </span>
                  )}
                </div>
                <h2 className="mt-1 truncate text-[18px] font-black tracking-tight text-slate-900">
                  最近 3 个月表现 × 测试集 IC
                </h2>
                <p className="mt-0.5 truncate text-[10px] text-slate-500">
                  {windowText} · 排序指标：{METRICS.find((m) => m.key === sortKey)?.label ?? '—'}
                </p>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <Select
                size="small"
                value={sortKey}
                onChange={(value) => {
                  setSortKey(value as MetricKey);
                  setSortDir('desc');
                }}
                options={METRICS.map((metric) => ({
                  value: metric.key,
                  label: metric.label,
                }))}
                popupMatchSelectWidth={false}
                className="min-w-[170px]"
              />
              <Tooltip title="重新读取所有模型的统计（不触发后台重算）">
                <Button
                  size="small"
                  onClick={resetAndReload}
                  icon={<RefreshCw size={13} />}
                >
                  刷新
                </Button>
              </Tooltip>
              <Tooltip title={sortDir === 'desc' ? '从高到低' : '从低到高'}>
                <Button
                  size="small"
                  onClick={() => setSortDir((prev) => (prev === 'desc' ? 'asc' : 'desc'))}
                  icon={
                    sortDir === 'desc' ? (
                      <ArrowDownWideNarrow size={13} />
                    ) : (
                      <ArrowUpWideNarrow size={13} />
                    )
                  }
                >
                  {sortDir === 'desc' ? '降序' : '升序'}
                </Button>
              </Tooltip>
              <Tooltip title={maximized ? '还原窗口大小' : '铺满整个窗口'}>
                <Button
                  size="small"
                  aria-label={maximized ? '还原窗口' : '最大化窗口'}
                  onClick={toggleMaximize}
                  icon={maximized ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
                />
              </Tooltip>
              <Button
                size="small"
                type="text"
                onClick={onClose}
                aria-label="关闭对比"
                icon={<X size={14} />}
                className="!text-slate-500 hover:!bg-slate-100 hover:!text-slate-900"
              />
            </div>
          </div>
        </header>

        <ModelCompareTable
          models={displayModels}
          frozenCount={frozenCount}
          evals={evals}
          favorites={favorites}
          pinned={pinned}
          sortKey={sortKey}
          sortDir={sortDir}
          bestByMetric={bestByMetric}
          leadCounts={leadCounts}
          onSort={onClickMetric}
          onToggleFavorite={onToggleFavorite}
          onTogglePin={onTogglePin}
          onToggleFreeze={handleToggleFreeze}
        />

        <footer className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-slate-200 bg-slate-50 px-4 py-2">
          <div className="text-[10px] leading-relaxed text-slate-500">
            表格可左右滚动 · 拖动左上角手柄移动窗口 · 拖右边 / 下边 / 右下角缩放 · 点列头雪花冻结该列（最多{' '}
            {MAX_FROZEN_COLUMNS} 列）· 点指标行切换排序 · 缺失显示「—」不折算 0
          </div>
          <Button size="small" onClick={onClose} className="rounded-lg font-bold">
            关闭
          </Button>
        </footer>

        {/* 缩放热区：标题栏右缘调宽 / 窗口下缘调高 / 右下角整体缩放
            （右边条只压在标题栏上，避免挡住表格自己的纵向滚动条） */}
        <button
          type="button"
          aria-label="调整对比窗口宽度，左右方向键调整"
          title="拖动调整宽度"
          onPointerDown={beginInteraction('resize-e')}
          onPointerMove={handlePointerMove}
          onPointerUp={endInteraction}
          onPointerCancel={endInteraction}
          onLostPointerCapture={endInteraction}
          onKeyDown={(event) => handleResizeKeyDown(event, 'resize-e')}
          className="absolute right-0 top-0 z-30 h-20 w-1.5 cursor-ew-resize touch-none bg-transparent transition-colors hover:bg-indigo-400/40 focus-visible:bg-indigo-400/50 focus-visible:outline-none"
        />
        <button
          type="button"
          aria-label="调整对比窗口高度，上下方向键调整"
          title="拖动调整高度"
          onPointerDown={beginInteraction('resize-s')}
          onPointerMove={handlePointerMove}
          onPointerUp={endInteraction}
          onPointerCancel={endInteraction}
          onLostPointerCapture={endInteraction}
          onKeyDown={(event) => handleResizeKeyDown(event, 'resize-s')}
          className="absolute bottom-0 left-0 right-4 z-30 h-1.5 cursor-ns-resize touch-none bg-transparent transition-colors hover:bg-indigo-400/40 focus-visible:bg-indigo-400/50 focus-visible:outline-none"
        />
        <button
          type="button"
          aria-label="调整对比窗口大小，方向键调整宽高，Shift 加速"
          title="拖动调整大小"
          onPointerDown={beginInteraction('resize-se')}
          onPointerMove={handlePointerMove}
          onPointerUp={endInteraction}
          onPointerCancel={endInteraction}
          onLostPointerCapture={endInteraction}
          onKeyDown={(event) => handleResizeKeyDown(event, 'resize-se')}
          className="absolute bottom-0 right-0 z-40 flex h-4 w-4 cursor-nwse-resize touch-none items-end justify-end rounded-br-2xl text-slate-400 transition-colors hover:bg-indigo-400/25 hover:text-indigo-600 focus-visible:bg-indigo-400/30 focus-visible:outline-none"
        >
          <MoveDiagonal2 size={11} className="pointer-events-none m-0.5" />
        </button>

        <span className="sr-only" role="status" aria-live="polite">
          {announcement}
        </span>
      </div>
    </Modal>
  );
};
