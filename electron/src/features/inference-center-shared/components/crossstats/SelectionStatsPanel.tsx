/**
 * 截面排名 · 单模型 Top50 历史统计（测试集）。
 *
 * 用户口径（2026-10-21）：
 * - 用**测试集**（pred.parquet 的 split=test）评估单个模型的选股能力；
 * - 两种换股节奏：按模型训练周期换股（T+H）或每日滚动（T+1 每天都换一批）；
 * - 与组合选股「周期收益」同款统计：总收益、收益金额、最大回撤、胜率、月度拆解；
 * - 收盘价入选/退出，不计手续费、印花税与滑点。
 *
 * 展示面合规：只说「入选 / 退出 / 换股 / 持有」，不出现方向性交易字眼；
 * 红涨绿跌（A 股口径）；缺失值一律 `—`，绝不显示成 0。
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  DatePicker,
  Empty,
  InputNumber,
  Segmented,
  Spin,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import { Calculator, RefreshCw } from 'lucide-react';
import type {
  SelectionStatsCohort,
  SelectionStatsMonth,
  SelectionStatsQuery,
} from '../../../../services/selectionStatsTypes';
import {
  formatAmount,
  formatReturnPct,
  returnToneClass,
} from '../combo/comboRankingModel';
import {
  buildEquityGeometry,
  formatPct,
  formatSignedAmount,
  formatWinRate,
} from '../combo/periodReturnModel';
import { useSelectionStats } from '../../hooks/useSelectionStats';
import {
  SELECTION_QUICK_RANGES,
  cohortNote,
  coverageToneClass,
  selectionConventionLabel,
  selectionHoldModeOf,
  selectionQuickQuery,
  selectionRangeLabel,
  selectionWarningLabel,
  type SelectionHoldMode,
  type SelectionQuickRange,
} from './selectionStatsModel';

const { RangePicker } = DatePicker;

const DEFAULT_TOP_K = 50;
const MAX_TOP_K = 50;
const MIN_TOP_K = 1;
const DEFAULT_CAPITAL_WAN = 1000;
const EQUITY_WIDTH = 640;
const EQUITY_HEIGHT = 160;

function clampTopK(value: number | null | undefined): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return DEFAULT_TOP_K;
  return Math.min(MAX_TOP_K, Math.max(MIN_TOP_K, Math.round(value)));
}

export interface SelectionStatsPanelProps {
  modelId: string | null;
  /** 模型训练周期（metadata.target_horizon_days），用于「按模型周期换股」 */
  horizonDays: number;
  market: string;
  marketLabel?: string;
  /** 推理中心「排除黑名单」开关：历史统计同样按黑名单剔除入选标的 */
  excludeBlacklist?: boolean;
}

