/**
 * 因子报告左栏 —— 因子浏览控制台（2026-10-10 增强）。
 *
 * 用户原话：「那么多因子，我要选一些因子出来看，不知道怎么选，一个一个看太久」。
 * 于是左栏从「搜索 + 库筛选」升级为研究控制台：
 * - **排序可切**：|IC| / IC（带方向）/ ICIR / 多空收益 / 换手 / 名称，配升/降序；
 *   切换排序键时方向跟键走（换手、名称天生升序，其余降序找最强）；
 * - **质量门槛**：|ICIR| ≥、|IC| ≥（正反不限——强反向因子也是好因子） 、换手 ≤；
 * - **方向**：全部 / 正向 IC>0 / 反向 IC<0；
 * - **★ 自选**：localStorage 按数据集持久化（`qm:factor-report:favs:${dataset}`），
 *   可「只看自选」——几百个因子里挑出来的那十几个不再每次重找；
 * - **选中项被筛掉**时给提示 + 一键恢复显示，避免「报告在右、左栏找不到人」。
 *
 * 空值（null/NaN）一律**沉底**（升降序都不翻），与站内「缺失 ≠ 0」口径一致。
 */

import React, { useMemo, useState } from 'react';
import { Search, Star } from 'lucide-react';
import type { FactorSummary } from '../../types/factorReport';

