import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Search, Play, Calendar, Sparkles, RefreshCw, Layers, Database, Sliders, Clock,
  Cpu, TrendingUp, BarChart3, History, Shield, CheckCircle2, AlertCircle, Info, Star,
  LayoutGrid, ArrowRight, Activity
} from 'lucide-react';
import {
  Button, Input, Select, DatePicker, message, Spin, Tooltip, Tag, Tabs, Badge, Card, Table, Typography
} from 'antd';
import dayjs, { Dayjs } from 'dayjs';
import { clsx } from 'clsx';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  inferenceCenterService,
  SingleStockPredictionResponse,
  AvailableModelOption,
  KlineItem,
} from '../services/inferenceCenterService';
import {
  modelTrainingService,
  UserModelRecord,
  SystemModelRecord,
  InferenceRunRecord,
  InferencePrecheckResult,
  AutoInferenceSettings,
  LatestInferenceRunInfo,
} from '../services/modelTrainingService';
import {
  calcTimeSplitStats,
  extractModelType,
  formatTrendLabel,
  getMeta,
  getMetrics,
  isSystemModel,
  modelDisplayName,
  systemModelToUserModel,
} from './modelRegistryUtils';
import { InferenceCenterPanel } from './modelRegistryPanels';
import { StockForecastChart } from '../features/inference-center/components/StockForecastChart';
import { FeatureDriversPanel } from '../features/inference-center/components/FeatureDriversPanel';
import { ModelConsensusPanel } from '../features/inference-center/components/ModelConsensusPanel';
import { InferenceHistoryPanel } from '../components/inference/InferenceHistoryPanel';
import { InferenceBacktestModule } from '../components/backtestCenter/InferenceBacktestModule';
import { useAppSelector } from '../store';

import { getMarketConfig } from '../config/marketConfig';
import { normalizeStockCode } from '../utils/portfolioUtils';
import { hkStockListService } from '../services/hkStockListService';

const { Text } = Typography;

// 模型卡片类型
type ModelCardOption = AvailableModelOption & {
  category: 'tree' | 'dl' | 'ensemble';
  tag: string;
  horizonDesc: string;
  sharpe: number;
  quantileSupport: boolean;
};

