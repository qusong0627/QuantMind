import type { ResearchFiltersState } from './types';

export const PRESET_FILTER_MAP: Record<string, any> = {
  高分优选: { minScore: 0.05 },
  连板突破: { limitUpDays: 2 },
  白马蓝筹: { roeMin: 5, totalMvMin: 80 },
  题材活跃: { turnoverMin: 8, amountMin: 10 },
  低位反弹: { maGap20Max: -0.03, rsiMax: 45 },
};

export const DEFAULT_RESEARCH_FILTERS: ResearchFiltersState = {
  minScore: -1.0,
  limitUpDays: 0,
  amountRange: [0, 100000],
  turnoverRange: [0, 100],
  volumeTrendOnly: false,
  highConfidenceOnly: false,
  selectedSectors: [],
  selectedConcepts: [],
  selectedIndices: [],
  peRange: [0, 100000],
  roeRange: [-1000, 1000],
  profitGrowthRange: [-1000, 1000],
  pbRange: [0, 1000],
  totalMvRange: [0, 1000000],
  floatMvRange: [0, 1000000],
  listedDaysRange: [0, 30000],
  return3dRange: [-100, 100],
  rsiRange: [0, 100],
  mainFlowRange: [-1000000, 1000000],
  instOwnershipRange: [0, 100],
  maGap5Range: [-100, 100],
  maGap10Range: [-100, 100],
  maGap20Range: [-100, 100],
  volRatio5Range: 0,
  volRatio20Range: 0,
  rsi14Range: [0, 100],
  return1dRange: [-100, 100],
  excludeSt: false,
  marketType: 'all',
  advancedFiltersEnabled: false,
};

export const BUTTON_STYLES = {
  headerRefresh:
    'h-9 rounded-xl border-slate-200 bg-white px-4 text-xs font-bold text-slate-600 shadow-sm transition-all hover:border-blue-400 hover:text-blue-500 hover:shadow-md active:scale-95',
  headerSave:
    'h-9 rounded-xl border border-slate-200 bg-white px-4 text-xs font-bold text-slate-700 shadow-sm transition-all hover:border-slate-300 hover:text-slate-900 active:scale-95',
  applyFilters:
    'group relative w-full overflow-hidden rounded-2xl bg-slate-900 py-3.5 font-black text-white shadow-xl shadow-slate-900/20 transition-all hover:bg-slate-800 hover:shadow-2xl hover:-translate-y-0.5 active:scale-95 active:translate-y-0',
};

export const FIELD_STYLES = {
  select: 'research-next-select rounded-xl font-bold border-slate-200',
  input: 'research-next-input rounded-xl border-slate-200 font-medium h-10',
  slider: 'research-next-slider py-4',
  collapse: 'research-next-collapse border-none bg-transparent',
  table: 'research-next-table custom-scrollbar',
  segmented: 'research-next-segmented rounded-2xl p-1 bg-slate-100',
};

export const TEMPLATE_BUTTON_STYLES = {
  idle: 'bg-slate-50 text-slate-500 border-slate-200 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-500',
  active: 'bg-blue-600 text-white border-blue-600 shadow-md shadow-blue-500/20',
};
