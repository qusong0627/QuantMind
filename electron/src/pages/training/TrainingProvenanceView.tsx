import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Spin, message } from 'antd';
import { clsx } from 'clsx';
import { BarChart3, SlidersHorizontal, Terminal } from 'lucide-react';
import { modelTrainingService, type ModelTrainingRunStatus } from '../../services/modelTrainingService';
import { parseTrainingResult, type TrainingResult } from './trainingUtils';
import { buildTrainingRequestPreview, type TrainingRunPreviewMeta } from './TrainingRunDetailTabs';
import { ProvenanceHeader } from './provenance/ProvenanceHeader';
import { ResultPanel } from './provenance/ResultPanel';
import { ConfigPanel, LogsPanel } from './provenance/Panels';

type TabKey = 'result' | 'logs' | 'config';

export interface TrainingProvenanceViewProps {
  runId?: string | null;
  meta?: TrainingRunPreviewMeta;
  detail?: ModelTrainingRunStatus | null;
  loading?: boolean;
  error?: string;
}

/**
 * 模型管理「训练溯源」专用视图：档案头 + 三段式（结果 / 日志 / 提交配置）。
 * 与训练记录弹窗的 TrainingRunDetailTabs 分离，避免把完整结果页塞进窄面板。
 */
export const TrainingProvenanceView: React.FC<TrainingProvenanceViewProps> = ({
  runId,
  meta = {},
  detail = null,
  loading = false,
  error = '',
}) => {
  const [activeTab, setActiveTab] = useState<TabKey>('result');
  const [runRequest, setRunRequest] = useState<Record<string, unknown> | null>(null);
  const [requestLoading, setRequestLoading] = useState(false);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    setRequestLoading(true);
    modelTrainingService
      .getTrainingRunRequest(runId)
      .then((data) => {
        if (alive) setRunRequest(data?.request || null);
      })
      .catch(() => {
        if (alive) setRunRequest(null);
      })
      .finally(() => {
        if (alive) setRequestLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId]);

  const parsedResult = useMemo<TrainingResult | null>(() => {
    if (!runId || !detail?.result) return null;
    return parseTrainingResult(buildTrainingRequestPreview(runRequest, meta), runId, detail.result);
  }, [runId, detail, runRequest, meta]);

  const exportConfig = useCallback(() => {
    if (!runId || !runRequest) {
      message.info('该训练没有保存提交载荷');
      return;
    }
    const blob = new Blob([JSON.stringify(runRequest, null, 2)], {
      type: 'application/json;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${runId}_request.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [runId, runRequest]);

  if (!runId) {
    return (
      <Alert
        type="info"
        showIcon
        className="rounded-2xl"
        message="该模型没有关联训练任务"
        description="从模型管理导入或系统内置的模型不会带训练 run；重新训练后才会生成训练溯源。"
      />
    );
  }

  const tabs: Array<{ key: TabKey; label: string; icon: React.ReactNode; count?: string }> = [
    { key: 'result', label: '结果', icon: <BarChart3 size={12} />, count: parsedResult ? undefined : '—' },
    { key: 'logs', label: '日志', icon: <Terminal size={12} />, count: detail?.logs ? `${detail.logs.split('\n').length}` : undefined },
    { key: 'config', label: '提交配置', icon: <SlidersHorizontal size={12} /> },
  ];

  const body = (() => {
    if (loading && !detail) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-2">
          <Spin />
          <span className="text-[11px] text-slate-400">加载训练档案…</span>
        </div>
      );
    }
    if (error && !detail) {
      return <Alert type="error" showIcon className="rounded-2xl" message="训练详情加载失败" description={error} />;
    }
    if (activeTab === 'logs') return <LogsPanel logs={detail?.logs} />;
    if (activeTab === 'config') {
      return requestLoading && !runRequest ? (
        <div className="flex h-full flex-col items-center justify-center gap-2">
          <Spin />
          <span className="text-[11px] text-slate-400">加载提交配置…</span>
        </div>
      ) : (
        <ConfigPanel request={runRequest} onExport={exportConfig} />
      );
    }
    if (parsedResult) {
      return (
        <div className="h-full overflow-y-auto custom-scrollbar pr-1">
          <ResultPanel result={parsedResult} runId={runId} onExportConfig={exportConfig} />
        </div>
      );
    }
    return (
      <Alert
        type="warning"
        showIcon
        className="rounded-2xl"
        message="这条训练没有可解析的结果"
        description={
          <div className="space-y-2">
            <p className="text-[11px] text-slate-500">
              可能是训练失败、被取消，或结果载荷由旧版本产生。
              {detail?.result ? '原始 result 载荷如下：' : '该任务没有 result 载荷。'}
            </p>
            {detail?.result ? (
              <pre className="max-h-[46vh] overflow-auto rounded-xl bg-slate-50 p-3 text-[10px] text-slate-600 custom-scrollbar">
                {JSON.stringify(detail.result, null, 2)}
              </pre>
            ) : null}
          </div>
        }
      />
    );
  })();

  return (
    <div className="flex h-[calc(var(--app-h)-320px)] min-h-[440px] flex-col gap-2.5 px-1 pb-1">
      <ProvenanceHeader
        runId={runId}
        meta={meta}
        detail={detail}
        result={parsedResult}
        onExportConfig={exportConfig}
      />

      <div className="flex shrink-0 items-center gap-1 rounded-xl border border-slate-200/80 bg-white p-1 shadow-[0_1px_2px_rgba(15,23,42,0.03)]">
        {tabs.map((tab) => {
          const active = tab.key === activeTab;
          return (
            <button
              key={tab.key}
              type="button"
              onClick={() => setActiveTab(tab.key)}
              className={clsx(
                'inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-black transition-all sm:flex-none sm:px-5',
                active
                  ? 'bg-slate-900 text-white shadow-sm'
                  : 'text-slate-500 hover:bg-slate-50 hover:text-slate-700',
              )}
            >
              {tab.icon}
              {tab.label}
              {tab.count !== undefined ? (
                <span className={clsx('rounded-full px-1.5 py-0.5 font-mono text-[9px]', active ? 'bg-white/15 text-white/80' : 'bg-slate-100 text-slate-400')}>
                  {tab.count}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1">{body}</div>
    </div>
  );
};

export default TrainingProvenanceView;
