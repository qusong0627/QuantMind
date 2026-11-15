/** 中栏结果区：K线（带进出场箭头）+ 净值 + 统计卡 + 逐笔。
 *
 * 内置模板与 Pine 库策略两条链路跑完都落到同一个 `LabResult`，这里只负责画，
 * 不关心策略从哪来——合并页能做到「选中即同屏对比」的关键。
 */
import { useMemo } from 'react';
import EquityLineChart from './EquityLineChart';
import KLineChart from './KLineChart';
import type { Freq, Kline, LabMarket } from './api';
import { FREQ_NAME, MARKET_NAME, MARKET_SOURCE, MARKET_TZ_OFFSET } from './markets';
import type { LabResult } from './labResult';
import { paneGroups, seriesColor } from './indicators';
import { fmt, fmtMoney, fmtPct } from './format';

const TRADE_ROWS = 60;

function StatCards({ result }: { result: LabResult }) {
  const st = result.stats;
  const cards: { label: string; value: string; tone?: 'up' | 'down'; hero?: boolean }[] = [
    {
      label: '策略净收益',
      value: `${fmtMoney(st['Net profit']?.value, result.unit)} (${fmtPct(st['Net profit']?.pct)})`,
      // 缺失不猜方向：null 时不加涨跌色（值本身显示「—」）
      tone: st['Net profit']?.pct == null
        ? undefined
        : st['Net profit']!.pct >= 0 ? 'up' : 'down',
      hero: true,
    },
    { label: '持有基准', value: fmtPct(st['Buy & hold return']?.pct) },
    {
      label: '最大回撤',
      // 缺失一律「—」：`pct ?? 0` 会把「没这个指标」画成 0.00%（假平盘）
      value: st['Max equity drawdown']?.pct == null
        ? '—'
        : fmtPct(-st['Max equity drawdown'].pct),
    },
    { label: '胜率', value: st['Percent profitable']?.pct == null ? '—' : `${fmt(st['Percent profitable'].pct)}%` },
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
  // 分钟线的成交时刻是完整时间；日线只显示日期
  const when = (s?: string | null) => (s ? s.slice(0, s.length > 10 ? 16 : 10) : '');
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
              <th>入场时间</th>
              <th>入场价</th>
              <th>离场时间</th>
              <th>离场价</th>
              <th>盈亏</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t, i) => (
              <tr
                key={`${t.entry_time}-${i}`}
                className={(t.profit ?? 0) >= 0 ? 'up' : 'down'}
              >
                <td>{when(t.entry_time)}</td>
                <td>{fmt(t.entry_price)}</td>
                <td>{t.exit_time ? when(t.exit_time) : '持仓中'}</td>
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
function IndicatorLegend({ result, upColor, downColor }: {
  result: LabResult;
  upColor: string;
  downColor: string;
}) {
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
                      ? `linear-gradient(90deg, ${upColor} 50%, ${downColor} 50%)`
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
  market,
  freq,
  upColor,
  downColor,
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
  market: LabMarket;
  freq: Freq;
  upColor: string;
  downColor: string;
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
  const equityPoints = useMemo(() => result?.equity ?? [], [result]);
  const hasIndicators = !!result
    && (result.indicators.overlays.length > 0 || result.indicators.panes.length > 0);
  // 每个指标副图约 70px：不抬高容器的话，4 个副图会把主图挤成一条缝
  const paneCount = result && showIndicators ? paneGroups(result.indicators).length : 0;
  const chartHeight = 460 + Math.min(paneCount, 6) * 70;

  const crossMarket = !!result && result.market !== market;
  const crossFreq = !!result && result.freq !== freq;
  return (
    <div className="lab-center">
      {crossMarket && (
        <div className="lab-warn">
          这份回测属于 {MARKET_NAME[result.market]} 市场，与当前 {MARKET_NAME[market]} K线不是同一数据面——
          成交点不画在当前图上；切到 {MARKET_NAME[result.market]} 或重新运行即可。
        </div>
      )}
      {crossFreq && !crossMarket && (
        <div className="lab-warn">
          这份回测是 {FREQ_NAME[result.freq]}，与当前 {FREQ_NAME[freq]} K线不是同一周期——
          成交点不画在当前图上；切到 {FREQ_NAME[result.freq]} 或重新运行即可。
        </div>
      )}
      {!crossMarket && !crossFreq && result?.symbol && result.symbol !== symbol && (
        <div className="lab-warn">
          成交点属于 {result.symbol}（{result.adj}），与当前K线 {symbol} 不是同一只——
          <button onClick={onSyncSymbol}>切到 {result.symbol}</button>
        </div>
      )}
      <section className="lab-chart">
        {barsBusy && <div className="lab-mask">加载中…</div>}
        <KLineChart
          bars={bars}
          trades={crossMarket || crossFreq ? [] : (result?.trades ?? [])}
          upColor={upColor}
          downColor={downColor}
          freq={freq}
          tzOffsetHours={MARKET_TZ_OFFSET[market]}
          height={chartHeight}
          showMarkers={showMarkers}
          indicators={result?.indicators}
          showIndicators={showIndicators}
        />
        <div className="lab-hint">
          <span>
            {MARKET_SOURCE[market]} {FREQ_NAME[freq]} · {bars.length} 根 · {bars[0]?.date ?? '—'} ~{' '}
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
        {hasIndicators && showIndicators && result && (
          <IndicatorLegend result={result} upColor={upColor} downColor={downColor} />
        )}
      </section>

      {!result && (
        <div className="lab-empty">
          <b>还没有回测结果</b>
          左栏挑一个策略（内置模板或 Pine 库）→ 右栏点运行：K 线上用 ↑/↓ 箭头标出入场/离场点，
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
              净值曲线（初始{' '}
              {result.equity.length
                ? `${result.unit}${result.capital.toLocaleString('zh-CN')}`
                : '—'}）
              {result.source === 'pine' && (
                <span className="lab-tag-src">本金取自候选的 initial_capital</span>
              )}
            </div>
            <EquityLineChart points={equityPoints} height={200}
                             tzOffsetHours={MARKET_TZ_OFFSET[result.market]} />
          </section>

          <TradeTable result={result} />
        </>
      )}
    </div>
  );
}
