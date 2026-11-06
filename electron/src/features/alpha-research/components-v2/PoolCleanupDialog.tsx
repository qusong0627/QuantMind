import React, { useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { Loader2, X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { getPoolCleanupSuggestions, type PoolCleanupReport, type PoolCleanupSuggestion } from '../services-v2/poolCleanupApi';
import { removeFactorFromPool } from '../services-v2/factorJobsApi';
import { formatDateTime } from '../utils-v2';
import { Button } from './ui/Button';

function errorText(failure: unknown): string {
  const detail: unknown = isAxiosError(failure) ? failure.response?.data?.detail : undefined;
  return typeof detail === 'string' ? detail : failure instanceof Error ? failure.message : '本人研究池操作失败';
}
const metric = (value: number | null) => value == null ? '缺失' : value.toFixed(4);
const universeLabel = (value: string) => value || '未标注股票池';

export const PoolCleanupDialog: React.FC<{
  universe?: string;
  onClose: () => void;
  onRemoved: (factorId: string, universe: string) => void;
}> = ({ universe, onClose, onRemoved }) => {
  const [report, setReport] = useState<PoolCleanupReport | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<PoolCleanupSuggestion | null>(null);
  const actionController = useRef<AbortController | null>(null);
  const readController = useRef<AbortController | null>(null);
  const actionBusy = useRef(false);
  useEffect(() => registerSessionReset(() => {
    readController.current?.abort(); actionController.current?.abort();
    setReport(null); setSelected(null); setNotice(''); setError(''); onClose();
  }), [onClose]);
  useEffect(() => () => { readController.current?.abort(); actionController.current?.abort(); }, []);
  useEffect(() => { setOffset(0); setSelected(null); setNotice(''); }, [universe]);
  useEffect(() => {
    const controller = new AbortController(); readController.current = controller;
    const generation = getSessionGeneration(); setLoading(true); setReport(null); setSelected(null); setError('');
    getPoolCleanupSuggestions(universe, offset, controller.signal).then((value) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setReport(value);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorText(failure));
    }).finally(() => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setLoading(false);
    });
    return () => controller.abort();
  }, [universe, offset, refresh]);

  const remove = async () => {
    if (!selected || actionBusy.current) return;
    actionBusy.current = true;
    const target = selected;
    const controller = new AbortController(); actionController.current = controller;
    const generation = getSessionGeneration(); setBusy(true); setError('');
    try {
      await removeFactorFromPool(target.factor_id, { universe: target.universe }, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        onRemoved(target.factor_id, target.universe);
        setSelected(null); setNotice(`「${target.factor_name}」已退出 ${universeLabel(target.universe)}，可在因子库重新加入。`);
        setOffset(0); setRefresh((value) => value + 1);
      }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setError(`${errorText(failure)}。若网络中断，请先刷新核对成员状态。`);
      }
    } finally {
      actionBusy.current = false;
      if (!controller.signal.aborted && generation === getSessionGeneration()) setBusy(false);
    }
  };

  return <div role="dialog" aria-modal="true" aria-label="本人因子池清理建议" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4">
    <div className="w-full max-w-4xl max-h-[90vh] overflow-y-auto rounded-2xl border border-border bg-background p-5 shadow-xl space-y-4">
      <div className="flex items-center justify-between"><h3 className="font-semibold">研究池清理建议</h3><button type="button" aria-label="关闭研究池建议" onClick={onClose}><X className="h-4 w-4" /></button></div>
      <p className="text-sm text-muted-foreground">基于本人已保存的研究证据，分别在每个股票池内比较。缺失值不按零参与；建议不等于无效结论，由你逐条决定是否退出此池。因子本身、历史产物和训练引用均保留，其他股票池成员关系不变。</p>
      {notice && <p className="text-sm text-success">{notice}</p>}
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div className="flex items-center justify-between text-xs text-muted-foreground"><span>{universe === undefined ? '本人全部股票池（各池独立评估）' : universeLabel(universe)}</span><Button variant="outline" size="sm" disabled={loading || busy} onClick={() => setRefresh((value) => value + 1)}>刷新证据</Button></div>
      {loading && <p className="flex items-center gap-2 text-sm"><Loader2 className="h-4 w-4 animate-spin" />读取本人池证据</p>}
      {report && <>
        <p className="text-xs text-muted-foreground">证据快照 {formatDateTime(report.as_of)} · 已完成活跃成员 {report.pool_size} · 建议 {report.total} · 已退出池成员 {report.archived_member_count}</p>
        <div className="rounded bg-muted/30 p-3 text-xs space-y-1">
          <p>冗余：最大 |ρ| ≥ {report.criteria.corr_dup} 且另一成员更强；预测力：池内后 {Math.round(report.criteria.weak_icir_quantile * 100)}%（至少 {report.criteria.min_icir_sample} 个有效 ICIR，同值不判弱）；多样性贡献 ≤ 0。</p>
          <p>缺少证据：ICIR {report.missing_evidence.icir}，检索分 {report.missing_evidence.pool_score}，当前版本相关边 {report.missing_evidence.max_pool_corr}，多样性贡献 {report.missing_evidence.diversity_contrib}。旧版未绑定面板版本的相关边需在因子详情中重建值关系后参与；未计算的多样性不判零。</p>
          <p>数值来自已保存指标，可能属于不同评估窗口；比较仅作研究池整理参考。</p>
          <div className="max-h-24 overflow-auto">{report.scopes.map((scope) => <p key={scope.universe}>{universeLabel(scope.universe)}：{scope.pool_size} 成员 / {scope.icir_sample_size} 个有效 ICIR / 末位阈值 {metric(scope.weak_icir_threshold)}</p>)}</div>
        </div>
        {!report.items.length && <p className="text-sm text-muted-foreground">本页没有符合判据的建议；缺失证据不会据此判定为弱因子。</p>}
        {report.items.map((item) => <article key={`${item.universe}\u0000${item.factor_id}`} className="rounded border border-border/60 p-3 space-y-2">
          <div className="flex flex-wrap justify-between gap-2 text-sm"><span className="break-all">{item.factor_name} · {universeLabel(item.universe)}</span><span>{item.severity === 'high' ? '优先审阅' : '可审阅'}</span></div>
          <p className="text-xs text-muted-foreground break-all">{item.factor_id}</p>
          <p className="text-xs">ICIR {metric(item.icir)} · 检索分 {metric(item.pool_score)} · 最大 |ρ| {metric(item.max_pool_corr)} · 多样性贡献 {metric(item.diversity_contrib)}</p>
          {item.diversity_source && <p className="text-xs text-muted-foreground break-all">多样性来源：{item.diversity_source.job_id} · {formatDateTime(item.diversity_source.completed_at)} · 共同有效日 {item.diversity_source.n_days}；全池成员、代码和面板版本与该作业一致。</p>}
          {item.reasons.map((reason) => <p className="text-xs" key={reason.code}><strong>{reason.label}：</strong>{reason.detail}</p>)}
          <Button variant="outline" size="sm" disabled={busy || loading} onClick={() => { setSelected(item); setError(''); }}>审阅并退出此池</Button>
        </article>)}
        <div className="flex justify-between text-xs"><span>第 {Math.floor(offset / 50) + 1} 页</span><div className="flex gap-2"><Button variant="outline" size="sm" disabled={offset === 0 || busy || loading} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><Button variant="outline" size="sm" disabled={!report.has_more || busy || loading} onClick={() => setOffset(offset + 50)}>下一页</Button></div></div>
      </>}
      {selected && <div className="rounded border border-destructive/40 p-3 space-y-2 text-sm"><p>确认将「{selected.factor_name}」退出 {universeLabel(selected.universe)}？此操作会停止以后从该池检索此因子，可在因子库重新加入。</p><div className="flex justify-end gap-2"><Button variant="outline" size="sm" disabled={busy} onClick={() => setSelected(null)}>取消</Button><Button variant="destructive" size="sm" disabled={busy || loading} onClick={() => void remove()}>{busy ? '提交中…' : '确认退出此池'}</Button></div></div>}
    </div>
  </div>;
};
