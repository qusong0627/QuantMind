/** 行情回测工作台：左（策略）/ 中（K线+结果）/ 右（运行+备注+对话）三栏同屏。 */
import ResultView from './ResultView';
import StrategyList from './StrategyList';
import StrategyPanel from './StrategyPanel';
import { useWorkbench } from './useWorkbench';
import type { AdjMode } from '../../api/client';

const ADJ_LABELS: { id: AdjMode; label: string }[] = [
  { id: 'unadjusted', label: '不复权' },
  { id: 'forward', label: '前复权' },
  { id: 'backward', label: '后复权' },
];

export default function Workbench({
  symbol,
  onSymbol,
}: {
  symbol: string;
  onSymbol: (code: string) => void;
}) {
  const wb = useWorkbench(symbol, onSymbol);

  return (
    <>
      <header className="lab-bar">
        <div className="lab-search">
          <span className="lab-k">标的</span>
          <div className="lab-input-wrap">
            <input
              value={wb.query}
              placeholder="代码或名称：600309 / 万华化学"
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
        <div className="lab-adj">
          <span className="lab-k">复权</span>
          <span className="seg">
            {ADJ_LABELS.map((a) => (
              <button key={a.id} className={wb.adj === a.id ? 'on' : ''} onClick={() => wb.setAdj(a.id)}>
                {a.label}
              </button>
            ))}
          </span>
        </div>
      </header>

      {wb.err && <div className="lab-err">{wb.err}</div>}

      <div className="lab-wb">
        <StrategyList wb={wb} />
        <ResultView
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
