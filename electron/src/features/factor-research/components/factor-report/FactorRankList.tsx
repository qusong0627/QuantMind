/** 因子排行榜列表：搜索 + 库筛选 + 排序 + 选中（429 因子） */

import React, { useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import type { FactorSummary } from '../../types/factorReport';

interface FactorRankListProps {
  factors: FactorSummary[];
  selected: string | null;
  onSelect: (name: string) => void;
  loading?: boolean;
}

const LIBRARY_LABELS: Record<string, string> = {
  alpha158: 'Alpha158',
  alpha101: 'Alpha101',
  gtja191: 'GTJA191',
};

/** IC 强弱的色阶（红=正向预测力，绿=反向 —— 与站内涨跌色一致） */
function icColor(v: number): string {
  const a = Math.min(Math.abs(v) / 0.08, 1);
  return v >= 0
    ? `rgba(225, 29, 72, ${0.12 + a * 0.75})`
    : `rgba(5, 150, 105, ${0.12 + a * 0.75})`;
}

export const FactorRankList: React.FC<FactorRankListProps> = ({ factors, selected, onSelect, loading }) => {
  const [keyword, setKeyword] = useState('');
  const [library, setLibrary] = useState<string>('all');

  const shown = useMemo(() => {
    const kw = keyword.trim().toUpperCase();
    return factors.filter((f) => {
      if (library !== 'all' && f.library !== library) return false;
      if (kw && !f.name.toUpperCase().includes(kw) && !String(f.display_name || '').toUpperCase().includes(kw)) {
        return false;
      }
      return true;
    });
  }, [factors, keyword, library]);

  // 库筛选动态化：Alpha 库是 alpha158/alpha101/gtja191，L1/L2 数据集则是 L1/L2
  const libraries = useMemo(() => {
    const seen: string[] = [];
    for (const f of factors) {
      if (f.library && !seen.includes(f.library)) seen.push(f.library);
    }
    return seen;
  }, [factors]);

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 pt-3 pb-2 border-b border-slate-100 flex flex-col gap-2">
        <div className="relative">
          <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索因子（中文名或代码）"
            className="w-full rounded-lg border border-slate-200 bg-slate-50/70 pl-8 pr-2 py-1.5 text-xs text-slate-700 outline-none focus:border-indigo-300 focus:bg-white"
          />
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          <button
            onClick={() => setLibrary('all')}
            className={`px-2 py-0.5 rounded-full text-[10px] font-bold border transition-colors ${
              library === 'all'
                ? 'bg-indigo-600 text-white border-indigo-600'
                : 'bg-white text-slate-500 border-slate-200 hover:border-indigo-200 hover:text-indigo-600'
            }`}
          >
            全部
          </button>
          {libraries.map((lib) => (
            <button
              key={lib}
              onClick={() => setLibrary(lib)}
              className={`px-2 py-0.5 rounded-full text-[10px] font-bold border transition-colors ${
                library === lib
                  ? 'bg-indigo-600 text-white border-indigo-600'
                  : 'bg-white text-slate-500 border-slate-200 hover:border-indigo-200 hover:text-indigo-600'
              }`}
            >
              {LIBRARY_LABELS[lib] || lib}
            </button>
          ))}
          <span className="ml-auto text-[10px] text-slate-400 font-mono">{shown.length} 个</span>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar">
        {loading && shown.length === 0 && (
          <div className="p-4 text-center text-xs text-slate-400">加载中…</div>
        )}
        {shown.map((f) => {
          const active = f.name === selected;
          return (
            <button
              key={f.name}
              onClick={() => onSelect(f.name)}
              className={`w-full text-left px-3 py-1.5 border-b border-slate-50 transition-colors ${
                active ? 'bg-indigo-50/80' : 'hover:bg-slate-50'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className={`text-xs font-bold truncate ${active ? 'text-indigo-700' : 'text-slate-700'}`}
                      title={f.display_name ? `${f.name} · ${f.display_name}` : f.name}>
                  {f.display_name || f.name}
                </span>
                <span
                  className="shrink-0 rounded px-1.5 py-[1px] text-[10px] font-mono font-bold text-white"
                  style={{ backgroundColor: icColor(f.ic_mean) }}
                  title={`IC ${f.ic_mean}`}
                >
                  {f.ic_mean >= 0 ? '+' : ''}{f.ic_mean.toFixed(3)}
                </span>
              </div>
              <div className="flex items-center gap-2 mt-0.5 text-[10px] text-slate-400 font-mono">
                {f.display_name && <span className="truncate text-slate-500">{f.name}</span>}
                <span>ICIR {f.icir.toFixed(2)}</span>
                <span>换手 {(f.turnover * 100).toFixed(0)}%</span>
                <span className="ml-auto text-slate-300 shrink-0">{f.library}</span>
              </div>
            </button>
          );
        })}
        {!loading && shown.length === 0 && (
          <div className="p-4 text-center text-xs text-slate-400">没有匹配的因子</div>
        )}
      </div>
    </div>
  );
};
