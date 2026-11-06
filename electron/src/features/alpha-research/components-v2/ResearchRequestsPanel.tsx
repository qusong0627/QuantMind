import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { cancelResearchRequest, getResearchRequest, listResearchRequests, type ResearchRequest } from '../services-v2/researchRequestsApi';
import { formatDateTime } from '../utils-v2';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';
import { Button } from './ui/Button';

const ACTIVE = new Set(['queued', 'running', 'stopping']);
const LABELS: Record<string, string> = { queued: '准备中', running: '进行中', stopping: '停止中', completed: '完成', failed: '失败', cancelled: '已取消' };

export const ResearchRequestsPanel: React.FC = () => {
  const authenticated = useAppSelector((state) => state.auth.isAuthenticated && !state.auth.isLoading);
  const [rows, setRows] = useState<ResearchRequest[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<ResearchRequest | null>(null);
  const [busy, setBusy] = useState('');
  const [epoch, setEpoch] = useState(getSessionGeneration);
  const listController = useRef<AbortController | null>(null);
  const actionController = useRef<AbortController | null>(null);
  useEffect(() => registerSessionReset(() => {
    listController.current?.abort(); actionController.current?.abort(); setRows([]); setDetail(null);
    setOffset(0); setHasMore(false); setError(''); setBusy(''); setEpoch(getSessionGeneration());
  }), []);
  const load = useCallback(async () => {
    if (!authenticated) return;
    listController.current?.abort();
    const controller = new AbortController(); listController.current = controller;
    const generation = getSessionGeneration();
    try {
      const page = await listResearchRequests(offset, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setRows(page.items); setHasMore(page.has_more); setError(''); }
    } catch (failure) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '研究请求历史读取失败');
    }
  }, [authenticated, offset]);
  useEffect(() => {
    void load(); const timer = window.setInterval(() => void load(), 10000);
    return () => { window.clearInterval(timer); listController.current?.abort(); actionController.current?.abort(); };
  }, [load, epoch]);
  const action = async (row: ResearchRequest, stop: boolean) => {
    actionController.current?.abort();
    const controller = new AbortController(); actionController.current = controller;
    const generation = getSessionGeneration(); setBusy(row.request_id);
    try {
      const value = stop ? await cancelResearchRequest(row.request_id, controller.signal) : await getResearchRequest(row.request_id, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) { setDetail(value); await load(); }
    } catch (failure) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '研究请求操作失败');
    } finally { if (!controller.signal.aborted && generation === getSessionGeneration()) setBusy(''); }
  };
  if (!authenticated) return null;
  return <Card className="glass">
    <CardHeader><CardTitle className="text-sm">解释与方向拆解请求</CardTitle><p className="text-xs text-muted-foreground">使用独立调用名额。关闭页面只停止等待，实际停止请使用下方按钮。调用尝试数用于平台预算，不代表服务商计费。</p></CardHeader>
    <CardContent className="space-y-3">
      {error && <p className="text-xs text-destructive">{error}</p>}
      {!rows.length && !error && <p className="text-xs text-muted-foreground">本页暂无研究请求。</p>}
      {rows.map((row) => <article key={row.request_id} className="rounded border border-border/60 p-3 space-y-1">
        <div className="flex flex-wrap justify-between gap-2 text-xs"><span>{row.kind === 'explain' ? '因子解释' : '研究方向拆解'} · {LABELS[row.status] || row.status}</span><span>{formatDateTime(row.created_at)}</span></div>
        <p className="text-xs text-muted-foreground break-all">{row.label} · 调用尝试 {row.call_count} 次{row.source_revision_no ? ` · 代码版本 ${row.source_revision_no}` : ''}</p>
        <div className="flex gap-2"><Button variant="ghost" size="sm" disabled={busy === row.request_id} onClick={() => void action(row, false)}>查看原结果</Button>{ACTIVE.has(row.status) && <Button variant="ghost" size="sm" disabled={row.status === 'stopping' || busy === row.request_id} onClick={() => void action(row, true)}>停止请求</Button>}</div>
        {row.error_message && <p className="text-xs text-destructive">{row.error_message}</p>}
      </article>)}
      <div className="flex justify-between text-xs"><span>第 {Math.floor(offset / 20) + 1} 页</span><div className="flex gap-2"><Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</Button><Button variant="outline" size="sm" disabled={!hasMore} onClick={() => setOffset(offset + 20)}>下一页</Button></div></div>
      {detail && <div className="border-t pt-3 space-y-2"><div className="flex justify-between text-xs"><span>{detail.request_id} · {LABELS[detail.status]}</span><button type="button" onClick={() => setDetail(null)}>关闭</button></div><p className="text-xs text-muted-foreground">显示当次请求保存的结果；版本或父本后续变化不会改写历史。</p><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-muted/30 p-3 text-xs">{detail.result ? JSON.stringify(detail.result, null, 2) : detail.error_message || '请求尚未产生结果'}</pre></div>}
    </CardContent>
  </Card>;
};
