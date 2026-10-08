import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Group } from '@visx/group';
import { GridRows, GridColumns } from '@visx/grid';
import { Area, LinePath } from '@visx/shape';
import { scaleLinear } from '@visx/scale';
import { curveMonotoneX } from '@visx/curve';
import { LinearGradient } from '@visx/gradient';
import { AxisBottom, AxisLeft, AxisRight } from '@visx/axis';
import { ParentSize } from '@visx/responsive';
import dayjs from 'dayjs';
import { EquityPoint } from '../api/client';
import { fmtMoney, fmtMoneySigned } from '../utils/format';
import { nearestIdxOfTime, stepAroundTime } from '../utils/equity';

export interface ChartLine {
  id: string;
  label: string;
  color: string;
  points: { t: number; v: number }[]; // v 为绝对净值
  /** 名义基准金额（如实盘分账 ¥10 万）→ hover 时换算金额盈亏 */
  notional?: number;
  /** 整线虚线（空仓/现金恒定的平线用，保留信息量但不抢视线） */
  dash?: boolean;
  /** 分段虚线：points 下标区间 [from,to]（含两端）画虚线，其余实线。
   *  用于「空仓那一段才虚线」：买入后自动回实线，不整线虚化。 */
  dashSegs?: [number, number][];
  /** 绝对金额线：不参与 pct 归一化，走右侧独立刻度（如分账合计 ¥30 万量级） */
  abs?: boolean;
  /** 断口：这些时间点起是"跨非交易日"后的第一个采样点，线在此断开不与前点相连
   *  （周末/法定节假日不画线；数据点仍在，tooltip 可见） */
  gapStarts?: Set<number>;
  /** 成交标记：时间就近吸附到采样点，画买入 ▲（线下）/ 卖出 ▼（线上）小三角。
   *  用途：空仓段虚线转回实线的界点在全览（分钟级 900 点）下只有几像素宽，
   *  标记让「今天买回来了没有、几点买的」不放大也看得见（2026-09-11）。 */
  fills?: { t: number; side?: string; kind?: 'adjust'; note?: string }[];
}

/** 悬停补充信息（可选）：当时持仓 + 附近成交（时序事实，随鼠标滑动查看） */
export interface HoverEvent {
  ts: string;
  agent?: string | null;
  side?: string;
  code?: string;
  volume?: number;
  price?: number | null;
}

export interface HoldingSpan {
  agent: string;
  code: string;
  vol: number;
  from: number; // 毫秒（含）
  to: number; // 毫秒（含）
}

export interface BenchLine {  id: string;
  label: string;
  color: string;
  points: { t: number; v: number }[]; // v 为指数点位
  /** 断口（跨非交易日后的第一个点），同 ChartLine.gapStarts */
  gapStarts?: Set<number>;
}

const margin = { top: 16, right: 16, bottom: 34, left: 62 };

/** 悬停吸附点：命中哪条线、哪个采样点、线上像素坐标 */
interface HoverSnap {
  x: number;
  y: number;
  label: string;
  id: string;
  v: number;
  t: number;
}

/** 平移/缩放视图：offsetPx = 窗口相对最左端的像素偏移；scale = 1 全览 */
interface View { offsetPx: number; scale: number }

/** 展示数据：归一化/裁剪后的线集合（EquityChart 内 useMemo 产出，引用稳定） */
interface DisplayLines { lines: ChartLine[]; bench: BenchLine | null }

/** 断轴序号窗口内点数 → 刻度文本精度 */
type TickFmt = 'HH:mm' | 'MM-DD HH:mm' | 'MM-DD' | 'YYYY-MM';

type NumScale = ReturnType<typeof scaleLinear<number>>;

const isSameView = (a: View, b: View) => a.offsetPx === b.offsetPx && a.scale === b.scale;

/** 时间戳 → 轴刻度/tooltip 文本（实盘数据为 +08:00，本机可能非北京时区 → 统一按 +8h 显示） */
const tsToText = (t: number, tickFmt: TickFmt): string => {
  const d = new Date(t + 8 * 3600000);
  if (tickFmt === 'MM-DD HH:mm') {
    return `${d.toISOString().slice(5, 10)} ${d.toISOString().slice(11, 16)}`;
  }
  if (tickFmt === 'HH:mm') return d.toISOString().slice(11, 16);
  if (tickFmt === 'YYYY-MM') return d.toISOString().slice(0, 7);
  return dayjs(t).format('MM-DD');
};

/** tooltip 用全精度：轴刻度跨年时省略年份（YYYY-MM），浮层不能省 */
const tsToFull = (t: number): string => new Date(t + 8 * 3600000).toISOString().slice(0, 10);

/** 静态图表层（memo）：网格/三向轴/渐变 defs/面积垫层/折线/末端标签。
 *  悬停只把 hoverId 传进来（换线高亮才需要重建整层）；
 *  同一条线上的 x 轴滑动只走 HoverTip 浮层，不触碰这里。 */
