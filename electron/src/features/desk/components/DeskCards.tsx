/** 交易台其余卡片：候选信号 / 执行 / 盈亏 / 健康（浅色专业金融风；每块带 source 下钻）
 *
 * 排版口径：名称+代码（StockLabel）、数字一律 font-mono tabular-nums、
 * 卡片头部=图标色块+标题+微统计；行 hover 可点 → 居中下钻弹窗。
 */

import React from 'react';
import { Activity, BarChart3, HeartPulse, Wallet } from 'lucide-react';
import type { ExecutionBlock, ExecutionItem, HealthBlock, PnlBlock, SignalItem, SignalsBlock } from '../types';
import { TermTooltip } from '../../shared/TermTooltip';
import { EvalScoreBadge } from '../../../components/shared/EvalScoreBadge';
import { useUiMode } from '../../shared/useUiMode';
import { ComplianceReturn } from '../../../components/shared/compliance/ComplianceChrome';
import { StockLabel } from './StockLabel';
import { CARD, CardHeader, StatTile } from './cardKit';
import {
  executionSummary,
  formatMoney,
  healthItemViews,
  pnlSummary,
  priceSourceHint,
} from '../deskModel';

function SourceFooter({ source, onDrillDown }: { source: string; onDrillDown?: () => void }) {
  const { isSimple } = useUiMode();
  if (isSimple && !onDrillDown) return null; // 简单模式收起技术来源（专业模式/下钻保留）
  return (
    <footer className="text-[10px] text-slate-400 mt-2.5">
      {onDrillDown ? (
        <button
          type="button"
          onClick={onDrillDown}
          className="hover:text-blue-600 underline decoration-dotted underline-offset-2"
          title="下钻：来源链与原始载荷"
        >
          来源：{source}（点击下钻）
        </button>
      ) : (
        <>来源：{source}</>
      )}
    </footer>
  );
}

export const SignalsCard: React.FC<{
  signals: SignalsBlock | null | undefined;
  /** T-FE-03 v2：条目级逐层下钻（信号字段 → 原始条目 → 信号块载荷） */
  onItemDrill?: (item: SignalItem) => void;
}> = ({ signals, onItemDrill }) => (
  <section className={CARD}>
    <CardHeader
      icon={<BarChart3 className="h-4 w-4" />}
      title="候选信号"
      meta={
        signals?.trade_date ? (
          <span className="text-[11px] font-mono text-slate-400">{signals.trade_date}</span>
        ) : null
      }
      extra={
        signals?.trade_date ? (
          <EvalScoreBadge objectType="daily_selection" objectId={signals.trade_date} prefix="选股评分" />
        ) : null
      }
    />

    <div className="grid grid-cols-3 gap-2 mb-3">
      <StatTile label="BUY" value={signals?.buy} tone="red" />
      <StatTile label="SELL" value={signals?.sell} tone="green" />
      <StatTile label="HOLD" value={signals?.hold} tone="slate" />
    </div>

    <div className="space-y-0.5 flex-1 min-h-0 overflow-y-auto">
      {(signals?.top_buy || []).map((item, index) => {
        const rank = item.rank_pct === null || item.rank_pct === undefined ? null : item.rank_pct;
        return (
          <button
            key={item.symbol}
            type="button"
            disabled={!onItemDrill}
            onClick={onItemDrill ? () => onItemDrill(item) : undefined}
            title={onItemDrill ? '点击下钻：信号字段 → 原始条目' : undefined}
            className={`w-full text-left flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors ${onItemDrill ? 'hover:bg-slate-50 cursor-pointer' : 'cursor-default'}`}
          >
            <span className="text-[10px] font-mono text-slate-300 w-3 shrink-0 tabular-nums">
              {index + 1}
            </span>
            <StockLabel symbol={item.symbol} name={item.name} className="min-w-0" />
            <span className="ml-auto flex items-center gap-2 shrink-0">
              {rank !== null && (
                <span className="h-1 w-12 rounded-full bg-slate-100 overflow-hidden hidden sm:block">
                  <span
                    className="block h-full rounded-full bg-red-400"
                    style={{ width: `${Math.max(0, Math.min(1, rank)) * 100}%` }}
                  />
                </span>
              )}
              <TermTooltip term="rank_pct">
                <span className="text-[11px] font-mono text-slate-500 tabular-nums">
                  {rank === null ? '—' : rank.toFixed(3)}
                </span>
              </TermTooltip>
              <span className="text-[10px] font-mono text-slate-400 tabular-nums w-14 text-right">
                {item.score === null || item.score === undefined ? '—' : item.score.toFixed(4)}
              </span>
            </span>
          </button>
        );
      })}
      {(!signals?.top_buy || signals.top_buy.length === 0) && (
        <p className="text-xs text-slate-400 px-2 py-1">暂无 BUY 信号</p>
      )}
    </div>
    {!!signals?.buy && (
      <p className="px-2 pt-1.5 text-[10px] text-slate-400">
        共 {signals.buy} 只 BUY 信号，按 rank 分位强弱展示前 {signals.top_buy?.length || 0} 只（列表内可滚动）
      </p>
    )}
    <SourceFooter source={signals?.source || '—'} />
  </section>
);

