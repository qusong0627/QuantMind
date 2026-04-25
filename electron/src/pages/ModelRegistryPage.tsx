import React, { useState, useEffect, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Layers, Star, RefreshCw, Search, Code, Calendar, Layers2,
  History, Archive, Brain, CheckCircle2, Clock, XCircle, X,
  ChevronRight, Play, Cpu, TrendingUp, Download, ChevronDown,
  ChevronUp, Shield, Zap, Activity, ListFilter,
} from 'lucide-react';
import {
  Button, Card, Tag, Typography, Empty, Spin, message,
  Progress, Divider, Row, Col, Input, Modal, Tabs, Switch,
  DatePicker, Table, Drawer, Badge, Tooltip, Collapse, Select,
} from 'antd';
import { clsx } from 'clsx';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import {
  modelTrainingService,
  UserModelRecord,
  SystemModelRecord,
  ModelTrainingRunStatus,
  InferenceRunRecord,
  InferencePrecheckResult,
  InferenceRankingResult,
  AutoInferenceSettings,
  LatestInferenceRunInfo,
  ModelShapSummaryResponse,
} from '../services/modelTrainingService';
import {
  calcTimeSplitStats,
  extractModelType,
  extractTimePeriods,
  formatTrendLabel,
  getMeta,
  getMetrics,
  getStatusConfig,
  isSystemModel,
  modelDisplayName,
  resolveMetricNumber,
  systemModelToUserModel,
} from './modelRegistryUtils';
import {
  ModelCard,
  ModelDetailPanel,
  TrainingSourcePanel,
  AttributionAnalysisPanel,
  InferenceCenterPanel,
  MetricCard,
  TimeItem,
  InfoCell,
} from './modelRegistryPanels';
import { PAGE_LAYOUT } from '../config/pageLayout';
const { Text } = Typography;

