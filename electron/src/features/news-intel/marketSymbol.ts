/** 前端行情接口的代码口径：A 股要 suffix（600519.SH），港美原样。 */

import type { MarketKey } from './services/newsIntelService';

export function toMarketSymbol(market: MarketKey, code: string): string {
  const raw = String(code || '').trim().toUpperCase();
  if (!raw) return '';
  if (market === 'HK') {
    const hk = raw.match(/^(\d{4,5})(?:\.HK)?$/);
    if (hk) return `${hk[1]}.HK`;
    return raw;
  }
  if (market !== 'CN') return raw;
  const prefixed = raw.match(/^(SH|SZ|BJ)(\d{6})$/);
  if (prefixed) return `${prefixed[2]}.${prefixed[1]}`;
  if (raw.includes('.')) return raw;
  if (/^\d{6}$/.test(raw)) {
    if (raw.startsWith('60') || raw.startsWith('68') || raw.startsWith('90')) return `${raw}.SH`;
    if (raw.startsWith('00') || raw.startsWith('30') || raw.startsWith('20')) return `${raw}.SZ`;
    if (/^(83|43|87|88|92)/.test(raw)) return `${raw}.BJ`;
  }
  return raw;
}

/** 代码 → 市场；用于切市场后过滤掉别的市场的股票。 */
export function marketOf(code: string): MarketKey | 'UNKNOWN' {
  const raw = String(code || '').trim().toUpperCase();
  if (!raw) return 'UNKNOWN';
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(raw) || /^(SH|SZ|BJ)\d{6}$/.test(raw)) return 'CN';
  if (/^\d{4,5}\.HK$/.test(raw) || /^HK\d{4,5}$/.test(raw)) return 'HK';
  if (/^\d{4,5}$/.test(raw)) return 'HK';
  if (/^[A-Z][A-Z0-9.\-]{0,9}$/.test(raw) && !/^\d+$/.test(raw)) return 'US';
  if (/^\d{6}$/.test(raw)) return 'CN';
  return 'UNKNOWN';
}
