/** 跨市场共享 · 板块热力矩形图（treemap）
 *
 * 数据形状（name / value / pct_change / leader / leader_pct）在 A 股（申万一级）、
 * 港股（恒生行业）、美股（GICS 11 大类）三个市场一致，因此组件跨市场共用。
 *
 * 历史：本组件原为 `features/market-analysis/components/ShenwanHeatmapChart.tsx`
 * （A 股专用命名），提升到共享目录后原路径改为 re-export，调用方零改动。
 */

import React, { useEffect, useRef } from 'react';
import * as echarts from 'echarts';

export interface SectorHeatmapItem {
  name: string;
  value: number; // 规模权重（亿），仅用于面积
  pct_change: number; // 涨跌幅 %
  leader?: string;
  leader_pct?: number;
  /** 主力净额（亿）；仅下钻/按净额着色时有值 */
  flow_net?: number;
  /** 周期涨幅（%）：>1 日时用于着色/标签，1 日等于 pct_change */
  pct_period?: number | null;
  /** 周期日均主力净额（亿/日） */
  flow_daily?: number | null;
  /** 前一窗口日均（亿/日） */
  prev_flow_daily?: number | null;
  /** 加速度 = 当前日均 − 前一窗口日均（亿/日） */
  flow_accel?: number | null;
  /** 周期净额比 */
  flow_ratio?: number | null;
  /** 当前窗口「成分主力净流入为正」的日均占比（资金广度，0~1） */
  breadth_flow?: number | null;
  /** 资金广度变化（当前 − 前一窗口） */
  breadth_flow_chg?: number | null;
  /** 板块净额比加速（当前窗口日均 − 前一窗口日均） */
  flow_ratio_accel?: number | null;
  /** 成分收盘价在 MA20 上方的占比（价格广度，0~1） */
  pct_above_ma20?: number | null;
  /** 价格广度变化（当前 − 5 日前） */
  pct_above_ma20_chg?: number | null;
  /** 价格加速度：当前 N 日逐日等权收益均值 − 前 N 日均值（百分点/日） */
  price_accel?: number | null;
  /** 情绪热度（0~1，截面分位合成；不是概率） */
  sentiment_score?: number | null;
  /** 板块方向概率（键 5/10/20）；仅行业二级最新快照有值 */
  direction?: Record<
    string,
    {
      prob: number;
      rel_prob: number;
      market_prob?: number;
      label?: 'up' | 'down' | 'neutral';
      mean?: number | null;
      n?: number | null;
      date_win?: number | null;
      n_dates?: number | null;
      rank?: number | null;
      state?: { stress: string; flow: string; price: string };
    }
  > | null;
  /** tooltip 透传附加字段（下钻层展示细分行业/量比/成交额等）；不参与布局 */
  extra?: Record<string, unknown>;
}

interface SectorHeatmapChartProps {
  data?: SectorHeatmapItem[];
  height?: number | string;
  /** 面积权重的量纲名（tooltip 文案），默认「市值权重规模」 */
  valueLabel?: string;
  /** 点击色块回调（下钻入口）；不传则色块不可点 */
  onItemClick?: (name: string) => void;
  /** 逐块取色覆盖（如按主力净额着色）；不传则用内置涨跌幅档位色 */
  colorOf?: (item: SectorHeatmapItem) => string;
  /** 逐块标签数值文本覆盖（第二行）；不传则用 `+x.xx%` */
  labelValueOf?: (item: SectorHeatmapItem) => string;
  /** tooltip 内追加一行（下钻层显示主力净额）；返回空串则不显示 */
  tooltipExtraOf?: (item: SectorHeatmapItem) => string;
  /** 涨跌幅行的口径名（默认「平均涨跌幅」；中位聚合的市场传「中位涨跌幅」） */
  pctLabel?: string;
  /**
   * 渲染键：当取色/标签口径变化（如切换「涨跌幅 ↔ 主力净额」）时，
   * 调用方把该值一并改变即可触发重绘。回调函数一律走 ref 读取最新值，
   * 因此不必在调用点用 useCallback 包裹，也不会因内联箭头函数导致图表重建。
   */
  renderKey?: string | number;
}

