/**
 * 因子池谱系图（ECharts force 布局）。
 *
 * 视觉语义（与池页其它面板同口径）：
 * - 节点大小 ∝ pool_score，颜色按新颖度（novelty）冷→暖；无面板的因子画成菱形
 *   并降低不透明度——「无面板」是真实状态（只参与公式/任务边），不假装算过；
 * - 边按 relation 区分：correlated_with（值级 |ρ|≥0.8）实线暖色、线宽 ∝ |weight|；
 *   similar_to（公式/语义）虚线紫色；task_round（同任务同轮骨架）点线灰色。
 *
 * 点击节点回调 factorId（页面侧做详情卡）；图表本身不持有业务状态。
 */

import React, { useMemo } from 'react';
import * as echarts from 'echarts';
import ReactECharts from 'echarts-for-react';
import type { PoolGraphEdge, PoolGraphNode } from '../services-v2/api';

interface FactorPoolGraphProps {
  nodes: PoolGraphNode[];
  edges: PoolGraphEdge[];
  height?: number;
  onSelectNode?: (factorId: string) => void;
}

const RELATION_META: Record<
  string,
  { label: string; color: string; type: 'solid' | 'dashed' | 'dotted' }
> = {
  correlated_with: { label: '值级相关', color: '#f59e0b', type: 'solid' },
  similar_to: { label: '公式/语义相似', color: '#8b5cf6', type: 'dashed' },
  task_round: { label: '同任务同轮', color: '#64748b', type: 'dotted' },
};

const RELATION_FALLBACK = { label: '其它', color: '#94a3b8', type: 'dashed' as const };

function relationMeta(relation: string) {
  return RELATION_META[relation] ?? { ...RELATION_FALLBACK, label: relation || '其它' };
}

function clamp01(value: number | null): number {
  if (value == null || !Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

function shortName(name: string): string {
  return name.length > 12 ? `${name.slice(0, 12)}…` : name;
}

export const FactorPoolGraph: React.FC<FactorPoolGraphProps> = ({
  nodes,
  edges,
  height = 560,
  onSelectNode,
}) => {
  const option = useMemo(() => {
    const categories = Object.entries(RELATION_META).map(([key, meta]) => ({
      name: meta.label,
      key,
    }));
    // 边用 relation 归类（图例可点掉某类关系）；颜色/线型逐边再覆写一遍，
    // 保证即使图例 category 配色被 ECharts 覆盖也保留语义。
    const catIndexByKey = new Map(categories.map((c, i) => [c.key, i]));
    const extraRelations = Array.from(
      new Set(edges.map((e) => e.relation).filter((r) => !catIndexByKey.has(r))),
    );
    for (const r of extraRelations) {
      catIndexByKey.set(r, categories.length);
      categories.push({ name: relationMeta(r).label, key: r });
    }

    return {
      tooltip: {
        confine: true,
        formatter: (params: any) => {
          if (params.dataType === 'edge') {
            const meta = relationMeta(params.data?.relation ?? '');
            return `${meta.label}<br/>${params.data?.sourceName ?? ''} — ${params.data?.targetName ?? ''}${
              params.data?.weight != null ? `<br/>权重 ${Number(params.data.weight).toFixed(3)}` : ''
            }`;
          }
          const d = params.data ?? {};
          const lines = [
            `<b>${d.name ?? ''}</b>`,
            `pool_score：${d.poolScore != null ? Number(d.poolScore).toFixed(4) : '—'}`,
            `新颖度：${d.novelty != null ? Number(d.novelty).toFixed(3) : '—'}`,
            d.icir != null ? `ICIR：${Number(d.icir).toFixed(3)}` : 'ICIR：—',
            `被检索：${d.timesRetrieved ?? 0} 次`,
            d.hasPanel ? '有面板' : '无面板（仅公式/任务边）',
          ];
          return lines.join('<br/>');
        },
      },
      legend: {
        top: 0,
        textStyle: { fontSize: 11, color: '#64748b' },
        data: categories.map((c) => c.name),
      },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          categories,
          force: { repulsion: 260, edgeLength: [70, 170], gravity: 0.08 },
          label: {
            show: true,
            position: 'right',
            fontSize: 10,
            color: '#475569',
            formatter: (p: any) => shortName(String(p.data?.name ?? '')),
          },
          emphasis: { focus: 'adjacency', label: { fontWeight: 'bold' } },
          data: nodes.map((n) => {
            const novelty = clamp01(n.novelty);
            return {
              id: n.factorId,
              name: n.factorName,
              symbol: n.hasPanel ? 'circle' : 'diamond',
              symbolSize: 10 + 22 * clamp01(n.poolScore),
              itemStyle: {
                color: n.novelty != null ? `rgba(124, 58, 237, ${0.35 + 0.6 * novelty})` : '#94a3b8',
                borderColor: '#ffffff',
                borderWidth: 1,
                opacity: n.hasPanel ? 1 : 0.66,
              },
              poolScore: n.poolScore,
              novelty: n.novelty,
              timesRetrieved: n.timesRetrieved,
              hasPanel: n.hasPanel,
              icir: n.icir,
            };
          }),
          edges: edges.map((e) => {
            const meta = relationMeta(e.relation);
            const width =
              e.weight != null ? 1 + 3 * clamp01(Math.abs(e.weight)) : 1.5;
            return {
              source: e.source,
              target: e.target,
              relation: e.relation,
              weight: e.weight,
              sourceName: nodes.find((n) => n.factorId === e.source)?.factorName ?? e.source,
              targetName: nodes.find((n) => n.factorId === e.target)?.factorName ?? e.target,
              category: catIndexByKey.get(e.relation) ?? 0,
              lineStyle: { color: meta.color, type: meta.type, width, opacity: 0.55, curveness: 0.08 },
            };
          }),
          lineStyle: { opacity: 0.5 },
        },
      ],
    };
  }, [nodes, edges]);

  if (nodes.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-xl bg-secondary/20 border border-dashed border-border/60 text-sm text-muted-foreground"
        style={{ height }}
      >
        池内还没有因子——回测完成后因子会自动登记，谱系边由池刷新生成。
      </div>
    );
  }

  return (
    <ReactECharts
      echarts={echarts}
      option={option}
      style={{ height, width: '100%' }}
      notMerge={true}
      lazyUpdate={true}
      onEvents={
        onSelectNode
          ? {
              click: (params: any) => {
                if (params?.dataType === 'node' && params?.data?.id) {
                  onSelectNode(String(params.data.id));
                }
              },
            }
          : undefined
      }
    />
  );
};

export default FactorPoolGraph;
