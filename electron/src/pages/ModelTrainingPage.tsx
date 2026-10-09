import React, { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Brain, ChevronRight, Play, Settings2, BarChart, Database,
  Copy, Sparkles, RefreshCcw, Target, Upload, Layers, Square
} from 'lucide-react';
import {
  Button, Space, Tag, Typography, message, Card, Select, Modal, Alert, Tooltip
} from 'antd';
import dayjs, { Dayjs } from 'dayjs';
import { clsx } from 'clsx';
import { PAGE_LAYOUT } from '../config/pageLayout';
import { modelTrainingService } from '../services/modelTrainingService';
import { useAppDispatch, useAppSelector } from '../store';
import { selectCurrentMarket, AppMarket, setMarket } from '../store/slices/uiSlice';
import { getMarketConfig } from '../config/marketConfig';
import { TrainingTarget, TrainingParams, TrainingContext, TrainingStatus, TrainingDraft, SplitKey, TimePeriodMap, FeatureCategory, STORAGE_KEY, DEFAULT_FEATURE_CATEGORIES, getDefaultFeaturesForMarket, resolveDefaultSelectedFeatures, DEFAULT_TIME_PERIODS, DEFAULT_TARGET, DEFAULT_PARAMS, DEFAULT_CONTEXT, buildAutoDisplayName, buildLabelFormula, buildEffectiveTradeDate, daysBetween, toISOStringRange, restoreRange, shouldMigrateLegacyDraftPeriods, buildTrainingRequest, formatRange, toDynamicCategories, TrainingResult, buildBackendTrainingPayload, parseTrainingResult, parseSuggestedTimePeriods, MODEL_DL_DEFAULTS, WfaConfig, ImportedTrainingConfig, buildTrainingConfigFile, parseTrainingConfig, serializeTrainingConfig, TrainingFactorFilterConfig, DEFAULT_FACTOR_FILTER, TrainingMarket, TRAINING_MARKET_OPTIONS, resolveTrainingMarket, buildCrossSourceFeaturePlan, buildExtraFactorSourceOptions, buildFeatureOwnershipMap, findCrossSourceFeatureConflicts, formatCrossSourceConflictMessage, mergeSourceFeatureCategories, sanitizeExtraFactorSources } from './training/trainingUtils';
import { AdminModelFeatureDataCoverage, QuantDBTrainingSource } from '../features/admin/types';
import { adminService } from '../features/admin/services/adminService';
import { FeatureSelector } from './training/FeatureSelector';
import { StockPoolPickerModal } from '../components/backtest/StockPoolPickerModal';
import type { StockPoolOption } from '../services/stockPoolOptionService';
import { TrainingTargetConfig } from './training/TrainingTargetConfig';
import { ParameterConfig } from './training/ParameterConfig';
import { TrainingConsole } from './training/TrainingConsole';
import { TrainingResultView } from './training/TrainingResultView';

const { Title } = Typography;
const NODE_STORAGE_KEY = 'qm.training.selectedNode';
const NODE_READY = new Set(['ready', 'busy']);

const TRAINING_MODULES = [
  { title: '特征选择', description: '筛选输入因子', icon: Database, hint: '第一步' },
  { title: '训练目标', description: '定义 T+N 标签口径', icon: Target, hint: '第二步' },
  { title: '参数配置', description: '设置超参与训练上下文', icon: Settings2, hint: '第三步' },
  { title: '执行训练', description: '编排请求与日志预览', icon: Play, hint: '第四步' },
  { title: '结果入库', description: '查看元数据与产物', icon: BarChart, hint: '第五步' },
];

const TRAINING_PAGE_BOTTOM_SAFE_CLASS = 'pb-[30px]';
// 直读 ML 数据集训练的市场（数据源选择 + 目录版本门禁），与后端
// quantdb_factor_reader.MARKET_FACTOR_SOURCES 保持一致。
const QUANTDB_DIRECT_MARKETS = ['CN', 'HK', 'US', 'FUTURES', 'CRYPTO', 'CUSTOM'];
const isQuantDBMarket = (market: string) => QUANTDB_DIRECT_MARKETS.includes(market);
const DEFAULT_FACTOR_SOURCE = 'l1_factors';
let draftRestoreNoticeShown = false;

/**
 * 同步读草稿里的附加因子库。
 *
 * 必须放在 state 初始化里而不是 mount effect 里：目录加载 effect 在 mount 时就开始跑，
 * 等 effect 再补附加库，首轮加载会按「只有锚库」的可用集合过滤勾选，
 * 把草稿里来自副库的特征静默剔掉（用户看到的是「草稿恢复了，但少了一批特征」）。
 */
const readDraftExtraFactorSources = (): string[] => {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return [];
    const parsed = JSON.parse(saved) as TrainingDraft;
    return sanitizeExtraFactorSources(parsed.extraFactorSources, DEFAULT_FACTOR_SOURCE);
  } catch {
    return [];
  }
};

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

// ==========================================================================
// P0-4: useReducer 草稿恢复（原子化 7 字段一次性写入）
// ==========================================================================

interface FormState {
  selectedFeatures: string[];
  timePeriods: TimePeriodMap;
  wfaConfig: WfaConfig;
  target: TrainingTarget;
  params: TrainingParams;
  context: TrainingContext;
  displayName: string;
  displayNameMode: 'auto' | 'manual';
  draftHydrated: boolean;
  /** 股票池引用（如 pool:csi300），为空表示全市场训练 */
  poolRef: string | null;
  poolName: string | null;
  poolId: string | null;
}

interface ImportPreview {
  config: ImportedTrainingConfig;
  unavailableFeatures: string[];
  marketChanged: boolean;
  catalogVersionChanged: boolean;
  /** 配置里声明、但当前市场不可用（未发布 / 无目录版本）的附加因子库 */
  unavailableExtraSources: string[];
}

type FormAction =
  | { type: 'HYDRATE'; payload: TrainingDraft | null }
  | { type: 'SET_FEATURES'; payload: string[] }
  | { type: 'SET_TIME'; key: SplitKey; value: [Dayjs, Dayjs] }
  | { type: 'SET_TARGET'; payload: TrainingTarget }
  | { type: 'SET_PARAMS'; payload: TrainingParams }
  | { type: 'SET_CONTEXT'; payload: TrainingContext }
  | { type: 'SET_DISPLAY_NAME'; payload: { name: string; mode: 'auto' | 'manual' } }
  | { type: 'SET_WFA'; payload: WfaConfig }
  | { type: 'SET_POOL'; payload: { ref: string | null; name: string | null; id: string | null } }
  | { type: 'SET_FEATURE_CATEGORIES'; payload: FeatureCategory[] }
  | { type: 'SET_MARKET_CONTEXT'; payload: { market: TrainingMarket; benchmark: string } };

