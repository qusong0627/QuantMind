import React, { useEffect, useState, useCallback, useMemo } from 'react';
import {
  Button, Tag, Typography, Empty, Spin, Modal, Select,
} from 'antd';
import { clsx } from 'clsx';
import { Trash2, ChevronLeft, ChevronRight } from 'lucide-react';
import { modelTrainingService, InferenceRunRecord, InferenceRankingResult } from '../../services/modelTrainingService';
import { InferenceRunDetailView } from './InferenceRunDetailView';

const { Text } = Typography;

interface Props {
  modelId: string;
  onDelete: (runId: string) => Promise<void> | void;
}

const STATUS_META: Record<string, { color: string; label: string }> = {
  completed: { color: 'green', label: '成功' },
  running: { color: 'processing', label: '运行中' },
  failed: { color: 'error', label: '失败' },
};

/** 排序口径：与旧表格的两个可排序列一致（推理日期 / 信号数），在此按当前页客户端排序 */
type SortKey = 'date_desc' | 'date_asc' | 'signals_desc';

const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: 'date_desc', label: '推理日期 ↓' },
  { value: 'date_asc', label: '推理日期 ↑' },
  { value: 'signals_desc', label: '信号数 ↓' },
];

const PAGE_SIZE_OPTIONS = ['10', '20', '50', '100'];

/** 一个统计格：标签 + 等宽数值 */
const Stat: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <span className="inline-flex items-baseline gap-0.5">
    <span className="text-[10px] text-slate-400">{label}</span>
    {value}
  </span>
);

/** 分数分布计数展示：null 显示占位 */
const DistCount: React.FC<{ value: number | null | undefined; className?: string }> = ({ value, className }) => (
  <Text className={clsx('text-[11px] font-mono font-bold', className || 'text-slate-600')}>
    {value === null || value === undefined ? '—' : value}
  </Text>
);

/** 截面分数均值：≥0 红（与排名分数配色一致），<0 绿 */
const ScoreMean: React.FC<{ value: number | null | undefined }> = ({ value }) => (
  <Text
    className={clsx(
      'text-[11px] font-mono font-bold',
      value === null || value === undefined
        ? 'text-slate-400'
        : Number(value) >= 0 ? 'text-rose-600' : 'text-emerald-600',
    )}
  >
    {value === null || value === undefined ? '—' : Number(value).toFixed(4)}
  </Text>
);

