import React, { useCallback, useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { Modal } from 'antd';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { useAppSelector } from '../../../store';
import { listPersonalPapers, migratePersonalPaper, downloadPersonalPaper, deletePersonalPaper, type PersonalPaper } from '../services-v2/papersApi';
import { Button } from './ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';

const PAGE_SIZE = 50;

export const MyPapersPanel: React.FC<{ onImported?: () => void }> = ({ onImported }) => {
  const [papers, setPapers] = useState<PersonalPaper[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [sessionEpoch, setSessionEpoch] = useState(() => getSessionGeneration());
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<PersonalPaper | null>(null);
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(() => {
    controller.current?.abort();
    if (!isAuthenticated || isAuthenticating) { setPapers([]); setLoading(false); return; }
    const current = new AbortController();
    controller.current = current;
    const generation = getSessionGeneration();
    setLoading(true);
    listPersonalPapers({ limit: PAGE_SIZE + 1, offset }, current.signal).then((rows) => {
      if (!current.signal.aborted && generation === getSessionGeneration()) {
        setPapers(rows.slice(0, PAGE_SIZE)); setHasMore(rows.length > PAGE_SIZE);
      }
    }).catch((cause: unknown) => {
      if (!current.signal.aborted && generation === getSessionGeneration()) setError(cause instanceof Error ? cause.message : '旧论文读取失败');
    }).finally(() => {
      if (!current.signal.aborted && generation === getSessionGeneration()) setLoading(false);
    });
  }, [offset, isAuthenticated, isAuthenticating]);
  useEffect(() => { load(); return () => controller.current?.abort(); }, [load, sessionEpoch]);
  useEffect(() => registerSessionReset(() => {
    controller.current?.abort(); setPapers([]); setOffset(0); setHasMore(false); setBusy(''); setError(''); setMessage(''); setDeleteTarget(null); setLoading(false); setSessionEpoch(getSessionGeneration());
  }), []);

  const migrate = async (paper: PersonalPaper) => {
    const current = new AbortController();
    controller.current?.abort(); controller.current = current;
    const generation = getSessionGeneration();
    setBusy(paper.paper_id); setError(''); setMessage('');
    try {
      await migratePersonalPaper(paper.paper_id, current.signal);
      if (current.signal.aborted || generation !== getSessionGeneration()) return;
      setPapers((rows) => rows.filter((row) => row.paper_id !== paper.paper_id));
      setMessage('论文已迁移到下方文档列表；开源版暂不支持论文解析。');
      setBusy('');
      onImported?.();
      load();
    } catch (cause) {
      if (!current.signal.aborted && generation === getSessionGeneration()) {
        const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setError(typeof detail === 'string' ? detail : cause instanceof Error ? cause.message : '迁移失败');
      }
    } finally {
      if (!current.signal.aborted && generation === getSessionGeneration()) setBusy('');
    }
  };
  const download = async (paper: PersonalPaper) => {
    const current = new AbortController();
    controller.current?.abort(); controller.current = current;
    const generation = getSessionGeneration();
    setBusy(paper.paper_id); setError('');
    try {
      const blob = await downloadPersonalPaper(paper.paper_id, current.signal);
      if (current.signal.aborted || generation !== getSessionGeneration()) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = paper.filename;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (cause) {
      if (!current.signal.aborted && generation === getSessionGeneration()) setError(cause instanceof Error ? cause.message : '下载失败');
    } finally {
      if (!current.signal.aborted && generation === getSessionGeneration()) setBusy('');
    }
  };
  const remove = async (paper: PersonalPaper) => {
    setDeleteTarget(null);
    const current = new AbortController();
    controller.current?.abort(); controller.current = current;
    const generation = getSessionGeneration();
    setBusy(paper.paper_id); setError('');
    try {
      await deletePersonalPaper(paper.paper_id, current.signal);
      if (current.signal.aborted || generation !== getSessionGeneration()) return;
      setBusy(''); load();
    } catch (cause) {
      if (!current.signal.aborted && generation === getSessionGeneration()) setError(cause instanceof Error ? cause.message : '删除失败');
    } finally {
      if (!current.signal.aborted && generation === getSessionGeneration()) setBusy('');
    }
  };
  if (!loading && !papers.length && offset === 0 && !error && !message) return null;
  return <Card className="glass">
    <CardHeader><CardTitle className="text-sm">待迁移的旧论文</CardTitle></CardHeader>
    <CardContent>
      <p className="mb-2 text-xs text-muted-foreground">迁移后可在统一列表中管理，原文件不会复制。开源版暂不支持论文解析。</p>
      {error && <p className="mb-2 text-xs text-destructive">{error}</p>}
      {message && <p role="status" className="mb-2 text-xs text-muted-foreground">{message}</p>}
      {loading ? <p>正在读取旧论文…</p> : <ul className="space-y-1">
        {papers.map((paper) => <li key={paper.paper_id} className="flex items-center justify-between gap-2 text-xs">
          <span className="truncate">{paper.filename}</span><span className="flex shrink-0 gap-1">
            <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => void migrate(paper)}>迁移到文档</Button>
            <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => void download(paper)}>下载</Button>
            <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => setDeleteTarget(paper)}>删除</Button>
          </span>
        </li>)}
      </ul>}
      <div className="mt-2 flex justify-end gap-2">
        <Button variant="outline" size="sm" disabled={offset === 0 || !!busy || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
        <Button variant="outline" size="sm" disabled={!hasMore || !!busy || loading} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
      </div>
    </CardContent>
    <Modal centered title="删除旧论文" open={!!deleteTarget} okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onCancel={() => setDeleteTarget(null)} onOk={() => { if (deleteTarget) void remove(deleteTarget); }}>
      <p>确定删除“{deleteTarget?.filename}”吗？此操作不可恢复。</p>
    </Modal>
  </Card>;
};
