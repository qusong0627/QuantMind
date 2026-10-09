/**
 * 因子研究 —— 排行榜页签：区间内全列排序（综合分 = 0.5×有效性 + 0.5×业绩）、
 * 双标签展示与筛选、勾选带入对比 / 合成。
 *
 * 分类限定：左侧目录点 L1/L2 后，这里只排那一类（榜单内的名次/综合分本就是
 * 全库截面口径，分类只是过滤视图）；顶部徽章可一键清除。
 */
import React, { useMemo, useState } from 'react';
import { ArrowRightLeft, CheckSquare, Layers, Loader2, PackagePlus, Square, TrendingUp, X } from 'lucide-react';
import type { CategoryFilter, LeaderboardRow } from '../types/factorResearch';
import { ALL_TAGS, Card, fmtNum, fmtPct, TagChip } from './common';

interface Props {
  rows: LeaderboardRow[];
  loading: boolean;
  error: string | null;
  selected: string[];
  tagFilter: string[];
  n: number;
  onNChange: (n: number) => void;
  onToggle: (code: string) => void;
  onToggleTag: (tag: string) => void;
  /** 左侧目录选中的分类限定（null=全部） */
  categoryFilter: CategoryFilter | null;
  onClearCategoryFilter: () => void;
  onSendCompare: () => void;
  onSendCompose: () => void;
  onOpenSingle: (code: string) => void;
  meta: Record<string, unknown>;
  /** 仅管理员可见：把勾选因子写进训练因子目录草稿。非管理员不传（不渲染）。 */
  onRegisterToTraining?: () => void;
}

type Col = {
  key: keyof LeaderboardRow | 'name';
  label: string;
  width?: string;
  align?: 'left' | 'right';
  fmt?: (v: unknown, row: LeaderboardRow) => string;
  hint?: string;
};

const COLS: Col[] = [
  { key: 'rank', label: '#', width: 'w-8', align: 'left' },
  // 因子列必须**定宽**。此前它没有宽度，在 `table-layout: auto` 下由内容决定：
  // 私人库每个因子都要渲染 name_cn + code + l1/l2（外加两个可能的徽章），
  // 于是它把剩下来的宽度全部吃掉，后面 9 个数值列被挤成一条缝。
  { key: 'name', label: '因子', width: 'w-[13rem]', align: 'left' },
  { key: 'composite', label: '综合分', width: 'w-14', align: 'right', fmt: (v) => fmtNum(v as number, 3) },
  { key: 'annual_return', label: '年化', width: 'w-14', align: 'right', fmt: (v) => fmtPct(v as number) },
  { key: 'sharpe', label: '夏普', width: 'w-12', align: 'right', fmt: (v) => fmtNum(v as number) },
  { key: 'max_drawdown', label: '最大回撤', width: 'w-16', align: 'right', fmt: (v) => fmtPct(v as number) },
  { key: 'win_rate', label: '月胜率', width: 'w-14', align: 'right', fmt: (v) => fmtPct(v as number) },
  { key: 'excess_300', label: '超额300', width: 'w-16', align: 'right', fmt: (v) => fmtPct(v as number) },
  { key: 'excess_800', label: '超额800', width: 'w-16', align: 'right', fmt: (v) => fmtPct(v as number) },
  { key: 'ic_mean', label: 'RankIC', width: 'w-14', align: 'right', fmt: (v) => fmtNum(v as number, 3) },
  { key: 'ic_ir', label: 'IC_IR', width: 'w-12', align: 'right', fmt: (v) => fmtNum(v as number) },
  { key: 'median_mv_yi', label: '中位市值(亿)', width: 'w-20', align: 'right', fmt: (v) => fmtNum(v as number, 0) },
];

