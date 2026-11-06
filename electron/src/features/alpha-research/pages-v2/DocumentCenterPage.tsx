import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Download, Eye, FileText, Pencil, RefreshCw, Save, Trash2, UploadCloud, WandSparkles, CircleStop, Play } from 'lucide-react';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import {
  deleteResearchDocument,
  createDocumentMiningBatchId,
  decomposeResearchDirection,
  dispatchDocumentMiningBatch,
  dispatchDocumentMining,
  downloadResearchDocument,
  getResearchDocument,
  getResearchDocumentMarkdown,
  listResearchDocuments,
  reconcileDocumentParse,
  saveOrganizedDraft,
  submitDocumentOrganization,
  submitDocumentParse,
  uploadResearchDocument,
  type ResearchDocument,
  type DirectionDecomposeResult,
  type DocumentMiningBatchReceipt,
} from '../services-v2/docApi';
import { cancelFactorJob, listFactorPool, type FactorPoolEntry } from '../services-v2/factorJobsApi';
import { Button } from '../components-v2/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';

const PAGE_SIZE = 20;

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function errorMessage(error: unknown): string {
  const response = (error as { response?: { data?: { detail?: unknown } } })?.response;
  return typeof response?.data?.detail === 'string'
    ? response.data.detail
    : error instanceof Error
      ? error.message
      : '文档请求失败，请稍后重试';
}

