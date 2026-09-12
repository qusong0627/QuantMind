/** 个股终端指标引擎
 *
 * 一等公民：所有叠加（内置指标、后续自定义表达式、策略条件）共用这套计算。
 * 输入为 OHLCV 序列，输出与输入等长的序列（前置不足周期处为 null）。
 */

import { KlineBar } from '../types';

export type Series = (number | null)[];

export function sma(values: number[], period: number): Series {
  const out: Series = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

export function ema(values: number[], period: number): Series {
  const out: Series = new Array(values.length).fill(null);
  const k = 2 / (period + 1);
  let prev: number | null = null;
  for (let i = 0; i < values.length; i++) {
    if (prev === null) {
      // 以首个值为种子
      prev = values[i];
      if (i >= period - 1) out[i] = prev;
    } else {
      prev = values[i] * k + prev * (1 - k);
      if (i >= period - 1) out[i] = prev;
    }
  }
  return out;
}

/** 布林带：中轨 MA20，上下轨 ±2 倍标准差 */
export function boll(values: number[], period = 20, mult = 2): { mid: Series; upper: Series; lower: Series } {
  const mid = sma(values, period);
  const upper: Series = new Array(values.length).fill(null);
  const lower: Series = new Array(values.length).fill(null);
  for (let i = period - 1; i < values.length; i++) {
    const win = values.slice(i - period + 1, i + 1);
    const m = mid[i] as number;
    const std = Math.sqrt(win.reduce((s, v) => s + (v - m) ** 2, 0) / period);
    upper[i] = m + mult * std;
    lower[i] = m - mult * std;
  }
  return { mid, upper, lower };
}

export function macd(values: number[], fast = 12, slow = 26, signal = 9): { dif: Series; dea: Series; hist: Series } {
  const ef = ema(values, fast);
  const es = ema(values, slow);
  const dif: Series = values.map((_, i) => {
    const f = ef[i], s = es[i];
    return f !== null && s !== null ? f - s : null;
  });
  // DEA = DIF 的 EMA（在非空段上计算）
  const difNums = dif.map(v => v ?? 0);
  const deaRaw = ema(difNums, signal);
  const firstValid = dif.findIndex(v => v !== null);
  const dea: Series = dif.map((v, i) => (v === null || i < firstValid + signal - 1 ? null : deaRaw[i]));
  const hist: Series = dif.map((v, i) => {
    const d = dea[i];
    return v !== null && d !== null ? (v - d) * 2 : null;
  });
  return { dif, dea, hist };
}

export function rsi(values: number[], period = 14): Series {
  const out: Series = new Array(values.length).fill(null);
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i < values.length; i++) {
    const ch = values[i] - values[i - 1];
    const gain = Math.max(ch, 0);
    const loss = Math.max(-ch, 0);
    if (i <= period) {
      avgGain += gain / period;
      avgLoss += loss / period;
      if (i === period) out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
      out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
    }
  }
  return out;
}

/** KDJ：RSV -> K -> D -> J */
export function kdj(bars: KlineBar[], n = 9, k1 = 3, d1 = 3): { k: Series; d: Series; j: Series } {
  const k: Series = new Array(bars.length).fill(null);
  const d: Series = new Array(bars.length).fill(null);
  const j: Series = new Array(bars.length).fill(null);
  let prevK: number | null = null;
  let prevD: number | null = null;
  for (let i = 0; i < bars.length; i++) {
    if (i < n - 1) continue;
    const win = bars.slice(i - n + 1, i + 1);
    const hh = Math.max(...win.map(b => b.high));
    const ll = Math.min(...win.map(b => b.low));
    const rsv = hh === ll ? 50 : ((bars[i].close - ll) / (hh - ll)) * 100;
    const curK = prevK === null ? rsv : (rsv * k1 + prevK * (k1 - 1)) / (2 * k1 - 1);
    const curD = prevD === null ? curK : (curK * d1 + prevD * (d1 - 1)) / (2 * d1 - 1);
    k[i] = curK; d[i] = curD; j[i] = 3 * curK - 2 * curD;
    prevK = curK; prevD = curD;
  }
  return { k, d, j };
}

export function atr(bars: KlineBar[], period = 14): Series {
  const out: Series = new Array(bars.length).fill(null);
  let sum = 0;
  for (let i = 1; i < bars.length; i++) {
    const tr = Math.max(
      bars[i].high - bars[i].low,
      Math.abs(bars[i].high - bars[i - 1].close),
      Math.abs(bars[i].low - bars[i - 1].close),
    );
    if (i <= period) {
      sum += tr;
      if (i === period) out[i] = sum / period;
    } else {
      const prev = out[i - 1] as number;
      out[i] = (prev * (period - 1) + tr) / period;
    }
  }
  return out;
}

/** 成交量均线 */
export function volMa(bars: KlineBar[], period: number): Series {
  return sma(bars.map(b => b.volume ?? 0), period);
}
