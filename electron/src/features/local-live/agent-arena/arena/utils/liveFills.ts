import type { LiveTradeLog } from '../api/client';

/** 实盘（通达信桥）成交归一化 + 归属到决策回合。
 *  模拟盘成交只精确到日；桥的成交回报精确到秒（`2026-09-08T09:37:42`），
 *  复盘要把「哪一轮决策 → 哪一笔成交」对齐，因此单独走一套。 */

export interface LiveFill {
  ts: string; // ISO（秒级，含时区）
  side: 'buy' | 'sell';
  code: string;
  volume: number;
  /** 成交价（桥 filled_price） */
  price: number | null;
  /** 成本价（部分回报缺失 → null） */
  costPrice: number | null;
  orderId: string | null;
  mode: string;
}

/** 桥日志 → 成交记录；非成交（无 side/volume/价）返回 null。 */
export function toLiveFill(log: LiveTradeLog | null | undefined): LiveFill | null {
  if (!log || !log.ts || !log.code) return null;
  const sideRaw = String(log.side ?? '').toLowerCase();
  const side: LiveFill['side'] | null = sideRaw === 'buy' ? 'buy' : sideRaw === 'sell' ? 'sell' : null;
  const volume = Number(log.volume);
  if (!side || !Number.isFinite(volume) || volume <= 0) return null;
  const price = Number(log.fill?.filled_price ?? log.price);
  const costRaw = Number(log.cost_price);
  return {
    ts: log.ts,
    side,
    code: log.code,
    volume,
    price: Number.isFinite(price) && price > 0 ? price : null,
    costPrice: Number.isFinite(costRaw) && costRaw > 0 ? costRaw : null,
    orderId: log.fill?.order_id ?? log.result?.order_id ?? null,
    mode: log.mode ?? '',
  };
}

/** 人工对账记录（mode="fill_adjust"）：没有方向也没有成交回报，是台账校正
 *  （2026-09-08 实录：空账本兜底事故让 pro 误卖 flash 的 688183，当日 13:24
 *  对账把卖款归回 flash、幽灵仓清出）。单独一类，供图表/成交表以「对账」呈现、
 *  不与真实成交混画——否则同一笔资产会被画两次。 */
export interface LiveAdjust {
  ts: string;
  code: string;
  volume: number;
  price: number | null;
  /** 对账说明（必填：无法解释的校正不如不显示） */
  note: string;
  /** 归属模型（对账行落在谁的账上；跨页筛选要用） */
  agent: string | null;
  mode: string;
}

/** 桥日志 → 对账记录；非 fill_adjust 或缺 note 返回 null。 */
export function toLiveAdjust(log: LiveTradeLog | null | undefined): LiveAdjust | null {
  if (!log || log.mode !== 'fill_adjust' || !log.ts || !log.code) return null;
  const note = String(log.note ?? '').trim();
  if (!note) return null;
  const volume = Number(log.volume);
  const price = Number(log.price);
  const agent = String(log.agent ?? '').trim();
  return {
    ts: log.ts,
    code: log.code,
    volume: Number.isFinite(volume) && volume > 0 ? volume : 0,
    price: Number.isFinite(price) && price > 0 ? price : null,
    note,
    agent: agent || null,
    mode: log.mode,
  };
}

export interface RoundRef {
  seq: number;
  ts: string | null;
}

/** 归属规则：一笔成交算到「时间上不晚于它、且间隔 ≤ maxGapMin 的最近一个回合」；
 *  找不到（早于本页最早回合 / 隔夜跳空太久）则列为未归属，不硬塞。 */
export function attachFillsToRounds<T extends RoundRef>(
  rounds: T[],
  fills: LiveFill[],
  maxGapMin = 90,
): { bySeq: Map<number, LiveFill[]>; orphan: LiveFill[] } {
  const timed = rounds
    .filter((r): r is T & { ts: string } => !!r.ts)
    .map((r) => ({ seq: r.seq, ms: new Date(r.ts).getTime() }))
    .filter((r) => Number.isFinite(r.ms))
    .sort((a, b) => a.ms - b.ms);
  const bySeq = new Map<number, LiveFill[]>();
  const orphan: LiveFill[] = [];
  const gapMs = maxGapMin * 60_000;
  for (const fill of fills) {
    const ms = new Date(fill.ts).getTime();
    if (!Number.isFinite(ms)) {
      orphan.push(fill);
      continue;
    }
    let pick: { seq: number; ms: number } | null = null;
    for (const r of timed) {
      if (r.ms <= ms) pick = r;
      else break;
    }
    if (!pick || ms - pick.ms > gapMs) {
      orphan.push(fill);
      continue;
    }
    const arr = bySeq.get(pick.seq) ?? [];
    arr.push(fill);
    bySeq.set(pick.seq, arr);
  }
  for (const arr of bySeq.values()) arr.sort((a, b) => (a.ts < b.ts ? -1 : 1));
  return { bySeq, orphan };
}
