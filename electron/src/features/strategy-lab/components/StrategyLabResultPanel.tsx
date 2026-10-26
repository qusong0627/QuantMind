import React, { useMemo, useState } from 'react';
import { Card, Empty, Tag, Tabs, Table, Alert, Typography, Button, Space, message } from 'antd';
import type { StrategyLabRunResult, StrategyLabTradeRecord } from '../types';
import KpiCards from './KpiCards';
import EquityChart from './EquityChart';
import MonthlyReturnsHeatmap from './MonthlyReturnsHeatmap';
import StrategyLabKlineView from './StrategyLabKlineView';
import YearlyStats from './YearlyStats';
import ScoreCard from './ScoreCard';
import WhyExplainerModal from './WhyExplainerModal';
import { humanizeError } from '../utils/humanizeError';
import { strategyLabService } from '../services/strategyLabService';

const { Paragraph, Text } = Typography;

interface Props {
  result: StrategyLabRunResult | null;
  loading: boolean;
  /** Optional: original code; if present, enables 转模板 / 4 关卡 actions. */
  code?: string;
  /** Current strategy ID for history lookup */
  strategyId?: string | null;
  /** Current strategy name for backtest history filtering */
  strategyName?: string | null;
  /** Day 18: previous successful run, rendered as a dashed overlay on EquityChart. */
  prevResult?: StrategyLabRunResult | null;
  onClearPrev?: () => void;
  /** Day 17: hand-drawn lines synced with the parent page so they survive re-runs. */
  drawnLines?: Record<string, number>;
  onDrawnLinesChange?: (next: Record<string, number>) => void;
  /** 内嵌右栏：去掉外层卡片阴影/边框，隐藏 Strategy Lab 专属动作。 */
  compact?: boolean;
  /** 是否显示 4 关卡/转模板/加入扫描（minibt/qlib 结果应 false）。 */
  showLabActions?: boolean;
}

const fmtPct = (v: number | null | undefined, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
};

const fmt = (v: number | null | undefined, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return v.toFixed(digits);
};

