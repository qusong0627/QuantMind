import React, { useEffect, useState, useCallback, useRef } from 'react';
import {
  Button, Card, Tag, Typography, Empty, Spin, Progress, Tabs,
  Table, Tooltip, InputNumber, Select, DatePicker, message, Space, Divider,
} from 'antd';
import { clsx } from 'clsx';
import dayjs from 'dayjs';
import {
  Play, RefreshCw, TrendingUp, TrendingDown, BarChart3,
  Info, AlertCircle, ArrowUpRight, ArrowDownRight, Layers,
  Clock, CheckCircle2, XCircle, Calendar, Zap, Activity,
} from 'lucide-react';
import {
  modelTrainingService,
  BatchInferenceRecord,
  BatchAggregateResult,
  BatchAggregateSymbol,
  BatchAggregateDaily,
} from '../../services/modelTrainingService';
import { neutralizationService } from '../../services/neutralizationService';

const { Text } = Typography;

const fmt = (n: number | null | undefined, digits = 4): string => {
  if (n === null || n === undefined || !Number.isFinite(n as number)) return '—';
  return (n as number).toFixed(digits);
};

const fmtPct = (n: number | null | undefined, digits = 1): string => {
  if (n === null || n === undefined || !Number.isFinite(n as number)) return '—';
  return `${((n as number) * 100).toFixed(digits)}%`;
};

const fmtInt = (n: number | null | undefined): string => {
  if (n === null || n === undefined) return '—';
  return Math.round(n).toLocaleString();
};

// ─── 口径说明卡 ──────────────────────────────────────────────────────────────

