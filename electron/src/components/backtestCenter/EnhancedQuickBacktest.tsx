/**
 * QuickBacktest 增强版 - 集成 Monaco Editor
 *
 * 新增功能：
 * - Monaco Editor 代码编辑器
 * - 策略模板选择
 * - 代码语法高亮
 * - 配置保存/加载
 * - WebSocket 实时进度
 */

import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import Editor from '@monaco-editor/react';
import {
  Play,
  Save,
  FolderOpen,
  FileCode,
  Settings as SettingsIcon,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  CheckCircle,
  BookOpen,
  History,
  Clock,
  TrendingUp
} from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import { useBacktestCenterStore } from '../../stores/backtestCenterStore';
import { backtestService } from '../../services/backtestService';
import { useWebSocket } from '../../utils/websocket';
import { strategyTemplates, getTemplateById } from '../../constants/strategyTemplates';
import { blendBacktestProgress, getBacktestStageMessage } from '../backtest/progressUtils';
import { getStoredTailTradeMode, setStoredTailTradeMode, getTailTradeDealPrice, getTailTradeSignalLagDays, ALLOW_FEATURE_SIGNAL_FALLBACK } from '../../shared/qlib/tailTradeMode';

const DEFAULT_TEMPLATE_ID = 'standard_topk';

