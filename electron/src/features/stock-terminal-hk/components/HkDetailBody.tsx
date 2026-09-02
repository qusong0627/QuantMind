/** 港股个股详情 Tabs — CCASS 席位 / 南向持股 / 估值 / 分红 / 财务 / 分析师 / 资讯
 *
 * 数据来自 /research/hk/stock-detail 一次聚合（quanthk 本地各数据集）。
 */

import React, { useEffect, useState } from 'react';
import { Spin, Tag } from 'antd';
import { Building2, Waves, Coins, CalendarClock, BarChart3, Target, Newspaper } from 'lucide-react';
import { SERVICE_ENDPOINTS } from '../../../config/services';

interface HkStockDetail {
  symbol: string;
  name: string;
  ccass: {
    trade_date: string;
    total_pct: number;
    top: Array<{ participant_id: string; participant_name: string; holding_pct: number }>;
  };
  south: {
    trade_date: string;
    holding_pct: number | null;
    holding_quantity: number | null;
    d1: number | null;
    d5: number | null;
    d20: number | null;
    series: Array<{ date: string; pct: number }>;
  };
  valuation: Record<string, number | string | null>;
  dividend: Array<{ ex_date: string; pay_date: string; plan: string; dividend: number | null }>;
  financial: Record<string, number | null>;
  analyst: {
    price_target?: { mean: number | null; high: number | null; low: number | null };
    recommendation?: { period: string; buy: number; hold: number; sell: number; buy_ratio: number | null };
  };
}

