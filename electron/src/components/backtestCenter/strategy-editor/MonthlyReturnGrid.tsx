/**
 * 月度收益热力网格（策略编辑结果坞）。
 *
 * A 股口径：正收益红、负收益绿（与全站 heatmapColor 一致）；强度按 |收益|/10%
 * 截断，避免单月暴涨把整行染色。右侧「年度」列独立按年内首末点计算。
 */
import React from 'react';
import { Tooltip } from 'antd';
import type { MonthlyRow } from './resultMetrics';

interface Props {
  rows: MonthlyRow[];
}

const MONTHS = Array.from({ length: 12 }, (_, i) => i + 1);

const cellStyle = (ret: number | null): React.CSSProperties => {
  if (ret === null) return {};
  const intensity = Math.min(Math.abs(ret) / 0.1, 1);
  const alpha = 0.12 + intensity * 0.68;
  return ret >= 0
    ? { background: `rgba(239, 68, 68, ${alpha})`, color: intensity > 0.45 ? '#fff' : '#7f1d1d' }
    : { background: `rgba(16, 185, 129, ${alpha})`, color: intensity > 0.45 ? '#fff' : '#064e3b' };
};

const label = (ret: number | null) => {
  if (ret === null) return '';
  return `${(ret * 100).toFixed(1)}`;
};

export const MonthlyReturnGrid: React.FC<Props> = ({ rows }) => {
  if (!rows.length) {
    return <div className="flex h-full items-center justify-center text-[11px] text-slate-400">样本不足以计算月度收益</div>;
  }
  return (
    <div className="se-scroll h-full overflow-auto px-3 py-2">
      <div
        className="grid min-w-[760px] items-center gap-1"
        style={{ gridTemplateColumns: '44px repeat(12, minmax(0, 1fr)) 64px' }}
      >
        <div className="pr-1 text-right text-[9px] font-bold tracking-wider text-slate-400">年份</div>
        {MONTHS.map((month) => (
          <div key={month} className="text-center text-[9px] font-semibold text-slate-400">
            {month}月
          </div>
        ))}
        <div className="text-center text-[9px] font-bold tracking-wider text-slate-400">年度</div>

        {rows.map((row) => (
          <React.Fragment key={row.year}>
            <div className="pr-1 text-right font-mono text-[10px] font-semibold text-slate-500">{row.year}</div>
            {row.months.map((cell) => (
              <Tooltip
                key={cell.month}
                title={
                  cell.ret === null
                    ? `${row.year}-${String(cell.month).padStart(2, '0')} 无数据`
                    : `${row.year}-${String(cell.month).padStart(2, '0')}：${(cell.ret * 100).toFixed(2)}%`
                }
              >
                {cell.ret === null ? (
                  <div className="se-month-empty" />
                ) : (
                  <div className="se-month-cell" style={cellStyle(cell.ret)}>
                    {label(cell.ret)}
                  </div>
                )}
              </Tooltip>
            ))}
            <div
              className="se-month-cell border border-slate-200/70 font-semibold"
              style={cellStyle(row.yearReturn)}
            >
              {row.yearReturn === null ? '—' : `${(row.yearReturn * 100).toFixed(1)}`}
            </div>
          </React.Fragment>
        ))}
      </div>
      <div className="mt-2 flex items-center gap-2 text-[9px] text-slate-400">
        <span>亏损</span>
        <span className="flex overflow-hidden rounded-sm">
          {[0.7, 0.5, 0.3, 0.18].map((alpha) => (
            <span key={`d${alpha}`} className="h-2 w-5" style={{ background: `rgba(16,185,129,${alpha})` }} />
          ))}
          {[0.18, 0.3, 0.5, 0.7].map((alpha) => (
            <span key={`u${alpha}`} className="h-2 w-5" style={{ background: `rgba(239,68,68,${alpha})` }} />
          ))}
        </span>
        <span>盈利</span>
        <span className="ml-auto">单位为 %，强度按 |月收益| / 10% 截断</span>
      </div>
    </div>
  );
};

export default MonthlyReturnGrid;
