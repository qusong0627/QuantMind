import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Dna,
  Layers,
  Loader2,
  RefreshCw,
  Sparkles,
  X,
} from 'lucide-react';
import {
  decomposeDirection,
  dispatchMiningBatch,
  normalizeAgentTask,
} from '../services-v2/api';
import type { BatchDispatchResult, DecomposeCard } from '../services-v2/api';
import { useTaskContext } from '../context-v2/TaskContext';
import type { Task } from '../types-v2';

/** 种子（父本）因子引用：id 用于下发，name 用于卡片徽章展示 */
export interface SeedFactorRef {
  id: string;
  name: string;
}

/**
 * 拆解请求（HomePage 持状态；`key` 是代次——同方向连点两次「智能拆解」
 * 也要重开面板重新发起，而不是复用上一次的结果）。
 */
export interface DecomposeRequest {
  key: number;
  direction: string;
  market: string;
  universe: string;
  /** 父本种子（≤3）：拆解围绕其做受控变异（T-MV-01） */
  seeds?: SeedFactorRef[];
}

/** ChatInput「智能拆解」按钮交给 HomePage 的载荷（key 由 HomePage 补） */
export type DecomposeRequestPayload = Omit<DecomposeRequest, 'key'>;

/** 单卡派发的演化轮数：与首页单条提交的默认（maxRounds=3）保持一致 */
export const DECOMPOSE_LOOP_N = 3;

interface DecomposePanelProps {
  request: DecomposeRequest;
  onClose: () => void;
  /** 「去演化台」去向（HomePage 接 onNavigate）；缺省只提供关闭 */
  onOpenDashboard?: () => void;
}

/** 编辑态的卡片：id 稳定（拆解结果下标），selected 默认全选 */
interface EditableCard extends DecomposeCard {
  id: number;
  selected: boolean;
}

/**
 * 卡片 → 派发用的方向文本。多行结构（标题/假设/依据/验证建议）比单行
 * 更适合下游 evolve 的解析；空字段省略，不产生空冒号行。
 */
export function composeDirection(card: DecomposeCard): string {
  const lines: string[] = [];
  const title = card.title.trim();
  const hypothesis = card.hypothesis.trim();
  if (title) lines.push(title);
  if (hypothesis) lines.push(`假设：${hypothesis}`);
  if (card.rationale?.trim()) lines.push(`依据：${card.rationale.trim()}`);
  if (card.evaluation_hint?.trim()) lines.push(`验证建议：${card.evaluation_hint.trim()}`);
  return lines.join('\n');
}

/** 后端/网络错误 → 可直接上屏的文案（FastAPI detail 优先） */
function errorDetailOf(err: unknown): string {
  const anyErr = err as any;
  const detail = anyErr?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  return anyErr?.message || '请求失败，请稍后重试';
}

type PanelPhase = 'loading' | 'error' | 'preview' | 'dispatching' | 'done';

/**
 * 智能拆解面板：粗方向 → 正交卡片（可勾选/可编辑）→ 一键批量派发。
 *
 * 拆解只读不落任务；派发才逐条成任务（满员自动排队，不 429 背压）。
 * 派发成功的任务经 `adoptDispatchedTasks` 进多任务注册表，排队任务在
 * 首页任务行显示「排队中 · 第 N 位」。
 */
