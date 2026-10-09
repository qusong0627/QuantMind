/**
 * 挖掘历史 —— 「每次挖了什么」的权威回看页（机构级 P0 / T-FM-04）。
 *
 * 数据源是 PG `rd_agent_mining_tasks`（GET /alpha-agent/tasks/history），
 * 不是 launcher 的内存列表：引擎重启、容器重建都不会让这张表失忆，
 * 所以 legacy 迁移行（source=legacy，方向为空）也能和今天的任务并排出现。
 *
 * 三条展示纪律：
 * 1. **空值诚实**：legacy 行方向为空就是「—」，不回落默认文案——那会让用户
 *    以为当时挖的是默认方向；失败行必须把后端 error 原文带出来。
 * 2. **total 是真的 COUNT**（后端 count_history），分页文案按它渲染。
 * 3. **操作只做跳转语义**：查看结果/重跑把整行原样交给 AppRoot，
 *    本组件不自己拼装新任务、不直接换页。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { History, RefreshCw, FileText, RotateCcw, ExternalLink, AlertCircle, Inbox, FileSearch } from 'lucide-react';
import { PageHeader } from '../components-v2/layout/PageHeader';
import { DocsHistoryTab } from '../components-v2/DocsHistoryTab';
import { isDocMiningEnabled } from '../../../config/docMiningFlags';
import { formatShortTime } from '../utils-v2';
import type { DocRow } from '../services-v2/docMiningApi';
import {
  getMiningHistory,
  MINING_HISTORY_PAGE_SIZE,
  type MiningHistoryRow,
} from '../services-v2/api';

export interface HistoryPageProps {
  /** 「查看结果」：AppRoot 负责跳因子库并按 task_id 过滤 */
  onViewResults?: (row: MiningHistoryRow) => void;
  /** 「重跑」：AppRoot 负责跳首页并把整行回填进 ChatInput */
  onRetry?: (row: MiningHistoryRow) => void;
  /** 「文档解析 → 继续挖掘」：AppRoot 负责跳首页并把文档带回文档链 */
  onResumeDoc?: (row: DocRow) => void;
}

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  hong_kong: '港股',
  us_stock: '美股',
  crypto: '加密货币',
  futures: '期货',
};

const STATUS_META: Record<string, { label: string; cls: string }> = {
  pending: { label: '排队中', cls: 'bg-slate-100 text-slate-500 border-slate-200' },
  running: { label: '运行中', cls: 'bg-indigo-50 text-indigo-600 border-indigo-200' },
  completed: { label: '已完成', cls: 'bg-emerald-50 text-emerald-600 border-emerald-200' },
  failed: { label: '失败', cls: 'bg-rose-50 text-rose-600 border-rose-200' },
  cancelled: { label: '已取消', cls: 'bg-amber-50 text-amber-600 border-amber-200' },
};

/** 认不出的状态原样显示，不倒进某个桶里假装认识。 */
function statusMeta(status: string): { label: string; cls: string } {
  return (
    STATUS_META[status] ?? {
      label: status || '未知',
      cls: 'bg-slate-100 text-slate-500 border-slate-200',
    }
  );
}

function sourceLabel(row: MiningHistoryRow): string {
  if (row.source === 'doc') {
    return row.doc_id ? `文档 ${row.doc_id.slice(0, 8)}` : '文档';
  }
  if (row.source === 'legacy') return '历史迁移';
  return '文字';
}

function marketLabel(row: MiningHistoryRow): string {
  const market = MARKET_LABELS[row.market] ?? row.market;
  return row.universe ? `${market} · ${row.universe}` : market;
}

