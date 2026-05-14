/**
 * Qlib 专家模式回测组件
 * 左侧代码编辑，右侧结果输出。
 */

import React, { useState, useRef, useEffect } from 'react';
import Editor from '@monaco-editor/react';
import { Play, RefreshCw, Code2, Settings2, BarChart3, Info, AlertCircle, Upload, Cloud, CalendarRange } from 'lucide-react';

import type { BacktestConfig, BacktestResult } from '../../services/backtestService';
import { strategyManagementService } from '../../services/strategyManagementService';
import { QlibBacktestResult, QlibStrategyParams } from '../../types/backtest/qlib';
import { BACKTEST_CONFIG } from '../../config/backtest';
import { MultiStockCodeInput } from './MultiStockCodeInput';
import { authService } from '../../features/auth/services/authService';
import { normalizeUserId } from '../../features/strategy-wizard/utils/userId';
import { QlibResultDisplay, ErrorLogModal } from './QlibResultComponents';
import { blendBacktestProgress, getBacktestStageMessage } from './progressUtils';

const UNIVERSE_PRESETS = [
  { label: '全部', value: 'all' },
  { label: '沪深300', value: 'csi300' },
  { label: '中证500', value: 'csi500' },
  { label: '中证800', value: 'csi800' },
  { label: '中证1000', value: 'csi1000' },
];

