import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Download, Loader2, Trash2, UploadCloud } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import {
  deletePersonalPaper,
  downloadPersonalPaper,
  listPersonalPapers,
  uploadPersonalPaper,
  type PersonalPaper,
} from '../services-v2/papersApi';
import { Button } from './ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';

const PAGE_SIZE = 50;

function formatBytes(value: number): string {
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(2)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${value} B`;
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export const MyPapersPanel: React.FC = () => {
  const [papers, setPapers] = useState<PersonalPaper[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [uploading, setUploading] = useState(false);
  const [busyId, setBusyId] = useState('');
  const action = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const load = useCallback(() => {
    const controller = new AbortController();
    action.current?.abort();
    action.current = controller;
    const generation = getSessionGeneration();
    setLoading(true);
    setError('');
    listPersonalPapers({ limit: PAGE_SIZE, offset: 0 }, controller.signal)
      .then((rows) => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) {
          setPapers(rows);
        }
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) {
          setError(failure instanceof Error ? failure.message : '论文列表读取失败');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && generation === getSessionGeneration()) {
          setLoading(false);
        }
      });
  }, []);

  useEffect(() => {
    load();
    return () => action.current?.abort();
  }, [load]);

  useEffect(() => registerSessionReset(() => {
    action.current?.abort();
    setPapers([]);
    setError('');
  }), []);

  const upload = async (file: File) => {
    const controller = new AbortController();
    action.current?.abort();
    action.current = controller;
    const generation = getSessionGeneration();
    setUploading(true);
    setError('');
    try {
      await uploadPersonalPaper(file, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        load();
      }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setError(failure instanceof Error ? failure.message : '论文上传失败');
      }
    } finally {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setUploading(false);
      }
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  const remove = async (paper: PersonalPaper) => {
    if (!window.confirm(`删除论文 ${paper.filename}？\n文件与记录都会被删除，不可恢复。`)) return;
    const controller = new AbortController();
    action.current?.abort();
    action.current = controller;
    const generation = getSessionGeneration();
    setBusyId(paper.paper_id);
    setError('');
    try {
      await deletePersonalPaper(paper.paper_id, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setPapers((rows) => rows.filter((row) => row.paper_id !== paper.paper_id));
      }
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setError(failure instanceof Error ? failure.message : '论文删除失败');
      }
    } finally {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setBusyId('');
      }
    }
  };

  const download = async (paper: PersonalPaper) => {
    const controller = new AbortController();
    setBusyId(paper.paper_id);
    setError('');
    try {
      const blob = await downloadPersonalPaper(paper.paper_id, controller.signal);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = paper.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (failure: unknown) {
      setError(failure instanceof Error ? failure.message : '论文下载失败');
    } finally {
      setBusyId('');
    }
  };

  return (
    <Card className="glass">
      <CardHeader className="pb-2 flex flex-row items-center justify-between">
        <CardTitle className="text-sm">我的论文 · 本人上传持久保存</CardTitle>
        <Button
          variant="outline"
          size="sm"
          disabled={uploading}
          onClick={() => fileInput.current?.click()}
        >
          {uploading ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <UploadCloud className="mr-1 h-3.5 w-3.5" />}
          上传论文
        </Button>
        <input
          ref={fileInput}
          type="file"
          accept=".pdf,application/pdf"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
          }}
        />
      </CardHeader>
      <CardContent>
        {error && <p className="mb-2 text-xs text-destructive">{error}</p>}
        {loading ? (
          <p className="py-4 text-center text-xs text-muted-foreground">正在读取本人论文…</p>
        ) : papers.length === 0 ? (
          <p className="py-4 text-center text-xs text-muted-foreground">还没有上传论文，仅支持 PDF（单文件最大 20 MiB）。</p>
        ) : (
          <ul className="space-y-1.5">
            {papers.map((paper) => (
              <li key={paper.paper_id} className="flex items-center justify-between gap-3 rounded-lg border border-border/60 p-2.5">
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium" title={paper.filename}>{paper.filename}</div>
                  <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                    {formatBytes(paper.size_bytes)} · {formatDateTime(paper.created_at)}
                  </div>
                </div>
                <div className="flex shrink-0 gap-1">
                  <Button variant="ghost" size="sm" className="h-7 w-7 p-0" title="下载" disabled={!!busyId} onClick={() => void download(paper)}>
                    <Download className="h-3.5 w-3.5" />
                  </Button>
                  <Button variant="ghost" size="sm" className="h-7 w-7 p-0" title="删除论文（文件与记录，不可恢复）" disabled={!!busyId} onClick={() => void remove(paper)}>
                    {busyId === paper.paper_id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5 text-destructive" />}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
};
