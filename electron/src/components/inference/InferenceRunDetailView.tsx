import React, { useState, useMemo, useCallback } from 'react';
import dayjs from 'dayjs';
import {
  Button, Tag, Typography, Empty, Spin, Table, Collapse, Input, Tooltip, Select, Modal, DatePicker,
} from 'antd';
import { clsx } from 'clsx';
import {
  ArrowLeft, ArrowRight, TrendingUp, Download, Search, CheckCircle2, XCircle,
} from 'lucide-react';
import type { InferenceRankingResult, InferenceRankingItem } from '../../services/modelTrainingService';
import { ScoreDistributionPanel } from './ScoreDistributionPanel';
import { StrategyDashboard } from './StrategyDashboard';
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

export const InferenceRunDetailView: React.FC<Props> = ({ runId, result, loading, onBack, onRetry, onNavigateDate }) => {
  const [rankingSearch, setRankingSearch] = useState('');
  const [boardFilter, setBoardFilter] = useState<string>('all');
  const [industryFilter, setIndustryFilter] = useState<string>('all');
  const [bucketFilter, setBucketFilter] = useState<string>('all');
  const [trendFilter, setTrendFilter] = useState<string>('all');
  const [capFilter, setCapFilter] = useState<string>('all');
  const [exporting, setExporting] = useState(false);
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
  // 市场推断：按模型 ID 前缀（mdl_hk_ → 港股；缺省 CN），驱动筛选文案/列口径
  const detailMarket = useMemo(() => {
    const mid = String(result?.summary?.model_id || '');
    if (mid.startsWith('mdl_hk_')) return 'HK';
    if (mid.startsWith('mdl_us_')) return 'US';
    return 'CN';
  }, [result]);
  const isHk = detailMarket === 'HK';
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

  const handleExport = () => {
    if (!result) return;
    setExporting(true);
    try {
      exportRankingCsv(result);
    } finally {
      setExporting(false);
    }
  };

  // K线弹窗内导航：按当前筛选后的排名列表切换上一只/下一只股票
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

  const navPrevStock = () => {
    if (!stockModal) return;
    const idx = filteredRankings.findIndex(r => r.code === stockModal.symbol);
    if (idx <= 0) return;
    openStockModal(filteredRankings[idx - 1]);
  };

  const navNextStock = () => {
    if (!stockModal) return;
    const idx = filteredRankings.findIndex(r => r.code === stockModal.symbol);
    if (idx < 0 || idx >= filteredRankings.length - 1) return;
    openStockModal(filteredRankings[idx + 1]);
  };

  // 按代码/名称搜索后跳转（全市场，非筛选后）
  const navSearchStock = (value: string) => {
    const kw = value.trim().toLowerCase();
    if (!kw) return;
    const hit = (result?.rankings || []).find(r =>
      r.code.toLowerCase().includes(kw) || r.name.toLowerCase().includes(kw)
    );
    if (hit) openStockModal(hit);
  };

  // 可用于筛选的行业列表（按出现次数降序）
  const industryOptions = useMemo(() => {
    if (!result) return [];
    const counts = new Map<string, number>();
    result.rankings.forEach(r => {
      if (r.industry) counts.set(r.industry, (counts.get(r.industry) || 0) + 1);
    });
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([industry, count]) => ({ value: industry, label: `${industry} (${count})` }));
  }, [result]);

  // 板块选项（5大板块）
  const boardOptions = useMemo(() => {
    if (!result) return [];
    const counts = new Map<string, number>();
    result.rankings.forEach(r => {
      if (r.board) counts.set(r.board, (counts.get(r.board) || 0) + 1);
    });
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([board, count]) => ({ value: board, label: `${board} (${count})` }));
  }, [result]);

  // 分数区间 + 负分标注选项（合并为一个下拉，分组显示）
  const bucketOptions = useMemo(() => {
    const groups: any[] = [];
    if (result?.summary?.score_buckets?.length) {
      groups.push({
        label: '分数区间',
        options: result.summary.score_buckets.map(b => ({ value: b.key, label: `${b.label} · ${b.action}` })),
      });
    }
    const negOptions = isHk
      ? [
          { value: 'neg_extreme', label: '极端负分 ≤-0.20' },
          { value: 'neg_short', label: '做空候选 · 小市值≤-0.15' },
          { value: 'neg_mistake', label: '错杀候选 · 高市值负分' },
          { value: 'neg_resistant', label: '防御性行业负分' },
          { value: 'neg_general', label: '一般负分' },
        ]
      : [
          { value: 'neg_extreme', label: '极端负分 ≤-0.20' },
          { value: 'neg_short', label: '做空候选 · 微盘/小盘≤-0.15' },
          { value: 'neg_mistake', label: '错杀候选 · 大盘/超大盘负分' },
          { value: 'neg_resistant', label: '抗跌行业 · 银行/半导体' },
          { value: 'neg_general', label: '一般负分' },
        ];
    groups.push({ label: '负分标注', options: negOptions });
    return groups;
  }, [result]);

  const trendOptions = [
    { value: '先升后降', label: '先升后降 · 最佳买点' },
    { value: '连续上升', label: '连续上升 · 过热不追' },
    { value: '连续下降', label: '连续下降 · 信号衰退' },
    { value: '上升', label: '单日上升' },
    { value: '下降', label: '单日下降' },
    { value: '持平', label: '持平' },
  ];

  // 市值分档选项
  // 市值分档：港股按港元市值口径（数据标注缺失时筛选自然无匹配，文案先行对齐）
  const capOptions = isHk
    ? [
        { value: '微盘', label: '微盘 <20亿' },
        { value: '小盘', label: '小盘 20-100亿' },
        { value: '中盘', label: '中盘 100-300亿' },
        { value: '大盘', label: '大盘 300-1000亿' },
        { value: '超大盘', label: '超大盘 >1000亿' },
      ]
    : [
        { value: '微盘', label: '微盘 <30亿' },
        { value: '小盘', label: '小盘 30-100亿' },
        { value: '中盘', label: '中盘 100-300亿' },
        { value: '大盘', label: '大盘 300-1000亿' },
        { value: '超大盘', label: '超大盘 >1000亿' },
      ];

  // 综合筛选（融合模型分数为 [-1,1] 时，桶阈值来自后端 score_buckets 的自适应分位数）
  const filteredRankings = useMemo(() => {
    if (!result) return [];
    // 从后端 score_buckets 提取每个桶的标签做区间解析，用于 wide-scale 时替代硬编码阈值
    const wideScale = !!result.summary?.is_wide_scale || result.summary?.market_signal?.score_scale === 'wide';
    return result.rankings.filter(r => {
      if (boardFilter !== 'all' && r.board !== boardFilter) return false;
      if (industryFilter !== 'all' && r.industry !== industryFilter) return false;
      if (trendFilter !== 'all' && r.trend !== trendFilter) return false;
      if (capFilter !== 'all' && r.market_cap_tier !== capFilter) return false;
      if (bucketFilter !== 'all') {
        // 负分标注筛选（合并进分数下拉）
        if (bucketFilter === 'neg_extreme') return r.negative_tag === '极端负分';
        if (bucketFilter === 'neg_short') return r.negative_tag === '做空候选';
        if (bucketFilter === 'neg_mistake') return r.negative_tag === '错杀候选';
        if (bucketFilter === 'neg_resistant') return r.negative_tag === '抗跌行业';
        if (bucketFilter === 'neg_general') return r.negative_tag === '负分';
        const s = r.score;
        if (wideScale) {
          // 用后端返回的分位数桶阈值
          const buckets = result.summary?.score_buckets || [];
          const b = buckets.find(x => x.key === bucketFilter);
          if (b) {
            const label = String(b.label || '');
            const m = label.match(/≥\s*(-?[\d.]+)|(-?[\d.]+)\s*[-~]\s*(-?[\d.]+)/);
            if (m) {
              if (m[1] !== undefined) return s >= parseFloat(m[1]);
              if (m[2] !== undefined && m[3] !== undefined) return s >= parseFloat(m[2]) && s < parseFloat(m[3]);
            }
            return true;
          }
        }
        if (bucketFilter === 'lt_010' && !(s < 0.10)) return false;
        if (bucketFilter === 'gold' && !(s >= 0.10 && s < 0.12)) return false;
        if (bucketFilter === 'opt_012_015' && !(s >= 0.12 && s < 0.15)) return false;
        if (bucketFilter === 'warn_015_020' && !(s >= 0.15 && s < 0.20)) return false;
        if (bucketFilter === 'gte_020' && !(s >= 0.20)) return false;
      }
      if (rankingSearch && !r.code.includes(rankingSearch) && !r.name.includes(rankingSearch)) return false;
      return true;
    });
  }, [result, boardFilter, industryFilter, trendFilter, bucketFilter, capFilter, rankingSearch]);

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
          {/* 策略驾驶舱：BoardCard 自带卡片外壳，不再套 glass-panel 避免双层嵌套显乱 */}
          {result.summary && <StrategyDashboard summary={result.summary} market={detailMarket} />}
          {result.summary?.board_top1 && result.summary.board_top1.length > 0 && (
            <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <Text className="text-sm font-black text-slate-800 uppercase tracking-tight leading-none">板块 Top1 统计</Text>
                  <Text className="text-[11px] text-slate-400 font-medium">5大板块各自最高分取平均，反映市场广度</Text>
                </div>
                {result.summary.board_top1_avg !== undefined && result.summary.board_top1_avg !== null && (
                  <div className="flex items-center gap-2">
                    <Text className="text-xs text-slate-400 font-black uppercase tracking-wide">avg Top1</Text>
                    <span className={clsx('font-black text-base font-mono rounded-lg px-2.5 py-1',
                      result.summary.board_top1_avg >= 0.11 ? 'bg-rose-50 text-rose-600' : result.summary.board_top1_avg >= 0.09 ? 'bg-amber-50 text-amber-600' : 'bg-slate-100 text-slate-500')}>
                      {result.summary.board_top1_avg.toFixed(4)}
                    </span>
                    {result.summary.board_top1_avg >= 0.11
                      ? <Tag color="red" className="m-0 rounded-full text-[11px] font-black">市场信号偏强</Tag>
                      : result.summary.board_top1_avg >= 0.09
                        ? <Tag color="orange" className="m-0 rounded-full text-[11px] font-black">震荡偏强</Tag>
                        : <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-500 font-bold text-[11px]">市场偏弱</Tag>}
                  </div>
                )}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
                {result.summary.board_top1.map(b => (
                  <div key={b.board} className="rounded-2xl border border-slate-100 bg-slate-50/60 p-3">
                    <div className="flex items-center justify-between mb-2">
                      <Text className="text-xs font-black text-slate-400 uppercase tracking-wide">{b.board}</Text>
                      <Text className="text-[11px] font-mono text-slate-400 truncate max-w-[70px]">{b.top1_symbol}</Text>
                    </div>
                    <Text className={clsx('block font-black font-mono text-base', Number(b.top1_score) >= 0.11 ? 'text-rose-600' : 'text-slate-800')}>
                      {Number(b.top1_score).toFixed(4)}
                    </Text>
                    <Text className="text-xs text-slate-500 truncate block mt-0.5">{b.top1_name || '—'}</Text>
                  </div>
                ))}
              </div>
            </div>
          )}
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
          {result.summary?.score_distribution && (
            <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
              <ScoreDistributionPanel
                dist={result.summary.score_distribution}
                rankings={result.rankings}
                activeBucket={bucketFilter}
                onSelectBucket={(key) => setBucketFilter(key ?? 'all')}
              />
            </div>
          )}
          <div className="glass-panel rounded-3xl p-5 border border-slate-100/50 space-y-3">
            {/* 单行筛选：分数(含负分标注) 放最前 */}
            <div className="flex flex-wrap items-center gap-2">
              <Select
                value={bucketFilter}
                onChange={setBucketFilter}
                options={[{ value: 'all', label: '全部分数' }, ...bucketOptions]}
                className="w-52"
                size="small"
                placeholder="筛选分数/负分标注"
              />
              <Input
                prefix={<Search size={13} className="text-slate-300" />}
                placeholder="搜索股票代码或名称..."
                value={rankingSearch}
                onChange={e => setRankingSearch(e.target.value)}
                allowClear
                className="rounded-xl h-8 text-xs border-slate-200 w-44"
              />
              <Select
                value={boardFilter}
                onChange={setBoardFilter}
                options={[{ value: 'all', label: '全部板块' }, ...boardOptions]}
                className="w-36"
                size="small"
                placeholder="筛选板块"
              />
              <Select
                value={industryFilter}
                onChange={setIndustryFilter}
                options={[{ value: 'all', label: '全部行业' }, ...industryOptions]}
                className="w-44"
                size="small"
                showSearch
                optionFilterProp="label"
                placeholder="筛选行业"
              />
              <Select
                value={capFilter}
                onChange={setCapFilter}
                options={[{ value: 'all', label: '全部市值' }, ...capOptions]}
                className="w-40"
                size="small"
                placeholder="筛选市值"
              />
              <Select
                value={trendFilter}
                onChange={setTrendFilter}
                options={[{ value: 'all', label: '全部趋势' }, ...trendOptions]}
                className="w-44"
                size="small"
                placeholder="筛选趋势"
              />
              <Text className="text-xs text-slate-400 font-medium">筛选后 {filteredRankings.length} / {result.rankings.length} 支</Text>
            </div>
            <Table
              size="small"
              rowKey="rank"
              pagination={{ pageSize: 20, showTotal: t => `共 ${t} 支` }}
              dataSource={filteredRankings}
              onRow={(record: any) => ({
                onClick: () => openStockModal(record),
                className: 'cursor-pointer',
              })}
              rowClassName={(record: any) => clsx(
                record.negative_tag === '极端负分' ? 'bg-rose-50/70 hover:bg-rose-50!' :
                record.negative_tag === '做空候选' ? 'bg-rose-50/40 hover:bg-rose-50!' :
                record.negative_tag === '错杀候选' ? 'bg-blue-50/40 hover:bg-blue-50!' :
                record.negative_tag === '抗跌行业' ? 'bg-emerald-50/40 hover:bg-emerald-50!' : '',
              )}
              columns={[
                {
                  title: '排名', dataIndex: 'rank', width: 56,
                  render: (n: number) => (
                    <span className={clsx('font-black text-xs', n <= 3 ? 'text-amber-500' : 'text-slate-500')}>
                      {n <= 3 ? ['🥇', '🥈', '🥉'][n - 1] : n}
                    </span>
                  ),
                },
                {
                  title: '股票', key: 'stock',
                  render: (_: any, r: any) => {
                    const hasName = r.name && r.name !== r.code;
                    return (
                      <div>
                        <div className={clsx('text-xs font-black', hasName ? 'text-slate-800' : 'text-slate-400 italic')}>
                          {hasName ? r.name : '名称未匹配'}
                        </div>
                        <div className="text-xs font-mono text-slate-400">{r.code}</div>
                      </div>
                    );
                  },
                },
                ...(isHk ? [] : [{
                  title: '板块', dataIndex: 'board', width: 76,
                  render: (v: string) => {
                    const map: Record<string, { color: string; label: string }> = {
                      沪主板: { color: 'red', label: '沪主板' },
                      深主板: { color: 'blue', label: '深主板' },
                      中小板: { color: 'orange', label: '中小板' },
                      创业板: { color: 'green', label: '创业板' },
                      科创板: { color: 'purple', label: '科创板' },
                      北交所: { color: 'cyan', label: '北交所' },
                    };
                    const c = map[v];
                    if (!c) return <Text className="text-[11px] text-slate-300">—</Text>;
                    return <Tag color={c.color} className="text-[11px] font-black m-0">{c.label}</Tag>;
                  },
                }]),
                {
                  title: '行业', dataIndex: 'industry', width: 96,
                  render: (ind: string) => (
                    ind
                      ? <Tooltip title={ind}><Text className="text-xs text-slate-600 block truncate">{ind}</Text></Tooltip>
                      : <Text className="text-[11px] text-slate-300">—</Text>
                  ),
                },
                {
                  title: '市值', dataIndex: 'market_cap_tier', width: 76,
                  render: (tier: string, r: any) => {
                    const tmap: Record<string, { cls: string }> = {
                      微盘: { cls: 'text-rose-600 bg-rose-50 border-rose-100' },
                      小盘: { cls: 'text-orange-600 bg-orange-50 border-orange-100' },
                      中盘: { cls: 'text-amber-600 bg-amber-50 border-amber-100' },
                      大盘: { cls: 'text-blue-600 bg-blue-50 border-blue-100' },
                      超大盘: { cls: 'text-indigo-600 bg-indigo-50 border-indigo-100' },
                    };
                    const c = tmap[tier];
                    return (
                      <Tooltip title={r.market_cap_yi ? `${r.market_cap_yi.toFixed(1)} ${isHk ? '亿港元' : '亿'}` : '市值未知'}>
                        {c ? (
                          <span className={clsx('inline-block rounded-lg border px-2 py-0.5 text-[11px] font-black', c.cls)}>{tier}</span>
                        ) : (
                          <Text className="text-[11px] text-slate-300">—</Text>
                        )}
                      </Tooltip>
                    );
                  },
                },
                {
                  title: '趋势', dataIndex: 'trend', width: 92,
                  render: (t: string, r: any) => {
                    const map: Record<string, { cls: string; label: string }> = {
                      '先升后降': { cls: 'text-emerald-600 bg-emerald-50 border-emerald-100', label: '先升后降' },
                      '连续上升': { cls: 'text-rose-600 bg-rose-50 border-rose-100', label: '连续上升' },
                      '连续下降': { cls: 'text-slate-500 bg-slate-100 border-slate-200', label: '连续下降' },
                      '上升': { cls: 'text-emerald-600 bg-emerald-50 border-emerald-100', label: '↑ 升' },
                      '下降': { cls: 'text-slate-500 bg-slate-100 border-slate-200', label: '↓ 降' },
                      '持平': { cls: 'text-slate-400 bg-slate-50 border-slate-100', label: '→ 平' },
                    };
                    const c = map[t];
                    if (!c) return <Text className="text-[11px] text-slate-300">—</Text>;
                    return (
                      <Tooltip title={`T-2: ${r.prev2_score?.toFixed?.(4) ?? '—'} → T-1: ${r.prev_score?.toFixed?.(4) ?? '—'} → T: ${Number(r.score).toFixed(4)}`}>
                        <span className={clsx('inline-block rounded-lg border px-2 py-0.5 text-[11px] font-black', c.cls)}>{c.label}</span>
                      </Tooltip>
                    );
                  },
                },
                {
                  title: '得分', dataIndex: 'score',
                  render: (s: number) => {
                    // A股习惯：涨红跌绿；按当前批次分位数分级着色
                    const dist = result?.summary?.score_distribution as any;
                    let cls = s >= 0 ? 'text-rose-600' : 'text-emerald-600';
                    if (dist && typeof dist.p25 === 'number' && typeof dist.p75 === 'number') {
                      if (s >= dist.p75) cls = 'text-rose-600';
                      else if (s >= dist.p50) cls = 'text-orange-500';
                      else if (s >= dist.p25) cls = 'text-sky-600';
                      else cls = 'text-emerald-600';
                    }
                    return (
                      <span className={clsx('font-black text-xs font-mono', cls)}>
                        {s >= 0 ? '+' : ''}{s.toFixed(4)}
                      </span>
                    );
                  },
                },
                {
                  title: '信号',
                  dataIndex: 'signal',
                  width: 110,
                  render: (sig: string, r: any) => {
                    // A股习惯：做多红、做空绿
                    const map: Record<string, { color: string; label: string }> = {
                      buy: { color: 'red', label: '↑ 做多' },
                      sell: { color: 'green', label: '↓ 做空' },
                      hold: { color: 'default', label: '→ 持有' },
                    };
                    const c = map[sig] ?? map.hold;
                    // 策略评级：黄金区间 + 先升后降 = 强烈关注
                    // 融合模型分数为 [-1,1] 时，用后端 score_buckets 的自适应分位数阈值替代硬编码 0.10/0.12
                    const s = Number(r.score);
                    const wideScale = !!(result?.summary?.is_wide_scale || result?.summary?.market_signal?.score_scale === 'wide');
                    const buckets = result?.summary?.score_buckets || [];
                    const bucketGte = (key: string): number | null => {
                      const b = buckets.find(x => x.key === key);
                      const m = b ? String(b.label).match(/≥\s*(-?[\d.]+)/) : null;
                      return m ? parseFloat(m[1]) : null;
                    };
                    let isGold = false;
                    let isHigh = false;
                    let highLabel = '高分区';
                    if (wideScale) {
                      // 最高分区(≥80分位)视为首选，中高区间视为可选
                      const gte = bucketGte('gte_020');
                      const opt = bucketGte('opt_012_015');
                      isHigh = gte !== null && s >= gte;
                      highLabel = '最高分区 · 首选';
                      isGold = opt !== null && s >= opt && (gte === null || s < gte);
                    } else {
                      isGold = s >= 0.10 && s < 0.12;
                      isHigh = s >= 0.12;
                    }
                    const isBestTrend = r.trend === '先升后降';
                    const isOverheat = r.trend === '连续上升';
                    let rating: { cls: string; label: string } | null = null;
                    if (isGold && isBestTrend) rating = { cls: 'text-emerald-600 bg-emerald-50 border-emerald-200', label: wideScale ? '高分+最佳买点' : '黄金+最佳买点' };
                    else if (isGold) rating = { cls: 'text-emerald-600 bg-emerald-50 border-emerald-200', label: wideScale ? '中高分区' : '黄金区间' };
                    else if (isHigh && isOverheat) rating = { cls: 'text-rose-600 bg-rose-50 border-rose-200', label: '高分过热' };
                    else if (isHigh) rating = { cls: 'text-amber-600 bg-amber-50 border-amber-200', label: highLabel };
                    else rating = { cls: 'text-slate-500 bg-slate-100 border-slate-200', label: '弱信号' };
                    // 负分标签（含抗跌行业/一般负分）
                    const negTag = r.negative_tag;
                    const negCls = negTag === '极端负分' ? 'text-rose-700 bg-rose-100 border-rose-200'
                      : negTag === '做空候选' ? 'text-rose-600 bg-rose-50 border-rose-200'
                      : negTag === '错杀候选' ? 'text-blue-600 bg-blue-50 border-blue-200'
                      : negTag === '抗跌行业' ? 'text-emerald-600 bg-emerald-50 border-emerald-200'
                      : negTag === '负分' ? 'text-slate-500 bg-slate-100 border-slate-200' : null;
                    const negTooltip: Record<string, string> = {
                      '极端负分': '分数≤-0.20，微盘最危险（-0.25 → 下跌77.7%）',
                      '做空候选': '微盘/小盘 + 分数≤-0.15，下跌概率68-72%，做空首选',
                      '错杀候选': '大盘/超大盘负分，常被错杀，反而值得关注',
                      '抗跌行业': '银行/半导体等抗跌行业负分，不跌反涨概率高',
                      '负分': '一般负分，轻负分(>-0.06)无信息',
                    };
                    return (
                      <div className="flex flex-col items-start gap-1">
                        <Tag color={c.color} className="text-[11px] font-black m-0">{c.label}</Tag>
                        {rating && (
                          <span className={clsx('rounded-md border px-1.5 py-0.5 text-[11px] font-black', rating.cls)}>{rating.label}</span>
                        )}
                        {negCls && (
                          <Tooltip title={negTooltip[negTag] || negTag}>
                            <span className={clsx('rounded-md border px-1.5 py-0.5 text-[11px] font-black cursor-help', negCls)}>{negTag}</span>
                          </Tooltip>
                        )}
                      </div>
                    );
                  },
                },
              ]}
            />
          </div>
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
          const curIdx = filteredRankings.findIndex(r => r.code === stockModal.symbol);
          const hasPrev = curIdx > 0;
          const hasNext = curIdx >= 0 && curIdx < filteredRankings.length - 1;
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
                    ? `第 ${stockModal.rank ?? curIdx + 1} 名 · ${curIdx + 1}/${filteredRankings.length}`
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
