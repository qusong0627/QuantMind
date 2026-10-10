import { useMemo, useState } from 'react';
import { LogLine } from '../api/client';
import { logoOf, modelColor, shortName } from './ModelCard';
import { renderInline } from '../utils/markdown';
import './ModelChat.css';
import { modeOf } from '../utils/modeTag';
import { FillLike, renderActionTags } from '../utils/actionTags';
import { parseAnalysis } from '../utils/parseAnalysis';
import { ProtoSpec, isGateAgent, protoSpecOf, protoSummary } from '../utils/newsProtocol';
import NewsProtocolView from './NewsProtocolView';
import {
  COMPLIANCE_AI_GENERATED_TEXT,
  ComplianceStrip,
} from '../../../../../components/shared/compliance/ComplianceChrome';
import { asUpdater } from '../reactCompat';

/** 一个分析回合：单条日志（一次 LLM 分析 = user prompt + assistant 总结） */
interface MixedRound {
  kind?: 'review';
  model: string;
  /** 存储层 agent id（新闻段落判定用；展示名会被中文化） */
  modelId?: string;
  /** 日志写入时间（实盘分析时间），用于全局倒序 */
  ts: string | null;
  user: string;
  thought: string;
  /** 本轮数据缺口（分析端写入日志条目；'account_unreachable' = 桥挂无账户数据） */
  dataGaps?: string[];
}

/** 模型对话的合规尾注：AI 生成提示 + 统一免责（文案走 compliance 单一来源，勿抄字面量）。 */
function McDisclaimer() {
  return (
    <div className="mc-disclaimer border-t-2 border-black pt-2">
      <div className="mc-ai-note font-mono text-[10px] leading-4 text-slate-500">
        {COMPLIANCE_AI_GENERATED_TEXT}
      </div>
      <ComplianceStrip className="mt-0.5" />
    </div>
  );
}

/** 模型对话 — 全部模型混合流：
 *  不按模型分组，所有模型的最新分析按时间全局倒序混排（每个模型的最新都排前面），
 *  卡片边框按模型配色区分。 */
