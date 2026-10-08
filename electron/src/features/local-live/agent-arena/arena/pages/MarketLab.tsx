/** 行情回测：工作台（策略库 + 单标的回测同屏）/ 策略排行 / 单策略选股。
 *
 * 数据：quantdb（容器 /data/quantdb 只读挂载），全市场日线，三种复权口径。
 * 回测：PyneCore 运行时 + backend/services/lab_strategies/*.py 六个模板，
 *       以及 Pine 策略库经宿主 worker 转写后在 bwrap 沙箱里的回测产物。
 * 批量结果：scripts/lab_batch_backtest.py 跑出的 data/lab_batch/*.json（页面只读）。
 */
import { useState } from 'react';
import BatchPanel, { type LabView } from '../components/lab/BatchPanel';
import Workbench from '../components/lab/Workbench';
import './MarketLab.css';

type Tab = 'workbench' | LabView;

const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: 'workbench', label: '工作台', hint: '策略库 + 单标的回测同屏，K线标买卖点' },
  { id: 'rank', label: '策略排行', hint: '一个池子，哪个策略最强' },
  { id: 'screener', label: '单策略选股', hint: '一个策略，该买哪几只' },
];

export default function MarketLab() {
  const [tab, setTab] = useState<Tab>('workbench');
  const [symbol, setSymbol] = useState('600309.SH');

  // 选股页点一行 → 跳到工作台看这只票
  const pickSymbol = (code: string) => {
    setSymbol(code);
    setTab('workbench');
  };

  return (
    <div className="lab-page">
      <header className="lab-head">
        <div className="lab-title">
          <h1>行情回测</h1>
          <div className="lab-sub">quantdb 全市场日线 · 三档复权 · 宿主沙箱里跑策略</div>
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
        <Workbench symbol={symbol} onSymbol={setSymbol} />
      </div>
      {tab !== 'workbench' && <BatchPanel view={tab} onPickSymbol={pickSymbol} />}
    </div>
  );
}