function fetchDetail(symbol: string): Promise<HkStockDetail> {
  const token = localStorage.getItem('access_token') || '';
  return fetch(
    `${SERVICE_ENDPOINTS.USER_SERVICE}/research/hk/stock-detail?symbol=${encodeURIComponent(symbol)}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  ).then(async (r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = (await r.json()) as HkStockDetail;
    return j || ({} as HkStockDetail);
  });
}

function fmtYi(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

function fmtCnt(v: number | null | undefined): string {
  if (v === null || v === undefined) return '--';
  return v.toLocaleString('zh-CN');
}

function Row({ label, value, strong = false, unit = '' }: { label: string; value: React.ReactNode; strong?: boolean; unit?: string }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-slate-50 last:border-0">
      <span className="text-[11px] text-slate-400 font-bold">{label}</span>
      <span className={`text-[11px] font-mono ${strong ? 'text-slate-800 font-black' : 'text-slate-600'}`}>
        {value}
        {unit && <span className="text-slate-400 ml-0.5">{unit}</span>}
      </span>
    </div>
  );
}

function PctCell({ v, suffix = '%', invert = false }: { v: number | null; suffix?: string; invert?: boolean }) {
  if (v === null || v === undefined) return <span className="text-slate-300 font-mono">--</span>;
  const up = invert ? v < 0 : v > 0;
  return (
    <span className={`font-mono font-bold ${up ? 'text-rose-600' : 'text-emerald-600'}`}>
      {v > 0 ? '+' : ''}{v.toFixed(2)}{suffix}
    </span>
  );
}

function CcassBlock({ d }: { d: HkStockDetail['ccass'] }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-2">
        <Building2 className="w-3.5 h-3.5 text-indigo-500" />
        <span className="text-[11px] font-black text-slate-700">CCASS 席位（前十占比 {d.total_pct.toFixed(1)}%）</span>
        {d.trade_date && <span className="ml-auto text-[9px] font-mono text-slate-400">{d.trade_date}</span>}
      </div>
      {d.top.length ? d.top.slice(0, 8).map((it, i) => (
        <div key={it.participant_id + i} className="flex items-center gap-2 py-1 border-b border-slate-50 last:border-0">
          <span className="w-4 text-[9px] font-black text-slate-300">{i + 1}</span>
          <span className={`w-11 text-center rounded text-[8px] font-black px-1 py-0.5 ${it.participant_id.startsWith('C0') ? 'bg-indigo-50 text-indigo-600' : 'bg-amber-50 text-amber-600'}`}>
            {it.participant_id.startsWith('C0') ? '托管行' : '券商'}
          </span>
          <span className="flex-1 min-w-0 text-[11px] text-slate-700 truncate">{it.participant_name}</span>
          <span className="text-[11px] font-mono font-black text-slate-800">{it.holding_pct.toFixed(2)}%</span>
        </div>
      )) : <div className="text-[11px] text-slate-400 py-2">暂无 CCASS 披露</div>}
    </div>
  );
}

function SouthBlock({ d }: { d: HkStockDetail['south'] }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-2">
        <Waves className="w-3.5 h-3.5 text-sky-500" />
        <span className="text-[11px] font-black text-slate-700">南向持股（港股通）</span>
        {d.trade_date && <span className="ml-auto text-[9px] font-mono text-slate-400">{d.trade_date}</span>}
      </div>
      <div className="flex items-baseline gap-2 mb-2">
        <span className="text-lg font-black font-mono text-slate-900">{d.holding_pct?.toFixed(2) ?? '--'}%</span>
        <span className="text-[10px] text-slate-400">持股 {fmtCnt(d.holding_quantity)} 股</span>
      </div>
      <div className="grid grid-cols-3 gap-1.5 mb-2">
        {([
          ['1日', d.d1], ['5日', d.d5], ['20日', d.d20],
        ] as const).map(([label, v]) => (
          <div key={label} className="rounded-lg bg-slate-50 border border-slate-100 px-2 py-1">
            <div className="text-[9px] text-slate-400 font-bold">{label}变化</div>
            <PctCell v={v} />
          </div>
        ))}
      </div>
      {d.series.length > 1 && (
        <div className="flex items-end gap-[2px] h-14">
          {d.series.map((p, i) => (
            <div
              key={i}
              title={`${p.date} ${p.pct.toFixed(2)}%`}
              className="flex-1 rounded-t bg-sky-400/70 hover:bg-sky-500"
              style={{ height: `${Math.max(8, (p.pct / (Math.max(...d.series.map((x) => x.pct)) || 1)) * 100)}%` }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ValuationBlock({ d }: { d: HkStockDetail['valuation'] }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1">
        <Coins className="w-3.5 h-3.5 text-purple-500" />
        <span className="text-[11px] font-black text-slate-700">估值 · 股息</span>
        {d.published_at && <span className="ml-auto text-[9px] font-mono text-slate-400">快照 {String(d.published_at)}</span>}
      </div>
      <Row label="市盈率 TTM" value={fmtYi(d.pe_ttm as number)} />
      <Row label="市净率 MRQ" value={fmtYi(d.pb as number)} />
      <Row label="市销率 TTM" value={fmtYi(d.ps_ttm as number)} />
      <Row label="股息率 TTM" value={fmtYi(d.dividend_yield as number)} unit="%" />
      <Row label="总市值" value={fmtYi(d.total_mv_yi as number)} unit="亿港元" strong />
    </div>
  );
}

function DividendBlock({ d, name }: { d: HkStockDetail['dividend']; name: string }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-2">
        <CalendarClock className="w-3.5 h-3.5 text-emerald-500" />
        <span className="text-[11px] font-black text-slate-700">派息历史（近 12 期）</span>
      </div>
      {d.length ? d.map((it, i) => (
        <div key={i} className="flex items-center gap-2 py-1 border-b border-slate-50 last:border-0">
          <span className="w-20 shrink-0 text-[10px] font-mono text-slate-500">{it.ex_date}</span>
          <span className="flex-1 min-w-0 text-[10px] text-slate-600 truncate" title={it.plan}>
            {it.plan || `${name} 派息`}
          </span>
          <span className="text-[11px] font-mono font-black text-slate-800">
            {it.dividend !== null ? `HK$ ${it.dividend}` : '--'}
          </span>
        </div>
      )) : <div className="text-[11px] text-slate-400 py-2">暂无派息记录</div>}
    </div>
  );
}

function FinancialBlock({ d }: { d: HkStockDetail['financial'] }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1">
        <BarChart3 className="w-3.5 h-3.5 text-slate-500" />
        <span className="text-[11px] font-black text-slate-700">财务速览</span>
      </div>
      <Row label="营业总收入" value={fmtYi(d.revenue as number)} unit="港元" strong />
      <Row label="净利润" value={fmtYi(d.net_profit as number)} unit="港元" strong />
      <Row label="销售净利率" value={fmtYi(d.net_margin as number)} unit="%" />
      <Row label="ROE" value={fmtYi(d.roe as number)} unit="%" />
      <Row label="每股股息 TTM" value={d.dps_ttm != null ? d.dps_ttm.toFixed(4) : '--'} unit="港元" />
      <Row label="基本每股收益" value={d.eps != null ? d.eps.toFixed(3) : '--'} unit="港元" />
    </div>
  );
}

function AnalystBlock({ d, close }: { d: HkStockDetail['analyst']; close?: number }) {
  const pt = d.price_target;
  const rec = d.recommendation;
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1">
        <Target className="w-3.5 h-3.5 text-rose-500" />
        <span className="text-[11px] font-black text-slate-700">分析师共识</span>
      </div>
      {pt ? (
        <>
          <Row label="目标价均值" value={fmtYi(pt.mean as number)} unit="港元" strong />
          <Row label="目标价区间" value={`${fmtYi(pt.low as number)} ~ ${fmtYi(pt.high as number)}`} unit="港元" />
          {close ? <Row label="较现价空间" value={(pt.mean != null ? ((pt.mean - close) / close) * 100 : 0).toFixed(1)} unit="%" /> : null}
        </>
      ) : <div className="text-[11px] text-slate-400 py-1">暂无目标价</div>}
      {rec && (
        <div className="mt-2">
          <div className="flex items-center gap-1.5 mb-1.5">
            <span className="text-[10px] text-slate-400 font-bold">评级分布</span>
            {rec.buy_ratio != null && (
              <Tag color={rec.buy_ratio >= 60 ? 'green' : rec.buy_ratio >= 40 ? 'orange' : 'red'} className="m-0 rounded-full text-[9px] font-black">
                买入占比 {rec.buy_ratio}%
              </Tag>
            )}
          </div>
          <div className="h-2 rounded-full bg-slate-100 overflow-hidden flex">
            <div className="bg-rose-400" style={{ width: `${(rec.buy / Math.max(rec.buy + rec.hold + rec.sell, 1)) * 100}%` }} />
            <div className="bg-slate-300" style={{ width: `${(rec.hold / Math.max(rec.buy + rec.hold + rec.sell, 1)) * 100}%` }} />
            <div className="bg-emerald-400" style={{ width: `${(rec.sell / Math.max(rec.buy + rec.hold + rec.sell, 1)) * 100}%` }} />
          </div>
          <div className="flex justify-between mt-1 text-[9px] font-mono text-slate-400">
            <span>买入 {rec.buy}</span><span>持有 {rec.hold}</span><span>卖出 {rec.sell}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function NewsBlock({ symbol, name }: { symbol: string; name: string }) {
  const [items, setItems] = useState<Array<{ id: number; title: string; published_at: string; summary: string }>>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!symbol) return;
    let alive = true;
    setLoading(true);
    const token = localStorage.getItem('access_token') || '';
    fetch(
      `${SERVICE_ENDPOINTS.USER_SERVICE}/news/articles?keyword=${encodeURIComponent(name)}&page_size=15&sort=time_desc`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((j: any) => {
        if (!alive) return;
        const list = (j?.data?.items ?? j?.items ?? []) as Array<any>;
        setItems(list.map((it) => ({ id: it.id, title: it.title, published_at: it.published_at, summary: it.summary || '' })));
      })
      .catch(() => alive && setItems([]))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [symbol, name]);

  return (
    <div>
      <div className="flex items-center gap-1.5 mb-2">
        <Newspaper className="w-3.5 h-3.5 text-slate-500" />
        <span className="text-[11px] font-black text-slate-700">相关资讯（RSS 聚合）</span>
      </div>
      {loading ? (
        <div className="py-6 flex justify-center"><Spin size="small" /></div>
      ) : items.length ? (
        <div className="flex flex-col gap-2">
          {items.map((it) => (
            <a
              key={it.id}
              href={`#/news`}
              className="block rounded-xl border border-slate-100 bg-slate-50/50 px-2.5 py-2 hover:bg-slate-100/70 transition-colors"
            >
              <div className="text-[11px] font-bold text-slate-800 leading-snug line-clamp-2">{it.title}</div>
              <div className="mt-1 text-[9px] font-mono text-slate-400">{String(it.published_at).slice(0, 10)}</div>
            </a>
          ))}
        </div>
      ) : (
        <div className="text-[11px] text-slate-400 py-2">暂无相关资讯</div>
      )}
    </div>
  );
}

