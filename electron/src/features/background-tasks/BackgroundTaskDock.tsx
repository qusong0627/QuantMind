import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { useBackgroundTasks } from './useBackgroundTasks';
import { cancelInferenceTask, cancelMiningTask } from './taskSources';
import { TASK_KIND_META, TASK_KIND_ORDER, type BackgroundTask, type ServerLoad } from './types';
import './backgroundTaskDock.css';

const POS_KEY = 'qm.bgDock.pos.v1';
const OPACITY_KEY = 'qm.bgDock.opacity.v1';
const EXPANDED_KEY = 'qm.bgDock.expanded.v1';
const OPACITY_STEPS = [0.72, 0.86, 1] as const;
const MARGIN = 12;

interface DockPos {
  x: number;
  y: number;
}

function readPos(): DockPos | null {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as DockPos;
    if (Number.isFinite(parsed?.x) && Number.isFinite(parsed?.y)) return parsed;
  } catch {
    /* 坏值按未保存处理 */
  }
  return null;
}

function defaultPos(estimatedWidth: number, estimatedHeight: number): DockPos {
  return {
    x: Math.max(MARGIN, window.innerWidth - estimatedWidth - 24),
    y: Math.max(MARGIN, window.innerHeight - estimatedHeight - 88),
  };
}

function clampPos(pos: DockPos, width: number, height: number): DockPos {
  return {
    x: Math.min(Math.max(MARGIN, pos.x), Math.max(MARGIN, window.innerWidth - width - MARGIN)),
    y: Math.min(Math.max(MARGIN, pos.y), Math.max(MARGIN, window.innerHeight - height - MARGIN)),
  };
}