const ChartStatic = memo(function ChartStatic({
  iw, ih, display, allTimes, tToIdx,
  xScale, yScale, rightScale, yOf, tickFmt,
  winStartIdx, winEndIdx, focus, hoverId, mode, currency,
}: {
  iw: number;
  ih: number;
  display: DisplayLines;
  allTimes: number[];
  tToIdx: Map<number, number>;
  xScale: NumScale;
  yScale: NumScale;
  rightScale: NumScale | null;
  /** 线取 y：abs 线走右刻度，其余走主刻度 */
  yOf: (l: { abs?: boolean }, v: number) => number;
  tickFmt: TickFmt;
  /** 当前可见序号窗口 [winStartIdx, winEndIdx]（面积垫层基线取窗口内最低值） */
  winStartIdx: number;
  winEndIdx: number;
  focus: string | null;
  /** 悬停中的线 id（null = 无悬停）；同线不同 x 不触发本层重建 */
  hoverId: string | null;
  mode: 'pct' | 'dollar';
  currency: string;
}) {
  const idxOf = (t: number) => tToIdx.get(t) ?? 0;
  // 序号 → 真实时间（轴刻度与 invert 反查都用它）
  const tsOfIdx = (v: number) => {
    const t = allTimes[Math.round(Number(v))];
    return t != null ? tsToText(t, tickFmt) : '';
  };
  // 刻度文本去重：连续同文刻度只标首个。启动早期实盘只有同分钟若干点
  // （20s 采样 00:00:00–00:00:40）时，HH:mm 下 10 个刻度全是 00:00 → 一排同文噪声轴。
  // 省略重复标签后轴显示单个 00:00 + 刻度线，数据铺开后自动恢复（2026-09-07 修复）。
  let lastTickLabel: string | null = null;
  return (
    <>
      <GridRows scale={yScale} width={iw} stroke="rgba(0,0,0,0.06)" strokeDasharray="3 4" />
      <GridColumns scale={xScale} height={ih} stroke="rgba(0,0,0,0.06)" strokeDasharray="3 4" />
      <AxisBottom
        top={ih}
        scale={xScale}
        numTicks={Math.min(10, Math.floor(iw / 90))}
        stroke="rgba(0,0,0,0.2)"
        tickStroke="rgba(0,0,0,0.2)"
        tickLabelProps={() => ({
          fill: '#666', fontSize: 10, textAnchor: 'middle', dy: 8,
          fontFamily: "'Courier New', monospace",
        })}
        tickFormat={(v) => {
          const text = tsOfIdx(Number(v));
          if (text === '') return text;
          if (text === lastTickLabel) return ''; // 连续同文(同分钟多点/隔日同时刻) → 省略
          lastTickLabel = text;
          return text;
        }}
      />
      <AxisLeft
        scale={yScale}
        numTicks={6}
        stroke="rgba(0,0,0,0.2)"
        tickStroke="rgba(0,0,0,0.2)"
        tickLabelProps={() => ({
          fill: '#666', fontSize: 10, textAnchor: 'end', dx: -6,
          fontFamily: "'Courier New', monospace",
        })}
        tickFormat={(v) =>
          mode === 'pct'
            ? `${Number(v).toFixed(1)}%`
            : fmtMoney(Number(v), currency, 0)
        }
      />
      {rightScale && (
        <AxisRight
          scale={rightScale}
          left={margin.left + iw}
          numTicks={5}
          stroke="rgba(0,0,0,0.2)"
          tickStroke="rgba(0,0,0,0.2)"
          tickLabelProps={() => ({
            fill: '#888', fontSize: 9, textAnchor: 'start', dx: 6,
            fontFamily: "'Courier New', monospace",
          })}
          tickFormat={(v) => {
            const n = Number(v);
            return n >= 10000 ? `${(n / 10000).toFixed(1)}万` : `${Math.round(n)}`;
          }}
        />
      )}
      <defs>
        {/* 每线渐变垫层: 线色向下渐隐(柔化视觉); 悬停/聚焦时线本身加光晕 */}
        {display.lines.map((l, i) => (
          <LinearGradient key={`g-${l.id}`} id={`grad-${i}`}
            from={l.color} to={l.color} fromOpacity={0.25} toOpacity={0} vertical />
        ))}
        {display.bench && (
          <LinearGradient id="grad-bench"
            from={display.bench.color} to={display.bench.color} fromOpacity={0.16} toOpacity={0} vertical />
        )}
        <filter id="line-glow" x="-30%" y="-30%" width="160%" height="160%">
          <feDropShadow dx="0" dy="1.5" stdDeviation="2.5" floodColor="#000" floodOpacity="0.28" />
        </filter>
        <clipPath id="chart-clip">
          <rect x={0} y={0} width={iw} height={ih} />
        </clipPath>
      </defs>

      <Group clipPath="url(#chart-clip)">
        {/* 基准线: 聚焦某模型时退到 0.5 淡度, 其余默认 0.9 */}
        {display.bench && (() => {
          const hl = hoverId === display.bench.id;
          const active = !focus || hl;
          const baseV = display.bench.points.reduce((m, p) => Math.min(m, p.v), Infinity);
          return (
            <Group opacity={hoverId != null ? (hl ? 1 : 0.35) : focus ? 0.55 : 0.9}>
              <Area
                data={display.bench.points}
                defined={(p: { t: number; v: number }) =>
                  !display.bench?.gapStarts?.has(p.t)}
                x={(p: { t: number; v: number }) => xScale(idxOf(p.t)) ?? 0}
                y0={() => yScale(baseV) ?? 0}
                y1={(p: { t: number; v: number }) => yScale(p.v) ?? 0}
                fill="url(#grad-bench)"
                curve={curveMonotoneX}
              />
              <LinePath
                data={display.bench.points}
                defined={(p: { t: number; v: number }) =>
                  !display.bench?.gapStarts?.has(p.t)}
                x={(p) => xScale(idxOf(p.t)) ?? 0}
                y={(p) => yScale(p.v) ?? 0}
                stroke={display.bench.color}
                strokeWidth={hl ? 2.4 : 1.2}
                strokeDasharray={active ? '5 4' : '3 4'}
                filter={hl ? 'url(#line-glow)' : undefined}
                curve={curveMonotoneX}
              />
            </Group>
          );
        })()}

        {/* 模型线: 平滑曲线 + 渐变垫层; 聚焦时未选模型虚线淡化, hover 时最近线加粗光晕 */}
        {display.lines.map((l, i) => {
          const focusActive = !focus || l.id === focus;          // 图例选中判定
          const hl = hoverId === l.id;                            // 悬停高亮判定
          // 面积垫层基线 = 当前可见窗口内的最低净值（窗口外点不算，否则放大后垫层贴底）
          const winPts = l.points.filter((p) => {
            const idx = idxOf(p.t);
            return idx >= winStartIdx && idx <= winEndIdx;
          });
          const baseV = winPts.length ? Math.min(...winPts.map((p) => p.v)) : l.points[0]?.v ?? 0;
          return (
            <Group key={l.id} opacity={hoverId != null ? (hl ? 1 : 0.35) : focusActive ? 1 : 0.45}>
              <Area
                data={l.points}
                defined={(p: { t: number; v: number }) => !l.gapStarts?.has(p.t)}
                x={(p: { t: number; v: number }) => xScale(idxOf(p.t)) ?? 0}
                y0={() => yOf(l, baseV) ?? 0}
                y1={(p: { t: number; v: number }) => yOf(l, p.v) ?? 0}
                fill={`url(#grad-${i})`}
                curve={curveMonotoneX}
              />
              {/* 折线：无分段 → 整条一条 path；有 dashSegs → 按实/虚窗口逐段画，
                  空仓段 4 4 虚线，其余保持实线（或未聚焦时的 6 5 淡线） */}
              {((l.dashSegs && l.dashSegs.length) ? dashWindows(l.points.length, l.dashSegs) : [[0, l.points.length]])
                .map(([s, e], k) => {
                  const segPts = l.points.slice(s, e);
                  if (!segPts.length) return null;
                  const dashed = !!l.dashSegs?.some(([a, b]) => s >= a && s <= b);
                  return (
                    <LinePath
                      key={`${l.id}-${k}`}
                      data={segPts}
                      defined={(p: { t: number; v: number }) => !l.gapStarts?.has(p.t)}
                      x={(p) => xScale(idxOf(p.t)) ?? 0}
                      y={(p) => yOf(l, p.v) ?? 0}
                      stroke={l.color}
                      strokeWidth={hl ? 2.8 : focusActive ? 2 : 1.3}
                      strokeDasharray={dashed ? '4 4' : focusActive ? undefined : '6 5'}
                      filter={hl ? 'url(#line-glow)' : undefined}
                      curve={curveMonotoneX}
                    />
                  );
                })}
            </Group>
          );
        })}

        {/* 成交标记：买入 ▲（点下方，仿通达信 B 位）/ 卖出 ▼（点上方）。
            只画窗口内的；聚焦/悬停淡化与对应线一致。 */}
        {display.lines.map((l) => {
          if (!l.fills?.length || !l.points.length) return null;
          const focusActive = !focus || l.id === focus;
          const hl = hoverId === l.id;
          // 线自身的时间跨度：窗口化（如「近5日」）后，早于窗口的成交不能吸附到
          // 首点画成假标记（会挤在左边缘 x=0）——直接不画。
          const t0 = l.points[0].t;
          const t1 = l.points[l.points.length - 1].t;
          return (
            <Group key={`fills-${l.id}`}
              opacity={hoverId != null ? (hl ? 1 : 0.35) : focusActive ? 1 : 0.45}>
              {l.fills.map((f, k) => {
                if (f.t < t0 || f.t > t1) return null;
                const idx = nearestIdxOfTime(l.points, f.t);
                if (idx < 0) return null;
                // x 轴是「采样点序号」的**多线并集**空间（allTimes）：折线每点都经
                // idxOf(p.t) 换算，标记也必须走同一换算——nearestIdxOfTime 给的是
                // **本线**序号，直接 xScale(idx) 会把近端成交画到早得多的位置
                // （2026-10-07 实测 9/29 的成交标记横移 ~260px，看起来"不在线上"）。
                const uIdx = idxOf(l.points[idx].t);
                if (uIdx < winStartIdx || uIdx > winEndIdx) return null;
                const x = xScale(uIdx) ?? 0;
                const y = yOf(l, l.points[idx].v) ?? 0;
                if (f.kind === 'adjust') {
                  // 对账（fill_adjust）≠ 成交：菱形 + 悬停说明。资产归属在原时刻
                  // 记在别人名下（如 09-08 pro 误卖 flash 的 688183），这里画的是
                  // 「几点几分把账归回来」，不是一笔新交易。
                  const r = 5;
                  // data-*：台阶两端的**图内点位**（pct 模式已归一化到 100 起点，
                  // 不是人民币）+ 像素高差，供 DOM 探针核对「为什么这条标了/没标」
                  const { pre, post } = stepAroundTime(l.points, f.t);
                  const vPre = pre >= 0 ? l.points[pre].v : NaN;
                  const vPost = post >= 0 ? l.points[post].v : NaN;
                  const yPre = yOf(l, vPre);
                  const yPost = yOf(l, vPost);
                  const diamond = (
                    <path
                      key={`${l.id}-fill-${k}`}
                      className="eq-fill-mark"
                      data-side="adjust"
                      data-pre={vPre}
                      data-post={vPost}
                      data-step-px={yPre != null && yPost != null ? Math.abs(yPre - yPost).toFixed(1) : ''}
                      d={`M ${x} ${y - r} L ${x + r} ${y} L ${x} ${y + r} L ${x - r} ${y} Z`}
                      fill="#f59e0b"
                      stroke="#fff"
                      strokeWidth={1}
                    >
                      <title>{f.note ?? '对账'}</title>
                    </path>
                  );
                  // 台阶标注：图是「当时账本」的分钟快照、不回溯重写，所以对账在
                  // 线上是一步跳跃——量出前后两点之差标上去，免得看着像暴跌。
                  // 前后任一侧不在可见窗口内就不标（会指向窗外几何）。
                  // 同主标记：pre/post 是本线序号，先换算到联合轴序号再比可见窗口
                  const uPre = pre >= 0 ? idxOf(l.points[pre].t) : -1;
                  const uPost = post >= 0 ? idxOf(l.points[post].t) : -1;
                  if (uPre < winStartIdx || uPost < 0 || uPost > winEndIdx) return diamond;
                  const delta = vPost - vPre;
                  // 台阶太矮（<6px，肉眼与分钟级抖动无异）不标：文字会盖住菱形，
                  // 噪声大于信息（09-01 300308 清出行净值本就平坦 → 不标）。
                  if (!Number.isFinite(yPre) || !Number.isFinite(yPost) || Math.abs(yPre - yPost) < 6) {
                    return diamond;
                  }
                  // 金额换算：pct 模式图内点位已归一化到 100 起点（display 里做的），
                  // 直接用会按「点」报数（实测 ¥14,026 的台阶被写成 ¥14）——沿用 hover
                  // 的同一口径 Δ/基准 × notional（分账 ¥10 万）。dollar/abs 线本就是金额。
                  const baseV = l.points[0]?.v || 0;
                  const stepMoney =
                    mode === 'dollar' || l.abs
                      ? delta
                      : l.notional && baseV
                        ? (delta / baseV) * l.notional
                        : null;
                  // 右侧留白不够就翻到左锚：金额标签（「对账 ≈-¥14,026」两枚全角字 +
                  // 数字）约 80px，70px 会把它截在 chart-clip 边上（尾部断字）。
                  const atRight = x + 84 > iw;
                  return (
                    <g key={`${l.id}-fill-${k}`}>
                      {diamond}
                      <line
                        className="eq-adjust-link"
                        x1={x}
                        x2={x}
                        y1={yPre}
                        y2={yPost}
                        stroke="#f59e0b"
                        strokeWidth={1}
                        strokeDasharray="3 3"
                        opacity={0.85}
                      />
                      <text
                        x={atRight ? x - 8 : x + 8}
                        y={(yPre + yPost) / 2 + 3}
                        textAnchor={atRight ? 'end' : 'start'}
                        fontSize={10}
                        fill="#b45309"
                        stroke="#fff"
                        strokeWidth={3}
                        paintOrder="stroke"
                        fontFamily="'Courier New', monospace"
                      >
                        {stepMoney != null
                          ? `对账 ≈${fmtMoneySigned(stepMoney, currency)}`
                          : `对账 ≈${delta >= 0 ? '+' : ''}${delta.toFixed(2)}%`}
                      </text>
                    </g>
                  );
                }
                const isBuy = String(f.side).toLowerCase() === 'buy';
                const d = isBuy
                  ? `M ${x} ${y + 4} L ${x - 4.5} ${y + 11} L ${x + 4.5} ${y + 11} Z`
                  : `M ${x} ${y - 4} L ${x - 4.5} ${y - 11} L ${x + 4.5} ${y - 11} Z`;
                return (
                  <path
                    key={`${l.id}-fill-${k}`}
                    className="eq-fill-mark"
                    data-side={isBuy ? 'buy' : 'sell'}
                    d={d}
                    fill={isBuy ? '#e0483e' : '#12b886'}
                    stroke="#fff"
                    strokeWidth={1}
                  />
                );
              })}
            </Group>
          );
        })}
      </Group>
      {display.lines.map((l) => {
        const last = l.points[l.points.length - 1];
        if (!last) return null;
        const x = xScale(idxOf(last.t)) ?? 0;
        const y = yOf(l, last.v) ?? 0;
        // 末端标签: 圆点右侧显示 模型名+值; 靠右(>60%宽)时放左侧右对齐, 防溢出
        const onRight = x > iw * 0.6;
        const anchor = onRight ? 'end' : 'start';
        const lx = onRight ? x - 10 : x + 10;
        const focusActive = !focus || l.id === focus;
        return (
          <Group key={`end-${l.id}`} opacity={hoverId != null ? (hoverId === l.id ? 1 : 0.3) : focusActive ? 1 : 0.45}>
            {/* 末端标签：实心圆点，颜色=模型线色（用户口径） */}
            <circle cx={x} cy={y} r={5.5} fill={l.color} stroke="#fff" strokeWidth={2} />
            <text x={lx} y={y - 8} fill={l.color} fontSize={10} fontWeight={700} textAnchor={anchor}
              fontFamily="'Courier New', monospace">
              {l.label}
            </text>
            <text x={lx} y={y + 7} fontSize={10} textAnchor={anchor}
              fontFamily="'Courier New', monospace"
              fill={l.abs ? (last.v >= 0 ? '#c0392b' : '#27ae60') : l.color}>
              {mode === 'pct' && !l.abs
                ? `${Number(last.v).toFixed(1)}%`
                : fmtMoney(last.v, currency, 0)}
            </text>
          </Group>
        );
      })}
    </>
  );
});

