/** 聪明钱 · 个股档案弹窗（细案 §6.1 StockDrawer：时间线 / 席位构成 / 大宗明细 / 北向序列）。
 *
 * 2026-10-29 用户反馈：不要右侧抽屉，改为居中 Modal 弹出；内容与取数口径不变。
 *
 * 消费 GET /smart-money/stock/{symbol}（前缀式代码）。纪律：
 * 缺失「—」不补 0；lhb 的 fwd_ret_* 是东财历史回溯口径（只展示，标注非预测）；
 * 金额 元→亿、占比/折溢率分数→%；柱分位条用中性紫（分不是涨跌）。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Building2, CalendarClock, Coins, LineChart as LineChartIcon, ShieldAlert } from 'lucide-react';
import { Modal, Tag, Tooltip } from 'antd';
import {
  getSmartMoneyStock,
  type SmartDzjyRow,
  type SmartStockDetailResponse,
  type SmartStockSeatRow,
  type SmartTimelineEvent,
} from '../../services/smartMoneyService';
import {
  MISSING,
  directionLabel,
  eventTypeLabel,
  fmtNum,
  fmtPct,
  fmtSigned,
  fmtYiFromYuan,
  pillarLabel,
  scoreCls,
  seatClassLabel,
  signCls,
} from '../../smartMoneyVocabulary';
import { AXIS_LABEL, CHART_COLORS, SmartChart } from './charts';

const EVENT_TONES: Record<string, string> = {
  lhb: 'bg-rose-50 text-rose-700 border-rose-100',
  inst: 'bg-purple-50 text-purple-700 border-purple-100',
  block_trade: 'bg-amber-50 text-amber-700 border-amber-100',
  survey: 'bg-sky-50 text-sky-700 border-sky-100',
};

const SIDE_LABEL: Record<string, string> = { buy: '买入席位', sell: '卖出席位' };

/** 柱值按 unit（ratio/z/count）格式化 */
function fmtPillarVal(v: number | null | undefined, unit: string | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return MISSING;
  if (unit === 'ratio') return fmtPct(v, 2);
  if (unit === 'z') return fmtSigned(v, 2);
  return fmtNum(v, unit === 'count' ? 0 : 2);
}

const EventRow: React.FC<{ t: SmartTimelineEvent }> = ({ t }) => {
  const facts: string[] = [];
  if (t.type === 'lhb') {
    if (t.net_buy != null) facts.push(`净买 ${fmtYiFromYuan(t.net_buy)} 亿`);
    if (t.net_ratio != null) facts.push(`净买占比 ${fmtPct(t.net_ratio, 1)}`);
    if (t.reason) facts.push(t.reason);
  } else if (t.type === 'inst') {
    if (t.inst_net_buy != null) facts.push(`机构净买 ${fmtYiFromYuan(t.inst_net_buy)} 亿`);
    if (t.inst_buy_seats != null || t.inst_sell_seats != null) {
      facts.push(`买席 ${t.inst_buy_seats ?? MISSING} / 卖席 ${t.inst_sell_seats ?? MISSING}`);
    }
  } else if (t.type === 'block_trade') {
    if (t.deal_price != null) facts.push(`成交价 ${fmtNum(t.deal_price, 2)}`);
    if (t.premium != null) facts.push(`折溢率 ${fmtPct(t.premium, 2)}`);
    if (t.amount != null) facts.push(`金额 ${fmtYiFromYuan(t.amount)} 亿`);
    if (t.n_trades != null) facts.push(`${t.n_trades} 笔`);
  } else if (t.type === 'survey') {
    if (t.inst_count != null) facts.push(`机构 ${t.inst_count} 家`);
    if (t.reception_type) facts.push(t.reception_type);
    if (t.reception_place) facts.push(t.reception_place);
  }
  return (
    <div className="flex items-start gap-1.5 px-1 py-1 border-b border-slate-50 last:border-0">
      <span className="shrink-0 text-[9px] font-mono text-slate-400 pt-0.5 w-[52px]">
        {t.date?.slice(2)}
      </span>
      <span
        className={`shrink-0 text-[9px] font-bold border rounded px-1 ${
          EVENT_TONES[t.type] ?? 'bg-slate-50 text-slate-500 border-slate-200'
        }`}
      >
        {t.label || eventTypeLabel(t.type)}
      </span>
      <span className="min-w-0 flex-1 text-[10px] text-slate-600 leading-snug">
        {facts.length ? facts.join(' · ') : MISSING}
        {t.interpretation ? (
          <span className="text-slate-400">（{t.interpretation}）</span>
        ) : null}
      </span>
      {t.type === 'lhb' && (t.fwd_ret_5d != null || t.fwd_ret_10d != null) && (
        <Tooltip title="东财口径历史前向收益（回溯展示，非预测；不入信号）">
          <span className="shrink-0 text-[9px] font-mono text-slate-400">
            h5 <span className={signCls(t.fwd_ret_5d)}>{fmtPct(t.fwd_ret_5d, 1)}</span>
            {' · '}h10 <span className={signCls(t.fwd_ret_10d)}>{fmtPct(t.fwd_ret_10d, 1)}</span>
          </span>
        </Tooltip>
      )}
    </div>
  );
};

