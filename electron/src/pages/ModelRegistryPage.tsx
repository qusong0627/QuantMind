import React, { useState, useEffect, useCallback, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Layers, Star, RefreshCw, Search, Code, Calendar, Layers2,
  Archive, Brain, Clock, XCircle, X,
  Play, Download,
  ChevronUp, Shield, Zap, ListFilter, Activity,
  Compass, Sparkles, Pencil, ArrowUpDown, RotateCcw,
  Folder, FolderPlus, FolderInput, FolderMinus, Check,
  GitCompare,
} from 'lucide-react';
import {
  Button, Card, Tag, Typography, Empty, Spin, message,
  Progress, Divider, Row, Col, Input, Modal, Tabs, Switch,
  DatePicker, Table, Badge, Tooltip, Select, Dropdown,
} from 'antd';
import type { MenuProps } from 'antd';
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
} from './modelRegistryPanels';
import { AttributionAnalysisPanel } from './modelRegistryAttributionPanel';
import { FusionModal } from './FusionModal';
import {
  MODEL_SORT_OPTIONS,
  ModelListPrefs,
  ModelSortKey,
  loadModelListPrefs,
  prefsFromServer,
  prefsToServer,
  saveModelListPrefs,
  sortModelRecords,
  toggleId,
} from './modelRegistryListPrefs';
import {
  UNFILED_FOLDER_KEY,
  addFolder,
  assignModelsToFolder,
  countModelsByFolder,
  deleteFolder,
  filterModelsByFolder,
  folderOfModel,
  generateFolderId,
  renameFolder,
  validateFolderName,
} from './modelRegistryFolders';
import { ModelFolderModal, ModelFoldersPanel } from './modelFoldersPanel';
import { ModelCompareModal } from './ModelCompareModal';
import { PublishModelModal } from './hub/PublishModelModal';
import { DriftTabPanel } from './DriftTabPanel';
import { MarketRegimePanel } from './MarketRegimePanel';
import { InferenceCoveragePanel } from './InferenceCoveragePanel';
import { ModelRollingPanel } from './ModelRollingPanel';
import { NeutralizationModelPanel } from '../features/inference-center-shared/panels/NeutralizationPanel';
import {
  buildFeatureLabelMap,
  DEFAULT_FEATURE_CATEGORIES,
  RETRAIN_SEED_STORAGE_KEY,
  serializeTrainingConfig,
  toDynamicCategories,
} from './training/trainingUtils';
import { buildRetrainTrainingConfig } from './training/retrainConfig';
import { PAGE_LAYOUT } from '../config/pageLayout';
import { useAppSelector } from '../store';
import { selectCurrentMarket } from '../store/slices/uiSlice';
import { getMarketConfig } from '../config/marketConfig';
import { MarketSelector } from '../components/layout/MarketSelector';
import { confirmAdmissionOverride, getAdmissionGate } from '../utils/modelAdmission';
const { Text } = Typography;

type ModelListTab = 'active' | 'favorites' | 'archived';

/** 左栏宽度（可拖拽调整）：默认/最小/上限，按窗口宽度收敛并持久化到 localStorage。 */
const SIDEBAR_WIDTH_DEFAULT = 380;
const SIDEBAR_WIDTH_MIN = 300;
const SIDEBAR_WIDTH_MAX = 720;
const SIDEBAR_WIDTH_STORAGE_KEY = 'qm.modelRegistry.sidebarWidth';

const maxSidebarWidth = (): number => {
  if (typeof window === 'undefined') return SIDEBAR_WIDTH_MAX;
  return Math.max(
    SIDEBAR_WIDTH_MIN,
    Math.min(SIDEBAR_WIDTH_MAX, Math.floor(window.innerWidth * 0.55)),
  );
};

const clampSidebarWidth = (width: number): number =>
  Math.round(Math.min(maxSidebarWidth(), Math.max(SIDEBAR_WIDTH_MIN, width)));

const readStoredSidebarWidth = (): number => {
  if (typeof window === 'undefined') return SIDEBAR_WIDTH_DEFAULT;
  try {
    const raw = Number(window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY));
    if (Number.isFinite(raw) && raw > 0) return clampSidebarWidth(raw);
  } catch {
    /* 隐私模式/配额异常：用默认值 */
  }
  return clampSidebarWidth(SIDEBAR_WIDTH_DEFAULT);
};

/** 服务端是否已有可用的偏好数据（升级前只有本地缓存时才需要抬一次）。 */
const hasPrefsData = (prefs: ModelListPrefs): boolean =>
  prefs.pinned.length > 0 ||
  prefs.favorites.length > 0 ||
  prefs.sortKey !== 'default' ||
  prefs.folders.length > 0 ||
  Object.keys(prefs.modelFolders).length > 0;

