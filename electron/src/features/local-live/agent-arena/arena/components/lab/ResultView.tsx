/** 中栏结果区：K线（带买卖点）+ 净值 + 统计卡 + 逐笔。
 *
 * 内置模板与 Pine 库策略两条链路跑完都落到同一个 `LabResult`，这里只负责画，
 * 不关心策略从哪来——合并页能做到「选中即同屏对比」的关键。
 */
import { useMemo } from 'react';
import EquityChart, { type ChartLine } from '../EquityChart';
import KLineChart from '../KLineChart';
import type { Kline } from '../../api/client';
import type { LabResult } from './labResult';
import { DOWN_COLOR, paneGroups, seriesColor, UP_COLOR } from './indicators';
import { fmt, fmtMoney, fmtPct } from './format';

const TRADE_ROWS = 60;

function StatCards({ result }: { result: LabResult }) {
  const st = result.stats;
  const cards: { label: string; value: string; tone?: 'up' | 'down'; hero?: boolean }[] = [
    {
      label: '策略净收益',
      value: `${fmtMoney(st['Net profit']?.value, result.unit)} (${fmtPct(st['Net profit']?.pct)})`,
      tone: (st['Net profit']?.pct ?? 0) >= 0 ? 'up' : 'down',
      hero: true,
    },
    { label: '买入持有', value: fmtPct(st['Buy & hold return']?.pct) },
    { label: '最大回撤', value: fmtPct(-(st['Max equity drawdown']?.pct ?? 0)) },
    { label: '胜率', value: `${fmt(st['Percent profitable']?.pct)}%` },
    { label: '盈亏比', value: fmt(st['Profit factor']?.value) },
    { label: '夏普', value: fmt(st['Sharpe ratio']?.value) },
    { label: '索提诺', value: fmt(st['Sortino ratio']?.value) },
    { label: '成交笔数', value: fmt(st['Total trades']?.value, 0) },
    { label: '平均持仓(根)', value: fmt(st['Avg # bars in trades']?.value) },
  ];
  return (
    <div className="lab-stats">
      {cards.map((c) => (
        <div key={c.label} className={`lab-stat ${c.tone ?? ''} ${c.hero ? 'hero' : ''}`}>
          <span className="k">{c.label}</span>
          <span className="v">{c.value}</span>
        </div>
      ))}
    </div>
  );
}