const SeatRow: React.FC<{ s: SmartStockSeatRow }> = ({ s }) => (
  <div className="grid grid-cols-[minmax(120px,1.5fr)_64px_minmax(0,0.7fr)_minmax(0,0.7fr)] gap-1.5 px-1 py-0.5 items-baseline border-b border-slate-50 last:border-0">
    <span className="min-w-0 flex items-baseline gap-1">
      <span className="text-[10px] font-bold text-slate-700 truncate" title={s.seat_name ?? ''}>
        {s.seat_name || MISSING}
      </span>
    </span>
    <span className="text-[9px] font-bold text-slate-500">{seatClassLabel(s.class, s.class_label)}</span>
    <Tooltip title={`买 ${fmtYiFromYuan(s.buy_amount)} 亿 · 卖 ${fmtYiFromYuan(s.sell_amount)} 亿`}>
      <span className={`text-right text-[10px] font-mono font-bold ${signCls(s.net_amount)}`}>
        {fmtYiFromYuan(s.net_amount)}
      </span>
    </Tooltip>
    <span className="text-right text-[9px] font-mono text-slate-400">
      {s.side === 'buy' ? fmtPct(s.buy_ratio, 1) : fmtPct(s.sell_ratio, 1)}
    </span>
  </div>
);

const DzjyRowView: React.FC<{ d: SmartDzjyRow }> = ({ d }) => (
  <div className="grid grid-cols-[52px_56px_56px_64px_minmax(0,1fr)] gap-1.5 px-1 py-0.5 items-baseline border-b border-slate-50 last:border-0">
    <span className="text-[9px] font-mono text-slate-400">{d.date?.slice(5)}</span>
    <span className="text-right text-[10px] font-mono text-slate-600">{fmtNum(d.deal_price, 2)}</span>
    <span className={`text-right text-[10px] font-mono ${signCls(d.premium)}`}>
      {fmtPct(d.premium, 2)}
    </span>
    <span className="text-right text-[10px] font-mono text-slate-600">{fmtYiFromYuan(d.amount)}</span>
    <span className="min-w-0 flex items-center gap-1 justify-end">
      {d.buyer_is_inst && (
        <span className="text-[8px] font-bold border rounded px-0.5 bg-purple-50 text-purple-700 border-purple-100">
          机构买
        </span>
      )}
      {d.seller_is_inst && (
        <span className="text-[8px] font-bold border rounded px-0.5 bg-amber-50 text-amber-700 border-amber-100">
          机构卖
        </span>
      )}
      <span className="text-[9px] text-slate-400 truncate" title={`${d.seat_buyer ?? ''} ← ${d.seat_seller ?? ''}`}>
        {d.seat_buyer || MISSING}
      </span>
    </span>
  </div>
);

