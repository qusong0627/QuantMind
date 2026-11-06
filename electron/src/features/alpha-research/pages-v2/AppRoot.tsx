import React, { useState, useEffect, useRef } from 'react';
import { HomePage } from '../pages-v2/HomePage';
import { MiningDashboardPage } from '../pages-v2/MiningDashboardPage';
import { FactorLibraryPage } from '../pages-v2/FactorLibraryPage';
import { BacktestPage } from '../pages-v2/BacktestPage';
import { SettingsPage } from '../pages-v2/SettingsPage';
import { TaskCenterPage } from '../pages-v2/TaskCenterPage';
import { PaperLibraryPage } from '../pages-v2/PaperLibraryPage';
import { Layout } from '../components-v2/layout/Layout';
import type { PageId } from '../components-v2/layout/Layout';
import { ParticleBackground } from '../components-v2/ParticleBackground';
import { TaskProvider, useTaskContext } from '../context-v2/TaskContext';
import { registerSessionReset } from '../../../store/sessionBoundary';
import { migrateSessionScopedStorageKey } from '../../../store/sessionScopedStorage';

// 品牌更名 QuantaAlpha→AutoAlpha：把旧存储键一次性搬到新键，老用户偏好不断档。
migrateSessionScopedStorageKey('quantaalpha_config', 'autoalpha_config');

// Inner component to access context
const AppContent: React.FC = () => {
  const [currentPage, setCurrentPage] = useState<PageId>('home');
  const [selectedFactorId, setSelectedFactorId] = useState('');
  const { miningStartSeq } = useTaskContext();

  useEffect(() => registerSessionReset(() => {
    setSelectedFactorId('');
    setCurrentPage('home');
  }), []);

  const openFactorBacktest = (factorId: string) => {
    setSelectedFactorId(factorId);
    setCurrentPage('backtest');
  };

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
      {currentPage === 'mining_dashboard' && <MiningDashboardPage onNavigate={setCurrentPage} onBacktestFactor={openFactorBacktest} />}
      {currentPage === 'library' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <FactorLibraryPage onBacktestFactor={openFactorBacktest} />
        </Layout>
      )}
      {currentPage === 'backtest' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <BacktestPage initialFactorId={selectedFactorId} />
        </Layout>
      )}
      {currentPage === 'settings' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <SettingsPage />
        </Layout>
      )}
      {currentPage === 'tasks' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <TaskCenterPage />
        </Layout>
      )}
      {currentPage === 'papers' && (
        <Layout currentPage={currentPage} onNavigate={setCurrentPage}>
          <PaperLibraryPage />
        </Layout>
      )}
    </>
  );
};

const AppRoot: React.FC = () => {
  return (
    <TaskProvider>
      <AppContent />
    </TaskProvider>
  );
};

export default AppRoot;
export { AppRoot as App };
