/**
 * 因子研究 —— 左侧因子目录：大类 → 小类分组、搜索、标签筛选、
 * 每行显示综合分与两个标签，勾选框带入对比/合成，点击名称看单因子。
 *
 * 两种视图（顶部切换）：
 * - 「分类」：L1→L2 分组树。点分类名 = 把右侧排行榜限定到该分类（再点取消）；
 *   折叠箭头独立成钮 —— 「看子类」和「筛榜单」是两个动作，合成一个按钮会互相打架；
 * - 「列表」：全库平铺、按综合分降序 —— 「整个列表排序」不用切到右侧看。
 */
import React, { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, LayoutList, ListTree, Loader2, Search } from 'lucide-react';
import type { CategoryFilter, FactorMeta, LeaderboardRow } from '../types/factorResearch';
import { fmtNum, matchTagFilter, TagChip } from './common';

interface Props {
  factors: FactorMeta[];
  l1Order: string[];
  rowsByCode: Map<string, LeaderboardRow>;
  selected: string[];
  activeCode: string | null;
  tagFilter: string[];
  /** 目录拉取中：区分「正在加载」与「确实没有匹配因子」 */
  loading?: boolean;
  /** 当前分类限定（null=全部） */
  categoryFilter: CategoryFilter | null;
  onSelectCategory: (f: CategoryFilter | null) => void;
  onToggle: (code: string) => void;
  onOpen: (code: string) => void;
}

type ViewMode = 'tree' | 'flat';

const VIEW_MODES: Array<{ key: ViewMode; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { key: 'tree', label: '分类', icon: ListTree },
  { key: 'flat', label: '列表', icon: LayoutList },
];

