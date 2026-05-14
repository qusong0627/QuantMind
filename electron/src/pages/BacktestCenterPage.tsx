/**
 * 回测中心页面
 */

import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, TrendingUp, TrendingDown, DollarSign, Shield, Target, Activity, RefreshCw } from 'lucide-react';
import { BacktestHistory } from '../components/backtest/BacktestHistory';
import type { BacktestResult } from '../services/backtestService';
import { useBacktestStore } from '../stores/backtestStore';
import ReactECharts from 'echarts-for-react';
import { PAGE_LAYOUT } from '../config/pageLayout';

export const BacktestCenterPage: React.FC = () => {
  const [selectedBacktest, setSelectedBacktest] = useState<BacktestResult | null>(null);
  const { config } = useBacktestStore();
  const userId = config.user_id || 'default_user';

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <div className={PAGE_LAYOUT.headerClass} style={{ height: PAGE_LAYOUT.headerHeight }}>
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center shadow-md">
              <TrendingUp className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-lg font-bold text-gray-800 leading-tight">回测中心</h1>
              <p className="text-[10px] text-gray-400 uppercase tracking-wider font-semibold">Backtest & Analysis</p>
            </div>
          </div>
        </div>
        <div className={PAGE_LAYOUT.scrollContainerClass}>
          <div className={PAGE_LAYOUT.contentInnerClass + " p-6"}>
            <BacktestHistory userId={userId} onViewDetail={(backtest) => setSelectedBacktest(backtest)} />
          </div>
        </div>
        <AnimatePresence>
          {selectedBacktest && (
            <BacktestDetailModal backtest={selectedBacktest} onClose={() => setSelectedBacktest(null)} />
          )}
        </AnimatePresence>
      </div>
    </div>
  );
};

interface BacktestDetailModalProps {
  backtest: BacktestResult;
  onClose: () => void;
}

