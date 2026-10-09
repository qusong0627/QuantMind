import React, { useState, useEffect, useRef } from 'react';
import { HomePage } from '../pages-v2/HomePage';
import { MiningDashboardPage } from '../pages-v2/MiningDashboardPage';
import { FactorLibraryPage } from '../pages-v2/FactorLibraryPage';
import { FactorPoolPage } from '../pages-v2/FactorPoolPage';
import { BacktestPage } from '../pages-v2/BacktestPage';
import { SettingsPage } from '../pages-v2/SettingsPage';
import { HistoryPage } from '../pages-v2/HistoryPage';
import { Layout } from '../components-v2/layout/Layout';
import type { PageId } from '../components-v2/layout/Layout';
import { ParticleBackground } from '../components-v2/ParticleBackground';
import { TaskProvider, useTaskContext } from '../context-v2/TaskContext';
import { RunQueueProvider } from '../context-v2/RunQueueContext';
import type { MiningHistoryRow } from '../services-v2/api';
import type { DocRow } from '../services-v2/docMiningApi';
import type { DocMiningResume, MiningRetryDraft, TaskConfig } from '../types-v2';

/** 后端 market 值域（不认得的市场不往回填里塞——宁可留空也不冒充） */
const RETRY_MARKETS: readonly string[] = ['a_share', 'crypto', 'hong_kong', 'us_stock', 'futures'];

/**
 * 历史行 → 重跑草稿。key 由调用方给代次（连点两次「重跑」必须重新应用）。
 * 认不出的 market/data_source 留空：回填错的选项比不回填更危险。
 */
export function buildRetryDraft(row: MiningHistoryRow, key: number): MiningRetryDraft {
  return {
    key,
    userInput: row.direction || '',
    miningMarket: RETRY_MARKETS.includes(row.market)
      ? (row.market as TaskConfig['miningMarket'])
      : undefined,
    universe: row.universe || undefined,
    dataSource:
      row.data_source === 'qlib_bin' || row.data_source === 'parquet'
        ? (row.data_source as TaskConfig['dataSource'])
        : undefined,
  };
}

// Inner component to access context
const AppContent: React.FC = () => {
  const [currentPage, setCurrentPage] = useState<PageId>('home');
  // 挖掘历史的一次性状态：查看结果（→因子库按任务过滤）、重跑（→首页回填）、
  // 「继续挖掘」（→首页文档链恢复该文档）
  const [libraryTask, setLibraryTask] = useState<{ taskId: string; label: string } | null>(null);
  const [retryDraft, setRetryDraft] = useState<MiningRetryDraft | null>(null);
  const [docResume, setDocResume] = useState<DocMiningResume | null>(null);
  const retryKeyRef = useRef(1);
  const docKeyRef = useRef(1);
  const { miningStartSeq } = useTaskContext();

  // 仅当用户「主动开始」一次挖掘时自动进入演化台；
  // 恢复历史/运行中任务（刷新或离开再回来）不触发，避免被强制带走。
  const lastStartSeqRef = useRef(miningStartSeq);
  useEffect(() => {
    if (miningStartSeq !== lastStartSeqRef.current) {
      lastStartSeqRef.current = miningStartSeq;
      setCurrentPage('mining_dashboard');
    }
  }, [miningStartSeq]);

  // 导航一律直达目标页；顺带清掉历史页带过来的一次性状态：
  // 从导航进因子库 = 全新入口（不带任务过滤）；从导航回首页 = 不带重跑草稿、
  // 不带文档恢复草稿（否则每次进首页都会把上次的状态重新应用回输入区）。
  const handleNavigate = (page: PageId) => {
    if (page === 'library') setLibraryTask(null);
    if (page === 'home') {
      setRetryDraft(null);
      setDocResume(null);
    }
    setCurrentPage(page);
  };

  const handleHistoryViewResults = (row: MiningHistoryRow) => {
    setLibraryTask({
      taskId: row.task_id,
      // 方向为空（legacy 行）用短 id 兜底，别让过滤横幅显示空白
      label: row.direction || row.task_id.slice(0, 8),
    });
    setCurrentPage('library');
  };

  const handleHistoryRetry = (row: MiningHistoryRow) => {
    // 与「继续挖掘」互斥：后置的 docResume 效应会盖过重跑的字面回填
    setDocResume(null);
    setRetryDraft(buildRetryDraft(row, retryKeyRef.current++));
    setCurrentPage('home');
  };

  // 「文档解析 → 继续挖掘」：整行带回首页文档链；key 代次语义与重跑一致
  const handleDocsResume = (row: DocRow) => {
    setRetryDraft(null);
    setDocResume({
      key: docKeyRef.current++,
      docId: row.doc_id,
      filename: row.filename,
    });
    setCurrentPage('home');
  };

  return (
    <>
      <ParticleBackground />
      {/* Conditional rendering: only mount the active page, unmount others when switching.
          TaskProvider persists mining/backtest state across page switches. */}
      {currentPage === 'home' && (
        <HomePage onNavigate={handleNavigate} retryDraft={retryDraft} docResume={docResume} />
      )}
      {currentPage === 'mining_dashboard' && (
        <MiningDashboardPage onNavigate={handleNavigate} />
      )}
      {currentPage === 'library' && (
        <Layout currentPage={currentPage} onNavigate={handleNavigate}>
          <FactorLibraryPage
            onNavigate={handleNavigate}
            taskFilter={libraryTask}
            onClearTaskFilter={() => setLibraryTask(null)}
          />
        </Layout>
      )}
      {currentPage === 'pool' && (
        <Layout currentPage={currentPage} onNavigate={handleNavigate}>
          <FactorPoolPage />
        </Layout>
      )}
      {currentPage === 'backtest' && (
        <Layout currentPage={currentPage} onNavigate={handleNavigate}>
          <BacktestPage />
        </Layout>
      )}
      {currentPage === 'history' && (
        <Layout currentPage={currentPage} onNavigate={handleNavigate}>
          <HistoryPage
            onViewResults={handleHistoryViewResults}
            onRetry={handleHistoryRetry}
            onResumeDoc={handleDocsResume}
          />
        </Layout>
      )}
      {currentPage === 'settings' && (
        <Layout currentPage={currentPage} onNavigate={handleNavigate}>
          <SettingsPage />
        </Layout>
      )}
    </>
  );
};

const AppRoot: React.FC = () => {
  return (
    <TaskProvider>
      {/* 行级回测/物化队列跨页共享：挂在 TaskProvider 内、页面外，切页不丢 */}
      <RunQueueProvider>
        <AppContent />
      </RunQueueProvider>
    </TaskProvider>
  );
};

export default AppRoot;
export { AppRoot as App };
