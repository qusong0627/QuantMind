import React from 'react';
import { Square } from 'lucide-react';
import { ProgressSidebar } from '../components-v2/ProgressSidebar';
import { LiveCharts } from '../components-v2/LiveCharts';
import { FactorList } from '../components-v2/FactorList';
import { useTaskContext } from '../context-v2/TaskContext';
import { Layout } from '../components-v2/layout/Layout';
import type { PageId } from '../components-v2/layout/Layout';

interface MiningDashboardPageProps {
  onNavigate?: (page: PageId) => void;
  onBacktestFactor?: (factorId: string) => void;
}

export const MiningDashboardPage: React.FC<MiningDashboardPageProps> = ({ onNavigate, onBacktestFactor }) => {
  const { miningTask: task, stopMining } = useTaskContext();

  // If no task, this page shouldn't be active (or show empty state)
  if (!task) {
    return (
      <Layout
        currentPage="home"
        onNavigate={onNavigate || (() => {})}
        showNavigation={!!onNavigate}
      >
        <div className="flex flex-col items-center justify-center min-h-[60vh] animate-fade-in-up">
          <p className="text-xs text-muted-foreground">当前无进行中的挖掘任务</p>
          <button 
            className="mt-4 text-primary hover:underline"
            onClick={() => onNavigate?.('home')}
          >
            返回主页
          </button>
        </div>
      </Layout>
    );
  }

  return (
    <Layout
      currentPage="home"
      onNavigate={onNavigate || (() => {})}
      showNavigation={!!onNavigate}
    >
      {/* 任务状态栏：状态 + 进度 + 停止（替代原底部悬浮输入框） */}
      <div className="mb-4 flex items-center justify-center gap-3 rounded-2xl border border-border/60 bg-white/80 backdrop-blur-xl px-4 py-3 shadow-xs text-center">
        {task.status === 'running' || task.status === 'stopping' ? (
          <span className="relative flex h-2.5 w-2.5 shrink-0">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-blue-500" />
          </span>
        ) : (
          <span className={`h-2.5 w-2.5 rounded-full shrink-0 ${task.status === 'queued' ? 'bg-amber-500' : task.status === 'completed' ? 'bg-emerald-500' : task.status === 'cancelled' ? 'bg-slate-400' : 'bg-red-500'}`} />
        )}
        <span className="text-xs font-bold text-slate-800 whitespace-nowrap">
          {task.status === 'queued'
            ? `排队中${task.queuePosition ? `（第 ${task.queuePosition} 位）` : ''}`
            : task.status === 'running'
            ? '任务运行中'
            : task.status === 'stopping'
              ? '正在停止，等待进程退出确认'
              : task.status === 'completed'
                ? '任务已完成'
                : task.status === 'cancelled'
                  ? '任务已取消'
                  : task.status === 'failed'
                    ? '任务已失败'
                    : '任务等待中'}
        </span>
        <span className="text-xs text-muted-foreground truncate min-w-0 max-w-md" title={task.progress?.message}>
          {task.progress?.message || `Loop ${task.progress?.currentRound ?? 0}/${task.progress?.totalRounds ?? 0}`}
        </span>
        <div className="w-28 h-1.5 rounded-full bg-slate-100 overflow-hidden hidden sm:block">
          <div
            className="h-full rounded-full bg-gradient-to-r from-blue-500 to-indigo-500 transition-all duration-500"
            style={{ width: `${Math.min(100, Math.max(0, task.progress?.progress ?? 0))}%` }}
          />
        </div>
        <span className="text-[11px] font-mono text-slate-400 hidden md:block w-9 text-right">
          {Math.min(100, Math.max(0, task.progress?.progress ?? 0))}%
        </span>
        {(task.status === 'running' || task.status === 'queued') && (
          <button
            onClick={stopMining}
            className="flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-bold text-red-600 bg-red-50 hover:bg-red-100 border border-red-100 transition-colors cursor-pointer"
            title={task.status === 'queued' ? '取消排队' : '停止当前任务'}
          >
            <Square className="w-3 h-3" />
            {task.status === 'queued' ? '取消排队' : '停止任务'}
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <div className="lg:col-span-1">
          <ProgressSidebar
            progress={task.progress}
            factorCount={task.metrics?.totalFactors ?? 0}
            bestRankIc={task.metrics?.rankIc ?? null}
            bestFactorName={task.metrics?.factorName ?? null}
          />
        </div>
        <div className="lg:col-span-3">
          <LiveCharts
            metrics={task.metrics || null}
            logs={task.logs}
          />
        </div>

        <div className="lg:col-span-4">
           <FactorList
             metrics={task.metrics || null}
             onBacktestFactor={onBacktestFactor}
           />
        </div>
      </div>
    </Layout>
  );
};