export const StrategyLabResultPanel: React.FC<Props> = ({ result, loading, code, strategyId, strategyName, prevResult, onClearPrev, drawnLines, onDrawnLinesChange, compact = false, showLabActions = true }) => {
  const [explainTrade, setExplainTrade] = useState<StrategyLabTradeRecord | null>(null);
  const [overfitReport, setOverfitReport] = useState<any>(null);
  const [overfitLoading, setOverfitLoading] = useState(false);
  const [translateLoading, setTranslateLoading] = useState(false);
  const [watchLoading, setWatchLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [backtestHistory, setBacktestHistory] = useState<any[]>([]);

  const alpha = useMemo(() => {
    if (!result?.equity?.length) return null;
    const last = result.equity[result.equity.length - 1];
    const first = result.equity[0];
    if (!first || !last) return null;
    const stratRet = first.value ? last.value / first.value - 1 : 0;
    if (
      first.benchmark === null ||
      first.benchmark === undefined ||
      last.benchmark === null ||
      last.benchmark === undefined
    ) {
      return null;
    }
    const benchRet = first.benchmark ? last.benchmark / first.benchmark - 1 : 0;
    return stratRet - benchRet;
  }, [result]);

  // Fetch backtest history when strategyName changes (server-side filter)
  React.useEffect(() => {
    if (!strategyName) {
      setBacktestHistory([]);
      return;
    }
    setHistoryLoading(true);
    strategyLabService.getBacktestHistory({
      page: 1,
      pageSize: 20,
      sortBy: 'created_at',
      sortOrder: 'desc',
      strategyName,
    })
      .then((data) => setBacktestHistory(data.backtests))
      .catch(() => setBacktestHistory([]))
      .finally(() => setHistoryLoading(false));
  }, [strategyName]);

  if (loading && !result) {
    return (
      <Card loading className="strategy-lab-result-empty">
        <div style={{ height: 320 }} />
      </Card>
    );
  }

  if (!result) {
    return (
      <Card className="strategy-lab-result-empty">
        <Empty description={<>点击右上角 <Text code>运行</Text> 开始一次回测</>} />
      </Card>
    );
  }

  if (result.status === 'failed') {
    const human = humanizeError(result.error || '', result.error_traceback || '');
    const isDataMissing = /行情数据缺失|FileNotFoundError|qlib data path|No such file/i.test(
      `${result.error || ''}\n${result.error_traceback || ''}`,
    );
    return (
      <Card>
        <Alert
          type="error"
          showIcon
          message={`回测失败 (run_id=${result.run_id})`}
          description={
            <>
              <Paragraph strong style={{ marginBottom: 4 }}>{human.title}</Paragraph>
              {human.hint && (
                <Paragraph style={{ marginBottom: 8, color: '#475569' }}>{human.hint}</Paragraph>
              )}
              {isDataMissing && (
                <Paragraph style={{ marginBottom: 8 }}>
                  <Button
                    size="small"
                    type="primary"
                    onClick={() => {
                      window.location.hash = '#/admin/data-management';
                    }}
                  >
                    去数据平台同步
                  </Button>
                  <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                    管理员可在「数据平台」页一键拉取目标市场行情。
                  </Text>
                </Paragraph>
              )}
              <Paragraph copyable={{ text: result.error || '' }}>{result.error || 'Unknown error'}</Paragraph>
              {result.error_traceback && (
                <pre
                  style={{
                    fontSize: 11,
                    maxHeight: 240,
                    overflow: 'auto',
                    background: '#222',
                    color: '#eee',
                    padding: 8,
                  }}
                >
                  {result.error_traceback}
                </pre>
              )}
            </>
          }
        />
      </Card>
    );
  }

  const handleOverfitCheck = async () => {
    if (!code) {
      message.warning('未提供脚本代码，无法运行 4 关卡检测');
      return;
    }
    setOverfitLoading(true);
    try {
      const report = await strategyLabService.runOverfitCheck(code);
      setOverfitReport(report);
    } catch (e: any) {
      message.error(`4 关卡检测失败: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setOverfitLoading(false);
    }
  };

  const handleConvertTemplate = async () => {
    if (!code) {
      message.warning('未提供脚本代码，无法转模板');
      return;
    }
    setTranslateLoading(true);
    try {
      const out = await strategyLabService.translateToTemplate(code);
      message.success(`已转换为模板：${out.strategy_name}（可在策略向导中查看）`);
    } catch (e: any) {
      message.error(`转模板失败: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setTranslateLoading(false);
    }
  };

  const handleAddWatch = async () => {
    if (!code) {
      message.warning('未提供脚本代码，无法加入扫描');
      return;
    }
    const name = window.prompt('为该策略起一个名字（出现在 Dashboard 信号卡上）：', '我的实验室策略');
    if (!name) return;
    setWatchLoading(true);
    try {
      const out = await strategyLabService.addWatch(code, name);
      message.success(`已加入每日扫描（sha=${out.script_sha.slice(0, 8)}…）`);
    } catch (e: any) {
      message.error(`加入失败: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setWatchLoading(false);
    }
  };

  return (
    <Card
      title={
        compact ? undefined : (
        <span>
          回测结果
          <Tag color="green" style={{ marginLeft: 8 }}>{result.status}</Tag>
          <Tag color="blue">{result.elapsed_sec.toFixed(2)}s</Tag>
          {result.data_snapshot_at && (
            <Tag color="purple" style={{ marginLeft: 4 }}>data@{result.data_snapshot_at}</Tag>
          )}
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
            run_id: {result.run_id.slice(0, 8)}…
          </Text>
        </span>
        )
      }
      extra={
        !compact && showLabActions ? (
        <Space size="small">
          <Button
            size="small"
            loading={overfitLoading}
            disabled={!code}
            onClick={handleOverfitCheck}
          >
            4 关卡检测
          </Button>
          <Button
            size="small"
            loading={watchLoading}
            disabled={!code}
            onClick={handleAddWatch}
          >
            加入每日扫描
          </Button>
          <Button
            size="small"
            type="primary"
            loading={translateLoading}
            disabled={!code}
            onClick={handleConvertTemplate}
          >
            转模板
          </Button>
        </Space>
        ) : undefined
      }
      style={
        compact
          ? { boxShadow: 'none', border: 'none', background: 'transparent' }
          : undefined
      }
      bodyStyle={{
        paddingTop: compact ? 4 : 12,
        paddingLeft: compact ? 0 : undefined,
        paddingRight: compact ? 0 : undefined,
        paddingBottom: compact ? 0 : undefined,
      }}
    >
      <ScoreCard result={result} overfit={overfitReport} />
      <KpiCards
        metrics={result.metrics}
        extras={{
          alpha,
          nDataPoints: result.equity.length,
        }}
      />

      <Tabs
        size={compact ? 'small' : 'middle'}
        defaultActiveKey="equity"
        style={{ marginTop: compact ? 4 : 12 }}
        items={[
          {
            key: 'equity',
            label: `净值曲线 (${result.equity.length})${prevResult ? ' · vs 上一版' : ''}`,
            children: (
              <>
                {prevResult && (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 8 }}
                    message={
                      <Space size="small">
                        <span>对比基线：上一次回测</span>
                        <Tag color="orange">
                          Δ累计收益 {(((result.metrics.cum_return ?? 0) - (prevResult.metrics.cum_return ?? 0)) * 100).toFixed(2)}%
                        </Tag>
                        <Tag color={result.metrics.sharpe >= prevResult.metrics.sharpe ? 'green' : 'red'}>
                          ΔSharpe {(result.metrics.sharpe - prevResult.metrics.sharpe).toFixed(2)}
                        </Tag>
                        {onClearPrev && (
                          <Button size="small" type="link" onClick={onClearPrev}>清除对比</Button>
                        )}
                      </Space>
                    }
                  />
                )}
                <EquityChart
                  equity={result.equity}
                  prevEquity={prevResult?.equity ?? null}
                  prevLabel="上一版"
                  height={300}
                />
              </>
            ),
          },
          {
            key: 'kline',
            label: 'K 线 ▲▼',
            children: (
              <StrategyLabKlineView
                result={result}
                onMarkerClick={setExplainTrade}
                drawnLines={drawnLines}
                onDrawnLinesChange={onDrawnLinesChange}
              />
            ),
          },
          {
            key: 'monthly',
            label: '月度热力',
            children: <MonthlyReturnsHeatmap equity={result.equity} />,
          },
          {
            key: 'history',
            label: `回测记录 (${backtestHistory.length})`,
            children: historyLoading ? (
              <Card loading />
            ) : backtestHistory.length === 0 ? (
              <Empty description="暂无回测记录" />
            ) : (
              <Table
                size="small"
                rowKey="backtest_id"
                pagination={{ pageSize: 10, showSizeChanger: false }}
                dataSource={backtestHistory}
                columns={[
                  {
                    title: '回测时间',
                    dataIndex: 'created_at',
                    key: 'created_at',
                    render: (v: string) => v ? new Date(v).toLocaleString('zh-CN') : '—',
                  },
                  {
                    title: '状态',
                    dataIndex: 'status',
                    key: 'status',
                    render: (s: string) => (
                      <Tag color={s === 'completed' || s === 'success' ? 'green' : s === 'failed' ? 'red' : 'orange'}>
                        {s === 'completed' ? '成功' : s === 'failed' ? '失败' : s}
                      </Tag>
                    ),
                  },
                  {
                    title: '累计收益',
                    dataIndex: 'total_return',
                    key: 'total_return',
                    render: (v: number) => v !== null && v !== undefined ? fmtPct(v) : '—',
                  },
                  {
                    title: 'Sharpe',
                    dataIndex: 'sharpe_ratio',
                    key: 'sharpe',
                    render: (v: number) => v !== null && v !== undefined ? fmt(v, 2) : '—',
                  },
                  {
                    title: '最大回撤',
                    dataIndex: 'max_drawdown',
                    key: 'max_drawdown',
                    render: (v: number) => v !== null && v !== undefined ? fmtPct(Math.abs(v)) : '—',
                  },
                  {
                    title: '操作',
                    key: 'action',
                    render: (_, record) => (
                      <Button
                        size="small"
                        type="link"
                        onClick={() => message.info('查看回测详情功能开发中')}
                      >
                        查看
                      </Button>
                    ),
                  },
                ]}
              />
            ),
          },
          {
            key: 'yearly',
            label: '年度统计',
            children: <YearlyStats equity={result.equity} />,
          },
          {
            key: 'trades',
            label: `成交 (${result.trades.length})`,
            children: <TradeTable trades={result.trades} onExplain={setExplainTrade} />,
          },
          {
            key: 'positions',
            label: `末日持仓 (${result.positions.length})`,
            children:
              result.positions.length === 0 ? (
                <Empty description="末日空仓" />
              ) : (
                <Table
                  size="small"
                  rowKey={(r) => r.symbol}
                  pagination={false}
                  dataSource={result.positions}
                  columns={[
                    { title: '代码', dataIndex: 'symbol' },
                    { title: '数量', dataIndex: 'qty' },
                    { title: '成本', dataIndex: 'cost', render: (v: number) => fmt(v, 0) },
                    { title: '市值', dataIndex: 'market_value', render: (v: number) => fmt(v, 0) },
                    { title: '盈亏%', dataIndex: 'pnl_pct', render: (v: number) => fmtPct(v) },
                  ]}
                />
              ),
          },
          {
            key: 'logs',
            label: `日志 (${result.logs.length})`,
            children:
              result.logs.length === 0 ? (
                <Empty description="无日志" />
              ) : (
                <pre
                  style={{
                    fontSize: 11,
                    background: '#1e1e1e',
                    color: '#d4d4d4',
                    padding: 12,
                    borderRadius: 4,
                    maxHeight: 240,
                    overflow: 'auto',
                    margin: 0,
                  }}
                >
                  {result.logs
                    .map((l) => `[${l.level.toUpperCase()}] ${l.ts || ''} ${l.msg}`)
                    .join('\n')}
                </pre>
              ),
          },
          ...(result.warnings.length > 0
            ? [
                {
                  key: 'warnings',
                  label: `提示 (${result.warnings.length})`,
                  children: (
                    <Alert
                      type="warning"
                      showIcon
                      message="回测引擎产生以下提示"
                      description={
                        <ul style={{ margin: 0, paddingLeft: 20 }}>
                          {result.warnings.map((w, i) => (
                            <li key={i} style={{ fontSize: 12 }}>
                              {w}
                            </li>
                          ))}
                        </ul>
                      }
                    />
                  ),
                },
              ]
            : []),
        ]}
      />

      <WhyExplainerModal trade={explainTrade} onClose={() => setExplainTrade(null)} />
    </Card>
  );
};

const TradeTable: React.FC<{
  trades: StrategyLabTradeRecord[];
  onExplain?: (t: StrategyLabTradeRecord) => void;
}> = ({ trades, onExplain }) => {
  if (!trades.length) return <Empty description="无成交" />;
  return (
    <Table
      size="small"
      rowKey="_key"
      pagination={{ pageSize: 50, showSizeChanger: false }}
      scroll={{ y: 280 }}
      dataSource={trades.map((r, i) => ({ ...r, _key: String(i) }))}
      columns={[
        { title: '日期', dataIndex: 'date', width: 100 },
        { title: '代码', dataIndex: 'symbol', width: 100 },
        {
          title: '方向',
          dataIndex: 'direction',
          width: 70,
          render: (v: string) => <Tag color={v === 'BUY' ? 'red' : 'green'}>{v}</Tag>,
        },
        { title: '价格', dataIndex: 'price', width: 80, render: (v: number) => fmt(v, 2) },
        { title: '数量', dataIndex: 'qty', width: 80 },
        {
          title: 'PnL',
          dataIndex: 'pnl',
          width: 80,
          render: (v: number | null) =>
            v === null || v === undefined ? (
              '—'
            ) : (
              <span style={{ color: v >= 0 ? '#3f8600' : '#cf1322' }}>{fmt(v, 0)}</span>
            ),
        },
        { title: '原因', dataIndex: 'reason', ellipsis: true },
        ...(onExplain
          ? [
              {
                title: '',
                width: 60,
                render: (_: any, t: StrategyLabTradeRecord) => (
                  <Button size="small" type="link" onClick={() => onExplain(t)}>
                    Why
                  </Button>
                ),
              },
            ]
          : []),
      ]}
    />
  );
};

export default StrategyLabResultPanel;
