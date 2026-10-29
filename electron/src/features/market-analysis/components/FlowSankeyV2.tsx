/**
 * 资金流全景桑基 v2（细案 v2 §4）。
 *
 * - 4 层：主力/散户 → 四档单型 → 板块 → 个股 topK；
 * - ECharts sankey 为 DAG 不可成环——「双向」= 符号双向（value=|净额| 渲宽，
 *   红=净流入 / 绿=净流出）+ 交互双向（点板块/个股回调联动工作台）；
 * - 板块节点带「连N」streak 标注（P 级只挂板块；个股不设预测徽章）；
 * - 个股层节点标签默认隐藏（列内拥挤），hover tooltip 给出全部字段。
 */

import React, { useEffect, useMemo, useRef } from 'react';
import * as echarts from 'echarts';
import { Spin } from 'antd';
import type { FlowPanoramaLink, FlowPanoramaNode } from '../services/moneyFlowService';
import { fmtYi, fmtPct, fmtRatio } from '../services/moneyFlowService';

const INFLOW_COLOR = '#e11d48'; // 红=净流入（A 股口径）
const OUTFLOW_COLOR = '#059669'; // 绿=净流出
const ENTITY_COLORS: Record<string, string> = { main: '#4f46e5', retail: '#94a3b8' };
const SIZE_COLORS: Record<string, [string, string]> = {
  super: ['#818cf8', '#a5b4fc'],
  large: ['#6366f1', '#c7d2fe'],
};

export interface FlowSankeyV2Props {
  nodes: FlowPanoramaNode[];
  links: FlowPanoramaLink[];
  loading?: boolean;
  height?: number;
  selectedSector?: string | null;
  onSelectSector: (name: string) => void;
  onSelectStock: (node: FlowPanoramaNode) => void;
  /** flow=按当前净额着色；prob=按模型分着色（节点，边仍按净额） */
  colorMode?: 'flow' | 'prob';
  /** 模型分（0~1）：板块名 / 个股前缀码 → 分数 */
  probByKey?: Record<string, number | null>;
  probHi?: number;
  probLo?: number;
  /** 板块聚合概率的独立阈值（板块分布比个股窄） */
  sectorProbHi?: number;
  sectorProbLo?: number;
}

function nodeColor(
  n: FlowPanoramaNode,
  colorMode: 'flow' | 'prob',
  probByKey: Record<string, number | null> | undefined,
  probHi: number,
  probLo: number,
  sectorProbHi: number,
  sectorProbLo: number,
): string {
  if (n.kind === 'entity') return ENTITY_COLORS[n.key] || '#64748b';
  if (n.kind === 'size') {
    const pair = SIZE_COLORS[n.key];
    if (pair) return n.sign >= 0 ? pair[0] : pair[1];
  }
  if (colorMode === 'prob' && (n.kind === 'sector' || n.kind === 'stock')) {
    const p = probByKey?.[n.key];
    if (p === null || p === undefined) return '#cbd5e1';
    const hi = n.kind === 'sector' ? sectorProbHi : probHi;
    const lo = n.kind === 'sector' ? sectorProbLo : probLo;
    if (p >= hi) return INFLOW_COLOR;
    if (p <= lo) return OUTFLOW_COLOR;
    return '#94a3b8';
  }
  return n.sign >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR;
}

