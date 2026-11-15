/**
 * 组合选股 · 滚动建仓视图。
 *
 * 横向 N+1 列（D-N … 今日），每列 `per_day` 只，默认 11×5 = 55 格（窗口内去重约 50）。
 * 色块只表达「新进 / 留存 / 退出」，**刻意不用红绿**——红绿在 A 股是涨跌方向，
 * 与「是否新进」无关，混用会把方向暗示从颜色里加回来。涨跌幅与当日盈亏
 * 只出现在**文字与数字**上（红涨绿跌），不参与卡片底色。
 *
 * 每个格子（2026-10-21 追加）：当日涨跌幅、当日盈亏（仅当日实际持有）、
 * 窗口内在榜天数 / 首次入选至今 / 次日收益均值与胜率（hover 给完整统计）。
 * 每列头部（同日追加）：沪深300 当日涨跌幅、收盘、相对 20 日线位置。
 *
 * 口径：入选日收盘按等权预算建仓（受 ST/退市与成交额门槛约束，跳过不替补）
 * → 次一交易日收盘结算；最后一批只估值不结算。
 * 退出只在「昨列 → 今日」标注一次（后端已限定），这里不做二次推算。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Collapse, Empty, InputNumber, Spin, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { RefreshCw } from 'lucide-react';
import type {
  BlendRollingBatch,
  BlendRollingDaily,
  BlendRollingPick,
  BlendRollingResponse,
} from '../../../../services/blendTypes';
import { formatResearchScore, researchScore } from '../../../shared/researchScore';
import { positionTagClass } from '../../positionLabels';
import {
  ROLLING_TONE,
  exitLabel,
  exitTradeLabel,
  formatAmount,
  formatReturnPct,
  positionLabel,
  returnToneClass,
  rollingHeldLabel,
  rollingStatus,
  scrollGeometry,
  type ScrollGeometry,
} from './comboRankingModel';
import { blendWarningLabel } from './portfolioMetricsModel';
import {
  marketChangeLabel,
  marketDayLabel,
  marketPositionLabel,
} from './portfolioMetricsModel';
import { formatPct, formatSignedAmount, formatWinRate } from './periodReturnModel';
import { DataSourceBadge, PortfolioMetricsStrip } from './PortfolioMetricsStrip';
import { buildRollingMetricSections } from './rollingMetricsSections';
import './rollingBoard.css';

const HIDDEN_SCROLLBAR: ScrollGeometry = {
  visible: false,
  thumbWidth: 0,
  thumbLeft: 0,
  progress: 0,
};

const DEFAULT_CAPITAL = 10_000_000;
const MIN_CAPITAL_WAN = 1;
const MAX_CAPITAL_WAN = 1_000_000;
/** 统计条折叠状态（按面板区分：周期收益面板用另一把键） */
const METRICS_COLLAPSED_KEY = 'qm:combo:rolling:metrics-collapsed';

/** 告警条只展示前 3 条；可交易性硬条件缺席优先露出，不能被成员级告警挤掉 */
const PRIORITY_WARNING_CODES = new Set([
  'tradability_unavailable',
  'st_filter_unavailable',
  'st_filter_static',
]);

function orderedWarnings(warnings: string[]): string[] {
  const priority: string[] = [];
  const rest: string[] = [];
  for (const warning of warnings) {
    (PRIORITY_WARNING_CODES.has(warning) ? priority : rest).push(warning);
  }
  return [...priority, ...rest];
}

/**
 * 建仓状态文案：展示面只说「建仓 / 未建仓」，不出现买卖字眼。
 * 后端新增状态码未登记时降级为「未建仓」，不吞成空白。
 */
const BUY_STATUS_TEXT: Record<string, string> = {
  bought: '建仓',
  next_day: '次日建仓',
  skip_st: '未建仓 · ST/退市',
  skip_low_adv: '未建仓 · 成交额不足',
  skip_no_amount: '未建仓 · 缺成交额',
  skip_no_price: '未建仓 · 缺收盘价',
};

