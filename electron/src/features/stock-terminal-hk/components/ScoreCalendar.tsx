/** 分数日历：个股历史推理分数画在月历上（红正绿负、深浅按绝对值、同日多模型取最新）
 *
 * 交互：
 * - 点击有分数日期 -> 整表切换到该信号日（onBarClick）
 * - 点击无分数日期 -> 弹「是否现在推理」（用该日前一交易日的股市数据，onInfer 处理）
 * - 拖动跨多个日期 -> 弹批量推理确认（range 模式，onBatchInfer 处理）
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, CalendarDays, Zap, TrendingUp, Activity } from 'lucide-react';
import { Modal, Spin, message } from 'antd';
import type { InferenceExecutionResult } from '../../../services/modelTrainingService';
import { modelTrainingService } from '../../../services/modelTrainingService';

/** 单日分数条目：value 用于着色，score 为基准值（同 value），side 为信号方向 */
export interface CalendarScore { date: string; value: number; side: string | null; }

/** 月份聚合：key=YYYY-MM（如 2026-08），scores 为当月每日分数（最新覆盖同模型重复） */
export interface MonthBucket { key: string; year: number; month: number; scores: Map<string, number>; sides: Map<string, string>; }

/** 分数着色：红=正、绿=负（A股涨红跌绿），深浅按 |v|：0-0.05 淡 / 0.05-0.10 中 / 0.10-0.20 深 / ≥0.20 最深+白字 */
export function scoreCellClass(v: number): string {
  const a = Math.abs(v);
  const pos = v >= 0;
  const deep = a >= 0.20;
  const mid = a >= 0.10;
  const light = a >= 0.05;
  const base = pos
    ? (deep ? 'bg-rose-600' : mid ? 'bg-rose-400' : light ? 'bg-rose-300' : 'bg-rose-100')
    : (deep ? 'bg-emerald-600' : mid ? 'bg-emerald-500' : light ? 'bg-emerald-300' : 'bg-emerald-100');
  const txt = deep ? 'text-white' : pos ? 'text-rose-700' : 'text-emerald-700';
  return `${base} ${txt}`;
}

export function fmtScore(v: number): string {
  return `${v >= 0 ? '+' : ''}${v.toFixed(4)}`;
}

/** 聚合多模型分数历史为 月->日 映射（同日多模型取最新 created_at，即 items 顺序末位） */
export function bucketByMonth(items: { trade_date: string; fusion_score: number | null; signal_side: string | null }[]): MonthBucket[] {
  const byMonth = new Map<string, { year: number; month: number; scores: Map<string, number>; sides: Map<string, string> }>();
  for (const it of items) {
    if (it.fusion_score == null) continue;
    const d = String(it.trade_date ?? '').slice(0, 10);
    if (d.length !== 10) continue;
    const key = d.slice(0, 7);
    let b = byMonth.get(key);
    if (!b) {
      b = { year: Number(d.slice(0, 4)), month: Number(d.slice(5, 7)), scores: new Map(), sides: new Map() };
      byMonth.set(key, b);
    }
    b.scores.set(d, Number(it.fusion_score));  // 后写覆盖先写：取最新
    if (it.signal_side) b.sides.set(d, String(it.signal_side));
  }
  return [...byMonth.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([key, b]) => ({ key, ...b }));
}

