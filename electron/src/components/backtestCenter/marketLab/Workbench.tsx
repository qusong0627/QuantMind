/** 行情回测工作台：左（策略）/ 中（K线+结果）/ 右（运行+备注+对话）三栏同屏。 */
import ResultView from './ResultView';
import StrategyList from './StrategyList';
import StrategyPanel from './StrategyPanel';
import { useWorkbench } from './useWorkbench';
import { FREQ_NAME, MARKET_DOWN_COLOR, MARKET_UP_COLOR, SEARCH_HINT } from './markets';
import type { AdjMode, Freq, LabMarket } from './api';

const ADJ_LABELS: { id: AdjMode; label: string }[] = [
  { id: 'unadjusted', label: '不复权' },
  { id: 'forward', label: '前复权' },
  { id: 'backward', label: '后复权' },
];

const FREQ_ORDER: Freq[] = ['1d', '5m', '1m'];

export default function Workbench({
  market,
  symbol,
  onSymbol,
}: {
  market: LabMarket;
  symbol: string;
  onSymbol: (code: string) => void;
}) {
  const wb = useWorkbench(market, symbol, onSymbol);

  return (
    <>
      <header className="lab-bar">
        <div className="lab-search">
          <span className="lab-k">标的</span>
          <div className="lab-input-wrap">
            <input
              value={wb.query}
              placeholder={`代码或名称：${SEARCH_HINT[wb.market] ?? SEARCH_HINT.cn}`}
              onChange={(e) => wb.onQuery(e.target.value)}
              onFocus={() => wb.hits.length && wb.setShowHits(true)}
            />
            {wb.showHits && wb.hits.length > 0 && (
              <ul className="lab-hits">
                {wb.hits.map((h) => (
                  <li key={h.code} onMouseDown={() => wb.pickSymbol(h)}>
                    <span className="code">{h.code}</span>
                    <span className="name">{h.name}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
        <div className="lab-symbol">
          <strong>{wb.stockName || wb.symbol}</strong>
          <span className="code">{wb.symbol}</span>
          <span className="lab-chip">日线</span>
        </div>
        <div className="lab-freq">
          <span className="lab-k">周期</span>
          <span className="seg">
            {FREQ_ORDER.filter((f) => wb.freqs.includes(f)).map((f) => {
              const usable = wb.availFreqs.includes(f);
              return (
                <button
                  key={f}
                  className={`${wb.freq === f ? 'on' : ''}${usable ? '' : ' unavailable'}`}
                  title={usable ? undefined : '该市场暂无此周期数据（点击自动回日线）'}
                  onClick={() => wb.setFreq(f)}
                >
                  {FREQ_NAME[f]}
                </button>
              );
            })}
          </span>
        </div>
        <div className="lab-adj">
          <span className="lab-k">复权</span>
          <span className="seg">
            {ADJ_LABELS
              .filter((a) => (wb.freq === '1d' ? wb.adjs : ['unadjusted'] as AdjMode[]).includes(a.id))
              .map((a) => (
                <button key={a.id} className={wb.adj === a.id ? 'on' : ''} onClick={() => wb.setAdj(a.id)}>
                  {a.label}
                </button>
              ))}
          </span>
        </div>
      </header>

      {wb.err && <div className="lab-err">{wb.err}</div>}
      {wb.freqNote && <div className="lab-warn">{wb.freqNote}</div>}

      <div className="lab-wb">
        <StrategyList wb={wb} />
        <ResultView
          market={wb.market}
          freq={wb.freq}
          upColor={MARKET_UP_COLOR[wb.market]}
          downColor={MARKET_DOWN_COLOR[wb.market]}
          bars={wb.bars}
          barsBusy={wb.barsBusy}
          symbol={wb.symbol}
          result={wb.result}
          showMarkers={wb.showMarkers}
          onToggleMarkers={wb.toggleMarkers}
          showIndicators={wb.showIndicators}
          onToggleIndicators={wb.toggleIndicators}
          fullSpan={wb.fullSpan}
          onToggleSpan={wb.toggleSpan}
          onSyncSymbol={wb.syncToResult}
        />
        <StrategyPanel wb={wb} />
      </div>
    </>
  );
}
