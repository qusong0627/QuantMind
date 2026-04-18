import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AnimatePresence, motion } from 'framer-motion';
import { 
  Brain, ChevronRight, Play, Settings2, BarChart, Database, 
  Copy, Sparkles, RefreshCcw, Target 
} from 'lucide-react';
import { 
  Button, Space, Tag, Typography, message, Card 
} from 'antd';
import dayjs, { Dayjs } from 'dayjs';
import { clsx } from 'clsx';
import { PAGE_LAYOUT } from '../config/pageLayout';
import { modelTrainingService } from '../services/modelTrainingService';
import { TrainingTarget, TrainingParams, TrainingContext, TrainingStatus, TrainingDraft, SplitKey, TimePeriodMap, FeatureCategory, STORAGE_KEY, DEFAULT_FEATURE_CATEGORIES, PRESET_DEFAULT_FEATURES, DEFAULT_TIME_PERIODS, DEFAULT_TARGET, DEFAULT_PARAMS, DEFAULT_CONTEXT, buildAutoDisplayName, buildLabelFormula, buildEffectiveTradeDate, daysBetween, toISOStringRange, restoreRange, shouldMigrateLegacyDraftPeriods, buildTrainingRequest, formatRange, toDynamicCategories, TrainingResult, buildBackendTrainingPayload, parseTrainingResult, parseSuggestedTimePeriods } from './training/trainingUtils';
import { AdminModelFeatureDataCoverage } from '../features/admin/types';
import { FeatureSelector } from './training/FeatureSelector';
import { TrainingTargetConfig } from './training/TrainingTargetConfig';
import { ParameterConfig } from './training/ParameterConfig';
import { TrainingConsole } from './training/TrainingConsole';
import { TrainingResultView } from './training/TrainingResultView';

const { Title, Paragraph } = Typography;

const TRAINING_MODULES = [
  { title: '特征选择', description: '筛选输入因子', icon: Database, hint: '第一步' },
  { title: '训练目标', description: '定义 T+N 标签口径', icon: Target, hint: '第二步' },
  { title: '参数配置', description: '设置超参与训练上下文', icon: Settings2, hint: '第三步' },
  { title: '执行训练', description: '编排请求与日志预览', icon: Play, hint: '第四步' },
  { title: '结果入库', description: '查看元数据与产物', icon: BarChart, hint: '第五步' },
];

const TRAINING_PAGE_BOTTOM_SAFE_CLASS = 'pb-[30px]';
let draftRestoreNoticeShown = false;

const MetricCard: React.FC<{
  label: string;
  value: string;
  hint?: string;
  centered?: boolean;
}> = ({ label, value, hint, centered = false }) => (
  <div className={clsx('rounded-2xl border border-slate-200 bg-white p-4 shadow-sm', centered && 'text-center')}>
    <div className={clsx('text-[10px] font-black uppercase tracking-[0.18em] text-slate-400', centered && 'text-center')}>{label}</div>
    <div className={clsx('mt-2 text-lg font-semibold text-slate-900', centered && 'text-center')}>{value}</div>
    {hint ? <div className={clsx('mt-1 text-xs text-slate-500', centered && 'text-center')}>{hint}</div> : null}
  </div>
);

