/** 右栏：当前策略的详情、运行入口、备注/对话，以及可折叠的 Pine 源码编辑器。 */
import { useState } from 'react';
import AgentChatPanel from './AgentChatPanel';
import NotePanel from './NotePanel';
import { FREQ_NAME, MARKET_NAME } from './markets';
import { fmt, fmtPct, KIND_LABEL, STAGE_LABEL, sanitizeStrategyTitle } from './format';
import type { Workbench } from './useWorkbench';
import { asUpdater } from './reactCompat';

const ADJ_LABEL: Record<string, string> = {
  unadjusted: '不复权',
  forward: '前复权',
  backward: '后复权',
};

function SubTabs({ wb }: { wb: Workbench }) {
  const [sub, setSub] = useState<'note' | 'chat'>('note');
  const isPine = wb.sel?.kind === 'pine';
  return (
    <section className="lab-panel-block">
      <div className="lab-subtabs">
        <button className={sub === 'note' ? 'on' : ''} onClick={() => setSub('note')}>
          备注
        </button>
        <button className={sub === 'chat' ? 'on' : ''} onClick={() => setSub('chat')}>
          对话
        </button>
      </div>
      {sub === 'note' ? (
        <NotePanel wb={wb} />
      ) : isPine ? (
        <AgentChatPanel wb={wb} />
      ) : (
        <p className="lab-note">
          对话只针对策略库里的条目——内置模板不在库里，先在左栏选一条 Pine 策略。
        </p>
      )}
    </section>
  );
}