export function showInferenceResult(date: string, res: InferenceExecutionResult | undefined): void {
  if (!res || res.success === false) {
    message.error(`${date} 推理未产生结果${res?.error_message ? `（${res.error_message}）` : ''}`);
    return;
  }
  const sig = res.signals_count ?? 0;
  const pred = res.prediction_trade_date || res.target_date || date;
  const dataD = res.data_trade_date || date;
  const dist = res.score_distribution;
  const mkt = res.market_signal;
  const goldZone = res.gold_zone_count;
  const boards = (res.board_top1 ?? []).slice(0, 3);

  const landedOnClicked = pred === date;
  const hint = landedOnClicked
    ? `信号已写入 ${pred}（与点击日一致），日历该日格将点亮。`
    : `信号写入生效日 ${pred}（基于数据日 ${dataD} 的 T+1），点击日 ${date} 仍为空属正常——请切换到 ${pred} 查看。`;

  Modal.info({
    title: (
      <div className="flex items-center gap-1.5">
        <TrendingUp className="w-4 h-4 text-rose-500" />
        <span>{date} 推理完成</span>
      </div>
    ),
    width: 460,
    okText: '知道了',
    content: (
      <div className="text-[12px] text-slate-700 space-y-2 pt-1">
        <div className="flex items-center gap-1.5 text-[11px] text-blue-700 bg-blue-50 border border-blue-100 rounded px-2 py-1">
          <Activity className="w-3 h-3 shrink-0" />
          <span>{hint}</span>
        </div>
        <div className="grid grid-cols-2 gap-x-3 gap-y-1">
          <Stat label="信号条数" value={sig.toLocaleString()} />
          <Stat label="数据基准日" value={dataD} />
          <Stat label="信号生效日" value={pred} accent="text-rose-600" />
          {mkt ? <Stat label="市场信号" value={mkt.label} /> : null}
          {dist ? <Stat label="正分占比" value={`${(dist.positive_pct * 100).toFixed(1)}%`} /> : null}
          {dist ? <Stat label="均分/中位" value={`${fmtScore(dist.mean)} / ${fmtScore(dist.median)}`} /> : null}
          {typeof goldZone === 'number' ? <Stat label="黄金区个股" value={goldZone.toLocaleString()} /> : null}
        </div>
        {boards.length > 0 && (
          <div className="border-t border-slate-100 pt-1.5">
            <div className="text-[11px] font-bold text-slate-500 mb-1">板块 Top1</div>
            <div className="space-y-0.5">
              {boards.map(b => (
                <div key={b.board} className="flex items-center justify-between text-[11px]">
                  <span className="text-slate-600">{b.board}</span>
                  <span className="font-mono text-rose-600">{fmtScore(b.top1_score)} <span className="text-slate-400">{b.top1_name}</span></span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    ),
  });
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: string }): JSX.Element {
  return (
    <div className="flex items-center justify-between">
      <span className="text-slate-400">{label}</span>
      <span className={`font-mono font-bold ${accent ?? 'text-slate-700'}`}>{value}</span>
    </div>
  );
}

const WD = ['日', '一', '二', '三', '四', '五', '六'];  // 周天为第一天

interface Props {
  symbol: string;          // suffix（600519.SH）
  onBarClick?: (date: string) => void;  // 点击日期 -> 整表切换到该信号日分数
  selectedDate?: string | null;  // 当前列表基准信号日（琥珀色圈高亮）
  height?: number;         // 可用高度，决定是否滚动
  /** 当前筛选模型（列表下拉选的 model_id），推理补分数时用；缺省=全模型融合 */
  modelId?: string;
  /** 推理完成后回调（单日或批量），供外层刷新日历数据 */
  onInferred?: () => void;
  /** 变化时重新拉取推理历史（外层在推理完成后自增触发刷新） */
  refreshKey?: number;
}

export function ScoreCalendar({ symbol, onBarClick, selectedDate, modelId, onInferred, refreshKey = 0 }: Props) {
  const [items, setItems] = useState<{ trade_date: string; fusion_score: number | null; signal_side: string | null }[]>([]);
  const [loading, setLoading] = useState(false);
  const [viewKey, setViewKey] = useState<string>('');  // 当前查看月份 YYYY-MM（空=最新月）
  const [inferring, setInferring] = useState(false);
  // 拖动选择：按下无分数日期开始，滑过多日成区间
  const dragStartRef = useRef<string | null>(null);
  const dragDatesRef = useRef<Set<string>>(new Set());
  // 拖过有分数格子松手时抑制一次 onClick（否则推理确认+切日期同时弹）
  const suppressClickRef = useRef(false);

  // 生效模型：用户筛选了模型用筛选模型，否则用模型管理里的默认模型（而非全模型融合）
  const [resolvedModelId, setResolvedModelId] = useState<string | undefined>(modelId);
  useEffect(() => {
    let cancelled = false;
    if (modelId) {
      setResolvedModelId(modelId);
      return;
    }
    // 未筛选：取默认模型
    modelTrainingService.getDefaultModel().then((m) => {
      const mid = (m as any)?.model_id || (m as any)?.id;
      if (!cancelled && mid) setResolvedModelId(mid);
    }).catch(() => { /* 无默认模型则空 */ });
    return () => { cancelled = true; };
  }, [modelId]);

  useEffect(() => {
    if (!symbol) { setItems([]); return; }
    let cancelled = false;
    setLoading(true);
    const code = symbol.split('.')[0];
    modelTrainingService.getStockInferenceHistory(code, 500, resolvedModelId || undefined).then(resp => {
      if (!cancelled) setItems(resp?.items ?? []);
    }).catch(() => { if (!cancelled) setItems([]); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [symbol, refreshKey, resolvedModelId]);

  /** 当前生效模型（与刷新数据、模型管理推理同口径）。
   * 优先用下拉筛选模型；未筛选时取模型管理的默认模型——绝不使用
   * history.models[0]，因为那与刷新时按默认模型过滤不一致，会导致
   * 「推理了但刷新后看不到分数」的错觉。 */
  const activeModelId = modelId || resolvedModelId;

  /** 单日推理：用所选日（后端会回退到最近可用交易日）补分数。
   * 推理与刷新必须用同一个模型，否则信号落库后刷新查不到。 */
  const inferSingle = async (date: string) => {
    const model = activeModelId;
    if (!model) {
      message.error('未选择模型且无默认模型，请先在「模型管理」设置默认模型或选择模型');
      return;
    }
    Modal.confirm({
      title: '该日期无推理分数',
      content: `是否现在用该模型推理 ${date}？\n（将使用该日前一交易日的股市数据，与「模型管理」推理口径一致）`,
      okText: '开始推理',
      cancelText: '取消',
      onOk: async () => {
        setInferring(true);
        try {
          const res = await modelTrainingService.runModelInference(model, date);
          showInferenceResult(date, res as InferenceExecutionResult);
          const pred = (res as InferenceExecutionResult)?.prediction_trade_date || date;
          pendingNavRef.current = pred;   // 刷新数据到位后跳到生效日所在月
          onInferred?.();
        } catch {
          message.error(`${date} 推理失败，请稍后重试`);
        } finally {
          setInferring(false);
        }
      },
    });
  };

  /** 批量推理（range 模式）：拖动选中的日期区间。与单日推理同口径用 activeModelId。 */
  const inferBatch = (dates: string[]) => {
    if (!dates.length) return;
    const model = activeModelId;
    if (!model) {
      message.error('未选择模型且无默认模型，请先在「模型管理」设置默认模型或选择模型');
      return;
    }
    const sorted = [...dates].sort();
    const start = sorted[0], end = sorted[sorted.length - 1];
    Modal.confirm({
      title: `批量推理 ${sorted.length} 个交易日`,
      content: `区间 ${start} ~ ${end}，将按交易日逐个补推理分数（与「模型管理」批量推理同口径）。是否开始？`,
      okText: '开始批量推理',
      cancelText: '取消',
      onOk: async () => {
        setInferring(true);
        try {
          const batch = await modelTrainingService.submitBatchInference({
            model_id: model,
            mode: 'range',
            start_date: start,
            end_date: end,
            reuse_existing: true,
          });
          // 轮询直到完成（分批推理跑后台，最多等 10 分钟）
          const deadline = Date.now() + 10 * 60 * 1000;
          while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 3000));
            const st = await modelTrainingService.getBatchInference(batch.batch_id);
            if (st.status === 'completed' || st.status === 'partial' || st.status === 'failed') {
              if (st.status === 'failed') message.error('批量推理失败');
              else message.success(`批量推理完成（${st.progress_done}/${st.progress_total ?? st.progress_done}）`);
              onInferred?.();
              return;
            }
          }
          message.warning('批量推理仍在后台进行，稍后刷新日历可见');
          onInferred?.();
        } catch {
          message.error('批量推理提交失败');
        } finally {
          setInferring(false);
        }
      },
    });
  };

  const months = useMemo(() => bucketByMonth(items), [items]);

  // 默认跳到最新有分数的月份
  const [initialized, setInitialized] = useState(false);
  const curKey = months.length ? months[months.length - 1].key : '';
  useEffect(() => {
    if (!initialized && curKey) { setViewKey(curKey); setInitialized(true); }
  }, [curKey, initialized]);

  // 推理完成刚返回 prediction_trade_date 后，刷新数据到位时把视图跳到该日所在月，
  // 让用户新点亮的格子可见（信号写的是 T+1 生效日，常落在下一个月）。
  const pendingNavRef = useRef<string | null>(null);
  useEffect(() => {
    const target = pendingNavRef.current;
    if (!target || !months.length) return;
    const targetMonth = target.slice(0, 7);
    if (months.some(m => m.key === targetMonth)) {
      setViewKey(targetMonth);
      pendingNavRef.current = null;
    }
  }, [months]);

  const monthIdx = months.findIndex(m => m.key === viewKey);
  const bucket = monthIdx >= 0 ? months[monthIdx] : null;

  // 计算月历格子
  const cells = useMemo(() => {
    if (!bucket) return [];
    const { year, month } = bucket;
    const first = new Date(year, month - 1, 1);
    const lead = first.getDay();   // 周天(0)为第一天
    const days = new Date(year, month, 0).getDate();
    const out: ({ kind: 'blank' } | { kind: 'day'; date: string; day: number; value: number | null; side: string | null; today: boolean; active: boolean })[] = [];
    for (let i = 0; i < lead; i++) out.push({ kind: 'blank' });
    for (let d = 1; d <= days; d++) {
      const date = `${year}-${String(month).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      out.push({
        kind: 'day', date, day: d,
        value: bucket.scores.get(date) ?? null,
        side: bucket.sides.get(date) ?? null,
        today: date === new Date().toISOString().slice(0, 10),
        active: date === selectedDate,   // 当前列表基准信号日
      });
    }
    return out;
  }, [bucket, selectedDate]);

  const monthScoreRange = useMemo(() => {
    const vals = cells.filter((c): c is any => c.kind === 'day' && c.value != null).map((c: any) => c.value);
    if (!vals.length) return null;
    return { min: Math.min(...vals), max: Math.max(...vals), count: vals.length };
  }, [cells]);

  const prev = () => { if (monthIdx > 0) setViewKey(months[monthIdx - 1].key); };
  const next = () => { if (monthIdx >= 0 && monthIdx < months.length - 1) setViewKey(months[monthIdx + 1].key); };

  // ── 拖动多选（仅无分数日期）：按下开始，滑过多日成区间，松手批量推理 ──
  const [dragSet, setDragSet] = useState<Set<string>>(new Set());

  const startDrag = (date: string) => {
    dragStartRef.current = date;
    dragDatesRef.current = new Set([date]);
    setDragSet(new Set([date]));
  };
  const extendDrag = (date: string) => {
    if (!dragStartRef.current) return;
    dragDatesRef.current.add(date);
    setDragSet(new Set(dragDatesRef.current));
  };
  const endDrag = (commit: boolean) => {
    const dates = dragDatesRef.current;
    const wasDragging = dragStartRef.current != null;
    dragStartRef.current = null;
    dragDatesRef.current = new Set();
    setDragSet(new Set());
    if (!commit || !dates.size) return;
    suppressClickRef.current = wasDragging;   // 拖过的松手不触发 onClick 切日期
    if (dates.size > 1) inferBatch([...dates]);
    else inferSingle([...dates][0]);   // 单击无分数日期 -> 单日推理
  };

  // 拖出格子/日历区松开也要收尾；endDragRef 每帧更新，避免窗口监听持首帧闭包
  const endDragRef = useRef<(commit: boolean) => void>(() => {});
  endDragRef.current = endDrag;
  useEffect(() => {
    const up = () => endDragRef.current(true);
    window.addEventListener('mouseup', up);
    return () => window.removeEventListener('mouseup', up);
  }, []);

  return (
    <div className="flex flex-col gap-2 h-full">
      {/* 月切换 */}
      <div className="flex items-center justify-between px-1">
        <button onClick={prev} disabled={monthIdx <= 0}
          className="w-5 h-5 rounded-md border border-slate-200 flex items-center justify-center text-slate-500 hover:bg-slate-50 disabled:opacity-30">
          <ChevronLeft className="w-3 h-3" />
        </button>
        <span className="text-xs font-black text-slate-700 font-mono">{bucket ? `${bucket.year}年${bucket.month}月` : '--'}</span>
        <button onClick={next} disabled={monthIdx < 0 || monthIdx >= months.length - 1}
          className="w-5 h-5 rounded-md border border-slate-200 flex items-center justify-center text-slate-500 hover:bg-slate-50 disabled:opacity-30">
          <ChevronRight className="w-3 h-3" />
        </button>
      </div>

      {/* 星期头 */}
      <div className="grid grid-cols-7 text-center text-[9px] font-bold text-slate-400">
        {WD.map(w => <span key={w}>{w}</span>)}
      </div>

      {/* 月历格子 */}
      <Spin spinning={loading || inferring} size="small" tip={inferring ? '推理中…' : undefined}>
        <div className="grid grid-cols-7 gap-1">
          {cells.map((c, i) => c.kind === 'blank' ? <span key={`b${i}`} /> : (
            <button
              key={c.date}
              onMouseDown={() => { if (c.value == null) startDrag(c.date); }}
              onMouseEnter={() => { if (c.value == null && dragStartRef.current) extendDrag(c.date); }}
              onMouseUp={() => endDrag(true)}
              onClick={() => {
                if (c.value == null || dragStartRef.current) return;
                if (suppressClickRef.current) { suppressClickRef.current = false; return; }
                onBarClick?.(c.date);
              }}
              disabled={c.value == null && inferring}
              title={c.value != null
                ? `${c.date} 分数 ${fmtScore(c.value)}${c.side ? ` · ${c.side}` : ''}（点击整表切换当天）`
                : `${c.date} 无推理 · 点击推理该日；按住拖动可批量选多日`}
              className={`aspect-square rounded-md text-[9px] font-mono font-bold flex flex-col items-center justify-center leading-none border transition-transform ${
                c.value != null ? 'hover:scale-105 cursor-pointer' : 'cursor-pointer hover:border-blue-300 hover:bg-blue-50'
              } ${
                c.value == null
                  ? (dragSet.has(c.date) ? 'bg-blue-100 text-blue-500 border-blue-400' : 'bg-slate-50 text-slate-300 border-slate-100')
                  : `${scoreCellClass(c.value)} border-transparent`
              } ${c.today ? 'ring-2 ring-blue-400 ring-offset-1' : ''} ${c.active ? 'ring-2 ring-amber-500 ring-offset-1' : ''}`}
            >
              {c.day}
              {c.value != null && <span className="text-[7px] opacity-80">{c.side === 'BUY' ? 'B' : c.side === 'SELL' ? 'S' : ''}</span>}
            </button>
          ))}
          {cells.length === 0 && (
            <div className="col-span-7 flex flex-col items-center justify-center gap-1 py-8 text-[10px] text-slate-400">
              <CalendarDays className="w-5 h-5 opacity-40" />
              暂无推理分数历史
            </div>
          )}
        </div>
      </Spin>

      {/* 拖动提示：拖动中显示已选天数 */}
      {dragSet.size > 1 && (
        <div className="flex items-center gap-1 text-[9px] font-bold text-blue-600 bg-blue-50 border border-blue-200 rounded px-2 py-1">
          <Zap className="w-3 h-3" /> 已选 {dragSet.size} 天，松手批量推理
        </div>
      )}

      {/* 月份统计 + 图例 */}
      <div className="mt-auto pt-1.5 border-t border-slate-100 flex flex-col gap-1">
        {monthScoreRange && (
          <div className="flex items-center justify-between text-[9px] font-mono text-slate-500 px-1">
            <span>推理日 <b className="text-slate-700">{monthScoreRange.count}</b> 天</span>
            <span>最低 <b className="text-emerald-600">{monthScoreRange.min.toFixed(3)}</b> · 最高 <b className="text-rose-600">{monthScoreRange.max.toFixed(3)}</b></span>
          </div>
        )}
        <div className="flex items-center justify-center gap-1 flex-wrap text-[8px] text-slate-400">
          <span>红=正分</span>
          <span className="w-3 h-3 rounded-sm bg-rose-100" />
          <span className="w-3 h-3 rounded-sm bg-rose-300" />
          <span className="w-3 h-3 rounded-sm bg-rose-400" />
          <span className="w-3 h-3 rounded-sm bg-rose-600" />
          <span className="ml-1">绿=负分</span>
          <span className="w-3 h-3 rounded-sm bg-emerald-100" />
          <span className="w-3 h-3 rounded-sm bg-emerald-300" />
          <span className="w-3 h-3 rounded-sm bg-emerald-500" />
          <span className="w-3 h-3 rounded-sm bg-emerald-600" />
          <span className="ml-1">B=买入 S=卖出</span>
        </div>
      </div>
    </div>
  );
}