export const CatalogSidebar: React.FC<Props> = ({
  factors, l1Order, rowsByCode, selected, activeCode, tagFilter, loading,
  categoryFilter, onSelectCategory, onToggle, onOpen,
}) => {
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [mode, setMode] = useState<ViewMode>('tree');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return factors.filter((f) => {
      if (q && !`${f.code} ${f.name_cn} ${f.l2}`.toLowerCase().includes(q)) return false;
      if (tagFilter.length) {
        // 与排行榜同一 matchTagFilter（组内 OR / 组间 AND）——两边分叉会让左侧目录
        // 仍列出右侧已滤掉的因子，点进去「查无此人」。
        const row = rowsByCode.get(f.code);
        const envTag = row ? row.env_tag : f.env_tag;
        const timeTag = row ? row.time_tag : f.time_tag;
        if (!matchTagFilter(envTag, timeTag, tagFilter)) return false;
      }
      return true;
    });
  }, [factors, query, tagFilter, rowsByCode]);

  const groups = useMemo(() => {
    const byL1 = new Map<string, Map<string, FactorMeta[]>>();
    for (const f of filtered) {
      const l2m = byL1.get(f.l1) || new Map<string, FactorMeta[]>();
      const arr = l2m.get(f.l2) || [];
      arr.push(f);
      l2m.set(f.l2, arr);
      byL1.set(f.l1, l2m);
    }
    const order = l1Order.length ? l1Order : Array.from(byL1.keys());
    const compositeOf = (code: string) => rowsByCode.get(code)?.composite ?? -Infinity;
    return order
      .filter((l1) => byL1.has(l1))
      .map((l1) => {
        const l2m = byL1.get(l1)!;
        const l2Groups = Array.from(l2m.entries()).map(([l2, list]) => ({
          l2,
          list: [...list].sort((a, b) => compositeOf(b.code) - compositeOf(a.code)),
        }));
        const count = l2Groups.reduce((n, g) => n + g.list.length, 0);
        return { l1, l2Groups, count };
      });
  }, [filtered, l1Order, rowsByCode]);

  const flat = useMemo(() => {
    const compositeOf = (code: string) => rowsByCode.get(code)?.composite ?? -Infinity;
    return [...filtered].sort((a, b) => compositeOf(b.code) - compositeOf(a.code));
  }, [filtered, rowsByCode]);

  const rowOf = (f: FactorMeta, showPath = false) => {
    const row = rowsByCode.get(f.code);
    const isActive = f.code === activeCode;
    const isSel = selected.includes(f.code);
    return (
      <div
        key={f.code}
        className={`group flex items-center gap-1.5 pl-2.5 pr-1.5 py-[3px] cursor-pointer transition-colors ${
          isActive ? 'bg-blue-50' : 'hover:bg-slate-50'
        } ${!f.available ? 'opacity-45' : ''}`}
        onClick={() => f.available && onOpen(f.code)}
      >
        <input
          type="checkbox"
          checked={isSel}
          disabled={!f.available}
          onClick={(e) => e.stopPropagation()}
          onChange={() => onToggle(f.code)}
          className="w-3 h-3 accent-indigo-600 shrink-0"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <span className={`truncate text-[11px] font-bold ${isActive ? 'text-blue-700' : 'text-slate-700'}`}>
              {f.name_cn}
            </span>
            {!f.available && <span className="shrink-0 text-[8px] text-slate-400">缺数据</span>}
            {showPath && (
              <span className="ml-auto shrink-0 text-[8px] text-slate-300 whitespace-nowrap">
                {f.l1.slice(0, 2)} / {f.l2}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5 mt-[1px]">
            <span className="font-mono text-[9px] text-slate-400">{f.code}</span>
            {row && (
              <span className="font-mono text-[9px] font-bold text-indigo-500" title="综合分">
                {fmtNum(row.composite, 2)}
              </span>
            )}
            {row && <TagChip tag={row.env_tag} small />}
          </div>
        </div>
      </div>
    );
  };

  const empty = <div className="text-[10px] text-slate-300 text-center py-6">无匹配因子</div>;

  return (
    <div className="w-[248px] shrink-0 flex flex-col bg-white rounded-2xl border border-slate-200/80 shadow-sm overflow-hidden">
      <div className="p-2 border-b border-slate-100">
        <div className="flex items-center gap-1.5 rounded-lg bg-slate-50 border border-slate-200 px-2 py-1">
          <Search className="w-3 h-3 text-slate-400" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索因子…"
            className="w-full bg-transparent text-[11px] outline-none placeholder:text-slate-300"
          />
        </div>
        <div className="mt-1 flex items-center">
          <div className="flex items-center gap-0.5 rounded-full bg-slate-100 border border-slate-200 p-0.5">
            {VIEW_MODES.map((m) => {
              const Icon = m.icon;
              return (
                <button
                  key={m.key}
                  onClick={() => setMode(m.key)}
                  title={m.key === 'tree' ? '按类别分组浏览' : '整个列表按综合分排序'}
                  className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold transition-colors ${
                    mode === m.key ? 'bg-white text-indigo-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  <Icon className="w-3 h-3" />
                  {m.label}
                </button>
              );
            })}
          </div>
          <div className="flex-1" />
          <span className="text-[9px] font-bold text-indigo-500">已选 {selected.length}</span>
        </div>
        <div className="mt-1 px-0.5">
          <span className="text-[9px] text-slate-400">
            {mode === 'tree' ? '点分类名 → 右侧只排这一类 · 箭头折叠' : '按综合分降序 · 随区间自动重算'}
          </span>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar py-1">
        {loading && factors.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-8 text-[10px] text-slate-400">
            <Loader2 className="w-4 h-4 animate-spin text-indigo-400" />
            正在加载因子目录…
          </div>
        ) : mode === 'flat' ? (
          flat.length === 0 ? (
            empty
          ) : (
            <>
              <div className="px-3 pt-1.5 pb-1 text-[9px] text-slate-400">全库 {flat.length} 个因子，按综合分降序</div>
              {flat.map((f) => rowOf(f, true))}
            </>
          )
        ) : groups.length === 0 ? (
          empty
        ) : (
          groups.map((g) => {
            const isCollapsed = collapsed[g.l1];
            const l1Active = categoryFilter?.l1 === g.l1 && !categoryFilter?.l2;
            return (
              <div key={g.l1}>
                <div className={`flex items-center ${l1Active ? 'bg-blue-50' : 'hover:bg-slate-50'}`}>
                  <button
                    aria-label={isCollapsed ? `展开 ${g.l1}` : `折叠 ${g.l1}`}
                    onClick={() => setCollapsed({ ...collapsed, [g.l1]: !isCollapsed })}
                    className="p-1.5 text-slate-400 hover:text-slate-600"
                  >
                    {isCollapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                  </button>
                  <button
                    onClick={() => onSelectCategory(l1Active ? null : { l1: g.l1, l2: null })}
                    title={l1Active ? '取消该分类限定（回到全部）' : '只在排行榜看该大类（再点取消）'}
                    className="flex-1 min-w-0 flex items-center gap-1 py-1.5 pr-2 text-left"
                  >
                    <span className={`truncate text-[10px] font-extrabold ${l1Active ? 'text-blue-700' : 'text-slate-500'}`}>
                      {g.l1}
                    </span>
                    <span className="ml-auto shrink-0 text-[9px] text-slate-300">{g.count}</span>
                  </button>
                </div>
                {!isCollapsed &&
                  g.l2Groups.map((lg) => {
                    const l2Active = categoryFilter?.l1 === g.l1 && categoryFilter?.l2 === lg.l2;
                    return (
                      <div key={lg.l2} className="pb-0.5">
                        <button
                          onClick={() => onSelectCategory(l2Active ? null : { l1: g.l1, l2: lg.l2 })}
                          title={l2Active ? '取消该分类限定（回到全部）' : '只在排行榜看该小类（再点取消）'}
                          className={`w-full text-left px-3 pt-1 pb-0.5 text-[9px] font-bold transition-colors ${
                            l2Active ? 'bg-blue-50 text-blue-600' : 'text-slate-400/90 hover:text-slate-600'
                          }`}
                        >
                          {lg.l2}
                        </button>
                        {lg.list.map((f) => rowOf(f))}
                      </div>
                    );
                  })}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
