import React, { useState, useMemo, useCallback } from 'react';
import dayjs from 'dayjs';
import {
  Button, Tag, Typography, Empty, Spin, Collapse, Tooltip, Select, Modal, DatePicker,
} from 'antd';
import { clsx } from 'clsx';
import {
  ArrowLeft, ArrowRight, TrendingUp, Download, Search, CheckCircle2, XCircle,
} from 'lucide-react';
import type { InferenceRankingResult, InferenceRankingItem } from '../../services/modelTrainingService';
import { StockScoreChart } from './StockScoreChart';
import { splitInferenceLogs, exportRankingCsv } from './inferenceDetailUtils';

const { Text } = Typography;

interface Props {
  runId: string;
  result: InferenceRankingResult | null;
  loading: boolean;
  onBack: () => void;
  onRetry?: () => void;
  /** 切换到指定推理日期的 run（±1 天导航 / 日期选择） */
  onNavigateDate?: (inferenceDate: string) => void;
}

/** 4 指标卡 */
const MetricCell: React.FC<{ label: string; value: string; valueClass?: string; sub?: string }> = ({
  label, value, valueClass, sub,
}) => (
  <div className="rounded-2xl border border-slate-100 bg-slate-50/60 p-3 text-center">
    <div className="text-xs font-bold text-slate-500 mb-1">{label}</div>
    <div className={clsx('font-mono font-black text-lg leading-none', valueClass || 'text-slate-800')}>{value}</div>
    {sub && <div className="text-[11px] text-slate-400 mt-1">{sub}</div>}
  </div>
);

/** 排名行：点击打开 K 线弹窗 */
const RankRow: React.FC<{ item: InferenceRankingItem; onOpen: (item: InferenceRankingItem) => void }> = ({
  item, onOpen,
}) => {
  const s = Number(item.score);
  return (
    <div
      onClick={() => onOpen(item)}
      className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-slate-50/70 border border-slate-100/60 hover:bg-blue-50/40 transition-colors cursor-pointer"
    >
      <span className="w-6 h-6 rounded-md flex items-center justify-center text-xs font-bold shrink-0 bg-slate-200 text-slate-600">
        {item.rank}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <Text className="text-xs font-bold text-slate-800 font-mono truncate">{item.code}</Text>
          <Text className="text-xs text-slate-500 truncate">{item.name || ''}</Text>
        </div>
        {item.industry && (
          <Text className="text-xs text-slate-400 truncate block">{item.industry}</Text>
        )}
      </div>
      <div className="flex flex-col items-end shrink-0">
        <Text className={clsx('text-sm font-mono font-bold', s >= 0 ? 'text-rose-600' : 'text-emerald-600')}>
          {s >= 0 ? '+' : ''}{s.toFixed(4)}
        </Text>
        {item.signal === 'buy' ? (
          <Text className="text-xs font-bold text-rose-500">↑</Text>
        ) : item.signal === 'sell' ? (
          <Text className="text-xs font-bold text-emerald-500">↓</Text>
        ) : null}
      </div>
    </div>
  );
};

