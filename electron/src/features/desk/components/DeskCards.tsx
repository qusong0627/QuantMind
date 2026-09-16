/** 交易台其余卡片：候选信号 / 执行 / 盈亏 / 健康（每块带 source 下钻） */

import React from 'react';
import { Activity, BarChart3, HeartPulse, Wallet } from 'lucide-react';
import type { ExecutionBlock, HealthBlock, PnlBlock, SignalsBlock } from '../types';
import { TermTooltip } from '../../shared/TermTooltip';
import { EvalScoreBadge } from '../../../components/shared/EvalScoreBadge';
import { useUiMode } from '../../shared/useUiMode';
import {
  executionSummary,
  formatMoney,
  formatPct,
  healthItemViews,
  pnlSummary,
} from '../deskModel';

function SourceFooter({ source, onDrillDown }: { source: string; onDrillDown?: () => void }) {
  const { isSimple } = useUiMode();
  if (isSimple && !onDrillDown) return null; // 简单模式收起技术来源（专业模式/下钻保留）
  return (
    <footer className="text-[10px] text-slate-400 mt-2">
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

export const SignalsCard: React.FC<{ signals: SignalsBlock | null | undefined }> = ({ signals }) => (
  <section className="bg-white rounded-2xl border border-gray-200 p-4">
    <header className="flex items-center gap-2 mb-2">
      <BarChart3 className="w-4 h-4 text-blue-600" />
      <h3 className="text-sm font-semibold text-slate-800">候选信号</h3>
      <span className="text-[11px] text-slate-400">{signals?.trade_date || '—'}</span>
      {signals?.trade_date && (
        <span className="ml-auto">
          <EvalScoreBadge objectType="daily_selection" objectId={signals.trade_date} prefix="选股评分" />
        </span>
      )}
    </header>
    <div className="flex gap-3 text-[11px] text-slate-600 mb-2">
      <span className="text-red-600">BUY {signals?.buy ?? '—'}</span>
      <span className="text-emerald-600">SELL {signals?.sell ?? '—'}</span>
      <span>HOLD {signals?.hold ?? '—'}</span>
    </div>
    <div className="space-y-1">
      {(signals?.top_buy || []).slice(0, 5).map((item) => (
        <div key={item.symbol} className="flex items-center justify-between text-xs">
          <span className="text-slate-800">{item.symbol}</span>
          <span className="text-slate-500">
            <TermTooltip term="rank_pct">rank_pct</TermTooltip>{' '}
            {item.rank_pct === null || item.rank_pct === undefined ? '—' : item.rank_pct.toFixed(3)}
            <span className="text-slate-400"> · score {item.score?.toFixed(4) ?? '—'}</span>
          </span>
        </div>
      ))}
      {(!signals?.top_buy || signals.top_buy.length === 0) && (
        <p className="text-xs text-slate-400">暂无 BUY 信号</p>
      )}
    </div>
    <SourceFooter source={signals?.source || '—'} />
  </section>
);

export const ExecutionCard: React.FC<{ execution: ExecutionBlock | null | undefined }> = ({ execution }) => {
  const summary = executionSummary(execution);
  return (
    <section className="bg-white rounded-2xl border border-gray-200 p-4">
      <header className="flex items-center gap-2 mb-2">
        <Activity className="w-4 h-4 text-blue-600" />
        <h3 className="text-sm font-semibold text-slate-800">今日执行</h3>
      </header>
      <div className="flex gap-3 text-[11px] text-slate-600 mb-2">
        <span>SIM {summary.simCount}</span>
        <span>REAL {summary.realCount}</span>
        <span className="text-red-600">成交 {summary.filled}</span>
        <span className={summary.rejected > 0 ? 'text-amber-600' : ''}>拒单 {summary.rejected}</span>
      </div>
      <div className="space-y-1 max-h-[180px] overflow-y-auto pr-1">
        {(execution?.items || []).slice(0, 6).map((item, index) => (
          <div key={`${item.client_order_id || item.symbol}-${index}`} className="text-xs flex items-center gap-2">
            <span
              className={`text-[10px] px-1 py-0.5 rounded border ${
                item.mode === 'REAL'
                  ? 'bg-purple-50 text-purple-700 border-purple-200'
                  : 'bg-slate-100 text-slate-600 border-slate-200'
              }`}
            >
              {item.mode}
            </span>
            <span
              className={String(item.side).toUpperCase() === 'BUY' ? 'text-red-600' : 'text-emerald-600'}
            >
              {String(item.side).toUpperCase() === 'BUY' ? '买' : '卖'}
            </span>
            <span className="text-slate-800">{item.symbol}</span>
            <span className="text-slate-400">{item.status}</span>
            {item.price_source && <span className="text-slate-400">{item.price_source}</span>}
          </div>
        ))}
        {(!execution?.items || execution.items.length === 0) && (
          <p className="text-xs text-slate-400">今日暂无委托（含盘前挂单）</p>
        )}
      </div>
      <SourceFooter source={execution?.source || '—'} />
    </section>
  );
};

export const PnlCard: React.FC<{
  pnl: PnlBlock | null | undefined;
  positionCount?: number;
  onDrillDown?: () => void;
}> = ({ pnl, positionCount, onDrillDown }) => {
  const summary = pnlSummary(pnl);
  return (
    <section className="bg-white rounded-2xl border border-gray-200 p-4">
      <header className="flex items-center gap-2 mb-2">
        <Wallet className="w-4 h-4 text-blue-600" />
        <h3 className="text-sm font-semibold text-slate-800">账户盈亏</h3>
        {pnl?.snapshot_date && <span className="text-[11px] text-slate-400">{pnl.snapshot_date}</span>}
      </header>
      {!summary.available ? (
        <p className="text-xs text-slate-500 py-4 text-center">{pnl?.detail || '无资金快照'}</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <div className="text-slate-400">总资产</div>
              <div className="text-slate-800 font-semibold">{formatMoney(pnl?.total_asset)}</div>
            </div>
            <div>
              <div className="text-slate-400">累计收益</div>
              <div
                className={`font-semibold ${
                  summary.totalPnl >= 0 ? 'text-red-600' : 'text-emerald-600'
                }`}
              >
                {formatMoney(summary.totalPnl)}
                {summary.returnPct !== null ? `（${formatPct(summary.returnPct)}）` : ''}
              </div>
            </div>
            <div>
              <div className="text-slate-400">今日盈亏</div>
              <div className={`font-semibold ${summary.todayPnl >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                {formatMoney(summary.todayPnl)}
              </div>
            </div>
            <div>
              <div className="text-slate-400">持仓市值</div>
              <div className="text-slate-800 font-semibold">
                {formatMoney(pnl?.market_value)}
                {positionCount ? <span className="text-slate-400 font-normal"> · {positionCount} 只</span> : null}
              </div>
            </div>
          </div>
        </>
      )}
      <SourceFooter source={pnl?.source || '—'} onDrillDown={onDrillDown} />
    </section>
  );
};

export const HealthCard: React.FC<{ health: HealthBlock | null | undefined }> = ({ health }) => {
  const items = healthItemViews(health);
  return (
    <section className="bg-white rounded-2xl border border-gray-200 p-4">
      <header className="flex items-center gap-2 mb-2">
        <HeartPulse className="w-4 h-4 text-blue-600" />
        <h3 className="text-sm font-semibold text-slate-800">系统健康</h3>
        <span className="text-[11px] text-slate-500">
          {health?.ok ?? 0} 正常 / {health?.warn ?? 0} 警告 / {health?.fail ?? 0} 异常
        </span>
      </header>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <span
            key={item.id}
            title={`${item.detail}${item.suggestion ? `\n建议：${item.suggestion}` : ''}`}
            className={`text-[11px] px-2 py-0.5 rounded-full border inline-flex items-center gap-1 ${item.style.text} border-gray-200 bg-white`}
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
