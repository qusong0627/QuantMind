/** 管线进度条（FE-B）：数据→推理→信号→结算 四步状态（与体检同源） */

import React from 'react';
import type { PipelineStep } from '../types';
import { statusStyle } from '../deskModel';

interface PipelineBarProps {
  steps: PipelineStep[];
}

export const PipelineBar: React.FC<PipelineBarProps> = ({ steps }) => (
  <div className="flex flex-wrap items-stretch gap-2">
    {(steps || []).map((step, index) => {
      const style = statusStyle(step.status);
      return (
        <React.Fragment key={step.key}>
          <div
            className="flex-1 min-w-[150px] rounded-2xl border border-gray-200 bg-white px-3 py-2.5"
            title={`${step.detail}\n（来源：${step.source}）`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${style.dot}`} />
              <span className="text-xs font-semibold text-slate-800">{step.label}</span>
              <span className={`text-[11px] ${style.text}`}>{style.label}</span>
            </div>
            <div className="text-[11px] text-slate-500 mt-1 line-clamp-2">{step.detail || '—'}</div>
          </div>
          {index < steps.length - 1 && (
            <div className="self-center text-slate-300 text-sm select-none">→</div>
          )}
        </React.Fragment>
      );
    })}
  </div>
);
