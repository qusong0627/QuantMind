/** 看板风格筛选面板：驱动左侧股票列表（状态由页面持有，compact=左侧列内嵌模式） */

import { useEffect, useState } from 'react';
import { SlidersHorizontal, X, RotateCcw } from 'lucide-react';
import { Select } from 'antd';
import { stockTerminalService } from '../services/stockTerminalService';
import { MarketCalendarFilter } from './MarketCalendarFilter';

export interface ListFilters {
  board?: string;
  capTier?: string;
  bucket?: string;     // 分数档 key -> score_min/max
  trend?: string;
  industry?: string;
  concept?: string;
  indexCode?: string;  // 宽基指数成分
  indexName?: string;
  model?: string;
  date?: string;
  scoreMin?: number;
  tagId?: string;
  tagName?: string;
  side?: string;       // 信号方向 BUY/SELL/HOLD（列表表头筛选）
  excludeSt?: boolean; // 排除 ST 股
}

export const BOARD_OPTIONS = ['沪市主板', '深市主板', '科创板', '创业板', '北交所'];
export const CAP_TIER_OPTIONS = [
  { value: '微盘', label: '微盘 <30亿' },
  { value: '小盘', label: '小盘 30-100亿' },
  { value: '中盘', label: '中盘 100-300亿' },
  { value: '大盘', label: '大盘 300-1000亿' },
  { value: '超大盘', label: '超大盘 >1000亿' },
];
export const TREND_OPTIONS = [
  { value: '连续上升', label: '连续上升' },
  { value: '连续下降', label: '连续下降' },
  { value: '先升后降', label: '先升后降 · 最佳买点' },
  { value: '上升', label: '单日上升' },
  { value: '下降', label: '单日下降' },
  { value: '持平', label: '持平' },
];
export const BUCKET_OPTIONS: { value: string; label: string; min?: number; max?: number }[] = [
  { value: 'golden', label: '黄金区间 0.10-0.12', min: 0.10, max: 0.12 },
  { value: 'optional', label: '可选 0.12-0.15', min: 0.12, max: 0.15 },
  { value: 'caution', label: '谨慎 0.15-0.20', min: 0.15, max: 0.20 },
  { value: 'extreme', label: '极端高分 ≥0.20', min: 0.20 },
  { value: 'neg_extreme', label: '极端负分 ≤-0.20', max: -0.20 },
  { value: 'neg_short', label: '做空候选 ≤-0.15', max: -0.15 },
  { value: 'pos', label: '全部正分 ≥0', min: 0 },
  { value: 'neg', label: '全部负分 <0', max: 0 },
];

export function bucketScoreRange(bucket?: string): { min?: number; max?: number } {
  const def = BUCKET_OPTIONS.find(b => b.value === bucket);
  return { min: def?.min, max: def?.max };
}

interface Props {
  filters: ListFilters;
  onChange: (f: ListFilters) => void;
  total: number;        // 筛选后数量（已选条件的命中数）
  fullTotal: number;    // 全市场数量
  models?: { model_id: string; display_name?: string }[];  // 真实模型（值用 model_id，标签用显示名）
  /** 左侧列内嵌模式：筛选网格 2 列紧凑排布 */
  compact?: boolean;
  /** 各条件下拉选项命中数（由父级统计），如 { board: {沪市主板: 1500}, capTier: {中盘: 800} } */
  optionCounts?: Record<string, Record<string, number>>;
  /** 表头列筛选模式：面板只留「推理模型 + 概念板块」两列（板块/行业/市值/趋势/得分/信号移到列表表头） */
  columnOnly?: boolean;
  /** 概念板块与推理模型之间显示大盘 MA20 日历图标（按日期筛选推理排序） */
  showMarketCalendar?: boolean;
  /** 日历补推理完成后回调（外层刷新列表） */
  onInferred?: () => void;
}