function formReducer(state: FormState, action: FormAction): FormState {
  switch (action.type) {
    case 'HYDRATE': {
      if (!action.payload) return { ...state, draftHydrated: true };
      const p = action.payload;
      const restoredParams = { ...DEFAULT_PARAMS, ...p.params };
      // 单选模型：历史草稿若存有多选，只保留主模型
      restoredParams.model_types = [restoredParams.model_type];
      restoredParams.ensemble_method = 'none';
      if (!p.params?.model_types && p.params?.model_type) {
        restoredParams.model_types = [p.params.model_type];
      }
      if (restoredParams.model_type && MODEL_DL_DEFAULTS[restoredParams.model_type]) {
        const defaults = MODEL_DL_DEFAULTS[restoredParams.model_type];
        Object.entries(defaults).forEach(([key, value]) => {
          if (restoredParams[key as keyof TrainingParams] === undefined) {
            (restoredParams as Record<string, unknown>)[key] = value;
          }
        });
      }
      const restoredWfa = p.wfa ?? { enabled: false, strategy: 'rolling', nWindows: 4, trainYears: 3, valMonths: 12, stepMonths: 12 };
      return {
        ...state,
        selectedFeatures: p.selectedFeatures && p.selectedFeatures.length > 0
          ? p.selectedFeatures : state.selectedFeatures,
        timePeriods: {
          train: restoreRange(p.timePeriods?.train, DEFAULT_TIME_PERIODS.train),
          val: restoreRange(p.timePeriods?.val, DEFAULT_TIME_PERIODS.val),
          test: restoreRange(p.timePeriods?.test, DEFAULT_TIME_PERIODS.test),
        },
        target: p.target || DEFAULT_TARGET,
        params: restoredParams,
        context: { ...DEFAULT_CONTEXT, ...p.context },
        displayNameMode: p.displayNameMode || 'auto',
        displayName: p.displayName || state.displayName,
        wfaConfig: restoredWfa,
        poolRef: p.poolRef || null,
        poolName: p.poolName || null,
        poolId: p.poolId || null,
        draftHydrated: true,
      };
    }
    case 'SET_POOL':
      return { ...state, poolRef: action.payload.ref, poolName: action.payload.name, poolId: action.payload.id };
    case 'SET_FEATURES':
      return { ...state, selectedFeatures: action.payload };
    case 'SET_TIME':
      return { ...state, timePeriods: { ...state.timePeriods, [action.key]: action.value } };
    case 'SET_TARGET':
      return { ...state, target: action.payload };
    case 'SET_PARAMS':
      return { ...state, params: action.payload };
    case 'SET_CONTEXT':
      return { ...state, context: action.payload };
    case 'SET_DISPLAY_NAME':
      return { ...state, displayName: action.payload.name, displayNameMode: action.payload.mode };
    case 'SET_WFA':
      return { ...state, wfaConfig: action.payload };
    case 'SET_FEATURE_CATEGORIES':
      return { ...state };
    case 'SET_MARKET_CONTEXT':
      return { ...state, context: { ...state.context, ...action.payload } };
    default:
      return state;
  }
}

// ==========================================================================
// Component
// ==========================================================================

