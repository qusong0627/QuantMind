import React, { useEffect, useMemo, useState } from 'react';
import {
  Button, Card, Tag, Typography, Empty, Spin, Table, Select, message, Checkbox,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  RefreshCw, TrendingUp, Minus, Download,
  BarChart as BarChartIcon,
} from 'lucide-react';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip as ReTooltip, ResponsiveContainer, Cell, CartesianGrid, LabelList,
} from 'recharts';
import {
  getDailySelection,
  STRATEGY_PRESETS,
  DailySelectionResponse,
  StrategyPreset,
  CandidateStock,
} from '../../services/stockPickingService';
import { buildCsvText, downloadCsvFile } from '../../utils/csvExport';

const { Text } = Typography;

const STATE_COLOR: Record<string, string> = {
  牛市: '#f5222d',
  震荡偏强: '#fa8c16',
  震荡: '#faad14',
  震荡偏弱: '#8c8c8c',
  熊市: '#52c41a',
  无信号: '#bfbfbf',
};

const TREND_COLOR: Record<string, { color: string; label: string }> = {
  先升后降: { color: 'green', label: '先升后降' },
  上升中: { color: 'green', label: '上升中' },
  明日回落: { color: 'green', label: '明日回落' },
  回落中: { color: 'blue', label: '回落中' },
  震荡: { color: 'gold', label: '震荡' },
  连续上升: { color: 'volcano', label: '连续上升' },
  连续下降: { color: 'red', label: '连续下降' },
  趋势未知: { color: 'default', label: '—' },
};

