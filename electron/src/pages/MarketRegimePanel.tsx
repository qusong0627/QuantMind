import React, { useEffect, useMemo, useState } from 'react';
import { Button, Card, Empty, Select, Spin, Tag, Typography, Collapse } from 'antd';
import { TrendingUp, Info } from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import { modelTrainingService } from '../services/modelTrainingService';
import {
  INDEX_COLORS,
  rebaseIndexPoints,
  regimeIndexOptions,
  resolveScoreRows,
  type RegimeIndexOption,
  type RegimeIndexSeries,
} from './marketRegimeModel';

const { Text } = Typography;

export const MarketRegimePanel: React.FC<{ modelId: string; market?: string }> = ({
  modelId,
  market,
}) => {
  const fallbackOptions = useMemo(() => regimeIndexOptions(market), [market]);
  const marketLabel = String(market || 'CN').toUpperCase();
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<any>(null);
  // 默认取本市场首个指数（CN=沪深300 / HK=恒生 / US=标普500 / 期货=中证商品）；
  // 数据到达后若该指数无序列，再收敛到实际可用指数。
  const [selectedIndices, setSelectedIndices] = useState<string[]>([]);
  // 均分是信号日 T 的分数，指数默认错开 T+1（次日可交易）；可切 T+N / 不错开
  const [indexOffset, setIndexOffset] = useState(1);

  useEffect(() => {
    // 市场切换：先落到本市场默认指数，避免保留上一市场的 000300.SH
    setSelectedIndices(fallbackOptions[0] ? [fallbackOptions[0].code] : []);
  }, [fallbackOptions]);

  useEffect(() => {
    if (!modelId) return;
    setLoading(true);
    modelTrainingService
      .getMarketRegime(modelId, 90, indexOffset)
      .then(setData)
      .catch(() => setData({ series: [], indices: [] }))
      .finally(() => setLoading(false));
  }, [modelId, indexOffset]);

  const series: any[] = data?.series ?? [];
  const current = data?.current ?? null;
  const indexOptions: RegimeIndexOption[] =
    (data?.index_options?.length ?? 0) > 0 ? data.index_options : fallbackOptions;
  const availableIndices: RegimeIndexSeries[] = data?.indices ?? [];

  // 模型切换后若原选择不在新数据里，收敛到首个可用指数（不静默画空线）
  useEffect(() => {
    if (!availableIndices.length) return;
    const available = availableIndices.map((x) => x.code);
    const kept = selectedIndices.filter((code) => available.includes(code));
    if (kept.length === selectedIndices.length) return;
    setSelectedIndices(kept.length > 0 ? kept : [available[0]]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const selectedSeries = availableIndices.filter((x) => selectedIndices.includes(x.code));
  // 单选一个有范围的指数 → 均分用该指数范围（成分股/沪市/深市）；多选/仅均分 → 全市场
  const scopedIndex = selectedSeries.length === 1 ? selectedSeries[0] : null;
  const scoreInfo = useMemo(() => {
    const dates = series.map((s: any) => String(s.trade_date));
    const allRows = series.map((s: any) => ({
      trade_date: String(s.trade_date),
      avg_score: s.avg_score,
      median_score: s.median_score,
      count: Number(s.count || 0),
    }));
    return resolveScoreRows(allRows, scopedIndex, dates);
  }, [series, scopedIndex]);
  const scoreName =
    scoreInfo.scoped && scopedIndex ? `${scopedIndex.name} 均分` : '全市场均分';

  const offsetOptions = useMemo(() => {
    const horizon = Number(data?.horizon_days) || 1;
    const options = [
      { value: 0, label: '不错开（T+0）' },
      { value: 1, label: '错开 T+1（次日）' },
    ];
    if (horizon > 1) {
      options.push({ value: horizon, label: `错开 T+${horizon}（模型周期）` });
    }
    return options;
  }, [data?.horizon_days]);

  const option = useMemo(() => {
    if (!series.length) return null;
    const dates = scoreInfo.rows.map((r) => r.trade_date);
    const vals = scoreInfo.rows.map((r) => r.avg_score);
    const hasIndex = selectedSeries.length > 0;
    const indexLines = selectedSeries.map((ix) => ({
      name: ix.name,
      type: 'line' as const,
      yAxisIndex: 1,
      data: rebaseIndexPoints(ix.points, dates),
      connectNulls: true,
      showSymbol: false,
      smooth: false,
      lineStyle: { width: 1.5, color: INDEX_COLORS[ix.code] ?? '#64748b' },
      itemStyle: { color: INDEX_COLORS[ix.code] ?? '#64748b' },
      emphasis: { focus: 'series' as const },
    }));
    return {
      color: ['#3b82f6', ...selectedSeries.map((ix) => INDEX_COLORS[ix.code] ?? '#64748b')],
      tooltip: {
        trigger: 'axis',
        formatter: (ps: any) => {
          const list = Array.isArray(ps) ? ps : [ps];
          const idx = list[0]?.dataIndex ?? 0;
          const row = scoreInfo.rows[idx] ?? series[idx] ?? {};
          const lines = [
            `<b>${row.trade_date ?? ''}</b>`,
            `${scoreName} ${row.avg_score ?? '—'} · 中位 ${row.median_score ?? '—'} · 样本 ${row.count ?? 0}`,
          ];
          for (const p of list) {
            if (p.seriesName === '均分') continue;
            const ix = availableIndices.find((x) => x.name === p.seriesName);
            const point = ix?.points.find(
              (pt) => String(pt.trade_date).slice(0, 10) === row.trade_date,
            );
            const indexDay =
              point?.index_date && point.index_date !== row.trade_date
                ? ` · 指数日 ${point.index_date}`
                : '';
            lines.push(
              `${p.marker ?? ''}${p.seriesName}: ${
                p.value == null ? '—' : Number(p.value).toFixed(2)
              }${point?.close != null ? `（收盘 ${point.close}${indexDay}）` : ''}`,
            );
          }
          return lines.join('<br/>');
        },
      },
      legend: {
        top: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { fontSize: 10 },
        data: [scoreName, ...selectedSeries.map((x) => x.name)],
      },
      grid: { left: 52, right: hasIndex ? 56 : 16, top: 30, bottom: 28 },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLabel: { fontSize: 10, color: '#64748b' },
      },
      yAxis: hasIndex
        ? [
            {
              type: 'value',
              name: '均分',
              nameTextStyle: { fontSize: 10, color: '#64748b' },
              axisLabel: { fontSize: 10, color: '#64748b', formatter: (v: number) => v.toFixed(3) },
              splitLine: { lineStyle: { color: '#f1f5f9' } },
            },
            {
              type: 'value',
              name: '指数(首日=100)',
              nameTextStyle: { fontSize: 10, color: '#64748b' },
              position: 'right',
              scale: true,
              axisLabel: { fontSize: 10, color: '#64748b', formatter: (v: number) => v.toFixed(1) },
              splitLine: { show: false },
            },
          ]
        : [
            {
              type: 'value',
              axisLabel: { fontSize: 10, color: '#64748b', formatter: (v: number) => v.toFixed(3) },
              splitLine: { lineStyle: { color: '#f1f5f9' } },
            },
          ],
      series: [
        {
          name: scoreName,
          type: 'line',
          yAxisIndex: 0,
          data: vals,
          smooth: true,
          symbol: 'circle',
          symbolSize: 3,
          connectNulls: true,
          lineStyle: { width: 2, color: '#3b82f6' },
          itemStyle: { color: '#3b82f6' },
          areaStyle: { color: 'rgba(59,130,246,0.08)' },
        },
        ...indexLines,
      ],
    };
  }, [series, selectedSeries, availableIndices, scoreInfo, scoreName]);

  if (loading) return <div className="flex justify-center py-12"><Spin /></div>;
  if (!series.length) return <Empty description="暂无推理均分时序" />;

  const latestScoped = scoreInfo.scoped
    ? [...scoreInfo.rows].reverse().find((r) => r.avg_score != null) ?? null
    : null;
  const latestScore = latestScoped ?? current;
  const latestLabel =
    scoreInfo.scoped && scopedIndex ? `最新均分（${scopedIndex.name}）` : '最新均分';

  const indexHint =
    marketLabel === 'HK'
      ? '可多选恒生指数 / 恒生国企 / 恒生科技 / 恒生红筹；「全部叠加」一次画全。'
      : marketLabel === 'US'
        ? '可多指标普500 / 纳斯达克100 / 道琼斯工业 / 纳斯达克综指；「全部叠加」一次画全。'
        : marketLabel === 'FUTURES'
          ? '可叠加中证商品期货指数 / 中证商品期货价格指数；「全部叠加」一次画全。'
          : marketLabel === 'CRYPTO'
            ? '本市场暂无指数叠加，只显示均分曲线。'
            : '可多选沪深300 / 中证500 / 中证1000 / 上证综指 / 深证成指；「全部叠加」一次画全。';
  const scopeHint = availableIndices.some(
    (x) => x.scope === 'constituents' && x.points.some((p) => p.avg_score != null),
  )
    ? '**单选有成分映射的指数时，均分按该指数范围计算**；多选或「仅均分」时用全市场均分。'
    : '本市场指数无成分映射（或数据未覆盖），单选指数只叠加走势，均分仍为全市场均分。';

  return (
    <div className="space-y-4">
      <Card size="small" className="rounded-2xl">
        <Collapse
          ghost
          items={[{
            key: 'explain',
            label: <span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><Info size={12} className="text-blue-500"/>口径说明</span>,
            children: <div className="text-xs text-slate-600 leading-relaxed space-y-1">
              <div>均分 = 当日全市场 `pred / fusion_score` 均值（与个股终端一致，回退读 `pred.parquet`）。</div>
              <div>均分日期 = **信号日 T**（用 T 日及之前数据算出）；指数默认取 **T+1（次日可交易）** 对齐，可切换 T+N / 不错开。</div>
              <div>{scopeHint}</div>
              <div>指数叠加用于**走势对比**：按窗口首个有效日归一为 100，与均分（左轴）分轴显示，不改变均分口径。</div>
              <div>{indexHint}</div>
            </div>,
          }]}
        />
      </Card>

      <div className="grid grid-cols-2 gap-3">
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">{latestLabel}</div>
          <div className="text-lg font-mono font-black text-blue-600">
            {latestScore?.avg_score == null ? '—' : Number(latestScore.avg_score).toFixed(4)}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">
            {latestScore?.trade_date} · 样本 {latestScore?.count ?? 0} 只
          </div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">样本</div>
          <div className="text-lg font-mono font-black text-slate-800">{series.length} 日</div>
          <div className="text-[11px] text-slate-400">近 {series[0]?.trade_date} ~ {series[series.length-1]?.trade_date}</div>
        </Card>
      </div>

      <Card className="rounded-2xl" size="small">
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <TrendingUp size={14} className="text-blue-500" />
          <Text className="text-xs font-black text-slate-700">均分时序 × 大盘指数</Text>
          {scoreInfo.scoped && scopedIndex && (
            <Tag color="blue" className="text-[10px]">
              {scopedIndex.name}
              {scopedIndex.constituent_count ? ` · ${scopedIndex.constituent_count} 只` : ''}
            </Tag>
          )}
          <Select
            mode="multiple"
            size="small"
            allowClear
            value={selectedIndices}
            onChange={(v) => setSelectedIndices(v as string[])}
            options={indexOptions.map((o) => ({ value: o.code, label: o.name }))}
            placeholder="选择对比指数"
            maxTagCount="responsive"
            style={{ minWidth: 240, maxWidth: 420 }}
            className="ml-2"
            disabled={indexOptions.length === 0}
          />
          <Select
            size="small"
            value={indexOffset}
            onChange={(v) => setIndexOffset(Number(v))}
            options={offsetOptions}
            style={{ width: 190 }}
            title="均分日期=信号日 T；指数默认取 T+1（次日可交易）"
          />
          {indexOptions.length > 0 ? (
            <>
              <Button
                size="small"
                onClick={() => setSelectedIndices(indexOptions.map((o) => o.code))}
              >
                全部叠加
              </Button>
              <Button size="small" onClick={() => setSelectedIndices([])}>
                仅均分
              </Button>
            </>
          ) : (
            <Tag className="text-[10px]">本市场无指数叠加</Tag>
          )}
          {selectedSeries.length > 0 && (
            <span className="ml-auto text-[10px] text-slate-400">
              指数按窗口首日=100 归一 · 右轴 · 信号日 T → 指数 T+{indexOffset}
            </span>
          )}
          {data?.indices_error && (
            <Tag color="warning" className="text-[10px]">
              指数读取失败：{String(data.indices_error).slice(0, 80)}
            </Tag>
          )}
        </div>
        {option && <ReactECharts option={option} style={{ height: 340 }} notMerge />}
      </Card>
    </div>
  );
};

export default MarketRegimePanel;