const BacktestDetailModal: React.FC<BacktestDetailModalProps> = ({ backtest: initialBacktest, onClose }) => {
  const [backtest, setBacktest] = useState<BacktestResult>(initialBacktest);
  const [isLoadingFull, setIsLoadingFull] = useState(false);
  const [activeTab, setActiveTab] = useState<'overview' | 'analysis' | 'trades'>('overview');

  React.useEffect(() => {
    const fetchFullResult = async () => {
      if (initialBacktest.backtest_id) {
        setIsLoadingFull(true);
        try {
          const { backtestService } = await import('../services/backtestService');
          const fullResult = await backtestService.getResult(initialBacktest.backtest_id, true);
          setBacktest({ ...initialBacktest, ...fullResult });
        } catch (err) {
          console.error('获取完整回测结果失败:', err);
        } finally {
          setIsLoadingFull(false);
        }
      }
    };
    fetchFullResult();
  }, [initialBacktest.backtest_id]);

  const metrics = [
    { label: '总收益率', value: `${((backtest.total_return || 0) * 100).toFixed(2)}%`, icon: TrendingUp, color: (backtest.total_return || 0) > 0 ? 'text-green-600' : 'text-red-600' },
    { label: '年化收益', value: `${((backtest.annual_return || 0) * 100).toFixed(2)}%`, icon: Activity, color: 'text-blue-600' },
    { label: '夏普比率', value: (backtest.sharpe_ratio || 0).toFixed(2), icon: Shield, color: 'text-purple-600' },
    { label: '最大回撤', value: `${((backtest.max_drawdown || 0) * 100).toFixed(2)}%`, icon: TrendingDown, color: 'text-orange-600' },
    { label: '交易日盈亏占比', value: `${((backtest.win_rate || 0) * 100).toFixed(2)}%`, icon: Target, color: 'text-cyan-600' },
    { label: '盈亏比', value: (backtest.profit_factor || 0).toFixed(2), icon: DollarSign, color: 'text-yellow-600' },
  ];

  const equityCurveOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(255, 255, 255, 0.95)', borderColor: 'rgba(0, 0, 0, 0.1)', textStyle: { color: '#374151' } },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '3%', containLabel: true },
    xAxis: { type: 'category', data: backtest.equity_curve?.map((d) => d.date) || [], axisLine: { lineStyle: { color: '#d1d5db' } }, axisLabel: { color: '#6b7280' } },
    yAxis: { type: 'value', axisLine: { lineStyle: { color: '#d1d5db' } }, axisLabel: { color: '#6b7280' }, splitLine: { lineStyle: { color: '#e5e7eb' } } },
    series: [{ name: '权益', type: 'line', data: backtest.equity_curve?.map((d) => d.value) || [], smooth: true, lineStyle: { width: 2, color: '#3b82f6' }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(59, 130, 246, 0.3)' }, { offset: 1, color: 'rgba(59, 130, 246, 0.05)' }] } } }],
  };

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <motion.div initial={{ scale: 0.9, y: 20 }} animate={{ scale: 1, y: 0 }} exit={{ scale: 0.9, y: 20 }} onClick={(e) => e.stopPropagation()} className="bg-white rounded-2xl border border-gray-200 shadow-xl max-w-4xl w-full max-h-[90vh] overflow-auto">
        <div className="sticky top-0 z-10 bg-white border-b border-gray-200 p-4">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-2xl font-bold text-gray-800 mb-1">回测详情</h2>
              <p className="text-sm text-gray-600">{backtest.symbol} | {backtest.start_date} ~ {backtest.end_date}</p>
            </div>
            <button onClick={onClose} className="p-2 hover:bg-gray-100 rounded-2xl transition-colors"><X className="w-6 h-6 text-gray-600" /></button>
          </div>
          <div className="flex gap-6 mt-4">
            {[ { id: 'overview', label: '概览', icon: Activity }, { id: 'analysis', label: '因子分析', icon: Target }, { id: 'trades', label: '成交记录', icon: DollarSign } ].map((tab) => (
              <button key={tab.id} onClick={() => setActiveTab(tab.id as any)} className={`flex items-center gap-2 pb-2 text-sm font-bold transition-all border-b-2 ${activeTab === tab.id ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-400 hover:text-gray-600'}`}>
                <tab.icon className="w-4 h-4" />{tab.label}
              </button>
            ))}
          </div>
        </div>

        <div className="p-4 space-y-6">
          {activeTab === 'overview' && (
            <div className="space-y-6">
              <div className="grid grid-cols-3 gap-4">
                {metrics.map((metric) => (
                  <div key={metric.label} className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
                    <div className="flex items-center justify-between mb-2"><span className="text-xs text-gray-500 font-bold uppercase">{metric.label}</span><metric.icon className={`w-4 h-4 ${metric.color}`} /></div>
                    <div className={`text-2xl font-bold ${metric.color}`}>{metric.value}</div>
                  </div>
                ))}
              </div>
              {backtest.equity_curve && (
                <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
                  <h3 className="text-sm font-bold text-gray-500 uppercase mb-4">收益曲线</h3>
                  <ReactECharts option={equityCurveOption} style={{ height: '300px' }} notMerge lazyUpdate />
                </div>
              )}
              <div className="grid grid-cols-2 gap-4">
                <DrawdownChart drawdownData={backtest.drawdown_curve || []} />
                <ReturnsDistributionChart equityCurve={backtest.equity_curve || []} />
              </div>
            </div>
          )}

          {activeTab === 'analysis' && (
            <div className="space-y-6">
              <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-bold text-gray-500 uppercase">高级分层收益统计</h3>
                  {isLoadingFull && <RefreshCw className="w-4 h-4 animate-spin text-blue-500" />}
                </div>
                {backtest.stratified_returns ? <StratifiedReturnsChart data={backtest.stratified_returns} /> : <div className="h-[300px] flex items-center justify-center text-gray-400">正在获取统计数据...</div>}
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
                  <h3 className="text-sm font-bold text-gray-500 uppercase mb-4">风格暴露分析</h3>
                  {backtest.style_attribution ? <StyleExposureChart attribution={backtest.style_attribution} /> : <div className="h-[200px] flex items-center justify-center text-gray-400">正在分析...</div>}
                </div>
                <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
                  <h3 className="text-sm font-bold text-gray-500 uppercase mb-4">因子质量看板</h3>
                  {backtest.factor_metrics ? <FactorQualityChart metrics={backtest.factor_metrics} /> : <div className="h-[200px] flex items-center justify-center text-gray-400">正在分析...</div>}
                </div>
              </div>
            </div>
          )}

          {activeTab === 'trades' && (
            <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
              <h3 className="text-sm font-bold text-gray-500 uppercase mb-4">成交记录分析</h3>
              <BacktestTradesSection backtestId={backtest.backtest_id || ''} />
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
};

const StratifiedReturnsChart: React.FC<{ data: any[] }> = ({ data }) => {
  const option = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: data.map(d => `Group ${d.group}`), axisLabel: { color: '#6b7280' } },
    yAxis: { type: 'value', axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(1)}%` }, splitLine: { lineStyle: { color: '#e5e7eb' } } },
    series: [{ name: '年化收益', type: 'bar', data: data.map(d => d.annual_return), itemStyle: { color: (params: any) => ['#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981'][params.dataIndex % 5] }, label: { show: true, position: 'top', formatter: (p: any) => `${(p.value * 100).toFixed(1)}%`, fontSize: 10 } }]
  };
  return <ReactECharts option={option} style={{ height: '300px' }} />;
};

const BacktestTradesSection: React.FC<{ backtestId: string }> = ({ backtestId }) => {
  const [data, setData] = useState<{ trades: any[], positions: any[] } | null>(null);
  const [loading, setLoading] = useState(true);
  React.useEffect(() => { if (backtestId) backtestService.getTrades(backtestId).then(setData).catch(console.error).finally(() => setLoading(false)); }, [backtestId]);
  if (loading) return <div className="py-20 text-center text-gray-400">正在获取流水数据...</div>;
  if (!data || data.trades.length === 0) return <div className="py-20 text-center text-gray-400">暂无成交记录</div>;
  return (
    <div className="bg-white rounded-xl border border-gray-100 overflow-hidden shadow-inner">
      <table className="w-full text-[10px] text-left">
        <thead className="bg-gray-50 text-gray-400 font-bold uppercase border-b border-gray-100">
          <tr><th className="px-4 py-2">日期</th><th className="px-4 py-2">标的</th><th className="px-4 py-2 text-right">价格</th><th className="px-4 py-2 text-right">数量</th></tr>
        </thead>
        <tbody className="divide-y divide-gray-50">
          {data.trades.slice(0, 50).map((t, idx) => (
            <tr key={idx} className="hover:bg-blue-50/20"><td className="px-4 py-1 text-gray-400 font-mono italic">{t.datetime}</td><td className="px-4 py-1 font-bold text-gray-700">{t.instrument}</td><td className="px-4 py-1 text-right font-mono">¥{t.price?.toFixed(2)}</td><td className="px-4 py-1 text-right font-mono">{t.volume}</td></tr>
          ))}
        </tbody>
      </table>
      {data.trades.length > 50 && <div className="p-2 text-center text-[10px] text-gray-400 border-t border-gray-50 bg-gray-50/50">仅显示前 50 条 (共 {data.trades.length} 条)</div>}
    </div>
  );
};

const DrawdownChart: React.FC<{ drawdownData: any[] }> = ({ drawdownData }) => {
  const option = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(255, 255, 255, 0.95)', borderColor: 'rgba(0, 0, 0, 0.1)', textStyle: { color: '#374151' } },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '3%', containLabel: true },
    xAxis: { type: 'category', data: drawdownData.map((d) => d.date), axisLine: { lineStyle: { color: '#d1d5db' } }, axisLabel: { color: '#6b7280' } },
    yAxis: { type: 'value', axisLine: { lineStyle: { color: '#d1d5db' } }, axisLabel: { color: '#6b7280', formatter: (v: number) => `${(v * 100).toFixed(0)}%` }, splitLine: { lineStyle: { color: '#e5e7eb' } } },
    series: [{ name: '回撤', type: 'line', data: drawdownData.map((d) => d.value), smooth: true, lineStyle: { width: 2, color: '#ef4444' }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(239, 68, 68, 0.3)' }, { offset: 1, color: 'rgba(239, 68, 68, 0.05)' }] } } }],
  };
  return (
    <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
      <h3 className="text-xs font-bold text-gray-500 uppercase mb-4">回撤曲线</h3>
      <ReactECharts option={option} style={{ height: '200px' }} notMerge lazyUpdate />
    </div>
  );
};

const ReturnsDistributionChart: React.FC<{ equityCurve: any[] }> = ({ equityCurve }) => {
  const dailyReturns: number[] = [];
  for (let i = 1; i < equityCurve.length; i++) {
    const prev = equityCurve[i - 1].value;
    const curr = equityCurve[i].value;
    dailyReturns.push(((curr - prev) / prev) * 100);
  }
  const bins = 20;
  const min = Math.min(...dailyReturns);
  const max = Math.max(...dailyReturns);
  const binWidth = (max - min) / bins;
  const histogram = Array(bins).fill(0);
  const binLabels = [];
  for (let i = 0; i < bins; i++) {
    const start = min + i * binWidth;
    binLabels.push(`${start.toFixed(1)}%`);
    dailyReturns.forEach(r => { if (r >= start && r < start + binWidth) histogram[i]++; });
  }
  const option = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '3%', containLabel: true },
    xAxis: { type: 'category', data: binLabels, axisLabel: { color: '#6b7280', fontSize: 8, rotate: 45 } },
    yAxis: { type: 'value', axisLabel: { color: '#6b7280' }, splitLine: { lineStyle: { color: '#e5e7eb' } } },
    series: [{ name: '频次', type: 'bar', data: histogram, itemStyle: { color: '#3b82f6' } }],
  };
  return (
    <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
      <h3 className="text-xs font-bold text-gray-500 uppercase mb-4">收益分布直方图</h3>
      <ReactECharts option={option} style={{ height: '200px' }} notMerge lazyUpdate />
    </div>
  );
};

const StyleExposureChart: React.FC<{ attribution: any }> = ({ attribution }) => {
  const indicators = [ { name: 'Size', key: 'size' }, { name: 'Value', key: 'value' }, { name: 'Momentum', key: 'momentum' }, { name: 'Volatility', key: 'volatility' } ];
  const portfolioData = indicators.map(i => attribution.portfolio?.[i.key] || 0);
  const benchmarkData = indicators.map(i => attribution.benchmark?.[i.key] || 0);
  const option = {
    legend: { data: ['组合', '基准'], bottom: 0, textStyle: { fontSize: 10 } },
    radar: { indicator: indicators.map(i => ({ name: i.name, max: 2, min: -2 })), axisName: { color: '#374151', fontSize: 10 } },
    series: [{ type: 'radar', data: [ { value: benchmarkData, name: '基准', lineStyle: { type: 'dashed' }, itemStyle: { color: '#9ca3af' }, areaStyle: { color: 'rgba(156, 163, 175, 0.1)' } }, { value: portfolioData, name: '组合', itemStyle: { color: '#3b82f6' }, areaStyle: { color: 'rgba(59, 130, 246, 0.2)' } } ] }]
  };
  return <ReactECharts option={option} style={{ height: '200px' }} />;
};

const FactorQualityChart: React.FC<{ metrics: any }> = ({ metrics }) => {
  const option = {
    tooltip: { trigger: 'item' },
    series: [{ name: '质量', type: 'pie', radius: ['40%', '70%'], avoidLabelOverlap: false, itemStyle: { borderRadius: 5, borderColor: '#fff', borderWidth: 1 }, label: { show: false }, data: [ { value: Math.abs(metrics.rank_ic || 0), name: 'Rank IC', itemStyle: { color: '#3b82f6' } }, { value: Math.abs(metrics.icir || 0) / 2, name: 'ICIR', itemStyle: { color: '#8b5cf6' } }, { value: 0.1, name: '稳定', itemStyle: { color: '#10b981' } } ] }]
  };
  return <ReactECharts option={option} style={{ height: '150px' }} />;
};

const MonthlyReturnsHeatmap: React.FC<{ monthlyReturns: any[] }> = ({ monthlyReturns }) => {
  const heatmapData: any[] = [];
  const yearsSet = new Set<string>();
  monthlyReturns.forEach(item => {
    const [year, month] = item.month.split('-');
    yearsSet.add(year);
    heatmapData.push([parseInt(month) - 1, Array.from(yearsSet).sort().indexOf(year), item.return * 100]);
  });
  const yearLabels = Array.from(yearsSet).sort();
  const option = {
    tooltip: { position: 'top', formatter: (p: any) => `${yearLabels[p.value[1]]}-${p.value[0]+1}月: ${p.value[2].toFixed(2)}%` },
    grid: { left: '3%', right: '3%', bottom: '3%', top: '3%', containLabel: true },
    xAxis: { type: 'category', data: ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'], axisLabel: { fontSize: 8 } },
    yAxis: { type: 'category', data: yearLabels, axisLabel: { fontSize: 8 } },
    visualMap: { min: -5, max: 5, calculable: true, orient: 'horizontal', left: 'center', bottom: 0, inRange: { color: ['#ef4444', '#ffffff', '#10b981'] } },
    series: [{ name: '月度收益', type: 'heatmap', data: heatmapData, label: { show: true, fontSize: 8, formatter: (p: any) => `${p.value[2].toFixed(1)}%` } }]
  };
  return <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200"><h3 className="text-xs font-bold text-gray-500 uppercase mb-4">月度收益热力图</h3><ReactECharts option={option} style={{ height: '200px' }} /></div>;
};

const YearlyReturnsChart: React.FC<{ yearlyReturns: any[] }> = ({ yearlyReturns }) => {
  const option = {
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: yearlyReturns.map(d => d.year), axisLabel: { fontSize: 8 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 8, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
    series: [{ name: '年度收益', type: 'bar', data: yearlyReturns.map(d => ({ value: d.return, itemStyle: { color: d.return > 0 ? '#10b981' : '#ef4444' } })), label: { show: true, position: 'top', fontSize: 8, formatter: (p: any) => `${(p.value * 100).toFixed(1)}%` } }]
  };
  return <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200"><h3 className="text-xs font-bold text-gray-500 uppercase mb-4">年度收益对比</h3><ReactECharts option={option} style={{ height: '200px' }} /></div>;
};
