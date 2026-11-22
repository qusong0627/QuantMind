import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Spin, Tabs, message } from 'antd';
import { Copy, Download, FileText, SlidersHorizontal, Terminal } from 'lucide-react';
import { modelTrainingService, type ModelTrainingRunStatus } from '../../services/modelTrainingService';
import {
  DEFAULT_CONTEXT,
  DEFAULT_PARAMS,
  DEFAULT_TARGET,
  buildLabelFormula,
  parseTrainingResult,
  type TrainingContext,
  type TrainingParams,
  type TrainingRequestPayload,
  type TrainingResult,
  type TrainingTarget,
} from './trainingUtils';
import { TrainingResultCompactContext, TrainingResultView } from './TrainingResultView';

/** 构造结果页请求预览时，从列表摘要/模型元数据补的少量字段。 */
export interface TrainingRunPreviewMeta {
  display_name?: string | null;
  market?: string | null;
  target_horizon_days?: number | null;
  created_at?: string | null;
}

/** 把后台提交载荷折成结果页需要的 TrainingRequestPayload（只补结果页读的字段）。 */
export const buildTrainingRequestPreview = (
  request: Record<string, unknown> | null,
  meta: TrainingRunPreviewMeta = {},
): TrainingRequestPayload => {
  const raw = (request || {}) as Record<string, any>;
  const ctx = (raw.context || {}) as Record<string, any>;
  const lgb = (raw.lgb_params || {}) as Record<string, any>;
  const features = Array.isArray(raw.features)
    ? (raw.features as string[])
    : Array.isArray(raw.resolved_features)
      ? (raw.resolved_features as string[])
      : [];
  const target: TrainingTarget = {
    ...DEFAULT_TARGET,
    horizonDays: Number(
      raw.target_horizon_days || meta.target_horizon_days || DEFAULT_TARGET.horizonDays,
    ),
    mode: (raw.target_mode as TrainingTarget['mode']) || DEFAULT_TARGET.mode,
    executionPrice: (raw.execution_price as TrainingTarget['executionPrice']) || DEFAULT_TARGET.executionPrice,
    labelTransform: (raw.label_transform as TrainingTarget['labelTransform']) || DEFAULT_TARGET.labelTransform,
    labelTopPct: Number(raw.label_top_pct ?? DEFAULT_TARGET.labelTopPct),
    labelDropExtremePct: Number(raw.label_drop_extreme_pct ?? DEFAULT_TARGET.labelDropExtremePct),
  };
  const params: TrainingParams = {
    ...DEFAULT_PARAMS,
    objective: (lgb.objective as TrainingParams['objective']) || DEFAULT_PARAMS.objective,
    metric: (lgb.metric as TrainingParams['metric']) || DEFAULT_PARAMS.metric,
  };
  const market = (ctx.market || meta.market || 'CN') as TrainingContext['market'];
  const context: TrainingContext = {
    ...DEFAULT_CONTEXT,
    market,
    initialCapital: Number(ctx.initial_capital ?? DEFAULT_CONTEXT.initialCapital),
    benchmark: String(ctx.benchmark ?? DEFAULT_CONTEXT.benchmark),
    commissionRate: Number(ctx.commission_rate ?? DEFAULT_CONTEXT.commissionRate),
    slippage: Number(ctx.slippage ?? DEFAULT_CONTEXT.slippage),
    dealPrice: (ctx.deal_price as TrainingContext['dealPrice']) || DEFAULT_CONTEXT.dealPrice,
    industry_as_feature: Boolean(ctx.industry_as_feature ?? DEFAULT_CONTEXT.industry_as_feature),
  };
  // 老任务的 request_payload 常缺 label_formula / training_window（空串），
  // 但起止日期与标签参数都在：缺哪个就从哪个重建，别让结果页显示空白。
  const trainRange: [string, string] = [String(raw.train_start || ''), String(raw.train_end || '')];
  const validRange: [string, string] = [String(raw.valid_start || ''), String(raw.valid_end || '')];
  const testRange: [string, string] = [String(raw.test_start || ''), String(raw.test_end || '')];
  const derivedWindow = [trainRange, validRange, testRange]
    .filter(([start, end]) => start && end)
    .map(([start, end]) => `${start} → ${end}`)
    .join(' | ');
  const derivedLabelFormula = String(raw.execution_price || '').trim()
    ? buildLabelFormula(target)
    // 老任务没存 execution_price：不臆造 open/close，只写通用口径并标注缺失
    : `label = close(T+${target.horizonDays + 1}) / entry(T+1) - 1（入场价口径未记录）`;
  return {
    displayName: String(raw.display_name || raw.job_name || meta.display_name || ''),
    selectedFeatures: features,
    featureCategories: [],
    target,
    timePeriods: {
      train: trainRange,
      val: validRange,
      test: testRange,
    },
    params,
    context,
    generatedAt: String(raw.generated_at || meta.created_at || ''),
    labelFormula: String(raw.label_formula || '').trim() || derivedLabelFormula,
    effectiveTradeDate: '',
    trainingWindow: String(raw.training_window || '').trim() || derivedWindow,
    pool_id: (raw.pool_id as string) || null,
  };
};