export const ModelRegistryPage: React.FC = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [userModels, setUserModels] = useState<UserModelRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [settingDefault, setSettingDefault] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [mainTab, setMainTab] = useState('detail');
  const [showConfigModal, setShowConfigModal] = useState(false);
  const [activeConfigTab, setActiveConfigTab] = useState<'meta' | 'metrics'>('meta');
  const [trainingRun, setTrainingRun] = useState<ModelTrainingRunStatus | null>(null);
  const [trainingRunLoading, setTrainingRunLoading] = useState(false);
  const [shapSummary, setShapSummary] = useState<ModelShapSummaryResponse | null>(null);
  const [shapLoading, setShapLoading] = useState(false);
  const [shapError, setShapError] = useState('');
  const [inferenceDate, setInferenceDate] = useState<dayjs.Dayjs | null>(dayjs().subtract(1, 'day'));
  const [inferenceRunning, setInferenceRunning] = useState(false);
  const [lastInferenceRun, setLastInferenceRun] = useState<InferenceRunRecord | null>(null);
  const [inferenceHistory, setInferenceHistory] = useState<InferenceRunRecord[]>([]);
  const [inferenceHistoryLoading, setInferenceHistoryLoading] = useState(false);
  const [inferencePrecheck, setInferencePrecheck] = useState<InferencePrecheckResult | null>(null);
  const [inferencePrecheckLoading, setInferencePrecheckLoading] = useState(false);
  const [inferenceTargetDate, setInferenceTargetDate] = useState<string>('—');
  const [inferenceTargetLoading, setInferenceTargetLoading] = useState(false);
  const [autoSettings, setAutoSettings] = useState<AutoInferenceSettings | null>(null);
  const [autoSaving, setAutoSaving] = useState(false);
  const [latestInferenceRun, setLatestInferenceRun] = useState<LatestInferenceRunInfo | null>(null);
  const [latestInferenceRunLoading, setLatestInferenceRunLoading] = useState(false);
  const [rankingOpen, setRankingOpen] = useState(false);
  const [rankingResult, setRankingResult] = useState<InferenceRankingResult | null>(null);
  const [rankingLoading, setRankingLoading] = useState(false);
  const [rankingExporting, setRankingExporting] = useState(false);
  const [rankingSearch, setRankingSearch] = useState('');
  const [historyRunIdFilter, setHistoryRunIdFilter] = useState('');
  const [historyStatusFilter, setHistoryStatusFilter] = useState<'all' | 'running' | 'completed' | 'failed'>('all');
  const [historyDateFilter, setHistoryDateFilter] = useState<dayjs.Dayjs | null>(null);

  const allModels = userModels;
  const activeModels = allModels.filter(m => m.status !== 'archived');
  const archivedModels = allModels.filter(m => m.status === 'archived');
  const displayModels = (showArchived ? allModels : activeModels).filter(m =>
    !searchQuery ||
    m.model_id.toLowerCase().includes(searchQuery.toLowerCase()) ||
    modelDisplayName(m).toLowerCase().includes(searchQuery.toLowerCase())
  );

  const selectedModel = userModels.find(m => m.model_id === selectedId) ?? null;
  const meta = selectedModel ? getMeta(selectedModel) : {} as ReturnType<typeof getMeta>;
  const metrics = selectedModel ? getMetrics(selectedModel) : {} as ReturnType<typeof getMetrics>;
  const timePeriods = selectedModel ? extractTimePeriods(getMeta(selectedModel)) : null;
  const horizonDays = Number(meta?.target_horizon_days ?? meta?.horizon_days ?? 3);

  const splitInferenceLogs = useCallback((stdout?: string | null, stderr?: string | null) => {
    const infoLines: string[] = [];
    const errorLines: string[] = [];
    const pushLines = (raw: string, source: 'stdout' | 'stderr') => {
      raw.split(/\r?\n/).forEach((line) => {
        const text = line.trimEnd();
        if (!text) return;
        const upper = text.toUpperCase();
        const isError = /\b(ERROR|CRITICAL|EXCEPTION|TRACEBACK|FAILED|FAILURE)\b/.test(upper);
        const isInfo = /\bINFO\b/.test(upper);
        const isWarn = /\b(WARNING|WARN)\b/.test(upper);
        if (isError) {
          errorLines.push(text);
          return;
        }
        if (source === 'stderr' && isInfo && !isWarn) {
          infoLines.push(text);
          return;
        }
        infoLines.push(text);
      });
    };
    if (stdout) pushLines(stdout, 'stdout');
    if (stderr) pushLines(stderr, 'stderr');
    return {
      stdout: infoLines.join('\n'),
      stderr: errorLines.join('\n'),
    };
  }, []);

  const loadModels = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const resp = await modelTrainingService.listUserModels(true);
      const items = resp.items ?? [];
      setUserModels(items);
      
      if (!selectedId && items.length > 0) {
        const def = items.find(m => m.is_default) ?? items[0];
        if (def) setSelectedId(def.model_id);
      }
    } catch (err: any) {
      message.error(`加载模型列表失败: ${err?.message ?? '未知错误'}`);
    } finally {
      setLoading(false);
    }
  }, [selectedId]);

  useEffect(() => { loadModels(); }, []);

  useEffect(() => {
    setMainTab('detail');
    setTrainingRun(null);
    setShapSummary(null);
    setShapLoading(false);
    setShapError('');
    setLastInferenceRun(null);
    setInferenceHistory([]);
    setInferencePrecheck(null);
    setAutoSettings(null);
    setLatestInferenceRun(null);
    setHistoryRunIdFilter('');
    setHistoryStatusFilter('all');
    setHistoryDateFilter(null);
    setInferenceTargetDate('—');
    setInferenceTargetLoading(false);
  }, [selectedId]);

  const handleTabChange = (key: string) => {
    setMainTab(key);
    if (key === 'training' && selectedModel?.source_run_id && !trainingRun) {
      loadTrainingRun(selectedModel.source_run_id);
    }
    if (key === 'attribution' && selectedModel && !shapSummary && !shapLoading) {
      void loadShapSummary(selectedModel.model_id);
    }
    if (key === 'inference' && selectedModel) {
      void refreshInferencePanel(selectedModel.model_id);
    }
  };

  const loadTrainingRun = useCallback(async (runId: string) => {
    setTrainingRunLoading(true);
    try {
      const run = await modelTrainingService.getTrainingRun(runId);
      setTrainingRun(run);
    } catch { setTrainingRun(null); }
    finally { setTrainingRunLoading(false); }
  }, []);

  const loadShapSummary = useCallback(async (modelId: string) => {
    setShapLoading(true);
    setShapError('');
    try {
      const summary = await modelTrainingService.getModelShapSummary(modelId);
      setShapSummary(summary);
    } catch (err: any) {
      setShapSummary(null);
      const detail = err?.response?.data?.detail;
      setShapError(String(detail || err?.message || '加载 SHAP 归因结果失败'));
    } finally {
      setShapLoading(false);
    }
  }, []);

  const loadPrecheck = useCallback(async (modelId: string, inferenceDate?: string) => {
    setInferencePrecheckLoading(true);
    try {
      const resp = await modelTrainingService.precheckInference(modelId, inferenceDate);
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

  const loadInferenceTargetDate = useCallback(async () => {
    if (!inferenceDate) {
      setInferenceTargetDate('—');
      return;
    }
    setInferenceTargetLoading(true);
    try {
      const base = inferenceDate.format('YYYY-MM-DD');
      const resolved = await modelTrainingService.resolveInferenceDateByCalendar('SSE', base);
      const predicted = await modelTrainingService.calcTargetDateByCalendar('SSE', resolved.date, horizonDays);
      setInferenceTargetDate(predicted || '—');
    } catch {
      setInferenceTargetDate('—');
    } finally {
      setInferenceTargetLoading(false);
    }
  }, [inferenceDate, horizonDays]);

  const loadInferenceHistory = useCallback(async (
    modelId: string,
    options?: {
      runId?: string;
      status?: string;
      inferenceDate?: string;
      page?: number;
      pageSize?: number;
    },
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
    } catch { setInferenceHistory([]); }
    finally { setInferenceHistoryLoading(false); }
  }, []);

  const loadAutoSettings = useCallback(async (modelId: string) => {
    try {
      const s = await modelTrainingService.getAutoInferenceSettings(modelId);
      setAutoSettings(s);
    } catch { setAutoSettings(null); }
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

  const refreshInferencePanel = useCallback(async (modelId: string) => {
    const currentDate = inferenceDate ? inferenceDate.format('YYYY-MM-DD') : undefined;
    await Promise.all([
      loadPrecheck(modelId, currentDate),
      loadAutoSettings(modelId),
      loadLatestInferenceRun(modelId),
    ]);
  }, [inferenceDate, loadAutoSettings, loadLatestInferenceRun, loadPrecheck]);

  useEffect(() => {
    if (selectedModel && mainTab === 'inference') {
      void refreshInferencePanel(selectedModel.model_id);
    }
  }, [selectedModel?.model_id, mainTab, inferenceDate, refreshInferencePanel]);

  useEffect(() => {
    if (mainTab === 'inference') {
      void loadInferenceTargetDate();
    }
  }, [mainTab, loadInferenceTargetDate]);

  useEffect(() => {
    if (selectedModel && mainTab === 'inference') {
      void loadInferenceHistory(selectedModel.model_id, {
        runId: historyRunIdFilter || undefined,
        status: historyStatusFilter === 'all' ? undefined : historyStatusFilter,
        inferenceDate: historyDateFilter ? historyDateFilter.format('YYYY-MM-DD') : undefined,
        page: 1,
        pageSize: 20,
      });
    }
  }, [selectedModel?.model_id, mainTab, historyRunIdFilter, historyStatusFilter, historyDateFilter, loadInferenceHistory]);

  const handleSetDefault = async () => {
    if (!selectedModel) return;
    await handleSetDefaultById(selectedModel.model_id);
  };

  const handleSetDefaultById = async (modelId: string) => {
    setSettingDefault(true);
    const canonicalId = modelId.startsWith('sys-') ? modelId.slice(4) : modelId;
    try {
      await modelTrainingService.setDefaultModel(canonicalId);
      message.success(`已设为默认模型：${modelId}`);
      await loadModels(true);
      setSelectedId(modelId);
    } catch (err: any) {
      message.error(`设置失败: ${err?.message ?? '未知'}`);
    } finally { setSettingDefault(false); }
  };

  const handleArchive = () => {
    if (!selectedModel) return;
    Modal.confirm({
      title: '归档模型',
      content: `确定归档 "${selectedModel.model_id}"？归档后不再参与推理，但数据不会删除。`,
      okText: '确认归档', okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setArchiving(true);
        try {
          await modelTrainingService.archiveUserModel(selectedModel.model_id);
          message.success('已归档');
          setSelectedId(null);
          await loadModels(true);
        } catch (err: any) {
          message.error(`归档失败: ${err?.message ?? '未知'}`);
        } finally { setArchiving(false); }
      },
    });
  };

  const handleRunInference = async () => {
    if (!selectedModel || !inferenceDate) return;
    setInferenceRunning(true);
    setLastInferenceRun(null);
    try {
      const requestedDateStr = inferenceDate.format('YYYY-MM-DD');
      const resolvedDate = await modelTrainingService.resolveInferenceDateByCalendar('SSE', requestedDateStr);
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
      const run = await modelTrainingService.runModelInference(
        selectedModel.model_id,
        inferenceDateStr,
      );
      setLastInferenceRun(run);
      if (run.success) {
        message.success(`推理完成，共生成 ${run.signals_count} 支排名信号`);
      } else {
        message.warning(run.error_message || run.fallback_reason || '推理执行完成但返回失败状态');
      }
      await refreshInferencePanel(selectedModel.model_id);
      await loadInferenceHistory(selectedModel.model_id, {
        runId: historyRunIdFilter || undefined,
        status: historyStatusFilter === 'all' ? undefined : historyStatusFilter,
        inferenceDate: historyDateFilter ? historyDateFilter.format('YYYY-MM-DD') : undefined,
        page: 1,
        pageSize: 20,
      });
    } catch (err: any) {
      message.error(`推理失败: ${err?.message ?? '未知'}`);
    } finally { setInferenceRunning(false); }
  };

  const handleToggleAuto = async (enabled: boolean) => {
    if (!selectedModel || !autoSettings) return;
    setAutoSaving(true);
    try {
      const next = { ...autoSettings, enabled };
      const saved = await modelTrainingService.saveAutoInferenceSettings(selectedModel.model_id, next);
      setAutoSettings(saved);
      message.success(enabled ? '自动推理已开启' : '自动推理已关闭');
    } catch { message.error('保存失败'); }
    finally { setAutoSaving(false); }
  };

  const handleViewRanking = async (runId: string) => {
    setRankingOpen(true);
    setRankingLoading(true);
    setRankingResult(null);
    setRankingSearch('');
    try {
      const r = await modelTrainingService.getInferenceResult(runId);
      setRankingResult(r);
    } catch { message.error('加载排名数据失败'); }
    finally { setRankingLoading(false); }
  };

  const handleExportCSV = () => {
    if (!rankingResult || rankingResult.rankings.length === 0) {
      message.warning('暂无可导出的排名数据');
      return;
    }
    setRankingExporting(true);
    const rows = [
      ['排名', '股票代码', '股票名称', '预测得分', '信号'],
      ...rankingResult.rankings.map(r => [r.rank, r.code, r.name, r.score, r.signal]),
    ];
    const escapeCsvCell = (value: unknown) => {
      const raw = value === null || value === undefined ? '' : String(value);
      if (!/[",\n\r]/.test(raw)) return raw;
      return `"${raw.replace(/"/g, '""')}"`;
    };
    try {
      const csv = rows.map(r => r.map(escapeCsvCell).join(',')).join('\n');
      const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `ranking_${rankingResult.target_date || 'result'}_${rankingResult.summary?.run_id || 'run'}.csv`;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      message.success(`已导出 ${rankingResult.rankings.length} 条排名数据`);
    } catch (err: any) {
      message.error(`导出失败: ${err?.message ?? '未知错误'}`);
    } finally {
      setRankingExporting(false);
    }
  };

  const targetDate = inferenceTargetDate || '—';

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <div className="flex flex-row h-full w-full overflow-hidden">
          {/* ═══ 左侧边栏 ═══ */}
          <div className="w-[300px] flex-shrink-0 border-r border-slate-100 bg-white flex flex-col shadow-lg shadow-slate-100/50 z-10 h-full overflow-hidden">
            {/* 顶部标题 + 搜索 */}
            <div className="px-5 pt-5 pb-4 border-b border-slate-50">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2.5">
                  <div className="w-8 h-8 rounded-xl bg-blue-600 flex items-center justify-center shadow shadow-blue-500/30 text-white">
                    <Layers size={17} />
                  </div>
                  <span className="text-[15px] font-black text-slate-800 tracking-tight">模型资产库</span>
                </div>
                <button
                  onClick={() => loadModels()}
                  className="w-7 h-7 flex items-center justify-center rounded-lg text-slate-400 hover:text-blue-600 hover:bg-blue-50 transition-all"
                >
                  <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
                </button>
              </div>
              <Input
                prefix={<Search size={13} className="text-slate-300" />}
                placeholder="搜索模型..."
                className="rounded-xl border-slate-100 bg-slate-50 h-9 text-xs"
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
              />
            </div>
            {/* 分类切换 */}
            <div className="px-4 pt-3 pb-2 flex gap-1">
              <button
                onClick={() => setShowArchived(false)}
                className={clsx(
                  'flex-1 py-1 rounded-lg text-[10px] font-black tracking-widest transition-all',
                  !showArchived ? 'bg-blue-600 text-white shadow shadow-blue-200' : 'text-slate-400 hover:text-slate-600'
                )}
              >使用中 ({activeModels.length})</button>
              <button
                onClick={() => setShowArchived(true)}
                className={clsx(
                  'flex-1 py-1 rounded-lg text-[10px] font-black tracking-widest transition-all',
                  showArchived ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-slate-600'
                )}
              >已归档 ({archivedModels.length})</button>
            </div>
            {/* 模型列表 */}
            <div className="flex-1 overflow-y-auto px-3 pb-5 space-y-1.5 custom-scrollbar">
              {loading ? (
                <div className="flex items-center justify-center py-16"><Spin /></div>
              ) : displayModels.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 gap-4">
                  <Empty description={<span className="text-xs text-slate-400">暂无模型</span>} />
                  <Button
                    type="primary" size="small"
                    className="rounded-xl bg-blue-600 border-none font-bold text-xs"
                    icon={<Brain size={12} />}
                    onClick={() => navigate('/model-training')}
                  >去训练模型</Button>
                </div>
              ) : (
                <>
                  <div className="px-2 pt-2 pb-1 flex items-center gap-1.5">
                    <Brain size={10} className="text-blue-500" />
                    <span className="text-[9px] font-black text-blue-500 tracking-widest">我的模型资产</span>
                  </div>
                  {displayModels.map(model => (
                    <ModelCard
                      key={model.model_id}
                      model={model}
                      isSelected={selectedId === model.model_id}
                      onClick={() => setSelectedId(model.model_id)}
                      onSetDefault={() => void handleSetDefaultById(model.model_id)}
                      canSetDefault={!model.is_default && model.status !== 'archived'}
                    />
                  ))}
                </>
              )}
            </div>
            {/* 底部操作 */}
            <div className="px-4 pb-4 pt-3 border-t border-slate-50">
              <Button
                type="primary" block
                icon={<Brain size={14} />}
                className="rounded-xl h-10 bg-slate-900 border-none font-black text-xs"
                onClick={() => navigate('/model-training')}
              >训练新模型</Button>
            </div>
          </div>
          {/* ═══ 右侧主区 ═══ */}
          <div className="flex-1 overflow-y-auto custom-scrollbar h-full bg-white relative">
            <AnimatePresence mode="wait">
              {!selectedModel ? (
                <motion.div
                  key="empty"
                  initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  className="h-full flex flex-col items-center justify-center gap-5 text-center"
                >
                  <div className="w-16 h-16 rounded-3xl bg-slate-100 flex items-center justify-center">
                    <Layers size={30} className="text-slate-300" />
                  </div>
                  <div>
                    <p className="font-black text-slate-300 tracking-widest text-sm">请选择模型</p>
                    <p className="text-xs text-slate-300 mt-1">从左侧选择模型查看详情</p>
                  </div>
                </motion.div>
              ) : (
                <motion.div
                  key={selectedModel.model_id}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -8 }}
                  className="p-8 max-w-5xl mx-auto"
                >
                  {/* ── 模型 Header ── */}
                  <div className="flex justify-between items-start mb-6">
                    <div>
                      <div className="flex flex-wrap items-center gap-2 mb-2">
                        {/* 状态 badge */}
                        <span className={clsx(
                          'px-2.5 py-1 rounded-xl text-[9px] font-black uppercase tracking-wider flex items-center gap-1 border',
                          getStatusConfig(selectedModel.status).bg,
                          getStatusConfig(selectedModel.status).color,
                          getStatusConfig(selectedModel.status).border,
                        )}>
                          {getStatusConfig(selectedModel.status).icon}
                          {getStatusConfig(selectedModel.status).label}
                        </span>
                        {selectedModel.is_default && (
                          <span className="flex items-center gap-1 px-2.5 py-1 rounded-xl text-[9px] font-black bg-amber-50 text-amber-600 border border-amber-200">
                            <Star size={9} fill="currentColor" /> 默认
                          </span>
                        )}
                        <Tag className="rounded-xl border-none text-[9px] font-black uppercase bg-cyan-50 text-cyan-600 m-0">
                          {extractModelType(selectedModel)}
                        </Tag>
                        {getMeta(selectedModel).feature_count && (
                          <Tag className="rounded-xl border-none text-[9px] font-black bg-purple-50 text-purple-600 m-0">
                            {getMeta(selectedModel).feature_count} 维
                          </Tag>
                        )}
                      </div>
                      <h2 className="text-2xl font-black text-slate-900 tracking-tight m-0 font-mono leading-tight">
                        {modelDisplayName(selectedModel)}
                      </h2>
                      <p className="text-xs text-slate-400 mt-1 font-mono">
                        {selectedModel.model_id}
                        {selectedModel.created_at && ` · 创建于 ${dayjs(selectedModel.created_at).format('YYYY-MM-DD')}`}
                      </p>
                      {getMeta(selectedModel).description && (
                        <p className="text-xs text-slate-500 mt-2 max-w-xl">{getMeta(selectedModel).description}</p>
                      )}
                    </div>
                    <div className="flex gap-2 flex-shrink-0">
                      <Button
                        icon={<Code size={13} />}
                        className="rounded-xl h-9 px-4 font-bold border-slate-200 text-xs"
                        onClick={() => setShowConfigModal(true)}
                      >配置</Button>
                      {selectedModel.status !== 'archived' && (
                        <>
                          <Button
                            icon={<Archive size={13} />}
                            className="rounded-xl h-9 px-4 font-bold border-slate-200 text-xs text-slate-500"
                            onClick={handleArchive}
                            loading={archiving}
                          >归档</Button>
                          {!selectedModel.is_default && (
                            <Button
                              type="primary"
                              icon={<Star size={13} />}
                              className="rounded-xl h-9 px-5 font-black bg-slate-900 border-none text-xs"
                              onClick={handleSetDefault}
                              loading={settingDefault}
                            >设为默认</Button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                  {/* ── 主 Tabs ── */}
                  <Tabs
                    activeKey={mainTab}
                    onChange={handleTabChange}
                    className="model-main-tabs"
                    items={[
                      {
                        key: 'detail',
                        label: <span className="text-xs font-black uppercase tracking-widest px-1">模型详情</span>,
                        children: <ModelDetailPanel model={selectedModel} />,
                      },
                      ...(selectedModel.source_run_id ? [{
                        key: 'training',
                        label: <span className="text-xs font-black uppercase tracking-widest px-1">训练溯源</span>,
                        children: (
                          <TrainingSourcePanel
                            model={selectedModel}
                            trainingRun={trainingRun}
                            loading={trainingRunLoading}
                          />
                        ),
                      }] : []),
                      {
                        key: 'attribution',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center gap-1.5">
                            <Brain size={11} />归因分析
                          </span>
                        ),
                        children: (
                          <AttributionAnalysisPanel
                            model={selectedModel}
                            shapSummary={shapSummary}
                            loading={shapLoading}
                            error={shapError}
                            onRefresh={() => {
                              if (selectedModel) {
                                void loadShapSummary(selectedModel.model_id);
                              }
                            }}
                          />
                        ),
                      },
                      {
                        key: 'inference',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center gap-1.5">
                            <Cpu size={11} />推理中心
                          </span>
                        ),
                        children: (
                          <InferenceCenterPanel
                            model={selectedModel}
                            inferenceDate={inferenceDate}
                            onDateChange={setInferenceDate}
                            targetDate={targetDate}
                            targetDateLoading={inferenceTargetLoading}
                            horizonDays={horizonDays}
                            running={inferenceRunning}
                            onRun={handleRunInference}
                            lastRun={lastInferenceRun}
                            history={inferenceHistory}
                            historyLoading={inferenceHistoryLoading}
                            onViewRanking={handleViewRanking}
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
                          />
                        ),
                      },
                    ]}
                  />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </div>
      {/* ═══ 配置 Modal ═══ */}
      <Modal
        title={null}
        open={showConfigModal}
        onCancel={() => setShowConfigModal(false)}
        footer={null}
        width={820}
        centered
        styles={{ 
          body: { padding: 0 },
          mask: { backdropFilter: 'blur(4px)', backgroundColor: 'rgba(0,0,0,0.2)' }
        }}
        className="config-modal-container"
      >
        <div className="bg-white rounded-2xl overflow-hidden flex flex-col">
          {/* Header */}
          <div className="px-8 py-6 border-b border-slate-50 flex items-center justify-between bg-white">
            <div className="flex items-center gap-4">
              <div className="w-11 h-11 bg-blue-50 rounded-2xl flex items-center justify-center text-blue-600 shadow-sm border border-blue-100">
                <Code size={20} />
              </div>
              <div>
                <h3 className="text-lg font-black text-slate-800 m-0 tracking-tight flex items-center gap-2">
                  配置文件浏览器
                  <span className="px-2 py-0.5 bg-slate-100 text-[9px] font-black text-slate-400 rounded-md tracking-widest">只读</span>
                </h3>
                <p className="text-[10px] text-slate-400 font-mono mt-0.5">{selectedModel?.model_id}</p>
              </div>
            </div>
            <button 
              onClick={() => setShowConfigModal(false)}
              className="p-2 hover:bg-slate-50 rounded-xl text-slate-300 hover:text-slate-500 transition-all"
            >
              <XCircle size={20} />
            </button>
          </div>
          {/* Tab Selector */}
          <div className="px-8 pt-4">
            <div className="flex bg-slate-50 p-1 rounded-xl w-fit">
              <button
                onClick={() => setActiveConfigTab('meta')}
                className={clsx(
                  "px-4 py-1.5 text-[10px] font-black tracking-widest rounded-lg transition-all",
                  activeConfigTab === 'meta' ? "bg-white text-blue-600 shadow-sm" : "text-slate-400 hover:text-slate-600"
                )}
              >
                元数据
              </button>
              <button
                onClick={() => setActiveConfigTab('metrics')}
                className={clsx(
                  "px-4 py-1.5 text-[10px] font-black tracking-widest rounded-lg transition-all",
                  activeConfigTab === 'metrics' ? "bg-white text-blue-600 shadow-sm" : "text-slate-400 hover:text-slate-600"
                )}
              >
                指标
              </button>
            </div>
          </div>
          {/* Code Area */}
          <div className="p-8">
            <div className="bg-slate-900 rounded-2xl p-6 border border-slate-800 shadow-inner relative group">
              <div className="absolute top-4 right-4 opacity-0 group-hover:opacity-100 transition-opacity">
                 <Tag className="bg-slate-800 border-slate-700 text-slate-400 text-[8px] font-mono">格式</Tag>
              </div>
              <pre className="text-[11px] font-mono text-emerald-400 leading-relaxed whitespace-pre-wrap max-h-[420px] overflow-auto custom-scrollbar scrollbar-dark">
                {activeConfigTab === 'meta'
                  ? JSON.stringify(selectedModel?.metadata_json ?? {}, null, 2)
                  : JSON.stringify(selectedModel?.metrics_json ?? {}, null, 2)}
              </pre>
            </div>
            {/* Actions */}
            <div className="mt-6 flex justify-between items-center">
              <div className="flex items-center gap-2 text-[10px] text-slate-400 font-bold">
                <Shield size={12} className="text-blue-500" />
                资产受保护资源，仅供审计查看
              </div>
              <div className="flex gap-3">
                <Button 
                  className="rounded-xl h-10 px-6 font-bold border-slate-100 text-slate-500 hover:bg-slate-50" 
                  onClick={() => setShowConfigModal(false)}
                >
                  关闭
                </Button>
                <Button 
                  type="primary" 
                  icon={<Download size={14} />}
                  className="rounded-xl h-10 px-8 font-black bg-blue-600 border-none shadow-lg shadow-blue-200" 
                  onClick={() => {
                    const txt = activeConfigTab === 'meta'
                      ? JSON.stringify(selectedModel?.metadata_json ?? {}, null, 2)
                      : JSON.stringify(selectedModel?.metrics_json ?? {}, null, 2);
                    navigator.clipboard.writeText(txt);
                    message.success('已复制到剪贴板');
                  }}
                >
                  复制内容
                </Button>
              </div>
            </div>
          </div>
        </div>
      </Modal>
      {/* ═══ 排名结果 Drawer ═══ */}
      <Drawer
        open={rankingOpen}
        onClose={() => { setRankingOpen(false); setRankingResult(null); }}
        width={580}
        closable={false}
        zIndex={20000}
        styles={{ 
          header: { padding: '16px 20px', borderBottom: '1px solid #f8fafc' },
          body: { padding: '24px' } 
        }}
        title={
          <div className="flex items-center gap-3 min-w-0">
            <button 
              onClick={() => { setRankingOpen(false); setRankingResult(null); }}
              className="window-no-drag group flex items-center justify-center w-8 h-8 rounded-xl hover:bg-slate-50 text-slate-400 hover:text-slate-900 transition-all cursor-pointer relative z-50 flex-shrink-0"
            >
              <X size={18} className="transition-transform group-hover:scale-110" />
            </button>
            <div className="w-10 h-10 bg-blue-50 rounded-2xl flex items-center justify-center text-blue-600 shadow-sm border border-blue-100/50 flex-shrink-0">
              <TrendingUp size={20} />
            </div>
            <div className="flex flex-col min-w-0">
              <span className="font-black text-slate-800 text-base tracking-tight leading-none truncate">
                排名结果
              </span>
              {rankingResult && (
                <span className="text-[10px] font-bold text-slate-400 mt-1 uppercase tracking-widest truncate">
                  目标交易日：{rankingResult.target_date}
                </span>
              )}
            </div>
          </div>
        }
        extra={
          <Tooltip title={!rankingResult?.rankings.length ? '当前没有可导出的排名数据' : '导出当前排名结果为 CSV'}>
            <Button
              type="default"
              icon={<Download size={14} className={rankingExporting ? 'animate-pulse' : ''} />}
              className="rounded-xl h-9 px-4 font-black border-slate-200 text-[11px] shadow-sm hover:translate-y-[-1px] transition-all flex-shrink-0"
              disabled={rankingExporting || !rankingResult || rankingResult.rankings.length === 0}
              loading={rankingExporting}
              onClick={handleExportCSV}
            >
              {rankingExporting ? '导出中...' : '导出 CSV'}
            </Button>
          </Tooltip>
        }
      >
        {rankingLoading ? (
          <div className="flex items-center justify-center h-48"><Spin size="large" /></div>
        ) : rankingResult ? (
          <div className="space-y-3">
            {rankingResult.summary && (
              <div className="space-y-3 rounded-2xl border border-slate-100 bg-slate-50 p-3">
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <Text className="text-[10px] text-slate-400 font-black uppercase block">运行批次</Text>
                    <Text className="text-xs font-black text-slate-800 font-mono">{rankingResult.summary.run_id}</Text>
                  </div>
                  <div>
                    <Text className="text-[10px] text-slate-400 font-black uppercase block">模型</Text>
                    <Text className="text-xs font-black text-slate-800 font-mono">{rankingResult.summary.effective_model_id || rankingResult.summary.model_id}</Text>
                  </div>
                  <div>
                    <Text className="text-[10px] text-slate-400 font-black uppercase block">状态</Text>
                    <Tag color={rankingResult.summary.status === 'failed' ? 'red' : 'green'} className="m-0 rounded-full text-[9px] font-black">
                      {rankingResult.summary.status === 'failed' ? '失败' : rankingResult.summary.status === 'completed' ? '成功' : '进行中'}
                    </Tag>
                  </div>
                  <div>
                    <Text className="text-[10px] text-slate-400 font-black uppercase block">信号数</Text>
                    <Text className="text-xs font-black text-slate-800">{rankingResult.summary.signals_count}</Text>
                  </div>
                  <div>
                    <Text className="text-[10px] text-slate-400 font-black uppercase block">兜底</Text>
                    <Text className="text-xs font-black text-slate-800">{rankingResult.summary.fallback_used ? '是' : '否'}</Text>
                  </div>
                  <div>
                    <Text className="text-[10px] text-slate-400 font-black uppercase block">耗时</Text>
                    <Text className="text-xs font-black text-slate-800">{(Number(rankingResult.summary.duration_ms || 0) / 1000).toFixed(1)}s</Text>
                  </div>
                </div>
                <Collapse
                  key={rankingResult.summary.run_id}
                  ghost
                  className="inference-result-collapse"
                  defaultActiveKey={rankingResult.summary.status === 'failed' ? ['diagnostics', 'precheck', 'stderr'] : []}
                  items={[
                    {
                      key: 'diagnostics',
                      label: <span className="text-[11px] font-black text-slate-700">诊断信息</span>,
                      children: (
                        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                          <div className="rounded-xl border border-slate-100 bg-white p-3">
                            <Text className="text-[10px] text-slate-400 font-black uppercase block">失败阶段</Text>
                            <Text className="text-xs font-black text-slate-800">{rankingResult.summary.failure_stage || '—'}</Text>
                          </div>
                          <div className="rounded-xl border border-slate-100 bg-white p-3">
                            <Text className="text-[10px] text-slate-400 font-black uppercase block">回退原因</Text>
                            <Text className="text-xs font-black text-slate-800 break-all">{rankingResult.summary.fallback_reason || '—'}</Text>
                          </div>
                          <div className="rounded-xl border border-slate-100 bg-white p-3">
                            <Text className="text-[10px] text-slate-400 font-black uppercase block">实际模型</Text>
                            <Text className="text-xs font-black text-slate-800 font-mono break-all">
                              {rankingResult.summary.active_model_id || '—'}
                            </Text>
                          </div>
                          <div className="rounded-xl border border-slate-100 bg-white p-3">
                            <Text className="text-[10px] text-slate-400 font-black uppercase block">生效模型</Text>
                            <Text className="text-xs font-black text-slate-800 font-mono break-all">
                              {rankingResult.summary.effective_model_id || '—'}
                            </Text>
                          </div>
                          <div className="rounded-xl border border-slate-100 bg-white p-3 sm:col-span-2">
                            <Text className="text-[10px] text-slate-400 font-black uppercase block">数据源</Text>
                            <Text className="text-xs font-black text-slate-800 font-mono break-all">
                              {rankingResult.summary.active_data_source || '—'}
                            </Text>
                          </div>
                          <div className="rounded-xl border border-slate-100 bg-white p-3 sm:col-span-2">
                            <Text className="text-[10px] text-slate-400 font-black uppercase block">错误信息</Text>
                            <Text className="text-xs font-black text-rose-600 break-all">
                              {rankingResult.summary.error_message || rankingResult.summary.error_msg || '—'}
                            </Text>
                          </div>
                        </div>
                      ),
                    },
                    {
                      key: 'precheck',
                      label: <span className="text-[11px] font-black text-slate-700">前置检查</span>,
                      children: (() => {
                        const precheck = (rankingResult.summary?.result_json as any)?.precheck || (rankingResult.summary?.request_json as any)?.precheck || null;
                        if (!precheck) {
                          return <Empty description={<span className="text-xs text-slate-400">暂无前置检查记录</span>} />;
                        }
                        const items = Array.isArray(precheck.items) ? precheck.items : [];
                        return (
                          <div className="space-y-2">
                            <div className="flex flex-wrap gap-2">
                              <Tag color={precheck.passed ? 'green' : 'red'} className="m-0 rounded-full text-[9px] font-black">
                                {precheck.passed ? '通过' : '阻断'}
                              </Tag>
                              <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">
                                {precheck.effective_model_id || precheck.model_id || '—'}
                              </Tag>
                              <Tag className="m-0 rounded-full border-0 bg-blue-50 text-blue-700 font-bold">
                                {precheck.prediction_trade_date || '—'}
                              </Tag>
                            </div>
                            <div className="space-y-2">
                              {items.length > 0 ? items.map((item: any) => (
                                <div
                                  key={item.key}
                                  className={clsx(
                                    'flex items-start justify-between gap-3 rounded-xl border px-3 py-2',
                                    item.passed ? 'border-slate-100 bg-white' : 'border-rose-100 bg-rose-50/60',
                                  )}
                                >
                                  <div className="min-w-0">
                                    <div className="flex items-center gap-2">
                                      {item.passed ? <CheckCircle2 size={11} className="text-emerald-500 flex-shrink-0" /> : <XCircle size={11} className="text-rose-500 flex-shrink-0" />}
                                      <Text className="text-[11px] font-black text-slate-800">{item.label}</Text>
                                      <Tag className={clsx('m-0 rounded-full border-0 text-[9px] font-bold', item.severity === 'hard' ? 'bg-rose-50 text-rose-500' : 'bg-slate-100 text-slate-500')}>
                                        {item.severity === 'hard' ? '硬门禁' : '提示'}
                                      </Tag>
                                    </div>
                                    <Text className="mt-1 block text-[10px] text-slate-500 break-all">{item.detail}</Text>
                                  </div>
                                  <Tag color={item.passed ? 'green' : 'red'} className="m-0 rounded-full text-[9px] font-black">
                                    {item.passed ? '通过' : '未通过'}
                                  </Tag>
                                </div>
                              )) : (
                                <Empty description={<span className="text-xs text-slate-400">暂无检查明细</span>} />
                              )}
                            </div>
                          </div>
                        );
                      })(),
                    },
                    {
                      key: 'stdout',
                      label: <span className="text-[11px] font-black text-slate-700">标准输出</span>,
                      children: (() => {
                        const logs = splitInferenceLogs(rankingResult.summary.stdout, rankingResult.summary.stderr);
                        return logs.stdout ? (
                          <div className="rounded-xl border border-slate-200 bg-slate-950 p-3">
                            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all text-[10px] leading-relaxed text-emerald-400 custom-scrollbar scrollbar-dark">
                              {logs.stdout}
                            </pre>
                          </div>
                        ) : (
                          <Empty description={<span className="text-xs text-slate-400">暂无标准输出</span>} />
                        );
                      })(),
                    },
                    {
                      key: 'stderr',
                      label: <span className="text-[11px] font-black text-slate-700">错误输出</span>,
                      children: (() => {
                        const logs = splitInferenceLogs(rankingResult.summary.stdout, rankingResult.summary.stderr);
                        return logs.stderr ? (
                          <div className="rounded-xl border border-rose-100 bg-rose-50/70 p-3">
                            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all text-[10px] leading-relaxed text-rose-700 custom-scrollbar">
                              {logs.stderr}
                            </pre>
                          </div>
                        ) : (
                          <Empty description={<span className="text-xs text-slate-400">暂无错误输出</span>} />
                        );
                      })(),
                    },
                  ]}
                />
              </div>
            )}
            <Input
              prefix={<Search size={13} className="text-slate-300" />}
              placeholder="搜索股票代码或名称..."
              value={rankingSearch}
              onChange={e => setRankingSearch(e.target.value)}
              className="rounded-xl h-9 text-xs border-slate-200"
            />
            <Table
              size="small"
              rowKey="rank"
              pagination={{ pageSize: 20, showTotal: t => `共 ${t} 支` }}
              dataSource={rankingResult.rankings.filter(r =>
                !rankingSearch || r.code.includes(rankingSearch) || r.name.includes(rankingSearch)
              )}
              columns={[
                {
                  title: '排名', dataIndex: 'rank', width: 56,
                  render: (n: number) => (
                    <span className={clsx('font-black text-xs', n <= 3 ? 'text-amber-500' : 'text-slate-500')}>
                      {n <= 3 ? ['🥇', '🥈', '🥉'][n - 1] : n}
                    </span>
                  ),
                },
                {
                  title: '股票', key: 'stock',
                  render: (_: any, r: any) => (
                    <div>
                      <div className="text-xs font-black text-slate-800">{r.name}</div>
                      <div className="text-[10px] font-mono text-slate-400">{r.code}</div>
                    </div>
                  ),
                },
                {
                  title: '得分', dataIndex: 'score',
                  render: (s: number) => (
                    <span className="font-black text-xs text-slate-900">
                      {s.toFixed(4)}
                    </span>
                  ),
                },
                {
                  title: '信号', dataIndex: 'signal',
                  render: (sig: string) => {
                    const map: Record<string, { color: string; label: string }> = {
                      buy: { color: 'green', label: '↑ 做多' },
                      sell: { color: 'red', label: '↓ 做空' },
                      hold: { color: 'default', label: '→ 持有' },
                    };
                    const c = map[sig] ?? map.hold;
                    return <Tag color={c.color} className="text-[9px] font-black">{c.label}</Tag>;
                  },
                },
              ]}
            />
          </div>
        ) : (
          <Empty description={<span className="text-xs text-slate-400">暂无数据</span>} />
        )}
      </Drawer>
    </div>
  );
};

export default ModelRegistryPage;
