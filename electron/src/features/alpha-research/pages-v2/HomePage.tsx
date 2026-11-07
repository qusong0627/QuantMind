import React, { useCallback, useEffect, useState } from 'react';
import {
  Sparkles, Dna, Database, BarChart3, ArrowRight, Zap,
  Layers, Shield, Activity, Cpu,
  Loader2, Square
} from 'lucide-react';
import { ChatInput } from '../components-v2/ChatInput';
import { ResearchDirectionsDialog } from '../components-v2/ResearchDirectionsDialog';
import { SampledDirectionsDialog } from '../components-v2/SampledDirectionsDialog';
import { ResearchExperienceDialog } from '../components-v2/ResearchExperienceDialog';
import { VariantLedgerDialog } from '../components-v2/VariantLedgerDialog';
import { Layout } from '../components-v2/layout/Layout';
import type { PageId } from '../components-v2/layout/Layout';
import { useTaskContext } from '../context-v2/TaskContext';
import { getDataSummary } from '../services-v2/api';
import type { DataSummary, TaskConfig } from '../types-v2';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';

interface HomePageProps {
  onNavigate?: (page: PageId) => void;
}

const PRESET_PROMPTS = [
  '挖掘基于 5 日动量反转与成交量偏度组合的超额收益因子',
  '构建捕捉日频波动率非对称性与价格跳跃的量价特征',
  '寻找尾盘主力资金净流入与换手率背离的Alpha选股因子',
  '基于多周期均线发散度与流动性溢价挖掘中短线稳健因子',
];

const PRESET_LABELS = ['动量与量能', '波动与跳跃', '资金与换手', '趋势与流动性'];

