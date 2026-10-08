/** 批量回测结果：批次选择 + 策略排行 / 单策略选股两个子视图。
 *
 * 数据来自 scripts/lab_batch_backtest.py 跑出的 data/lab_batch/*.json，
 * 页面只读（跑批是 CPU 密集作业，不进 API 进程）。
 */
import { useEffect, useState } from 'react';
import { fetchLabBatchRun, fetchLabBatchRuns, type LabBatchDetail, type LabBatchRun } from '../../api/client';
import BatchRankTable from './BatchRankTable';
import BatchScreener from './BatchScreener';
import { asUpdater } from '../../reactCompat';

export type LabView = 'rank' | 'screener';

const fmtTime = (s: string) => (s || '').slice(0, 16);

export default function BatchPanel({
  view,
  onPickSymbol,
}: {
  view: LabView;
  onPickSymbol: (code: string) => void;
}) {
  const [runs, setRuns] = useState<LabBatchRun[]>([]);
  const [runId, setRunId] = useState('');
  const [detail, setDetail] = useState<LabBatchDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    fetchLabBatchRuns(30)
      .then((list) => {
        setRuns(list);
        setRunId(asUpdater((cur) => cur || (list[0]?.run_id ?? '')));
      })
      .catch(() => setErr('批次列表加载失败'));
  }, []);

  useEffect(() => {
    if (!runId) return;
    setBusy(true);
    setErr('');
    fetchLabBatchRun(runId)
      .then(setDetail)
      .catch(() => {
        setDetail(null);
        setErr(`批次读取失败：${runId}`);
      })
      .finally(() => setBusy(false));
  }, [runId]);

  if (!runs.length && !err) {
    return (
      <div className="lab-empty">
        <b>还没有批量回测结果</b>
        先在服务器上跑一批，跑完这里会出现策略排行与选股：
        <code>pine_lab/.venv/bin/python scripts/lab_batch_backtest.py --pool hs300</code>
      </div>
    );
  }

  return (
    <div className="lab-batch">
      <div className="lab-batch-bar">
        <label>
          <span>批次</span>
          <select value={runId} onChange={(e) => setRunId(e.target.value)}>
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {fmtTime(r.created)} · {r.pool_label} · {r.universe} 标的
              </option>
            ))}
          </select>
        </label>
        <div className="lab-batch-meta">
          {detail && (
            <>
              <span>{detail.pool_label}</span>
              <span>{detail.universe} 标的</span>
              <span>{detail.counts?.rows ?? 0} 行结果</span>
              <span>{detail.adj === 'backward' ? '后复权' : detail.adj}</span>
              {detail.elapsed_sec ? <span>跑批 {detail.elapsed_sec}s</span> : null}
            </>
          )}
        </div>
      </div>

      <details className="lab-cmd">
        <summary>跑新批次 · 在服务器上执行（跑批吃 CPU，不进 API 进程）</summary>
        <div className="lab-cmd-body">
          在仓库根目录执行：
          <code>pine_lab/.venv/bin/python scripts/lab_batch_backtest.py --pool hs300</code>
          池子可换 sz50 / zz500 / zz1000 / all（全市场，按流通市值降序，配 --limit N 限量）。
        </div>
      </details>

      {err && <div className="lab-err">{err}</div>}
      {busy && !detail && <div className="lab-empty">加载中…</div>}

      {detail && view === 'rank' && <BatchRankTable detail={detail} />}
      {detail && view === 'screener' && (
        <BatchScreener runId={detail.run_id} strategies={detail.strategies ?? []} onPickSymbol={onPickSymbol} />
      )}
    </div>
  );
}