export const StockPickingPanel: React.FC = () => {
  const [strategy, setStrategy] = useState<StrategyPreset>('balanced');
  const [ignoreMa20, setIgnoreMa20] = useState(false);
  const [data, setData] = useState<DailySelectionResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async (s: StrategyPreset = strategy, ignore: boolean = ignoreMa20) => {
    setLoading(true);
    try {
      const resp = await getDailySelection(s, undefined, ignore);
      setData(resp);
    } catch (err: any) {
      message.error(`选股失败: ${err?.message ?? '未知错误'}`);
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleStrategyChange = (s: StrategyPreset) => {
    setStrategy(s);
    void load(s);
  };

  const handleIgnoreMa20Change = (checked: boolean) => {
    setIgnoreMa20(checked);
    void load(strategy, checked);
  };

  const market = data?.market_state;
  const industryData = useMemo(
    () => (data?.industry_signals ?? []).map((it, idx) => ({ ...it, idx })),
    [data],
  );

  const columns: ColumnsType<CandidateStock> = [
    { title: '股票', dataIndex: 'symbol', width: 110, render: (v, r) => (
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-[12px] text-slate-800">{v}</span>
        {r.name && <span className="text-[10px] text-slate-400">{r.name}</span>}
      </div>
    )},
    { title: '分数', dataIndex: 'score', width: 80, sorter: (a, b) => a.score - b.score, render: (v: number) => (
      <span className="font-mono font-bold text-[12px]" style={{ color: v >= 0.12 ? '#fa8c16' : '#1677ff' }}>{v.toFixed(3)}</span>
    )},
    { title: '行业', dataIndex: 'industry', width: 100, render: (v: string) => (
      <Tag className="rounded-md text-[10px] m-0">{v}</Tag>
    )},
    { title: '趋势', dataIndex: 'trend', width: 90, render: (v: string) => {
      const meta = TREND_COLOR[v] ?? TREND_COLOR['趋势未知'];
      return <Tag color={meta.color} className="rounded-md text-[10px] m-0">{meta.label}</Tag>;
    }},
  ];

  const handleExportCsv = () => {
    if (!data?.candidates?.length) return;
    const header = ['symbol', 'name', 'score', 'industry', 'trend'];
    const rows = data.candidates.map(c => [
      c.symbol, c.name, c.score, c.industry, c.trend,
    ]);
    const csv = buildCsvText(header, rows, { textColumns: [0], filename: '' });
    if (downloadCsvFile(csv, `选股_${data.meta.trade_date ?? 'today'}.csv`)) {
      message.success(`已导出 ${rows.length} 条候选`);
    }
  };

  if (loading && !data) {
    return <div className="flex items-center justify-center py-24"><Spin /></div>;
  }

  return (
    <div className="space-y-4">
      {/* 顶部工具条 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-black tracking-widest text-slate-400">策略风格</span>
          <Select
            value={strategy}
            onChange={handleStrategyChange}
            size="small"
            style={{ width: 120 }}
            options={STRATEGY_PRESETS.map(p => ({ value: p.key, label: `${p.label}${p.desc ? ` · ${p.desc}` : ''}` }))}
            className="rounded-lg"
          />
          <div className="flex items-center gap-1.5 text-[10px] text-slate-400">
            {STRATEGY_PRESETS.find(p => p.key === strategy) && (
              <>
                <Tag className="rounded-md m-0 text-[9px]">entry={STRATEGY_PRESETS.find(p => p.key === strategy)!.entry}</Tag>
                <Tag className="rounded-md m-0 text-[9px]">exit={STRATEGY_PRESETS.find(p => p.key === strategy)!.exit}</Tag>
                <Tag className="rounded-md m-0 text-[9px]">强行业≥{STRATEGY_PRESETS.find(p => p.key === strategy)!.strong_min}</Tag>
              </>
            )}
            <Checkbox
              checked={ignoreMa20}
              onChange={e => handleIgnoreMa20Change(e.target.checked)}
              className="ml-2 text-[10px]"
            >
              忽略MA20过滤
            </Checkbox>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {data?.meta?.trade_date && (
            <span className="text-[10px] text-slate-400 font-mono">信号日 {data.meta.trade_date}</span>
          )}
          <Button
            size="small" icon={<RefreshCw size={12} className={loading ? 'animate-spin' : ''} />}
            className="rounded-lg text-[10px] font-bold"
            onClick={() => void load()}
            loading={loading}
          >刷新</Button>
        </div>
      </div>

      {!data ? (
        <Empty description="无选股数据（请先运行模型推理）" className="py-24" />
      ) : (
        <>
          {/* 市场状态卡片 */}
          <div className="grid grid-cols-2 gap-3">
            <Card
              className="rounded-2xl border-slate-100 shadow-sm"
              styles={{ body: { padding: '16px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center' } }}
            >
              <div className="flex items-center gap-1.5 mb-2">
                <TrendingUp size={13} className="text-blue-500" />
                <span className="text-[10px] font-black tracking-widest text-slate-400">大盘状态</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xl font-black tracking-tight" style={{ color: STATE_COLOR[market?.state ?? ''] ?? '#595959' }}>
                  {market?.state ?? '—'}
                </span>
                {ignoreMa20 && !market?.index_above_ma20 ? (
                  <Tag color="gold" className="rounded-md m-0 text-[9px]">已忽略MA20</Tag>
                ) : (
                  <Tag color={market?.index_above_ma20 ? 'green' : 'red'} className="rounded-md m-0 text-[9px]">
                    {market?.index_above_ma20 ? 'MA20之上' : '跌破MA20'}
                  </Tag>
                )}
              </div>
              <div className="mt-1 text-[10px] text-slate-400">{market?.index_detail}</div>
            </Card>
            <Card
              className="rounded-2xl border-slate-100 shadow-sm"
              styles={{ body: { padding: '16px 20px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center' } }}
            >
              <div className="flex items-center gap-1.5 mb-2">
                <BarChartIcon size={13} className="text-violet-500" />
                <span className="text-[10px] font-black tracking-widest text-slate-400">行业信号</span>
              </div>
              <div className="flex items-end justify-center gap-4">
                <div>
                  <div className="text-xl font-black font-mono text-slate-800">{market?.avg_top1?.toFixed(3) ?? '—'}</div>
                  <div className="text-[9px] text-slate-400">avgTop1</div>
                </div>
                <div>
                  <div className="text-xl font-black font-mono text-slate-800">{market?.strong_count ?? '—'}</div>
                  <div className="text-[9px] text-slate-400">强行业数</div>
                </div>
              </div>
            </Card>
            </div>

          {/* 行业 Top1 排行 */}
          <Card
            className="rounded-2xl border-slate-100 shadow-sm"
            title={<span className="text-xs font-black tracking-widest">行业 Top1 排行</span>}
            styles={{ header: { padding: '12px 20px' }, body: { padding: '12px 20px 4px' } }}
            extra={
              <div className="flex items-center gap-2 text-[10px] text-slate-400">
                {STRATEGY_PRESETS.find(p => p.key === strategy) && (
                  <>参考阈值 ≥ {STRATEGY_PRESETS.find(p => p.key === strategy)!.entry}</>
                )}
              </div>
            }
          >
            {industryData.length === 0 ? (
              <Empty description="无行业信号" className="py-12" />
            ) : (
              <ResponsiveContainer width="100%" height={industryData.length * 34 + 30}>
                <BarChart data={industryData} layout="vertical" margin={{ left: 8, right: 48 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" horizontal={false} />
                  <XAxis type="number" hide />
                  <YAxis
                    type="category" dataKey="industry" width={110}
                    tick={{ fontSize: 11, fill: '#595959' }} axisLine={false} tickLine={false}
                  />
                  <ReTooltip
                    formatter={(value: any, name: any, props: any) => {
                      const it = props?.payload;
                      return [
                        <span key="v">
                          Top1 <b style={{ color: '#1677ff' }}>{Number(value).toFixed(4)}</b>
                          {it?.stock ? <span style={{ color: '#8c8c8c' }}> · {it.stock}</span> : null}
                        </span>,
                        '行业分数',
                      ];
                    }}
                    contentStyle={{ borderRadius: 8, fontSize: 11 }}
                  />
                  <Bar dataKey="top1" radius={[0, 4, 4, 0]} barSize={20} isAnimationActive={false}>
                    {industryData.map((it, i) => (
                      <Cell key={i} fill={it.top1 >= (STRATEGY_PRESETS.find(p => p.key === strategy)!.entry) ? '#7dd3fc' : '#c9e8fb'} />
                    ))}
                    <LabelList
                      dataKey="top1"
                      position="right"
                      formatter={(v: any) => Number(v).toFixed(3)}
                      style={{ fontSize: 10, fill: '#8c8c8c', fontWeight: 600 }}
                    />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </Card>

          {/* 候选股 */}
          <Card
            className="rounded-2xl border-slate-100 shadow-sm"
            title={
              <div className="flex items-center gap-2">
                <span className="text-xs font-black tracking-widest">候选股票</span>
                <Tag color="default" className="rounded-md m-0 text-[9px]">
                  {data.candidates.length} 只
                </Tag>
              </div>
            }
            styles={{ header: { padding: '12px 20px' }, body: { padding: '12px 20px' } }}
            extra={
              data.candidates.length > 0 && (
                <Button size="small" icon={<Download size={12} />} className="rounded-lg text-[10px]" onClick={handleExportCsv}>
                  导出CSV
                </Button>
              )
            }
          >
            {data.candidates.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<span className="text-xs text-slate-400">无股票通过全部过滤</span>}
                className="py-12"
              />
            ) : (
              <Table<CandidateStock>
                rowKey="symbol"
                size="small"
                columns={columns}
                dataSource={data.candidates}
                pagination={false}
                className="stock-picking-table"
              />
            )}
          </Card>

          {/* 被排除示例 */}
          {data.excluded_examples.length > 0 && (
            <Card
              className="rounded-2xl border-slate-100 shadow-sm"
              title={
                <div className="flex items-center gap-2">
                  <span className="text-xs font-black tracking-widest text-slate-500">过滤说明</span>
                  <Tag color="default" className="rounded-md m-0 text-[9px]">可点击切换</Tag>
                </div>
              }
              styles={{ header: { padding: '12px 20px' }, body: { padding: '12px 20px' } }}
            >
              <div className="space-y-1.5">
                {data.excluded_examples.map((ex, i) => {
                  const isMa20 = ex.detail?.includes('MA20');
                  return (
                    <div key={i} className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
                      <div className="flex items-start gap-2">
                        <Minus size={12} className="mt-0.5 flex-shrink-0 text-slate-300" />
                        <span><Text strong>{ex.reason}</Text>：{ex.detail}</span>
                      </div>
                      {isMa20 && (
                        <Checkbox
                          checked={ignoreMa20}
                          onChange={e => handleIgnoreMa20Change(e.target.checked)}
                          className="flex-shrink-0 text-[10px]"
                        >
                          忽略MA20
                        </Checkbox>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
};
