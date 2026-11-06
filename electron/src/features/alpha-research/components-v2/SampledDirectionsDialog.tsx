import React, { useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import type { TaskConfig } from '../types-v2';
import { createResearchMiningBatchId } from '../services-v2/docApi';
import {
  dispatchSampledDirectionBatch, getSampledDirectionBatch, listSampledDirectionBatches,
  type SampledDirectionBatch, type SampledDirectionHistory, type SampledDirectionRequest,
} from '../services-v2/directionBatchesApi';
import { getStoredDirectionConfig } from '../utils-v2/miningDirections';
import { formatDateTime } from '../utils-v2';
import { DirectionSelectionEvidence } from './DirectionSelectionEvidence';
import { Button } from './ui/Button';

const STATUS: Record<string, string> = { not_submitted: '尚未入队', pending: '等待入队', queued: '排队', running: '运行中', stopping: '停止中', completed: '完成', failed: '失败', cancelled: '已取消' };
const message = (failure: unknown) => isAxiosError(failure) && typeof failure.response?.data?.detail === 'string'
  ? failure.response.data.detail : failure instanceof Error ? failure.message : '批次操作失败';

export const SampledDirectionsDialog: React.FC<{ config: TaskConfig; quotaSupported?: boolean; onClose: () => void; onOpenTasks: () => void }> = ({ config, quotaSupported, onClose, onOpenTasks }) => {
  const [candidates, setCandidates] = useState(() => getStoredDirectionConfig().labels.join('\n'));
  const [mode, setMode] = useState<'selected' | 'random' | 'quota'>(() => {
    const stored = getStoredDirectionConfig().mode;
    return stored === 'quota' && !quotaSupported ? 'selected' : stored;
  });
  const [count, setCount] = useState(3);
  const [rounds, setRounds] = useState(config.maxRounds ?? 3);
  const [pending, setPending] = useState<SampledDirectionRequest | null>(null);
  const [detail, setDetail] = useState<SampledDirectionBatch | null>(null);
  const [history, setHistory] = useState<SampledDirectionHistory[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const action = useRef<AbortController | null>(null);
  const listing = useRef<AbortController | null>(null);
  const reading = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  useEffect(() => registerSessionReset(() => {
    action.current?.abort(); listing.current?.abort(); reading.current?.abort();
    setPending(null); setDetail(null); setHistory([]); setCandidates(''); setError(''); onClose();
  }), [onClose]);
  useEffect(() => () => { action.current?.abort(); listing.current?.abort(); reading.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController(); listing.current?.abort(); listing.current = controller;
    const generation = getSessionGeneration();
    void listSampledDirectionBatches(offset, controller.signal).then((value) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setHistory(value.items); setHasMore(value.has_more); }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(message(failure));
    });
    return () => controller.abort();
  }, [offset, refresh]);
  const read = async (id: string) => {
    const controller = new AbortController(); reading.current?.abort(); reading.current = controller;
    const generation = getSessionGeneration();
    setError('');
    try {
      const value = await getSampledDirectionBatch(id, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setDetail(value); setPending(value.request); setCandidates(value.request.directions.join('\n'));
        setMode(value.request.selection_mode); setCount(value.request.num_directions); setRounds(value.request.loop_n);
      }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(message(failure));
    }
  };
  const submit = async () => {
    if (submitting.current) return;
    const directions = candidates.split('\n').map((value) => value.trim()).filter(Boolean);
    if (!pending && (!directions.length || directions.length > 100 || new Set(directions).size !== directions.length || directions.some((value) => value.length > 1000))) {
      setError('请输入 1 至 100 条不同的非空方向，每条不超过 1000 字。'); return;
    }
    const payload: SampledDirectionRequest = pending ?? {
      batch_id: createResearchMiningBatchId(), directions, market: 'a_share', universe: config.universe ?? 'all_a',
      loop_n: rounds, selection_mode: mode, num_directions: count, data_source: config.dataSource ?? 'qlib_bin',
      datasets: config.datasets, freq: 'day', mining_start: config.miningStart, mining_end: config.miningEnd,
      valid_start: config.validStart, valid_end: config.validEnd,
    };
    const controller = new AbortController(); action.current?.abort(); action.current = controller;
    reading.current?.abort();
    const generation = getSessionGeneration();
    submitting.current = true; setBusy(true); setPending(payload); setError('');
    try {
      const value = await dispatchSampledDirectionBatch(payload, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setDetail(value); setRefresh((previous) => previous + 1); }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(`${message(failure)}。批次ID已保留，请先找回状态，再用原请求继续。`);
    } finally {
      if (action.current === controller) submitting.current = false;
      if (!controller.signal.aborted && generation === getSessionGeneration()) setBusy(false);
    }
  };
  return <div role="dialog" aria-modal="true" aria-label="多方向选择与派发" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4">
    <div className="w-full max-w-4xl max-h-[90vh] overflow-auto rounded-2xl border border-border bg-background p-5 shadow-xl space-y-4">
      <div className="flex justify-between"><h3 className="font-semibold">多方向选择与派发 · {config.universe ?? 'all_a'}</h3><button type="button" aria-label="关闭多方向派发" onClick={onClose}><X className="h-4 w-4" /></button></div>
      <p className="text-sm text-muted-foreground">按顺序选择前 N 条、按本人在该股票池的历史提交数加权不放回抽取 N 条，或按成功率×饱和惩罚三档配额（探索/优选/变异）分配 N 条。每条方向分别进入现有队列，仍受个人和全站名额限制，可能仅部分入队。提交会启动实际挖掘，使用系统配置的模型，可能产生费用。</p>
      <p className="text-xs text-muted-foreground">首次选择会保存；原批次重试不会重抽、重复创建已有任务或重跑终态任务。关闭窗口只停止等待。历史读取不会继续派发，可在任务中心分别停止已经入队的任务。</p>
      <label className="block text-sm">候选方向（每行一条）<textarea rows={5} className="mt-1 w-full rounded border border-border bg-background p-2" value={candidates} disabled={busy || Boolean(pending)} onChange={(event) => setCandidates(event.target.value)} /></label>
      <div className="flex flex-wrap gap-4 text-sm"><label>选择方式 <select value={mode} disabled={busy || Boolean(pending)} onChange={(event) => setMode(event.target.value as 'selected' | 'random' | 'quota')}><option value="selected">按候选顺序</option><option value="random">本人历史加权随机</option>{quotaSupported && <option value="quota">三档配额加权</option>}</select></label><label>方向数 <input type="number" min={1} max={10} value={count} disabled={busy || Boolean(pending)} onChange={(event) => setCount(Math.max(1, Math.min(10, Number(event.target.value) || 1)))} className="w-16" /></label><label>每方向轮数 <input type="number" min={1} max={20} value={rounds} disabled={busy || Boolean(pending)} onChange={(event) => setRounds(Math.max(1, Math.min(20, Number(event.target.value) || 1)))} className="w-16" /></label></div>
      <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy || Boolean(detail && detail.items.every((item) => item.task_id))} onClick={() => void submit()}>{busy ? '提交中…' : pending ? '用原批次继续派发' : '确认选择并派发'}</Button>{pending && <Button variant="outline" size="sm" disabled={busy} onClick={() => void read(pending.batch_id)}>找回原批次状态</Button>}<Button variant="outline" size="sm" disabled={busy} onClick={() => { reading.current?.abort(); setPending(null); setDetail(null); setError(''); }}>准备新批次</Button><Button variant="outline" size="sm" onClick={onOpenTasks}>去任务中心</Button></div>
      {pending && <p className="text-xs break-all">保留的批次ID：{pending.batch_id}</p>}
      {error && <p className="text-sm text-destructive">{error}</p>}
      {detail && <section className="border-t pt-3 space-y-3"><p className="text-xs">{formatDateTime(detail.created_at)} · 请求 {detail.request.num_directions} 条，实际选择 {detail.items.length} 条 · {detail.request.selection_mode === 'random' ? '本人历史加权随机' : detail.request.selection_mode === 'quota' ? '三档配额加权' : '按顺序'} · 股票池 {detail.request.universe} · 每条 {detail.request.loop_n} 轮</p>{detail.items.map((item) => <article key={item.index} className="rounded border border-border/60 p-3 space-y-2 text-xs"><p className="break-all">{item.index + 1}. {item.direction} · {STATUS[item.status] || item.status} · {item.task_id || '尚无已保存任务回执'}</p>{detail.dispatch?.items[item.index]?.error && <p className="text-destructive">{detail.dispatch.items[item.index].error}</p>}{item.evidence ? <DirectionSelectionEvidence value={item.evidence} /> : <p className="text-muted-foreground">此项直接按顺序或全集派发，没有抽样凭证。</p>}</article>)}</section>}
      <section className="space-y-2 border-t pt-3 text-xs"><div className="flex justify-between"><h4 className="font-medium">本人批次历史</h4><button type="button" disabled={busy} onClick={() => setRefresh((value) => value + 1)}>刷新历史</button></div>{history.map((item) => <button type="button" key={item.batch_id} disabled={busy} onClick={() => void read(item.batch_id)} className="block w-full rounded border border-border/50 p-2 text-left break-all">{formatDateTime(item.created_at)} · {item.universe} · {item.selected_count} 条 · {item.batch_id}</button>)}<div className="flex justify-end gap-2"><Button variant="outline" size="sm" disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</Button><Button variant="outline" size="sm" disabled={busy || !hasMore} onClick={() => setOffset(offset + 20)}>下一页</Button></div></section>
    </div>
  </div>;
};
