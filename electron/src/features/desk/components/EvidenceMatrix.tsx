/**
 * 全链证据矩阵（T-FE-16，设计《评估与打分体系》§七）：十环一屏——数据/特征/模型/信号/回测/
 * 模拟/执行/账本/策略/系统；每格红黄绿 + **无证据独立灰态（不粉饰）** + 点击下钻证据原文。
 */

import React from 'react';
import { Layers } from 'lucide-react';
import type { EvidenceBlock, EvidenceRing } from '../types';
import { evidenceSummary, statusStyle } from '../deskModel';
import { useUiMode } from '../../shared/useUiMode';

interface EvidenceMatrixProps {
  evidence: EvidenceBlock | null | undefined;
  onDrill: (ring: EvidenceRing) => void;
}

export const EvidenceMatrix: React.FC<EvidenceMatrixProps> = ({ evidence, onDrill }) => {
  const { isSimple } = useUiMode();
  const rings = evidence?.rings || [];
  const summary = evidenceSummary(rings);
  if (rings.length === 0) return null;

  return (
    <section className="bg-white rounded-2xl border border-gray-200 p-4">
      <header className="flex flex-wrap items-center gap-2 mb-3">
        <Layers className="w-4 h-4 text-blue-600" />
        <h3 className="text-sm font-semibold text-slate-800">全链证据矩阵</h3>
        <span className="text-[11px] text-slate-500">
          {summary.ok} 正常 / {summary.warn} 警告 / {summary.fail} 异常 / {summary.noEvidence} 无证据
        </span>
        {summary.gapLabels.length > 0 && (
          <span className="text-[11px] text-slate-400">
            无证据环节：{summary.gapLabels.join('、')}
          </span>
        )}
      </header>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
        {rings.map((ring) => {
          const style = statusStyle(ring.level);
          const isNoEvidence = ring.level === 'no_evidence';
          return (
            <button
              key={ring.key}
              type="button"
              onClick={() => onDrill(ring)}
              title={`[点击下钻] ${ring.artifact}（${ring.frequency}）\n${ring.summary || ''}`}
              className={`text-left rounded-2xl border p-3 transition-colors hover:bg-gray-50 ${
                isNoEvidence ? 'border-dashed border-slate-300 bg-slate-50/60' : 'border-gray-200 bg-white'
              }`}
            >
              <div className="flex items-center gap-1.5">
                <span className={`h-2 w-2 rounded-full ${style.dot}`} />
                <span className="text-xs font-semibold text-slate-800">{ring.label}</span>
                <span className={`text-[10px] ml-auto ${style.text}`}>{style.label}</span>
              </div>
              {!isSimple && (
                <div className="text-[10px] text-slate-400 mt-1 leading-4">
                  {ring.artifact}
                  <span className="text-slate-300"> · {ring.frequency}</span>
                </div>
              )}
              <div className={`text-[10px] mt-1 leading-4 line-clamp-2 ${isNoEvidence ? 'text-slate-400' : 'text-slate-500'}`}>
                {ring.summary || '—'}
              </div>
            </button>
          );
        })}
      </div>

      <footer className="text-[10px] text-slate-400 mt-2">
        来源：{evidence?.source || '—'}（每格可点击下钻至证据项与来源；「无证据」为独立状态，不等于通过）
      </footer>
    </section>
  );
};
