/**
 * 资金流全景「板块 / 个股方块图」纯数据模型。
 *
 * 方块面积 = |周期主力净额|；颜色优先用 T+N 概率模型（板块=聚合概率，个股=排序分），
 * 模型不可用时退回净流入红/净流出绿。纯函数，便于单测；ECharts 只负责布局与点击。
 */
import type {
  FlowPanoramaResponse,
  FlowProbabilityResponse,
  FlowSectorRow,
} from '../services/moneyFlowService';

export const BLOCK_INFLOW = '#e11d48';
export const BLOCK_OUTFLOW = '#059669';
export const BLOCK_NEUTRAL = '#94a3b8';

export type BlockKind = 'sector' | 'stock';

export interface FlowBlockDatum {
  name: string;
  value: number;
  kind: BlockKind;
  symbol?: string;
  sector?: string;
  net: number;
  pct: number | null;
  /** 展示分：板块=聚合概率；个股=未校准排序分（缺失为空） */
  score: number | null;
  /** 校准概率（个股用；板块与 score 相同） */
  prob: number | null;
  accel: number | null;
  children?: FlowBlockDatum[];
}

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const areaOf = (net: number): number => Math.max(Math.abs(net), 0.01);

function sectorProb(
  prob: FlowProbabilityResponse | null,
  name: string,
  h: number,
): { p: number | null; raw: number | null } {
  const rows = prob?.artifact?.latest?.sectors ?? [];
  const row = rows.find((r) => r.name === name);
  if (!row) return { p: null, raw: null };
  return {
    p: finite((row as unknown as Record<string, number | null>)[`p${h}`])
      ? ((row as unknown as Record<string, number>)[`p${h}`] as number)
      : null,
    raw: finite((row as unknown as Record<string, number | null>)[`raw${h}`])
      ? ((row as unknown as Record<string, number>)[`raw${h}`] as number)
      : null,
  };
}

export function buildSectorBlocks(
  sectors: FlowSectorRow[],
  prob: FlowProbabilityResponse | null,
  h: number,
): FlowBlockDatum[] {
  const probabilityAvailable = !!prob?.available && !!prob.artifact?.latest;
  return sectors
    .filter((s) => finite(s.net_main))
    .map((s) => {
      const p = probabilityAvailable ? sectorProb(prob, s.name, h) : { p: null, raw: null };
      return {
        name: s.name,
        kind: 'sector' as const,
        value: areaOf(s.net_main),
        net: s.net_main,
        pct: finite(s.pct_change) ? s.pct_change : null,
        score: p.raw ?? p.p,
        prob: p.p,
        accel: finite(s.accel) ? (s.accel as number) : null,
      };
    })
    .sort((a, b) => b.value - a.value);
}

export function buildStockBlocks(
  panorama: FlowPanoramaResponse | null,
  prob: FlowProbabilityResponse | null,
  h: number,
): FlowBlockDatum[] {
  const nodes = (panorama?.nodes ?? []).filter((n) => n.kind === 'stock');
  const probabilityAvailable = !!prob?.available && !!prob.artifact?.latest;
  const probMap = prob?.artifact?.latest?.stocks?.prob_map ?? {};
  const bySector = new Map<string, FlowBlockDatum>();
  for (const n of nodes) {
    if (!finite(n.net)) continue;
    const sectorName = n.sector || '其他';
    const raw = probabilityAvailable ? probMap[n.key]?.[String(h)] ?? null : null;
    const child: FlowBlockDatum = {
      name: n.name || n.key,
      kind: 'stock',
      symbol: n.key,
      sector: sectorName,
      value: areaOf(n.net),
      net: n.net,
      pct: finite(n.pct_change) ? (n.pct_change as number) : null,
      score: finite(raw) ? raw : null,
      prob: finite(raw) ? raw : null,
      accel: finite(n.accel) ? (n.accel as number) : null,
    };
    const parent = bySector.get(sectorName);
    if (parent) {
      parent.children?.push(child);
      parent.value += child.value;
    } else {
      bySector.set(sectorName, {
        name: sectorName,
        kind: 'sector',
        sector: sectorName,
        value: child.value,
        net: child.net,
        pct: child.pct,
        score: null,
        prob: null,
        accel: null,
        children: [child],
      });
    }
  }
  const out = [...bySector.values()];
  out.forEach((s) => s.children?.sort((a, b) => b.value - a.value));
  return out.sort((a, b) => b.value - a.value);
}

/** 颜色：概率可用时按 T+N 阈值（红=强、绿=弱）；否则按净额符号。 */
export function blockColor(
  d: Pick<FlowBlockDatum, 'kind' | 'score' | 'prob' | 'net'>,
  probabilityAvailable: boolean,
  hi: number,
  lo: number,
): string {
  if (probabilityAvailable) {
    const v = d.score ?? d.prob;
    if (finite(v)) return v >= hi ? BLOCK_INFLOW : v <= lo ? BLOCK_OUTFLOW : BLOCK_NEUTRAL;
  }
  return d.net >= 0 ? BLOCK_INFLOW : BLOCK_OUTFLOW;
}

/** 板块标签：名称 + 周期涨幅 + 概率/模型分（缺失不显示成 0）。 */
export function blockLabel(d: FlowBlockDatum, probabilityAvailable: boolean): string {
  const pct = finite(d.pct) ? `${d.pct >= 0 ? '+' : ''}${d.pct.toFixed(1)}%` : '—';
  if (probabilityAvailable && finite(d.score)) {
    const word = d.kind === 'sector' ? '概率' : '分';
    return `${d.name}\n${pct} · ${word}${(d.score * 100).toFixed(0)}`;
  }
  return `${d.name}\n${pct}`;
}
