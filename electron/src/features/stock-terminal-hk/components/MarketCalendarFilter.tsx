/** 大盘 MA20 日历筛选：概念板块与推理模型之间的日历图标，点开月历按日期筛选推理排序
 *
 * 日历格按上证指数收盘相对 MA20 的偏离度着色（A股涨红跌绿）：
 * 高于 MA20 红色、远高于越红；低于 MA20 绿色、偏离越远越绿。
 * - 点击有推理的日期 -> 列表切到该信号日（onPickDate）
 * - 点击无推理的交易日 -> 弹窗确认补推理（用该日前一交易日数据）
 * - 每格下方小字 = 该日 Top10 推理信号平均分
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react';
import { Modal, Popover, Spin, message } from 'antd';
import { stockTerminalService, MarketCalendarDay } from '../services/stockTerminalService';
import type { InferenceExecutionResult } from '../../../services/modelTrainingService';
import { modelTrainingService } from '../../../services/modelTrainingService';
import { showInferenceResult } from './ScoreCalendar';

const WD = ['日', '一', '二', '三', '四', '五', '六'];  // 周天为第一天

/** 偏离度着色：红=高于MA20（越偏越深）、绿=低于MA20；|dev|% 分四档 0/1.5/3/5 */
export function devCellClass(dev: number): string {
  const a = Math.abs(dev);
  const pos = dev >= 0;
  if (a >= 5) return pos ? 'bg-rose-600 text-white' : 'bg-emerald-600 text-white';
  if (a >= 3) return pos ? 'bg-rose-500 text-white' : 'bg-emerald-500 text-white';
  if (a >= 1.5) return pos ? 'bg-rose-300 text-rose-800' : 'bg-emerald-300 text-emerald-800';
  return pos ? 'bg-rose-100 text-rose-700' : 'bg-emerald-100 text-emerald-700';
}

