/**
 * 美股个股终端右侧详情体 —— 9 个 Tab。
 *
 * 数据由 `/api/v1/stock-terminal-us/detail` 一次聚合（除资讯单独取）。
 * 口径：估值走 f10 快照；财务只有年报（无季报）；机构持仓是 13F，披露滞后约一季；
 * 内部人来自 SEC Form 4，`Transaction` 列恒空、类型由 Text 前缀解析（后端已处理）。
 */

import { useEffect, useState } from 'react';
import { Spin } from 'antd';
import { stockTerminalService } from '../services/stockTerminalService';
import type { UsStockDetail, UsAnalysts, UsEarnings, UsFinancials, UsHoldings, UsCorporateActions, UsOverview, UsValuation } from '../types';

type DetailTab = 'overview' | 'valuation' | 'financials' | 'analysts' | 'earnings' | 'insiders' | 'holdings' | 'corporate' | 'news';

const TABS: { id: DetailTab; label: string }[] = [
  { id: 'overview', label: '概览' },
  { id: 'valuation', label: '估值' },
  { id: 'financials', label: '财务' },
  { id: 'analysts', label: '分析师' },
  { id: 'earnings', label: '财报' },
  { id: 'insiders', label: '内部人' },
  { id: 'holdings', label: '机构' },
  { id: 'corporate', label: '分红拆股' },
  { id: 'news', label: '资讯' },
];

// ── 数值格式化（全部 null 安全：历史事故是 null.toFixed 导致整页白屏） ──

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return v.toFixed(digits);
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  const abs = Math.abs(v);
  if (abs >= 1e12) return `$${(v / 1e12).toFixed(2)}万亿`;
  if (abs >= 1e8) return `$${(v / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `$${(v / 1e4).toFixed(2)}万`;
  return `$${v.toFixed(0)}`;
}

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`;
}

function fmtInt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return Math.round(v).toLocaleString('zh-CN');
}

/** 涨红跌绿（与 A 股/港股终端一致） */
function toneOf(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'text-slate-400';
  return v >= 0 ? 'text-rose-500' : 'text-emerald-500';
}