export const HomePage: React.FC<HomePageProps> = ({ onNavigate }) => {
  const {
    backendAvailable,
    miningTask: task,
    miningStarting,
    miningSubmissionPending,
    retryMiningSubmission,
    previewMining,
    startMining,
    stopMining,
  } = useTaskContext();

  const [dataSummary, setDataSummary] = useState<DataSummary | null>(null);
  const [activePrompt, setActivePrompt] = useState('');
  const [researchConfig, setResearchConfig] = useState<TaskConfig | null>(null);
  const [sampleConfig, setSampleConfig] = useState<TaskConfig | null>(null);
  const [sampleQuotaSupported, setSampleQuotaSupported] = useState(false);
  const [experienceUniverse, setExperienceUniverse] = useState<string | null>(null);
  const [variantUniverse, setVariantUniverse] = useState<string | null>(null);
  const [sessionEpoch, setSessionEpoch] = useState(getSessionGeneration);
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const closeResearchDirections = useCallback(() => setResearchConfig(null), []);
  const closeSampleDirections = useCallback(() => setSampleConfig(null), []);
  const closeResearchExperience = useCallback(() => setExperienceUniverse(null), []);
  const closeVariantLedger = useCallback(() => setVariantUniverse(null), []);

  useEffect(() => registerSessionReset(() => {
    setActivePrompt('');
    setResearchConfig(null);
    setSampleConfig(null);
    setSampleQuotaSupported(false);
    setExperienceUniverse(null);
    setVariantUniverse(null);
    setDataSummary(null);
    setSessionEpoch(getSessionGeneration());
  }), []);

  // 任务激活 = 正在提交（POST /evolve 进行中）或后端任务运行中
  const taskActive = miningStarting || task?.status === 'queued' || task?.status === 'running' || task?.status === 'stopping';

  // 「AI 因子挖掘」入口：有任务（运行中/已完成）才进演化台；
  // 无任务时滚动到输入框，避免打开空的进度页。
  const handleOpenDashboard = () => {
    if (task) {
      onNavigate?.('mining_dashboard');
    } else {
      document.getElementById('mining-input')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  };

  useEffect(() => {
    if (!isAuthenticated || isAuthenticating) return;
    const generation = getSessionGeneration();
    let cancelled = false;
    getDataSummary()
      .then((res) => {
        if (!cancelled && generation === getSessionGeneration()) setDataSummary(res.data ?? null);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [isAuthenticated, isAuthenticating, sessionEpoch]);

  const universeCount = dataSummary?.universes
    ? Object.keys(dataSummary.universes).length
    : 0;
  const dateRangeText = dataSummary?.dateRange?.start && dataSummary?.dateRange?.end
    ? `${dataSummary.dateRange.start} ~ ${dataSummary.dateRange.end}`
    : '数据概况暂不可用';

  return (
    <Layout
      currentPage="home"
      onNavigate={onNavigate || (() => {})}
      showNavigation={!!onNavigate}
    >
      <div className="max-w-5xl mx-auto flex flex-col items-center gap-8 py-6 pb-12 select-none animate-fade-in-up">
        {/* ================= 1. Hero Title & Headline ================= */}
        <div className="text-center max-w-2xl flex flex-col items-center">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-blue-50/90 border border-blue-100/80 text-blue-600 text-xs font-bold mb-3 shadow-2xs">
            <Sparkles className="w-3.5 h-3.5 text-blue-500 animate-pulse" />
            <span>LLM 驱动自主量化因子演化平台</span>
            <span className="w-1.5 h-1.5 rounded-full bg-blue-400" />
            <span className="text-[11px] font-mono text-slate-500">QuantMind</span>
          </div>

          <h2 className="text-2xl sm:text-3xl font-black tracking-tight mb-2.5 bg-gradient-to-r from-slate-900 via-blue-900 to-indigo-900 bg-clip-text text-transparent">
            因子研究工作台
          </h2>
          <p className="text-xs sm:text-sm text-slate-500 font-medium leading-relaxed">
            用自然语言描述量化假设，AI 自动生成表达式、因子特征、样本内挖掘与进化回测
          </p>

          <div className="flex items-center gap-2 mt-2">
            {backendAvailable === true && (
              <span className="inline-flex items-center gap-1 text-[11px] font-bold text-emerald-600 bg-emerald-50 px-2.5 py-0.5 rounded-full border border-emerald-100">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-ping" />
                已连接后端服务
              </span>
            )}
            {backendAvailable === false && (
              <span className="inline-flex items-center gap-1 text-[11px] font-bold text-amber-600 bg-amber-50 px-2.5 py-0.5 rounded-full border border-amber-100">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                后端暂不可用 · 提交任务将失败
              </span>
            )}
          </div>
        </div>

        {/* ================= 2. Central Integrated Prompt Input ================= */}
        <div id="mining-input" className="w-full flex flex-col gap-3">
          {/* 运行中任务进度提示 + 提交锁（避免重复提交） */}
          {taskActive && (
            <div className="w-full max-w-4xl mx-auto flex items-center gap-3 rounded-xl border border-blue-100 bg-blue-50/70 px-4 py-2.5 shadow-2xs">
              {miningStarting ? (
                <Loader2 className="w-4 h-4 text-blue-500 animate-spin shrink-0" />
              ) : (
                <span className="relative flex h-2.5 w-2.5 shrink-0">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-blue-500" />
                </span>
              )}
              <span className="text-xs font-bold text-blue-700 whitespace-nowrap">
                {miningStarting ? '任务提交中...' : '任务运行中'}
              </span>
              <span
                className="text-xs text-blue-600/70 truncate flex-1 min-w-0"
                title={task?.progress?.message}
              >
                {task?.progress?.message || '正在启动因子挖掘，请稍候...'}
              </span>
              {!miningStarting && task && (
                <>
                  <div className="w-24 h-1.5 rounded-full bg-blue-100 overflow-hidden hidden sm:block">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-blue-500 to-indigo-500 transition-all duration-500"
                      style={{ width: `${Math.min(100, Math.max(0, task.progress?.progress ?? 0))}%` }}
                    />
                  </div>
                  <button
                    type="button"
                    onClick={() => onNavigate?.('mining_dashboard')}
                    className="text-xs font-bold text-blue-600 hover:text-blue-700 whitespace-nowrap cursor-pointer"
                  >
                    查看演化台
                  </button>
                </>
              )}
              <button
                type="button"
                onClick={stopMining}
                className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-bold text-red-600 bg-white hover:bg-red-50 border border-red-100 transition-colors whitespace-nowrap cursor-pointer"
                title="停止当前任务"
              >
                <Square className="w-3 h-3" />
                停止
              </button>
            </div>
          )}

          {miningSubmissionPending && <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
            <p>上次挖掘提交结果尚未确认。确认操作使用原参数找回任务；若尚未入队，则以原提交继续申请。</p>
            <button type="button" disabled={miningStarting} className="mt-2 rounded border border-amber-300 px-3 py-1 disabled:opacity-50" onClick={retryMiningSubmission}>{miningStarting ? '确认中…' : '确认原提交'}</button>
          </div>}
          <ChatInput
            inline={true}
            initialPrompt={activePrompt}
            onPreview={previewMining}
            onResearchDirections={setResearchConfig}
            onSampleDirections={(config, quotaSupported) => { setSampleConfig(config); setSampleQuotaSupported(quotaSupported); }}
            onResearchExperience={(config) => setExperienceUniverse(config.universe || 'all_a')}
            onVariantLedger={(config) => setVariantUniverse(config.universe || 'all_a')}
            onSubmit={startMining}
            onStop={stopMining}
            isRunning={taskActive}
          />

          {/* Quick Starter Prompts */}
          <div className="mx-auto mt-2 w-full max-w-4xl">
            <div className="mb-3 flex items-center gap-2 px-1 text-xs text-slate-400">
              <Zap className="h-3.5 w-3.5 text-amber-500" />
              <span className="font-medium text-slate-500">从一个研究方向开始</span>
              <span className="hidden sm:inline">点击填入，继续完善你的假设</span>
            </div>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,22rem),1fr))] gap-3">
              {PRESET_PROMPTS.map((promptText, idx) => (
                <button
                  key={promptText}
                  type="button"
                  disabled={taskActive}
                  onClick={() => setActivePrompt(promptText)}
                  className="group flex items-center gap-3 rounded-xl border border-slate-200/80 bg-white/60 px-4 py-3 text-left transition-colors hover:border-blue-200 hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <div className="min-w-0 flex-1">
                    <span className="text-xs font-semibold text-slate-700 group-hover:text-blue-600">{PRESET_LABELS[idx]}</span>
                    <p className="mt-1 text-xs leading-5 text-slate-500">{promptText}</p>
                  </div>
                  <ArrowRight className="h-3.5 w-3.5 shrink-0 self-center text-slate-300 transition-colors group-hover:text-blue-500" />
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* ================= 3. Major Feature Portals (3-Column Grid) ================= */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 w-full">
          {/* AI 因子挖掘 */}
          <div
            onClick={handleOpenDashboard}
            className="group relative bg-white/80 hover:bg-white backdrop-blur-xl rounded-2xl p-5 border border-white/90 shadow-xs hover:shadow-md transition-all cursor-pointer flex flex-col items-center text-center"
          >
            <div className="absolute top-4 left-4 w-10 h-10 rounded-xl bg-gradient-to-br from-blue-50 to-indigo-50 border border-blue-100 flex items-center justify-center text-blue-600 group-hover:scale-105 transition-transform">
              <Dna className="w-5 h-5" />
            </div>
            <span className="absolute top-4 right-4 text-[10px] font-bold text-blue-600 bg-blue-50 px-1.5 py-0.5 rounded">
              演化台
            </span>
            <div className="flex-1 flex flex-col justify-center items-center gap-3 pt-6">
              <h3 className="text-sm font-black text-slate-800 group-hover:text-blue-600 transition-colors m-0">
                AI 因子挖掘
              </h3>
              <p className="text-xs text-slate-500 font-normal leading-relaxed m-0">
                LLM 自动理解需求，生成因子假设并执行多代遗传算法演化
              </p>
            </div>
            <div className="w-full pt-3 border-t border-slate-100 flex items-center justify-center gap-2 text-[11px] font-bold text-blue-600 group-hover:translate-x-0.5 transition-transform">
              <span>进入实时演化台</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </div>
          </div>

          {/* 因子库管理 */}
          <div
            onClick={() => onNavigate?.('library')}
            className="group relative bg-white/80 hover:bg-white backdrop-blur-xl rounded-2xl p-5 border border-white/90 shadow-xs hover:shadow-md transition-all cursor-pointer flex flex-col items-center text-center"
          >
            <div className="absolute top-4 left-4 w-10 h-10 rounded-xl bg-gradient-to-br from-emerald-50 to-teal-50 border border-emerald-100 flex items-center justify-center text-emerald-600 group-hover:scale-105 transition-transform">
              <Database className="w-5 h-5" />
            </div>
            <span className="absolute top-4 right-4 text-[10px] font-bold text-emerald-600 bg-emerald-50 px-1.5 py-0.5 rounded">
              全量库
            </span>
            <div className="flex-1 flex flex-col justify-center items-center gap-3 pt-6">
              <h3 className="text-sm font-black text-slate-800 group-hover:text-emerald-600 transition-colors m-0">
                因子库管理
              </h3>
              <p className="text-xs text-slate-500 font-normal leading-relaxed m-0">
                浏览、筛选、分析已挖掘的所有因子及其 IC/IR 与多空收益单调性
              </p>
            </div>
            <div className="w-full pt-3 border-t border-slate-100 flex items-center justify-center gap-2 text-[11px] font-bold text-emerald-600 group-hover:translate-x-0.5 transition-transform">
              <span>查看因子资产库</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </div>
          </div>

          {/* 独立回测 */}
          <div
            onClick={() => onNavigate?.('backtest')}
            className="group relative bg-white/80 hover:bg-white backdrop-blur-xl rounded-2xl p-5 border border-white/90 shadow-xs hover:shadow-md transition-all cursor-pointer flex flex-col items-center text-center"
          >
            <div className="absolute top-4 left-4 w-10 h-10 rounded-xl bg-gradient-to-br from-purple-50 to-pink-50 border border-purple-100 flex items-center justify-center text-purple-600 group-hover:scale-105 transition-transform">
              <BarChart3 className="w-5 h-5" />
            </div>
            <span className="absolute top-4 right-4 text-[10px] font-bold text-purple-600 bg-purple-50 px-1.5 py-0.5 rounded">
              单因子日频
            </span>
            <div className="flex-1 flex flex-col justify-center items-center gap-3 pt-6">
              <h3 className="text-sm font-black text-slate-800 group-hover:text-purple-600 transition-colors m-0">
                因子回测
              </h3>
              <p className="text-xs text-slate-500 font-normal leading-relaxed m-0">
                选择本人因子执行 A 股日频回测，查看服务端返回的任务状态与指标
              </p>
            </div>
            <div className="w-full pt-3 border-t border-slate-100 flex items-center justify-center gap-2 text-[11px] font-bold text-purple-600 group-hover:translate-x-0.5 transition-transform">
              <span>启动独立回测</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </div>
          </div>
        </div>

        {/* ================= 4. System Specifications Bento Cards (4-Column Grid) ================= */}
        <div className="w-full bg-white/80 backdrop-blur-xl rounded-2xl p-5 border border-white/90 shadow-xs">
          <div className="flex items-center justify-between pb-3 border-b border-slate-100 mb-3.5">
            <span className="text-xs font-black text-slate-800 flex items-center gap-1.5">
              <Shield className="w-4 h-4 text-blue-600" />
              系统投研环境与数据规格
            </span>
            <span className="text-[11px] font-mono text-slate-400">
              {backendAvailable === true ? '后端服务 · 可用' : backendAvailable === false ? '后端服务 · 暂不可用' : '后端服务 · 检查中'}
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5 text-xs">
            {/* 股票池 */}
            <div className="p-3 bg-slate-50/80 rounded-xl border border-slate-100 flex flex-col justify-center items-center text-center gap-2">
              <div>
                <span className="text-[10px] font-bold text-slate-400 flex items-center justify-center gap-1 mb-1">
                  <Layers className="w-3 h-3 text-blue-500" /> 股票池覆盖
                </span>
                <span className="text-xs font-bold text-slate-800 block">
                  {universeCount > 0 ? `${universeCount} 个 A 股可选池` : 'A 股系统股票池'}
                </span>
              </div>
              <span className="text-[10px] text-slate-500 mt-2">行情与股票池均使用 A 股数据</span>
            </div>

            {/* 基础因子集 */}
            <div className="p-3 bg-slate-50/80 rounded-xl border border-slate-100 flex flex-col justify-center items-center text-center gap-2">
              <div>
                <span className="text-[10px] font-bold text-slate-400 flex items-center justify-center gap-1 mb-1">
                  <Database className="w-3 h-3 text-emerald-500" /> 挖掘数据集
                </span>
                <span className="text-xs font-bold text-slate-800 block">
                  {dataSummary?.datasetCount != null ? `${dataSummary.datasetCount} 个` : '数据概况暂不可用'}
                </span>
              </div>
              <span className="text-[10px] text-slate-500 mt-2">L1/L2 · 财务 · 技术指标等，QuantDB 云端直供</span>
            </div>

            {/* 数据时间范围 */}
            <div className="p-3 bg-slate-50/80 rounded-xl border border-slate-100 flex flex-col justify-center items-center text-center gap-2">
              <div>
                <span className="text-[10px] font-bold text-slate-400 flex items-center justify-center gap-1 mb-1">
                  <Activity className="w-3 h-3 text-indigo-500" /> 训练与验证跨度
                </span>
                <span className="text-xs font-bold text-slate-800 block font-mono">
                  {dateRangeText}
                </span>
              </div>
              <span className="text-[10px] text-slate-500 mt-2">回测区间与指标以任务记录为准</span>
            </div>

            {/* 算力与演化 */}
            <div className="p-3 bg-slate-50/80 rounded-xl border border-slate-100 flex flex-col justify-center items-center text-center gap-2">
              <div>
                <span className="text-[10px] font-bold text-slate-400 flex items-center justify-center gap-1 mb-1">
                  <Cpu className="w-3 h-3 text-purple-500" /> 任务执行
                </span>
                <span className="text-xs font-bold text-slate-800 block">
                  PG 租约 · 受控运行
                </span>
              </div>
              <span className="text-[10px] text-slate-500 mt-2">跨账号名额由服务端统一协调</span>
            </div>
          </div>
        </div>
      </div>
      {researchConfig && (
        <ResearchDirectionsDialog
          config={researchConfig}
          onClose={closeResearchDirections}
          onOpenTasks={() => { setResearchConfig(null); onNavigate?.('tasks'); }}
        />
      )}
      {sampleConfig && <SampledDirectionsDialog config={sampleConfig} quotaSupported={sampleQuotaSupported} onClose={closeSampleDirections} onOpenTasks={() => { setSampleConfig(null); onNavigate?.('tasks'); }} />}
      {experienceUniverse && <ResearchExperienceDialog universe={experienceUniverse} onClose={closeResearchExperience} />}
      {variantUniverse && <VariantLedgerDialog universe={variantUniverse} onClose={closeVariantLedger} />}
    </Layout>
  );
};
export default HomePage;
