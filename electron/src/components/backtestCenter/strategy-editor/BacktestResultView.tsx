/**
 * 回测结果视图（结果坞内）。
 *
 * 与旧的 StrategyLabResultPanel 的差别：
 * - KPI 条按 A 股口径着色（红涨绿跌），评分用中性靛蓝，不把「分数」画成涨跌；
 * - 净值图占满整行，右侧只放真正会看的派生读数（期末净值/超额/月度胜率/最长水下）；
 * - 月度热力、成交/持仓、年度统计各自单独成页，避免旧版 Tab 套 Tab 的层级噪音。
 */
import React, { useMemo, useState } from 'react';
import { Alert, Table, Tooltip } from 'antd';
import { Activity, Info } from 'lucide-react';
import type {
  StrategyLabRunResult,
  StrategyLabTradeRecord,
} from '../../../features/strategy-lab/types';
import StrategyLabKlineView from '../../../features/strategy-lab/components/StrategyLabKlineView';
import YearlyStats from '../../../features/strategy-lab/components/YearlyStats';
import { humanizeError } from '../../../features/strategy-lab/utils/humanizeError';
import ResultEquityChart from './ResultEquityChart';
import MonthlyReturnGrid from './MonthlyReturnGrid';
import {
  SCORE_NEUTRAL,
  computeEquitySummary,
  computeMonthlyReturns,
  computeStrategyScore,
  formatMoney,
  formatNumber,
  formatPct,
  formatSignedPct,
  normalizeDrawdown,
  signedToneClass,
} from './resultMetrics';
import type { DockTab } from './types';

interface Props {
  result: StrategyLabRunResult;
  tab: DockTab;
}

const KpiTile: React.FC<{
  label: string;
  value: string;
  tone?: string;
  hint?: string;
}> = ({ label, value, tone = 'text-slate-800', hint }) => (
  <div className="se-kpi-tile flex-1" title={hint}>
    <div className="se-kpi-label">{label}</div>
    <div className={`se-kpi-value se-mono ${tone}`}>{value}</div>
  </div>
);

const ScoreRing: React.FC<{ result: StrategyLabRunResult }> = ({ result }) => {
  const score = computeStrategyScore(result);
  if (!score) return null;
  const circumference = 2 * Math.PI * 18;
  const dash = (score.score / 100) * circumference;
  return (
    <Tooltip
      title={
        <div className="space-y-0.5 text-[10px] leading-relaxed">
          {score.notes.map((note) => (
            <div key={note}>· {note}</div>
          ))}
        </div>
      }
    >
      <div className="flex w-[62px] shrink-0 flex-col items-center justify-center rounded-[10px] border border-indigo-100/70 bg-indigo-50/40 px-1 py-1">
        <div className="relative h-[42px] w-[42px]">
          <svg viewBox="0 0 44 44" className="h-full w-full -rotate-90">
            <circle cx="22" cy="22" r="18" fill="none" stroke="#e6e9f8" strokeWidth="3.5" />
            <circle
              cx="22"
              cy="22"
              r="18"
              fill="none"
              stroke={SCORE_NEUTRAL}
              strokeWidth="3.5"
              strokeLinecap="round"
              strokeDasharray={`${dash} ${circumference}`}
            />
          </svg>
          <span className="absolute inset-0 flex items-center justify-center font-mono text-[12px] font-bold text-indigo-700">
            {score.score}
          </span>
        </div>
        <div className="mt-0.5 text-[8.5px] font-semibold tracking-wider text-indigo-500">
          体检 {score.grade}
        </div>
      </div>
    </Tooltip>
  );
};

const StatLine: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div className="flex items-baseline justify-between gap-2 border-b border-dashed border-slate-100 py-1 last:border-b-0">
    <span className="shrink-0 text-[9.5px] text-slate-400">{label}</span>
    <span className="min-w-0 truncate text-right font-mono text-[10.5px] font-semibold text-slate-700">
      {value}
    </span>
  </div>
);

