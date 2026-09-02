/** 概况 Tab：公司信息 + 行情规模 + 估值指标 */

import { Building2, Coins, Users } from 'lucide-react';
import { StockProfile } from '../types';

interface Props {
  profile: StockProfile | null;
}

function Field({ label, value, accent }: { label: string; value: string; accent?: string }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-slate-50">
      <span className="text-[11px] font-bold text-slate-400">{label}</span>
      <span className={`text-xs font-bold ${accent ?? 'text-slate-700'}`}>{value}</span>
    </div>
  );
}

function fmt(v: number | null | undefined, suffix = '', digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return `${v.toFixed(digits)}${suffix}`;
}

export function OverviewTab({ profile }: Props) {
  if (!profile) return <div className="py-10 text-center text-[11px] text-slate-400">选择股票后展示概况</div>;

  const up = (profile.pct_change ?? 0) >= 0;
  const v = profile.valuation ?? {};

  return (
    <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
      {/* 基本信息 */}
      <div className="bg-white/70 rounded-2xl border border-slate-100 p-3">
        <div className="flex items-center gap-1.5 pb-2 mb-1 border-b border-slate-100">
          <Building2 className="w-3 h-3 text-blue-500" />
          <span className="text-[11px] font-bold text-slate-600">公司概况</span>
        </div>
        <Field label="股票代码" value={profile.symbol} accent="text-blue-600" />
        <Field label="市场板块" value={profile.board} />
        <Field label="所属行业" value={profile.industry ?? '--'} />
        <Field label="员工人数" value={profile.staff_num ? `${Math.round(profile.staff_num).toLocaleString()}人` : '--'} />
        <Field label="IPO 发行价" value={fmt(profile.ipo_price, '元')} />
        {profile.main_business && (
          <p className="text-[10px] text-slate-500 leading-relaxed pt-2 line-clamp-4" title={profile.main_business}>
            {profile.main_business}
          </p>
        )}
      </div>

      {/* 行情与规模 */}
      <div className="bg-white/70 rounded-2xl border border-slate-100 p-3">
        <div className="flex items-center gap-1.5 pb-2 mb-1 border-b border-slate-100">
          <Coins className="w-3 h-3 text-amber-500" />
          <span className="text-[11px] font-bold text-slate-600">行情与规模</span>
        </div>
        <Field label="最新收盘" value={fmt(profile.close, '元')} accent={up ? 'text-rose-500' : 'text-emerald-500'} />
        <Field
          label={`涨跌幅 (${profile.trade_date || '--'})`}
          value={`${up ? '+' : ''}${fmt(profile.pct_change, '%')}`}
          accent={up ? 'text-rose-500' : 'text-emerald-500'}
        />
        <Field label="总市值" value={fmt(profile.total_mv, '亿')} />
        <Field label="流通市值" value={fmt(profile.float_mv, '亿')} />
        <Field label="总股本" value={profile.total_share ? `${(profile.total_share / 10000).toFixed(1)}亿股` : '--'} />
        <Field label="涨跌停价" value={profile.limit_up_price ? `${fmt(profile.limit_up_price)} / ${fmt(profile.limit_down_price)}` : '--'} />
      </div>

      {/* 估值 */}
      <div className="bg-white/70 rounded-2xl border border-slate-100 p-3">
        <div className="flex items-center gap-1.5 pb-2 mb-1 border-b border-slate-100">
          <Users className="w-3 h-3 text-indigo-500" />
          <span className="text-[11px] font-bold text-slate-600">估值指标</span>
        </div>
        <Field label="PE (动)" value={fmt(profile.pe_dynamic)} />
        <Field label="PE (TTM)" value={fmt(v.pe_ttm)} />
        <Field label="PB (MRQ)" value={fmt(profile.pb)} />
        <Field label="PS (TTM)" value={fmt(v.ps_ttm)} />
        <Field label="股息率" value={fmt(profile.dividend_yield, '%')} />
        <Field label="净利润 TTM" value={v.net_profit_ttm != null ? `${(v.net_profit_ttm / 1e8).toFixed(1)}亿` : '--'} />
        <Field label="营收 TTM" value={v.revenue_ttm != null ? `${(v.revenue_ttm / 1e8).toFixed(1)}亿` : '--'} />
      </div>
    </div>
  );
}