/**
 * Qlib专用快速回测组件（Native 模式）
 * 仅支持标准参数配置，追求极致简洁与稳定性。
 */

import React, { useState, useRef, useEffect, useMemo } from 'react';
import { motion } from 'framer-motion';
import {
  Play, RefreshCw, BarChart3, Settings2, Info, AlertCircle, Copy, Check, ExternalLink, CalendarRange, Cpu,
  ChevronDown,
} from 'lucide-react';

import type { BacktestConfig } from '../../services/backtestService';
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
import { getStoredTailTradeMode, setStoredTailTradeMode, getTailTradeDealPrice, getTailTradeSignalLagDays, ALLOW_FEATURE_SIGNAL_FALLBACK } from '../../shared/qlib/tailTradeMode';
import { strategyManagementService } from '../../services/strategyManagementService';
import { modelTrainingService, UserModelRecord } from '../../services/modelTrainingService';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { getMarketConfig } from '../../config/marketConfig';
import dayjs from 'dayjs';

const MARKET_UNIVERSE_PRESETS: Record<string, { label: string; value: string }[]> = {
  CN: [
    { label: '全部', value: 'all' },
    { label: '沪深300', value: 'csi300' },
    { label: '中证500', value: 'csi500' },
    { label: '中证800', value: 'csi800' },
    { label: '中证1000', value: 'csi1000' },
  ],
  HK: [
    { label: '全部港股', value: 'all' },
    { label: '港股通成分', value: 'hsgt' },
    { label: '港股通指数系列', value: 'hsgt_10_index' },
    { label: '市值 Top50', value: 'val_top50' },
    { label: '市值 Top100', value: 'val_top100' },
    { label: '市值 Top300', value: 'val_top300' },
  ],
  US: [
    { label: '全部美股', value: 'all' },
  ],
  CRYPTO: [
    { label: '全部加密货币', value: 'all' },
    { label: 'Layer 1', value: 'layer1' },
  ],
};

const DEFAULT_TEMPLATE_ID = 'standard_topk';
const DEFAULT_TEMPLATE = getTemplateById(DEFAULT_TEMPLATE_ID);