interface FactorRankListProps {
  factors: FactorSummary[];
  selected: string | null;
  onSelect: (name: string) => void;
  /** 数据集名：自选收藏按数据集隔离存储（不同库的因子重名不互串） */
  dataset: string;
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

const SORT_KEYS = [
  { key: 'abs', label: '|IC|' },
  { key: 'ic', label: 'IC（带方向）' },
  { key: 'icir', label: 'ICIR' },
  { key: 'ls', label: '多空收益' },
  { key: 'turnover', label: '换手' },
  { key: 'name', label: '名称' },
] as const;
type SortKey = (typeof SORT_KEYS)[number]['key'];

/** 每个排序键的默认方向：换手/名称从小到大，其余从大到小（先看最强） */
const DEFAULT_ASC: Record<SortKey, boolean> = {
  abs: false, ic: false, icir: false, ls: false, turnover: true, name: true,
};

const MIN_ICIR_OPTS = ['', '0.3', '0.5', '1'];
const MIN_IC_OPTS = ['', '0.01', '0.02', '0.03', '0.04', '0.05'];
const MAX_TURNOVER_OPTS = ['', '20', '30', '50', '80'];

const favKey = (ds: string): string => `qm:factor-report:favs:${ds}`;

/** 读自选（坏数据一律当空，不把解析异常带进界面） */
function loadFavs(ds: string): string[] {
  try {
    const raw = localStorage.getItem(favKey(ds));
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

/** 写自选（隐私模式/配额满等写入失败静默——收藏是便利功能，不阻断浏览） */
function saveFavs(ds: string, arr: string[]): void {
  try {
    localStorage.setItem(favKey(ds), JSON.stringify(arr));
  } catch {
    /* ignore */
  }
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

function valueOf(f: FactorSummary, key: SortKey): number | string | null {
  switch (key) {
    case 'abs': return num(f.ic_mean) === null ? null : Math.abs(f.ic_mean);
    case 'ic': return num(f.ic_mean);
    case 'icir': return num(f.icir);
    case 'ls': return num(f.ls_mean);
    case 'turnover': return num(f.turnover);
    case 'name': return f.display_name || f.name;
  }
}

export const FactorRankList: React.FC<FactorRankListProps> = ({ factors, selected, onSelect, dataset, loading }) => {
  const [keyword, setKeyword] = useState('');
  const [library, setLibrary] = useState<string>('all');
  const [sortKey, setSortKey] = useState<SortKey>('abs');
  const [sortAsc, setSortAsc] = useState(DEFAULT_ASC.abs);
  const [minIcir, setMinIcir] = useState('');
  const [minIc, setMinIc] = useState('');
  const [maxTurnover, setMaxTurnover] = useState('');
  const [dir, setDir] = useState<'all' | 'pos' | 'neg'>('all');
  const [favsOnly, setFavsOnly] = useState(false);

  // 收藏按数据集隔离：切换数据集时同步换库（render 期调整派生状态，官方推荐模式）
  const [favs, setFavs] = useState<string[]>(() => loadFavs(dataset));
  const [favsDs, setFavsDs] = useState(dataset);
  if (favsDs !== dataset) {
    setFavsDs(dataset);
    setFavs(loadFavs(dataset));
  }

  const toggleFav = (name: string) => {
    const next = favs.includes(name) ? favs.filter((x) => x !== name) : [...favs, name];
    setFavs(next);
    saveFavs(dataset, next);
  };

  const onSortKeyChange = (k: SortKey) => {
    setSortKey(k);
    setSortAsc(DEFAULT_ASC[k]);
  };

  const filtersActive =
    !!keyword.trim() || library !== 'all' || !!minIcir || !!minIc || !!maxTurnover || dir !== 'all' || favsOnly;

  const resetFilters = () => {
    setKeyword('');
    setLibrary('all');
    setMinIcir('');
    setMinIc('');
    setMaxTurnover('');
    setDir('all');
    setFavsOnly(false);
  };

  // 库筛选动态化：Alpha 库是 alpha158/alpha101/gtja191，L1/L2 数据集则是 L1/L2
  const libraries = useMemo(() => {
    const seen: string[] = [];
    for (const f of factors) {
      if (f.library && !seen.includes(f.library)) seen.push(f.library);
    }
    return seen;
  }, [factors]);

  const shown = useMemo(() => {
    const kw = keyword.trim().toUpperCase();
    const minIcirV = minIcir === '' ? null : Number(minIcir);
    const minIcV = minIc === '' ? null : Number(minIc);
    const maxTurnV = maxTurnover === '' ? null : Number(maxTurnover);
    const filtered = factors.filter((f) => {
      if (library !== 'all' && f.library !== library) return false;
      if (kw && !f.name.toUpperCase().includes(kw) && !String(f.display_name || '').toUpperCase().includes(kw)) {
        return false;
      }
      const ic = num(f.ic_mean);
      const icir = num(f.icir);
      const turn = num(f.turnover);
      if (dir === 'pos' && !(ic !== null && ic > 0)) return false;
      if (dir === 'neg' && !(ic !== null && ic < 0)) return false;
      // 门槛按绝对值：强反向因子（IC 大幅为负）也是可用因子，方向交给「方向」控件管
      if (minIcirV !== null && !(icir !== null && Math.abs(icir) >= minIcirV)) return false;
      if (minIcV !== null && !(ic !== null && Math.abs(ic) >= minIcV)) return false;
      if (maxTurnV !== null && !(turn !== null && turn * 100 <= maxTurnV)) return false;
      if (favsOnly && !favs.includes(f.name)) return false;
      return true;
    });
    const keyed = filtered.map((f) => ({ f, v: valueOf(f, sortKey) }));
    keyed.sort((a, b) => {
      const an = a.v === null || a.v === undefined || Number.isNaN(a.v);
      const bn = b.v === null || b.v === undefined || Number.isNaN(b.v);
      if (an || bn) return an && bn ? 0 : an ? 1 : -1; // 空值沉底，升降序都不翻
      const c =
        typeof a.v === 'string' && typeof b.v === 'string'
          ? a.v.localeCompare(b.v)
          : (a.v as number) - (b.v as number);
      return sortAsc ? c : -c;
    });
    return keyed.map((x) => x.f);
  }, [factors, keyword, library, dir, minIcir, minIc, maxTurnover, favsOnly, favs, sortKey, sortAsc]);

  const shownNames = useMemo(() => new Set(shown.map((f) => f.name)), [shown]);
  const selectedHidden = selected !== null && !shownNames.has(selected);

  const chip = (active: boolean): string =>
    `px-2 py-0.5 rounded-full text-[10px] font-bold border transition-colors ${
      active
        ? 'bg-indigo-600 text-white border-indigo-600'
        : 'bg-white text-slate-500 border-slate-200 hover:border-indigo-200 hover:text-indigo-600'
    }`;

  const miniSelect = 'rounded-lg border border-slate-200 bg-white px-1 py-0.5 text-[10px] font-mono text-slate-600';

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
          <button onClick={() => setLibrary('all')} className={chip(library === 'all')}>
            全部
          </button>
          {libraries.map((lib) => (
            <button key={lib} onClick={() => setLibrary(lib)} className={chip(library === lib)}>
              {LIBRARY_LABELS[lib] || lib}
            </button>
          ))}
        </div>

        {/* 研究控制台：排序 + 质量门槛 + 方向 + 自选 */}
        <div className="rounded-xl border border-slate-200/80 bg-slate-50/60 p-2 flex flex-col gap-1.5">
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] font-bold text-slate-400 shrink-0">排序</span>
            <select
              data-testid="frl-sort"
              value={sortKey}
              onChange={(e) => onSortKeyChange(e.target.value as SortKey)}
              className={miniSelect}
            >
              {SORT_KEYS.map((s) => (
                <option key={s.key} value={s.key}>{s.label}</option>
              ))}
            </select>
            <button
              data-testid="frl-sort-dir"
              onClick={() => setSortAsc(!sortAsc)}
              title={sortAsc ? '当前升序，点击切降序' : '当前降序，点击切升序'}
              className="rounded-lg border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-bold text-slate-600 hover:border-indigo-200 hover:text-indigo-600"
            >
              {sortAsc ? '↑ 升序' : '↓ 降序'}
            </button>
            <div className="flex-1" />
            <span className="font-mono text-[10px] text-slate-500">{`${shown.length} / ${factors.length}`}</span>
          </div>

          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[10px] font-bold text-slate-400 shrink-0">质量</span>
            <label className="flex items-center gap-1 text-[10px] text-slate-500">
              |ICIR|≥
              <select data-testid="frl-min-icir" value={minIcir} onChange={(e) => setMinIcir(e.target.value)} className={miniSelect}>
                {MIN_ICIR_OPTS.map((v) => (
                  <option key={v || 'x'} value={v}>{v === '' ? '不限' : v}</option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[10px] text-slate-500">
              |IC|≥
              <select data-testid="frl-min-ic" value={minIc} onChange={(e) => setMinIc(e.target.value)} className={miniSelect}>
                {MIN_IC_OPTS.map((v) => (
                  <option key={v || 'x'} value={v}>{v === '' ? '不限' : v}</option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[10px] text-slate-500">
              换手≤
              <select data-testid="frl-max-turnover" value={maxTurnover} onChange={(e) => setMaxTurnover(e.target.value)} className={miniSelect}>
                {MAX_TURNOVER_OPTS.map((v) => (
                  <option key={v || 'x'} value={v}>{v === '' ? '不限' : `${v}%`}</option>
                ))}
              </select>
            </label>
          </div>

          <div className="flex items-center gap-1">
            <span className="text-[10px] font-bold text-slate-400 shrink-0">方向</span>
            {([['all', '全部'], ['pos', '正向 IC>0'], ['neg', '反向 IC<0']] as const).map(([k, label]) => (
              <button
                key={k}
                data-testid={`frl-dir-${k}`}
                onClick={() => setDir(k)}
                className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold transition-colors ${
                  dir === k ? 'bg-slate-800 text-white' : 'text-slate-500 hover:bg-slate-200/70'
                }`}
              >
                {label}
              </button>
            ))}
            <div className="flex-1" />
            <button
              data-testid="frl-favs-only"
              onClick={() => setFavsOnly(!favsOnly)}
              title={favsOnly ? '取消：显示全部因子' : '只看收藏的因子（点行尾 ★ 收藏）'}
              className={`flex items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-[10px] font-bold transition-colors ${
                favsOnly
                  ? 'border-amber-300 bg-amber-50 text-amber-600'
                  : 'border-slate-200 text-slate-500 hover:border-amber-200 hover:text-amber-500'
              }`}
            >
              <Star className="w-2.5 h-2.5" fill={favsOnly ? 'currentColor' : 'none'} />
              自选 {favs.length}
            </button>
          </div>
        </div>
      </div>

      {selectedHidden && (
        <div
          data-testid="frl-selected-hidden"
          className="mx-3 mt-2 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1.5 text-[10px] text-amber-700 flex items-center gap-2"
        >
          <span className="flex-1 min-w-0 truncate">
            当前筛选看不到「
            {factors.find((f) => f.name === selected)?.display_name || selected}
            」（{selected}）
          </span>
          <button onClick={resetFilters} className="shrink-0 font-bold text-indigo-600 hover:text-indigo-700">
            恢复显示
          </button>
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar">
        {loading && shown.length === 0 && (
          <div className="p-4 text-center text-xs text-slate-400">加载中…</div>
        )}
        {shown.map((f) => {
          const active = f.name === selected;
          const isFav = favs.includes(f.name);
          return (
            <div
              key={f.name}
              data-testid={`frl-item-${f.name}`}
              className={`group flex items-stretch border-b border-slate-50 transition-colors ${
                active ? 'bg-indigo-50/80' : 'hover:bg-slate-50'
              }`}
            >
              <button onClick={() => onSelect(f.name)} className="flex-1 min-w-0 text-left px-3 py-1.5">
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
              <button
                data-testid={`frl-fav-${f.name}`}
                onClick={() => toggleFav(f.name)}
                aria-label={isFav ? `取消收藏 ${f.name}` : `收藏 ${f.name}`}
                title={isFav ? '取消收藏' : '收藏（可「只看自选」）'}
                className={`shrink-0 px-1.5 transition-colors ${
                  isFav ? 'text-amber-400' : 'text-slate-200 group-hover:text-slate-300 hover:text-amber-300'
                }`}
              >
                <Star className="w-3 h-3" fill={isFav ? 'currentColor' : 'none'} />
              </button>
            </div>
          );
        })}
        {!loading && shown.length === 0 && (
          <div className="p-4 text-center text-xs text-slate-400">
            没有匹配的因子
            {filtersActive && (
              <button onClick={resetFilters} className="ml-2 font-bold text-indigo-500 hover:text-indigo-600">
                清除筛选
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
