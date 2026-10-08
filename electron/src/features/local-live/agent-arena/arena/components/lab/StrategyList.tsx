/** 左栏：策略来源。上段是后端 6 个内置模板，下段是 1001 条 Pine 语料。
 *  两段共用中栏同一个回测区——这是「合并页」的核心，不再是两个 tab 各看各的。 */
import type { Workbench } from './useWorkbench';

export default function StrategyList({ wb }: { wb: Workbench }) {
  const items = wb.list?.items ?? [];
  const canMore = wb.list ? wb.list.filtered > items.length : false;

  return (
    <aside className="lab-rail">
      <section className="lab-rail-sec">
        <div className="lab-rail-title">
          <span className="t">内置模板</span>
          <span className="lab-rail-count">{wb.strategies.length}</span>
        </div>
        <ul className="lab-tpl-list">
          {wb.strategies.map((s) => (
            <li
              key={s.id}
              className={wb.sel?.kind === 'template' && wb.sel.id === s.id ? 'on' : ''}
              title={s.desc}
              onClick={() => wb.selectTemplate(s.id)}
            >
              <span className="n">
                {s.name}
                {wb.noteIds.has(s.id) && (
                  <em className="note" title="有备注">
                    💬
                  </em>
                )}
              </span>
              <span className="d">{s.desc}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="lab-rail-sec lab-rail-grow">
        <div className="lab-rail-title">
          <span className="t">Pine 策略库</span>
          <span className="lab-rail-count">{wb.list?.filtered ?? 0}</span>
        </div>

        <div className="lab-rail-filter">
          <select value={wb.category} onChange={(e) => wb.setCategory(e.target.value)}>
            <option value="">全部分类</option>
            {(wb.list?.categories ?? []).map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}（{c.count}）
              </option>
            ))}
          </select>
          <input
            value={wb.q}
            placeholder="标题 / 文件名 / 序号"
            onChange={(e) => wb.setQ(e.target.value)}
          />
        </div>

        {wb.listErr && <div className="lab-err">{wb.listErr}</div>}

        <ul className="lab-pine-list">
          {items.map((it) => {
            const tp = wb.transpile[it.id];
            return (
              <li
                key={it.id}
                className={wb.sel?.kind === 'pine' && wb.sel.id === it.id ? 'on' : ''}
                title={`${it.file}\n${it.lines} 行 · ${it.category}`}
                onClick={() => wb.selectPine(it.id)}
              >
                <div className="lab-pine-row">
                  <span className="i">{it.id}</span>
                  <span className="n">{it.title || it.file}</span>
                  <span className="b">
                    {wb.noteIds.has(it.id) && (
                      <em className="note" title="有备注">
                        💬
                      </em>
                    )}
                    {it.source_kind === 'edited' && <em className="edited">改</em>}
                    {tp && (
                      <em className="ai" title="已 AI 转写为 Pyne-Python">
                        AI
                      </em>
                    )}
                    {tp?.has_report && (
                      <em className="rep" title="已有沙箱回测报告">
                        ▶
                      </em>
                    )}
                  </span>
                </div>
                <div className="lab-pine-sub">
                  {it.category}
                  {it.version ? ` · v${it.version}` : ''}
                  <span className={it.indent_ok ? 'dot ok' : 'dot bad'} />
                  {it.indent_ok ? '可编译' : '缩进缺失'}
                </div>
              </li>
            );
          })}
        </ul>

        {canMore && (
          <button className="lab-more" disabled={wb.listBusy} onClick={wb.loadMore}>
            {wb.listBusy ? '加载中…' : `加载更多（还有 ${wb.list!.filtered - items.length} 条）`}
          </button>
        )}
        {!wb.listBusy && !items.length && !wb.listErr && <div className="lab-empty">没有匹配的策略</div>}
      </section>
    </aside>
  );
}