export const ModelTrainingPage: React.FC = () => {
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState(0);
  const [featureCategories, setFeatureCategories] = useState<FeatureCategory[]>(DEFAULT_FEATURE_CATEGORIES);
  const [featureCatalogLoading, setFeatureCatalogLoading] = useState(false);
  const [dataCoverage, setDataCoverage] = useState<AdminModelFeatureDataCoverage | null>(null);
  const [selectedFeatures, setSelectedFeatures] = useState<string[]>(PRESET_DEFAULT_FEATURES);
  const [timePeriods, setTimePeriods] = useState<TimePeriodMap>(DEFAULT_TIME_PERIODS);
  const [target, setTarget] = useState<TrainingTarget>(DEFAULT_TARGET);
  const [params, setParams] = useState<TrainingParams>(DEFAULT_PARAMS);
  const [context, setContext] = useState<TrainingContext>(DEFAULT_CONTEXT);
  const [displayNameMode, setDisplayNameMode] = useState<'auto' | 'manual'>('auto');
  const [displayName, setDisplayName] = useState<string>(buildAutoDisplayName(dayjs(), DEFAULT_TARGET, PRESET_DEFAULT_FEATURES.length));
  const [trainingStatus, setTrainingStatus] = useState<TrainingStatus>('draft');
  const [executionStage, setExecutionStage] = useState('待配置');
  const [backendRunStatus, setBackendRunStatus] = useState<string>('');
  const [progress, setProgress] = useState(0);
  const [logs, setLogs] = useState<string[]>([]);
  const [result, setResult] = useState<TrainingResult | null>(null);
  const [resultError, setResultError] = useState<string>('');
  const [settingDefaultModel, setSettingDefaultModel] = useState(false);
  const [draftSavedAt, setDraftSavedAt] = useState<string>('');
  const [draftHydrated, setDraftHydrated] = useState(false);
  
  const timersRef = useRef<number[]>([]);
  const pollTimerRef = useRef<number | null>(null);
  const logsRef = useRef<string[]>([]);
  const catalogSuggestionAppliedRef = useRef(false);

  const labelFormula = useMemo(() => buildLabelFormula(target), [target]);
  const effectiveTradeDate = useMemo(() => buildEffectiveTradeDate(target, timePeriods.test[0]), [target, timePeriods.test]);
  const featureCount = selectedFeatures.length;
  const autoDisplayName = useMemo(
    () => buildAutoDisplayName(dayjs(), target, featureCount),
    [target, featureCount]
  );
  const trainDays = useMemo(() => daysBetween(timePeriods.train), [timePeriods.train]);
  const valDays = useMemo(() => daysBetween(timePeriods.val), [timePeriods.val]);
  const testDays = useMemo(() => daysBetween(timePeriods.test), [timePeriods.test]);
  const totalDays = trainDays + valDays + testDays;
  const requestPreview = useMemo(
    () => buildTrainingRequest(selectedFeatures, featureCategories, timePeriods, target, params, context, displayName),
    [selectedFeatures, featureCategories, timePeriods, target, params, context, displayName]
  );
  const isReadyToTrain = selectedFeatures.length > 0 && target.horizonDays >= 1 && totalDays > 0;
  const isTrainingInProgress =
    trainingStatus === 'running' ||
    ['pending', 'provisioning', 'running', 'waiting_callback'].includes((backendRunStatus || '').toLowerCase());
  const disableStartTraining = isTrainingInProgress && currentStep === 3;

  useEffect(() => {
    if (displayNameMode !== 'auto') return;
    if (displayName !== autoDisplayName) {
      setDisplayName(autoDisplayName);
    }
  }, [autoDisplayName, displayName, displayNameMode]);

  useEffect(() => {
    let active = true;
    const loadCatalog = async () => {
      setFeatureCatalogLoading(true);
      try {
        const catalog = await modelTrainingService.getFeatureCatalog();
        if (!active) return;
        const dynamicCats = toDynamicCategories(catalog);
        setFeatureCategories(dynamicCats);
        
        if (catalog.data_coverage) {
          setDataCoverage(catalog.data_coverage);
        }
        
        if (catalog.data_coverage?.suggested_periods && !catalogSuggestionAppliedRef.current) {
          const suggested = parseSuggestedTimePeriods(catalog.data_coverage.suggested_periods);
          if (suggested) {
            setTimePeriods(suggested);
            catalogSuggestionAppliedRef.current = true;
          }
        }
      } catch (error) {
        if (active) message.warning('特征字典加载失败，已回退到内置字段');
      } finally {
        if (active) setFeatureCatalogLoading(false);
      }
    };
    loadCatalog();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) { setDraftHydrated(true); return; }
    try {
      const parsed = JSON.parse(saved) as TrainingDraft;
      setSelectedFeatures(parsed.selectedFeatures || []);
      setTimePeriods({
        train: restoreRange(parsed.timePeriods?.train, DEFAULT_TIME_PERIODS.train),
        val: restoreRange(parsed.timePeriods?.val, DEFAULT_TIME_PERIODS.val),
        test: restoreRange(parsed.timePeriods?.test, DEFAULT_TIME_PERIODS.test),
      });
      setTarget(parsed.target || DEFAULT_TARGET);
      setParams({ ...DEFAULT_PARAMS, ...parsed.params });
      setContext({ ...DEFAULT_CONTEXT, ...parsed.context });
      setDisplayNameMode(parsed.displayNameMode || 'auto');
      setDisplayName(parsed.displayName || autoDisplayName);
      if (!draftRestoreNoticeShown) {
        draftRestoreNoticeShown = true;
        message.success('已恢复上次训练草稿');
      }
    } catch (e) { localStorage.removeItem(STORAGE_KEY); }
    setDraftHydrated(true);
  }, []); // Remove autoDisplayName from dependencies to run only once on mount

  useEffect(() => {
    if (!draftHydrated) return;
    const draft: TrainingDraft = {
      displayName, displayNameMode, selectedFeatures,
      timePeriods: {
        train: toISOStringRange(timePeriods.train),
        val: toISOStringRange(timePeriods.val),
        test: toISOStringRange(timePeriods.test),
      },
      target, params, context,
      lastSavedAt: new Date().toISOString(),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
    setDraftSavedAt(draft.lastSavedAt);
  }, [draftHydrated, displayName, displayNameMode, selectedFeatures, timePeriods, target, params, context]);

  const clearTimers = () => {
    timersRef.current.forEach(t => window.clearTimeout(t));
    timersRef.current = [];
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  const pushLog = (line: string) => {
    const next = [...logsRef.current, `[${dayjs().format('HH:mm:ss')}] ${line}`];
    logsRef.current = next;
    setLogs(next);
  };

  const startTraining = async () => {
    if (isTrainingInProgress) {
      message.warning('训练任务进行中，请稍候');
      return;
    }
    if (!isReadyToTrain) { message.warning('配置不完整'); return; }
    clearTimers();
    setResultError('');
    setResult(null);
    setTrainingStatus('running');
    setExecutionStage('准备训练请求');
    setProgress(5);
    pushLog(`正在提交训练请求：${displayName}`);
    
    try {
      const payload = buildBackendTrainingPayload(requestPreview, timePeriods);
      const { runId } = await modelTrainingService.runTraining(payload);
      pushLog(`提交成功，Run ID: ${runId}`);
      
      pollTimerRef.current = window.setInterval(async () => {
        const run = await modelTrainingService.getTrainingRun(runId);
        setBackendRunStatus(run.status || '');
        if (run.logs) {
           run.logs.split('\n').filter(Boolean).forEach(line => {
             if (!logsRef.current.some(l => l.includes(line))) pushLog(line);
           });
        }
        if (run.status === 'running') setProgress(Math.max(run.progress || 20, 20));
        
        if (run.isCompleted) {
          clearTimers();
          if (run.status === 'failed') {
            const errorMsg = (run.result as any)?.error || '训练失败';
            setResultError(errorMsg);
            setTrainingStatus('draft');
          } else {
            const parsed = parseTrainingResult(requestPreview, runId, run.result);
            if (parsed) {
              setResult(parsed);
              setResultError('');
              setTrainingStatus('completed');
              setProgress(100);
              setCurrentStep(4);
              message.success('训练完成');
            } else {
              setResultError('结果解析失败');
              setTrainingStatus('draft');
            }
          }
        }
      }, 3000);
    } catch (err: any) {
      message.error(`提交失败: ${err.message}`);
      setTrainingStatus('draft');
    }
  };

  const stepAction = () => {
    if (currentStep < 3) {
      setCurrentStep(currentStep + 1);
      return;
    }
    if (currentStep === 3) {
      startTraining();
      return;
    }
    // 重新配置逻辑
    setCurrentStep(0);
    setTrainingStatus('draft');
    setResult(null);
    setResultError('');
  };

  const handleResetAll = () => {
    clearTimers();
    setSelectedFeatures(PRESET_DEFAULT_FEATURES);
    setTimePeriods(DEFAULT_TIME_PERIODS);
    setTarget(DEFAULT_TARGET);
    setParams(DEFAULT_PARAMS);
    setContext(DEFAULT_CONTEXT);
    setDisplayNameMode('auto');
    setTrainingStatus('draft');
    setResult(null);
    setCurrentStep(0);
    localStorage.removeItem(STORAGE_KEY);
    message.info('配置已重置');
  };

  const handleSetDefaultModel = async () => {
    const id = result?.modelRegistration?.modelId || result?.modelId;
    if (!id) return;
    try {
      setSettingDefaultModel(true);
      await modelTrainingService.setDefaultModel(id);
      message.success('成功重置默认模型');
    } catch (e: any) { message.error(e.message); }
    finally { setSettingDefaultModel(false); }
  };

  const stepActionLabel = currentStep < 3 ? '下一步' : currentStep === 3 ? '开始训练' : '重新配置';
  const currentModule = TRAINING_MODULES[currentStep] || TRAINING_MODULES[0];
  const CurrentIcon = currentModule.icon;

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-gradient-to-br from-blue-500 to-purple-500 rounded-2xl flex items-center justify-center shadow-lg">
              <Brain className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center gap-2.5 ml-1">
              <h1 className="text-xl font-bold text-slate-800 tracking-tight">QuantMind</h1>
              <div className="h-4 w-[1px] bg-slate-200 self-center" />
              <span className="text-sm font-medium text-slate-500">模型训练中心</span>
            </div>
          </div>
        </header>

        <div className="flex flex-1 overflow-hidden">
          <aside className="bg-white border-r border-gray-200 flex flex-col shadow-sm" style={{ width: `${PAGE_LAYOUT.sidebarWidth}px` }}>
            <div className="flex-1 py-4 overflow-y-auto custom-scrollbar">
              <div className="px-6 mb-2">
                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">训练步骤</p>
              </div>
              <div className="space-y-1">
                {TRAINING_MODULES.map((m, i) => (
                  <button key={m.title} onClick={() => setCurrentStep(i)} className={clsx('relative w-full px-6 text-left py-3 flex items-center gap-3', currentStep === i ? 'bg-blue-50' : 'hover:bg-gray-50')}>
                    {currentStep === i && <div className="absolute left-0 top-0 bottom-0 w-1 bg-blue-500 rounded-r-full" />}
                    <div className={clsx('w-9 h-9 rounded-xl flex items-center justify-center', currentStep === i ? 'bg-blue-100 text-blue-600' : 'bg-gray-100 text-gray-400')}>
                      <m.icon size={16} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium text-gray-900">{m.title}</div>
                      <div className="text-[10px] text-gray-500 truncate">{m.description}</div>
                    </div>
                  </button>
                ))}
              </div>
            </div>
            <div className="p-4 border-t border-gray-100 space-y-3">
               <div className="rounded-xl bg-slate-50 p-3 border border-slate-100">
                  <div className="text-[10px] uppercase font-bold text-slate-400 mb-1">当前配置摘要</div>
                  <div className="text-xs font-semibold text-slate-700">T+{target.horizonDays} · {target.mode === 'classification' ? '分类' : '回归'}</div>
                  <div className="text-[10px] text-slate-400 mt-1 truncate">{labelFormula}</div>
               </div>
               <div className="flex gap-2">
                 <Button size="small" block className="rounded-lg" onClick={() => message.success('草稿已保存')}>保存草稿</Button>
                 <Button size="small" block className="rounded-lg" onClick={handleResetAll} disabled={isTrainingInProgress}>重置</Button>
               </div>
            </div>
          </aside>

          <main className="flex-1 flex flex-col bg-gray-50/50 min-w-0">
            <div className={PAGE_LAYOUT.breadcrumbClass}>
              <div className="flex items-center gap-2 text-sm">
                <span className="text-gray-500">训练中心</span>
                <span className="text-gray-400">/</span>
                <span className="text-gray-800 font-medium">{currentModule.title}</span>
              </div>
            </div>

            <div className={`flex-1 overflow-y-auto overflow-x-hidden p-6 ${TRAINING_PAGE_BOTTOM_SAFE_CLASS}`}>
              <div className="max-w-6xl mx-auto space-y-4">
                <Card className="rounded-2xl border-gray-200 shadow-sm" styles={{ body: { padding: 20 } }}>
                  <div className="flex items-start justify-between">
                    <div>
                        <div className="flex items-center gap-2">
                          <CurrentIcon size={18} className="text-blue-500" />
                          <Title level={4} className="!mb-0">{currentModule.title}</Title>
                        </div>
                        <Paragraph className="!mb-0 !mt-2 text-gray-500 text-xs">{currentModule.description}</Paragraph>
                    </div>
                    <Space>
                      <Button icon={<RefreshCcw size={14}/>} className="rounded-xl h-9" onClick={handleResetAll} disabled={isTrainingInProgress}>清空</Button>
                      <Button type="primary" icon={<ChevronRight size={14}/>} className="rounded-xl h-9 bg-blue-600" onClick={stepAction} disabled={disableStartTraining}>
                        {stepActionLabel}
                      </Button>
                    </Space>
                  </div>
                </Card>

                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                    <MetricCard label="特征数" value={`${featureCount}`} />
                    <MetricCard label="预测周期" value={`T+${target.horizonDays}`} hint={target.mode} />
                    <MetricCard label="数据集天数" value={`${totalDays}`} hint={`${trainDays}/${valDays}/${testDays}`} />
                    <MetricCard label="状态" value={trainingStatus === 'draft' ? '待配置' : trainingStatus === 'running' ? '训练中' : '已完成'} />
                </div>

                <AnimatePresence mode="wait">
                  <motion.div key={currentStep} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.2 }}>
                    {currentStep === 0 && <FeatureSelector categories={featureCategories} selectedFeatures={selectedFeatures} onChange={setSelectedFeatures} loading={featureCatalogLoading} />}
                    {currentStep === 1 && <TrainingTargetConfig target={target} timePeriods={timePeriods} onTargetChange={setTarget} onTimeChange={(k, v) => setTimePeriods({...timePeriods, [k]: v})} dataCoverage={dataCoverage} />}
                    {currentStep === 2 && <ParameterConfig params={params} context={context} onParamsChange={setParams} onContextChange={setContext} displayName={displayName} onDisplayNameChange={(n, m) => { setDisplayName(n); setDisplayNameMode(m); }} autoDisplayName={autoDisplayName} />}
                    {currentStep === 3 && <TrainingConsole trainingStatus={trainingStatus} executionStage={executionStage} progress={progress} logs={logs} backendRunStatus={backendRunStatus} result={result} requestPreview={requestPreview} totalDays={totalDays} trainDays={trainDays} valDays={valDays} testDays={testDays} target={target} />}
                    {currentStep === 4 && <TrainingResultView result={result} resultError={resultError} settingDefaultModel={settingDefaultModel} onSetDefaultModel={handleSetDefaultModel} trainingStatus={trainingStatus} />}
                  </motion.div>
                </AnimatePresence>
              </div>
            </div>
          </main>
        </div>
      </div>
    </div>
  );
};

export default ModelTrainingPage;
