/**
 * 单因子详情区：常驻头（7 指标环 + 分组/成本/基准控件）+ 五个页签 + 跨页签常驻的相关性热力图。
 *
 * 页签按用户给定的**小标题分类**：
 *   概览 / IC / 分组回测 / 相对基准超额 / 风格相关性数值汇总
 *
 * 页签内容用条件渲染（不是 CSS 隐藏）—— 五个页签加起来约 25 张 ECharts，
 * 全部同时挂载会一次性吃掉几百 MB 并让首次交互卡死。
 */

import React, { useState } from 'react';
import { Download, FileText } from 'lucide-react';
import { FactorHeadline } from './detail/FactorHeadline';
import { OverviewTab } from './detail/OverviewTab';
import { IcTab } from './detail/IcTab';
import { GroupTab } from './detail/GroupTab';
import { ExcessTab } from './detail/ExcessTab';
import { StyleTab } from './detail/StyleTab';
import { FactorCorrelationHeatmap } from './FactorCorrelationHeatmap';
import { buildCsvText, downloadCsvFile } from '../../../../utils/csvExport';
import type {
  FactorBlocks,
  FactorCorrelation,
  FactorDetail,
  FactorDetailParams,
  FactorRelated,
  FactorSummary,
} from '../../types/factorReport';

export type DetailTabKey = 'overview' | 'ic' | 'group' | 'excess' | 'style';

const TABS: Array<{ key: DetailTabKey; label: string }> = [
  { key: 'overview', label: '概览' },
  { key: 'ic', label: 'IC' },
  { key: 'group', label: '分组回测' },
  { key: 'excess', label: '相对基准超额' },
  { key: 'style', label: '风格相关性数值汇总' },
];

interface Props {
  factor: string;
  dataset: string;
  summary: FactorSummary | null;
  /** 全库因子（指标环的全库百分位分布来源） */
  library: FactorSummary[];
  detail: FactorDetail | null;
  loading: boolean;
  params: FactorDetailParams;
  onParams: (p: FactorDetailParams) => void;
  correlation: FactorCorrelation | null;
  related: FactorRelated | null;
  corrLoading: boolean;
  onPick: (name: string) => void;
}

/** `detail.blocks` 是「可用块」或「错误块」的联合，按有无 headline 判别 */
function splitBlocks(detail: FactorDetail | null): { blocks: FactorBlocks | null; stale: boolean } {
  const b = detail?.blocks;
  if (!b) return { blocks: null, stale: true };
  if ('headline' in b || 'ic_block' in b || 'group_block' in b) {
    return { blocks: b as FactorBlocks, stale: false };
  }
  return { blocks: null, stale: true };
}

