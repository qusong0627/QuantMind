/** 批量回测发起器：股票池 / 自定义代码 × 策略 → 入队，轮询宿主 worker 进度。
 *
 * 执行不在容器里（容器 Python 3.10 装不了 pynecore）：API 只写队列，
 * 宿主 `scripts/lab_batch_worker.py` 消费并跑 CLI；本组件只投递 + 轮询状态。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  fetchLabBatchActive,
  fetchLabBatchPools,
  fetchLabBatchStatus,
  fetchLabStrategies,
  startLabBatch,
  type LabBatchPool,
  type LabBatchStatus,
  type LabStrategy,
} from './api';
import { errText } from './format';

const POLL_MS = 2000;

const EXTRA_POOLS: LabBatchPool[] = [
  { id: 'all', label: '全市场（按流通市值降序）', count: 0 },
  { id: 'custom', label: '自定义代码', count: 0 },
];

const STATUS_TEXT: Record<LabBatchStatus['status'], string> = {
  queued: '排队中（宿主机每分钟取一次）',
  running: '回测中',
  done: '已完成',
  failed: '失败',
};

export default function BatchStartPanel({
  onDone,
}: {
  onDone: (runId: string) => void;
}) {
  const [pools, setPools] = useState<LabBatchPool[]>([]);
  const [strategies, setStrategies] = useState<LabStrategy[]>([]);
  const [pool, setPool] = useState('hs300');
  const [codesText, setCodesText] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [job, setJob] = useState<LabBatchStatus | null>(null);
  const timer = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (timer.current) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  }, []);

  const poll = useCallback((runId: string) => {
    stopPolling();
    const tick = () => {
      fetchLabBatchStatus(runId)
        .then((st) => {
          setJob(st);
          if (st.status === 'done' || st.status === 'failed') {
            stopPolling();
            if (st.status === 'done') onDone(runId);
            else setErr(st.error || '批次失败');
          }
        })
        .catch((e) => {
          // 单次失败不打断；连续失败由下一拍/用户重试兜底
          setErr(errText(e));
        });
    };
    tick();
    timer.current = window.setInterval(tick, POLL_MS);
  }, [onDone, stopPolling]);

  useEffect(() => {
    fetchLabBatchPools()
      .then(setPools)
      .catch(() => setPools([]));
    fetchLabStrategies()
      .then((list) => {
        setStrategies(list);
        setSelected(list.map((s) => s.id));
      })
      .catch(() => setStrategies([]));
    fetchLabBatchActive()
      .then((active) => {
        if (active) {
          setJob(active);
          poll(active.run_id);
        }
      })
      .catch(() => undefined);
    return stopPolling;
  }, [poll, stopPolling]);

  const allPools = useMemo(() => [...pools, ...EXTRA_POOLS], [pools]);
  const current = allPools.find((p) => p.id === pool);
  const parsedCodes = useMemo(
    () => codesText.split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean),
    [codesText],
  );

  const toggle = (id: string) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const submit = async () => {
    setErr('');
    if (!selected.length) {
      setErr('至少选一个策略');
      return;
    }
    if (pool === 'custom' && !parsedCodes.length) {
      setErr('自定义池至少填一个代码');
      return;
    }
    setBusy(true);
    try {
      const r = await startLabBatch({
        pool,
        codes: pool === 'custom' ? parsedCodes : undefined,
        strategies: selected,
      });
      setJob(r.status);
      poll(r.status.run_id);
    } catch (e) {
      setErr(errText(e) || '入队失败');
      // 已有批次在跑：接上它的进度，而不是让用户干等
      fetchLabBatchActive()
        .then((active) => {
          if (active) {
            setJob(active);
            poll(active.run_id);
          }
        })
        .catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };

  const running = job?.status === 'queued' || job?.status === 'running';
  const pct = job?.total ? Math.min(100, Math.round(((job.done ?? 0) / job.total) * 100)) : 0;

  return (
    <div className="lab-batch-start">
      <div className="lab-batch-start-head">
        <div>
          <div className="lab-block-title" style={{ borderBottom: 'none', paddingBottom: 0, marginBottom: 4 }}>
            发起批量回测
          </div>
          <div className="lab-note" style={{ margin: 0 }}>
            股票池 × 所选策略逐标的回测，产物进入下方「策略排行 / 单策略选股」。
            执行在宿主机，同一时间只跑一个批次。
          </div>
        </div>
      </div>

      <div className="lab-batch-form">
        <label className="lab-field">
          <span>股票池</span>
          <select value={pool} onChange={(e) => setPool(e.target.value)} disabled={running || busy}>
            {allPools.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}{p.count ? `（${p.count} 只）` : ''}
              </option>
            ))}
          </select>
        </label>

        {pool === 'custom' && (
          <label className="lab-field lab-field-col">
            <span>自定义代码</span>
            <textarea
              className="lab-batch-codes"
              rows={3}
              spellCheck={false}
              value={codesText}
              disabled={running || busy}
              placeholder="600519.SH, 000001.SZ；也支持每行一个，或 600519 / 000001 / SH600519"
              onChange={(e) => setCodesText(e.target.value)}
            />
            <em className="lab-batch-hint">已识别 {parsedCodes.length} 个代码（提交时后端再校验是否为已知 A 股）</em>
          </label>
        )}

        <div className="lab-field lab-field-col">
          <span>策略</span>
          <div className="lab-batch-strategies">
            {strategies.map((s) => (
              <label key={s.id} className="lab-batch-check">
                <input
                  type="checkbox"
                  checked={selected.includes(s.id)}
                  disabled={running || busy}
                  onChange={() => toggle(s.id)}
                />
                {s.name}
              </label>
            ))}
          </div>
        </div>

        <div className="lab-batch-actions">
          <button
            className="lab-run lab-batch-submit"
            disabled={busy || running || !selected.length || (pool === 'custom' && !parsedCodes.length)}
            onClick={submit}
          >
            {busy ? '入队中…' : running ? '已有批次在跑…' : `开始回测（${current?.label ?? pool}）`}
          </button>
          {job && (
            <span className="lab-batch-hint">
              {job.run_id} · {STATUS_TEXT[job.status]}
            </span>
          )}
        </div>

        {job && (
          <div className="lab-batch-progress">
            <div className="lab-batch-progress-bar">
              <i style={{ width: `${job.status === 'done' ? 100 : pct}%` }} />
            </div>
            <div className="lab-batch-hint">
              {job.status === 'queued'
                ? '等待宿主机 worker 取任务…'
                : `已跑 ${job.done ?? 0}/${job.total ?? '—'} 标的 · ${job.rows ?? 0} 行`
                  + (job.elapsed_sec != null ? ` · ${job.elapsed_sec}s` : '')}
              {job.status === 'failed' && ` · ${job.error || '失败'}`}
            </div>
          </div>
        )}

        {err && <div className="lab-err">{err}</div>}
      </div>
    </div>
  );
}