export const InferenceRunDetailView: React.FC<Props> = ({ runId, result, loading, onBack, onRetry, onNavigateDate }) => {
  // 日期导航：从 runId（run_YYYYMMDD_xxx）解析当前推理日期
  const [datePickerValue, setDatePickerValue] = useState<dayjs.Dayjs | null>(null);
  const currentInferenceDate = useMemo(() => {
    const m = runId.match(/run_(\d{4})(\d{2})(\d{2})/);
    return m ? `${m[1]}-${m[2]}-${m[3]}` : null;
  }, [runId]);
  const handleShiftDate = useCallback((delta: number) => {
    if (!currentInferenceDate || !onNavigateDate) return;
    const d = dayjs(currentInferenceDate).add(delta, 'day');
    onNavigateDate(d.format('YYYY-MM-DD'));
  }, [currentInferenceDate, onNavigateDate]);
  const handlePickDate = useCallback((d: dayjs.Dayjs | null) => {
    if (d && onNavigateDate) {
      setDatePickerValue(d);
      onNavigateDate(d.format('YYYY-MM-DD'));
    }
  }, [onNavigateDate]);
  const [stockModal, setStockModal] = useState<{
    symbol: string;
    name: string;
    rank?: number;
    score?: number;
    board?: string;
    industry?: string;
    market_cap_tier?: string;
    market_cap_yi?: number;
    negative_tag?: string;
  } | null>(null);
  const [exporting, setExporting] = useState(false);

  const handleExport = () => {
    if (!result) return;
    setExporting(true);
    try {
      exportRankingCsv(result);
    } finally {
      setExporting(false);
    }
  };

  // 4 指标 + 左右双列：直接从排名明細计算，与列表展示永远一致
  const distStats = useMemo(() => {
    const rankings = result?.rankings ?? [];
    let pos = 0;
    let neg = 0;
    let zero = 0;
    let sum = 0;
    let n = 0;
    for (const r of rankings) {
      const s = Number(r.score);
      if (Number.isNaN(s)) continue;
      n += 1;
      sum += s;
      if (s > 0) pos += 1;
      else if (s < 0) neg += 1;
      else zero += 1;
    }
    return { pos, neg, zero, mean: n > 0 ? sum / n : null as number | null, total: n };
  }, [result]);

  /** 左列：正分 Top100（分数从高到低） */
  const positiveTop100 = useMemo(
    () => (result?.rankings ?? []).filter(r => Number(r.score) > 0).slice(0, 100),
    [result],
  );
  /** 右列：负分 100 只倒序（分数最低优先） */
  const negativeBottom100 = useMemo(() => {
    const negs = (result?.rankings ?? []).filter(r => Number(r.score) < 0);
    return negs.slice(-100).reverse();
  }, [result]);

  // K线弹窗内导航：按全量排名列表切换上一只/下一只股票
  const openStockModal = (item: InferenceRankingItem) => {
    setStockModal({
      symbol: item.code,
      name: item.name,
      rank: item.rank,
      score: Number(item.score),
      board: item.board,
      industry: item.industry,
      market_cap_tier: item.market_cap_tier,
      market_cap_yi: item.market_cap_yi,
      negative_tag: item.negative_tag,
    });
  };

  const allRankings = result?.rankings ?? [];
  const navPrevStock = () => {
    if (!stockModal) return;
    const idx = allRankings.findIndex(r => r.code === stockModal.symbol);
    if (idx <= 0) return;
    openStockModal(allRankings[idx - 1]);
  };

  const navNextStock = () => {
    if (!stockModal) return;
    const idx = allRankings.findIndex(r => r.code === stockModal.symbol);
    if (idx < 0 || idx >= allRankings.length - 1) return;
    openStockModal(allRankings[idx + 1]);
  };

  // 按代码/名称搜索后跳转（全市场）
  const navSearchStock = (value: string) => {
    const kw = value.trim().toLowerCase();
    if (!kw) return;
    const hit = (result?.rankings || []).find(r =>
      r.code.toLowerCase().includes(kw) || r.name.toLowerCase().includes(kw)
    );
    if (hit) openStockModal(hit);
  };

  return (
    <div className="space-y-4">
      {/* 页头：返回 + 标题 + 导出 */}
      <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
        <div className="flex items-center gap-3">
          <Button
            size="small"
            icon={<ArrowLeft size={13} />}
            onClick={onBack}
            className="rounded-xl h-8 px-3 text-xs font-bold border-slate-200 flex-shrink-0"
          >
            返回列表
          </Button>
          <div className="w-10 h-10 bg-blue-50 rounded-2xl flex items-center justify-center text-blue-600 shadow-sm border border-blue-100/50 flex-shrink-0">
            <TrendingUp size={18} />
          </div>
          <div className="flex flex-col min-w-0 flex-1">
            <span className="font-black text-slate-800 text-lg tracking-tight leading-none truncate">排名结果</span>
            <span className="text-xs font-bold text-slate-400 mt-1 uppercase tracking-widest truncate font-mono">
              {runId} · {result?.target_date ? `目标交易日 ${result.target_date}` : '加载中…'}
            </span>
          </div>
          {onNavigateDate && (
            <div className="flex items-center gap-1.5 flex-shrink-0">
              <Tooltip title="前一天">
                <Button size="small" icon={<ArrowLeft size={13} />} className="rounded-xl h-8 w-8 p-0 text-xs font-bold border-slate-200" onClick={() => handleShiftDate(-1)} />
              </Tooltip>
              <DatePicker
                size="small"
                value={datePickerValue}
                onChange={handlePickDate}
                allowClear={false}
                placeholder="选日期"
                className="rounded-xl !text-xs !w-28"
              />
              <Tooltip title="后一天">
                <Button size="small" icon={<ArrowRight size={13} />} className="rounded-xl h-8 w-8 p-0 text-xs font-bold border-slate-200" onClick={() => handleShiftDate(1)} />
              </Tooltip>
            </div>
          )}
          {result?.summary?.status === 'failed' && onRetry && (
            <Tooltip title="重新加载">
              <Button size="small" icon={<Search size={13} />} onClick={onRetry} className="rounded-xl text-xs font-bold h-8 px-3">
                重试
              </Button>
            </Tooltip>
          )}
          <Tooltip title={!result?.rankings?.length ? '当前没有可导出的排名数据' : '导出当前排名结果为 CSV'}>
            <Button
              type="default"
              icon={<Download size={14} className={exporting ? 'animate-pulse' : ''} />}
              className="rounded-xl h-9 px-4 font-black border-slate-200 text-xs shadow-sm hover:translate-y-[-1px] transition-all flex-shrink-0"
              disabled={exporting || !result || !result.rankings?.length}
              loading={exporting}
              onClick={handleExport}
            >
              {exporting ? '导出中...' : '导出 CSV'}
            </Button>
          </Tooltip>
        </div>
      </div>

      {loading ? (
        <div className="glass-panel rounded-3xl p-10 border border-slate-100/50 flex items-center justify-center">
          <div className="flex flex-col items-center gap-3 py-8">
            <Spin size="large" />
            <Text className="text-xs text-slate-400 font-medium">正在加载推理结果…</Text>
          </div>
        </div>
      ) : result ? (
        <div className="space-y-3">
          {/* 4 指标 */}
          <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
              <MetricCell label="正分标的" value={String(distStats.pos)} valueClass="text-rose-600" sub={`共 ${distStats.total} 只`} />
              <MetricCell label="负分标的" value={String(distStats.neg)} valueClass="text-emerald-600" />
              <MetricCell label="平分标的" value={String(distStats.zero)} />
              <MetricCell
                label="平均分"
                value={distStats.mean === null ? '—' : distStats.mean.toFixed(4)}
                valueClass={distStats.mean === null ? undefined : distStats.mean >= 0 ? 'text-rose-600' : 'text-emerald-600'}
              />
            </div>
          </div>

          {/* 双列：左正分 Top100 / 右负分 100 只倒序 */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div className="glass-panel rounded-3xl p-5 border border-slate-100/50 flex flex-col overflow-hidden">
              <div className="flex items-center justify-between mb-3 shrink-0">
                <Text className="text-xs font-bold text-slate-500">正分 Top100</Text>
                <Tag className="m-0 border-0 text-xs font-bold px-2 rounded-md bg-rose-50 text-rose-600">
                  {distStats.pos} 只 · 显示前 {positiveTop100.length}
                </Tag>
              </div>
              {positiveTop100.length > 0 ? (
                <div className="overflow-y-auto custom-scrollbar pr-1 overscroll-contain" style={{ maxHeight: 560 }}>
                  <div className="flex flex-col gap-1">
                    {positiveTop100.map((r) => (
                      <RankRow key={r.code} item={r} onOpen={openStockModal} />
                    ))}
                  </div>
                </div>
              ) : (
                <div className="flex-1 flex items-center justify-center py-10 text-xs text-slate-400">暂无正分标的</div>
              )}
            </div>
            <div className="glass-panel rounded-3xl p-5 border border-slate-100/50 flex flex-col overflow-hidden">
              <div className="flex items-center justify-between mb-3 shrink-0">
                <Text className="text-xs font-bold text-slate-500">负分 Bottom100 · 倒序</Text>
                <Tag className="m-0 border-0 text-xs font-bold px-2 rounded-md bg-emerald-50 text-emerald-600">
                  {distStats.neg} 只 · 分数最低优先
                </Tag>
              </div>
              {negativeBottom100.length > 0 ? (
                <div className="overflow-y-auto custom-scrollbar pr-1 overscroll-contain" style={{ maxHeight: 560 }}>
                  <div className="flex flex-col gap-1">
                    {negativeBottom100.map((r) => (
                      <RankRow key={r.code} item={r} onOpen={openStockModal} />
                    ))}
                  </div>
                </div>
              ) : (
                <div className="flex-1 flex items-center justify-center py-10 text-xs text-slate-400">暂无负分标的</div>
              )}
            </div>
          </div>

          {result.summary && (
            <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
              <Collapse
                ghost
                className="inference-result-collapse run-detail-collapse"
                expandIconPosition="end"
                defaultActiveKey={result.summary.status === 'failed' ? ['run-detail'] : []}
                items={[{
                  key: 'run-detail',
                  label: (
                    <div className="flex items-center gap-3 py-1">
                      <Text className="text-sm font-black text-slate-800 uppercase tracking-tight leading-none">运行详情</Text>
                      <Tag color={result.summary.status === 'failed' ? 'red' : 'green'} className="m-0 rounded-full text-[11px] font-black">
                        {result.summary.status === 'failed' ? '失败' : result.summary.status === 'completed' ? '成功' : '进行中'}
                      </Tag>
                      {result.summary.signals_count ? <Tag className="m-0 border-0 bg-slate-100 text-slate-500 text-[11px] font-bold rounded-md px-2">信号 {result.summary.signals_count}</Tag> : null}
                      <Text className="text-[11px] text-slate-300 font-mono">{result.summary.run_id}</Text>
                    </div>
                  ),
                  children: (
                    <div className="space-y-3 pt-2">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <div>
                  <Text className="text-xs text-slate-400 font-black uppercase block">运行批次</Text>
                  <Text className="text-xs font-black text-slate-800 font-mono break-all">{result.summary.run_id}</Text>
                </div>
                <div>
                  <Text className="text-xs text-slate-400 font-black uppercase block">模型</Text>
                  <Text className="text-xs font-black text-slate-800 font-mono break-all">{result.summary.effective_model_id || result.summary.model_id}</Text>
                </div>
                <div>
                  <Text className="text-xs text-slate-400 font-black uppercase block">状态</Text>
                  <Tag color={result.summary.status === 'failed' ? 'red' : 'green'} className="m-0 rounded-full text-[11px] font-black">
                    {result.summary.status === 'failed' ? '失败' : result.summary.status === 'completed' ? '成功' : '进行中'}
                  </Tag>
                </div>
                <div>
                  <Text className="text-xs text-slate-400 font-black uppercase block">信号数</Text>
                  <Text className="text-xs font-black text-slate-800">{result.summary.signals_count}</Text>
                </div>
                <div>
                  <Text className="text-xs text-slate-400 font-black uppercase block">模型切换</Text>
                  <Text className="text-xs font-black text-slate-800">{(result.summary.model_switch_used ?? result.summary.fallback_used) ? '是' : '否'}</Text>
                </div>
                <div>
                  <Text className="text-xs text-slate-400 font-black uppercase block">执行模式</Text>
                  <Text className="text-xs font-black text-slate-800">{result.summary.execution_mode === 'independent_model' ? '独立模型' : result.summary.execution_mode === 'system_chain' ? '系统链路' : '—'}</Text>
                </div>
                <div>
                  <Text className="text-xs text-slate-400 font-black uppercase block">耗时</Text>
                  <Text className="text-xs font-black text-slate-800">{(Number(result.summary.duration_ms || 0) / 1000).toFixed(1)}s</Text>
                </div>
              </div>
              <Collapse
                key={result.summary.run_id}
                ghost
                className="inference-result-collapse"
                defaultActiveKey={result.summary.status === 'failed' ? ['diagnostics', 'precheck', 'stderr'] : []}
                items={[
                  {
                    key: 'diagnostics',
                    label: <span className="text-xs font-black text-slate-700">诊断信息</span>,
                    children: (
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        <div className="rounded-2xl border border-slate-100 bg-white p-3">
                          <Text className="text-xs text-slate-400 font-black uppercase block">失败阶段</Text>
                          <Text className="text-xs font-black text-slate-800">{result.summary.failure_stage || '—'}</Text>
                        </div>
                        <div className="rounded-2xl border border-slate-100 bg-white p-3">
                          <Text className="text-xs text-slate-400 font-black uppercase block">模型切换原因</Text>
                          <Text className="text-xs font-black text-slate-800 break-all">{result.summary.model_switch_reason || result.summary.fallback_reason || '—'}</Text>
                        </div>
                        <div className="rounded-2xl border border-slate-100 bg-white p-3">
                          <Text className="text-xs text-slate-400 font-black uppercase block">实际模型</Text>
                          <Text className="text-xs font-black text-slate-800 font-mono break-all">
                            {result.summary.active_model_id || '—'}
                          </Text>
                        </div>
                        <div className="rounded-2xl border border-slate-100 bg-white p-3">
                          <Text className="text-xs text-slate-400 font-black uppercase block">生效模型</Text>
                          <Text className="text-xs font-black text-slate-800 font-mono break-all">
                            {result.summary.effective_model_id || '—'}
                          </Text>
                        </div>
                        <div className="rounded-2xl border border-slate-100 bg-white p-3 sm:col-span-2">
                          <Text className="text-xs text-slate-400 font-black uppercase block">数据源</Text>
                          <Text className="text-xs font-black text-slate-800 font-mono break-all">
                            {result.summary.active_data_source || '—'}
                          </Text>
                        </div>
                        <div className="rounded-2xl border border-slate-100 bg-white p-3 sm:col-span-2">
                          <Text className="text-xs text-slate-400 font-black uppercase block">错误信息</Text>
                          <Text className="text-xs font-black text-rose-600 break-all">
                            {result.summary.error_message || result.summary.error_msg || '—'}
                          </Text>
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: 'precheck',
                    label: <span className="text-xs font-black text-slate-700">前置检查</span>,
                    children: (() => {
                      const precheck = (result.summary?.result_json as any)?.precheck || (result.summary?.request_json as any)?.precheck || null;
                      if (!precheck) {
                        return <div className="flex justify-center py-2"><Empty description={<span className="text-xs text-slate-400">暂无前置检查记录</span>} /></div>;
                      }
                      const items = Array.isArray(precheck.items) ? precheck.items : [];
                      return (
                        <div className="space-y-2">
                          <div className="flex flex-wrap gap-2">
                            <Tag color={precheck.passed ? 'green' : 'red'} className="m-0 rounded-full text-[11px] font-black">
                              {precheck.passed ? '通过' : '阻断'}
                            </Tag>
                            <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">
                              {precheck.effective_model_id || precheck.model_id || '—'}
                            </Tag>
                            <Tag className="m-0 rounded-full border-0 bg-blue-50 text-blue-700 font-bold">
                              {precheck.prediction_trade_date || '—'}
                            </Tag>
                          </div>
                          <div className="space-y-2">
                            {items.length > 0 ? items.map((item: any) => (
                              <div
                                key={item.key}
                                className={clsx(
                                  'flex items-start justify-between gap-3 rounded-2xl border px-3 py-2',
                                  item.passed ? 'border-slate-100 bg-white' : 'border-rose-100 bg-rose-50/60',
                                )}
                              >
                                <div className="min-w-0">
                                  <div className="flex items-center gap-2">
                                    {item.passed ? <CheckCircle2 size={11} className="text-emerald-500 flex-shrink-0" /> : <XCircle size={11} className="text-rose-500 flex-shrink-0" />}
                                    <Text className="text-xs font-black text-slate-800">{item.label}</Text>
                                    <Tag className={clsx('m-0 rounded-full border-0 text-[11px] font-bold', item.severity === 'hard' ? 'bg-rose-50 text-rose-500' : 'bg-slate-100 text-slate-500')}>
                                      {item.severity === 'hard' ? '硬门禁' : '提示'}
                                    </Tag>
                                  </div>
                                  <Text className="mt-1 block text-xs text-slate-500 break-all">{item.detail}</Text>
                                </div>
                                <Tag color={item.passed ? 'green' : 'red'} className="m-0 rounded-full text-[11px] font-black">
                                  {item.passed ? '通过' : '未通过'}
                                </Tag>
                              </div>
                            )) : (
                              <div className="flex justify-center py-2"><Empty description={<span className="text-xs text-slate-400">暂无检查明细</span>} /></div>
                            )}
                          </div>
                        </div>
                      );
                    })(),
                  },
                  {
                    key: 'stdout',
                    label: <span className="text-xs font-black text-slate-700">标准输出</span>,
                    children: (() => {
                      const logs = splitInferenceLogs(result.summary?.stdout, result.summary?.stderr);
                      return logs.stdout ? (
                        <div className="rounded-2xl border border-slate-200 bg-slate-950 p-3">
                          <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all text-xs leading-relaxed text-emerald-400 custom-scrollbar scrollbar-dark">
                            {logs.stdout}
                          </pre>
                        </div>
                      ) : (
                        <div className="flex justify-center py-2"><Empty description={<span className="text-xs text-slate-400">暂无标准输出</span>} /></div>
                      );
                    })(),
                  },
                  {
                    key: 'stderr',
                    label: <span className="text-xs font-black text-slate-700">错误输出</span>,
                    children: (() => {
                      const logs = splitInferenceLogs(result.summary?.stdout, result.summary?.stderr);
                      return logs.stderr ? (
                        <div className="rounded-2xl border border-rose-100 bg-rose-50/70 p-3">
                          <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all text-xs leading-relaxed text-rose-700 custom-scrollbar">
                            {logs.stderr}
                          </pre>
                        </div>
                      ) : (
                        <div className="flex justify-center py-2"><Empty description={<span className="text-xs text-slate-400">暂无错误输出</span>} /></div>
                      );
                    })(),
                  },
                ]}
              />
                    </div>
                  ),
                }]}
              />
            </div>
          )}
        </div>
      ) : (
        <div className="glass-panel rounded-3xl p-10 border border-slate-100/50 flex items-center justify-center">
          <Empty description={<span className="text-xs text-slate-400">暂无数据</span>} />
        </div>
      )}

      {/* 股票 K线 + 历史推理分数弹窗 */}
      <Modal
        open={!!stockModal}
        onCancel={() => setStockModal(null)}
        footer={null}
        width={900}
        centered
        title={null}
        styles={{
          body: { padding: '20px', maxHeight: '78vh', overflowY: 'auto' },
          mask: { backdropFilter: 'blur(4px)', backgroundColor: 'rgba(0,0,0,0.2)' },
        }}
      >
        {stockModal && (() => {
          const curIdx = allRankings.findIndex(r => r.code === stockModal.symbol);
          const hasPrev = curIdx > 0;
          const hasNext = curIdx >= 0 && curIdx < allRankings.length - 1;
          return (
            <div className="space-y-3">
              {/* 导航工具栏：上一只/下一只 + 搜索 + 当前排名 */}
              <div className="flex items-center gap-2 bg-slate-50 rounded-2xl border border-slate-100 px-3 py-2">
                <Button
                  size="small"
                  disabled={!hasPrev}
                  onClick={navPrevStock}
                  className="rounded-xl text-xs font-bold h-8 px-3 flex-shrink-0"
                >
                  ‹ 上一只
                </Button>
                <Button
                  size="small"
                  disabled={!hasNext}
                  onClick={navNextStock}
                  className="rounded-xl text-xs font-bold h-8 px-3 flex-shrink-0"
                >
                  下一只 ›
                </Button>
                <div className="flex-1 min-w-0">
                  <Select
                    showSearch
                    size="small"
                    placeholder="搜索全市场股票代码或名称，回车跳转..."
                    className="w-full"
                    optionFilterProp="label"
                    notFoundContent="无匹配股票"
                    filterOption={(input, option) => {
                      const kw = String(input || '').toLowerCase();
                      const label = String((option as any)?.label || '');
                      const value = String((option as any)?.value || '');
                      return label.toLowerCase().includes(kw) || value.toLowerCase().includes(kw);
                    }}
                    onChange={(v) => navSearchStock(String(v))}
                    options={(result?.rankings || []).map(r => ({
                      value: r.code,
                      label: `${r.code} · ${r.name || '—'} · 第${r.rank}名`,
                    }))}
                  />
                </div>
                <div className="text-xs text-slate-400 font-mono flex-shrink-0">
                  {curIdx >= 0
                    ? `第 ${stockModal.rank ?? curIdx + 1} 名 · ${curIdx + 1}/${allRankings.length}`
                    : '不在当前筛选'}
                </div>
              </div>
              <StockScoreChart
                symbol={stockModal.symbol}
                name={stockModal.name}
                market="A"
                days={3650}
                height={380}
                stockInfo={{
                  rank: stockModal.rank,
                  score: stockModal.score,
                  board: stockModal.board,
                  industry: stockModal.industry,
                  market_cap_tier: stockModal.market_cap_tier,
                  market_cap_yi: stockModal.market_cap_yi,
                  negative_tag: stockModal.negative_tag,
                }}
                wideScale={!!(result?.summary?.is_wide_scale || result?.summary?.market_signal?.score_scale === 'wide')}
                modelId={result?.summary?.model_id || result?.summary?.effective_model_id}
              />
            </div>
          );
        })()}
      </Modal>
    </div>
  );
};