const NetView: React.FC<{ result: StrategyLabRunResult }> = ({ result }) => {
  const summary = useMemo(() => computeEquitySummary(result.equity), [result.equity]);
  const hasBenchmark = useMemo(
    () => result.equity.some((point) => point.benchmark !== null && point.benchmark !== undefined),
    [result.equity],
  );
  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col px-2 pb-1">
        <div className="flex items-center gap-3 px-2 pt-1 text-[9px] text-slate-400">
          <span className="flex items-center gap-1">
            <span className="h-[2px] w-4 rounded bg-[#2563eb]" /> 策略净值
          </span>
          {hasBenchmark && (
            <span className="flex items-center gap-1">
              <span className="h-[2px] w-4 rounded bg-[#94a3b8]" style={{ backgroundImage: 'linear-gradient(90deg,#94a3b8 55%,transparent 55%)' }} />
              基准
            </span>
          )}
          <span className="flex items-center gap-1">
            <span className="h-2 w-3 rounded-sm bg-emerald-400/40" /> 回撤
          </span>
        </div>
        <div className="min-h-0 flex-1">
          <ResultEquityChart equity={result.equity} />
        </div>
      </div>
      <aside className="se-scroll w-[218px] shrink-0 overflow-y-auto border-l border-slate-100 bg-slate-50/40 px-3 py-2">
        <div className="mb-1 flex items-center gap-1 text-[9px] font-bold uppercase tracking-wider text-slate-400">
          <Info className="h-2.5 w-2.5" /> 关键读数
        </div>
        {summary ? (
          <>
            <StatLine label="回测区间" value={`${summary.startDate.slice(0, 10)} → ${summary.endDate.slice(0, 10)}`} />
            <StatLine label="交易日" value={`${summary.tradingDays}`} />
            <StatLine label="期末净值" value={formatNumber(summary.finalNetValue, 4)} />
            <StatLine label="基准收益" value={formatSignedPct(summary.benchmarkReturn)} />
            <StatLine
              label="超额"
              value={<span className={signedToneClass(summary.alpha)}>{formatSignedPct(summary.alpha)}</span>}
            />
            <StatLine label="月度胜率" value={formatPct(summary.monthlyWinRate, 1)} />
            <StatLine label="最长水下" value={`${summary.maxDrawdownDays} 个交易日`} />
          </>
        ) : (
          <div className="py-6 text-center text-[10px] text-slate-400">无净值数据</div>
        )}

      </aside>
    </div>
  );
};

type TradeRow = StrategyLabTradeRecord & { _key: string };

const directionTag = (direction: StrategyLabTradeRecord['direction']) =>
  direction === 'BUY' ? (
    <span className="rounded bg-red-50 px-1.5 py-0.5 text-[9px] font-bold text-red-500">买入</span>
  ) : (
    <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[9px] font-bold text-emerald-600">卖出</span>
  );

