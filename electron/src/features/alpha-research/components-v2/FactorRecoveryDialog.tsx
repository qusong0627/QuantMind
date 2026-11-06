import React, { useCallback, useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { getRecoveryHistory, getRecoveryJob, submitFactorRecovery, type RecoverySubmission, type RecoveryAttempt } from '../services-v2/factorRecoveryApi';
import { cancelFactorJob, type FactorJob } from '../services-v2/factorJobsApi';
import { generateId } from '../utils-v2';
import { Button } from './ui/Button';

export const FactorRecoveryDialog: React.FC<{
  factors: Array<{ factorId: string; factorName: string }>; universe: string;
  onClose: () => void; onChanged: () => void;
}> = ({ factors, universe, onClose, onChanged }) => {
  const [selectedUniverse, setSelectedUniverse] = useState(universe);
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [job, setJob] = useState<FactorJob | null>(null);
  const [history, setHistory] = useState<RecoveryAttempt[]>([]);
  const [historyError, setHistoryError] = useState('');
  const action = useRef<AbortController | null>(null);
  const frozen = useRef<{ body: RecoverySubmission; key: string } | null>(null);
  const [locked, setLocked] = useState(false);
  const applyJob = useCallback((value: FactorJob) => setJob((current) => {
    if (current && ['completed', 'failed', 'cancelled'].includes(current.status)) return current;
    if (current?.status === 'stopping' && ['queued', 'running'].includes(value.status)) return current;
    return value;
  }), []);
  useEffect(() => registerSessionReset(() => {
    action.current?.abort(); frozen.current = null; setJob(null); setHistory([]); setConfirmed(false); onClose();
  }), [onClose]);
  useEffect(() => () => action.current?.abort(), []);
  const singleId = factors.length === 1 ? factors[0]?.factorId : null;
  useEffect(() => {
    if (!singleId) return undefined;
    const controller = new AbortController();
    const generation = getSessionGeneration();
    getRecoveryHistory(singleId, controller.signal).then((rows) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setHistory(rows); setHistoryError(''); }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setHistoryError(failure instanceof Error ? failure.message : '历史读取失败');
    });
    return () => controller.abort();
  }, [singleId, job?.status]);
  const jobId = job?.job_id;
  const active = job && ['queued', 'running', 'stopping'].includes(job.status);
  useEffect(() => {
    if (!jobId || !active) return undefined;
    const controller = new AbortController();
    const generation = getSessionGeneration();
    let pending = false;
    const timer = window.setInterval(() => {
      if (pending) return;
      pending = true;
      getRecoveryJob(jobId, controller.signal).then((value) => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) applyJob(value);
      }).catch((failure: unknown) => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '状态读取失败');
      }).finally(() => { pending = false; });
    }, 4000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [jobId, active, applyJob]);

  const submit = async () => {
    if (!confirmed || busy) return;
    if (!frozen.current) frozen.current = {
      body: { factor_ids: factors.map((factor) => factor.factorId), universe: selectedUniverse.trim(),
        ...(start ? { start_date: start } : {}), ...(end ? { end_date: end } : {}), confirm_ai: true },
      key: globalThis.crypto?.randomUUID?.() ?? generateId(),
    };
    setLocked(true); setBusy(true); setError('');
    const controller = new AbortController(); action.current?.abort(); action.current = controller;
    const generation = getSessionGeneration();
    try {
      const value = await submitFactorRecovery(frozen.current.body, frozen.current.key, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setJob(value); onChanged(); }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '恢复提交失败');
    } finally { if (!controller.signal.aborted && generation === getSessionGeneration()) setBusy(false); }
  };
  const stop = async () => {
    if (!job || busy) return;
    const controller = new AbortController(); action.current?.abort(); action.current = controller;
    const generation = getSessionGeneration(); setBusy(true);
    try {
      const value = await cancelFactorJob(job.job_id, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) applyJob(value);
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '停止请求失败');
    } finally { if (!controller.signal.aborted && generation === getSessionGeneration()) setBusy(false); }
  };
  return <div role="dialog" aria-modal="true" aria-label="本人因子代码恢复" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
    <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-2xl bg-background border border-border p-5 space-y-4">
      <div className="flex justify-between"><h3 className="font-semibold">补码并评估 · {factors.length} 个本人因子</h3><button type="button" onClick={() => { onChanged(); onClose(); }} aria-label="关闭代码恢复"><X className="h-4 w-4" /></button></div>
      <p className="text-sm text-muted-foreground">缺码因子会调用系统 AI，最多两次生成尝试，并保存为新的代码版本后运行沙箱评估。它是按公式重新实现，不能视为找回历史原代码。已有代码只评估；连续三个因子失败后停止批次。</p>
      <ul className="max-h-32 overflow-auto text-sm list-disc pl-5">{factors.map((factor) => <li key={factor.factorId}>{factor.factorName}</li>)}</ul>
      <div className="grid gap-2 sm:grid-cols-3 text-xs"><label>股票池<input className="block w-full border rounded p-2 bg-background" value={selectedUniverse} disabled={locked} onChange={(event) => setSelectedUniverse(event.target.value)} /></label><label>开始日期（留空用默认）<input type="date" className="block w-full border rounded p-2 bg-background" value={start} disabled={locked} onChange={(event) => setStart(event.target.value)} /></label><label>结束日期（留空用默认）<input type="date" className="block w-full border rounded p-2 bg-background" value={end} disabled={locked} onChange={(event) => setEnd(event.target.value)} /></label></div>
      {!job && <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />我确认恢复上述因子，并同意缺码时调用系统 AI；已保存代码不会因停止或评估失败而撤回。</label>}
      {error && <p className="text-sm text-destructive">{error}</p>}
      {job ? <div className="text-sm space-y-2"><p>作业 {job.job_id} · {job.status}</p><p>{String(job.progress_snapshot?.stage || '')}</p>{job.result_snapshot && <p>评估成功 {String(job.result_snapshot.completed ?? 0)} · 失败 {String(job.result_snapshot.failed ?? 0)} · 未处理 {Array.isArray(job.result_snapshot.unprocessed) ? job.result_snapshot.unprocessed.length : 0}</p>}{job.error_message && <p className="text-destructive">{job.error_message}</p>}{active && <Button disabled={busy || job.status === 'stopping'} onClick={() => void stop()}>停止恢复作业</Button>}</div>
        : <Button disabled={!confirmed || !selectedUniverse.trim() || busy || Boolean(start && end && start > end)} onClick={() => void submit()}>{busy ? '提交中…' : locked ? '重试原请求' : '确认并排队恢复'}</Button>}
      <p className="text-xs text-muted-foreground">关闭弹窗不停止作业。可在任务中心查看持久化进度；重新打开可查看本人恢复历史。停止时已保存的新版本保留。</p>
      {job?.result_snapshot?.results?.map((item) => <div key={item.factor_id} className="text-xs border rounded p-2"><p>{item.factor_id} · {item.status} · 版本 {item.factor_revision_no ?? '未发布'}</p>{item.error && <p className="text-destructive">{item.error}</p>}</div>)}
      {singleId && <div className="border-t pt-3 text-xs space-y-2"><h4 className="font-medium">本人恢复历史</h4>{historyError && <p className="text-destructive">{historyError}</p>}{history.map((item) => <p key={item.job_id}>{item.created_at} · {item.status}（作业 {item.job_status}） · 版本 {item.revision_no ?? '未发布'} · {item.model || '已有代码'}{item.error_message ? ` · ${item.error_message}` : ''}</p>)}</div>}
    </div>
  </div>;
};
