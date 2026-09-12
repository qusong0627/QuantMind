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
}

interface SectorHeatmapChartProps {
  data?: SectorHeatmapItem[];
  height?: number | string;
  /** 面积权重的量纲名（tooltip 文案），默认「市值权重规模」 */
  valueLabel?: string;
}

export const SectorHeatmapChart: React.FC<SectorHeatmapChartProps> = ({
  data,
  height = 460,
  valueLabel = '市值权重规模',
}) => {
  const chartRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);

  const sectorItems = data && data.length > 0 ? data : [];

  // 两行 rich 文本（名称行高15 + 数值行~12.5）加上 gapWidth 裁剪补偿的总高校准值（离屏 SVG 渲染实测，误差 ±0.25px）；
  // echarts treemap 会把文本盒钉死为色块尺寸并忽略 verticalAlign，只能靠 padding 在盒内下移实现居中
  const LABEL_TEXT_HEIGHT = 31;

  useEffect(() => {
    if (!chartRef.current || sectorItems.length === 0) return;

    if (!instanceRef.current) {
      instanceRef.current = echarts.init(chartRef.current);
    }
    const chart = instanceRef.current;

    const formattedData = sectorItems.map((item) => ({
      name: item.name,
      // 🎯 非线性压缩(幂0.4)，避免超大板块(如半导体/银行)吞掉整张图；tooltip 用 raw_value 显示真实规模
      value: Math.pow(Math.max(item.value || 0, 1), 0.4),
      raw_value: item.value,
      pct_change: item.pct_change,
      leader: item.leader,
      leader_pct: item.leader_pct,
      itemStyle: {
        color:
          item.pct_change > 2
            ? '#ef4444'
            : item.pct_change > 0
            ? '#f87171'
            : item.pct_change === 0
            ? '#94a3b8'
            : item.pct_change > -2
            ? '#34d399'
            : '#10b981',
      },
    }));

    const option: echarts.EChartsOption = {
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
          return `
            <div style="font-family: sans-serif; font-size: 12px; padding: 2px 4px;">
              <div style="font-weight: bold; font-size: 13px; margin-bottom: 4px; border-bottom: 1px solid #475569; padding-bottom: 4px;">${info.name}</div>
              <div>${valueLabel}: <b>${cap} 亿</b></div>
              <div>平均涨跌幅: <span style="color: ${color}; font-weight: bold;">${pct >= 0 ? '+' : ''}${pct}%</span></div>
              ${leaderText}
            </div>
          `;
        },
      },
      series: [
        {
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
            formatter: (params: any) => {
              const d = params.data || {};
              const pct = d.pct_change ?? 0;
              return `{name|${params.name}}\n{pct|${pct >= 0 ? '+' : ''}${pct}%}`;
            },
            rich: {
              name: {
                fontSize: 11,
                fontWeight: 'bold',
                color: '#ffffff',
                lineHeight: 15,
              },
              pct: {
                fontSize: 10,
                fontWeight: 'bold',
                color: '#ffffff',
                fontFamily: 'monospace',
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

    // 第一遍渲染：确定每个色块的实际布局（treemap 布局只取决于 value，与 label padding 无关）
    chart.setOption(option);

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
          if (!layout) return item;
          const pad = Math.max(0, (layout.height - LABEL_TEXT_HEIGHT) / 2);
          return { ...item, label: { padding: [pad, 0, pad, 0], align: 'center' } };
        });
        chart.setOption({ series: [{ data: centeredData }] });
      } catch {
        // 内部 API 变化时退化为默认顶部对齐，不影响图表展示
      }
    };
    applyCenteredLabels();

    // resize 后色块高度变化，先恢复无 padding 布局再按新尺寸重算居中，避免旧 padding 错位（数据未变时布局不变，重算是幂等的）
    const handleResize = () => {
      chart.resize();
      chart.setOption({ series: [{ data: formattedData }] });
      applyCenteredLabels();
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.dispose();
      instanceRef.current = null;
    };
  }, [sectorItems, valueLabel]);

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

  return <div ref={chartRef} style={{ width: '100%', height }} className="w-full" />;
};

// ---- 向后兼容别名（A 股/港股侧的历史命名，勿在新代码中使用） ----
export type ShenwanSectorItem = SectorHeatmapItem;
export const ShenwanHeatmapChart = SectorHeatmapChart;
