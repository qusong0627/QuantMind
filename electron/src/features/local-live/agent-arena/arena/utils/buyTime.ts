/** 从成交流推导每只当前持仓的建仓时间（FIFO：买建仓、卖冲销，取仍有剩余的最老批次）。
 *
 *  用途：港股富途持仓经桥回报时不带买入时刻（client.ts 富途映射器 buy_time 恒为空），
 *  用页面已轮询的订单历史（fetchFutuTrades）回填，让「持仓」tab 的日期筛选可用；
 *  历史窗口外推不出的 code 不产出（前端按「买入日未知」展示，不参与日期筛选）。
 *
 *  口径与后端 backend/services/live_buy_time.py 的 FIFO 兜底一致；
 *  时间戳统一截到分钟（与桥口径 "YYYY-MM-DDTHH:MM" 相同，spec 里 slice(5) 显示一致）。
 */

export interface DealLike {
  code: string;
  side?: string | null;
  volume: number | string;
  ts: string;
}

/** code → 建仓时间（16 字符 "YYYY-MM-DDTHH:MM"）；清仓/推不出的 code 不出现 */
export function deriveBuyTimes(deals: DealLike[]): Record<string, string> {
  const lots = new Map<string, { ts: string; vol: number }[]>();
  for (const d of [...deals].sort((a, b) => (a.ts < b.ts ? -1 : 1))) {
    const code = String(d.code ?? '');
    const side = String(d.side ?? '').toUpperCase();
    const vol = Number(d.volume) || 0;
    if (!code || vol <= 0 || (side !== 'BUY' && side !== 'SELL')) continue;
    const queue = [...(lots.get(code) ?? [])];
    if (side === 'BUY') {
      queue.push({ ts: String(d.ts).slice(0, 16), vol });
    } else {
      // 卖冲销：FIFO 先冲老仓；卖多于在册（历史窗口外的买入）只冲已知部分
      let left = vol;
      while (left > 0 && queue.length) {
        const used = Math.min(left, queue[0].vol);
        queue[0] = { ts: queue[0].ts, vol: queue[0].vol - used };
        left -= used;
        if (queue[0].vol <= 0) queue.shift();
      }
    }
    lots.set(code, queue);
  }
  const out: Record<string, string> = {};
  for (const [code, queue] of lots) {
    if (queue.length > 0) out[code] = queue[0].ts;
  }
  return out;
}