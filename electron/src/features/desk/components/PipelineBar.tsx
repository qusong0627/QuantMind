/** 管线进度条（FE-B）：数据→推理→信号→结算 四步状态（与体检同源；T-FE-03 v2 可逐层下钻）
 *
 * 浅色专业金融风：单卡内 ①②③④ 串联步进条——编号节点 + 状态色 + 细分隔线，
 * 整段可点下钻（步骤 → 证据环 → 证据项）。
 */

import React from 'react';
import type { PipelineStep } from '../types';
import { statusStyle } from '../deskModel';

interface PipelineBarProps {
  steps: PipelineStep[];
  /** 点击步骤 → 逐层下钻（步骤 → 同源证据环 → 逐证据项） */
  onStepDrill?: (step: PipelineStep) => void;
}

export const PipelineBar: React.FC<PipelineBarProps> = ({ steps, onStepDrill }) => (
  <section className="bg-white rounded-2xl border border-slate-200/80 shadow-[0_1px_2px_rgba(15,23,42,0.04)] px-1.5 py-1.5">
    <div className="flex items-stretch">
      {(steps || []).map((step, index) => {
        const style = statusStyle(step.status);
        const inner = (
          <>
            <div className="flex items-center gap-2 min-w-0">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-100 text-[10px] font-bold text-slate-500 shrink-0 tabular-nums">
                {index + 1}
              </span>
              <span className="text-[12.5px] font-semibold text-slate-800 truncate">{step.label}</span>
              <span className={`ml-auto inline-flex items-center gap-1 text-[11px] font-medium shrink-0 ${style.text}`}>
                <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
                {style.label}
              </span>
            </div>
            <div className="text-[11px] text-slate-500 mt-1 truncate leading-4">{step.detail || '—'}</div>
          </>
        );
        return (
          <React.Fragment key={step.key}>
            {onStepDrill ? (
              <button
                type="button"
                onClick={() => onStepDrill(step)}
                title={`${step.detail}\n（来源：${step.source}）\n点击下钻：步骤 → 证据环 → 证据项`}
                className="group flex-1 min-w-[150px] text-left rounded-xl px-3 py-2 hover:bg-slate-50 transition-colors"
              >
                {inner}
              </button>
            ) : (
              <div
                className="flex-1 min-w-[150px] rounded-xl px-3 py-2"
                title={`${step.detail}\n（来源：${step.source}）`}
              >
                {inner}
              </div>
            )}
            {index < steps.length - 1 && (
              <div className="w-px bg-slate-100 my-2 shrink-0" aria-hidden="true" />
            )}
          </React.Fragment>
        );
      })}
    </div>
  </section>
);
