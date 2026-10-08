import { useMemo, useState } from 'react';
import { LogLine, LogUsage, PositionRecord, TradeRecord } from '../api/client';
import { logoOf, modelColor } from './ModelCard';
import { fmtClock, fmtAgo, fmtDateTime, fmtDay, fmtWeekday } from '../utils/datetime';
import { fmtMoney, fmtNum } from '../utils/format';
import { renderInline, renderMarkdown } from '../utils/markdown';
import { DecItem, parseAnalysis } from '../utils/parseAnalysis';
import { sameSymbol, stockName } from '../utils/symbols';
import { attachFillsToRounds, LiveFill } from '../utils/liveFills';
import './ModelChat.css';
import { modeOf } from '../utils/modeTag';
import { asUpdater } from '../reactCompat';

/** 一个决策回合：一行日志 = 一次 LLM 分析（user 提示词 + assistant 输出）。
 *  复盘要求「时间到秒、内容不截断」，因此时间戳、token、成交都原样带到卡片里。 */
interface Round {
  seq: number; // 时间顺序编号（1 = 本页最早一轮）
  ts: string | null; // 日志写入时间（ISO，含时区）
  kind: 'review' | 'analysis';
  usage: LogUsage | null;
  user: string;
  thought: string;
  date: string | null; // 交易日（提示词里的日期，回退日志日期）
}

type Filter = 'all' | 'trade' | 'review' | 'idle';

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: '全部轮次' },
  { id: 'trade', label: '有成交' },
  { id: 'idle', label: '无成交' },
  { id: 'review', label: '盘后复盘' },
];

/** user prompt 里提取日期：英文 "today's (2026-08-25) positions." 或中文 "今日（2026-08-25）的持仓"。 */
function extractDate(content: string): string | null {
  const m = content.match(/[（(](\d{4}-\d{2}-\d{2})[）)]/);
  return m ? m[1] : null;
}

/** assistant 输出里的工具调用行（"已调用：<工具名>×N …"）——复盘看本轮用了什么工具。 */
function extractToolLine(thought: string): string {
  return (thought.match(/^\s*已调用[：:].*$/m) ?? [''])[0].trim();
}

const isUser = (role?: string) => role === 'user' || role === 'human';
const isAssistant = (role?: string) => role === 'assistant' || role === 'ai';
const joinContent = (msgs: { role?: string; content?: string }[], pick: (r?: string) => boolean) =>
  msgs
    .filter((m) => pick(m.role))
    .map((m) => (m.content ?? '').trim())
    .filter(Boolean)
    .join('\n\n');

/** 模型对话（单模型复盘视图）：
 *  回合头（轮次号 + 类型 + 模式 + 状态 + 秒级时间）→ 展开后：轮次元数据 /
 *  用户提示词 / 分析链路 / 推理论证 / 结构化决策 / 当日成交明细。
 *  全部字段来自真实数据，缺失显示 —，不编造（止损/止盈等无结构化数据就不占位）。 */