/** 悬停浮层（每次悬停滑动只重建这里 ~20 个节点：十字线 + 单框 tooltip）。
 *  不再把 hover 状态喂给静态图表层 → 图表层 memo 命中，900点×N线 path 原地不动。 */
function HoverTip({
  hover, display, iw, ih, tickFmt, mode, currency,
  events, holdings, names, priceMap,
}: {
  hover: HoverSnap;
  display: DisplayLines;
  iw: number;
  ih: number;
  tickFmt: TickFmt;
  mode: 'pct' | 'dollar';
  currency: string;
  events?: HoverEvent[];
  holdings?: HoldingSpan[];
  names?: Record<string, string>;
  priceMap?: Record<string, number>;
}) {
  const fmtTs = (t: number) => (tickFmt === 'YYYY-MM' ? tsToFull(t) : tsToText(t, tickFmt));
  const hl = display.lines.find((l) => l.id === hover.id);
  const base = hl && hl.points.length ? hl.points[0].v : null;
  const chg = base ? ((hover.v - base) / base) * 100 : null;
  // 金额盈亏: abs 线 = 绝对增减额；其余按收益率 × 名义基准换算（分账 ¥10 万）
  const pnlAmt = base
    ? hl?.abs
      ? hover.v - base
      : hl?.notional
        ? ((hover.v / base) - 1) * hl.notional
        : null
    : null;
  const pnlColor = (v: number | null) => (v == null || v >= 0 ? '#c0392b' : '#27ae60');
  const tx = Math.min(hover.x + 6, iw - 212);
  const ty = Math.max(hover.y - 58, 2);
  const isBench = display.bench?.id === hover.id;
  const agentName =
    isBench || hover.label === '总账户' || hover.label === '分账合计'
      ? null
      : hover.id.replace(/^live-/, '');
  // 单框时序信息：跟随悬停模型，只列该 agent 的持仓（名称×数量×金额）与附近成交
  const t = hover.t;
  const W = 3 * 60000;
  const heldRows = agentName
    ? (holdings ?? [])
        .filter((h) => h.agent === agentName && t >= h.from && t <= h.to)
        .slice(0, 6)
        .map((h) => {
          const px = priceMap?.[h.code];
          return {
            key: `h-${h.code}`,
            text: `${names?.[h.code] ?? '（无名称）'} ×${h.vol}股  ${px != null ? fmtMoney(px * h.vol, currency, 0) : '金额—'}`,
          };
        })
    : [];
  const evtRows = (events ?? [])
    .filter((e) => {
      if (agentName && e.agent !== agentName) return false;
      const ms = new Date(e.ts).getTime();
      return Number.isFinite(ms) && Math.abs(ms - t) <= W;
    })
    .slice(0, 4)
    .map((e) => {
      const isBuy = String(e.side).toUpperCase() === 'BUY';
      return {
        key: `e-${e.ts}-${e.code}`,
        text: `${e.ts.slice(5, 16)} ${isBuy ? '买入' : '卖出'} ${names?.[e.code ?? ''] ?? ''}${e.volume ?? ''}${e.price != null ? `@${e.price}` : ''}`,
        color: isBuy ? '#e0483e' : '#12b886',
      };
    });
  const extraRows = [...heldRows, ...evtRows].filter(
    (r, i, arr) => arr.findIndex((x) => x.key === r.key) === i,
  );
  const extraH = extraRows.length ? 40 + extraRows.length * 14 : 0;
  return (
    <Group>
      <line x1={hover.x} x2={hover.x} y1={0} y2={ih} stroke="rgba(0,0,0,0.25)" strokeDasharray="2 3" />
      <rect x={tx} y={ty} width={236} height={58 + extraH} fill="#fff" stroke="#000" strokeWidth={1} rx={4} />
      <text x={tx + 6} y={ty + 13} fill="#000" fontSize={10} fontWeight={700}
        fontFamily="'Courier New', monospace">
        {hover.label} · {fmtTs(hover.t)}
      </text>
      <text x={tx + 6} y={ty + 27} fill="#000" fontSize={10}
        fontFamily="'Courier New', monospace">
        {hl?.abs || mode === 'dollar'
          ? fmtMoney(hover.v, currency, 0)
          : `${Number(hover.v).toFixed(1)}%`}
        {chg != null && (
          <tspan fill={pnlColor(chg)}>
            {'  '}{chg >= 0 ? '+' : ''}{chg.toFixed(2)}%
          </tspan>
        )}
      </text>
      {pnlAmt != null && (
        <text x={tx + 6} y={ty + 40} fill={pnlColor(pnlAmt)} fontSize={10} fontWeight={700}
          fontFamily="'Courier New', monospace">
          盈亏 {pnlAmt >= 0 ? '+' : ''}{fmtMoney(pnlAmt, currency, 0)}
        </text>
      )}
      <text x={tx + 6} y={ty + 53} fill="#666" fontSize={9}
        fontFamily="'Courier New', monospace">
        {isBench ? '基准指数' : hover.label === '总账户' ? '通达信桥实时总资产' : hover.label === '分账合计' ? '分账合计净值（3 agent）' : hover.id.startsWith('real-ledger-') ? '实盘账户日终总资产 · 涨跌自首个记录日' : '虚拟净值（¥10万起步）'}
      </text>
      {extraH > 0 && (
        <g>
          <text x={tx + 8} y={ty + 68} fill="#444" fontSize={9} fontWeight={700}
            fontFamily="'Courier New', monospace">
            {agentName ? `${agentName.replace('deepseek-v4-', '')} 当时持仓 / 成交` : '附近成交'}
          </text>
          <line x1={tx + 8} x2={tx + 228} y1={ty + 77} y2={ty + 77}
            stroke="#eee" strokeWidth={1} />
          {extraRows.map((r, i) => (
            <text key={r.key} x={tx + 8} y={ty + 92 + 14 * i}
              fontSize={9} fontFamily="'Courier New', monospace"
              fill={(r as { color?: string }).color ?? '#555'}>
              {r.text}
            </text>
          ))}
        </g>
      )}
    </Group>
  );
}

