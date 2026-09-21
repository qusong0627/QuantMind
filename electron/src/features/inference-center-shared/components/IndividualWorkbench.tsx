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
import { Button, DatePicker, Input, Select, Spin, Tag, Tooltip, Typography, message } from 'antd';
import { clsx } from 'clsx';
import {
  Search, Clock, Calendar, Database, Play, TrendingUp, Sparkles, Layers, X,
} from 'lucide-react';
import { StockForecastChart } from './StockForecastChart';
import { ModelScoreCurveGrid } from './ModelScoreCurveGrid';
import { FeatureDriversPanel } from './FeatureDriversPanel';
import { ModelConsensusPanel } from './ModelConsensusPanel';
import {
  CONSENSUS_PICK_LIMIT,
  type IndividualPrediction,
  type ModelCategoryFilter,
} from '../hooks/useIndividualPrediction';
import type { SuggestionItem } from '../adapter';
import {
    RESEARCH_SCORE_HINT,
    formatResearchScore,
    researchScore,
    researchScoreBand,
} from '../../shared/researchScore';

const { Text } = Typography;

/**
 * 档位配色：**同一色相的深浅**，不用红绿。
 *
 * 红绿是涨跌语义，会给一个中性的分位分数带上方向暗示；单色深浅只表达
 * 「靠前/靠后」的强弱，与本徽标要说的事一致。
 */
const RESEARCH_SCORE_TONE: Record<string, string> = {
    头部: 'text-indigo-700 bg-indigo-50 border-indigo-200',
    居前: 'text-indigo-600 bg-indigo-50/70 border-indigo-200',
    居中: 'text-slate-600 bg-slate-100 border-slate-200',
    居后: 'text-slate-500 bg-slate-50 border-slate-200',
    尾部: 'text-slate-400 bg-slate-50 border-slate-200',
    '—': 'text-slate-400 bg-slate-50 border-slate-200 border-dashed',
};

/**
 * 周期下拉的兜底项：仅在该市场模型一条周期都没记录时使用。
 * 正常情况下选项由 `ip.horizonOptions` 从**模型真实训练周期**生成 —— 早先这份
 * 写死的 T+1/T+3/T+5/T+10 是无源之水：选了 T+3 而后端一个 T+3 模型都没有，
 * 分数纹丝不动，用户却以为换了周期。
 */
const HORIZON_FALLBACK = [{ label: '周期未记录', value: 5 }];