/** 港股详情聚合组件：外部传入 symbol + 名称 */
export function HkDetailBody({ symbol, name, close }: { symbol: string; name: string; close?: number }) {
  const [detail, setDetail] = useState<HkStockDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState('ccass');

  useEffect(() => {
    if (!symbol) return;
    let alive = true;
    setLoading(true);
    fetchDetail(symbol)
      .then((d) => alive && setDetail(d))
      .catch(() => alive && setDetail(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [symbol]);

  const TABS = [
    { id: 'ccass', label: 'CCASS' },
    { id: 'south', label: '南向' },
    { id: 'valuation', label: '估值' },
    { id: 'dividend', label: '分红' },
    { id: 'financial', label: '财务' },
    { id: 'analyst', label: '分析师' },
    { id: 'news', label: '资讯' },
  ];

  if (loading && !detail) {
    return <div className="py-8 flex justify-center"><Spin /></div>;
  }
  if (!detail) {
    return <div className="py-6 text-center text-[11px] text-slate-400">详情加载失败（检查 quanthk 数据）</div>;
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-4 gap-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-1.5 py-1 rounded-lg text-[10px] font-bold border ${
              tab === t.id
                ? 'bg-sky-600 text-white border-sky-600'
                : 'bg-slate-50 text-slate-600 border-slate-200 hover:bg-white'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="min-h-[260px]">
        {tab === 'ccass' && <CcassBlock d={detail.ccass} />}
        {tab === 'south' && <SouthBlock d={detail.south} />}
        {tab === 'valuation' && <ValuationBlock d={detail.valuation} />}
        {tab === 'dividend' && <DividendBlock d={detail.dividend} name={name} />}
        {tab === 'financial' && <FinancialBlock d={detail.financial} />}
        {tab === 'analyst' && <AnalystBlock d={detail.analyst} close={close} />}
        {tab === 'news' && <NewsBlock symbol={symbol} name={name} />}
      </div>
    </div>
  );
}