export const SectorHeatmapChart: React.FC<SectorHeatmapChartProps> = ({
  data,
  height = 460,
  valueLabel = '市值权重规模',
  onItemClick,
  colorOf,
  labelValueOf,
  tooltipExtraOf,
  pctLabel = '平均涨跌幅',
  renderKey = '',
}) => {
  const chartRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);

  // 回调一律经 ref 透传最新实现：既能避免内联函数触发重建，又保证点击/取色用的是最新闭包。
  const onItemClickRef = useRef(onItemClick);
  const colorOfRef = useRef(colorOf);
  const labelValueOfRef = useRef(labelValueOf);
  const tooltipExtraOfRef = useRef(tooltipExtraOf);
  const pctLabelRef = useRef(pctLabel);
  useEffect(() => {
    onItemClickRef.current = onItemClick;
    colorOfRef.current = colorOf;
    labelValueOfRef.current = labelValueOf;
    tooltipExtraOfRef.current = tooltipExtraOf;
    pctLabelRef.current = pctLabel;
  });

  const sectorItems = data && data.length > 0 ? data : [];

  // 两行 rich 文本（名称行高15 + 数值行~12.5）加上 gapWidth 裁剪补偿的总高校准值（离屏 SVG 渲染实测，误差 ±0.25px）；
  // echarts treemap 会把文本盒钉死为色块尺寸并忽略 verticalAlign，只能靠 padding 在盒内下移实现居中
  const LABEL_TEXT_HEIGHT = 31;

  const redrawRef = useRef<() => void>(() => {});
  const clickAttachedRef = useRef(false);

  // 只初始化一次：之后数据/颜色/日期变化都走 setOption 合并，动画才能看出色块变化，
  // 而不是每次 dispose + init 导致整图闪一下。
  useEffect(() => {
    if (!chartRef.current) return;
    const chart = echarts.init(chartRef.current);
    instanceRef.current = chart;
    const handleClick = (params: any) => {
      if (params?.componentType !== 'series' || !params?.name) return;
      onItemClickRef.current?.(String(params.name));
    };
    const attachClick = (c: echarts.ECharts) => {
      if (clickAttachedRef.current) return;
      c.on('click', handleClick);
      clickAttachedRef.current = true;
    };
    attachClick(chart);
    redrawRef.current = () => {
      chart.resize();
    };
    const ro = new ResizeObserver(() => {
      chart.resize();
      redrawRef.current();
    });
    ro.observe(chartRef.current);
    return () => {
      ro.disconnect();
      chart.off('click', handleClick);
      chart.dispose();
      instanceRef.current = null;
      clickAttachedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current || sectorItems.length === 0) return;
    const chart = instanceRef.current ?? echarts.init(chartRef.current);
    instanceRef.current = chart;
    // 首屏数据为空时挂载 effect 拿不到 DOM（渲染的是空态占位），图表会在本 effect
    // 里首次初始化；点击处理器必须在这里补挂，否则只能 hover、不能下钻。
    if (!clickAttachedRef.current) {
      chart.on('click', (params: any) => {
        if (params?.componentType !== 'series' || !params?.name) return;
        onItemClickRef.current?.(String(params.name));
      });
      clickAttachedRef.current = true;
    }

    const formattedData = sectorItems.map((item) => ({
      id: item.name,
      name: item.name,
      // 🎯 非线性压缩(幂0.4)，避免超大板块(如半导体/银行)吞掉整张图；tooltip 用 raw_value 显示真实规模
      value: Math.pow(Math.max(item.value || 0, 1), 0.4),
      raw_value: item.value,
      pct_change: item.pct_change,
      leader: item.leader,
      leader_pct: item.leader_pct,
      flow_net: (item as { flow_net?: number }).flow_net,
      extra: item.extra,
      // 原始 item 透传：tooltipExtraOf 需要 flow_accel/price_accel/direction/情绪等字段，
      // 只传手工挑选的字段会让「量价共振/方向概率」tooltip 全显示 —。
      source: item,
      // 第二行标签文本：默认涨跌幅，下钻层可覆盖为「+x.xx亿」等
      label_value:
        labelValueOfRef.current?.(item) ??
        `${item.pct_change >= 0 ? '+' : ''}${item.pct_change.toFixed(2)}%`,
      itemStyle: {
        // 默认按涨跌幅档位着色；下钻层可传 colorOf 覆盖（按主力净额等）
        color:
          colorOfRef.current?.(item) ??
          (item.pct_change > 2
            ? '#ef4444'
            : item.pct_change > 0
            ? '#f87171'
            : item.pct_change === 0
            ? '#94a3b8'
            : item.pct_change > -2
            ? '#34d399'
            : '#10b981'),
        borderRadius: 2,
      },
    }));

    const option: echarts.EChartsOption = {
      animationDuration: 500,
      animationDurationUpdate: 650,
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicInOut',
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 23, 42, 0.92)',
        borderColor: '#334155',
        textStyle: { color: '#ffffff' },
        formatter: (info: any) => {
          const d = info.data || {};
          const cap = (d.raw_value ?? d.value) ?? info.value ?? 0;
          const pct = d.pct_change ?? 0;
          const color = pct >= 0 ? '#f87171' : '#34d399';
          const leaderText = d.leader ? `<div style="margin-top: 2px; color: #cbd5e1;">领涨龙头: <b>${d.leader} (${d.leader_pct >= 0 ? '+' : ''}${d.leader_pct}%)</b></div>` : '';
          const extra = (() => {
            const fn = tooltipExtraOfRef.current;
            if (!fn) return '';
            const raw = fn(
              (d.source as SectorHeatmapItem | undefined) ?? {
                name: info.name,
                value: cap,
                pct_change: pct,
                leader: d.leader,
                leader_pct: d.leader_pct,
                flow_net: d.flow_net,
                extra: d.extra,
              } as SectorHeatmapItem,
            );
            return raw ? `<div style="margin-top: 2px;">${raw}</div>` : '';
          })();
          return `
            <div style="font-family: sans-serif; font-size: 12px; padding: 2px 4px;">
              <div style="font-weight: bold; font-size: 13px; margin-bottom: 4px; border-bottom: 1px solid #475569; padding-bottom: 4px;">${info.name}</div>
              <div>${valueLabel}: <b>${cap} 亿</b></div>
              <div>${pctLabelRef.current}: <span style="color: ${color}; font-weight: bold;">${pct >= 0 ? '+' : ''}${pct}%</span></div>
              ${leaderText}
              ${extra}
            </div>
          `;
        },
      },
      series: [
        {
          id: 'sector-heatmap',
          type: 'treemap',
          top: 4,
          bottom: 4,
          left: 4,
          right: 4,
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          label: {
            show: true,
            overflow: 'truncate',
            ellipsis: '…',
            formatter: (params: any) => {
              const d = params.data || {};
              // label_value 由 formattedData 预计算（默认涨跌幅；下钻层可覆盖为净额）
              const val = d.label_value ?? '';
              return `{name|${params.name}}\n{pct|${val}}`;
            },
            rich: {
              name: {
                fontSize: 11,
                fontWeight: 'bold',
                color: '#ffffff',
                lineHeight: 15,
                align: 'center',
              },
              pct: {
                fontSize: 10,
                fontWeight: 'bold',
                color: '#ffffff',
                fontFamily: 'monospace',
                align: 'center',
              },
            },
          },
          itemStyle: {
            borderColor: '#ffffff',
            borderWidth: 1.5,
            gapWidth: 1.5,
          },
          data: formattedData,
        },
      ],
    };

    // 合并更新（不销毁实例）：日期/颜色/周期变化时 ECharts 自己补间色块与布局，
    // replaceMerge 只替换 series 数据，保留图表实例与动画状态。
    chart.setOption(option, { notMerge: false, replaceMerge: ['series'] });

    // 第二遍渲染：按每个色块自身高度注入上下 padding，实现文字水平+垂直居中；
    // padding 在「盒高=色块高」的文本盒内生效：上下各留 (块高-文字高)/2，两行文本整体上下居中；
    // align: 'center' 让名称/涨跌幅两行水平居中于色块中心线（块宽不足时由 truncate 兜底）
    const applyCenteredLabels = () => {
      try {
        const seriesModel = (chart as any).getModel?.()?.getSeriesByIndex?.(0);
        const treeRoot = seriesModel?.getData?.()?.tree?.root;
        if (!treeRoot) return;
        const layoutByName: Record<string, { width: number; height: number }> = {};
        treeRoot.eachNode({ attr: 'viewChildren', order: 'preorder' }, (node: any) => {
          const layout = node.getLayout?.();
          if (node.isRoot || !layout || layout.width <= 0) return;
          layoutByName[node.name] = layout;
        });
        const centeredData = formattedData.map((item) => {
          const layout = layoutByName[item.name];
          if (!layout) return { ...item, label: { show: false } };
          // 小色块不显示文字：避免下钻层 100+ 只个股时标签互相挤压；
          // 大色块统一两行居中 + 超宽省略。
          const show = layout.width >= 44 && layout.height >= 26;
          const pad = Math.max(0, (layout.height - LABEL_TEXT_HEIGHT) / 2);
          return {
            ...item,
            label: { show, padding: [pad, 0, pad, 0], align: 'center', overflow: 'truncate' },
          };
        });
        chart.setOption({ series: [{ id: 'sector-heatmap', data: centeredData }] });
      } catch {
        // 内部 API 变化时退化为默认顶部对齐，不影响图表展示
      }
    };
    applyCenteredLabels();

    redrawRef.current = () => {
      chart.resize();
      chart.setOption({ series: [{ id: 'sector-heatmap', data: formattedData }] });
      applyCenteredLabels();
    };
  }, [sectorItems, valueLabel, pctLabel, renderKey]);

  if (sectorItems.length === 0) {
    return (
      <div
        className="w-full flex items-center justify-center text-xs text-slate-400 bg-slate-50/60 rounded-2xl border border-dashed border-slate-200"
        style={{ height }}
      >
        暂无热力图数据
      </div>
    );
  }

  return (
    <div
      ref={chartRef}
      style={{ width: '100%', height, cursor: onItemClick ? 'pointer' : 'default' }}
      className="w-full"
    />
  );
};

// ---- 向后兼容别名（A 股/港股侧的历史命名，勿在新代码中使用） ----
export type ShenwanSectorItem = SectorHeatmapItem;
export const ShenwanHeatmapChart = SectorHeatmapChart;