export const SelectionStatsPanel: React.FC<SelectionStatsPanelProps> = ({
  modelId,
  horizonDays,
  market,
  marketLabel,
  excludeBlacklist = false,
}) => {
  // 非 A 股市场直接不给 hook 模型，避免明知不支持还发请求
  const isSupportedMarket = !market || market === 'CN';
  const state = useSelectionStats(isSupportedMarket ? modelId : null, excludeBlacklist);
  const [mode, setMode] = useState<SelectionHoldMode>('period');
  const [topK, setTopK] = useState<number | null>(DEFAULT_TOP_K);
  const [capitalWan, setCapitalWan] = useState<number | null>(DEFAULT_CAPITAL_WAN);
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);

  // 换模型：表单回到默认值（持有期取新模型周期）
  useEffect(() => {
    setMode('period');
    setTopK(DEFAULT_TOP_K);
    setCapitalWan(DEFAULT_CAPITAL_WAN);
    setRange(null);
  }, [modelId]);

  // 新结果落地：表单回显后端实际采用的参数（区间被夹取后也一致）
  useEffect(() => {
    if (!state.stats) return;
    setMode(selectionHoldModeOf(state.stats));
    setTopK(state.stats.requested.top_k);
    setCapitalWan(state.stats.requested.capital / 10000);
    setRange([
      dayjs(state.stats.requested.start ?? state.stats.effective.start),
      dayjs(state.stats.requested.end ?? state.stats.effective.end),
    ]);
  }, [state.stats]);

  const buildQuery = (): SelectionStatsQuery => {
    const query: SelectionStatsQuery = {};
    if (range?.[0]) query.start = range[0].format('YYYY-MM-DD');
    if (range?.[1]) query.end = range[1].format('YYYY-MM-DD');
    query.top_k = clampTopK(topK);
    query.hold_days = mode === 'daily' ? 1 : Math.max(1, horizonDays || 1);
    if (capitalWan && Number.isFinite(capitalWan) && capitalWan > 0) {
      query.capital = capitalWan * 10000;
    }
    return query;
  };

  const handleCalculate = () => state.load(buildQuery());

  const handleQuick = (kind: SelectionQuickRange) => {
    const quick = selectionQuickQuery(kind, state.bounds);
    if (quick.start && quick.end) {
      setRange([dayjs(quick.start), dayjs(quick.end)]);
    }
    state.load({ ...buildQuery(), ...quick });
  };

  const stats = state.stats;
  const summary = stats?.summary ?? null;
  const geometry = useMemo(
    () =>
      stats && summary
        ? buildEquityGeometry(
            stats.points,
            summary.initial_capital,
            EQUITY_WIDTH,
            EQUITY_HEIGHT,
          )
        : null,
    [stats, summary],
  );

  const monthColumns: ColumnsType<SelectionStatsMonth> = [
    {
      title: '月份',
      dataIndex: 'month',
      width: 100,
      render: (value: string, row) => (
        <span className="flex items-center gap-1">
          <span className="font-mono text-[11px]">{value}</span>
          {row.partial && <Tag className="!m-0 !text-[9px] !leading-tight">不完整</Tag>}
        </span>
      ),
    },
    {
      title: '月收益',
      dataIndex: 'return_pct',
      width: 92,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono font-bold ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '收益金额',
      dataIndex: 'profit_amount',
      width: 110,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatSignedAmount(value)}
        </span>
      ),
    },
    {
      title: '沪深300',
      dataIndex: 'benchmark_return_pct',
      width: 92,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '超额',
      dataIndex: 'excess_return_pct',
      width: 92,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '区间',
      key: 'span',
      width: 168,
      render: (_value, row) => (
        <span className="font-mono text-[10px] text-slate-500">
          {row.start_date} ~ {row.end_date}
        </span>
      ),
    },
    {
      title: '期数',
      dataIndex: 'cohorts',
      width: 64,
      align: 'right',
      render: (value: number) => <span className="font-mono text-[11px]">{value}</span>,
    },
    {
      title: '期胜率',
      dataIndex: 'win_rate_pct',
      width: 80,
      align: 'right',
      render: (value: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">{formatWinRate(value)}</span>
      ),
    },
  ];

  const cohortColumns: ColumnsType<SelectionStatsCohort> = [
    {
      title: '期',
      dataIndex: 'index',
      width: 52,
      align: 'right',
      render: (value: number) => <span className="font-mono text-[11px]">{value}</span>,
    },
    {
      title: '信号日',
      dataIndex: 'signal_date',
      width: 100,
      render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
    },
    {
      title: '入选日',
      dataIndex: 'entry_date',
      width: 100,
      render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
    },
    {
      title: '退出日',
      dataIndex: 'exit_date',
      width: 118,
      render: (value: string | null, row) => (
        <span className="font-mono text-[10px]">
          {value ?? row.mark_date}
          {row.exit_kind === 'open' && (
            <Tooltip title="区间末尾未到期，按最后有效收盘价估值">
              <span className="ml-1 text-amber-600">未到期</span>
            </Tooltip>
          )}
        </span>
      ),
    },
    {
      title: '有效入选',
      key: 'priced',
      width: 92,
      align: 'right',
      render: (_value, row) => (
        <span className="font-mono text-[11px]">
          {row.priced}/{row.held}
        </span>
      ),
    },
    {
      title: '期收益',
      dataIndex: 'return_pct',
      width: 92,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono font-bold ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '沪深300',
      dataIndex: 'benchmark_return_pct',
      width: 92,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '超额',
      dataIndex: 'excess_return_pct',
      width: 92,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '期末权益',
      dataIndex: 'equity',
      width: 104,
      align: 'right',
      render: (value: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">{formatAmount(value)}</span>
      ),
    },
    {
      title: '备注',
      key: 'note',
      width: 190,
      render: (_value, row) => (
        <span className="text-[10px] text-slate-500">{cohortNote(row)}</span>
      ),
    },
  ];

  if (!isSupportedMarket) {
    return (
      <div
        className="h-full flex items-center justify-center bg-white border border-slate-200 rounded-xl"
        data-testid="selection-stats-unsupported"
      >
        <Empty
          description={`单模型 Top50 历史统计当前仅支持 A 股模型（${marketLabel ?? market} 暂未开放）`}
        />
      </div>
    );
  }

  if (!modelId) {
    return (
      <div className="h-full flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Empty description="先在上方选择模型，再统计其测试集历史排名能力" />
      </div>
    );
  }

  if (state.error && !stats) {
    return (
      <div className="p-3" data-testid="selection-stats-error">
        <Alert
          type="error"
          showIcon
          message="历史统计计算失败"
          description={<span className="break-all">{state.error}</span>}
          action={
            <Button size="small" loading={state.loading} onClick={state.reload}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (state.loading && !stats) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="读取测试集评分与前复权收盘价，滚动计算历史排名…" />
      </div>
    );
  }

  return (
    <div
      className="h-full min-h-0 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden"
      data-testid="selection-stats-root"
    >
      <div className="shrink-0 flex flex-wrap items-center gap-2 px-3 py-2 border-b border-slate-100">
        <Segmented<SelectionHoldMode>
          size="small"
          value={mode}
          onChange={(value) => setMode(value)}
          options={[
            {
              value: 'period',
              label: `按模型周期 T+${Math.max(1, horizonDays || 1)}`,
            },
            { value: 'daily', label: '每日滚动 T+1' },
          ]}
          data-testid="selection-stats-mode"
        />
        <span className="text-[11px] text-slate-500">每期入选</span>
        <InputNumber
          size="small"
          className="!w-20"
          min={MIN_TOP_K}
          max={MAX_TOP_K}
          value={topK}
          onChange={(value) => setTopK(typeof value === 'number' ? value : null)}
          suffix="只"
          data-testid="selection-stats-topk"
        />
        <span className="text-[11px] text-slate-500">统计区间</span>
        <RangePicker
          size="small"
          className="!w-60"
          value={range}
          onChange={(value) => setRange(value as [Dayjs | null, Dayjs | null] | null)}
          allowEmpty={[true, true]}
          placeholder={['起始日', '截止日']}
        />
        <span className="text-[11px] text-slate-500">初始资金（万）</span>
        <InputNumber
          size="small"
          className="!w-24"
          min={1}
          step={100}
          value={capitalWan}
          onChange={(value) => setCapitalWan(typeof value === 'number' ? value : null)}
          suffix="万"
          data-testid="selection-stats-capital"
        />
        <Button
          size="small"
          type="primary"
          icon={<Calculator size={13} />}
          loading={state.loading}
          onClick={handleCalculate}
          data-testid="selection-stats-calculate"
        >
          计算
        </Button>
        <div className="flex items-center gap-1">
          {SELECTION_QUICK_RANGES.map((item) => (
            <Button
              key={item.key}
              size="small"
              type="text"
              className="!px-1.5 !text-[11px]"
              disabled={state.loading || !state.bounds}
              onClick={() => handleQuick(item.key)}
              data-testid={`selection-stats-quick-${item.key}`}
            >
              {item.label}
            </Button>
          ))}
        </div>
        <Button
          size="small"
          icon={<RefreshCw size={13} />}
          loading={state.loading}
          onClick={state.reload}
          className="ml-auto"
        >
          刷新
        </Button>
      </div>

      {state.error && stats && (
        <div className="shrink-0 px-3 pt-2" data-testid="selection-stats-inline-error">
          <Alert
            type="error"
            showIcon
            message={<span className="break-all">{state.error}</span>}
            action={
              <Button size="small" loading={state.loading} onClick={state.reload}>
                重试
              </Button>
            }
          />
        </div>
      )}

      {stats && summary && (
        <div
          className="shrink-0 flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 border-b border-slate-100 bg-slate-50/70 text-[11px] text-slate-500"
          data-testid="selection-stats-summary"
        >
          <span className="flex items-baseline gap-1">
            <span className="font-bold text-slate-600">总收益</span>
            <span
              className={`font-mono text-lg font-black ${returnToneClass(summary.total_return_pct)}`}
              data-testid="selection-stats-total-return"
            >
              {formatReturnPct(summary.total_return_pct)}
            </span>
          </span>
          <span className="flex items-baseline gap-1">
            <span>收益金额</span>
            <strong
              className={`font-mono text-sm ${returnToneClass(summary.profit_amount)}`}
              data-testid="selection-stats-profit"
            >
              {formatSignedAmount(summary.profit_amount)}
            </strong>
          </span>
          <span>
            期末权益{' '}
            <strong className="font-mono text-slate-700">
              {formatAmount(summary.final_equity)}
            </strong>
          </span>
          <span>
            最大回撤{' '}
            <strong className={`font-mono ${returnToneClass(summary.max_drawdown_pct)}`}>
              {formatPct(summary.max_drawdown_pct)}
            </strong>
          </span>
          <span>
            沪深300{' '}
            <strong className={`font-mono ${returnToneClass(summary.benchmark_return_pct)}`}>
              {formatReturnPct(summary.benchmark_return_pct)}
            </strong>
          </span>
          <span>
            超额{' '}
            <strong className={`font-mono ${returnToneClass(summary.excess_return_pct)}`}>
              {formatReturnPct(summary.excess_return_pct)}
            </strong>
          </span>
          <span>
            期胜率{' '}
            <strong className="font-mono text-slate-700">
              {formatWinRate(summary.cohort_win_rate_pct)}
            </strong>
          </span>
          <span>
            已了结{' '}
            <strong className="font-mono text-slate-700">{summary.closed_cohorts}</strong> 期 ·
            未到期 <strong className="font-mono text-slate-700">{summary.open_cohorts}</strong> 期
          </span>
          <span>
            单只胜率{' '}
            <strong className="font-mono text-slate-700">
              {formatWinRate(summary.pick_win_rate_pct)}
            </strong>
          </span>
          <span>
            报价覆盖{' '}
            <strong className={`font-mono ${coverageToneClass(summary.price_coverage_pct)}`}>
              {formatWinRate(summary.price_coverage_pct)}
            </strong>
          </span>
          <Tag
            color={summary.data_quality === 'complete' ? 'green' : 'orange'}
            className="!m-0 !text-[10px]"
            data-testid="selection-stats-quality"
          >
            {summary.data_quality === 'complete'
              ? '数据完整'
              : summary.data_quality === 'partial'
                ? '数据不完整'
                : '数据不可用'}
          </Tag>
        </div>
      )}

      {stats && stats.warnings.length > 0 && (
        <div className="shrink-0 px-3 pt-2" data-testid="selection-stats-warnings">
          <Alert
            type="warning"
            showIcon
            message={stats.warnings.map(selectionWarningLabel).join('；')}
          />
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-auto overscroll-contain p-3 space-y-3">
        {!stats || !summary ? (
          <div className="h-full flex items-center justify-center">
            <Empty description="选好换股节奏与区间后点「计算」，统计该模型测试集的历史排名能力" />
          </div>
        ) : (
          <>
            <div className="text-[10px] text-slate-400" data-testid="selection-stats-range">
              {selectionRangeLabel(stats)} · 信号日 {stats.effective.signal_days} · 持有区间{' '}
              {stats.effective.trading_days} 个交易日 · {selectionConventionLabel(stats)}
            </div>

            {geometry && (
              <div
                className="rounded-lg border border-slate-200 bg-white px-2 py-1"
                data-testid="selection-stats-chart"
              >
                <div className="flex items-center justify-between px-1 text-[10px] text-slate-400">
                  <span>区间权益（初始 {formatAmount(summary.initial_capital)}）</span>
                  <span className="font-mono">
                    {formatAmount(geometry.minEquity)} ~ {formatAmount(geometry.maxEquity)}
                  </span>
                </div>
                <svg
                  viewBox={`0 0 ${EQUITY_WIDTH} ${EQUITY_HEIGHT}`}
                  preserveAspectRatio="none"
                  className="w-full h-40"
                  role="img"
                  aria-label="区间权益曲线"
                >
                  <line
                    x1={0}
                    x2={EQUITY_WIDTH}
                    y1={geometry.baselineY}
                    y2={geometry.baselineY}
                    stroke="#cbd5e1"
                    strokeDasharray="4 4"
                    strokeWidth={1}
                  />
                  <path d={geometry.areaPath} fill="#dbeafe" opacity={0.6} />
                  <path
                    d={geometry.linePath}
                    fill="none"
                    stroke="#2563eb"
                    strokeWidth={1.6}
                    vectorEffect="non-scaling-stroke"
                  />
                </svg>
              </div>
            )}

            <div data-testid="selection-stats-months">
              <div className="mb-1 text-[11px] font-bold text-slate-600">月度拆解</div>
              <Table<SelectionStatsMonth>
                size="small"
                rowKey="month"
                columns={monthColumns}
                dataSource={stats.months}
                pagination={false}
                scroll={{ x: 800 }}
              />
            </div>

            <Collapse
              size="small"
              data-testid="selection-stats-details"
              items={[
                {
                  key: 'cohorts',
                  label: `各期明细（${stats.cohorts.length} 期 · 每期等权持有 ${stats.requested.hold_days} 日）`,
                  children: (
                    <Table<SelectionStatsCohort>
                      size="small"
                      rowKey="index"
                      columns={cohortColumns}
                      dataSource={stats.cohorts}
                      pagination={{ pageSize: 20, hideOnSinglePage: true }}
                      scroll={{ x: 1020 }}
                      data-testid="selection-stats-cohorts"
                    />
                  ),
                },
              ]}
            />
          </>
        )}
      </div>

      <div className="shrink-0 px-3 py-1.5 border-t border-slate-100 text-[10px] text-slate-400">
        口径：测试集每日按模型分数取前 {stats?.requested.top_k ?? DEFAULT_TOP_K} 只，T+
        {stats?.params.execution_lag_days ?? 1} 收盘入选，持有{' '}
        {stats?.requested.hold_days ?? Math.max(1, horizonDays || 1)}{' '}
        个交易日收盘退出（每日滚动 = 1 日）；每期全部权益等权投入，期与期首尾相接复利滚动。前复权收盘价，
        不计手续费、印花税与滑点，不建模停牌/涨跌停等不可交易状态；缺失价格不按 0 计。
        结果用于评估模型历史排名能力，不构成任何投资建议。
      </div>
    </div>
  );
};

export default SelectionStatsPanel;