export const ModelRegistryPage: React.FC = () => {
  const navigate = useNavigate();
  const currentMarket = useAppSelector(selectCurrentMarket);
  const marketConfig = getMarketConfig(currentMarket);
  const [loading, setLoading] = useState(true);
  const [userModels, setUserModels] = useState<UserModelRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedIdRef = React.useRef<string | null>(null);
  React.useEffect(() => { selectedIdRef.current = selectedId; }, [selectedId]);
  const [showPublishModal, setShowPublishModal] = useState(false);
  const [listTab, setListTab] = useState<ModelListTab>('active');
  const [listPrefs, setListPrefs] = useState<ModelListPrefs>(() => loadModelListPrefs(currentMarket));
  // 置顶/收藏/排序/文件夹：本地先写缓存，400ms 防抖同步到服务端（重启/换浏览器不丢）
  const prefsSaveTimerRef = useRef<number | null>(null);
  const listPrefsRef = useRef(listPrefs);
  // 加载期间用户已改过偏好：远端结果晚到也不能覆盖本地刚做的操作
  const prefsTouchedRef = useRef(false);
  // 文件夹：null=全部，UNFILED_FOLDER_KEY=未分组，其余为文件夹 ID
  const [activeFolder, setActiveFolder] = useState<string | null>(null);
  // 左栏宽度：拖拽分隔条或方向键调整，双击分隔条恢复默认
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => readStoredSidebarWidth());
  const [sidebarDragging, setSidebarDragging] = useState(false);
  const [sidebarAnnounce, setSidebarAnnounce] = useState('');
  const sidebarDragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const [folderPanelOpen, setFolderPanelOpen] = useState(true);
  const [folderModalMode, setFolderModalMode] = useState<'create' | 'rename' | null>(null);
  const [folderModalTargetId, setFolderModalTargetId] = useState<string | null>(null);
  const [folderModalMoveIds, setFolderModalMoveIds] = useState<string[] | null>(null);
  const [folderNameValue, setFolderNameValue] = useState('');
  // 整理模式：多选卡片批量移动到文件夹（与融合模式互斥）
  const [organizeMode, setOrganizeMode] = useState(false);
  // 整理模式多选后横向对比（最近3个月 + 测试集 IC）
  const [showCompareModal, setShowCompareModal] = useState(false);
  const [organizeSelected, setOrganizeSelected] = useState<string[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [settingDefault, setSettingDefault] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [activating, setActivating] = useState(false);
  const [retraining, setRetraining] = useState(false);
  const [showRenameModal, setShowRenameModal] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [fusionMode, setFusionMode] = useState(false);
  const [fusionSelected, setFusionSelected] = useState<string[]>([]);
  const [showFusionModal, setShowFusionModal] = useState(false);
  const [mainTab, setMainTab] = useState('detail');
  const [showConfigModal, setShowConfigModal] = useState(false);
  const [activeConfigTab, setActiveConfigTab] = useState<'meta' | 'metrics'>('meta');
  const [trainingRun, setTrainingRun] = useState<ModelTrainingRunStatus | null>(null);
  const [trainingRunLoading, setTrainingRunLoading] = useState(false);
  const [shapSummary, setShapSummary] = useState<ModelShapSummaryResponse | null>(null);
  const [shapLoading, setShapLoading] = useState(false);
  const [shapError, setShapError] = useState('');
  const [featureLabelMap, setFeatureLabelMap] = useState<Record<string, string>>(() => buildFeatureLabelMap(DEFAULT_FEATURE_CATEGORIES));
  const [featureCatalogLoaded, setFeatureCatalogLoaded] = useState(false);
  const [inferenceDate, setInferenceDate] = useState<dayjs.Dayjs | null>(dayjs());
  const [inferenceRunning, setInferenceRunning] = useState(false);
  const [inferenceMode, setInferenceMode] = useState<'single' | 'batch' | 'batch-range' | 'history'>('single');
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
  const [historyRunIdFilter, setHistoryRunIdFilter] = useState('');
  const [historyStatusFilter, setHistoryStatusFilter] = useState<'all' | 'running' | 'completed' | 'failed'>('all');
  const [historyDateFilter, setHistoryDateFilter] = useState<dayjs.Dayjs | null>(null);

  const allModels = userModels;
  const activeModels = allModels.filter(m => m.status !== 'archived');
  const archivedModels = allModels.filter(m => m.status === 'archived');
  const favoriteModels = allModels.filter(m => listPrefs.favorites.includes(m.model_id));
  const tabModels =
    listTab === 'favorites' ? favoriteModels
      : listTab === 'archived' ? archivedModels
        : activeModels;
  const folderCounts = countModelsByFolder(tabModels, listPrefs);
  const folderFilteredModels = filterModelsByFolder(tabModels, activeFolder, listPrefs);
  const activeFolderName = activeFolder === null
    ? null
    : activeFolder === UNFILED_FOLDER_KEY
      ? '未分组'
      : listPrefs.folders.find((folder) => folder.id === activeFolder)?.name ?? null;
  const displayModels = sortModelRecords(
    folderFilteredModels.filter(m =>
      !searchQuery ||
      m.model_id.toLowerCase().includes(searchQuery.toLowerCase()) ||
      modelDisplayName(m).toLowerCase().includes(searchQuery.toLowerCase())
    ),
    { pinned: listPrefs.pinned, sortKey: listPrefs.sortKey },
  );
  const activeSortLabel =
    MODEL_SORT_OPTIONS.find(o => o.key === listPrefs.sortKey)?.shortLabel ?? '排序';

  const selectedModel = userModels.find(m => m.model_id === selectedId) ?? null;
  const meta = selectedModel ? getMeta(selectedModel) : {} as ReturnType<typeof getMeta>;
  const metrics = selectedModel ? getMetrics(selectedModel) : {} as ReturnType<typeof getMetrics>;
  const timePeriods = selectedModel ? extractTimePeriods(getMeta(selectedModel)) : null;
  const horizonDays = Number(meta?.target_horizon_days ?? meta?.horizon_days ?? 3);

  // ─── 融合模式（成员门槛与后端 resolve_fusion_members 对齐：本租户 ready/active，系统模型不可选） ───
  const isFusionEligible = (m: UserModelRecord) =>
    !isSystemModel(m) && ['ready', 'active'].includes(String(m.status));
  const fusionSelectedModels = userModels.filter((m) => fusionSelected.includes(m.model_id));
  const fusionMarkets = new Set(
    fusionSelectedModels.map((m) => String(getMeta(m).market || 'CN').toUpperCase()),
  );
  const canFuse = fusionSelectedModels.length >= 2 && fusionMarkets.size === 1;

  const toggleFusionMode = () => {
    if (fusionMode) {
      setFusionMode(false);
      setFusionSelected([]);
      return;
    }
    // 融合与整理都是多选语义，同屏只保留一个
    setOrganizeMode(false);
    setOrganizeSelected([]);
    setFusionMode(true);
  };

  const toggleOrganizeMode = () => {
    if (organizeMode) {
      setOrganizeMode(false);
      setOrganizeSelected([]);
      return;
    }
    setFusionMode(false);
    setFusionSelected([]);
    setOrganizeMode(true);
  };

  const toggleOrganizeSelect = (modelId: string) => {
    setOrganizeSelected((prev) =>
      prev.includes(modelId) ? prev.filter((id) => id !== modelId) : [...prev, modelId],
    );
  };

  /** 文件夹行点击：选中即过滤下方模型列表；再点同一文件夹取消过滤回到全部。 */
  const handleSelectFolder = (filter: string | null) => {
    if (filter !== null && filter !== UNFILED_FOLDER_KEY && activeFolder === filter) {
      setActiveFolder(null);
      return;
    }
    setActiveFolder(filter);
  };

  // ─── 左栏宽度拖拽/键盘调整 ────────────────────────────────────────
  const handleSidebarResizeStart = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    sidebarDragRef.current = { startX: event.clientX, startWidth: sidebarWidth };
    setSidebarDragging(true);
  };

  const handleSidebarResizeMove = (event: React.PointerEvent<HTMLButtonElement>) => {
    const drag = sidebarDragRef.current;
    if (!drag || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
    setSidebarWidth(
      clampSidebarWidth(drag.startWidth + (event.clientX - drag.startX)),
    );
  };

  const handleSidebarResizeEnd = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (!sidebarDragRef.current) return;
    sidebarDragRef.current = null;
    setSidebarDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setSidebarAnnounce(`模型列表宽度 ${sidebarWidth} 像素`);
  };

  const handleSidebarResizeKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    let next: number | null = null;
    if (event.key === 'ArrowLeft') next = clampSidebarWidth(sidebarWidth - 16);
    else if (event.key === 'ArrowRight') next = clampSidebarWidth(sidebarWidth + 16);
    else if (event.key === 'Home') next = clampSidebarWidth(SIDEBAR_WIDTH_DEFAULT);
    if (next === null) return;
    event.preventDefault();
    setSidebarWidth(next);
    setSidebarAnnounce(`模型列表宽度 ${next} 像素`);
  };

  const handleSidebarResetWidth = () => {
    const next = clampSidebarWidth(SIDEBAR_WIDTH_DEFAULT);
    setSidebarWidth(next);
    setSidebarAnnounce(`模型列表宽度已恢复默认，${next} 像素`);
  };

  const toggleFusionSelect = (m: UserModelRecord) => {
    if (!isFusionEligible(m)) {
      message.warning('该模型不可作为融合成员（需为本人 ready/active 用户模型，系统模型与未就绪模型除外）');
      return;
    }
    setFusionSelected((prev) =>
      prev.includes(m.model_id) ? prev.filter((id) => id !== m.model_id) : [...prev, m.model_id],
    );
  };

  const loadModels = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [resp, sysModels] = await Promise.all([
        modelTrainingService.listUserModels(true, currentMarket),
        modelTrainingService.listSystemModels(currentMarket),
      ]);
      const allItems = resp.items ?? [];
      // 后端已按 market 过滤，老模型（无 market）后端兼容为 CN，前端仅做兜底
      const items = allItems.filter((m) => {
        const meta = m.metadata_json || {};
        const modelMarket = ((meta.market as string) || '').toUpperCase();
        if (!modelMarket) return currentMarket === 'CN';
        return modelMarket === currentMarket;
      });
      // 系统模型转为 UserModelRecord 格式并合并到列表顶部，已物化的系统模型以用户表为准去重
      const existingIds = new Set(items.map((m) => m.model_id));
      const sysItems: UserModelRecord[] = (sysModels ?? [])
        .filter((sm) => {
          if (existingIds.has(sm.model_id)) return false;
          const meta = sm as unknown as Record<string, unknown>;
          const mkt = ((meta.market as string) || '').toUpperCase();
          if (!mkt) return currentMarket === 'CN';
          return mkt === currentMarket;
        })
        .map((sm) => ({
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
            feature_columns: sm.feature_columns,
            market: (sm as unknown as Record<string, unknown>).market,
          },
          metrics_json: sm.performance_metrics ?? {},
          is_default: false,
          created_at: sm.created_at,
        }));
      const merged = [...sysItems, ...items];
      setUserModels(merged);

      const prev = selectedIdRef.current;
      if (prev && merged.some((m) => m.model_id === prev)) {
        // 保持当前选中
      } else {
        const def = merged.find((m) => m.is_default) ?? merged[0];
        if (def) setSelectedId(def.model_id);
      }
    } catch (err: any) {
      message.error(`加载模型列表失败: ${err?.message ?? '未知错误'}`);
    } finally {
      setLoading(false);
    }
  }, [currentMarket]);

  useEffect(() => { loadModels(); }, [loadModels]);

  // 偏好按市场隔离：先用本地缓存秒开，再以服务端为准（跨重启/换浏览器持久化）。
  useEffect(() => {
    const market = currentMarket;
    const local = loadModelListPrefs(market);
    prefsTouchedRef.current = false;
    listPrefsRef.current = local;
    setListPrefs(local);
    let alive = true;
    modelTrainingService
      .getModelListPrefs(market)
      .then((remote) => {
        if (!alive || prefsTouchedRef.current) return;
        const remotePrefs = prefsFromServer(remote);
        const remoteHasData =
          remotePrefs.pinned.length > 0 ||
          remotePrefs.favorites.length > 0 ||
          remotePrefs.sortKey !== 'default' ||
          remotePrefs.folders.length > 0 ||
          Object.keys(remotePrefs.modelFolders).length > 0;
        if (remoteHasData) {
          listPrefsRef.current = remotePrefs;
          setListPrefs(remotePrefs);
          saveModelListPrefs(market, remotePrefs);
          return;
        }
        // 服务端尚未保存过：把升级前的本地偏好抬到服务端（只抬一次）
        if (hasPrefsData(local)) {
          void modelTrainingService
            .saveModelListPrefs({ market, ...prefsToServer(local) })
            .catch(() => undefined);
        }
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [currentMarket]);

  useEffect(() => {
    listPrefsRef.current = listPrefs;
  }, [listPrefs]);

  // 左栏宽度：持久化，并在窗口变窄时重新收敛到可用范围。
  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(sidebarWidth));
    } catch {
      /* 配额满 / 隐私模式：忽略 */
    }
  }, [sidebarWidth]);

  useEffect(() => {
    const onResize = () => setSidebarWidth((width) => clampSidebarWidth(width));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // 切市场 / 删除当前文件夹后，筛选项必须回到「全部」，避免落到不存在的文件夹上。
  useEffect(() => {
    setActiveFolder(null);
    setOrganizeMode(false);
    setOrganizeSelected([]);
  }, [currentMarket]);

  // 切换页签 / 文件夹相当于换了一批可见模型：整理模式下的勾选随之清空
  useEffect(() => {
    setOrganizeSelected([]);
  }, [listTab, activeFolder]);

  useEffect(() => {
    if (activeFolder === null || activeFolder === UNFILED_FOLDER_KEY) return;
    if (!listPrefs.folders.some((folder) => folder.id === activeFolder)) {
      setActiveFolder(null);
    }
  }, [activeFolder, listPrefs.folders]);

  useEffect(
    () => () => {
      if (prefsSaveTimerRef.current) window.clearTimeout(prefsSaveTimerRef.current);
    },
    [],
  );

  /**
   * 偏好统一写入口：用 ref 读最新值，避免同一事件里连续两次更新互相覆盖
   * （如「新建文件夹 + 把选中模型移进去」）。本地立即落缓存，服务端 400ms 防抖。
   */
  const commitListPrefs = useCallback(
    (updater: (prev: ModelListPrefs) => ModelListPrefs) => {
      const next = updater(listPrefsRef.current);
      if (next === listPrefsRef.current) return;
      prefsTouchedRef.current = true;
      listPrefsRef.current = next;
      setListPrefs(next);
      saveModelListPrefs(currentMarket, next);
      if (prefsSaveTimerRef.current) window.clearTimeout(prefsSaveTimerRef.current);
      prefsSaveTimerRef.current = window.setTimeout(() => {
        void modelTrainingService
          .saveModelListPrefs({ market: currentMarket, ...prefsToServer(next) })
          .catch(() => undefined);
      }, 400);
    },
    [currentMarket],
  );

  const updateListPrefs = (patch: Partial<ModelListPrefs>) => {
    commitListPrefs((prev) => ({ ...prev, ...patch }));
  };

  const openCreateFolder = (moveModelIds?: string[]) => {
    setFolderModalMode('create');
    setFolderModalTargetId(null);
    setFolderModalMoveIds(moveModelIds && moveModelIds.length > 0 ? moveModelIds : null);
    setFolderNameValue('');
  };

  const openRenameFolder = (folderId: string) => {
    const folder = listPrefs.folders.find((item) => item.id === folderId);
    if (!folder) return;
    setFolderModalMode('rename');
    setFolderModalTargetId(folderId);
    setFolderModalMoveIds(null);
    setFolderNameValue(folder.name);
  };

  const closeFolderModal = () => {
    setFolderModalMode(null);
    setFolderModalTargetId(null);
    setFolderModalMoveIds(null);
    setFolderNameValue('');
  };

  const submitFolderModal = () => {
    if (!folderModalMode) return;
    const error = validateFolderName(
      folderNameValue,
      listPrefs.folders,
      folderModalMode === 'rename' ? folderModalTargetId ?? undefined : undefined,
    );
    if (error) {
      message.warning(error);
      return;
    }
    const name = folderNameValue.trim();
    if (folderModalMode === 'create') {
      const folderId = generateFolderId(listPrefs.folders);
      const moveIds = folderModalMoveIds ?? [];
      commitListPrefs((prev) => {
        const withFolder = addFolder(prev, { id: folderId, name });
        return moveIds.length > 0
          ? assignModelsToFolder(withFolder, moveIds, folderId)
          : withFolder;
      });
      // 从「+」新建（无移动目标）时直接点开新文件夹；从卡片/整理模式触发则留在原视图继续整理
      if (moveIds.length === 0) {
        setActiveFolder(folderId);
      } else {
        setOrganizeSelected([]);
      }
      setFolderPanelOpen(true);
      message.success(
        moveIds.length > 0
          ? `已新建文件夹「${name}」并移入 ${moveIds.length} 个模型`
          : `已新建文件夹「${name}」`,
      );
    } else if (folderModalTargetId) {
      const folderId = folderModalTargetId;
      commitListPrefs((prev) => renameFolder(prev, folderId, name));
      message.success(`文件夹已重命名为「${name}」`);
    }
    closeFolderModal();
  };

  const handleDeleteFolder = (folderId: string) => {
    const folder = listPrefs.folders.find((item) => item.id === folderId);
    if (!folder) return;
    const count = countModelsByFolder(allModels, listPrefs).byFolder[folderId] ?? 0;
    Modal.confirm({
      title: '删除文件夹',
      content: count > 0
        ? `确定删除文件夹「${folder.name}」？其中 ${count} 个模型会回到「未分组」，模型本身不受影响。`
        : `确定删除文件夹「${folder.name}」？`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => {
        commitListPrefs((prev) => deleteFolder(prev, folderId));
        if (activeFolder === folderId) {
          setActiveFolder(null);
        }
        message.success('文件夹已删除');
      },
    });
  };

  const handleMoveModelsToFolder = (modelIds: string[], folderId: string | null) => {
    if (modelIds.length === 0) return;
    const label = folderId === null
      ? '未分组'
      : listPrefs.folders.find((folder) => folder.id === folderId)?.name ?? '文件夹';
    commitListPrefs((prev) => assignModelsToFolder(prev, modelIds, folderId));
    message.success(
      modelIds.length === 1
        ? `已移至「${label}」`
        : `已将 ${modelIds.length} 个模型移至「${label}」`,
    );
  };

  /** 卡片上的「移动到文件夹」菜单：folder:<id> / create / remove。 */
  const buildFolderMenuItems = (model: UserModelRecord): MenuProps['items'] => {
    const current = folderOfModel(model.model_id, listPrefs);
    return [
      ...listPrefs.folders.map((folder) => ({
        key: `folder:${folder.id}`,
        label: folder.name,
        icon: current?.id === folder.id ? <Check size={12} /> : <Folder size={12} />,
      })),
      ...(listPrefs.folders.length > 0 ? [{ type: 'divider' as const }] : []),
      { key: 'create', label: '新建文件夹…', icon: <FolderPlus size={12} /> },
      ...(current
        ? [{ key: 'remove', label: '移出文件夹', icon: <FolderMinus size={12} /> }]
        : []),
    ];
  };

  const handleFolderMenuClick = (model: UserModelRecord, key: string) => {
    if (key === 'create') {
      openCreateFolder([model.model_id]);
      return;
    }
    if (key === 'remove') {
      handleMoveModelsToFolder([model.model_id], null);
      return;
    }
    if (key.startsWith('folder:')) {
      handleMoveModelsToFolder([model.model_id], key.slice('folder:'.length));
    }
  };

  /** 整理模式底部动作菜单：folder:<id> / create / remove。 */
  const handleOrganizeMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (organizeSelected.length === 0) return;
    if (key === 'create') {
      openCreateFolder(organizeSelected);
      return;
    }
    if (key === 'remove') {
      handleMoveModelsToFolder(organizeSelected, null);
      setOrganizeSelected([]);
      return;
    }
    if (key.startsWith('folder:')) {
      handleMoveModelsToFolder(organizeSelected, key.slice('folder:'.length));
      setOrganizeSelected([]);
    }
  };

  const handleTogglePin = (modelId: string) => {
    // 最近置顶的排最前，方便刚钉上去的模型立刻可见
    updateListPrefs({ pinned: toggleId(listPrefs.pinned, modelId, true) });
  };

  const handleToggleFavorite = (modelId: string) => {
    updateListPrefs({ favorites: toggleId(listPrefs.favorites, modelId) });
  };

  const handleFusionCreated = async (modelId: string) => {
    setShowFusionModal(false);
    setFusionMode(false);
    setFusionSelected([]);
    await loadModels(true);
    setSelectedId(modelId);
    message.success('已切换至新融合模型详情');
  };

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
      void loadFeatureLabelCatalog();
      void loadShapSummary(selectedModel.model_id);
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

  const loadFeatureLabelCatalog = useCallback(async () => {
    if (featureCatalogLoaded) return;
    try {
      const catalog = await modelTrainingService.getFeatureCatalog();
      const categories = toDynamicCategories(catalog);
      if (categories.length > 0) {
        setFeatureLabelMap(buildFeatureLabelMap(categories));
      }
      setFeatureCatalogLoaded(true);
    } catch {
      setFeatureCatalogLoaded(true);
    }
  }, [featureCatalogLoaded]);

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
      const resolved = await modelTrainingService.resolveInferenceDateByCalendar(marketConfig.calendar, base);
      const predicted = await modelTrainingService.calcTargetDateByCalendar(marketConfig.calendar, resolved.date, horizonDays);
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

  const handleSetDefault = async () => {
    if (!selectedModel) return;
    await handleSetDefaultById(selectedModel.model_id);
  };

  const handleSetDefaultById = async (modelId: string) => {
    setSettingDefault(true);
    const canonicalId = modelId.startsWith('sys-') ? modelId.slice(4) : modelId;
    const applyDefault = async (force: boolean) => {
      await modelTrainingService.setDefaultModel(canonicalId, force);
      message.success(`已设为默认模型：${modelId}`);
      await loadModels(true);
      setSelectedId(modelId);
    };
    try {
      await applyDefault(false);
    } catch (err: unknown) {
      const gate = getAdmissionGate(err);
      if (gate && (await confirmAdmissionOverride(gate, '设为默认'))) {
        try {
          await applyDefault(true);
        } catch (retryErr: unknown) {
          message.error(`设置失败: ${retryErr instanceof Error ? retryErr.message : '未知'}`);
        }
      } else if (!gate) {
        message.error(`设置失败: ${err instanceof Error ? err.message : '未知'}`);
      }
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

  const handleRestore = () => {
    if (!selectedModel) return;
    Modal.confirm({
      title: '恢复模型',
      content: `确定将 "${modelDisplayName(selectedModel)}"（${selectedModel.model_id}）恢复到使用中？恢复后重新参与推理；如需接管推理，请再「设为默认」。`,
      okText: '确认恢复',
      cancelText: '取消',
      onOk: async () => {
        setRestoring(true);
        try {
          await modelTrainingService.restoreUserModel(selectedModel.model_id);
          message.success('已恢复使用');
          setListTab('active');
          await loadModels(true);
          setSelectedId(selectedModel.model_id);
        } catch (err: any) {
          message.error(`恢复失败: ${err?.response?.data?.detail ?? err?.message ?? '未知'}`);
        } finally { setRestoring(false); }
      },
    });
  };

  const handleRetrain = async () => {
    if (!selectedModel) return;
    setRetraining(true);
    try {
      const payload = await modelTrainingService.getRetrainConfig(selectedModel.model_id);
      const { config, warnings } = buildRetrainTrainingConfig(payload);
      sessionStorage.setItem(RETRAIN_SEED_STORAGE_KEY, serializeTrainingConfig(config));
      if (warnings.length > 0) message.warning(warnings.join('；'));
      message.success(
        `已带入「${config.configuration.displayName || '模型'}」：${config.configuration.selectedFeatures.length} 个特征，可在训练页调整时间切分`,
      );
      navigate('/model-training');
    } catch (err: any) {
      message.error(`带入训练配置失败: ${err?.message ?? '未知'}`);
    } finally {
      setRetraining(false);
    }
  };

  const handleActivate = () => {
    if (!selectedModel) return;
    Modal.confirm({
      title: '激活模型',
      content: '该模型因样本外验证未达阈值处于候选状态。确认手动激活？激活后可设为默认模型参与推理。',
      okText: '确认激活', okButtonProps: { type: 'primary' as const },
      cancelText: '取消',
      onOk: async () => {
        setActivating(true);
        const applyActivate = async (force: boolean) => {
          await modelTrainingService.activateUserModel(selectedModel.model_id, force);
          message.success('已激活');
          await loadModels(true);
          setSelectedId(selectedModel.model_id);
        };
        try {
          await applyActivate(false);
        } catch (err: unknown) {
          const gate = getAdmissionGate(err);
          if (gate && (await confirmAdmissionOverride(gate, '仍然激活'))) {
            try {
              await applyActivate(true);
            } catch (retryErr: unknown) {
              message.error(`激活失败: ${retryErr instanceof Error ? retryErr.message : '未知'}`);
            }
          } else if (!gate) {
            message.error(`激活失败: ${err instanceof Error ? err.message : '未知'}`);
          }
        } finally { setActivating(false); }
      },
    });
  };

  const openRenameModal = () => {
    if (!selectedModel) return;
    const meta = getMeta(selectedModel);
    setRenameValue(String(meta.display_name || meta.model_name || selectedModel.model_id));
    setShowRenameModal(true);
  };

  const handleRename = async () => {
    if (!selectedModel) return;
    const next = renameValue.trim();
    if (!next) { message.warning('展示名不能为空'); return; }
    if (next.length > 80) { message.warning('展示名过长（≤80 字符）'); return; }
    setRenaming(true);
    try {
      await modelTrainingService.renameUserModel(selectedModel.model_id, next);
      message.success(`已重命名为「${next}」`);
      setShowRenameModal(false);
      await loadModels(true);
      setSelectedId(selectedModel.model_id);
    } catch (err: any) {
      message.error(`重命名失败: ${err?.response?.data?.detail ?? err?.message ?? '未知'}`);
    } finally { setRenaming(false); }
  };

  const handleRunInference = async () => {
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
        // 2026-11-28：不能只说「前置检查未通过」——因子库没物化与模型坏了
        // 在用户眼里都是「不行」。把硬失败项原文抬到 toast。
        const failed = (precheck?.items || []).filter(
          (item) => item.severity !== 'soft' && !item.passed,
        );
        const detail = failed
          .map((item) => `${item.label || item.key}: ${item.detail || '未通过'}`)
          .join('；');
        message.error(detail ? `前置检查未通过：${detail}` : '前置检查未通过，请先处理阻断项', 8);
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

  const handleDeleteHistory = (runId: string) => {
    Modal.confirm({
      title: '删除推理历史',
      content: `确定要删除推理批次 "${runId}" 吗？此操作将同步删除关联的预测信号，且不可撤销。`,
      okText: '确定删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await modelTrainingService.deleteInferenceHistory(runId);
          message.success('推理历史已删除');
          if (selectedModel) {
            void loadInferenceHistory(selectedModel.model_id, {
              runId: historyRunIdFilter || undefined,
              status: historyStatusFilter === 'all' ? undefined : historyStatusFilter,
              inferenceDate: historyDateFilter ? historyDateFilter.format('YYYY-MM-DD') : undefined,
              page: 1,
              pageSize: 20,
            });
          }
        } catch (err: any) {
          message.error(`删除失败: ${err?.message ?? '未知错误'}`);
        }
      },
    });
  };

  const targetDate = inferenceTargetDate || '—';

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <div className="flex flex-row h-full w-full overflow-hidden">
          {/* ═══ 左侧边栏 ═══ */}
          <div
            className="flex-shrink-0 border-r border-slate-100 bg-white flex flex-col shadow-lg shadow-slate-100/50 z-10 h-full overflow-hidden"
            style={{ width: `${sidebarWidth}px` }}
          >
            {/* 顶部标题 + 搜索 */}
            <div className="px-5 pt-5 pb-4 border-b border-slate-50">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2.5">
                  <div className="w-8 h-8 rounded-xl bg-blue-600 flex items-center justify-center shadow shadow-blue-500/30 text-white">
                    <Layers size={17} />
                  </div>
                  <span className="text-[15px] font-black text-slate-800 tracking-tight">模型资产库 ({marketConfig.label})</span>
                </div>
                <div className="flex items-center gap-0.5">
                  <Tooltip title={fusionMode ? '退出融合模式' : '模型融合（多选 ≥2 个模型）'}>
                    <button
                      onClick={toggleFusionMode}
                      title="模型融合"
                      aria-label="模型融合"
                      className={clsx(
                        'w-7 h-7 flex items-center justify-center rounded-lg transition-all',
                        fusionMode
                          ? 'text-purple-600 bg-purple-50 ring-1 ring-purple-200'
                          : 'text-slate-400 hover:text-purple-600 hover:bg-purple-50',
                      )}
                    >
                      <Layers2 size={13} />
                    </button>
                  </Tooltip>
                  <button
                    onClick={() => loadModels()}
                    className="w-7 h-7 flex items-center justify-center rounded-lg text-slate-400 hover:text-blue-600 hover:bg-blue-50 transition-all"
                  >
                    <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
                  </button>
                </div>
              </div>
              <div className="mb-3 flex items-center justify-between gap-2">
                <MarketSelector />
                <span className="text-[10px] text-slate-400 font-mono whitespace-nowrap">切换市场 → 当前模型库</span>
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
              {([
                { key: 'active' as const, label: `使用中 (${activeModels.length})`, activeCls: 'bg-blue-600 text-white shadow shadow-blue-200' },
                { key: 'favorites' as const, label: `收藏 (${favoriteModels.length})`, activeCls: 'bg-amber-500 text-white shadow shadow-amber-200' },
                { key: 'archived' as const, label: `已归档 (${archivedModels.length})`, activeCls: 'bg-slate-800 text-white' },
              ]).map(tab => (
                <button
                  key={tab.key}
                  onClick={() => setListTab(tab.key)}
                  className={clsx(
                    'flex-1 py-1 rounded-lg text-[10px] font-black tracking-widest transition-all',
                    listTab === tab.key ? tab.activeCls : 'text-slate-400 hover:text-slate-600'
                  )}
                >{tab.label}</button>
              ))}
            </div>
            {/* 文件夹 */}
            <ModelFoldersPanel
              folders={listPrefs.folders}
              counts={folderCounts}
              activeFolder={activeFolder}
              open={folderPanelOpen}
              organizeMode={organizeMode}
              onToggleOpen={() => setFolderPanelOpen((prev) => !prev)}
              onToggleOrganize={toggleOrganizeMode}
              onSelectFolder={handleSelectFolder}
              onCreateFolder={() => openCreateFolder()}
              onRenameFolder={openRenameFolder}
              onDeleteFolder={handleDeleteFolder}
            />
            {/* 模型列表 */}
            <div className="flex-1 overflow-y-auto px-3 pb-5 space-y-1.5 custom-scrollbar">
              {loading ? (
                <div className="flex items-center justify-center py-16"><Spin /></div>
              ) : displayModels.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 gap-4">
                  <Empty
                    description={
                      <span className="text-xs text-slate-400">
                        {searchQuery ? '没有匹配的模型'
                          : activeFolder !== null ? `${activeFolderName ?? '该文件夹'}里还没有模型`
                            : listTab === 'favorites' ? '还没有收藏的模型（点卡片右上角书签收藏）'
                              : listTab === 'archived' ? '暂无归档模型'
                                : '暂无模型'}
                      </span>
                    }
                  />
                  {activeFolder !== null && !searchQuery && (
                    <p className="text-[10px] text-slate-400 text-center leading-relaxed max-w-[220px]">
                      点卡片上的文件夹按钮可直接移入，或用「整理模式」多选后批量移动。
                    </p>
                  )}
                  {listTab === 'active' && (
                    <Button
                      type="primary" size="small"
                      className="rounded-xl bg-blue-600 border-none font-bold text-xs"
                      icon={<Brain size={12} />}
                      onClick={() => navigate('/model-training')}
                    >去训练模型</Button>
                  )}
                </div>
              ) : (
                <>
                  <div className="px-2 pt-2 pb-1 flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 min-w-0">
                      {activeFolderName
                        ? <Folder size={10} className="text-blue-500 shrink-0" />
                        : <Brain size={10} className="text-blue-500 shrink-0" />}
                      <span className="text-[9px] font-black text-blue-500 tracking-widest truncate">
                        {activeFolderName ?? '我的模型资产'}
                      </span>
                      <span className="text-[9px] font-mono text-slate-400 shrink-0">{displayModels.length}</span>
                    </div>
                    {fusionMode ? (
                      <span className="text-[9px] font-black text-purple-600 tracking-widest whitespace-nowrap">融合模式 · 勾选成员</span>
                    ) : organizeMode ? (
                      <span className="text-[9px] font-black text-blue-600 tracking-widest whitespace-nowrap">整理模式 · 勾选模型</span>
                    ) : (
                      <Dropdown
                        trigger={['click']}
                        placement="bottomRight"
                        menu={{
                          selectable: true,
                          selectedKeys: [listPrefs.sortKey],
                          items: MODEL_SORT_OPTIONS.map(o => ({ key: o.key, label: o.label })),
                          onClick: ({ key }) => updateListPrefs({ sortKey: key as ModelSortKey }),
                        }}
                      >
                        <button
                          type="button"
                          title="模型排序"
                          className="flex items-center gap-1 px-1.5 py-1 rounded-md text-[9px] font-black text-slate-400 hover:text-blue-600 hover:bg-blue-50 transition-all shrink-0"
                        >
                          <ArrowUpDown size={10} />
                          {activeSortLabel}
                        </button>
                      </Dropdown>
                    )}
                  </div>
                  {displayModels.map(model => (
                    <ModelCard
                      key={model.model_id}
                      model={model}
                      isSelected={selectedId === model.model_id}
                      onClick={() => {
                        if (fusionMode) toggleFusionSelect(model);
                        else if (organizeMode) toggleOrganizeSelect(model.model_id);
                        else setSelectedId(model.model_id);
                      }}
                      onSetDefault={() => void handleSetDefaultById(model.model_id)}
                      canSetDefault={!fusionMode && !organizeMode && !model.is_default && model.status !== 'archived'}
                      isPinned={listPrefs.pinned.includes(model.model_id)}
                      isFavorite={listPrefs.favorites.includes(model.model_id)}
                      onTogglePin={() => handleTogglePin(model.model_id)}
                      onToggleFavorite={() => handleToggleFavorite(model.model_id)}
                      folderName={activeFolder === null ? folderOfModel(model.model_id, listPrefs)?.name ?? null : null}
                      folderMenu={!fusionMode && !organizeMode ? (
                        <span onClick={(e) => e.stopPropagation()}>
                          <Dropdown
                            trigger={['click']}
                            placement="bottomRight"
                            menu={{
                              items: buildFolderMenuItems(model),
                              onClick: ({ key }) => handleFolderMenuClick(model, key),
                            }}
                          >
                            <button
                              type="button"
                              aria-label="移动到文件夹"
                              title="移动到文件夹"
                              onClick={(e) => e.stopPropagation()}
                              className="w-5 h-5 flex items-center justify-center rounded-md text-slate-300 hover:text-blue-600 hover:bg-blue-50 transition-colors"
                            >
                              <FolderInput size={10} />
                            </button>
                          </Dropdown>
                        </span>
                      ) : undefined}
                      selectable={fusionMode || organizeMode}
                      selected={fusionMode
                        ? fusionSelected.includes(model.model_id)
                        : organizeSelected.includes(model.model_id)}
                      onToggleSelect={() => {
                        if (fusionMode) toggleFusionSelect(model);
                        else toggleOrganizeSelect(model.model_id);
                      }}
                      selectDisabled={fusionMode && !isFusionEligible(model)}
                    />
                  ))}
                </>
              )}
            </div>
            {/* 底部操作 */}
            <div className="px-4 pb-4 pt-3 border-t border-slate-100 bg-slate-50/50">
              {fusionMode ? (
                <div className="space-y-2">
                  <div className="text-[9px] text-slate-500 font-bold leading-snug">
                    已选 <span className="text-purple-600 font-black">{fusionSelectedModels.length}</span> 个成员
                    {!canFuse && fusionSelectedModels.length < 2 && '（需 ≥2 个本人 ready/active 模型，同市场）'}
                    {fusionSelectedModels.length >= 2 && fusionMarkets.size > 1 && '（成员跨市场，不可融合）'}
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="small"
                      className="rounded-xl h-9 px-3 font-bold text-xs border-slate-200 text-slate-500"
                      onClick={toggleFusionMode}
                    >
                      取消
                    </Button>
                    <Button
                      type="primary"
                      size="small"
                      icon={<Layers2 size={13} />}
                      disabled={!canFuse}
                      className="flex-1 rounded-xl h-9 bg-purple-600 hover:bg-purple-500 border-none font-black text-xs shadow-sm flex items-center justify-center gap-1.5"
                      onClick={() => setShowFusionModal(true)}
                    >
                      融合为集成模型
                    </Button>
                  </div>
                </div>
              ) : organizeMode ? (
                <div className="space-y-2">
                  <div className="text-[9px] text-slate-500 font-bold leading-snug">
                    已选 <span className="text-blue-600 font-black">{organizeSelected.length}</span> 个模型
                    <button
                      type="button"
                      className="ml-1.5 text-blue-500 hover:underline"
                      onClick={() => setOrganizeSelected(displayModels.map((m) => m.model_id))}
                    >
                      全选当前
                    </button>
                    {organizeSelected.length > 0 && (
                      <button
                        type="button"
                        className="ml-1.5 text-slate-400 hover:underline"
                        onClick={() => setOrganizeSelected([])}
                      >
                        清空
                      </button>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="small"
                      className="rounded-xl h-9 px-3 font-bold text-xs border-slate-200 text-slate-500"
                      onClick={toggleOrganizeMode}
                    >
                      退出
                    </Button>
                    <Dropdown
                      trigger={['click']}
                      disabled={organizeSelected.length === 0}
                      placement="topRight"
                      menu={{
                        items: [
                          ...listPrefs.folders.map((folder) => ({
                            key: `folder:${folder.id}`,
                            label: folder.name,
                            icon: <Folder size={12} />,
                          })),
                          ...(listPrefs.folders.length > 0 ? [{ type: 'divider' as const }] : []),
                          { key: 'create', label: '新建文件夹并移入…', icon: <FolderPlus size={12} /> },
                          { key: 'remove', label: '移出文件夹（未分组）', icon: <FolderMinus size={12} /> },
                        ],
                        onClick: handleOrganizeMenuClick,
                      }}
                    >
                      <Button
                        type="primary"
                        size="small"
                        icon={<FolderInput size={13} />}
                        disabled={organizeSelected.length === 0}
                        className="flex-1 rounded-xl h-9 bg-blue-600 hover:bg-blue-500 border-none font-black text-xs shadow-sm flex items-center justify-center gap-1.5"
                      >
                        移动到文件夹
                      </Button>
                    </Dropdown>
                    <Button
                      size="small"
                      icon={<GitCompare size={13} />}
                      disabled={organizeSelected.length < 2}
                      onClick={() => setShowCompareModal(true)}
                      title={
                        organizeSelected.length < 2
                          ? '至少选中 2 个模型才能对比'
                          : `对比选中的 ${organizeSelected.length} 个模型（最近3个月 + 测试集 IC）`
                      }
                      className="flex-1 rounded-xl h-9 border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100 font-black text-xs shadow-sm flex items-center justify-center gap-1.5"
                    >
                      对比
                    </Button>
                  </div>
                </div>
              ) : (
                <Button
                  type="primary"
                  icon={<Compass size={15} />}
                  className="w-full rounded-xl h-9 bg-blue-600 hover:bg-blue-500 border-none font-bold text-xs shadow-sm flex items-center justify-center gap-1.5"
                  onClick={() => navigate('/model-hub')}
                >
                  模型广场
                </Button>
              )}
            </div>
          </div>
          {/* 左栏宽度拖拽把手（可聚焦，方向键微调，双击恢复默认） */}
          <button
            type="button"
            aria-label="调整模型列表宽度（拖拽或左右方向键微调，双击恢复默认）"
            title="拖拽调整宽度，双击恢复默认"
            onPointerDown={handleSidebarResizeStart}
            onPointerMove={handleSidebarResizeMove}
            onPointerUp={handleSidebarResizeEnd}
            onPointerCancel={handleSidebarResizeEnd}
            onLostPointerCapture={() => {
              sidebarDragRef.current = null;
              setSidebarDragging(false);
            }}
            onKeyDown={handleSidebarResizeKeyDown}
            onDoubleClick={handleSidebarResetWidth}
            className={clsx(
              'shrink-0 w-1.5 cursor-col-resize touch-none select-none transition-colors',
              sidebarDragging ? 'bg-blue-400/70' : 'bg-slate-100 hover:bg-blue-200/80',
            )}
          />
          <span className="sr-only" aria-live="polite">{sidebarAnnounce}</span>
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
                  <div className="flex justify-between items-start mb-6 gap-4">
                    <div className="min-w-0 flex-1">
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
                        <Tag className="rounded-xl border-none text-[9px] font-black bg-blue-50 text-blue-600 m-0">
                          市场: {marketConfig.label}
                        </Tag>
                        {getMeta(selectedModel).feature_count && (
                          <Tag className="rounded-xl border-none text-[9px] font-black bg-purple-50 text-purple-600 m-0">
                            {getMeta(selectedModel).feature_count} 维
                          </Tag>
                        )}
                      </div>
                      <h2 className="text-2xl font-black text-slate-900 tracking-tight m-0 font-mono leading-tight">
                        {modelDisplayName(selectedModel)}
                        {!isSystemModel(selectedModel) && (
                          <Tooltip title="修改展示名">
                            <button
                              type="button"
                              onClick={openRenameModal}
                              aria-label="修改展示名"
                              className="ml-2 align-middle inline-flex items-center justify-center w-7 h-7 rounded-lg text-slate-300 hover:text-slate-700 hover:bg-slate-100 transition-colors"
                            >
                              <Pencil size={14} />
                            </button>
                          </Tooltip>
                        )}
                      </h2>
                      <p className="text-xs text-slate-400 mt-1 font-mono">
                        {selectedModel.model_id}
                        {selectedModel.created_at && ` · 创建于 ${dayjs(selectedModel.created_at).format('YYYY-MM-DD HH:mm:ss')}`}
                      </p>
                      {getMeta(selectedModel).description && (
                        <p className="text-xs text-slate-500 mt-2 max-w-xl">{getMeta(selectedModel).description}</p>
                      )}
                    </div>
                    <div className="flex gap-2 flex-shrink-0">
                      <Button
                        icon={<Sparkles size={13} />}
                        className="rounded-xl h-9 px-4 font-bold border-blue-200 text-blue-600 bg-blue-50/50 hover:bg-blue-100 text-xs"
                        onClick={() => setShowPublishModal(true)}
                      >
                        发布到广场
                      </Button>
                      <Button
                        icon={<Code size={13} />}
                        className="rounded-xl h-9 px-4 font-bold border-slate-200 text-xs"
                        onClick={() => setShowConfigModal(true)}
                      >配置</Button>
                      {!isSystemModel(selectedModel) && (
                        <Button
                          icon={<RefreshCw size={13} />}
                          className="rounded-xl h-9 px-4 font-bold border-emerald-200 text-emerald-700 bg-emerald-50/50 hover:bg-emerald-100 text-xs"
                          onClick={() => void handleRetrain()}
                          loading={retraining}
                        >重新训练</Button>
                      )}
                      {selectedModel.status === 'archived' ? (
                        <Button
                          type="primary"
                          icon={<RotateCcw size={13} />}
                          className="rounded-xl h-9 px-5 font-black bg-emerald-600 hover:bg-emerald-500 border-none text-xs"
                          onClick={handleRestore}
                          loading={restoring}
                        >恢复使用</Button>
                      ) : (
                        <>
                          {selectedModel.status === 'candidate' && (
                            <Button
                              type="primary"
                              icon={<Play size={13} />}
                              className="rounded-xl h-9 px-4 font-bold text-xs"
                              onClick={handleActivate}
                              loading={activating}
                            >激活</Button>
                          )}
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
                        key: 'rolling',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center gap-1.5">
                            滚动训练
                          </span>
                        ),
                        children: <ModelRollingPanel model={selectedModel} />,
                      },
                      {
                        key: 'attribution',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center">
                            归因分析
                          </span>
                        ),
                        children: (
                          <AttributionAnalysisPanel
                            model={selectedModel}
                            shapSummary={shapSummary}
                            loading={shapLoading}
                            error={shapError}
                            featureLabelMap={featureLabelMap}
                            onRefresh={() => {
                              if (selectedModel) {
                                void loadFeatureLabelCatalog();
                                void loadShapSummary(selectedModel.model_id);
                              }
                            }}
                          />
                        ),
                      },
                      {
                        key: 'drift',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center gap-1.5">
                            数据漂移
                          </span>
                        ),
                        children: (
                          <DriftTabPanel
                            modelId={selectedModel.model_id}
                            marketLabel={marketConfig.label}
                            featureLabelMap={featureLabelMap}
                          />
                        ),
                      },
                      {
                        key: 'market',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center gap-1.5">
                            大盘分析
                          </span>
                        ),
                        children: <MarketRegimePanel modelId={selectedModel.model_id} market={currentMarket} />,
                      },
                      {
                        key: 'inference-mgmt',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center gap-1.5">
                            推理管理
                          </span>
                        ),
                        children: <InferenceCoveragePanel modelId={selectedModel.model_id} calendar={marketConfig.calendar} />,
                      },
                      {
                        key: 'neutralization',
                        label: (
                          <span className="text-xs font-black uppercase tracking-widest px-1 flex items-center gap-1.5">
                            中性化
                          </span>
                        ),
                        children: (
                          <NeutralizationModelPanel
                            modelId={selectedModel.model_id}
                            modelName={modelDisplayName(selectedModel)}
                            modelType={
                              typeof selectedModel.metadata_json?.model_type === 'string'
                                ? selectedModel.metadata_json.model_type
                                : undefined
                            }
                            horizonDays={
                              Number.isFinite(
                                Number(selectedModel.metadata_json?.target_horizon_days),
                              )
                                ? Number(selectedModel.metadata_json?.target_horizon_days)
                                : undefined
                            }
                            featureCount={
                              Number.isFinite(
                                Number(selectedModel.metadata_json?.feature_count),
                              )
                                ? Number(selectedModel.metadata_json?.feature_count)
                                : undefined
                            }
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

      {/* ═══ 修改展示名 Modal ═══ */}
      <Modal
        title={null}
        open={showRenameModal}
        onCancel={() => setShowRenameModal(false)}
        onOk={handleRename}
        okText="保存"
        cancelText="取消"
        confirmLoading={renaming}
        width={460}
        centered
        okButtonProps={{ className: 'rounded-xl font-bold' }}
        cancelButtonProps={{ className: 'rounded-xl' }}
        styles={{ mask: { backdropFilter: 'blur(4px)', backgroundColor: 'rgba(0,0,0,0.2)' } }}
      >
        <div className="pt-2">
          <h3 className="text-lg font-black text-slate-800 m-0 tracking-tight flex items-center gap-2 mb-1">
            <Pencil size={18} className="text-blue-500" />
            修改展示名
          </h3>
          <p className="text-[11px] text-slate-400 m-0 mb-4">
            只改展示名（模型列表 / 详情 / 滚动训练派生描述）；模型 ID 与台账绑定不动。
          </p>
          <Input
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onPressEnter={handleRename}
            maxLength={80}
            showCount
            placeholder={`给模型起一个能看懂的名字，如：${marketConfig.benchmarkName}增强-低波`}
            className="rounded-xl"
            autoFocus
          />
        </div>
      </Modal>

      {/* ═══ 模型对比 Modal（最近3个月 + 测试集 IC） ═══ */}
      <ModelCompareModal
        open={showCompareModal}
        models={allModels.filter((model) => organizeSelected.includes(model.model_id))}
        favorites={listPrefs.favorites}
        pinned={listPrefs.pinned}
        onToggleFavorite={handleToggleFavorite}
        onTogglePin={handleTogglePin}
        onClose={() => setShowCompareModal(false)}
      />

      {/* ═══ 新建 / 重命名文件夹 Modal ═══ */}
      <ModelFolderModal
        mode={folderModalMode}
        value={folderNameValue}
        moveCount={folderModalMoveIds?.length ?? 0}
        onChange={setFolderNameValue}
        onCancel={closeFolderModal}
        onSubmit={submitFolderModal}
      />

      {/* ═══ 模型融合 Modal（机构级 v2：预览 → 创建即进日更推理） ═══ */}
      <FusionModal
        open={showFusionModal}
        models={fusionSelectedModels}
        onCancel={() => setShowFusionModal(false)}
        onCreated={(modelId) => void handleFusionCreated(modelId)}
      />

      {/* 发布到广场对话框 */}
      <PublishModelModal
        open={showPublishModal}
        onClose={() => setShowPublishModal(false)}
        userModels={userModels}
        initialModelId={selectedModel?.model_id}
        onSuccess={() => {
          message.success('模型发布流程已完成');
        }}
      />
    </div>
  );
};

export default ModelRegistryPage;