export const DecomposePanel: React.FC<DecomposePanelProps> = ({
  request,
  onClose,
  onOpenDashboard,
}) => {
  const { adoptDispatchedTasks } = useTaskContext();

  const [phase, setPhase] = useState<PanelPhase>('loading');
  /** 重新拆解代次（错误重试 / 换一批） */
  const [attempt, setAttempt] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [dispatchError, setDispatchError] = useState<string | null>(null);
  const [cards, setCards] = useState<EditableCard[]>([]);
  const [dropped, setDropped] = useState(0);
  /** 请求的父本中不在本池（已归档/跨池）的条数——如实提示，不静默 */
  const [seedsDropped, setSeedsDropped] = useState(0);
  const [result, setResult] = useState<BatchDispatchResult | null>(null);

  // 拆解：挂载/重试时发起（组件由 HomePage 按 request.key 重挂，代次语义天然成立）
  useEffect(() => {
    let cancelled = false;
    setPhase('loading');
    setLoadError(null);
    setDispatchError(null);
    setResult(null);
    const seedIds = request.seeds?.map((s) => s.id) ?? [];
    decomposeDirection({
      direction: request.direction,
      market: request.market,
      universe: request.universe,
      ...(seedIds.length ? { seedFactorIds: seedIds } : {}),
    })
      .then((res) => {
        if (cancelled) return;
        if (!res.success || !res.data) throw new Error(res.error || '拆解失败');
        setCards(
          res.data.cards.map((c, i) => ({ ...c, id: i, selected: true })),
        );
        setDropped(res.data.dropped);
        setSeedsDropped(res.data.context.seeds?.dropped ?? 0);
        setPhase('preview');
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(errorDetailOf(err));
        setPhase('error');
      });
    return () => {
      cancelled = true;
    };
  }, [request, attempt]);

  const toggleCard = useCallback((id: number) => {
    setCards((prev) =>
      prev.map((c) => (c.id === id ? { ...c, selected: !c.selected } : c)),
    );
  }, []);

  const editCard = useCallback((id: number, patch: Partial<DecomposeCard>) => {
    setCards((prev) => prev.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }, []);

  const selected = cards.filter((c) => c.selected);
  const allSelected = cards.length > 0 && selected.length === cards.length;
  /** 父本 id → 名称（卡片徽章展示用；查不到回 id 本身，不伪造名字） */
  const seedNameById = new Map((request.seeds ?? []).map((s) => [s.id, s.name]));
  /** 勾选但内容被编辑空的卡片：派发前必须拦住（空方向后端整包 400） */
  const hasBlankSelected = selected.some((c) => !composeDirection(c).trim());
  const canDispatch =
    selected.length > 0 && !hasBlankSelected && phase === 'preview';

  const handleDispatch = useCallback(async () => {
    const chosen = cards.filter((c) => c.selected && composeDirection(c).trim());
    if (chosen.length === 0) return;
    setPhase('dispatching');
    setDispatchError(null);
    try {
      const res = await dispatchMiningBatch({
        directions: chosen.map(composeDirection),
        market: request.market,
        universe: request.universe,
        loopN: DECOMPOSE_LOOP_N,
      });
      if (!res.success || !res.data) throw new Error(res.error || '派发失败');
      // 回执 index 对齐派发顺序（请求级错误整包 400，不存在错位半批）
      const tasks: Task[] = [];
      for (const it of res.data.items) {
        if (!it.taskId || it.status === 'failed') continue;
        const card = chosen[it.index];
        const direction = card ? composeDirection(card) : it.directionPreview;
        tasks.push(
          normalizeAgentTask(
            {
              task_id: it.taskId,
              status: it.status,
              queue_position: it.queuePosition,
              direction,
            },
            { userInput: direction },
          ),
        );
      }
      if (tasks.length > 0) adoptDispatchedTasks(tasks);
      setResult(res.data);
      setPhase('done');
    } catch (err) {
      setDispatchError(errorDetailOf(err));
      setPhase('preview');
    }
  }, [cards, request.market, request.universe, adoptDispatchedTasks]);

  const failures = (result?.items ?? []).filter((it) => it.status === 'failed');

  return (
    <div className="w-full max-w-4xl mx-auto rounded-2xl border border-indigo-100 bg-white/95 backdrop-blur-xl shadow-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-4 py-3 border-b border-slate-100 bg-gradient-to-r from-indigo-50/80 to-purple-50/60">
        <Sparkles className="h-4 w-4 text-indigo-500 shrink-0" />
        <span className="text-xs font-black text-slate-800 whitespace-nowrap">
          智能拆解
        </span>
        <span className="text-[11px] text-slate-500 truncate flex-1 min-w-0">
          粗方向 → 正交子假设卡片，逐张派发独立挖掘任务
        </span>
        <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap hidden sm:block">
          {request.market} · {request.universe}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded-full p-1 text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors cursor-pointer"
          title="收起拆解面板"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Loading */}
      {phase === 'loading' && (
        <div className="flex items-center gap-3 px-4 py-7 text-xs font-bold text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin text-indigo-500 shrink-0" />
          正在拆解方向并生成正交卡片（结合因子池现状，通常需要几秒到十几秒）…
        </div>
      )}

      {/* Load error */}
      {phase === 'error' && (
        <div className="px-4 py-4 flex flex-col gap-3">
          <div className="flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50/80 px-3.5 py-2.5 text-xs font-bold text-rose-600">
            <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
            <span className="flex-1 min-w-0 break-all">{loadError}</span>
          </div>
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setAttempt((a) => a + 1)}
              className="inline-flex items-center gap-1.5 rounded-full border border-indigo-200 bg-white px-3.5 py-1.5 text-[11px] font-bold text-indigo-600 hover:bg-indigo-50 transition-colors cursor-pointer"
            >
              <RefreshCw className="h-3 w-3" />
              重新拆解
            </button>
          </div>
        </div>
      )}

      {/* Preview */}
      {(phase === 'preview' || phase === 'dispatching') && (
        <div className="flex flex-col">
          <div className="flex items-center gap-3 px-4 pt-3 pb-2">
            <label className="inline-flex items-center gap-1.5 text-[11px] font-bold text-slate-600 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={() =>
                  setCards((prev) => prev.map((c) => ({ ...c, selected: !allSelected })))
                }
                className="h-3.5 w-3.5 accent-indigo-600 cursor-pointer"
              />
              全选（已选 {selected.length}/{cards.length}）
            </label>
            {dropped > 0 && (
              <span className="text-[11px] font-bold text-amber-600">
                另有 {dropped} 张超卡片数上限未展示
              </span>
            )}
            {seedsDropped > 0 && (
              <span className="text-[11px] font-bold text-amber-600">
                {seedsDropped} 个父本不在本池（可能已归档），已忽略
              </span>
            )}
          </div>

          <div className="flex flex-col gap-2 px-4 max-h-[420px] overflow-y-auto">
            {cards.map((card) => (
              <div
                key={card.id}
                className={`rounded-xl border px-3.5 py-2.5 transition-colors ${
                  card.selected
                    ? 'border-indigo-200 bg-indigo-50/40'
                    : 'border-slate-200 bg-slate-50/60 opacity-70'
                }`}
              >
                <div className="flex items-start gap-2.5">
                  <input
                    type="checkbox"
                    checked={card.selected}
                    onChange={() => toggleCard(card.id)}
                    className="mt-1 h-3.5 w-3.5 accent-indigo-600 cursor-pointer shrink-0"
                  />
                  <div className="flex-1 min-w-0 flex flex-col gap-1.5">
                    <input
                      value={card.title}
                      onChange={(e) => editCard(card.id, { title: e.target.value })}
                      disabled={phase === 'dispatching'}
                      className="w-full bg-transparent text-xs font-black text-slate-800 focus:outline-none border-b border-transparent focus:border-indigo-200"
                      placeholder="卡片标题"
                    />
                    <textarea
                      value={card.hypothesis}
                      onChange={(e) => editCard(card.id, { hypothesis: e.target.value })}
                      disabled={phase === 'dispatching'}
                      rows={2}
                      className="w-full resize-none bg-transparent text-xs text-slate-600 leading-relaxed focus:outline-none border-b border-transparent focus:border-indigo-200"
                      placeholder="一句话可检验的因子假设"
                    />
                    {card.rationale && (
                      <p className="m-0 text-[11px] text-slate-500 leading-relaxed">
                        依据：{card.rationale}
                      </p>
                    )}
                    {card.categories.length > 0 && (
                      <div className="flex items-center gap-1 flex-wrap">
                        <Layers className="h-3 w-3 text-slate-400 shrink-0" />
                        {card.categories.map((cid) => (
                          <span
                            key={cid}
                            className="rounded-full bg-white border border-slate-200 px-2 py-[1px] text-[10px] font-mono text-slate-500"
                          >
                            {cid}
                          </span>
                        ))}
                      </div>
                    )}
                    {card.seed_factor_id && (
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <Dna className="h-3 w-3 text-amber-500 shrink-0" />
                        <span
                          className="rounded-full bg-amber-50 border border-amber-200 px-2 py-[1px] text-[10px] font-bold text-amber-700"
                          title={`父本因子 ID：${card.seed_factor_id}（该卡围绕其做受控变异）`}
                        >
                          父本：{seedNameById.get(card.seed_factor_id) || card.seed_factor_id}
                        </span>
                      </div>
                    )}
                    {card.evaluation_hint && (
                      <p className="m-0 text-[11px] text-indigo-500/90 leading-relaxed">
                        验证：{card.evaluation_hint}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {dispatchError && (
            <div className="mx-4 mt-3 flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50/80 px-3.5 py-2.5 text-xs font-bold text-rose-600">
              <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
              <span className="flex-1 min-w-0 break-all">{dispatchError}</span>
            </div>
          )}

          <div className="flex items-center gap-3 px-4 py-3 mt-1 border-t border-slate-100">
            <span className="flex-1 min-w-0 text-[11px] text-slate-400">
              {hasBlankSelected
                ? '有勾选卡片的内容被编辑为空：补全标题/假设，或取消勾选'
                : '每张卡片各启动一个独立挖掘任务；并发满员时自动排队，不丢提交'}
            </span>
            <button
              type="button"
              onClick={() => setAttempt((a) => a + 1)}
              disabled={phase === 'dispatching'}
              className="shrink-0 inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200 transition-colors disabled:opacity-40 cursor-pointer"
              title="丢弃当前编辑与勾选，重新拆解一批"
            >
              <RefreshCw className="h-3 w-3" />
              换一批
            </button>
            <button
              type="button"
              onClick={() => void handleDispatch()}
              disabled={!canDispatch}
              className="shrink-0 inline-flex items-center gap-1.5 rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 px-4 py-1.5 text-[11px] font-black text-white shadow-sm shadow-indigo-500/25 hover:from-indigo-600 hover:to-purple-700 disabled:from-slate-300 disabled:to-slate-400 disabled:shadow-none disabled:cursor-not-allowed transition-all cursor-pointer"
            >
              {phase === 'dispatching' ? (
                <>
                  <Loader2 className="h-3 w-3 animate-spin" />
                  正在派发…
                </>
              ) : (
                <>派发 {selected.length} 个挖掘任务</>
              )}
            </button>
          </div>
        </div>
      )}

      {/* Done */}
      {phase === 'done' && result && (
        <div className="px-4 py-4 flex flex-col gap-3">
          <div className="flex items-center gap-2 text-xs font-black text-emerald-600">
            <CheckCircle2 className="h-4 w-4 shrink-0" />
            <span>
              已派发 {result.started + result.queued} 个任务
              {result.queued > 0 && `（其中 ${result.queued} 个排队中，排到自动开跑）`}
              {result.failed > 0 && `，${result.failed} 个失败`}
            </span>
          </div>
          {failures.length > 0 && (
            <div className="flex flex-col gap-1.5 rounded-xl border border-amber-200 bg-amber-50/70 px-3.5 py-2.5">
              {failures.map((it) => (
                <p
                  key={`${it.index}-${it.directionPreview}`}
                  className="m-0 text-[11px] leading-relaxed text-amber-700"
                >
                  <span className="font-bold">{it.directionPreview || `第 ${it.index + 1} 条`}</span>
                  ：{it.error || '派发失败'}
                </p>
              ))}
            </div>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-full border border-slate-200 bg-white px-3.5 py-1.5 text-[11px] font-bold text-slate-500 hover:text-slate-700 hover:bg-slate-50 transition-colors cursor-pointer"
            >
              关闭
            </button>
            {onOpenDashboard && (
              <button
                type="button"
                onClick={onOpenDashboard}
                className="rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 px-3.5 py-1.5 text-[11px] font-black text-white shadow-sm shadow-indigo-500/25 hover:from-indigo-600 hover:to-purple-700 transition-all cursor-pointer"
              >
                去演化台查看
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default DecomposePanel;