export const ModelTrainingPage: React.FC = () => {
  const navigate = useNavigate();
  const appDispatch = useAppDispatch();
  const currentMarket = useAppSelector(selectCurrentMarket);

  // 自定义数据市场（CUSTOM，如 rd_mined 挖掘因子库）没有行情/交易语义，
  // 不进全局 AppMarket，只在训练页做页面级覆盖：选它不改全局市场，
  // 全局市场在别处被切换时自动撤销覆盖。
  const [customMarketOverride, setCustomMarketOverride] = useState(false);
  const trainingMarket = useMemo(
    () => resolveTrainingMarket(customMarketOverride ? 'CUSTOM' : null, currentMarket),
    [customMarketOverride, currentMarket],
  );

  // ── useReducer：草稿持久化的 7 字段 ──
  const [formState, dispatch] = useReducer(formReducer, {
    selectedFeatures: trainingMarket === 'CN' ? [] : getDefaultFeaturesForMarket(trainingMarket),
    timePeriods: DEFAULT_TIME_PERIODS,
    wfaConfig: { enabled: false, strategy: 'rolling', nWindows: 4, trainYears: 3, valMonths: 12, stepMonths: 12 },
    target: DEFAULT_TARGET,
    params: DEFAULT_PARAMS,
    context: DEFAULT_CONTEXT,
    displayName: buildAutoDisplayName(dayjs(), DEFAULT_TARGET, 0, undefined, trainingMarket, DEFAULT_PARAMS.model_type),
    displayNameMode: 'auto' as const,
    poolRef: null,
    poolName: null,
    poolId: null,
    draftHydrated: false,
  });

  // ── useState: 训练运行时 state（不参与草稿持久化） ──
  const [currentStep, setCurrentStep] = useState(0);
  // A 股 QuantDB 的字段、分类与默认勾选只来自后端已发布目录。
  const [anchorFeatureCategories, setAnchorFeatureCategories] = useState<FeatureCategory[]>([]);
  const [featureCatalogLoading, setFeatureCatalogLoading] = useState(false);
  const [factorSource, setFactorSource] = useState(DEFAULT_FACTOR_SOURCE);
  const [factorSources, setFactorSources] = useState<QuantDBTrainingSource[]>([]);
  const [factorCatalogVersion, setFactorCatalogVersion] = useState<string | null>(null);
  // 附加因子库（跨源训练）：锚库仍由 factorSource 承载，这里只放副库 id。
  // 副库的目录版本不落 state：统一从 factorSources 现读当前已发布版本，避免 pin 到过期版本。
  const [extraFactorSources, setExtraFactorSources] = useState<string[]>(readDraftExtraFactorSources);
  const [extraFeatureCategories, setExtraFeatureCategories] = useState<Record<string, FeatureCategory[]>>({});
  const [extraCatalogErrors, setExtraCatalogErrors] = useState<Record<string, string>>({});
  const [dataCoverage, setDataCoverage] = useState<AdminModelFeatureDataCoverage | null>(null);
  const [trainingStatus, setTrainingStatus] = useState<TrainingStatus>('draft');
  const [executionStage, setExecutionStage] = useState('待配置');
  const [backendRunStatus, setBackendRunStatus] = useState<string>('');
  const [progress, setProgress] = useState(0);
  const [logs, setLogs] = useState<string[]>([]);
  const [result, setResult] = useState<TrainingResult | null>(null);
  const [resultError, setResultError] = useState<string>('');
  const [activeRunId, setActiveRunId] = useState<string>('');
  const [settingDefaultModel, setSettingDefaultModel] = useState(false);
  const [draftSavedAt, setDraftSavedAt] = useState<string>('');
  const [trainingNodes, setTrainingNodes] = useState<any[]>([]);
  const [selectedNode, setSelectedNode] = useState<string>(
    () => localStorage.getItem(NODE_STORAGE_KEY) || 'local'
  );
  const [nodesLoading, setNodesLoading] = useState(false);
  // 训练时长预算（分钟）。前端已移除配置入口，固定透传 720（宽于编排器默认 120，
  // 为 GRU/LSTM 等 DL 模型在 CPU 上的长训练留出余量），如需调整改这里。
  const maxTimeMinutes = 720;
  // 因子筛选开关与阈值（默认开启，后端默认 ic_icir: top-80 / |IC|≥0.01 / |ICIR|≥0.15 / 相关性<0.9）
  const [factorFilter, setFactorFilter] = useState<TrainingFactorFilterConfig>({ ...DEFAULT_FACTOR_FILTER });
  // 股票池（第一步数据范围）：null 表示全市场训练，否则为 pool:<code> 引用
  const [poolPickerOpen, setPoolPickerOpen] = useState(false);

  const timersRef = useRef<number[]>([]);
  const pollTimerRef = useRef<number | null>(null);
  const pollFailuresRef = useRef(0);
  const logsRef = useRef<string[]>([]);
  const serverLogSeenRef = useRef<Set<string>>(new Set());
  const catalogSuggestionAppliedRef = useRef(false);
  // 上一次目录加载对应的「市场|锚库」：用来区分「换锚库」与「只是增删附加库」
  const catalogAnchorKeyRef = useRef('');
  const importInputRef = useRef<HTMLInputElement>(null);
  const importedFeaturesRef = useRef<string[] | null>(null);
  // 草稿恢复的特征勾选：目录异步加载完成前 HYDRATE 已写入表单，
  // 用该 ref 把草稿勾选「跨过」目录加载的默认值重置，避免恢复被覆盖。
  const restoredDraftFeaturesRef = useRef<string[] | null>(null);
  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null);
  const [importingConfig, setImportingConfig] = useState(false);

  // Derive individual fields from formState for inline use
  const { selectedFeatures, timePeriods, wfaConfig, target, params, context, displayName, displayNameMode } = formState;

  // 目录加载 effect 要读「当前勾选」来决定增删附加库后是否保留选择。
  // 用 effect 同步（声明在目录加载 effect 之前，同一 commit 内先执行），避免读到上一轮的值。
  const selectedFeaturesRef = useRef<string[]>(selectedFeatures);
  useEffect(() => {
    selectedFeaturesRef.current = selectedFeatures;
  }, [selectedFeatures]);

  // ── 跨源因子库（锚库 + 附加库）派生值 ──
  // 锚库/附加库目录合并成一份可渲染分类表（附加库分类名带来源后缀、id 带库前缀）
  const sourceLabels = useMemo<Record<string, string>>(
    () => Object.fromEntries(factorSources.map((item) => [item.id, item.name || item.id])),
    [factorSources],
  );
  const featureCategories = useMemo(
    () => mergeSourceFeatureCategories(factorSource, anchorFeatureCategories, extraFeatureCategories, sourceLabels),
    [factorSource, anchorFeatureCategories, extraFeatureCategories, sourceLabels],
  );
  // 副库版本只从当前来源列表现读（catalog_version 就是该库已发布版本 id）
  const extraCatalogVersions = useMemo(() => {
    const versions: Record<string, string> = {};
    extraFactorSources.forEach((lib) => {
      const version = factorSources.find((item) => item.id === lib)?.catalog_version;
      if (version) versions[lib] = version;
    });
    return versions;
  }, [extraFactorSources, factorSources]);
  const featureOwnership = useMemo(
    () => buildFeatureOwnershipMap(factorSource, anchorFeatureCategories, extraFeatureCategories),
    [factorSource, anchorFeatureCategories, extraFeatureCategories],
  );
  const crossSourceConflicts = useMemo(
    () => findCrossSourceFeatureConflicts(factorSource, anchorFeatureCategories, extraFeatureCategories),
    [factorSource, anchorFeatureCategories, extraFeatureCategories],
  );
  // 选中特征 → 后端 features 数组：锚库裸名、附加库 "库:feature_key"，并给出副库版本 pin
  const crossSourcePlan = useMemo(
    () => buildCrossSourceFeaturePlan(selectedFeatures, factorSource, featureOwnership, extraCatalogVersions),
    [selectedFeatures, factorSource, featureOwnership, extraCatalogVersions],
  );
  const extraSourceOptions = useMemo(
    () => buildExtraFactorSourceOptions(factorSources, factorSource),
    [factorSources, factorSource],
  );
  const hasSelectableExtraSource = extraSourceOptions.some((option) => !option.disabled);
  const extraSourcesKey = extraFactorSources.join('|');
  const isExtraFactorSourceUsable = (lib: string) => {
    const source = factorSources.find((item) => item.id === lib);
    return Boolean(source && source.published && source.catalog_version);
  };

  const labelFormula = useMemo(() => buildLabelFormula(target), [target]);
  const effectiveTradeDate = useMemo(() => buildEffectiveTradeDate(target, timePeriods.test[0]), [target, timePeriods.test]);

  // 市场切换（含页面级 CUSTOM 覆盖）
  useEffect(() => {
    const mc = getMarketConfig(trainingMarket);
    dispatch({ type: 'SET_MARKET_CONTEXT', payload: { market: trainingMarket, benchmark: mc.benchmark } });
    dispatch({ type: 'SET_FEATURES', payload: trainingMarket === 'CN' ? [] : getDefaultFeaturesForMarket(trainingMarket) });
    catalogSuggestionAppliedRef.current = false;
  }, [trainingMarket]);

  // 全局市场在别处被切换时撤销页面级覆盖（覆盖只跟随本页下拉/导入）
  useEffect(() => { setCustomMarketOverride(false); }, [currentMarket]);

  // 训练市场下拉：五个真实市场走全局切换（各页面共享），CUSTOM 只置本页覆盖
  const handleTrainingMarketChange = (next: TrainingMarket) => {
    if (next === 'CUSTOM') {
      setCustomMarketOverride(true);
      return;
    }
    setCustomMarketOverride(false);
    if (next !== currentMarket) appDispatch(setMarket(next));
  };

  // 换锚库：把新锚库从附加库里剔除，避免「自己配自己」
  const handleFactorSourceChange = (next: string) => {
    setFactorSource(next);
    setExtraFactorSources((prev) => sanitizeExtraFactorSources(prev, next));
  };

  const featureCount = selectedFeatures.length;
  const autoDisplayName = useMemo(
    () => buildAutoDisplayName(dayjs(), target, featureCount, undefined, trainingMarket, params.model_type),
    [target, featureCount, trainingMarket, params.model_type]
  );
  const trainDays = useMemo(() => daysBetween(timePeriods.train), [timePeriods.train]);
  const valDays = useMemo(() => daysBetween(timePeriods.val), [timePeriods.val]);
  const testDays = useMemo(() => daysBetween(timePeriods.test), [timePeriods.test]);
  const totalDays = trainDays + valDays + testDays;
  const coverageDisplay = dataCoverage?.file_count
    ? `${dataCoverage.file_count} 个交易日`
    : '—';
  const coverageHint = dataCoverage?.min_date && dataCoverage?.max_date
    ? `${dataCoverage.min_date} ～ ${dataCoverage.max_date}`
    : '等待数据源状态';
  const requestPreview = useMemo(
    () => buildTrainingRequest(selectedFeatures, featureCategories, timePeriods, target, params, context, displayName, trainingMarket, wfaConfig, formState.poolRef),
    [selectedFeatures, featureCategories, timePeriods, target, params, context, displayName, trainingMarket, wfaConfig, formState.poolRef]
  );
  // 训练节点
  const selectedNodeObj = useMemo(
    () => trainingNodes.find((n) => n.id === selectedNode) || trainingNodes[0],
    [trainingNodes, selectedNode]
  );

  const isDirectCatalogReady = !isQuantDBMarket(trainingMarket) || (
    !!factorCatalogVersion && dataCoverage?.ready === true
  );
  const isSelectedNodeReady = selectedNodeObj
    ? NODE_READY.has(String(selectedNodeObj.readiness || ''))
    : trainingNodes.length === 0;
  // 跨源提交前必须闭合的两件事：归属未知的选中特征（会被后端当别库同名因子解析）、
  // 以及有特征被选中但没 pin 版本的附加库
  const crossSourceBlocker = crossSourcePlan.unresolved.length > 0
    ? `以下已选特征不在当前因子库目录中：${crossSourcePlan.unresolved.join('、')}。请取消选择或重新启用对应因子库。`
    : crossSourcePlan.unversionedSources.length > 0
      ? `附加因子库缺少已发布目录版本：${crossSourcePlan.unversionedSources
          .map((lib) => sourceLabels[lib] || lib)
          .join('、')}。请先在后台发布该库目录。`
      : '';
  const isCrossSourceReady = crossSourceBlocker === '';
  const isReadyToTrain = selectedFeatures.length > 0 && target.horizonDays >= 1 && totalDays > 0 && isDirectCatalogReady && isSelectedNodeReady && isCrossSourceReady;
  // 只看本页训练态，不用后端残留的 pending 把「开始训练」锁死
  const isTrainingInProgress = trainingStatus === 'running';
  const disableStartTraining = (isTrainingInProgress || !isSelectedNodeReady) && currentStep === 3;
  const startDisabledReason = isTrainingInProgress
    ? '训练任务进行中'
    : !isSelectedNodeReady
      ? `当前节点未就绪（${selectedNodeObj?.readiness_label || '检测中'}）。请在左侧选择已就绪的 AutoDL 节点，或点刷新。`
      : '';

  // 自动 displayName
  useEffect(() => {
    if (displayNameMode !== 'auto') return;
    if (displayName !== autoDisplayName) {
      dispatch({ type: 'SET_DISPLAY_NAME', payload: { name: autoDisplayName, mode: 'auto' } });
    }
  }, [autoDisplayName, displayName, displayNameMode]);

  const loadNodes = async (silent = false) => {
    if (!silent) setNodesLoading(true);
    try {
      const resp = await adminService.listTrainingNodes(true);
      if (resp?.nodes) {
        setTrainingNodes(resp.nodes);
      }
    } catch { /* silent */ } finally {
      if (!silent) setNodesLoading(false);
    }
  };

  useEffect(() => {
    loadNodes();
  }, []);

  useEffect(() => {
    if (selectedNode) localStorage.setItem(NODE_STORAGE_KEY, selectedNode);
  }, [selectedNode]);

  useEffect(() => {
    if (trainingNodes.length === 0) return;
    const current = trainingNodes.find((n) => n.id === selectedNode);
    if (current && NODE_READY.has(String(current.readiness || ''))) return;
    const preferred = trainingNodes.find((n) => n.type === 'remote' && n.readiness === 'ready')
      || trainingNodes.find((n) => n.readiness === 'ready')
      || trainingNodes.find((n) => n.type === 'remote' && NODE_READY.has(String(n.readiness || '')));
    if (preferred && preferred.id !== selectedNode) {
      setSelectedNode(preferred.id);
    }
  }, [trainingNodes, selectedNode]);

  // 直读市场（CN/HK）训练目录完全由后端发布版本驱动；不回退到任何内置字段。
  // 锚库与附加库目录在同一次加载里取齐后一起落地：拆成两个 effect 会互相覆盖勾选，
  // 也会让「归属表 / 冲突检测 / 默认勾选」看到不同步的目录快照。
  useEffect(() => {
    let active = true;
    const loadCatalog = async () => {
      setFeatureCatalogLoading(true);
      setFactorCatalogVersion(null);
      setDataCoverage(null);
      const anchorKey = `${trainingMarket}|${factorSource}`;
      // 换锚库/换市场才重算默认勾选；只是增删附加库时必须保住用户已勾的特征
      const anchorChanged = catalogAnchorKeyRef.current !== anchorKey;
      catalogAnchorKeyRef.current = anchorKey;
      try {
        let effectiveExtras = sanitizeExtraFactorSources(extraFactorSources, factorSource);
        if (isQuantDBMarket(trainingMarket)) {
          const sourceResult = await modelTrainingService.getQuantDBTrainingSources(trainingMarket);
          if (!active) return;
          const availableSources = sourceResult.sources || [];
          setFactorSources(availableSources);
          const selectedSource = availableSources.find((item) => item.id === factorSource);
          if (!selectedSource) {
            const defaultSource = availableSources.find((item) => item.default)?.id
              || sourceResult.default_source;
            if (defaultSource && defaultSource !== factorSource) setFactorSource(defaultSource);
            return;
          }
          // 副库必须是本市场里「已发布且有目录版本」的库：换市场/导错配置后自愈，
          // 也保证下面用的版本映射与目录来自同一份来源列表
          effectiveExtras = effectiveExtras.filter((lib) => {
            const source = availableSources.find((item) => item.id === lib);
            return Boolean(source && source.published && source.catalog_version);
          });
          if (effectiveExtras.join('|') !== extraFactorSources.join('|')) {
            setExtraFactorSources(effectiveExtras);
          }
        }

        const anchorCatalog = await modelTrainingService.getFeatureCatalog(
          trainingMarket,
          false,
          isQuantDBMarket(trainingMarket) ? factorSource : undefined,
        );

        // 附加库目录：并行拉取。单个库失败只影响该库，且必须显式提示，
        // 不能静默吞掉后假装本次训练只有锚库（特征会凭空少一截）。
        const extraLibs = isQuantDBMarket(trainingMarket) ? effectiveExtras : [];
        const extraResults = await Promise.all(extraLibs.map(async (lib) => {
          try {
            const catalog = await modelTrainingService.getFeatureCatalog(trainingMarket, false, lib);
            return { lib, categories: toDynamicCategories(catalog), error: '' };
          } catch (error) {
            const reason = error instanceof Error ? error.message : '目录加载失败';
            return { lib, categories: [] as FeatureCategory[], error: reason };
          }
        }));
        if (!active) return;

        const nextExtraCategories: Record<string, FeatureCategory[]> = {};
        const nextExtraErrors: Record<string, string> = {};
        extraResults.forEach((item) => {
          if (item.error) nextExtraErrors[item.lib] = item.error;
          else nextExtraCategories[item.lib] = item.categories;
        });
        setExtraFeatureCategories(nextExtraCategories);
        setExtraCatalogErrors(nextExtraErrors);
        if (Object.keys(nextExtraErrors).length > 0) {
          const failed = Object.entries(nextExtraErrors)
            .map(([lib, reason]) => `${sourceLabels[lib] || lib}（${reason}）`)
            .join('、');
          message.warning(`附加因子库目录加载失败，本次训练已排除：${failed}`);
        }

        const dynamicCats = toDynamicCategories(anchorCatalog);
        setAnchorFeatureCategories(dynamicCats);
        setDataCoverage(anchorCatalog.data_coverage || null);
        setFactorCatalogVersion(
          anchorCatalog.source === 'quantdb_factor_catalog' && anchorCatalog.catalog_status === 'ready'
            ? anchorCatalog.version_id
            : null,
        );
        const availableKeys = new Set([
          ...dynamicCats.flatMap((category) => category.features.map((feature) => feature.key)),
          ...Object.values(nextExtraCategories)
            .flatMap((categories) => categories.flatMap((category) => category.features.map((feature) => feature.key))),
        ]);
        const importedFeatures = importedFeaturesRef.current;
        const restoredFeatures = restoredDraftFeaturesRef.current;
        if (importedFeatures) {
          dispatch({ type: 'SET_FEATURES', payload: importedFeatures.filter((key) => availableKeys.has(key)) });
          importedFeaturesRef.current = null;
          restoredDraftFeaturesRef.current = null;
        } else if (restoredFeatures) {
          // 目录加载晚于草稿恢复：保留草稿里勾选且在当前目录可用的特征，而非重置为默认勾选
          dispatch({ type: 'SET_FEATURES', payload: restoredFeatures.filter((key) => availableKeys.has(key)) });
          restoredDraftFeaturesRef.current = null;
        } else if (!anchorChanged) {
          // 只是增删附加库：保留当前勾选，仅剔除随库一起消失的特征
          const current = selectedFeaturesRef.current;
          const kept = current.filter((key) => availableKeys.has(key));
          if (kept.length !== current.length) dispatch({ type: 'SET_FEATURES', payload: kept });
        } else {
          dispatch({ type: 'SET_FEATURES', payload: resolveDefaultSelectedFeatures(dynamicCats, trainingMarket) });
        }
        if (anchorCatalog.data_coverage?.suggested_periods && !catalogSuggestionAppliedRef.current) {
          const suggested = parseSuggestedTimePeriods(anchorCatalog.data_coverage.suggested_periods);
          if (suggested) {
            dispatch({ type: 'SET_TIME', key: 'train', value: suggested.train });
            dispatch({ type: 'SET_TIME', key: 'val', value: suggested.val });
            dispatch({ type: 'SET_TIME', key: 'test', value: suggested.test });
            catalogSuggestionAppliedRef.current = true;
          }
        }
      } catch (error) {
        if (active && (trainingMarket === 'CN' || trainingMarket === 'CUSTOM')) {
          // CN 与 CUSTOM 都禁止回退内置字段：字段名/契约只认后端已发布目录，
          // 拿 A 股预设去猜自定义数据（rd_mined 挖掘因子）是在编造特征。
          // 此分支刻意不弹提示（页面会以「目录未就绪」呈现），日志留痕供排查。
          console.warn('[ModelTrainingPage] 训练目录加载失败，特征已清空（禁止回退内置字段）:', trainingMarket, error);
          setAnchorFeatureCategories([]);
          setExtraFeatureCategories({});
          setExtraCatalogErrors({});
          dispatch({ type: 'SET_FEATURES', payload: [] });
        } else if (active) {
          setAnchorFeatureCategories(DEFAULT_FEATURE_CATEGORIES);
          setExtraFeatureCategories({});
          setExtraCatalogErrors({});
          dispatch({ type: 'SET_FEATURES', payload: getDefaultFeaturesForMarket(trainingMarket) });
          message.warning('特征字典加载失败，已回退到内置字段');
        }
      } finally {
        if (active) setFeatureCatalogLoading(false);
      }
    };
    loadCatalog();
    return () => { active = false; };
    // sourceLabels 只用于报错文案，不参与加载语义，故意不入依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trainingMarket, factorSource, extraSourcesKey]);

  // P0-4: 草稿恢复 — 一次 dispatch 原子化写入（替代 7 个 setState）
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) { dispatch({ type: 'HYDRATE', payload: null }); return; }
    try {
      const parsed = JSON.parse(saved) as TrainingDraft;
      dispatch({ type: 'HYDRATE', payload: parsed });
      // 草稿里记的是自定义数据市场：恢复页面级覆盖，让目录按 CUSTOM 重新加载
      if (parsed.context?.market === 'CUSTOM') setCustomMarketOverride(true);
      if (Array.isArray(parsed.selectedFeatures)) {
        restoredDraftFeaturesRef.current = parsed.selectedFeatures;
      }
      // 附加因子库不在这里恢复：它已在 useState 初始化器（readDraftExtraFactorSources）里
      // 同步读出，否则首轮目录加载会按「只有锚库」过滤掉草稿里副库的特征。
      // 是否可用由目录加载时按当前市场的来源列表再过滤一次。
      if (!draftRestoreNoticeShown) {
        draftRestoreNoticeShown = true;
        message.success('已恢复上次训练草稿');
      }
    } catch {
      localStorage.removeItem(STORAGE_KEY);
      dispatch({ type: 'HYDRATE', payload: null });
    }
  }, []); // 只 mount 时执行

  // P0-4: 草稿保存 — draftHydrated 守卫防止覆盖已恢复草稿
  useEffect(() => {
    if (!formState.draftHydrated) return;
    const draft: TrainingDraft = {
      displayName,
      displayNameMode,
      selectedFeatures,
      timePeriods: {
        train: toISOStringRange(timePeriods.train),
        val: toISOStringRange(timePeriods.val),
        test: toISOStringRange(timePeriods.test),
      },
      target,
      params,
      context,
      wfa: wfaConfig,
      poolRef: formState.poolRef,
      poolName: formState.poolName,
      poolId: formState.poolId,
      extraFactorSources,
      lastSavedAt: new Date().toISOString(),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
    setDraftSavedAt(draft.lastSavedAt);
  }, [formState.draftHydrated, formState.poolRef, formState.poolName, formState.poolId, displayName, displayNameMode, selectedFeatures, timePeriods, target, params, context, wfaConfig, extraFactorSources]);

  const clearTimers = () => {
    timersRef.current.forEach(t => window.clearTimeout(t));
    timersRef.current = [];
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  useEffect(() => {
    return () => {
      clearTimers();
    };
  }, []);

  const pushLog = (line: string) => {
    const next = [...logsRef.current, `[${dayjs().format('HH:mm:ss')}] ${line}`];
    logsRef.current = next;
    setLogs(next);
  };

  const ingestServerLogs = (text?: string) => {
    if (!text) return;
    text.split('\n').filter(Boolean).forEach((line) => {
      if (serverLogSeenRef.current.has(line)) return;
      serverLogSeenRef.current.add(line);
      pushLog(line);
    });
  };

  const startTraining = async () => {
    if (isTrainingInProgress) {
      message.warning('训练任务进行中，请稍候');
      return;
    }
    if (!isReadyToTrain) {
      message.warning(
        crossSourceBlocker
          || (isQuantDBMarket(trainingMarket) ? '数据源、映射版本或覆盖范围尚未就绪' : '配置不完整'),
      );
      return;
    }
    clearTimers();
    setResultError('');
    setResult(null);
    setTrainingStatus('running');
    setExecutionStage('准备训练请求');
    setProgress(5);
    logsRef.current = [];
    serverLogSeenRef.current = new Set();
    setLogs([]);
    pushLog(`正在提交训练请求：${displayName}`);

    try {
      const payload = buildBackendTrainingPayload(requestPreview, timePeriods, {
        nodeId: selectedNode,
        maxTimeMinutes,
        factorFilter,
        // 跨源：锚库发裸名、附加库发 "库:feature_key"，并 pin 副库目录版本
        crossSource: isQuantDBMarket(trainingMarket)
          ? { anchorSource: factorSource, ownership: featureOwnership, extraCatalogVersions }
          : undefined,
      });
      if (isQuantDBMarket(trainingMarket) && factorCatalogVersion) {
        payload.factor_source = factorSource;
        payload.factor_catalog_version = factorCatalogVersion;
      }
      const { runId } = await modelTrainingService.runTraining(payload);
      setActiveRunId(runId);
      pushLog(`提交成功，Run ID: ${runId}`);
      startPolling(runId);
    } catch (err: any) {
      message.error(`提交失败: ${err.message}`);
      setTrainingStatus('draft');
    }
  };

  // 统一轮询训练进度：新任务 / 切页恢复共用同一套逻辑。
  // 网络抖动或后端重启时静默重试；连续失败过久才停止并提示，避免进度条无声卡死。
  const startPolling = async (runId: string) => {
    clearTimers();
    pollFailuresRef.current = 0;
    const tick = async () => {
      let run;
      try {
        run = await modelTrainingService.getTrainingRun(runId);
        pollFailuresRef.current = 0;
      } catch {
        pollFailuresRef.current += 1;
        if (pollFailuresRef.current === 5) {
          pushLog('连续多次获取训练状态失败，仍在重试…（后端可能正在重启）');
        }
        if (pollFailuresRef.current >= 20) {
          clearTimers();
          message.error('已连续 1 分钟无法获取训练状态，停止轮询。请检查后端服务后重新进入本页恢复。');
        }
        return;
      }
      setBackendRunStatus(run.status || '');
      ingestServerLogs(run.logs);
      const liveStatuses = ['running', 'provisioning', 'waiting_callback', 'pending'];
      const failedByLog = /\[ERROR\].*(编排失败|训练异常退出|原生进程轮询异常)/.test(run.logs || '');
      if (liveStatuses.includes(run.status || '') && !failedByLog) {
        setProgress(Math.max(run.progress || 5, 5));
      }

      if (run.status === 'cancelled') {
        clearTimers();
        pushLog('训练已被取消');
        setTrainingStatus('draft');
        setExecutionStage('已取消');
        setBackendRunStatus('');
        setActiveRunId('');
        message.info('训练已取消');
        return;
      }

      if (run.isCompleted || failedByLog || run.status === 'failed') {
        clearTimers();
        if (run.status === 'failed' || failedByLog) {
          const errorMsg = (run.result as any)?.error || '训练失败';
          setResultError(errorMsg);
          setTrainingStatus('draft');
          setExecutionStage('待配置');
          setBackendRunStatus('');
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
    };
    void tick();
    pollTimerRef.current = window.setInterval(tick, 3000);
  };

  // 页面挂载时仅恢复「进行中」的训练任务：切页前的 running/provisioning 继续轮询，
  // 进度不丢。已完成/失败不恢复，页面停在第一步（避免进页即跳到第五步结果页）。
  useEffect(() => {
    let active = true;
    (async () => {
      const run = await modelTrainingService.getActiveTrainingRun();
      if (!active || !run) return;
      const inProgress =
        !run.isCompleted &&
        ['running', 'provisioning', 'waiting_callback', 'pending'].includes(run.status || '');
      if (!inProgress) return;
      ingestServerLogs(run.logs);
      setBackendRunStatus(run.status || '');
      setProgress(Math.max(run.progress || 5, 5));
      setTrainingStatus('running');
      setExecutionStage('训练进行中（已从上次会话恢复）');
      setCurrentStep(3);
      setActiveRunId(run.runId);
      startPolling(run.runId);
    })();
    return () => {
      active = false;
    };
    // 仅在挂载时执行一次，避免因依赖项变化反复触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stepAction = () => {
    if (currentStep < 3) {
      setCurrentStep(currentStep + 1);
      return;
    }
    if (currentStep === 3) {
      startTraining();
      return;
    }
    setCurrentStep(0);
    setTrainingStatus('draft');
    setResult(null);
    setResultError('');
  };

  const handleCancelTraining = () => {
    if (!activeRunId) return;
    Modal.confirm({
      title: '取消训练',
      content: `确定要取消训练任务 ${activeRunId} 吗？训练容器/进程将被停止，已产出的模型不会入库。`,
      okText: '取消训练',
      okButtonProps: { danger: true },
      cancelText: '继续训练',
      onOk: async () => {
        try {
          await modelTrainingService.cancelTrainingRun(activeRunId);
          clearTimers();
          pushLog('已提交取消请求，正在停止训练…');
          setTrainingStatus('draft');
          setExecutionStage('已取消');
          setBackendRunStatus('');
          setActiveRunId('');
          message.success('训练已取消');
        } catch (err: any) {
          message.error(`取消失败: ${err.message}`);
        }
      },
    });
  };

  const handleResetAll = () => {
    clearTimers();
    // 默认勾选只认锚库目录：附加库的 default_selected 不参与，避免一开附加库就自动多勾一片
    const features = anchorFeatureCategories.length > 0
      ? resolveDefaultSelectedFeatures(anchorFeatureCategories, trainingMarket)
      : getDefaultFeaturesForMarket(trainingMarket);
    dispatch({ type: 'SET_FEATURES', payload: features });
    setExtraFactorSources([]);
    const coveragePeriods = parseSuggestedTimePeriods(dataCoverage?.suggested_periods);
    dispatch({ type: 'SET_TIME',  key: 'train', value: coveragePeriods?.train || DEFAULT_TIME_PERIODS.train });
    dispatch({ type: 'SET_TIME',  key: 'val',   value: coveragePeriods?.val || DEFAULT_TIME_PERIODS.val });
    dispatch({ type: 'SET_TIME',  key: 'test',  value: coveragePeriods?.test || DEFAULT_TIME_PERIODS.test });
    dispatch({ type: 'SET_TARGET', payload: DEFAULT_TARGET });
    dispatch({ type: 'SET_PARAMS', payload: DEFAULT_PARAMS });
    // 重置保留当前训练市场（含 CUSTOM 覆盖），否则上下文会被打回默认 A 股
    dispatch({ type: 'SET_CONTEXT', payload: { ...DEFAULT_CONTEXT, market: trainingMarket, benchmark: getMarketConfig(trainingMarket).benchmark } });
    dispatch({ type: 'SET_DISPLAY_NAME', payload: { name: '', mode: 'auto' } });
    dispatch({ type: 'SET_WFA', payload: { enabled: false, strategy: 'rolling', nWindows: 4, trainYears: 3, valMonths: 12, stepMonths: 12 } });
    setTrainingStatus('draft');
    setResult(null);
    setCurrentStep(0);
    localStorage.removeItem(STORAGE_KEY);
    message.info('配置已重置');
  };

  const handleImportConfigFile = async (file?: File) => {
    if (!file) return;
    setImportingConfig(true);
    try {
      if (file.size > 1024 * 1024) throw new Error('配置文件不能超过 1 MB');
      const config = parseTrainingConfig(await file.text());
      const availableKeys = new Set(featureCategories.flatMap((category) => category.features.map((feature) => feature.key)));
      const unavailableFeatures = config.market === trainingMarket && availableKeys.size > 0
        ? config.draft.selectedFeatures.filter((key) => !availableKeys.has(key))
        : [];
      setImportPreview({
        config,
        unavailableFeatures,
        marketChanged: config.market !== trainingMarket,
        catalogVersionChanged: config.market === trainingMarket
          && isQuantDBMarket(config.market)
          && Boolean(config.factorCatalogVersion)
          && config.factorCatalogVersion !== factorCatalogVersion,
        // 换市场时来源列表还是旧市场的，判不了，交给目录加载后再过滤
        unavailableExtraSources: config.market === trainingMarket
          ? config.extraFactorSources.filter((lib) => !isExtraFactorSourceUsable(lib))
          : [],
      });
    } catch (error) {
      message.error(error instanceof Error ? `导入失败：${error.message}` : '导入失败：文件格式错误');
    } finally {
      setImportingConfig(false);
      if (importInputRef.current) importInputRef.current.value = '';
    }
  };

  const confirmImportConfig = () => {
    if (!importPreview) return;
    const { config } = importPreview;
    clearTimers();
    importedFeaturesRef.current = config.draft.selectedFeatures;
    // 配置中的时间切分优先级高于数据目录给出的首次打开建议值。
    catalogSuggestionAppliedRef.current = true;
    dispatch({ type: 'HYDRATE', payload: config.draft });
    if (config.market === 'CUSTOM') {
      // CUSTOM 没有全局态：只置页面级覆盖
      setCustomMarketOverride(true);
    } else if (config.market && config.market !== trainingMarket) {
      // 真实市场走全局切换（当前是 CUSTOM 覆盖时，这同时撤销覆盖）；
      // 走到这里 market 已被排除 CUSTOM 与 undefined，即 AppMarket
      setCustomMarketOverride(false);
      appDispatch(setMarket(config.market));
    }
    if (isQuantDBMarket(config.market) && config.factorSource && config.factorSource !== factorSource) {
      setFactorSource(config.factorSource);
    } else {
      const availableKeys = new Set(featureCategories.flatMap((category) => category.features.map((feature) => feature.key)));
      if (availableKeys.size > 0) {
        dispatch({ type: 'SET_FEATURES', payload: config.draft.selectedFeatures.filter((key) => availableKeys.has(key)) });
        importedFeaturesRef.current = null;
      }
    }
    // 附加因子库：换市场时来源列表还没加载，先原样带入，目录加载时按新市场的来源过滤；
    // 同市场就地过滤，避免带着一个已下线的库去提交
    const importedAnchor = isQuantDBMarket(config.market) && config.factorSource
      ? config.factorSource
      : factorSource;
    const importedExtras = sanitizeExtraFactorSources(config.extraFactorSources, importedAnchor);
    setExtraFactorSources(
      config.market === trainingMarket
        ? importedExtras.filter((lib) => isExtraFactorSourceUsable(lib))
        : importedExtras,
    );
    setTrainingStatus('draft');
    setResult(null);
    setResultError('');
    setCurrentStep(0);
    setImportPreview(null);
    message.success('训练配置已导入');
  };

  const handleExportConfig = async () => {
    const draft = {
      displayName,
      displayNameMode,
      selectedFeatures,
      timePeriods: {
        train: toISOStringRange(timePeriods.train),
        val: toISOStringRange(timePeriods.val),
        test: toISOStringRange(timePeriods.test),
      },
      target,
      params,
      context,
      wfa: wfaConfig,
      // 附加库清单随配置走，否则另一台机器导入后静默退化成单库训练
      extraFactorSources: isQuantDBMarket(trainingMarket) ? extraFactorSources : [],
    };
    const content = serializeTrainingConfig(buildTrainingConfigFile(draft, {
      market: trainingMarket,
      factor_source: isQuantDBMarket(trainingMarket) ? factorSource : undefined,
      factor_catalog_version: isQuantDBMarket(trainingMarket) ? factorCatalogVersion : undefined,
      extra_factor_catalog_versions: isQuantDBMarket(trainingMarket) ? extraCatalogVersions : undefined,
    }));
    const safeName = (displayName || 'model-training').replace(/[\\/:*?"<>|]/g, '_');
    const filename = `模型训练配置_${safeName}_${dayjs().format('YYYYMMDD')}.yml`;
    try {
      if (window.electronAPI?.exportSaveFile) {
        const saved = await window.electronAPI.exportSaveFile({ data: content, filename, fileType: 'yml' });
        if (saved.success) message.success('训练配置已导出');
        else if (!saved.canceled) message.error(saved.error || '导出失败');
        return;
      }
      const blob = new Blob([content], { type: 'application/x-yaml;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
      message.success('训练配置已导出');
    } catch (error) {
      message.error(error instanceof Error ? `导出失败：${error.message}` : '导出失败');
    }
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
                  <div className="flex items-center justify-between mb-2">
                    <div className="text-[10px] uppercase font-bold text-slate-400">训练执行配置</div>
                    <button
                      type="button"
                      onClick={() => loadNodes()}
                      disabled={nodesLoading}
                      className="text-[10px] text-slate-400 hover:text-blue-600 flex items-center gap-1 transition-colors disabled:opacity-50"
                      title="刷新节点就绪状态"
                    >
                      <RefreshCcw className={clsx('w-2.5 h-2.5', nodesLoading && 'animate-spin')} />
                      <span>{nodesLoading ? '检测中' : '刷新'}</span>
                    </button>
                  </div>
                  <Select
                    size="small"
                    className="w-full"
                    value={selectedNode}
                    loading={nodesLoading && trainingNodes.length === 0}
                    onChange={setSelectedNode}
                    placeholder="选择训练节点"
                    options={trainingNodes.map((node) => ({
                      value: node.id,
                      label: `${node.type === 'remote' ? '☁️' : '💻'} ${node.name} · ${node.readiness_label || (node.online ? '就绪' : '离线')}`,
                    }))}
                  />
                  {selectedNodeObj && (
                    <div className="mt-2 text-[10px] text-slate-500 truncate">
                      {selectedNodeObj.status_desc || selectedNodeObj.gpu_summary || (selectedNodeObj.type === 'remote' ? '远程 GPU 节点' : '本地 Docker 节点')}
                    </div>
                  )}
                  {selectedNodeObj && ['offline', 'warning'].includes(selectedNodeObj.readiness) && (
                    <details className="mt-2 text-[10px] text-amber-700">
                      <summary className="cursor-pointer select-none hover:text-amber-900">查看节点提示</summary>
                      <div className="mt-1.5 rounded bg-amber-50 p-2 leading-relaxed">
                        {selectedNodeObj.readiness === 'offline'
                          ? selectedNodeObj.error || '请先在云服务商控制台开机或检查连接配置。'
                          : <>本机训练镜像尚未就绪。<code className="mt-1 block select-all rounded bg-amber-100 px-1 py-0.5 font-mono text-[9px]">docker build -f docker/Dockerfile.trainer -t quantmind-trainer:latest .</code></>}
                      </div>
                    </details>
                  )}
               </div>
               <div className="rounded-xl bg-slate-50 p-3 border border-slate-100">
                  <div className="text-[10px] uppercase font-bold text-slate-400 mb-1">当前配置摘要</div>
                  <div className="text-xs font-semibold text-slate-700">T+{target.horizonDays} · {target.mode === 'classification' ? '分类' : '回归'}</div>
                  <div className="text-[10px] text-slate-400 mt-1 truncate">{labelFormula}</div>
               </div>
               <div className="flex gap-2">
                 <Button size="small" block className="rounded-xl font-bold h-8" onClick={() => message.success('草稿已保存')}>保存草稿</Button>
                 <Button size="small" block className="rounded-xl font-bold h-8" onClick={handleResetAll} disabled={isTrainingInProgress}>重置</Button>
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
                <Card className="rounded-2xl border-gray-200 shadow-sm" styles={{ body: { padding: '12px 20px' } }}>
                  <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
                    <div className="flex items-center gap-2 shrink-0">
                      <CurrentIcon size={18} className="text-blue-500" />
                      <Title level={5} className="!mb-0">{currentModule.title}</Title>
                    </div>
                    <div className="flex items-center gap-2 text-xs shrink-0">
                      <span className="font-medium text-slate-600 shrink-0">训练市场</span>
                      <Select
                        value={trainingMarket}
                        onChange={handleTrainingMarketChange}
                        className="min-w-36"
                        disabled={isTrainingInProgress}
                        options={TRAINING_MARKET_OPTIONS}
                      />
                      {trainingMarket === 'CUSTOM' && (
                        <Tooltip title="自定义数据市场：训练直接用后端已发布的自传数据集（如 RD 挖掘因子库），不改全局市场。">
                          <Tag color="purple" className="!m-0">自定义数据</Tag>
                        </Tooltip>
                      )}
                    </div>
                    {isQuantDBMarket(trainingMarket) && (
                      <div className="flex items-center gap-2 text-xs min-w-0">
                      <span className="font-medium text-slate-600 shrink-0">数据源</span>
                      <Select
                        value={factorSource}
                        onChange={handleFactorSourceChange}
                        className="min-w-52"
                        loading={featureCatalogLoading && factorSources.length === 0}
                        options={factorSources.map((item) => ({
                          value: item.id,
                          label: item.default ? `${item.name}（默认）` : item.name,
                          disabled: !item.ready,
                        }))}
                      />
                      {factorCatalogVersion
                        ? <Tag color="blue">目录版本 {factorCatalogVersion}</Tag>
                        : <Tooltip title="前往后台「训练服务 → 模型训练数据集」执行『刷新字段』数据扫描">
                            <Tag
                              color={dataCoverage?.ready ? 'default' : 'warning'}
                              className="cursor-pointer hover:opacity-80"
                              onClick={() => navigate('/admin/training-datasets')}
                            >
                              {factorSources.find((item) => item.id === factorSource)?.reason || '尚未发布因子目录'} →
                            </Tag>
                          </Tooltip>}
                      {factorSources.length > 0 && (
                        <>
                          <span className="font-medium text-slate-600 shrink-0">附加因子库</span>
                          <Select
                            mode="multiple"
                            value={extraFactorSources}
                            onChange={(next: string[]) => setExtraFactorSources(sanitizeExtraFactorSources(next, factorSource))}
                            className="min-w-48"
                            placeholder={hasSelectableExtraSource ? '可跨库组合因子' : '无可用附加因子库'}
                            disabled={isTrainingInProgress || !hasSelectableExtraSource}
                            loading={featureCatalogLoading && factorSources.length === 0}
                            maxTagCount="responsive"
                            options={extraSourceOptions.map((option) => ({
                              value: option.value,
                              label: option.label,
                              disabled: option.disabled,
                              // 置灰原因：鼠标悬停即可看到，不用去猜为什么点不动
                              title: option.reason || undefined,
                            }))}
                          />
                          {!hasSelectableExtraSource && (
                            <Tooltip title="附加因子库需要先在该库发布因子目录（后台『训练服务 → 模型训练数据集』执行『刷新字段』并发布）">
                              <Tag color="warning" className="cursor-pointer hover:opacity-80" onClick={() => navigate('/admin/training-datasets')}>
                                {extraSourceOptions.find((option) => option.disabled)?.reason || '无可用附加因子库'} →
                              </Tag>
                            </Tooltip>
                          )}
                          {extraFactorSources.length > 0 && (
                            <Tag color="geekblue">跨源训练 {extraFactorSources.length + 1} 库</Tag>
                          )}
                        </>
                      )}
                      </div>
                    )}
                    <Space className="ml-auto shrink-0">
                      {currentStep === 0 && (
                        <>
                          <input
                            ref={importInputRef}
                            type="file"
                            accept=".yml,.yaml,.txt,text/yaml,text/plain"
                            className="hidden"
                            onChange={(event) => void handleImportConfigFile(event.target.files?.[0])}
                          />
                          <Button
                            size="small"
                            icon={<Upload size={14} />}
                            className="rounded-xl h-8 font-bold px-3"
                            loading={importingConfig}
                            disabled={isTrainingInProgress}
                            onClick={() => importInputRef.current?.click()}
                          >
                            导入配置
                          </Button>
                        </>
                      )}
                      <Button size="small" icon={<RefreshCcw size={14}/>} className="rounded-xl h-8 font-bold px-3" onClick={handleResetAll} disabled={isTrainingInProgress}>清空</Button>
                      {isTrainingInProgress && (
                        <Button
                          size="small"
                          danger
                          icon={<Square size={14} />}
                          className="rounded-xl h-8 font-bold px-4"
                          onClick={handleCancelTraining}
                        >
                          取消训练
                        </Button>
                      )}
                      <Tooltip title={disableStartTraining ? startDisabledReason : undefined}>
                        <span className={disableStartTraining ? 'inline-block' : undefined}>
                          <Button size="small" type="primary" icon={<ChevronRight size={14}/>} className="rounded-xl h-8 bg-blue-600 font-bold px-4 shadow-sm" onClick={stepAction} disabled={disableStartTraining}>
                            {stepActionLabel}
                          </Button>
                        </span>
                      </Tooltip>
                    </Space>
                  </div>
                </Card>

                <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
                    <MetricCard label="市场" value={getMarketConfig(trainingMarket).label} centered />
                    <MetricCard label="特征数" value={`${featureCount}`} centered />
                    <MetricCard label="预测周期" value={`T+${target.horizonDays}`} hint={target.mode} centered />
                    <MetricCard label="数据覆盖" value={coverageDisplay} hint={coverageHint} centered />
                    <MetricCard label="状态" value={trainingStatus === 'draft' ? '待配置' : trainingStatus === 'running' ? '训练中' : '已完成'} centered />
                </div>

                <AnimatePresence mode="wait">
                  <motion.div key={currentStep} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.2 }}>
                    {currentStep === 0 && (
                      <>
                        <Card className="rounded-3xl border-slate-200 shadow-sm mb-4" styles={{ body: { padding: 20 } }}>
                          <div className="flex items-center gap-2 mb-1">
                            <Layers size={18} className="text-indigo-500" />
                            <Typography.Title level={4} className="!mb-0 !text-slate-900">训练股票池</Typography.Title>
                          </div>
                          <Typography.Paragraph className="!mb-3 !mt-2 !text-xs !text-slate-500 leading-relaxed">
                            限定训练样本的股票范围。默认全市场；选择自定义池后训练只用池内成分（后端解析，空池拒绝提交）。
                          </Typography.Paragraph>
                          <Space wrap>
                            <Button
                              type={formState.poolRef ? 'default' : 'primary'}
                              onClick={() => dispatch({ type: 'SET_POOL', payload: { ref: null, name: null, id: null } })}
                            >
                              全市场
                            </Button>
                            <Button
                              type={formState.poolRef ? 'primary' : 'default'}
                              icon={<Layers size={14} />}
                              onClick={() => setPoolPickerOpen(true)}
                            >
                              {formState.poolRef ? (formState.poolName || formState.poolRef) : '自定义股票池'}
                            </Button>
                            {formState.poolRef && (
                              <Tag color="blue" className="!m-0 font-mono">{formState.poolRef}</Tag>
                            )}
                          </Space>
                        </Card>
                        {extraFactorSources.length > 0 && Object.keys(extraCatalogErrors).length > 0 && (
                          <Alert
                            type="warning"
                            showIcon
                            className="mb-4"
                            message={`${Object.keys(extraCatalogErrors).length} 个附加因子库目录加载失败，本次训练已排除`}
                            description={Object.entries(extraCatalogErrors)
                              .map(([lib, reason]) => `${sourceLabels[lib] || lib}：${reason}`)
                              .join('；')}
                          />
                        )}
                        {extraFactorSources.length > 0 && crossSourceConflicts.length > 0 && (
                          <Alert
                            type="info"
                            showIcon
                            className="mb-4"
                            message={`${crossSourceConflicts.length} 个特征名在多个因子库中重名，非归属库的那一份已置灰`}
                            description={(
                              <div className="space-y-1">
                                {crossSourceConflicts.map((conflict) => (
                                  <div key={conflict.featureKey}>{formatCrossSourceConflictMessage(conflict, sourceLabels)}</div>
                                ))}
                              </div>
                            )}
                          />
                        )}
                        <FeatureSelector categories={featureCategories} selectedFeatures={selectedFeatures} onChange={(f) => dispatch({ type: 'SET_FEATURES', payload: f })} loading={featureCatalogLoading} onGuide={() => navigate('/admin/training-datasets')} anchorSource={factorSource} />
                      </>
                    )}
                    {currentStep === 1 && <TrainingTargetConfig target={target} timePeriods={timePeriods} onTargetChange={(t) => dispatch({ type: 'SET_TARGET', payload: t })} onTimeChange={(k, v) => dispatch({ type: 'SET_TIME', key: k, value: v })} dataCoverage={dataCoverage} factorFilter={factorFilter} onFactorFilterChange={setFactorFilter} />}
                    {currentStep === 2 && <ParameterConfig params={params} context={context} onParamsChange={(p) => dispatch({ type: 'SET_PARAMS', payload: p })} onContextChange={(c) => dispatch({ type: 'SET_CONTEXT', payload: c })} displayName={displayName} onDisplayNameChange={(n, m) => dispatch({ type: 'SET_DISPLAY_NAME', payload: { name: n, mode: m } })} autoDisplayName={autoDisplayName} market={trainingMarket} target={target} onTargetChange={(t) => dispatch({ type: 'SET_TARGET', payload: t })} wfa={wfaConfig} onWfaChange={(w) => dispatch({ type: 'SET_WFA', payload: w })} />}
                    {currentStep === 3 && <TrainingConsole trainingStatus={trainingStatus} executionStage={executionStage} progress={progress} logs={logs} backendRunStatus={backendRunStatus} result={result} requestPreview={requestPreview} totalDays={totalDays} trainDays={trainDays} valDays={valDays} testDays={testDays} target={target} factorFilter={factorFilter} onGoToResult={() => setCurrentStep(4)} />}
                    {currentStep === 4 && <TrainingResultView result={result} resultError={resultError} settingDefaultModel={settingDefaultModel} onSetDefaultModel={handleSetDefaultModel} onExportConfig={handleExportConfig} trainingStatus={trainingStatus} />}
                  </motion.div>
                </AnimatePresence>
              </div>
            </div>
          </main>
        </div>
      </div>
      <Modal
        title="导入模型训练配置"
        open={Boolean(importPreview)}
        okText="确认覆盖当前配置"
        cancelText="取消"
        onOk={confirmImportConfig}
        onCancel={() => setImportPreview(null)}
        okButtonProps={{ danger: true }}
      >
        {importPreview && (
          <div className="space-y-3">
            <Alert
              type="info"
              showIcon
              message={`将导入 ${importPreview.config.draft.selectedFeatures.length} 个特征、${importPreview.config.draft.params.model_types.length} 个模型`}
              description="确认后会覆盖当前特征、时间切分、目标、超参数和训练上下文；训练结果与运行状态不会被导入。"
            />
            {importPreview.marketChanged && (
              <Alert
                type="warning"
                showIcon
                message={`市场将从 ${getMarketConfig(trainingMarket).label} 切换为 ${getMarketConfig(importPreview.config.market ?? 'CN').label}`}
                description="系统会按新市场重新加载可用因子目录。"
              />
            )}
            {importPreview.unavailableFeatures.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`${importPreview.unavailableFeatures.length} 个特征在当前目录中不可用`}
                description={importPreview.unavailableFeatures.join('、')}
              />
            )}
            {importPreview.catalogVersionChanged && (
              <Alert
                type="warning"
                showIcon
                message="因子目录版本与当前环境不同"
                description="导入后会使用当前已发布目录；不可用特征会被自动排除。"
              />
            )}
            {importPreview.config.extraFactorSources.length > 0 && (
              <Alert
                type="info"
                showIcon
                message={`配置包含 ${importPreview.config.extraFactorSources.length} 个附加因子库（跨源训练）`}
                description={importPreview.config.extraFactorSources
                  .map((lib) => (importPreview.unavailableExtraSources.includes(lib)
                    ? `${lib}（当前环境不可用，导入后会被排除）`
                    : lib))
                  .join('、')}
              />
            )}
          </div>
        )}
      </Modal>
      <StockPoolPickerModal
        open={poolPickerOpen}
        onClose={() => setPoolPickerOpen(false)}
        selectedPoolId={formState.poolId}
        market={trainingMarket === 'CN' || trainingMarket === 'CUSTOM' ? 'CN' : undefined}
        title="训练股票池"
        onSelect={(pool: StockPoolOption) => {
          dispatch({ type: 'SET_POOL', payload: { ref: `pool:${pool.code}`, name: pool.name, id: pool.pool_id } });
          setPoolPickerOpen(false);
        }}
      />
    </div>
  );
};

export default ModelTrainingPage;