export const QlibQuickBacktest: React.FC = () => {
  const stopPollingRef = useRef<(() => void) | null>(null);
  const progressTimerRef = useRef<number | null>(null);
  const progressRef = useRef<number>(0);
  const backendProgressRef = useRef<number>(0);
  const runStartedAtRef = useRef<number>(0);
  const backtestConfig = useBacktestCenterStore((state) => state.backtestConfig);
  const activeModule = useBacktestCenterStore((state) => state.activeModule);
  const currentMarket = useAppSelector(selectCurrentMarket);
  const marketConfig = getMarketConfig(currentMarket);
  const UNIVERSE_PRESETS = useMemo(() => MARKET_UNIVERSE_PRESETS[currentMarket] || MARKET_UNIVERSE_PRESETS.CN, [currentMarket]);
  const MARKET_BENCHMARKS = useMemo(() => BACKTEST_CONFIG.QLIB.MARKET_BENCHMARKS[currentMarket] || BACKTEST_CONFIG.QLIB.MARKET_BENCHMARKS.CN, [currentMarket]);

  // 策略相关状态
  const [strategyInfo, setStrategyInfo] = useState<StrategyFile | null>(null);
  const [signalModelOpen, setSignalModelOpen] = useState(false);

  // 基础配置
  const [universePath, setUniversePath] = useState<string>('all');
  const [startDate, setStartDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_START);
  const [endDate, setEndDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_END);
  const [initialCapital, setInitialCapital] = useState(1000000);
  const [benchmark, setBenchmark] = useState(marketConfig.benchmark);
  const [seed] = useState('');
  const [dealPrice, setDealPrice] = useState<'open' | 'close'>('open');

  // 尾盘交易模式开关（持久化）
  const [tailTradeEnabled, setTailTradeEnabled] = useState<boolean>(() => getStoredTailTradeMode());
  const [showTailTradeTooltip, setShowTailTradeTooltip] = useState(false);
  const tailTradeTimerRef = useRef<number | null>(null);

  // 切换开关时同步缓存，并自动修正 dealPrice
  useEffect(() => {
    setStoredTailTradeMode(tailTradeEnabled);
    if (tailTradeEnabled) {
      setDealPrice('close');
    } else {
      setDealPrice('open');
    }
  }, [tailTradeEnabled]);
  
  // 数据日期范围（从后端获取）
  const [dataMinDate, setDataMinDate] = useState<string | null>(null);
  const [dataMaxDate, setDataMaxDate] = useState<string | null>(null);

  // 模型选择
  const [models, setModels] = useState<UserModelRecord[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string>('');

  // 策略参数
  const [strategyType, setStrategyType] = useState<string>(DEFAULT_TEMPLATE_ID);
  const [strategyParams, setStrategyParams] = useState<QlibStrategyParams>(
    getDefaultStrategyParams(DEFAULT_TEMPLATE_ID)
  );

  // 追踪是否已处理过 localStorage 中的策略 ID
  const pendingStrategyHandledRef = useRef(false);

  useEffect(() => {
    // 仅在 quick-backtest 模块激活时检查 localStorage
    if (activeModule !== 'quick-backtest') return;

    // 检查是否有从策略管理中心传递过来的策略ID
    const pendingId = localStorage.getItem('selected_backtest_strategy_id');
    if (pendingId && !pendingStrategyHandledRef.current) {
      pendingStrategyHandledRef.current = true;
      localStorage.removeItem('selected_backtest_strategy_id');
      loadPendingStrategy(pendingId);
    } else if (!strategyInfo && !pendingStrategyHandledRef.current) {
      // 首次加载且无待处理策略时，加载默认模板
      if (!DEFAULT_TEMPLATE) {
        return;
      }
      pendingStrategyHandledRef.current = true;
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
  }, [activeModule]);

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

  // 加载用户模型列表
  useEffect(() => {
    const loadModels = async () => {
      setModelsLoading(true);
      try {
        const [userResp, sysModels] = await Promise.all([
          modelTrainingService.listUserModels(true),
          modelTrainingService.listSystemModels(),
        ]);
        const sysItems: UserModelRecord[] = (sysModels ?? []).map((sm) => {
          const raw = sm as unknown as Record<string, any>;
          return {
            tenant_id: 'system',
            user_id: 'system',
            model_id: sm.model_id,
            source_run_id: '',
            status: 'active',
            storage_path: '',
            model_file: '',
            metadata_json: {
              display_name: sm.display_name,
              description: sm.description,
              framework: sm.framework,
              model_type: sm.model_type,
              feature_count: sm.feature_count,
              market: raw.market,
              target_horizon_days: raw.target_horizon_days,
              train_start: raw.train_start,
              train_end: raw.train_end,
              test_start: raw.test_start,
              test_end: raw.test_end,
              label_formula: raw.label_formula,
              target_mode: raw.target_mode,
              best_iteration: raw.best_iteration,
            },
            metrics_json: sm.performance_metrics ?? {},
            is_default: false,
            created_at: sm.created_at,
          };
        });
        const allModels = [...sysItems, ...(userResp.items ?? [])];
        setModels(allModels);
        // 默认选中 default 模型
        const def = allModels.find(m => m.is_default);
        if (def) setSelectedModelId(def.model_id);
        else if (allModels.length > 0) setSelectedModelId(allModels[0].model_id);
      } catch {
        // silent
      } finally {
        setModelsLoading(false);
      }
    };
    loadModels();
  }, []);

  // 按当前市场过滤模型
  const filteredModels = useMemo(() => {
    return models.filter((m) => {
      const meta = (m.metadata_json || {}) as Record<string, any>;
      const raw = String(meta.market || '').toUpperCase();
      const ctx = meta.context;
      const ctxMarket = String((ctx && typeof ctx === 'object' ? ctx.market : '') || '').toUpperCase();
      const mkt = raw || ctxMarket;
      if (!mkt) return true; // 无市场标记的模型始终显示
      // 当前市场映射
      const cur = currentMarket.toUpperCase();
      if (cur === 'CN') return mkt.includes('CN') || mkt.includes('A_SHARE') || mkt.includes('A股');
      if (cur === 'HK') return mkt.includes('HK') || mkt.includes('HONG_KONG') || mkt.includes('港股');
      if (cur === 'US') return mkt.includes('US') || mkt.includes('美股');
      if (cur === 'CRYPTO') return mkt.includes('CRYPTO') || mkt.includes('加密');
      return true;
    });
  }, [models, currentMarket]);

  // 当过滤列表变化时，确保选中模型仍在列表中
  useEffect(() => {
    if (filteredModels.length === 0) {
      setSelectedModelId('');
      return;
    }
    if (!filteredModels.find(m => m.model_id === selectedModelId)) {
      const def = filteredModels.find(m => m.is_default);
      setSelectedModelId(def ? def.model_id : filteredModels[0].model_id);
    }
  }, [filteredModels, selectedModelId]);

  // 市场切换时重置基准和股票池
  useEffect(() => {
    setBenchmark(marketConfig.benchmark);
    setUniversePath('all');
  }, [currentMarket]);

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
        model_id: selectedModelId || undefined,
        seed: seed.trim() === '' ? undefined : Number(seed),
        commission: 0.00025,
        deal_price: getTailTradeDealPrice(tailTradeEnabled),
        signal_lag_days: getTailTradeSignalLagDays(tailTradeEnabled),
        allow_feature_signal_fallback: ALLOW_FEATURE_SIGNAL_FALLBACK,
        qlib_provider_uri: marketConfig.qlibProviderUri,
        qlib_region: marketConfig.qlibRegion,
      };

      setLastConfig(config);
      const { backtestService } = await import('../../services/backtestService');
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
    (async () => {
      const { backtestService } = await import('../../services/backtestService');
      backtestService.logError({
        backtest_id: backtestId,
        message: msg,
        user_id: String(normalizeUserId(storedUser?.id ?? storedUser?.user_id) || 'unknown'),
        stack: traceback || new Error().stack
      }).catch(console.error);
    })();
  };

  useEffect(() => {
    return () => {
      stopSimulatedProgress();
      if (stopPollingRef.current) stopPollingRef.current();
      if (tailTradeTimerRef.current) clearTimeout(tailTradeTimerRef.current);
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
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="flex flex-col h-full bg-slate-50 overflow-hidden"
    >
      <div className="flex-1 flex flex-col xl:flex-row overflow-hidden">
        {/* 左侧配置栏 */}
        <div className="w-full xl:w-[520px] xl:min-w-[480px] xl:max-w-[560px] xl:border-r border-gray-200 bg-white/95 overflow-y-auto custom-scrollbar p-6 space-y-7 flex flex-col h-full">

          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: 0.05 }}
            className="p-4 rounded-2xl border bg-gradient-to-r from-blue-50 to-indigo-50 border-blue-100 flex items-start gap-3 shadow-sm"
          >
            <div className="w-9 h-9 rounded-2xl bg-blue-500/10 flex items-center justify-center shrink-0">
              <Info className="w-4 h-4 text-blue-600" />
            </div>
            <div>
              <div className="text-lg font-bold text-slate-800 tracking-tight">快速回测（标准参数模式）</div>
              <div className="text-xs text-slate-500 leading-relaxed mt-1">
                默认使用标准 Top-K 选股模板；前端显式参数优先，后端会自动做补全与兼容修复，适合快速验证截面信号的盈利表现。
              </div>
            </div>
          </motion.div>

          {/* 模型选择 + 详情 */}
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: 0.1 }}
            className="rounded-2xl border border-gray-200 bg-white shadow-sm"
          >
            <button
              type="button"
              onClick={() => setSignalModelOpen(!signalModelOpen)}
              className="w-full flex items-center justify-between gap-2 px-5 py-4 text-left"
            >
              <div className="flex items-center gap-2 text-lg font-bold text-slate-800 tracking-tight">
                <span className="w-8 h-8 rounded-xl bg-indigo-500/10 flex items-center justify-center"><Cpu className="w-4 h-4 text-indigo-600" /></span>
                Signal Model
              </div>
              <ChevronDown className={`w-5 h-5 text-gray-400 transition-transform ${signalModelOpen ? '' : '-rotate-90'}`} />
            </button>
            {signalModelOpen && (
              <div className="space-y-3 px-5 pb-5">
                <p className="text-xs text-gray-500 -mt-1">
                  选择训练好的 ML 模型，其预测分数将作为策略信号（signal=&lt;PRED&gt;）
                </p>
                <select
                  value={selectedModelId}
                  onChange={(e) => setSelectedModelId(e.target.value)}
                  disabled={modelsLoading}
                  className="w-full px-3 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:border-blue-500"
                >
                  {modelsLoading && <option value="">加载中...</option>}
                  {!modelsLoading && filteredModels.length === 0 && <option value="">暂无可用模型</option>}
                  {filteredModels.map((m) => {
                    const meta = (m.metadata_json || {}) as Record<string, any>;
                    const name = String(meta.display_name || m.model_id);
                    const fw = String(meta.framework || '');
                    const fc = meta.feature_count ? `${meta.feature_count}D` : '';
                    const mkt = String(meta.market || '');
                    return (
                      <option key={m.model_id} value={m.model_id}>
                        {name}{fw ? ` (${fw}` : ''}{fc ? ` ${fc}` : ''}{mkt ? ` ${mkt}` : ''}{fw ? ')' : ''}
                        {m.is_default ? ' ★' : ''}
                      </option>
                    );
                  })}
                </select>

            {/* 模型详情卡片 */}
            {selectedModelId && (() => {
              const sel = filteredModels.find(m => m.model_id === selectedModelId);
              if (!sel) return null;
              const meta = (sel.metadata_json || {}) as Record<string, any>;
              const metrics = (sel.metrics_json || {}) as Record<string, any>;
              const name = String(meta.display_name || sel.model_id);
              const fw = String(meta.framework || '-');
              const fc = meta.feature_count ?? '-';
              const mkt = String(meta.market || '');
              const mktUpper = mkt.toUpperCase();
              const mktLabel = mktUpper.includes('HK') ? '港股' : mktUpper.includes('US') ? '美股' : mktUpper.includes('CRYPTO') ? '加密' : 'A股';
              const horizon = meta.target_horizon_days ?? meta.horizon_days ?? '-';
              const trainStart = meta.train_start || meta.training_window?.split?.(' to ')?.[0] || '';
              const trainEnd = meta.train_end || meta.training_window?.split?.(' to ')?.[1] || '';
              const labelFormula = meta.label_formula || meta.label || '';

              // 从 metrics 中提取指标（兼容 mean_ic / auc / rmse 多种格式）
              const getPhase = (phase: string) => {
                const p = metrics[phase];
                if (p && typeof p === 'object') return p;
                return null;
              };
              const getIC = (phase: string) => {
                const p = getPhase(phase);
                if (p) return p.mean_ic ?? p.ic ?? null;
                return metrics[`${phase}_ic`] ?? null;
              };
              const getAUC = (phase: string) => {
                const p = getPhase(phase);
                if (p) return p.auc ?? null;
                return metrics[`${phase}_auc`] ?? null;
              };
              const getICIR = (phase: string) => {
                const p = getPhase(phase);
                if (p) return p.icir ?? p.rank_icir ?? null;
                return metrics[`${phase}_rank_icir`] ?? null;
              };
              const getRMSE = (phase: string) => {
                const p = getPhase(phase);
                if (p) return p.rmse ?? null;
                return null;
              };

              const hasIC = getIC('train') != null || getIC('valid') != null || getIC('val') != null || getIC('test') != null;
              const trainIC = getIC('train');
              const valIC = getIC('valid') || getIC('val');
              const testIC = getIC('test');
              const valICIR = getICIR('valid') || getICIR('val');

              const trainAUC = getAUC('train');
              const valAUC = getAUC('valid') || getAUC('val');
              const testAUC = getAUC('test');

              const trainRMSE = getRMSE('train');
              const valRMSE = getRMSE('valid') || getRMSE('val');
              const testRMSE = getRMSE('test');

              const fmt4 = (v: number | null) => v != null ? v.toFixed(4) : '-';
              const fmt2 = (v: number | null) => v != null ? v.toFixed(2) : '-';

              return (
                <div className="mt-2 p-3 rounded-xl bg-slate-50 border border-slate-100 text-xs space-y-2.5">
                  {/* 头部：名称 + 标签 */}
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-slate-700 text-sm">{name}</span>
                    <div className="flex gap-1.5">
                      {sel.is_default && (
                        <span className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 font-bold text-[9px]">DEFAULT</span>
                      )}
                      {mkt && (
                        <span className="px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 font-bold text-[9px]">{mktLabel}</span>
                      )}
                    </div>
                  </div>

                  {/* 基础信息网格 */}
                  <div className="grid grid-cols-3 gap-2">
                    <div>
                      <div className="text-[10px] text-slate-400">框架</div>
                      <div className="font-mono font-bold text-slate-600">{fw}</div>
                    </div>
                    <div>
                      <div className="text-[10px] text-slate-400">特征维度</div>
                      <div className="font-mono font-bold text-slate-600">{fc}D</div>
                    </div>
                    <div>
                      <div className="text-[10px] text-slate-400">预测周期</div>
                      <div className="font-mono font-bold text-slate-600">{horizon !== '-' ? `T+${horizon}` : '-'}</div>
                    </div>
                  </div>

                  {/* 训练区间 */}
                  {(trainStart || trainEnd) && (
                    <div>
                      <div className="text-[10px] text-slate-400">训练区间</div>
                      <div className="font-mono text-slate-600">{trainStart} ~ {trainEnd}</div>
                    </div>
                  )}

                  {/* 标签公式 */}
                  {labelFormula && (
                    <div>
                      <div className="text-[10px] text-slate-400">标签公式</div>
                      <div className="font-mono text-slate-600 break-all text-[10px]">{labelFormula}</div>
                    </div>
                  )}

                  {/* 性能指标 — IC 模式 */}
                  {hasIC && (
                    <div>
                      <div className="text-[10px] text-slate-400 mb-1.5">模型 IC 指标</div>
                      <div className="grid grid-cols-3 gap-2">
                        <div className="text-center p-1.5 rounded-lg bg-white border border-slate-100">
                          <div className="text-[9px] text-slate-400 font-bold">TRAIN</div>
                          <div className="font-mono font-bold text-slate-700">{fmt4(trainIC)}</div>
                        </div>
                        <div className="text-center p-1.5 rounded-lg bg-white border border-slate-100">
                          <div className="text-[9px] text-slate-400 font-bold">VALID</div>
                          <div className="font-mono font-bold text-slate-700">{fmt4(valIC)}</div>
                        </div>
                        <div className="text-center p-1.5 rounded-lg bg-white border border-slate-100">
                          <div className="text-[9px] text-slate-400 font-bold">TEST</div>
                          <div className="font-mono font-bold text-slate-700">{fmt4(testIC)}</div>
                        </div>
                      </div>
                      {valICIR != null && (
                        <div className="mt-1.5 flex items-center gap-2">
                          <span className="text-[10px] text-slate-400">ICIR (Valid)</span>
                          <span className={`font-mono font-bold ${Number(valICIR) > 0.5 ? 'text-green-600' : Number(valICIR) > 0 ? 'text-yellow-600' : 'text-red-500'}`}>
                            {Number(valICIR).toFixed(2)}
                          </span>
                        </div>
                      )}
                    </div>
                  )}

                  {/* 性能指标 — AUC/RMSE 模式（分类模型） */}
                  {!hasIC && (trainAUC != null || valAUC != null || testAUC != null) && (
                    <div>
                      <div className="text-[10px] text-slate-400 mb-1.5">模型指标</div>
                      <div className="grid grid-cols-3 gap-2">
                        <div className="text-center p-1.5 rounded-lg bg-white border border-slate-100">
                          <div className="text-[9px] text-slate-400 font-bold">TRAIN</div>
                          <div className="font-mono font-bold text-slate-700">
                            {trainAUC != null ? `AUC ${trainAUC.toFixed(4)}` : '-'}
                          </div>
                          {trainRMSE != null && (
                            <div className="text-[9px] text-slate-400 font-mono">RMSE {trainRMSE.toFixed(4)}</div>
                          )}
                        </div>
                        <div className="text-center p-1.5 rounded-lg bg-white border border-slate-100">
                          <div className="text-[9px] text-slate-400 font-bold">VALID</div>
                          <div className="font-mono font-bold text-slate-700">
                            {valAUC != null ? `AUC ${valAUC.toFixed(4)}` : '-'}
                          </div>
                          {valRMSE != null && (
                            <div className="text-[9px] text-slate-400 font-mono">RMSE {valRMSE.toFixed(4)}</div>
                          )}
                        </div>
                        <div className="text-center p-1.5 rounded-lg bg-white border border-slate-100">
                          <div className="text-[9px] text-slate-400 font-bold">TEST</div>
                          <div className="font-mono font-bold text-slate-700">
                            {testAUC != null ? `AUC ${testAUC.toFixed(4)}` : '-'}
                          </div>
                          {testRMSE != null && (
                            <div className="text-[9px] text-slate-400 font-mono">RMSE {testRMSE.toFixed(4)}</div>
                          )}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })()}
              </div>
            )}
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: 0.15 }}
            className="rounded-2xl border border-gray-200 bg-white p-1 shadow-sm"
          >
            <StrategyPicker
              onStrategySelected={handleStrategySelected}
              hideUpload={true}
              initialStrategy={strategyInfo}
            />
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: 0.2 }}
            className="space-y-5 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Run Setup</div>
                <h3 className="flex items-center gap-2 text-lg font-bold text-slate-800 tracking-tight">
                  <span className="w-8 h-8 rounded-xl bg-blue-500/10 flex items-center justify-center"><Settings2 className="w-4 h-4 text-blue-600" /></span> 基础配置
                </h3>
              </div>
              <div
                className="relative flex items-center gap-2 shrink-0"
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
                  className={`flex items-center gap-2 px-2.5 py-1 rounded-full border transition-all duration-200 ${
                    tailTradeEnabled
                      ? 'bg-blue-50 border-blue-200 text-blue-700'
                      : 'bg-gray-50 border-gray-200 text-gray-600 hover:bg-gray-100'
                  }`}
                >
                  <span className="text-[11px] font-bold">尾盘交易</span>
                  <span className={`px-1.5 py-0.5 rounded-lg text-[9px] font-black min-w-[28px] text-center transition-all ${
                    tailTradeEnabled
                      ? 'bg-blue-600 text-white'
                      : 'bg-white text-gray-500 border border-gray-100 shadow-sm'
                  }`}>
                    {tailTradeEnabled ? 'ON' : 'OFF'}
                  </span>
                </button>
                {showTailTradeTooltip && (
                  <div className="absolute top-full right-0 mt-2 px-2.5 py-1.5 bg-gray-900 text-white text-[11px] rounded-lg whitespace-nowrap z-50 shadow-lg">
                    {tailTradeEnabled
                      ? '尾盘交易：T日信号+T+1收盘成交'
                      : '标准口径：T日信号+T+1开盘成交'}
                    <div className="absolute bottom-full right-3 border-4 border-transparent border-b-gray-900" />
                  </div>
                )}
              </div>
            </div>

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
                  {MARKET_BENCHMARKS.map((bm) => (
                    <option key={bm.code} value={bm.code}>{bm.name} ({bm.code})</option>
                  ))}
                </select>
              </div>
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <label className="text-sm font-medium text-gray-600">成交价格 (Deal Price)</label>
                </div>
                <select
                  value={getTailTradeDealPrice(tailTradeEnabled)}
                  onChange={(e) => setDealPrice(e.target.value as 'open' | 'close')}
                  disabled={tailTradeEnabled}
                  className={`w-full px-3 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:border-blue-500 ${
                    tailTradeEnabled ? 'text-gray-400 cursor-not-allowed' : ''
                  }`}
                >
                  <option value="open">开盘价成交 (Open)</option>
                  <option value="close">收盘价成交 (Close)</option>
                </select>
              </div>
            </div>
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: 0.25 }}
            className="rounded-2xl border border-gray-200 bg-white p-1 shadow-sm"
          >
            <QlibStrategyConfigurator
              strategyType={strategyType as QlibStrategyType}
              params={strategyParams}
              onChange={setStrategyParams}
              strategyCode={strategyInfo?.code}
            />
          </motion.div>

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
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: 0.1 }}
            className="min-h-full bg-white border border-gray-200 rounded-2xl shadow-sm overflow-hidden"
          >
            {isRunning ? (
              <div className="flex h-full min-h-[520px] flex-col items-center justify-center">
                <div className="w-16 h-16 border-4 border-blue-500 border-t-transparent rounded-full animate-spin mb-4"></div>
                <div className="text-lg font-bold text-slate-800 tracking-tight tabular-nums">{progressMessage} {progress.toFixed(0)}%</div>
              </div>
            ) : result ? (
              <div className="p-4">
                <QlibResultDisplay result={result} fallbackConfig={lastConfig} />
              </div>
            ) : (
              <div className="flex items-center justify-center min-h-[520px] text-gray-400">
                <div className="text-center">
                  <div className="w-20 h-20 mx-auto mb-4 rounded-2xl bg-blue-500/10 flex items-center justify-center">
                    <BarChart3 className="h-10 w-10 text-blue-500 opacity-70" />
                  </div>
                  <div className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Backtest Output</div>
                  <p className="text-lg font-bold text-slate-800 tracking-tight">配置参数后点击"立即执行回测"</p>
                  <p className="text-xs text-slate-500 mt-1">结果、权益曲线和风险指标将在这里展示</p>
                </div>
              </div>
            )}
          </motion.div>
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
    </motion.div>
  );
};
