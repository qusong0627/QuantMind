/**
 * 挖掘历史 · 文档解析 Tab（T-FM-11）：文档链的回看与管理面。
 *
 * 数据源是 PG `rd_agent_docs`（GET /alpha-agent/docs），与挖掘任务表同库不同表：
 * - 状态只按后端字面量渲染，认不出的状态原样显示（不塞进近似的桶里）；
 * - 「看文本」拉解析产物 full.md（白名单内联预览），原文照实展示；
 * - 「继续挖掘」把整行交给 AppRoot（跳首页文档链），本组件不自己发任务；
 * - 删除二次确认：连解析文件一并清除（后端 cancel → rmtree → 软删）。
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  FileText, AlertCircle, Inbox, Loader2,
  Trash2, Play, Eye, EyeOff,
} from 'lucide-react';
import { formatShortTime } from '../utils-v2';
import {
  DOC_STATUS_LABELS,
  deleteDoc,
  extractDetail,
  getDocFileText,
  getDocQuota,
  listDocs,
  type DocQuotaStatus,
  type DocRow,
} from '../services-v2/docMiningApi';

export const DOCS_PAGE_SIZE = 20;

export interface DocsHistoryTabProps {
  /** 「继续挖掘」：AppRoot 负责跳首页并把文档带回文档链 */
  onResume?: (row: DocRow) => void;
  /** 外部「刷新」信号（HistoryPage 头部按钮）：代次递增即重新拉取 */
  refreshSeq?: number;
}

const STATUS_META: Record<string, { cls: string; busy?: boolean }> = {
  uploaded: { cls: 'bg-slate-100 text-slate-500 border-slate-200', busy: true },
  parsing: { cls: 'bg-indigo-50 text-indigo-600 border-indigo-200', busy: true },
  parsed: { cls: 'bg-blue-50 text-blue-600 border-blue-200' },
  organized: { cls: 'bg-emerald-50 text-emerald-600 border-emerald-200' },
  parse_failed: { cls: 'bg-rose-50 text-rose-600 border-rose-200' },
  expired: { cls: 'bg-amber-50 text-amber-600 border-amber-200' },
};

function statusMeta(status: string) {
  return (
    STATUS_META[status] ?? {
      cls: 'bg-slate-100 text-slate-500 border-slate-200',
    }
  );
}

function canDig(status: string): boolean {
  return status === 'parsed' || status === 'organized';
}

