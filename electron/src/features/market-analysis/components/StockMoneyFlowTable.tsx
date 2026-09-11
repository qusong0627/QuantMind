import React, { useState } from 'react';
import { Table, Calendar as CalendarIcon } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { TrendingUp, TrendingDown, ArrowUpRight, Calendar, Sparkles } from 'lucide-react';

export interface DailyFlowPoint {
  date: string;
  inflow: number; // 资金总流入 (亿)
  outflow: number; // 资金总流出 (亿)
  net_flow: number; // 资金净流入 (亿)
}

export interface StockMoneyFlowItem {
  symbol: string;
  name: string;
  close_price: number;
  pct_change: number;
  net_inflow: number; // 净流入 (元)
  gross_inflow?: number; // 资金总流入 (元)
  gross_outflow?: number; // 资金总流出 (元)
  main_ratio?: number; // 主力占比 %
  trend_30d?: number[]; // 30个交易日资金净流入历史 (亿元)
  daily_details_30d?: DailyFlowPoint[]; // 30日每日详细资金数据
  super_large?: number;
  large?: number;
  medium?: number;
  small?: number;
}

interface StockMoneyFlowTableProps {
  items: StockMoneyFlowItem[];
  loading?: boolean;
  isMini?: boolean;
  latestDate?: string; // 真实最新交易日 (YYYY-MM-DD)
  onStockClick?: (item: StockMoneyFlowItem) => void; // 🎯 点击个股打开资金流详情
}

