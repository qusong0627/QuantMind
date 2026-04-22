import React from 'react';
import { Card, Divider, Alert, Progress, Tabs, Empty, Typography, Button } from 'antd';
import { Play, FileText, LayoutGrid } from 'lucide-react';
import { clsx } from 'clsx';
import { useNavigate } from 'react-router-dom';
import { 
  TrainingStatus, 
  TrainingResult, 
  TrainingRequestPayload,
  daysBetween
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
}) => {
  const navigate = useNavigate();

  return (
    <div className="space-y-4">
      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="第四步：执行训练"
          desc="顶部工具栏统一承载训练操作，这里只保留状态、进度和编排详情，避免重复按钮干扰。"
          icon={<Play size={18} className="text-indigo-500" />}
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
                    : '任务将依次完成特征校验、标签构建、训练、验证和元数据打包。')
                : trainingStatus === 'completed'
                  ? '训练编排已完成，结果摘要会同步到模型管理页。'
                  : '确认配置无误后点击“开始训练”。'
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
            <div className="flex justify-end">
              <Button
                type="primary"
                size="large"
                icon={<LayoutGrid size={16} />}
                className="rounded-xl h-10 px-6 bg-emerald-600 border-none font-bold shadow-lg shadow-emerald-200"
                onClick={() => navigate('/model-registry')}
              >
                前往模型管理中心
              </Button>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2">
            <MetricCard
              label="请求状态"
              value={
                trainingStatus === 'running'
                  ? (backendRunStatus === 'waiting_callback' ? '等待回调' : '编排中')
                  : trainingStatus === 'completed'
                    ? '已完成'
                    : '待开始'
              }
              hint={`后端状态：${backendRunStatus || 'draft'} | T+${target.horizonDays} · ${target.mode}`}
              centered
            />
            <MetricCard label="总样本周期" value={`${totalDays} 天`} hint={`训练/验证/测试：${trainDays}/${valDays}/${testDays}`} centered />
          </div>

          <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
            <div className="flex items-center justify-between text-xs font-semibold text-slate-600">
              <span>执行进度 · {executionStage}</span>
              <span>{trainingStatus === 'draft' ? '未开始' : `${progress}%`}</span>
            </div>
            <Progress percent={progress} showInfo={false} className="mt-2" strokeColor="#4f46e5" />
          </div>
        </div>
      </Card>

      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="训练编排详情"
          desc="请求预览和运行日志分开展示，减少重复信息干扰。"
          icon={<FileText size={18} className="text-indigo-500" />}
        />
        <Divider className="my-4" />
        <Tabs
          defaultActiveKey="request"
          items={[
            {
              key: 'request',
              label: '请求预览',
              children: (
                <pre className="max-h-[420px] overflow-auto rounded-2xl border border-gray-200 bg-gray-50 p-4 text-[11px] leading-5 text-gray-700">
                  {JSON.stringify(requestPreview, null, 2)}
                </pre>
              ),
            },
            {
              key: 'logs',
              label: '运行日志',
              children: (
                <div className="min-h-56 rounded-2xl border border-gray-200 bg-white p-4 font-mono text-[12px] text-gray-700">
                  {logs.length === 0 ? (
                    <div className="flex h-48 items-center justify-center text-gray-500">
                      <Empty
                        description={<span className="text-gray-500">等待训练开始</span>}
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                      />
                    </div>
                  ) : (
                    <div className="space-y-1">
                      {logs.map((log, index) => (
                        <div key={`${log}-${index}`} className="flex gap-2 break-all">
                          <span className="text-gray-400">{log.slice(0, 10)}</span>
                          <span>{log.slice(11)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
};