const DetailView: React.FC<{ result: StrategyLabRunResult }> = ({ result }) => {
  const [pane, setPane] = useState<'trades' | 'positions'>('trades');
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <div className="flex rounded-lg bg-slate-100/80 p-0.5">
          {([
            ['trades', result.trades.length ? `成交 ${result.trades.length}` : '成交明细'],
            ['positions', result.positions.length ? `末日持仓 ${result.positions.length}` : '末日持仓'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setPane(key)}
              className={`rounded-md px-2.5 py-1 text-[10.5px] font-semibold transition-all ${
                pane === key ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="text-[9px] text-slate-300">成交为回测引擎撮合记录；统计与明细可能不同源</span>
      </div>
      <div className="se-scroll min-h-0 flex-1 overflow-auto px-3 pb-2">
        {pane === 'trades' ? (
          <Table<TradeRow>
            className="se-mini-table"
            size="small"
            rowKey="_key"
            dataSource={result.trades.map((trade, index) => ({
              ...trade,
              _key: `${index}-${trade.date}-${trade.symbol}`,
            }))}
            pagination={{ pageSize: 8, size: 'small', showSizeChanger: false }}
            locale={{
              emptyText:
                result.metrics.n_trades > 0
                  ? `引擎统计 ${result.metrics.n_trades} 次开仓，但本次结果未返回逐笔明细`
                  : '本次结果未返回成交明细',
            }}
            columns={[
              { title: '日期', dataIndex: 'date', width: 92 },
              { title: '代码', dataIndex: 'symbol', width: 92 },
              {
                title: '方向',
                dataIndex: 'direction',
                width: 66,
                render: (value: StrategyLabTradeRecord['direction']) => directionTag(value),
              },
              {
                title: '价格',
                dataIndex: 'price',
                width: 76,
                align: 'right',
                render: (value: number) => <span className="se-mono">{formatNumber(value, 2)}</span>,
              },
              {
                title: '数量',
                dataIndex: 'qty',
                width: 76,
                align: 'right',
                render: (value: number) => <span className="se-mono">{formatMoney(value)}</span>,
              },
              {
                title: 'PnL',
                dataIndex: 'pnl',
                width: 84,
                align: 'right',
                render: (value: number | null) => (
                  <span className={`se-mono font-semibold ${signedToneClass(value)}`}>
                    {value === null ? '—' : formatMoney(value)}
                  </span>
                ),
              },
              { title: '原因', dataIndex: 'reason', ellipsis: true },
            ]}
          />
        ) : (
          <Table
            className="se-mini-table"
            size="small"
            rowKey={(row: { symbol: string }) => row.symbol}
            dataSource={result.positions}
            pagination={false}
            locale={{ emptyText: '无持仓明细（空仓或引擎未返回）' }}
            columns={[
              { title: '代码', dataIndex: 'symbol', width: 100 },
              {
                title: '数量',
                dataIndex: 'qty',
                width: 90,
                align: 'right',
                render: (value: number) => <span className="se-mono">{formatMoney(value)}</span>,
              },
              {
                title: '成本',
                dataIndex: 'cost',
                width: 90,
                align: 'right',
                render: (value: number) => <span className="se-mono">{formatNumber(value, 2)}</span>,
              },
              {
                title: '市值',
                dataIndex: 'market_value',
                width: 110,
                align: 'right',
                render: (value: number) => <span className="se-mono">{formatMoney(value)}</span>,
              },
              {
                title: '盈亏%',
                dataIndex: 'pnl_pct',
                align: 'right',
                render: (value: number) => (
                  <span className={`se-mono font-semibold ${signedToneClass(value)}`}>
                    {formatSignedPct(value)}
                  </span>
                ),
              },
            ]}
          />
        )}
      </div>
    </div>
  );
};

const FailedView: React.FC<{ result: StrategyLabRunResult }> = ({ result }) => {
  const human = humanizeError(result.error || '', result.error_traceback || '');
  return (
    <div className="se-scroll h-full overflow-auto p-3">
      <Alert
        type="error"
        showIcon
        message={`回测失败 · run_id=${String(result.run_id || '').slice(0, 8)}`}
        description={
          <div className="space-y-1.5">
            <div className="text-[11.5px] font-semibold text-red-700">{human.title}</div>
            {human.hint && <div className="text-[11px] text-red-600/80">{human.hint}</div>}
            {result.error && (
              <pre className="max-h-[120px] overflow-auto rounded-md bg-slate-900/90 p-2 font-mono text-[10px] text-slate-100">
                {result.error}
              </pre>
            )}
            {result.error_traceback && (
              <pre className="max-h-[160px] overflow-auto rounded-md bg-slate-50 p-2 font-mono text-[9.5px] text-slate-500">
                {result.error_traceback}
              </pre>
            )}
          </div>
        }
      />
    </div>
  );
};

export const BacktestResultView: React.FC<Props> = ({ result, tab }) => {
  const summary = useMemo(() => computeEquitySummary(result.equity), [result.equity]);
  const monthly = useMemo(() => computeMonthlyReturns(result.equity), [result.equity]);

  if (result.status === 'failed') return <FailedView result={result} />;
  if (tab === 'logs' || tab === 'saved') return null;

  const m = result.metrics;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* KPI 条 */}
      <div className="flex shrink-0 items-stretch gap-1.5 border-b border-slate-100 px-2.5 py-2">
        <ScoreRing result={result} />
        <KpiTile label="累计收益" value={formatSignedPct(m.cum_return)} tone={signedToneClass(m.cum_return)} hint="期末权益 / 初始权益 - 1" />
        <KpiTile label="年化收益" value={formatSignedPct(m.annual_return)} tone={signedToneClass(m.annual_return)} hint="按 252 个交易日折算" />
        <KpiTile
          label="超额"
          value={summary?.alpha === null || summary?.alpha === undefined ? '—' : formatSignedPct(summary.alpha)}
          tone={signedToneClass(summary?.alpha)}
          hint="策略累计收益 − 基准累计收益"
        />
        <KpiTile label="夏普" value={formatNumber(m.sharpe, 2)} hint="日收益均值 / 标准差 × √252" />
        <KpiTile label="最大回撤" value={formatPct(normalizeDrawdown(m.max_drawdown))} tone="text-emerald-600" hint="历史峰值到谷底的最大跌幅（A 股口径：绿色为亏损）" />
        <KpiTile label="胜率" value={formatPct(m.win_rate, 1)} hint="盈利成交 / 总成交" />
        <KpiTile label="交易" value={`${m.n_trades}`} hint="成交笔数" />
      </div>

      <div className="min-h-0 flex-1">
        {tab === 'net' && <NetView result={result} />}
        {tab === 'monthly' && <MonthlyReturnGrid rows={monthly} />}
        {tab === 'kline' && (
          <div className="se-scroll h-full overflow-auto p-2">
            <StrategyLabKlineView result={result} height={300} />
          </div>
        )}
        {tab === 'trades' && <DetailView result={result} />}
        {tab === 'yearly' && (
          <div className="se-scroll h-full overflow-auto px-3 py-2">
            <div className="mb-2 flex items-center gap-1 text-[9px] text-slate-400">
              <Activity className="h-3 w-3" />
              年度收益按年内首末净值计算，最大回撤按年内峰谷计算
            </div>
            <div className="se-mini-table">
              <YearlyStats equity={result.equity} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default BacktestResultView;