const CaliberCard: React.FC<{ meta: BatchAggregateResult['meta'] }> = ({ meta }) => (
  <div className="bg-amber-50/60 rounded-2xl p-4 border border-amber-100/60 mb-4">
    <div className="flex items-start gap-2.5">
      <Info size={16} className="text-amber-500 mt-0.5 shrink-0" />
      <div className="space-y-2 text-[10px] text-amber-800/80 leading-relaxed">
        <div className="flex items-center gap-4 flex-wrap">
          <span><b>窗口</b> {meta.window_days} 天</span>
          <span><b>持有期</b> T+{meta.horizon_days}</span>
          <span><b>有效独立信号</b> {meta.effective_independent_bets}</span>
          <span><b>重叠率</b> {fmt(meta.overlap_ratio, 2)}</span>
          {meta.signal_autocorr !== null && (
            <span><b>信号自相关</b> {fmt(meta.signal_autocorr, 3)}</span>
          )}
        </div>
        {meta.warnings?.length > 0 && (
          <div className="space-y-1">
            {meta.warnings.map((w, i) => (
              <div key={i} className="flex items-start gap-1.5">
                <AlertCircle size={11} className="text-amber-500 mt-0.5 shrink-0" />
                <span>{w}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  </div>
);

// ─── 排行榜表格 ──────────────────────────────────────────────────────────────

const SymbolTable: React.FC<{
  title: string;
  data: BatchAggregateSymbol[];
  side: 'long' | 'short';
}> = ({ title, data, side }) => {
  if (!data.length) {
    return (
      <div className="py-6 text-center">
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-[10px] text-slate-400">暂无数据</span>} />
      </div>
    );
  }

  const isLong = side === 'long';
  const convictionKey = isLong ? 'conviction_long' : 'conviction_short';
  const bandKey = isLong ? 'band_hits' : 'band_hits_short';

  const columns = [
    {
      title: '#',
      width: 36,
      render: (_: unknown, __: unknown, idx: number) => (
        <Text className="text-[10px] font-black text-slate-400">{idx + 1}</Text>
      ),
    },
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 80,
      render: (v: string, r: BatchAggregateSymbol) => (
        <div>
          <Text className="text-[11px] font-black text-slate-800 block">{v}</Text>
          <Text className="text-[9px] text-slate-400 block truncate">{r.stock_name || ''}</Text>
        </div>
      ),
    },
    {
      title: '信念分',
      dataIndex: convictionKey,
      width: 70,
      sorter: (a: BatchAggregateSymbol, b: BatchAggregateSymbol) =>
        (a[convictionKey] as number) - (b[convictionKey] as number),
      render: (v: number) => (
        <div className="flex items-center gap-1">
          <div className="w-8 h-1.5 bg-slate-100 rounded-full overflow-hidden">
            <div
              className={clsx('h-full rounded-full', isLong ? 'bg-blue-500' : 'bg-rose-500')}
              style={{ width: `${Math.min(v, 100)}%` }}
            />
          </div>
          <Text className="text-[10px] font-mono font-black text-slate-700">{Math.round(v)}</Text>
        </div>
      ),
    },
    {
      title: '加权分位',
      dataIndex: 'weighted_pct',
      width: 70,
      sorter: (a: BatchAggregateSymbol, b: BatchAggregateSymbol) => a.weighted_pct - b.weighted_pct,
      render: (v: number) => (
        <Text className="text-[10px] font-mono font-bold text-slate-600">{fmtPct(v)}</Text>
      ),
    },
    {
      title: '覆盖',
      dataIndex: 'coverage',
      width: 55,
      render: (v: number) => <Text className="text-[10px] font-mono text-slate-500">{fmtPct(v, 0)}</Text>,
    },
    {
      title: '上榜',
      dataIndex: isLong ? 'topk_hits' : 'bottomk_hits',
      width: 50,
      render: (v: number) => <Text className="text-[10px] font-mono text-slate-500">{v}</Text>,
    },
    {
      title: '带命中',
      dataIndex: bandKey,
      width: 50,
      render: (v: number) => <Text className="text-[10px] font-mono text-slate-500">{v}</Text>,
    },
    {
      title: '趋势',
      dataIndex: 'trend_rho',
      width: 60,
      sorter: (a: BatchAggregateSymbol, b: BatchAggregateSymbol) =>
        (a.trend_rho ?? 0) - (b.trend_rho ?? 0),
      render: (v: number | null) => {
        if (v === null || v === undefined) return <Text className="text-[10px] text-slate-300">—</Text>;
        const up = v > 0;
        return (
          <span className={clsx('text-[10px] font-mono font-bold flex items-center gap-0.5', up ? 'text-emerald-600' : 'text-rose-600')}>
            {up ? <TrendingUp size={10} /> : <TrendingDown size={10} />}
            {v.toFixed(2)}
          </span>
        );
      },
    },
    {
      title: '单调',
      width: 42,
      render: (_: unknown, r: BatchAggregateSymbol) => {
        if (r.is_monotonic_up) return <Tag color="green" className="m-0 text-[8px] px-1 border-0">↑</Tag>;
        if (r.is_monotonic_down) return <Tag color="red" className="m-0 text-[8px] px-1 border-0">↓</Tag>;
        return <Text className="text-[9px] text-slate-300">—</Text>;
      },
    },
  ];

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <div className={clsx('h-3 w-1 rounded-full', isLong ? 'bg-blue-500' : 'bg-rose-500')} />
        <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">{title}</Text>
        <Tag className="m-0 border-0 bg-slate-100 text-slate-500 text-[9px] font-bold rounded-md px-1.5">{data.length}</Tag>
      </div>
      <Table
        dataSource={data}
        columns={columns}
        rowKey="symbol"
        size="small"
        pagination={false}
        scroll={{ y: 320 }}
        className="batch-symbol-table"
        rowClassName={(_, idx) => clsx(idx % 2 === 0 ? 'bg-white' : 'bg-slate-50/30')}
      />
    </div>
  );
};

// ─── 涨跌幅榜 ────────────────────────────────────────────────────────────────

const MoversPanel: React.FC<{ movers: BatchAggregateResult['movers'] }> = ({ movers }) => {
  const [activeMetric, setActiveMetric] = useState<string>('pct_jump');

  const metric = movers[activeMetric];
  if (!metric) return null;

  const moverColumns = [
    { title: '#', width: 32, render: (_: unknown, __: unknown, i: number) => <Text className="text-[10px] font-black text-slate-400">{i + 1}</Text> },
    { title: '代码', dataIndex: 'symbol', width: 80, render: (v: string) => <Text className="text-[11px] font-black text-slate-800 font-mono">{v}</Text> },
    { title: '名称', dataIndex: 'stock_name', width: 70, render: (v: string) => <Text className="text-[10px] text-slate-500 truncate block">{v || ''}</Text> },
    {
      title: '幅度', dataIndex: 'value', width: 80,
      render: (v: number) => <Text className={clsx('text-[11px] font-mono font-bold', v >= 0 ? 'text-rose-600' : 'text-emerald-600')}>{v >= 0 ? '+' : ''}{v.toFixed(4)}</Text>,
    },
    { title: '日期', dataIndex: 'change_date', width: 80, render: (v: string) => v ? <Text className="text-[9px] text-slate-400 font-mono">{v}</Text> : <Text className="text-[9px] text-slate-300">—</Text> },
  ];

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <div className="h-3 w-1 rounded-full bg-violet-500" />
        <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">涨跌幅榜</Text>
      </div>
      <div className="flex gap-1.5 mb-3 flex-wrap">
        {Object.entries(movers).map(([key, m]) => (
          <Button
            key={key}
            size="small"
            type={activeMetric === key ? 'primary' : 'default'}
            className={clsx('rounded-lg text-[10px] font-bold h-7 px-3', activeMetric === key ? 'bg-violet-600 border-violet-600' : 'border-slate-200')}
            onClick={() => setActiveMetric(key)}
          >
            {key === 'pct_jump' ? '分位跃升' : key === 'daily_max_jump' ? '单日突变' : key === 'raw_score_change' ? '原始分差' : '名次变化'}
          </Button>
        ))}
      </div>
      {metric.warning && (
        <div className="flex items-center gap-1.5 mb-2 px-2 py-1.5 bg-amber-50 rounded-lg border border-amber-100">
          <AlertCircle size={11} className="text-amber-500" />
          <Text className="text-[9px] text-amber-700">{metric.warning}</Text>
        </div>
      )}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="flex items-center gap-1 mb-1.5">
            <ArrowUpRight size={11} className="text-rose-500" />
            <Text className="text-[10px] font-black text-rose-600 uppercase">跃升 TOP</Text>
          </div>
          <Table dataSource={metric.up} columns={moverColumns} rowKey="symbol" size="small" pagination={false} scroll={{ y: 240 }}
            rowClassName={(_, idx) => clsx(idx % 2 === 0 ? 'bg-white' : 'bg-rose-50/20')} />
        </div>
        <div>
          <div className="flex items-center gap-1 mb-1.5">
            <ArrowDownRight size={11} className="text-emerald-500" />
            <Text className="text-[10px] font-black text-emerald-600 uppercase">跌落 TOP</Text>
          </div>
          <Table dataSource={metric.down} columns={moverColumns} rowKey="symbol" size="small" pagination={false} scroll={{ y: 240 }}
            rowClassName={(_, idx) => clsx(idx % 2 === 0 ? 'bg-white' : 'bg-emerald-50/20')} />
        </div>
      </div>
    </div>
  );
};

// ─── 分数矩阵热力图 ──────────────────────────────────────────────────────────

const ScoreMatrixHeatmap: React.FC<{ data: BatchAggregateResult }> = ({ data }) => {
  const topN = 40;
  const topSymbols = data.per_symbol.slice(0, topN);
  const dates: string[] = data.meta.dates || [];
  if (!topSymbols.length || !dates.length) {
    return <Empty description={<span className="text-[10px] text-slate-400">暂无数据</span>} />;
  }

  const cellW = Math.max(32, Math.min(60, 600 / dates.length));
  const labelW = 72;

  const getColor = (pct: number): string => {
    if (pct >= 0.9) return 'bg-blue-600';
    if (pct >= 0.7) return 'bg-blue-400';
    if (pct >= 0.5) return 'bg-blue-200';
    if (pct >= 0.3) return 'bg-slate-200';
    if (pct >= 0.1) return 'bg-rose-200';
    return 'bg-rose-400';
  };

  const getTextColor = (pct: number): string => {
    if (pct >= 0.9 || pct < 0.1) return 'text-white';
    return 'text-slate-700';
  };

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <div className="h-3 w-1 rounded-full bg-blue-500" />
        <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">
          分数矩阵热力图（Top {topN} by 信念分）
        </Text>
      </div>
      <div className="overflow-x-auto">
        <div className="inline-block" style={{ minWidth: labelW + dates.length * cellW + 16 }}>
          {/* Date headers */}
          <div className="flex" style={{ paddingLeft: labelW }}>
            {dates.map(d => (
              <div key={d} style={{ width: cellW }} className="text-center py-1">
                <Text className="text-[8px] text-slate-400 font-mono font-bold">
                  {d.slice(5)}
                </Text>
              </div>
            ))}
          </div>
          {/* Rows */}
          {topSymbols.map(s => (
            <div key={s.symbol} className="flex items-center">
              <div style={{ width: labelW }} className="pr-2 text-right truncate">
                <Text className="text-[9px] font-mono font-bold text-slate-600">{s.symbol}</Text>
              </div>
              {dates.map(d => {
                const daily: BatchAggregateDaily | undefined = data.daily.find(dd => dd.trade_date === d && !dd.missing);
                const hasData = !!daily && (daily.count ?? 0) > 0;
                const pct = s.weighted_pct;
                return (
                  <Tooltip key={d} title={`${s.symbol} ${d}\n加权分位: ${fmtPct(pct)}`}>
                    <div
                      style={{ width: cellW, height: 22 }}
                      className={clsx(
                        'flex items-center justify-center text-[7px] font-mono font-bold border border-white/50',
                        hasData ? getColor(pct) : 'bg-slate-50',
                        hasData ? getTextColor(pct) : 'text-slate-300',
                      )}
                    >
                      {hasData ? (pct * 100).toFixed(0) : '—'}
                    </div>
                  </Tooltip>
                );
              })}
            </div>
          ))}
        </div>
      </div>
      {/* Legend */}
      <div className="flex items-center gap-3 mt-3 px-1">
        <Text className="text-[9px] text-slate-400 font-bold">分位:</Text>
        {[
          { label: '≥90%', color: 'bg-blue-600 text-white' },
          { label: '≥70%', color: 'bg-blue-400 text-white' },
          { label: '≥50%', color: 'bg-blue-200 text-slate-700' },
          { label: '≥30%', color: 'bg-slate-200 text-slate-700' },
          { label: '≥10%', color: 'bg-rose-200 text-slate-700' },
          { label: '<10%', color: 'bg-rose-400 text-white' },
        ].map(l => (
          <div key={l.label} className="flex items-center gap-1">
            <div className={clsx('w-3 h-3 rounded-sm', l.color)} />
            <Text className="text-[8px] text-slate-500">{l.label}</Text>
          </div>
        ))}
      </div>
    </div>
  );
};

// ─── 每日横截面统计 ──────────────────────────────────────────────────────────

const DailyStatsPanel: React.FC<{ daily: BatchAggregateResult['daily'] }> = ({ daily }) => {
  const columns = [
    { title: '日期', dataIndex: 'trade_date', width: 90, render: (v: string) => <Text className="text-[10px] font-mono font-bold text-slate-700">{v}</Text> },
    { title: '样本', dataIndex: 'count', width: 50, render: (v: number) => <Text className="text-[10px] font-mono text-slate-500">{v || '—'}</Text> },
    { title: '均值', dataIndex: 'score_mean', width: 65, render: (v: number) => <Text className="text-[10px] font-mono text-slate-500">{fmt(v, 3)}</Text> },
    { title: '标准差', dataIndex: 'score_std', width: 60, render: (v: number) => <Text className="text-[10px] font-mono text-slate-500">{fmt(v, 3)}</Text> },
    { title: 'BUY', dataIndex: 'buy_count', width: 42, render: (v: number) => <Text className="text-[10px] font-mono text-rose-500">{v ?? '—'}</Text> },
    { title: 'SELL', dataIndex: 'sell_count', width: 42, render: (v: number) => <Text className="text-[10px] font-mono text-emerald-500">{v ?? '—'}</Text> },
    {
      title: '换手率', dataIndex: 'topk_turnover', width: 65,
      render: (v: number | null) => {
        if (v === null || v === undefined) return <Text className="text-[10px] text-slate-300">—</Text>;
        const color = v > 0.7 ? 'text-rose-500' : v > 0.4 ? 'text-amber-500' : 'text-emerald-500';
        return <Text className={clsx('text-[10px] font-mono font-bold', color)}>{fmtPct(v, 0)}</Text>;
      },
    },
    { title: '共识重', dataIndex: 'consensus_overlap', width: 55, render: (v: number) => <Text className="text-[10px] font-mono text-slate-500">{v ?? '—'}</Text> },
  ];

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <div className="h-3 w-1 rounded-full bg-emerald-500" />
        <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">每日横截面统计</Text>
      </div>
      <Table
        dataSource={daily.filter(d => !d.missing)}
        columns={columns}
        rowKey="trade_date"
        size="small"
        pagination={false}
        scroll={{ y: 320 }}
        rowClassName={(_, idx) => clsx(idx % 2 === 0 ? 'bg-white' : 'bg-slate-50/30')}
      />
    </div>
  );
};

