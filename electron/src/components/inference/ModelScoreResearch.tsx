import React, { useState, useCallback, useEffect, useRef } from 'react';
import { Typography, Button, Select, Table, Tag, Spin, Empty, Alert, Progress, Tooltip } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import clsx from 'clsx';
import {
  submitScoreCalibration,
  getCalibrationTask,
  getCalibrationHistory,
  ScoreCalibrationResponse,
  CalibrationHistoryItem,
} from '../../services/stockPickingService';

const { Text } = Typography;

/** 分数档颜色：按数值边界动态着色（负分红、高分绿、中间灰），适配任意量级模型 */
const bandColor = (band: string, nature?: string): string => {
  // 优先用后端标注的档位性质（基于实际收益，最可靠）
  if (nature === '最优') return '#10b981';
  if (nature === '最差') return '#f43f5e';
  if (nature === '最热') return '#f97316';
  // 从分数档字符串解析数值边界：如 "≥2.500" / "-2.000~-1.500" / "0.080~0.100"
  const m = band.match(/^≥\s*(-?[\d.]+)/) || band.match(/^(-?[\d.]+)\s*~\s*(-?[\d.]+)/);
  if (m) {
    const a = parseFloat(m[1]);
    if (a >= 0) return '#10b981';        // 高分区（正分）
    if (a <= -0.5) return '#f43f5e';     // 低分区（深负）
    return '#64748b';
  }
  if (band.includes('-')) return '#f43f5e';
  return '#64748b';
};

/** 数值颜色：正红负绿（A股习惯：涨红跌绿） */
const numColor = (v: number | null | undefined): string => {
  if (v === null || v === undefined) return '#94a3b8';
  return v > 0 ? '#ef4444' : v < 0 ? '#10b981' : '#64748b';
};

/** 区块标题：编号徽章 + 标题文字 + 颜色 */
const SectionTitle: React.FC<{ idx: number; color: string; children: React.ReactNode }> = ({ idx, color, children }) => (
  <div className="mb-2 flex items-center gap-2">
    <span className="flex h-4 w-4 items-center justify-center rounded-full text-[9px] font-black text-white" style={{ background: color }}>
      {idx}
    </span>
    <Text className="text-[10px] font-black uppercase tracking-widest" style={{ color }}>
      {children}
    </Text>
  </div>
);

interface Props {
  modelId?: string;
}

