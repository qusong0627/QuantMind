/**
 * Qlib专用快速回测组件（Native 模式）
 * 仅支持标准参数配置，追求极致简洁与稳定性。
 */

import React, { useState, useRef, useEffect } from 'react';
import {
  Play, RefreshCw, BarChart3, Settings2, Info, AlertCircle, Copy, Check, ExternalLink, CalendarRange
} from 'lucide-react';

import { backtestService, BacktestConfig } from '../../services/backtestService';
import { QlibBacktestResult, QlibStrategyParams, QlibStrategyType } from '../../types/backtest/qlib';
import { BACKTEST_CONFIG } from '../../config/backtest';
import { QlibStrategyConfigurator } from './QlibStrategyConfigurator';
import { StrategyPicker } from './StrategyPicker';
import { StrategyFile } from '../../types/backtest/strategy';
import { authService } from '../../features/auth/services/authService';
import { QlibResultDisplay, ErrorLogModal } from './QlibResultComponents';
import { useBacktestCenterStore } from '../../stores/backtestCenterStore';
import { normalizeUserId } from '../../features/strategy-wizard/utils/userId';
import { QLIB_REBALANCE_DAY_OPTIONS } from '../../shared/qlib/rebalance';
import { getTemplateById } from '../../data/qlibStrategyTemplates';
import { blendBacktestProgress, getBacktestStageMessage } from './progressUtils';
import { getDefaultStrategyParams, sanitizeStrategyParams } from '../../shared/qlib/strategyParams';
import { strategyManagementService } from '../../services/strategyManagementService';
import dayjs from 'dayjs';

const UNIVERSE_PRESETS = [
  { label: '全部', value: 'all' },
  { label: '沪深300', value: 'csi300' },
  { label: '中证500', value: 'csi500' },
  { label: '中证800', value: 'csi800' },
  { label: '中证1000', value: 'csi1000' },
];

const DEFAULT_TEMPLATE_ID = 'standard_topk';
const DEFAULT_TEMPLATE = getTemplateById(DEFAULT_TEMPLATE_ID);

