import React, { useCallback, useEffect, useState } from 'react';
import dayjs from 'dayjs';
import {
  Button,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ArrowLeft,
  History,
  RefreshCw,
} from 'lucide-react';
import {
  modelTrainingService,
  type ModelTrainingRunStatus,
  type TrainingRunHistoryItem,
} from '../../services/modelTrainingService';
import { TrainingRunDetailTabs } from './TrainingRunDetailTabs';

interface TrainingRunHistoryProps {
  open: boolean;
  onClose: () => void;
}

const STATUS_META: Record<string, { color: string; label: string }> = {
  completed: { color: 'green', label: '已完成' },
  failed: { color: 'red', label: '失败' },
  cancelled: { color: 'default', label: '已取消' },
  running: { color: 'blue', label: '训练中' },
  provisioning: { color: 'geekblue', label: '准备中' },
  pending: { color: 'gold', label: '排队中' },
  waiting_callback: { color: 'cyan', label: '等待回调' },
};

const MARKET_LABEL: Record<string, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
  CRYPTO: '区块链',
  FUTURES: '期货',
  CUSTOM: '自定义',
};

const fmtPct = (value: number | null | undefined, digits = 2): string =>
  typeof value === 'number' && Number.isFinite(value)
    ? `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`
    : '—';

const fmtNum = (value: number | null | undefined, digits = 3): string =>
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';

const fmtDuration = (seconds: number | null | undefined): string => {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
};

const fmtTime = (value: string | null | undefined): string =>
  value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '—';

const StatusBadge: React.FC<{ status: string; progress?: number }> = ({ status, progress }) => {
  const meta = STATUS_META[status] || { color: 'default', label: status || '未知' };
  return (
    <div className="flex flex-col items-start gap-1">
      <Tag bordered={false} color={meta.color} className="!m-0 !rounded-full !px-2.5 !text-[10px] !font-bold">
        {meta.label}
      </Tag>
      {status === 'running' ? (
        <div className="h-1 w-14 overflow-hidden rounded-full bg-slate-100">
          <div
            className="h-full rounded-full bg-blue-500 transition-all"
            style={{ width: `${Math.min(100, Math.max(0, progress || 0))}%` }}
          />
        </div>
      ) : null}
    </div>
  );
};

