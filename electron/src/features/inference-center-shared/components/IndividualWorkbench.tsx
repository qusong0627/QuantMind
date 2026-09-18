/**
 * 右栏：个股预测推理工作台。
 *
 * 上方一条工具条 = 原有配置项（代码联想 / 基准日 / 周期 / 模型选型 / 分类过滤）压成一行，
 * 窄窗口下自动折行；下方是结果区：K 线 + 分位扇形、模型指标、多模型分数曲线、SHAP 归因、共识矩阵。
 * 由左栏排名榜联动驱动 —— 换标的时不必再手输代码。
 *
 * 版面取向：报价条只出现一次核心数字（分数挪到报价条后，指标卡不再重复一遍），
 * 圆角与阴影收敛到同一档，避免卡片套卡片的杂乱感。
 */

import React from 'react';
import { Button, DatePicker, Input, Select, Spin, Tag, Tooltip, Typography } from 'antd';
import { clsx } from 'clsx';
import {
  Search, Clock, Calendar, Database, Play, TrendingUp, Sparkles,
} from 'lucide-react';
import { StockForecastChart } from './StockForecastChart';
import { ModelScoreCurveGrid } from './ModelScoreCurveGrid';
import { FeatureDriversPanel } from './FeatureDriversPanel';
import { ModelConsensusPanel } from './ModelConsensusPanel';
import type { IndividualPrediction, ModelCategoryFilter } from '../hooks/useIndividualPrediction';
import type { SuggestionItem } from '../adapter';

const { Text } = Typography;

const HORIZON_OPTIONS = [
  { label: 'T+1 次日预期', value: 1 },
  { label: 'T+3 短线周期', value: 3 },
  { label: 'T+5 一周趋势 (推荐)', value: 5 },
  { label: 'T+10 双周展望', value: 10 },
];

const CATEGORY_OPTIONS: { value: ModelCategoryFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'dl', label: '深度' },
  { value: 'tree', label: '树模' },
];

interface IndividualWorkbenchProps {
  ip: IndividualPrediction;
  /** 货币符号（¥ / HK$ / $） */
  currencySymbol: string;
  /** 输入框占位提示 */
  searchPlaceholder: string;
  /** 联想项展示文案 */
  suggestionLabel: (item: SuggestionItem) => string;
  /** 多模型分数曲线需要的后缀式代码 */
  toSuffixSymbol: (symbol: string) => string;
}

/** 报价条里的一格：标签在上、值在下，横向细线分隔 */
const QuoteCell: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex flex-col justify-center min-w-0 px-3 first:pl-0">
    <span className="text-[10px] text-slate-400 font-semibold leading-tight">{label}</span>
    <span className="text-[13px] font-black font-mono leading-tight truncate">{children}</span>
  </div>
);