/** 周期标签：T+1 说「次日」，其余说「N 日」，并带出该周期有多少模型 */
export function horizonLabel(horizon: number, modelCount: number): string {
  const span = horizon === 1 ? '次日' : `${horizon} 日`;
  return `T+${horizon} ${span} · ${modelCount} 个模型`;
}

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

  /**
   * 研究评分徽标：显示该标的在**同一模型、同一交易日**截面内的分位（0–100）。
   *
   * 这里原本是「强烈看多／偏多研判／中性观望／看空警示」四档方向结论。方向性结论
   * 是投资建议的形态，开源发行版一律改为中性的**位置**描述——只回答「今天排在
   * 前面还是后面」，回答「该不该买」是使用者自己的事。
   *
   * 配色按档位单色深浅，**不用红绿**：红绿本身就在暗示涨跌方向，等于把刚去掉的
   * 方向结论从颜色里又加回来。
   */
  const researchScoreBadge = (rankPct: number | null | undefined) => {
    const score = researchScore(rankPct);
    const band = researchScoreBand(score);
    const tone = RESEARCH_SCORE_TONE[band] ?? RESEARCH_SCORE_TONE['居中'];
    return (
      <span
        className={`flex items-center gap-1.5 border px-2 py-0.5 rounded-md font-black text-[11px] ${tone}`}
        title={RESEARCH_SCORE_HINT}
      >
        <span className="w-1.5 h-1.5 rounded-full bg-current opacity-70" />
        研究评分 {formatResearchScore(score)}
        {score === null ? '' : ` · ${band}`}
      </span>
    );
  };

  /**
   * 模型信号分数的字色。**固定中性色，不按涨跌着色。**
   *
   * 原实现是 `expected_return >= 0 ? 红 : 绿` —— 按预期收益的正负上色。A 股红涨绿跌，
   * 这个着色等于把「模型认为它会涨」直接翻译成红字：同一句话换个通道又说了一遍，
   * 而且比文字更难察觉。研究评分徽标去掉红绿是同一个理由，两处要一致。
   */
  const SCORE_TEXT_TONE = 'text-slate-700';
  const coverage = prediction && prediction.confidence > 0
    ? `${(prediction.confidence * 100).toFixed(1)}%`
    : '—';
  const ic = ip.selectedModel?.accuracy;

  /**
   * 共识点名守卫。`maxCount` 只挡下拉里的点选，挡不住程序化写入与
   * 「先点满再切市场」这类边界；上限同时是后端的 CPU 护栏，越界必须显式拦下
   * 而不是让请求跑到服务端才发现。
   */
  const handleConsensusChange = (ids: string[]) => {
    if (ids.length > CONSENSUS_PICK_LIMIT) {
      message.warning(`共识模型最多点 ${CONSENSUS_PICK_LIMIT} 个：每多一个都要现场跑一次完整推理`);
      return;
    }
    ip.setConsensusModelIds(ids);
  };

  const consensusCount = ip.consensusModelIds.length;

  return (
    <div className="flex-1 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden">
      {/* ── 配置工具条 ────────────────────────────────────────
          七个控件在 1440（Electron 最小宽）的 876px 里本来刚好放不下，
          「开始个股推理」会被挤到第二行、整条吃掉 89px。这里把两处弹性下限
          收到实际需要的最小值、间距收一档，换 1440 下单行（实测 49px）。 */}
      <div className="shrink-0 px-3 py-2 border-b border-slate-200 bg-slate-50/60 flex flex-wrap items-center gap-1.5">
        <div className="relative flex-1 min-w-[140px]">
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
          className="w-[168px] shrink-0"
          suffixIcon={<Clock size={12} className="text-indigo-500" />}
          title="预测周期取自该市场模型的真实训练周期（T+N）"
          options={
            ip.horizonOptions.length > 0
              ? ip.horizonOptions.map((o) => ({
                  value: o.horizon,
                  label: horizonLabel(o.horizon, o.modelCount),
                }))
              : HORIZON_FALLBACK
          }
        />

        <span className="w-px h-5 bg-slate-200 shrink-0" />

        <Select
          value={ip.modelId}
          onChange={ip.setModelId}
          size="small"
          className="flex-1 min-w-[130px]"
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

      {/* ── 共识点名条：多模型共识的唯一入口 ─────────────────────────
          日更批推理只跑「生效模型」一个，落库分数天然是每标的每日 1 行，
          所以「不点名」= 共识矩阵只有 1 个样本（后端会标注为样本过少）。
          点名后由前端传 consensus_model_ids，后端在 execute=true 时现场补算。 */}
      <div
        data-testid="consensus-picker"
        className="shrink-0 px-3 py-1.5 border-b border-slate-200 bg-violet-50/40 flex flex-wrap items-center gap-2"
      >
        <Tooltip title="点名参与横向共识的模型。执行推理时后端会为这些模型现场补算该标的分数（不写库、不发布），共识矩阵即由这些模型 + 主模型构成。">
          <span className="flex items-center gap-1 text-[10px] font-bold text-violet-700 shrink-0 cursor-help">
            <Layers size={12} className="text-violet-500" />
            共识点名
          </span>
        </Tooltip>

        <Select
          mode="multiple"
          size="small"
          maxCount={CONSENSUS_PICK_LIMIT}
          value={ip.consensusModelIds}
          onChange={handleConsensusChange}
          disabled={ip.models.length === 0}
          className="flex-1 min-w-[240px]"
          placeholder={
            ip.models.length === 0
              ? '该市场暂无可点名的模型'
              : `不选 = 只读当日已落库分数（通常仅 1 个模型）；最多点名 ${CONSENSUS_PICK_LIMIT} 个模型现场补算`
          }
          optionLabelProp="label"
          maxTagCount="responsive"
          options={ip.models.map((m) => ({
            value: m.modelId,
            label: m.modelName,
            display: (
              <div className="flex items-center justify-between gap-3">
                <span className="truncate">{m.modelName}</span>
                <span className="text-[10px] font-bold font-mono px-1.5 py-0.5 rounded bg-slate-100 border border-slate-300 text-slate-600 shrink-0">
                  {m.horizonDesc}
                </span>
              </div>
            ),
          }))}
          optionRender={(option) => option.data.display}
        />

        {/* 主模型恒参与共识，点名里重复选它不会重复计数（后端按 id 去重） */}
        {consensusCount > 0 && (
          <span
            data-testid="consensus-picker-count"
            className="shrink-0 flex items-center gap-1 text-[10px] font-bold font-mono text-violet-700 bg-white border border-violet-200 rounded px-1.5 py-0.5"
          >
            {consensusCount}/{CONSENSUS_PICK_LIMIT}
          </span>
        )}

        <span className="shrink-0 text-[10px] text-slate-500 leading-tight">
          {consensusCount > 0
            ? `点「开始个股推理」现场补算 ${consensusCount} 个模型（并发执行，数十秒起）`
            : '多模型共识需点名，否则只有主模型一个样本'}
        </span>

        {consensusCount > 0 && (
          <Button
            size="small"
            type="text"
            icon={<X size={11} />}
            onClick={() => ip.setConsensusModelIds([])}
            className="shrink-0 h-6 px-1.5 text-[10px] text-slate-500 hover:text-slate-800"
          >
            清空
          </Button>
        )}
      </div>

      {/* 基准日回退提示：所选日无数据时后端会回退，必须显式告知 */}
      {prediction?.as_of_date && ip.date && prediction.as_of_date !== ip.date.format('YYYY-MM-DD') && (
        <div className="shrink-0 px-3 py-1 bg-amber-50 border-b border-amber-100 text-[10px] text-amber-700 font-semibold">
          实际数据日：{prediction.as_of_date}（所选日期无数据已回退）
        </div>
      )}

      {/* 周期未落实提示：请求周期该市场无模型时后端会改用最近周期，分数口径随之改变 */}
      {prediction?.horizon_warning && (
        <div className="shrink-0 px-3 py-1.5 bg-rose-50 border-b border-rose-100 text-[10px] text-rose-700 font-semibold leading-relaxed">
          {prediction.horizon_warning}
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
            {/* 标签带出周期：分数是「T+N 的模型输出」，脱离周期谈分数没有意义 */}
            <QuoteCell label={`模型信号分数 · T+${prediction.horizon ?? ip.horizon}`}>
              <span className={SCORE_TEXT_TONE}>{prediction.predicted_score.toFixed(4)}</span>
            </QuoteCell>
          </div>

          <div className="ml-auto flex items-center gap-2 shrink-0 flex-wrap">
            {researchScoreBadge(prediction.rank_pct)}
            <Tooltip title={prediction.model_name || ip.selectedModel?.modelName || '—'}>
              {/* 模型名动辄「ML7 solo · nativeftt · 2016-24/2025val/2026test_CN」这种
                  三段式，200px 只露出前两段，最关键的验证窗口被截掉。放宽到 340px，
                  表述仍由外层 flex-wrap 兜底（窄屏换行而不是挤压其它字段）。 */}
              <span className="flex items-center gap-1.5 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded-md max-w-[340px]">
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

            {/* 上排：K 线自适应 + 指标定宽侧栏。
                刻意**不用**按比例的分栏网格 —— 指标卡的内容高度是恒定的，按比例发宽度
                会在超宽屏把它拉成一大片空白（2560 下实测 781px 宽、约 200px 高的空档），
                而 K 线图真正缺的是宽与高。宽度增量全部给图，侧栏钉一个够放内容的定宽。
                高度随视口走（clamp），短窗口不矮于原值，高窗口才长得起来。 */}
            <div
              className="flex flex-col xl:flex-row gap-3 shrink-0"
              style={{ height: 'clamp(340px, 42vh, 520px)' }}
            >
              <div className="flex-1 min-w-0 min-h-0 bg-white rounded-xl border border-slate-200 flex flex-col overflow-hidden">
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

              {/* 指标卡：分数已在报价条出现过，这里只放它没有的（分位区间 / 校准 / IC）。
                  定宽（340px）而非按比例 —— 内容宽度需求是恒定的，多给的宽度只会变成留白。 */}
              <div className="w-full xl:w-[340px] xl:shrink-0 min-h-0 bg-white rounded-xl border border-slate-200 flex flex-col">
                <div className="shrink-0 flex items-center justify-between px-3 h-9 border-b border-slate-100">
                  <span className="text-[11px] font-bold text-slate-700">模型推理指标</span>
                  <Tooltip title="分数取自落库的模型推理结果（Persisted Model Score）">
                    <Sparkles size={12} className="text-slate-400 cursor-help" />
                  </Tooltip>
                </div>

                <div className="flex-1 min-h-0 p-3 flex flex-col gap-3">
                  {/* 区间口径：模型分位头 vs 已实现波动率推算的锥体。二者可信度不同，
                      标题与角标必须随 forecast_basis 切换——把波动率锥渲染成
                      「模型分位预测」是口径造假，会把统计假设当成模型的判断力。 */}
                  <div className="p-3 bg-slate-50 rounded-lg border border-slate-200">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-[11px] font-bold text-slate-600">
                        {prediction.forecast_basis === 'realized_vol'
                          ? '波动率锥区间'
                          : prediction.p10_return != null
                            ? '分位数区间'
                            : '收益区间'}
                      </span>
                      <span
                        className={clsx(
                          'text-[10px] px-1.5 py-0.5 rounded border font-bold',
                          prediction.forecast_basis === 'realized_vol'
                            ? 'text-slate-500 bg-white border-slate-200'
                            : 'text-indigo-600 bg-indigo-50 border-indigo-100',
                        )}
                      >
                        {prediction.forecast_basis === 'realized_vol'
                          ? '统计口径 · 非模型分位'
                          : '验证集校准'}
                      </span>
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
                        <p className="mt-2.5 pt-2 border-t border-slate-200 text-[10px] text-slate-500 m-0 leading-relaxed">
                          区间覆盖率 <strong className="font-mono text-slate-700">{coverage}</strong>
                          {prediction.daily_vol_pct != null && prediction.forecast_basis === 'realized_vol' && (
                            <>
                              {' · '}日波动 <strong className="font-mono text-slate-700">{(prediction.daily_vol_pct * 100).toFixed(2)}%</strong>
                            </>
                          )}
                        </p>
                      </>
                    ) : (
                      <p className="text-[11px] leading-relaxed text-slate-500 m-0">
                        该模型未启用分位推理；当前仅提供真实信号分数。
                      </p>
                    )}
                    {prediction.forecast_note && (
                      <p className="mt-2 pt-2 border-t border-slate-200 text-[10px] text-slate-500 m-0 leading-relaxed">
                        {prediction.forecast_note}
                      </p>
                    )}
                  </div>

                  {/* 两个指标格吃掉剩余高度（原先 `mt-auto` 只贴底，中间留出一条死白）。
                      排成**上下两行**而不是左右两列：卡宽固定 340，两列会把格子拉成两个
                      又高又空的方框；横向两行则随高度自然长高，标签左、数值右，一眼扫完。 */}
                  {/* `auto-rows-fr` 不能省：只写 flex-1 的话栅格行仍是 auto 高，
                      两行会挤在顶部、把剩余高度全留成底部一段死白。 */}
                  <div className="flex-1 grid grid-cols-1 auto-rows-fr gap-2 min-h-[76px]">
                    <div className="px-3 bg-slate-50 rounded-lg border border-slate-200 flex items-center justify-between gap-2">
                      <span className="text-[11px] text-slate-500 font-semibold">模型准确率 (IC)</span>
                      <span className="font-mono text-xl font-black text-slate-700 leading-none">
                        {ic != null && ic !== 0 ? (typeof ic === 'number' ? ic.toFixed(3) : ic) : '—'}
                      </span>
                    </div>
                    <div className="px-3 bg-slate-50 rounded-lg border border-slate-200 flex items-center justify-between gap-2">
                      <span className="text-[11px] text-slate-500 font-semibold">区间覆盖率</span>
                      <span className="font-mono text-xl font-black text-slate-700 leading-none">{coverage}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* 多模型分数曲线：与下方归因/共识是不同维度，并存而非二选一。
                面板内是横向滚动、纵向 hidden，高度不够时下面的模型卡会被**裁掉**而不是
                出现滚动条 —— 所以这里的下限必须真的够放一张小卡：
                标题 49 + 覆盖度横幅 41 + 小卡(标题 28 + 曲线 + 页脚 29) + 内边距 40。
                原 250px 给不出这些（1080 下 28vh=302，小卡只剩 146，实测裁掉 79px：
                页脚整条 + 曲线负值段一起消失）。取 330 起步，曲线才有 ~160px。 */}
            <div
              className="bg-white rounded-xl border border-slate-200 overflow-hidden flex-none"
              style={{ height: 'clamp(330px, 32vh, 460px)' }}
            >
              <ModelScoreCurveGrid
                consensus={prediction.consensus}
                consensusScore={prediction.consensus_score}
                coverage={prediction.consensus_coverage}
                coverageNote={prediction.consensus_note}
                selectedCount={consensusCount}
                suffixSymbol={toSuffixSymbol(prediction.symbol || ip.symbol)}
                asOfDate={prediction.as_of_date}
              />
            </div>

            <div
              className="grid grid-cols-1 lg:grid-cols-2 gap-3 flex-none"
              style={{ minHeight: 'clamp(240px, 26vh, 360px)' }}
            >
              <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                <FeatureDriversPanel
                  drivers={prediction.drivers}
                  source={prediction.drivers_source}
                  note={prediction.drivers_note}
                  modelName={prediction.drivers_model_name}
                />
              </div>
              <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                <ModelConsensusPanel
                  consensus={prediction.consensus}
                  consensusScore={prediction.consensus_score}
                  coverage={prediction.consensus_coverage}
                  coverageNote={prediction.consensus_note}
                  selectedCount={consensusCount}
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