export const ModelScoreResearch: React.FC<Props> = ({ modelId }) => {
  const [data, setData] = useState<ScoreCalibrationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const [progressMsg, setProgressMsg] = useState('');
  const [days, setDays] = useState(180);
  const [horizons, setHorizons] = useState('1,3,5,10');
  const [history, setHistory] = useState<CalibrationHistoryItem[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const clearPoll = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const h = await getCalibrationHistory(20, modelId);
      if (h.status === 'success') setHistory(h.items || []);
    } catch (err: any) {
      console.error('[ScoreCalibration] load history failed:', err);
    }
  }, [modelId]);

  /** 点击历史记录：加载该次校准的完整结果 */
  /** 把后端 result 包装成前端渲染结构（补 status + 合并 total_samples/latest_trade_date 到 meta） */
  const normalizeResult = useCallback((result: ScoreCalibrationResponse, taskMeta?: any): ScoreCalibrationResponse => {
    return {
      ...result,
      status: 'success',
      meta: {
        ...(taskMeta || {}),
        backtest_days: taskMeta?.backtest_days ?? result.total_samples ?? undefined,
        total_samples: (result as any)?.total_samples ?? taskMeta?.total_samples,
        latest_trade_date: (result as any)?.latest_trade_date ?? taskMeta?.latest_trade_date,
      },
    };
  }, []);

  const viewHistoryTask = useCallback(async (tid: string) => {
    setLoading(true);
    setData(null);
    setProgress(100);
    setProgressMsg('加载历史结果...');
    try {
      const t = await getCalibrationTask(tid);
      if (t.status === 'completed' && t.result) {
        setData(normalizeResult(t.result, t.meta));
      } else if (t.status === 'not_found') {
        setData({ status: 'error', detail: '该历史记录已过期（服务重启后内存任务清空），请重新校准' });
      } else {
        setData({ status: 'error', detail: t.error || t.detail || '历史结果加载失败' });
      }
    } catch (err: any) {
      setData({ status: 'error', detail: err?.message || '历史结果加载失败' });
    } finally {
      setLoading(false);
    }
  }, [normalizeResult]);

  const load = useCallback(async () => {
    clearPoll();
    setLoading(true);
    setData(null);
    setProgress(0);
    setProgressMsg('提交校准任务...');
    try {
      const res = await submitScoreCalibration({ days, horizons, top_n: 50, model_id: modelId });
      if (res.status !== 'submitted' || !res.task_id) {
        setData({ status: 'error', detail: res.detail || '提交失败' });
        setLoading(false);
        return;
      }
      setTaskId(res.task_id);
      // 轮询进度
      pollRef.current = setInterval(async () => {
        try {
          const t = await getCalibrationTask(res.task_id!);
          setProgress(t.progress ?? 0);
          setProgressMsg(t.message || '');
          if (t.status === 'completed' && t.result) {
            clearPoll();
            setData(normalizeResult(t.result, t.meta));
            setLoading(false);
            void loadHistory();
          } else if (t.status === 'failed' || t.status === 'error') {
            clearPoll();
            setData({ status: 'error', detail: t.error || t.detail || '校准失败' });
            setLoading(false);
          } else if (t.status === 'not_found') {
            clearPoll();
            setData({ status: 'error', detail: '任务不存在' });
            setLoading(false);
          }
        } catch (err: any) {
          // 轮询失败暂时忽略，下次再试
          console.error('[ScoreCalibration] poll failed:', err);
        }
      }, 2000);
    } catch (err: any) {
      setData({ status: 'error', detail: err?.message || '加载失败' });
      setLoading(false);
    }
  }, [days, horizons, clearPoll, normalizeResult, modelId]);

  useEffect(() => {
    return () => clearPoll();
  }, [clearPoll]);

  useEffect(() => {
    // 进入页面只加载历史，不自动开始校准；模型切换时重载该模型的历史
    void loadHistory();
    return () => clearPoll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelId]);

  const summaryColumns = [
    {
      title: '分数档',
      dataIndex: 'score_band',
      width: 120,
      render: (v: string, r: any) => (
        <div className="flex items-center gap-1.5">
          <Text className="font-black" style={{ color: bandColor(v, r?.nature) }}>{v}</Text>
          {r?.nature && (
            <Tag className="m-0 border-0 text-[8px] font-bold px-1.5"
              color={r.nature === '最优' ? 'green' : r.nature === '最差' ? 'red' : r.nature === '最热' ? 'orange' : 'default'}>
              {r.nature === '最优' ? '★最优' : r.nature === '最差' ? '▼最差' : r.nature === '最热' ? '🔥最热' : ''}
            </Tag>
          )}
        </div>
      ),
    },
    {
      title: '样本',
      dataIndex: 'n',
      width: 70,
      render: (v: number) => <Text className="font-mono text-[10px]">{v.toLocaleString()}</Text>,
    },
    {
      title: 'Top50内',
      dataIndex: 'top50_count',
      width: 70,
      render: (v: number) => <Text className="font-mono text-[10px]">{v}</Text>,
    },
    {
      title: '平均排名',
      dataIndex: 'avg_rank',
      width: 80,
      render: (v: number) => <Text className="font-mono text-[10px] text-slate-500">{v.toLocaleString()}</Text>,
    },
    ...([1, 3, 5, 10].filter(h => horizons.split(',').includes(String(h))).map(h => ({
      title: `T+${h} 上涨`,
      key: `up_${h}`,
      width: 76,
      render: (_: unknown, r: ScoreCalibrationResponse['score_summary'] extends (infer T)[] ? T : never) => {
        const hs = (r?.horizons || []).find(x => x.horizon === h);
        if (!hs) return <Text className="text-[10px] text-slate-300">—</Text>;
        return (
          <Text className="font-mono text-[10px]" style={{ color: numColor(hs.win_rate - 50) }}>
            {hs.win_rate.toFixed(1)}%
          </Text>
        );
      },
    }))),
    ...([1, 3, 5, 10].filter(h => horizons.split(',').includes(String(h))).map(h => ({
      title: `T+${h} 下跌`,
      key: `down_${h}`,
      width: 76,
      render: (_: unknown, r: ScoreCalibrationResponse['score_summary'] extends (infer T)[] ? T : never) => {
        const hs = (r?.horizons || []).find(x => x.horizon === h);
        if (!hs) return <Text className="text-[10px] text-slate-300">—</Text>;
        return (
          <Text className="font-mono text-[10px]" style={{ color: numColor(hs.down_prob > 50 ? -1 : 1) }}>
            {hs.down_prob.toFixed(1)}%
          </Text>
        );
      },
    }))),
    ...([1, 3, 5, 10].filter(h => horizons.split(',').includes(String(h))).map(h => ({
      title: `T+${h} 均收`,
      key: `ret_${h}`,
      width: 76,
      render: (_: unknown, r: ScoreCalibrationResponse['score_summary'] extends (infer T)[] ? T : never) => {
        const hs = (r?.horizons || []).find(x => x.horizon === h);
        if (!hs) return <Text className="text-[10px] text-slate-300">—</Text>;
        return (
          <Text className="font-mono text-[10px]" style={{ color: numColor(hs.avg_ret) }}>
            {hs.avg_ret > 0 ? '+' : ''}{hs.avg_ret.toFixed(2)}%
          </Text>        );
      },
    }))),
    {
      title: '超额收益',
      key: 'excess',
      width: 90,
      render: (_: unknown, r: any) => {
        const ex = r?.excess_ret;
        if (ex === null || ex === undefined) return <Text className="text-[10px] text-slate-300">—</Text>;
        return (
          <Tooltip title="相对全市场主周期均收的超额收益（跑赢大盘为正）">
            <Text className="font-mono text-[10px]" style={{ color: numColor(ex) }}>
              {ex > 0 ? '+' : ''}{ex.toFixed(2)}%
            </Text>
          </Tooltip>
        );
      },
    },
  ];

  const matrixColumns = [
    { title: '分数档', dataIndex: 'score_band', width: 110, fixed: 'left' as const,
      render: (v: string) => <Text className="font-black" style={{ color: bandColor(v) }}>{v}</Text> },
    ...(['微盘', '小盘', '中盘', '大盘', '超大盘'].map(cap => ({
      title: cap,
      key: cap,
      render: (_: unknown, r: ScoreCalibrationResponse['matrix'] extends (infer T)[] ? T : never) => {
        const cell = (r?.caps || []).find(c => c.cap === cap);
        if (!cell || cell.n === 0) return <Text className="text-[10px] text-slate-300">—</Text>;
        return (
          <div className="text-[9px]">
            <div className="font-mono" style={{ color: numColor(cell.avg_ret) }}>
              {cell.avg_ret > 0 ? '+' : ''}{cell.avg_ret?.toFixed(2)}%
            </div>
            <div className="font-mono text-slate-400">跌{cell.down_prob?.toFixed(0)}%</div>
          </div>
        );
      },
    }))),
  ];

  return (
    <div className="space-y-4">
      {/* 控制栏 */}
      <div className="rounded-2xl border border-slate-200 bg-gradient-to-br from-slate-50 to-white p-3 shadow-sm">
        <div className="flex items-center gap-2 flex-wrap">
          <Select value={days} onChange={setDays} style={{ width: 120 }} className="!text-xs" options={[
            { value: 60, label: '60 交易日' },
            { value: 120, label: '120 交易日' },
            { value: 180, label: '180 交易日' },
            { value: 365, label: '365 交易日' },
          ]} />
          <Select value={horizons} onChange={setHorizons} style={{ width: 140 }} className="!text-xs" options={[
            { value: '1,3,5,10', label: 'T+1/3/5/10' },
            { value: '1,5,20', label: 'T+1/5/20' },
            { value: '5', label: '仅 T+5' },
            { value: '1,3,5,10,20', label: 'T+1~T+20' },
          ]} />
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}
            className="rounded-lg text-[10px] font-bold h-8 px-3">
            {loading ? '校准中...' : '重新校准'}
          </Button>
          <span className="mx-1 h-4 w-px bg-slate-200" />
          {data?.meta && (
            <Tag className="border-0 bg-blue-50 text-blue-600 text-[9px] font-bold">
              {data.meta?.backtest_days ?? '-'}天 · {(data as any)?.total_samples?.toLocaleString() ?? '-'}样本 · {(data as any)?.latest_trade_date ?? '-'}
            </Tag>
          )}
          </div>
        {/* 方向自检横幅 */}
        {(data as any)?.direction_check && (() => {
          const dc = (data as any).direction_check;
          const ok = dc.direction_ok;
          return (
            <div className={clsx(
              'mt-2.5 rounded-xl border px-3 py-2 flex items-center gap-2',
              ok ? 'border-emerald-200 bg-emerald-50/60' : 'border-rose-200 bg-rose-50/60',
            )}>
              <span className={clsx('w-1.5 h-1.5 rounded-full flex-shrink-0', ok ? 'bg-emerald-500' : 'bg-rose-500')} />
              <Text className={clsx('text-[10px] font-bold', ok ? 'text-emerald-700' : 'text-rose-700')}>
                {ok ? '分数方向有效' : '分数方向存疑'}
              </Text>
              <Text className="text-[10px] font-mono text-slate-500">
                RankIC {dc.rank_ic?.toFixed?.(3) ?? '—'} · 正向 {dc.positive_days ?? 0}/{dc.total_days ?? 0} 天
              </Text>
              <Text className="text-[10px] text-slate-500">
                市场 {dc.market_state || '—'} · 全市场均收 {dc.market_avg_ret?.toFixed?.(2) ?? '—'}%
              </Text>
            </div>
          );
        })()}
      </div>

      {loading && (
        <div className="rounded-2xl border border-blue-100 bg-blue-50/50 p-4 shadow-sm">
          <div className="mb-2 flex items-center justify-between">
            <Text className="text-[10px] font-black uppercase tracking-widest text-blue-600">
              模型分数校准中...
            </Text>
            <Text className="text-[10px] font-mono text-blue-500">{progress}%</Text>
          </div>
          <Progress percent={progress} size="small" status="active" strokeColor="#3b82f6" />
          <Text className="text-[10px] text-slate-500 mt-1 block">{progressMsg || '计算中，请稍候'}</Text>
        </div>
      )}

      <Spin spinning={loading}>
        {!data || data.status !== 'success' ? (
          <Empty description={data?.detail || '暂无分数校准数据'} className="py-16" />
        ) : (
          <div className="space-y-4">
            {data.score_summary && data.score_summary.length > 0 && (
              <div className="rounded-2xl border border-slate-100 bg-white p-4 shadow-sm">
                <SectionTitle idx={1} color="#0ea5e9">分数档 × 多周期收益</SectionTitle>
                <Table
                  dataSource={data.score_summary}
                  columns={summaryColumns as any}
                  size="small"
                  pagination={false}
                  scroll={{ x: 900 }}
                  rowKey="score_band"
                />
              </div>
            )}

            {/* 最优分数区间（按胜率反推） */}
            {(data as any).winrate_zones && (data as any).winrate_zones.status === 'success' && (
              <div className="rounded-2xl border border-emerald-100 bg-emerald-50/30 p-4 shadow-sm">
                <SectionTitle idx={2} color="#059669">最优分数区间（按历史胜率统计分档）</SectionTitle>
                <div className="space-y-1">
                  {((data as any).winrate_zones.zones || []).map((z: any, i: number) => {
                    const isLong = z.label?.includes('做多');
                    return (
                      <div key={i} className="flex items-center justify-between text-[10px] py-0.5">
                        <div className="flex items-center gap-2">
                          <Tag className="m-0 border-0 text-[9px] font-bold"
                            color={isLong ? 'red' : 'green'}>
                            {isLong ? '上升' : '下降'}
                          </Tag>
                          <span className="font-black text-slate-700">T+{z.horizon} {z.score_min.toFixed(3)}~{z.score_max.toFixed(3)}</span>
                        </div>
                        <span className="font-mono">
                          <span style={{ color: numColor(z.win_rate - 50) }}>胜率 {z.win_rate}%</span>
                          <span className="text-slate-400"> / 下跌 {z.down_prob}% / 均收 </span>
                          <span style={{ color: numColor(z.avg_ret) }}>{z.avg_ret > 0 ? '+' : ''}{z.avg_ret}%</span>
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* 多条件组合最优区间（大盘×市值×板块） */}
            {(data as any).condition_zones && (data as any).condition_zones.status === 'success' && (
              <div className="rounded-2xl border border-blue-100 bg-blue-50/30 p-4 shadow-sm">
                <SectionTitle idx={3} color="#2563eb">多条件组合最优区间（大盘状态 × 市值 × 板块）</SectionTitle>
                {(data as any).condition_zones.metric_note && (
                  <div className="mb-2 text-[9px] text-slate-500">
                    📌 {(data as any).condition_zones.metric_note}
                  </div>
                )}
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                  <div>
                    <div className="mb-1 text-[9px] font-bold text-emerald-600">
                      上升胜率最高区间，共 {((data as any).condition_zones.buy_zones || []).length} 段
                    </div>
                    <div className="space-y-0.5 max-h-72 overflow-y-auto pr-1 custom-scrollbar">
                      {(((data as any).condition_zones.buy_zones || [])).slice(0, 15).map((z: any, i: number) => (
                        <div key={i} className="flex items-center justify-between text-[10px] py-0.5">
                          <span className="font-bold text-slate-700">
                            {z.regime === '大盘多' ? '📈' : z.regime === '大盘空' ? '📉' : ''}{z.regime}·{z.cap}·{z.board}
                          </span>
                          <span className="font-mono text-[10px]">
                            T+{z.horizon} {z.score_min.toFixed(3)}~{z.score_max.toFixed(3)}
                            <span style={{ color: numColor(z.win_rate - 50) }}> 胜率{z.win_rate}%</span>
                            <span className="text-slate-400"> 均收</span>
                            <span style={{ color: numColor(z.avg_ret) }}>{z.avg_ret > 0 ? '+' : ''}{z.avg_ret}%</span>
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="mb-1 text-[9px] font-bold text-rose-600">
                      下跌概率最高区间，共 {((data as any).condition_zones.sell_zones || []).length} 段
                    </div>
                    <div className="space-y-0.5 max-h-72 overflow-y-auto pr-1 custom-scrollbar">
                      {(((data as any).condition_zones.sell_zones || [])).slice(0, 15).map((z: any, i: number) => (
                        <div key={i} className="flex items-center justify-between text-[10px] py-0.5">
                          <span className="font-bold text-slate-700">
                            {z.regime === '大盘多' ? '📈' : z.regime === '大盘空' ? '📉' : ''}{z.regime}·{z.cap}·{z.board}
                          </span>
                          <span className="font-mono text-[10px]">
                            T+{z.horizon} {z.score_min.toFixed(3)}~{z.score_max.toFixed(3)}
                            <span style={{ color: numColor(-z.down_prob + 50) }}> 下跌{z.down_prob}%</span>
                            <span className="text-slate-400"> 均收</span>
                            <span style={{ color: numColor(z.avg_ret) }}>{z.avg_ret > 0 ? '+' : ''}{z.avg_ret}%</span>
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {data.matrix && data.matrix.length > 0 && (
              <div className="rounded-2xl border border-slate-100 bg-white p-4 shadow-sm">
                <SectionTitle idx={4} color="#0f766e">分数档 × 市值 × 下跌概率（主周期 T+{data.meta?.main_horizon ?? 5}）</SectionTitle>
                <Table
                  dataSource={data.matrix}
                  columns={matrixColumns as any}
                  size="small"
                  pagination={false}
                  scroll={{ x: 600 }}
                  rowKey="score_band"
                />
              </div>
            )}

            {/* 多维度 × 分数 涨跌概率（地区/概念/风格） */}
            {(data as any).dimension_zones && (data as any).dimension_zones.status === 'success' && (
              <div className="rounded-2xl border border-violet-100 bg-violet-50/30 p-4 shadow-sm">
                <SectionTitle idx={5} color="#7c3aed">多维度 × 分数 涨跌概率（地区/概念/风格）</SectionTitle>
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                  {[
                    { label: '地区板块', key: 'region', color: 'text-amber-600' },
                    { label: '概念板块', key: 'concept', color: 'text-sky-600' },
                    { label: '风格板块', key: 'style', color: 'text-fuchsia-600' },
                  ].map(sec => {
                    const items = ((data as any).dimension_zones[sec.key] || []);
                    if (!items.length) return null;
                    return (
                      <div key={sec.key}>
                        <div className={`mb-1 text-[9px] font-bold ${sec.color}`}>{sec.label}</div>
                        <div className="space-y-0.5 max-h-56 overflow-y-auto pr-1 custom-scrollbar">
                          {items.slice(0, 8).map((x: any, i: number) => (
                            <div key={i} className="py-0.5 text-[9px]">
                              <div className="font-bold text-slate-700 truncate">{x.name}</div>
                              <div className="font-mono text-[9px]">
                                <span className="text-emerald-600">升{x.buy.score_min.toFixed(2)}~{x.buy.score_max.toFixed(2)}胜{x.buy.win_rate}%均{x.buy.avg_ret > 0 ? '+' : ''}{x.buy.avg_ret}%</span>
                                <span className="text-slate-300"> | </span>
                                <span className="text-rose-600">跌{x.sell.score_min.toFixed(2)}~{x.sell.score_max.toFixed(2)}跌{x.sell.down_prob}%</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {data.neg_industry_avg && data.neg_industry_avg.length > 0 && (
                <div className="rounded-2xl border border-rose-100 bg-rose-50/40 p-4 shadow-sm">
                  <SectionTitle idx={6} color="#e11d48">负分行业 avg（最新日）</SectionTitle>
                  <div className="space-y-1">
                    {data.neg_industry_avg.map(r => (
                      <div key={r.industry} className="flex items-center justify-between text-[10px]">
                        <span className="font-bold text-slate-600">{r.industry}</span>
                        <span className="font-mono" style={{ color: numColor(r.neg_avg) }}>
                          {r.neg_avg.toFixed(3)}（{r.neg_count}只 / 最深{(r as any).neg_min?.toFixed(3) ?? (r as any).neg_extreme?.toFixed(3) ?? '-'}）
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {data.pos_industry_avg && data.pos_industry_avg.length > 0 && (
                <div className="rounded-2xl border border-emerald-100 bg-emerald-50/40 p-4 shadow-sm">
                  <SectionTitle idx={7} color="#059669">正分行业 avg（最新日）</SectionTitle>
                  <div className="space-y-1">
                    {data.pos_industry_avg.map(r => (
                      <div key={r.industry} className="flex items-center justify-between text-[10px]">
                        <span className="font-bold text-slate-600">{r.industry}</span>
                        <span className="font-mono" style={{ color: numColor(r.pos_avg) }}>
                          {r.pos_avg.toFixed(3)}（{r.pos_count}只 / 最高{(r as any).pos_extreme?.toFixed(3) ?? '-'}）
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {data.neg_board_avg && data.neg_board_avg.length > 0 && (
                <div className="rounded-2xl border border-rose-100 bg-rose-50/40 p-4 shadow-sm">
                  <SectionTitle idx={8} color="#e11d48">板块负分 avg（最新日）</SectionTitle>
                  <div className="space-y-1">
                    {data.neg_board_avg.map(r => (
                      <div key={r.board} className="flex items-center justify-between text-[10px]">
                        <span className="font-bold text-slate-600">{r.board}</span>
                        <span className="font-mono" style={{ color: numColor(r.neg_avg) }}>
                          {r.neg_avg.toFixed(3)}（{r.neg_count}只）
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {data.pos_board_avg && data.pos_board_avg.length > 0 && (
                <div className="rounded-2xl border border-emerald-100 bg-emerald-50/40 p-4 shadow-sm">
                  <SectionTitle idx={9} color="#059669">板块正分 avg（最新日）</SectionTitle>
                  <div className="space-y-1">
                    {data.pos_board_avg.map(r => (
                      <div key={r.board} className="flex items-center justify-between text-[10px]">
                        <span className="font-bold text-slate-600">{r.board}</span>
                        <span className="font-mono" style={{ color: numColor(r.pos_avg) }}>
                          {r.pos_avg.toFixed(3)}（{r.pos_count}只）
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {data.warnings && data.warnings.length > 0 && (
              <Alert type="warning" message={data.warnings.join('；')} className="!text-[10px]" />
            )}
          </div>
        )}
      </Spin>

      {/* 校准历史 */}
      {history.length > 0 && (
        <div className="rounded-2xl border border-slate-100 bg-white p-4 shadow-sm">
          <SectionTitle idx={11} color="#64748b">校准历史（{history.length} 次）</SectionTitle>
          <div className="space-y-1">
            {history.map(h => (
              <div key={h.task_id}
                onClick={() => void viewHistoryTask(h.task_id)}
                className="flex items-center justify-between text-[10px] py-2 border-b border-slate-50 last:border-0 cursor-pointer hover:bg-slate-50 rounded-lg px-1 transition-colors">
                <div className="flex items-center gap-2 min-w-0">
                  <Tag className="m-0 border-0 text-[9px] font-bold"
                    color={h.status === 'completed' ? 'green' : h.status === 'failed' ? 'red' : 'processing'}>
                    {h.status === 'completed' ? '完成' : h.status === 'failed' ? '失败' : '进行中'}
                  </Tag>
                  <span className="font-mono text-slate-600 truncate">{h.task_id.slice(0, 20)}...</span>
                  <span className="text-slate-400">
                    {h.params?.days ?? '-'}天 / T{String(h.params?.horizons ?? '')} / 样本{h.total_samples?.toLocaleString() ?? '-'}
                    {h.params?.model_id ? ` / ${String(h.params.model_id).slice(0, 12)}...` : ''}
                  </span>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  {h.latest_trade_date && (
                    <span className="text-slate-400 font-mono">{h.latest_trade_date}</span>
                  )}
                  <span className="text-blue-500 font-bold ml-1">查看 ›</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