/** 建仓状态配色：只用蓝/靛/灰，不借用红绿（红绿在 A 股是涨跌方向） */
const BUY_STATUS_TONE: Record<string, string> = {
  bought: 'border-indigo-200 bg-indigo-50 text-indigo-700',
  next_day: 'border-sky-200 bg-sky-50 text-sky-700',
};
const BUY_STATUS_SKIP_TONE = 'border-slate-200 bg-slate-100 text-slate-500';

function buyStatusLabel(pick: BlendRollingPick): string | null {
  const status = pick.buy_status;
  if (!status) return null;
  if (status === 'bought') {
    const weight = pick.entry_weight_pct;
    return typeof weight === 'number' && Number.isFinite(weight)
      ? `建仓 ${weight.toFixed(1)}%`
      : '建仓';
  }
  return BUY_STATUS_TEXT[status] ?? '未建仓';
}

function buyStatusTone(pick: BlendRollingPick): string {
  return BUY_STATUS_TONE[String(pick.buy_status ?? '')] ?? BUY_STATUS_SKIP_TONE;
}

/** 建仓状态 hover 说明（跳过原因 + ADV） */
function buyStatusHint(pick: BlendRollingPick): string {
  const adv =
    typeof pick.adv_yi === 'number' && Number.isFinite(pick.adv_yi)
      ? `ADV20 ${pick.adv_yi.toFixed(2)} 亿`
      : null;
  switch (pick.buy_status) {
    case 'bought':
      return ['按入选日收盘建仓', adv].filter(Boolean).join(' · ');
    case 'next_day':
      return '末日入选为下一批次候选：只给计划权重，本窗口不建仓';
    case 'skip_st':
      return 'ST/退市标的（按当前证券主表静态标记）不建仓，对应资金留作现金';
    case 'skip_low_adv':
      return ['ADV20 低于门槛，不建仓', adv].filter(Boolean).join(' · ');
    case 'skip_no_amount':
      return '缺 ADV20（有效成交额历史不足），不建仓';
    case 'skip_no_price':
      return '缺入选日收盘价，不建仓';
    default:
      return '本批次未建仓';
  }
}

/** 市值（亿元）→ 展示值；缺失返回 null（渲染 `—`，不写 0） */
function capLabel(marketCapYi: number | null | undefined): string | null {
  if (typeof marketCapYi !== 'number' || !Number.isFinite(marketCapYi)) return null;
  if (marketCapYi >= 10000) return `${(marketCapYi / 10000).toFixed(2)} 万亿`;
  return `${marketCapYi.toFixed(0)} 亿`;
}

/** 格子 hover：窗口内完整统计（缺失一律 `—`） */
function pickStatsLabel(pick: BlendRollingPick): string {
  const stats = pick.stats;
  const parts = [
    `${pick.name || pick.symbol} · 排名 ${pick.rank}`,
    `综合分 ${formatResearchScore(researchScore(pick.pct))}`,
    positionLabel(pick),
  ];
  if (stats) {
    parts.push(`在榜 ${stats.appearances ?? '—'} 日（连续 ${stats.streak_days ?? '—'}）`);
    if (stats.first_seen) parts.push(`首次 ${stats.first_seen}`);
    parts.push(`首次至今 ${formatReturnPct(stats.since_first_pct)}`);
    parts.push(
      `次日均值 ${formatReturnPct(stats.next_day_avg_pct)}（胜率 ${formatWinRate(
        stats.next_day_win_rate_pct,
      )} · ${stats.next_day_samples ?? '—'} 样本）`,
    );
    parts.push(`次日波动 ${formatReturnPct(stats.next_day_std_pct)}`);
    parts.push(`末日收盘 ${stats.latest_close ?? '—'}`);
  }
  parts.push(`当日涨跌 ${formatReturnPct(pick.change_pct)}`);
  if (pick.weight_pct !== null && pick.weight_pct !== undefined) {
    parts.push(`当日权重 ${pick.weight_pct.toFixed(2)}%`);
  }
  if (pick.day_pnl_amount !== null && pick.day_pnl_amount !== undefined) {
    parts.push(`当日盈亏 ${formatSignedAmount(pick.day_pnl_amount)}`);
  }
  const buyLabel = buyStatusLabel(pick);
  if (buyLabel) parts.push(`建仓状态 ${buyLabel}`);
  // 跳过者的 weight 恒为 0（不是「按 0% 建仓」），只在有计划权重时展示
  if (
    pick.buy_status === 'next_day' &&
    typeof pick.entry_weight_pct === 'number' &&
    Number.isFinite(pick.entry_weight_pct)
  ) {
    parts.push(`计划建仓比例 ${pick.entry_weight_pct.toFixed(2)}%`);
  }
  if (typeof pick.adv_yi === 'number' && Number.isFinite(pick.adv_yi)) {
    parts.push(`ADV20 ${pick.adv_yi.toFixed(2)} 亿`);
  }
  return parts.join(' · ');
}

