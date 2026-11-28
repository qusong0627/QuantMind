import React, { useEffect, useRef, useState } from 'react';
import { clsx } from 'clsx';
import type { SignalEvalReport, SignalEvalTopN } from '../services/modelSignalEvalTypes';

type Execution = SignalEvalReport['execution'];

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;
}

function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

function fmtWan(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(2)} 亿`;
  return `${Math.round(value / 1e4)} 万`;
}

/** A 股习惯：红涨绿跌（收益/超额专用；徽章仍用绿/红语义）。 */
function returnTone(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'text-slate-400';
  return value > 0 ? 'text-rose-500' : value < 0 ? 'text-emerald-600' : 'text-slate-500';
}

const TOP_BADGE: Record<number, string> = {
  20: 'bg-indigo-50 text-indigo-700 ring-indigo-100',
  50: 'bg-sky-50 text-sky-700 ring-sky-100',
  100: 'bg-slate-100 text-slate-600 ring-slate-200',
};

const GROUP_HEAD: Record<string, string> = {
  ret: 'bg-indigo-50/50 text-indigo-600',
  idx: 'bg-sky-50/50 text-sky-600',
  risk: 'bg-amber-50/50 text-amber-600',
  cap: 'bg-slate-100/70 text-slate-500',
};

const GroupHead: React.FC<{
  label: string;
  colSpan: number;
  tone: keyof typeof GROUP_HEAD;
  title?: string;
  divided?: boolean;
}> = ({ label, colSpan, tone, title, divided = false }) => (
  <th
    colSpan={colSpan}
    title={title}
    className={clsx(
      'h-7 border-b border-slate-100 px-2 text-center text-[8px] font-black uppercase tracking-[0.16em]',
      GROUP_HEAD[tone],
      divided && 'border-l border-l-slate-200/70',
    )}
  >
    {label}
  </th>
);

const SubHead: React.FC<{
  children: React.ReactNode;
  title?: string;
  divided?: boolean;
}> = ({ children, title, divided = false }) => (
  <th
    title={title}
    className={clsx(
      'h-8 whitespace-nowrap border-b border-slate-100 px-2.5 text-right text-[9px] font-bold text-slate-500',
      divided && 'border-l border-l-slate-200/70 bg-slate-50/40',
    )}
  >
    {children}
  </th>
);

export interface SignalEvalTopNTableProps {
  rows: SignalEvalTopN[];
  execution?: Execution;
}

/** TopN 长多表：收益 / 四大指数超额 / 风险 / 容量 四组，严格实盘口径。 */
export const SignalEvalTopNTable: React.FC<SignalEvalTopNTableProps> = ({ rows, execution }) => {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  const [contentWidth, setContentWidth] = useState(980);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const update = () => {
      setOverflow(el.scrollWidth > el.clientWidth + 1);
      setContentWidth(el.scrollWidth);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [rows]);

  const syncFromTable = () => {
    if (bottomRef.current && scrollRef.current) {
      bottomRef.current.scrollLeft = scrollRef.current.scrollLeft;
    }
  };
  const syncFromBottom = () => {
    if (bottomRef.current && scrollRef.current) {
      scrollRef.current.scrollLeft = bottomRef.current.scrollLeft;
    }
  };

  // 口径跟随模型训练时的 execution_price：open = T+1 开盘，close = T+1 收盘。
  const basis = execution?.benchmark_basis ?? 'open→open';
  const entryWord = (execution?.entry ?? 'T+1 开盘').includes('收盘') ? '收盘' : '开盘';
  const exitWord = (execution?.exit ?? '').includes('收盘') ? '收盘' : '开盘';

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.03)]">
      <div
        ref={scrollRef}
        onScroll={syncFromTable}
        className="table-scroll-hidden overflow-x-auto"
      >
        <table className="w-full min-w-[1120px] table-fixed border-collapse text-[10px]">
          <colgroup>
            <col className="w-[76px]" />
            <col className="w-[86px]" />
            <col className="w-[108px]" />
            <col className="w-[86px]" />
            <col className="w-[86px]" />
            <col className="w-[86px]" />
            <col className="w-[90px]" />
            <col className="w-[84px]" />
            <col className="w-[62px]" />
            <col className="w-[76px]" />
            <col className="w-[84px]" />
          </colgroup>
          <thead>
            <tr>
              <th
                rowSpan={2}
                className="sticky left-0 z-20 h-[60px] border-b border-slate-100 bg-white px-3 text-left align-middle text-[9px] font-black text-slate-500"
              >
                组合
              </th>
              <GroupHead label="收益" colSpan={2} tone="ret" />
              <GroupHead
                label={`指数超额 · ${basis}`}
                colSpan={4}
                tone="idx"
                divided
                title={`同期 TopN 收益 − 指数同期 ${basis} 收益；红正绿负`}
              />
              <GroupHead label="风险" colSpan={2} tone="risk" divided />
              <GroupHead label="容量" colSpan={2} tone="cap" divided />
            </tr>
            <tr className="bg-white">
              <SubHead>近 3 月</SubHead>
              <SubHead title="相对同窗口打分池等权收益">vs 打分池</SubHead>
              <SubHead title="相对沪深300（399300.SZ）" divided>沪深300</SubHead>
              <SubHead title="相对上证指数（000001.SH）">上证</SubHead>
              <SubHead title="相对深证成指（399001.SZ）">深证</SubHead>
              <SubHead title="相对中证1000（000852.SH）">中证1000</SubHead>
              <SubHead title={`T+1 ${entryWord}买入、持有期内的最大回撤`} divided>最大回撤</SubHead>
              <SubHead title="日收益年化夏普（252 日）">夏普</SubHead>
              <SubHead title="通过 ADV/涨停/ST 过滤后的平均持仓只数" divided>平均持仓</SubHead>
              <SubHead title="5% 参与率、按最不流动持仓估算的容量">容量(5%)</SubHead>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.n}
                className="group border-b border-slate-100/80 last:border-0 hover:bg-slate-50/60"
              >
                <td className="sticky left-0 z-10 h-10 bg-white px-3 align-middle group-hover:bg-slate-50/60">
                  <span
                    className={clsx(
                      'inline-flex min-w-[46px] items-center justify-center rounded-md px-1.5 py-0.5 font-mono text-[10px] font-black ring-1',
                      TOP_BADGE[row.n] ?? 'bg-slate-100 text-slate-600 ring-slate-200',
                    )}
                  >
                    Top{row.n}
                  </span>
                </td>
                <td className={clsx('h-10 px-2.5 text-right align-middle font-mono font-black tabular-nums', returnTone(row.total_return))}>
                  {fmtPct(row.total_return)}
                </td>
                <td className="h-10 px-2.5 text-right align-middle font-mono tabular-nums">
                  <span className={returnTone(row.excess)}>{fmtPct(row.excess)}</span>
                  <span className="ml-1 text-[8px] font-normal text-slate-300">t {fmtNum(row.excess_t, 1)}</span>
                </td>
                <td className={clsx('h-10 border-l border-l-slate-100 px-2.5 text-right align-middle font-mono tabular-nums', returnTone(row.excess_hs300))}>
                  {fmtPct(row.excess_hs300)}
                </td>
                <td className={clsx('h-10 px-2.5 text-right align-middle font-mono tabular-nums', returnTone(row.excess_sse))}>
                  {fmtPct(row.excess_sse)}
                </td>
                <td className={clsx('h-10 px-2.5 text-right align-middle font-mono tabular-nums', returnTone(row.excess_szse))}>
                  {fmtPct(row.excess_szse)}
                </td>
                <td className={clsx('h-10 px-2.5 text-right align-middle font-mono tabular-nums', returnTone(row.excess_zz1000))}>
                  {fmtPct(row.excess_zz1000)}
                </td>
                <td className="h-10 border-l border-l-slate-100 px-2.5 text-right align-middle font-mono tabular-nums text-slate-500">
                  {fmtPct(row.mdd)}
                </td>
                <td className="h-10 px-2.5 text-right align-middle font-mono tabular-nums text-slate-500">{fmtNum(row.sharpe)}</td>
                <td className="h-10 border-l border-l-slate-100 px-2.5 text-right align-middle font-mono tabular-nums text-slate-500">
                  {fmtNum(row.avg_names, 1)}
                </td>
                <td className="h-10 px-2.5 text-right align-middle font-mono tabular-nums text-slate-500">{fmtWan(row.capacity_p5)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-slate-100 bg-slate-50/50 px-3 py-2 text-[9px] text-slate-400">
        <span>
          指数超额 = 同期 TopN 收益 − 指数 {basis} 收益；<span className="text-rose-400">红正</span>
          <span className="text-emerald-500">绿负</span>。
        </span>
        <span className="flex flex-wrap items-center gap-1.5">
          {execution ? (
            <>
              <span className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">{execution.entry}买</span>
              <span className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">{execution.exit}卖</span>
              <span className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">
                {execution.cost_bps_per_side}bp/边
              </span>
              <span className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">
                ADV≥{(execution.adv_floor_yuan / 1e4).toFixed(0)}万
              </span>
              {execution.st_pit_excluded ? (
                <span className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">PIT ST 剔除</span>
              ) : null}
            </>
          ) : (
            <span>T+1 {entryWord}买 · 期末{exitWord}卖 · 双边成本</span>
          )}
        </span>
      </div>

      {overflow ? (
        <div className="border-t border-slate-100 bg-white px-3 py-1.5">
          <div
            ref={bottomRef}
            onScroll={syncFromBottom}
            className="table-scroll overflow-x-auto"
            aria-hidden
          >
            <div style={{ width: contentWidth, height: 1 }} />
          </div>
        </div>
      ) : null}
    </div>
  );
};
