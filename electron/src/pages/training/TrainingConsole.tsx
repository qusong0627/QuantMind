import React, { useState, useEffect, useRef } from 'react';
import dayjs from 'dayjs';
import { Card, Divider, Alert, Progress, Tabs, Empty, Typography, Button, Tag, message, Tooltip } from 'antd';
import { Play, FileText, LayoutGrid, Copy, Terminal, CheckCircle2, Layers, Calendar, Target, Clock, ArrowDown } from 'lucide-react';
import { clsx } from 'clsx';
import { useNavigate } from 'react-router-dom';
import {
  TrainingStatus,
  TrainingResult,
  TrainingRequestPayload,
  TrainingFactorFilterConfig,
  DEFAULT_FACTOR_FILTER,
} from './trainingUtils';

interface TrainingConsoleProps {
  trainingStatus: TrainingStatus;
  executionStage: string;
  progress: number;
  logs: string[];
  backendRunStatus: string;
  result: TrainingResult | null;
  requestPreview: TrainingRequestPayload;
  totalDays: number;
  trainDays: number;
  valDays: number;
  testDays: number;
  target: { horizonDays: number; mode: string };
  /** 因子筛选配置（IC/ICIR）—— 用于「特征与基准」展示筛选是否开启 */
  factorFilter?: TrainingFactorFilterConfig;
  /** 运行时选项（节点/时长/资源）—— 备用扩展槽 */
  runtimeOptions?: React.ReactNode;
  /** 查看第五步入库结果 */
  onGoToResult?: () => void;
}

const SectionHeader: React.FC<{ title: string; desc: string; icon?: React.ReactNode }> = ({ title, desc, icon }) => (
  <div className="flex items-start justify-between gap-4">
    <div>
      <div className="flex items-center gap-2">
        {icon}
        <Typography.Title level={4} className="!mb-0 !text-slate-900">
          {title}
        </Typography.Title>
      </div>
      <Typography.Paragraph className="!mb-0 !mt-2 !text-xs !text-slate-500 leading-relaxed">
        {desc}
      </Typography.Paragraph>
    </div>
  </div>
);

/** 样本切分区间只展示日期（ISO 自带的时间与时区偏移不展示，用本地时区回吐日期避免差一天） */
const formatDateOnly = (value?: string): string => {
  if (!value) return '—';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD') : value.slice(0, 10);
};

const MetricCard: React.FC<{
  label: string;
  value: string;
  hint?: string;
  centered?: boolean;
}> = ({ label, value, hint, centered = false }) => (
  <div className={clsx('rounded-2xl border border-slate-200 bg-white p-4 shadow-sm', centered && 'text-center')}>
    <div className={clsx('text-[10px] font-black uppercase tracking-[0.18em] text-slate-400', centered && 'text-center')}>{label}</div>
    <div className={clsx('mt-2 text-lg font-semibold text-slate-900', centered && 'text-center')}>{value}</div>
    {hint ? <div className={clsx('mt-1 text-xs text-slate-500', centered && 'text-center')}>{hint}</div> : null}
  </div>
);