// ─── 主组件 ──────────────────────────────────────────────────────────────────

interface Props {
  modelId: string;
  horizonDays: number;
}

export const BatchInferencePanel: React.FC<Props> = ({ modelId, horizonDays }) => {
  const [anchorDate, setAnchorDate] = useState<dayjs.Dayjs | null>(dayjs());
  const [windowDays, setWindowDays] = useState<number>(horizonDays || 10);
  const [topK, setTopK] = useState(20);
  const [side, setSide] = useState<'both' | 'long' | 'short'>('both');
  const [submitting, setSubmitting] = useState(false);
  const [currentBatch, setCurrentBatch] = useState<BatchInferenceRecord | null>(null);
  const [aggregate, setAggregate] = useState<BatchAggregateResult | null>(null);
  const [aggLoading, setAggLoading] = useState(false);
  const [pollTimer, setPollTimer] = useState<ReturnType<typeof setInterval> | null>(null);
  const [batches, setBatches] = useState<BatchInferenceRecord[]>([]);
  const [batchesLoading, setBatchesLoading] = useState(false);
  const [neutralInfo, setNeutralInfo] = useState<{
    mode: string;
    status: string | null;
    coverage: number | null;
    date: string | null;
  } | null>(null);

  const loadNeutral = useCallback(async () => {
    try {
      const bundle = await neutralizationService.getSettings(modelId);
      const latest = bundle.diagnostics?.[0];
      setNeutralInfo({
        mode: bundle.settings.mode,
        status: latest?.status ?? null,
        coverage: latest?.coverage ?? null,
        date: latest?.data_trade_date ?? null,
      });
    } catch {
      setNeutralInfo(null);
    }
  }, [modelId]);

  useEffect(() => {
    void loadNeutral();
  }, [loadNeutral]);

  useEffect(() => {
    if (currentBatch && (currentBatch.status === 'completed' || currentBatch.status === 'partial')) {
      void loadNeutral();
    }
  }, [currentBatch?.status, loadNeutral]);

  const loadBatches = useCallback(async () => {
    setBatchesLoading(true);
    try {
      const result = await modelTrainingService.listBatchInferences(modelId);
      setBatches(result.items || []);
    } catch {
      // ignore
    } finally {
      setBatchesLoading(false);
    }
  }, [modelId]);

  useEffect(() => {
    loadBatches();
  }, [loadBatches]);

  const startPolling = useCallback((batchId: string) => {
    if (pollTimer) clearInterval(pollTimer);
    const timer = setInterval(async () => {
      try {
        const batch = await modelTrainingService.getBatchInference(batchId);
        setCurrentBatch(batch);
        if (batch.status === 'completed' || batch.status === 'partial' || batch.status === 'failed') {
          clearInterval(timer);
          setPollTimer(null);
          if (batch.status !== 'failed') {
            loadAggregate(batchId);
          }
        }
      } catch {
        clearInterval(timer);
        setPollTimer(null);
      }
    }, 3000);
    setPollTimer(timer);
  }, [pollTimer]);

  useEffect(() => {
    return () => {
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [pollTimer]);

  const handleSubmit = async () => {
    if (!anchorDate) {
      message.warning('请选择锚定日');
      return;
    }
    setSubmitting(true);
    try {
      const batch = await modelTrainingService.submitBatchInference({
        model_id: modelId,
        anchor_date: anchorDate.format('YYYY-MM-DD'),
        window_days: windowDays,
        top_k: topK,
        side,
        reuse_existing: true,
      });
      setCurrentBatch(batch);
      message.success(`批量推理已提交：${batch.batch_id}`);
      startPolling(batch.batch_id);
      loadBatches();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '提交失败';
      message.error(msg);
    } finally {
      setSubmitting(false);
    }
  };

  const loadAggregate = useCallback(async (batchId: string) => {
    if (!batchId) return;
    setAggLoading(true);
    try {
      const agg = await modelTrainingService.getBatchAggregate(batchId, {
        top_k: topK,
        side,
      });
      setAggregate(agg);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '聚合查询失败';
      message.error(msg);
    } finally {
      setAggLoading(false);
    }
  }, [topK, side]);

  const handleSelectBatch = async (batch: BatchInferenceRecord) => {
    setCurrentBatch(batch);
    if (batch.status === 'completed' || batch.status === 'partial') {
      loadAggregate(batch.batch_id);
    } else if (batch.status === 'running') {
      startPolling(batch.batch_id);
    }
  };

  const progressPct = currentBatch
    ? Math.round((currentBatch.progress_done / Math.max(currentBatch.progress_total, 1)) * 100)
    : 0;

  return (
    <div className="space-y-4">
      {/* 输入区 */}
      <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
        <div className="flex items-center gap-3 mb-4">
          <div className="bg-violet-500/10 p-2 rounded-xl text-violet-600">
            <Layers size={18} />
          </div>
          <Text className="text-sm font-black text-slate-800 uppercase tracking-tight leading-none">批量多日推理</Text>
          {neutralInfo && (
            <Tooltip title="中性化影子层：只写独立影子表，不改原始分数与下单链路">
              <span className="ml-auto flex items-center gap-1.5">
                <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">中性化</span>
                <Tag color={neutralInfo.mode === 'enforce' ? 'green' : neutralInfo.mode === 'shadow' ? 'blue' : 'default'} className="!m-0 text-[10px]">
                  {neutralInfo.mode === 'enforce' ? '启用' : neutralInfo.mode === 'shadow' ? '影子' : '关闭'}
                </Tag>
                {neutralInfo.status && (
                  <Tag
                    color={neutralInfo.status === 'ok' ? 'green' : neutralInfo.status === 'degraded' ? 'orange' : 'red'}
                    className="!m-0 text-[10px]"
                  >
                    {neutralInfo.status === 'ok' ? '正常' : neutralInfo.status === 'degraded' ? '降级' : '失败'}
                    {neutralInfo.coverage != null ? ` · 覆盖 ${fmtPct(neutralInfo.coverage)}` : ''}
                    {neutralInfo.date ? ` · ${neutralInfo.date}` : ''}
                  </Tag>
                )}
              </span>
            </Tooltip>
          )}
        </div>

        <div className="grid grid-cols-12 gap-4 items-end">
          <div className="col-span-3">
            <Text className="text-[10px] font-black text-slate-400 uppercase mb-1.5 block tracking-widest pl-1">锚定日</Text>
            <DatePicker
              value={anchorDate}
              onChange={setAnchorDate}
              disabledDate={d => d.isAfter(dayjs())}
              className="w-full rounded-xl h-9 border-slate-100 bg-white"
            />
          </div>
          <div className="col-span-2">
            <Text className="text-[10px] font-black text-slate-400 uppercase mb-1.5 block tracking-widest pl-1">回溯天数 N</Text>
            <InputNumber
              value={windowDays}
              onChange={v => setWindowDays(v || horizonDays)}
              min={1}
              max={60}
              className="w-full rounded-xl h-9 border-slate-100"
            />
          </div>
          <div className="col-span-2">
            <Text className="text-[10px] font-black text-slate-400 uppercase mb-1.5 block tracking-widest pl-1">Top-K</Text>
            <InputNumber
              value={topK}
              onChange={v => setTopK(v || 20)}
              min={5}
              max={500}
              className="w-full rounded-xl h-9 border-slate-100"
            />
          </div>
          <div className="col-span-2">
            <Text className="text-[10px] font-black text-slate-400 uppercase mb-1.5 block tracking-widest pl-1">方向</Text>
            <Select value={side} onChange={setSide} className="w-full h-9">
              <Select.Option value="both">多+空</Select.Option>
              <Select.Option value="long">仅多头</Select.Option>
              <Select.Option value="short">仅空头</Select.Option>
            </Select>
          </div>
          <div className="col-span-3">
            <Button
              type="primary"
              size="large"
              onClick={handleSubmit}
              loading={submitting}
              className="w-full rounded-xl h-9 bg-violet-600 border-0 font-bold shadow-md shadow-violet-100 text-xs"
            >
              批量推理 {windowDays} 天
            </Button>
          </div>
        </div>
      </div>

      {/* 进度 + 历史批次 */}
      {currentBatch && (
        <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              <div className={clsx(
                'p-2 rounded-xl',
                currentBatch.status === 'running' ? 'bg-blue-500/10 text-blue-500' :
                currentBatch.status === 'completed' ? 'bg-emerald-500/10 text-emerald-500' :
                currentBatch.status === 'partial' ? 'bg-amber-500/10 text-amber-500' :
                'bg-rose-500/10 text-rose-500'
              )}>
                {currentBatch.status === 'running' ? <Activity size={16} /> :
                 currentBatch.status === 'completed' ? <CheckCircle2 size={16} /> :
                 currentBatch.status === 'failed' ? <XCircle size={16} /> :
                 <Clock size={16} />}
              </div>
              <div>
                <Text className="text-xs font-black text-slate-800 block">{currentBatch.batch_id}</Text>
                <Text className="text-[10px] text-slate-400">
                  {currentBatch.anchor_date} · N={currentBatch.window_days} · H={currentBatch.horizon_days}
                </Text>
              </div>
            </div>
            <Tag color={
              currentBatch.status === 'completed' ? 'green' :
              currentBatch.status === 'running' ? 'blue' :
              currentBatch.status === 'partial' ? 'orange' : 'red'
            } className="m-0 border-0 text-[9px] font-black uppercase rounded-md px-2">
              {currentBatch.status}
            </Tag>
          </div>

          {currentBatch.status === 'running' && (
            <Progress
              percent={progressPct}
              size="small"
              className="mb-1"
              strokeColor="#7c3aed"
            />
          )}

          {currentBatch.error_message && (
            <div className="mt-2 p-2 bg-rose-50 rounded-lg border border-rose-100">
              <Text className="text-[10px] text-rose-600">{currentBatch.error_message}</Text>
            </div>
          )}
        </div>
      )}

      {/* 历史批次快速入口 */}
      {batches.length > 0 && (
        <div className="glass-panel rounded-2xl p-4 border border-slate-100/50">
          <div className="flex items-center gap-2 mb-2">
            <Clock size={12} className="text-slate-400" />
            <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">历史批次</Text>
          </div>
          <div className="flex gap-2 flex-wrap">
            {batches.slice(0, 8).map(b => (
              <Button
                key={b.batch_id}
                size="small"
                type={currentBatch?.batch_id === b.batch_id ? 'primary' : 'default'}
                className={clsx('rounded-lg text-[9px] font-bold h-7 px-3', currentBatch?.batch_id === b.batch_id ? 'bg-violet-600 border-violet-600' : 'border-slate-200')}
                onClick={() => handleSelectBatch(b)}
              >
                {b.anchor_date} N={b.window_days}
                <Tag color={b.status === 'completed' ? 'green' : b.status === 'running' ? 'blue' : b.status === 'partial' ? 'orange' : 'red'}
                  className="m-0 ml-1 border-0 text-[7px] px-1 rounded">{b.status}</Tag>
              </Button>
            ))}
          </div>
        </div>
      )}

      {/* 聚合结果 */}
      {aggregate && (
        <div className="space-y-4">
          <CaliberCard meta={aggregate.meta} />

          <Tabs
            defaultActiveKey="consensus"
            size="small"
            items={[
              {
                key: 'consensus',
                label: '共识榜',
                children: (
                  <div className="space-y-4">
                    {aggregate.groups.consensus_long && aggregate.groups.consensus_long.length > 0 && (
                      <SymbolTable title="共识多头池" data={aggregate.groups.consensus_long} side="long" />
                    )}
                    {aggregate.groups.top_hitters && aggregate.groups.top_hitters.length > 0 && (
                      <SymbolTable title="上榜频次排行" data={aggregate.groups.top_hitters} side="long" />
                    )}
                    {aggregate.groups.rising && aggregate.groups.rising.length > 0 && (
                      <SymbolTable title="动量改善池" data={aggregate.groups.rising} side="long" />
                    )}
                    {aggregate.groups.fading && aggregate.groups.fading.length > 0 && (
                      <SymbolTable title="动量恶化池" data={aggregate.groups.fading} side="long" />
                    )}
                    {aggregate.groups.consensus_short && aggregate.groups.consensus_short.length > 0 && (
                      <SymbolTable title="共识空头池" data={aggregate.groups.consensus_short} side="short" />
                    )}
                    {aggregate.groups.bottom_hitters && aggregate.groups.bottom_hitters.length > 0 && (
                      <SymbolTable title="底部频次排行" data={aggregate.groups.bottom_hitters} side="short" />
                    )}
                  </div>
                ),
              },
              {
                key: 'movers',
                label: '涨跌幅榜',
                children: <MoversPanel movers={aggregate.movers} />,
              },
              {
                key: 'heatmap',
                label: '分数矩阵',
                children: <ScoreMatrixHeatmap data={aggregate} />,
              },
              {
                key: 'daily',
                label: '每日统计',
                children: <DailyStatsPanel daily={aggregate.daily} />,
              },
            ]}
          />
        </div>
      )}

      {/* Loading */}
      {aggLoading && (
        <div className="flex items-center justify-center py-8">
          <Spin size="large" />
          <Text className="text-sm text-slate-400 ml-3">聚合计算中...</Text>
        </div>
      )}

      {/* 空状态 */}
      {!aggregate && !aggLoading && !currentBatch && (
        <div className="py-16">
          <Empty description={<span className="text-xs text-slate-400 font-medium">选择锚定日并提交批量推理，查看多日聚合统计</span>} />
        </div>
      )}
    </div>
  );
};