function formatElapsed(startedAt?: string | null): string | null {
  if (!startedAt) return null;
  const started = Date.parse(startedAt);
  if (!Number.isFinite(started)) return null;
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

function LoadMeter({ load }: { load: ServerLoad | null }) {
  if (!load) return null;
  const cpu = load.cpu_pct;
  const memPct =
    load.mem_used_gb != null && load.mem_total_gb
      ? (load.mem_used_gb / load.mem_total_gb) * 100
      : null;
  const tone = (v: number | null) => (v == null ? 'idle' : v >= 85 ? 'hot' : v >= 60 ? 'warm' : 'cool');
  return (
    <div className="bgdock-load" aria-label="服务器负载">
      <span className={`bgdock-meter bgdock-meter--${tone(cpu)}`} title="CPU 使用率">
        <span className="bgdock-meter__label">CPU</span>
        <span className="bgdock-meter__value">{cpu == null ? '—' : `${Math.round(cpu)}%`}</span>
        <span className="bgdock-meter__track" aria-hidden="true">
          <span className="bgdock-meter__fill" style={{ inlineSize: `${cpu ?? 0}%` }} />
        </span>
      </span>
      <span className={`bgdock-meter bgdock-meter--${tone(memPct)}`} title="内存占用">
        <span className="bgdock-meter__label">MEM</span>
        <span className="bgdock-meter__value">
          {load.mem_used_gb == null ? '—' : `${load.mem_used_gb.toFixed(0)}G`}
        </span>
        <span className="bgdock-meter__track" aria-hidden="true">
          <span className="bgdock-meter__fill" style={{ inlineSize: `${memPct ?? 0}%` }} />
        </span>
      </span>
      {load.gpu && (
        <span className={`bgdock-meter bgdock-meter--${tone(load.gpu.util_pct)}`} title={load.gpu.name}>
          <span className="bgdock-meter__label">GPU</span>
          <span className="bgdock-meter__value">{Math.round(load.gpu.util_pct)}%</span>
          <span className="bgdock-meter__track" aria-hidden="true">
            <span className="bgdock-meter__fill" style={{ inlineSize: `${load.gpu.util_pct}%` }} />
          </span>
        </span>
      )}
    </div>
  );
}

function TaskRow({
  task,
  onOpen,
  onCancel,
  onDismiss,
}: {
  task: BackgroundTask;
  onOpen: (task: BackgroundTask) => void;
  onCancel: (task: BackgroundTask) => void;
  onDismiss: (task: BackgroundTask) => void;
}) {
  const elapsed = formatElapsed(task.startedAt);
  // 取消只对**服务端支持取消**且在跑的任务开放：挖掘 / 推理补推。
  // 其余任务（训练/同步）没有取消端点：× 只把它从坞里隐藏，任务本身继续跑。
  const canCancel =
    (task.kind === 'mining' || task.kind === 'inference') &&
    (task.status === 'running' || task.status === 'queued');
  return (
    <div className="bgdock-row">
      <button type="button" className="bgdock-row__main" onClick={() => onOpen(task)}>
        <span className={`bgdock-row__dot bgdock-row__dot--${task.status}`} aria-hidden="true" />
        <span className="bgdock-row__body">
          <span className="bgdock-row__head">
            <span className="bgdock-row__kind">{TASK_KIND_META[task.kind].label}</span>
            <span className="bgdock-row__title">{task.title}</span>
          </span>
          <progress
            className="bgdock-row__bar"
            {...(task.progress == null ? {} : { value: task.progress, max: 100 })}
            aria-label={`${TASK_KIND_META[task.kind].label}进度`}
          />
          <span className="bgdock-row__meta">
            {task.progress == null ? '进行中' : `${Math.round(task.progress)}%`}
            {elapsed ? ` · ${elapsed}` : ''}
            {task.detail ? ` · ${task.detail}` : ''}
          </span>
        </span>
      </button>
      <button
        type="button"
        className="bgdock-row__cancel"
        onClick={() => (canCancel ? onCancel(task) : onDismiss(task))}
        title={
          canCancel
            ? task.kind === 'mining'
              ? '取消该挖掘任务'
              : '取消该推理补推任务'
            : '关闭：仅从坞中隐藏，任务继续在后台运行'
        }
        aria-label={`${canCancel ? '取消' : '关闭'} ${task.title}`}
      >
        ×
      </button>
    </div>
  );
}

export const BackgroundTaskDock: React.FC<{ enabled?: boolean }> = ({ enabled = true }) => {
  const navigate = useNavigate();
  const { tasks, load, completedNote, clearCompletedNote } = useBackgroundTasks(enabled);
  const [expanded, setExpanded] = useState<boolean>(() => {
    try {
      return localStorage.getItem(EXPANDED_KEY) === '1';
    } catch {
      return false;
    }
  });
  const [opacityIndex, setOpacityIndex] = useState<number>(() => {
    const raw = Number(localStorage.getItem(OPACITY_KEY));
    return Number.isInteger(raw) && raw >= 0 && raw < OPACITY_STEPS.length ? raw : 1;
  });
  const [pos, setPos] = useState<DockPos | null>(() => readPos());
  // 关闭悬浮条（本次会话内隐藏；刷新/重开页面恢复）。面板内的 × 只是收面板，
  // 这里的 × 才是把整条坞关掉——用户口径「后台任务整一个关闭按钮」。
  const [hidden, setHidden] = useState(false);
  // 行级关闭：训练/同步没有取消端点，× 只从坞里隐藏（任务继续跑）。
  // 任务从服务端消失后清掉对应 id，下次同名任务仍会出现。
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const dockRef = useRef<HTMLDivElement | null>(null);
  const dragState = useRef<{ dx: number; dy: number; moved: boolean } | null>(null);

  const visibleTasks = useMemo(
    () => tasks.filter((t) => !dismissed.has(t.id)),
    [tasks, dismissed],
  );
  const running = visibleTasks.length;
  const counts = useMemo(() => {
    const map = new Map<string, number>();
    for (const t of visibleTasks) map.set(t.kind, (map.get(t.kind) ?? 0) + 1);
    return TASK_KIND_ORDER.filter((k) => map.get(k)).map((k) => ({ kind: k, n: map.get(k)! }));
  }, [visibleTasks]);

  // 任务结束后把 dismiss 记录清掉（同一 id 再来时不该被旧的关闭状态挡住）
  useEffect(() => {
    setDismissed((prev) => {
      if (!prev.size) return prev;
      const live = new Set(tasks.map((t) => t.id));
      const next = new Set([...prev].filter((id) => live.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [tasks]);

  useEffect(() => {
    localStorage.setItem(EXPANDED_KEY, expanded ? '1' : '0');
  }, [expanded]);

  useEffect(() => {
    localStorage.setItem(OPACITY_KEY, String(opacityIndex));
  }, [opacityIndex]);

  // 首次挂载与窗口尺寸变化时把悬浮条夹回视口内
  useEffect(() => {
    const sync = () => {
      const el = dockRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      setPos((prev) => clampPos(prev ?? defaultPos(rect.width, rect.height), rect.width, rect.height));
    };
    sync();
    window.addEventListener('resize', sync);
    return () => window.removeEventListener('resize', sync);
  }, []);

  useEffect(() => {
    if (!completedNote) return;
    const timer = setTimeout(clearCompletedNote, 4000);
    return () => clearTimeout(timer);
  }, [completedNote, clearCompletedNote]);

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const el = dockRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    dragState.current = { dx: event.clientX - rect.left, dy: event.clientY - rect.top, moved: false };
    // Pointer capture 在部分宿主（老 Electron / jsdom）缺失，缺了就退化为普通拖动
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }, []);

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const state = dragState.current;
    const el = dockRef.current;
    if (!state || !el) return;
    const rect = el.getBoundingClientRect();
    state.moved = true;
    setPos(clampPos({ x: event.clientX - state.dx, y: event.clientY - state.dy }, rect.width, rect.height));
  }, []);

  const onPointerUp = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const state = dragState.current;
    dragState.current = null;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    }
    if (!state?.moved) {
      // 没拖动就是点击：展开/收起
      setExpanded((v) => !v);
      return;
    }
    setPos((prev) => {
      if (!prev) return prev;
      localStorage.setItem(POS_KEY, JSON.stringify(prev));
      return prev;
    });
  }, []);

  const resetPos = useCallback(() => {
    const el = dockRef.current;
    const rect = el?.getBoundingClientRect();
    localStorage.removeItem(POS_KEY);
    setPos(defaultPos(rect?.width ?? 320, rect?.height ?? 40));
  }, []);

  const openTask = useCallback(
    (task: BackgroundTask) => {
      setExpanded(false);
      navigate(task.href);
    },
    [navigate],
  );

  const cancelTask = useCallback((task: BackgroundTask) => {
    // 失败不打扰（下一轮轮询自然会把它带回来）；关闭后由服务端状态驱动消失。
    const request =
      task.kind === 'inference'
        ? cancelInferenceTask(task.id)
        : cancelMiningTask(task.id);
    void request.catch(() => undefined);
  }, []);

  const dismissTask = useCallback((task: BackgroundTask) => {
    setDismissed((prev) => new Set(prev).add(task.id));
  }, []);

  const opensUpward = pos ? pos.y > window.innerHeight * 0.42 : true;
  const style = {
    transform: pos ? `translate3d(${pos.x}px, ${pos.y}px, 0)` : undefined,
    '--bgdock-alpha': String(OPACITY_STEPS[opacityIndex]),
  } as React.CSSProperties;

  // 挂到 body：底部导航 z-index 1050，且页面里可能有带 transform 的祖先，
  // 两者都会让 fixed 定位/层级失效。portal 一次解决。
  if (hidden) return null;
  return createPortal(
    <div
      ref={dockRef}
      className={`bgdock${expanded ? ' bgdock--expanded' : ''}${opensUpward ? '' : ' bgdock--down'}`}
      style={style}
      role="region"
      aria-label="后台任务坞"
    >
      {expanded && (
        <div className="bgdock-panel" id="bgdock-panel">
          <div className="bgdock-panel__head">
            <span className="bgdock-panel__title">后台任务</span>
            <span className="bgdock-panel__count">{running ? `${running} 个进行中` : '空闲'}</span>
            <button
              type="button"
              className="bgdock-icon"
              onClick={() => setOpacityIndex((i) => (i + 1) % OPACITY_STEPS.length)}
              title={`透明度 ${Math.round(OPACITY_STEPS[opacityIndex] * 100)}%，点击切换`}
              aria-label="切换悬浮条透明度"
            >
              ◐
            </button>
            <button
              type="button"
              className="bgdock-icon"
              onClick={resetPos}
              title="回到默认位置"
              aria-label="悬浮条回到默认位置"
            >
              ⌖
            </button>
            <button
              type="button"
              className="bgdock-icon"
              onClick={() => setExpanded(false)}
              title="关闭后台任务面板"
              aria-label="关闭后台任务面板"
            >
              ×
            </button>
          </div>
          <div className="bgdock-panel__body">
            {running === 0 ? (
              <p className="bgdock-empty">当前没有后台任务</p>
            ) : (
              visibleTasks.map((task) => (
                <TaskRow
                  key={task.id}
                  task={task}
                  onOpen={openTask}
                  onCancel={cancelTask}
                  onDismiss={dismissTask}
                />
              ))
            )}
          </div>
          <LoadMeter load={load} />
        </div>
      )}

      <div className="bgdock-bar">
        <button
          type="button"
          className="bgdock-grip"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onDoubleClick={resetPos}
          aria-expanded={expanded}
          aria-controls="bgdock-panel"
          aria-label={`后台任务，${running} 个进行中；拖动可移动，双击回默认位置`}
        >
          <span className={`bgdock-pulse${running ? ' bgdock-pulse--on' : ''}`} aria-hidden="true" />
          <span className="bgdock-grip__label">{running ? `${running} 进行中` : '后台任务'}</span>
          {counts.map(({ kind, n }) => (
            <span key={kind} className={`bgdock-chip bgdock-chip--${kind}`} title={TASK_KIND_META[kind].label}>
              {TASK_KIND_META[kind].short}
              {n > 1 ? n : ''}
            </span>
          ))}
        </button>
        <LoadMeter load={load} />
        <button
          type="button"
          className="bgdock-bar__close"
          onClick={() => setHidden(true)}
          title="关闭后台任务坞（刷新后恢复）"
          aria-label="关闭后台任务坞"
        >
          ×
        </button>
      </div>

      <span className="bgdock-live" role="status" aria-live="polite">
        {completedNote ?? ''}
      </span>
    </div>,
    document.body,
  );
};

export default BackgroundTaskDock;
