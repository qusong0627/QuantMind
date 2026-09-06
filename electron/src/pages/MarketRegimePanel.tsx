import React, { useEffect, useMemo, useState } from 'react';
import { Card, Spin, Typography, Tag, Collapse, Empty } from 'antd';
import { TrendingUp, Info } from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import { modelTrainingService } from '../services/modelTrainingService';

const { Text } = Typography;

export const MarketRegimePanel: React.FC<{ modelId: string }> = ({ modelId }) => {
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<any>(null);

  useEffect(() => {
    if (!modelId) return;
    setLoading(true);
    modelTrainingService
      .getMarketRegime(modelId, 90)
      .then(setData)
      .catch(() => setData({ series: [] }))
      .finally(() => setLoading(false));
  }, [modelId]);

  const series: any[] = data?.series ?? [];
  const current = data?.current ?? null;

  const option = useMemo(() => {
    if (!series.length) return null;
    const dates = series.map((s: any) => s.trade_date);
    const vals = series.map((s: any) => s.avg_score);
    return {
      tooltip: { trigger: 'axis', formatter: (ps: any) => {
        const p = ps[0];
        const row = series[p.dataIndex];
        return `${row.trade_date}<br/>均值 ${row.avg_score} 中位数 ${row.median_score} 样本 ${row.count}`;
      }},
      grid: { left: 48, right: 16, top: 24, bottom: 28 },
      xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: 10, color: '#64748b' } },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 10, color: '#64748b', formatter: (v: number) => v.toFixed(3) },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      series: [
        {
          type: 'line',
          data: vals,
          smooth: true,
          symbol: 'circle',
          symbolSize: 3,
          lineStyle: { width: 2, color: '#3b82f6' },
          itemStyle: { color: '#3b82f6' },
          areaStyle: { color: 'rgba(59,130,246,0.08)' },
        },
      ],
    };
  }, [series]);

  if (loading) return <div className="flex justify-center py-12"><Spin /></div>;
  if (!series.length) return <Empty description="暂无推理均分时序" />;

  return (
    <div className="space-y-4">
      <Card size="small" className="rounded-2xl" >
        <Collapse
          ghost
          items={[{
            key: 'explain',
            label: <span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><Info size={12} className="text-blue-500"/>口径说明</span>,
            children: <div className="text-xs text-slate-600 leading-relaxed space-y-1">
              <div>均分 = 当日全市场 `pred / fusion_score` 均值（与个股终端一致，回退读 `pred.parquet`）。</div>
              <div>仅展示最近 90 交易日，曲线反映模型对全市场整体的平均打分走势。</div>
            </div>,
          }]}
        />
      </Card>

      <div className="grid grid-cols-2 gap-3">
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">最新均分</div>
          <div className="text-lg font-mono font-black text-blue-600">{Number(current?.avg_score ?? 0).toFixed(4)}</div>
          <div className="text-[11px] text-slate-500 mt-1">{current?.trade_date} · 全市场 {current?.count} 只</div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">样本</div>
          <div className="text-lg font-mono font-black text-slate-800">{series.length} 日</div>
          <div className="text-[11px] text-slate-400">近 {series[0]?.trade_date} ~ {series[series.length-1]?.trade_date}</div>
        </Card>
      </div>

      <Card className="rounded-2xl" size="small">
        <div className="flex items-center gap-2 mb-2">
          <TrendingUp size={14} className="text-blue-500" />
          <Text className="text-xs font-black text-slate-700">全市场均分时序</Text>
        </div>
        {option && <ReactECharts option={option} style={{ height: 320 }} />}
      </Card>
    </div>
  );
};

export default MarketRegimePanel;
