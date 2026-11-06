import React, { useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { Loader2, WandSparkles, X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import type { TaskConfig } from '../types-v2';
import { UNIVERSE_LABELS } from '../services-v2/api';
import { listFactorPool, type FactorPoolEntry } from '../services-v2/factorJobsApi';
import {
  createResearchMiningBatchId,
  decomposeResearchDirection,
  dispatchResearchMiningBatch,
  type DocumentMiningBatchReceipt,
  type ResearchMiningBatchRequest,
} from '../services-v2/docApi';

interface HypothesisDraft {
  title: string;
  text: string;
  selected: boolean;
  seedFactorId: string | null;
}

interface ResearchDirectionsDialogProps {
  config: TaskConfig;
  onClose: () => void;
  onOpenTasks: () => void;
}

function messageFromError(error: unknown): string {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === 'string') return detail;
  }
  return error instanceof Error ? error.message : '研究方向操作失败，请稍后重试';
}

export const ResearchDirectionsDialog: React.FC<ResearchDirectionsDialogProps> = ({ config, onClose, onOpenTasks }) => {
  const universe = config.universe ?? 'all_a';
  const [direction, setDirection] = useState(config.userInput);
  const [rounds, setRounds] = useState(config.maxRounds ?? 3);
  const [maxCards, setMaxCards] = useState(6);
  const [parents, setParents] = useState<FactorPoolEntry[]>([]);
  const [parentError, setParentError] = useState('');
  const [selectedParents, setSelectedParents] = useState<string[]>([]);
  const [cards, setCards] = useState<HypothesisDraft[]>([]);
  const [busy, setBusy] = useState<'decompose' | 'dispatch' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pendingBatch, setPendingBatch] = useState<ResearchMiningBatchRequest | null>(null);
  const [receipt, setReceipt] = useState<DocumentMiningBatchReceipt | null>(null);
  const generation = useRef(getSessionGeneration());
  const mounted = useRef(true);
  const actionPending = useRef(false);
  const requests = useRef(new Set<AbortController>());
  const active = () => mounted.current && generation.current === getSessionGeneration();
  const frozen = pendingBatch !== null;
  const selectedCards = cards.filter((card) => card.selected);

  useEffect(() => {
    mounted.current = true;
    generation.current = getSessionGeneration();
    const controller = new AbortController();
    requests.current.add(controller);
    setParents([]);
    setSelectedParents([]);
    setParentError('');
    listFactorPool({ universe, limit: 500 }, controller.signal)
      .then((items) => {
        if (!controller.signal.aborted && active()) setParents(items.filter((item) => item.status === 'completed'));
      })
      .catch(() => {
        if (!controller.signal.aborted && active()) setParentError('本人因子池暂不可用，可以不选择父因子继续研究');
      })
      .finally(() => requests.current.delete(controller));
    const unregister = registerSessionReset(() => {
      requests.current.forEach((request) => request.abort());
      onClose();
    });
    return () => {
      mounted.current = false;
      requests.current.forEach((request) => request.abort());
      requests.current.clear();
      unregister();
    };
  }, [universe, onClose]);

  const decompose = async () => {
    if (actionPending.current || busy || frozen || !direction.trim()) return;
    actionPending.current = true;
    const controller = new AbortController();
    requests.current.add(controller);
    setBusy('decompose');
    setError('');
    setNotice('');
    setCards([]);
    try {
      const result = await decomposeResearchDirection(direction.trim(), controller.signal, selectedParents, { universe, maxCards });
      if (controller.signal.aborted || !active()) return;
      setCards(result.cards.map((card) => ({
        title: card.title,
        text: [card.title, `研究假设：${card.hypothesis}`, card.rationale ? `研究逻辑：${card.rationale}` : '', card.evaluation_hint ? `验证建议：${card.evaluation_hint}` : ''].filter(Boolean).join('\n'),
        selected: true,
        seedFactorId: card.seed_factor_id && selectedParents.includes(card.seed_factor_id) ? card.seed_factor_id : null,
      })));
      if (!result.cards.length) setNotice('未生成可用假设，请调整研究需求后重新拆解');
      else if (result.dropped > 0) setNotice(`已保留 ${result.cards.length} 个方向，超出上限的 ${result.dropped} 个方向未加入`);
    } catch (cause) {
      if (!controller.signal.aborted && active()) setError(messageFromError(cause));
    } finally {
      actionPending.current = false;
      requests.current.delete(controller);
      if (active()) setBusy(null);
    }
  };

  const dispatch = async () => {
    if (actionPending.current || busy) return;
    if (!pendingBatch && (!selectedCards.length || selectedCards.length > 10 || selectedCards.some((card) => !card.text.trim() || card.text.trim().length > 20_000))) {
      setError('请选择 1 到 10 个非空假设，每个方向不超过 20000 字');
      return;
    }
    if (!Number.isInteger(rounds) || rounds < 1 || rounds > 20) {
      setError('每个方向的演化轮数须为 1 到 20');
      return;
    }
    actionPending.current = true;
    const payload = pendingBatch ?? {
      batch_id: createResearchMiningBatchId(),
      directions: selectedCards.map((card) => card.text.trim()),
      market: 'a_share' as const,
      universe,
      loop_n: rounds,
      data_source: config.dataSource ?? 'qlib_bin',
      freq: 'day' as const,
      datasets: config.datasets,
      mining_start: config.miningStart,
      mining_end: config.miningEnd,
      valid_start: config.validStart,
      valid_end: config.validEnd,
      seed_factor_ids: selectedCards.map((card) => card.seedFactorId),
    };
    setPendingBatch(payload);
    const controller = new AbortController();
    requests.current.add(controller);
    setBusy('dispatch');
    setError('');
    try {
      const result = await dispatchResearchMiningBatch(payload, controller.signal);
      if (!controller.signal.aborted && active()) setReceipt(result);
    } catch (cause) {
      if (!controller.signal.aborted && active()) setError(`${messageFromError(cause)}；可沿用当前批次安全重试，也可到任务中心核对已入队任务`);
    } finally {
      actionPending.current = false;
      requests.current.delete(controller);
      if (active()) setBusy(null);
    }
  };

  const editCard = (index: number, patch: Partial<HypothesisDraft>) => {
    if (frozen || busy) return;
    setCards((current) => current.map((card, position) => position === index ? { ...card, ...patch } : card));
  };

  return (
    <div role="dialog" aria-modal="true" aria-label="研究方向拆解与批量挖掘" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
      <div className="flex max-h-[90vh] w-full max-w-4xl flex-col rounded-2xl bg-white text-slate-900 shadow-2xl">
        <div className="flex items-start justify-between border-b border-slate-100 p-5">
          <div>
            <h3 className="text-sm font-bold">研究方向拆解与批量挖掘</h3>
            <p className="mt-1 text-xs text-slate-500">先编辑假设卡，再提交 RD-Agent；任务按全站现有串行队列运行，离开页面后可在任务中心查看。</p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭研究方向" className="rounded-lg p-1 text-slate-500 hover:bg-slate-100"><X className="h-5 w-5" /></button>
        </div>
        <div className="space-y-4 overflow-y-auto p-5">
          <div className="rounded-xl bg-slate-50 p-3 text-xs leading-6 text-slate-600">
            <p>股票池：{UNIVERSE_LABELS[universe] ?? universe} · A股日频</p>
            <p>挖掘区间：{config.miningStart ? `${config.miningStart} 至 ${config.miningEnd}` : '服务器默认窗口'}{config.validStart ? `；样本外检验：${config.validStart} 至 ${config.validEnd}` : ''}</p>
          </div>
          <label className="block text-xs font-medium">
            研究需求
            <textarea value={direction} onChange={(event) => { setDirection(event.target.value); setCards([]); }} maxLength={20_000} rows={3} disabled={Boolean(busy) || frozen} className="mt-2 w-full rounded-xl border border-slate-200 p-3 text-xs font-normal disabled:bg-slate-50" />
          </label>
          <div>
            <p className="text-xs font-medium">参考本人因子池（可选，最多 3 个父因子）</p>
            <p className="mt-1 text-xs text-slate-500">父因子仅作为受控变异参考，不会修改原因子。</p>
            {parentError && <p className="mt-2 text-xs text-amber-700">{parentError}</p>}
            {!parentError && !parents.length && <p className="mt-2 text-xs text-slate-500">当前股票池没有可用的本人父因子，可直接拆解方向。</p>}
            <div className="mt-2 flex max-h-28 flex-wrap gap-2 overflow-y-auto">
              {parents.map((parent) => (
                <label key={parent.factor_id} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-2 py-1 text-xs">
                  <input type="checkbox" checked={selectedParents.includes(parent.factor_id)} disabled={Boolean(busy) || frozen || (!selectedParents.includes(parent.factor_id) && selectedParents.length >= 3)} onChange={(event) => {
                    setSelectedParents((current) => event.target.checked ? [...current, parent.factor_id] : current.filter((id) => id !== parent.factor_id));
                    setCards([]);
                  }} />
                  {parent.factor_name}
                </label>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-4">
            <label className="text-xs text-slate-600">假设卡数量 <select value={maxCards} disabled={Boolean(busy) || frozen} onChange={(event) => setMaxCards(Number(event.target.value))} className="ml-2 rounded-lg border border-slate-200 p-1.5">{[3, 6, 10].map((count) => <option key={count} value={count}>{count}</option>)}</select></label>
            <label className="text-xs text-slate-600">每个方向演化轮数 <input type="number" min={1} max={20} value={rounds} disabled={Boolean(busy) || frozen} onChange={(event) => setRounds(Number(event.target.value))} className="ml-2 w-16 rounded-lg border border-slate-200 p-1.5" /></label>
            <button type="button" onClick={() => void decompose()} disabled={Boolean(busy) || frozen || !direction.trim()} className="inline-flex items-center gap-1.5 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs font-semibold text-blue-700 disabled:opacity-40">{busy === 'decompose' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <WandSparkles className="h-3.5 w-3.5" />}{cards.length ? '重新拆解' : '拆解为假设卡'}</button>
          </div>
          {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-xs text-red-700">{error}</p>}
          {notice && <p role="status" className="text-xs text-slate-600">{notice}</p>}
          {cards.map((card, index) => (
            <div key={index} className="rounded-xl border border-slate-200 p-3">
              <label className="flex items-center gap-2 text-xs font-semibold"><input type="checkbox" checked={card.selected} disabled={Boolean(busy) || frozen} onChange={(event) => editCard(index, { selected: event.target.checked })} />{index + 1}. {card.title}</label>
              <textarea rows={5} value={card.text} maxLength={20_000} disabled={Boolean(busy) || frozen} onChange={(event) => editCard(index, { text: event.target.value })} className="mt-2 w-full rounded-lg border border-slate-200 p-2 text-xs disabled:bg-slate-50" />
              {selectedParents.length > 0 && <label className="text-xs text-slate-600">本方向父因子 <select value={card.seedFactorId ?? ''} disabled={Boolean(busy) || frozen} onChange={(event) => editCard(index, { seedFactorId: event.target.value || null })} className="ml-2 max-w-full rounded-lg border border-slate-200 p-1.5"><option value="">不指定父因子</option>{parents.filter((parent) => selectedParents.includes(parent.factor_id)).map((parent) => <option key={parent.factor_id} value={parent.factor_id}>{parent.factor_name}</option>)}</select></label>}
            </div>
          ))}
          {receipt && (
            <div className="rounded-xl border border-blue-100 bg-blue-50 p-3 text-xs">
              <p className="font-semibold">派发回执：已启动 {receipt.started}，排队 {receipt.queued}，失败 {receipt.failed}</p>
              <ul className="mt-2 space-y-2">{receipt.items.map((item) => <li key={item.index} className="break-words"><span className="font-medium">方向 {item.index + 1}</span>：{item.status}{item.queue_position !== null ? ` · 排队位置 ${item.queue_position}` : ''}{item.task_id ? ` · ${item.task_id}` : ''}{item.error ? ` · ${item.error}` : ''}</li>)}</ul>
            </div>
          )}
          {frozen && <p className="text-xs text-slate-500">本批次内容已固定。重试会沿用同一批次编号，已入队任务不会重复创建；编辑新方向请关闭后重新发起。</p>}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-slate-100 p-4">
          {(receipt || pendingBatch) && <button type="button" onClick={onOpenTasks} className="rounded-lg border border-slate-200 px-3 py-2 text-xs">查看任务中心</button>}
          <button type="button" onClick={onClose} className="rounded-lg border border-slate-200 px-3 py-2 text-xs">关闭</button>
          <button type="button" onClick={() => void dispatch()} disabled={Boolean(busy) || (!pendingBatch && !selectedCards.length) || (Boolean(receipt) && receipt?.failed === 0)} className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-xs font-semibold text-white disabled:opacity-40">{busy === 'dispatch' && <Loader2 className="h-4 w-4 animate-spin" />}{pendingBatch ? '沿用当前批次重试' : `提交所选 ${selectedCards.length} 个方向`}</button>
        </div>
      </div>
    </div>
  );
};