export const DocsHistoryTab: React.FC<DocsHistoryTabProps> = ({ onResume, refreshSeq }) => {
  const [rows, setRows] = useState<DocRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [quota, setQuota] = useState<DocQuotaStatus | null>(null);
  // 「看文本」：同时只展开一行；文本按 doc_id 缓存，不重复拉
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState<Record<string, string>>({});
  const [previewLoading, setPreviewLoading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // 迟到的响应不许倒灌（快速翻页时旧请求可能后到）
  const reqSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++reqSeqRef.current;
    setLoading(true);
    setError(null);
    try {
      const page = await listDocs({ limit: DOCS_PAGE_SIZE, offset });
      if (seq !== reqSeqRef.current) return;
      setRows(page.items);
      setTotal(page.total);
    } catch (e: unknown) {
      if (seq !== reqSeqRef.current) return;
      setError(`加载失败：${extractDetail(e)}`);
      setRows([]);
      setTotal(0);
    } finally {
      if (seq === reqSeqRef.current) setLoading(false);
    }
  }, [offset]);

  useEffect(() => {
    void load();
  }, [load]);

  // 外部刷新信号：跳过首帧（初次挂载上面那条 effect 已拉），之后代次一变就重拉
  const refreshSeqRef = useRef(refreshSeq ?? 0);
  useEffect(() => {
    if (refreshSeq === undefined || refreshSeq === refreshSeqRef.current) return;
    refreshSeqRef.current = refreshSeq;
    void load();
  }, [refreshSeq, load]);

  useEffect(() => {
    getDocQuota()
      .then(setQuota)
      .catch(() => setQuota(null)); // 余量是辅助信息：拿不到就不显示，不拦列表
  }, []);

  const togglePreview = useCallback(
    async (row: DocRow) => {
      if (previewId === row.doc_id) {
        setPreviewId(null);
        return;
      }
      setPreviewId(row.doc_id);
      if (previewText[row.doc_id] !== undefined) return;
      setPreviewLoading(true);
      try {
        const text = await getDocFileText(row.doc_id);
        setPreviewText((prev) => ({ ...prev, [row.doc_id]: text }));
      } catch (e: unknown) {
        setPreviewText((prev) => ({
          ...prev,
          [row.doc_id]: `（原文读取失败：${extractDetail(e)}）`,
        }));
      } finally {
        setPreviewLoading(false);
      }
    },
    [previewId, previewText],
  );

  const handleDelete = useCallback(
    async (row: DocRow) => {
      const ok = window.confirm(
        `删除「${row.filename}」？\n解析文件将一并清除，此操作不可恢复。`,
      );
      if (!ok) return;
      setDeletingId(row.doc_id);
      setError(null);
      try {
        await deleteDoc(row.doc_id);
        setPreviewId((cur) => (cur === row.doc_id ? null : cur));
        await load();
      } catch (e: unknown) {
        setError(`删除失败：${extractDetail(e)}`);
      } finally {
        setDeletingId(null);
      }
    },
    [load],
  );

  const hasPrev = offset > 0;
  const hasNext = offset + DOCS_PAGE_SIZE < total;
  const rangeText =
    total === 0
      ? '共 0 份'
      : `共 ${total} 份 · 当前 ${offset + 1}-${Math.min(offset + DOCS_PAGE_SIZE, total)}`;

  return (
    <div className="flex flex-col gap-3">
      {/* 配额条 */}
      {quota && (
        <div
          className={`flex items-center gap-3 rounded-xl border px-4 py-2 text-[11px] font-bold ${
            quota.warning || !quota.token_configured
              ? 'border-amber-200 bg-amber-50/70 text-amber-700'
              : 'border-slate-100 bg-slate-50/70 text-slate-500'
          }`}
        >
          {!quota.token_configured ? (
            <span>
              解析服务未配置（MinerU Token 缺失）：可在个人中心「其他设置 → AI 服务配置」填写自己的 Token，或联系管理员配置服务器
            </span>
          ) : (
            <>
              <span>今日已用 {quota.user_used}/{quota.user_limit} 页</span>
              <span className="text-slate-300">|</span>
              <span>平台剩余 {quota.platform_remaining} 页</span>
              {quota.exhausted && <span>· 今日额度已用尽</span>}
            </>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50/80 px-4 py-2.5 text-xs font-bold text-rose-600">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span className="flex-1 min-w-0 truncate" title={error}>
            {error}
          </span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 rounded-full border border-rose-200 bg-white px-3 py-1 text-[11px] font-bold text-rose-600 hover:bg-rose-50 cursor-pointer"
          >
            重试
          </button>
        </div>
      )}

      <div className="rounded-2xl border border-white/90 bg-white/80 backdrop-blur-xl shadow-xs overflow-hidden">
        {rows.length === 0 && !loading && !error ? (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <Inbox className="h-8 w-8 text-slate-300" />
            <p className="m-0 text-sm font-bold text-slate-500">还没有上传过文档</p>
            <p className="m-0 text-xs text-slate-400">
              在「因子挖掘」首页切换到「上传文档」，解析完成后可继续挖掘或在此回看
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-slate-100 text-[10px] font-black uppercase tracking-wider text-slate-400">
                  <th className="px-4 py-2.5">文件名</th>
                  <th className="px-3 py-2.5">状态</th>
                  <th className="px-3 py-2.5 text-right">页数</th>
                  <th className="px-3 py-2.5">上传时间</th>
                  <th className="px-4 py-2.5 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const meta = statusMeta(row.status);
                  const expanded = previewId === row.doc_id;
                  return (
                    <React.Fragment key={row.doc_id}>
                      <tr className="border-b border-slate-50 last:border-b-0 hover:bg-slate-50/60 transition-colors">
                        <td className="px-4 py-2.5 max-w-[20rem]">
                          <span className="inline-flex items-center gap-1.5 min-w-0">
                            <FileText className="h-3.5 w-3.5 text-slate-400 shrink-0" />
                            <span
                              className="truncate font-bold text-slate-700"
                              title={`${row.filename}${row.task_id ? ` · 已关联挖掘任务 ${row.task_id}` : ''}`}
                            >
                              {row.filename}
                            </span>
                            {row.task_id && (
                              <span className="shrink-0 rounded bg-blue-50 px-1.5 py-0.5 text-[9px] font-bold text-blue-600">
                                已挖掘
                              </span>
                            )}
                          </span>
                          {row.error && (
                            <div
                              className="mt-0.5 truncate text-[10px] text-rose-500"
                              title={row.error}
                            >
                              {row.error}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          <span
                            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold ${meta.cls}`}
                          >
                            {meta.busy && (
                              <Loader2 className="h-2.5 w-2.5 animate-spin" />
                            )}
                            {DOC_STATUS_LABELS[row.status] ?? row.status}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 text-right font-mono font-bold text-slate-700">
                          {row.page_count ?? '—'}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          <span className="font-mono text-[11px] text-slate-500">
                            {formatShortTime(row.created_at)}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              type="button"
                              onClick={() => void togglePreview(row)}
                              disabled={!canDig(row.status)}
                              title={canDig(row.status) ? '查看解析出的文本' : '解析完成后可看文本'}
                              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-bold text-slate-600 hover:border-blue-300 hover:text-blue-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
                            >
                              {expanded ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                              {expanded ? '收起' : '看文本'}
                            </button>
                            <button
                              type="button"
                              onClick={() => onResume?.(row)}
                              disabled={!canDig(row.status) || !onResume}
                              title={canDig(row.status) ? '带着这份文档回到首页继续挖掘' : '解析完成后可继续挖掘'}
                              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-bold text-slate-600 hover:border-indigo-300 hover:text-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
                            >
                              <Play className="h-3 w-3" />
                              继续挖掘
                            </button>
                            <button
                              type="button"
                              onClick={() => void handleDelete(row)}
                              disabled={deletingId === row.doc_id}
                              title="删除文档（解析文件一并清除）"
                              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-bold text-slate-600 hover:border-rose-300 hover:text-rose-600 disabled:opacity-40 cursor-pointer"
                            >
                              {deletingId === row.doc_id ? (
                                <Loader2 className="h-3 w-3 animate-spin" />
                              ) : (
                                <Trash2 className="h-3 w-3" />
                              )}
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                      {expanded && (
                        <tr className="border-b border-slate-50 last:border-b-0 bg-slate-50/50">
                          <td colSpan={5} className="px-4 py-3">
                            {previewLoading && previewText[row.doc_id] === undefined ? (
                              <span className="inline-flex items-center gap-1.5 text-[11px] font-bold text-slate-500">
                                <Loader2 className="h-3 w-3 animate-spin" />
                                正在读取全文…
                              </span>
                            ) : (
                              <pre className="max-h-72 overflow-auto rounded-xl bg-white border border-slate-100 p-3 text-[11px] leading-relaxed text-slate-600 whitespace-pre-wrap break-all m-0">
                                {previewText[row.doc_id] ?? ''}
                              </pre>
                            )}
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="flex items-center justify-between border-t border-slate-100 px-4 py-2.5">
          <span className="text-[11px] font-bold text-slate-400">{rangeText}</span>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => setOffset((o) => Math.max(0, o - DOCS_PAGE_SIZE))}
              disabled={!hasPrev || loading}
              className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-600 hover:border-blue-300 hover:text-blue-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
            >
              上一页
            </button>
            <button
              type="button"
              onClick={() => setOffset((o) => o + DOCS_PAGE_SIZE)}
              disabled={!hasNext || loading}
              className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-600 hover:border-blue-300 hover:text-blue-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
            >
              下一页
            </button>
          </div>
        </div>
      </div>

      <span className="text-[10px] text-slate-400">
        文档由 MinerU 云端解析；删除会同时清除服务器上的解析文件
      </span>
    </div>
  );
};

export default DocsHistoryTab;
