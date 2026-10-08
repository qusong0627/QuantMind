/** 右栏「对话」：围绕当前策略问答（ask）或让 agent 改策略（edit）。
 *
 * edit 模式下模型只产出**候选 Pine**，宿主 worker 会用同一条转写链路在断网沙箱里回测，
 * 把「改前 / 改后」指标并列给用户看——不点「采用」绝不写回正式源码。
 */
import { useEffect, useRef, useState } from 'react';
import { STAGE_LABEL, compareCellText, compareDeltaClass, compareDeltaText, fmtWhen } from './format';
import type { Workbench } from './useWorkbench';

/** 空白对话时的快捷提问——把「不知道怎么问」的空白填掉 */
const QUICK_ASK = [
  '这个策略的核心逻辑是什么？',
  '适合什么行情？最大的风险在哪？',
  '哪个参数最敏感？过拟合风险如何？',
];

const QUICK_EDIT = [
  '把止损改成固定 3%',
  '加一个 ADX > 20 的趋势过滤',
  '把参数调保守一些，并说明理由',
];

function CandidateCard({ wb }: { wb: Workbench }) {
  const cand = wb.chatJob?.candidate;
  if (!cand) return null;
  const keys = Object.keys(cand.compare.after);
  return (
    <div className="lab-cand">
      <div className="lab-cand-head">
        <b>候选回测对比</b>
        <span>
          {cand.compare.symbol} · {cand.compare.adj} · 成交 {cand.trades ?? '—'} 笔
          {cand.compare.before_at ? ` · 改前基线 ${fmtWhen(cand.compare.before_at)}` : ''}
        </span>
      </div>
      {cand.compare.before_stale && (
        <div className="lab-cand-warn">
          「改前」取自最近一次正式回测，比当前源码还旧（源码改过或闸门收紧后没重跑）——
          两列不是同一份源码，先重跑一次再对比才准。
        </div>
      )}
      <table className="lab-cand-table">
        <thead>
          <tr>
            <th>指标</th>
            <th>改前</th>
            <th>改后</th>
            <th>变化</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => {
            const b = cand.compare.before[k];
            const a = cand.compare.after[k];
            return (
              <tr key={k}>
                <td>{a?.label ?? b?.label ?? k}</td>
                <td>{compareCellText(b)}</td>
                <td>{compareCellText(a)}</td>
                <td className={compareDeltaClass(b, a)}>{compareDeltaText(b, a)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <details className="lab-cand-src">
        <summary>看候选源码</summary>
        <pre>{cand.pine}</pre>
      </details>
      <button className="lab-run" disabled={wb.applyBusy} onClick={wb.applyCandidate}>
        {wb.applyBusy ? '采用中…' : '采用这版（写入 edited/ 并重跑）'}
      </button>
    </div>
  );
}

export default function AgentChatPanel({ wb }: { wb: Workbench }) {
  const [text, setText] = useState('');
  const logRef = useRef<HTMLDivElement>(null);
  const msgs = wb.chatPending ? [...wb.chat, wb.chatPending] : wb.chat;
  const editing = wb.chatMode === 'edit';
  const quick = editing ? QUICK_EDIT : QUICK_ASK;

  // 新消息/阶段变化时滚到底部（答案比较长，不滚就看不见）
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs.length, wb.chatStage]);

  const send = (value: string) => {
    const t = value.trim();
    if (!t || wb.chatBusy) return;
    wb.sendChat(t);
    setText('');
  };

  return (
    <div className="lab-chat">
      <div className="lab-chat-modes">
        <button
          className={`lab-more${editing ? '' : ' on'}`}
          disabled={wb.chatBusy}
          onClick={() => wb.setChatMode('ask')}
        >
          问策略
        </button>
        <button
          className={`lab-more${editing ? ' on' : ''}`}
          disabled={wb.chatBusy}
          onClick={() => wb.setChatMode('edit')}
        >
          改策略
        </button>
        <span className="lab-chat-hint">
          {editing ? '产出候选 → 沙箱回测 → 你点采用才写回源码' : ''}
        </span>
      </div>

      <div className="lab-chat-log" ref={logRef}>
        {!msgs.length && (
          <div className="lab-empty">
            {editing
              ? '说清要改什么（例：止损改成固定 3%）。agent 会给出改后源码，并在断网沙箱里回测出前后对比。'
              : '问策略逻辑、适用行情、参数敏感度——agent 会读到当前 Pine 源码、你的备注与最近一次回测。'}
          </div>
        )}
        {msgs.map((m, i) => (
          <div
            key={`${m.job_id ?? 'local'}-${m.role}-${i}`}
            className={`lab-msg ${m.role}${m.pending ? ' pending' : ''}`}
          >
            <div className="lab-msg-role">{m.role === 'user' ? '我' : 'Agent'}</div>
            <div className="lab-msg-body">{m.content}</div>
            {m.role === 'assistant' && !m.pending && m.content.trim() && (
              <button
                className="lab-msg-act"
                disabled={wb.noteBusy}
                onClick={() => wb.appendNote(m.content)}
              >
                写入备注
              </button>
            )}
          </div>
        ))}
        {wb.chatStage && (
          <div className="lab-msg assistant">
            <div className="lab-msg-role">Agent</div>
            <div className="lab-msg-body lab-typing">
              {STAGE_LABEL[wb.chatStage] ?? wb.chatStage}…
            </div>
          </div>
        )}
      </div>

      <CandidateCard wb={wb} />

      {wb.applyMsg && <div className="lab-ok">{wb.applyMsg}</div>}
      {wb.applyErr && <div className="lab-err">{wb.applyErr}</div>}
      {wb.chatErr && <div className="lab-err">{wb.chatErr}</div>}

      {!msgs.length && (
        <div className="lab-chat-quick">
          {quick.map((q) => (
            <button key={q} className="lab-more" disabled={wb.chatBusy} onClick={() => send(q)}>
              {q}
            </button>
          ))}
        </div>
      )}

      <div className="lab-chat-form">
        <textarea
          className="lab-chat-input"
          rows={2}
          value={text}
          disabled={wb.chatBusy}
          placeholder={
            wb.chatBusy
              ? '等 agent 跑完再发下一条…'
              : editing
                ? '要改什么？（Enter 发送，Shift+Enter 换行）'
                : '问点什么（Enter 发送，Shift+Enter 换行）'
          }
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send(text);
            }
          }}
        />
        <button
          className="lab-run lab-chat-send"
          disabled={wb.chatBusy || !text.trim()}
          onClick={() => send(text)}
        >
          {wb.chatBusy ? (editing ? '改+回测中…' : '思考中…') : '发送'}
        </button>
      </div>

      {wb.versions.length > 0 && (
        <div className="lab-versions">
          <span>可回滚版本 {wb.versions.length} 个</span>
          <button className="lab-more" disabled={wb.applyBusy} onClick={wb.revert}>
            回滚到上一版
          </button>
        </div>
      )}

      <p className="lab-note">
        模型与沙箱都跑在宿主上（容器不执行模型代码，也没有 bwrap）。改策略产出的候选只活在
        chat/ 里，点「采用」才会写入 edited/，每次采用/回滚都留版本快照。
      </p>
    </div>
  );
}