export default function ChatStream({
  agents,
  fills,
  heldCodes,
}: {
  agents: { name: string; id?: string; lines: LogLine[] }[];
  /** 实盘成交事实（时间窗匹配 → 动作标签以成交为准，不靠文字猜） */
  fills?: FillLike[];
  /** 当前仍持有的代码集合（卖后仍持=减仓，卖光=清仓；买后已持=加仓） */
  heldCodes?: Set<string>;
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [sections, setSections] = useState<Record<number, Set<string>>>({});
  const [exp, setExp] = useState<Record<string, boolean>>({});
  const expKey = (i: number, k: string) => `${i}-${k}`;

  /** 跨行合并成回合：user 开新回合，assistant 并入最近回合。
   *  A 股日志单行含 user+assistant；港股把 user/assistant 拆到不同日志行（各 1 条），
   *  必须跨行合并才能还原完整回合，否则 assistant-only 行「没有用户提示词」。
   *  港股日志缺 timestamp 字段，回退从 user prompt 文本里抽日期（today's (2026-08-28)）。 */
  const rounds: MixedRound[] = useMemo(() => {
    const extractDate = (s: string): string | null => {
      const m = s.match(/[（(](\d{4}-\d{2}-\d{2})[）)]/);
      return m ? m[1] : null;
    };
    const out: MixedRound[] = [];
    for (const ag of agents) {
      let cur: MixedRound | null = null;
      const flush = () => {
        if (cur && (cur.user || cur.thought)) out.push(cur);
        cur = null;
      };
      for (const line of ag.lines) {
        const lineTs = line.timestamp ?? null;
        if ((line as { kind?: string }).kind === 'review') {
          const text = (line.new_messages ?? [])
            .filter((m) => String(m.role) === 'assistant' || String(m.role) === 'ai')
            .map((m) => String(m.content ?? '')).join('\n\n');
          if (text.trim()) out.push({ kind: 'review', model: ag.name, modelId: ag.id, ts: lineTs, user: '', thought: text });
          continue;
        }
        for (const msg of line.new_messages ?? []) {
          const content = (msg.content ?? '').trim();
          if (!content) continue;
          const role = msg.role ?? 'system';
          if (role === 'user' || role === 'human') {
            flush();
            cur = { model: ag.name, modelId: ag.id, ts: lineTs ?? extractDate(content), user: content, thought: '' };
          } else if (role === 'assistant' || role === 'ai') {
            if (!cur) cur = { model: ag.name, modelId: ag.id, ts: lineTs, user: '', thought: '' };
            cur.thought += (cur.thought ? '\n\n' : '') + content;
            if (!cur.ts) cur.ts = lineTs;
          }
          if (line.data_gaps?.length && cur) {
            cur.dataGaps = [...new Set([...(cur.dataGaps ?? []), ...line.data_gaps])];
          }
        }
      }
      flush();
    }
    // 最新分析在前（跨模型全局排序）
    return out.sort((a, b) => {
      if (a.ts && b.ts) return a.ts < b.ts ? 1 : -1;
      if (a.ts) return -1;
      if (b.ts) return 1;
      return 0;
    });
  }, [agents]);

  const scrollTo = (sel: string) =>
    requestAnimationFrame(() => {
      document.querySelector(sel)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });

  const toggle = (idx: number) => {
    const willOpen = !open.has(idx);
    setOpen(asUpdater((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    }));
    if (willOpen) scrollTo('[data-card="' + idx + '"] .mc-summary');
  };

  const toggleSection = (idx: number, key: string) => {
    const willOpen = !(sections[idx] ?? new Set<string>()).has(key);
    setSections(asUpdater((prev) => {
      const cur = new Set(prev[idx] ?? []);
      if (cur.has(key)) cur.delete(key);
      else cur.add(key);
      return { ...prev, [idx]: cur };
    }));
    if (willOpen) scrollTo('[data-card="' + idx + '"] [data-sec="' + key + '"]');
  };

  /** 四段式解析（总结/链路/决策/推理），与 rounds 对齐 */
  const parsed = useMemo(() => rounds.map((r) => parseAnalysis(r.thought)), [rounds]);

  if (!rounds.length)
    return (
      <div className="mc-list">
        <div className="empty-state">暂无分析记录</div>
        <McDisclaimer />
      </div>
    );

  return (
    <div className="mc-list">
      {rounds.map((r, i) => {
        const isReview = r.kind === 'review';
        const isOpen = isReview ? !open.has(i) : open.has(i);
        const sec = sections[i] ?? new Set<string>();
        const pa = parsed[i];
        // 新闻 agent：输出是行式填表协议，走结构化渲染（不做交易动作标签 —— 新闻不下单，
        // 「维持买入评级」这类正文会被关键词兜底误判成「买入」）
        const proto: ProtoSpec | null = protoSpecOf(r.modelId);
        const isNews = !!proto || isGateAgent(r.modelId);
        const newsSum = isNews ? protoSummary(r.thought, r.modelId) : '';
        return (
          <div
            className={`mc-card ${isOpen ? 'open' : ''}`} data-card={i}
            key={`${r.model}-${r.ts ?? i}`}
            style={{ borderColor: modelColor(r.model) }}
          >
            <div
              className="mc-head"
              onClick={() => toggle(i)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  toggle(i);
                }
              }}
            >
              <span className="mc-logo">{logoOf(r.model)}</span>
              <span className="mc-model" style={{ color: modelColor(r.model) }}>
                {shortName(r.model)}
              </span>
              {isNews ? <span className="mc-mode-chip news">新闻</span> : modeOf(r.user)}
              {!isNews &&
                renderActionTags(r.thought, {
                  fills,
                  model: r.model,
                  tsMs: r.ts ? new Date(r.ts).getTime() : null,
                  heldCodes,
                })}
              <span className="mc-status" style={isReview ? { background: '#0d8a6b' } : undefined}>
                  {isReview ? '📋 复盘' : r.thought ? '已分析' : '仅提示'}
                </span>
              <span className="mc-date">{r.ts ? r.ts.slice(5, 16) : '—'}</span>
              <span className={`mc-expand ${isOpen ? 'open' : ''}`}>{isOpen ? '▼' : '▶'}</span>
            </div>
            {r.dataGaps?.includes('account_unreachable') && (
              <div
                className="mc-gap-banner border-b-2 border-black bg-amber-100 px-3.5 py-1.5 font-mono text-[11px] leading-4 text-amber-900"
                role="alert"
              >
                ⚠️ 本轮无实盘账户数据（桥不可达）：分析仅基于新闻面，未点评持仓
              </div>
            )}
            <div className="mc-summary"><span className="mc-sum-label">总结</span><span className="mc-sum-text">
                  {isReview
                    ? '盘后复盘：展开查看逐笔归因 / 行为审计 / 明日预案'
                    : isNews
                      ? newsSum || renderInline((r.thought || r.user).replace(/\s+/g, ' ').trim())
                      : renderInline(pa.summary || (r.thought || r.user).replace(/\s+/g, ' ').trim())}
                </span></div>
            {isOpen && (
              <div className="mc-body">
                {r.user && (
                  <div className={`mc-section ${sec.has('prompt') ? 'folded' : ''}`} data-sec="prompt">
                    <div className="mc-section-head" onClick={() => toggleSection(i, 'prompt')}>
                      <span className="mc-caret">{sec.has('prompt') ? '▶' : '▼'}</span>
                      用户提示词
                    </div>
                    {!sec.has('prompt') && (() => {
        const txt = r.user;
        const long = txt.length > 480;
        const open = !!exp[expKey(i, 'prompt')];
        return (
          <>
            <pre className={`mc-code  ${long && !open ? 'mc-clamp' : ''}`}>{renderInline(txt)}</pre>
            {long && (
              <button className="mc-more"
                onClick={() => setExp(asUpdater((e) => ({ ...e, [expKey(i, 'prompt')]: !open })))}>
                {open ? '收起' : '展开全文'}
              </button>
            )}
          </>
        );
      })()}
                  </div>
                )}
                {isNews && (
                  <div className="mc-section" data-sec="proto">
                    <div className="mc-section-head static">
                      <span className="mc-caret">▼</span>
                      结构化输出（{r.modelId === 'news-gate' ? '剔除清单' : '按段字段表渲染'}）
                    </div>
                    <NewsProtocolView
                      text={r.thought}
                      agentId={r.modelId ?? ''}
                      spec={proto ?? { agent: '', rows: [] }}
                    />
                  </div>
                )}
                {!isNews && pa.chain && (
                  <div className={`mc-section ${sec.has('chain') ? 'folded' : ''}`} data-sec="chain">
                    <div className="mc-section-head" onClick={() => toggleSection(i, 'chain')}>
                      <span className="mc-caret">{sec.has('chain') ? '▶' : '▼'}</span>
                      分析链路（工具调用）
                    </div>
                    {!sec.has('chain') && (() => {
        const txt = pa.chain;
        const long = txt.length > 480;
        const open = !!exp[expKey(i, 'chain')];
        return (
          <>
            <pre className={`mc-code  ${long && !open ? 'mc-clamp' : ''}`}>{renderInline(txt)}</pre>
            {long && (
              <button className="mc-more"
                onClick={() => setExp(asUpdater((e) => ({ ...e, [expKey(i, 'chain')]: !open })))}>
                {open ? '收起' : '展开全文'}
              </button>
            )}
          </>
        );
      })()}
                  </div>
                )}
                {!isNews && pa.decisions.length > 0 && (
                  <div className={`mc-section ${sec.has('decisions') ? 'folded' : ''}`} data-sec="decisions">
                    <div className="mc-section-head" onClick={() => toggleSection(i, 'decisions')}>
                      <span className="mc-caret">{sec.has('decisions') ? '▶' : '▼'}</span>
                      交易决策{pa.decisions.length ? ` (${pa.decisions.length})` : ''}
                    </div>
                    {!sec.has('decisions') && (
                      <div className="mc-trades">
                        {pa.decisions.slice(0, 6).map((d, k) => {
                          const act = String(d.action || '').toLowerCase();
                          const cls = act === 'buy' ? 'buy' : act === 'sell' ? 'sell' : 'hold';
                          const tag =
                            act === 'buy' ? '买入' : act === 'sell' ? '卖出' : act === 'watch' ? '观察' : '持有';
                          const exitBits = [
                            d.stop_loss != null ? `止损 ${d.stop_loss}` : '',
                            d.take_profit != null ? `止盈 ${d.take_profit}` : '',
                            d.move_stop != null ? `移动止损 ${d.move_stop}` : '',
                            d.confidence ? `置信 ${Math.round(d.confidence * 100)}%` : '',
                            d.risk_amount != null ? `风险 ¥${d.risk_amount.toLocaleString('en-US')}` : '',
                          ].filter(Boolean);
                          return (
                            <div className="mc-decision-mini" key={`${d.code}-${k}`}>
                              <div className="mc-decision-mini-row">
                                <span className={`mc-decision-mini-side ${cls}`}>{tag}</span>
                                <b className="mc-decision-mini-code">{d.name || d.code || '—'}</b>
                                {d.pct != null && (
                                  <span className="mc-decision-mini-pct">{Math.round(d.pct * 100)}%</span>
                                )}
                                <span className="mc-decision-mini-reason">{d.reason ?? ''}</span>
                              </div>
                              {(exitBits.length > 0 || d.invalidation) && (
                                <div className="mc-decision-mini-exit">
                                  {exitBits.join(' · ')}
                                  {d.invalidation ? ` · 失效: ${d.invalidation}` : ''}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}
                {!isNews && pa.reasoning && (
                  <div className={`mc-section ${sec.has('reason') ? 'folded' : ''}`} data-sec="reason">
                    <div className="mc-section-head" onClick={() => toggleSection(i, 'reason')}>
                      <span className="mc-caret">{sec.has('reason') ? '▶' : '▼'}</span>
                      推理论证
                    </div>
                    {!sec.has('reason') && (() => {
        const txt = pa.reasoning;
        const long = txt.length > 480;
        const open = !!exp[expKey(i, 'reason')];
        return (
          <>
            <pre className={`mc-code mc-thought ${long && !open ? 'mc-clamp' : ''}`}>{renderInline(txt)}</pre>
            {long && (
              <button className="mc-more"
                onClick={() => setExp(asUpdater((e) => ({ ...e, [expKey(i, 'reason')]: !open })))}>
                {open ? '收起' : '展开全文'}
              </button>
            )}
          </>
        );
      })()}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
      <McDisclaimer />
    </div>
  );
}
