/** 因子去重清单（弹窗）：|ρ| ≥ 阈值的同源因子簇，每簇留一个代表，其余建议剔除 */

import React, { useEffect, useMemo, useState } from 'react';
import { Modal, message } from 'antd';
import { Copy, Layers, RefreshCw } from 'lucide-react';
import { getFactorClusters } from '../../services/factorReportService';
import type { FactorCluster, FactorClusterResponse } from '../../types/factorReport';

interface Props {
  open: boolean;
  dataset: string;
  datasetLabel: string;
  onClose: () => void;
  onPick: (factor: string) => void;
}

const THRESHOLDS = [0.8, 0.9, 0.95];

function corrTone(v: number): string {
  return v >= 0 ? 'text-rose-600' : 'text-emerald-600';
}

/** 生成 Markdown 清单（可直接粘到课题/文档里） */
function toMarkdown(res: FactorClusterResponse, datasetLabel: string): string {
  const s = res.summary;
  const lines = [
    `## ${datasetLabel} 因子去重清单（|ρ| ≥ ${res.threshold}，保留口径 ${res.keep}）`,
    '',
    `> ${s?.n_total ?? 0} 个因子 → ${s?.n_clusters ?? 0} 个同源簇，建议剔除 ${s?.n_duplicates ?? 0} 个，保留 ${s?.n_keep ?? 0} 个`,
    '',
    '| 代表（保留） | ICIR | 重复项（剔除） | 与代表相关性 |',
    '|---|---|---|---|',
  ];
  for (const c of res.clusters || []) {
    const others = c.members.filter((m) => !m.is_rep);
    lines.push(
      `| ${c.representative}${c.representative_display ? ' ' + c.representative_display : ''} | ` +
        `${c.representative_icir?.toFixed(3) ?? '—'} | ` +
        `${others.map((m) => `${m.name}(ICIR ${m.icir?.toFixed(2) ?? '—'})`).join('<br/>')} | ` +
        `${others.map((m) => m.corr_to_rep.toFixed(3)).join('<br/>')} |`,
    );
  }
  return lines.join('\n');
}

export const FactorClusterModal: React.FC<Props> = ({ open, dataset, datasetLabel, onClose, onPick }) => {
  const [threshold, setThreshold] = useState(0.9);
  const [data, setData] = useState<FactorClusterResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useMemo(
    () => () => {
      setLoading(true);
      getFactorClusters(dataset, threshold)
        .then((res) => setData(res))
        .catch(() => setData(null))
        .finally(() => setLoading(false));
    },
    [dataset, threshold],
  );

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const summary = data?.summary;
  const clusters: FactorCluster[] = data?.clusters || [];

  const copyMarkdown = async () => {
    if (!data?.available) return;
    try {
      await navigator.clipboard.writeText(toMarkdown(data, datasetLabel));
      message.success('去重清单已复制为 Markdown');
    } catch {
      message.error('复制失败（浏览器未授权剪贴板）');
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="78vw"
      centered
      destroyOnHidden
      title={
        <div className="flex items-center gap-3 flex-wrap">
          <span className="flex items-center gap-1.5 text-sm font-black text-slate-800">
            <Layers className="w-4 h-4 text-indigo-500" />
            因子去重清单 · {datasetLabel}
          </span>
          <div className="flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5">
            {THRESHOLDS.map((t) => (
              <button
                key={t}
                onClick={() => setThreshold(t)}
                className={`rounded-full px-2.5 py-0.5 text-[11px] font-bold transition-colors ${
                  threshold === t ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                }`}
                title={`同源阈值 |ρ| ≥ ${t}`}
              >
                |ρ|≥{t}
              </button>
            ))}
          </div>
          <button
            onClick={copyMarkdown}
            className="flex items-center gap-1 rounded-full border border-slate-200 px-2.5 py-0.5 text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200"
            title="复制为 Markdown 表格"
          >
            <Copy className="w-3 h-3" />
            复制清单
          </button>
          <button
            onClick={load}
            className="flex items-center gap-1 rounded-full border border-slate-200 px-2.5 py-0.5 text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </button>
        </div>
      }
    >
      {!data?.available ? (
        <div className="py-10 text-center text-xs text-slate-500">
          {loading ? '计算中…' : data?.reason || '该数据集快照不可用'}
        </div>
      ) : (
        <div className="flex flex-col gap-3 max-h-[74vh] overflow-y-auto custom-scrollbar pr-1">
          {summary && (
            <div className="rounded-2xl bg-indigo-50/70 border border-indigo-100 px-4 py-2.5 text-xs text-slate-700 leading-5">
              <span className="font-black text-indigo-700">{summary.n_total}</span> 个因子 →
              检出 <span className="font-black text-indigo-700">{summary.n_clusters}</span> 个同源簇 →
              建议剔除 <span className="font-black text-rose-600">{summary.n_duplicates}</span> 个重复因子，
              保留 <span className="font-black text-emerald-600">{summary.n_keep}</span> 个
              <span className="text-slate-400">（最大簇 {summary.largest_cluster} 个成员）</span>
              <div className="text-[11px] text-slate-500 mt-0.5">
                每簇保留 |ICIR| 最高的代表；相关性取绝对值 —— 负相关同样是同源（反向因子）。
                完整 PDF 见 QuantBot 顶栏「调研报告」→ factor_dedup。
              </div>
            </div>
          )}

          {clusters.length === 0 && !loading && (
            <div className="py-8 text-center text-xs text-slate-400">
              该阈值下没有同源因子，说明因子之间互相独立
            </div>
          )}

          {clusters.map((c) => (
            <div key={c.representative} className="rounded-2xl border border-slate-200 bg-white p-3">
              <div className="flex items-center justify-between gap-2 mb-1.5">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="rounded-full bg-emerald-50 border border-emerald-100 px-2 py-[1px] text-[10px] font-black text-emerald-700">
                    保留
                  </span>
                  <button
                    onClick={() => { onPick(c.representative); onClose(); }}
                    className="text-xs font-black text-slate-800 hover:text-indigo-600 truncate"
                    title="在因子报告里打开该因子"
                  >
                    {c.representative}
                    {c.representative_display && (
                      <span className="ml-1.5 font-normal text-slate-400">{c.representative_display}</span>
                    )}
                  </button>
                  <span className="text-[10px] font-mono text-slate-400">
                    ICIR {c.representative_icir?.toFixed(3) ?? '—'}
                  </span>
                </div>
                <span className="text-[10px] text-slate-400 font-mono shrink-0">{c.size} 个同源</span>
              </div>

              <div className="flex flex-col gap-0.5">
                {c.members.filter((m) => !m.is_rep).map((m) => (
                  <div key={m.name} className="flex items-center justify-between gap-2 px-1 py-0.5 rounded hover:bg-slate-50">
                    <button
                      onClick={() => { onPick(m.name); onClose(); }}
                      className="text-[11px] font-bold text-slate-600 hover:text-indigo-600 truncate"
                      title="查看该因子"
                    >
                      {m.name}
                      {m.display_name && <span className="ml-1.5 font-normal text-slate-400">{m.display_name}</span>}
                    </button>
                    <span className="flex items-center gap-3 shrink-0 text-[10px] font-mono text-slate-400">
                      <span>ICIR {m.icir?.toFixed(2) ?? '—'}</span>
                      <span className={`font-bold ${corrTone(m.corr_to_rep)}`}
                            title="与代表的相关性（负值=反向同源）">
                        ρ {m.corr_to_rep > 0 ? '+' : ''}{m.corr_to_rep.toFixed(3)}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
};