export function fmtAvg(v: number | null | undefined): string {
  return v == null ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(4)}`;
}

interface Props {
  /** 当前列表基准信号日（琥珀圈高亮），undefined=最新 */
  date?: string;
  /** 当前筛选模型（推理概况/补推理口径），undefined=全模型 */
  model?: string;
  onPickDate: (d?: string) => void;
  /** 补推理完成后回调（外层刷新列表） */
  onInferred?: () => void;
}

export function MarketCalendarFilter({ date: selectedDate, model, onPickDate, onInferred }: Props) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState<MarketCalendarDay[]>([]);
  const [loading, setLoading] = useState(false);
  const [inferring, setInferring] = useState(false);
  const [viewKey, setViewKey] = useState('');   // 当前查看月份 YYYY-MM（空=最新月）
  // 补推理后自增触发重拉（带 refresh 跳过后端缓存）；ref 计数避免函数式 setState
  const refreshRef = useRef(0);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    stockTerminalService.getMarketCalendar(12, model || undefined, refreshRef.current > 0).then(d => {
      if (!cancelled) setDays(d.days ?? []);
    }).catch(() => { if (!cancelled) setDays([]); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [model, refreshTick]);

  const dayMap = useMemo(() => new Map(days.map(d => [d.date, d])), [days]);
  const months = useMemo(
    () => [...new Set(days.map(d => d.date.slice(0, 7)))].sort(),
    [days],
  );

  // 默认查看最新有数据的月份
  useEffect(() => {
    if (!viewKey && months.length) setViewKey(months[months.length - 1]);
  }, [months, viewKey]);

  const monthIdx = months.indexOf(viewKey);
  const [year, month] = viewKey ? [Number(viewKey.slice(0, 4)), Number(viewKey.slice(5, 7))] : [0, 0];

  const cells = useMemo(() => {
    if (!viewKey) return [];
    const first = new Date(year, month - 1, 1);
    const lead = first.getDay();
    const daysInMonth = new Date(year, month, 0).getDate();
    const out: ({ kind: 'blank' } | { kind: 'day'; date: string; day: number; entry: MarketCalendarDay | undefined })[] = [];
    for (let i = 0; i < lead; i++) out.push({ kind: 'blank' });
    for (let d = 1; d <= daysInMonth; d++) {
      const date = `${viewKey}-${String(d).padStart(2, '0')}`;
      out.push({ kind: 'day', date, day: d, entry: dayMap.get(date) });
    }
    return out;
  }, [viewKey, year, month, dayMap]);

  const monthStats = useMemo(() => {
    type DayCell = { kind: 'day'; date: string; day: number; entry: MarketCalendarDay | undefined };
    const scored = cells
      .filter((c): c is DayCell => c.kind === 'day' && !!c.entry?.has_inference)
      .map((c) => c.entry as MarketCalendarDay);
    if (!scored.length) return null;
    const avgs = scored.map(d => d.top10_avg).filter((v): v is number => v != null);
    return {
      count: scored.length,
      avgMin: avgs.length ? Math.min(...avgs) : null,
      avgMax: avgs.length ? Math.max(...avgs) : null,
    };
  }, [cells]);

  /** 无推理交易日 -> 确认补推理（当前筛选模型，缺省取默认模型） */
  const inferDay = async (d: string) => {
    let targetModel = model || '';
    if (!targetModel) {
      try {
        const m = await modelTrainingService.getDefaultModel();
        targetModel = (m as any)?.model_id || (m as any)?.id || '';
      } catch { /* 无默认模型则下方报错 */ }
    }
    Modal.confirm({
      title: `${d} 无推理信号`,
      content: `是否现在对该日执行推理？\n（将使用该日前一交易日的股市数据，完成后列表与日历自动刷新）`,
      okText: '开始推理',
      cancelText: '取消',
      onOk: async () => {
        if (!targetModel) { message.error('暂无可用模型'); return; }
        setInferring(true);
        try {
          const res = await modelTrainingService.runModelInference(targetModel, d);
          showInferenceResult(d, res as InferenceExecutionResult);
          refreshRef.current += 1;
          setRefreshTick(refreshRef.current);
          onInferred?.();
        } catch {
          message.error(`${d} 推理失败，请稍后重试`);
        } finally {
          setInferring(false);
        }
      },
    });
  };

  const handleCellClick = (c: { kind: 'day'; date: string; entry?: MarketCalendarDay }) => {
    if (!c.entry) return;                       // 非交易日
    if (c.entry.has_inference) {
      onPickDate(c.date);
      setOpen(false);
    } else {
      void inferDay(c.date);
    }
  };

  const prev = () => { if (monthIdx > 0) setViewKey(months[monthIdx - 1]); };
  const next = () => { if (monthIdx >= 0 && monthIdx < months.length - 1) setViewKey(months[monthIdx + 1]); };

  const content = (
    <div className="w-[286px] flex flex-col gap-1.5">
      {/* 标题 + 月份切换 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1 min-w-0">
          <span className="text-[10px] font-black text-slate-700 truncate">大盘 MA20 日历</span>
          <span className="text-[9px] text-slate-400">· 上证指数</span>
        </div>
        <div className="flex items-center gap-1">
          <button onClick={prev} disabled={monthIdx <= 0}
            className="w-4 h-4 rounded border border-slate-200 flex items-center justify-center text-slate-500 hover:bg-slate-50 disabled:opacity-30">
            <ChevronLeft className="w-2.5 h-2.5" />
          </button>
          <span className="text-[10px] font-black text-slate-700 font-mono w-14 text-center">{viewKey ? `${year}年${month}月` : '--'}</span>
          <button onClick={next} disabled={monthIdx < 0 || monthIdx >= months.length - 1}
            className="w-4 h-4 rounded border border-slate-200 flex items-center justify-center text-slate-500 hover:bg-slate-50 disabled:opacity-30">
            <ChevronRight className="w-2.5 h-2.5" />
          </button>
        </div>
      </div>

      {/* 星期头 */}
      <div className="grid grid-cols-7 text-center text-[8px] font-bold text-slate-400">
        {WD.map(w => <span key={w}>{w}</span>)}
      </div>

      {/* 月历格子 */}
      <Spin spinning={loading || inferring} size="small" tip={inferring ? '推理中…' : undefined}>
        <div className="grid grid-cols-7 gap-0.5">
          {cells.map((c, i) => c.kind === 'blank' ? <span key={`b${i}`} /> : (
            <button
              key={c.date}
              onClick={() => handleCellClick(c)}
              disabled={!c.entry || inferring}
              title={c.entry
                ? `${c.date} 收盘 ${c.entry.close.toFixed(2)} / MA20 ${c.entry.ma20.toFixed(2)}（偏离 ${c.entry.dev_pct >= 0 ? '+' : ''}${c.entry.dev_pct}%）`
                  + (c.entry.has_inference
                    ? ` · 信号 ${c.entry.signal_count} 条 · Top10 均分 ${fmtAvg(c.entry.top10_avg)}（点击筛选该日）`
                    : ' · 无推理，点击补推理')
                : `${c.date} 非交易日`}
              className={`aspect-square rounded text-[9px] font-mono font-bold flex flex-col items-center justify-center leading-none border transition-transform ${
                !c.entry
                  ? 'bg-slate-50 text-slate-300 border-transparent cursor-default'
                  : `${devCellClass(c.entry.dev_pct)} ${c.entry.has_inference
                    ? 'border-transparent hover:scale-110 cursor-pointer'
                    : 'border-dashed border-slate-400/60 cursor-pointer hover:scale-105'}`
              } ${selectedDate === c.date ? 'ring-2 ring-amber-500 ring-offset-1' : ''}`}
            >
              {c.day}
              {c.entry?.has_inference && c.entry.top10_avg != null && (
                <span className="text-[6px] opacity-80">{c.entry.top10_avg.toFixed(3)}</span>
              )}
            </button>
          ))}
          {cells.length === 0 && !loading && (
            <div className="col-span-7 flex flex-col items-center gap-1 py-6 text-[10px] text-slate-400">
              <CalendarDays className="w-4 h-4 opacity-40" />
              暂无日历数据
            </div>
          )}
        </div>
      </Spin>

      {/* 月份统计 + 图例 */}
      <div className="flex flex-col gap-1 pt-1 border-t border-slate-100">
        {monthStats && (
          <div className="flex items-center justify-between text-[8px] font-mono text-slate-500">
            <span>推理日 <b className="text-slate-700">{monthStats.count}</b> 天</span>
            <span>Top10均分 <b className="text-rose-600">{fmtAvg(monthStats.avgMin)}</b> ~ <b className="text-rose-600">{fmtAvg(monthStats.avgMax)}</b></span>
          </div>
        )}
        <div className="flex items-center justify-center gap-1 flex-wrap text-[7px] text-slate-400">
          <span>高于MA20</span>
          <span className="w-2.5 h-2.5 rounded-sm bg-rose-100" />
          <span className="w-2.5 h-2.5 rounded-sm bg-rose-300" />
          <span className="w-2.5 h-2.5 rounded-sm bg-rose-500" />
          <span className="w-2.5 h-2.5 rounded-sm bg-rose-600" />
          <span className="ml-1">低于</span>
          <span className="w-2.5 h-2.5 rounded-sm bg-emerald-100" />
          <span className="w-2.5 h-2.5 rounded-sm bg-emerald-300" />
          <span className="w-2.5 h-2.5 rounded-sm bg-emerald-500" />
          <span className="w-2.5 h-2.5 rounded-sm bg-emerald-600" />
          <span className="ml-1">越偏越深 · 虚线=无推理</span>
        </div>
      </div>
    </div>
  );

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomLeft"
      content={content}
      arrow={false}
    >
      <button
        title="大盘 MA20 日历 · 按日期筛选推理排序"
        className={`h-[22px] w-8 shrink-0 rounded-md border flex items-center justify-center transition-colors ${
          selectedDate
            ? 'border-amber-300 bg-amber-50 text-amber-600'
            : 'border-slate-200 text-indigo-500 hover:bg-indigo-50 hover:border-indigo-300'
        }`}
      >
        <CalendarDays className="w-3.5 h-3.5" />
      </button>
    </Popover>
  );
}
