import React, { useState, useEffect, useRef } from 'react';
import { HomePage } from '../pages-v2/HomePage';
import { MiningDashboardPage } from '../pages-v2/MiningDashboardPage';
import { FactorLibraryPage } from '../pages-v2/FactorLibraryPage';
import { FactorPoolPage } from '../pages-v2/FactorPoolPage';
import { BacktestPage } from '../pages-v2/BacktestPage';
import { SettingsPage } from '../pages-v2/SettingsPage';
import { Layout } from '../components-v2/layout/Layout';
import type { PageId } from '../components-v2/layout/Layout';
import { ParticleBackground } from '../components-v2/ParticleBackground';
import { TaskProvider, useTaskContext } from '../context-v2/TaskContext';
import { RunQueueProvider } from '../context-v2/RunQueueContext';

// Inner component to access context
const AppContent: React.FC = () => {
  const [currentPage, setCurrentPage] = useState<PageId>('home');
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

  return (
    <>
      <ParticleBackground />
      {/* Conditional rendering: only mount the active page, unmount others when switching.
          TaskProvider persists mining/backtest state across page switches. */}
      {currentPage === 'home' && <HomePage onNavigate={setCurrentPage} />}
      {currentPage === 'mining_dashboard' && <MiningDashboardPage onNavigate={setCurrentPage} />}
      {currentPage === 'library' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <FactorLibraryPage onNavigate={setCurrentPage} />
        </Layout>
      )}
      {currentPage === 'pool' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <FactorPoolPage />
        </Layout>
      )}
      {currentPage === 'backtest' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <BacktestPage />
        </Layout>
      )}
      {currentPage === 'settings' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
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