function nodeTooltipHtml(n: FlowPanoramaNode, prob?: number | null): string {
  const rows: string[] = [];
  rows.push(`<div style="font-weight:800;font-size:12px;margin-bottom:4px">${n.name}</div>`);
  if (prob !== undefined && prob !== null) {
    rows.push(`模型分：<b>${(prob * 100).toFixed(1)}</b>（未来强度排序，非真实胜率）`);
  }
  if (n.net_daily !== undefined && n.net_daily !== null) {
    const cur = (n.net_daily / 1e8).toFixed(2);
    const prev =
      n.prev_net_daily === undefined || n.prev_net_daily === null
        ? '—'
        : (n.prev_net_daily / 1e8).toFixed(2);
    rows.push(`日均净额：<b>${cur} 亿/日</b>（前一窗口 ${prev} 亿/日）`);
    if (n.accel !== undefined && n.accel !== null) {
      const a = n.accel / 1e8;
      rows.push(
        `速率：<b style="color:${a >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${a > 0 ? '+' : ''}${a.toFixed(2)} 亿/日</b>` +
          `（${a >= 0 ? '加速流入' : '加速流出'}）`,
      );
    }
  }
  if (n.kind === 'sector') {
    rows.push(`主力净额：<b style="color:${n.sign >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${fmtYi(n.net)}</b>`);
    rows.push(`合计净额（含散户）：${fmtYi(n.net_total ?? null)}`);
    rows.push(`净额比（净额/成交额）：${fmtRatio(n.net_ratio ?? null)}`);
    rows.push(`成分均涨幅：${fmtPct(n.pct_change ?? null)} · 成分 ${n.members ?? '—'} 只`);
    rows.push(
      n.streak !== null && n.streak !== undefined
        ? `连续净流入：${n.streak} 日${n.evidence ? '（已过证据闸门，悬停列表徽章看详情）' : ''}`
        : '连续净流入：无离线产物数据',
    );
    rows.push('<div style="margin-top:4px;color:#94a3b8">点击查看板块明细与成分</div>');
  } else if (n.kind === 'stock') {
    rows.push(`主力净额（窗口）：<b style="color:${n.sign >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${fmtYi(n.net)}</b>`);
    rows.push(`所属板块：${n.sector ?? '—'}`);
    rows.push(`板块内位置：第 ${n.inner_rank ?? '—'} / ${n.inner_total ?? '—'}（按主力净额）`);
    if (n.close !== null && n.close !== undefined) {
      rows.push(`最新收盘：${n.close} · 涨跌 ${fmtPct(n.pct_change ?? null)}`);
    }
    rows.push('<div style="margin-top:4px;color:#94a3b8">点击查看个股资金流详情（含板块归因）</div>');
  } else if (n.kind === 'size') {
    rows.push(`四档单型净额：<b style="color:${n.sign >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${fmtYi(n.net)}</b>`);
  } else {
    rows.push(`该主体窗口净额：<b style="color:${n.sign >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${fmtYi(n.net)}</b>`);
    rows.push('<span style="color:#94a3b8">主力=超大单+大单；散户=中单+小单</span>');
  }
  return `<div style="font-size:11px;line-height:1.7;max-width:260px">${rows.join('<br/>')}</div>`;
}

