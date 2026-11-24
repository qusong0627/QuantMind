import React, { useState } from 'react';
import { Alert, Collapse } from 'antd';
import { Boxes, ClipboardCopy, FileText, ListFilter } from 'lucide-react';
import type { TrainingResult } from '../trainingUtils';
import { AuditSection } from '../AuditSection';
import { EvalReportSection } from '../EvalReportSection';
import {
  CompareRunsModal,
  FactorSelectionReport,
  StrictEvalSection,
} from '../TrainingResultView';
import { copyText, fmtTime, Shell } from './primitives';
import { FeatureSection, ICLadder, VerdictSection } from './SummarySections';
import { ConfigSection, LineageSection, WfaSection } from './RecordSections';

export const ResultPanel: React.FC<{
  result: TrainingResult;
  runId: string;
  onExportConfig: () => void;
}> = ({ result, runId, onExportConfig }) => {
  const [compareOpen, setCompareOpen] = useState(false);
  const reversed = result.metrics?.score_direction === 'reversed';
  const evalReport = result.eval_report || result.metadata.eval_report;
  return (
    <div className="space-y-3 pb-2">
      {reversed ? (
        <Alert
          type="warning"
          showIcon
          className="rounded-2xl border-amber-100 bg-amber-50/70"
          message="检测到反向模型"
          description="验证集 IC < 0，模型预测方向与收益相反（高分=跌、低分=涨）。推理时已自动翻转分数；建议复核特征与标签口径，或重新训练。"
        />
      ) : null}

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)]">
        <VerdictSection
          result={result}
          runId={runId}
          onExportConfig={onExportConfig}
          onCompare={() => setCompareOpen(true)}
        />
        <ICLadder result={result} />
      </div>

      <FeatureSection result={result} />

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
        <ConfigSection result={result} />
        <LineageSection result={result} />
      </div>

      <StrictEvalSection runId={runId} />

      <EvalReportSection report={evalReport} />

      <WfaSection wfa={result.wfa} />

      {result.metadata.factor_selection ? (
        <Collapse
          ghost
          className="provenance-collapse rounded-2xl border border-slate-200/80 bg-white px-1"
          items={[
            {
              key: 'factor-selection',
              label: (
                <span className="inline-flex items-center gap-2 text-[11px] font-black text-slate-600">
                  <ListFilter size={13} className="text-emerald-500" /> 因子筛选报告
                  <span className="font-mono text-[10px] font-normal text-slate-400">
                    入选 {result.metadata.factor_selection.selected?.length ?? 0} / 输入 {result.metadata.factor_selection.stage_counts?.input ?? '—'}
                  </span>
                </span>
              ),
              children: <FactorSelectionReport report={result.metadata.factor_selection} />,
            },
          ]}
        />
      ) : null}

      <AuditSection runId={runId} />

      <Shell
        title="训练产物"
        hint={result.completedAt ? `结果回传 ${fmtTime(result.completedAt)}` : undefined}
        icon={<Boxes size={14} />}
      >
        <div className="flex flex-wrap gap-1.5">
          {result.artifacts.length ? (
            result.artifacts.map((artifact) => (
              <span
                key={artifact}
                className="inline-flex items-center gap-1 rounded-md border border-indigo-100 bg-indigo-50/70 px-2 py-0.5 font-mono text-[9px] font-bold text-indigo-700"
              >
                <FileText size={9} /> {artifact}
              </span>
            ))
          ) : (
            <span className="text-[10px] text-slate-400">该训练未登记产物清单</span>
          )}
        </div>
        <Collapse
          ghost
          className="provenance-collapse -mx-2 mt-1"
          items={[
            {
              key: 'metadata',
              label: <span className="text-[10px] font-black text-slate-500">原始 metadata</span>,
              children: (
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => void copyText(JSON.stringify(result.metadata, null, 2), 'metadata 已复制')}
                    className="absolute right-2 top-2 inline-flex items-center gap-1 rounded bg-white/90 px-1.5 py-1 text-[9px] font-bold text-slate-500 ring-1 ring-slate-200 hover:text-indigo-600"
                  >
                    <ClipboardCopy size={9} /> 复制
                  </button>
                  <pre className="max-h-72 overflow-auto rounded-xl bg-slate-50 p-3 text-[10px] leading-relaxed text-slate-600 custom-scrollbar">
                    {JSON.stringify(result.metadata, null, 2)}
                  </pre>
                </div>
              ),
            },
          ]}
        />
      </Shell>

      <CompareRunsModal open={compareOpen} onClose={() => setCompareOpen(false)} defaultRunId={runId} />
    </div>
  );
};