export const EnhancedQuickBacktest: React.FC = () => {
  const progressTimerRef = useRef<number | null>(null);
  const progressRef = useRef<number>(0);
  const backendProgressRef = useRef<number>(0);
  const runStartedAtRef = useRef<number>(0);
  const {
    backtestConfig,
    updateBacktestConfig,
    backtestHistory,
    fetchHistory,
    selectedBacktests,
    toggleSelection,
  } = useBacktestCenterStore();

  const [strategyCode, setStrategyCode] = useState<string>('');
  const [isRunning, setIsRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressMessage, setProgressMessage] = useState('');
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string>('');
  const [showTemplates, setShowTemplates] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [savedConfigs, setSavedConfigs] = useState<any[]>([]);
  const [showHistory, setShowHistory] = useState(true);
  const [isComparing, setIsComparing] = useState(false);
  const [targetType, setTargetType] = useState<'single' | 'index'>('single');

  // 尾盘交易模式开关（持久化）
  const [tailTradeEnabled, setTailTradeEnabled] = useState<boolean>(() => getStoredTailTradeMode());
  const [showTailTradeTooltip, setShowTailTradeTooltip] = useState(false);
  const tailTradeTimerRef = useRef<number | null>(null);

  // 切换开关时同步缓存
  useEffect(() => {
    setStoredTailTradeMode(tailTradeEnabled);
  }, [tailTradeEnabled]);

  const backtestId = useRef<string>('');

  // 初始加载
  useEffect(() => {
    fetchHistory(backtestConfig.user_id || 'default_user');
    loadSavedConfigs();
    
    // 检查是否有从策略管理中心传递过来的策略ID
    const pendingId = localStorage.getItem('selected_backtest_strategy_id');
    if (pendingId) {
      localStorage.removeItem('selected_backtest_strategy_id');
      loadPendingStrategy(pendingId);
    } else {
      const preferredTemplate =
        getTemplateById(DEFAULT_TEMPLATE_ID) || strategyTemplates[0];
      if (preferredTemplate) {
        setStrategyCode(preferredTemplate.code);
      }
    }
  }, []);

  const loadPendingStrategy = async (id: string) => {
    try {
      const { strategyManagementService } = await import('../../services/strategyManagementService');
      const strategy = await strategyManagementService.getStrategy(id);
      if (strategy) {
        setStrategyCode(strategy.code);
        updateBacktestConfig({
          strategy_type: strategy.id,
          strategy_params: strategy.execution_config?.parameters || {},
        });
      }
    } catch (err) {
      console.error('Failed to load pending strategy:', err);
    }
  };

  const loadSavedConfigs = () => {
    const saved = localStorage.getItem('backtest_configs');
    if (saved) {
      setSavedConfigs(JSON.parse(saved));
    }
  };

  const stopSimulatedProgress = () => {
    if (progressTimerRef.current != null) {
      window.clearInterval(progressTimerRef.current);
      progressTimerRef.current = null;
    }
  };

  const startSimulatedProgress = () => {
    stopSimulatedProgress();
    runStartedAtRef.current = Date.now();
    progressRef.current = 3;
    backendProgressRef.current = 0;
    setProgress(3);
    setProgressMessage('正在准备回测任务...');
    progressTimerRef.current = window.setInterval(() => {
      const next = blendBacktestProgress(
        progressRef.current || 0,
        backendProgressRef.current || 0,
        Math.max(0, Date.now() - runStartedAtRef.current)
      );
      progressRef.current = next;
      setProgress(next);
      setProgressMessage(
        getBacktestStageMessage(next, backendProgressRef.current || 0)
      );
    }, 800);
  };

  const updateProgressMonotonic = (nextProgress: number, status?: string, msg?: string) => {
    const bounded = Math.min(99, Math.max(0, nextProgress));
    backendProgressRef.current = Math.max(backendProgressRef.current || 0, bounded);
    const merged = Math.max(progressRef.current || 0, bounded);
    progressRef.current = merged;
    setProgress(merged);
    setProgressMessage(
      getBacktestStageMessage(merged, backendProgressRef.current, status, msg)
    );
  };

  // 获取 WebSocket 地址逻辑优化
  const getFullWsUrl = () => {
    if (!backtestId.current) return '';
    // 从 backtestService 获取基准 WS 地址
    const baseWs = (backtestService as any).wsUrl || '';
    // 确保 path 正确：如果是云端 wss://api.quantmind.cloud/ws，则拼接 /backtest/{id}
    // 如果是开发环境已包含 /api/v1，则继续拼接
    const separator = baseWs.endsWith('/') ? '' : '/';
    return `${baseWs}${separator}backtest/${backtestId.current}`;
  };

  const { isConnected } = useWebSocket(
    getFullWsUrl(),
    {
      enabled: isRunning && !!backtestId.current,
      onMessage: (msg) => {
        if (msg.type === 'progress') {
          const normalized = (msg.data.progress || 0) * 100;
          updateProgressMonotonic(normalized, msg.data.status || 'running', msg.data.message);
        } else if (msg.type === 'completed') {
          stopSimulatedProgress();
          setProgress(100);
          setProgressMessage('回测已完成');
          setIsRunning(false);
          handleLoadResult(backtestId.current);
        } else if (msg.type === 'error') {
          stopSimulatedProgress();
          setError(msg.data.message || '回测失败');
          setIsRunning(false);
        }
      },
    }
  );

  const handleSaveConfig = () => {
    const configName = window.prompt('请输入配置名称：');
    if (!configName) return;
    const newConfig = {
      id: Date.now().toString(),
      name: configName,
      config: { ...backtestConfig, strategy_code: strategyCode },
      created_at: new Date().toISOString(),
    };
    const updated = [...savedConfigs, newConfig];
    setSavedConfigs(updated);
    localStorage.setItem('backtest_configs', JSON.stringify(updated));
  };

  const handleLoadConfig = (configId: string) => {
    const config = savedConfigs.find(c => c.id === configId);
    if (config) {
      updateBacktestConfig(config.config);
      setStrategyCode(config.config.strategy_code || '');
    }
  };

  const handleDeleteConfig = (configId: string) => {
    if (!window.confirm('确定要删除这个配置吗？')) return;
    const updated = savedConfigs.filter(c => c.id !== configId);
    setSavedConfigs(updated);
    localStorage.setItem('backtest_configs', JSON.stringify(updated));
  };

  const handleSelectTemplate = (templateId: string) => {
    const template = getTemplateById(templateId);
    if (template) {
      setStrategyCode(template.code);
      setShowTemplates(false);
    }
  };

  const handleRunBacktest = async () => {
    if (!strategyCode.trim() || !backtestConfig.symbol) {
      setError('请输入策略代码和标的代码');
      return;
    }
    setIsRunning(true);
    setError('');
    setResult(null);
    setProgress(0);
    setProgressMessage('准备中...');
    startSimulatedProgress();
    try {
      const response = await backtestService.runBacktest({
        ...backtestConfig,
        symbol: backtestConfig.symbol || '',
        strategy_code: strategyCode,
        initial_capital: backtestConfig.initial_capital || 100000,
        user_id: backtestConfig.user_id || 'default',
        deal_price: getTailTradeDealPrice(tailTradeEnabled),
        signal_lag_days: getTailTradeSignalLagDays(tailTradeEnabled),
        allow_feature_signal_fallback: ALLOW_FEATURE_SIGNAL_FALLBACK,
      } as any);
      backtestId.current = response.backtest_id;
      if (!isConnected) {
        backtestService.pollStatus(response.backtest_id, {
          onProgress: (prog, status, msg) => {
            const normalized = prog <= 1 ? prog * 100 : prog;
            updateProgressMonotonic(normalized, status, msg);
          },
          onComplete: (final) => {
            stopSimulatedProgress();
            setProgress(100);
            setProgressMessage('回测已完成');
            handleLoadResult(final.backtest_id);
          },
          onError: (err) => {
            stopSimulatedProgress();
            setError(err.message || '回测失败');
            setProgressMessage('回测失败');
            setIsRunning(false);
          },
        });
      }
    } catch (err: any) {
      stopSimulatedProgress();
      setError(err.message || '回测失败');
      setIsRunning(false);
    }
  };

  const handleLoadResult = async (id: string) => {
    try {
      const data = await backtestService.getResult(id);
      setResult(data);
      setProgress(100);
      setProgressMessage('回测已完成');
      fetchHistory(backtestConfig.user_id || 'default_user');
    } catch (err: any) {
      setError('加载结果失败');
    } finally {
      stopSimulatedProgress();
      setIsRunning(false);
    }
  };

  useEffect(() => {
    return () => {
      stopSimulatedProgress();
      if (tailTradeTimerRef.current) clearTimeout(tailTradeTimerRef.current);
    };
  }, []);

  return (
    <div className="flex h-full bg-gray-50 overflow-hidden relative">
      {/* 历史侧边栏 */}
      <AnimatePresence>
        {showHistory && (
          <motion.div
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 280, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            className="h-full bg-white border-r border-gray-200 flex flex-col shadow-sm z-20"
          >
            <div className="p-4 border-b border-gray-100 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <History className="w-5 h-5 text-gray-600" />
                <h3 className="font-bold text-gray-800">最近回测</h3>
              </div>
              <button onClick={() => setShowHistory(false)} className="p-1 hover:bg-gray-100 rounded-lg text-gray-400">
                <ChevronDown className="w-4 h-4 rotate-90" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto custom-scrollbar p-2 space-y-2">
              {backtestHistory.length === 0 ? (
                <div className="text-center py-10 text-gray-400 text-sm">暂无历史记录</div>
              ) : (
                backtestHistory.map((item) => (
                  <div key={item.backtest_id} className="relative group">
                    <button
                      onClick={() => { setIsComparing(false); handleLoadResult(item.backtest_id); }}
                      className={`w-full text-left p-3 rounded-2xl border transition-all ${result?.backtest_id === item.backtest_id ? 'bg-blue-50 border-blue-200' : 'bg-white border-gray-100'
                        }`}
                    >
                      <div className="flex justify-between items-start mb-1">
                        <span className="text-xs font-mono text-blue-600">{item.backtest_id.substring(0, 8)}</span>
                        <span className="text-[10px] text-gray-400">{new Date(item.created_at).toLocaleDateString()}</span>
                      </div>
                      <div className="text-sm font-bold text-gray-800 truncate">{item.config?.symbol || '未知品种'}</div>
                    </button>
                    <div className="absolute top-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity">
                      <input type="checkbox" checked={selectedBacktests.includes(item.backtest_id)} onChange={() => toggleSelection(item.backtest_id)} className="w-4 h-4" />
                    </div>
                  </div>
                ))
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {!showHistory && (
        <button onClick={() => setShowHistory(true)} className="absolute left-0 top-1/2 -translate-y-1/2 bg-white border border-gray-200 p-1 rounded-r-xl shadow-md z-30">
          <ChevronDown className="w-4 h-4 -rotate-90" />
        </button>
      )}

      {/* 主工作区 */}
      <div className="flex-1 flex gap-6 p-6 overflow-hidden">
        {/* 左侧配置 */}
        <div className="w-[450px] flex flex-col gap-4 overflow-y-auto custom-scrollbar pr-2">
          <ConfigSection title="基本配置" icon={SettingsIcon}>
            <div className="space-y-3">
              <div className="space-y-1">
                <label className="text-sm font-medium text-gray-700">股票池 (Symbols)</label>
                <div className="grid grid-cols-5 gap-1.5">
                  {[
                    { label: '全部', value: 'all' },
                    { label: 'HS300', value: 'csi300' },
                    { label: 'ZZ500', value: 'csi500' },
                    { label: 'ZZ800', value: 'csi800' },
                    { label: 'ZZ1000', value: 'csi1000' },
                  ].map((preset) => {
                    const active = backtestConfig.symbol === preset.value;
                    return (
                      <button
                        key={preset.value}
                        type="button"
                        onClick={() => updateBacktestConfig({ symbol: preset.value })}
                        className={`px-1 py-2 text-[10px] font-bold rounded-xl border transition-all ${
                          active
                            ? 'bg-blue-600 text-white border-blue-600 shadow-sm'
                            : 'bg-white text-gray-600 border-gray-200 hover:border-blue-300 hover:text-blue-600'
                        }`}
                      >
                        {preset.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <InputField label="开始日期" type="date" value={backtestConfig.start_date || ''} onChange={(v) => updateBacktestConfig({ start_date: v })} />
                <InputField label="结束日期" type="date" value={backtestConfig.end_date || ''} onChange={(v) => updateBacktestConfig({ end_date: v })} />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <InputField label="初始资金" type="number" value={backtestConfig.initial_capital?.toString() || '1000000'} onChange={(v) => updateBacktestConfig({ initial_capital: parseFloat(v) })} />
                <div className="space-y-1">
                  <label className="text-sm font-medium text-gray-700">调仓周期</label>
                  <select
                    value={backtestConfig.strategy_params?.rebalance_days || 5}
                    onChange={(e) => updateBacktestConfig({
                      strategy_params: { ...backtestConfig.strategy_params, rebalance_days: Number(e.target.value) }
                    })}
                    className="w-full px-3 py-2 bg-white border border-gray-300 rounded-2xl text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                  >
                    <option value={1}>每1天 (1 Day)</option>
                    <option value={3}>每3天 (3 Days)</option>
                    <option value={5}>每5天 (5 Days)</option>
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label className="text-sm font-medium text-gray-700">基准指数</label>
                  <select
                    value={backtestConfig.benchmark_symbol || 'SH000300'}
                    onChange={(e) => updateBacktestConfig({ benchmark_symbol: e.target.value })}
                    className="w-full px-3 py-2 bg-white border border-gray-300 rounded-2xl text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                  >
                    <option value="SH000300">沪深300</option>
                    <option value="SH000905">中证500</option>
                    <option value="SH000852">中证1000</option>
                    <option value="SZ399006">创业板指</option>
                  </select>
                </div>
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <label className="text-sm font-medium text-gray-700">基准价格</label>
                    {/* 尾盘交易模式开关 */}
                    <div
                      className="relative"
                      onMouseEnter={() => {
                        tailTradeTimerRef.current = window.setTimeout(() => setShowTailTradeTooltip(true), 1000);
                      }}
                      onMouseLeave={() => {
                        if (tailTradeTimerRef.current) { clearTimeout(tailTradeTimerRef.current); tailTradeTimerRef.current = null; }
                        setShowTailTradeTooltip(false);
                      }}
                    >
                      <button
                        type="button"
                        onClick={() => setTailTradeEnabled(!tailTradeEnabled)}
                        className={`relative inline-flex items-center h-5 w-9 rounded-full transition-colors duration-200 focus:outline-none ${
                          tailTradeEnabled ? 'bg-blue-600' : 'bg-gray-300'
                        }`}
                      >
                        <span
                          className={`inline-block w-3.5 h-3.5 rounded-full bg-white shadow transform transition-transform duration-200 ${
                            tailTradeEnabled ? 'translate-x-[18px]' : 'translate-x-[3px]'
                          }`}
                        />
                      </button>
                      {showTailTradeTooltip && (
                        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2.5 py-1.5 bg-gray-900 text-white text-[11px] rounded-lg whitespace-nowrap z-50 shadow-lg">
                          {tailTradeEnabled
                            ? '尾盘交易：当日预测+收盘成交'
                            : '次日生效：T+1预测+开盘成交'}
                          <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
                        </div>
                      )}
                    </div>
                    <span className="text-[11px] text-gray-400 ml-auto">
                      {tailTradeEnabled ? '尾盘' : '次日'}
                    </span>
                  </div>
                  <select
                    value={getTailTradeDealPrice(tailTradeEnabled)}
                    onChange={(e) => updateBacktestConfig({ deal_price: e.target.value as any })}
                    disabled={tailTradeEnabled}
                    className={`w-full px-3 py-2 bg-white border border-gray-300 rounded-2xl text-sm focus:ring-2 focus:ring-blue-500 outline-none ${
                      tailTradeEnabled ? 'text-gray-400 cursor-not-allowed bg-gray-100' : ''
                    }`}
                  >
                    <option value="open">开盘价 (Open)</option>
                    <option value="close">收盘价 (Close)</option>
                  </select>
                </div>
              </div>
            </div>
          </ConfigSection>

          <ConfigSection title="策略代码" icon={FileCode}>
            <div className="space-y-3">
              <div className="bg-blue-50 border border-blue-200 rounded-2xl p-3 text-xs text-blue-700 flex gap-2 items-start">
                <AlertCircle className="w-4 h-4 flex-shrink-0" />
                <p>当前使用 Qlib 回测引擎，建议使用预设模板。点击“开始回测”将代码提交至计算集群。</p>
              </div>
              <div className="flex gap-2">
                <button onClick={() => setShowTemplates(!showTemplates)} className="flex-1 py-2 bg-purple-100 text-purple-700 rounded-2xl text-sm font-medium flex items-center justify-center gap-2">
                  <BookOpen className="w-4 h-4" /> 策略模板
                </button>
                <button onClick={handleSaveConfig} className="px-4 py-2 bg-blue-100 text-blue-700 rounded-2xl text-sm font-medium flex items-center justify-center gap-2">
                  <Save className="w-4 h-4" /> 保存
                </button>
              </div>
              <AnimatePresence>
                {showTemplates && (
                  <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="space-y-2 max-h-60 overflow-y-auto custom-scrollbar p-2 bg-gray-50 rounded-2xl border border-gray-200">
                    {strategyTemplates.map((t) => (
                      <button key={t.id} onClick={() => handleSelectTemplate(t.id)} className="w-full text-left p-3 bg-white hover:bg-blue-50 rounded-xl transition-colors border border-gray-200 mb-2">
                        <div className="font-bold text-gray-800 text-sm">{t.name}</div>
                        <div className="text-[10px] text-gray-500 mt-1">{t.description}</div>
                      </button>
                    ))}
                  </motion.div>
                )}
              </AnimatePresence>
              <div className="border border-gray-300 rounded-2xl overflow-hidden shadow-sm h-[300px]">
                <Editor height="100%" defaultLanguage="python" value={strategyCode} onChange={(v) => setStrategyCode(v || '')} theme="vs-light" options={{ minimap: { enabled: false }, fontSize: 12, lineNumbers: 'on', automaticLayout: true }} />
              </div>
            </div>
          </ConfigSection>

          <button onClick={handleRunBacktest} disabled={isRunning} className="w-full py-4 bg-gradient-to-r from-blue-500 to-indigo-600 hover:from-blue-600 hover:to-indigo-700 text-white rounded-2xl font-bold shadow-lg disabled:opacity-50 flex items-center justify-center gap-2">
            {isRunning ? <RefreshCwIcon className="w-5 h-5 animate-spin" /> : <Play className="w-5 h-5" />}
            {isRunning ? `运行中 ${progress.toFixed(0)}%` : '开始回测'}
          </button>

          {isRunning && (
            <div className="space-y-1.5 px-1">
              <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
                <motion.div className="h-full bg-blue-500" initial={{ width: 0 }} animate={{ width: `${progress}%` }} />
              </div>
              <div className="flex justify-between text-[10px] text-gray-400 font-medium">
                <span>{progressMessage || '准备中...'}</span>
                <span>{progress.toFixed(0)}%</span>
              </div>
            </div>
          )}

          {error && (
            <div className="p-3 bg-red-50 border border-red-100 rounded-2xl text-red-600 text-xs flex gap-2 items-center">
              <AlertCircle className="w-4 h-4" /> {error}
            </div>
          )}
        </div>

        {/* 右侧展示 */}
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          {isComparing ? (
            <div className="h-full flex items-center justify-center bg-white rounded-3xl border border-gray-200">
              <p className="text-gray-400">对比功能正在升级中...</p>
            </div>
          ) : result ? (
            <ResultsPanel result={result} />
          ) : (
            <EmptyState />
          )}
        </div>
      </div>
    </div>
  );
};

const RefreshCwIcon = ({ className }: { className?: string }) => (
  <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" /><path d="M21 3v5h-5" /></svg>
);

const ConfigSection: React.FC<{ title: string; icon: any; children: React.ReactNode }> = ({ title, icon: Icon, children }) => (
  <div className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm">
    <div className="flex items-center gap-2 mb-3">
      <Icon className="w-5 h-5 text-gray-600" />
      <h3 className="font-medium text-gray-800">{title}</h3>
    </div>
    {children}
  </div>
);

const InputField: React.FC<{ label: string; value: string; onChange: (v: string) => void; type?: string; placeholder?: string }> = ({ label, value, onChange, type = 'text', placeholder }) => (
  <div className="space-y-1">
    <label className="text-sm font-medium text-gray-700">{label}</label>
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className={`w-full px-3 py-2 border rounded-2xl text-sm outline-none transition-all ${type === 'date'
        ? 'bg-white border-gray-300 text-gray-900 focus:ring-2 focus:ring-blue-500 premium-date-picker'
        : 'bg-white border-gray-300 text-gray-900 focus:ring-2 focus:ring-blue-500'
        }`}
      style={type === 'date' ? { colorScheme: 'light' } : {}}
    />
  </div>
);

const EmptyState: React.FC = () => (
  <div className="h-full flex items-center justify-center">
    <div className="text-center">
      <FileCode className="w-16 h-16 text-gray-300 mx-auto mb-4" />
      <p className="text-gray-500 font-medium">还没有回测结果</p>
      <p className="text-xs text-gray-400 mt-1">配置参数并运行回测以查看结果</p>
    </div>
  </div>
);

const ResultsPanel: React.FC<{ result: any }> = ({ result }) => {
  const metrics = result.metrics || {};
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-3 gap-4">
        <MetricCard label="总收益率" value={(metrics.total_return * 100).toFixed(2) + '%'} color={metrics.total_return >= 0 ? 'green' : 'red'} icon={TrendingUp} />
        <MetricCard label="年化收益" value={(metrics.annual_return * 100).toFixed(2) + '%'} color={metrics.annual_return >= 0 ? 'green' : 'red'} icon={TrendingUp} />
        <MetricCard label="夏普比率" value={metrics.sharpe_ratio?.toFixed(2) || 'N/A'} color="blue" icon={CheckCircle} />
      </div>
      {result.equity_curve && (
        <div className="bg-white rounded-3xl border border-gray-200 p-6 shadow-sm">
          <h3 className="text-lg font-bold text-gray-800 mb-4">权益曲线</h3>
          <EquityChart data={result.equity_curve} />
        </div>
      )}
    </div>
  );
};

const MetricCard: React.FC<{ label: string; value: string; color: string; icon: any }> = ({ label, value, color, icon: Icon }) => {
  const colors: any = {
    green: 'bg-green-50 text-green-600 border-green-100',
    red: 'bg-red-50 text-red-600 border-red-100',
    blue: 'bg-blue-50 text-blue-600 border-blue-100',
  };
  return (
    <div className={`p-4 rounded-2xl border ${colors[color] || 'bg-gray-50 text-gray-600 border-gray-100 shadow-sm'}`}>
      <div className="flex justify-between items-center mb-1">
        <span className="text-xs font-medium opacity-80">{label}</span>
        <Icon className="w-4 h-4 opacity-60" />
      </div>
      <div className="text-xl font-bold">{value}</div>
    </div>
  );
};

const EquityChart: React.FC<{ data: any }> = ({ data }) => {
  const option = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: data?.dates || [], axisLabel: { color: '#9ca3af', fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { color: '#9ca3af', fontSize: 10 }, splitLine: { lineStyle: { type: 'dashed', color: '#f3f4f6' } } },
    series: [{ name: '权益', type: 'line', data: data?.values || [], smooth: true, itemStyle: { color: '#3b82f6' }, areaStyle: { color: 'rgba(59, 130, 246, 0.1)' } }],
    grid: { top: 20, bottom: 20, left: 40, right: 20 }
  };
  return <ReactECharts option={option} style={{ height: '300px' }} />;
};
