/** 行情回测（回测中心模块）：工作台（策略库 + 单标的回测同屏）/ 策略排行 / 单策略选股。
 *
 * 数据：quantdb（容器 /data/quantdb 只读挂载），全市场日线，三种复权口径。
 * 回测：PyneCore 运行时 + backend/services/lab_strategies/*.py 六个模板，
 *       以及 Pine 策略库经宿主 worker 转写后在 bwrap 沙箱里的回测产物。
 * 批量结果：scripts/lab_batch_backtest.py 跑出的 data/lab_batch/*.json（页面只读）。
 *
 * 2026-11-15 从「实盘交易」栏整体迁入回测中心（原 agent-arena 的 MarketLabTab）。
 */
import { useEffect, useState, type CSSProperties } from 'react';
import BatchPanel, { type LabView } from './BatchPanel';
import Workbench from './Workbench';
import type { LabMarket } from './api';
import {
  DEFAULT_SYMBOL,
  MARKET_DOWN_COLOR,
  MARKET_NAME,
  MARKET_SOURCE,
  MARKET_UP_COLOR,
} from './markets';
import { asUpdater } from './reactCompat';
import './marketLab.css';

type Tab = 'workbench' | LabView;

const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: 'workbench', label: '工作台', hint: '策略库 + 单标的回测同屏，K线标进出场箭头' },
  { id: 'rank', label: '策略排行', hint: '一个池子，哪个策略最强' },
  { id: 'screener', label: '单策略选股', hint: '一个策略，适合哪几只' },
];

export default function MarketLabModule({ market = 'cn' }: { market?: LabMarket }) {
  const [tab, setTab] = useState<Tab>('workbench');
  // 每个市场各记各的标的：切市场当帧就能拿到该市场标的，不会先拿旧市场代码发一次请求
  const [symbols, setSymbols] = useState<Record<LabMarket, string>>(() => ({ ...DEFAULT_SYMBOL }));
  const symbol = symbols[market] ?? DEFAULT_SYMBOL[market];

  const setSymbol = (code: string) => {
    setSymbols(asUpdater((prev: Record<LabMarket, string>) => ({ ...prev, [market]: code })));
  };

  // 右上角市场切换 → 回到工作台（工作台内部会清掉旧市场的回测状态与复权档）
  useEffect(() => {
    setTab('workbench');
  }, [market]);

  // 选股页点一行 → 跳到工作台看这只票
  const pickSymbol = (code: string) => {
    setSymbol(code);
    setTab('workbench');
  };

  return (
    // CSS 全部作用域化在 .market-lab-root 下（迁入时从 .qm-arena-root 改的 scope），
    // 少了这个根节点整棵子树会退化成裸 HTML。
    // 涨跌色按市场切换：美股/加密绿涨红跌，A股/港股/期货红涨绿跌。
    <div
      className="market-lab-root"
      style={{
        '--up-color': MARKET_UP_COLOR[market],
        '--down-color': MARKET_DOWN_COLOR[market],
      } as CSSProperties}
    >
      <div className="lab-page">
        <header className="lab-head">
          <div className="lab-title">
            <h1>行情回测</h1>
            <div className="lab-sub">
              {MARKET_NAME[market]} {MARKET_SOURCE[market]} · 宿主沙箱里跑策略
            </div>
          </div>
          <nav className="lab-tabs">
            {TABS.map((t) => (
              <button
                key={t.id}
                className={tab === t.id ? 'on' : ''}
                onClick={() => setTab(t.id)}
              >
                <span className="t">{t.label}</span>
                <span className="h">{t.hint}</span>
              </button>
            ))}
          </nav>
        </header>

        {/* 工作台常挂载、切 tab 只隐藏：否则每次切回都要重拉 1001 条策略库 */}
        <div className={tab === 'workbench' ? '' : 'lab-hidden'}>
          <Workbench market={market} symbol={symbol} onSymbol={setSymbol} />
        </div>
        {tab !== 'workbench' && market !== 'cn' && (
          <div className="lab-empty">
            <b>批量回测当前只支持 A 股</b>
            「策略排行 / 单策略选股」来自 scripts/lab_batch_backtest.py 的 A 股股票池产物；
            切回 A 股查看，或在工作台做单标的回测。
          </div>
        )}
        {tab !== 'workbench' && market === 'cn' && (
          <BatchPanel view={tab} onPickSymbol={pickSymbol} />
        )}

        <footer className="lab-compliance">
          本页为策略研究与历史回测演示，不构成任何投资建议；回测收益不代表未来表现。
          图表只显示策略的入场/离场箭头，不构成交易指令。
        </footer>
      </div>
    </div>
  );
}