export const QlibQuickBacktest: React.FC = () => {
  const stopPollingRef = useRef<(() => void) | null>(null);
  const progressTimerRef = useRef<number | null>(null);
  const progressRef = useRef<number>(0);
  const backendProgressRef = useRef<number>(0);
  const runStartedAtRef = useRef<number>(0);
  const backtestConfig = useBacktestCenterStore((state) => state.backtestConfig);

  // 策略相关状态
  const [strategyInfo, setStrategyInfo] = useState<StrategyFile | null>(null);

  // 基础配置
  const [universePath, setUniversePath] = useState<string>(UNIVERSE_PRESETS[0].value);
  const [startDate, setStartDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_START);
  const [endDate, setEndDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_END);
  const [initialCapital, setInitialCapital] = useState(1000000);
  const [benchmark, setBenchmark] = useState('SH000300');
  const [seed] = useState('');
  const [dealPrice, setDealPrice] = useState<'open' | 'close'>('close');
  
  // 数据日期范围（从后端获取）
  const [dataMinDate, setDataMinDate] = useState<string | null>(null);
  const [dataMaxDate, setDataMaxDate] = useState<string | null>(null);

  // 策略参数
  const [strategyType, setStrategyType] = useState<string>(DEFAULT_TEMPLATE_ID);
  const [strategyParams, setStrategyParams] = useState<QlibStrategyParams>(
    getDefaultStrategyParams(DEFAULT_TEMPLATE_ID)
  );

  useEffect(() => {
    // 检查是否有从策略管理中心传递过来的策略ID
    const pendingId = localStorage.getItem('selected_backtest_strategy_id');
    if (pendingId) {
      localStorage.removeItem('selected_backtest_strategy_id');
      loadPendingStrategy(pendingId);
    } else {
      if (!DEFAULT_TEMPLATE) {
        return;
      }
      setStrategyInfo({
        id: DEFAULT_TEMPLATE.id,
        name: DEFAULT_TEMPLATE.name,
        source: 'template',
        code: DEFAULT_TEMPLATE.code,
        description: DEFAULT_TEMPLATE.description,
        is_qlib_format: true,
        language: 'qlib',
      });
    }
  }, []);

  // 获取 Qlib 数据日期范围
  useEffect(() => {
    const fetchDataRange = async () => {
      const result = await backtestService.getQlibDataRange();
      if (result.exists && result.min_date && result.max_date) {
        setDataMinDate(result.min_date);
        setDataMaxDate(result.max_date);
      }
    };
    fetchDataRange();
  }, []);

  const loadPendingStrategy = async (id: string) => {
    try {
      const strategy = await strategyManagementService.getStrategy(id);
      if (strategy) {
        handleStrategySelected(strategy.code, strategy);
      }
    } catch (err) {
      console.error('Failed to load pending strategy:', err);
    }
  };

  const [isRunning, setIsRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressMessage, setProgressMessage] = useState('准备中...');
  const [result, setResult] = useState<QlibBacktestResult | null>(null);
  const [lastConfig, setLastConfig] = useState<BacktestConfig | null>(null);
  const [error, setError] = useState('');
  const [fullTraceback, setFullTraceback] = useState('');
  const [showErrorLog, setShowErrorLog] = useState(false);
  const [lastBacktestId, setLastBacktestId] = useState('');
  const [copied, setCopied] = useState(false);

  const handleCopyLog = () => {
    const textToCopy = `Error: ${error}\n\nTraceback:\n${fullTraceback}`;
    navigator.clipboard.writeText(textToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  type BacktestConfigExt = Partial<BacktestConfig> & {
    qlib_strategy_type?: string;
    qlib_strategy_params?: QlibStrategyParams;
  };
  const sharedConfig = backtestConfig as BacktestConfigExt;

  // 处理策略选择
  const handleStrategySelected = (
    _code: string,
    info?: StrategyFile,
    params?: QlibStrategyParams
  ) => {
    setStrategyInfo(info || null);
    setError('');

    if (info?.source === 'template') {
      setStrategyType(info.id);
      setStrategyParams(sanitizeStrategyParams(info.id, params, undefined, info.code));
    } else {
      // 个人策略或上传策略，使用 CustomStrategy 运行
      // 这样后端会执行代码内容，而不是仅依赖 ID
      setStrategyType('CustomStrategy');
      setStrategyParams(sanitizeStrategyParams('CustomStrategy', params || strategyParams, undefined, info?.code));
    }
  };

  const startSimulatedProgress = () => {
    if (progressTimerRef.current != null) window.clearInterval(progressTimerRef.current);
    runStartedAtRef.current = Date.now();
    backendProgressRef.current = 0;
    progressRef.current = 3;
    setProgress(3);
    setProgressMessage('正在准备回测任务...');
    progressTimerRef.current = window.setInterval(() => {
      const p = progressRef.current || 0;
      const elapsedMs = Math.max(0, Date.now() - runStartedAtRef.current);
      const backendProgress = backendProgressRef.current || 0;
      const bounded = blendBacktestProgress(p, backendProgress, elapsedMs);
      progressRef.current = bounded;
      setProgress(bounded);
      setProgressMessage(getBacktestStageMessage(bounded, backendProgress, 'running'));
    }, 800);
  };

  const updateProgressMonotonic = (nextProgress: number, cap = 99, status?: string, msg?: string) => {
    const bounded = Math.min(cap, Math.max(0, nextProgress));
    backendProgressRef.current = Math.max(backendProgressRef.current || 0, bounded);
    const merged = Math.max(progressRef.current || 0, bounded);
    progressRef.current = merged;
    setProgress(merged);
    setProgressMessage(getBacktestStageMessage(merged, backendProgressRef.current, status, msg));
  };

  const stopSimulatedProgress = () => {
    if (progressTimerRef.current != null) {
      window.clearInterval(progressTimerRef.current);
      progressTimerRef.current = null;
    }
  };

  const handleRun = async (override?: string | React.MouseEvent) => {
    const overrideCode = typeof override === 'string' ? override : undefined;
    if (!strategyInfo && !overrideCode) {
      setError('请选择一个策略模板');
      return;
    }

    const topk = Number(strategyParams.topk ?? 0);
    const nDrop = Number(strategyParams.n_drop ?? 0);
    if (topk > 0 && nDrop > topk) {
      setError('参数校验失败：每日最大调仓数 (n_drop) 不能大于持仓股票总数 (topk)。');
      setProgress(0);
      setProgressMessage('参数校验未通过');
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
        strategy_type: strategyType,
        strategy_params: strategyParams,
        benchmark_symbol: benchmark,
        strategy_code: overrideCode || strategyInfo?.code || '',
        strategy_id: strategyInfo?.id,
        seed: seed.trim() === '' ? undefined : Number(seed),
        commission: 0.00025,
        deal_price: dealPrice,
      };

      setLastConfig(config);
      const response = await backtestService.runBacktest(config);

      if (response.status === 'completed') {
        finishRun(response as unknown as QlibBacktestResult);
      } else if (response.status === 'failed') {
        failRun(response.error_message || '回测启动失败', response.backtest_id, response.full_error);
      } else {
        stopPollingRef.current = backtestService.pollStatus(response.backtest_id, {
          onProgress: (prog, status, msg) => {
            const normalized = prog <= 1 ? prog * 100 : prog;
            updateProgressMonotonic(normalized, 99, status, msg);
          },
          onComplete: (final) => finishRun(final as unknown as QlibBacktestResult),
          onError: (err) => failRun(err.message, response.backtest_id, (err as any).traceback)
        });
      }
    } catch (err: unknown) {
      failRun(err instanceof Error ? err.message : '回测执行异常');
    }
  };

  const finishRun = (res: QlibBacktestResult) => {
    stopSimulatedProgress();
    setResult(res);
    setProgress(100);
    setProgressMessage('回测已完成');
    setIsRunning(false);
  };

  const failRun = (msg: string, backtestId?: string, traceback?: string) => {
    stopSimulatedProgress();
    setError(msg || '策略运行异常 (后端未返回具体错误)');
    setFullTraceback(traceback || '');
    setLastBacktestId(backtestId || '');
    setIsRunning(false);
    setProgress(0);
    setProgressMessage('回测失败');

    // 发送到后端日志
    const storedUser = authService.getStoredUser() as any;
    backtestService.logError({
      backtest_id: backtestId,
      message: msg,
      user_id: String(normalizeUserId(storedUser?.id ?? storedUser?.user_id) || 'unknown'),
      stack: traceback || new Error().stack
    }).catch(console.error);
  };

  useEffect(() => {
    return () => {
      stopSimulatedProgress();
      if (stopPollingRef.current) stopPollingRef.current();
    };
  }, []);

  // 同步回测中心共享配置（如参数优化的一键回填）
  useEffect(() => {
    if (backtestConfig.start_date) {
      setStartDate(String(backtestConfig.start_date));
    }
    if (backtestConfig.end_date) {
      setEndDate(String(backtestConfig.end_date));
    }
    const syncedType =
      sharedConfig.qlib_strategy_type || backtestConfig.strategy_type;
    if (syncedType) {
      setStrategyType(String(syncedType));
    }

    if (backtestConfig.symbol && typeof backtestConfig.symbol === 'string') {
      setUniversePath(String(backtestConfig.symbol));
    }

    const syncedParams =
      sharedConfig.qlib_strategy_params || backtestConfig.strategy_params;
    if (syncedParams && typeof syncedParams === 'object') {
      setStrategyParams(
        sanitizeStrategyParams(
          String(syncedType || strategyType || DEFAULT_TEMPLATE_ID),
          syncedParams as QlibStrategyParams,
          undefined,
          strategyInfo?.code
        )
      );
    }
  }, [
    backtestConfig.start_date,
    backtestConfig.end_date,
    backtestConfig.strategy_type,
    backtestConfig.symbol,
    sharedConfig.qlib_strategy_type,
    backtestConfig.strategy_params,
    sharedConfig.qlib_strategy_params,
    strategyType,
    strategyInfo?.code,
  ]);

  return (
    <div className="flex flex-col h-full bg-slate-50 overflow-hidden">
      <div className="flex-1 flex flex-col xl:flex-row overflow-hidden">
        {/* 左侧配置栏 */}
        <div className="w-full xl:w-[520px] xl:min-w-[480px] xl:max-w-[560px] xl:border-r border-gray-200 bg-white/95 overflow-y-auto custom-scrollbar p-6 space-y-7 flex flex-col h-full">

          <div className="p-4 rounded-2xl border bg-gradient-to-r from-blue-50 to-indigo-50 border-blue-100 flex items-start gap-3">
            <Info className="w-4 h-4 mt-0.5 text-blue-500" />
            <div>
              <div className="text-sm font-bold text-blue-900">标准参数模式</div>
              <div className="text-xs text-gray-600 leading-relaxed mt-1">
                默认使用标准 Top-K 选股模板；前端显式参数优先，后端会自动做补全与兼容修复，适合快速验证截面信号的盈利表现。
              </div>
            </div>
          </div>

          <StrategyPicker
            onStrategySelected={handleStrategySelected}
            hideUpload={true}
          />

          <div className="space-y-5 rounded-2xl border border-gray-200 bg-white p-5">
            <h3 className="flex items-center gap-2 font-bold text-gray-800 text-base">
              <Settings2 className="w-4 h-4 text-gray-400" /> 基础配置
            </h3>

            <div>
              <label className="block text-sm font-medium text-gray-600 mb-2">股票池 (Symbols)</label>
              <div className="grid grid-cols-5 gap-2">
                {UNIVERSE_PRESETS.map((preset) => {
                  const active = universePath === preset.value;
                  return (
                    <button
                      key={preset.value}
                      type="button"
                      onClick={() => setUniversePath(preset.value)}
                      className={`px-2 py-2 text-xs font-medium rounded-xl border transition-all ${
                        active
                          ? 'bg-blue-600 text-white border-blue-600 shadow-sm'
                          : 'bg-white text-gray-600 border-gray-300 hover:border-blue-300 hover:text-blue-600'
                      }`}
                    >
                      {preset.label}
                    </button>
                  );
                })}
              </div>
            </div>

            {dataMinDate && dataMaxDate && (
              <div className="flex items-center gap-2 text-xs text-slate-500 bg-slate-50 px-3 py-2 rounded-xl">
                <CalendarRange className="w-3.5 h-3.5 text-indigo-500" />
                <span>数据有效期：</span>
                <span className="font-mono text-slate-700">{dataMinDate}</span>
                <span className="text-slate-400">~</span>
                <span className="font-mono text-slate-700">{dataMaxDate}</span>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-600 mb-2">开始日期 (Start Date)</label>
                <input
                  type="date"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  min={dataMinDate || BACKTEST_CONFIG.QLIB.DATA_START}
                  max={dataMaxDate || BACKTEST_CONFIG.QLIB.DATA_END}
                  className="w-full px-3 py-2.5 bg-white border border-gray-200 rounded-xl text-sm text-gray-900 focus:outline-none focus:border-blue-500 premium-date-picker"
                  style={{ colorScheme: 'light' }}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-600 mb-2">结束日期 (End Date)</label>
                <input
                  type="date"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  min={dataMinDate || BACKTEST_CONFIG.QLIB.DATA_START}
                  max={dataMaxDate || BACKTEST_CONFIG.QLIB.DATA_END}
                  className="w-full px-3 py-2.5 bg-white border border-gray-200 rounded-xl text-sm text-gray-900 focus:outline-none focus:border-blue-500 premium-date-picker"
                  style={{ colorScheme: 'light' }}
                />
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-600 mb-2">初始资金 (Capital)</label>
                <input
                  type="number"
                  value={initialCapital}
                  onChange={(e) => setInitialCapital(Number(e.target.value))}
                  className="w-full px-3 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-600 mb-2">调仓周期 (Rebalance)</label>
                <select
                  value={strategyParams.rebalance_days || 3}
                  onChange={(e) => setStrategyParams({ ...strategyParams, rebalance_days: Number(e.target.value) })}
                  className="w-full px-3 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:border-blue-500"
                >
                  {QLIB_REBALANCE_DAY_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label} ({item.labelEn})
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-600 mb-2">基准指数 (Benchmark)</label>
                <select value={benchmark} onChange={(e) => setBenchmark(e.target.value)} className="w-full px-3 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:border-blue-500">
                  {BACKTEST_CONFIG.QLIB.BENCHMARKS.map((bm) => (
                    <option key={bm.code} value={bm.code}>{bm.name} ({bm.code})</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-600 mb-2">成交价格 (Deal Price)</label>
                <select
                  value={dealPrice}
                  onChange={(e) => setDealPrice(e.target.value as 'open' | 'close')}
                  className="w-full px-3 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:border-blue-500"
                >
                  <option value="close">收盘价成交 (Close)</option>
                  <option value="open">开盘价成交 (Open)</option>
                </select>
              </div>
            </div>
          </div>

          <QlibStrategyConfigurator
            strategyType={strategyType as QlibStrategyType}
            params={strategyParams}
            onChange={setStrategyParams}
            strategyCode={strategyInfo?.code}
          />

          {error && (
            <div className="bg-red-50 border border-red-200 p-5 rounded-2xl shadow-sm">
              <div className="flex items-center justify-between mb-3">
                <div className="text-sm font-bold text-red-800 flex items-center gap-2">
                  <AlertCircle className="w-4 h-4" /> 策略执行失败
                </div>
                <button
                  onClick={handleCopyLog}
                  className="flex items-center gap-1.5 text-[11px] font-bold text-red-600 hover:text-red-800 transition-colors bg-white px-2.5 py-1 rounded-lg border border-red-100 shadow-sm"
                >
                  {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                  {copied ? '已复制' : '复制错误日志'}
                </button>
              </div>

              <div className="text-[12px] text-red-700 leading-relaxed font-medium mb-4 break-words">
                {error}
              </div>

              {fullTraceback && (
                <button
                  type="button"
                  onClick={() => setShowErrorLog(true)}
                  className="flex items-center gap-1.5 text-[11px] font-bold text-red-600 hover:underline"
                >
                  <ExternalLink className="w-3 h-3" /> 查看完整 Python 堆栈追踪 (Traceback)
                </button>
              )}
            </div>
          )}


          <div className="sticky bottom-0 z-10 -mx-6 px-6 pt-4 pb-5 bg-gradient-to-t from-white via-white to-transparent border-t border-gray-100">
            <button
              onClick={handleRun}
              disabled={isRunning}
              className="w-full py-3.5 bg-gradient-to-r from-blue-600 to-indigo-600 text-white rounded-2xl font-bold hover:shadow-lg transition-all flex items-center justify-center gap-2"
            >
              {isRunning ? <><RefreshCw className="w-4 h-4 animate-spin" /> 回测中 {progress.toFixed(0)}%</> : <><Play className="w-4 h-4 fill-current" /> 立即执行回测</>}
            </button>
          </div>
        </div>

        {/* 右侧展示区 */}
        <div className="flex-1 overflow-y-auto custom-scrollbar p-6 md:p-8 bg-slate-50">
          {isRunning ? (
            <div className="flex flex-col items-center justify-center h-full">
              <div className="w-16 h-16 border-4 border-blue-500 border-t-transparent rounded-full animate-spin mb-4"></div>
              <div className="text-lg font-bold text-gray-800">{progressMessage} {progress.toFixed(0)}%</div>
            </div>
          ) : result ? (
            <QlibResultDisplay result={result} fallbackConfig={lastConfig} />
          ) : (
            <div className="flex items-center justify-center h-full text-gray-400">
              <div className="text-center">
                <BarChart3 className="h-16 w-16 mx-auto mb-4 opacity-20" />
                <p>配置参数后点击"立即执行回测"</p>
              </div>
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
          onFixed={(repairedCode, strategyId) => {
            if (repairedCode) {
              setStrategyInfo(strategyInfo ? { ...strategyInfo, code: repairedCode, id: strategyId || strategyInfo.id } : null);
            }
          }}
        />
      )}
    </div>
  );
};
