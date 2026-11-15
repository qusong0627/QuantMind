/** 新增自定义 Pine 策略的弹窗：标题 / 分类 / 源码。
 *
 * 只调 `wb.createPine`（后端写 custom/，索引重建不丢）；成功后选中新条目。
 * 源码不做前端语法校验——真正的静态闸在宿主 worker 的转写/沙箱链路里。
 */
import { useState } from 'react';
import type { Workbench } from './useWorkbench';

const SAMPLE = `//@version=5
strategy("我的策略", overlay=true)

fast = ta.sma(close, 5)
slow = ta.sma(close, 20)

if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("Long")
`;

export default function NewPineModal({
  wb,
  onClose,
}: {
  wb: Workbench;
  onClose: () => void;
}) {
  const [title, setTitle] = useState('');
  const [category, setCategory] = useState('我的策略');
  const [source, setSource] = useState(SAMPLE);

  const submit = async () => {
    const ok = await wb.createPine({ title, category, source });
    if (ok) onClose();
  };

  return (
    <div className="lab-modal-mask" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="lab-modal" role="dialog" aria-label="新增 Pine 策略">
        <div className="lab-modal-title">
          新增 Pine 策略
          <button className="lab-modal-x" onClick={onClose} aria-label="关闭">×</button>
        </div>

        <label className="lab-field">
          <span>标题</span>
          <input
            autoFocus
            value={title}
            placeholder="例：我的双均线过滤版"
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>

        <label className="lab-field">
          <span>分类</span>
          <input
            value={category}
            placeholder="我的策略"
            onChange={(e) => setCategory(e.target.value)}
          />
        </label>

        <label className="lab-field lab-field-col">
          <span>Pine 源码</span>
          <textarea
            className="lab-lib-editor lab-modal-src"
            spellCheck={false}
            value={source}
            onChange={(e) => setSource(e.target.value)}
          />
        </label>

        {wb.createErr && <div className="lab-err">{wb.createErr}</div>}

        <div className="lab-modal-actions">
          <button className="lab-more" onClick={onClose} disabled={wb.createBusy}>
            取消
          </button>
          <button
            className="lab-run lab-modal-ok"
            disabled={wb.createBusy || !title.trim() || !source.trim()}
            onClick={submit}
          >
            {wb.createBusy ? '创建中…' : '创建策略'}
          </button>
        </div>
      </div>
    </div>
  );
}
