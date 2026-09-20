/**
 * 持仓预警面板（持仓哨兵的站内面板）
 *
 * 数据源：`/api/v1/trading/holding-alerts`（默认只看 active）。与副驾驶面板并列挂在交易台，
 * 也用同一套卡壳（`features/desk/components/cardKit`）保持观感一致。
 *
 * 三条纪律：
 * 1. **哨兵状态如实展示**：没在跑 / 心跳过期 / 你名下 0 只，都要显式说出来 ——
 *    否则「列表为空」会被读成「我的持仓没问题」，而这正是最危险的误读。
 * 2. **一键卖出**复用候选推送的 `PushConfirmPanel(side='sell')`：数量由服务端按
 *    `available_volume` 给，预检逐笔列风险，**不自动下单**（用户明确要求只提醒 + 手动确认）。
 * 3. **只有真提交出去才回写「已卖出」**（`shouldMarkExecuted`）；被拦截/预演/零成功
 *    一律保持待处理，绝不显示假的已完成。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertTriangle,
  CheckCheck,
  Loader2,
  RefreshCw,
  ShieldAlert,
  X,
} from 'lucide-react';
import { CARD, CardHeader, StatTile } from '../desk/components/cardKit';
import { PushConfirmPanel } from '../stock-terminal/components/PushConfirmPanel';
import {
  holdingAlertService,
  type HoldingAlertConfig,
  type HoldingAlertItem,
  type HoldingSentinelStatus,
} from '../../services/holdingAlertService';
import type { PushChannel } from '../stock-terminal-shared/types';
import { toSuffixCode } from '../../utils/portfolioUtils';
import {
  alertActionTarget,
  alertAgeText,
  kindLabel,
  panelCounts,
  scoreTransition,
  sentinelHeadline,
  severityMeta,
  shouldMarkExecuted,
  type PushOutcomeLike,
} from './alertModel';

/** 面板自身刷新节奏（投递通道另有 30s 轮询，两者互不依赖） */
export const PANEL_POLL_MS = 60_000;

const ROW_TONE: Record<string, string> = {
  red: 'border-red-100 bg-red-50/40',
  amber: 'border-amber-100 bg-amber-50/40',
  blue: 'border-slate-200/80 bg-white',
  slate: 'border-slate-200/80 bg-white',
};

const HEADLINE_TONE: Record<string, string> = {
  red: 'border-red-200 bg-red-50 text-red-700',
  amber: 'border-amber-200 bg-amber-50 text-amber-700',
  green: 'border-emerald-100 bg-emerald-50/70 text-emerald-700',
  blue: 'border-blue-100 bg-blue-50 text-blue-600',
  slate: 'border-slate-200 bg-slate-50 text-slate-600',
};

const DEFAULT_CHANNELS: PushChannel[] = ['sim'];

