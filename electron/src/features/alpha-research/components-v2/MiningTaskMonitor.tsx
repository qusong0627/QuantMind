/**
 * 挖掘任务监视器 —— 挂在 App 壳上，**刷新、切页、走进任何一个栏目都不会丢**。
 *
 * 为什么必须有这个东西（2026-10-07）：挖掘进度此前只活在 `TaskContext` 的内存里，
 * 而它的「恢复」逻辑只认 `status === 'running'` 一条、且 `.catch(() => {})` 把失败
 * 静默吞掉。于是刷新页面 = 进度消失，用户只能盯着一个空页面猜后端还在不在跑
 * （当天真的有一次任务因为部署重启被杀，界面上一点痕迹都没有）。
 *
 * 这里的做法是**把真相留在后端**：任务状态本来就由 `AlphaAgentLauncher` 持久化，
 * `GET /alpha-agent/tasks` 是现成的接口，前端只要在壳上轮询它，刷新自然就是重放
 * 同一条查询。所以这个组件的状态全部可以丢——丢了下一轮 5 秒就回来了。
 *
 * 三条刻意的取舍：
 * 1. **轮询不依赖任何 props**（`refresh` 空依赖）。`SnapshotPanel` 那边刚踩过
 *    「effect 依赖一个每次渲染都换新的回调 → 5 秒轮询退化成请求风暴」的坑，
 *    这边从源头避免：壳上的常驻组件，一个失控的循环会一直打引擎。
 * 2. **认不出的状态不归类**。`normalizeTaskStatus` 把未知值折成 `idle`，
 *    面板里不能把它显示成「完成」——宁可让它落在一个「其他」桶里。
 * 3. **失败一定带原因**。`error_message` 是后端给的原文（例如
 *    "Server restarted while task was running"），面板只做截断不做改写。
 * 4. **未登录不轮询**（`enabled`，2026-10-09）：壳在公开路由（登录页）也会挂载，
 *    无 token 的 `GET /tasks` 每 5 秒打一发 401 + 控制台报错。由壳传入登录态；
 *    `enabled` 翻 true 的那一挂立刻拉一次，不等下一个节拍。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, CircleDashed, Loader2, X } from 'lucide-react';
import { cancelMining, listTasks } from '../services-v2/api';
import type { Task } from '../types-v2';

/** 后端 `_update_progress` 每 3 秒刷一次进度，这里 5 秒足够；也够温和。 */
const POLL_MS = 5000;
/** 后端 `list_tasks` 是**无上限**的（内存里所有历史任务），面板一次只列最新的这些。 */
const MAX_ROWS = 20;
/** 失败原因在行内只留这么长，全文放进 title。 */
const REASON_MAX = 72;

type Bucket = 'running' | 'completed' | 'failed' | 'other';

const BUCKET_OF: Record<Task['status'], Bucket> = {
  running: 'running',
  completed: 'completed',
  failed: 'failed',
  idle: 'other',
};

const BUCKET_LABEL: Record<Bucket, string> = {
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  other: '未知',
};

/** `created_at` 现在是带 Z 的 ISO-8601；空值就老实显示未知，不填 now()。 */
function formatStarted(iso: string): string {
  if (!iso) return '开始时间未知';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '开始时间未知';
  const sameDay = d.toDateString() === new Date().toDateString();
  const hhmm = d.toLocaleTimeString('zh-CN', { hour12: false });
  if (sameDay) return `开始于 ${hhmm}`;
  return `开始于 ${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hhmm}`;
}

/** 完成/失败任务按创建时间倒序排在前面，运行中的永远置顶。 */
function sortForDisplay(tasks: Task[]): Task[] {
  const rank: Record<Bucket, number> = { running: 0, other: 1, failed: 2, completed: 3 };
  return [...tasks].sort((a, b) => {
    const byBucket = rank[BUCKET_OF[a.status]] - rank[BUCKET_OF[b.status]];
    if (byBucket !== 0) return byBucket;
    return (Date.parse(b.createdAt) || 0) - (Date.parse(a.createdAt) || 0);
  });
}