export const TrainingConsole: React.FC<TrainingConsoleProps> = ({
  trainingStatus,
  executionStage,
  progress,
  logs,
  backendRunStatus,
  result,
  requestPreview,
  totalDays,
  trainDays,
  valDays,
  testDays,
  target,
  factorFilter,
  runtimeOptions,
  onGoToResult,
}) => {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<string>('request');
  const logsContainerRef = useRef<HTMLDivElement>(null);

  // 训练开始后自动切换到日志 Tab
  useEffect(() => {
    if (trainingStatus === 'running' || logs.length > 0) {
      setActiveTab('logs');
    }
  }, [trainingStatus]);

  // 新日志到来时自动滚动到底部
  useEffect(() => {
    if (activeTab === 'logs' && logsContainerRef.current) {
      logsContainerRef.current.scrollTop = logsContainerRef.current.scrollHeight;
    }
  }, [logs, activeTab]);

  const handleCopyJson = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(requestPreview, null, 2));
      message.success('请求配置 JSON 已复制到剪贴板');
    } catch {
      message.error('复制失败');
    }
  };

  const handleCopyLogs = async () => {
    if (logs.length === 0) {
      message.info('当前暂无日志可复制');
      return;
    }
    try {
      await navigator.clipboard.writeText(logs.join('\n'));
      message.success('运行日志已复制到剪贴板');
    } catch {
      message.error('复制失败');
    }
  };

  const scrollToBottom = () => {
    if (logsContainerRef.current) {
      logsContainerRef.current.scrollTo({
        top: logsContainerRef.current.scrollHeight,
        behavior: 'smooth',
      });
    }
  };

  const modelTypes = requestPreview.params?.model_types || (requestPreview.params?.model_type ? [requestPreview.params.model_type] : []);

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
      {/* 左侧：执行状态、进度与配置核对 */}
      <div className="space-y-4">
        <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
          <SectionHeader
            title="第四步：执行训练"
            desc="顶部工具栏统一承载训练操作，这里保留状态监控、进度追踪与任务核对。"
            icon={<Play size={18} className="text-blue-500" />}
          />
          <Divider className="my-4" />
          <div className="space-y-4">
            <Alert
              type={trainingStatus === 'running' ? 'info' : trainingStatus === 'completed' ? 'success' : 'warning'}
              showIcon
              message={
                trainingStatus === 'running'
                  ? `训练运行中 · ${executionStage}`
                  : trainingStatus === 'completed'
                    ? `训练已完成 · ${result?.modelId || '—'}`
                    : '尚未开始训练'
              }
              description={
                trainingStatus === 'running'
                  ? (backendRunStatus === 'waiting_callback'
                      ? 'Batch 作业已结束，当前处于 waiting_callback，等待容器最终回调写入完成状态。'
                      : '任务将依次完成特征校验、标签构建、模型训练、验证评估和元数据打包。')
                  : trainingStatus === 'completed'
                    ? '训练编排已完成，结果摘要已同步至模型管理库，可直接用于回测或推理。'
                    : '确认右侧请求参数与左侧配置无误后，点击顶部右上角“开始训练”按钮。'
              }
              className={clsx(
                'rounded-2xl',
                trainingStatus === 'running'
                  ? 'border-blue-100 bg-blue-50/70'
                  : trainingStatus === 'completed'
                    ? 'border-emerald-100 bg-emerald-50/70'
                    : 'border-amber-100 bg-amber-50/70'
              )}
            />

            {trainingStatus === 'completed' && (
              <div className="flex items-center justify-end gap-2">
                {onGoToResult && (
                  <Button
                    size="middle"
                    className="rounded-xl h-9 px-4 font-bold border-slate-200 text-xs text-slate-700 hover:bg-slate-50"
                    onClick={onGoToResult}
                  >
                    查看第五步入库结果
                  </Button>
                )}
                <Button
                  type="primary"
                  size="middle"
                  icon={<LayoutGrid size={16} />}
                  className="rounded-xl h-9 px-5 bg-emerald-600 border-none font-bold shadow-md shadow-emerald-200 text-xs"
                  onClick={() => navigate('/model-registry')}
                >
                  前往模型管理中心
                </Button>
              </div>
            )}

            <div className="grid gap-3 grid-cols-2">
              <MetricCard
                label="请求状态"
                value={
                  trainingStatus === 'running'
                    ? (backendRunStatus === 'waiting_callback' ? '等待回调' : '编排中')
                    : trainingStatus === 'completed'
                      ? '已完成'
                      : '待开始'
                }
                hint={`后端状态：${backendRunStatus || 'draft'} | T+${target.horizonDays} · ${target.mode === 'classification' ? '分类' : '回归'}`}
                centered
              />
              <MetricCard
                label="总样本周期"
                value={`${totalDays} 天`}
                hint={`训练/验证/测试：${trainDays}/${valDays}/${testDays}`}
                centered
              />
            </div>

            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
              <div className="flex items-center justify-between text-xs font-semibold text-slate-700">
                <span className="flex items-center gap-1.5">
                  {trainingStatus === 'running' && (
                    <span className="relative flex h-2 w-2">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
                      <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-600"></span>
                    </span>
                  )}
                  执行进度 · {executionStage}
                </span>
                <span className="font-mono">{trainingStatus === 'draft' ? '未开始' : `${progress}%`}</span>
              </div>
              <Progress percent={progress} showInfo={false} className="mt-2" strokeColor="#2563eb" />
            </div>

            {/* 配置就绪核对清单 */}
            <div className="rounded-2xl border border-slate-200 bg-white p-4 space-y-3">
              <div className="flex items-center justify-between text-xs font-bold text-slate-700">
                <div className="flex items-center gap-1.5">
                  <CheckCircle2 size={14} className="text-emerald-500" />
                  <span>任务配置核对</span>
                </div>
                <span className="text-[11px] font-normal text-slate-400">{requestPreview.displayName || '未命名任务'}</span>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded-xl bg-slate-50 p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-400 font-semibold mb-1">模型架构</div>
                  <div className="flex flex-wrap gap-1">
                    {modelTypes.length > 0 ? (
                      modelTypes.map((m) => (
                        <Tag key={m} color="blue" className="!mr-0 font-mono text-[10px]">
                          {m}
                        </Tag>
                      ))
                    ) : (
                      <span className="text-slate-500 font-mono text-[11px]">未选择模型</span>
                    )}
                    {requestPreview.params?.ensemble_method && requestPreview.params.ensemble_method !== 'none' && (
                      <Tag color="purple" className="!mr-0 font-mono text-[10px]">
                        {requestPreview.params.ensemble_method}
                      </Tag>
                    )}
                  </div>
                </div>
                <div className="rounded-xl bg-slate-50 p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-400 font-semibold mb-1">特征与基准</div>
                  <div className="text-slate-700 font-medium truncate">
                    {requestPreview.selectedFeatures?.length || 0} 个因子 · {requestPreview.context?.benchmark || '000300.SH'}
                  </div>
                  {(factorFilter?.enabled ?? DEFAULT_FACTOR_FILTER.enabled) ? (
                    <div className="mt-1 text-[10px] text-amber-600">
                      将按 IC/ICIR 筛选（|IC|≥{factorFilter?.icThreshold ?? DEFAULT_FACTOR_FILTER.icThreshold} · top-{factorFilter?.nTop ?? DEFAULT_FACTOR_FILTER.nTop}）
                    </div>
                  ) : (
                    <div className="mt-1 text-[10px] text-emerald-600">未启用筛选，全部特征将直接入模</div>
                  )}
                </div>
                <div className="rounded-xl bg-slate-50 p-2.5 border border-slate-100 col-span-2">
                  <div className="text-[10px] text-slate-400 font-semibold mb-1">样本切分区间</div>
                  <div className="text-slate-600 font-mono text-[11px] truncate">
                    {formatDateOnly(requestPreview.timePeriods?.train?.[0])} ~ {formatDateOnly(requestPreview.timePeriods?.test?.[1])}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </Card>

        {runtimeOptions && (
          <div className="space-y-4">
            {runtimeOptions}
          </div>
        )}
      </div>

      {/* 右侧：请求预览与运行日志 */}
      <div className="space-y-4">
        <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="flex items-center gap-2">
                <FileText size={18} className="text-blue-500" />
                <Typography.Title level={4} className="!mb-0 !text-slate-900">
                  训练编排详情
                </Typography.Title>
              </div>
              <Typography.Paragraph className="!mb-0 !mt-2 !text-xs !text-slate-500 leading-relaxed">
                请求预览与实时运行日志集中呈现，便于核实参数与排查问题。
              </Typography.Paragraph>
            </div>
            <div className="flex items-center gap-2">
              {activeTab === 'request' ? (
                <Button
                  size="small"
                  icon={<Copy size={13} />}
                  onClick={handleCopyJson}
                  className="rounded-xl text-xs font-medium h-7"
                >
                  复制 JSON
                </Button>
              ) : (
                <div className="flex items-center gap-1.5">
                  <Button
                    size="small"
                    icon={<ArrowDown size={13} />}
                    onClick={scrollToBottom}
                    className="rounded-xl text-xs font-medium h-7"
                  >
                    到底部
                  </Button>
                  <Button
                    size="small"
                    icon={<Copy size={13} />}
                    onClick={handleCopyLogs}
                    className="rounded-xl text-xs font-medium h-7"
                  >
                    复制日志
                  </Button>
                </div>
              )}
            </div>
          </div>

          <Divider className="my-4" />

          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            items={[
              {
                key: 'request',
                label: (
                  <span className="flex items-center gap-1.5 text-xs font-semibold">
                    <FileText size={14} />
                    请求预览
                    <Tag className="!mr-0 !ml-1 rounded-full text-[10px] bg-slate-100 text-slate-600 border-none">
                      {requestPreview.selectedFeatures?.length || 0} 特征
                    </Tag>
                  </span>
                ),
                children: (
                  <pre className="h-[440px] overflow-auto rounded-2xl border border-slate-200 bg-slate-50/80 p-4 font-mono text-[11px] leading-5 text-slate-700 custom-scrollbar select-text">
                    {JSON.stringify(requestPreview, null, 2)}
                  </pre>
                ),
              },
              {
                key: 'logs',
                label: (
                  <span className="flex items-center gap-1.5 text-xs font-semibold">
                    <Terminal size={14} />
                    运行日志
                    {logs.length > 0 && (
                      <Tag className="!mr-0 !ml-1 rounded-full text-[10px] bg-blue-100 text-blue-600 border-none font-mono">
                        {logs.length}
                      </Tag>
                    )}
                    {trainingStatus === 'running' && (
                      <span className="relative flex h-2 w-2 ml-1">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
                      </span>
                    )}
                  </span>
                ),
                children: (
                  <div
                    ref={logsContainerRef}
                    className="h-[440px] overflow-y-auto rounded-2xl border border-slate-800 bg-slate-950 p-4 font-mono text-[11.5px] leading-relaxed text-slate-200 custom-scrollbar select-text"
                  >
                    {logs.length === 0 ? (
                      <div className="flex h-full items-center justify-center text-slate-500">
                        <Empty
                          description={<span className="text-slate-500 text-xs">暂无日志，点击顶部“开始训练”后将实时输出</span>}
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                        />
                      </div>
                    ) : (
                      <div className="space-y-1">
                        {logs.map((log, index) => {
                          const isError = /error|fail|exception/i.test(log);
                          const isWarning = /warn/i.test(log);
                          const isSuccess = /success|completed|done/i.test(log);
                          return (
                            <div
                              key={`${log}-${index}`}
                              className={clsx(
                                'flex gap-2 break-all py-0.5 border-b border-slate-900/50 last:border-none',
                                isError
                                  ? 'text-red-400 bg-red-950/20'
                                  : isWarning
                                    ? 'text-amber-300'
                                    : isSuccess
                                      ? 'text-emerald-400'
                                      : 'text-slate-300'
                              )}
                            >
                              <span className="text-slate-500 select-none shrink-0">{log.slice(0, 10)}</span>
                              <span className="flex-1">{log.slice(11) || log}</span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                ),
              },
            ]}
          />
        </Card>
      </div>
    </div>
  );
};