export const InferenceHistoryPanel: React.FC<Props> = ({ modelId, onDelete }) => {
  const [items, setItems] = useState<InferenceRunRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sortKey, setSortKey] = useState<SortKey>('date_desc');
  const [loading, setLoading] = useState(false);
  // 详情视图状态：null = 列表，非 null = 正在查看该 run 的详情（弹窗承载，避免挤在窄栏里）
  const [viewingRunId, setViewingRunId] = useState<string | null>(null);
  const [detailResult, setDetailResult] = useState<InferenceRankingResult | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await modelTrainingService.listInferenceHistory(modelId, {
        page,
        pageSize,
      });
      setItems(resp.items);
      setTotal(resp.total);
    } catch {
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [modelId, page, pageSize]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadDetail = useCallback(async (runId: string) => {
    setViewingRunId(runId);
    setDetailResult(null);
    setDetailLoading(true);
    try {
      const r = await modelTrainingService.getInferenceResult(runId);
      setDetailResult(r);
    } catch {
      setDetailResult(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // 按推理日期导航：用该日期的 run 加载详情（±1 天切换 / 指定日期）
  const navigateToDate = useCallback(async (inferenceDate: string) => {
    if (viewingRunId === null) return;
    setDetailLoading(true);
    try {
      const resp = await modelTrainingService.listInferenceHistory(modelId, {
        inferenceDate,
        page: 1,
        pageSize: 1,
      });
      const target = resp.items?.[0];
      if (target?.run_id) {
        setViewingRunId(target.run_id);
        const r = await modelTrainingService.getInferenceResult(target.run_id);
        setDetailResult(r);
      } else {
        setDetailResult(null);
      }
    } catch {
      setDetailResult(null);
    } finally {
      setDetailLoading(false);
    }
  }, [modelId, viewingRunId]);

  const handleBackToList = useCallback(() => {
    setViewingRunId(null);
    setDetailResult(null);
  }, []);

  const handleDelete = async (run: InferenceRunRecord) => {
    await onDelete(run.run_id);
    void load();
  };

  const sorted = useMemo(() => {
    const copy = [...items];
    if (sortKey === 'signals_desc') return copy.sort((a, b) => (b.signals_count || 0) - (a.signals_count || 0));
    const dir = sortKey === 'date_asc' ? 1 : -1;
    return copy.sort((a, b) =>
      dir * (a.inference_date || '').localeCompare(b.inference_date || ''),
    );
  }, [items, sortKey]);

  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="h-full min-h-0 flex flex-col">
      {/* 排序 + 总数：紧凑一条 */}
      <div className="shrink-0 h-8 px-1 flex items-center gap-2 border-b border-slate-100">
        <span className="text-[11px] text-slate-400">共 <strong className="font-mono text-slate-600">{total}</strong> 条</span>
        <Select
          size="small"
          value={sortKey}
          onChange={setSortKey}
          options={SORT_OPTIONS}
          className="ml-auto w-[104px]"
        />
      </div>

      <Spin spinning={loading} wrapperClassName="flex-1 min-h-0" className="h-full">
        {items.length === 0 && !loading ? (
          <div className="h-full flex items-center justify-center">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<span className="text-xs text-slate-400 font-medium">暂无推理历史记录</span>}
            />
          </div>
        ) : (
          <div className="h-full overflow-y-auto custom-scrollbar divide-y divide-slate-100">
            {sorted.map((r) => {
              const meta = STATUS_META[r.status] || { color: 'default', label: r.status };
              const dist = r.score_distribution;
              return (
                <div
                  key={r.run_id}
                  role="button"
                  tabIndex={0}
                  onClick={() => void loadDetail(r.run_id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      void loadDetail(r.run_id);
                    }
                  }}
                  className="group px-2 py-1.5 hover:bg-blue-50/40 cursor-pointer transition-colors"
                >
                  <div className="flex items-center gap-2">
                    <Text className="text-xs font-mono font-bold text-slate-700 shrink-0">
                      {r.inference_date || r.data_trade_date || '—'}
                    </Text>
                    {r.calendar_adjusted && r.requested_inference_date && r.requested_inference_date !== r.inference_date && (
                      <Text className="text-[10px] text-amber-500 shrink-0" title="原请求日期非交易日，已回退">
                        原 {r.requested_inference_date}
                      </Text>
                    )}
                    <Text className="text-[10px] text-slate-400 font-mono shrink-0">
                      → {r.target_date || r.prediction_trade_date || '—'}
                    </Text>

                    <Tag color={meta.color} className="m-0 border-0 text-[10px] font-bold px-1.5 py-0 rounded shrink-0 leading-tight">
                      {meta.label}
                    </Tag>

                    <span className="ml-auto flex items-center gap-1.5 shrink-0">
                      <Text className="text-[11px] font-mono font-bold text-slate-600">
                        {r.signals_count || '—'}
                      </Text>
                      <Text className="text-[10px] text-slate-400">信号</Text>
                      <Button
                        size="small"
                        type="text"
                        danger
                        icon={<Trash2 size={12} />}
                        title="删除该次推理记录"
                        className="p-0 h-5 w-5 min-w-0 flex items-center justify-center opacity-0 group-hover:opacity-70 hover:!opacity-100"
                        onClick={(e) => { e.stopPropagation(); void handleDelete(r); }}
                      />
                    </span>
                  </div>

                  {/* 分数分布：原先的「正分/负分/平分/平均分」四列，压成一行 */}
                  <div className="mt-0.5 flex items-center gap-3">
                    <Stat label="正" value={<DistCount value={dist?.positive_count} className="text-rose-600" />} />
                    <Stat label="负" value={<DistCount value={dist?.negative_count} className="text-emerald-600" />} />
                    <Stat label="平" value={<DistCount value={dist?.zero_count} />} />
                    <Stat label="均" value={<ScoreMean value={dist?.mean} />} />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Spin>

      {/* 分页：总数、翻页、每页条数（原表格分页配置完整保留） */}
      {total > 0 && (
        <div className="shrink-0 h-8 px-1 flex items-center gap-2 border-t border-slate-100">
          <span className="text-[11px] font-mono text-slate-500">{page} / {pageCount}</span>
          <Button
            size="small" type="text" className="p-0 h-5 w-5 min-w-0"
            icon={<ChevronLeft size={13} />}
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
          />
          <Button
            size="small" type="text" className="p-0 h-5 w-5 min-w-0"
            icon={<ChevronRight size={13} />}
            disabled={page >= pageCount}
            onClick={() => setPage(page + 1)}
          />
          <Select
            size="small"
            value={String(pageSize)}
            onChange={(v) => { setPageSize(Number(v)); setPage(1); }}
            options={PAGE_SIZE_OPTIONS.map((v) => ({ value: v, label: `${v} 条/页` }))}
            className="ml-auto w-[96px]"
          />
        </div>
      )}

      {/* 详情：宽弹窗承载（详情视图按视口断点排版，放进 520px 窄栏会被压扁） */}
      <Modal
        open={viewingRunId !== null}
        onCancel={handleBackToList}
        footer={null}
        width={1160}
        centered
        destroyOnClose
        // 不要弹窗自带标题栏：详情视图自己的工具条已经写了批次与目标日，
        // 再叠一条标题就是重复的顶部空间。关闭 X 仍在右上角浮动。
        title={null}
        styles={{ body: { padding: 0, maxHeight: '84vh', overflowY: 'auto' } }}
      >
        {viewingRunId && (
          <InferenceRunDetailView
            runId={viewingRunId}
            result={detailResult}
            loading={detailLoading}
            onBack={handleBackToList}
            onRetry={() => void loadDetail(viewingRunId)}
            onNavigateDate={(date) => void navigateToDate(date)}
          />
        )}
      </Modal>
    </div>
  );
};