export interface MiningTaskMonitorProps {
  /**
   * 有登录态才轮询。壳在公开路由（登录页）也会挂载本组件，未登录时轮询
   * 只会打 401；登录后翻 true，立即拉取。默认 true（兼容既有调用/测试）。
   */
  enabled?: boolean;
}

const MiningTaskMonitor: React.FC<MiningTaskMonitorProps> = ({ enabled = true }) => {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [pollError, setPollError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);

  const aliveRef = useRef(true);
  // 收起时的「任务指纹」：之后只要出现没见过的任务、或又有任务开跑，就重新冒头。
  const dismissedSigRef = useRef('');
  const cancellingRef = useRef<string[]>([]);

  const refresh = useCallback(async () => {
    try {
      const r = await listTasks();
      if (!aliveRef.current) return;
      setTasks(r.data?.tasks ?? []);
      setPollError(null);
    } catch (e: unknown) {
      if (!aliveRef.current) return;
      // 轮询失败不改变已有列表（后端重启的瞬间不该把面板清空），只挂个提示。
      setPollError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    aliveRef.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => {
      aliveRef.current = false;
      clearInterval(timer);
    };
  }, [refresh, enabled]);

  const running = useMemo(() => tasks.filter((t) => BUCKET_OF[t.status] === 'running'), [tasks]);
  const ordered = useMemo(() => sortForDisplay(tasks).slice(0, MAX_ROWS), [tasks]);

  // 指纹只包含「任务集合 + 各自状态」，跟进度无关：进度每 5 秒都在变，
  // 拿它当指纹会让收起的手一松就弹回来。
  const signature = useMemo(
    () =>
      tasks
        .map((t) => `${t.taskId}:${t.status}`)
        .sort()
        .join('|'),
    [tasks],
  );

  useEffect(() => {
    if (!dismissed) return;
    if (signature !== dismissedSigRef.current) setDismissed(false);
  }, [signature, dismissed]);

  const handleCancel = useCallback(
    async (taskId: string) => {
      if (cancellingRef.current.includes(taskId)) return;
      cancellingRef.current = [...cancellingRef.current, taskId];
      setCancelling(cancellingRef.current);
      setActionError(null);
      try {
        await cancelMining(taskId);
        await refresh();
      } catch (e: unknown) {
        setActionError(e instanceof Error ? e.message : String(e));
      } finally {
        cancellingRef.current = cancellingRef.current.filter((id) => id !== taskId);
        setCancelling(cancellingRef.current);
      }
    },
    [refresh],
  );

  // 未登录（登录页不再挂答案）或一个任务都没有：整个组件不渲染
  // （不占屏幕、不给导航栏添乱）
  if (!enabled || tasks.length === 0) return null;

  const primary = running[0];
  const chipLabel = primary
    ? `因子挖掘 ${primary.progress.progress}%${
        primary.progress.totalRounds ? ` · Loop ${primary.progress.currentRound}/${primary.progress.totalRounds}` : ''
      }`
    : '无运行中的挖掘任务';

  // 收起后只在「任务集合或状态变了」时回来（见上面的 signature）。
  // 别在这里顺手把「有任务在跑」当成重新冒头的条件：它会跟 signature 打架，
  // 也会让「收起」这个动作在任务跑完前完全无效。
  if (dismissed) return null;

  return (
    <div
      className="fixed right-4 z-[1040] flex flex-col items-end gap-2"
      style={{ bottom: 'calc(var(--dock-height, 0px) + 12px)' }}
    >
      {open && (
        <section
          aria-label="后台挖掘任务"
          className="w-[min(92vw,26rem)] max-h-[60vh] overflow-hidden rounded-2xl border border-slate-200 bg-white/95 shadow-2xl backdrop-blur-xl"
        >
          <header className="flex items-center gap-2 border-b border-slate-100 px-3 py-2">
            <span className="text-xs font-bold text-slate-700">后台挖掘任务</span>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-slate-500">
              {running.length} 运行中 / {tasks.length} 总计
            </span>
            <div className="flex-1" />
            <button
              onClick={() => setOpen(false)}
              className="rounded-full p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              aria-label="收起任务面板"
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          </header>

          {pollError && (
            <p className="border-b border-amber-100 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-700">
              任务列表刷新失败：{pollError.slice(0, 120)}（显示的是最后一次成功的数据）
            </p>
          )}
          {actionError && (
            <p className="border-b border-rose-100 bg-rose-50 px-3 py-1.5 text-[11px] text-rose-600">
              取消失败：{actionError.slice(0, 120)}
            </p>
          )}

          <ul className="max-h-[calc(60vh-2.5rem)] divide-y divide-slate-100 overflow-y-auto">
            {ordered.map((t) => {
              const bucket = BUCKET_OF[t.status];
              const isRunning = bucket === 'running';
              const reason = t.progress.message || '';
              return (
                <li key={t.taskId} className="flex items-start gap-2 px-3 py-2">
                  <span className="mt-0.5 shrink-0">
                    {bucket === 'running' ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-indigo-500" />
                    ) : bucket === 'completed' ? (
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                    ) : bucket === 'failed' ? (
                      <AlertTriangle className="h-3.5 w-3.5 text-rose-500" />
                    ) : (
                      <CircleDashed className="h-3.5 w-3.5 text-slate-400" />
                    )}
                  </span>

                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <code className="font-mono text-[10px] text-slate-500">{t.taskId.slice(0, 8)}</code>
                      <span
                        className={`text-[11px] font-bold ${
                          isRunning
                            ? 'text-indigo-600'
                            : bucket === 'completed'
                              ? 'text-emerald-600'
                              : bucket === 'failed'
                                ? 'text-rose-600'
                                : 'text-slate-500'
                        }`}
                      >
                        {BUCKET_LABEL[bucket]}
                        {isRunning && t.progress.totalRounds
                          ? ` ${t.progress.progress}% · Loop ${t.progress.currentRound}/${t.progress.totalRounds}`
                          : ''}
                      </span>
                    </div>
                    <div className="mt-0.5 truncate text-[10px] text-slate-400" title={reason}>
                      {formatStarted(t.createdAt)}
                      {reason && !isRunning && ` · ${reason.slice(0, REASON_MAX)}`}
                      {isRunning && t.progress.message && ` · ${t.progress.message.slice(0, REASON_MAX)}`}
                    </div>
                  </div>

                  {isRunning && (
                    <button
                      onClick={() => void handleCancel(t.taskId)}
                      disabled={cancelling.includes(t.taskId)}
                      className="shrink-0 rounded-full border border-slate-200 px-2 py-0.5 text-[10px] font-bold text-slate-500 hover:border-rose-200 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50"
                    >
                      {cancelling.includes(t.taskId) ? '取消中…' : '取消'}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
          {tasks.length > MAX_ROWS && (
            <p className="border-t border-slate-100 px-3 py-1.5 text-[10px] text-slate-400">
              只显示最近 {MAX_ROWS} 条（共 {tasks.length} 条）
            </p>
          )}
        </section>
      )}

      <div className="flex items-center gap-1">
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[11px] font-bold shadow-lg backdrop-blur ${
            primary
              ? 'border-indigo-200 bg-white/95 text-indigo-600'
              : 'border-slate-200 bg-white/90 text-slate-500'
          }`}
        >
          {primary ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <CheckCircle2 className="h-3 w-3 text-slate-400" />
          )}
          {chipLabel}
        </button>
        <button
          onClick={() => {
            dismissedSigRef.current = signature;
            setDismissed(true);
            setOpen(false);
          }}
          className="rounded-full border border-slate-200 bg-white/90 p-1 text-slate-400 shadow-lg backdrop-blur hover:text-slate-600"
          aria-label="隐藏任务监视器"
          title="隐藏（任务状态变化时会再次出现）"
        >
          <X className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
};

export default MiningTaskMonitor;
