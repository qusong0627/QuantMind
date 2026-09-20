/**
 * 全链证据矩阵（T-FE-16，设计《评估与打分体系》§七）：十环一屏——数据/特征/模型/信号/回测/
 * 模拟/执行/账本/策略/系统；每格绿黄红 + **无证据独立深黄态（不粉饰）** + 点击下钻证据原文。
 *
 * 配色唯一出处是 deskModel 的 statusStyle()——本文件不再自持色表。
 *
 * 排版：tile 化（左侧状态色条 + hover 浮起），点击 → 居中下钻弹窗。
 */

import React from 'react';
import { Layers } from 'lucide-react';
import type { EvidenceBlock, EvidenceRing } from '../types';
import { evidenceSummary, statusStyle } from '../deskModel';
import { useUiMode } from '../../shared/useUiMode';
import { CARD, CardHeader } from './cardKit';

interface EvidenceMatrixProps {
  evidence: EvidenceBlock | null | undefined;
  onDrill: (ring: EvidenceRing) => void;
}

/** 图例顺序（label 与配色全部取自 statusStyle，此处只定顺序与计数字段） */
const LEGEND_ORDER = ['ok', 'warn', 'fail', 'no_evidence'] as const;

export const EvidenceMatrix: React.FC<EvidenceMatrixProps> = ({ evidence, onDrill }) => {
  const { isSimple } = useUiMode();
  const rings = evidence?.rings || [];
  const summary = evidenceSummary(rings);
  if (rings.length === 0) return null;

  return (
    <section className={CARD}>
      <CardHeader
        icon={<Layers className="h-4 w-4" />}
        title="全链证据矩阵"
        extra={
          <span className="inline-flex items-center gap-2 text-[11px] font-medium text-slate-500">
            {LEGEND_ORDER.map((level) => {
              const style = statusStyle(level);
              const count = level === 'no_evidence' ? summary.noEvidence : summary[level];
              return (
                <span key={level} className="inline-flex items-center gap-1">
                  <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
                  <span className={style.text}>{count}</span> {style.label}
                </span>
              );
            })}
            {summary.gapLabels.length > 0 && (
              <span className="text-slate-400 font-normal hidden 2xl:inline">
                （{summary.gapLabels.join('、')}）
              </span>
            )}
          </span>
        }
      />

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
              className={`group relative overflow-hidden text-left rounded-xl border p-2.5 pl-3.5 transition-all hover:shadow-md hover:-translate-y-px ${
                isNoEvidence ? 'border-dashed border-amber-300 bg-amber-50/40' : 'border-slate-200/80 bg-white hover:border-slate-300'
              }`}
            >
              <span className={`absolute left-0 top-0 h-full w-1 ${style.bar}`} />
              <div className="flex items-center gap-1.5">
                <span className="text-xs font-semibold text-slate-800 truncate">{ring.label}</span>
                <span className={`text-[10px] ml-auto shrink-0 font-medium ${style.text}`}>{style.label}</span>
              </div>
              {!isSimple && (
                <div className="text-[10px] text-slate-400 mt-1 leading-4 truncate font-mono">
                  {ring.artifact}
                  <span className="text-slate-300"> · {ring.frequency}</span>
                </div>
              )}
              <div className={`text-[10px] mt-1 leading-4 line-clamp-2 ${isNoEvidence ? 'text-amber-700/80' : 'text-slate-500'}`}>
                {ring.summary || '—'}
              </div>
            </button>
          );
        })}
      </div>

      <footer className="text-[10px] text-slate-400 mt-2.5">
        来源：{evidence?.source || '—'}（每格可点击下钻至证据项与来源；「无证据」为独立状态，不等于通过）
      </footer>
    </section>
  );
};
