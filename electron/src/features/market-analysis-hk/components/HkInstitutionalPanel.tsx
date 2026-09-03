/** 机构持仓分析面板 —— 市场机构持仓结构 / 个股机构查询 / 机构增减持榜
 *
 * 数据口径：CCASS 前 50 大披露席位 × 资金属性分类（内资/港资/外资·欧美/外资·亚太）
 */

import React, { useEffect, useState } from 'react';
import { Briefcase, ChevronDown, ChevronRight, Search, TrendingDown, TrendingUp } from 'lucide-react';
import { Input, message } from 'antd';
import type {
  InstitutionalMovers,
  InstitutionalOverview,
  InstitutionalParticipants,
  InstitutionalSuggestItem,
} from '../types';
import {
  getInstitutionalMovers,
  getInstitutionalOverview,
  getInstitutionalParticipants,
  getInstitutionalSuggest,
} from '../services/api';
import { EmptyHint, PctText, PeriodChips, RankRow, SectionCard, fmtInt } from './shared/ui';
import { InstitutionalStockDetail } from './InstitutionalStockDetail';
import { INST_CATEGORY_COLORS } from './InstitutionalTrendChart';

const CATEGORY_CHIPS: Array<{ id: string; label: string }> = [
  { id: 'all', label: '全部' },
  { id: 'cn_broker', label: '内资·券商' },
  { id: 'southbound', label: '内资·港股通' },
  { id: 'hk', label: '港资' },
  { id: 'us_eu', label: '外资·欧美' },
  { id: 'apac', label: '外资·亚太' },
];

const AUDIT_META: Record<string, string> = {
  all: '全部',
  cn_broker: '内资·中資券商',
  southbound: '内资·港股通',
  hk: '港资',
  us_eu: '外资·欧美',
  apac: '外资·亚太',
  other: '其他',
};