export default function StrategyPanel({ wb }: { wb: Workbench }) {
  const tpl =
    wb.sel?.kind === 'template'
      ? wb.strategies.find((s) => s.id === wb.sel?.id)
      : undefined;
  const st = wb.result?.stats;
  const tp = wb.cur ? wb.transpile[wb.cur.id] : undefined;

  return (
    <aside className="lab-panel">
      {!wb.sel && (
        <div className="lab-panel-block">
          <div className="lab-block-title">策略</div>
          <div className="lab-empty">左侧选一个策略</div>
        </div>
      )}

      {tpl && (
        <div className="lab-panel-block">
          <div className="lab-block-title">内置模板</div>
          <div className="lab-lib-title">
            <strong>{tpl.name}</strong>
            <span className="code">{tpl.id}</span>
          </div>
          <p className="lab-desc">{tpl.desc}</p>
          <div className="lab-params">
            {tpl.params.map((p) => (
              <label key={p.k}>
                <span>{p.label}</span>
                <input
                  type="number"
                  step="any"
                  value={wb.params[p.k] ?? p.default}
                  onChange={(e) =>
                    wb.setParams(asUpdater((prev) => ({ ...prev, [p.k]: Number(e.target.value) })))
                  }
                />
              </label>
            ))}
          </div>
          <button className="lab-run" disabled={wb.runBusy} onClick={wb.run}>
            {wb.runBusy ? '回测中…' : `运行回测（${ADJ_LABEL[wb.runAdj]} · ${FREQ_NAME[wb.freq]} · 全历史）`}
          </button>
          {wb.market !== 'cn' && (
            <p className="lab-note">
              {MARKET_NAME[wb.market]}回测使用该市场统一口径（
              {wb.market === 'hk' ? '0.15%/边（含印花税）' : '0.05%/边'} · 95% 仓位），
              收益仅作策略形态参考，不是该市场的精确撮合成本。
            </p>
          )}
        </div>
      )}

      {wb.sel?.kind === 'pine' && wb.cur && (
        <div className="lab-panel-block">
          <div className="lab-block-title">策略库</div>
          <div className="lab-lib-title">
            <strong>{sanitizeStrategyTitle(wb.cur.title || wb.cur.file)}</strong>
            <span className="code">{wb.cur.id}</span>
          </div>
          <div className="lab-lib-tags">
            <span className="tag">{wb.cur.category}</span>
            <span className="tag">{wb.cur.version ? `Pine v${wb.cur.version}` : '版本未知'}</span>
            <span className="tag">{wb.cur.lines} 行</span>
            <span className={`tag ${wb.cur.indent_ok ? '' : 'warn'}`}>
              {KIND_LABEL[wb.cur.source_kind] ?? wb.cur.source_kind}
            </span>
            {wb.cur.url && (
              <a className="tag link" href={wb.cur.url} target="_blank" rel="noreferrer">
                TradingView ↗
              </a>
            )}
          </div>

          {!wb.cur.indent_ok && (
            <p className="lab-note">
              旧版爬虫产物：块缩进被清洗规则吃掉了，TradingView 与任何 Pine 工具都编译不了，
              只能当参考阅读。
            </p>
          )}

          {wb.cur.source_kind === 'imported' &&
            wb.cur.import_status &&
            wb.cur.import_status !== 'ready' &&
            wb.cur.import_status !== 'ready_security' && (
              <p className="lab-note">
                该条未进入可运行集（{wb.cur.import_status}）：PyneCore 6.9.2 未实现相关
                request.*，或 request.security 的标的/周期无法静态解析。清单见
                data/pine_library/imported_deferred.json。
              </p>
            )}

          {wb.cur.source_kind === 'imported' && (
            <p className="lab-note">
              外部导入：与 TradingView 计算一致；成本/仓位按当前市场统一覆盖
              （A股/美股 0.05%/边、港股 0.15%/边）。仅本地研究，不参与评估/晋级。
              {wb.cur.has_security && wb.cur.security_timeframes?.length
                ? ` 多周期：${wb.cur.security_timeframes.join(' / ')}。`
                : ''}
            </p>
          )}

          <button className="lab-run" disabled={wb.runBusy || wb.running} onClick={wb.run}>
            {wb.running
              ? '跑着呢…'
              : wb.runBusy
                ? '入队中…'
                : wb.cur.source_kind === 'imported'
                  ? `回测（预编译 · ${ADJ_LABEL[wb.runAdj]} · ${FREQ_NAME[wb.freq]}）`
                  : `转写并回测（${ADJ_LABEL[wb.runAdj]} · ${FREQ_NAME[wb.freq]}）`}
          </button>
          {wb.market !== 'cn' && (
            <p className="lab-note">
              {MARKET_NAME[wb.market]}回测使用该市场统一口径（
              {wb.market === 'hk' ? '0.15%/边（含印花税）' : '0.05%/边'}），
              收益仅作策略形态参考。
            </p>
          )}
          {wb.cur.source_kind !== 'imported' && (
            <p className="lab-note">
              AI 转写使用「个人中心 → 其他设置 → AI 服务配置」的 Key / 模型 / 接口地址；
              未配置时回退服务器 .env。
            </p>
          )}

          {wb.running && (
            <div className="lab-transpile">
              <span className="tag">{STAGE_LABEL[wb.job?.stage ?? ''] ?? wb.job?.stage}</span>
              <span className="lab-transpile-stats">
                {wb.cur.source_kind === 'imported'
                  ? '宿主上直接沙箱回测（已预编译，不调模型）'
                  : '宿主上转写 + 沙箱回测，通常 10–30 秒（报错自动修，最多 2 轮）'}
              </span>
            </div>
          )}

          {wb.job?.status === 'failed' && (
            <div className="lab-err">
              {wb.job.error || '失败'}
              {wb.job.stage === 'static' &&
                (wb.job.problems ?? []).map((p) => <div key={p}>· {p}</div>)}
            </div>
          )}

          {wb.result && (
            <div className="lab-transpile">
              <span className="tag">
                {tp && tp.problems.length ? `静态未过（${tp.problems.length}）` : '静态通过'}
              </span>
              <span className="lab-transpile-stats">
                回测 {wb.result.symbol}（{wb.result.adj}）· {wb.result.trades.length} 笔
              </span>
            </div>
          )}

          <details className="lab-src">
            <summary>Pine 源码（{wb.draft.split('\n').length} 行）</summary>
            <textarea
              className="lab-lib-editor"
              spellCheck={false}
              value={wb.draft}
              onChange={(e) => wb.setDraft(e.target.value)}
            />
            <div className="lab-lib-actions">
              <button className="lab-run lab-lib-save" disabled={!wb.dirty || wb.saving} onClick={wb.save}>
                {wb.saving ? '保存中…' : wb.dirty ? '保存编辑' : '已保存'}
              </button>
              <button className="lab-more" disabled={!wb.cur.edited} onClick={wb.reset}>
                丢弃编辑
              </button>
            </div>
            {wb.saveMsg && <span className="lab-lib-msg">{wb.saveMsg}</span>}
            {wb.saveErr && <span className="lab-lib-msg err">{wb.saveErr}</span>}
          </details>

          {wb.cur.source_kind === 'custom' && (
            <div className="lab-lib-actions lab-lib-danger">
              <button
                className="lab-more"
                disabled={wb.deleteBusy}
                onClick={async () => {
                  if (!window.confirm(
                    `删除自定义策略「${wb.cur?.title ?? wb.cur?.id}」？源码与备注一并删除，不可恢复。`,
                  )) return;
                  await wb.deletePine();
                }}
              >
                {wb.deleteBusy ? '删除中…' : '删除这条自定义策略'}
              </button>
              {wb.deleteErr && <span className="lab-lib-msg err">{wb.deleteErr}</span>}
            </div>
          )}
        </div>
      )}

      {wb.sel && (
        <div className="lab-panel-block">
          <div className="lab-block-title">摘要</div>
          {st ? (
            <div className="lab-stats">
              <div
                className={`lab-stat ${
                  st['Net profit']?.pct == null
                    ? ''
                    : st['Net profit']!.pct >= 0 ? 'up' : 'down'
                }`}
              >
                <span className="k">策略收益</span>
                <span className="v">{fmtPct(st['Net profit']?.pct)}</span>
              </div>
              <div className="lab-stat">
                <span className="k">持有基准</span>
                <span className="v">{fmtPct(st['Buy & hold return']?.pct)}</span>
              </div>
              <div className="lab-stat down">
                <span className="k">最大回撤</span>
                <span className="v">
                  {st['Max equity drawdown']?.pct == null
                    ? '—'
                    : fmtPct(-st['Max equity drawdown'].pct)}
                </span>
              </div>
              <div className="lab-stat">
                <span className="k">夏普 / 成交</span>
                <span className="v">
                  {fmt(st['Sharpe ratio']?.value, 2)} / {fmt(st['Total trades']?.value, 0)}
                </span>
              </div>
            </div>
          ) : (
            <div className="lab-empty">还没跑过——点上面的运行按钮</div>
          )}
        </div>
      )}

      {wb.sel && <SubTabs wb={wb} />}
    </aside>
  );
}
