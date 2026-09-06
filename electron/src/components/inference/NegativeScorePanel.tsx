import React, { useEffect, useMemo, useState } from 'react';
import {
  Button, Card, Tag, Typography, Empty, Spin, Table, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  RefreshCw, TrendingDown, ShieldCheck, Download,
} from 'lucide-react';
import {
  getNegativeSelection,
  NegativeSelectionResponse,
  ShortCandidate,
  MissedReference,
} from '../../services/stockPickingService';
import { buildCsvText, downloadCsvFile } from '../../utils/csvExport';

const { Text } = Typography;

const CAP_COLOR: Record<string, string> = {
  微盘: 'red',
  小盘: 'volcano',
  中盘: 'gold',
  大盘: 'blue',
  超大盘: 'purple',
  未知: 'default',
};

const BOARD_COLOR: Record<string, string> = {
  主板: 'blue',
  创业板: 'green',
  科创板: 'purple',
  其他: 'default',
};

export const NegativeScorePanel: React.FC = () => {
  const [data, setData] = useState<NegativeSelectionResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const resp = await getNegativeSelection();
      setData(resp);
    } catch (err: any) {
      message.error(`负分分析失败: ${err?.message ?? '未知错误'}`);
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const shortColumns: ColumnsType<ShortCandidate> = [
    { title: '股票', dataIndex: 'symbol', width: 120, render: (v, r) => (
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-[12px] text-slate-800">{v}</span>
        {r.name && <span className="text-[10px] text-slate-400">{r.name}</span>}
      </div>
    )},
    { title: '分数', dataIndex: 'score', width: 90, sorter: (a, b) => a.score - b.score, render: (v: number) => (
      <span className="font-mono font-bold text-[12px] text-red-500">{v.toFixed(4)}</span>
    )},
    { title: '市值', dataIndex: 'cap', width: 70, render: (v: string) => (
      <Tag color={CAP_COLOR[v] ?? 'default'} className="rounded-md text-[9px] m-0">{v}</Tag>
    )},
    { title: '板块', dataIndex: 'board', width: 70, render: (v: string) => (
      <Tag color={BOARD_COLOR[v] ?? 'default'} className="rounded-md text-[9px] m-0">{v}</Tag>
    )},
    { title: '负分依据', dataIndex: 'short_reason', render: (v: string) => (
      <span className="text-[10px] text-slate-500">{v}</span>
    )},
  ];

  const missedColumns: ColumnsType<MissedReference> = [
    { title: '股票', dataIndex: 'symbol', width: 120, render: (v, r) => (
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-[12px] text-slate-800">{v}</span>
        {r.name && <span className="text-[10px] text-slate-400">{r.name}</span>}
      </div>
    )},
    { title: '分数', dataIndex: 'score', width: 90, sorter: (a, b) => a.score - b.score, render: (v: number) => (
      <span className="font-mono font-bold text-[12px] text-blue-500">{v.toFixed(4)}</span>
    )},
    { title: '市值', dataIndex: 'cap', width: 70, render: (v: string) => (
      <Tag color={CAP_COLOR[v] ?? 'default'} className="rounded-md text-[9px] m-0">{v}</Tag>
    )},
    { title: '板块', dataIndex: 'board', width: 70, render: (v: string) => (
      <Tag color={BOARD_COLOR[v] ?? 'default'} className="rounded-md text-[9px] m-0">{v}</Tag>
    )},
    { title: '参考', dataIndex: 'missed_reason', render: (v: string) => (
      <span className="text-[10px] text-emerald-600">{v}</span>
    )},
  ];

  const matrixTable = useMemo(() => {
    const rows = data?.matrix ?? [];
    return rows.map(row => {
      const obj: Record<string, string | number> = { score_band: row.score_band };
      row.caps.forEach(c => { obj[c.cap] = c.count; });
      return obj;
    });
  }, [data]);

  const handleExportCsv = () => {
    if (!data?.short_candidates?.length) return;
    const header = ['symbol', 'name', 'score', 'cap', 'board', 'short_reason'];
    const rows = data.short_candidates.map(c => [
      c.symbol, c.name, c.score, c.cap, c.board, c.short_reason,
    ]);
    const csv = buildCsvText(header, rows, { textColumns: [0], filename: '' });
    if (downloadCsvFile(csv, `负分候选_${data.meta.trade_date ?? 'today'}.csv`)) {
      message.success(`已导出 ${rows.length} 条负分候选`);
    }
  };

  if (loading && !data) {
    return <div className="flex items-center justify-center py-24"><Spin /></div>;
  }

  return (
    <div className="space-y-4">
      {/* 顶部工具条 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-[10px] text-slate-400">
          <TrendingDown size={13} className="text-red-500" />
          <span className="font-black tracking-widest">负分标的分析</span>
          <span className="text-slate-300">|</span>
          <span>负分标的 {data?.meta?.negative_count ?? '—'} 只</span>
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
        <Empty description="无负分数据（请先运行模型推理）" className="py-24" />
      ) : (
        <>
          {/* 分数×市值 矩阵 */}
          <Card
            className="rounded-2xl border-slate-100 shadow-sm"
            title={
              <div className="flex items-center gap-2">
                <span className="text-xs font-black tracking-widest">分数 × 市值 分布矩阵</span>
                <Tag color="default" className="rounded-md m-0 text-[9px]">
                  微盘/小盘分数≤-0.15
                </Tag>
              </div>
            }
            styles={{ header: { padding: '12px 20px' }, body: { padding: '12px 20px' } }}
          >
            {matrixTable.length === 0 ? (
              <Empty description="无负分标的" className="py-8" />
            ) : (
              <Table
                rowKey="score_band"
                size="small"
                pagination={false}
                columns={[
                  { title: '分数段', dataIndex: 'score_band', width: 110, render: (v: string) => (
                    <span className="font-mono text-[11px] font-bold text-slate-600">{v}</span>
                  )},
                  { title: '微盘', dataIndex: '微盘', align: 'center' },
                  { title: '小盘', dataIndex: '小盘', align: 'center' },
                  { title: '中盘', dataIndex: '中盘', align: 'center' },
                  { title: '大盘', dataIndex: '大盘', align: 'center' },
                  { title: '超大盘', dataIndex: '超大盘', align: 'center' },
                ]}
                dataSource={matrixTable}
              />
            )}
          </Card>

          {/* 做空候选 */}
          <Card
            className="rounded-2xl border-slate-100 shadow-sm"
            title={
              <div className="flex items-center gap-2">
                <span className="text-xs font-black tracking-widest">负分聚焦（微盘/小盘）</span>
                <Tag color="red" className="rounded-md m-0 text-[9px]">{data.short_candidates.length} 只</Tag>
              </div>
            }
            styles={{ header: { padding: '12px 20px' }, body: { padding: '12px 20px' } }}
            extra={
              data.short_candidates.length > 0 && (
                <Button size="small" icon={<Download size={12} />} className="rounded-lg text-[10px]" onClick={handleExportCsv}>
                  导出CSV
                </Button>
              )
            }
          >
            {data.short_candidates.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<span className="text-xs text-slate-400">无负分标的</span>}
                className="py-8"
              />
            ) : (
              <Table<ShortCandidate>
                rowKey="symbol"
                size="small"
                columns={shortColumns}
                dataSource={data.short_candidates}
                pagination={false}
              />
            )}
          </Card>

          {/* 错杀参考 */}
          <Card
            className="rounded-2xl border-slate-100 shadow-sm"
            title={
              <div className="flex items-center gap-2">
                <ShieldCheck size={13} className="text-emerald-500" />
                <span className="text-xs font-black tracking-widest">大盘/科创板负分参考</span>
                <Tag color="green" className="rounded-md m-0 text-[9px]">{data.missed_reference.length} 只</Tag>
              </div>
            }
            styles={{ header: { padding: '12px 20px' }, body: { padding: '12px 20px' } }}
          >
            {data.missed_reference.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<span className="text-xs text-slate-400">无相关标的</span>}
                className="py-8"
              />
            ) : (
              <Table<MissedReference>
                rowKey="symbol"
                size="small"
                columns={missedColumns}
                dataSource={data.missed_reference}
                pagination={false}
              />
            )}
          </Card>

          {/* 研究结论提示 */}
          <Card className="rounded-2xl border-amber-100 bg-amber-50/50 shadow-sm" styles={{ body: { padding: '14px 20px' } }}>
            <div className="flex items-start gap-2 text-[11px] text-amber-800">
              <TrendingDown size={13} className="mt-0.5 flex-shrink-0" />
              <span>
                <Text strong>负分统计：</Text>
                下跌概率随市值分档分化：微盘/小盘 分数≤-0.15（T+5下跌概率68.6%）；大盘/超大盘/科创板负分相对抗跌（超大盘-0.13~-0.14上涨概率56.8%）；轻负分(&gt;-0.06)信息量低。当前信号日 {data.meta.trade_date} 负分最深 {data.short_candidates?.[0]?.score?.toFixed(4) ?? '—'}。
              </span>
            </div>
          </Card>
        </>
      )}
    </div>
  );
};
