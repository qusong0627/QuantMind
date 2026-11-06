import React, { useEffect, useState } from 'react';
import { X, RefreshCw } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { getResearchExperience, type ResearchExperience } from '../services-v2/experienceApi';

const sourceLabels: Record<string, string> = {
  mining_task: '挖掘任务', statistical_result: '统计派结果', factor_job: '物化与验证作业',
};

export const ResearchExperienceDialog: React.FC<{ universe: string; onClose: () => void }> = ({ universe, onClose }) => {
  const [memory, setMemory] = useState<ResearchExperience | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const generation = getSessionGeneration();
    const unregister = registerSessionReset(() => { controller.abort(); setMemory(null); onClose(); });
    setMemory(null);
    setError('');
    setLoading(true);
    getResearchExperience(universe, controller.signal).then((result) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setMemory(result);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) {
        setError(reason instanceof Error ? reason.message : '研究经验加载失败');
      }
    }).finally(() => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setLoading(false);
    });
    return () => { controller.abort(); unregister(); };
  }, [universe, refresh, onClose]);

  return <div role="dialog" aria-modal="true" aria-label="本人研究经验" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
    <div className="flex max-h-[85vh] w-full max-w-4xl flex-col rounded-2xl bg-white shadow-2xl">
      <div className="flex items-center justify-between gap-3 border-b border-slate-100 p-4">
        <h3 className="font-bold text-slate-900">本人研究经验 · {universe}</h3>
        <div className="flex gap-2">
          <button type="button" disabled={loading} onClick={() => setRefresh((current) => current + 1)} className="rounded-lg p-2 text-slate-500 disabled:opacity-40" aria-label="刷新研究经验"><RefreshCw className="h-4 w-4" /></button>
          <button type="button" onClick={onClose} className="rounded-lg p-2 text-slate-500" aria-label="关闭研究经验"><X className="h-4 w-4" /></button>
        </div>
      </div>
      <div className="space-y-5 overflow-y-auto p-4 text-sm text-slate-600">
        <p className="rounded-lg bg-blue-50 p-3 text-xs text-blue-800">仅汇总当前账号在所选股票池中的历史记录。任务完成不代表因子有效；经验只提供研究线索，所有新因子仍需独立检验。</p>
        {loading && <p role="status">正在读取本人历史记录…</p>}
        {error && <p role="alert" className="text-red-600">{error}</p>}
        {memory && <>
          <div className="space-y-1 text-xs text-slate-500">
            <p>读取任务 {memory.sources.tasks}/{memory.sources.tasks_total}，统计结果 {memory.sources.statistical_results}/{memory.sources.statistical_results_total}，物化与验证作业 {memory.sources.factor_jobs}/{memory.sources.factor_jobs_total}。</p>
            <p>{memory.sources.scope}。</p>
            {memory.sources.sample_truncated && <p className="text-amber-700">已限制为最近的记录样本；以下计数不代表全部历史。</p>}
            <p>{memory.injection_enabled ? '新任务和方向拆解会引用有界经验摘要。' : '当前服务器已关闭经验提示注入；此处仍可只读查看。'}</p>
          </div>
          {!memory.available ? <p>当前股票池暂无可用历史经验。</p> : <>
            <section className="space-y-2">
              <h4 className="font-semibold text-slate-800">拒因与失败</h4>
              {memory.reason_buckets.length === 0 && <p className="text-xs text-slate-400">所读取记录没有拒因或失败信息。</p>}
              <div className="grid gap-2 sm:grid-cols-2">
                {memory.reason_buckets.map((item) => <article key={item.bucket} className="space-y-1 rounded-lg border border-slate-200 p-3">
                  <div className="flex justify-between gap-2"><span className="font-medium">{item.label}</span><span>{item.count} 次</span></div>
                  <p className="text-xs text-slate-400">来源：{item.sources.map((source) => sourceLabels[source] || source).join('、')}</p>
                  <p className="break-words text-xs">{item.sample_columns.join('、') || '无样例'}</p>
                  {item.sample_dirs.length > 0 && <p className="break-words text-xs text-slate-400">方向：{item.sample_dirs.join('；')}</p>}
                </article>)}
              </div>
            </section>
            <section className="space-y-2">
              <h4 className="font-semibold text-slate-800">相关性重复提示</h4>
              <p className="text-xs text-slate-400">{memory.sources.duplicates_scope}。</p>
              {memory.saturated_directions.length === 0 ? <p className="text-xs text-slate-400">所读取结果没有相关性拒绝记录。</p> : memory.saturated_directions.map((item, index) => <article key={index} className="rounded-lg border border-slate-200 p-3">
                <p className="break-words font-medium">{item.label}</p>
                <p className="mt-1 text-xs">拒绝 {item.dup_hits} 次，近 90 日 {item.dup_hits_90d} 次；相关候选：{item.top_opponent || '未记录'}。</p>
              </article>)}
              {memory.sources.saturated_directions_omitted > 0 && <p className="text-xs text-slate-400">另有 {memory.sources.saturated_directions_omitted} 个方向未展示。</p>}
            </section>
            <section className="space-y-2">
              <h4 className="font-semibold text-slate-800">方向产出（最多 50 项）</h4>
              <div className="overflow-x-auto"><table className="w-full text-left text-xs">
                <thead><tr className="border-b border-slate-200"><th className="p-2">方向</th><th className="p-2">任务</th><th className="p-2">完成</th><th className="p-2">失败</th><th className="p-2">取消</th><th className="p-2">未归档因子</th></tr></thead>
                <tbody>{memory.direction_stats.map((item, index) => <tr key={index} className="border-b border-slate-100"><td className="max-w-sm break-words p-2">{item.label}</td><td className="p-2">{item.tasks}</td><td className="p-2">{item.completed}</td><td className="p-2">{item.failed}</td><td className="p-2">{item.cancelled}</td><td className="p-2">{item.factors}</td></tr>)}</tbody>
              </table></div>
              {memory.sources.directions_omitted > 0 && <p className="text-xs text-slate-400">另有 {memory.sources.directions_omitted} 个方向未展示。</p>}
            </section>
          </>}
        </>}
      </div>
    </div>
  </div>;
};