export const IndividualWorkbench: React.FC<IndividualWorkbenchProps> = ({
  ip,
  currencySymbol,
  searchPlaceholder,
  suggestionLabel,
  toSuffixSymbol,
}) => {
  const { prediction } = ip;

  /** 评级徽标：沿用全站「涨红跌绿」（三市场一致） */
  const ratingBadge = (rating: string) => {
    switch (rating) {
      case 'STRONG_BUY':
        return (
          <span className="flex items-center gap-1.5 text-rose-700 bg-rose-50 border border-rose-200 px-2 py-0.5 rounded-md font-black text-[11px]">
            <span className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />
            强烈看多
          </span>
        );
      case 'BUY':
        return (
          <span className="flex items-center gap-1.5 text-red-600 bg-red-50 border border-red-200 px-2 py-0.5 rounded-md font-black text-[11px]">
            <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
            偏多研判
          </span>
        );
      case 'HOLD':
        return (
          <span className="flex items-center gap-1.5 text-slate-600 bg-slate-100 border border-slate-200 px-2 py-0.5 rounded-md font-black text-[11px]">
            <span className="w-1.5 h-1.5 rounded-full bg-slate-400" />
            中性观望
          </span>
        );
      default:
        return (
          <span className="flex items-center gap-1.5 text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-md font-black text-[11px]">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
            看空警示
          </span>
        );
    }
  };

  const scoreColor = (v: number) => (v >= 0 ? 'text-rose-600' : 'text-emerald-600');
  const coverage = prediction && prediction.confidence > 0
    ? `${(prediction.confidence * 100).toFixed(1)}%`
    : '—';
  const ic = ip.selectedModel?.accuracy;

  return (
    <div className="flex-1 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden">
      {/* ── 配置工具条（一行，窄窗口自动折行）──────────────────── */}
      <div className="shrink-0 px-3 py-2 border-b border-slate-200 bg-slate-50/60 flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[170px]">
          <div className="flex items-center bg-white border border-slate-200 hover:border-blue-400 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-100 rounded-md pl-2.5 pr-2 h-8 transition-all">
            <Search size={13} className="text-slate-400 shrink-0 mr-1.5" />
            <Input
              variant="borderless"
              placeholder={searchPlaceholder}
              value={ip.inputCode}
              onChange={(e) => {
                ip.setInputCode(e.target.value.toUpperCase());
                ip.setShowSuggestions(true);
              }}
              onFocus={() => ip.setShowSuggestions(true)}
              onBlur={() => {
                // 延迟提交，给下拉项的 click 留出触发窗口
                window.setTimeout(() => {
                  ip.setShowSuggestions(false);
                  ip.commitCode(ip.inputCode);
                }, 160);
              }}
              onPressEnter={() => ip.commitCode(ip.inputCode)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') ip.setShowSuggestions(false);
              }}
              className="p-0 font-mono font-bold text-xs text-blue-600"
              style={{ flex: 1, minWidth: 70, padding: 0 }}
            />
            <span className="text-[11px] font-bold text-slate-500 shrink-0 pl-2 border-l border-slate-200 max-w-[110px] truncate">
              {prediction?.stock_name || '标的资产'}
            </span>
          </div>

          {/* 联想下拉：本地/远端标的表 */}
          {ip.showSuggestions && ip.suggestions.length > 0 && (
            <div className="absolute z-50 left-0 right-0 top-full mt-1 bg-white border border-slate-200 rounded-md shadow-lg max-h-60 overflow-y-auto custom-scrollbar">
              {ip.suggestions.map((s) => (
                <div
                  key={s.symbol}
                  // 阻止 mousedown 抢先触发输入框 onBlur 提交
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => ip.selectSuggestion(s)}
                  className="px-3 py-1.5 hover:bg-blue-50 cursor-pointer border-b border-slate-50 last:border-b-0 flex items-center justify-between gap-2"
                >
                  <span className="text-xs font-bold text-slate-700 truncate">{s.name || s.symbol}</span>
                  <span className="text-[11px] font-mono text-blue-600 shrink-0">{suggestionLabel(s)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <DatePicker
          value={ip.date}
          onChange={ip.setDate}
          allowClear={false}
          size="small"
          className="w-[118px] shrink-0"
          suffixIcon={<Calendar size={12} className="text-amber-500" />}
        />

        <Select
          value={ip.horizon}
          onChange={ip.setHorizon}
          size="small"
          className="w-[112px] shrink-0"
          suffixIcon={<Clock size={12} className="text-indigo-500" />}
          options={HORIZON_OPTIONS}
        />

        <span className="w-px h-5 bg-slate-200 shrink-0" />

        <Select
          value={ip.modelId}
          onChange={ip.setModelId}
          size="small"
          className="flex-1 min-w-[160px]"
          suffixIcon={<Database size={12} className="text-purple-500" />}
          optionLabelProp="label"
          options={ip.filteredModels.map((m) => ({
            value: m.modelId,
            label: m.modelName,
            // 下拉里保留原卡片列表的全部信息：名称 + 训练状态
            display: (
              <div className="flex items-center justify-between gap-3">
                <span className="truncate">{m.modelName}</span>
                <span className="text-[10px] font-bold font-mono px-1.5 py-0.5 rounded bg-slate-100 border border-slate-300 text-slate-600 shrink-0">
                  {m.tag}
                </span>
              </div>
            ),
          }))}
          optionRender={(option) => option.data.display}
        />

        <div className="flex items-center gap-0.5 bg-slate-200/70 p-0.5 rounded-md text-[11px] shrink-0">
          {CATEGORY_OPTIONS.map((cat) => (
            <button
              key={cat.value}
              type="button"
              onClick={() => ip.setCategoryFilter(cat.value)}
              className={clsx(
                'px-2 py-0.5 rounded font-bold transition-colors',
                ip.categoryFilter === cat.value
                  ? 'bg-white text-blue-700 shadow-2xs'
                  : 'text-slate-600 hover:text-slate-900',
              )}
            >
              {cat.label}
            </button>
          ))}
        </div>

        <Button
          type="primary"
          size="small"
          icon={<Play size={12} fill="currentColor" />}
          loading={ip.loading}
          onClick={ip.execute}
          className="shrink-0 rounded-md h-8 px-3 bg-blue-600 border-0 font-bold text-[11px]"
        >
          开始个股推理
        </Button>
      </div>

      {/* 基准日回退提示：所选日无数据时后端会回退，必须显式告知 */}
      {prediction?.as_of_date && ip.date && prediction.as_of_date !== ip.date.format('YYYY-MM-DD') && (
        <div className="shrink-0 px-3 py-1 bg-amber-50 border-b border-amber-100 text-[10px] text-amber-700 font-semibold">
          实际数据日：{prediction.as_of_date}（所选日期无数据已回退）
        </div>
      )}

      {/* ── 报价条：名称/代码/基准价/分数/评级/模型，一行读完 ──── */}
      {prediction ? (
        <div className="shrink-0 px-4 h-14 border-b border-slate-200 flex items-center gap-3 flex-wrap">
          <div className="flex items-baseline gap-2 min-w-0">
            <span className="text-base font-black text-slate-800 tracking-tight truncate">{prediction.stock_name}</span>
            <span className="text-[11px] font-mono font-bold text-slate-400 shrink-0">{prediction.symbol}</span>
          </div>

          <span className="w-px h-7 bg-slate-200 shrink-0" />

          <div className="flex items-center shrink-0 divide-x divide-slate-200">
            <QuoteCell label="基准价格">
              <span className="text-slate-900">
                {currencySymbol}{prediction.current_price ? prediction.current_price.toFixed(2) : '—'}
              </span>
            </QuoteCell>
            <QuoteCell label="模型信号分数">
              <span className={scoreColor(prediction.expected_return)}>{prediction.predicted_score.toFixed(4)}</span>
            </QuoteCell>
          </div>

          <div className="ml-auto flex items-center gap-2 shrink-0 flex-wrap">
            {prediction.rating && ratingBadge(prediction.rating)}
            <Tooltip title={prediction.model_name || ip.selectedModel?.modelName || '—'}>
              <span className="flex items-center gap-1.5 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded-md max-w-[200px]">
                <span className="text-[10px] text-slate-400 font-semibold shrink-0">模型</span>
                <span className="text-[11px] font-bold text-slate-700 truncate">
                  {prediction.model_name || ip.selectedModel?.modelName || '—'}
                </span>
              </span>
            </Tooltip>
            <span className="flex items-center gap-1 text-[10px] font-bold text-slate-400" title="分数来自落库的真实模型推理结果">
              <span className="w-1.5 h-1.5 rounded-full bg-rose-500" />
              真实推理
            </span>
          </div>
        </div>
      ) : (
        <div className="shrink-0 px-4 h-14 border-b border-slate-200 flex items-center gap-2">
          <TrendingUp size={15} className="text-slate-300" />
          <span className="text-xs text-slate-500 font-semibold">
            在左侧排名榜点一只股票，或在上方输入代码后回车
          </span>
        </div>
      )}

      {/* ── 结果区 ──────────────────────────────────────────── */}
      <div className="flex-1 min-h-0 p-3 flex flex-col gap-3 overflow-y-auto custom-scrollbar bg-slate-50/60">
        {ip.loading && !prediction ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-3 bg-white rounded-xl border border-slate-200 min-h-[300px]">
            <Spin size="large" />
            <span className="text-xs font-semibold text-slate-500">正在接入真实推理引擎与行情...</span>
          </div>
        ) : prediction ? (
          <div className="flex flex-col gap-3">
            {prediction.forecast_warning && (
              <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-800">
                {prediction.forecast_warning}
              </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-5 gap-3 shrink-0" style={{ minHeight: '340px' }}>
              <div className="lg:col-span-3 bg-white rounded-xl border border-slate-200 flex flex-col overflow-hidden min-h-[320px]">
                <StockForecastChart
                  currencySymbol={currencySymbol}
                  kline={ip.kline}
                  forecast={prediction.forecast_curve}
                  symbol={prediction.symbol}
                  stockName={prediction.stock_name}
                  currentPrice={prediction.current_price}
                  modelName={prediction.model_name || ip.selectedModel?.modelName}
                  asOfDate={prediction.as_of_date}
                />
              </div>

              {/* 指标卡：分数已在报价条出现过，这里只放它没有的（分位区间 / 校准 / IC） */}
              <div className="lg:col-span-2 bg-white rounded-xl border border-slate-200 flex flex-col">
                <div className="shrink-0 flex items-center justify-between px-3 h-9 border-b border-slate-100">
                  <span className="text-[11px] font-bold text-slate-700">模型推理指标</span>
                  <Tooltip title="分数取自落库的模型推理结果（Persisted Model Score）">
                    <Sparkles size={12} className="text-slate-400 cursor-help" />
                  </Tooltip>
                </div>

                <div className="flex-1 min-h-0 p-3 flex flex-col gap-3">
                  <div className="p-3 bg-slate-50 rounded-lg border border-slate-200">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-[11px] font-bold text-slate-600">分位数区间</span>
                      <span className="text-[10px] text-slate-400">验证集校准</span>
                    </div>
                    {prediction.p10_return != null && prediction.p90_return != null ? (
                      <>
                        <div className="grid grid-cols-3 gap-2 text-center">
                          <div>
                            <div className="text-[10px] text-emerald-600">P10 下界</div>
                            <div className="font-mono text-xs font-bold text-emerald-700">{prediction.p10_return.toFixed(2)}%</div>
                          </div>
                          <div>
                            <div className="text-[10px] text-blue-600">P50 中枢</div>
                            <div className="font-mono text-xs font-bold text-blue-700">{(prediction.p50_return ?? 0).toFixed(2)}%</div>
                          </div>
                          <div>
                            <div className="text-[10px] text-rose-600">P90 上界</div>
                            <div className="font-mono text-xs font-bold text-rose-700">{prediction.p90_return.toFixed(2)}%</div>
                          </div>
                        </div>
                        <p className="mt-2.5 pt-2 border-t border-slate-200 text-[10px] text-slate-500 m-0">
                          区间覆盖率 <strong className="font-mono text-slate-700">{coverage}</strong>
                        </p>
                      </>
                    ) : (
                      <p className="text-[11px] leading-relaxed text-slate-500 m-0">
                        该模型未启用分位推理；当前仅提供真实信号分数。
                      </p>
                    )}
                  </div>

                  <div className="mt-auto grid grid-cols-2 gap-2 text-center">
                    <div className="px-2 py-2 bg-slate-50 rounded-lg border border-slate-200">
                      <div className="text-[10px] text-slate-400 font-semibold">模型准确率 (IC)</div>
                      <div className="font-mono text-xs font-bold text-slate-700">
                        {ic != null && ic !== 0 ? (typeof ic === 'number' ? ic.toFixed(3) : ic) : '—'}
                      </div>
                    </div>
                    <div className="px-2 py-2 bg-slate-50 rounded-lg border border-slate-200">
                      <div className="text-[10px] text-slate-400 font-semibold">区间覆盖率</div>
                      <div className="font-mono text-xs font-bold text-slate-700">{coverage}</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* 多模型分数曲线：与下方归因/共识是不同维度，并存而非二选一 */}
            <div className="bg-white rounded-xl border border-slate-200 overflow-hidden flex-none" style={{ height: '250px' }}>
              <ModelScoreCurveGrid
                consensus={prediction.consensus}
                consensusScore={prediction.consensus_score}
                selectedCount={0}
                suffixSymbol={toSuffixSymbol(prediction.symbol || ip.symbol)}
                asOfDate={prediction.as_of_date}
              />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 flex-none" style={{ minHeight: '240px' }}>
              <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                <FeatureDriversPanel drivers={prediction.drivers} source={prediction.drivers_source} />
              </div>
              <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                <ModelConsensusPanel
                  consensus={prediction.consensus}
                  consensusScore={prediction.consensus_score}
                  selectedCount={0}
                />
              </div>
            </div>
          </div>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center gap-3 bg-white rounded-xl border border-dashed border-slate-200 text-slate-500 min-h-[300px]">
            <Database size={26} className="opacity-30" />
            <span className="text-xs font-semibold">
              点左侧「本次推理排名」里的任意一行，这里会直接出该股的预测与因子归因
            </span>
          </div>
        )}
      </div>
    </div>
  );
};