interface SmartMoneyStockDrawerProps {
  symbol: string | null;
  open: boolean;
  onClose: () => void;
  /** 跳个股终端浮窗（页面级共用） */
  onOpenTerminal?: (symbol: string) => void;
}

export const SmartMoneyStockDrawer: React.FC<SmartMoneyStockDrawerProps> = ({
  symbol,
  open,
  onClose,
  onOpenTerminal,
}) => {
  const [data, setData] = useState<SmartStockDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!symbol) return;
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const resp = await getSmartMoneyStock(symbol);
      if (my !== seq.current) return;
      setData(resp);
    } catch (e) {
      if (my !== seq.current) return;
      setData(null);
      setError(e instanceof Error ? e.message : '个股档案加载失败');
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    if (open && symbol) void load();
  }, [open, symbol, load]);

  const item = data?.item ?? null;
  const pillars = data?.pillars ?? [];
  const timelines = data?.timeline ?? [];
  const north = data?.north ?? [];
  const dzjy = data?.dzjy ?? [];

  const northOption = React.useMemo(() => {
    const pts = north.filter((n) => n.pct_pp !== null);
    if (!pts.length) return null;
    return {
      tooltip: {
        trigger: 'axis',
        textStyle: { fontSize: 10 },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const i = ps[0]?.dataIndex ?? 0;
          const n = pts[i];
          if (!n) return '';
          return `${n.quarter}（报告期 ${n.report_date}）<br/>持股占比 ${fmtPct(n.pct_pp, 2)}<br/>环比 ${fmtSigned(n.chg_pp, 2)}pp`;
        },
      },
      grid: { left: 44, right: 10, top: 8, bottom: 18 },
      xAxis: {
        type: 'category',
        data: pts.map((n) => n.quarter),
        boundaryGap: false,
        axisLabel: { ...AXIS_LABEL, interval: Math.max(0, Math.floor(pts.length / 5)) },
      },
      yAxis: {
        type: 'value',
        axisLabel: { ...AXIS_LABEL, formatter: (v: number) => `${v.toFixed(1)}%` },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      series: [
        {
          type: 'line',
          data: pts.map((n) => Number(n.pct_pp) * 100),
          smooth: true,
          symbol: 'circle',
          symbolSize: 4,
          connectNulls: false,
          lineStyle: { width: 1.6, color: CHART_COLORS.score },
          itemStyle: { color: CHART_COLORS.score },
          areaStyle: { color: CHART_COLORS.scoreFill },
        },
      ],
    };
  }, [north]);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={1080}
      centered
      styles={{ body: { maxHeight: 'calc(100vh - 170px)', overflowY: 'auto', paddingRight: 8 } }}
      title={
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[13px] font-extrabold text-slate-800 truncate">
            {data?.name || symbol || '个股档案'}
          </span>
          <span className="text-[10px] font-mono text-slate-400">{symbol}</span>
          {data?.industry && (
            <span className="text-[9px] text-slate-500 bg-slate-100 rounded px-1">{data.industry}</span>
          )}
          {data && (
            <Tag
              color={data.on_board ? 'purple' : 'default'}
              className="!text-[9px] !leading-4 !m-0"
            >
              {data.on_board ? '在池' : '未在池'}
            </Tag>
          )}
          {data?.partial && (
            <Tooltip title="该日分区为部分数据（构建中断），统计可能不完整">
              <span className="text-[9px] text-amber-600 border border-amber-200 bg-amber-50 rounded px-1">
                部分数据
              </span>
            </Tooltip>
          )}
        </div>
      }
    >
      {error ? (
        <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1.5">
          <AlertCircle className="w-3.5 h-3.5 text-amber-600 shrink-0" />
          <span className="text-[10px] text-amber-800 min-w-0">{error}</span>
          <button
            onClick={() => void load()}
            className="ml-auto shrink-0 text-[10px] font-bold text-amber-700 hover:text-amber-900"
          >
            重试
          </button>
        </div>
      ) : loading && !data ? (
        <div className="text-[11px] text-slate-400 py-8 text-center">加载中…</div>
      ) : !data ? null : (
        <div className="flex flex-col gap-4 min-w-0 pb-6">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[9px] text-slate-400 font-mono">
              as_of {data.as_of ?? MISSING} · 生成 {data.generated_at ?? MISSING}
            </span>
            {onOpenTerminal && symbol && (
              <button
                type="button"
                onClick={() => {
                  onOpenTerminal(symbol);
                  onClose();
                }}
                className="ml-auto text-[9px] font-extrabold text-purple-700 bg-purple-50 border border-purple-100 rounded-full px-2 py-0.5 hover:bg-purple-100 transition-colors"
              >
                打开个股终端 ➔
              </button>
            )}
          </div>

          {/* 评分与柱分位 */}
          <section>
            <div className="flex items-center gap-2 px-1 pb-1">
              <Coins className="w-3.5 h-3.5 text-purple-600" />
              <span className="text-[11px] font-extrabold text-slate-700">聪明钱评分</span>
              <span className={`text-[16px] font-mono font-extrabold ${scoreCls(item?.score)}`}>
                {item?.score != null ? fmtNum(item.score, 1) : MISSING}
              </span>
              <span className="text-[9px] text-slate-400">
                覆盖柱 {item?.covered ?? MISSING}/{item?.n_pillars ?? MISSING}
              </span>
            </div>
            {pillars.length ? (
              <div className="flex flex-col">
                {pillars.map((p) => {
                  const pct = item?.pct?.[p.name] ?? null;
                  const val = item?.val?.[p.name] ?? null;
                  const pctW = pct === null ? 0 : Math.max(0, Math.min(100, Number(pct)));
                  return (
                    <div
                      key={p.name}
                      className="grid grid-cols-[118px_minmax(0,1fr)_74px_54px] gap-1.5 px-1 py-1 items-center border-b border-slate-50 last:border-0"
                    >
                      <span className="text-[9px] text-slate-500 truncate" title={p.label || pillarLabel(p.name)}>
                        {p.label || pillarLabel(p.name)}
                      </span>
                      <Tooltip title={`分位 ${pct === null ? MISSING : fmtNum(pct, 1)}（0–100，同批标的截面）`}>
                        <div className="relative h-[6px] rounded-full bg-slate-100 overflow-hidden">
                          <div
                            className="absolute inset-y-0 left-0 rounded-full bg-purple-600"
                            style={{ width: `${pctW}%` }}
                          />
                          {/* 50 分位参照线 */}
                          <div className="absolute inset-y-0 left-1/2 w-px bg-slate-300" />
                        </div>
                      </Tooltip>
                      <span className="text-right text-[9px] font-mono text-slate-500">
                        分位 {pct === null ? MISSING : fmtNum(pct, 1)}
                      </span>
                      <Tooltip title={`入柱方向：${directionLabel(p.direction)}`}>
                        <span
                          className={`text-center text-[9px] font-bold border rounded px-0.5 ${
                            p.direction === 1
                              ? 'bg-purple-50 text-purple-700 border-purple-100'
                              : p.direction === -1
                                ? 'bg-blue-50 text-blue-700 border-blue-100'
                                : 'bg-slate-50 text-slate-400 border-slate-200'
                          }`}
                        >
                          {directionLabel(p.direction)}
                        </span>
                      </Tooltip>
                      <span className="col-span-4 -mt-0.5 text-[9px] font-mono text-slate-400 text-right pr-1">
                        值 {fmtPillarVal(val, p.unit)}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="text-[10px] text-slate-400 px-1">
                该标的未入池（无评分/柱分位；下方事件与席位为原始事实）
              </div>
            )}
          </section>

          {/* 事件时间线 */}
          <section>
            <div className="flex items-center gap-2 px-1 pb-1">
              <CalendarClock className="w-3.5 h-3.5 text-purple-600" />
              <span className="text-[11px] font-extrabold text-slate-700">事件时间线</span>
              <span className="text-[9px] text-slate-400">近 365 天 · 日期降序</span>
            </div>
            {timelines.length ? (
              timelines.map((t, i) => <EventRow key={`${t.type}-${t.date}-${i}`} t={t} />)
            ) : (
              <div className="text-[10px] text-slate-400 px-1">近 365 天无聪明钱事件</div>
            )}
          </section>

          {/* 最近龙虎榜席位构成 */}
          <section>
            <div className="flex items-center gap-2 px-1 pb-1">
              <Building2 className="w-3.5 h-3.5 text-purple-600" />
              <span className="text-[11px] font-extrabold text-slate-700">最近龙虎榜席位构成</span>
              {data.seats?.date && (
                <span className="text-[9px] font-mono text-slate-400">{data.seats.date}</span>
              )}
            </div>
            {data.seats?.seats?.length ? (
              (['buy', 'sell'] as const).map((side) => {
                const rows = data.seats!.seats.filter((s) => s.side === side);
                if (!rows.length) return null;
                return (
                  <div key={side} className="mb-1">
                    <div className="text-[9px] font-extrabold text-slate-400 px-1">
                      {SIDE_LABEL[side]}（{rows.length}）
                    </div>
                    {rows.map((s, i) => (
                      <SeatRow key={`${side}-${s.seat_name}-${i}`} s={s} />
                    ))}
                  </div>
                );
              })
            ) : (
              <div className="text-[10px] text-slate-400 px-1">近 120 天无龙虎榜席位记录</div>
            )}
          </section>

          {/* 大宗明细 */}
          <section>
            <div className="flex items-center gap-2 px-1 pb-1">
              <ShieldAlert className="w-3.5 h-3.5 text-purple-600" />
              <span className="text-[11px] font-extrabold text-slate-700">大宗交易明细</span>
              <span className="text-[9px] text-slate-400">近 20 笔 · 折溢率正=溢价</span>
            </div>
            {dzjy.length ? (
              <>
                <div className="grid grid-cols-[52px_56px_56px_64px_minmax(0,1fr)] gap-1.5 px-1 pb-0.5 text-[8px] font-extrabold text-slate-400 border-b border-slate-100">
                  <span>日</span>
                  <span className="text-right">成交价</span>
                  <span className="text-right">折溢</span>
                  <span className="text-right">金额亿</span>
                  <span className="text-right">买方席位</span>
                </div>
                {dzjy.map((d, i) => (
                  <DzjyRowView key={`${d.date}-${i}`} d={d} />
                ))}
              </>
            ) : (
              <div className="text-[10px] text-slate-400 px-1">近 120 天无大宗交易</div>
            )}
          </section>

          {/* 北向持仓 */}
          <section>
            <div className="flex items-center gap-2 px-1 pb-1">
              <LineChartIcon className="w-3.5 h-3.5 text-purple-600" />
              <span className="text-[11px] font-extrabold text-slate-700">北向持股占比</span>
              <span className="text-[9px] text-slate-400">季度披露 · 延迟约 3 个月</span>
            </div>
            {northOption ? (
              <SmartChart option={northOption} height={130} />
            ) : (
              <div className="text-[10px] text-slate-400 px-1">无北向持仓记录（或该标的非陆股通标的）</div>
            )}
          </section>

          {data.study_ref && (
            <div className="text-[9px] text-slate-400 px-1 leading-snug">
              本档案的统计口径见「事件研究」区（{data.study_ref.families.join('、')}）。
              {data.study_ref.note}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
};