function Row({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-slate-100 last:border-0">
      <span className="text-[11px] text-slate-400 shrink-0">{label}</span>
      <span className={`text-[12px] font-mono font-bold ${tone ?? 'text-slate-700'} truncate ml-2`}>{value}</span>
    </div>
  );
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="mb-3 rounded-xl bg-white border border-slate-200/80 p-3">
      <div className="flex items-baseline gap-2 mb-1.5">
        <span className="text-[12px] font-black text-slate-700">{title}</span>
        {hint && <span className="text-[10px] text-slate-400">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

function Empty({ text = '暂无数据' }: { text?: string }) {
  return <div className="py-8 text-center text-[11px] text-slate-400">{text}</div>;
}

// ── 各面板 ──

function OverviewPanel({ d }: { d: UsStockDetail }) {
  const o: UsOverview = d.overview;
  const fromHigh = o.week52_high && o.close ? ((o.close / o.week52_high - 1) * 100) : null;
  return (
    <>
      <Section title="公司">
        <Row label="中文名" value={o.cn_name ?? '--'} />
        <Row label="英文名" value={o.en_name ?? '--'} />
        <Row label="GICS 板块" value={o.sector ?? '--'} />
        <Row label="细分行业" value={o.industry ?? '--'} />
      </Section>
      <Section title="行情" hint={o.trade_date ?? ''}>
        <Row label="最新收盘" value={fmtNum(o.close, 3)} />
        <Row label="涨跌幅" value={fmtPct(o.pct_change)} tone={toneOf(o.pct_change)} />
        <Row label="总市值" value={o.cap_display ?? fmtUsd(o.market_cap)} />
        <Row label="52 周高 / 低" value={`${fmtNum(o.week52_high)} / ${fmtNum(o.week52_low)}`} />
        <Row label="距 52 周高点" value={fmtPct(fromHigh)} tone={toneOf(fromHigh)} />
        <Row label="平均成交量" value={fmtInt(o.avg_volume)} />
      </Section>
      <div className="text-[10px] text-slate-400 leading-relaxed px-1">
        标的池为标普 500 + 纳指补充（约 500 只），非全市场；日线为 yfinance 原始未复权价。
      </div>
    </>
  );
}

function ValuationPanel({ d }: { d: UsStockDetail }) {
  const v: UsValuation = d.valuation;
  const hasAny = v.pe_ratio != null || v.pb_ratio != null || v.market_cap != null;
  if (!hasAny) return <Empty text="该标的无估值快照（f10）" />;
  return (
    <Section title="估值快照" hint={`${v.source ?? 'f10'} · ${v.asof ?? ''}`}>
      <Row label="市盈率 PE" value={fmtNum(v.pe_ratio)} />
      <Row label="市净率 PB" value={fmtNum(v.pb_ratio)} />
      <Row label="股息率" value={v.dividend_yield == null ? '--' : `${fmtNum(v.dividend_yield)}%`} />
      <Row label="总市值" value={fmtUsd(v.market_cap)} />
      <Row label="52 周高" value={fmtNum(v.week52_high)} />
      <Row label="52 周低" value={fmtNum(v.week52_low)} />
      <div className="text-[10px] text-slate-400 mt-2 leading-relaxed">
        快照口径：仅含 Yahoo 可得的 PE/PB/股息率与 52 周区间，库里没有 PS、EV、Forward PE。
      </div>
    </Section>
  );
}

function FinancialsPanel({ d }: { d: UsStockDetail }) {
  const f: UsFinancials = d.financials;
  const [group, setGroup] = useState<'income' | 'balance' | 'cashflow'>('income');
  const rows = f[group] ?? [];
  if (!f.periods?.length || !rows.length) return <Empty text="无财务数据（仅年报，部分标的缺）" />;
  const groups: { id: typeof group; label: string }[] = [
    { id: 'income', label: '利润表' },
    { id: 'balance', label: '资产负债表' },
    { id: 'cashflow', label: '现金流量表' },
  ];
  return (
    <div className="rounded-xl bg-white border border-slate-200/80 p-3">
      <div className="flex items-center gap-1 mb-2">
        {groups.map((g) => (
          <button
            key={g.id}
            onClick={() => setGroup(g.id)}
            className={`px-2 py-1 rounded-full text-[10px] font-bold border transition-colors ${group === g.id ? 'bg-blue-600 text-white border-blue-600' : 'bg-slate-50 text-slate-600 border-slate-200'}`}
          >
            {g.label}
          </button>
        ))}
        <span className="ml-auto text-[10px] text-slate-400">仅年报 · 单位美元</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[10px]">
          <thead>
            <tr className="text-slate-400">
              <th className="text-left font-normal py-1 sticky left-0 bg-white">科目</th>
              {f.periods.map((p) => (
                <th key={p} className="text-right font-normal py-1 whitespace-nowrap pl-2">{String(p).slice(0, 10)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-slate-100">
                <td className="py-1 text-slate-600 sticky left-0 bg-white pr-2">{r.label}</td>
                {r.values.map((v, i) => (
                  <td key={i} className="py-1 text-right font-mono text-slate-700 whitespace-nowrap pl-2">
                    {v == null ? '--' : fmtUsd(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const ACTION_TONE: Record<string, string> = {
  up: 'bg-rose-50 text-rose-600 border-rose-200',
  down: 'bg-emerald-50 text-emerald-600 border-emerald-200',
  init: 'bg-blue-50 text-blue-600 border-blue-200',
  reiterated: 'bg-slate-50 text-slate-500 border-slate-200',
  other: 'bg-slate-50 text-slate-500 border-slate-200',
};
const ACTION_LABEL: Record<string, string> = { up: '上调', down: '下调', init: '首次', reiterated: '重申', other: '其他' };

function AnalystsPanel({ d }: { d: UsStockDetail }) {
  const a: UsAnalysts = d.analysts;
  const t = a.target;
  const upside = t?.mean != null && d.overview.close ? ((t.mean / d.overview.close - 1) * 100) : null;
  const latest = a.ratings?.[0];
  const total = latest
    ? (latest.strongBuy ?? 0) + (latest.buy ?? 0) + (latest.hold ?? 0) + (latest.sell ?? 0) + (latest.strongSell ?? 0)
    : 0;
  return (
    <>
      {t && (t.mean != null || t.high != null) ? (
        <Section title="目标价" hint={upside != null ? `隐含空间 ${fmtPct(upside)}` : ''}>
          <Row label="现价" value={fmtNum(t.current)} />
          <Row label="均值目标" value={fmtNum(t.mean)} tone={toneOf(upside)} />
          <Row label="中位数" value={fmtNum(t.median)} />
          <Row label="最高 / 最低" value={`${fmtNum(t.high)} / ${fmtNum(t.low)}`} />
        </Section>
      ) : null}
      {latest && total > 0 ? (
        <Section title="评级分布" hint={latest.period === '0m' ? '当前' : latest.period}>
          {[
            { k: '强烈买入', v: latest.strongBuy, cls: 'bg-rose-600' },
            { k: '买入', v: latest.buy, cls: 'bg-rose-400' },
            { k: '持有', v: latest.hold, cls: 'bg-slate-400' },
            { k: '卖出', v: latest.sell, cls: 'bg-emerald-400' },
            { k: '强烈卖出', v: latest.strongSell, cls: 'bg-emerald-600' },
          ].map((r) => (
            <div key={r.k} className="flex items-center gap-2 py-0.5">
              <span className="text-[10px] text-slate-500 w-14 shrink-0">{r.k}</span>
              <div className="flex-1 h-1.5 rounded-full bg-slate-100 overflow-hidden">
                <div className={`h-full ${r.cls}`} style={{ width: `${total ? ((r.v ?? 0) / total) * 100 : 0}%` }} />
              </div>
              <span className="text-[10px] font-mono text-slate-600 w-6 text-right">{r.v ?? 0}</span>
            </div>
          ))}
        </Section>
      ) : null}
      <Section title="评级变动" hint="近 30 条">
        {a.upgrades?.length ? (
          <div className="space-y-1">
            {a.upgrades.map((u, i) => (
              <div key={`${u.date}-${u.firm}-${i}`} className="flex items-center gap-2 text-[10px]">
                <span className="font-mono text-slate-400 w-[68px] shrink-0">{u.date}</span>
                <span className={`shrink-0 px-1 py-0.5 rounded border text-[9px] font-bold ${ACTION_TONE[u.action] ?? ACTION_TONE.other}`}>
                  {ACTION_LABEL[u.action] ?? u.action}
                </span>
                <span className="flex-1 min-w-0 truncate text-slate-600" title={`${u.from_grade ?? '--'} → ${u.to_grade ?? '--'} @ ${u.firm ?? ''}`}>
                  {u.firm ?? '--'} · {u.from_grade ?? '--'} → <span className="font-bold">{u.to_grade ?? '--'}</span>
                </span>
                <span className="shrink-0 font-mono text-slate-500">
                  {u.current_target != null ? fmtNum(u.current_target, 0) : ''}
                  {u.current_target != null && u.prior_target != null && u.current_target > u.prior_target ? ' ↑' : ''}
                </span>
              </div>
            ))}
          </div>
        ) : <Empty text="无评级变动记录" />}
      </Section>
    </>
  );
}

function EarningsPanel({ d }: { d: UsStockDetail }) {
  const e: UsEarnings = d.earnings;
  return (
    <>
      <Section title="未来财报日" hint="预测口径，会调整">
        {e.upcoming?.length ? (
          <div className="space-y-1">
            {e.upcoming.map((u, i) => (
              <div key={`${u.date}-${i}`} className="flex items-center gap-2 text-[11px]">
                <span className="font-mono text-slate-600 w-[76px] shrink-0">{u.date}</span>
                <span className="text-slate-400">EPS 预期</span>
                <span className="font-mono text-slate-700">{fmtNum(u.eps_estimate)}</span>
                <span className="ml-auto text-slate-400">营收预期</span>
                <span className="font-mono text-slate-700">{fmtUsd(u.revenue_estimate)}</span>
              </div>
            ))}
          </div>
        ) : <Empty text="无未来财报日" />}
      </Section>
      <Section title="历史超预期" hint="近 8 期">
        {e.history?.length ? (
          <div className="space-y-1">
            <div className="flex items-center gap-2 text-[9px] text-slate-400 pb-1 border-b border-slate-100">
              <span className="w-[76px] shrink-0">季度</span>
              <span className="w-12 text-right">实际</span>
              <span className="w-12 text-right">预期</span>
              <span className="flex-1 text-right">超预期</span>
            </div>
            {e.history.map((h, i) => (
              <div key={`${h.quarter}-${i}`} className="flex items-center gap-2 text-[10px]">
                <span className="font-mono text-slate-500 w-[76px] shrink-0">{String(h.quarter).slice(0, 10)}</span>
                <span className="w-12 text-right font-mono text-slate-700">{fmtNum(h.actual)}</span>
                <span className="w-12 text-right font-mono text-slate-500">{fmtNum(h.estimate)}</span>
                <span className={`flex-1 text-right font-mono font-bold ${toneOf(h.surprise_pct)}`}>{fmtPct(h.surprise_pct)}</span>
              </div>
            ))}
            <div className="text-[10px] text-slate-400 pt-1">
              注：本表会出现 200%+ 的极端值，经两张独立来源交叉验证为源数据预估偏低，不是计算口径问题。
            </div>
          </div>
        ) : <Empty text="无历史财报记录" />}
      </Section>
    </>
  );
}

function InsidersPanel({ d }: { d: UsStockDetail }) {
  const ins = d.insiders;
  const net = ins.net;
  const items = ins.items ?? [];
  return (
    <>
      <Section title="内部人净买卖" hint="SEC Form 4">
        <Row label="买入金额" value={fmtUsd(net?.buy_value)} tone="text-rose-500" />
        <Row label="卖出金额" value={fmtUsd(net?.sell_value)} tone="text-emerald-500" />
        <Row label="净额" value={fmtUsd(net?.net_value)} tone={toneOf(net?.net_value)} />
        <Row label="买入 / 卖出笔数" value={`${fmtInt(net?.buy_count)} / ${fmtInt(net?.sell_count)}`} />
      </Section>
      <Section title="交易流水" hint="近 30 条">
        {items.length ? (
          <div className="space-y-1">
            {items.map((it, i) => (
              <div key={`${it.date}-${it.insider}-${i}`} className="flex items-center gap-2 text-[10px]">
                <span className="font-mono text-slate-400 w-[68px] shrink-0">{it.date}</span>
                <span className={`shrink-0 px-1 py-0.5 rounded text-[9px] font-bold ${it.type === 'buy' ? 'bg-rose-50 text-rose-600' : it.type === 'sell' ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-50 text-slate-500'}`}>
                  {it.type === 'buy' ? '买入' : it.type === 'sell' ? '卖出' : '其他'}
                </span>
                <span className="flex-1 min-w-0 truncate text-slate-600" title={`${it.insider ?? ''} ${it.position ?? ''}`}>{it.insider ?? '--'}</span>
                <span className="shrink-0 font-mono text-slate-500">{fmtInt(it.shares)}股</span>
                <span className="shrink-0 font-mono text-slate-700 w-16 text-right">{fmtUsd(it.value)}</span>
              </div>
            ))}
          </div>
        ) : <Empty text="无内部人交易记录" />}
      </Section>
    </>
  );
}

function HoldingsPanel({ d }: { d: UsStockDetail }) {
  const h: UsHoldings = d.holdings;
  const funds = h.funds ?? [];
  return (
    <>
      <Section title="持股结构" hint={`13F 口径 · 披露日 ${h.reported_date ?? '--'}`}>
        <Row label="内部人持股" value={h.insiders_pct == null ? '--' : `${fmtNum(h.insiders_pct)}%`} />
        <Row label="机构持股" value={h.institutions_pct == null ? '--' : `${fmtNum(h.institutions_pct)}%`} />
        <Row label="机构持流通股" value={h.institutions_float_pct == null ? '--' : `${fmtNum(h.institutions_float_pct)}%`} />
        <Row label="机构家数" value={fmtInt(h.institutions_count)} />
      </Section>
      <Section title="主要机构" hint="按持有比例">
        {funds.length ? (
          <div className="space-y-1">
            {funds.map((f, i) => (
              <div key={`${f.holder}-${i}`} className="flex items-center gap-2 text-[10px]">
                <span className="flex-1 min-w-0 truncate text-slate-600">{f.holder ?? '--'}</span>
                <span className="shrink-0 font-mono text-slate-700 w-12 text-right">{f.pct_held == null ? '--' : `${fmtNum(f.pct_held)}%`}</span>
                <span className={`shrink-0 font-mono w-12 text-right ${toneOf(f.pct_change)}`}>{fmtPct(f.pct_change)}</span>
              </div>
            ))}
          </div>
        ) : <Empty text="无机构持仓记录" />}
      </Section>
      <div className="text-[10px] text-slate-400 leading-relaxed px-1">
        13F 为季度披露，数据滞后约一个季度，不代表当前持仓。
      </div>
    </>
  );
}

function CorporatePanel({ d }: { d: UsStockDetail }) {
  const c: UsCorporateActions = d.corporate_actions;
  return (
    <>
      <Section title="分红记录" hint="近 20 次 · 单位美元">
        {c.dividends?.length ? (
          <div className="space-y-1">
            {c.dividends.map((x, i) => (
              <div key={`${x.date}-${i}`} className="flex items-center gap-2 text-[10px]">
                <span className="font-mono text-slate-400 w-[76px] shrink-0">{x.date}</span>
                <span className="font-mono text-slate-700">{x.amount == null ? '--' : `$${fmtNum(x.amount, 4)}`}</span>
              </div>
            ))}
          </div>
        ) : <Empty text="无分红记录" />}
      </Section>
      <Section title="拆股记录" hint="全部历史">
        {c.splits?.length ? (
          <div className="space-y-1">
            {c.splits.map((x, i) => (
              <div key={`${x.date}-${i}`} className="flex items-center gap-2 text-[10px]">
                <span className="font-mono text-slate-400 w-[76px] shrink-0">{x.date}</span>
                <span className="font-mono text-amber-600">{x.ratio == null ? '--' : `${fmtNum(x.ratio)}:1`}</span>
              </div>
            ))}
          </div>
        ) : <Empty text="无拆股记录" />}
      </Section>
      <div className="text-[10px] text-slate-400 leading-relaxed px-1">
        K 线上的橙色竖线即拆股日 —— 未复权价在拆股日会出现跳变，属正常现象。
      </div>
    </>
  );
}

function NewsPanel({ symbol }: { symbol: string }) {
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    stockTerminalService
      .getNews(symbol)
      .then((r) => {
        if (!cancelled) setItems(r.items ?? []);
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  if (loading) return <div className="py-8 text-center"><Spin size="small" /></div>;
  if (!items.length) return <Empty text="暂无相关资讯" />;
  return (
    <div className="space-y-2">
      {items.map((n, i) => (
        <a
          key={n.id ?? i}
          href={n.link ?? n.url ?? '#'}
          target="_blank"
          rel="noreferrer"
          className="block rounded-xl bg-white border border-slate-200/80 p-2.5 hover:border-blue-300 transition-colors"
        >
          <div className="text-[11px] font-bold text-slate-700 leading-snug line-clamp-2">{n.title ?? '--'}</div>
          <div className="mt-1 flex items-center gap-2 text-[10px] text-slate-400">
            <span>{String(n.published_at ?? '').slice(0, 16)}</span>
            {n.sentiment_label && (
              <span className={`px-1 rounded ${n.sentiment_label === 'positive' ? 'bg-rose-50 text-rose-500' : n.sentiment_label === 'negative' ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-50 text-slate-500'}`}>
                {n.sentiment_label}
              </span>
            )}
            {n.source && <span className="truncate">{n.source}</span>}
          </div>
        </a>
      ))}
    </div>
  );
}

// ── 主体 ──

export function UsDetailBody({ symbol }: { symbol: string }) {
  const [tab, setTab] = useState<DetailTab>('overview');
  const [detail, setDetail] = useState<UsStockDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    stockTerminalService
      .getDetail(symbol)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        setFailed(!d);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-2 border-b border-slate-100 bg-white shrink-0">
        <div className="grid grid-cols-5 gap-1.5">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-2 py-1.5 rounded-full text-[11px] font-bold border transition-colors ${tab === t.id ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-slate-50 text-slate-600 border-slate-200 hover:bg-white hover:border-slate-300'}`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-3 pb-16 bg-gray-50/30 custom-scrollbar">
        {tab === 'news' ? (
          <NewsPanel symbol={symbol} />
        ) : loading && !detail ? (
          <div className="py-10 text-center"><Spin size="small" /></div>
        ) : !detail ? (
          <Empty text={failed ? '详情加载失败' : '暂无详情数据'} />
        ) : (
          <>
            {tab === 'overview' && <OverviewPanel d={detail} />}
            {tab === 'valuation' && <ValuationPanel d={detail} />}
            {tab === 'financials' && <FinancialsPanel d={detail} />}
            {tab === 'analysts' && <AnalystsPanel d={detail} />}
            {tab === 'earnings' && <EarningsPanel d={detail} />}
            {tab === 'insiders' && <InsidersPanel d={detail} />}
            {tab === 'holdings' && <HoldingsPanel d={detail} />}
            {tab === 'corporate' && <CorporatePanel d={detail} />}
          </>
        )}
      </div>
    </div>
  );
}