export const TrainingRunHistory: React.FC<TrainingRunHistoryProps> = ({ open, onClose }) => {
  const [items, setItems] = useState<TrainingRunHistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [marketFilter, setMarketFilter] = useState<string>('');
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);

  const [selected, setSelected] = useState<TrainingRunHistoryItem | null>(null);
  const [detail, setDetail] = useState<ModelTrainingRunStatus | null>(null);
  const [runRequest, setRunRequest] = useState<Record<string, unknown> | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true);
    modelTrainingService
      .listTrainingRuns({
        status: statusFilter || undefined,
        market: marketFilter || undefined,
        q: query || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      })
      .then((res) => {
        if (!alive) return;
        setItems(res.items || []);
        setTotal(Number(res.total || 0));
      })
      .catch((err: unknown) => {
        if (!alive) return;
        message.error(err instanceof Error ? `训练记录加载失败：${err.message}` : '训练记录加载失败');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, page, pageSize, statusFilter, marketFilter, query, refreshKey]);

  const openDetail = useCallback(async (item: TrainingRunHistoryItem) => {
    setSelected(item);
    setDetail(null);
    setRunRequest(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      const [status, request] = await Promise.all([
        modelTrainingService.getTrainingRun(item.run_id),
        modelTrainingService
          .getTrainingRunRequest(item.run_id)
          .catch(() => null),
      ]);
      setDetail(status);
      setRunRequest(request?.request || null);
    } catch (err: unknown) {
      setDetailError(err instanceof Error ? err.message : '训练详情加载失败');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const pageCompleted = items.filter((item) => item.status === 'completed').length;
  const pageWithStrict = items.filter((item) => item.strict?.status === 'ready').length;

  const columns: ColumnsType<TrainingRunHistoryItem> = [
    {
      title: '训练时间',
      dataIndex: 'created_at',
      width: 152,
      render: (_: unknown, row) => (
        <div className="leading-tight">
          <div className="text-xs font-bold tabular-nums text-slate-800">{fmtTime(row.created_at)}</div>
          <div className="mt-1 text-[10px] text-slate-400">耗时 {fmtDuration(row.duration_seconds)}</div>
        </div>
      ),
    },
    {
      title: '模型',
      dataIndex: 'display_name',
      render: (_: unknown, row) => (
        <div className="min-w-0">
          <div className="truncate text-[13px] font-bold text-slate-900">{row.display_name}</div>
          <div className="mt-0.5 truncate font-mono text-[10px] text-slate-400">{row.run_id}</div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            <Tag bordered={false} className="!m-0 !rounded-md !bg-slate-100 !px-1.5 !text-[10px] !text-slate-600">
              {row.model_type || '—'}
            </Tag>
            <Tag bordered={false} color="blue" className="!m-0 !rounded-md !px-1.5 !text-[10px]">
              {MARKET_LABEL[row.market] || row.market || '—'}
            </Tag>
            {row.target_horizon_days ? (
              <Tag bordered={false} color="purple" className="!m-0 !rounded-md !px-1.5 !text-[10px]">
                T+{row.target_horizon_days}
              </Tag>
            ) : null}
            <span className="text-[10px] text-slate-400">{row.feature_count} 特征</span>
          </div>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 92,
      render: (value: string, row) => <StatusBadge status={value} progress={row.progress} />,
    },
    {
      title: '测试表现',
      width: 236,
      render: (_: unknown, row) => {
        const strict = row.strict;
        return (
          <div className="leading-tight">
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-black tabular-nums text-slate-800">
                {fmtNum(row.metrics.test_rank_ic)}
              </span>
              <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">RankIC</span>
              <span className="text-[10px] text-slate-400">ICIR {fmtNum(row.metrics.test_icir, 2)}</span>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px]">
              {strict ? (
                <>
                  <span className="rounded-md bg-emerald-50 px-1.5 py-0.5 font-semibold tabular-nums text-emerald-600">
                    Top20 25bp {fmtPct(strict.total_25bp)}
                  </span>
                  {strict.excess_hs300 != null ? (
                    <span className="rounded-md bg-indigo-50 px-1.5 py-0.5 font-semibold tabular-nums text-indigo-600">
                      超额 {fmtPct(strict.excess_hs300)}
                    </span>
                  ) : null}
                  {strict.status === 'ready' ? (
                    <Tag bordered={false} color="green" className="!m-0 !rounded-md !px-1.5 !text-[10px]">
                      严格评估
                    </Tag>
                  ) : strict.status === 'running' ? (
                    <Tag bordered={false} color="blue" className="!m-0 !rounded-md !px-1.5 !text-[10px]">
                      评估中
                    </Tag>
                  ) : null}
                </>
              ) : (
                <span className="text-[10px] text-slate-300">严格评估未生成</span>
              )}
            </div>
          </div>
        );
      },
    },
    {
      title: '日志',
      width: 74,
      align: 'center',
      render: (_: unknown, row) =>
        row.has_logs ? (
          <Tooltip title={`${row.logs_chars.toLocaleString()} 字符`}>
            <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-slate-500">
              {(row.logs_chars / 1024).toFixed(0)}KB
            </span>
          </Tooltip>
        ) : (
          <span className="text-[10px] text-slate-300">—</span>
        ),
    },
    {
      title: '操作',
      width: 156,
      align: 'right',
      render: (_: unknown, row) => (
        <Space size={4}>
          <Button
            size="small"
            type="primary"
            className="!h-7 !rounded-lg !px-2.5 !text-[11px] !font-semibold"
            disabled={!row.has_result}
            onClick={(event) => {
              event.stopPropagation();
              void openDetail(row);
            }}
          >
            查看结果
          </Button>
          <Button
            size="small"
            className="!h-7 !rounded-lg !px-2.5 !text-[11px]"
            disabled={!row.has_logs && !row.has_result}
            onClick={(event) => {
              event.stopPropagation();
              void openDetail(row);
            }}
          >
            详情
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Modal
        open={open}
        onCancel={onClose}
        footer={null}
        width="min(1320px, calc(100vw - 32px))"
        centered
        destroyOnClose
        className="training-history-modal"
        styles={{ body: { paddingTop: 8 } }}
        title={
          <div className="flex flex-wrap items-center gap-3 py-1">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-indigo-500 to-blue-600 text-white shadow-sm">
              <History size={18} />
            </span>
            <span className="flex min-w-0 flex-col">
              <span className="text-base font-black text-slate-900">训练记录</span>
              <span className="text-[11px] font-normal text-slate-400">
                每次训练的结果页、日志与提交配置都会保留；点击任意一行打开当时的结果页
              </span>
            </span>
            <span className="ml-auto flex items-center gap-2 text-[11px] text-slate-400">
              <Tag bordered={false} className="!m-0 !rounded-full !bg-slate-100 !px-2.5 !text-[11px] !text-slate-600">
                共 {total} 次
              </Tag>
              <span>本页 完成 {pageCompleted} · 严格评估 {pageWithStrict}</span>
            </span>
          </div>
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-2xl bg-slate-50/80 px-3 py-2.5">
          <Select
            size="small"
            allowClear
            placeholder="状态"
            className="min-w-[112px]"
            value={statusFilter || undefined}
            onChange={(value) => {
              setStatusFilter(value || '');
              setPage(1);
            }}
            options={Object.entries(STATUS_META).map(([value, meta]) => ({
              value,
              label: meta.label,
            }))}
          />
          <Select
            size="small"
            allowClear
            placeholder="市场"
            className="min-w-[112px]"
            value={marketFilter || undefined}
            onChange={(value) => {
              setMarketFilter(value || '');
              setPage(1);
            }}
            options={Object.entries(MARKET_LABEL).map(([value, label]) => ({ value, label }))}
          />
          <Input.Search
            size="small"
            allowClear
            className="w-64"
            placeholder="搜索 run_id / 模型名"
            value={queryInput}
            onChange={(event) => setQueryInput(event.target.value)}
            onSearch={(value) => {
              setQuery(value.trim());
              setPage(1);
            }}
            enterButton
          />
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            className="!h-7 !rounded-lg"
            onClick={() => setRefreshKey((key) => key + 1)}
          >
            刷新
          </Button>
          <span className="ml-auto text-[11px] text-slate-400">日志与完整结果按需加载</span>
        </div>
        <Table<TrainingRunHistoryItem>
          size="small"
          rowKey="run_id"
          loading={loading}
          columns={columns}
          dataSource={items}
          locale={{ emptyText: <Empty description="还没有训练记录" /> }}
          rowClassName="transition-colors hover:bg-indigo-50/40"
          onRow={(row) => ({
            onClick: () => void openDetail(row),
            style: { cursor: 'pointer' },
          })}
          pagination={{
            current: page,
            pageSize,
            total,
            size: 'small',
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showQuickJumper: total > pageSize * 5,
            showTotal: (value) => `共 ${value} 条`,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage);
              setPageSize(nextSize);
            },
          }}
        />
      </Modal>

      <Modal
        open={Boolean(selected)}
        onCancel={() => setSelected(null)}
        footer={null}
        width="min(1400px, calc(100vw - 24px))"
        centered
        destroyOnClose
        className="training-run-detail-modal"
        styles={{ body: { paddingTop: 4, maxHeight: '76vh', overflowY: 'auto' } }}
        title={
          selected ? (
            <div className="flex flex-wrap items-center gap-2 py-1">
              <Button
                size="small"
                type="text"
                icon={<ArrowLeft size={14} />}
                className="!rounded-lg !font-semibold"
                onClick={() => setSelected(null)}
              >
                返回列表
              </Button>
              <span className="min-w-0 truncate text-[15px] font-black text-slate-900">
                {selected.display_name}
              </span>
              <Tag bordered={false} className="!m-0 !rounded-md !bg-slate-100 font-mono !text-[10px] !text-slate-500">
                {selected.run_id}
              </Tag>
              <StatusBadge status={selected.status} progress={selected.progress} />
              <span className="text-[11px] text-slate-400">
                {MARKET_LABEL[selected.market] || selected.market || '—'}
                {selected.target_horizon_days ? ` · T+${selected.target_horizon_days}` : ''}
                {` · ${selected.feature_count} 特征`}
                {` · 耗时 ${fmtDuration(selected.duration_seconds)}`}
                {` · ${fmtTime(selected.created_at)}`}
              </span>
            </div>
          ) : null
        }
      >
        <TrainingRunDetailTabs
          runId={selected?.run_id}
          meta={selected ?? {}}
          detail={detail}
          runRequest={runRequest}
          loading={detailLoading}
          error={detailError}
        />
      </Modal>
    </>
  );
};

export default TrainingRunHistory;
