import type { PositionRecord } from '../api/client';

/** 持仓快照差分：相邻两个快照逐票比对，给出新开/加仓/减仓/清仓。
 *  复盘要的是「哪天动了什么」，而不是每个日期重复一遍全量持仓。 */

export type ChangeKind = 'open' | 'add' | 'reduce' | 'close';

export interface PositionChange {
  symbol: string;
  from: number;
  to: number;
  delta: number;
  kind: ChangeKind;
}

export interface SnapshotDiff {
  date: string;
  /** 该日记录的原始动作（日志侧落库的 this_action） */
  action: PositionRecord['this_action'];
  changes: PositionChange[];
  /** 收盘持仓只数（不含 CASH） */
  holdings: number;
  cash: number | null;
}

const kindOf = (from: number, to: number): ChangeKind | null => {
  if (from === to) return null;
  if (from === 0 && to > 0) return 'open';
  if (from > 0 && to === 0) return 'close';
  return to > from ? 'add' : 'reduce';
};

/** 按日期升序传入（接口顺序），返回按日期降序的快照差分。 */
export function diffSnapshots(records: PositionRecord[]): SnapshotDiff[] {
  const out: SnapshotDiff[] = [];
  for (let i = 0; i < records.length; i++) {
    const cur = records[i];
    const prev = i > 0 ? records[i - 1] : null;
    const positions = cur.positions ?? {};
    const prevPositions = prev?.positions ?? {};
    const changes: PositionChange[] = [];
    const symbols = new Set([...Object.keys(positions), ...Object.keys(prevPositions)]);
    let holdings = 0;
    for (const symbol of symbols) {
      if (symbol === 'CASH') continue;
      const to = Number(positions[symbol] ?? 0);
      const from = Number(prevPositions[symbol] ?? 0);
      if (to > 0) holdings += 1;
      const kind = kindOf(from, to);
      if (kind) changes.push({ symbol, from, to, delta: to - from, kind });
    }
    // 首日快照：把已有的持仓记为建仓（没有前一日可比）；变动大的在前，同幅按代码稳定排序
    changes.sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || a.symbol.localeCompare(b.symbol));
    out.push({
      date: cur.date,
      action: cur.this_action ?? null,
      changes,
      holdings,
      cash: positions.CASH != null ? Number(positions.CASH) : null,
    });
  }
  return out.reverse();
}

export const CHANGE_LABEL: Record<ChangeKind, string> = {
  open: '新开',
  add: '加仓',
  reduce: '减仓',
  close: '清仓',
};
