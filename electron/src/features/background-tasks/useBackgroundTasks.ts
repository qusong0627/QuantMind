import { useEffect, useRef, useState } from 'react';
import { fetchBackgroundTasks, fetchServerLoad } from './taskSources';
import type { BackgroundTask, ServerLoad } from './types';

const TASKS_INTERVAL_MS = 4000;
const LOAD_INTERVAL_MS = 3000;

/**
 * 后台任务坞的轮询源。
 *
 * 页面隐藏时停轮询（Electron 窗口最小化/Ctrl+Tab 切走后不该继续打接口）；
 * 每个来源都在 taskSources 里各自 try/catch，单源失败只是少一块，不会让整坞空掉。
 */
export function useBackgroundTasks(enabled: boolean) {
  const [tasks, setTasks] = useState<BackgroundTask[]>([]);
  const [load, setLoad] = useState<ServerLoad | null>(null);
  const [completedNote, setCompletedNote] = useState<string | null>(null);
  const prevIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let tasksTimer: ReturnType<typeof setInterval> | null = null;
    let loadTimer: ReturnType<typeof setInterval> | null = null;

    const pullTasks = async () => {
      if (document.hidden) return;
      const next = await fetchBackgroundTasks();
      if (cancelled) return;
      // 消失的任务 = 刚结束，用它驱动一次 polite 播报（不抢焦点）
      const nextIds = new Set(next.map((t) => t.id));
      const finished = [...prevIdsRef.current].filter((id) => !nextIds.has(id));
      if (finished.length) setCompletedNote(`${finished.length} 个后台任务已结束`);
      prevIdsRef.current = nextIds;
      setTasks(next);
    };

    const pullLoad = async () => {
      if (document.hidden) return;
      const next = await fetchServerLoad();
      if (!cancelled) setLoad(next);
    };

    void pullTasks();
    void pullLoad();
    tasksTimer = setInterval(pullTasks, TASKS_INTERVAL_MS);
    loadTimer = setInterval(pullLoad, LOAD_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (tasksTimer) clearInterval(tasksTimer);
      if (loadTimer) clearInterval(loadTimer);
    };
  }, [enabled]);

  return { tasks, load, completedNote, clearCompletedNote: () => setCompletedNote(null) };
}
