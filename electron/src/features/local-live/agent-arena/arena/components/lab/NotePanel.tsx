/** 备注面板：每条策略一份 markdown 备注 + 标签 + 评分 + 状态。
 *
 * 表单状态是本地草稿（保存前的编辑不该被服务端回包打断），只在切换策略时用服务端值重置。
 */
import { useEffect, useRef, useState } from 'react';
import { NOTE_STATUSES, type NoteStatus } from '../../api/client';
import type { Workbench } from './useWorkbench';

export default function NotePanel({ wb }: { wb: Workbench }) {
  const [text, setText] = useState('');
  const [tags, setTags] = useState('');
  const [rating, setRating] = useState(0);
  const [status, setStatus] = useState<NoteStatus>('待研究');
  const loadedId = useRef('');

  const id = wb.sel?.id ?? '';
  useEffect(() => {
    if (!id || wb.note?.id !== id || loadedId.current === id) return;
    loadedId.current = id;
    setText(wb.note.note);
    setTags(wb.note.tags.join(' '));
    setRating(wb.note.rating ?? 0);
    setStatus(wb.note.status);
  }, [id, wb.note]);

  if (!id) return null;

  const save = () =>
    wb.saveNote({
      note: text,
      tags,
      rating: rating || null,
      status,
      by: 'user',
    });

  return (
    <>
      <textarea
        className="lab-note-edit"
        value={text}
        placeholder="这只策略怎么用：适合什么行情、坑在哪、改过什么……"
        onChange={(e) => setText(e.target.value)}
      />

      <div className="lab-note-meta">
        <label>
          <span>标签</span>
          <input
            value={tags}
            placeholder="空格分隔：趋势 多周期"
            onChange={(e) => setTags(e.target.value)}
          />
        </label>
        <label>
          <span>评分</span>
          <select value={rating} onChange={(e) => setRating(Number(e.target.value))}>
            <option value={0}>未评</option>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {'★'.repeat(n)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>状态</span>
          <select value={status} onChange={(e) => setStatus(e.target.value as NoteStatus)}>
            {NOTE_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="lab-lib-actions">
        <button className="lab-run lab-lib-save" disabled={wb.noteBusy} onClick={save}>
          {wb.noteBusy ? '保存中…' : '保存备注'}
        </button>
        {wb.noteMsg && <span className="lab-lib-msg">{wb.noteMsg}</span>}
        {wb.noteErr && <span className="lab-lib-msg err">{wb.noteErr}</span>}
      </div>
      {wb.note?.updated && (
        <div className="lab-note-stamp">
          更新于 {wb.note.updated.replace('T', ' ').slice(0, 19)} · {wb.note.by}
        </div>
      )}
    </>
  );
}