export interface RollingBuildPanelProps {
  rolling: BlendRollingResponse | null;
  /** 批次本金（元）；改变后由上层重新请求 */
  capital: number;
  onCapitalCommit: (capital: number) => void;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

export const RollingBuildPanel: React.FC<RollingBuildPanelProps> = ({
  rolling,
  capital,
  onCapitalCommit,
  loading,
  error,
  onRetry,
}) => {
  // 常驻横向滚动条（覆盖式原生滚动条不滚动时不可见，见滚动条组件注释）
  const boardRef = useRef<HTMLDivElement | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [overflowX, setOverflowX] = useState(false);
  const [scrollbar, setScrollbar] = useState<ScrollGeometry>(HIDDEN_SCROLLBAR);

  // 本金输入：本地编辑，失焦/回车才提交（避免逐字符触发重新计算）
  const effectiveCapital = rolling?.capital ?? capital ?? DEFAULT_CAPITAL;
  const [capitalWan, setCapitalWan] = useState<number | null>(
    Math.round(effectiveCapital / 10000),
  );
  useEffect(() => {
    setCapitalWan(Math.round(effectiveCapital / 10000));
  }, [effectiveCapital]);

  const commitCapital = () => {
    const wan =
      typeof capitalWan === 'number' && Number.isFinite(capitalWan)
        ? Math.round(capitalWan)
        : 0;
    if (wan < MIN_CAPITAL_WAN) {
      setCapitalWan(Math.round(effectiveCapital / 10000));
      return;
    }
    const next = wan * 10000;
    if (next !== effectiveCapital) onCapitalCommit(next);
  };

  const syncScrollbar = useCallback(() => {
    const el = boardRef.current;
    if (!el) return;
    const hasOverflow = el.scrollWidth > el.clientWidth + 1;
    setOverflowX(hasOverflow);
    const next = hasOverflow
      ? scrollGeometry({
          clientWidth: el.clientWidth,
          scrollWidth: el.scrollWidth,
          scrollLeft: el.scrollLeft,
          trackWidth: trackRef.current?.clientWidth || el.clientWidth,
        })
      : HIDDEN_SCROLLBAR;
    // 纵向滚动也会触发 onScroll；几何没变就不重渲染整块看板
    setScrollbar((prev) =>
      prev.visible === next.visible &&
      Math.abs(prev.thumbWidth - next.thumbWidth) < 0.5 &&
      Math.abs(prev.thumbLeft - next.thumbLeft) < 0.5 &&
      Math.abs(prev.progress - next.progress) < 0.001
        ? prev
        : next,
    );
  }, []);

  useEffect(() => {
    const el = boardRef.current;
    if (!el) return;
    const frame = requestAnimationFrame(syncScrollbar);
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(syncScrollbar);
    observer?.observe(el);
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [syncScrollbar, rolling]);

  // 轨道在 overflowX 变 true 后才挂载，挂载后重新量一次宽度（首帧回退用看板宽度）
  useEffect(() => {
    if (!overflowX) return;
    const frame = requestAnimationFrame(syncScrollbar);
    return () => cancelAnimationFrame(frame);
  }, [overflowX, syncScrollbar]);

  const onTrackPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const el = boardRef.current;
    const track = trackRef.current;
    if (!el || !track) return;
    const maxScroll = el.scrollWidth - el.clientWidth;
    if (maxScroll <= 0) return;
    const rect = track.getBoundingClientRect();
    const travel = Math.max(1, rect.width - scrollbar.thumbWidth);
    // 点轨道空白处 = 把滑块中心挪到点击处；点滑块本身 = 直接拖
    if ((event.target as HTMLElement).dataset.role !== 'thumb') {
      const target = Math.min(
        1,
        Math.max(0, (event.clientX - rect.left - scrollbar.thumbWidth / 2) / travel),
      );
      el.scrollLeft = target * maxScroll;
      syncScrollbar();
    }
    const startX = event.clientX;
    const startScroll = el.scrollLeft;
    const move = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX;
      el.scrollLeft = Math.min(
        maxScroll,
        Math.max(0, startScroll + (delta / travel) * maxScroll),
      );
      syncScrollbar();
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      window.removeEventListener('pointercancel', stop);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
    window.addEventListener('pointercancel', stop);
    event.preventDefault();
  };

  const performance = rolling?.summary.performance;
  // filters 用于区分「数据源不可用」与「该市场不适用 / 手动关闭」
  const filters = rolling?.filters;
  const metricSections = useMemo(
    () =>
      performance
        ? buildRollingMetricSections(performance, filters, rolling?.benchmark?.name)
        : [],
    [performance, filters, rolling?.benchmark?.name],
  );
  // 折叠后仍保留一行关键数字：窗口收益 / 收益金额 / 建仓覆盖 / 期末现金
  const collapsedSummary = useMemo(() => {
    if (!performance) return undefined;
    const parts = [
      `窗口收益 ${formatReturnPct(performance.total_return_pct)}`,
      `收益金额 ${formatSignedAmount(performance.profit_amount)}`,
    ];
    if (
      typeof performance.evaluated_picks === 'number' &&
      Number.isFinite(performance.evaluated_picks) &&
      typeof performance.bought_positions === 'number' &&
      Number.isFinite(performance.bought_positions)
    ) {
      parts.push(`建仓 ${performance.bought_positions}/${performance.evaluated_picks}`);
    }
    parts.push(`期末现金 ${formatPct(performance.final_cash_pct)}`);
    return parts.join(' · ');
  }, [performance]);

  const batchColumns: ColumnsType<BlendRollingBatch> = useMemo(
    () => [
      {
        title: '建仓日',
        dataIndex: 'entry_date',
        width: 100,
        render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
      },
      {
        title: '结算日',
        dataIndex: 'exit_date',
        width: 100,
        render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
      },
      {
        title: '入选 / 有价',
        key: 'positions',
        width: 92,
        align: 'right',
        render: (_value, row) => (
          <span className="font-mono text-[11px]">
            {row.positions} / {row.priced}
          </span>
        ),
      },
      {
        title: '建仓',
        dataIndex: 'bought',
        width: 62,
        align: 'right',
        render: (value: number | undefined, row) => (
          <Tooltip title={`入选 ${row.positions} 只中实际建仓 ${value ?? '—'} 只；跳过的标的不替补`}>
            {/* 每行同 testid：断言请用 getAllByTestId / querySelectorAll，不要用单数 getByTestId */}
            <span className="font-mono text-[11px]" data-testid="combo-rolling-batch-bought">
              {value ?? '—'}
            </span>
          </Tooltip>
        ),
      },
      {
        title: '批次收益',
        dataIndex: 'return_pct',
        width: 90,
        align: 'right',
        render: (value: number | null) => (
          <span className={`font-mono font-bold ${returnToneClass(value)}`}>
            {formatReturnPct(value)}
          </span>
        ),
      },
      {
        title: '盈亏金额',
        dataIndex: 'pnl_amount',
        width: 104,
        align: 'right',
        render: (value: number | null) => (
          <span className={`font-mono ${returnToneClass(value)}`}>
            {formatSignedAmount(value)}
          </span>
        ),
      },
      {
        title: '批次权益',
        dataIndex: 'equity',
        width: 110,
        align: 'right',
        render: (value: number) => <span className="font-mono">{formatAmount(value)}</span>,
      },
      {
        title: '名义金额',
        dataIndex: 'notional_amount',
        width: 110,
        align: 'right',
        render: (value: number) => <span className="font-mono">{formatAmount(value)}</span>,
      },
      {
        title: '持有',
        dataIndex: 'holding_days',
        width: 62,
        align: 'right',
        render: (value: number) => <span className="font-mono text-[11px]">{value} 日</span>,
      },
      {
        title: '状态',
        dataIndex: 'settled',
        width: 78,
        render: (value: boolean) =>
          value ? (
            <Tag color="green" className="!m-0 !text-[9px]">
              已结算
            </Tag>
          ) : (
            <Tooltip title="最后一个批次只在末日收盘估值，计入浮动收益，不结算">
              <Tag color="blue" className="!m-0 !text-[9px]">
                浮动中
              </Tag>
            </Tooltip>
          ),
      },
    ],
    [],
  );

  const dailyColumns: ColumnsType<BlendRollingDaily> = useMemo(
    () => [
      {
        title: '建仓日',
        dataIndex: 'date',
        width: 100,
        render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
      },
      {
        title: '结算日',
        dataIndex: 'exit_date',
        width: 100,
        render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
      },
      {
        title: '当日收益',
        dataIndex: 'return_pct',
        width: 88,
        align: 'right',
        render: (value: number | null) => (
          <span className={`font-mono font-bold ${returnToneClass(value)}`}>
            {formatReturnPct(value)}
          </span>
        ),
      },
      {
        title: '权益',
        dataIndex: 'equity',
        width: 110,
        align: 'right',
        render: (value: number) => <span className="font-mono">{formatAmount(value)}</span>,
      },
      {
        title: '持仓市值',
        dataIndex: 'market_value',
        width: 110,
        align: 'right',
        render: (value: number) => <span className="font-mono">{formatAmount(value)}</span>,
      },
      {
        title: '现金',
        dataIndex: 'cash',
        width: 100,
        align: 'right',
        render: (value: number) => <span className="font-mono">{formatAmount(value)}</span>,
      },
      {
        title: '仓位',
        dataIndex: 'exposure_pct',
        width: 74,
        align: 'right',
        render: (value: number | null) => (
          <span className="font-mono text-[11px]">
            {value === null ? '—' : `${value.toFixed(1)}%`}
          </span>
        ),
      },
      {
        title: '基准',
        dataIndex: 'benchmark_return_pct',
        width: 80,
        align: 'right',
        render: (value: number | null) => (
          <span className="font-mono text-[11px] text-slate-600">{formatReturnPct(value)}</span>
        ),
      },
      {
        title: '超额',
        dataIndex: 'excess_return_pct',
        width: 84,
        align: 'right',
        render: (value: number | null) => (
          <span className={`font-mono text-[11px] font-bold ${returnToneClass(value)}`}>
            {formatReturnPct(value)}
          </span>
        ),
      },
      {
        title: '状态',
        dataIndex: 'settled',
        width: 78,
        render: (value: boolean) =>
          value ? (
            <span className="text-[10px] text-green-700">已结算</span>
          ) : (
            <span className="text-[10px] text-blue-700">浮动中</span>
          ),
      },
    ],
    [],
  );

  if (error) {
    return (
      <div className="p-3" data-testid="combo-rolling-error">
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (loading && !rolling) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="读取滚动窗口…" />
      </div>
    );
  }

  if (!rolling || rolling.columns.length === 0) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Empty description="该组合暂无滚动窗口数据" />
      </div>
    );
  }

  const memberCount = rolling.members.length;
  const latest = rolling.data_trade_date;
  const batches = rolling.batches ?? [];
  const daily = rolling.daily ?? [];
  const detailItems = [
    batches.length > 0
      ? {
          key: 'batches',
          label: `每日批次（${batches.length} 批 · 结算 ${
            performance?.settled_batches ?? batches.filter((row) => row.settled).length
          } · 浮动 ${
            performance?.open_batches ?? batches.filter((row) => !row.settled).length
          }）`,
          children: (
            <Table<BlendRollingBatch>
              size="small"
              rowKey="entry_date"
              columns={batchColumns}
              dataSource={batches}
              pagination={false}
              scroll={{ x: 920 }}
              data-testid="combo-rolling-batches"
            />
          ),
        }
      : null,
    daily.length > 0
      ? {
          key: 'daily',
          label: `逐日净值（${daily.length} 日）`,
          children: (
            <Table<BlendRollingDaily>
              size="small"
              rowKey="date"
              columns={dailyColumns}
              dataSource={daily}
              pagination={false}
              scroll={{ x: 940 }}
              data-testid="combo-rolling-daily"
            />
          ),
        }
      : null,
  ].filter((item): item is NonNullable<typeof item> => item !== null);

  return (
    <div
      className="h-full min-h-0 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden"
      data-testid="combo-rolling-root"
    >
      <div className="shrink-0 flex flex-wrap items-center gap-3 px-3 py-2 border-b border-slate-100">
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
          <span>
            窗口 <strong className="font-mono text-slate-800">{rolling.columns.length}</strong> 日
          </span>
          <span>
            每日 <strong className="font-mono text-slate-800">{rolling.per_day}</strong> 只
          </span>
          <span>
            去重 <strong className="font-mono text-slate-800">{rolling.summary.unique_symbols}</strong> 只
          </span>
          <span className="text-blue-700">
            今日新进 <strong className="font-mono">{rolling.summary.new_today}</strong>
          </span>
          <span className="text-amber-700">
            今日退出 <strong className="font-mono">{rolling.summary.exited_today}</strong>
          </span>
          <span className="text-slate-400">生效日 {rolling.target_date}</span>
        </div>
        <Tooltip title="新进=蓝、留存=白、退出=琥珀；色块只表达名单变化。涨跌幅与当日盈亏用红涨绿跌">
          <span className="ml-auto text-[10px] text-slate-400 cursor-help">色块图例</span>
        </Tooltip>
        <Button size="small" icon={<RefreshCw size={13} />} loading={loading} onClick={onRetry}>
          刷新
        </Button>
      </div>

      <div className="shrink-0 flex flex-wrap items-center gap-2 px-3 py-1 border-b border-slate-100">
        <span className="text-[11px] text-slate-500">批次本金（万）</span>
        <InputNumber
          size="small"
          className="!w-24"
          min={MIN_CAPITAL_WAN}
          max={MAX_CAPITAL_WAN}
          step={100}
          value={capitalWan}
          onChange={(value) => setCapitalWan(typeof value === 'number' ? value : null)}
          onBlur={commitCapital}
          onPressEnter={commitCapital}
          suffix="万"
          data-testid="combo-rolling-capital"
        />
        <DataSourceBadge
          source={rolling.source ?? null}
          bounds={rolling.bounds ?? null}
          benchmark={rolling.benchmark ?? null}
          capital={effectiveCapital}
          testId="combo-rolling-source"
        />
        <Tooltip title="每日按全部权益等权预算入选（收盘价）；ST/退市与成交额不足的标的不建仓，单只金额不超过 ADV20 的参与率上限；次一交易日收盘结算，最后一批只估值不结算。未计手续费、印花税与滑点。">
          <span className="ml-auto text-[10px] text-slate-400 cursor-help">换仓口径</span>
        </Tooltip>
      </div>

      {performance && (
        <PortfolioMetricsStrip
          sections={metricSections}
          testId="combo-rolling-performance"
          collapsible
          storageKey={METRICS_COLLAPSED_KEY}
          collapsedSummary={collapsedSummary}
        />
      )}

      {rolling.warnings.length > 0 && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-rolling-warnings">
          <Alert
            type="warning"
            showIcon
            message={orderedWarnings(rolling.warnings)
              .slice(0, 3)
              .map(blendWarningLabel)
              .join('；')}
          />
        </div>
      )}

      <div
        ref={boardRef}
        onScroll={syncScrollbar}
        className="rolling-board-scroll flex-1 min-h-0 overflow-auto overscroll-contain p-2"
        data-testid="combo-rolling-board"
      >
        <div className="flex gap-1.5 min-w-max items-stretch">
          {rolling.columns.map((column, index) => {
            const isLatest = column.date === latest;
            const exits = exitLabel(column);
            const market = column.market;
            return (
              <div
                key={column.date}
                className="w-[168px] shrink-0 flex flex-col rounded-lg border border-slate-200 bg-slate-50/60"
                data-testid={`combo-rolling-col-${column.date}`}
              >
                <div
                  className={`px-2 py-1.5 border-b border-slate-200 rounded-t-lg ${
                    isLatest ? 'bg-blue-50' : 'bg-white'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] text-slate-500">
                      {index === rolling.columns.length - 1
                        ? '今日'
                        : `D-${rolling.columns.length - 1 - index}`}
                    </span>
                    {isLatest && <Tag className="!m-0 text-[9px] leading-tight">最新</Tag>}
                  </div>
                  <div className="font-mono text-[11px] font-bold text-slate-700">{column.date}</div>
                  <Tooltip title={marketDayLabel(market)}>
                    <div
                      className="flex items-center justify-between gap-1 cursor-help"
                      data-testid={`combo-rolling-market-${column.date}`}
                    >
                      <span
                        className={`font-mono text-[10px] font-bold ${returnToneClass(
                          market?.change_pct,
                        )}`}
                      >
                        {marketChangeLabel(market)}
                      </span>
                      <span className="text-[9px] text-slate-400 truncate">
                        {marketPositionLabel(market?.position)}
                      </span>
                    </div>
                  </Tooltip>
                </div>

                <div className="flex-1 p-1 space-y-1">
                  {column.picks.map((pick) => {
                    const status = rollingStatus(pick);
                    const label = positionLabel(pick);
                    const composite = formatResearchScore(researchScore(pick.pct));
                    const cap = capLabel(pick.market_cap_yi);
                    const buyLabel = buyStatusLabel(pick);
                    const profileLine = [pick.industry || '行业—', cap ?? '市值—'].join(' · ');
                    const stats = pick.stats;
                    const hasPnl =
                      pick.day_pnl_amount !== null && pick.day_pnl_amount !== undefined;
                    return (
                      <Tooltip key={pick.symbol} title={pickStatsLabel(pick)}>
                        <div
                          className={`rounded-md border px-1.5 py-1 ${ROLLING_TONE[status]}`}
                          data-testid={`combo-rolling-pick-${column.date}-${pick.symbol}`}
                        >
                          <div className="flex items-center justify-between gap-1">
                            <span className="font-mono text-[10px] font-black truncate">
                              {pick.code || pick.symbol}
                            </span>
                            <span className="font-mono text-[10px] shrink-0">{composite}</span>
                          </div>
                          <div className="text-[10px] truncate opacity-80">{pick.name || '—'}</div>
                          <div className="text-[9px] truncate opacity-60">{profileLine}</div>
                          <div className="flex items-center justify-between gap-1 mt-0.5">
                            <span
                              className={`rounded border px-1 text-[9px] font-bold ${positionTagClass(label)}`}
                            >
                              {label}
                            </span>
                            <span className="text-[9px] opacity-70">{rollingHeldLabel(pick)}</span>
                          </div>
                          {/* 建仓状态：建仓 x% / 次日建仓 / 未建仓 · 原因（蓝灰，不借用红绿） */}
                          {buyLabel && (
                            <div
                              className="mt-0.5"
                              data-testid={`combo-rolling-pick-buy-${column.date}-${pick.symbol}`}
                              title={buyStatusHint(pick)}
                            >
                              <span
                                className={`inline-block rounded border px-1 text-[9px] font-bold ${buyStatusTone(
                                  pick,
                                )}`}
                              >
                                {buyLabel}
                              </span>
                            </div>
                          )}
                          {/* 当日涨跌 / 当日盈亏（仅当日实际持有；红涨绿跌） */}
                          <div className="flex items-center justify-between gap-1 mt-0.5 font-mono text-[9px]">
                            <span
                              className={returnToneClass(pick.change_pct)}
                              data-testid={`combo-rolling-pick-change-${column.date}-${pick.symbol}`}
                            >
                              当日 {formatReturnPct(pick.change_pct)}
                            </span>
                            {hasPnl && (
                              <span
                                className={returnToneClass(pick.day_pnl_amount)}
                                data-testid={`combo-rolling-pick-pnl-${column.date}-${pick.symbol}`}
                              >
                                {formatSignedAmount(pick.day_pnl_amount)}
                              </span>
                            )}
                          </div>
                          {/* 窗口内表现：在榜天数 / 首次至今 / 次日均值 */}
                          <div
                            className="text-[9px] font-mono opacity-70 truncate"
                            data-testid={`combo-rolling-pick-stats-${column.date}-${pick.symbol}`}
                          >
                            在榜 {stats?.appearances ?? '—'} · 首入{' '}
                            {formatReturnPct(stats?.since_first_pct)} · 次日{' '}
                            {formatReturnPct(stats?.next_day_avg_pct)}
                          </div>
                        </div>
                      </Tooltip>
                    );
                  })}
                  {column.picks.length === 0 && (
                    <div className="text-center text-[10px] text-slate-400 py-4">当日无标的</div>
                  )}
                </div>

                <div
                  className="px-1.5 py-1 border-t border-dashed border-slate-200 min-h-[26px] space-y-0.5"
                  data-testid={`combo-rolling-exits-${column.date}`}
                >
                  {(column.exit_rows?.length ?? 0) > 0 ? (
                    <>
                      <div className="text-[9px] font-bold text-amber-700">
                        退出 {column.exit_rows?.length} 只
                      </div>
                      {column.exit_rows?.map((row) => (
                        <Tooltip key={row.symbol} title={exitTradeLabel(row)}>
                          <div
                            className="flex items-center justify-between gap-1 cursor-help"
                            data-testid={`combo-rolling-exit-${column.date}-${row.symbol}`}
                          >
                            <span className="text-[9px] truncate text-slate-600">
                              {row.name || row.code || row.symbol}
                            </span>
                            <span
                              className={`text-[9px] font-mono font-bold shrink-0 ${returnToneClass(
                                row.return_pct,
                              )}`}
                            >
                              {formatReturnPct(row.return_pct)}
                            </span>
                          </div>
                        </Tooltip>
                      ))}
                    </>
                  ) : exits ? (
                    <Tooltip title="上一列在榜、本列不在榜（只标注一次）">
                      <div className="text-[9px] text-amber-700 leading-tight">
                        退出：{exits}
                      </div>
                    </Tooltip>
                  ) : (
                    <div className="text-[9px] text-slate-300">—</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {overflowX && (
        <div className="shrink-0 px-2 pb-1">
          <div
            ref={trackRef}
            role="scrollbar"
            aria-orientation="horizontal"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(scrollbar.progress * 100)}
            aria-label="滚动建仓横向滚动"
            className="rolling-board-scrollbar relative h-2.5 w-full rounded-full bg-slate-100 cursor-pointer touch-none select-none"
            data-testid="combo-rolling-scrollbar"
            onPointerDown={onTrackPointerDown}
          >
            <div
              data-role="thumb"
              className="absolute top-0 h-full rounded-full bg-slate-400 hover:bg-slate-500"
              style={{ left: scrollbar.thumbLeft, width: scrollbar.thumbWidth }}
              data-testid="combo-rolling-scroll-thumb"
            />
          </div>
        </div>
      )}

      {detailItems.length > 0 && (
        <div
          className="shrink-0 max-h-[38%] overflow-auto border-t border-slate-100 px-2 py-1"
          data-testid="combo-rolling-details"
        >
          <Collapse size="small" items={detailItems} />
        </div>
      )}

      <div className="shrink-0 px-3 py-1.5 border-t border-slate-100 text-[10px] text-slate-400">
        持有天数 = 本窗口内连续在榜天数；断榜后重新计数，跨窗口不伪造连续。退出收益 =
        退出日收盘价 ÷ 入选日收盘价 − 1（按收盘价计，未计费用；窗口首列按窗口起点计）。
        批次按「入选日收盘等权预算建仓 → 次一交易日收盘结算」滚动；入选后先过 ST/退市与
        成交额门槛（ADV20 缺失或低于门槛的标的不建仓），单只金额 = min(等权预算,
        ADV20 × 参与率)，跳过的标的不替补、对应资金留作现金。ST 标记取当前证券主表
        截面，历史重算含前视偏差。格子里的当日盈亏只对当日实际持有的标的计算，新进标的
        当天不计盈亏。缺失行情一律显示 —。
      </div>
    </div>
  );
};

export default RollingBuildPanel;
