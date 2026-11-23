/**
 * WP7 数据审计卡：未来列 / 标签契约 / trade_mask / ST / 退市 / 指纹 / 季度 PIT 探针。
 *
 * 训练溯源页与模型详情页共用同一实现（详情页放在样本时间轴上方）。
 */
import React from 'react';
import { Alert, Button, Card, Tag, message } from 'antd';
import { clsx } from 'clsx';
import { modelTrainingService, type TrainingAuditRow } from '../../services/modelTrainingService';
import { useAuth } from '../../features/auth/hooks';
import { REPORT_POLL_INTERVAL_MS, REPORT_POLL_MAX_ATTEMPTS, extractHttpStatus } from './reportPolling';

export const AuditSection: React.FC<{ runId?: string }> = ({ runId }) => {
  const [loading, setLoading] = React.useState(false);
  const [row, setRow] = React.useState<TrainingAuditRow | null>(null);
  const [refreshKey, setRefreshKey] = React.useState(0);
  const [submitting, setSubmitting] = React.useState(false);
  const [pending, setPending] = React.useState(false);
  const pollCountRef = React.useRef(0);
  const { user } = useAuth();
  const isAdmin = Boolean(user?.is_admin);

  const reload = React.useCallback(() => setRefreshKey((key) => key + 1), []);

  React.useEffect(() => {
    if (!runId) {
      setRow(null);
      setPending(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    modelTrainingService
      .getTrainingAuditReport(runId)
      .then((data) => {
        if (cancelled) return;
        setRow(data);
        if (data) setPending(false);
      })
      .catch(() => {
        if (!cancelled) setRow(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, refreshKey]);

  // 数据审计只在完成时落库（没有 running 行），轮询只由 pending 驱动。
  React.useEffect(() => {
    if (!pending) {
      pollCountRef.current = 0;
      return;
    }
    if (pollCountRef.current >= REPORT_POLL_MAX_ATTEMPTS) return;
    const timer = window.setTimeout(() => {
      pollCountRef.current += 1;
      reload();
    }, REPORT_POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [pending, refreshKey, reload]);

  const submitRecompute = async () => {
    if (!runId || submitting) return;
    setSubmitting(true);
    try {
      await modelTrainingService.recomputeTrainingAuditReport(runId);
      message.success('已提交重算，后台计算中…');
      pollCountRef.current = 0;
      setPending(true);
      reload();
    } catch (err: unknown) {
      message.error(
        extractHttpStatus(err) === 403 ? '需要管理员权限才能重算数据审计' : '提交重算失败，请稍后重试',
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (!runId) return null;

  const levelTone: Record<string, string> = {
    pass: 'bg-emerald-50 text-emerald-600',
    fail: 'bg-rose-50 text-rose-500',
    warn: 'bg-amber-50 text-amber-600',
    info: 'bg-slate-100 text-slate-500',
  };

  return (
    <Card
      className="rounded-2xl border-slate-200"
      size="small"
      title={
        <span className="text-sm font-black text-slate-800">
          数据审计
          <span className="ml-2 text-[10px] font-normal text-slate-400">
            PIT / 泄漏 / 可成交 / 复现
          </span>
        </span>
      }
    >
      {pending ? (
        <div className="py-4 text-center text-xs text-slate-500">
          已提交重算，后台计算中…
          <Button type="link" size="small" className="!px-2 !text-xs" onClick={reload}>
            刷新
          </Button>
        </div>
      ) : loading ? (
        <div className="py-4 text-center text-xs text-slate-500">正在加载数据审计…</div>
      ) : null}
      {!loading && !pending && (!row || row.status === 'failed') ? (
        <Alert
          type={row?.status === 'failed' ? 'error' : 'info'}
          showIcon
          className="rounded-2xl"
          message={row?.status === 'failed' ? '数据审计失败' : '数据审计尚未生成'}
          description={
            <div className="space-y-2">
              <div>{row?.error || '训练完成后会自动运行；老模型可在这里直接重算。'}</div>
              {isAdmin ? (
                <Button
                  size="small"
                  type="primary"
                  loading={submitting}
                  onClick={() => void submitRecompute()}
                >
                  重算数据审计
                </Button>
              ) : (
                <span className="text-[11px] text-slate-400">重算需要管理员权限。</span>
              )}
            </div>
          }
        />
      ) : null}
      {!loading && row?.status === 'ready' && row.report ? (
        <div className="space-y-2">
          {row.report.blocked ? (
            <Alert
              type="error"
              showIcon
              className="rounded-2xl"
              message="审计未通过：该模型不可晋升"
              description="存在 fail 级检查（见下方明细）。"
            />
          ) : null}
          <div className="grid gap-2 md:grid-cols-2">
            {(row.report.checks || []).map((check) => (
              <div key={check.id} className="flex items-start gap-2 text-xs">
                <Tag
                  className={clsx(
                    'm-0 shrink-0 rounded-full border-0 px-2 py-0 text-[10px]',
                    levelTone[check.status === 'fail' ? 'fail' : check.level] || levelTone.info,
                  )}
                >
                  {check.status === 'fail' ? '未通过' : check.level === 'info' ? '信息' : '通过'}
                </Tag>
                <div className="min-w-0">
                  <span className="font-mono text-[11px] text-slate-600">{check.id}</span>
                  <span className="ml-2 text-slate-500">{check.detail}</span>
                </div>
              </div>
            ))}
          </div>
          {row.report.warnings?.length ? (
            <div className="rounded-xl bg-amber-50/60 p-2 text-[11px] text-amber-700">
              {row.report.warnings.map((w, i) => (
                <div key={i}>• {w}</div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
};

export default AuditSection;
