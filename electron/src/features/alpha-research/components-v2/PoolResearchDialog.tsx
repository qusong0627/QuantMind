import React, { useCallback, useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { cancelFactorJob } from '../services-v2/factorJobsApi';
import { getPoolResearch, listPoolResearch, submitPoolResearch, type PoolResearchDetail, type PoolResearchHistoryItem } from '../services-v2/poolResearchApi';
import { formatDateTime, generateId } from '../utils-v2';
import { Button } from './ui/Button';
import { PoolDiversityEvidence } from './PoolDiversityEvidence';

const ACTIVE = new Set(['queued', 'running', 'stopping']);
const STATUS: Record<string, string> = { queued: '排队', running: '计算中', stopping: '停止中', completed: '完成', failed: '失败', cancelled: '取消' };
const TRACE: Record<string, string> = { ok: '已计算', no_panel: '无可用面板', no_parents: '无可用父本', no_qualified_parents: '父本重叠样本不足', insufficient_days: '完整观测有效日不足', bad_panel: '面板不完整' };
const numberText = (value: number | null | undefined) => typeof value === 'number' ? value.toFixed(4) : '未知';
const message = (failure: unknown) => {
  const detail: unknown = isAxiosError(failure) ? failure.response?.data?.detail : undefined;
  return typeof detail === 'string' ? detail : failure instanceof Error ? failure.message : '池研究操作失败';
};

export const PoolResearchDialog: React.FC<{ universe: string; onClose: () => void }> = ({ universe, onClose }) => {
  const [detail, setDetail] = useState<PoolResearchDetail | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [history, setHistory] = useState<PoolResearchHistoryItem[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(false);
  const requestKey = useRef('');
  const actionBusy = useRef(false);
  const actionController = useRef<AbortController | null>(null);
  const historyController = useRef<AbortController | null>(null);
  const readController = useRef<AbortController | null>(null);
  useEffect(() => registerSessionReset(() => {
    actionController.current?.abort(); historyController.current?.abort(); readController.current?.abort();
    requestKey.current = ''; setDetail(null); setHistory([]); setSelectedId(''); setError(''); onClose();
  }), [onClose]);
  useEffect(() => () => { actionController.current?.abort(); historyController.current?.abort(); readController.current?.abort(); }, []);
  useEffect(() => {
    actionController.current?.abort(); actionBusy.current = false; setBusy(false);
    setDetail(null); setSelectedId(''); setHistory([]); setOffset(0); setRetry(false); requestKey.current = '';
  }, [universe]);
  const loadHistory = useCallback(async () => {
    historyController.current?.abort();
    const controller = new AbortController(); historyController.current = controller;
    const generation = getSessionGeneration();
    try {
      const value = await listPoolResearch(universe, offset, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setHistory(value.items); setHasMore(value.has_more); }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(message(failure));
    }
  }, [universe, offset]);
  useEffect(() => { void loadHistory(); return () => historyController.current?.abort(); }, [loadHistory, refresh]);
  useEffect(() => {
    if (!selectedId) return;
    let timer: number | undefined;
    const controller = new AbortController(); readController.current = controller;
    const generation = getSessionGeneration();
    const read = async () => {
      try {
        const value = await getPoolResearch(selectedId, controller.signal);
        if (controller.signal.aborted || generation !== getSessionGeneration()) return;
        setDetail(value);
        if (ACTIVE.has(value.job.status)) timer = window.setTimeout(() => void read(), 3000);
        else void loadHistory();
      } catch (failure: unknown) {
        if (!controller.signal.aborted && generation === getSessionGeneration()) setError(`${message(failure)}；可从历史重新查看原作业。`);
      }
    };
    void read();
    return () => { controller.abort(); if (timer !== undefined) window.clearTimeout(timer); };
  }, [selectedId, universe, refresh, loadHistory]);

  const submit = async () => {
    if (actionBusy.current) return;
    actionBusy.current = true;
    const controller = new AbortController(); actionController.current = controller;
    const generation = getSessionGeneration();
    if (!requestKey.current) requestKey.current = globalThis.crypto?.randomUUID?.() ?? generateId();
    setBusy(true); setError('');
    try {
      const job = await submitPoolResearch(universe, requestKey.current, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setSelectedId(job.job_id); setDetail(null); setRetry(false); requestKey.current = ''; setRefresh((value) => value + 1);
      }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setRetry(true); setError(`${message(failure)}。请求可能已入队，重试使用同一请求键，也可查看历史。`); }
    } finally {
      if (actionController.current === controller) actionBusy.current = false;
      if (!controller.signal.aborted && generation === getSessionGeneration()) setBusy(false);
    }
  };
  const stop = async () => {
    if (!detail || actionBusy.current) return;
    actionBusy.current = true;
    const controller = new AbortController(); actionController.current = controller;
    const generation = getSessionGeneration(); setBusy(true); setError('');
    try {
      await cancelFactorJob(detail.job.job_id, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) setRefresh((value) => value + 1);
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(message(failure));
    } finally {
      if (actionController.current === controller) actionBusy.current = false;
      if (!controller.signal.aborted && generation === getSessionGeneration()) setBusy(false);
    }
  };
  const result = detail?.job.result_snapshot;
  return <div role="dialog" aria-modal="true" aria-label="本人池正交与多样性证据" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4">
    <div className="w-full max-w-5xl max-h-[90vh] overflow-auto rounded-2xl border border-border bg-background p-5 shadow-xl space-y-4">
      <div className="flex justify-between"><h3 className="font-semibold">池正交与多样性证据 · {universe || '未标注股票池'}</h3><button type="button" aria-label="关闭池正交与多样性证据" onClick={onClose}><X className="h-4 w-4" /></button></div>
      <p className="text-sm text-muted-foreground">冻结本人当前池成员和已保存面板，使用 ICIR 较高的最多 5 个父本做逐日截面回归。默认每日至少 30 只股票、20 个有效日；残差与全部合格父本的最大日均秩相关绝对值小于 0.3 才标记正交。结果是历史研究证据，不改变因子、训练引用或入池门槛。</p>
      <p className="text-xs text-muted-foreground">本次使用现有面板，同时计算全池多样性，不重跑因子或读取新行情。缺面板、缺收益、样本不足和无法比较的父本均保留为未知。关闭弹窗不停止作业，可在任务中心继续查看或停止。</p>
      <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy || Boolean(detail && ACTIVE.has(detail.job.status))} onClick={() => void submit()}>{busy ? '提交中…' : retry ? '重试原提交' : '提交一次池研究评估'}</Button><Button variant="outline" size="sm" disabled={busy} onClick={() => setRefresh((value) => value + 1)}>刷新历史与结果</Button>{detail && ACTIVE.has(detail.job.status) && <Button variant="outline" size="sm" disabled={busy || detail.job.status === 'stopping'} onClick={() => void stop()}>停止原作业</Button>}</div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div className="space-y-1 text-xs"><h4 className="font-medium">本人该股票池历史</h4>{!history.length && <p className="text-muted-foreground">本页暂无历史</p>}{history.map((item) => <button type="button" key={item.job_id} className="block w-full rounded border border-border/50 p-2 text-left" onClick={() => { setSelectedId(item.job_id); setDetail(null); setError(''); setRefresh((value) => value + 1); }}>{formatDateTime(item.created_at)} · {STATUS[item.status]} · {item.job_id}{item.error_message ? ` · ${item.error_message}` : ''}</button>)}<div className="flex justify-end gap-2"><Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</Button><Button variant="outline" size="sm" disabled={!hasMore} onClick={() => setOffset(offset + 20)}>下一页</Button></div></div>
      {detail && <div className="space-y-3 border-t pt-3">
        <p className="text-sm break-all">{detail.job.job_id} · {STATUS[detail.job.status]}{detail.job.progress_snapshot?.stage ? ` · ${String(detail.job.progress_snapshot.stage)}` : ''}</p>
        <p className="text-xs text-muted-foreground">{detail.current_members_match ? '提交清单与当前池记录一致' : '当前池成员、指标或面板/代码版本已变化；以下仍是原作业证据'} · {detail.numeric_context_match ? '数值实现与提交时一致' : '数值实现已变化，原结果未重新计算'}</p>
        {detail.job.error_message && <p className="text-xs text-destructive">{detail.job.error_message}</p>}
        {result && <><p className="text-xs">成员 {result.factor_count} · 已计算 {result.stats.evaluated} · 正交 {result.stats.orthogonal} · 未正交 {result.stats.not_orthogonal} · 已计算但无法完整比较 {result.stats.unknown} · 无面板 {result.stats.no_panel}</p>
          <PoolDiversityEvidence result={result} />
          {result.results.map((row) => <article className="rounded border border-border/60 p-3 text-xs space-y-1" key={row.factor_id}>
            <h4 className="font-medium break-all">{row.factor_name} · 代码版本 {row.factor_revision_no} · {TRACE[row.orthogonality.status] || row.orthogonality.status}</h4>
            <p>{row.orthogonality.orthogonal === true ? '正交' : row.orthogonality.orthogonal === false ? '未正交' : '正交性未知'} · 残差 Rank IC {numberText(row.orthogonality.residual_ic)} · 原值 Rank IC {numberText(row.orthogonality.candidate_ic)} · 最大父本 |ρ| {numberText(row.orthogonality.max_parent_abs_corr)}</p>
            <p>OLS 有效日 {row.orthogonality.n_days ?? 0} · 完整观测 {row.orthogonality.n_obs ?? 0}{row.panel_window ? ` · 面板 ${row.panel_window.start_date} 至 ${row.panel_window.end_date}` : ''}</p>
            {row.panel_source_job_id && <p className="break-all text-muted-foreground">面板来源回测：{row.panel_source_job_id} · 代次 {row.panel_source_generation}</p>}
            <p className="break-all text-muted-foreground">父本：{row.orthogonality.parents?.map((parent) => `${parent.name || parent.factor_id} · 版本 ${parent.factor_revision_no} (|ρ| ${numberText(parent.abs_corr)})`).join('、') || '无'}</p>
            {row.orthogonality.note === 'fret_missing' && <p>前瞻收益缺失，未计算残差 IC。</p>}{row.orthogonality.note === 'frozen_panel_unavailable' && <p>冻结的面板不可读，没有替换为其他版本。</p>}
            {row.orthogonality.note === 'panel_source_unverified' && <p>面板来源代码版本或已完成作业凭证不匹配；请先通过正常回测生成有来源的私有面板。</p>}
          </article>)}
        </>}
      </div>}
    </div>
  </div>;
};