export const LeaderboardTab: React.FC<Props> = ({
  rows, loading, error, selected, tagFilter, n, onNChange, onToggle, onToggleTag,
  categoryFilter, onClearCategoryFilter,
  onSendCompare, onSendCompose, onOpenSingle, meta, onRegisterToTraining,
}) => {
  const [sortKey, setSortKey] = useState<string>('composite');
  const [asc, setAsc] = useState(false);

  const view = useMemo(() => {
    let filtered = rows;
    if (categoryFilter) {
      filtered = filtered.filter(
        (r) => r.l1 === categoryFilter.l1 && (!categoryFilter.l2 || r.l2 === categoryFilter.l2),
      );
    }
    if (tagFilter.length) {
      filtered = filtered.filter((r) => tagFilter.includes(r.env_tag) || tagFilter.includes(r.time_tag));
    }
    const sorted = [...filtered];
    sorted.sort((a, b) => {
      if (sortKey === 'name') {
        return asc ? a.name_cn.localeCompare(b.name_cn) : b.name_cn.localeCompare(a.name_cn);
      }
      const av = a[sortKey as keyof LeaderboardRow] as number | null | undefined;
      const bv = b[sortKey as keyof LeaderboardRow] as number | null | undefined;
      const na = av === null || av === undefined || Number.isNaN(av) ? -Infinity : Number(av);
      const nb = bv === null || bv === undefined || Number.isNaN(bv) ? -Infinity : Number(bv);
      return asc ? na - nb : nb - na;
    });
    return sorted;
  }, [rows, sortKey, asc, tagFilter, categoryFilter]);

  const range = meta?.range as { start?: string; end?: string; n_months?: number } | undefined;

  const clickSort = (key: string) => {
    if (key === sortKey) {
      setAsc(!asc);
    } else {
      setSortKey(key);
      setAsc(key === 'max_drawdown'); // 回撤默认升序（最抗跌在前）
    }
  };

  return (
    <div className="flex-1 min-h-0 flex flex-col gap-2">
      {/* 工具条 */}
      <div className="shrink-0 flex items-center gap-2 flex-wrap">
        <span className="flex items-center gap-1">
          <span className="text-[10px] font-bold text-slate-400">持仓数</span>
          <input
            type="number"
            min={1}
            max={100}
            value={n}
            onChange={(e) => onNChange(Math.max(1, Math.min(100, Number(e.target.value) || 30)))}
            className="w-14 rounded-lg border border-slate-200 px-1.5 py-0.5 text-[11px] font-mono text-right"
          />
          {[10, 30, 50].map((x) => (
            <button
              key={x}
              onClick={() => onNChange(x)}
              className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${
                n === x ? 'border-blue-200 bg-blue-50 text-blue-600' : 'border-slate-200 text-slate-500 hover:bg-slate-50'
              }`}
            >
              {x}
            </button>
          ))}
        </span>
        <span className="text-[10px] text-slate-300">|</span>
        <span className="text-[11px] font-bold text-slate-400">
          {range ? `${range.start} ~ ${range.end} · ${range.n_months} 个月末` : ''}
        </span>
        <div className="flex-1" />
        <button
          onClick={onSendCompare}
          disabled={selected.length < 2}
          className="flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-[11px] font-bold text-blue-600 hover:bg-blue-100 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <ArrowRightLeft className="w-3 h-3" />
          带入对比（{selected.length}）
        </button>
        <button
          onClick={onSendCompose}
          disabled={selected.length < 1}
          className="flex items-center gap-1.5 rounded-full border border-violet-200 bg-violet-50 px-3 py-1 text-[11px] font-bold text-violet-600 hover:bg-violet-100 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Layers className="w-3 h-3" />
          带入合成（{selected.length}）
        </button>
        {onRegisterToTraining && (
          <button
            onClick={onRegisterToTraining}
            disabled={selected.length < 1}
            title="把勾选的因子写进训练因子目录草稿（只写草稿，需在训练数据集页发布后模型才会用上）"
            className="flex items-center gap-1.5 rounded-full bg-slate-900 px-3 py-1 text-[11px] font-bold text-white hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <PackagePlus className="w-3 h-3" />
            注册到训练目录（{selected.length}）
          </button>
        )}
      </div>

      {/* 说明 */}
      <details className="shrink-0 rounded-xl border border-slate-200/80 bg-white px-3 py-1.5 text-[10px] text-slate-500">
        <summary className="cursor-pointer font-bold text-slate-600 select-none">
          综合分与标签口径（点击展开）
        </summary>
        <div className="mt-1.5 leading-relaxed space-y-1">
          <p>
            所有因子按综合得分排名。综合分 = 因子有效性(50%) + 实战业绩(50%)：分项指标在**全因子截面**标准化
            (z-score) 后加权，方向已统一（数值越大越好）。有效性 = RankIC 均值与 IC_IR；业绩 = 年化 / 夏普 /
            −最大回撤 / 月度胜率。业绩口径：top-N 等权（持仓数可切换）、月末调仓、0.2% 双边成本（按换手计）；
            区间选择会整体重算。中位市值 / 市值风格（大盘≥500亿 · 中盘100-500亿 · 小盘&lt;100亿）与前三行业
            按最新截面的 Top-N 持仓统计。
          </p>
          <p>
            标签（每个因子 2 个，按当前区间自动判定，基准 top-30）：市场环境——沪深300 滚动 3 月涨跌 &gt;+5% 记牛、&lt;−5% 记熊、其余震荡，
            因子在三环境的月均超额（组合−沪深300）在因子间 z-score，取最高者 → 牛市进攻型 / 熊市防御型 / 震荡占优型，
            三项 z 均 &lt; 0.5 → 全天候型；时效——近 12 月 RankIC 均值与区间全样本之差：&gt;+0.012 近期转强、
            &lt;−0.012 近期失效、两者 |RankIC| 都 &lt; 0.01 持续低效、其余长期稳定型。
            环境标签为**因子报告口径**（离线按当前区间计算）；与平台 regime 时间线（20 日量价三态）是两套口径，
            不并入、不可互比。
          </p>
          <p>
            点左侧目录的分类名（L1/L2）= 榜单只看该类，再点一次或点顶部「分类」徽章取消；
            点击列头可改排序（如点「最大回撤」看最抗跌、点「年化」看最赚钱）；勾选因子后可一键带入对比 /
            合成
            {/* 只在实际有入口时提这句：经典因子库不提供注册（后端会逐条跳过），
                写死这句话会把用户指向一个刷不出来的「刷新字段」流程。 */}
            {onRegisterToTraining && '；管理员还可「注册到训练目录」，把勾选的因子写进训练特征库的草稿'}。
          </p>
        </div>
      </details>

      {/* 分类限定 + 标签筛选 */}
      <div className="shrink-0 flex items-center gap-1 flex-wrap">
        {categoryFilter && (
          <button
            onClick={onClearCategoryFilter}
            title="清除分类限定（回到全部因子）"
            className="flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[10px] font-bold text-blue-600 hover:bg-blue-100"
          >
            分类：{categoryFilter.l1}
            {categoryFilter.l2 ? ` / ${categoryFilter.l2}` : ''}
            <X className="w-2.5 h-2.5" />
          </button>
        )}
        <span className="text-[10px] font-bold text-slate-400 mr-0.5">标签筛选</span>
        {ALL_TAGS.map((t) => {
          const on = tagFilter.includes(t);
          return (
            <button key={t} onClick={() => onToggleTag(t)} className="transition-transform hover:scale-105">
              <span className={on ? 'ring-1 ring-indigo-400 rounded-full inline-block' : 'opacity-70'}>
                <TagChip tag={t} />
              </span>
            </button>
          );
        })}
      </div>

      {/* 表格 */}
      <Card
        title={`因子排行榜（${view.length}${categoryFilter ? ` / 全部 ${rows.length}` : ''}）`}
        className="flex-1"
        extra={
          loading && rows.length ? (
            <span className="flex items-center gap-1 text-[10px] font-bold text-indigo-500">
              <Loader2 className="w-3 h-3 animate-spin" /> 重算中…
            </span>
          ) : (
            <span className="text-[10px] text-slate-400">点击行看单因子 · 点列头排序</span>
          )
        }
      >
        {error ? (
          <div className="h-full flex items-center justify-center text-xs text-rose-500">{error}</div>
        ) : loading && rows.length === 0 ? (
          // 首次进入要现算全库综合分（私人库 2000+ 因子需数秒）：给可读的进度提示，
          // 而不是一块无字灰屏。已有数据的重算不落到这里（表格变暗 + 「重算中」）。
          <div className="h-full flex flex-col items-center justify-center gap-3">
            <Loader2 className="w-5 h-5 animate-spin text-indigo-400" />
            <div className="text-xs text-slate-400">正在加载因子数据…</div>
            <div className="text-[10px] text-slate-300">
              首次进入需要数秒（私人因子库 2000+ 因子现算综合分与标签）
            </div>
          </div>
        ) : (
          <div className={`h-full overflow-y-auto custom-scrollbar ${loading ? 'opacity-60 pointer-events-none' : ''}`}>
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-white z-10">
                <tr className="text-slate-400 font-bold">
                  <th className="text-left py-1.5 pl-1 w-8"></th>
                  {COLS.map((c) => (
                    <th
                      key={String(c.key)}
                      onClick={() => clickSort(String(c.key))}
                      className={`py-1.5 cursor-pointer select-none hover:text-slate-600 ${c.width || ''} ${
                        c.align === 'right' ? 'text-right' : 'text-left'
                      }`}
                    >
                      {c.label}
                      {sortKey === String(c.key) && <span className="ml-0.5 text-indigo-500">{asc ? '↑' : '↓'}</span>}
                    </th>
                  ))}
                  <th className="py-1.5 text-right w-14">市值风格</th>
                  <th className="text-left py-1.5 pl-1">标签</th>
                  <th className="text-left py-1.5 pl-1">前三行业（最新选股）</th>
                </tr>
              </thead>
              <tbody>
                {view.length === 0 && (
                  <tr>
                    <td colSpan={16} className="py-4 text-center text-[11px] text-slate-400">
                      {categoryFilter
                        ? '该分类下没有因子——点上方「分类」徽章清除限定'
                        : '没有符合当前筛选的因子'}
                    </td>
                  </tr>
                )}
                {view.map((r) => {
                  const isSel = selected.includes(r.code);
                  return (
                    <tr
                      key={r.code}
                      className={`border-t border-slate-100 hover:bg-slate-50/70 cursor-pointer ${isSel ? 'bg-blue-50/50' : ''}`}
                      onClick={() => onOpenSingle(r.code)}
                    >
                      <td className="py-1.5 pl-1" onClick={(e) => { e.stopPropagation(); onToggle(r.code); }}>
                        {isSel ? <CheckSquare className="w-3.5 h-3.5 text-blue-600" /> : <Square className="w-3.5 h-3.5 text-slate-300" />}
                      </td>
                      <td className="py-1.5 font-mono text-slate-400">{r.rank}</td>
                      <td className="py-1.5 w-[13rem] max-w-[13rem]">
                        {/* flex-wrap：正常一行放得下；带徽章的少数行让徽章折到第二行，
                            而不是把徽章裁掉（它们是数据质量告警，被裁等于没告警）。 */}
                        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 min-w-0">
                          <span className="font-bold text-slate-700 max-w-[7rem] truncate" title={r.name_cn}>
                            {r.name_cn}
                          </span>
                          {/* 私人因子库 2754/2754 的 name_cn 与 code 是**同一个字符串**（实测），
                              两个都印等于把同一串字符排两遍并挤掉后面的数值列；只有经典库
                              （82/82 两者不同、name_cn 是「动量」这类中文名）才需要并列显示。 */}
                          {r.code !== r.name_cn && (
                            <span className="shrink-0 font-mono text-[10px] text-slate-400" title={r.code}>
                              {r.code}
                            </span>
                          )}
                          <span className="shrink-0 text-[9px] text-slate-300 whitespace-nowrap">
                            {r.l1.slice(0, 2)} / {r.l2}
                          </span>
                          {r.insufficient && (
                            <span className="shrink-0 rounded-full bg-amber-50 border border-amber-100 px-1.5 py-[1px] text-[9px] font-bold text-amber-600">
                              数据不足（{r.n_months ?? 0} 月）
                            </span>
                          )}
                          {r.suspicious && (
                            <span
                              className="shrink-0 rounded-full bg-orange-50 border border-orange-100 px-1.5 py-[1px] text-[9px] font-bold text-orange-600"
                              title="|IC|>0.3 或 |ICIR|>5，超出真实因子的物理上限，疑似未来函数（数据质量问题，已沉底不参与正常排序）"
                            >
                              疑似未来函数
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-1.5 text-right font-mono font-bold text-indigo-600">{fmtNum(r.composite, 3)}</td>
                      <td className={`py-1.5 text-right font-mono font-bold ${(r.annual_return || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                        {fmtPct(r.annual_return)}
                      </td>
                      <td className="py-1.5 text-right font-mono text-slate-600">{fmtNum(r.sharpe)}</td>
                      <td className="py-1.5 text-right font-mono text-slate-500">{fmtPct(r.max_drawdown)}</td>
                      <td className="py-1.5 text-right font-mono text-slate-500">{fmtPct(r.win_rate)}</td>
                      <td className={`py-1.5 text-right font-mono ${(r.excess_300 || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                        {fmtPct(r.excess_300)}
                      </td>
                      <td className={`py-1.5 text-right font-mono ${(r.excess_800 || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                        {fmtPct(r.excess_800)}
                      </td>
                      <td className={`py-1.5 text-right font-mono ${(r.ic_mean || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                        {fmtNum(r.ic_mean, 3)}
                      </td>
                      <td className="py-1.5 text-right font-mono text-slate-500">{fmtNum(r.ic_ir)}</td>
                      <td className="py-1.5 text-right font-mono text-slate-600">{fmtNum(r.median_mv_yi, 0)}</td>
                      <td className="py-1.5 text-right">
                        {r.mv_style ? (
                          <span className="rounded-full bg-slate-50 border border-slate-200 px-1.5 py-[1px] text-[9px] font-bold text-slate-500">
                            {r.mv_style}
                          </span>
                        ) : (
                          <span className="text-slate-300">—</span>
                        )}
                      </td>
                      <td className="py-1.5 pl-1">
                        <span className="flex items-center gap-1">
                          <TagChip tag={r.env_tag} small />
                          <TagChip tag={r.time_tag} small />
                        </span>
                      </td>
                      <td className="py-1.5 pl-1 text-[10px] text-slate-500 whitespace-nowrap">
                        {(r.top_industries || []).map((x) => `${x.name} ${x.count}`).join('、') || '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <div className="shrink-0 text-[10px] text-slate-400 flex items-center gap-1">
        <TrendingUp className="w-3 h-3" />
        口径：全 A 非 ST/退市池，月末调仓 Top-N 等权（持仓数可切换），双边成本 0.2%（按换手计），前复权价 · 与 factor-lib-demo 一致
      </div>
    </div>
  );
};