export const QlibExpertBacktest: React.FC = () => {
  const stopPollingRef = useRef<(() => void) | null>(null);
  const progressTimerRef = useRef<number | null>(null);
  const progressRef = useRef<number>(0);
  const backendProgressRef = useRef<number>(0);
  const runStartedAtRef = useRef<number>(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [strategyCode, setStrategyCode] = useState<string>(DEFAULT_EXPERT_CODE);
  const [isSaving, setIsSaving] = useState(false);
  const [showSaveModal, setShowSaveModal] = useState(false);
  const [saveName, setSaveName] = useState('');
  const [saveDescription, setSaveDescription] = useState('由专家模式生成');

  // ... (状态定义保持不变)

  const handleImportFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const content = await strategyManagementService.readLocalFile(file);
      setStrategyCode(content);
    } catch (err: any) {
      setError(`导入失败: ${err.message}`);
    }
  };

  const handleOpenSaveModal = () => {
    if (!strategyCode.trim()) {
      setError('策略代码不能为空');
      return;
    }
    const now = new Date();
    const dateStr = `${(now.getMonth() + 1).toString().padStart(2, '0')}${now.getDate().toString().padStart(2, '0')}_${now.getHours().toString().padStart(2, '0')}${now.getMinutes().toString().padStart(2, '0')}`;
    setSaveName(`专家策略_${dateStr}`);
    setShowSaveModal(true);
  };

  const handleSaveToCloud = async () => {
    if (!saveName.trim()) {
      alert('请输入策略名称');
      return;
    }

    setIsSaving(true);
    try {
      // 1. 预检：从代码中提取结构化配置 (为支撑10万并发做准备)
      const extractedParams = await strategyManagementService.extractConfig(strategyCode);

      // 2. 正式保存
      await strategyManagementService.saveStrategy({
        name: saveName,
        code: strategyCode,
        description: saveDescription,
        source: 'personal',
        is_verified: false,
        is_qlib_format: true,
        language: 'qlib',
        tags: ['ExpertMode'],
        parameters: extractedParams
      });
      setShowSaveModal(false);
      alert('策略已成功保存至个人中心（已通过配置合规性验证）');
    } catch (err: any) {
      setError(`验证或保存失败: ${err.message}`);
    } finally {
      setIsSaving(false);
    }
  };

  // 基础配置
  const [universePath, setUniversePath] = useState<string>(UNIVERSE_PRESETS[0].value);
  const [startDate, setStartDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_START);
  const [endDate, setEndDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_END);
  const [initialCapital, setInitialCapital] = useState(1000000);
  const [benchmark, setBenchmark] = useState('SH000300');
  
  // 数据日期范围（从后端获取）
  const [dataMinDate, setDataMinDate] = useState<string | null>(null);
  const [dataMaxDate, setDataMaxDate] = useState<string | null>(null);

  const [isRunning, setIsRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressMessage, setProgressMessage] = useState('准备中...');
  const [result, setResult] = useState<BacktestResult | QlibBacktestResult | null>(null);
  const [error, setError] = useState('');
  const [fullTraceback, setFullTraceback] = useState('');
  const [lastBacktestId, setLastBacktestId] = useState('');
  const [showErrorLog, setShowErrorLog] = useState(false);

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

  const updateProgressMonotonic = (nextProgress: number, status?: string) => {
    const bounded = Math.min(99, Math.max(0, nextProgress));
    backendProgressRef.current = Math.max(backendProgressRef.current || 0, bounded);
    const merged = Math.max(progressRef.current || 0, bounded);
    progressRef.current = merged;
    setProgress(merged);
    setProgressMessage(
      getBacktestStageMessage(merged, backendProgressRef.current, status)
    );
  };

  const handleRun = async (overrideCode?: string) => {
    // 防御性处理：确保 codeToRun 始终是字符串，避免 React 事件对象干扰
    const codeToRun = (typeof overrideCode === 'string') ? overrideCode : strategyCode;

    if (!codeToRun || typeof codeToRun !== 'string' || !codeToRun.trim()) {
      setError('策略代码不能为空');
      return;
    }

    setIsRunning(true);
    setProgress(0);
    setProgressMessage('准备中...');
    setResult(null);
    setError('');
    startSimulatedProgress();

    try {
      const storedUser = authService.getStoredUser() as any;
      const resolvedUserId = storedUser?.id ?? storedUser?.user_id;
      if (!resolvedUserId) throw new Error('未登录或用户信息缺失');

      const config: BacktestConfig = {
        symbol: universePath,
        start_date: startDate,
        end_date: endDate,
        initial_capital: initialCapital,
        user_id: normalizeUserId(resolvedUserId),
        strategy_type: 'CustomStrategy',
        strategy_params: { topk: 50, n_drop: 5 }, // 专家模式默认参数
        benchmark_symbol: benchmark,
        strategy_code: codeToRun,
        commission: 0.00025,
      };

      const { backtestService } = await import('../../services/backtestService');
      const response = await backtestService.runBacktest(config);

      if (response.status === 'completed') {
        finishRun(response);
      } else if (response.status === 'failed') {
        failRun(response.error_message || '回测启动失败', response.backtest_id, response.full_error);
      } else {
        stopPollingRef.current = backtestService.pollStatus(response.backtest_id, {
          onProgress: (p, status) => {
            const normalized = p <= 1 ? p * 100 : p;
            updateProgressMonotonic(normalized, status);
          },
          onComplete: (final) => finishRun(final),
          onError: (err) => failRun(err.message, response.backtest_id, (err as any).traceback)
        });
      }
    } catch (err: any) {
      failRun(err.message);
    }
  };

  const finishRun = (res: BacktestResult | QlibBacktestResult) => {
    stopSimulatedProgress();
    setResult(res);
    setProgress(100);
    setProgressMessage('回测已完成');
    setIsRunning(false);
  };

  const failRun = (msg: string, backtestId?: string, traceback?: string) => {
    stopSimulatedProgress();
    setError(msg);
    setFullTraceback(traceback || '');
    setLastBacktestId(backtestId || '');
    setIsRunning(false);
    setProgress(0);
    setProgressMessage('回测失败');
  };

  useEffect(() => {
    return () => {
      stopSimulatedProgress();
      if (stopPollingRef.current) stopPollingRef.current();
    };
  }, []);

  // 获取 Qlib 数据日期范围
  useEffect(() => {
    const fetchDataRange = async () => {
      const { backtestService } = await import('../../services/backtestService');
      const result = await backtestService.getQlibDataRange();
      if (result.exists && result.min_date && result.max_date) {
        setDataMinDate(result.min_date);
        setDataMaxDate(result.max_date);
      }
    };
    fetchDataRange();
  }, []);

  return (
    <div className="flex h-full bg-white overflow-hidden rounded-2xl border border-gray-200 shadow-sm">
      {/* 左侧：代码编辑区 */}
      <div className="w-1/2 flex flex-col border-r border-gray-200">
        <div className="p-4 border-b border-gray-100 flex items-center justify-between bg-gray-50/50">
          <div className="flex items-center gap-2">
            <Code2 className="w-4 h-4 text-indigo-500" />
            <span className="text-sm font-bold text-gray-700">Python 策略编辑器</span>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => fileInputRef.current?.click()}
              className="text-[10px] bg-white border border-gray-200 px-2 py-1 rounded flex items-center gap-1 hover:bg-gray-50 transition-colors"
            >
              <Upload className="w-3 h-3" /> 导入文件
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".py,.txt"
              onChange={handleImportFile}
              className="hidden"
            />
            <button
              onClick={handleOpenSaveModal}
              disabled={isSaving}
              className="text-[10px] bg-indigo-50 text-indigo-600 border border-indigo-100 px-2 py-1 rounded flex items-center gap-1 hover:bg-indigo-100 transition-colors disabled:opacity-50"
            >
              <Cloud className="w-3 h-3" />
              保存到个人中心
            </button>
            <div className="text-[10px] text-gray-400 font-mono ml-1">ExpertMode</div>
          </div>
        </div>
        <div className="flex-1 relative">
          <Editor
            height="100%"
            defaultLanguage="python"
            theme="vs-dark"
            value={strategyCode}
            onChange={(val) => setStrategyCode(val || '')}
            options={{
              fontSize: 13,
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              automaticLayout: true,
              padding: { top: 10 }
            }}
          />
        </div>
      </div>

      {/* 右侧：结果与控制区 */}
      <div className="w-1/2 flex flex-col overflow-hidden bg-gray-50/30">
        {/* 控制面板 */}
        <div className="p-6 border-b border-gray-200 bg-white space-y-6">
          <div className="flex items-start gap-2 rounded-xl border border-indigo-100 bg-indigo-50/60 px-3 py-2 text-[11px] text-indigo-700">
            <Info className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>
              口径：前端显式参数优先，后端会自动补全与兼容修复；若要跨入口保持一致，请显式传入
              <code className="mx-1 rounded bg-white/70 px-1 py-0.5 font-mono text-[10px]">initial_capital</code>
              和
              <code className="mx-1 rounded bg-white/70 px-1 py-0.5 font-mono text-[10px]">deal_price</code>。
            </span>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-[10px] font-bold text-gray-400 uppercase mb-1">开始日期</label>
              <input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                min={dataMinDate || BACKTEST_CONFIG.QLIB.DATA_START}
                max={dataMaxDate || BACKTEST_CONFIG.QLIB.DATA_END}
                className="w-full px-3 py-2 bg-white border border-gray-200 rounded-xl text-xs text-gray-900 focus:ring-1 focus:ring-indigo-500 outline-none premium-date-picker"
                style={{ colorScheme: 'light' }}
              />
            </div>
            <div>
              <label className="block text-[10px] font-bold text-gray-400 uppercase mb-1">结束日期</label>
              <input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                min={dataMinDate || BACKTEST_CONFIG.QLIB.DATA_START}
                max={dataMaxDate || BACKTEST_CONFIG.QLIB.DATA_END}
                className="w-full px-3 py-2 bg-white border border-gray-200 rounded-xl text-xs text-gray-900 focus:ring-1 focus:ring-indigo-500 outline-none premium-date-picker"
                style={{ colorScheme: 'light' }}
              />
            </div>
          </div>
          
          {dataMinDate && dataMaxDate && (
            <div className="flex items-center gap-2 text-[10px] text-slate-500 bg-slate-50 px-3 py-1.5 rounded-lg">
              <CalendarRange className="w-3 h-3 text-indigo-500" />
              <span>数据有效期：</span>
              <span className="font-mono text-slate-700">{dataMinDate}</span>
              <span className="text-slate-400">~</span>
              <span className="font-mono text-slate-700">{dataMaxDate}</span>
            </div>
          )}

          <div>
            <label className="block text-[10px] font-bold text-gray-400 uppercase mb-2">股票池 (Symbols)</label>
            <div className="grid grid-cols-5 gap-2">
              {UNIVERSE_PRESETS.map((preset) => {
                const active = universePath === preset.value;
                return (
                  <button
                    key={preset.value}
                    type="button"
                    onClick={() => setUniversePath(preset.value)}
                    className={`px-2 py-2 text-[10px] font-bold rounded-xl border transition-all ${
                      active
                        ? 'bg-indigo-600 text-white border-indigo-600 shadow-sm'
                        : 'bg-white text-gray-600 border-gray-200 hover:border-indigo-300 hover:text-indigo-600'
                    }`}
                  >
                    {preset.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="flex items-center gap-4">
            <div className="flex-1">
              <label className="block text-[10px] font-bold text-gray-400 uppercase mb-1">初始资金</label>
              <input type="number" value={initialCapital} onChange={(e) => setInitialCapital(Number(e.target.value))} className="w-full px-3 py-2 bg-gray-50 border border-gray-200 rounded-xl text-xs focus:ring-1 focus:ring-indigo-500 outline-none" />
            </div>
            <button
              onClick={() => handleRun()}
              disabled={isRunning}
              className="mt-5 px-8 py-2 bg-indigo-600 text-white rounded-xl font-bold hover:bg-indigo-700 transition-all flex items-center gap-2 shadow-lg shadow-indigo-100 disabled:opacity-50"
            >
              {isRunning ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4 fill-current" />}
              执行代码
            </button>
          </div>

          {error && (
            <div className="p-3 bg-red-50 border border-red-100 rounded-xl flex items-center justify-between">
              <div className="flex items-center gap-2 text-red-600 text-xs font-medium truncate">
                <AlertCircle className="w-4 h-4" /> {error}
              </div>
              <button onClick={() => setShowErrorLog(true)} className="text-[10px] text-red-700 underline shrink-0">调试</button>
            </div>
          )}
        </div>

        {/* 结果显示 */}
        <div className="flex-1 overflow-y-auto p-6 custom-scrollbar">
          {isRunning ? (
            <div className="h-full flex flex-col items-center justify-center space-y-4">
              <div className="w-12 h-12 border-4 border-indigo-100 border-t-indigo-600 rounded-full animate-spin"></div>
              <div className="text-sm font-bold text-gray-500">{progressMessage} {progress.toFixed(0)}%</div>
            </div>
          ) : result ? (
            <QlibResultDisplay result={result} />
          ) : (
            <div className="h-full flex flex-col items-center justify-center text-gray-300 space-y-4">
              <BarChart3 className="w-16 h-16 opacity-10" />
              <p className="text-sm font-medium">等待代码执行结果</p>
            </div>
          )}
        </div>
      </div>
      {showErrorLog && (
        <ErrorLogModal
          error={error}
          traceback={fullTraceback}
          backtestId={lastBacktestId}
          onClose={() => setShowErrorLog(false)}
          onFixed={(repairedCode) => {
            if (repairedCode) {
              setStrategyCode(repairedCode);
            }
          }}
        />
      )}

      {/* 保存策略模态框 */}
      {showSaveModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[100] p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden animate-in fade-in zoom-in duration-200">
            <div className="p-6 border-b border-gray-100 flex items-center justify-between bg-indigo-50/30">
              <div className="flex items-center gap-2">
                <Cloud className="w-5 h-5 text-indigo-600" />
                <h3 className="font-bold text-gray-800">保存策略至个人中心</h3>
              </div>
              <button
                onClick={() => setShowSaveModal(false)}
                className="text-gray-400 hover:text-gray-600 transition-colors"
              >
                <RefreshCw className="w-4 h-4 rotate-45" />
              </button>
            </div>

            <div className="p-6 space-y-4">
              <div>
                <label className="block text-xs font-bold text-gray-400 uppercase mb-2">策略名称</label>
                <input
                  type="text"
                  value={saveName}
                  onChange={(e) => setSaveName(e.target.value)}
                  placeholder="请输入策略名称"
                  className="w-full px-4 py-3 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 focus:border-transparent outline-none transition-all"
                  autoFocus
                />
              </div>

              <div>
                <label className="block text-xs font-bold text-gray-400 uppercase mb-2">策略描述 (可选)</label>
              <textarea
                value={saveDescription}
                onChange={(e) => setSaveDescription(e.target.value)}
                placeholder="简单描述一下你的策略逻辑（标准口径）..."
                rows={3}
                className="w-full px-4 py-3 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 focus:border-transparent outline-none transition-all resize-none"
              />
              </div>

              <div className="pt-2 flex gap-3">
                <button
                  onClick={() => setShowSaveModal(false)}
                  className="flex-1 py-3 border border-gray-200 text-gray-600 rounded-xl font-bold hover:bg-gray-50 transition-all text-sm"
                >
                  取消
                </button>
                <button
                  onClick={handleSaveToCloud}
                  disabled={isSaving || !saveName.trim()}
                  className="flex-1 py-3 bg-indigo-600 text-white rounded-xl font-bold hover:bg-indigo-700 transition-all shadow-lg shadow-indigo-100 disabled:opacity-50 flex items-center justify-center gap-2 text-sm"
                >
                  {isSaving ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Cloud className="w-4 h-4" />
                  )}
                  确认保存
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

const DEFAULT_EXPERT_CODE = `"""
QUANTMIND QLIB 策略开发规范
===================================

口径说明：
- 当前系统是云端 AI-IDE + 回测中心的组合，不再要求固定函数名或固定模板结构。
- 专家模式支持 STRATEGY_CONFIG / get_strategy_config() / get_strategy_instance()。
- 前端显式参数优先，后端会做补全、修复与兼容适配。

1. 推荐策略结构
----------------
Qlib 引擎会优先解析 STRATEGY_CONFIG 或 get_strategy_config()。

核心参数：
- class: 策略实现类名（如 RedisTopkStrategy、RedisLongShortTopkStrategy）。
- module_path: Python 模块路径。
- kwargs: 传给策略构造函数的参数：
    - signal: 通常使用 "<PRED>"，表示平台默认生产模型预测分。
    - topk: 每期选股数量。
    - n_drop: 每期调仓数量，0 表示按全量调仓处理。
    - rebalance_days: 调仓周期（交易日）。
    - account_stop_loss: 账户级硬止损线。
    - max_leverage: 最大杠杆倍数。

2. 当前系统默认口径
--------------------
- 初始资金：专家模式 UI 默认 100 万；若走后端接口，请显式传入 initial_capital。
- 基准指数：默认 SH000300。
- 成交价格：后端默认 close；若要降低前视偏差，建议显式切换为 open。
- 交易参数：topk=50、n_drop=5、rebalance_days=3 是常见默认值，但前端显式输入优先。

3. 推荐模板
-----------
"""

from backend.services.engine.qlib_app.utils.extended_strategies import RedisTopkStrategy


def get_strategy_config():
    """入口函数：返回策略配置字典"""
    return {
        "class": "RedisTopkStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
        "kwargs": {
            "signal": "<PRED>",
            "topk": 50,
            "n_drop": 5,
            "rebalance_days": 3,
            "max_leverage": 1.0,
            "account_stop_loss": 0.1,
            "only_tradable": True,
        }
    }


STRATEGY_CONFIG = get_strategy_config()
`;