const STATUS_FILTERS: Array<{ value: string; label: string }> = [
  { value: 'all', label: '全部状态' },
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

export const HistoryPage: React.FC<HistoryPageProps> = ({
  onViewResults,
  onRetry,
  onResumeDoc,
}) => {
  const [rows, setRows] = useState<MiningHistoryRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState('all');
  const [marketFilter, setMarketFilter] = useState('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 文档解析 Tab（构建期开关关时整排 Tab 不渲染，挖掘任务 Tab 不受影响）
  const docsEnabled = isDocMiningEnabled();
  const [activeTab, setActiveTab] = useState<'tasks' | 'docs'>('tasks');
  const [docsRefreshSeq, setDocsRefreshSeq] = useState(0);
  const tabsShown = docsEnabled;
  // 当前展示的是文档 Tab（开关关时恒为任务 Tab）
  const onDocsTab = docsEnabled && activeTab === 'docs';

  // 迟到的响应不许倒灌（快速改过滤条件时旧请求可能后到）
  const reqSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++reqSeqRef.current;
    setLoading(true);
    setError(null);
    try {
      const r = await getMiningHistory({
        market: marketFilter !== 'all' ? marketFilter : undefined,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        limit: MINING_HISTORY_PAGE_SIZE,
        offset,
      });
      if (seq !== reqSeqRef.current) return;
      if (!r.success || !r.data) throw new Error(r.error || '加载失败');
      setRows(r.data.tasks);
      setTotal(r.data.total);
    } catch (e: unknown) {
      if (seq !== reqSeqRef.current) return;
      const msg = e instanceof Error ? e.message : String(e);
      setError(`加载失败：${msg}`);
      setRows([]);
      setTotal(0);
    } finally {
      if (seq === reqSeqRef.current) setLoading(false);
    }
  }, [marketFilter, statusFilter, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  const hasPrev = offset > 0;
  const hasNext = offset + MINING_HISTORY_PAGE_SIZE < total;
  const rangeText = useMemo(() => {
    if (total === 0) return '共 0 条';
    const start = offset + 1;
    const end = Math.min(offset + MINING_HISTORY_PAGE_SIZE, total);
    return `共 ${total} 条 · 当前 ${start}-${end}`;
  }, [total, offset]);

  return (
    <div className="flex flex-col gap-4 py-6 select-none animate-fade-in-up">
      <PageHeader
        icon={History}
        title="挖掘历史"
        subtitle="每次挖了什么、挖出多少因子 —— 进程重启也不会失忆"
        actions={
          <>
            {!onDocsTab && (
              <select
                aria-label="市场筛选"
                value={marketFilter}
                onChange={(e) => {
                  setMarketFilter(e.target.value);
                  setOffset(0);
                }}
                className="rounded-full bg-white px-3 py-1.5 text-xs font-bold text-slate-600 border border-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-200 cursor-pointer"
              >
                <option value="all">全部市场</option>
                {Object.entries(MARKET_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            )}
            {!onDocsTab && (
              <select
                aria-label="状态筛选"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value);
                  setOffset(0);
                }}
                className="rounded-full bg-white px-3 py-1.5 text-xs font-bold text-slate-600 border border-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-200 cursor-pointer"
              >
                {STATUS_FILTERS.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            )}
            <button
              type="button"
              onClick={() => (onDocsTab ? setDocsRefreshSeq((s) => s + 1) : void load())}
              disabled={!onDocsTab && loading}
              className="flex items-center gap-1.5 rounded-full bg-white px-3 py-1.5 text-xs font-bold text-slate-600 border border-slate-200 hover:border-blue-300 hover:text-blue-600 disabled:opacity-50 cursor-pointer"
              title="刷新"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${!onDocsTab && loading ? 'animate-spin' : ''}`} />
              刷新
            </button>
          </>
        }
      />

      {/* Tab 切换（文档链构建期开关关闭时整排不渲染） */}
      {tabsShown && (
        <div
          role="tablist"
          aria-label="挖掘历史分类"
          className="flex w-fit items-center gap-1 rounded-full border border-slate-200/80 bg-white/80 p-1 shadow-2xs"
        >
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'tasks'}
            onClick={() => setActiveTab('tasks')}
            className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-black transition-colors cursor-pointer ${
              activeTab === 'tasks'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-slate-500 hover:text-blue-600'
            }`}
          >
            <History className="h-3.5 w-3.5" />
            挖掘任务
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'docs'}
            onClick={() => setActiveTab('docs')}
            className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-black transition-colors cursor-pointer ${
              activeTab === 'docs'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-slate-500 hover:text-blue-600'
            }`}
          >
            <FileSearch className="h-3.5 w-3.5" />
            文档解析
          </button>
        </div>
      )}

      {onDocsTab && (
        <DocsHistoryTab onResume={onResumeDoc} refreshSeq={docsRefreshSeq} />
      )}

      {!onDocsTab && error && (
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

      {!onDocsTab && (
        <div className="rounded-2xl border border-white/90 bg-white/80 backdrop-blur-xl shadow-xs overflow-hidden">
        {rows.length === 0 && !loading && !error ? (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <Inbox className="h-8 w-8 text-slate-300" />
            <p className="m-0 text-sm font-bold text-slate-500">还没有挖掘记录</p>
            <p className="m-0 text-xs text-slate-400">
              在「因子挖掘」页提交一次任务后，这里会按时间倒序保留每次挖了什么
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-slate-100 text-[10px] font-black uppercase tracking-wider text-slate-400">
                  <th className="px-4 py-2.5">时间</th>
                  <th className="px-3 py-2.5">来源</th>
                  <th className="px-3 py-2.5">市场 · 池</th>
                  <th className="px-3 py-2.5">方向</th>
                  <th className="px-3 py-2.5">状态</th>
                  <th className="px-3 py-2.5 text-right">因子数</th>
                  <th className="px-4 py-2.5 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const meta = statusMeta(row.status);
                  const canView = row.factor_count > 0;
                  return (
                    <tr
                      key={row.task_id}
                      className="border-b border-slate-50 last:border-b-0 hover:bg-slate-50/60 transition-colors"
                    >
                      <td className="px-4 py-2.5 whitespace-nowrap">
                        <span
                          className="font-mono text-[11px] text-slate-500"
                          title={`任务 ${row.task_id}${
                            row.completed_at ? ` · 完成于 ${row.completed_at}` : ''
                          }`}
                        >
                          {formatShortTime(row.created_at)}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap text-slate-500">
                        <span className="inline-flex items-center gap-1">
                          <FileText className="h-3 w-3 text-slate-400" />
                          {sourceLabel(row)}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap text-slate-500">
                        {marketLabel(row)}
                      </td>
                      <td className="px-3 py-2.5 max-w-[22rem]">
                        <div className="truncate font-bold text-slate-700" title={row.direction}>
                          {row.direction || '—'}
                        </div>
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
                          className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-bold ${meta.cls}`}
                        >
                          {meta.label}
                          {(row.status === 'running' || row.status === 'pending') &&
                            ` ${row.progress_pct}%`}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono font-bold text-slate-700">
                        {row.factor_count}
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => onViewResults?.(row)}
                            disabled={!canView || !onViewResults}
                            title={canView ? '在因子库中查看该任务的产出' : '该任务没有已落库因子'}
                            className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-bold text-slate-600 hover:border-blue-300 hover:text-blue-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
                          >
                            <ExternalLink className="h-3 w-3" />
                            查看结果
                          </button>
                          <button
                            type="button"
                            onClick={() => onRetry?.(row)}
                            disabled={!onRetry}
                            title="回到首页，把当时的方向/市场/数据源回填后再跑"
                            className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-bold text-slate-600 hover:border-indigo-300 hover:text-indigo-600 disabled:opacity-40 cursor-pointer"
                          >
                            <RotateCcw className="h-3 w-3" />
                            重跑
                          </button>
                        </div>
                      </td>
                    </tr>
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
              onClick={() => setOffset((o) => Math.max(0, o - MINING_HISTORY_PAGE_SIZE))}
              disabled={!hasPrev || loading}
              className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-600 hover:border-blue-300 hover:text-blue-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
            >
              上一页
            </button>
            <button
              type="button"
              onClick={() => setOffset((o) => o + MINING_HISTORY_PAGE_SIZE)}
              disabled={!hasNext || loading}
              className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-600 hover:border-blue-300 hover:text-blue-600 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
            >
              下一页
            </button>
          </div>
        </div>
        </div>
      )}
    </div>
  );
};

export default HistoryPage;
