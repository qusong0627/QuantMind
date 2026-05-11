import React from 'react';
import axios from 'axios';
import { motion } from 'framer-motion';
import {
  Activity,
  BarChart3,
  CandlestickChart,
  Download,
  Filter,
  Flame,
  LibraryBig,
  LayoutDashboard,
  Microscope,
  Quote,
  RefreshCw,
  Search,
  Sparkles,
  Target,
} from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import {
  Button,
  Card,
  Collapse,
  Empty,
  Input,
  InputNumber,
  message,
  Modal,
  Pagination,
  Segmented,
  Select,
  Slider,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PAGE_LAYOUT } from '../config/pageLayout';
import { modelTrainingService } from '../services/modelTrainingService';
import { researchService, type ResearchRunOption } from '../services/researchService';
import '../styles/research-next-theme.css';

const { Text, Title, Paragraph } = Typography;

type SignalType = 'buy' | 'hold' | 'sell';
type ConfidenceLevel = 'high' | 'medium' | 'watch';
type SortKey = 'score' | 'limitUp' | 'turnover' | 'amount';
type FilterSectionKey = 'common' | 'market' | 'sector' | 'fundamental' | 'technical' | 'advanced';
type DataSourceTab = 'candidates' | 'watchlist' | 'pool';

interface ResearchModelOption {
  modelId: string;
  name: string;
  style: string;
  description: string;
}

interface WatchlistRow {
  key: string;
  symbol: string;
  stockName: string | null;
  addedAt: string | null;
  sourceRunId: string | null;
  notes: string | null;
  tags: string[];
}

interface ResearchPoolRow {
  key: string;
  symbol: string;
  stockName: string | null;
  addedAt: string | null;
  sourceRunId: string | null;
  modelId: string | null;
  fusionScore: number | null;
  thesisSummary: string | null;
  status: string;
  notes: string | null;
  tags: string[];
}

interface ResearchStockRow {
  key: string;
  modelId: string;
  runId: string;
  rank: number;
  code: string;
  name: string;
  score: number;
  latestChange: number;
  nextDayReturn?: number | null;
  day3Return?: number | null;
  consecutiveLimitUpDays: number;
  volumeTrend3d: boolean;
  volumeTrend5d: boolean;
  turnoverRate: number;
  amount: number;
  sector: string;
  concept: string;
  signal?: 'buy' | 'hold' | 'sell';
  pe?: number;
  roe?: number;
  profitGrowth?: number;
  rsi?: number;
  mainFlow?: number;
  flowNetAmount?: number;
  instOwnership?: number;
  ma5?: number;
  ma10?: number;
  ma20?: number;
  maGap5?: number;
  volRatio5?: number;
  return1d?: number;
  pb?: number;
  totalMv?: number;
  floatMv?: number;
  listedDays?: number;
  return3d?: number;
  maGap10?: number;
  maGap20?: number;
  rsi14?: number;
  volRatio20?: number;
  conceptTags?: string[];
  indexTags?: string[];
  riskFlags: string[];
  thesis: string;
  confidence?: 'high' | 'medium' | 'watch';
  isMatched?: boolean;
  isSt?: boolean;
  isTradable?: boolean;
  isHs300?: boolean;
  isCsi500?: boolean;
  isCsi1000?: boolean;
}

const PRESET_FILTER_MAP: Record<string, any> = {
  '高分优选': { minScore: 0.05 },
  '连板突破': { limitUpDays: 2 },
  '白马蓝筹': { roeMin: 5, totalMvMin: 80 },
  '题材活跃': { turnoverMin: 8, amountMin: 10 },
  '低位反弹': { maGap20Max: -0.03, rsiMax: 45 },
};

const DEFAULT_RESEARCH_FILTERS = {
  minScore: -1.0,
  limitUpDays: 0,
  amountRange: [0, 100000] as [number, number],
  turnoverRange: [0, 100] as [number, number],
  volumeTrendOnly: false,
  highConfidenceOnly: false,
  selectedSectors: [] as string[],
  selectedConcepts: [] as string[],
  selectedIndices: [] as string[],
  peRange: [0, 100000] as [number, number],
  roeRange: [-1000, 1000] as [number, number],
  profitGrowthRange: [-1000, 1000] as [number, number],
  pbRange: [0, 1000] as [number, number],
  totalMvRange: [0, 1000000] as [number, number],
  floatMvRange: [0, 1000000] as [number, number],
  listedDaysRange: [0, 30000] as [number, number],
  return3dRange: [-100, 100] as [number, number],
  rsiRange: [0, 100] as [number, number],
  mainFlowRange: [-1000000, 1000000] as [number, number],
  instOwnershipRange: [0, 100] as [number, number],
  maGap5Range: [-100, 100] as [number, number],
  maGap10Range: [-100, 100] as [number, number],
  maGap20Range: [-100, 100] as [number, number],
  volRatio5Range: 0,
  volRatio20Range: 0,
  rsi14Range: [0, 100] as [number, number],
  return1dRange: [-100, 100] as [number, number],
  excludeSt: false,
  marketType: 'all',
  advancedFiltersEnabled: false,
};

const BUTTON_STYLES = {
  headerRefresh: 'h-9 rounded-xl border-slate-200 bg-white px-4 text-xs font-bold text-slate-600 shadow-sm transition-all hover:border-blue-400 hover:text-blue-500 hover:shadow-md active:scale-95',
  headerSave: 'h-9 rounded-xl border border-slate-200 bg-white px-4 text-xs font-bold text-slate-700 shadow-sm transition-all hover:border-slate-300 hover:text-slate-900 active:scale-95',
  applyFilters: 'group relative w-full overflow-hidden rounded-2xl bg-slate-900 py-3.5 font-black text-white shadow-xl shadow-slate-900/20 transition-all hover:bg-slate-800 hover:shadow-2xl hover:-translate-y-0.5 active:scale-95 active:translate-y-0',
};

const FIELD_STYLES = {
  select: 'research-next-select rounded-xl font-bold border-slate-200',
  input: 'research-next-input rounded-xl border-slate-200 font-medium h-10',
  slider: 'research-next-slider py-4',
  collapse: 'research-next-collapse border-none bg-transparent',
  table: 'research-next-table custom-scrollbar',
  segmented: 'research-next-segmented rounded-2xl p-1 bg-slate-100',
};

const TEMPLATE_BUTTON_STYLES = {
  idle: 'bg-slate-50 text-slate-500 border-slate-200 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-500',
  active: 'bg-blue-600 text-white border-blue-600 shadow-md shadow-blue-500/20',
};

const ResearchMetricCard: React.FC<{
  icon: any;
  label: string;
  value: string | number;
  subLabel: string;
  accentColor: string;
}> = ({ icon: Icon, label, value, subLabel, accentColor }) => (
  <motion.div 
    whileHover={{ y: -6, scale: 1.02, transition: { type: "spring", stiffness: 400, damping: 10 } }}
    className="research-stat-card group relative overflow-hidden rounded-[32px] border border-white p-7 shadow-xl shadow-slate-200/50 transition-all duration-500 hover:shadow-2xl hover:shadow-blue-500/10"
    style={{ 
      background: `linear-gradient(135deg, white 0%, ${accentColor}05 100%)`,
    }}
  >
    {/* 背景装饰光晕 */}
    <div 
      className="absolute -right-6 -top-6 h-32 w-32 rounded-full blur-3xl opacity-20 transition-all duration-700 group-hover:scale-150 group-hover:opacity-40"
      style={{ backgroundColor: accentColor }}
    />
    
    <div className="relative z-10 flex items-start justify-between">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <div className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: accentColor }} />
          <span className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">{label}</span>
        </div>
        
        <div className="flex items-baseline gap-1">
          <div className="text-5xl font-black text-slate-900 tracking-tight transition-all duration-500 group-hover:scale-110 origin-left">
            {value}
          </div>
          <div className="h-2 w-2 rounded-full opacity-0 transition-opacity duration-500 group-hover:opacity-100" style={{ backgroundColor: accentColor }} />
        </div>
        
        <div className="flex items-center gap-1.5 rounded-full bg-slate-50/50 py-1 pr-3 w-fit">
          <div className="h-1 w-3 rounded-full" style={{ backgroundColor: accentColor }} />
          <span className="text-[11px] font-bold text-slate-500/90 whitespace-nowrap">{subLabel}</span>
        </div>
      </div>

      <div 
        className="flex h-14 w-14 items-center justify-center rounded-[22px] shadow-2xl transition-all duration-700 group-hover:rotate-12 group-hover:scale-110"
        style={{ 
          background: `linear-gradient(135deg, ${accentColor} 0%, ${accentColor}dd 100%)`,
          boxShadow: `0 10px 20px -5px ${accentColor}40`
        }}
      >
        <Icon className="h-7 w-7 text-white" />
      </div>
    </div>
  </motion.div>
);

/**
 * 优雅的范围输入组件 - 用于投研筛选器手动输入
 */
const RangeInput: React.FC<{
  label?: string;
  value: [number, number] | number;
  onChange: (val: any) => void;
  placeholder?: [string, string] | string;
  prefix?: string;
  suffix?: string;
  isSingle?: boolean;
  step?: number;
}> = ({ label, value, onChange, placeholder, prefix, suffix, isSingle, step = 1 }) => {
  const isRange = Array.isArray(value);
  return (
    <div className="space-y-1.5">
      {label && <div className="text-[11px] font-black text-slate-500 uppercase tracking-tight">{label}</div>}
      <div className="flex items-center gap-1.5">
        <InputNumber
          className="research-next-input-number flex-1"
          placeholder={isRange ? (Array.isArray(placeholder) ? placeholder[0] : "下限") : (typeof placeholder === 'string' ? placeholder : "数值")}
          value={isRange ? value[0] : value}
          onChange={(v) => {
            if (isRange) onChange([v ?? 0, value[1]]);
            else onChange(v ?? 0);
          }}
          prefix={prefix}
          suffix={suffix}
          step={step}
          controls={false}
        />
        {isRange && (
          <>
            <div className="h-[1px] w-2 bg-slate-300" />
            <InputNumber
              className="research-next-input-number flex-1"
              placeholder={Array.isArray(placeholder) ? placeholder[1] : "上限"}
              value={value[1]}
              onChange={(v) => onChange([value[0], v ?? 0])}
              prefix={prefix}
              suffix={suffix}
              step={step}
              controls={false}
            />
          </>
        )}
      </div>
    </div>
  );
};