/** 执行状态 → 中文 pill（数值型状态原样展示，不丢失信息） */
function executionStatusView(status: string): { label: string; cls: string } {
  const s = String(status || '').toUpperCase();
  if (s === 'FILLED') return { label: '成交', cls: 'bg-red-50 text-red-700 border-red-200' };
  if (s === 'REJECTED') return { label: '拒单', cls: 'bg-rose-50 text-rose-700 border-rose-200' };
  if (s === 'CANCELLED' || s === 'CANCELED') return { label: '已撤', cls: 'bg-slate-100 text-slate-500 border-slate-200' };
  if (s === 'SUBMITTED' || s === 'PENDING' || s === 'NEW') return { label: '已报', cls: 'bg-amber-50 text-amber-700 border-amber-200' };
  return { label: status || '—', cls: 'bg-slate-50 text-slate-600 border-slate-200' };
}

export const ExecutionCard: React.FC<{
  execution: ExecutionBlock | null | undefined;
  /** T-FE-03 v2：条目级逐层下钻（订单字段 → 取价来源 → 原始条目） */
  onItemDrill?: (item: ExecutionItem) => void;
}> = ({ execution, onItemDrill }) => {
  const summary = executionSummary(execution);
  return (
    <section className={CARD}>
      <CardHeader icon={<Activity className="h-4 w-4" />} title="今日执行" />

      <div className="grid grid-cols-4 gap-1.5 mb-3">
        <StatTile label="SIM" value={summary.simCount} tone="slate" />
        <StatTile label="REAL" value={summary.realCount} tone="slate" />
        <StatTile label="成交" value={summary.filled} tone="red" />
        <StatTile label="拒单" value={summary.rejected} tone={summary.rejected > 0 ? 'amber' : 'slate'} />
      </div>

      <div className="space-y-0.5 max-h-[190px] overflow-y-auto pr-1 flex-1">
        {(execution?.items || []).slice(0, 6).map((item, index) => {
          const statusView = executionStatusView(item.status);
          const isBuy = String(item.side).toUpperCase() === 'BUY';
          return (
            <button
              key={`${item.client_order_id || item.symbol}-${index}`}
              type="button"
              disabled={!onItemDrill}
              onClick={onItemDrill ? () => onItemDrill(item) : undefined}
              title={item.price_source ? `取价来源：${priceSourceHint(item.price_source)}` : undefined}
              className={`w-full text-left flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors ${onItemDrill ? 'hover:bg-slate-50 cursor-pointer' : 'cursor-default'}`}
            >
              <span
                className={`text-[9px] font-semibold px-1 py-0.5 rounded border shrink-0 ${
                  item.mode === 'REAL'
                    ? 'bg-purple-50 text-purple-700 border-purple-200'
                    : 'bg-slate-100 text-slate-500 border-slate-200'
                }`}
              >
                {item.mode}
              </span>
              <span className={`text-[12px] font-semibold shrink-0 ${isBuy ? 'text-red-600' : 'text-emerald-600'}`}>
                {isBuy ? '买' : '卖'}
              </span>
              <StockLabel symbol={item.symbol} name={item.name} className="min-w-0" />
              <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full border shrink-0 ${statusView.cls}`}>
                {statusView.label}
              </span>
            </button>
          );
        })}
        {(!execution?.items || execution.items.length === 0) && (
          <p className="text-xs text-slate-400 px-2 py-1">今日暂无委托（含盘前挂单）</p>
        )}
      </div>
      <SourceFooter source={execution?.source || '—'} />
    </section>
  );
};

const PnlMetric: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="min-w-0">
    <div className="text-[10px] text-slate-400 mb-0.5">{label}</div>
    <div className="text-[13px] font-semibold font-mono tabular-nums leading-5 truncate">{children}</div>
  </div>
);

export const PnlCard: React.FC<{
  pnl: PnlBlock | null | undefined;
  positionCount?: number;
  onDrillDown?: () => void;
}> = ({ pnl, positionCount, onDrillDown }) => {
  const summary = pnlSummary(pnl);
  const upCls = summary.totalPnl >= 0 ? 'text-red-600' : 'text-emerald-600';
  const todayCls = summary.todayPnl >= 0 ? 'text-red-600' : 'text-emerald-600';
  return (
    <section className={CARD}>
      <CardHeader
        icon={<Wallet className="h-4 w-4" />}
        title="账户盈亏"
        meta={
          pnl?.snapshot_date ? (
            <span className="text-[11px] font-mono text-slate-400">{pnl.snapshot_date}</span>
          ) : null
        }
      />
      {!summary.available ? (
        <p className="text-xs text-slate-500 py-6 text-center flex-1">{pnl?.detail || '无资金快照'}</p>
      ) : (
        <div className="flex-1 flex flex-col justify-center gap-3">
          <div>
            <div className="text-[11px] text-slate-400 mb-0.5">总资产</div>
            <div className="text-2xl font-bold font-mono tabular-nums text-slate-900 leading-8">
              {formatMoney(pnl?.total_asset)}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3 pt-3 border-t border-slate-100">
            <PnlMetric label="累计收益">
              <span className={upCls}>{formatMoney(summary.totalPnl)}</span>
              {summary.returnPct !== null && (
                <div className="text-[10px] font-normal text-slate-500 mt-0.5">
                  <ComplianceReturn value={summary.returnPct} windowText={pnl?.snapshot_date || undefined} />
                </div>
              )}
            </PnlMetric>
            <PnlMetric label="今日盈亏">
              <span className={todayCls}>{formatMoney(summary.todayPnl)}</span>
            </PnlMetric>
            <PnlMetric label="持仓市值">
              <span className="text-slate-800">{formatMoney(pnl?.market_value)}</span>
              {positionCount ? (
                <span className="text-slate-400 font-normal text-[10px]"> · {positionCount} 只</span>
              ) : null}
            </PnlMetric>
          </div>
        </div>
      )}
      <SourceFooter source={pnl?.source || '—'} onDrillDown={onDrillDown} />
    </section>
  );
};

export const HealthCard: React.FC<{ health: HealthBlock | null | undefined }> = ({ health }) => {
  const items = healthItemViews(health);
  return (
    <section className={CARD}>
      <CardHeader
        icon={<HeartPulse className="h-4 w-4" />}
        title="系统健康"
        extra={
          <span className="inline-flex items-center gap-2 text-[11px] font-medium text-slate-500">
            <span className="inline-flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
              {health?.ok ?? 0} 正常
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
              {health?.warn ?? 0} 警告
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-rose-600" />
              {health?.fail ?? 0} 异常
            </span>
          </span>
        }
      />
      <div className="flex flex-wrap gap-1.5 flex-1 content-start">
        {items.map((item) => (
          <span
            key={item.id}
            title={`${item.detail}${item.suggestion ? `\n建议：${item.suggestion}` : ''}`}
            className={`text-[11px] px-2 py-0.5 rounded-full border border-slate-200 bg-white inline-flex items-center gap-1.5 cursor-default hover:border-slate-300 transition-colors ${item.style.text}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${item.style.dot}`} />
            {item.name}
          </span>
        ))}
        {items.length === 0 && <p className="text-xs text-slate-400">体检未运行（?health=false）</p>}
      </div>
      <SourceFooter source={health?.source || '—'} />
    </section>
  );
};