interface TrainingRunDetailTabsProps {
  runId?: string | null;
  meta?: TrainingRunPreviewMeta;
  /** 外部已拉取的详情（训练记录弹窗传）；不传则组件自己拉。 */
  detail?: ModelTrainingRunStatus | null;
  /** 外部已拉取的提交载荷；不传则组件自己拉。 */
  runRequest?: Record<string, unknown> | null;
  loading?: boolean;
  error?: string;
  defaultActiveKey?: string;
  /** 紧凑密度（模型管理「训练溯源」内嵌时用），压小结果页留白与图表。 */
  compact?: boolean;
  /** 覆盖「提交配置 → 下载 JSON」行为（训练记录弹窗沿用其下载实现）。 */
  onExportConfig?: () => void;
}

/** 训练结果 + 日志 + 提交配置三页签；训练记录弹窗与模型管理「训练溯源」共用。 */
export const TrainingRunDetailTabs: React.FC<TrainingRunDetailTabsProps> = ({
  runId,
  meta = {},
  detail: externalDetail,
  runRequest: externalRequest,
  loading: externalLoading,
  error: externalError,
  defaultActiveKey = 'result',
  compact = false,
  onExportConfig,
}) => {
  const [detail, setDetail] = useState<ModelTrainingRunStatus | null>(null);
  const [runRequest, setRunRequest] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const needDetail = externalDetail === undefined;
  const needRequest = externalRequest === undefined;

  useEffect(() => {
    if (!runId || (!needDetail && !needRequest)) return;
    let alive = true;
    setLoading(true);
    setError('');
    const tasks: Array<Promise<unknown>> = [];
    if (needDetail) {
      tasks.push(
        modelTrainingService.getTrainingRun(runId).then((data) => {
          if (alive) setDetail(data);
        }),
      );
    }
    if (needRequest) {
      tasks.push(
        modelTrainingService
          .getTrainingRunRequest(runId)
          .then((data) => {
            if (alive) setRunRequest(data?.request || null);
          })
          .catch(() => {
            if (alive) setRunRequest(null);
          }),
      );
    }
    Promise.all(tasks)
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : '训练详情加载失败');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId, needDetail, needRequest]);

  const effectiveDetail = needDetail ? detail : (externalDetail ?? null);
  const effectiveRequest = needRequest ? runRequest : (externalRequest ?? null);
  const effectiveLoading = needDetail ? loading : Boolean(externalLoading);
  const effectiveError = needDetail ? error : (externalError || '');

  const parsedResult = useMemo<TrainingResult | null>(() => {
    if (!runId || !effectiveDetail?.result) return null;
    return parseTrainingResult(
      buildTrainingRequestPreview(effectiveRequest, meta),
      runId,
      effectiveDetail.result,
    );
  }, [runId, effectiveDetail, effectiveRequest, meta]);

  const copyLogs = useCallback(async () => {
    const text = effectiveDetail?.logs || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      message.success('日志已复制');
    } catch {
      message.warning('复制失败，请手动选择日志文本');
    }
  }, [effectiveDetail]);

  const downloadLogs = useCallback(() => {
    if (!runId || !effectiveDetail?.logs) return;
    const blob = new Blob([effectiveDetail.logs], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${runId}.log`;
    a.click();
    URL.revokeObjectURL(url);
  }, [runId, effectiveDetail]);

  const exportConfig = useCallback(() => {
    if (onExportConfig) {
      onExportConfig();
      return;
    }
    if (!runId || !effectiveRequest) {
      message.info('该训练没有保存提交载荷');
      return;
    }
    const blob = new Blob([JSON.stringify(effectiveRequest, null, 2)], {
      type: 'application/json;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${runId}_request.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [onExportConfig, runId, effectiveRequest]);

  if (!runId) {
    return <Alert type="info" showIcon message="该模型没有关联训练任务" />;
  }
  if (effectiveLoading) {
    return (
      <div className="flex h-56 items-center justify-center">
        <Spin tip="加载训练结果与日志…" />
      </div>
    );
  }
  if (effectiveError) {
    return <Alert type="error" showIcon message="训练详情加载失败" description={effectiveError} />;
  }

  return (
    <TrainingResultCompactContext.Provider value={compact}>
      <Tabs
        defaultActiveKey={defaultActiveKey}
      items={[
        {
          key: 'result',
          label: (
            <span className="inline-flex items-center gap-1">
              <FileText size={14} /> 结果
            </span>
          ),
          children: parsedResult ? (
            <TrainingResultView
              result={parsedResult}
              resultError={effectiveError}
              settingDefaultModel={false}
              onSetDefaultModel={() => message.info('历史训练结果请到「模型管理」设置默认模型')}
              onExportConfig={exportConfig}
              trainingStatus={effectiveDetail?.status || ''}
              runId={runId}
            />
          ) : (
            <Alert
              type="warning"
              showIcon
              message="这条训练没有可解析的结果"
              description={
                <div className="space-y-2">
                  <p className="text-xs text-slate-500">
                    可能是训练失败、被取消，或结果载荷由旧版本产生。
                    {effectiveDetail?.result ? '原始结果如下：' : '该任务没有 result 载荷。'}
                  </p>
                  {effectiveDetail?.result ? (
                    <pre className="max-h-[50vh] overflow-auto rounded-lg bg-slate-50 p-3 text-[11px] text-slate-600">
                      {JSON.stringify(effectiveDetail.result, null, 2)}
                    </pre>
                  ) : null}
                </div>
              }
            />
          ),
        },
        {
          key: 'logs',
          label: (
            <span className="inline-flex items-center gap-1">
              <Terminal size={14} /> 日志
            </span>
          ),
          children: (
            <div>
              <div className="mb-2 flex items-center gap-2">
                <Button size="small" icon={<Copy size={14} />} onClick={() => void copyLogs()}>
                  复制
                </Button>
                <Button size="small" icon={<Download size={14} />} onClick={downloadLogs}>
                  下载 .log
                </Button>
                <span className="text-[11px] text-slate-400">
                  {(effectiveDetail?.logs || '').length} 字符
                </span>
              </div>
              <pre className="max-h-[64vh] min-h-[240px] overflow-auto rounded-xl bg-slate-950 p-4 text-[11px] leading-relaxed text-slate-200">
                {effectiveDetail?.logs || '该训练没有保存日志。'}
              </pre>
            </div>
          ),
        },
        {
          key: 'config',
          label: (
            <span className="inline-flex items-center gap-1">
              <SlidersHorizontal size={14} /> 提交配置
            </span>
          ),
          children: (
            <div>
              <div className="mb-2 flex items-center gap-2">
                <Button size="small" icon={<Download size={14} />} onClick={exportConfig}>
                  下载 JSON
                </Button>
                <span className="text-[11px] text-slate-400">
                  训练提交时的钉扎载荷（含特征清单）
                </span>
              </div>
              <pre className="max-h-[64vh] overflow-auto rounded-xl bg-slate-50 p-4 text-[11px] text-slate-700">
                {effectiveRequest ? JSON.stringify(effectiveRequest, null, 2) : '该训练没有保存提交载荷。'}
              </pre>
            </div>
          ),
        },
      ]}
      />
    </TrainingResultCompactContext.Provider>
  );
};

export default TrainingRunDetailTabs;