export default function ModelChat({
  logs,
  trades,
  positions,
  model,
  currency = '$',
  names = {},
  liveFills = [],
}: {
  logs: LogLine[];
  trades: TradeRecord[];
  positions?: PositionRecord[];
  model: string;
  currency?: string;
  names?: Record<string, string>;
  /** 通达信桥实盘成交（秒级时间），按时间窗归属到回合 */
  liveFills?: LiveFill[];
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [filter, setFilter] = useState<Filter>('all');

  /** 成交按日期索引（交易决策 匹配） */
  const tradesByDate = useMemo(() => {
    const map = new Map<string, TradeRecord[]>();
    for (const t of trades ?? []) {
      if (t.action !== 'buy' && t.action !== 'sell') continue;
      const arr = map.get(t.date) ?? [];
      arr.push(t);
      map.set(t.date, arr);
    }
    return map;
  }, [trades]);

  /** 某交易日收盘时该 symbol 的持仓（跨写法匹配快照键） */
  const qtyAt = (date: string, symbol: string): number | null => {
    const snap = (positions ?? []).find((p) => p.date === date);
    if (!snap) return null;
    for (const [sym, qty] of Object.entries(snap.positions ?? {})) {
      if (sym !== 'CASH' && sameSymbol(sym, symbol)) return Number(qty);
    }
    return null;
  };

  /** 回合分组：一行日志一个回合（A 股日志单行含 user+assistant）；
   *  港股把 user/assistant 拆到不同行，跨行并入最近的回合。 */
  const rounds: Round[] = useMemo(() => {
    const asc: Omit<Round, 'seq'>[] = [];
    let pending: Omit<Round, 'seq'> | null = null;
    for (const line of logs ?? []) {
      const msgs = line.new_messages ?? [];
      const kind: Round['kind'] = line.kind === 'review' ? 'review' : 'analysis';
      const ts = line.timestamp ?? null;
      const usage = line.usage ?? null;
      const user = joinContent(msgs, isUser);
      const thought = joinContent(msgs, isAssistant);
      const lineRound: Omit<Round, 'seq'> = {
        ts,
        kind,
        usage,
        user,
        thought,
        date: extractDate(user || thought) ?? (ts ? fmtDay(ts) : null),
      };
      if (user && !thought) {
        // 只有提示词：开一个新回合，等后续行的 assistant 补进来
        if (pending) asc.push(pending);
        pending = lineRound;
        continue;
      }
      if (pending && !pending.thought) {
        pending.thought = thought || pending.thought;
        pending.ts = pending.ts ?? ts;
        pending.usage = pending.usage ?? usage;
        asc.push(pending);
        pending = null;
        continue;
      }
      if (lineRound.user || lineRound.thought) asc.push(lineRound);
    }
    if (pending) asc.push(pending);
    return asc.map((r, i) => ({ ...r, seq: i + 1 }));
  }, [logs]);

  /** 每回合的结构化决策（parseAnalysis 从 JSON 块提取） */
  const parsed = useMemo(() => rounds.map((r) => parseAnalysis(r.thought)), [rounds]);
  /** 实盘成交按时间窗归属到回合（模拟盘只有日期，实盘秒级——两套口径分开算） */
  const liveFillsByRound = useMemo(() => attachFillsToRounds(rounds, liveFills), [rounds, liveFills]);
  const tradeCounts = useMemo(
    () =>
      rounds.map((r) => {
        const simCount = r.date ? (tradesByDate.get(r.date) ?? []).length : 0;
        const liveCount = liveFillsByRound.bySeq.get(r.seq)?.length ?? 0;
        return simCount + liveCount;
      }),
    [rounds, tradesByDate, liveFillsByRound],
  );

  const visible = rounds
    .map((r, i) => ({ r, i }))
    .filter(({ r, i }) => {
      if (filter === 'trade') return (tradeCounts[i] ?? 0) > 0;
      if (filter === 'idle') return (tradeCounts[i] ?? 0) === 0;
      if (filter === 'review') return r.kind === 'review';
      return true;
    })
    .reverse(); // 最新在上

  const toggle = (seq: number) =>
    setOpen(asUpdater((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    }));

  const allOpen = visible.length > 0 && visible.every(({ r }) => open.has(r.seq));
  const toggleAll = () => setOpen(allOpen ? new Set() : new Set(visible.map(({ r }) => r.seq)));

  if (!rounds.length) return <div className="empty-state">暂无决策对话</div>;

  const firstTs = rounds[0]?.ts ?? null;
  const lastTs = rounds[rounds.length - 1]?.ts ?? null;
  const tradeRounds = tradeCounts.filter((c) => c > 0).length;

  return (
    <div className="mc-wrap">
      <div className="mc-toolbar">
        <span className="mc-toolbar-stat">
          本页 <b>{rounds.length}</b> 轮 · 有成交 <b>{tradeRounds}</b> 轮 · 复盘{' '}
          <b>{rounds.filter((r) => r.kind === 'review').length}</b> 轮
          {liveFills.length > 0 && (
            <>
              {' '}· 实盘成交 <b>{liveFills.length}</b> 笔
              {liveFillsByRound.orphan.length > 0 && (
                <span className="mc-toolbar-note">（{liveFillsByRound.orphan.length} 笔不在本页时间窗内）</span>
              )}
            </>
          )}
        </span>
        <span className="mc-toolbar-range">
          数据范围 {fmtDateTime(firstTs)} → {fmtDateTime(lastTs)}
        </span>
        <span className="mc-toolbar-btns">
          <div className="mc-filters">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                className={`mc-filter ${filter === f.id ? 'active' : ''}`}
                onClick={() => setFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>
          <button className="mc-filter" onClick={toggleAll}>
            {allOpen ? '全部收起' : '全部展开'}
          </button>
        </span>
      </div>

      <div className="mc-list">
        {visible.length === 0 && <div className="empty-state">当前筛选下没有回合</div>}
        {visible.map(({ r, i }) => {
          const dayTrades = r.date ? (tradesByDate.get(r.date) ?? []) : [];
          const live = liveFillsByRound.bySeq.get(r.seq) ?? [];
          const fillCount = dayTrades.length + live.length;
          const pa = parsed[i];
          const isOpen = open.has(r.seq);
          const toolLine = extractToolLine(r.thought);
          const status =
            r.kind === 'review' ? '盘后复盘' : fillCount ? `成交 ${fillCount} 笔` : r.thought ? '未成交' : '仅提示';
          const summary =
            (pa.summary || r.thought || r.user).replace(/\s+/g, ' ').trim() || '（无内容）';
          return (
            <div className={`mc-card ${isOpen ? 'open' : ''}`} key={r.seq} style={{ borderColor: modelColor(model) }}>
              <div
                className="mc-head"
                onClick={() => toggle(r.seq)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    toggle(r.seq);
                  }
                }}
              >
                <span className="mc-seq">#{String(r.seq).padStart(3, '0')}</span>
                <span className="mc-logo">{logoOf(model)}</span>
                <span className="mc-model" style={{ color: modelColor(model) }}>
                  {model.replace('deepseek-v4-', '').toUpperCase()}
                </span>
                {modeOf(r.user)}
                {r.kind === 'review' && <span className="mc-kind review">📋 复盘轮</span>}
                <span className={`mc-status ${dayTrades.length ? '' : r.kind === 'review' ? 'review' : 'idle'}`}>{status}</span>
                <span className="mc-date" title={r.ts ?? ''}>
                  {fmtDateTime(r.ts)}
                  {r.ts && <em className="mc-date-wd">{fmtWeekday(r.ts)}</em>}
                  {r.ts && <em className="mc-date-ago">{fmtAgo(r.ts)}</em>}
                </span>
                <span className={`mc-expand ${isOpen ? 'open' : ''}`}>{isOpen ? '▼' : '▶'}</span>
              </div>

              {r.date && (
                <div className="mc-subline">
                  <span className="mc-subline-k">交易日</span>
                  <b>{r.date}</b>
                  <span className="mc-subline-k">记录时刻</span>
                  <b>{fmtClock(r.ts)}</b>
                  {r.usage?.total_tokens != null && (
                    <>
                      <span className="mc-subline-k">tokens</span>
                      <b>{r.usage.total_tokens.toLocaleString('en-US')}</b>
                      <span className="mc-subline-note">
                        in {fmtNum(r.usage.prompt_tokens, 0)} / out {fmtNum(r.usage.completion_tokens, 0)}
                        {r.usage.usage_est ? '（估算）' : ''}
                      </span>
                    </>
                  )}
                  {toolLine && (
                    <>
                      <span className="mc-subline-k">工具</span>
                      <b className="mc-subline-tool">{toolLine}</b>
                    </>
                  )}
                </div>
              )}

              <div className="mc-summary">
                <span className="mc-sum-label">摘要</span>
                <span className="mc-sum-text">{renderInline(summary)}</span>
              </div>

              {isOpen && (
                <div className="mc-body">
                  <MetaSection round={r} dayTrades={dayTrades} live={live} currency={currency} />
                  {r.user && (
                    <Section title="用户提示词" hint={`${r.user.length.toLocaleString('en-US')} 字`}>
                      <div className="mc-code">{renderMarkdown(r.user)}</div>
                    </Section>
                  )}
                  {pa.chain && (
                    <Section title="分析链路（工具调用）">
                      <div className="mc-code">{renderMarkdown(pa.chain)}</div>
                    </Section>
                  )}
                  {pa.reasoning && pa.reasoning !== r.thought && (
                    <Section title="推理论证" hint={`${pa.reasoning.length.toLocaleString('en-US')} 字`}>
                      <div className="mc-code mc-thought">{renderMarkdown(pa.reasoning)}</div>
                    </Section>
                  )}
                  <Section
                    title="结构化决策"
                    hint={pa.decisions.length ? `${pa.decisions.length} 条` : '本轮未输出 JSON 决策'}
                  >
                    {pa.decisions.length ? (
                      <div className="mc-dec-list">
                        {pa.decisions.map((d, k) => (
                          <StructuredDecision key={`${d.code ?? d.name ?? k}-${k}`} dec={d} fills={dayTrades} names={names} />
                        ))}
                      </div>
                    ) : (
                      <div className="mc-no-trade">本轮未输出结构化决策 JSON。</div>
                    )}
                  </Section>
                  <Section
                    title="实盘成交（通达信桥 · 秒级）"
                    hint={live.length ? `${live.length} 笔` : '本轮时间窗内无实盘回报'}
                  >
                    {live.length ? (
                      <div className="mc-live-list">
                        {live.map((f, j) => (
                          <div className={`mc-live-fill ${f.side}`} key={`${f.ts}-${f.code}-${j}`}>
                            <span className="mc-live-ts">{fmtDateTime(f.ts)}</span>
                            <span className={`mc-side ${f.side}`}>{f.side === 'buy' ? '▲ 买入' : '▼ 卖出'}</span>
                            <b className="mc-live-sym">{stockName(names, f.code) ?? f.code}</b>
                            <span className="mc-live-code">{f.code}</span>
                            <span className="mc-live-qty">{f.volume.toLocaleString('en-US')} 股</span>
                            <span className="mc-live-price">@{f.price != null ? fmtNum(f.price) : '—'}</span>
                            <span className="mc-live-amt">
                              {f.price != null ? fmtMoney(f.volume * f.price, currency, 0) : '—'}
                            </span>
                            {f.orderId && <span className="mc-live-oid">订单 {f.orderId}</span>}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="mc-no-trade">本轮时间窗（±90 分钟）内没有通达信桥成交回报。</div>
                    )}
                  </Section>
                  <Section
                    title="模拟盘成交明细（按日）"
                    hint={dayTrades.length ? `${dayTrades.length} 笔` : '无成交'}
                  >
                    {dayTrades.length ? (
                      <div className="mc-trades">
                        {dayTrades.map((t, j) => (
                          <DecisionCard
                            key={`${t.date}-${t.symbol}-${j}`}
                            trade={t}
                            qtyAfter={qtyAt(t.date, t.symbol)}
                            currency={currency}
                            names={names}
                          />
                        ))}
                      </div>
                    ) : (
                      <div className="mc-no-trade">
                        {r.date ? `${r.date} 未产生成交回报。` : '未产生成交回报。'}
                      </div>
                    )}
                  </Section>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 折叠小节（默认展开，点击标题折叠） */
function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  const [folded, setFolded] = useState(false);
  return (
    <div className={`mc-section ${folded ? 'folded' : ''}`}>
      <div className="mc-section-head" onClick={() => setFolded(asUpdater((v) => !v))}>
        <span className="mc-caret">{folded ? '▶' : '▼'}</span>
        {title}
        {hint && <span className="mc-section-hint">{hint}</span>}
      </div>
      {!folded && children}
    </div>
  );
}

/** 轮次元数据：完整时间戳（含时区）到秒 + 来源字段，供复盘对齐日志原文。 */
function MetaSection({
  round,
  dayTrades,
  live,
  currency,
}: {
  round: Round;
  dayTrades: TradeRecord[];
  live: LiveFill[];
  currency: string;
}) {
  const simTurnover = dayTrades.reduce((sum, t) => sum + Math.abs(t.notional ?? 0), 0);
  const liveTurnover = live.reduce((sum, f) => sum + f.volume * (f.price ?? 0), 0);
  const fills = dayTrades.length + live.length;
  return (
    <div className="mc-meta">
      <MetaCell k="日志时间戳" v={round.ts ?? '—'} wide />
      <MetaCell k="本地时刻" v={`${fmtDateTime(round.ts)} ${fmtWeekday(round.ts)}`} />
      <MetaCell k="距今" v={fmtAgo(round.ts) || '—'} />
      <MetaCell k="轮次类型" v={round.kind === 'review' ? '盘后复盘（只读）' : '盘中交易轮'} />
      <MetaCell k="本轮成交" v={fills ? `${fills} 笔` : '0 笔'} />
      <MetaCell
        k="成交金额"
        v={fills ? `${fmtMoney(liveTurnover, currency, 0)}${dayTrades.length ? ` + ${fmtMoney(simTurnover, currency, 0)}（模拟）` : ''}` : '—'}
      />
    </div>
  );
}

function MetaCell({ k, v, wide }: { k: string; v: string; wide?: boolean }) {
  return (
    <div className={`mc-meta-cell ${wide ? 'wide' : ''}`}>
      <span className="mc-meta-k">{k}</span>
      <span className="mc-meta-v">{v}</span>
    </div>
  );
}

/** 结构化决策卡：模型自己输出的 JSON 决策（action/code/pct/reason/止损止盈…），
 *  并标注该决策是否对应当日真实成交 —— 复盘看「说的和做的是否一致」。 */
function StructuredDecision({ dec, fills, names }: { dec: DecItem; fills: TradeRecord[]; names: Record<string, string> }) {
  const side = (dec.action ?? '').toLowerCase();
  const sideCls = side === 'sell' ? 'sell' : side === 'buy' ? 'buy' : 'hold';
  const matched = fills.filter((f) => dec.code && sameSymbol(f.symbol, dec.code));
  const label = side === 'buy' ? '买入' : side === 'sell' ? '卖出' : side === 'hold' ? '持有' : side === 'watch' ? '观察' : side || '—';
  return (
    <div className={`mc-dec ${sideCls}`}>
      <div className="mc-dec-row">
        <span className={`mc-dec-side ${sideCls}`}>{label}</span>
        <b className="mc-dec-code">{dec.name || stockName(names, dec.code) || dec.code || '—'}</b>
        {dec.code && <span className="mc-dec-sym">{dec.code}</span>}
        {dec.pct != null && <span className="mc-dec-pct">目标仓位 {(dec.pct * 100).toFixed(0)}%</span>}
        {dec.confidence != null && <span className="mc-dec-pct">置信度 {(dec.confidence * 100).toFixed(0)}%</span>}
        {matched.length ? (
          <span className="mc-dec-exec done">已执行 {matched.length} 笔</span>
        ) : (
          <span className="mc-dec-exec none">未成交</span>
        )}
      </div>
      {dec.reason && <div className="mc-dec-reason">{renderInline(dec.reason)}</div>}
      {(dec.stop_loss != null || dec.take_profit != null || dec.move_stop != null || dec.invalidation) && (
        <div className="mc-dec-exit">
          {dec.stop_loss != null && <span>止损 {dec.stop_loss}</span>}
          {dec.take_profit != null && <span>止盈 {dec.take_profit}</span>}
          {dec.move_stop != null && <span>移动止损 {dec.move_stop}</span>}
          {dec.invalidation && <span>失效条件 {dec.invalidation}</span>}
        </div>
      )}
    </div>
  );
}

/** 成交决策卡：真实成交回报 + 成交后持仓，字段来自 /trades 与持仓快照。 */
function DecisionCard({
  trade,
  qtyAfter,
  currency,
  names,
}: {
  trade: TradeRecord;
  qtyAfter: number | null;
  currency: string;
  names: Record<string, string>;
}) {
  const side = (trade.action ?? '').toLowerCase() === 'buy' ? 'buy' : 'sell';
  const name = stockName(names, trade.symbol);
  const qty = Number(trade.amount) || 0;
  const before = qtyAfter != null ? qtyAfter - (side === 'buy' ? qty : -qty) : null;
  return (
    <div className={`mc-decision ${side}`}>
      <div className="mc-decision-head">
        <span className={`mc-side ${side}`}>{side === 'buy' ? '▲ 买入 BUY' : '▼ 卖出 SELL'}</span>
        <b className="mc-decision-sym">{name ?? trade.symbol}</b>
        {name && <span className="mc-decision-code">{trade.symbol}</span>}
        <span className="mc-decision-date">{trade.date}</span>
      </div>
      <div className="mc-decision-body">
        <MetaCell k="数量" v={qty.toLocaleString('en-US')} />
        <MetaCell k="成交价" v={trade.price != null ? fmtNum(trade.price) : '—'} />
        <MetaCell k="成交金额" v={trade.notional != null ? fmtMoney(Math.abs(trade.notional), currency, 0) : '—'} />
        <MetaCell k="成交后现金" v={fmtMoney(trade.cash_after ?? 0, currency, 0)} />
        <MetaCell k="持仓变化" v={before != null && qtyAfter != null ? `${before.toLocaleString('en-US')} → ${qtyAfter.toLocaleString('en-US')}` : qtyAfter != null ? `→ ${qtyAfter.toLocaleString('en-US')}` : '—'} />
      </div>
    </div>
  );
}