function TradeTable({ result }: { result: LabResult }) {
  const rows = useMemo(() => result.trades.slice(-TRADE_ROWS).reverse(), [result]);
  if (!rows.length) return null;
  return (
    <section className="lab-trades">
      <div className="lab-block-title">
        逐笔成交（共 {result.trades.length} 笔，显示最近 {rows.length} 笔）
      </div>
      <div className="lab-table-wrap">
        <table>
          <thead>
            <tr>
              <th>买入时间</th>
              <th>买入价</th>
              <th>卖出时间</th>
              <th>卖出价</th>
              <th>盈亏</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t, i) => (
              <tr
                key={`${t.entry_time}-${i}`}
                className={(t.profit ?? 0) >= 0 ? 'up' : 'down'}
              >
                <td>{t.entry_time?.slice(0, 10)}</td>
                <td>{fmt(t.entry_price)}</td>
                <td>{t.exit_time?.slice(0, 10) ?? '持仓中'}</td>
                <td>{fmt(t.exit_price)}</td>
                <td>{t.profit === null ? '—' : fmtPct(t.profit_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** 图例：取色必须与 KLineChart 一致（叠加线按序取、副图组内按序取）。 */
function IndicatorLegend({ result }: { result: LabResult }) {
  const ind = result.indicators;
  const groups = paneGroups(ind);
  if (!ind.overlays.length && !groups.length) return null;
  const swatch = (bg: string) => ({ background: bg });
  return (
    <div className="lab-legend">
      {ind.overlays.map((s, i) => (
        <span key={s.key} className="lab-legend-item">
          <i style={swatch(seriesColor(true, i))} />
          {s.label}
        </span>
      ))}
      {groups.map((g) => (
        <span key={g} className="lab-legend-group">
          {ind.panes
            .filter((s) => s.group === g)
            .map((s, si) => (
              <span key={s.key} className="lab-legend-item">
                <i
                  style={swatch(
                    s.kind === 'hist'
                      ? `linear-gradient(90deg, ${UP_COLOR} 50%, ${DOWN_COLOR} 50%)`
                      : seriesColor(false, si),
                  )}
                />
                {s.label}
              </span>
            ))}
        </span>
      ))}
    </div>
  );
}

export default function ResultView({
  bars,
  barsBusy,
  symbol,
  result,
  showMarkers,
  onToggleMarkers,
  showIndicators,
  onToggleIndicators,
  fullSpan,
  onToggleSpan,
  onSyncSymbol,
}: {
  bars: Kline[];
  barsBusy: boolean;
  symbol: string;
  result: LabResult | null;
  showMarkers: boolean;
  onToggleMarkers: () => void;
  showIndicators: boolean;
  onToggleIndicators: () => void;
  fullSpan: boolean;
  onToggleSpan: () => void;
  onSyncSymbol: () => void;
}) {
  const equityLines = useMemo<ChartLine[]>(
    () =>
      result
        ? [
            {
              id: 'equity',
              label: '策略净值',
              color: '#1a1a1a',
              points: result.equity.map((p) => ({ t: Date.parse(p.date), v: p.value })),
            },
          ]
        : [],
    [result],
  );
  const hasIndicators = !!result
    && (result.indicators.overlays.length > 0 || result.indicators.panes.length > 0);
  // 每个指标副图约 70px：不抬高容器的话，4 个副图会把主图挤成一条缝
  const paneCount = result && showIndicators ? paneGroups(result.indicators).length : 0;
  const chartHeight = 460 + Math.min(paneCount, 6) * 70;

  return (
    <div className="lab-center">
      {result?.symbol && result.symbol !== symbol && (
        <div className="lab-warn">
          成交点属于 {result.symbol}（{result.adj}），与当前K线 {symbol} 不是同一只——
          <button onClick={onSyncSymbol}>切到 {result.symbol}</button>
        </div>
      )}
      <section className="lab-chart">
        {barsBusy && <div className="lab-mask">加载中…</div>}
        <KLineChart
          bars={bars}
          trades={result?.trades ?? []}
          height={chartHeight}
          showMarkers={showMarkers}
          indicators={result?.indicators}
          showIndicators={showIndicators}
        />
        <div className="lab-hint">
          <span>
            quantdb 日线 · {bars.length} 根 · {bars[0]?.date ?? '—'} ~{' '}
            {bars[bars.length - 1]?.date ?? '—'}
          </span>
          <div className="lab-tools">
            <button className="lab-span" onClick={onToggleSpan}>
              {fullSpan ? '近 600 根' : '全历史'}
            </button>
            <button className="lab-span" onClick={onToggleMarkers}>
              {showMarkers ? '隐藏成交点' : '显示成交点'}
            </button>
            {hasIndicators && (
              <button className="lab-span" onClick={onToggleIndicators}>
                {showIndicators ? '隐藏指标' : '显示指标'}
              </button>
            )}
          </div>
        </div>
        {hasIndicators && showIndicators && result && <IndicatorLegend result={result} />}
      </section>

      {!result && (
        <div className="lab-empty">
          <b>还没有回测结果</b>
          左栏挑一个策略（内置模板或 Pine 库）→ 右栏点运行：K 线上标出买卖点，
          下面给出净值曲线、九项统计与逐笔明细。
        </div>
      )}

      {result && (
        <>
          <section className="lab-block">
            <div className="lab-block-title">
              结果 · {result.title} @ {result.symbol}
              <span className="lab-tag-src">
                {result.source === 'pine' ? '策略库' : '内置模板'}
              </span>
            </div>
            <StatCards result={result} />
          </section>

          <section className="lab-equity">
            <div className="lab-block-title">
              净值曲线（初始 {result.unit}
              {result.capital.toLocaleString('zh-CN')}）
              {result.source === 'pine' && (
                <span className="lab-tag-src">本金取自候选的 initial_capital</span>
              )}
            </div>
            <EquityChart lines={equityLines} currency={result.unit} mode="dollar" height={200} />
          </section>

          <TradeTable result={result} />
        </>
      )}
    </div>
  );
}
