export type SignalType = 'buy' | 'hold' | 'sell';
export type ConfidenceLevel = 'high' | 'medium' | 'watch';
export type SortKey = 'score' | 'limitUp' | 'turnover' | 'amount';
export type FilterSectionKey =
  | 'common'
  | 'market'
  | 'sector'
  | 'fundamental'
  | 'technical'
  | 'advanced';
export type DataSourceTab = 'candidates' | 'watchlist' | 'pool';

export interface ResearchModelOption {
  modelId: string;
  name: string;
  style: string;
  description: string;
}

export interface WatchlistRow {
  key: string;
  symbol: string;
  stockName: string | null;
  addedAt: string | null;
  sourceRunId: string | null;
  notes: string | null;
  tags: string[];
}

export interface ResearchPoolRow {
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

export interface ResearchStockRow {
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
  volumeTrend3d: number;
  volumeTrend5d: boolean;
  turnoverRate: number;
  amount: number;
  marketCap?: number;
  sector: string;
  concept: string;
  signal?: SignalType;
  closePrice?: number;
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
  hitReasons?: string[];
  volumeBars?: number[];
  thesis: string;
  confidence?: ConfidenceLevel;
  isMatched?: boolean;
  isSt?: boolean;
  isTradable?: boolean;
  isHs300?: boolean;
  isCsi500?: boolean;
  isCsi1000?: boolean;
}

export interface ResearchFiltersState {
  minScore: number;
  limitUpDays: number;
  amountRange: [number, number];
  turnoverRange: [number, number];
  volumeTrendOnly: boolean;
  highConfidenceOnly: boolean;
  selectedSectors: string[];
  selectedConcepts: string[];
  selectedIndices: string[];
  peRange: [number, number];
  roeRange: [number, number];
  profitGrowthRange: [number, number];
  pbRange: [number, number];
  totalMvRange: [number, number];
  floatMvRange: [number, number];
  listedDaysRange: [number, number];
  return3dRange: [number, number];
  rsiRange: [number, number];
  mainFlowRange: [number, number];
  instOwnershipRange: [number, number];
  maGap5Range: [number, number];
  maGap10Range: [number, number];
  maGap20Range: [number, number];
  volRatio5Range: number;
  volRatio20Range: number;
  rsi14Range: [number, number];
  return1dRange: [number, number];
  excludeSt: boolean;
  marketType: string;
  advancedFiltersEnabled: boolean;
}
