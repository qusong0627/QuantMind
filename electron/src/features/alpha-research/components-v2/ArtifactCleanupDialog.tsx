import React, { useEffect, useRef, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { previewArtifactCleanup, submitArtifactCleanup, type ArtifactCleanupPreview } from '../services-v2/artifactCleanupApi';
import { generateId } from '../utils-v2';
import { Button } from './ui/Button';

export const ArtifactCleanupDialog: React.FC<{
  taskId: string; onClose: () => void; onSubmitted: () => void;
}> = ({ taskId, onClose, onSubmitted }) => {
  const [preview, setPreview] = useState<ArtifactCleanupPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [attempted, setAttempted] = useState(false);
  const requestKey = useRef('');
  const action = useRef<AbortController | null>(null);

  useEffect(() => registerSessionReset(() => {
    action.current?.abort(); setPreview(null); setConfirmed(false); onClose();
  }), [onClose]);
  useEffect(() => () => action.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    const generation = getSessionGeneration();
    action.current?.abort(); setSubmitting(false); setPreview(null); setLoading(true); setError(''); setConfirmed(false); setAttempted(false);
    requestKey.current = globalThis.crypto?.randomUUID?.() ?? generateId();
    previewArtifactCleanup([taskId], controller.signal).then((result) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setPreview(result);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '清理预览失败');
    }).finally(() => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setLoading(false);
    });
    return () => controller.abort();
  }, [taskId, refresh]);

  const submit = async () => {
    if (!preview || !confirmed || submitting) return;
    const controller = new AbortController();
    action.current?.abort(); action.current = controller;
    const generation = getSessionGeneration();
    setSubmitting(true); setError(''); setAttempted(true);
    try {
      await submitArtifactCleanup(preview, requestKey.current, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) { onSubmitted(); onClose(); }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '清理提交失败，可使用原请求重试');
    } finally {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setSubmitting(false);
    }
  };

  return <div role="dialog" aria-modal="true" aria-label="清理本人任务产物" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
    <div className="w-full max-w-lg rounded-2xl border border-border bg-background p-5 shadow-xl space-y-4">
      <div className="flex items-center justify-between"><h3 className="font-semibold">清理任务文件</h3><button type="button" aria-label="关闭清理预览" onClick={onClose}><X className="h-4 w-4" /></button></div>
      <p className="break-all font-mono text-xs text-muted-foreground">{taskId}</p>
      <p className="text-sm text-muted-foreground">删除本次研究的日志、工作区及原始文件产物。保留任务历史、已保存的规格与结果、因子代码和版本，以及已发布的训练数据引用。文件删除后无法恢复。</p>
      {loading && <div className="flex items-center gap-2 text-sm"><Loader2 className="h-4 w-4 animate-spin" />核验执行状态与产物</div>}
      {preview && <><p className="text-sm">预计释放 {(preview.total_bytes / (1024 ** 2)).toFixed(2)} MiB</p><label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} disabled={submitting} onChange={(event) => setConfirmed(event.target.checked)} />我确认删除上述任务文件，并了解文件无法恢复。</label></>}
      {error && <p className="text-sm text-destructive">{error}</p>}
      {attempted && error && <p className="text-xs text-muted-foreground">请求可能已被接收；“重试原请求”会返回同一作业。也可关闭弹窗，在任务中心确认状态。</p>}
      <div className="flex justify-end gap-2">
        <Button variant="outline" disabled={loading || submitting} onClick={() => setRefresh((value) => value + 1)}>重新预览</Button>
        <Button variant="destructive" disabled={!preview || !confirmed || submitting || loading} onClick={() => void submit()}>{submitting ? '提交中…' : attempted ? '重试原请求' : '确认并排队清理'}</Button>
      </div>
    </div>
  </div>;
};