export const HoldingAlertPanel: React.FC = () => {
  const navigate = useNavigate();
  const [items, setItems] = useState<HoldingAlertItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [config, setConfig] = useState<HoldingAlertConfig | null>(null);
  const [sentinel, setSentinel] = useState<HoldingSentinelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState(0);
  const [notice, setNotice] = useState('');
  /** 正在卖出/清仓的那条预警（null = 面板关着） */
  const [sellTarget, setSellTarget] = useState<HoldingAlertItem | null>(null);
  const [channels, setChannels] = useState<PushChannel[]>(DEFAULT_CHANNELS);

  const load = useCallback(async () => {
    setError('');
    try {
      const result = await holdingAlertService.listAlerts({ status: 'active', limit: 50 });
      setItems(result.items);
      setCounts(result.counts);
      setConfig(result.config);
      setSentinel(result.sentinel);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { void load(); }, PANEL_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const headline = useMemo(() => sentinelHeadline(sentinel), [sentinel]);
  const { active, criticalInPage } = useMemo(() => panelCounts(counts, items), [counts, items]);
  const monitored = sentinel?.mine?.monitored ?? sentinel?.monitored ?? null;

  const handleDismiss = useCallback(async (item: HoldingAlertItem) => {
    setBusyId(item.id);
    setError('');
    try {
      const changed = await holdingAlertService.dismissAlert(item.id);
      setNotice(changed ? '已忽略该预警' : '该预警已处理过，无需重复操作');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(0);
    }
  }, [load]);

  const handleMarkExecuted = useCallback(async (item: HoldingAlertItem) => {
    setBusyId(item.id);
    setError('');
    try {
      const changed = await holdingAlertService.markExecuted(item.id);
      setNotice(changed ? '已标记为已卖出' : '该预警已处理过，无需重复操作');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(0);
    }
  }, [load]);

  const handleSellDone = useCallback(async (outcome: PushOutcomeLike) => {
    const target = sellTarget;
    setSellTarget(null);
    if (!target) return;
    if (!shouldMarkExecuted(outcome)) {
      // 没真发出去就不动状态；否则「已卖出」是假的
      setNotice('本次未提交成功，预警保持待处理——请查看推送面板里的逐笔原因');
      return;
    }
    try {
      await holdingAlertService.markExecuted(target.id);
      setNotice(`${target.stockName || target.symbol} 卖出已提交，预警标记为已卖出`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      await load();
    }
  }, [sellTarget, load]);

  return (
    <section className={CARD} data-testid="holding-alert-panel">
      <CardHeader
        icon={<ShieldAlert size={15} />}
        title="持仓预警 · 哨兵"
        meta={
          <span className={`rounded-md border px-1.5 py-0.5 text-[10px] font-semibold ${HEADLINE_TONE[headline.tone]}`}>
            {headline.warn ? '需注意' : '运行中'}
          </span>
        }
        extra={
          <button
            type="button"
            onClick={() => void load()}
            className="flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            刷新
          </button>
        }
      />

      {/* 哨兵存活：红/琥珀条就是「现在不会有预警」或「预警可能滞后」的明说 */}
      <div className={`mb-2 flex items-start gap-1.5 rounded-lg border px-2 py-1.5 text-[11px] ${HEADLINE_TONE[headline.tone]}`}>
        {headline.warn ? <AlertTriangle size={12} className="mt-0.5 shrink-0" /> : <CheckCheck size={12} className="mt-0.5 shrink-0" />}
        <span>{headline.text}</span>
      </div>

      {error && (
        <div className="mb-2 rounded-lg border border-red-100 bg-red-50 px-2 py-1 text-[11px] text-red-600">
          {error}
        </div>
      )}
      {notice && (
        <div className="mb-2 flex items-start gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] text-slate-600">
          <span className="flex-1">{notice}</span>
          <button type="button" onClick={() => setNotice('')} className="shrink-0 text-slate-400 hover:text-slate-600">
            <X size={11} />
          </button>
        </div>
      )}

      <div className="mb-3 grid grid-cols-3 gap-2">
        <StatTile label="待处理" value={active} tone={active > 0 ? 'amber' : 'slate'} />
        <StatTile label="危急(本页)" value={criticalInPage} tone={criticalInPage > 0 ? 'red' : 'slate'} />
        <StatTile label="监控标的" value={monitored ?? '—'} tone="blue" />
      </div>

      {items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 px-2 py-3 text-[11px] text-slate-400">
          {!sentinel?.running
            ? '哨兵未运行，当前不会产生新预警'
            : active === 0
              ? '暂无待处理预警'
              : '暂无数据'}
        </div>
      ) : (
        <ul className="max-h-72 space-y-1.5 overflow-y-auto pr-1">
          {items.map((item) => {
            const sev = severityMeta(item.severity);
            const busy = busyId === item.id;
            return (
              <li
                key={item.id}
                className={`rounded-xl border py-2 pl-3 pr-2 transition-all hover:shadow-sm ${ROW_TONE[sev.tone]}`}
              >
                <div className="flex items-center gap-1.5">
                  <span className={`shrink-0 rounded-md border px-1.5 py-0.5 text-[10px] font-semibold ${HEADLINE_TONE[sev.tone]}`}>
                    {sev.label}
                  </span>
                  <span className="shrink-0 rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-600">
                    {kindLabel(item.kind)}
                  </span>
                  <span className="truncate text-[12px] font-bold text-slate-800">
                    {item.stockName || item.symbol}
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-slate-400">{item.symbol}</span>
                  <span className="ml-auto shrink-0 text-[10px] text-slate-400">{alertAgeText(item.createdAt)}</span>
                </div>

                <p className="mt-1 text-[11px] leading-relaxed text-slate-600">{item.content || item.title}</p>

                <div className="mt-1.5 flex items-center gap-2">
                  <span className="shrink-0 font-mono text-[11px] font-semibold text-slate-700">
                    {scoreTransition(item)}
                  </span>
                  <span className="ml-auto flex shrink-0 items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => setSellTarget(item)}
                      className="rounded-lg bg-red-600 px-2 py-1 text-[11px] font-bold text-white hover:bg-red-700"
                    >
                      一键卖出
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => navigate(alertActionTarget(item.actionUrl))}
                      className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
                    >
                      去持仓
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void handleMarkExecuted(item)}
                      className="rounded-lg border border-emerald-200 bg-white px-2 py-1 text-[11px] text-emerald-600 hover:bg-emerald-50 disabled:opacity-50"
                    >
                      已卖出
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void handleDismiss(item)}
                      className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-50 disabled:opacity-50"
                    >
                      忽略
                    </button>
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {config?.enabled === false && (
        <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
          持仓监控总开关已关闭（可在「个人中心 → 持仓监控与提醒」打开）
        </div>
      )}

      <PushConfirmPanel
        open={sellTarget !== null}
        symbols={sellTarget ? [toSuffixCode(sellTarget.symbol)] : []}
        side="sell"
        channels={channels}
        onChannelsChange={setChannels}
        onClose={() => setSellTarget(null)}
        onDone={(data) => { void handleSellDone(data); }}
      />
    </section>
  );
};

export default HoldingAlertPanel;