export const DocumentCenterPage: React.FC<{ hideHeader?: boolean }> = ({ hideHeader = false }) => {
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const [documents, setDocuments] = useState<ResearchDocument[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [markdownPreview, setMarkdownPreview] = useState<{ filename: string; content: string; truncated: boolean } | null>(null);
  const [organizeTarget, setOrganizeTarget] = useState<ResearchDocument | null>(null);
  const [organizeKind, setOrganizeKind] = useState<'free' | 'paper'>('free');
  const [organizeExtra, setOrganizeExtra] = useState('');
  const [organizedDraft, setOrganizedDraft] = useState<{ docId: string; filename: string; text: string } | null>(null);
  const [miningDirection, setMiningDirection] = useState<{ docId: string; filename: string; text: string } | null>(null);
  const [decomposedDirections, setDecomposedDirections] = useState<DirectionDecomposeResult | null>(null);
  const [directionCardDrafts, setDirectionCardDrafts] = useState<string[]>([]);
  const [selectedDirectionIndexes, setSelectedDirectionIndexes] = useState<number[]>([]);
  const [directionBatchId, setDirectionBatchId] = useState<string | null>(null);
  const [directionBatchReceipt, setDirectionBatchReceipt] = useState<DocumentMiningBatchReceipt | null>(null);
  const [seedCandidates, setSeedCandidates] = useState<FactorPoolEntry[]>([]);
  const [selectedSeedIds, setSelectedSeedIds] = useState<string[]>([]);
  const [directionSeedIds, setDirectionSeedIds] = useState<Array<string | null>>([]);
  const [sessionEpoch, setSessionEpoch] = useState(() => getSessionGeneration());
  const fileInput = useRef<HTMLInputElement | null>(null);
  const listController = useRef<AbortController | null>(null);
  const actionController = useRef<AbortController | null>(null);
  const requestEpoch = useRef(0);

  useEffect(() => registerSessionReset(() => {
    requestEpoch.current += 1;
    listController.current?.abort();
    actionController.current?.abort();
    listController.current = null;
    actionController.current = null;
    setDocuments([]);
    setOffset(0);
    setHasMore(false);
    setBusyId('');
    setError('');
    setMessage('');
    setMarkdownPreview(null);
    setOrganizeTarget(null);
    setOrganizedDraft(null);
    setMiningDirection(null);
    setDecomposedDirections(null);
    setDirectionCardDrafts([]);
    setSelectedDirectionIndexes([]);
    setDirectionBatchId(null);
    setDirectionBatchReceipt(null);
    setSeedCandidates([]);
    setSelectedSeedIds([]);
    setDirectionSeedIds([]);
    setOrganizeExtra('');
    setLoading(false);
    setSessionEpoch(getSessionGeneration());
  }), []);

  const loadDocuments = useCallback(async (nextOffset = offset) => {
    if (!isAuthenticated || isAuthenticating) {
      setDocuments([]);
      setHasMore(false);
      setLoading(false);
      return;
    }
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    const generation = getSessionGeneration();
    const requestId = ++requestEpoch.current;
    setLoading(true);
    setError('');
    try {
      const page = await listResearchDocuments({ limit: PAGE_SIZE, offset: nextOffset }, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration() || requestId !== requestEpoch.current) return;
      setDocuments(page.items);
      setHasMore(page.has_more);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration() && requestId === requestEpoch.current) setLoading(false);
    }
  }, [isAuthenticated, isAuthenticating, offset]);

  useEffect(() => {
    void loadDocuments(offset);
  }, [loadDocuments, offset, sessionEpoch]);

  useEffect(() => {
    if (!documents.some((document) => document.status === 'parsing' || ['queued', 'running', 'stopping'].includes(document.organize_state || ''))) return undefined;
    const timer = window.setInterval(() => void loadDocuments(offset), 5000);
    return () => window.clearInterval(timer);
  }, [documents, loadDocuments, offset]);

  useEffect(() => () => {
    listController.current?.abort();
    actionController.current?.abort();
  }, []);

  const startAction = () => {
    actionController.current?.abort();
    const controller = new AbortController();
    actionController.current = controller;
    return { controller, generation: getSessionGeneration() };
  };

  const handleUpload = async (fileList?: FileList | null) => {
    const files = fileList ? Array.from(fileList) : [];
    if (!files.length) return;
    setError('');
    setMessage('');
    if (files.length > 10) {
      setError('每批最多选择 10 个 PDF 文件');
      if (fileInput.current) fileInput.current.value = '';
      return;
    }
    if (files.some((file) => !file.name.toLowerCase().endsWith('.pdf'))
      || files.reduce((total, file) => total + file.size, 0) > 20 * 1024 * 1024) {
      setError('请选择不超过 10 个、合计不超过 20 MiB 的 PDF 文件');
      if (fileInput.current) fileInput.current.value = '';
      return;
    }
    const { controller, generation } = startAction();
    setBusyId('upload');
    try {
      await uploadResearchDocument(files, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMessage(files.length === 1
        ? `已安全保存：${files[0].name}`
        : `已安全保存 ${files.length} 个文件并合并为一份本人文档`);
      setOffset(0);
      await loadDocuments(0);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  const handleDownload = async (document: ResearchDocument) => {
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      const blob = await downloadResearchDocument(document.doc_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      const url = URL.createObjectURL(blob);
      const anchor = window.document.createElement('a');
      anchor.href = url;
      anchor.download = document.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleDelete = async (document: ResearchDocument) => {
    if (!window.confirm(`删除本人文档“${document.filename}”？`)) return;
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      await deleteResearchDocument(document.doc_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMessage(`已删除：${document.filename}`);
      await loadDocuments(offset);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleParse = async (document: ResearchDocument) => {
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      const job = await submitDocumentParse(document.doc_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMessage(`已提交解析任务 ${job.job_id.slice(0, 8)}，完成后将显示结果状态`);
      await loadDocuments(offset);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleCancelParse = async (document: ResearchDocument) => {
    if (!document.parse_job_id) return;
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      await cancelFactorJob(document.parse_job_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMessage('已请求停止解析；系统会等待 MinerU 外部任务确认终态');
      await loadDocuments(offset);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleReconcileParse = async (document: ResearchDocument) => {
    const batchId = window.prompt(
      '请从 MinerU 管理端找到这份文档原提交对应的 batch_id。系统会查询批次并核对文档全部原件 ID；不要填写其他文档的批次。',
    )?.trim();
    if (!batchId) return;
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      const job = await reconcileDocumentParse(document.doc_id, batchId, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMessage(`已核验并恢复原解析批次 ${job.job_id.slice(0, 8)}；没有新建 MinerU 任务`);
      await loadDocuments(offset);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handlePreview = async (document: ResearchDocument) => {
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      const preview = await getResearchDocumentMarkdown(document.doc_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMarkdownPreview({ filename: document.filename, content: preview.content, truncated: preview.truncated });
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleSubmitOrganization = async () => {
    const document = organizeTarget;
    if (!document) return;
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      const job = await submitDocumentOrganization(
        document.doc_id,
        { kind: organizeKind, extra: organizeExtra.trim() || undefined },
        controller.signal,
      );
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setOrganizeTarget(null);
      setOrganizeExtra('');
      setMessage(`已提交整理任务 ${job.job_id.slice(0, 8)}，完成后可编辑并确认草稿`);
      await loadDocuments(offset);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleCancelOrganization = async (document: ResearchDocument) => {
    if (!document.organize_job_id) return;
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      await cancelFactorJob(document.organize_job_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMessage('已请求停止整理任务');
      await loadDocuments(offset);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleOpenOrganizedDraft = async (document: ResearchDocument) => {
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      const detail = await getResearchDocument(document.doc_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      if (typeof detail.organized_text !== 'string') throw new Error('整理草稿不可用');
      setOrganizedDraft({
        docId: document.doc_id,
        filename: document.filename,
        text: detail.organized_text,
      });
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleSaveOrganizedDraft = async () => {
    if (!organizedDraft) return;
    const { controller, generation } = startAction();
    setBusyId(organizedDraft.docId);
    setError('');
    try {
      await saveOrganizedDraft(organizedDraft.docId, organizedDraft.text, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMessage('整理草稿已保存到本人文档');
      await loadDocuments(offset);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleOpenMiningDirection = async (document: ResearchDocument) => {
    const { controller, generation } = startAction();
    setBusyId(document.doc_id);
    setError('');
    try {
      const detail = await getResearchDocument(document.doc_id, controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      const markdown = detail.organized_text || '';
      const section = markdown.match(/^## 挖掘方向[^\n]*\n+([\s\S]*?)(?=^## |\s*$)/m)?.[1]?.trim();
      const canonicalDirection = (detail.organized_payload as { direction?: unknown } | null)?.direction;
      const direction = section || (typeof canonicalDirection === 'string' ? canonicalDirection : '');
      if (!direction.trim()) throw new Error('整理稿中没有可派发的挖掘方向');
      let candidates: FactorPoolEntry[] = [];
      try {
        candidates = await listFactorPool({ universe: 'csi300', limit: 500 }, controller.signal);
      } catch {
        // Parent selection is optional; the document direction remains usable.
      }
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setSeedCandidates(candidates.filter((entry) => entry.status === 'completed'));
      setSelectedSeedIds([]);
      setDirectionSeedIds([]);
      setMiningDirection({ docId: document.doc_id, filename: document.filename, text: direction });
      setDecomposedDirections(null);
      setDirectionCardDrafts([]);
      setSelectedDirectionIndexes([]);
      setDirectionBatchId(null);
      setDirectionBatchReceipt(null);
      setDirectionSeedIds([]);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleDispatchDocumentMining = async () => {
    if (!miningDirection) return;
    const { controller, generation } = startAction();
    setBusyId(miningDirection.docId);
    setError('');
    try {
      const task = await dispatchDocumentMining(miningDirection.docId, miningDirection.text.trim(), controller.signal);
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setMiningDirection(null);
      setMessage(`已从本人文档派发挖掘任务 ${task.task_id}（${task.status}），可在任务中心查看`);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const handleDecomposeMiningDirection = async () => {
    if (!miningDirection) return;
    const { controller, generation } = startAction();
    setBusyId(miningDirection.docId);
    setError('');
    try {
      const result = await decomposeResearchDirection(
        miningDirection.text.trim(),
        controller.signal,
        selectedSeedIds,
      );
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setDecomposedDirections(result);
      setDirectionCardDrafts(result.cards.map((card) => [
        card.title,
        `研究假设：${card.hypothesis}`,
        card.rationale ? `研究逻辑：${card.rationale}` : '',
        card.evaluation_hint ? `验证建议：${card.evaluation_hint}` : '',
      ].filter(Boolean).join('\n')));
      setSelectedDirectionIndexes(result.cards.map((_, index) => index));
      setDirectionSeedIds(result.cards.map((card) => (
        card.seed_factor_id && selectedSeedIds.includes(card.seed_factor_id) ? card.seed_factor_id : null
      )));
      setDirectionBatchId(null);
      setDirectionBatchReceipt(null);
      if (result.dropped > 0) setMessage(`方向已拆解；超出上限的 ${result.dropped} 张卡片未展示`);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const applyDirectionCard = (card: DirectionDecomposeResult['cards'][number], index: number) => {
    if (!miningDirection) return;
    setMiningDirection({ ...miningDirection, text: directionCardDrafts[index] || card.hypothesis });
  };

  const handleDispatchSelectedDirections = async () => {
    if (!miningDirection || selectedDirectionIndexes.length === 0) return;
    const selected = [...selectedDirectionIndexes].sort((a, b) => a - b);
    const directions = selected.map((index) => directionCardDrafts[index]?.trim() || '');
    if (directions.some((direction) => !direction)) {
      setError('所选假设卡不能为空，请补全内容或取消选择');
      return;
    }
    const batchId = directionBatchId || createDocumentMiningBatchId();
    setDirectionBatchId(batchId);
    const { controller, generation } = startAction();
    setBusyId(miningDirection.docId);
    setError('');
    try {
      const receipt = await dispatchDocumentMiningBatch(
        miningDirection.docId,
        directions,
        batchId,
        controller.signal,
        selected.map((index) => directionSeedIds[index] || null),
      );
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setDirectionBatchReceipt(receipt);
      setMessage(`批量派发完成：已启动 ${receipt.started}，排队 ${receipt.queued}，失败 ${receipt.failed}；可查看逐项回执`);
    } catch (cause) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(errorMessage(cause));
    } finally {
      if (generation === getSessionGeneration()) setBusyId('');
    }
  };

  const statusLabel = (document: ResearchDocument) => {
    if (['queued', 'running', 'stopping'].includes(document.organize_state || '')) {
      return document.organize_state === 'queued' ? '等待整理' : document.organize_state === 'stopping' ? '正在停止整理' : '整理中';
    }
    if (document.organize_state === 'failed') return `整理失败${document.error ? `：${document.error}` : ''}`;
    if (document.organize_state === 'cancelled') return '整理已取消';
    if (document.status === 'uploaded') return '已保存';
    if (document.status === 'parsing') {
      if (document.parse_state === 'submitting') return '正在提交解析';
      if (document.parse_state === 'pending') return '等待解析';
      if (document.parse_state === 'running') return `解析中${document.page_count ? ` · ${document.page_count} 页` : ''}`;
      if (document.parse_state === 'uncertain') return '提交状态待核验';
      return '解析中';
    }
    if (document.status === 'parsed') return `解析完成${document.page_count ? ` · ${document.page_count} 页` : ''}`;
    if (document.status === 'parse_failed') return `解析失败${document.error ? `：${document.error}` : ''}`;
    if (document.status === 'organized') return `已整理${document.organize_kind ? ` · ${document.organize_kind === 'paper' ? '论文复现' : '自由挖掘'}` : ''}`;
    return document.status;
  };

  return (
    <div className="space-y-4 animate-fade-in-up">
      {!hideHeader && (
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <FileText className="h-5 w-5 text-primary" />研究文档
          </h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            PDF 原文和 MinerU 解析产物按当前账号隔离保存；解析结果可供后续整理与文档驱动挖掘使用。
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" className="text-xs" onClick={() => void loadDocuments(offset)} disabled={loading || !!busyId}>
            <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${loading ? 'animate-spin' : ''}`} />刷新
          </Button>
          <Button variant="primary" size="sm" className="text-xs" onClick={() => fileInput.current?.click()} disabled={!isAuthenticated || !!busyId}>
            <UploadCloud className="h-3.5 w-3.5 mr-1.5" />{busyId === 'upload' ? '上传中…' : '上传 PDF'}
          </Button>
          <input
            ref={fileInput}
            type="file"
            accept="application/pdf,.pdf"
            multiple
            className="hidden"
            onChange={(event) => void handleUpload(event.target.files)}
          />
        </div>
      </header>
      )}

      {error && <Card className="border-destructive/50"><CardContent className="p-4 flex items-center gap-2 text-destructive"><AlertCircle className="h-4 w-4" />{error}</CardContent></Card>}
      {message && <div role="status" className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-xs text-emerald-700">{message}</div>}

      <Card className="glass">
        <CardHeader><CardTitle className="text-sm">本人文档</CardTitle></CardHeader>
        <CardContent className="p-0 overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="border-b border-border/60 text-muted-foreground">
              <th className="p-3 text-left">文件名</th><th className="p-3 text-left">大小</th><th className="p-3 text-left">状态</th><th className="p-3 text-left">上传时间</th><th className="p-3 text-right">操作</th>
            </tr></thead>
            <tbody>
              {documents.map((document) => (
                <tr key={document.doc_id} className="border-b border-border/40 hover:bg-muted/30">
                  <td className="p-3"><div className="font-medium">{document.filename}</div><div className="font-mono text-xs text-muted-foreground">{document.sha256.slice(0, 12)}…</div></td>
                  <td className="p-3">{formatBytes(document.size_bytes)}</td>
                  <td className="p-3"><span className="rounded-full bg-muted px-2 py-1 text-xs">{statusLabel(document)}</span></td>
                  <td className="p-3 text-muted-foreground">{new Date(document.created_at).toLocaleString()}</td>
                  <td className="p-3 text-right whitespace-nowrap">
                    {(document.status === 'uploaded' || document.status === 'parse_failed') && (
                      <Button variant="ghost" size="sm" title="开始解析" onClick={() => void handleParse(document)} disabled={!!busyId}>
                        <WandSparkles className="h-4 w-4" />
                      </Button>
                    )}
                    {document.status === 'parsing' && document.parse_job_id && (
                      document.parse_state === 'uncertain' ? (
                        <Button variant="outline" size="sm" title="核验并恢复原 MinerU 批次" onClick={() => void handleReconcileParse(document)} disabled={!!busyId}>
                          {busyId === document.doc_id ? '核验中…' : '核验批次'}
                        </Button>
                      ) : (
                        <Button variant="ghost" size="sm" title="停止解析" onClick={() => void handleCancelParse(document)} disabled={!!busyId}>
                          <CircleStop className="h-4 w-4 text-destructive" />
                        </Button>
                      )
                    )}
                    {(document.status === 'parsed' || document.status === 'organized') && (
                      <Button variant="ghost" size="sm" title="预览解析内容" onClick={() => void handlePreview(document)} disabled={!!busyId}>
                        <Eye className="h-4 w-4" />
                      </Button>
                    )}
                    {(document.status === 'parsed' || document.status === 'organized') && !['queued', 'running', 'stopping'].includes(document.organize_state || '') && (
                      <Button variant="ghost" size="sm" title="整理为挖掘简报或论文复现卡" onClick={() => { setOrganizeTarget(document); setOrganizeKind(document.organize_kind === 'paper' ? 'paper' : 'free'); }} disabled={!!busyId}>
                        <WandSparkles className="h-4 w-4" />
                      </Button>
                    )}
                    {['queued', 'running', 'stopping'].includes(document.organize_state || '') && document.organize_job_id && (
                      <Button variant="ghost" size="sm" title="停止整理" onClick={() => void handleCancelOrganization(document)} disabled={!!busyId || document.organize_state === 'stopping'}>
                        <CircleStop className="h-4 w-4 text-destructive" />
                      </Button>
                    )}
                    {document.status === 'organized' && document.organize_state === 'completed' && (
                      <>
                        <Button variant="ghost" size="sm" title="按本人整理方向派发挖掘" onClick={() => void handleOpenMiningDirection(document)} disabled={!!busyId}>
                          <Play className="h-4 w-4" />
                        </Button>
                        <Button variant="ghost" size="sm" title="编辑整理草稿" onClick={() => void handleOpenOrganizedDraft(document)} disabled={!!busyId}>
                          <Pencil className="h-4 w-4" />
                        </Button>
                      </>
                    )}
                    <Button variant="ghost" size="sm" title="下载原文" onClick={() => void handleDownload(document)} disabled={!!busyId}>
                      <Download className="h-4 w-4" />
                    </Button>
                    <Button variant="ghost" size="sm" title="删除文档" onClick={() => void handleDelete(document)} disabled={!!busyId || document.status === 'parsing' || ['queued', 'running', 'stopping'].includes(document.organize_state || '')}>
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </td>
                </tr>
              ))}
              {!loading && documents.length === 0 && <tr><td colSpan={5} className="p-8 text-center text-xs text-muted-foreground">还没有文档，请上传 PDF 原文</td></tr>}
              {loading && <tr><td colSpan={5} className="p-8 text-center text-xs text-muted-foreground">正在读取本人文档…</td></tr>}
            </tbody>
          </table>
        </CardContent>
        <div className="flex items-center justify-between border-t border-border/40 p-4 text-xs text-muted-foreground">
          <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={offset === 0 || loading || !!busyId} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
            <Button variant="outline" size="sm" disabled={!hasMore || loading || !!busyId} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
          </div>
        </div>
      </Card>
      {markdownPreview && (
        <Card className="glass">
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">解析预览 · {markdownPreview.filename}</CardTitle>
            <Button variant="ghost" size="sm" onClick={() => setMarkdownPreview(null)}>关闭</Button>
          </CardHeader>
          <CardContent>
            {markdownPreview.truncated && <div className="mb-3 text-xs text-muted-foreground">预览最多显示 512 KiB；完整解析产物仍保存在本人文档空间。</div>}
            <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border/50 bg-muted/20 p-4 text-xs">{markdownPreview.content}</pre>
          </CardContent>
        </Card>
      )}
      {organizeTarget && (
        <Card className="glass">
          <CardHeader><CardTitle className="text-sm">整理文档 · {organizeTarget.filename}</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <label className="block text-xs">整理口径
              <select className="mt-1 w-full rounded-md border border-border bg-background p-2" value={organizeKind} onChange={(event) => setOrganizeKind(event.target.value as 'free' | 'paper')}>
                <option value="free">自由挖掘简报</option>
                <option value="paper">论文复现卡</option>
              </select>
            </label>
            <label className="block text-xs">补充要求（可选）
              <textarea className="mt-1 min-h-20 w-full rounded-md border border-border bg-background p-2" maxLength={2000} value={organizeExtra} onChange={(event) => setOrganizeExtra(event.target.value)} placeholder="例如：关注 A 股日频数据和可验证的价量信号" />
            </label>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setOrganizeTarget(null)} disabled={!!busyId}>取消</Button>
              <Button variant="primary" onClick={() => void handleSubmitOrganization()} disabled={!!busyId}>提交整理任务</Button>
            </div>
          </CardContent>
        </Card>
      )}
      {organizedDraft && (
        <Card className="glass">
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">整理草稿 · {organizedDraft.filename}</CardTitle>
            <Button variant="ghost" size="sm" onClick={() => setOrganizedDraft(null)}>关闭</Button>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-xs text-muted-foreground">草稿只属于当前账号。编辑并保存后，可作为下一阶段文档驱动挖掘的输入。</p>
            <textarea className="min-h-[50vh] w-full rounded-md border border-border bg-background p-4 font-mono text-xs" maxLength={256 * 1024} value={organizedDraft.text} onChange={(event) => setOrganizedDraft({ ...organizedDraft, text: event.target.value })} />
            <div className="flex justify-end">
              <Button variant="primary" onClick={() => void handleSaveOrganizedDraft()} disabled={!!busyId || !organizedDraft.text.trim()}><Save className="mr-2 h-4 w-4" />保存草稿</Button>
            </div>
          </CardContent>
        </Card>
      )}
      {miningDirection && (
        <Card className="glass">
          <CardHeader><CardTitle className="text-sm">从文档派发挖掘 · {miningDirection.filename}</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <p className="text-xs text-muted-foreground">任务会绑定本人文档来源。可先拆解成正交假设卡，再选择并调整要派发的方向；当前派发到 A 股 RD-Agent 队列。</p>
            <div className="rounded-md border border-border/60 p-3">
              <div className="text-xs font-medium">受控变异父本（最多 3 个）</div>
              <p className="mt-1 text-xs text-muted-foreground">只列出当前账号因子池中已完成的因子。选择后，拆解模型可为每张假设卡指定一个父本，派发时会冻结父本公式快照。</p>
              {seedCandidates.length === 0 ? (
                <div className="mt-2 text-xs text-muted-foreground">当前股票池没有可用父本；仍可按普通文档方向拆解和派发。</div>
              ) : (
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  {seedCandidates.map((entry) => {
                    const checked = selectedSeedIds.includes(entry.factor_id);
                    return (
                      <label key={entry.factor_id} className="flex items-start gap-2 rounded border border-border/50 p-2 text-xs">
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={!!busyId || (!checked && selectedSeedIds.length >= 3)}
                          onChange={(event) => {
                            const next = event.target.checked
                              ? [...selectedSeedIds, entry.factor_id]
                              : selectedSeedIds.filter((id) => id !== entry.factor_id);
                            setSelectedSeedIds(next);
                            setDecomposedDirections(null);
                            setDirectionCardDrafts([]);
                            setSelectedDirectionIndexes([]);
                            setDirectionSeedIds([]);
                            setDirectionBatchId(null);
                            setDirectionBatchReceipt(null);
                          }}
                        />
                        <span className="min-w-0"><span className="font-medium">{entry.factor_name || entry.factor_id}</span><span className="block truncate font-mono text-muted-foreground">{entry.factor_id}</span></span>
                      </label>
                    );
                  })}
                </div>
              )}
            </div>
            <textarea className="min-h-48 w-full rounded-md border border-border bg-background p-4 text-xs" maxLength={20000} value={miningDirection.text} onChange={(event) => setMiningDirection({ ...miningDirection, text: event.target.value })} />
            <div className="flex justify-end">
              <Button variant="outline" onClick={() => void handleDecomposeMiningDirection()} disabled={!!busyId || !miningDirection.text.trim()}>拆解为正交假设卡</Button>
            </div>
            {decomposedDirections && (
              <div className="space-y-2 rounded-md border border-border/60 p-3">
                <div className="text-xs font-medium">拆解结果 · {decomposedDirections.cards.length} 张卡片</div>
                {decomposedDirections.cards.map((card, index) => (
                  <div key={`${index}-${card.title}`} className="rounded border border-border/50 p-3">
                    <label className="flex items-center gap-2 text-xs text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={selectedDirectionIndexes.includes(index)}
                        disabled={!!busyId}
                        onChange={(event) => {
                          setSelectedDirectionIndexes(event.target.checked
                            ? [...new Set([...selectedDirectionIndexes, index])].sort((a, b) => a - b)
                            : selectedDirectionIndexes.filter((item) => item !== index));
                          setDirectionBatchId(null);
                          setDirectionBatchReceipt(null);
                        }}
                      />
                      纳入批量派发
                    </label>
                    <textarea
                      className="mt-2 min-h-28 w-full rounded-md border border-border bg-background p-3 text-xs"
                      maxLength={20000}
                      value={directionCardDrafts[index] || ''}
                      disabled={!!busyId}
                      onChange={(event) => {
                        setDirectionCardDrafts(directionCardDrafts.map((item, itemIndex) => itemIndex === index ? event.target.value : item));
                        setDirectionBatchId(null);
                        setDirectionBatchReceipt(null);
                      }}
                    />
                    <div className="mt-2 flex items-center justify-between gap-2">
                      <span className="text-xs text-muted-foreground">{card.categories.join('、')}</span>
                      <Button variant="outline" size="sm" onClick={() => applyDirectionCard(card, index)} disabled={!!busyId}>载入单方向</Button>
                    </div>
                    {selectedSeedIds.length > 0 && (
                      <label className="mt-2 block text-xs">变异父本
                        <select
                          className="mt-1 w-full rounded-md border border-border bg-background p-2"
                          value={directionSeedIds[index] || ''}
                          disabled={!!busyId}
                          onChange={(event) => {
                            setDirectionSeedIds(directionSeedIds.map((id, itemIndex) => itemIndex === index ? event.target.value || null : id));
                            setDirectionBatchId(null);
                            setDirectionBatchReceipt(null);
                          }}
                        >
                          <option value="">不关联父本</option>
                          {seedCandidates.filter((entry) => selectedSeedIds.includes(entry.factor_id)).map((entry) => (
                            <option key={entry.factor_id} value={entry.factor_id}>{entry.factor_name || entry.factor_id}</option>
                          ))}
                        </select>
                      </label>
                    )}
                  </div>
                ))}
                <div className="flex justify-end">
                  <Button
                    variant="primary"
                    onClick={() => void handleDispatchSelectedDirections()}
                    disabled={!!busyId || selectedDirectionIndexes.length === 0 || selectedDirectionIndexes.some((index) => !directionCardDrafts[index]?.trim())}
                  >
                    {directionBatchReceipt?.failed ? '重试/回读所选方向' : `批量派发所选方向（${selectedDirectionIndexes.length}）`}
                  </Button>
                </div>
                {directionBatchReceipt && (
                  <div className="space-y-1 rounded-md border border-border/50 p-3 text-xs">
                    <div>批次 {directionBatchReceipt.batch_id} · 启动 {directionBatchReceipt.started} · 排队 {directionBatchReceipt.queued} · 失败 {directionBatchReceipt.failed}</div>
                    {directionBatchReceipt.items.map((item) => (
                      <div key={item.index} className="flex flex-wrap justify-between gap-2">
                        <span>方向 {item.index + 1}：{item.status}{item.error ? ` · ${item.error}` : ''}</span>
                        {item.task_id && <span>任务 {item.task_id}</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setMiningDirection(null)} disabled={!!busyId}>取消</Button>
              <Button variant="primary" onClick={() => void handleDispatchDocumentMining()} disabled={!!busyId || !miningDirection.text.trim()}>派发挖掘任务</Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
};

export default DocumentCenterPage;