export const InferenceCenterHkPage: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const currentMarket = 'HK' as const; // 港股版页面：市场固定，不随顶部切换器
  const marketConfig = getMarketConfig(currentMarket);

  // 顶层 Tab：'cross-section'（市场截面推理）| 'individual'（个股预测推理）
  const initialTopTab = (location.state as any)?.tab === 'cross-section' ? 'cross-section' : 'cross-section';
  const initialModelId = (location.state as any)?.modelId || '';
  const [topTab, setTopTab] = useState<'cross-section' | 'individual'>(initialTopTab);

  // ─────────────────────────────────────────────────────────────
  // 模块 1：市场截面推理 (Cross-Section Inference) 状态
  // ─────────────────────────────────────────────────────────────
  const [crossSectionMode, setCrossSectionMode] = useState<'single' | 'history' | 'backtest'>('single');
  const [registeredModels, setRegisteredModels] = useState<UserModelRecord[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string>(initialModelId);
  const [inferenceDate, setInferenceDate] = useState<Dayjs | null>(dayjs());
  const [inferenceTargetDate, setInferenceTargetDate] = useState<string>('');
  const [inferenceTargetLoading, setInferenceTargetLoading] = useState(false);
  const [inferenceRunning, setInferenceRunning] = useState(false);
  const [lastInferenceRun, setLastInferenceRun] = useState<InferenceRunRecord | null>(null);
  const [latestInferenceRun, setLatestInferenceRun] = useState<LatestInferenceRunInfo | null>(null);
  const [latestInferenceRunLoading, setLatestInferenceRunLoading] = useState(false);
  const [inferencePrecheck, setInferencePrecheck] = useState<InferencePrecheckResult | null>(null);
  const [inferencePrecheckLoading, setInferencePrecheckLoading] = useState(false);
  const [autoSettings, setAutoSettings] = useState<AutoInferenceSettings | null>(null);
  const [autoSaving, setAutoSaving] = useState(false);
  const [inferenceHistory, setInferenceHistory] = useState<InferenceRunRecord[]>([]);
  const [inferenceHistoryLoading, setInferenceHistoryLoading] = useState(false);
  const [historyRunIdFilter, setHistoryRunIdFilter] = useState('');
  const [historyStatusFilter, setHistoryStatusFilter] = useState<'all' | 'running' | 'completed' | 'failed'>('all');
  const [historyDateFilter, setHistoryDateFilter] = useState<Dayjs | null>(null);

  // ─────────────────────────────────────────────────────────────
  // 模块 2：个股预测推理 (Individual Stock Inference) 状态
  // ─────────────────────────────────────────────────────────────
  const [symbol, setSymbol] = useState('0700.HK');
  const [inputCode, setInputCode] = useState('0700.HK');
  const [singleStockModelId, setSingleStockModelId] = useState<string>('');
  const [modelCategoryFilter, setModelCategoryFilter] = useState<'all' | 'dl' | 'tree' | 'ensemble'>('all');
  const [horizon, setHorizon] = useState<number>(5);
  const [singleStockDate, setSingleStockDate] = useState<Dayjs | null>(dayjs());
  const [consensusModelIds, setConsensusModelIds] = useState<string[]>([]);
  const [singleStockLoading, setSingleStockLoading] = useState(false);
  const [availableModels, setAvailableModels] = useState<ModelCardOption[]>([]);
  const [kline, setKline] = useState<KlineItem[]>([]);
  const [prediction, setPrediction] = useState<SingleStockPredictionResponse | null>(null);
  // 目标代码联想搜索
  const [codeSuggestions, setCodeSuggestions] = useState<any[]>([]);
  const [showCodeSuggestions, setShowCodeSuggestions] = useState(false);

  // 挂载时预加载港股名称表（quanthk security_master，内存搜索）
  useEffect(() => {
    hkStockListService.load().catch((err) => {
      console.warn('[InferenceCenterHK] 港股名称加载失败，联想降级:', err);
    });
  }, []);

  // 输入防抖联想：代码或名称模糊匹配
  useEffect(() => {
    const kw = inputCode.trim();
    if (!showCodeSuggestions || kw.length < 2) {
      setCodeSuggestions([]);
      return;
    }
    const timer = setTimeout(() => {
      try {
        // 输入已是前缀式(如 SH600036)时取纯数字部分匹配，兼容本地 code 字段
        const digits = kw.replace(/^(SH|SZ|BJ)/i, '').replace(/[^\dA-Za-z]/g, '');
        const results = hkStockListService.isLoaded() ? hkStockListService.search(kw, 8) : [];
        setCodeSuggestions(results);
      } catch {
        setCodeSuggestions([]);
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [inputCode, showCodeSuggestions]);

  const handleSelectSuggestion = (stock: { symbol: string; name?: string }) => {
    // HK 服务条目 symbol 已是 0700.HK 后缀式，统一走前缀归一
    const normalized = normalizeStockCode(stock.symbol);
    setShowCodeSuggestions(false);
    setCodeSuggestions([]);
    handleCommitSingleCode(normalized);
  };

  // ─────────────────────────────────────────────────────────────
  // 截面推理：按当前市场加载注册模型（用户模型 + 系统模型）
  // ─────────────────────────────────────────────────────────────
  const loadRegisteredModels = useCallback(async () => {
    setModelsLoading(true);
    try {
      const marketUpper = currentMarket.toUpperCase();
      const [uRes, sList] = await Promise.all([
        modelTrainingService.listUserModels(false, marketUpper).catch(() => ({ items: [], total: 0 })),
        modelTrainingService.listSystemModels(marketUpper).catch(() => []),
      ]);
      const activeUser = (uRes.items || []).filter((m) => m.status !== 'archived');
      const activeSys = (sList || []).map(systemModelToUserModel);
      const combined = [...activeUser, ...activeSys];
      setRegisteredModels(combined);

      if (combined.length > 0) {
        if (!selectedModelId || !combined.some((m) => m.model_id === selectedModelId)) {
          const defaultM = combined.find((m) => m.is_default) || combined[0];
          setSelectedModelId(defaultM.model_id);
        }
      } else if (selectedModelId) {
        // 当前市场没有可用模型：清空选择，避免残留其他市场的模型
        setSelectedModelId('');
      }
    } catch (err) {
      console.error('加载注册模型失败:', err);
    } finally {
      setModelsLoading(false);
    }
  }, [selectedModelId, currentMarket]);

  useEffect(() => {
    void loadRegisteredModels();
  }, [loadRegisteredModels]);

  const selectedModel = useMemo(() => {
    return registeredModels.find((m) => m.model_id === selectedModelId) || registeredModels[0] || null;
  }, [registeredModels, selectedModelId]);

  const horizonDays = useMemo(() => {
    if (!selectedModel) return 5;
    const meta = getMeta(selectedModel);
    return Number(meta?.target_horizon_days ?? meta?.target_horizon ?? 5);
  }, [selectedModel]);

  // 截面推理：Precheck
  const loadPrecheck = useCallback(async (modelId: string, checkDate?: string) => {
    setInferencePrecheckLoading(true);
    try {
      const resp = await modelTrainingService.precheckInference(modelId, checkDate);
      setInferencePrecheck(resp);
      if (resp?.prediction_trade_date) {
        setInferenceTargetDate(resp.prediction_trade_date);
      }
      return resp;
    } catch {
      setInferencePrecheck(null);
      return null;
    } finally {
      setInferencePrecheckLoading(false);
    }
  }, []);

  // 截面推理：目标日期
  const loadInferenceTargetDate = useCallback(async () => {
    if (!inferenceDate) {
      setInferenceTargetDate('—');
      return;
    }
    setInferenceTargetLoading(true);
    try {
      const base = inferenceDate.format('YYYY-MM-DD');
      const resolved = await modelTrainingService.resolveInferenceDateByCalendar(marketConfig.calendar, base);
      const predicted = await modelTrainingService.calcTargetDateByCalendar(marketConfig.calendar, resolved.date, horizonDays);
      setInferenceTargetDate(predicted || '—');
    } catch {
      setInferenceTargetDate('—');
    } finally {
      setInferenceTargetLoading(false);
    }
  }, [inferenceDate, horizonDays, marketConfig.calendar]);

  // 截面推理：历史与状态
  const loadInferenceHistory = useCallback(async (
    modelId: string,
    options?: { runId?: string; status?: string; inferenceDate?: string; page?: number; pageSize?: number }
  ) => {
    setInferenceHistoryLoading(true);
    try {
      const resp = await modelTrainingService.listInferenceHistory(modelId, {
        runId: options?.runId,
        status: options?.status,
        inferenceDate: options?.inferenceDate,
        page: options?.page ?? 1,
        pageSize: options?.pageSize ?? 20,
      });
      setInferenceHistory(resp.items);
      if (lastInferenceRun === null) {
        const firstCompleted = resp.items.find((r) => r.status === 'completed') ?? null;
        setLastInferenceRun(firstCompleted);
      }
    } catch {
      setInferenceHistory([]);
    } finally {
      setInferenceHistoryLoading(false);
    }
  }, [lastInferenceRun]);

  const loadAutoSettings = useCallback(async (modelId: string) => {
    try {
      const s = await modelTrainingService.getAutoInferenceSettings(modelId);
      setAutoSettings(s);
    } catch {
      setAutoSettings(null);
    }
  }, []);

  const loadLatestInferenceRun = useCallback(async (modelId: string) => {
    setLatestInferenceRunLoading(true);
    try {
      const latest = await modelTrainingService.getLatestInferenceRun(modelId);
      setLatestInferenceRun(latest);
    } catch {
      setLatestInferenceRun(null);
    } finally {
      setLatestInferenceRunLoading(false);
    }
  }, []);

  const refreshCrossSectionPanel = useCallback(async (modelId: string) => {
    const currentDate = inferenceDate ? inferenceDate.format('YYYY-MM-DD') : undefined;
    await Promise.all([
      loadPrecheck(modelId, currentDate),
      loadAutoSettings(modelId),
      loadLatestInferenceRun(modelId),
    ]);
  }, [inferenceDate, loadAutoSettings, loadLatestInferenceRun, loadPrecheck]);

  useEffect(() => {
    if (selectedModel && topTab === 'cross-section' && crossSectionMode === 'single') {
      void refreshCrossSectionPanel(selectedModel.model_id);
    }
  }, [selectedModel?.model_id, topTab, crossSectionMode, inferenceDate, refreshCrossSectionPanel]);

  useEffect(() => {
    if (topTab === 'cross-section') {
      void loadInferenceTargetDate();
    }
  }, [topTab, loadInferenceTargetDate]);

  useEffect(() => {
    if (selectedModel && topTab === 'cross-section' && crossSectionMode === 'single') {
      void loadInferenceHistory(selectedModel.model_id, {
        runId: historyRunIdFilter || undefined,
        status: historyStatusFilter === 'all' ? undefined : historyStatusFilter,
        inferenceDate: historyDateFilter ? historyDateFilter.format('YYYY-MM-DD') : undefined,
        page: 1,
        pageSize: 20,
      });
    }
  }, [selectedModel?.model_id, topTab, crossSectionMode, historyRunIdFilter, historyStatusFilter, historyDateFilter, loadInferenceHistory]);

  useEffect(() => {
    setLastInferenceRun(null);
  }, [selectedModel?.model_id]);

  const handleRunCrossSectionInference = async () => {
    if (!selectedModel || !inferenceDate) return;
    setInferenceRunning(true);
    setLastInferenceRun(null);
    try {
      const requestedDateStr = inferenceDate.format('YYYY-MM-DD');
      const resolvedDate = await modelTrainingService.resolveInferenceDateByCalendar(marketConfig.calendar, requestedDateStr);
      const inferenceDateStr = resolvedDate.date;
      if (resolvedDate.adjusted && inferenceDateStr) {
        setInferenceDate(dayjs(inferenceDateStr));
        message.info(`所选日期 ${requestedDateStr} 非交易日，已自动回退到最近交易日 ${inferenceDateStr}`);
      }
      const precheck = await loadPrecheck(selectedModel.model_id, inferenceDateStr);
      if (!precheck?.passed) {
        message.error('前置检查未通过，请先处理阻断项');
        return;
      }
      const runInfo = await modelTrainingService.runModelInference(selectedModel.model_id, inferenceDateStr);
      setLastInferenceRun(runInfo);
      message.success(`截面推理已完成: 产物已入库（样本数: ${runInfo.signals_count}）`);
      void refreshCrossSectionPanel(selectedModel.model_id);
      void loadInferenceHistory(selectedModel.model_id);
    } catch (err: any) {
      message.error(`推理失败: ${err?.message ?? '未知错误'}`);
    } finally {
      setInferenceRunning(false);
    }
  };

  const handleSetDefaultModel = async () => {
    if (!selectedModel) return;
    const canonicalId = selectedModel.model_id.startsWith('sys-') ? selectedModel.model_id.slice(4) : selectedModel.model_id;
    try {
      await modelTrainingService.setDefaultModel(canonicalId);
      message.success(`已设为默认模型：${selectedModel.model_id}`);
      await loadRegisteredModels();
    } catch (err: any) {
      message.error(`设置失败: ${err?.message ?? '未知'}`);
    }
  };

  const handleToggleAuto = async (enabled: boolean) => {
    if (!selectedModel || !autoSettings) return;
    setAutoSaving(true);
    try {
      const next = { ...autoSettings, enabled };
      const saved = await modelTrainingService.saveAutoInferenceSettings(selectedModel.model_id, next);
      setAutoSettings(saved);
      message.success(enabled ? '自动推理已开启' : '自动推理已关闭');
    } catch {
      message.error('保存失败');
    } finally {
      setAutoSaving(false);
    }
  };

  const handleDeleteHistory = async (runId: string) => {
    if (!selectedModel) return;
    try {
      await modelTrainingService.deleteInferenceHistory(runId);
      message.success('历史记录已删除');
      void loadInferenceHistory(selectedModel.model_id);
      void loadLatestInferenceRun(selectedModel.model_id);
    } catch (err: any) {
      message.error(`删除失败: ${err?.message ?? '未知错误'}`);
    }
  };

  // ─────────────────────────────────────────────────────────────
  // 个股预测推理：获取可用模型与执行真实预测
  // ─────────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    inferenceCenterService
      .getAvailableModels(currentMarket)
      .then((list) => {
        if (cancelled) return;
        const liveModels: ModelCardOption[] = (list || [])
          .filter((m) => Boolean(m.modelId))
          .map((m) => {
          const kind = String(m.modelType || m.modelId || '').toLowerCase();
          const isDL =
            kind.includes('ensemble') || kind.includes('stacking') ? false :
            kind.includes('tft') || kind.includes('gru') || kind.includes('lstm') ||
            kind.includes('transformer') || kind.includes('pytorch') || kind.includes('tensorflow') ||
            kind.includes('dl');
          const isEns = kind.includes('ensemble') || kind.includes('stacking');
          return {
            ...m,
            category: isEns ? 'ensemble' : isDL ? 'dl' : 'tree',
            tag: m.hasInference ? '已训练' : '生产可用',
            horizonDesc: 'T+1 ~ T+10 灵活周期',
            sharpe: 0,
            quantileSupport: false,
          };
        });
        setAvailableModels(liveModels);
        if (liveModels.length > 0 && !singleStockModelId) {
          setSingleStockModelId(liveModels[0].modelId);
        }
      })
      .catch((err) => {
        console.warn('获取个股推理模型列表失败:', err);
      });
    return () => {
      cancelled = true;
    };
  }, [currentMarket, singleStockModelId]);

  const handleRunSingleStockInference = useCallback(async (
    targetSymbol?: string,
    targetModelId?: string,
    targetHorizon?: number
  ) => {
    const sym = (targetSymbol || symbol || '0700.HK').trim();
    const mId = targetModelId || singleStockModelId;
    const hor = targetHorizon || horizon;

    if (!sym) {
      message.warning('请输入有效的股票代码');
      return;
    }

    setPrediction(null);
    setSingleStockLoading(true);
    try {
      const klineData = await inferenceCenterService.getStockKline(sym, 60);
      if (klineData && klineData.length > 0) {
        setKline(klineData);
      }

      const dateStr = singleStockDate ? singleStockDate.format('YYYY-MM-DD') : undefined;
      const res = await inferenceCenterService.predictSingleStock({
        symbol: sym,
        model_id: mId || undefined,
        date: dateStr,
        horizon: hor,
        market: currentMarket,
        consensus_model_ids: consensusModelIds.length ? consensusModelIds : undefined,
        execute: Boolean(targetSymbol === undefined && targetModelId === undefined),
      });

      if (res && res.status === 'success') {
        setPrediction(res);
        if (!singleStockModelId && res.model_id) {
          setSingleStockModelId(res.model_id);
        }
      }
    } catch (e: any) {
      console.error('获取真实推理数据失败:', e);
      const apiMessage =
        e?.response?.data?.detail ||
        e?.response?.data?.error?.message ||
        e?.response?.data?.message;
      message.error(apiMessage || `推理接口异常: ${e?.message || '未知错误'}`);
    } finally {
      setSingleStockLoading(false);
    }
  }, [symbol, singleStockModelId, horizon, singleStockDate, currentMarket, consensusModelIds]);

  const filteredSingleModels = useMemo(() => {
    if (modelCategoryFilter === 'all') return availableModels;
    return availableModels.filter((m) => m.category === modelCategoryFilter);
  }, [availableModels, modelCategoryFilter]);

  const currentSelectedSingleModel = useMemo(() => {
    return availableModels.find((m) => m.modelId === singleStockModelId) || availableModels[0];
  }, [availableModels, singleStockModelId]);

  const handleCommitSingleCode = (raw: string) => {
    if (!raw.trim()) return;
    const normalized = normalizeStockCode(raw.trim());
    setSymbol(normalized);
    setInputCode(normalized);
    handleRunSingleStockInference(normalized);
  };

  const getRatingBadge = (rating: string) => {
    switch (rating) {
      case 'STRONG_BUY':
        return (
          <div className="flex items-center gap-1.5 text-rose-700 bg-rose-50/90 border border-rose-200/90 px-3 py-1 rounded-xl font-black text-xs">
            <span className="w-2 h-2 rounded-full bg-rose-500 animate-pulse" />
            强烈看多 (STRONG BUY)
          </div>
        );
      case 'BUY':
        return (
          <div className="flex items-center gap-1.5 text-red-600 bg-red-50/90 border border-red-200 px-3 py-1 rounded-xl font-black text-xs">
            <span className="w-2 h-2 rounded-full bg-red-500" />
            偏多研判 (BUY)
          </div>
        );
      case 'HOLD':
        return (
          <div className="flex items-center gap-1.5 text-slate-700 bg-slate-100 border border-slate-200 px-3 py-1 rounded-xl font-black text-xs">
            <span className="w-2 h-2 rounded-full bg-slate-400" />
            中性观望 (HOLD)
          </div>
        );
      default:
        return (
          <div className="flex items-center gap-1.5 text-emerald-700 bg-emerald-50 border border-emerald-200 px-3 py-1 rounded-xl font-black text-xs">
            <span className="w-2 h-2 rounded-full bg-emerald-500" />
            看空警示 (SELL)
          </div>
        );
    }
  };

  return (
    <div className="w-full h-full bg-[#f8fafc] p-6 flex flex-col overflow-hidden font-sans box-border select-none">
      {/* 顶部主切换栏 */}
      <div className="flex items-center justify-between bg-white border border-gray-200 rounded-2xl px-6 py-3 mb-4 shadow-2xs shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-2xl bg-gradient-to-tr from-blue-600 to-indigo-500 flex items-center justify-center text-white shadow-md shadow-blue-200">
            <Cpu className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-base font-black text-slate-800 m-0 tracking-tight">模型推理中心</h1>
              <Tag color="blue" className="rounded-full text-[10px] font-bold border-0 px-2 py-0">
                '港股市场'
              </Tag>
            </div>
            <p className="text-[11px] text-slate-400 m-0">生产级截面批量打分 · 单标的特征归因与共识走势预测</p>
          </div>
        </div>

        {/* 顶部一级模式切换 */}
        <div className="flex items-center bg-slate-100/80 p-1 rounded-xl border border-slate-200/60">
          <button
            type="button"
            onClick={() => setTopTab('cross-section')}
            className={clsx(
              'flex items-center gap-2 px-5 py-1.5 rounded-lg text-xs font-bold transition-all',
              topTab === 'cross-section'
                ? 'bg-white text-blue-600 shadow-sm border border-slate-200/50'
                : 'text-slate-500 hover:text-slate-800'
            )}
          >
            <LayoutGrid size={14} />
            市场截面推理
          </button>
          <button
            type="button"
            onClick={() => setTopTab('individual')}
            className={clsx(
              'flex items-center gap-2 px-5 py-1.5 rounded-lg text-xs font-bold transition-all',
              topTab === 'individual'
                ? 'bg-white text-blue-600 shadow-sm border border-slate-200/50'
                : 'text-slate-500 hover:text-slate-800'
            )}
          >
            <TrendingUp size={14} />
            个股预测推理
          </button>
        </div>
      </div>

      {/* ================= 模式 1：市场截面推理 ================= */}
      {topTab === 'cross-section' && (
        <div className="flex-1 min-h-0 bg-white border border-gray-200 shadow-sm rounded-[28px] flex flex-col overflow-hidden">
          {/* 模型选择与二级导航 Bar */}
          <div className="px-6 py-3.5 border-b border-gray-200 bg-slate-50/50 flex flex-wrap items-center justify-between gap-4 shrink-0">
            {/* 模型选择 */}
            <div className="flex items-center gap-3">
              <span className="text-xs font-bold text-slate-500 flex items-center gap-1.5">
                <Database size={14} className="text-blue-500" />
                当前推理模型:
              </span>
              <Select
                value={selectedModelId}
                onChange={setSelectedModelId}
                loading={modelsLoading}
                className="w-72"
                options={registeredModels.map((m) => ({
                  value: m.model_id,
                  label: (
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-semibold truncate">{modelDisplayName(m)}</span>
                      {m.is_default && (
                        <Tag color="gold" className="!mr-0 text-[10px] scale-90">默认</Tag>
                      )}
                    </div>
                  ),
                }))}
              />
              {selectedModel && (
                <div className="flex items-center gap-2 text-[11px] text-slate-500 bg-white px-3 py-1 rounded-xl border border-slate-200">
                  <span>架构: <strong className="text-slate-700 font-mono">{extractModelType(selectedModel)}</strong></span>
                  <span className="text-slate-300">|</span>
                  <span>目标: <strong className="text-blue-600 font-mono">T+{horizonDays}</strong></span>
                  {selectedModel.is_default && (
                    <>
                      <span className="text-slate-300">|</span>
                      <span className="text-amber-600 font-bold flex items-center gap-0.5">
                        <Star size={11} fill="currentColor" /> 默认生效
                      </span>
                    </>
                  )}
                </div>
              )}
            </div>

            {/* 子模式切换 */}
            <div className="flex items-center gap-2">
              <Button
                size="small"
                type={crossSectionMode === 'single' ? 'primary' : 'default'}
                className={clsx('rounded-xl text-xs font-bold h-8 px-4', crossSectionMode === 'single' ? 'bg-blue-600 border-blue-600' : 'border-slate-200')}
                onClick={() => setCrossSectionMode('single')}
              >
                单日推理
              </Button>
              <Button
                size="small"
                type={crossSectionMode === 'history' ? 'primary' : 'default'}
                className={clsx('rounded-xl text-xs font-bold h-8 px-4', crossSectionMode === 'history' ? 'bg-indigo-600 border-indigo-600' : 'border-slate-200')}
                onClick={() => setCrossSectionMode('history')}
              >
                推理历史
              </Button>
              <Button
                size="small"
                type={crossSectionMode === 'backtest' ? 'primary' : 'default'}
                className={clsx('rounded-xl text-xs font-bold h-8 px-4', crossSectionMode === 'backtest' ? 'bg-amber-600 border-amber-600' : 'border-slate-200')}
                onClick={() => setCrossSectionMode('backtest')}
              >
                推理回测
              </Button>
            </div>
          </div>

          {/* 截面主体展示区 */}
          <div className="flex-1 min-h-0 p-6 overflow-y-auto custom-scrollbar">
            {!selectedModel ? (
              <div className="flex h-full items-center justify-center">
                <Spin />
              </div>
            ) : crossSectionMode === 'single' ? (
              <InferenceCenterPanel
                model={selectedModel}
                inferenceDate={inferenceDate}
                onDateChange={setInferenceDate}
                targetDate={inferenceTargetDate}
                targetDateLoading={inferenceTargetLoading}
                horizonDays={horizonDays}
                running={inferenceRunning}
                onRun={handleRunCrossSectionInference}
                onRunAsDefault={handleSetDefaultModel}
                isDefault={selectedModel.is_default}
                lastRun={lastInferenceRun}
                history={inferenceHistory}
                historyLoading={inferenceHistoryLoading}
                autoSettings={autoSettings}
                autoSaving={autoSaving}
                onToggleAuto={handleToggleAuto}
                latestInferenceRun={latestInferenceRun}
                latestInferenceRunLoading={latestInferenceRunLoading}
                precheck={inferencePrecheck}
                precheckLoading={inferencePrecheckLoading}
                onRefreshPrecheck={() => {
                  if (selectedModel) {
                    void loadPrecheck(selectedModel.model_id, inferenceDate?.format('YYYY-MM-DD'));
                  }
                }}
                historyRunIdFilter={historyRunIdFilter}
                onHistoryRunIdFilterChange={setHistoryRunIdFilter}
                historyStatusFilter={historyStatusFilter}
                onHistoryStatusFilterChange={setHistoryStatusFilter}
                historyDateFilter={historyDateFilter}
                onHistoryDateFilterChange={setHistoryDateFilter}
                onDeleteHistory={handleDeleteHistory}
              />
            ) : crossSectionMode === 'history' ? (
              <InferenceHistoryPanel modelId={selectedModel.model_id} onDelete={handleDeleteHistory} />
            ) : (
              <InferenceBacktestModule modelId={selectedModel.model_id} />
            )}
          </div>
        </div>
      )}

      {/* ================= 模式 2：个股预测推理 ================= */}
      {topTab === 'individual' && (
        <div className="flex-1 min-h-0 bg-white border border-gray-200 shadow-sm rounded-[28px] flex overflow-hidden">
          {/* 左侧：个股参数配置 */}
          <div className="w-80 shrink-0 flex flex-col border-r border-gray-200 bg-white p-5 overflow-y-auto custom-scrollbar">
            <div className="flex items-center justify-between pb-3.5 border-b border-slate-100 mb-4 px-1">
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-xl bg-blue-50 border border-blue-100 flex items-center justify-center text-blue-600">
                  <Sliders className="w-4 h-4" />
                </div>
                <div>
                  <h3 className="text-sm font-black text-slate-800 m-0">个股预测配置</h3>
                  <p className="text-[10px] text-slate-400 m-0">目标代码 · 预测周期 · 模型选型</p>
                </div>
              </div>
              <span className="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.8)]" />
            </div>

            <div className="flex flex-col gap-3.5 mb-4">
              {/* 目标个股 */}
              <div className="flex flex-col gap-1.5">
                <span className="text-[11px] font-bold text-slate-500 flex items-center gap-1">
                  <Search className="w-3.5 h-3.5 text-blue-500" /> 目标代码
                </span>
                <div className="relative">
                  <div className="flex items-center bg-slate-50/70 border border-slate-200 hover:border-blue-400 focus-within:border-blue-500 focus-within:bg-white focus-within:ring-2 focus-within:ring-blue-100 rounded-xl px-3 py-1.5 transition-all shadow-2xs">
                    <Input
                      variant="borderless"
                      placeholder="输入代码/名称搜索 (如 600036 或 茅台)"
                      value={inputCode}
                      onChange={(e) => {
                        setInputCode(e.target.value.toUpperCase());
                        setShowCodeSuggestions(true);
                      }}
                      onFocus={() => setShowCodeSuggestions(true)}
                      onBlur={() => {
                        // 延迟提交，给下拉项的 click 留出触发窗口
                        window.setTimeout(() => {
                          setShowCodeSuggestions(false);
                          handleCommitSingleCode(inputCode);
                        }, 160);
                      }}
                      onPressEnter={() => handleCommitSingleCode(inputCode)}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape') setShowCodeSuggestions(false);
                      }}
                      className="p-0 font-mono font-bold text-sm text-blue-600 focus:outline-none"
                      style={{ flex: 1, minWidth: 100, padding: 0 }}
                    />
                    <div className="flex items-center gap-1 pl-2 border-l border-slate-200 shrink-0">
                      <span className="text-xs font-bold text-slate-700 select-none">
                        {prediction?.stock_name || '标的资产'}
                      </span>
                    </div>
                  </div>

                  {/* 联想下拉：本地股票列表内存搜索 */}
                  {showCodeSuggestions && codeSuggestions.length > 0 && (
                    <div className="absolute z-50 left-0 right-0 top-full mt-1 bg-white border border-slate-200 rounded-xl shadow-lg max-h-60 overflow-y-auto custom-scrollbar">
                      {codeSuggestions.map((s) => (
                        <div
                          key={s.symbol}
                          // 阻止 mousedown 抢先触发输入框 onBlur 提交
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => handleSelectSuggestion(s)}
                          className="px-3 py-2 hover:bg-blue-50 cursor-pointer border-b border-slate-50 last:border-b-0 flex items-center justify-between gap-2"
                        >
                          <span className="text-xs font-bold text-slate-700 truncate">
                            {s.name || s.symbol}
                          </span>
                          <span className="text-[11px] font-mono text-blue-600 shrink-0">
                            {s.symbol}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* 预测周期 */}
              <div className="flex flex-col gap-1.5">
                <span className="text-[11px] font-bold text-slate-500 flex items-center gap-1">
                  <Clock className="w-3.5 h-3.5 text-indigo-500" /> 预测周期 (Horizon)
                </span>
                <Select
                  value={horizon}
                  onChange={(val) => setHorizon(val)}
                  style={{ width: '100%', height: 34 }}
                  options={[
                    { label: 'T+1 次日预期', value: 1 },
                    { label: 'T+3 短线周期', value: 3 },
                    { label: 'T+5 一周趋势 (推荐)', value: 5 },
                    { label: 'T+10 双周展望', value: 10 },
                  ]}
                />
              </div>

              {/* 基准日期 */}
              <div className="flex flex-col gap-1.5">
                <span className="text-[11px] font-bold text-slate-500 flex items-center gap-1">
                  <Calendar className="w-3.5 h-3.5 text-amber-500" /> 基准日期 (支持盲测)
                </span>
                <DatePicker
                  value={singleStockDate}
                  onChange={(d) => setSingleStockDate(d)}
                  style={{ width: '100%', height: 34, borderRadius: 10 }}
                  allowClear={false}
                />
              </div>

              {/* 模型选型 */}
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-bold text-slate-500 flex items-center gap-1">
                    <Database className="w-3.5 h-3.5 text-purple-500" /> 模型选型
                  </span>
                  <div className="flex items-center gap-1 bg-slate-100 p-0.5 rounded-lg text-[10px]">
                    {(['all', 'dl', 'tree'] as const).map((cat) => (
                      <button
                        key={cat}
                        type="button"
                        onClick={() => setModelCategoryFilter(cat)}
                        className={clsx(
                          'px-1.5 py-0.5 rounded-md font-bold transition-all',
                          modelCategoryFilter === cat ? 'bg-white text-blue-600 shadow-2xs' : 'text-slate-500 hover:text-slate-800'
                        )}
                      >
                        {cat === 'all' ? '全部' : cat === 'dl' ? '深度' : '树模'}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="flex flex-col gap-2 max-h-48 overflow-y-auto custom-scrollbar pr-0.5">
                  {filteredSingleModels.map((m) => {
                    const isSelected = singleStockModelId === m.modelId;
                    return (
                      <div
                        key={m.modelId}
                        onClick={() => setSingleStockModelId(m.modelId)}
                        className={clsx(
                          'p-2.5 rounded-xl border transition-all cursor-pointer flex flex-col gap-1',
                          isSelected
                            ? 'bg-blue-50/80 border-blue-300 ring-2 ring-blue-100 shadow-xs'
                            : 'bg-slate-50/60 border-slate-200/80 hover:bg-slate-100/80 hover:border-slate-300'
                        )}
                      >
                        <div className="flex items-center justify-between">
                          <span className={clsx('text-xs font-bold truncate', isSelected ? 'text-blue-700' : 'text-slate-800')}>
                            {m.modelName}
                          </span>
                          <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-white/90 border border-slate-200 text-slate-500">
                            {m.tag}
                          </span>
                        </div>
                        <div className="flex items-center justify-between text-[10px] text-slate-400">
                          <span>{m.horizonDesc}</span>
                          <span>Sharpe: <strong className="text-slate-600">{m.sharpe.toFixed(2)}</strong></span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="mt-auto pt-3 border-t border-slate-100">
              <Button
                type="primary"
                block
                icon={<Play size={15} fill="currentColor" />}
                loading={singleStockLoading}
                onClick={() => handleRunSingleStockInference()}
                style={{
                  height: 40,
                  borderRadius: 12,
                  fontWeight: 800,
                  fontSize: 13,
                  background: 'linear-gradient(135deg, #2563eb, #3b82f6)',
                  boxShadow: '0 4px 14px rgba(37, 99, 235, 0.28)',
                }}
              >
                开始个股推理
              </Button>
            </div>
          </div>

          {/* 右侧：预测结果与图表 */}
          <div className="flex-1 min-w-0 flex flex-col bg-gray-50/50 overflow-hidden">
            {prediction ? (
              <div className="bg-white px-6 py-3.5 border-b border-gray-200 flex items-center justify-between shrink-0 z-10">
                <div className="flex items-center gap-4">
                  <div className="flex items-center gap-2">
                    <span className="text-lg font-black text-slate-800 tracking-tight">
                      {prediction.stock_name}
                    </span>
                    <span className="text-xs font-mono font-bold text-blue-600 bg-blue-50 px-2 py-0.5 rounded-lg border border-blue-100/80">
                      {prediction.symbol}
                    </span>
                  </div>
                  <div className="h-5 w-[1px] bg-slate-200" />
                  <div className="flex items-baseline gap-1.5 font-mono">
                    <span className="text-xs text-slate-400 font-sans font-medium">基准价格</span>
                    <span className="text-base font-black text-slate-900">
                      HK${prediction.current_price ? prediction.current_price.toFixed(2) : '—'}
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <div className="flex items-center gap-1.5 text-xs text-slate-500 bg-slate-50 px-3 py-1.5 rounded-xl border border-slate-200">
                    <span>模型信号分数:</span>
                    <strong className={clsx('font-mono font-black', prediction.expected_return >= 0 ? 'text-rose-600' : 'text-emerald-600')}>
                      {prediction.predicted_score.toFixed(4)}
                    </strong>
                  </div>
                  {getRatingBadge(prediction.rating)}
                  <div className="flex items-center gap-1.5 bg-rose-50/70 border border-rose-100 px-3 py-1 rounded-xl">
                    <span className="text-[11px] text-slate-500 font-semibold">来源:</span>
                    <span className="text-xs font-black font-mono text-rose-600">真实推理</span>
                  </div>
                </div>
              </div>
            ) : (
              <div className="bg-white px-6 py-4 border-b border-gray-200 flex items-center justify-between shrink-0 z-10">
                <span className="text-sm text-slate-400 font-semibold">请在左侧选择标的并点击「开始个股推理」</span>
              </div>
            )}

            <div className="flex-1 min-h-0 p-5 flex flex-col gap-4 overflow-y-auto custom-scrollbar">
              {singleStockLoading && !prediction ? (
                <div className="flex-1 flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-gray-200 shadow-xs min-h-[300px]">
                  <Spin size="large" />
                  <span className="text-xs font-semibold text-slate-500">正在接入真实推理引擎与行情...</span>
                </div>
              ) : prediction ? (
                <div className="flex-1 min-h-0 flex flex-col gap-4">
                  <div className="grid grid-cols-1 lg:grid-cols-3 gap-4" style={{ height: '420px', minHeight: '420px' }}>
                    <div className="lg:col-span-2 bg-white rounded-2xl border border-gray-200 shadow-xs flex flex-col overflow-hidden">
                      <StockForecastChart
                        kline={kline}
                        forecast={prediction.forecast_curve}
                        symbol={prediction.symbol}
                        stockName={prediction.stock_name}
                        currentPrice={prediction.current_price}
                        modelName={prediction.model_name || currentSelectedSingleModel?.modelName}
                        asOfDate={prediction.as_of_date}
                      />
                    </div>

                    <div className="bg-white rounded-2xl p-5 border border-gray-200 shadow-xs flex flex-col justify-between">
                      <div>
                        <div className="flex items-center justify-between pb-2.5 border-b border-slate-100 mb-3">
                          <span className="text-xs font-bold text-slate-500">模型推理指标</span>
                          <Tag color="blue" className="rounded font-mono text-[10px] m-0">Persisted Model Score</Tag>
                        </div>

                        <div className="p-3.5 bg-slate-50/80 rounded-2xl border border-slate-200/80 mb-3 text-center">
                          <span className="text-[11px] text-slate-400 font-semibold block mb-0.5">模型信号分数</span>
                          <span className={clsx('text-2xl font-black font-mono', prediction.expected_return >= 0 ? 'text-rose-600' : 'text-emerald-600')}>
                            {prediction.predicted_score.toFixed(4)}
                          </span>
                        </div>

                        <div className="p-3.5 bg-gradient-to-br from-slate-50 to-blue-50/30 rounded-2xl border border-slate-200/80">
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-xs font-bold text-slate-700">分位数区间</span>
                            <Tooltip title={prediction.p10_return != null && prediction.p90_return != null ? '真实 LightGBM 分位回归结果，区间已按验证集校准。' : '当前注册模型未启用分位推理，因此不展示估算区间。'}>
                              <Sparkles className="w-3.5 h-3.5 text-slate-400 cursor-pointer" />
                            </Tooltip>
                          </div>
                          {prediction.p10_return != null && prediction.p90_return != null ? (
                            <>
                              <div className="grid grid-cols-3 gap-2 text-center">
                                <div><div className="text-[10px] text-emerald-600">P10 下界</div><div className="font-mono text-xs font-bold text-emerald-700">{prediction.p10_return.toFixed(2)}%</div></div>
                                <div><div className="text-[10px] text-blue-600">P50 中枢</div><div className="font-mono text-xs font-bold text-blue-700">{(prediction.p50_return ?? 0).toFixed(2)}%</div></div>
                                <div><div className="text-[10px] text-rose-600">P90 上界</div><div className="font-mono text-xs font-bold text-rose-700">{prediction.p90_return.toFixed(2)}%</div></div>
                              </div>
                              <p className="mt-2 text-[10px] text-slate-400 m-0">验证集校准覆盖率：{prediction.confidence > 0 ? `${(prediction.confidence * 100).toFixed(1)}%` : '—'}</p>
                            </>
                          ) : (
                            <p className="text-[11px] leading-relaxed text-slate-400 m-0">该模型未启用分位推理；当前仅提供真实信号分数。</p>
                          )}
                        </div>
                      </div>

                      <div className="pt-2.5 border-t border-slate-100 text-[11px] text-slate-400 flex items-center justify-between">
                        <span>模型准确率 (IC): <strong className="text-slate-700 font-mono">{currentSelectedSingleModel?.accuracy != null && currentSelectedSingleModel.accuracy !== 0 ? (typeof currentSelectedSingleModel.accuracy === 'number' ? currentSelectedSingleModel.accuracy.toFixed(3) : currentSelectedSingleModel.accuracy) : '—'}</strong></span>
                        <span>绩效指标: <strong className="text-slate-700 font-mono">以注册信息为准</strong></span>
                      </div>
                    </div>
                  </div>

                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4" style={{ minHeight: '250px' }}>
                    <div className="bg-white rounded-2xl border border-gray-200 shadow-xs overflow-hidden">
                      <FeatureDriversPanel drivers={prediction.drivers} source={prediction.drivers_source} />
                    </div>
                    <div className="bg-white rounded-2xl border border-gray-200 shadow-xs overflow-hidden">
                      <ModelConsensusPanel
                        consensus={prediction.consensus}
                        consensusScore={prediction.consensus_score}
                        selectedCount={consensusModelIds.length}
                      />
                    </div>
                  </div>
                </div>
              ) : (
                <div className="flex-1 flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-dashed border-gray-200 text-slate-400 min-h-[300px]">
                  <Database size={28} className="opacity-30" />
                  <span className="text-xs font-semibold">请在左侧配置参数并点击「开始个股推理」查看多维量化分析</span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default InferenceCenterHkPage;