export function StockFilterPanel({ filters, onChange, total, fullTotal, models: modelOptions = [], compact = false, optionCounts = {}, columnOnly = false, showMarketCalendar = false, onInferred }: Props) {
  const [industries, setIndustries] = useState<string[]>([]);
  const [concepts, setConcepts] = useState<string[]>([]);

  useEffect(() => {
    stockTerminalService.getIndustries().then(setIndustries).catch(() => setIndustries([]));
    stockTerminalService.getConcepts().then(setConcepts).catch(() => setConcepts([]));
  }, []);

  const set = (patch: Partial<ListFilters>) => onChange({ ...filters, ...patch });

  /** 选项 label：名称 + 命中数量（父级传的统计），未统计则只显示名称 */
  const opt = (dim: string, name: string) => {
    const n = optionCounts[dim]?.[name];
    return n != null ? `${name} ${n}` : name;
  };

  const activeChips: { key: string; label: string; clear: () => void }[] = [];
  if (!columnOnly) {
    if (filters.board) activeChips.push({ key: 'board', label: `板块 ${filters.board}`, clear: () => set({ board: undefined }) });
    if (filters.capTier) activeChips.push({ key: 'cap', label: `市值 ${filters.capTier}`, clear: () => set({ capTier: undefined }) });
    const bd = BUCKET_OPTIONS.find(b => b.value === filters.bucket);
    if (bd) activeChips.push({ key: 'bucket', label: `分数 ${bd.label}`, clear: () => set({ bucket: undefined, scoreMin: undefined }) });
    if (filters.trend) activeChips.push({ key: 'trend', label: `趋势 ${filters.trend}`, clear: () => set({ trend: undefined }) });
    if (filters.industry) activeChips.push({ key: 'industry', label: `行业 ${filters.industry}`, clear: () => set({ industry: undefined }) });
    if (filters.side) activeChips.push({ key: 'side', label: `信号 ${filters.side}`, clear: () => set({ side: undefined }) });
  }
  if (filters.concept) activeChips.push({ key: 'concept', label: `概念 ${filters.concept}`, clear: () => set({ concept: undefined }) });
  if (filters.indexCode) activeChips.push({ key: 'index', label: `宽基 ${filters.indexName ?? filters.indexCode}`, clear: () => set({ indexCode: undefined, indexName: undefined }) });
  if (filters.model) {
    const m = modelOptions.find(x => x.model_id === filters.model);
    activeChips.push({ key: 'model', label: `模型 ${m?.display_name || filters.model}`, clear: () => set({ model: undefined }) });
  }
  if (filters.date) activeChips.push({ key: 'date', label: `日期 ${filters.date}`, clear: () => set({ date: undefined }) });
  if (filters.scoreMin != null && !filters.bucket) activeChips.push({ key: 'smin', label: `分数≥${filters.scoreMin}`, clear: () => set({ scoreMin: undefined }) });
  if (filters.tagId) activeChips.push({ key: 'tag', label: `标签 ${filters.tagName}`, clear: () => set({ tagId: undefined, tagName: undefined }) });

  return (
    <div className="flex flex-col gap-1.5 shrink-0">
      {/* 标题行 */}
      <div className="flex items-center justify-between px-1">
        <div className="flex items-center gap-1.5">
          <SlidersHorizontal className="w-3.5 h-3.5 text-blue-500" />
          <span className="text-xs font-black text-slate-700">条件筛选</span>
          <span className="text-[10px] text-slate-400 font-bold">
            {fullTotal > 0 && <>命中 <b className="text-blue-600">{total}</b> / {fullTotal} 只</>}
          </span>
          <button
            onClick={() => set({ excludeSt: !filters.excludeSt })}
            className={`text-[10px] font-bold px-1.5 py-0.5 rounded border transition-colors ${
              filters.excludeSt
                ? 'bg-amber-50 text-amber-600 border-amber-200'
                : 'bg-slate-50 text-slate-400 border-slate-200 hover:border-slate-300'
            }`}
          >
            {filters.excludeSt ? '已排除ST' : '排除ST'}
          </button>
        </div>
        {(activeChips.length > 0 || filters.excludeSt) && (
          <button onClick={() => onChange({})} className="flex items-center gap-1 text-[10px] font-bold text-slate-400 hover:text-rose-500 transition-colors">
            <RotateCcw className="w-3 h-3" /> 清空
          </button>
        )}
      </div>

      {/* 筛选下拉网格：columnOnly 只留 概念+日历+模型，其余维度在列表表头筛选 */}
      <div className={`grid gap-1.5 items-center ${compact ? (showMarketCalendar ? 'grid-cols-[1fr_auto_1fr]' : 'grid-cols-2') : 'grid-cols-4'}`}>
        {!columnOnly && (
          <>
            <Select allowClear size="small" placeholder="板块" value={filters.board || undefined}
              onChange={v => set({ board: v })} options={BOARD_OPTIONS.map(b => ({ label: opt('board', b), value: b }))} />
            <Select allowClear size="small" placeholder="市值档" value={filters.capTier || undefined}
              onChange={v => set({ capTier: v })} options={CAP_TIER_OPTIONS.map(c => ({ label: opt('capTier', c.value), value: c.value }))} />
            <Select allowClear size="small" placeholder="分数档" value={filters.bucket || undefined}
              onChange={v => set({ bucket: v, scoreMin: undefined })}
              options={BUCKET_OPTIONS.map(b => ({ label: opt('bucket', b.value), value: b.value }))} />
            <Select allowClear size="small" placeholder="趋势" value={filters.trend || undefined}
              onChange={v => set({ trend: v })} options={TREND_OPTIONS.map(t => ({ label: opt('trend', t.value), value: t.value }))} />
            <Select allowClear showSearch size="small" placeholder="行业" value={filters.industry || undefined}
              optionFilterProp="label" onChange={v => set({ industry: v })}
              options={industries.map(i => ({ label: i, value: i }))} />
          </>
        )}
        <Select allowClear showSearch size="small" placeholder="概念板块" value={filters.concept || undefined}
          optionFilterProp="label" onChange={v => set({ concept: v })}
          options={concepts.map(c => ({ label: c, value: c }))}
          maxTagCount={1} listHeight={200} />
        {showMarketCalendar && (
          <MarketCalendarFilter
            date={filters.date}
            model={filters.model}
            onPickDate={d => set({ date: d })}
            onInferred={onInferred}
          />
        )}
        <Select allowClear showSearch size="small" placeholder="推理模型" value={filters.model || undefined}
          optionFilterProp="label" onChange={v => set({ model: v })}
          options={modelOptions.map(m => ({ label: m.display_name || m.model_id, value: m.model_id }))} />
      </div>

      {/* 已选条件：每条件单独一行；columnOnly 模式不渲染（状态已在列表表头高亮） */}
      {!columnOnly && activeChips.length > 0 && (
        <div className="flex flex-col gap-1 px-1">
          {activeChips.map(c => (
            <div key={c.key} className="flex items-center gap-1.5 min-w-0">
              <button onClick={c.clear}
                className="flex items-center gap-1 px-2 py-0.5 rounded-md bg-blue-50 text-blue-600 border border-blue-100 text-[10px] font-bold hover:bg-rose-50 hover:text-rose-500 hover:border-rose-100 transition-colors max-w-full min-w-0">
                <span className="truncate">{c.label}</span> <X className="w-2.5 h-2.5 shrink-0" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
