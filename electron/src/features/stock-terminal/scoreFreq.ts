/**
 * 分数频率标注（纯展示逻辑，可单测）
 *
 * 口径唯一来源是后端 `backend/shared/signal_scores.score_freq_of`：
 * 只有 `source='realtime'`（盘中热集推理落库）才是实时分，其余一律日频。
 * 前端**只做显示**，不重新判定 —— 这里写死两档正是为了不给「猜一个频率」留口子。
 *
 * 为什么要标：持仓/自选的分数在实时推理开启时是盘中分、关闭或该标的未被热集
 * 覆盖时是隔夜批次分。两者外观一样，用户会拿隔夜分当盘中分做决定。
 */

import type { ScoreFreq } from '../stock-terminal-shared/types';

export interface ScoreFreqView {
  freq: ScoreFreq;
  /** 徽章短文案 */
  label: string;
  /** 徽章配色（sky=实时，slate=日频降级） */
  cls: string;
  /** 悬停解释：把「这是什么分、什么时候的」说全 */
  title: string;
}

const REALTIME_CLS = 'bg-sky-50 text-sky-700 border-sky-200';
const DAILY_CLS = 'bg-slate-100 text-slate-500 border-slate-200';

/** 单行分频徽章：realtime 才给徽章，daily 给「日频」灰标（有分数信息时才渲染）。 */
export function scoreFreqView(
  freq: string | null | undefined,
  asOf?: string | null,
): ScoreFreqView | null {
  if (freq !== 'realtime' && freq !== 'daily') return null;
  const date = String(asOf || '').trim();
  if (freq === 'realtime') {
    return {
      freq,
      label: '实时',
      cls: REALTIME_CLS,
      title: `盘中实时分（热集推理落库${date ? ` · ${date}` : ''}）—— 盘中会随热集刷新`,
    };
  }
  return {
    freq,
    label: '日频',
    cls: DAILY_CLS,
    title: `日频批次分${date ? `（信号日 ${date}）` : ''} —— 盘中实时推理未开启或该标的未进热集，此分不随盘中刷新`,
  };
}

/**
 * 列表头部总览：本页分数里有没有实时分。
 *
 * `realtimeRows` 来自后端同日分数的实测计数（不是「开关是否打开」的猜测）：
 * 开关开着但行情未到达时后端不发布伪实时分，这里的计数就还是 0，顶部就该说日频。
 */
export function tableFreqView(
  realtimeRows: number | null | undefined,
  signalDate?: string | null,
): ScoreFreqView {
  const n = Number(realtimeRows);
  const date = String(signalDate || '').trim();
  if (Number.isFinite(n) && n > 0) {
    return {
      freq: 'realtime',
      label: '含实时分',
      cls: REALTIME_CLS,
      title: `本日 ${n} 只标的是盘中实时分（行内带「实时」徽章），其余为日频批次分${date ? `（信号日 ${date}）` : ''}`,
    };
  }
  return {
    freq: 'daily',
    label: '日频',
    cls: DAILY_CLS,
    title: `本页为日频批次分${date ? `（信号日 ${date}）` : ''} —— 盘中实时推理未开启或本轮无实时分落库`,
  };
}
