import React, { useEffect, useState } from 'react';
import {
  Sparkles, Bot, Database, BarChart3, ArrowRight, Zap,
  Layers, CheckCircle2, TrendingUp, Shield, Activity, Cpu,
  Loader2, Square
} from 'lucide-react';
import { ChatInput } from '../components-v2/ChatInput';
import { Layout } from '../components-v2/layout/Layout';
import type { PageId } from '../components-v2/layout/Layout';
import { useTaskContext } from '../context-v2/TaskContext';
import { getDataSummary } from '../services-v2/api';
import type { DataSummary } from '../types-v2';

interface HomePageProps {
  onNavigate?: (page: PageId) => void;
}

const PRESET_PROMPTS = [
  '挖掘基于 5 日动量反转与成交量偏度组合的超额收益因子',
  '构建捕捉日内高频波动率非对称性与价格跳跃的量价特征',
  '寻找尾盘主力资金净流入与换手率背离的Alpha选股因子',
  '基于多周期均线发散度与流动性溢价挖掘中短线稳健因子',
];

export const HomePage: React.FC<HomePageProps> = ({ onNavigate }) => {
  const {
    backendAvailable,
    miningTask: task,
    miningStarting,
    startMining,
    stopMining,
  } = useTaskContext();

  const [dataSummary, setDataSummary] = useState<DataSummary | null>(null);
  const [activePrompt, setActivePrompt] = useState('');

  // 任务激活 = 正在提交（POST /evolve 进行中）或后端任务运行中
  const taskActive = miningStarting || task?.status === 'running';

  useEffect(() => {
    getDataSummary()
      .then((res) => setDataSummary(res.data ?? null))
      .catch(() => {});
  }, []);

  const universeCount = dataSummary?.universes
    ? Object.keys(dataSummary.universes).length
    : 0;
  const dateRangeText = dataSummary?.dateRange?.start && dataSummary?.dateRange?.end
    ? `${dataSummary.dateRange.start} ~ ${dataSummary.dateRange.end}`
    : '2016-01-01 ~ 2021-12-31';
  const l1Columns = dataSummary?.datasets?.l1_factors?.columns ?? 101;
  const l1Categories = dataSummary?.datasets?.l1_factors?.categoryCount ?? 15;

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
            <span className="text-[11px] font-mono text-slate-500">AutoAlpha 2.0</span>
          </div>

          <h2 className="text-3xl sm:text-4xl font-black tracking-tight mb-2.5 bg-gradient-to-r from-slate-900 via-blue-900 to-indigo-900 bg-clip-text text-transparent">
            欢迎使用 QuantaAlpha
          </h2>
          <p className="text-sm sm:text-base text-slate-500 font-medium leading-relaxed">
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
                后端未连接 · 使用演示模式
              </span>
            )}
          </div>
        </div>

        {/* ================= 2. Central Integrated Prompt Input ================= */}
        <div className="w-full flex flex-col gap-3">
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

          <ChatInput
            inline={true}
            initialPrompt={activePrompt}
            onSubmit={startMining}
            onStop={stopMining}
            isRunning={taskActive}
          />

          {/* Quick Starter Prompts */}
          <div className="flex items-center gap-2 flex-wrap justify-center px-2">
            <span className="text-[11px] font-bold text-slate-400 flex items-center gap-1">
              <Zap className="w-3 h-3 text-amber-500" /> 推荐方向:
            </span>
            {PRESET_PROMPTS.map((promptText, idx) => (
              <button
                key={idx}
                type="button"
                onClick={() => setActivePrompt(promptText)}
                className="text-[11px] font-medium text-slate-600 hover:text-blue-600 bg-white/70 hover:bg-white border border-slate-200/80 hover:border-blue-300 rounded-full px-3 py-1 transition-all shadow-2xs hover:shadow-xs cursor-pointer truncate max-w-[340px]"
                title={promptText}
              >
                {promptText}
              </button>
            ))}
          </div>
        </div>

        {/* ================= 3. Major Feature Portals (3-Column Grid) ================= */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 w-full">
          {/* AI 因子挖掘 */}
          <div
            onClick={() => onNavigate?.('mining_dashboard')}
            className="group relative bg-white/80 hover:bg-white backdrop-blur-xl rounded-2xl p-5 border border-white/90 shadow-xs hover:shadow-md transition-all cursor-pointer flex flex-col items-center text-center"
          >
            <div className="absolute top-4 left-4 w-10 h-10 rounded-xl bg-gradient-to-br from-blue-50 to-indigo-50 border border-blue-100 flex items-center justify-center text-blue-600 group-hover:scale-105 transition-transform">
              <Bot className="w-5 h-5" />
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
              样本外验证
            </span>
            <div className="flex-1 flex flex-col justify-center items-center gap-3 pt-6">
              <h3 className="text-sm font-black text-slate-800 group-hover:text-purple-600 transition-colors m-0">
                全周期回测
              </h3>
              <p className="text-xs text-slate-500 font-normal leading-relaxed m-0">
                选择已生成的因子库执行全市场、全周期样本外回测评估
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
              Qlib Alpha Engine · Ready
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
                  {universeCount > 0 ? `5 大市场 · ${universeCount} 个 A 股可选池` : '沪深 300 / 中证 500 / 1000'}
                </span>
              </div>
              <span className="text-[10px] text-slate-500 mt-2">A 股、港股、美股、加密、期货</span>
            </div>

            {/* 基础因子集 */}
            <div className="p-3 bg-slate-50/80 rounded-xl border border-slate-100 flex flex-col justify-center items-center text-center gap-2">
              <div>
                <span className="text-[10px] font-bold text-slate-400 flex items-center justify-center gap-1 mb-1">
                  <Database className="w-3 h-3 text-emerald-500" /> 基础特征集
                </span>
                <span className="text-xs font-bold text-slate-800 block">
                  QuantDB L1 ({l1Columns} 维 / {l1Categories} 大类)
                </span>
              </div>
              <span className="text-[10px] text-slate-500 mt-2">动量/波动/流动性等 15 类多因子库</span>
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
              <span className="text-[10px] text-slate-500 mt-2">初步回测在样本外验证集执行</span>
            </div>

            {/* 算力与演化 */}
            <div className="p-3 bg-slate-50/80 rounded-xl border border-slate-100 flex flex-col justify-center items-center text-center gap-2">
              <div>
                <span className="text-[10px] font-bold text-slate-400 flex items-center justify-center gap-1 mb-1">
                  <Cpu className="w-3 h-3 text-purple-500" /> 并行演化架构
                </span>
                <span className="text-xs font-bold text-slate-800 block">
                  多轮迭代 · 多方向并行
                </span>
              </div>
              <span className="text-[10px] text-slate-500 mt-2">消耗与（进化轮次 × 方向数）成正比</span>
            </div>
          </div>
        </div>
      </div>
    </Layout>
  );
};
export default HomePage;