interface ChartInnerProps {
  width: number;
  height: number;
  display: DisplayLines;
  allTimes: number[];
  tToIdx: Map<number, number>;
  domain: { idxMin: number; idxMax: number };
  focus: string | null;
  mode: 'pct' | 'dollar';
  currency: string;
  events?: HoverEvent[];
  holdings?: HoldingSpan[];
  names?: Record<string, string>;
  priceMap?: Record<string, number>;
}

/** 绘图区组件：持有 hover/view/drag 交互状态，产出 svg。
 *  平铺在 EquityChart 下并用 memo 包裹——数据未变时父级重渲不碰图表。
 *  内部：几何/刻度都收敛进 geo useMemo（平移缩放/数据变化才变），
 *  静态层 ChartStatic 只认 geo 引用 + hoverId；悬停滑动只更新 HoverTip。 */
const ChartInner = memo(function ChartInner({
  width, height, display, allTimes, tToIdx, domain,
  focus, mode, currency, events, holdings, names, priceMap,
}: ChartInnerProps) {
  const iw = Math.max(width - margin.left - margin.right, 10);
  const ih = Math.max(height - margin.top - margin.bottom, 10);

  // 悬停吸附点。hover 键 = id|t，同点重复 pointermove 直接跳过 setState
  //（拖动/滚轮后旧像素失效，见 commitView 清空）
  const [hover, setHover] = useState<HoverSnap | null>(null);
  const hoverKeyRef = useRef<string | null>(null);
  const applyHover = useCallback((next: HoverSnap | null) => {
    const key = next ? `${next.id}|${next.t}` : null;
    if (key === hoverKeyRef.current) return; // 悬停同点: 不触发任何重渲
    hoverKeyRef.current = key;
    setHover(next);
  }, []);

  // 时间轴交互: 拖拽平移 · 滚轮缩放 · 双击复位(scale=1 全览, offsetPx=窗口相对左端偏移)
  const [view, setView] = useState<View>({ offsetPx: 0, scale: 1 });
  const pendingViewRef = useRef<View>(view);    // 事件流内几何计算用的最新值
  const committedViewRef = useRef<View>(view);  // 已提交给 React 渲染的值
  const rafRef = useRef<number | null>(null);
  // rAF 合并提交：滚轮/触屏拖动是高频事件流（触屏 pinch 一帧可多次 wheel），
  // 若逐事件 setView → 每帧多次全量重建 SVG（900点×N线+轴线），页面卡死。
  // 只保留每动画帧最后一次 view，其余丢弃（2026-09-07 触屏卡死复盘）。
  const commitView = useCallback((next: View) => {
    pendingViewRef.current = next; // 同步镜像：连续 wheel 之间几何计算用最新值
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const p = pendingViewRef.current;
      if (isSameView(p, committedViewRef.current)) return; // 已在边界的重复提交: 跳过
      committedViewRef.current = p;
      setView(p);
      applyHover(null); // 平移/缩放后旧吸附点像素坐标失效 → 清空，等下一次 pointermove 重新吸附
    });
  }, [applyHover]);
  useEffect(() => () => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
  }, []);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ startX: number; startOffset: number } | null>(null);
  // 全览时每序号像素宽（idxSpan 仅随数据变化，拖拽/缩放期间恒定）
  const idxSpan = domain.idxMax - domain.idxMin || 1;

  // 手势防护: React 合成 onWheel/onTouchMove 在 root 上是 passive, preventDefault 无效 →
  // 触摸板横向滑动会被浏览器当成"前进/后退"手势触发整页导航(看起来像刷新)。
  // 用原生 non-passive 监听在元素上拦截, 图表内的滑动/拖拽绝不落到页面。
  // （原生监听已 preventDefault → React onWheel 里无需再调；那里是 passive 无效果。）
  const gestureGuard = useCallback((el: SVGRectElement | null) => {
    if (!el) return;
    const prevent = (e: Event) => e.preventDefault();
    el.addEventListener('wheel', prevent, { passive: false });
    el.addEventListener('touchstart', prevent, { passive: false });
    el.addEventListener('touchmove', prevent, { passive: false });
  }, []);

  // 几何与刻度：窗口起点/终点、三轴 scale、右侧金额刻度、取数 yOf、刻度精度。
  // 平移缩放/尺寸/数据任一变化才重算（hover 状态不在此列）。
  const geo = useMemo(() => {
    // 序号轴窗口: scale=1 全览; 放大后 offsetPx 平移(0=最左, max=最右, 渲染时防越界 clamp)
    const idxSpan = domain.idxMax - domain.idxMin || 1;
    const pxPerIdx = (iw / idxSpan) * view.scale;
    const maxOffset = Math.max(0, iw * (view.scale - 1));
    const offsetPx = Math.min(view.offsetPx, maxOffset);
    const winStartIdx = domain.idxMin + offsetPx / pxPerIdx;
    const winEndIdx = Math.min(winStartIdx + idxSpan / view.scale, domain.idxMax);
    const xScale = scaleLinear({ domain: [winStartIdx, winEndIdx], range: [0, iw] });

    const idxOf = (t: number) => tToIdx.get(t) ?? 0;
    // Y 域: 当前窗口内数据自适应(+7% 边距; 窗口内无点回退全范围)
    // abs 线（分账合计金额）不参与主刻度域——量级不同，右侧单独刻度
    const yLines = [...display.lines.filter((l) => !l.abs), ...(display.bench ? [display.bench] : [])];
    let vMin = Infinity, vMax = -Infinity;
    for (const l of yLines) {
      for (const p of l.points) {
        const idx = idxOf(p.t);
        if (idx >= winStartIdx && idx <= winEndIdx) {
          vMin = Math.min(vMin, p.v);
          vMax = Math.max(vMax, p.v);
        }
      }
    }
    if (!Number.isFinite(vMin)) {
      for (const l of yLines) {
        for (const p of l.points) {
          vMin = Math.min(vMin, p.v);
          vMax = Math.max(vMax, p.v);
        }
      }
    }
    if (!Number.isFinite(vMin) || vMin === vMax) { vMin = 0; vMax = 1; }
    if (mode === 'dollar' && vMin < 0) vMin = 0; // 绝对净值不画负区
    const padY = (vMax - vMin) * 0.07 || 1;
    const yScale = scaleLinear({ domain: [vMin - padY, vMax + padY], range: [ih, 0] });
    // 右侧独立刻度（绝对金额线，如分账合计 ¥30 万量级——与 pct 线不同轴）
    const absLines = display.lines.filter((l) => l.abs);
    let rMin = Infinity, rMax = -Infinity;
    for (const l of absLines) {
      for (const p of l.points) {
        const idx = idxOf(p.t);
        if (idx >= winStartIdx && idx <= winEndIdx) {
          rMin = Math.min(rMin, p.v);
          rMax = Math.max(rMax, p.v);
        }
      }
    }
    if (!Number.isFinite(rMin)) { rMin = 0; rMax = 1; }
    if (rMin === rMax) { rMin -= 1; rMax += 1; }
    const rPad = (rMax - rMin) * 0.07 || 1;
    const rightScale = absLines.length
      ? scaleLinear({ domain: [rMin - rPad, rMax + rPad], range: [ih, 0] })
      : null;
    const yOf = (l: { abs?: boolean }, v: number) =>
      l.abs && rightScale ? rightScale(v) : yScale(v);

    // 刻度格式: 先按「相邻采样平均墙钟间隔」识别数据粒度——
    // 日线级(≥12h/点)只看日期; 低频(≥90min/点)必须带日期, 隔日同时刻才不重影;
    // 分钟级沿用点数启发式(断轴后点数≈交易分钟): ≤240 点≈一个交易时段→HH:mm;
    // ≤1920 点≈8 交易日→MM-DD HH:mm; 更久→MM-DD。
    // （纯点数启发式会把 30 天日线(30 点@15:00)套成 HH:mm → 整轴同时刻重复, 2026-09-07 修复）
    const winPoints = Math.round(winEndIdx - winStartIdx) + 1;
    const iLo = Math.min(allTimes.length - 1, Math.max(0, Math.round(winStartIdx)));
    const iHi = Math.min(allTimes.length - 1, Math.max(0, Math.round(winEndIdx)));
    const tLo = allTimes[iLo] ?? allTimes[0];
    const tHi = allTimes[iHi] ?? allTimes[allTimes.length - 1];
    const spanMs = Math.max(1, (tHi ?? 0) - (tLo ?? 0));
    const avgStepMs = spanMs / Math.max(1, winPoints - 1);
    const tickFmt: TickFmt =
      avgStepMs >= 12 * 3600000
        // 日线：跨年窗口只显示 MM-DD 会分不清年份（回测 10 年净值曲线），改 YYYY-MM
        ? (spanMs >= 365 * 86400000 ? 'YYYY-MM' : 'MM-DD')
      : avgStepMs >= 90 * 60000 ? 'MM-DD HH:mm'
      : winPoints <= 240 ? 'HH:mm'
      : winPoints <= 1920 ? 'MM-DD HH:mm'
      : 'MM-DD';
    return { winStartIdx, winEndIdx, xScale, yScale, rightScale, yOf, tickFmt };
  }, [iw, ih, view, domain, tToIdx, allTimes, display, mode]);
  const { winStartIdx, winEndIdx, xScale, yScale, rightScale, yOf, tickFmt } = geo;

  return (
    <svg width={width} height={height} role="img" aria-label="净值对比图">
      <Group left={margin.left} top={margin.top}>
        <ChartStatic
          iw={iw} ih={ih} display={display} allTimes={allTimes} tToIdx={tToIdx}
          xScale={xScale} yScale={yScale} rightScale={rightScale} yOf={yOf} tickFmt={tickFmt}
          winStartIdx={winStartIdx} winEndIdx={winEndIdx}
          focus={focus} hoverId={hover?.id ?? null} mode={mode} currency={currency}
        />
        {hover && (
          <HoverTip
            hover={hover} display={display} iw={iw} ih={ih} tickFmt={tickFmt}
            mode={mode} currency={currency}
            events={events} holdings={holdings} names={names} priceMap={priceMap}
          />
        )}
      </Group>
      {width > 8 && (
        <rect
          ref={gestureGuard}
          x={margin.left} y={margin.top} width={iw} height={ih}
          fill="transparent"
          style={{ cursor: dragging ? 'grabbing' : 'grab', touchAction: 'none', userSelect: 'none' }}
          onDoubleClick={() => {
            const reset = { offsetPx: 0, scale: 1 };
            pendingViewRef.current = reset;
            committedViewRef.current = reset;
            setView(reset);
          }}
          onWheel={(e) => {
            // 页面滚动拦截在 gestureGuard 原生 non-passive 监听完成；
            // React 的 onWheel 挂在 passive 根监听上，这里 preventDefault 无效（只报控制台错）→ 不调用
            const rect = (e.currentTarget as SVGRectElement).getBoundingClientRect();
            const mx = e.clientX - rect.left;
            // 以鼠标下时间点为锚缩放（几何基于 pendingViewRef 最新值，rAF 合并下 state 可能滞后）
            const cur = pendingViewRef.current;
            const pxPerIdx0 = iw / idxSpan;
            const idxAtMx = domain.idxMin + cur.offsetPx / pxPerIdx0
              + mx / (pxPerIdx0 * cur.scale);
            // 自然缩放：deltaY 细密事件（触屏 pinch）平滑累乘，不逐 tick 跳档
            const s1 = Math.max(1, Math.min(50, cur.scale * Math.exp(-e.deltaY * 0.0012)));
            const pxPerIdx1 = pxPerIdx0 * s1;
            const offsetPx = Math.max(0, Math.min(iw * (s1 - 1),
              (idxAtMx - domain.idxMin) * pxPerIdx1 - mx));
            commitView({ scale: s1, offsetPx });
          }}
          onPointerDown={(e) => {
            applyHover(null); // 拖动开始即清残留 tooltip（拖动结束/中途都不显示旧吸附）
            dragRef.current = { startX: e.clientX, startOffset: pendingViewRef.current.offsetPx };
            setDragging(true);
            try {
              (e.currentTarget as SVGRectElement).setPointerCapture?.(e.pointerId);
            } catch { /* 捕获失败不阻塞拖动 */ }
          }}
          onPointerMove={(e) => {
            const rect = (e.currentTarget as SVGRectElement).getBoundingClientRect();
            const relX = e.clientX - rect.left;
            const relY = e.clientY - rect.top;
            if (dragRef.current) {
              const dx = e.clientX - dragRef.current.startX;
              const cur = pendingViewRef.current;
              commitView({
                ...cur,
                offsetPx: Math.max(0, Math.min(iw * (cur.scale - 1),
                  dragRef.current.startOffset - dx)),
              });
              return; // 拖动中不触发 hover（触屏也不磁吸，省扫描）
            }
            if (e.pointerType !== 'mouse') return;
            // 越界防护: pointer capture 拖动释放后，残余 move 的坐标可能落在绘图区外
            //（比如甩出边界才松手），此时不留悬停
            if (relX < 0 || relY < 0 || relX > iw || relY > ih) {
              applyHover(null);
              return;
            }
            // 2D 最近吸附（仅鼠标悬停）: 鼠标停在哪根线附近, 就高亮哪根并显示它的信息
            const candidates = [...display.lines, ...(display.bench ? [display.bench] : [])];
            let best: { d: number; label: string; id: string; v: number; t: number; x: number; y: number } | null = null;
            for (const l of candidates) {
              for (const p of l.points) {
                const idx = tToIdx.get(p.t);
                if (idx == null || idx < winStartIdx || idx > winEndIdx) continue; // 窗口外点不参与
                const px = xScale(idx) ?? -1e9;
                const py = yScale(p.v) ?? -1e9;
                const d = Math.hypot(px - relX, py - relY);
                if (!best || d < best.d) best = { d, label: l.label, id: l.id, v: p.v, t: p.t, x: px, y: py };
              }
            }
            if (best && best.d < 70) {
              applyHover({ x: best.x, y: best.y, label: best.label, id: best.id, v: best.v, t: best.t });
            } else {
              applyHover(null);
            }
          }}
          onPointerUp={() => { dragRef.current = null; setDragging(false); }}
          onPointerCancel={() => { dragRef.current = null; setDragging(false); applyHover(null); }}
          onPointerLeave={() => { dragRef.current = null; setDragging(false); applyHover(null); }}
        />
      )}
    </svg>
  );
});