function CategoryChips({
  value,
  onChange,
}: {
  value: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {CATEGORY_CHIPS.map((c) => (
        <button
          key={c.id}
          onClick={() => onChange(c.id)}
          className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold transition-all ${
            value === c.id
              ? 'bg-purple-600 text-white shadow-sm'
              : 'bg-slate-100 text-slate-500 hover:text-slate-800 hover:bg-slate-200'
          }`}
        >
          {c.label}
        </button>
      ))}
    </div>
  );
}

export const HkInstitutionalPanel: React.FC = () => {
  // 市场结构
  const [overview, setOverview] = useState<InstitutionalOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  // 增减持榜
  const [movers, setMovers] = useState<InstitutionalMovers | null>(null);
  const [cat, setCat] = useState('all');
  const [dir, setDir] = useState<'increase' | 'decrease'>('increase');
  const [period, setPeriod] = useState('5');
  // 个股查询
  const [query, setQuery] = useState('');
  const [suggestions, setSuggestions] = useState<InstitutionalSuggestItem[]>([]);
  const [selected, setSelected] = useState<InstitutionalSuggestItem | null>(null);
  // 审计
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditCat, setAuditCat] = useState('all');
  const [audit, setAudit] = useState<InstitutionalParticipants | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    getInstitutionalOverview()
      .then((d) => alive && setOverview(d))
      .catch(() => alive && setOverview(null))
      .finally(() => alive && setOverviewLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setMovers(null);
    getInstitutionalMovers(cat, Number(period), dir, 20)
      .then((d) => alive && setMovers(d))
      .catch(() => alive && setMovers(null));
    return () => {
      alive = false;
    };
  }, [cat, period, dir]);

  useEffect(() => {
    let alive = true;
    const kw = query.trim();
    if (!kw) {
      setSuggestions([]);
      return;
    }
    getInstitutionalSuggest(kw, 8)
      .then((d) => alive && setSuggestions(d))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [query]);

  useEffect(() => {
    if (!auditOpen) return;
    let alive = true;
    setAuditLoading(true);
    getInstitutionalParticipants(auditCat, '', auditCat === 'all' ? 50 : 200)
      .then((d) => alive && setAudit(d))
      .catch(() => alive && setAudit(null))
      .finally(() => alive && setAuditLoading(false));
    return () => {
      alive = false;
    };
  }, [auditOpen, auditCat]);

  const selectStock = (sym: string, name?: string) => {
    setQuery(name || sym);
    setSuggestions([]);
    setSelected({ symbol: sym, name: name || sym });
  };

  const openMover = (sym: string, name: string) => {
    selectStock(sym, name);
    message.success(`已载入 ${name} 机构持仓`);
  };

  const totalValue = overview?.disclosed_value_yi || 0;

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-2.5 items-start">
      {/* 左：市场机构持仓结构 */}
      <div className="xl:col-span-1 flex flex-col gap-2.5">
        <SectionCard
          title={
            <span className="flex items-center gap-1.5">
              <Briefcase className="w-3.5 h-3.5 text-purple-600" />
              市场机构持仓结构
            </span>
          }
          extra={
            overview && (
              <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
                {overview.trade_date}
              </span>
            )
          }
        >
          {overviewLoading && !overview ? (
            <EmptyHint loading />
          ) : overview?.categories.length ? (
            <div className="flex flex-col gap-2">
              {overview.categories.map((c, i) => (
                <div key={c.category} className="flex flex-col gap-0.5">
                  <div className="flex items-center gap-1.5">
                    <span
                      className="w-2 h-2 rounded-full flex-shrink-0"
                      style={{ background: INST_CATEGORY_COLORS[c.category] || '#94a3b8' }}
                    />
                    <span className="text-[11px] font-extrabold text-slate-700 flex-1 truncate">
                      {c.label}
                    </span>
                    <span className="text-[11px] font-mono font-extrabold text-slate-800">
                      {c.value_yi.toFixed(1)}亿
                    </span>
                    <span className="text-[10px] font-mono text-slate-400 w-12 text-right">
                      {c.pct_of_disclosed.toFixed(1)}%
                    </span>
                    <span className="w-16 text-right">
                      {i !== 0 && c.d1_yi !== 0 ? (
                        <span
                          className={`text-[10px] font-mono font-extrabold ${c.d1_yi > 0 ? 'text-red-600' : 'text-green-600'}`}
                        >
                          {c.d1_yi > 0 ? '+' : ''}
                          {c.d1_yi.toFixed(1)}亿
                        </span>
                      ) : (
                        <span className="text-[10px] font-mono text-slate-300">--</span>
                      )}
                    </span>
                  </div>
                  <div className="h-1 rounded-full bg-slate-100 overflow-hidden ml-3.5">
                    <div
                      className="h-full rounded-full transition-all"
                      style={{
                        width: `${Math.min(100, (c.value_yi / totalValue) * 100)}%`,
                        background: INST_CATEGORY_COLORS[c.category] || '#94a3b8',
                      }}
                    />
                  </div>
                </div>
              ))}
              <div className="grid grid-cols-3 gap-1.5 pt-1 border-t border-slate-100 mt-1">
                <div className="text-center">
                  <div className="text-[12px] font-extrabold font-mono text-slate-800">
                    {fmtInt(overview.stock_count)}
                  </div>
                  <div className="text-[9px] text-slate-400">覆盖股票</div>
                </div>
                <div className="text-center">
                  <div className="text-[12px] font-extrabold font-mono text-slate-800">
                    {overview.disclosed_value_yi.toFixed(0)}亿
                  </div>
                  <div className="text-[9px] text-slate-400">披露席位市值</div>
                </div>
                <div className="text-center">
                  <div className="text-[12px] font-extrabold font-mono">
                    <span className="text-red-600">{overview.change_stats.increased}</span>
                    <span className="text-slate-300 mx-0.5">/</span>
                    <span className="text-green-600">{overview.change_stats.decreased}</span>
                  </div>
                  <div className="text-[9px] text-slate-400">5日增持/减持家数</div>
                </div>
              </div>
              {overview.south_date && overview.south_date !== overview.trade_date && (
                <div className="text-[9px] font-mono text-slate-400">
                  南向披露日 {overview.south_date}
                </div>
              )}
              {overview.hkscc_nominees.noted && (
                <div className="text-[9px] font-mono text-amber-500">
                  HKSCC 托管池 {overview.hkscc_nominees.value_yi}亿（独立标注，不计入分类）
                </div>
              )}
            </div>
          ) : (
            <EmptyHint text="机构持仓数据不可用" />
          )}
        </SectionCard>

        {/* 参与者分类审计（可折叠） */}
        <SectionCard
          title={
            <button
              onClick={() => setAuditOpen(!auditOpen)}
              className="flex items-center gap-1.5 text-xs font-extrabold text-slate-800 hover:text-purple-700"
            >
              {auditOpen ? (
                <ChevronDown className="w-3.5 h-3.5 text-purple-600" />
              ) : (
                <ChevronRight className="w-3.5 h-3.5 text-purple-600" />
              )}
              参与者分类审计
            </button>
          }
          extra={
            audit && (
              <span className="text-[9px] font-mono text-slate-400">
                {audit.total} 家 · {audit.trade_date}
              </span>
            )
          }
        >
          {!auditOpen ? (
            <div className="text-[10px] text-slate-400">
              查看全部席位/银行的分类归属，发现误分类可反馈调整规则
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-1 flex-wrap">
                {Object.entries(AUDIT_META).map(([id, label]) => (
                  <button
                    key={id}
                    onClick={() => setAuditCat(id)}
                    className={`px-2 py-0.5 rounded-full text-[9px] font-extrabold transition-all ${
                      auditCat === id
                        ? 'bg-slate-700 text-white'
                        : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {auditLoading ? (
                <EmptyHint loading />
              ) : audit?.items.length ? (
                <div className="flex flex-col max-h-72 overflow-y-auto">
                  {audit.items.map((p, i) => (
                    <div
                      key={p.participant_id + i}
                      className="grid grid-cols-[1fr_56px_48px] gap-1 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center"
                    >
                      <span className="min-w-0">
                        <span className="text-[10px] font-bold text-slate-700 block truncate">
                          {p.participant_name || '(未具名)'}
                        </span>
                        <span className="text-[8px] font-mono text-slate-400 block">
                          {p.participant_id || '--'}
                        </span>
                      </span>
                      <span className="text-right text-[10px] font-mono text-slate-500">
                        {p.hold_yi.toFixed(1)}亿
                      </span>
                      <span className="text-right">
                        <span
                          className="px-1 py-0.5 rounded text-[8px] font-extrabold"
                          style={{
                            background: `${INST_CATEGORY_COLORS[p.category] || '#94a3b8'}1a`,
                            color: INST_CATEGORY_COLORS[p.category] || '#64748b',
                          }}
                        >
                          {AUDIT_META[p.category] || p.category}
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <EmptyHint text="该分类无参与者" />
              )}
            </div>
          )}
        </SectionCard>
      </div>

      {/* 中：个股机构查询 */}
      <SectionCard
        className="xl:col-span-1"
        title={
          <span className="flex items-center gap-1.5">
            <Search className="w-3.5 h-3.5 text-purple-600" />
            个股机构查询
          </span>
        }
        extra={
          <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
            代码 / 简繁中文 / 英文
          </span>
        }
      >
        <div className="flex flex-col gap-2">
          <Input
            placeholder="搜 0700 / 腾讯 / 騰訊 / TENCENT"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="!rounded-xl !text-[11px]"
            size="small"
            allowClear
            prefix={<Search className="w-3 h-3 text-slate-400" />}
          />
          {suggestions.length > 0 && !selected && (
            <div className="flex flex-col border border-slate-100 rounded-xl overflow-hidden">
              {suggestions.slice(0, 6).map((s) => (
                <button
                  key={s.symbol}
                  onClick={() => selectStock(s.symbol, s.name)}
                  className="flex items-center gap-2 px-2 py-1.5 hover:bg-purple-50/70 transition-colors text-left border-b border-slate-50 last:border-0"
                >
                  <span className="text-[11px] font-bold text-slate-800 truncate">{s.name}</span>
                  <span className="text-[9px] font-mono text-slate-400 flex-shrink-0">
                    {s.symbol}
                  </span>
                </button>
              ))}
            </div>
          )}
          {selected ? (
            <InstitutionalStockDetail symbol={selected.symbol} />
          ) : (
            <EmptyHint text="输入代码或名称查询个股的机构持仓结构、趋势与增减持" />
          )}
        </div>
      </SectionCard>

      {/* 右：机构增减持榜 */}
      <SectionCard
        className="xl:col-span-1"
        title={
          <span className="flex items-center gap-1.5">
            {dir === 'increase' ? (
              <TrendingUp className="w-3.5 h-3.5 text-red-500" />
            ) : (
              <TrendingDown className="w-3.5 h-3.5 text-green-500" />
            )}
            机构{dir === 'increase' ? '增持' : '减持'}榜
          </span>
        }
        extra={
          movers && (
            <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
              {movers.base_date} → {movers.trade_date}
            </span>
          )
        }
      >
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-1.5 flex-wrap">
            <PeriodChips
              options={[
                { id: '5', label: '5日' },
                { id: '20', label: '20日' },
                { id: '60', label: '60日' },
              ]}
              value={period}
              onChange={setPeriod}
            />
            <div className="flex items-center gap-1 rounded-full bg-slate-100 p-0.5 ml-auto">
              <button
                onClick={() => setDir('increase')}
                className={`px-2.5 py-1 rounded-full text-[10px] font-extrabold transition-all ${
                  dir === 'increase'
                    ? 'bg-red-600 text-white shadow-sm'
                    : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                增持
              </button>
              <button
                onClick={() => setDir('decrease')}
                className={`px-2.5 py-1 rounded-full text-[10px] font-extrabold transition-all ${
                  dir === 'decrease'
                    ? 'bg-green-600 text-white shadow-sm'
                    : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                减持
              </button>
            </div>
          </div>
          <CategoryChips value={cat} onChange={setCat} />
          {movers?.items.length ? (
            <div className="flex flex-col max-h-[520px] overflow-y-auto">
              {movers.items.map((it, i) => (
                <button
                  key={it.symbol}
                  onClick={() => openMover(it.symbol, it.name)}
                  className="text-left w-full"
                >
                  <RankRow
                    rank={i + 1}
                    name={it.name}
                    nameSub={it.symbol}
                    main={
                      <span className="flex items-center gap-1">
                        <PctText value={it.delta_yi} suffix="亿" className="!text-[11px]" />
                        {it.first_seen && (
                          <span className="px-1 py-0.5 rounded bg-amber-50 text-amber-600 border border-amber-100 text-[8px] font-extrabold">
                            新进
                          </span>
                        )}
                      </span>
                    }
                    right={
                      <span className="flex flex-col items-end flex-shrink-0">
                        <span className="text-[10px] font-mono text-slate-500">
                          {it.hold_yi.toFixed(1)}亿
                        </span>
                        <span className="text-[9px] font-mono text-slate-400">
                          占比 {it.hold_pct.toFixed(2)}% · {it.delta_pct_abs > 0 ? '+' : ''}
                          {it.delta_pct_abs.toFixed(2)}pct
                        </span>
                      </span>
                    }
                  />
                </button>
              ))}
            </div>
          ) : movers ? (
            <EmptyHint text="该条件下无增减持记录" />
          ) : (
            <EmptyHint loading />
          )}
        </div>
      </SectionCard>

      {/* 口径脚注 */}
      <div className="xl:col-span-3 text-[9px] text-slate-400 leading-relaxed px-1">
        口径说明：持仓数据取自 CCASS 每只港股前 50 大披露席位，席位变动含过户/结算噪音；
        增减持按席持仓量跨窗口差分，估算市值 = 数量 × 最新收盘价（除净日有偏差）；
        港股通持股已并入「内资·港股通」席（与港交所南向披露同口径），未重复计算。
      </div>
    </div>
  );
};