export const FlowSankeyV2: React.FC<FlowSankeyV2Props> = ({
  nodes,
  links,
  loading = false,
  height = 560,
  selectedSector,
  onSelectSector,
  onSelectStock,
  colorMode = 'flow',
  probByKey,
  probHi = 0.55,
  probLo = 0.45,
  sectorProbHi = 0.5,
  sectorProbLo = 0.42,
}) => {
  const chartRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);
  const clickRef = useRef({ onSelectSector, onSelectStock });
  clickRef.current = { onSelectSector, onSelectStock };

  const graphNodes = useMemo(
    () =>
      nodes.map((n) => {
        const highlighted = n.kind === 'sector' && selectedSector === n.name;
        return {
          name: n.name,
          value: Math.abs(n.net),
          depth: n.layer - 1,
          _node: n,
          itemStyle: {
            color: nodeColor(
              n,
              colorMode,
              probByKey,
              probHi,
              probLo,
              sectorProbHi,
              sectorProbLo,
            ),
            borderColor: highlighted ? '#f59e0b' : '#ffffff',
            borderWidth: highlighted ? 3 : 1,
            borderRadius: 3,
          },
          label: {
            show: n.kind !== 'stock',
            formatter: (() => {
              if (n.kind !== 'sector') return n.name;
              const streak = (n.streak ?? 0) >= 2 ? ` 连${n.streak}` : '';
              const arrow =
                n.accel === undefined || n.accel === null || n.accel === 0
                  ? ''
                  : n.accel > 0
                    ? ' ↑'
                    : ' ↓';
              return `${n.name}${streak}${arrow}`;
            })(),
            fontSize: 11,
            fontWeight: 'bold' as const,
            color: '#334155',
          },
        };
      }),
    [nodes, selectedSector, colorMode, probByKey, probHi, probLo, sectorProbHi, sectorProbLo],
  );

  const graphLinks = useMemo(
    () =>
      links.map((l) => ({
        source: l.source,
        target: l.target,
        value: l.value,
        _link: l,
        lineStyle: {
          color: l.sign >= 0 ? 'rgba(225,29,72,0.28)' : 'rgba(5,150,105,0.28)',
          curveness: 0.5,
        },
      })),
    [links],
  );

  useEffect(() => {
    if (!chartRef.current) return;
    if (!instanceRef.current) {
      instanceRef.current = echarts.init(chartRef.current);
      instanceRef.current.on('click', (params: any) => {
        if (params?.dataType !== 'node') return;
        const node: FlowPanoramaNode | undefined = params?.data?._node;
        if (!node) return;
        if (node.kind === 'sector') clickRef.current.onSelectSector(node.name);
        else if (node.kind === 'stock') clickRef.current.onSelectStock(node);
      });
    }
    const chart = instanceRef.current;
    if (graphNodes.length === 0 || graphLinks.length === 0) {
      chart.clear();
      return;
    }
    const option: echarts.EChartsOption = {
      tooltip: {
        trigger: 'item',
        triggerOn: 'mousemove',
        formatter: (params: any) => {
          const node: FlowPanoramaNode | undefined = params?.data?._node;
          const link: FlowPanoramaLink | undefined = params?.data?._link;
          if (node) {
            const key = (node as { _node?: FlowPanoramaNode })._node?.key;
            return nodeTooltipHtml(node, key ? probByKey?.[key] : undefined);
          }
          if (link) {
            return (
              `<div style="font-size:11px;line-height:1.6">${link.source} → ${link.target}<br/>` +
              `净额：<b style="color:${link.sign >= 0 ? INFLOW_COLOR : OUTFLOW_COLOR}">${fmtYi(link.net)}</b>` +
              `<br/><span style="color:#94a3b8">边宽=净额绝对值；红=净流入、绿=净流出</span></div>`
            );
          }
          return '';
        },
      },
      series: [
        {
          type: 'sankey',
          left: 8,
          right: 120,
          top: 12,
          bottom: 12,
          nodeWidth: 14,
          nodeGap: 7,
          draggable: false,
          emphasis: { focus: 'adjacency', label: { show: true, fontSize: 11 } },
          data: graphNodes,
          links: graphLinks,
          label: { color: '#334155' },
          lineStyle: { opacity: 0.55 },
        },
      ],
    };
    try {
      chart.setOption(option, { notMerge: true });
    } catch (e) {
      // 数据异常（如成环）兜底：清空而不是击穿页面
      console.error('FlowSankeyV2 render failed:', e);
      chart.clear();
    }
  }, [graphNodes, graphLinks]);

  useEffect(() => {
    const el = chartRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => instanceRef.current?.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      instanceRef.current?.dispose();
      instanceRef.current = null;
    };
  }, []);

  const empty = graphNodes.length === 0 || graphLinks.length === 0;

  return (
    <div className="relative w-full min-w-0" style={{ height }}>
      <div ref={chartRef} className="w-full" style={{ height }} />
      {loading && (
        <div className="absolute inset-0 flex items-center justify-center bg-white/60 rounded-2xl">
          <Spin size="small" />
        </div>
      )}
      {empty && !loading && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-400 bg-slate-50/60 rounded-2xl border border-dashed border-slate-200">
          暂无可渲染的资金流结构
        </div>
      )}
    </div>
  );
};