export const ResearchPlatformPage: React.FC = () => {
  const safeNum = (value: unknown, fallback = 0): number =>
    typeof value === 'number' && Number.isFinite(value) ? value : fallback;
  const normalizeSymbol = (raw: string): string => {
    const s = (raw || '').trim().toUpperCase();
    if (!s) return s;
    
    // 1. 已经是正确的 Prefix 格式 (SH/SZ/BJ + 6位数字)
    if (/^(SH|SZ|BJ)\d{6}$/.test(s)) return s;
    
    // 2. 处理 Suffix 格式 (6位数字 + .SH/SZ/BJ)
    const suffixMatch = s.match(/^(\d{6})\.(SH|SZ|BJ)$/);
    if (suffixMatch) {
      const [, symbol, market] = suffixMatch;
      return `${market}${symbol}`;
    }
    
    // 3. 处理纯 6 位数字 (基于号段尝试自动补全)
    if (/^\d{6}$/.test(s)) {
      // 上海: 60, 68, 90
      if (s.startsWith('6') || s.startsWith('9')) return `SH${s}`;
      // 深圳: 00, 30, 20
      if (s.startsWith('0') || s.startsWith('2') || s.startsWith('3')) return `SZ${s}`;
      // 北京: 83, 43, 87, 88
      if (s.startsWith('4') || s.startsWith('8')) return `BJ${s}`;
    }
    
    return s;
  };
  const normalizeRoe = (value: unknown): number => {
    let v = safeNum(value, 0);
    if (Math.abs(v) > 200) v = v / 100;
    return v;
  };
  const normalizeYiValue = (value: unknown): number => {
    const v = safeNum(value, 0);
    return Math.abs(v) >= 1_000_000 ? v / 100_000_000 : v;
  };
  const fmt2 = (value: unknown): string => safeNum(value, 0).toFixed(2);
  const fmtPercent2 = (value: unknown): string => `${safeNum(value, 0).toFixed(2)}%`;
  const fmtSignedPercent2 = (value: unknown): string => {
    const v = safeNum(value, 0);
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
  };
  const fmtNullableSignedPercent2 = (value: unknown): string => (
    value === null || value === undefined ? '-' : fmtSignedPercent2(value)
  );
  const fmtMainFlowCn = (value: unknown): string => {
    const v = safeNum(value, 0); // 后端口径：百万
    if (Math.abs(v) >= 10000) return `${(v / 10000).toFixed(2)}亿`;
    return `${v.toFixed(2)}百万`;
  };
  const [availableModels, setAvailableModels] = React.useState<ResearchModelOption[]>([]);
  const [selectedModelId, setSelectedModelId] = React.useState<string>('');
  const [availableRuns, setAvailableRuns] = React.useState<ResearchRunOption[]>([]);
  const [selectedRunId, setSelectedRunId] = React.useState<string>('');
  const [candidatePool, setCandidatePool] = React.useState<ResearchStockRow[]>([]);
  const [overviewLoading, setOverviewLoading] = React.useState<boolean>(false);
  const [syncing, setSyncing] = React.useState<boolean>(false);
  const [keyword, setKeyword] = React.useState<string>('');
  const [activeDataSource, setActiveDataSource] = React.useState<DataSourceTab>('candidates');
  const [detailModalOpen, setDetailModalOpen] = React.useState<boolean>(false);
  const [selectedStockKey, setSelectedStockKey] = React.useState<string | null>(null);
  const [klineData, setKlineData] = React.useState<any[]>([]);
  const [klineLoading, setKlineLoading] = React.useState<boolean>(false);
  const [sortKey, setSortKey] = React.useState<SortKey>('score');

  // 分页状态
  const [candidatePage, setCandidatePage] = React.useState<number>(1);
  const [candidatePageSize, setCandidatePageSize] = React.useState<number>(10);
  const [watchlistPage, setWatchlistPage] = React.useState<number>(1);
  const [watchlistPageSize, setWatchlistPageSize] = React.useState<number>(12);
  const [poolPage, setPoolPage] = React.useState<number>(1);
  const [poolPageSize, setPoolPageSize] = React.useState<number>(12);

  // 自选和研究池数据
  const [watchlistData, setWatchlistData] = React.useState<WatchlistRow[]>([]);
  const [watchlistLoading, setWatchlistLoading] = React.useState<boolean>(false);
  const [watchlistTotal, setWatchlistTotal] = React.useState<number>(0);
  const [poolData, setPoolData] = React.useState<ResearchPoolRow[]>([]);
  const [poolLoading, setPoolLoading] = React.useState<boolean>(false);
  const [poolTotal, setPoolTotal] = React.useState<number>(0);

  // 自选/研究池特征富化映射
  const [watchlistFeatures, setWatchlistFeatures] = React.useState<Record<string, ResearchStockRow>>({});
  const [poolFeatures, setPoolFeatures] = React.useState<Record<string, ResearchStockRow>>({});

  const [minScore, setMinScore] = React.useState<number>(DEFAULT_RESEARCH_FILTERS.minScore);
  const [limitUpDays, setLimitUpDays] = React.useState<number>(DEFAULT_RESEARCH_FILTERS.limitUpDays);
  const [amountRange, setAmountRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.amountRange);
  const [turnoverRange, setTurnoverRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.turnoverRange);
  const [volumeTrendOnly, setVolumeTrendOnly] = React.useState<boolean>(DEFAULT_RESEARCH_FILTERS.volumeTrendOnly);
  const [highConfidenceOnly, setHighConfidenceOnly] = React.useState<boolean>(DEFAULT_RESEARCH_FILTERS.highConfidenceOnly);
  const [selectedSectors, setSelectedSectors] = React.useState<string[]>(DEFAULT_RESEARCH_FILTERS.selectedSectors);
  const [selectedConcepts, setSelectedConcepts] = React.useState<string[]>(DEFAULT_RESEARCH_FILTERS.selectedConcepts);
  const [selectedIndices, setSelectedIndices] = React.useState<string[]>(DEFAULT_RESEARCH_FILTERS.selectedIndices);

  const [peRange, setPeRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.peRange);
  const [roeRange, setRoeRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.roeRange);
  const [profitGrowthRange, setProfitGrowthRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.profitGrowthRange);
  const [pbRange, setPbRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.pbRange);
  const [totalMvRange, setTotalMvRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.totalMvRange);
  const [floatMvRange, setFloatMvRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.floatMvRange);
  const [listedDaysRange, setListedDaysRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.listedDaysRange);
  const [return3dRange, setReturn3dRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.return3dRange);

  const [rsiRange, setRsiRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.rsiRange);
  const [mainFlowRange, setMainFlowRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.mainFlowRange);
  const [instOwnershipRange, setInstOwnershipRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.instOwnershipRange);

  const [activePreset, setActivePreset] = React.useState<string | null>(null);
  const [activeFilterSections, setActiveFilterSections] = React.useState<FilterSectionKey[]>(['common']);

  // 扩展的研究条件
  const [maGap5Range, setMaGap5Range] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.maGap5Range);
  const [maGap10Range, setMaGap10Range] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.maGap10Range);
  const [maGap20Range, setMaGap20Range] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.maGap20Range);
  const [volRatio5Range, setVolRatio5Range] = React.useState<number>(DEFAULT_RESEARCH_FILTERS.volRatio5Range);
  const [volRatio20Range, setVolRatio20Range] = React.useState<number>(DEFAULT_RESEARCH_FILTERS.volRatio20Range);
  const [rsi14Range, setRsi14Range] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.rsi14Range);
  const [return1dRange, setReturn1dRange] = React.useState<[number, number]>(DEFAULT_RESEARCH_FILTERS.return1dRange);
  const [excludeSt, setExcludeSt] = React.useState<boolean>(DEFAULT_RESEARCH_FILTERS.excludeSt);
  const [marketType, setMarketType] = React.useState<string>(DEFAULT_RESEARCH_FILTERS.marketType);
  const [advancedFiltersEnabled, setAdvancedFiltersEnabled] = React.useState<boolean>(DEFAULT_RESEARCH_FILTERS.advancedFiltersEnabled);

  const [appliedFilters, setAppliedFilters] = React.useState({
    minScore, limitUpDays, amountRange, turnoverRange, volumeTrendOnly, highConfidenceOnly, selectedSectors, selectedConcepts, selectedIndices,
    peRange, roeRange, profitGrowthRange, rsiRange, mainFlowRange, instOwnershipRange,
    maGap5Range, maGap10Range, maGap20Range, volRatio5Range, volRatio20Range, rsi14Range, return1dRange,
    pbRange, totalMvRange, floatMvRange, listedDaysRange, return3dRange, advancedFiltersEnabled,
    excludeSt, marketType
  });

  const [loadRange, setLoadRange] = React.useState<number>(200);

  const hasPendingFilterChanges = React.useMemo(() => {
    return JSON.stringify({
      minScore, limitUpDays, amountRange, turnoverRange, volumeTrendOnly, highConfidenceOnly, selectedSectors, selectedConcepts, selectedIndices,
      peRange, roeRange, profitGrowthRange, rsiRange, mainFlowRange, instOwnershipRange,
      maGap5Range, maGap10Range, maGap20Range, volRatio5Range, volRatio20Range, rsi14Range, return1dRange,
      pbRange, totalMvRange, floatMvRange, listedDaysRange, return3dRange, advancedFiltersEnabled,
      excludeSt, marketType
    }) !== JSON.stringify(appliedFilters);
  }, [
    minScore, limitUpDays, amountRange, turnoverRange, volumeTrendOnly, highConfidenceOnly, selectedSectors, selectedConcepts, selectedIndices,
    peRange, roeRange, profitGrowthRange, rsiRange, mainFlowRange, instOwnershipRange,
    maGap5Range, maGap10Range, maGap20Range, volRatio5Range, volRatio20Range, rsi14Range, return1dRange,
    pbRange, totalMvRange, floatMvRange, listedDaysRange, return3dRange, advancedFiltersEnabled,
    appliedFilters
  ]);

  const applyCurrentFilters = () => {
    setAppliedFilters({
      minScore, limitUpDays, amountRange, turnoverRange, volumeTrendOnly, highConfidenceOnly, selectedSectors, selectedConcepts, selectedIndices,
      peRange, roeRange, profitGrowthRange, rsiRange, mainFlowRange, instOwnershipRange,
      maGap5Range, maGap10Range, maGap20Range, volRatio5Range, volRatio20Range, rsi14Range, return1dRange,
      pbRange, totalMvRange, floatMvRange, listedDaysRange, return3dRange, advancedFiltersEnabled,
      excludeSt, marketType
    });
    setCandidatePage(1);
    message.success('筛选条件已成功应用');
  };

  const resetFilters = () => {
    // 1. 重置 UI 状态（滑动条、输入框等）
    setMinScore(DEFAULT_RESEARCH_FILTERS.minScore);
    setLimitUpDays(DEFAULT_RESEARCH_FILTERS.limitUpDays);
    setAmountRange([...DEFAULT_RESEARCH_FILTERS.amountRange]);
    setTurnoverRange([...DEFAULT_RESEARCH_FILTERS.turnoverRange]);
    setVolumeTrendOnly(DEFAULT_RESEARCH_FILTERS.volumeTrendOnly);
    setHighConfidenceOnly(DEFAULT_RESEARCH_FILTERS.highConfidenceOnly);
    setSelectedSectors([...DEFAULT_RESEARCH_FILTERS.selectedSectors]);
    setSelectedConcepts([...DEFAULT_RESEARCH_FILTERS.selectedConcepts]);
    setSelectedIndices([...DEFAULT_RESEARCH_FILTERS.selectedIndices]);
    setPeRange([...DEFAULT_RESEARCH_FILTERS.peRange]);
    setRoeRange([...DEFAULT_RESEARCH_FILTERS.roeRange]);
    setProfitGrowthRange([...DEFAULT_RESEARCH_FILTERS.profitGrowthRange]);
    setPbRange([...DEFAULT_RESEARCH_FILTERS.pbRange]);
    setTotalMvRange([...DEFAULT_RESEARCH_FILTERS.totalMvRange]);
    setFloatMvRange([...DEFAULT_RESEARCH_FILTERS.floatMvRange]);
    setListedDaysRange([...DEFAULT_RESEARCH_FILTERS.listedDaysRange]);
    setReturn3dRange([...DEFAULT_RESEARCH_FILTERS.return3dRange]);
    setRsiRange([...DEFAULT_RESEARCH_FILTERS.rsiRange]);
    setMainFlowRange([...DEFAULT_RESEARCH_FILTERS.mainFlowRange]);
    setInstOwnershipRange([...DEFAULT_RESEARCH_FILTERS.instOwnershipRange]);
    setMaGap5Range([...DEFAULT_RESEARCH_FILTERS.maGap5Range]);
    setMaGap10Range([...DEFAULT_RESEARCH_FILTERS.maGap10Range]);
    setMaGap20Range([...DEFAULT_RESEARCH_FILTERS.maGap20Range]);
    setVolRatio5Range(DEFAULT_RESEARCH_FILTERS.volRatio5Range);
    setVolRatio20Range(DEFAULT_RESEARCH_FILTERS.volRatio20Range);
    setRsi14Range([...DEFAULT_RESEARCH_FILTERS.rsi14Range]);
    setReturn1dRange([...DEFAULT_RESEARCH_FILTERS.return1dRange]);
    setExcludeSt(DEFAULT_RESEARCH_FILTERS.excludeSt);
    setMarketType(DEFAULT_RESEARCH_FILTERS.marketType);
    setAdvancedFiltersEnabled(DEFAULT_RESEARCH_FILTERS.advancedFiltersEnabled);
    setActivePreset(null);

    // 2. 立即同步应用到已应用滤镜，确保“全量”即刻生效
    setAppliedFilters({
      ...DEFAULT_RESEARCH_FILTERS,
      amountRange: [...DEFAULT_RESEARCH_FILTERS.amountRange],
      turnoverRange: [...DEFAULT_RESEARCH_FILTERS.turnoverRange],
      selectedSectors: [...DEFAULT_RESEARCH_FILTERS.selectedSectors],
      selectedConcepts: [...DEFAULT_RESEARCH_FILTERS.selectedConcepts],
      selectedIndices: [...DEFAULT_RESEARCH_FILTERS.selectedIndices],
      peRange: [...DEFAULT_RESEARCH_FILTERS.peRange],
      roeRange: [...DEFAULT_RESEARCH_FILTERS.roeRange],
      profitGrowthRange: [...DEFAULT_RESEARCH_FILTERS.profitGrowthRange],
      pbRange: [...DEFAULT_RESEARCH_FILTERS.pbRange],
      totalMvRange: [...DEFAULT_RESEARCH_FILTERS.totalMvRange],
      floatMvRange: [...DEFAULT_RESEARCH_FILTERS.floatMvRange],
      listedDaysRange: [...DEFAULT_RESEARCH_FILTERS.listedDaysRange],
      return3dRange: [...DEFAULT_RESEARCH_FILTERS.return3dRange],
      rsiRange: [...DEFAULT_RESEARCH_FILTERS.rsiRange],
      mainFlowRange: [...DEFAULT_RESEARCH_FILTERS.mainFlowRange],
      instOwnershipRange: [...DEFAULT_RESEARCH_FILTERS.instOwnershipRange],
      maGap5Range: [...DEFAULT_RESEARCH_FILTERS.maGap5Range],
      maGap10Range: [...DEFAULT_RESEARCH_FILTERS.maGap10Range],
      maGap20Range: [...DEFAULT_RESEARCH_FILTERS.maGap20Range],
      rsi14Range: [...DEFAULT_RESEARCH_FILTERS.rsi14Range],
      return1dRange: [...DEFAULT_RESEARCH_FILTERS.return1dRange],
    });
    setCandidatePage(1);
  };

  const applyPreset = (presetName: string) => {
    const config = PRESET_FILTER_MAP[presetName];
    if (!config) return;
    
    // 1. 先彻底重置为全量宽松状态
    resetFilters();
    
    // 2. 应用特定模板参数
    let nextMinScore = -1.0;
    let nextLimitUpDays = 0;
    let nextRoeRange: [number, number] = [-1000, 1000];
    let nextTotalMvRange: [number, number] = [0, 1000000];
    let nextTurnoverRange: [number, number] = [0, 100];
    let nextMaGap20Range: [number, number] = [-100, 100];
    let nextRsiRange: [number, number] = [0, 100];

    if (config.minScore !== undefined) {
      setMinScore(config.minScore);
      nextMinScore = config.minScore;
    }
    if (config.limitUpDays !== undefined) {
      setLimitUpDays(config.limitUpDays);
      nextLimitUpDays = config.limitUpDays;
    }
    if (config.roeMin !== undefined) {
      const val: [number, number] = [config.roeMin, 100];
      setRoeRange(val);
      nextRoeRange = val;
    }
    if (config.totalMvMin !== undefined) {
      const val: [number, number] = [config.totalMvMin, 20000];
      setTotalMvRange(val);
      nextTotalMvRange = val;
    }
    if (config.turnoverMin !== undefined) {
      const val: [number, number] = [config.turnoverMin, 100];
      setTurnoverRange(val);
      nextTurnoverRange = val;
    }
    if (config.amountMin !== undefined) {
      setAmountRange([config.amountMin, 200]);
    }
    if (config.maGap20Max !== undefined) {
      const val: [number, number] = [-50, config.maGap20Max];
      setMaGap20Range(val);
      nextMaGap20Range = val;
    }
    if (config.rsiMax !== undefined) {
      const val: [number, number] = [0, config.rsiMax];
      setRsiRange(val);
      nextRsiRange = val;
    }
    
    // 3. 模板一键生效（直接更新 appliedFilters，无需用户二次点击）
    setAppliedFilters({
      minScore: nextMinScore,
      limitUpDays: nextLimitUpDays,
      roeRange: nextRoeRange,
      totalMvRange: nextTotalMvRange,
      turnoverRange: nextTurnoverRange,
      maGap20Range: nextMaGap20Range,
      rsiRange: nextRsiRange,
      excludeSt,
      marketType,
      amountRange,
      volumeTrendOnly,
      highConfidenceOnly,
      selectedSectors,
      selectedConcepts,
      selectedIndices,
      peRange,
      profitGrowthRange,
      mainFlowRange,
      instOwnershipRange,
      maGap5Range,
      maGap10Range,
      volRatio5Range,
      volRatio20Range,
      rsi14Range,
      return1dRange,
      pbRange,
      floatMvRange,
      listedDaysRange,
      return3dRange,
      advancedFiltersEnabled,
    });
    
    setActivePreset(presetName);
    message.success(`已应用模板：${presetName}`);
  };

  const [overview, setOverview] = React.useState<any>(null);
  const [refreshNonce, setRefreshNonce] = React.useState<number>(0);

  // 初始化加载模型
  React.useEffect(() => {
    let cancelled = false;
    const loadModels = async () => {
      try {
        const models = await researchService.getAvailableModels();
        if (cancelled) return;
        setAvailableModels(models);
        if (models.length > 0 && !selectedModelId) {
          setSelectedModelId(models[0].modelId);
        }
      } catch (error) {
        console.error('[ResearchPlatformPage] load models failed:', error);
      }
    };
    void loadModels();
    return () => { cancelled = true; };
  }, []);

  // 模型切换时加载批次
  React.useEffect(() => {
    if (!selectedModelId) {
      setAvailableRuns([]);
      setSelectedRunId('');
      return;
    }
    let cancelled = false;
    const loadRuns = async () => {
      try {
        const runs = await researchService.getInferenceRuns(selectedModelId);
        if (cancelled) return;
        setAvailableRuns(runs);
        if (runs.length > 0) {
          setSelectedRunId(runs[0].runId);
        } else {
          setSelectedRunId('');
        }
      } catch (error) {
        console.error('[ResearchPlatformPage] load runs failed:', error);
      }
    };
    void loadRuns();
    return () => { cancelled = true; };
  }, [selectedModelId, refreshNonce]);

  // 批次切换或同步刷新时加载原始数据
  React.useEffect(() => {
    if (!selectedRunId) {
      setCandidatePool([]);
      return;
    }
    let cancelled = false;
    const loadUniverse = async () => {
      setOverviewLoading(true);
      try {
        const result = await researchService.getResearchUniverse(selectedRunId, 1000);
        if (cancelled) return;
        setCandidatePool(
          (result.candidates || []).map((item: any) => ({
            ...item,
            score: safeNum(item?.score, 0),
            latestChange: safeNum(item?.latestChange, 0),
            nextDayReturn: item?.nextDayReturn !== null && item?.nextDayReturn !== undefined ? safeNum(item?.nextDayReturn, 0) : null,
            day3Return: item?.day3Return !== null && item?.day3Return !== undefined ? safeNum(item?.day3Return, 0) : null,
            consecutiveLimitUpDays: safeNum(item?.consecutiveLimitUpDays, 0),
            turnoverRate: safeNum(item?.turnoverRate, 0),
            amount: safeNum(item?.amount, 0),
            pe: safeNum(item?.pe, 0),
            roe: normalizeRoe(item?.roe),
            rsi: safeNum(item?.rsi, 0),
            profitGrowth: safeNum(item?.profitGrowth ?? item?.profit_growth, 0),
            mainFlow: safeNum(item?.mainFlow ?? item?.main_flow, 0),
            instOwnership: safeNum(item?.instOwnership ?? item?.inst_ownership, 0),
            ma5: safeNum(item?.ma5, 0),
            ma10: safeNum(item?.ma10, 0),
            pb: safeNum(item?.pb, 0),
            totalMv: normalizeYiValue(item?.totalMv ?? item?.total_mv ?? item?.marketCap),
            floatMv: normalizeYiValue(item?.floatMv ?? item?.float_mv),
            listedDays: safeNum(item?.listedDays ?? item?.listed_days, 0),
            return3d: safeNum(item?.return3d ?? item?.return_3d, 0),
            maGap10: safeNum(item?.maGap10 ?? item?.ma_gap_10, 0),
            maGap20: safeNum(item?.maGap20 ?? item?.ma_gap_20, 0),
            rsi14: safeNum(item?.rsi14 ?? item?.rsi_14 ?? item?.rsi, 0),
            volRatio20: safeNum(item?.volumeRatio20 ?? item?.volume_ratio_20, 0),
            conceptTags: Array.isArray(item?.conceptTags) ? item.conceptTags : [],
            indexTags: Array.isArray(item?.indexTags) ? item.indexTags : [],
            concept: item?.concept || '',
            isSt: Boolean(item?.isSt),
            isTradable: item?.isTradable !== undefined ? Boolean(item?.isTradable) : true,
            isHs300: Boolean(item?.isHs300),
            isCsi500: Boolean(item?.isCsi500),
            isCsi1000: Boolean(item?.isCsi1000),
            maGap5: safeNum(item?.maGap5 ?? item?.ma_gap_5, 0),
            volRatio5: safeNum(item?.volRatio5 ?? item?.volume_ratio_5, 0),
            confidence: item?.confidence || 'watch',
          }))
        );
        setOverview(result);
      } catch (e) {
        console.error('Load universe failed', e);
        if (!cancelled) setCandidatePool([]);
      } finally {
        if (!cancelled) setOverviewLoading(false);
      }
    };
    void loadUniverse();
    return () => { cancelled = true; };
  }, [selectedRunId, appliedFilters.minScore, appliedFilters.excludeSt, refreshNonce, loadRange]); // 只在关键变更时重刷

  const handleSyncCandidates = async () => {
    if (!selectedModelId) {
      message.warning('请先选择研究模型');
      return;
    }
    setSyncing(true);
    try {
      setRefreshNonce(refreshNonce + 1);
      message.success('候选池同步请求已发起');
    } finally {
      // 延迟一个 tick，避免按钮闪烁
      setTimeout(() => setSyncing(false), 300);
    }
  };

  // 加载自选数据（页面初始化时即加载，用于显示总数）
  React.useEffect(() => {
    let cancelled = false;
    const loadWatchlist = async () => {
      setWatchlistLoading(true);
      try {
        const result = await researchService.getWatchlist(100, 0);
        if (cancelled) return;
        setWatchlistData(result.items.map((item) => ({
          key: item.symbol,
          symbol: item.symbol,
          stockName: item.stockName,
          addedAt: item.addedAt,
          sourceRunId: item.sourceRunId,
          notes: item.notes,
          tags: item.tags,
        })));
        setWatchlistTotal(result.total || 0);
      } catch (error) {
        console.error('[ResearchPlatformPage] load watchlist failed:', error);
        if (!cancelled) {
          setWatchlistData([]);
          setWatchlistTotal(0);
        }
      } finally {
        if (!cancelled) setWatchlistLoading(false);
      }
    };
    void loadWatchlist();
    return () => { cancelled = true; };
  }, [refreshNonce]);

  // 加载研究池数据（页面初始化时即加载，用于显示总数）
  React.useEffect(() => {
    let cancelled = false;
    const loadPool = async () => {
      setPoolLoading(true);
      try {
        const result = await researchService.getResearchPool({ limit: 100, offset: 0 });
        if (cancelled) return;
        setPoolData(result.items.map((item) => ({
          key: item.symbol,
          symbol: item.symbol,
          stockName: item.stockName,
          addedAt: item.addedAt,
          sourceRunId: item.sourceRunId,
          modelId: item.modelId,
          fusionScore: item.fusionScore,
          thesisSummary: item.thesisSummary,
          status: item.status,
          notes: item.notes,
          tags: item.tags,
        })));
        setPoolTotal(result.total || 0);
      } catch (error) {
        console.error('[ResearchPlatformPage] load pool failed:', error);
        if (!cancelled) {
          setPoolData([]);
          setPoolTotal(0);
        }
      } finally {
        if (!cancelled) setPoolLoading(false);
      }
    };
    void loadPool();
    return () => { cancelled = true; };
  }, [refreshNonce]);

  // 富化自选特征数据
  React.useEffect(() => {
    if (!watchlistData.length) {
      setWatchlistFeatures({});
      return;
    }
    const symbols = watchlistData.map(item => item.symbol);
    researchService.getFeaturesBySymbols(symbols).then(features => {
      const map: Record<string, ResearchStockRow> = {};
      features.forEach(f => { map[f.code] = f; });
      setWatchlistFeatures(map);
    }).catch(() => setWatchlistFeatures({}));
  }, [watchlistData]);

  // 富化研究池特征数据
  React.useEffect(() => {
    if (!poolData.length) {
      setPoolFeatures({});
      return;
    }
    const symbols = poolData.map(item => item.symbol);
    researchService.getFeaturesBySymbols(symbols).then(features => {
      const map: Record<string, ResearchStockRow> = {};
      features.forEach(f => { map[f.code] = f; });
      setPoolFeatures(map);
    }).catch(() => setPoolFeatures({}));
  }, [poolData]);
    
  const handleAddToWatchlist = async (stock: ResearchStockRow) => {
    try {
      await researchService.addToWatchlist(stock.code, {
        runId: stock.runId,
        stockName: stock.name,
        featuresSnapshot: stock as unknown as Record<string, unknown>,
      });
      message.success(`已加入自选: ${stock.name}`);
      setRefreshNonce(refreshNonce + 1);
    } catch (error) {
      console.error('Add to watchlist failed', error);
      message.error('加入自选失败');
    }
  };

  const handleAddToResearchPool = async (stock: ResearchStockRow) => {
    try {
      await researchService.addToResearchPool(stock.code, {
        runId: stock.runId,
        stockName: stock.name,
        modelId: selectedModelId,
        fusionScore: stock.score,
        thesisSummary: stock.thesis,
        featuresSnapshot: stock as unknown as Record<string, unknown>,
      });
      message.success(`已加入研究池: ${stock.name}`);
      setRefreshNonce(refreshNonce + 1);
    } catch (error) {
      console.error('Add to research pool failed', error);
      message.error('加入研究池失败');
    }
  };

  const handleRemoveFromWatchlist = async (symbol: string, stockName: string | null) => {
    try {
      await researchService.removeFromWatchlist(symbol);
      message.success(`已从自选移除: ${stockName || symbol}`);
      setRefreshNonce(refreshNonce + 1);
    } catch (error) {
      console.error('Remove from watchlist failed', error);
      message.error('移出自选失败');
    }
  };

  const handleRemoveFromPool = async (symbol: string, stockName: string | null) => {
    try {
      await researchService.removeFromResearchPool(symbol);
      message.success(`已从研究池移除: ${stockName || symbol}`);
      setRefreshNonce(refreshNonce + 1);
    } catch (error) {
      console.error('Remove from pool failed', error);
      message.error('移出研究池失败');
    }
  };

  const filteredRows = React.useMemo(() => {
    // 1. 分离匹配和不匹配的股票
    const matches: ResearchStockRow[] = [];
    const nonMatches: ResearchStockRow[] = [];

    candidatePool.forEach((item) => {
      let isMatch = true;
      
      if (item.score < appliedFilters.minScore) isMatch = false;
      else if (item.consecutiveLimitUpDays < appliedFilters.limitUpDays) isMatch = false;
      else {
        const amountRange = appliedFilters.amountRange || [0, 10000];
        if (item.amount < amountRange[0] || item.amount > amountRange[1]) isMatch = false;
        
        const turnoverRange = appliedFilters.turnoverRange || [0, 100];
        if (item.turnoverRate < turnoverRange[0] || item.turnoverRate > turnoverRange[1]) isMatch = false;
        
        if (isMatch && appliedFilters.volumeTrendOnly && !item.volumeTrend3d) isMatch = false;

        // 高置信标的筛选
        if (isMatch && appliedFilters.highConfidenceOnly && item.confidence !== 'high') isMatch = false;

        // 行业筛选
        if (isMatch && (appliedFilters.selectedSectors?.length || 0) > 0) {
          if (!appliedFilters.selectedSectors.includes(item.sector)) isMatch = false;
        }

        // 概念筛选
        if (isMatch && (appliedFilters.selectedConcepts?.length || 0) > 0) {
          const itemConcepts = item.conceptTags || [];
          const hasMatch = appliedFilters.selectedConcepts.some(c => itemConcepts.includes(c));
          if (!hasMatch) isMatch = false;
        }
        if (isMatch && (appliedFilters.selectedIndices?.length || 0) > 0) {
          const itemIndices = item.indexTags || [];
          const hasIndexMatch = appliedFilters.selectedIndices.some((idx) => itemIndices.includes(idx));
          if (!hasIndexMatch) isMatch = false;
        }

        // 财务/估值筛选
        if (isMatch) {
          const roeRange = appliedFilters.roeRange || [-1000, 1000];
          if (item.roe < roeRange[0] || item.roe > roeRange[1]) isMatch = false;
        }
        
        if (isMatch) {
          const profitGrowthRange = appliedFilters.profitGrowthRange || [-500, 500];
          if (item.profitGrowth < profitGrowthRange[0] || item.profitGrowth > profitGrowthRange[1]) isMatch = false;
        }
        
        if (isMatch) {
          const pbRange = appliedFilters.pbRange || [0, 100];
          if ((item.pb || 0) < pbRange[0] || (item.pb || 0) > pbRange[1]) isMatch = false;
        }
        
        if (isMatch) {
          const totalMvRange = appliedFilters.totalMvRange || [0, 1000000];
          if ((item.totalMv || 0) < totalMvRange[0] || (item.totalMv || 0) > totalMvRange[1]) isMatch = false;
        }
        
        if (isMatch) {
          const floatMvRange = appliedFilters.floatMvRange || [0, 1000000];
          if ((item.floatMv || 0) < floatMvRange[0] || (item.floatMv || 0) > floatMvRange[1]) isMatch = false;
        }
        
        if (isMatch) {
          const listedDaysRange = appliedFilters.listedDaysRange || [0, 30000];
          if ((item.listedDays || 0) < listedDaysRange[0] || (item.listedDays || 0) > listedDaysRange[1]) isMatch = false;
        }
        
        if (isMatch) {
          const return3dRange = appliedFilters.return3dRange || [-100, 100];
          if ((item.return3d || 0) < return3dRange[0] || (item.return3d || 0) > return3dRange[1]) isMatch = false;
        }

        // 技术指标筛选
        if (isMatch) {
          const rsiRange = appliedFilters.rsiRange || [0, 100];
          if (item.rsi < rsiRange[0] || item.rsi > rsiRange[1]) isMatch = false;
        }
        
        if (isMatch) {
          const mainFlowRange = appliedFilters.mainFlowRange || [-100000, 100000];
          if (item.mainFlow < mainFlowRange[0] || item.mainFlow > mainFlowRange[1]) isMatch = false;
        }
        
        if (isMatch && appliedFilters.instOwnershipRange && item.instOwnership < appliedFilters.instOwnershipRange[0]) isMatch = false;

        // 特殊标签/状态：多维校验排除 ST / 退市股票
        if (isMatch && appliedFilters.excludeSt) {
          const upperName = item.name.toUpperCase();
          const isStByName = upperName.includes('ST') || upperName.includes('*ST');
          const isDelisting = item.name.includes('退') || upperName.includes('退市');
          if (
            item.isSt ||
            !item.isTradable ||
            item.riskFlags?.some(f => f.includes('ST')) ||
            isStByName ||
            isDelisting
          ) isMatch = false;
        }
        if (isMatch && appliedFilters.marketType && appliedFilters.marketType !== 'all' && appliedFilters.marketType !== '全市场') {
          const idxTags = item.indexTags || [];
          if (appliedFilters.marketType === 'hs300' && !idxTags.includes('沪深300')) isMatch = false;
          if (appliedFilters.marketType === 'zz500' && !idxTags.includes('中证500')) isMatch = false;
          if (appliedFilters.marketType === 'zz1000' && !idxTags.includes('中证1000')) isMatch = false;
        }

        if (isMatch && appliedFilters.volRatio5Range > 0) {
          const vr = item.volRatio5 || 0;
          if (vr < appliedFilters.volRatio5Range) isMatch = false;
        }

        if (isMatch) {
          const maGap5Range = appliedFilters.maGap5Range || [-100, 100];
          const gap = item.maGap5 || 0;
          if (gap < maGap5Range[0] || gap > maGap5Range[1]) isMatch = false;
        }
        
        if (isMatch) {
          const maGap20Range = appliedFilters.maGap20Range || [-100, 100];
          const gap20 = (item as any).maGap20 || item.maGap20 || 0;
          if (gap20 < maGap20Range[0] || gap20 > maGap20Range[1]) isMatch = false;
        }
        
        if (isMatch) {
          const peRange = appliedFilters.peRange || [0, 100000];
          // 强制执行 PE 过滤，不再依赖 advancedFiltersEnabled 开关
          if (item.pe < peRange[0] || item.pe > peRange[1]) isMatch = false;
        }
      }

      if (keyword && isMatch) {
        const k = keyword.toLowerCase();
        if (!item.name.toLowerCase().includes(k) && !item.code.toLowerCase().includes(k)) isMatch = false;
      }

      if (isMatch) {
        matches.push({ ...item, isMatched: true });
      } else {
        nonMatches.push({ ...item, isMatched: false });
      }
    });
    
    const sortFn = (left: ResearchStockRow, right: ResearchStockRow) => {
      if (sortKey === 'limitUp') return right.consecutiveLimitUpDays - left.consecutiveLimitUpDays || right.score - left.score;
      if (sortKey === 'turnover') return right.turnoverRate - left.turnoverRate || right.score - left.score;
      if (sortKey === 'amount') return right.amount - left.amount || right.score - left.score;
      return right.score - left.score || left.rank - right.rank;
    };

    // 2. 排序并返回符合条件的记录，并受限于 loadRange
    matches.sort(sortFn);

    // 只展示筛选后的结果，不再顺延不匹配的股票
    return matches.slice(0, loadRange).map((item, index) => ({ ...item, rank: index + 1 }));
  }, [appliedFilters, candidatePool, keyword, overview, sortKey, loadRange]);

  React.useEffect(() => {
    if (!filteredRows.length) {
      setSelectedStockKey(null);
      return;
    }
    if (!filteredRows.some((item) => item.key === selectedStockKey)) {
      setSelectedStockKey(filteredRows[0].key);
    }
  }, [filteredRows, selectedStockKey]);

  const selectedStock = React.useMemo(
    () => filteredRows.find((item) => item.key === selectedStockKey) || null,
    [filteredRows, selectedStockKey]
  );

  const radarMetrics = React.useMemo(() => {
    if (!selectedStock) return null;

    const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(max, value));
    const modelScore = clamp(safeNum(selectedStock.score, 0) * 100, 0, 100);
    const valuationScore = clamp(100 - safeNum(selectedStock.pe, 0), 0, 100);
    const profitabilityScore = clamp(Math.max(0, safeNum(selectedStock.roe, 0)) / 50 * 100, 0, 100);
    const momentumScore = clamp(safeNum(selectedStock.rsi, 0), 0, 100);
    const activityScore = clamp(safeNum(selectedStock.turnoverRate, 0) / 30 * 100, 0, 100);

    return {
      indicator: [
        { name: '模型评分', max: 100 },
        { name: '估值水平', max: 100 },
        { name: '盈利能力', max: 100 },
        { name: '动量强度', max: 100 },
        { name: '活跃度', max: 100 },
      ],
      value: [modelScore, valuationScore, profitabilityScore, momentumScore, activityScore],
    };
  }, [selectedStock]);


  // 加载 K 线数据
  React.useEffect(() => {
    if (!detailModalOpen || !selectedStock) {
      setKlineData([]);
      return;
    }
    let cancelled = false;
    const loadKline = async () => {
      setKlineLoading(true);
      try {
        const data = await researchService.getKlineData(normalizeSymbol(selectedStock.code), 60);
        if (cancelled) return;
        setKlineData(data);
      } catch (error) {
        console.error('[ResearchPlatformPage] load kline failed:', error);
        if (!cancelled) setKlineData([]);
      } finally {
        if (!cancelled) setKlineLoading(false);
      }
    };
    void loadKline();
    return () => { cancelled = true; };
  }, [detailModalOpen, selectedStock?.code]);

  // K 线图表配置
  const klineOption = React.useMemo(() => {
    if (!klineData.length) return null;
    const dates = klineData.map((d) => d.date);
    const ohlc = klineData.map((d) => [d.open, d.close, d.low, d.high]);
    const volumes = klineData.map((d) => d.volume);

    const predictionMatch = selectedRunId.match(/run_(\d{4})(\d{2})(\d{2})/);
    const predictionDate = predictionMatch ? `${predictionMatch[1]}-${predictionMatch[2]}-${predictionMatch[3]}` : null;
    const predictionIdx = predictionDate ? dates.indexOf(predictionDate) : -1;

    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        borderWidth: 1,
        borderColor: '#ccc',
        padding: 10,
        textStyle: { color: '#000', fontSize: 11 },
        formatter: (params: any[]) => {
          if (!params?.length) return '';
          const idx = params[0].dataIndex;
          const d = klineData[idx];
          if (!d) return '';
          return `
            <div style="font-size: 11px;">
              <div style="font-weight: bold; margin-bottom: 4px;">${d.date}</div>
              ${idx === predictionIdx ? '<div style="color: #f59e0b; font-weight: bold; margin-bottom: 4px;">[ 预测基准日 ]</div>' : ''}
              <div>开盘: ${d.open.toFixed(2)}</div>
              <div>收盘: ${d.close.toFixed(2)}</div>
              <div>最高: ${d.high.toFixed(2)}</div>
              <div>最低: ${d.low.toFixed(2)}</div>
              <div>成交量: ${(d.volume / 10000).toFixed(2)}万</div>
            </div>
          `;
        },
      },
      grid: [
        { left: '8%', right: '4%', top: '8%', height: '55%' },
        { left: '8%', right: '4%', top: '72%', height: '18%' },
      ],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
        { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: { onZero: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, min: 'dataMin', max: 'dataMax' },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 50, end: 100 }],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: ohlc,
          itemStyle: { color: '#ef4444', color0: '#22c55e', borderColor: '#ef4444', borderColor0: '#22c55e' },
          markPoint: predictionIdx !== -1 ? {
            symbol: 'path://M0,0 L10,0 L5,10 Z',
            symbolSize: [12, 12],
            symbolOffset: [0, -10],
            itemStyle: { color: '#f59e0b' },
            data: [{
              coord: [predictionIdx, ohlc[predictionIdx][3]],
              value: '预测',
              label: { show: false }
            }]
          } : undefined
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
          itemStyle: {
            color: (params: any) => {
              const d = klineData[params.dataIndex];
              return d?.close >= d?.open ? '#ef4444' : '#22c55e';
            },
          },
        },
      ],
    };
  }, [klineData, selectedRunId]);

  const sectorBreakdown = React.useMemo(() => {
    const counter = new Map<string, number>();
    filteredRows.forEach((item) => {
      counter.set(item.sector, (counter.get(item.sector) || 0) + 1);
    });
    return Array.from(counter.entries())
      .map(([name, count]) => ({ name, count }))
      .sort((left, right) => right.count - left.count)
      .slice(0, 5);
  }, [filteredRows]);

  // 从候选池提取可用的行业选项
  const availableSectorOptions = React.useMemo(() => {
    const counter = new Map<string, number>();
    candidatePool.forEach((item) => {
      if (item.sector) {
        counter.set(item.sector, (counter.get(item.sector) || 0) + 1);
      }
    });
    const result = Array.from(counter.entries())
      .map(([name, count]) => ({ value: name, label: `${name} (${count})` }))
      .sort((left, right) => left.label.localeCompare(right.label));
    console.log('[Research] Available sectors:', result);
    return result;
  }, [candidatePool]);

  // 从候选池提取可用的概念选项
  const availableConceptOptions = React.useMemo(() => {
    if (overview?.filters?.concepts?.length) {
      return overview.filters.concepts.map((name: string) => ({ value: name, label: name }));
    }
    const counter = new Map<string, number>();
    candidatePool.forEach((item) => {
      (item.conceptTags || []).forEach((tag: string) => {
        counter.set(tag, (counter.get(tag) || 0) + 1);
      });
    });
    const result = Array.from(counter.entries())
      .map(([name, count]) => ({ value: name, label: `${name} (${count})` }))
      .sort((left, right) => left.label.localeCompare(right.label))
      .slice(0, 50); // 限制选项数量
    console.log('[Research] Available concepts:', result);
    return result;
  }, [candidatePool, overview]);

  const availableIndexOptions = React.useMemo(() => {
    // 优先从后端 summary 获取精准全局统计
    const summary = overview?.summary;
    const items = [
      { name: '全市场', count: summary?.totalMarket || 0 },
      { name: '沪深300', count: summary?.hs300 || 0 },
      { name: '中证1000', count: summary?.zz1000 || 0 },
      { name: '两融标的', count: summary?.margin || 0 },
      { name: '创业板指数', count: summary?.chinext || 0 },
    ];

    // 如果 summary 里没数据，回退到本地统计
    const counter = new Map<string, number>();
    candidatePool.forEach((item) => {
      (item.indexTags || []).forEach((tag: string) => {
        counter.set(tag, (counter.get(tag) || 0) + 1);
      });
    });

    return items.map(idx => {
      const globalCount = idx.count;
      const localCount = counter.get(idx.name) || 0;
      // 如果全局有数显示全局，否则显示本地
      const displayCount = globalCount > 0 ? globalCount : localCount;
      return { value: idx.name, label: `${idx.name} (${displayCount})` };
    }).filter(opt => opt.label.indexOf('(0)') === -1);
  }, [candidatePool, overview]);

  const avgScore = React.useMemo(() => {
    if (!filteredRows.length) return '0.000';
    const sanitizedScores = filteredRows
      .map((item) => (Number.isFinite(item.score) ? Math.max(item.score, 0) : 0));
    const total = sanitizedScores.reduce((sum, score) => sum + score, 0);
    return (total / sanitizedScores.length).toFixed(3);
  }, [filteredRows]);

  const candidateTotal = overview?.pagination?.total ?? overview?.summary?.total ?? candidatePool.length;

  const selectedStockRiskBlocks = React.useMemo(() => {
    if (!selectedStock) return [];
    const blocks = [...(selectedStock.riskFlags || [])];
    if (!selectedStock.volumeTrend5d) blocks.push('近 5 日量能未持续放大');
    if (selectedStock.turnoverRate > 20) blocks.push('换手率偏高，追涨风险上升');
    if (selectedStock.latestChange > 5) blocks.push('短线涨幅较大，注意日内波动');
    return Array.from(new Set(blocks));
  }, [selectedStock]);

  const selectedStockMatchedConditions = React.useMemo(() => {
    if (!selectedStock) return [];
    const matches = [
      `模型分数 ≥ ${appliedFilters.minScore.toFixed(2)}`,
      `连板天数 ≥ ${appliedFilters.limitUpDays}`,
      `成交额 ${appliedFilters.amountRange[0]} - ${appliedFilters.amountRange[1]} 亿`,
      `换手率 ${appliedFilters.turnoverRange[0]} - ${appliedFilters.turnoverRange[1]}%`,
    ];
    if (appliedFilters.volumeTrendOnly) matches.push('近 3 日成交量递增');
    if (appliedFilters.highConfidenceOnly) matches.push('仅保留高置信标的');
    if (appliedFilters.selectedSectors.length) matches.push(`行业：${appliedFilters.selectedSectors.length} 个选中`);
    if (appliedFilters.selectedConcepts.length) matches.push(`概念：${appliedFilters.selectedConcepts.length} 个选中`);
    if (appliedFilters.selectedIndices.length) matches.push(`指数：${appliedFilters.selectedIndices.length} 个选中`);
    if (appliedFilters.advancedFiltersEnabled && appliedFilters.peRange[1] < 100) matches.push(`PE < ${appliedFilters.peRange[1]}`);
    if (appliedFilters.roeRange[0] > 0) matches.push(`ROE > ${appliedFilters.roeRange[0]}%`);
    return matches;
  }, [appliedFilters, selectedStock]);

  const columns = React.useMemo<ColumnsType<ResearchStockRow>>(
    () => [
      {
        title: <span className="whitespace-nowrap">排名</span>,
        dataIndex: 'rank',
        width: 60,
        align: 'center',
        render: (value: number) => <span className="whitespace-nowrap font-bold text-slate-700">{value}</span>,
      },
      {
        title: <span className="whitespace-nowrap">股票</span>,
        key: 'stock',
        width: 132,
        align: 'center',
        render: (_, record) => (
          <div className="text-center whitespace-nowrap">
            <div className="font-bold text-slate-900 whitespace-nowrap">{record.name}</div>
            <div className="text-xs text-slate-500 whitespace-nowrap">{record.code}</div>
          </div>
        ),
      },
      {
        title: <span className="whitespace-nowrap">模型分数</span>,
        dataIndex: 'score',
        width: 98,
        align: 'center',
        render: (value: number) => <span className="font-black text-blue-400 whitespace-nowrap">{value.toFixed(3)}</span>,
      },
      {
        title: <span className="whitespace-nowrap">涨跌幅</span>,
        dataIndex: 'latestChange',
        width: 96,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 0 ? 'font-semibold text-rose-500' : 'font-semibold text-emerald-500'}`}>
            {value >= 0 ? '+' : ''}{value.toFixed(2)}%
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">次日收益</span>,
        dataIndex: 'nextDayReturn',
        width: 96,
        align: 'center',
        render: (value: number | null | undefined) => {
          if (value === null || value === undefined) {
            return <span className="text-slate-300 font-medium">-</span>;
          }
          return (
            <span className={`whitespace-nowrap ${value >= 0 ? 'font-semibold text-rose-500' : 'font-semibold text-emerald-500'}`}>
              {value >= 0 ? '+' : ''}{value.toFixed(2)}%
            </span>
          );
        },
      },
      {
        title: <span className="whitespace-nowrap">3日收益</span>,
        dataIndex: 'day3Return',
        width: 96,
        align: 'center',
        render: (value: number | null | undefined) => {
          if (value === null || value === undefined) {
            return <span className="text-slate-300 font-medium">-</span>;
          }
          return (
            <span className={`whitespace-nowrap ${value >= 0 ? 'font-semibold text-rose-500' : 'font-semibold text-emerald-500'}`}>
              {value >= 0 ? '+' : ''}{value.toFixed(2)}%
            </span>
          );
        },
      },
      {
        title: <span className="whitespace-nowrap">连板</span>,
        dataIndex: 'consecutiveLimitUpDays',
        width: 54,
        align: 'center',
      },
      {
        title: <span className="whitespace-nowrap">3日量能</span>,
        dataIndex: 'volumeTrend3d',
        width: 92,
        align: 'center',
        render: (value: boolean) => <Tag color={value ? 'blue' : 'default'} className="rounded-lg border-none font-bold">{value ? '递增' : '平缓'}</Tag>,
      },
      {
        title: <span className="whitespace-nowrap">换手率</span>,
        dataIndex: 'turnoverRate',
        width: 90,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value.toFixed(2)}%</span>,
      },
      {
        title: <span className="whitespace-nowrap">成交额</span>,
        dataIndex: 'amount',
        width: 108,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value.toFixed(2)}亿</span>,
      },
      {
        title: <span className="whitespace-nowrap">PE(TTM)</span>,
        dataIndex: 'pe',
        width: 92,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value >= 0 ? value.toFixed(1) : '-'}</span>,
      },
      {
        title: <span className="whitespace-nowrap">ROE(%)</span>,
        dataIndex: 'roe',
        width: 92,
        align: 'center',
        render: (value: number) => (
          <span className="text-rose-500 font-bold whitespace-nowrap">
            {value > -100 && value < 100 ? `${value.toFixed(1)}%` : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">5日乖离</span>,
        dataIndex: 'maGap5',
        width: 92,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 0 ? 'text-indigo-500 font-medium' : 'text-slate-400'}`}>
            {value ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">RSI</span>,
        dataIndex: 'rsi',
        width: 76,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 70 ? 'text-rose-500 font-bold' : value <= 30 ? 'text-emerald-500' : 'text-slate-600'}`}>
            {value ? value.toFixed(1) : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">ATR</span>,
        dataIndex: 'atr',
        width: 80,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value.toFixed(3)}</span>,
      },
      {
        title: <span className="whitespace-nowrap">MACD</span>,
        dataIndex: 'macdHist',
        width: 80,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
            {value.toFixed(3)}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">20日乖离</span>,
        dataIndex: 'maGap20',
        width: 92,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 0 ? 'text-indigo-500 font-medium' : 'text-slate-400'}`}>
            {value ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">行业</span>,
        dataIndex: 'sector',
        width: 100,
        align: 'center',
        ellipsis: true,
      },
      {
        title: <span className="whitespace-nowrap">指数/状态</span>,
        key: 'status',
        width: 120,
        align: 'center',
        render: (_, record) => (
          <div className="flex flex-wrap gap-1 justify-center">
            {record.isSt && <Tag color="error" className="m-0 text-[10px] scale-90">ST</Tag>}
            {record.isHs300 && <Tag color="blue" className="m-0 text-[10px] scale-90">HS300</Tag>}
            {record.isCsi500 && <Tag color="cyan" className="m-0 text-[10px] scale-90">ZZ500</Tag>}
            {record.isCsi1000 && <Tag color="purple" className="m-0 text-[10px] scale-90">ZZ1000</Tag>}
          </div>
        ),
      },
    ],
    []
  );

  const watchlistColumns = React.useMemo<ColumnsType<ResearchStockRow>>(
    () => [
      {
        title: <span className="whitespace-nowrap">排名</span>,
        dataIndex: 'rank',
        width: 60,
        align: 'center',
        render: (value: number) => <span className="whitespace-nowrap font-bold text-slate-700">{value}</span>,
      },
      {
        title: <span className="whitespace-nowrap">股票</span>,
        key: 'stock',
        width: 132,
        align: 'center',
        render: (_, record) => (
          <div className="text-center whitespace-nowrap">
            <div className="font-bold text-slate-900 whitespace-nowrap">{record.name}</div>
            <div className="text-xs text-slate-500 whitespace-nowrap">{record.code}</div>
          </div>
        ),
      },
      {
        title: <span className="whitespace-nowrap">模型分数</span>,
        dataIndex: 'score',
        width: 98,
        align: 'center',
        render: (value: number) => <span className="font-black text-blue-400 whitespace-nowrap">{value.toFixed(3)}</span>,
      },
      {
        title: <span className="whitespace-nowrap">涨跌幅</span>,
        dataIndex: 'latestChange',
        width: 96,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 0 ? 'font-semibold text-rose-500' : 'font-semibold text-emerald-500'}`}>
            {value >= 0 ? '+' : ''}{value.toFixed(2)}%
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">换手率</span>,
        dataIndex: 'turnoverRate',
        width: 90,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value.toFixed(2)}%</span>,
      },
      {
        title: <span className="whitespace-nowrap">成交额</span>,
        dataIndex: 'amount',
        width: 108,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value.toFixed(2)}亿</span>,
      },
      {
        title: <span className="whitespace-nowrap">PE(TTM)</span>,
        dataIndex: 'pe',
        width: 92,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value >= 0 ? value.toFixed(1) : '-'}</span>,
      },
      {
        title: <span className="whitespace-nowrap">ROE(%)</span>,
        dataIndex: 'roe',
        width: 92,
        align: 'center',
        render: (value: number) => (
          <span className="text-rose-500 font-bold whitespace-nowrap">
            {value > -100 && value < 100 ? `${value.toFixed(1)}%` : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">RSI</span>,
        dataIndex: 'rsi',
        width: 76,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 70 ? 'text-rose-500 font-bold' : value <= 30 ? 'text-emerald-500' : 'text-slate-600'}`}>
            {value ? value.toFixed(1) : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">行业</span>,
        dataIndex: 'sector',
        width: 100,
        align: 'center',
        ellipsis: true,
      },
      {
        title: <span className="whitespace-nowrap">指数/状态</span>,
        key: 'status',
        width: 120,
        align: 'center',
        render: (_, record) => (
          <div className="flex flex-wrap gap-1 justify-center">
            {record.isSt && <Tag color="error" className="m-0 text-[10px] scale-90">ST</Tag>}
            {record.isHs300 && <Tag color="blue" className="m-0 text-[10px] scale-90">HS300</Tag>}
            {record.isCsi500 && <Tag color="cyan" className="m-0 text-[10px] scale-90">ZZ500</Tag>}
            {record.isCsi1000 && <Tag color="purple" className="m-0 text-[10px] scale-90">ZZ1000</Tag>}
          </div>
        ),
      },
      {
        title: <span className="whitespace-nowrap">操作</span>,
        key: 'actions',
        width: 80,
        fixed: 'right',
        align: 'center',
        render: (_, record) => (
          <div className="flex items-center justify-center" onClick={e => e.stopPropagation()}>
            <Button
              size="small"
              type="text"
              danger
              icon={<span className="text-[10px]">移除</span>}
              onClick={() => handleRemoveFromWatchlist(record.code, record.name)}
              title="从自选移除"
            />
          </div>
        ),
      },
    ],
    [watchlistFeatures]
  );

  const poolColumns = React.useMemo<ColumnsType<ResearchStockRow>>(
    () => [
      {
        title: <span className="whitespace-nowrap">排名</span>,
        dataIndex: 'rank',
        width: 60,
        align: 'center',
        render: (value: number) => <span className="whitespace-nowrap font-bold text-slate-700">{value}</span>,
      },
      {
        title: <span className="whitespace-nowrap">股票</span>,
        key: 'stock',
        width: 132,
        align: 'center',
        render: (_, record) => (
          <div className="text-center whitespace-nowrap">
            <div className="font-bold text-slate-900 whitespace-nowrap">{record.name}</div>
            <div className="text-xs text-slate-500 whitespace-nowrap">{record.code}</div>
          </div>
        ),
      },
      {
        title: <span className="whitespace-nowrap">模型分数</span>,
        dataIndex: 'score',
        width: 98,
        align: 'center',
        render: (value: number) => <span className="font-black text-blue-400 whitespace-nowrap">{value.toFixed(3)}</span>,
      },
      {
        title: <span className="whitespace-nowrap">涨跌幅</span>,
        dataIndex: 'latestChange',
        width: 96,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 0 ? 'font-semibold text-rose-500' : 'font-semibold text-emerald-500'}`}>
            {value >= 0 ? '+' : ''}{value.toFixed(2)}%
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">换手率</span>,
        dataIndex: 'turnoverRate',
        width: 90,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value.toFixed(2)}%</span>,
      },
      {
        title: <span className="whitespace-nowrap">成交额</span>,
        dataIndex: 'amount',
        width: 108,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value.toFixed(2)}亿</span>,
      },
      {
        title: <span className="whitespace-nowrap">PE(TTM)</span>,
        dataIndex: 'pe',
        width: 92,
        align: 'center',
        render: (value: number) => <span className="text-slate-600 font-medium whitespace-nowrap">{value >= 0 ? value.toFixed(1) : '-'}</span>,
      },
      {
        title: <span className="whitespace-nowrap">ROE(%)</span>,
        dataIndex: 'roe',
        width: 92,
        align: 'center',
        render: (value: number) => (
          <span className="text-rose-500 font-bold whitespace-nowrap">
            {value > -100 && value < 100 ? `${value.toFixed(1)}%` : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">RSI</span>,
        dataIndex: 'rsi',
        width: 76,
        align: 'center',
        render: (value: number) => (
          <span className={`whitespace-nowrap ${value >= 70 ? 'text-rose-500 font-bold' : value <= 30 ? 'text-emerald-500' : 'text-slate-600'}`}>
            {value ? value.toFixed(1) : '-'}
          </span>
        ),
      },
      {
        title: <span className="whitespace-nowrap">行业</span>,
        dataIndex: 'sector',
        width: 100,
        align: 'center',
        ellipsis: true,
      },
      {
        title: <span className="whitespace-nowrap">指数/状态</span>,
        key: 'status',
        width: 120,
        align: 'center',
        render: (_, record) => (
          <div className="flex flex-wrap gap-1 justify-center">
            {record.isSt && <Tag color="error" className="m-0 text-[10px] scale-90">ST</Tag>}
            {record.isHs300 && <Tag color="blue" className="m-0 text-[10px] scale-90">HS300</Tag>}
            {record.isCsi500 && <Tag color="cyan" className="m-0 text-[10px] scale-90">ZZ500</Tag>}
            {record.isCsi1000 && <Tag color="purple" className="m-0 text-[10px] scale-90">ZZ1000</Tag>}
          </div>
        ),
      },
      {
        title: <span className="whitespace-nowrap">操作</span>,
        key: 'actions',
        width: 80,
        fixed: 'right',
        align: 'center',
        render: (_, record) => (
          <div className="flex items-center justify-center" onClick={e => e.stopPropagation()}>
            <Button
              size="small"
              type="text"
              danger
              icon={<span className="text-[10px]">移除</span>}
              onClick={() => handleRemoveFromPool(record.code, record.name)}
              title="从研究池移除"
            />
          </div>
        ),
      },
    ],
    [poolFeatures]
  );

  // 导出 CSV
  const handleExportCSV = () => {
    if (filteredRows.length === 0) {
      message.warning('暂无数据可导出');
      return;
    }

    const headers = ['排名', '股票代码', '股票名称', '模型分数', '涨跌幅', '次日收益', '3日收益', '连板天数', '换手率', '成交额(亿)', '行业', 'PE(TTM)', 'ROE(%)', 'RSI', '5日乖离', '5日量比'];
    const rows = filteredRows.map((item) => [
      item.rank,
      item.code,
      item.name,
      item.score.toFixed(3),
      `${item.latestChange >= 0 ? '+' : ''}${item.latestChange.toFixed(2)}%`,
      item.nextDayReturn !== null ? `${item.nextDayReturn >= 0 ? '+' : ''}${item.nextDayReturn.toFixed(2)}%` : '-',
      item.day3Return !== null ? `${item.day3Return >= 0 ? '+' : ''}${item.day3Return.toFixed(2)}%` : '-',
      item.consecutiveLimitUpDays,
      item.turnoverRate.toFixed(2),
      item.amount.toFixed(2),
      item.sector,
      item.pe.toFixed(1),
      item.roe.toFixed(1),
      item.rsi.toFixed(1),
      (item.maGap5 || 0).toFixed(2),
      (item.volRatio5 || 0).toFixed(2),
    ]);

    const csvContent = [headers.join(','), ...rows.map((row) => row.join(','))].join('\n');
    const BOM = '﻿';
    const blob = new Blob([BOM + csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    const timestamp = new Date().toISOString().slice(0, 10);
    link.download = `投研候选池_${selectedModelId}_${timestamp}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    message.success(`已导出 ${filteredRows.length} 条数据`);
  };

  return (
    <>
      <div className={`${PAGE_LAYOUT.outerClass} research-platform-page`}>
      <div className={`${PAGE_LAYOUT.frameClass} overflow-y-auto custom-scrollbar`}>
          <header className={`${PAGE_LAYOUT.headerClass}`} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-2xl bg-gradient-to-br from-blue-500 via-indigo-500 to-violet-400 text-white shadow-lg shadow-blue-900/20">
                <Microscope className="h-5 w-5" />
              </div>
              <div>
                <h1 className="text-lg font-bold tracking-tight text-slate-900">投研平台</h1>
                <p className="text-[10px] font-bold uppercase tracking-[0.24em] text-slate-500">Professional Quant Workspace</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                icon={<RefreshCw className="h-4 w-4" />}
                className={BUTTON_STYLES.headerRefresh}
                loading={overviewLoading || syncing}
                onClick={handleSyncCandidates}
              >
                刷新数据
              </Button>
              <Button
                icon={<Download className="h-4 w-4" />}
                className={BUTTON_STYLES.headerSave}
                onClick={handleExportCSV}
                disabled={filteredRows.length === 0}
              >
                导出结果
              </Button>
            </div>
          </header>

          <div className="flex-1 flex flex-col">
            <div className={`${PAGE_LAYOUT.contentOuterClass}`}>
              <div className="grid gap-4 2xl:grid-cols-[320px_minmax(0,1fr)] xl:grid-cols-[300px_minmax(0,1fr)]">
                {/* 左侧侧边栏 - 吸顶且固定高度 */}
                <div className="flex flex-col gap-4 sticky top-4 h-[calc(100vh-120px)] z-30">
                  <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm flex-shrink-0">
                    <div className="flex items-center gap-2 mb-4 text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">
                      <LibraryBig className="h-4 w-4" />
                      候选池入口
                    </div>
                    
                    <div className="space-y-4">
                      <div>
                        <div className="mb-2 text-xs font-semibold text-slate-500">研究模型</div>
                        <Select
                          className={`w-full ${FIELD_STYLES.select} mb-3`}
                          value={selectedModelId}
                          onChange={setSelectedModelId}
                          placeholder="请选择投研模型"
                          options={availableModels.map((item) => ({
                            value: item.modelId,
                            label: item.name,
                          }))}
                        />
                      </div>

                      <div>
                        <div className="mb-2 text-xs font-semibold text-slate-500">推理批次</div>
                        <Select
                          className={`w-full ${FIELD_STYLES.select}`}
                          value={selectedRunId}
                          onChange={setSelectedRunId}
                          placeholder="选择推理批次"
                          options={availableRuns.map((item) => ({
                            value: item.runId,
                            label: `${item.inferenceDate} · ${item.runId.slice(-8)}`,
                          }))}
                        />
                      </div>

                      <div>
                        <div className="mb-2 text-xs font-semibold text-slate-500">默认加载范围</div>
                        <div className="flex gap-2 mb-4">
                          {[50, 100, 200, 500, 1000].map((range) => (
                            <button
                              key={range}
                              onClick={() => setLoadRange(range)}
                              className={`flex-1 px-3 py-2 rounded-xl text-xs font-bold transition-all duration-200 border ${
                                loadRange === range
                                  ? 'bg-blue-600 text-white border-blue-600 shadow-md shadow-blue-500/20'
                                  : 'bg-white text-slate-500 border-slate-200 hover:border-blue-300 hover:text-blue-500'
                              }`}
                            >
                              {range}
                            </button>
                          ))}
                        </div>
                      </div>

                      <div>
                        <div className="mb-2 text-xs font-semibold text-slate-500">快速模板</div>
                        <div className="grid grid-cols-3 gap-2">
                          {Object.keys(PRESET_FILTER_MAP).map((item) => (
                            <Tag
                              key={item}
                              className={`preset-tag cursor-pointer rounded-full px-2.5 py-1 text-[10px] text-center font-bold transition-all duration-300 border ${
                                activePreset === item ? TEMPLATE_BUTTON_STYLES.active : TEMPLATE_BUTTON_STYLES.idle
                              }`}
                              onClick={() => applyPreset(item)}
                            >
                              {item}
                            </Tag>
                          ))}
                          <Tag
                            className={`preset-tag cursor-pointer rounded-full px-2.5 py-1 text-[10px] text-center font-bold transition-all duration-300 border ${
                              !activePreset ? 'bg-blue-600 text-white border-blue-600' : 'bg-slate-50 text-slate-500 border-slate-200'
                            }`}
                            onClick={resetFilters}
                          >
                            全量候选
                          </Tag>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="flex-1 min-h-0 flex flex-col rounded-3xl border border-slate-200 bg-white shadow-sm relative overflow-hidden">
                    <div className="flex-1 overflow-y-auto custom-scrollbar p-5 pb-32">
                      <div className="flex items-center gap-2 mb-4 text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">
                        <Filter className="h-4 w-4" />
                        量化研究条件
                      </div>

                      <Collapse
                      className={FIELD_STYLES.collapse}
                      ghost
                      activeKey={activeFilterSections}
                      onChange={(keys) => setActiveFilterSections(Array.isArray(keys) ? (keys as FilterSectionKey[]) : ([keys] as FilterSectionKey[]))}
                      items={[
                        {
                          key: 'common',
                          label: <span className="text-xs font-bold text-slate-700 uppercase tracking-wide">核心指标与范围</span>,
                          children: (
                            <div className="space-y-4 pt-1">
                              <div className="flex items-center justify-between rounded-xl border border-slate-100 bg-slate-50/50 px-3 py-2">
                                <span className="text-[11px] font-bold text-slate-500">剔除 ST / 退市</span>
                                <Switch 
                                  size="small" 
                                  checked={excludeSt} 
                                  onChange={(val) => {
                                    setExcludeSt(val);
                                    setAppliedFilters({ ...appliedFilters, excludeSt: val });
                                  }} 
                                />
                              </div>
                              <RangeInput
                                label="模型分数 (≥)"
                                value={minScore}
                                onChange={setMinScore}
                                placeholder="最低分"
                                step={0.01}
                                isSingle
                              />
                              <RangeInput
                                label="连板天数 (≥)"
                                value={limitUpDays}
                                onChange={setLimitUpDays}
                                placeholder="连板数"
                                suffix="天"
                                isSingle
                              />
                            </div>
                          )
                        },
                        {
                          key: 'market',
                          label: <span className="text-xs font-bold text-slate-700 uppercase tracking-wide">行情与流动性</span>,
                          children: (
                            <div className="space-y-4 pt-1">
                              <RangeInput
                                label="成交额 (亿)"
                                value={amountRange}
                                onChange={(v) => setAmountRange(v)}
                                placeholder={["最小额", "最大额"]}
                                suffix="亿"
                              />
                              <RangeInput
                                label="换手率 (%)"
                                value={turnoverRange}
                                onChange={(v) => setTurnoverRange(v)}
                                placeholder={["最小换手", "最大换手"]}
                                suffix="%"
                                step={0.1}
                              />
                              <RangeInput
                                label="总市值 (亿)"
                                value={totalMvRange}
                                onChange={(v) => setTotalMvRange(v)}
                                placeholder={["最小市值", "最大市值"]}
                                suffix="亿"
                              />
                            </div>
                          )
                        },
                        {
                          key: 'technical',
                          label: <span className="text-xs font-bold text-slate-700 uppercase tracking-wide">技术面过滤</span>,
                          children: (
                            <div className="space-y-4 pt-1">
                              <RangeInput
                                label="5日乖离率 (%)"
                                value={maGap5Range}
                                onChange={(v) => setMaGap5Range(v)}
                                placeholder={["Min", "Max"]}
                                suffix="%"
                                step={0.1}
                              />
                              <RangeInput
                                label="20日乖离率 (%)"
                                value={maGap20Range}
                                onChange={(v) => setMaGap20Range(v)}
                                placeholder={["Min", "Max"]}
                                suffix="%"
                                step={0.1}
                              />
                              <RangeInput
                                label="5日量比 (≥)"
                                value={volRatio5Range}
                                onChange={setVolRatio5Range}
                                placeholder="量比值"
                                step={0.5}
                                isSingle
                              />
                              <RangeInput
                                label="RSI (6日)"
                                value={rsiRange}
                                onChange={(v) => setRsiRange(v)}
                                placeholder={["超卖", "超买"]}
                                step={1}
                              />
                            </div>
                          )
                        },
                        {
                          key: 'fundamental',
                          label: <span className="text-xs font-bold text-slate-700 uppercase tracking-wide">财务估值 (基本面)</span>,
                          children: (
                            <div className="space-y-4 pt-1">
                              <RangeInput
                                label="ROE (%) [≥]"
                                value={roeRange}
                                onChange={(v) => setRoeRange(v)}
                                placeholder={["Min", "Max"]}
                                suffix="%"
                                step={0.1}
                              />
                              <RangeInput
                                label="PE (TTM)"
                                value={peRange}
                                onChange={(v) => setPeRange(v)}
                                placeholder={["Min", "Max"]}
                                step={1}
                              />
                              <RangeInput
                                label="PB"
                                value={pbRange}
                                onChange={(v) => setPbRange(v)}
                                placeholder={["Min", "Max"]}
                                step={0.1}
                              />
                            </div>
                          )
                        },
                        {
                          key: 'sector',
                          label: <span className="text-xs font-bold text-slate-700 uppercase tracking-wide">行业/概念分类</span>,
                          children: (
                            <div className="space-y-4 pt-1">
                              <div className="space-y-2">
                                <div className="text-[11px] font-bold text-slate-500">行业筛选</div>
                                <Select
                                  mode="multiple"
                                  className={`w-full ${FIELD_STYLES.select}`}
                                  value={selectedSectors}
                                  onChange={setSelectedSectors}
                                  placeholder="选择行业（可多选）"
                                  options={availableSectorOptions}
                                  maxTagCount={2}
                                  maxTagPlaceholder={(omitted) => `+${omitted.length}`}
                                  showSearch
                                  filterOption={(input, option) => {
                                    const label = (option as any)?.label;
                                    return typeof label === 'string' && label.toLowerCase().includes(input.toLowerCase());
                                  }}
                                />
                              </div>
                              <div className="space-y-2">
                                <div className="text-[11px] font-bold text-slate-500">概念筛选</div>
                                <Select
                                  mode="multiple"
                                  className={`w-full ${FIELD_STYLES.select}`}
                                  value={selectedConcepts}
                                  onChange={setSelectedConcepts}
                                  placeholder="选择概念（可多选）"
                                  options={availableConceptOptions}
                                  maxTagCount={2}
                                  maxTagPlaceholder={(omitted) => `+${omitted.length}`}
                                  showSearch
                                  filterOption={(input, option) => {
                                    const label = (option as any)?.label;
                                    return typeof label === 'string' && label.toLowerCase().includes(input.toLowerCase());
                                  }}
                                />
                              </div>
                              <div className="space-y-2">
                                <div className="text-[11px] font-bold text-slate-500">指数筛选</div>
                                <Select
                                  mode="multiple"
                                  className={`w-full ${FIELD_STYLES.select}`}
                                  value={selectedIndices}
                                  onChange={setSelectedIndices}
                                  placeholder="选择指数（可多选）"
                                  options={availableIndexOptions}
                                  maxTagCount={2}
                                  maxTagPlaceholder={(omitted) => `+${omitted.length}`}
                                  showSearch
                                  filterOption={(input, option) => {
                                    const label = (option as any)?.label;
                                    return typeof label === 'string' && label.toLowerCase().includes(input.toLowerCase());
                                  }}
                                />
                              </div>
                              </div>
                            )
                          }
                        ]}
                      />
                    </div>
                    
                    <div className="absolute bottom-0 left-0 right-0 z-40 rounded-b-3xl border-t border-slate-200/80 bg-white/95 p-4 shadow-[0_-10px_30px_-15px_rgba(0,0,0,0.1)] backdrop-blur-xl supports-[backdrop-filter]:bg-white/90">
                      <div className="mb-3 text-[11px] font-medium text-slate-500 text-center">
                        {hasPendingFilterChanges ? '筛选条件已变更，点击应用后生效。' : '当前筛选条件已同步。'}
                      </div>
                      <div className="flex gap-3">
                        <Button size="middle" className="flex-1 rounded-xl font-bold border-slate-200" onClick={resetFilters}>
                          恢复默认
                        </Button>
                        <Button
                          size="middle"
                          type="primary"
                          className={`flex-1 rounded-xl font-black transition-all shadow-md ${hasPendingFilterChanges ? 'bg-blue-600 hover:bg-blue-500 hover:-translate-y-0.5' : 'bg-slate-300 border-none shadow-none text-slate-50'}`}
                          disabled={!hasPendingFilterChanges}
                          onClick={applyCurrentFilters}
                        >
                          应用筛选
                        </Button>
                      </div>
                    </div>
                  </div>
                </div>

                {/* 右侧主内容 */}
                <motion.div 
                  className="flex flex-col gap-4 min-w-0 flex-1 pb-20"
                  initial="hidden"
                  animate="visible"
                  variants={{
                    hidden: { opacity: 0 },
                    visible: {
                      opacity: 1,
                      transition: { staggerChildren: 0.1 }
                    }
                  }}
                >
                  <motion.div 
                    variants={{ hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } }}
                    className="grid gap-4 md:grid-cols-2 xl:grid-cols-3 flex-shrink-0"
                  >
                    <ResearchMetricCard
                      icon={LibraryBig}
                      label="候选池"
                      value={overview?.summary.total || 0}
                      subLabel="当前批次预测总量"
                      accentColor="#3b82f6"
                    />
                    <ResearchMetricCard
                      icon={Filter}
                      label="筛选结果"
                      value={filteredRows.length}
                      subLabel="符合当前条件的个股"
                      accentColor="#8b5cf6"
                    />
                    <ResearchMetricCard
                      icon={Flame}
                      label="高强度标的"
                      value={overview?.summary.strongCount || 0}
                      subLabel="模型高分命中 (≥0.05)"
                      accentColor="#f43f5e"
                    />
                  </motion.div>

                    <motion.div variants={{ hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } }} className="flex-1 min-h-0 flex flex-col glass-panel rounded-3xl overflow-hidden p-1 shadow-sm">
                      <motion.div 
                        initial={{ opacity: 0, y: 15 }}
                        animate={{ opacity: 1, y: 0 }}
                        className="glass-panel rounded-[32px] border border-white/60 p-7 mb-6 shadow-xl shadow-slate-200/50 flex-shrink-0 bg-white/40"
                      >
                        {/* 顶层：核心身份与状态 */}
                        <div className="flex flex-col md:flex-row md:items-end justify-between gap-6 pb-6 border-b border-slate-100/60">
                          <div className="space-y-3">
                            <div className="flex items-center gap-2 text-[10px] font-black uppercase tracking-widest text-slate-400">
                              <Sparkles className="h-3 w-3 text-blue-500" />
                              当前研究模型与批次
                            </div>
                            <div className="flex items-end gap-4">
                              <h2 className="text-4xl font-black text-slate-900 tracking-tight leading-none">
                                {availableModels.find(m => m.modelId === selectedModelId)?.name || '未选择模型'}
                              </h2>
                              <div className="flex items-center gap-1.5 px-3 py-1 rounded-xl bg-slate-900 text-[11px] font-black text-white shadow-lg shadow-slate-900/20 mb-0.5">
                                <Activity className="h-3 w-3" />
                                {selectedRunId}
                              </div>
                            </div>
                          </div>
                          
                          <div className="flex flex-wrap items-center gap-4">
                            <div className="flex flex-col gap-1 pr-6 border-r border-slate-100">
                              <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">执行周期</span>
                              <div className="flex items-center gap-3">
                                <div className="flex items-center gap-1.5 text-[12px] font-black text-slate-700">
                                  <Target className="h-3.5 w-3.5 text-blue-500" />
                                  {availableRuns.find(r => r.runId === selectedRunId)?.inferenceDate || '-'}
                                </div>
                                <div className="h-1 w-1 rounded-full bg-slate-300" />
                                <div className="flex items-center gap-1.5 text-[12px] font-black text-slate-700">
                                  <CandlestickChart className="h-3.5 w-3.5 text-emerald-500" />
                                  {availableRuns.find(r => r.runId === selectedRunId)?.targetDate || '-'}
                                </div>
                              </div>
                            </div>

                            <div className="flex flex-col gap-1">
                              <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">同步状态</span>
                              <Tag 
                                color={hasPendingFilterChanges ? 'warning' : 'success'} 
                                icon={hasPendingFilterChanges ? <RefreshCw className="h-3 w-3 animate-spin-slow" /> : <Search className="h-3 w-3" />}
                                className="m-0 rounded-xl border-none px-4 py-1.5 font-black text-[11px] shadow-sm uppercase tracking-wide flex items-center gap-1.5"
                              >
                                {hasPendingFilterChanges ? '待应用' : '已同步'}
                              </Tag>
                            </div>
                          </div>
                        </div>

                        {/* 下层：筛选条件与板块概览 */}
                        <div className="pt-6 grid grid-cols-1 lg:grid-cols-12 gap-8 items-center">
                          <div className="lg:col-span-7 space-y-3">
                            <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest flex items-center gap-2">
                              <Filter className="h-3 w-3" />
                              当前生效筛选条件
                            </div>
                            <div className="flex flex-wrap gap-2">
                              {selectedStockMatchedConditions.length > 0 ? (
                                selectedStockMatchedConditions.map((condition, idx) => (
                                  <motion.span 
                                    key={idx}
                                    whileHover={{ y: -2 }}
                                    className="bg-slate-100/80 hover:bg-white px-3 py-1.5 rounded-xl text-[11px] font-bold text-slate-600 flex items-center gap-1.5 border border-slate-200/50 transition-colors shadow-sm"
                                  >
                                    <div className="h-1.5 w-1.5 rounded-full bg-blue-400" />
                                    {condition}
                                  </motion.span>
                                ))
                              ) : (
                                <span className="text-[11px] font-bold text-slate-400 italic">未应用特定条件筛选</span>
                              )}
                            </div>
                          </div>

                          <div className="lg:col-span-5 space-y-3">
                            <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest flex items-center gap-2">
                              <BarChart3 className="h-3 w-3" />
                              核心板块分布
                            </div>
                            <div className="flex flex-wrap gap-2">
                              {sectorBreakdown.slice(0, 3).map((item, idx) => (
                                <motion.div 
                                  key={item.name}
                                  whileHover={{ scale: 1.05 }}
                                  className="bg-white/80 border border-slate-200 px-3 py-1.5 rounded-xl font-bold text-[11px] flex items-center gap-2 shadow-sm"
                                >
                                  <span className="text-slate-600">{item.name}</span>
                                  <span className={`px-1.5 py-0.5 rounded-md text-[10px] ${idx === 0 ? 'bg-blue-500 text-white' : 'bg-slate-100 text-slate-500'}`}>
                                    {item.count}
                                  </span>
                                </motion.div>
                              ))}
                              {sectorBreakdown.length > 3 && (
                                <div className="px-2 text-[10px] font-black text-slate-400 flex items-center cursor-help" title={sectorBreakdown.slice(3).map(s => `${s.name}(${s.count})`).join(', ')}>
                                  + {sectorBreakdown.length - 3} OTHERS
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      </motion.div>

                      <div className="flex items-center justify-between border-b border-slate-100 pb-4 mb-4 mt-2 gap-4 flex-shrink-0">
                        <Segmented 
                          value={activeDataSource} 
                          onChange={v => setActiveDataSource(v as DataSourceTab)}
                          options={[
                            { label: <div className="flex items-center gap-2 px-2"><LibraryBig className="h-3.5 w-3.5" />候选池 ({filteredRows.length})</div>, value: 'candidates' },
                            { label: <div className="flex items-center gap-2 px-2"><Quote className="h-3.5 w-3.5" />自选 ({watchlistTotal})</div>, value: 'watchlist' },
                            { label: <div className="flex items-center gap-2 px-2"><Microscope className="h-3.5 w-3.5" />研究池 ({poolTotal})</div>, value: 'pool' }
                          ]} 
                          className="research-next-segmented p-1.5" 
                        />
                        <div className="flex items-center gap-3">
                          {activeDataSource === 'candidates' && (
                            <div className="flex items-center rounded-[18px] border border-slate-200 bg-slate-50/50 p-1 gap-1">
                              {[
                                { key: 'score', label: '分数' },
                                { key: 'limitUp', label: '连板' },
                                { key: 'turnover', label: '换手' },
                                { key: 'amount', label: '成交额' },
                              ].map((item) => (
                                <button
                                  key={item.key}
                                  type="button"
                                  onClick={() => setSortKey(item.key as SortKey)}
                                  className={`min-w-[60px] whitespace-nowrap rounded-xl px-2.5 py-1.5 text-[10.5px] font-black transition-all ${
                                    sortKey === item.key
                                      ? 'bg-slate-800 text-white shadow-lg shadow-slate-400/20 scale-[1.02]'
                                      : 'text-slate-500 hover:text-slate-700 hover:bg-white'
                                  }`}
                                >
                                  {item.label}
                                </button>
                              ))}
                            </div>
                          )}
                          <Input 
                            className="premium-search-bar rounded-[18px] border-slate-200 font-bold h-10 max-w-[240px]" 
                            placeholder="搜索代码/名称..." 
                            prefix={<Search className="h-4 w-4 text-slate-400" />} 
                            value={keyword} 
                            onChange={e => setKeyword(e.target.value)} 
                          />
                        </div>
                      </div>

                      <div className="flex flex-col flex-1">
                        <div className="flex-1">
                          {activeDataSource === 'candidates' && (
                            <Table<ResearchStockRow>
                              className={FIELD_STYLES.table}
                              rowKey="key"
                              columns={columns}
                              dataSource={filteredRows.slice((candidatePage - 1) * candidatePageSize, candidatePage * candidatePageSize)}
                              pagination={false}
                              scroll={{ x: 1560 }}
                              onRow={r => ({ onClick: () => { setSelectedStockKey(r.key); setDetailModalOpen(true); } })}
                              rowClassName={r => `cursor-pointer transition-all ${r.key === selectedStockKey ? 'research-table-row-selected' : ''} ${r.isMatched === false ? 'opacity-40 grayscale-[0.5]' : 'font-medium'}`}
                            />
                          )}
                          {activeDataSource === 'watchlist' && (
                            <Table<ResearchStockRow>
                              className={FIELD_STYLES.table}
                              rowKey="key"
                              columns={watchlistColumns}
                              dataSource={watchlistData
                                .filter(item => !keyword || item.symbol.includes(keyword) || (item.stockName?.includes(keyword) ?? false))
                                .slice((watchlistPage - 1) * watchlistPageSize, watchlistPage * watchlistPageSize)
                                .map((item, idx) => ({
                                  ...(watchlistFeatures[item.symbol] || {
                                    key: item.key,
                                    code: item.symbol,
                                    name: item.stockName || '-',
                                    score: 0,
                                    signal: 'hold' as SignalType,
                                    latestChange: 0,
                                    nextDayReturn: null,
                                    day3Return: null,
                                    consecutiveLimitUpDays: 0,
                                    volumeTrend3d: false,
                                    volumeTrend5d: false,
                                    turnoverRate: 0,
                                    amount: 0,
                                    sector: '',
                                    concept: '',
                                    conceptTags: [],
                                    indexTags: [],
                                    riskFlags: [],
                                    closePrice: 0,
                                    pe: 0,
                                    roe: 0,
                                    profitGrowth: 0,
                                    rsi: 0,
                                    mainFlow: 0,
                                    instOwnership: 0,
                                    buyVol: 0,
                                    sellVol: 0,
                                    ma5: 0,
                                    ma10: 0,
                                    maGap5: 0,
                                    maGap10: 0,
                                    maGap20: 0,
                                    volRatio5: 0,
                                    return1d: 0,
                                    pb: 0,
                                    totalMv: 0,
                                    floatMv: 0,
                                    listedDays: 0,
                                    return3d: 0,
                                    isSt: false,
                                    isTradable: true,
                                    isHs300: false,
                                    isCsi500: false,
                                    isCsi1000: false,
                                    thesis: '',
                                    modelId: '',
                                    runId: '',
                                    rank: 0,
                                  }),
                                  rank: (watchlistPage - 1) * watchlistPageSize + idx + 1,
                                  key: item.key,
                                } as ResearchStockRow))}
                              loading={watchlistLoading}
                              pagination={false}
                              scroll={{ x: 1200 }}
                            />
                          )}
                          {activeDataSource === 'pool' && (
                            <Table<ResearchStockRow>
                              className={FIELD_STYLES.table}
                              rowKey="key"
                              columns={poolColumns}
                              dataSource={poolData
                                .filter(item => !keyword || item.symbol.includes(keyword) || (item.stockName?.includes(keyword) ?? false))
                                .slice((poolPage - 1) * poolPageSize, poolPage * poolPageSize)
                                .map((item, idx) => ({
                                  ...(poolFeatures[item.symbol] || {
                                    key: item.key,
                                    code: item.symbol,
                                    name: item.stockName || '-',
                                    score: item.fusionScore ?? 0,
                                    signal: 'hold' as SignalType,
                                    latestChange: 0,
                                    nextDayReturn: null,
                                    day3Return: null,
                                    consecutiveLimitUpDays: 0,
                                    volumeTrend3d: false,
                                    volumeTrend5d: false,
                                    turnoverRate: 0,
                                    amount: 0,
                                    sector: '',
                                    concept: '',
                                    conceptTags: [],
                                    indexTags: [],
                                    riskFlags: [],
                                    closePrice: 0,
                                    pe: 0,
                                    roe: 0,
                                    profitGrowth: 0,
                                    rsi: 0,
                                    mainFlow: 0,
                                    instOwnership: 0,
                                    buyVol: 0,
                                    sellVol: 0,
                                    ma5: 0,
                                    ma10: 0,
                                    maGap5: 0,
                                    maGap10: 0,
                                    maGap20: 0,
                                    volRatio5: 0,
                                    return1d: 0,
                                    pb: 0,
                                    totalMv: 0,
                                    floatMv: 0,
                                    listedDays: 0,
                                    return3d: 0,
                                    isSt: false,
                                    isTradable: true,
                                    isHs300: false,
                                    isCsi500: false,
                                    isCsi1000: false,
                                    thesis: '',
                                    modelId: '',
                                    runId: '',
                                    rank: 0,
                                  }),
                                  rank: (poolPage - 1) * poolPageSize + idx + 1,
                                  key: item.key,
                                } as ResearchStockRow))}
                              loading={poolLoading}
                              pagination={false}
                              scroll={{ x: 1200 }}
                            />
                          )}
                        </div>
                        <div className="flex justify-end items-center py-2 px-2 border-t border-slate-100 bg-white/80 backdrop-blur-sm">
                          {activeDataSource === 'candidates' && (
                            <Pagination
                              current={candidatePage}
                              pageSize={candidatePageSize}
                              total={filteredRows.length}
                              onChange={(page, pageSize) => { setCandidatePage(page); setCandidatePageSize(pageSize); }}
                              size="small"
                              showSizeChanger
                              showTotal={t => `共 ${t} 条`}
                            />
                          )}
                          {activeDataSource === 'watchlist' && (
                            <Pagination
                              current={watchlistPage}
                              pageSize={watchlistPageSize}
                              total={watchlistData.filter(item => !keyword || item.symbol.includes(keyword) || (item.stockName?.includes(keyword))).length}
                              onChange={(page, pageSize) => { setWatchlistPage(page); setWatchlistPageSize(pageSize); }}
                              size="small"
                              showSizeChanger
                              showTotal={t => `共 ${t} 条`}
                            />
                          )}
                          {activeDataSource === 'pool' && (
                            <Pagination
                              current={poolPage}
                              pageSize={poolPageSize}
                              total={poolData.filter(item => !keyword || item.symbol.includes(keyword) || (item.stockName?.includes(keyword))).length}
                              onChange={(page, pageSize) => { setPoolPage(page); setPoolPageSize(pageSize); }}
                              size="small"
                              showSizeChanger
                              showTotal={t => `共 ${t} 条`}
                            />
                          )}
                        </div>
                      </div>
                    </motion.div>
                </motion.div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <Modal 
        centered 
        width={1040} 
        open={detailModalOpen} 
        onCancel={() => setDetailModalOpen(false)} 
        title={selectedStock ? (
          <div className="flex items-center justify-between pr-8">
            <div className="flex items-center gap-2">
              <span className="text-slate-800 font-black tracking-tight">{selectedStock.name}</span>
              <span className="text-slate-400 font-bold text-sm">({selectedStock.code})</span>
              {selectedStock.isSt && <Tag color="error" className="ml-2 scale-90">ST</Tag>}
            </div>
            <div className="flex items-center gap-2">
              <Button 
                size="small"
                icon={<Quote className="h-3.5 w-3.5" />} 
                onClick={() => handleAddToWatchlist(selectedStock)}
                className="h-8 rounded-xl font-bold border-slate-200 text-xs hover:border-blue-400 hover:text-blue-500 transition-all active:scale-95"
              >
                加入自选
              </Button>
              <Button 
                size="small"
                type="primary" 
                icon={<Sparkles className="h-3.5 w-3.5" />} 
                onClick={() => handleAddToResearchPool(selectedStock)}
                className="h-8 rounded-xl font-bold bg-blue-600 text-xs shadow-md shadow-blue-500/20 transition-all hover:bg-blue-500 active:scale-95 border-none"
              >
                加入研究池
              </Button>
            </div>
          </div>
        ) : '详情'} 
        footer={null}
      >
        {selectedStock ? (
          <div className="max-h-[70vh] overflow-y-auto pr-2 custom-scrollbar space-y-4 py-2">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="p-4 bg-slate-50 rounded-2xl text-center">
                  <div className="text-[10px] font-bold text-slate-400">模型分数</div>
                  <div className="text-xl font-black text-blue-500">{safeNum(selectedStock.score, 0).toFixed(3)}</div>
                </div>
                <div className="p-4 bg-slate-50 rounded-2xl text-center">
                  <div className="text-[10px] font-bold text-slate-400">PE (TTM)</div>
                  <div className="text-xl font-black text-slate-700">{safeNum(selectedStock.pe, 0).toFixed(1)}</div>
                </div>
                <div className="p-4 bg-slate-50 rounded-2xl text-center">
                  <div className="text-[10px] font-bold text-slate-400">ROE</div>
                  <div className="text-xl font-black text-rose-500">
                    {Math.abs(safeNum(selectedStock.roe, 0)) <= 100 ? `${safeNum(selectedStock.roe, 0).toFixed(1)}%` : '-'}
                  </div>
                </div>
                <div className="p-4 bg-slate-50 rounded-2xl text-center">
                  <div className="text-[10px] font-bold text-slate-400">RSI</div>
                  <div className="text-xl font-black text-emerald-500">{safeNum(selectedStock.rsi, 0).toFixed(1)}</div>
                </div>
              </div>
              <div className="bg-slate-50/50 rounded-2xl p-2 border border-slate-100">
                <ReactECharts 
                  option={{
                    radar: {
                      indicator: radarMetrics?.indicator || [],
                      radius: '65%',
                      axisName: { color: '#94a3b8', fontSize: 10, fontWeight: 'bold' }
                    },
                    series: [{
                      type: 'radar',
                      data: radarMetrics ? [{
                        value: radarMetrics.value,
                        name: '综合评分',
                        itemStyle: { color: '#3b82f6' },
                        areaStyle: { color: 'rgba(59, 130, 246, 0.2)' }
                      }] : []
                    }]
                  }} 
                  style={{ height: '180px' }} 
                />
              </div>
            </div>
            
            <div className="p-5 border border-slate-100 rounded-3xl bg-white shadow-sm">
              <div className="text-[11px] font-black uppercase text-slate-500 mb-4 tracking-widest flex items-center gap-2">
                <Activity className="h-4 w-4" /> 技术与资金面透视
              </div>
              <div className="grid grid-cols-5 gap-3">
                {[
                  { label: 'MA5', val: fmt2(selectedStock.ma5) },
                  { label: 'MA10', val: fmt2(selectedStock.ma10) },
                  { label: '资金净流入', val: fmtMainFlowCn(selectedStock.flowNetAmount) },
                  { label: '主力资金', val: fmtMainFlowCn(selectedStock.mainFlow) },
                  { label: '利润增长', val: fmtPercent2(selectedStock.profitGrowth) }
                ].map((i, idx) => (
                  <div key={idx} className="bg-slate-50/50 p-3 rounded-xl text-center border border-slate-50">
                    <div className="text-[8px] font-bold text-slate-400">{i.label}</div>
                    <div className="text-xs font-black text-slate-800 mt-1">{i.val}</div>
                  </div>
                ))}
              </div>
            </div>

            <div className="p-5 border border-slate-100 rounded-3xl bg-white shadow-sm">
              <div className="text-[11px] font-black uppercase text-slate-500 mb-3 tracking-widest flex items-center gap-2">
                <BarChart3 className="h-4 w-4" /> 量化研究指标
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {[
                  { label: '模型分数', val: safeNum(selectedStock.score, 0).toFixed(3) },
                  { label: '连板天数', val: `${safeNum(selectedStock.consecutiveLimitUpDays, 0)} 天` },
                  { label: '成交额', val: `${safeNum(selectedStock.amount, 0).toFixed(2)} 亿` },
                  { label: '换手率', val: fmtPercent2(selectedStock.turnoverRate) },
                  { label: '涨跌幅', val: fmtSignedPercent2(selectedStock.latestChange) },
                  { label: '次日收益', val: fmtNullableSignedPercent2(selectedStock.nextDayReturn) },
                  { label: '3日收益', val: fmtNullableSignedPercent2(selectedStock.day3Return) },
                  { label: '行业', val: selectedStock.sector || '-' },
                  { label: '概念', val: (selectedStock.conceptTags && selectedStock.conceptTags.length > 0) ? selectedStock.conceptTags.join(' / ') : (selectedStock.concept || '-') },
                  { label: '指数', val: (selectedStock.indexTags || []).slice(0, 3).join(' / ') || '-' },
                  { label: '总市值', val: `${safeNum(selectedStock.totalMv, 0).toFixed(2)} 亿` },
                  { label: '流通市值', val: `${safeNum(selectedStock.floatMv, 0).toFixed(2)} 亿` },
                ].map((item, idx) => (
                  <div key={idx} className="min-h-[72px] rounded-2xl border border-slate-100 bg-slate-50/70 p-3 text-center">
                    <div className="text-[9px] font-black uppercase tracking-wide text-slate-400">{item.label}</div>
                    <div className="mt-2 text-sm font-black leading-5 text-slate-800 break-words">{item.val}</div>
                  </div>
                ))}
              </div>
            </div>

            <div className="p-5 border border-slate-100 rounded-3xl bg-white shadow-sm mt-3">
              <div className="text-[10px] font-black uppercase tracking-wider text-slate-500 mb-3">K 线走势 (近 60 日)</div>
              {klineLoading ? (
                <div className="flex h-[240px] items-center justify-center text-slate-400">加载中...</div>
              ) : klineOption ? (
                <ReactECharts option={klineOption} style={{ height: '240px' }} notMerge lazyUpdate />
              ) : (
                <div className="flex h-[240px] items-center justify-center text-slate-400 text-xs">暂无 K 线数据</div>
              )}
            </div>
          </div>
        ) : (
          <Empty description="请选择一只股票查看详情。" />
        )}
      </Modal>
    </>
  );
};

export default ResearchPlatformPage;