// 🎨 30个交易日按日交互 SVG 趋势图组件 (鼠标悬浮显示当日详细资金情况)
const InteractiveTrendLine30D: React.FC<{
  netInflow: number;
  historyPoints?: DailyFlowPoint[];
  height?: number;
  index: number;
}> = ({ historyPoints, height = 48, index }) => {
  const [hoverPt, setHoverPt] = useState<{
    idx: number;
    x: number;
    y: number;
    date: string;
    inflow: number;
    outflow: number;
    net_flow: number;
  } | null>(null);

  const width = 290;

  const dailyPoints = (historyPoints && historyPoints.length > 0 ? historyPoints : [])
    .slice()
    .sort((a, b) => a.date.localeCompare(b.date));

  if (dailyPoints.length < 2) {
    return (
      <div className="flex items-center h-full">
        <span className="text-[10px] text-slate-400">暂无 30 日资金明细</span>
      </div>
    );
  }

  const netVals = dailyPoints.map((p) => p.net_flow);

  const min = Math.min(...netVals);
  const max = Math.max(...netVals);
  const range = max - min === 0 ? 1 : max - min;

  // 计算 30 个数据点的 SVG 坐标
  const svgCoords = dailyPoints.map((pt, idx) => {
    const x = (idx / (dailyPoints.length - 1)) * (width - 16) + 8;
    const y = height - 8 - ((pt.net_flow - min) / range) * (height - 16);
    return { x, y, pt, idx };
  });

  const pathD = svgCoords.reduce((acc, pt, i) => {
    return i === 0 ? `M ${pt.x} ${pt.y}` : `${acc} L ${pt.x} ${pt.y}`;
  }, '');

  const areaD = `${pathD} L ${svgCoords[svgCoords.length - 1].x} ${height - 2} L ${svgCoords[0].x} ${height - 2} Z`;

  const isNetPositive = netVals[netVals.length - 1] >= netVals[0];
  const strokeColor = isNetPositive ? '#ef4444' : '#10b981';
  const gradientId = `flow-grad-int-${index}-${Math.random().toString(36).substring(2, 7)}`;

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const mouseX = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
    const closestIdx = Math.round((mouseX / rect.width) * (dailyPoints.length - 1));
    const target = svgCoords[closestIdx];

    setHoverPt({
      idx: closestIdx,
      x: target.x,
      y: target.y,
      date: target.pt.date,
      inflow: target.pt.inflow,
      outflow: target.pt.outflow,
      net_flow: target.pt.net_flow,
    });
  };

  const latestVal = netVals[netVals.length - 1].toFixed(2);

  // 第一行向上展示会被表格容器边缘遮挡，改为向下展示，其余行保持向上不变
  const isFirstRow = index === 0;

  return (
    <div className="relative flex items-center gap-3">
      {/* 悬浮交互浮层 (鼠标指向指定日期显示当日详细资金) */}
      {hoverPt && (
        <div className={`absolute left-1/2 -translate-x-1/2 bg-slate-950/95 text-white p-3 rounded-2xl shadow-2xl border border-purple-500/40 text-xs z-50 whitespace-nowrap backdrop-blur-md pointer-events-none animate-in fade-in zoom-in-95 duration-150 ${isFirstRow ? 'top-full mt-2' : 'bottom-full mb-2'}`}>
          <div className="font-mono text-[11px] text-purple-300 font-extrabold border-b border-slate-800 pb-1.5 mb-1.5 flex items-center justify-between gap-4">
            <span className="flex items-center gap-1">
              <Calendar className="w-3 h-3 text-purple-400" />
              {hoverPt.date}
            </span>
            <span className="px-1.5 py-0.5 rounded bg-purple-950 text-purple-300 text-[10px]">
              第 {hoverPt.idx + 1} / {dailyPoints.length} 交易日
            </span>
          </div>

          <div className="space-y-1">
            <div className="flex items-center justify-between gap-5 text-[11px]">
              <span className="text-slate-400 font-medium">当日资金总流入:</span>
              <span className="font-mono font-extrabold text-red-400">+{hoverPt.inflow.toFixed(2)} 亿</span>
            </div>
            <div className="flex items-center justify-between gap-5 text-[11px]">
              <span className="text-slate-400 font-medium">当日资金总流出:</span>
              <span className="font-mono font-extrabold text-emerald-400">-{hoverPt.outflow.toFixed(2)} 亿</span>
            </div>
            <div className="flex items-center justify-between gap-5 text-[11px] border-t border-slate-800/80 pt-1 mt-1">
              <span className="text-slate-200 font-bold">当日资金净流向:</span>
              <span className={`font-mono font-extrabold ${hoverPt.net_flow >= 0 ? 'text-red-400' : 'text-emerald-400'}`}>
                {hoverPt.net_flow >= 0 ? '+' : ''}{hoverPt.net_flow.toFixed(2)} 亿
              </span>
            </div>
          </div>
        </div>
      )}

      <div className="relative group cursor-crosshair">
        <svg
          width={width}
          height={height}
          className="overflow-visible"
          onMouseMove={handleMouseMove}
          onMouseLeave={() => setHoverPt(null)}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={strokeColor} stopOpacity="0.35" />
              <stop offset="100%" stopColor={strokeColor} stopOpacity="0.0" />
            </linearGradient>
          </defs>

          {/* 零轴虚线 */}
          <line x1="8" y1={height / 2} x2={width - 8} y2={height / 2} stroke="#cbd5e1" strokeDasharray="3 3" strokeWidth="1" />

          {/* 渐变填充与折线 */}
          <path d={areaD} fill={`url(#${gradientId})`} />
          <path d={pathD} fill="none" stroke={strokeColor} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />

          {/* 鼠标悬浮竖向准星辅助线与提示点 */}
          {hoverPt && (
            <>
              <line x1={hoverPt.x} y1="4" x2={hoverPt.x} y2={height - 4} stroke="#a855f7" strokeDasharray="2 2" strokeWidth="1.5" />
              <circle cx={hoverPt.x} cy={hoverPt.y} r="4.5" fill="#a855f7" stroke="#ffffff" strokeWidth="2" />
            </>
          )}

          {/* 最新交易日端点 */}
          {!hoverPt && (
            <circle cx={svgCoords[svgCoords.length - 1].x} cy={svgCoords[svgCoords.length - 1].y} r="3.5" fill={strokeColor} className="animate-pulse" />
          )}
        </svg>
      </div>

      <div className="flex flex-col text-[10px] font-mono font-bold leading-tight flex-shrink-0">
        <span className={isNetPositive ? 'text-red-500' : 'text-emerald-500'}>
          {isNetPositive ? '+' : ''}{latestVal}亿
        </span>
        <span className="text-slate-400 font-normal">30日看盘</span>
      </div>
    </div>
  );
};

