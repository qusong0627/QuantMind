/** 聪明钱 · 席位榜（龙虎榜营业部净买额排序，5/20 日窗口切换）。
 *
 * 口径：净买额 = 买入额−卖出额（元→亿展示）；n_days=窗口内上榜天数；
 * 席位行为是事实统计，不含「跟随营业部」类暗示文案。
 */

import React, { useMemo } from 'react';
import { AlertCircle, Store } from 'lucide-react';
import { Tooltip } from 'antd';
import { EmptyHint, FaceCard, PeriodChips, RankRow } from '../../../market-analysis-shared/ui';
import type { SmartBranchRow } from '../../services/smartMoneyService';
import { fmtYiFromYuan, signCls } from '../../smartMoneyVocabulary';
import { AXIS_LABEL, CHART_COLORS, SmartChart } from './charts';

const WINDOW_OPTIONS = [
  { id: '5', label: '5 日' },
  { id: '20', label: '20 日' },
];

interface SmartMoneyBranchBoardProps {
  rows: SmartBranchRow[] | null;
  window: 5 | 20;
  onWindowChange: (w: 5 | 20) => void;
  loading?: boolean;
  /** 窗口切换/加载失败 → 显式错误条（B4：不伪装成空数据） */
  error?: string | null;
  onRetry?: () => void;
  /** true = 无卡壳（统一大框内的标签面） */
  bare?: boolean;
}

export const SmartMoneyBranchBoard: React.FC<SmartMoneyBranchBoardProps> = ({
  rows,
  window,
  onWindowChange,
  loading,
  error,
  onRetry,
  bare = false,
}) => {
  const list = rows ?? [];

  const chartOption = useMemo(() => {
    const top = [...list]
      .filter((r) => r.net_amount !== null && r.net_amount !== undefined)
      .sort((a, b) => Math.abs(Number(b.net_amount)) - Math.abs(Number(a.net_amount)))
      .slice(0, 12);
    if (!top.length) return null;
    const ordered = top.reverse();
    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        textStyle: { fontSize: 10 },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const r = ordered[ps[0]?.dataIndex ?? 0];
          if (!r) return '';
          return [
            r.branch_name,
            `净买 ${fmtYiFromYuan(r.net_amount)} 亿`,
            `买 ${fmtYiFromYuan(r.buy_amount)} 亿 · 卖 ${fmtYiFromYuan(r.sell_amount)} 亿`,
            `上榜 ${r.n_days} 日`,
            r.stocks_bought ? `买入个股：${r.stocks_bought}` : '',
          ]
            .filter(Boolean)
            .join('<br/>');
        },
      },
      grid: { left: 110, right: 40, top: 6, bottom: 18 },
      xAxis: {
        type: 'value',
        axisLabel: { ...AXIS_LABEL },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      yAxis: {
        type: 'category',
        data: ordered.map((r) => r.branch_name),
        axisLabel: { fontSize: 9, color: CHART_COLORS.neutral, width: 104, overflow: 'truncate' },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: CHART_COLORS.axis } },
      },
      series: [
        {
          type: 'bar',
          barMaxWidth: 12,
          data: ordered.map((r) => ({
            value: Number(r.net_amount) / 1e8,
            itemStyle: {
              color:
                Number(r.net_amount) > 0
                  ? CHART_COLORS.pos
                  : Number(r.net_amount) < 0
                    ? CHART_COLORS.neg
                    : CHART_COLORS.neutral,
              borderRadius: [0, 3, 3, 0],
            },
          })),
          label: {
            show: true,
            position: 'right',
            fontSize: 9,
            color: CHART_COLORS.neutral,
            formatter: (p: { value: number | null }) =>
              p.value === null ? '' : `${p.value > 0 ? '+' : ''}${p.value.toFixed(1)}`,
          },
        },
      ],
    };
  }, [list]);

  return (
    <FaceCard
      bare={bare}
      icon={<Store className="w-3.5 h-3.5 text-purple-600" />}
      title="活跃席位（龙虎榜营业部）"
      extra={
        <PeriodChips
          options={WINDOW_OPTIONS}
          value={String(window)}
          onChange={(id) => onWindowChange(id === '20' ? 20 : 5)}
        />
      }
    >
      {error ? (
        <div className="mx-1 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1.5">
          <AlertCircle className="w-3.5 h-3.5 text-amber-600 shrink-0" />
          <span className="text-[10px] text-amber-800 min-w-0 truncate">{error}</span>
          {onRetry && (
            <button
              onClick={onRetry}
              className="ml-auto shrink-0 text-[10px] font-bold text-amber-700 hover:text-amber-900"
            >
              重试
            </button>
          )}
        </div>
      ) : loading && !list.length ? (
        <EmptyHint loading />
      ) : list.length ? (
        <div className="flex flex-col gap-2">
          {chartOption && (
            <div className="rounded-xl border border-slate-100 bg-white/60 p-1.5">
              <div className="text-[10px] font-bold text-slate-500 px-1 pb-1">
                营业部净买 Top 12 <span className="font-normal text-slate-400">（亿 · 红买绿卖）</span>
              </div>
              <SmartChart option={chartOption} height={Math.max(180, Math.min(12, list.length) * 20 + 40)} />
            </div>
          )}
          <details className="rounded-xl border border-slate-100 bg-white/60 px-2 py-1.5">
            <summary className="cursor-pointer select-none text-[10px] font-bold text-slate-500">
              席位明细（{list.length}）· 买入个股与买卖拆分
            </summary>
            <div className="mt-1 flex flex-col">
          {list.map((b, i) => (
            <RankRow
              key={b.branch_name}
              rank={i + 1}
              name={b.branch_name}
              nameSub={b.n_days > 0 ? `${b.n_days} 日` : undefined}
              main={
                b.stocks_bought ? (
                  <Tooltip title={`窗口内买入个股：${b.stocks_bought}`}>
                    <span className="text-[9px] text-slate-400 truncate">{b.stocks_bought}</span>
                  </Tooltip>
                ) : null
              }
              right={
                <Tooltip
                  title={`买 ${fmtYiFromYuan(b.buy_amount)} 亿 · 卖 ${fmtYiFromYuan(b.sell_amount)} 亿`}
                >
                  <span className={`text-[11px] font-mono font-extrabold ${signCls(b.net_amount)}`}>
                    {fmtYiFromYuan(b.net_amount)}
                    <span className="text-[9px] font-normal text-slate-400 ml-0.5">亿</span>
                  </span>
                </Tooltip>
              }
            />
          ))}
            </div>
          </details>
        </div>
      ) : (
        <EmptyHint text={`近 ${window} 日无席位记录`} />
      )}
    </FaceCard>
  );
};