export const FactorDetailTabs: React.FC<Props> = ({
  factor, dataset, summary, library, detail, loading, params, onParams,
  correlation, related, corrLoading, onPick,
}) => {
  const [tab, setTab] = useState<DetailTabKey>('overview');
  const [pdfHint, setPdfHint] = useState(false);

  const { blocks, stale } = splitBlocks(detail);
  const definitions = blocks?.definitions ?? {};
  const longGroup = params.longGroup ?? 3;
  const shortGroup = params.shortGroup ?? 9;
  const costBps = params.costBps ?? 20;
  const bench = params.bench ?? '000300.SH';

  const onExportCsv = () => {
    const payload = csvPayload(tab, detail, blocks);
    const csv = buildCsvText(payload.header, payload.rows);
    downloadCsvFile(csv, `factor_${factor}_${tab}_${params.horizon ?? 'fwd_ret_5'}.csv`);
  };

  return (
    <div className="flex-1 min-h-0 flex flex-col gap-2">
      <FactorHeadline
        factor={factor}
        summary={summary}
        library={library}
        blocks={blocks}
        nDates={detail?.n_dates ?? 0}
        start={detail?.start ?? '—'}
        end={detail?.end ?? '—'}
        longGroup={longGroup}
        shortGroup={shortGroup}
        costBps={costBps}
        bench={bench}
        onParams={(p) => onParams({
          longGroup: p.longGroup ?? longGroup,
          shortGroup: p.shortGroup ?? shortGroup,
          costBps: p.costBps ?? costBps,
          bench: p.bench ?? bench,
        })}
        definitions={definitions}
        staleSchema={stale}
      />

      {/* 页签条 + 导出 */}
      <div className="flex items-center gap-1 shrink-0 border-b border-slate-200 relative">
        {TABS.map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`relative px-3 py-1.5 text-xs font-bold transition-colors ${
                active ? 'text-indigo-700' : 'text-slate-400 hover:text-slate-600'
              }`}
            >
              {t.label}
              {active && (
                <span className="absolute inset-x-1 -bottom-px h-[2px] rounded-full bg-gradient-to-r from-indigo-500 to-violet-500" />
              )}
            </button>
          );
        })}

        <div className="ml-auto flex items-center gap-1.5 pb-1">
          <button
            onClick={onExportCsv}
            disabled={!detail || detail.empty}
            className="flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-[3px] text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200 disabled:opacity-40"
            title="导出当前页签的可见序列为 CSV（含 BOM，Excel 打开不乱码）"
          >
            <Download className="w-3 h-3" />
            CSV
          </button>
          <div className="relative">
            <button
              onClick={() => setPdfHint(!pdfHint)}
              disabled={!detail || detail.empty}
              className="flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-[3px] text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200 disabled:opacity-40"
              title="生成 PDF 完整报告（服务端渲染，含中文字体）"
            >
              <FileText className="w-3 h-3" />
              PDF
            </button>
            {pdfHint && (
              <div className="absolute right-0 top-full z-40 mt-1 w-[380px] rounded-xl border border-slate-200 bg-white p-3 text-[11px] leading-relaxed text-slate-600 shadow-lg">
                <div className="font-black text-slate-800 mb-1">PDF 由服务端生成（与去重报告同一交互）</div>
                前端不生成 PDF —— 浏览器侧没有中文字体，中文会变成方框。请在服务器执行：
                <pre className="mt-1.5 overflow-x-auto rounded-lg bg-slate-50 p-2 font-mono text-[10px] text-slate-700">
{`python backend/scripts/export_factor_report.py \\
  --dataset ${dataset} --factor ${factor}`}
                </pre>
                生成后落在报告档案的<b>因子研究</b>目录，可在 QuantBot 顶栏「调研报告」查看。
                <button
                  onClick={() => setPdfHint(false)}
                  className="mt-2 text-[10px] font-bold text-indigo-600 hover:underline"
                >
                  知道了
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 页签内容 */}
      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar pr-0.5">
        {loading && !detail ? (
          <div className="grid grid-cols-2 gap-3">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="h-[220px] rounded-2xl border border-slate-200/80 bg-white animate-pulse" />
            ))}
          </div>
        ) : !detail || detail.empty ? (
          <div className="h-[300px] flex items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-white/60">
            <span className="text-xs text-slate-400">{detail?.reason || '请选择一个因子查看报告'}</span>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {tab === 'overview' && (
              <OverviewTab
                factor={factor}
                dataset={dataset}
                detail={detail}
                blocks={blocks}
                costBps={costBps}
                onCost={(bps) => onParams({ longGroup, shortGroup, costBps: bps, bench })}
              />
            )}
            {tab === 'ic' && <IcTab detail={detail} blocks={blocks} />}
            {tab === 'group' && <GroupTab detail={detail} blocks={blocks} />}
            {tab === 'excess' && (
              <ExcessTab
                blocks={blocks}
                bench={bench}
                onBench={(b) => onParams({ longGroup, shortGroup, costBps, bench: b })}
                definitions={definitions}
              />
            )}
            {tab === 'style' && <StyleTab blocks={blocks} definitions={definitions} />}

            {/* 相关性热力图跨页签常驻（升级前就在详情区底部，位置不动） */}
            <div className="h-[240px] shrink-0 flex">
              <div className="flex-1 min-w-0">
                <FactorCorrelationHeatmap
                  correlation={correlation}
                  related={related}
                  loading={corrLoading}
                  onPick={onPick}
                />
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

// ─────────────────────────── CSV 导出内容 ───────────────────────────

/** 按当前页签导出**该页签真正在展示的序列**，而不是把整个 JSON 倒出来。 */
function csvPayload(
  tab: DetailTabKey,
  detail: FactorDetail | null,
  blocks: FactorBlocks | null,
): { header: string[]; rows: unknown[][] } {
  if (!detail || detail.empty) return { header: ['无数据'], rows: [] };

  if (tab === 'overview') {
    // 分位表：一行一组。日期序列（换手率、净值曲线）是另一维度的数据，
    // 硬塞进同一张表只会出现大片空列，不如各自成表。
    const q = detail.quantile_mean;
    return {
      header: ['group', 'mean_fwd_return', 'nav_end', 'n_dates'],
      rows: q.map((v, i) => [
        `G${i + 1}`,
        num(v),
        num(detail.quantile_curves?.[i]?.slice(-1)[0]),
        detail.quantile_curves?.[i]?.length ?? '',
      ]),
    };
  }

  if (tab === 'ic') {
    const ic = blocks?.ic_block && blocks.ic_block.available !== false ? blocks.ic_block : null;
    // 半截面 IC 只有「均值」与「累计曲线」两种形态，没有逐日序列 —— 不导出不存在的列
    return {
      header: ['date', 'ic', 'ic_rolling_20', 'ic_neutral'],
      rows: (ic?.dates ?? detail.dates).map((d, i) => [
        d,
        num(ic?.ic_series?.[i] ?? detail.ic_series?.[i]),
        num(ic?.ic_rolling?.[i] ?? detail.ic_rolling?.[i]),
        num(ic?.ic_neutral_series?.[i]),
      ]),
    };
  }

  if (tab === 'group') {
    const gb = blocks?.group_block && blocks.group_block.available !== false ? blocks.group_block : null;
    // 日收益走窗口、累计走全窗口 —— 以**累计轴**为准出行，日收益按尾部对齐取。
    // 直接拿同一个下标 i 去同时索引两根轴，会让后半段每一行的日收益都错位到别的日期上。
    const cumDates = gb?.cum_dates_full ?? gb?.dates ?? [];
    const dailyDates = gb?.dates ?? [];
    // 日频序列相对全窗口起点的偏移；该行日期落在窗口之外时日频列为空，
    // 而不是取到另一天的数（错位不会报错，只会给出一张看着正常的表）。
    const off = Math.max(0, cumDates.length - dailyDates.length);
    const dailyAt = (col: (number | null)[] | undefined, i: number) =>
      (i >= off && i - off < (col?.length ?? 0) ? num(col?.[i - off]) : '—');
    return {
      header: ['date', 'long_daily', 'short_daily', 'ls_daily', 'ls_cum'],
      rows: cumDates.map((d, i) => [
        d,
        dailyAt(gb?.long_daily, i),
        dailyAt(gb?.short_daily, i),
        dailyAt(gb?.ls_daily, i),
        num(gb?.ls_cum?.[i]),
      ]),
    };
  }

  if (tab === 'excess') {
    const ex = blocks?.excess_block && blocks.excess_block.available !== false ? blocks.excess_block : null;
    // 回撤列原先读的是 `long_excess_dd`（**标量**，最大值那一个数）—— 按 i 索引恒为
    // undefined，整列固定输出「—」。这里按与图表相同的口径由累计曲线现算。
    const cum = ex?.long_excess_cum ?? [];
    let peak = -Infinity;
    const dd = cum.map((v) => {
      if (v == null || !Number.isFinite(v)) return null;
      if (v > peak) peak = v;
      return peak > 0 ? +((v / peak - 1) * 100).toFixed(2) : 0;
    });
    return {
      header: ['date', 'long_excess_cum_pct', 'long_excess_dd_pct', 'benchmark'],
      rows: (ex?.dates ?? []).map((d, i) => [
        d,
        cum[i] == null ? '—' : +((cum[i]! - 1) * 100).toFixed(2),
        num(dd[i]),
        ex?.bench_symbol ?? '',
      ]),
    };
  }

  const st = blocks?.style_block && blocks.style_block.available !== false ? blocks.style_block : null;
  return {
    header: ['rank', 'style', 'label', 'mean_corr', 'std_corr', 'n_days'],
    rows: (st?.exposures ?? []).map((e) => [
      e.rank, e.style, e.label, num(e.mean_corr), num(e.std_corr), e.n_days,
    ]),
  };
}

const num = (v: number | null | undefined): number | '—' =>
  v == null || !Number.isFinite(v) ? '—' : +v.toFixed(6);