/** 多模型净值对比图（visx，浅色终端风）。
 *  mode: 'pct' = 归一化 100 起点（多市场/多币种可对比，基准天然同轴）；
 *        'dollar' = 绝对净值（同币种单市场对比）。
 *  timeRange: 'all' | '5d' 控制时间窗（5d = 最近 5 个交易日）。 */
export default function EquityChart({
  lines,
  benchmark,
  currency = '$',
  mode = 'pct',
  timeRange = 'all',
  height = 380,
  events,
  holdings,
  names,
  priceMap,
}: {
  lines: ChartLine[];
  benchmark?: BenchLine | null;
  currency?: string;
  mode?: 'pct' | 'dollar';
  timeRange?: 'all' | '5d';
  /** 数字 = 固定 px；字符串 = 任意 CSS 值（如 clamp(...) 响应式高度） */
  height?: number | string;
  /** 悬停补充：成交事件（拖尾时间窗内展示买卖） */
  events?: HoverEvent[];
  /** 悬停补充：持仓时间线（该时刻各 agent 持有代码/数量） */
  holdings?: HoldingSpan[];
  /** 股票代码 → 中文名（tooltip 不显示代码只显示名称） */
  names?: Record<string, string>;
  /** 股票代码 → 当前价（持仓金额按现价估算并标注） */
  priceMap?: Record<string, number>;
}) {
  // 底部图例: 选中某模型 → 该线实线、其他虚线淡化; null = 全部实线
  const [focus, setFocus] = useState<string | null>(null);

  // 时间窗裁剪 + 归一化（pct）/ 绝对（dollar）
  const display = useMemo(() => {
    // 「近5日」按时间跨度截取（近 5 个自然日），不是 slice(-5)——
    // 实盘净值是分钟级采样，按点数切会只剩 5 分钟（2026-09-03 修复）
    const windowed = (pts: { t: number; v: number }[]) => {
      if (timeRange !== '5d' || pts.length <= 1) return pts;
      const last = pts[pts.length - 1].t;
      const cutoff = last - 4 * 86400000; // 含当天共 5 个自然日
      const w = pts.filter((p) => p.t >= cutoff);
      return w.length >= 2 ? w : pts.slice(-5);
    };
    const toDisplay = (pts: { t: number; v: number }[]) => {
      const w = windowed(pts);
      if (mode === 'dollar') return w;
      const base = w[0]?.v || 1;
      return w.map((p) => ({ t: p.t, v: (p.v / base) * 100 }));
    };
    return {
      lines: lines.map((l) => ({
        ...l,
        points: l.abs ? windowed(l.points) : toDisplay(l.points),
      })),
      bench: benchmark && mode === 'pct' ? { ...benchmark, points: toDisplay(benchmark.points) } : null,
    };
  }, [lines, benchmark, mode, timeRange]);

  // 断轴：把绝对时间压成连续序号——隔夜/隔周末在 X 轴上紧挨，不画非交易空白。
  // 收盘 15:00 → 次日 09:30 之间 18.5h 没有数据点，scaleTime 会拉成大片空白平线，
  // 用「采样点序号」等距分布即可剪掉。刻度仍经 allTimes 反查显示真实北京时间。
  const allTimes = useMemo(() => {
    const set = new Set<number>();
    for (const l of display.lines) for (const p of l.points) set.add(p.t);
    if (display.bench) for (const p of display.bench.points) set.add(p.t);
    return [...set].sort((a, b) => a - b);
  }, [display]);
  const tToIdx = useMemo(() => {
    const m = new Map<number, number>();
    allTimes.forEach((t, i) => m.set(t, i));
    return m;
  }, [allTimes]);

  // X 域用序号；基准指数全历史时只作参照，取主曲线序号域，超窗部分 clipPath 裁掉。
  const domain = useMemo(() => {
    const n = allTimes.length;
    if (n < 2) return { idxMin: 0, idxMax: 1 };
    return { idxMin: 0, idxMax: n - 1 };
  }, [allTimes]);

  if (!lines.length) {
    return <div className="loading">暂无净值数据</div>;
  }

  return (
    <div
      style={{
        height: typeof height === 'number' ? `${height}px` : height,
        width: '100%',
        display: 'flex',
        flexDirection: 'column',
        position: 'relative',
      }}
    >
      <div
        style={{
          position: 'absolute', top: -2, right: 2, fontSize: 10, color: '#999',
          userSelect: 'none', pointerEvents: 'none', zIndex: 1,
        }}
      >
        拖拽平移 · 滚轮缩放 · 双击复位
        {lines.some((l) => l.fills?.length) && ' · ▲买入 ▼卖出'}
        {lines.some((l) => l.fills?.some((f) => f.kind === 'adjust')) && ' · ◆对账'}
      </div>
      {/* svg 区域 flex 吃满剩余高度；图例在下方自然高度，溢出会压住后续内容 */}
      <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
        <ParentSize>
          {({ width, height: h }) => (
            <ChartInner
              width={width} height={h}
              display={display} allTimes={allTimes} tToIdx={tToIdx} domain={domain}
              focus={focus} mode={mode} currency={currency}
              events={events} holdings={holdings} names={names} priceMap={priceMap}
            />
          )}
        </ParentSize>
      </div>
      {/* 底部图例: 点选模型 → 该线实线, 其他虚线淡化; "全部"恢复 */}
      <div style={{
        display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap',
        paddingLeft: margin.left, alignItems: 'center', userSelect: 'none',
      }}>
        <button
          onClick={() => setFocus(null)}
          style={legendChip(focus === null, '#333')}
        >
          全部
        </button>
        {lines.map((l) => (
          <button
            key={l.id}
            onClick={() => setFocus(focus === l.id ? null : l.id)}
            style={legendChip(focus === l.id, l.color)}
          >
            <span style={{ display: 'inline-block', width: 14, height: 0, borderTop: `2px solid ${l.color}`, verticalAlign: 'middle' }} />
            {l.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** 图例 chip 样式: 选中 = 线色底白字, 未选 = 白底灰字 */
const legendChip = (active: boolean, color: string): React.CSSProperties => ({
  display: 'inline-flex', alignItems: 'center', gap: 5,
  padding: '2px 10px', border: `1px solid ${active ? color : '#ccc'}`,
  borderRadius: 12, background: active ? color : '#fff',
  color: active ? '#fff' : '#555', fontSize: 11, cursor: 'pointer',
});

/** 相邻采样点自然日差 ≥2 = 中间隔了非交易日（周末/法定节假日）→ 线在此断开 */
const dayFloor = (t: number) => Math.floor(t / 86400000);

const gapStartsOf = (pts: { t: number }[]): Set<number> => {
  const s = new Set<number>();
  for (let i = 1; i < pts.length; i++) {
    if (dayFloor(pts[i].t) - dayFloor(pts[i - 1].t) >= 2) s.add(pts[i].t);
  }
  return s;
};

/** 等距时间序列降采样（分钟净值 5k 点 → ≤900 点/线，SVG 渲染上限；保留首尾）。
 *  卡顿治理（2026-09-07）：4944 点 × N agent 线每 20s 全量重绘是主渲染瓶颈之一。 */
export const MAX_CHART_POINTS = 900;

export const downsample = <T,>(pts: T[], n: number = MAX_CHART_POINTS): T[] => {
  if (pts.length <= n) return pts;
  const step = (pts.length - 1) / (n - 1);
  const out: T[] = [];
  for (let i = 0; i < n - 1; i++) out.push(pts[Math.min(pts.length - 1, Math.round(i * step))]);
  out.push(pts[pts.length - 1]);
  return out;
};

/** equity 序列 → 图表线（绝对净值，归一化在组件内完成） */
export const toChartLine = (
  id: string,
  label: string,
  color: string,
  points: EquityPoint[],
): ChartLine => {
  const pts = points.map((p) => ({ t: dayjs(p.date).valueOf(), v: p.equity }));
  const gaps = gapStartsOf(pts);            // 断点基于全量序列判断（抽稀后相邻距会失真）
  return { id, label, color, points: downsample(pts), gapStarts: gaps };
};

/** dashSegs（[from,to] 含两端）→ 实/虚渲染窗口 [start,end) 列表（升序、首尾补齐）。
 *  用于「空仓段虚线、持仓段实线」：窗口按分段边界切开，虚线区间标 dashed。 */
export const dashWindows = (
  n: number,
  segs: [number, number][],
): [number, number][] => {
  if (n <= 0) return [];
  const cuts = new Set<number>([0, n]);
  for (const [a, b] of segs) {
    cuts.add(Math.max(0, Math.min(n - 1, a)));
    cuts.add(Math.max(0, Math.min(n, b + 1)));
  }
  const edges = [...cuts].sort((x, y) => x - y);
  const out: [number, number][] = [];
  for (let i = 0; i + 1 < edges.length; i++) {
    if (edges[i + 1] > edges[i]) out.push([edges[i], edges[i + 1]]);
  }
  return out;
};

/** 指数序列 → 基准线 */
export const toBenchLine = (
  label: string,
  color: string,
  points: { time: string; close: number }[],
): BenchLine | null => {
  if (!points.length) return null;
  const pts = points.map((p) => ({ t: dayjs(p.time).valueOf(), v: p.close }));
  const gaps = gapStartsOf(pts);
  return {
    id: `bench-${label}`,
    label,
    color,
    points: downsample(pts),
    gapStarts: gaps,
  };
};