export const StockMoneyFlowTable: React.FC<StockMoneyFlowTableProps> = ({
  items,
  loading = false,
  isMini = false,
  latestDate,
  onStockClick,
}) => {
  const columns: ColumnsType<StockMoneyFlowItem> = [
    {
      title: '序号',
      key: 'index',
      width: 50,
      align: 'center',
      render: (_, __, index) => (
        <span className="font-mono text-xs font-extrabold text-purple-600">
          {index + 1}
        </span>

      ),
    },
    {
      title: '股票代码 / 名称',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 165,
      render: (symbol, record) => (
        <div className="flex items-center gap-2.5 py-1 whitespace-nowrap">
          <div className="w-8 h-8 rounded-xl bg-purple-50 text-purple-700 font-extrabold text-xs flex items-center justify-center border border-purple-100 shadow-inner flex-shrink-0">
            {record.name.substring(0, 1)}
          </div>
          <div className="truncate">
            <div className="font-extrabold text-slate-900 text-xs truncate flex items-center gap-0.5">
              <span>{record.name}</span>
              <ArrowUpRight className="w-3 h-3 text-slate-400 flex-shrink-0" />
            </div>
            <div className="text-[10px] text-slate-400 font-mono">{symbol}</div>
          </div>
        </div>
      ),
    },
    {
      title: '最新价',
      dataIndex: 'close_price',
      key: 'close_price',
      align: 'right',
      width: 85,
      render: (val) => (
        <span className="font-mono text-xs font-semibold text-slate-800 whitespace-nowrap">
          ¥{val.toFixed(2)}
        </span>
      ),
    },
    {
      title: '涨跌幅',
      dataIndex: 'pct_change',
      key: 'pct_change',
      align: 'right',
      width: 95,
      render: (val) => {
        const isPos = val >= 0;
        return (
          <span className={`font-mono text-xs font-bold flex items-center justify-end gap-0.5 whitespace-nowrap ${isPos ? 'text-red-500' : 'text-emerald-500'}`}>
            {isPos ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
            {isPos ? '+' : ''}{val.toFixed(2)}%
          </span>
        );
      },
    },
    {
      title: '资金总流入',
      key: 'gross_inflow',
      align: 'right',
      width: 110,
      render: (_, r) => (
        <span className="font-mono text-xs font-bold text-red-500 whitespace-nowrap">
          {r.gross_inflow ? `+${(r.gross_inflow / 1e8).toFixed(2)} 亿` : '—'}
        </span>
      ),
    },
    {
      title: '资金总流出',
      key: 'gross_outflow',
      align: 'right',
      width: 110,
      render: (_, r) => (
        <span className="font-mono text-xs font-bold text-emerald-600 whitespace-nowrap">
          {r.gross_outflow ? `-${(r.gross_outflow / 1e8).toFixed(2)} 亿` : '—'}
        </span>
      ),
    },
    {
      title: '当日资金净流入',
      dataIndex: 'net_inflow',
      key: 'net_inflow',
      align: 'right',
      width: 120,
      render: (val) => {
        const isPos = val >= 0;
        const valYi = (val / 1e8).toFixed(2);
        return (
          <span className={`font-mono text-xs font-extrabold px-2.5 py-0.5 rounded-full border whitespace-nowrap ${
            isPos ? 'bg-red-50 text-red-600 border-red-100 shadow-xs' : 'bg-emerald-50 text-emerald-600 border-emerald-100 shadow-xs'
          }`}>
            {isPos ? '+' : ''}{valYi} 亿
          </span>
        );
      },
    },
    ...(!isMini
      ? [
          {
            title: '30 个交易日资金净流入趋势 (鼠标划过查看当日明细)',
            key: 'trend_30d',
            width: 380,
            render: (_: any, r: StockMoneyFlowItem, index: number) => {
              return (
                <InteractiveTrendLine30D
                  netInflow={r.net_inflow}
                  historyPoints={r.daily_details_30d}
                  height={46}
                  index={index}
                />
              );
            },
          },
        ]
      : []),
  ];

  // 🎨 isMini 模式下的专用精细化全居中对齐列配置 (防止表头换行、统一居中)
  const miniColumns: ColumnsType<StockMoneyFlowItem> = [
    {
      title: <span className="whitespace-nowrap">序号</span>,
      key: 'index',
      width: 60,
      align: 'center',
      render: (_, __, index) => (
        <span className="font-mono text-xs font-extrabold text-purple-600">
          {index + 1}
        </span>

      ),
    },
    {
      title: '股票代码 / 名称',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 145,
      align: 'left',
      render: (symbol, record) => (
        <div className="flex items-center gap-2 py-0.5 whitespace-nowrap pl-1">
          <div className="flex flex-col text-left justify-center">
            <div className="font-extrabold text-slate-800 text-[11px] leading-tight flex items-center gap-0.5">
              <span>{record.name}</span>
              <ArrowUpRight className="w-2.5 h-2.5 text-slate-400 flex-shrink-0" />
            </div>
            <div className="text-[10px] text-purple-600/90 font-mono font-bold leading-tight">{symbol}</div>
          </div>
        </div>
      ),
    },
    {
      title: '最新价',
      dataIndex: 'close_price',
      key: 'close_price',
      align: 'center',
      width: 70,
      render: (val) => (
        <span className="font-mono text-[11px] font-semibold text-slate-800 whitespace-nowrap">
          ¥{val.toFixed(2)}
        </span>
      ),
    },
    {
      title: '涨跌幅',
      dataIndex: 'pct_change',
      key: 'pct_change',
      align: 'center',
      width: 80,
      render: (val) => {
        const isPos = val >= 0;
        return (
          <span className={`font-mono text-[11px] font-bold flex items-center justify-center gap-0.5 whitespace-nowrap ${isPos ? 'text-red-500' : 'text-emerald-500'}`}>
            {isPos ? <TrendingUp className="w-2.5 h-2.5" /> : <TrendingDown className="w-2.5 h-2.5" />}
            {isPos ? '+' : ''}{val.toFixed(2)}%
          </span>
        );
      },
    },
    {
      title: '总流入',
      key: 'gross_inflow',
      align: 'center',
      width: 80,
      render: (_, r) => (
        <span className="font-mono text-[11px] font-bold text-red-500 whitespace-nowrap">
          {r.gross_inflow ? `+${(r.gross_inflow / 1e8).toFixed(2)}亿` : '—'}
        </span>
      ),
    },
    {
      title: '总流出',
      key: 'gross_outflow',
      align: 'center',
      width: 80,
      render: (_, r) => (
        <span className="font-mono text-[11px] font-bold text-emerald-600 whitespace-nowrap">
          {r.gross_outflow ? `-${(r.gross_outflow / 1e8).toFixed(2)}亿` : '—'}
        </span>
      ),
    },
    {
      title: '净流入',
      dataIndex: 'net_inflow',
      key: 'net_inflow',
      align: 'center',
      width: 85,
      render: (val) => {
        const isPos = val >= 0;
        const valYi = (val / 1e8).toFixed(2);
        return (
          <span className={`font-mono text-[11px] font-extrabold px-2 py-0.5 rounded-full border whitespace-nowrap ${
            isPos ? 'bg-red-50 text-red-600 border-red-100 shadow-2xs' : 'bg-emerald-50 text-emerald-600 border-emerald-100 shadow-2xs'
          }`}>
            {isPos ? '+' : ''}{valYi}亿
          </span>
        );
      },
    },
  ];

  return (
    <div className={`w-full flex flex-col gap-3 bg-white/95 backdrop-blur-md rounded-2xl ${isMini ? 'p-1 border-0 shadow-none' : 'p-4 border border-purple-100/80 shadow-sm'}`}>
      {/* 按日查询交易日切换 Header */}
      {!isMini && (
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 border-b border-purple-100/60 pb-3">
          <div className="flex items-center gap-2">
            <span className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
              <Calendar className="w-3.5 h-3.5 text-purple-600" />
              <span>最新交易日:</span>
            </span>
            {latestDate ? (
              <span className="px-3 py-1 rounded-full bg-purple-100/80 text-purple-700 text-xs font-bold font-mono border border-purple-200 shadow-2xs">
                {latestDate} (最新数据)
              </span>
            ) : (
              <span className="px-3 py-1 rounded-full bg-slate-100 text-slate-400 text-xs font-bold font-mono border border-slate-200">
                待数据加载
              </span>
            )}
          </div>

          <span className="text-[11px] text-slate-500 font-medium flex items-center gap-1">
            <Sparkles className="w-3.5 h-3.5 text-purple-500" />
            右侧折线图支持鼠标光标滑动按日划转查阅 30 个交易日明细
          </span>
        </div>
      )}


      <div className="w-full overflow-hidden rounded-xl border border-slate-200/80 bg-white">
        <Table
          columns={isMini ? miniColumns : columns}
          dataSource={items.map((item, idx) => ({ ...item, key: idx }))}
          loading={loading}
          pagination={false}
          size="middle"
          rowClassName={onStockClick ? "h-16 hover:bg-purple-50/30 transition-colors cursor-pointer" : "h-16 hover:bg-purple-50/30 transition-colors"}
          onRow={onStockClick ? (record) => ({ onClick: () => onStockClick(record) }) : undefined}
          scroll={isMini ? undefined : { x: 1100 }}
        />
      </div>
    </div>
  );
};
