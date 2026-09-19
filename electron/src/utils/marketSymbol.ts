/**
 * 分市场证券代码归一化（前端唯一入口）。
 *
 * 背景：`normalizeStockCode`（portfolioUtils）是 **A 股专用**（6 位 / SH|SZ|BJ 前缀式），
 * 对港股代码是错的：
 *   - `00700`（5 位）原样透传 → 后端按 A 股口径也认不出 → 全链路 404；
 *   - `000700`（6 位）被补成 `SZ000700`（深市 A 股）→ **跨市场串号**，
 *     港股页里查出一只深市股票的数据。
 *
 * 港股在平台内的规范形态是 **4 位 + .HK**（`0700.HK`），与 QuantDB / 训练产物
 * （pred.parquet 实测 2738 只中 2737 只为 4 位+.HK）/ 南向数据一致；8 开头的
 * 5 位真码（如 `89888.HK` 人民币柜台）保留 5 位。
 *
 * 本文件与后端 `backend/shared/stock_utils.py::StockCodeUtil.to_hk_suffix`
 * 是同一口径的两端实现，改任一端必须同步另一端。
 */

/** 市场码（与后端 market 参数一致） */
export type MarketCode = 'CN' | 'HK' | 'US';

const HK_SUFFIX = '.HK';

/**
 * 港股代码 → 规范形态 `XXXX.HK`。
 *
 * 规则：去空白/大写 → 剥 `.HK` 后缀 → 左补零到 5 位 → 若以 `8` 开头保留 5 位，
 * 否则去前导零后右补到 4 位 → 追加 `.HK`。规则幂等，重复调用安全。
 *
 * @example
 * normalizeHkCode('00700')   // '0700.HK'
 * normalizeHkCode('700')     // '0700.HK'
 * normalizeHkCode('02057.HK')// '2057.HK'
 * normalizeHkCode('0700.hk') // '0700.HK'
 * normalizeHkCode('89888')   // '89888.HK'
 */
export const normalizeHkCode = (raw: string): string => {
  const s = (raw || '').trim().toUpperCase();
  if (!s) return s;
  const core = s.endsWith(HK_SUFFIX) ? s.slice(0, -HK_SUFFIX.length) : s;
  if (!core) return s;
  const padded = core.padStart(5, '0');
  if (padded.startsWith('8')) return `${padded}${HK_SUFFIX}`;
  const stripped = padded.replace(/^0+/, '') || '0';
  return `${stripped.padStart(4, '0')}${HK_SUFFIX}`;
};

/**
 * 按市场归一代码。港股走 {@link normalizeHkCode}，其余沿用 A 股口径。
 *
 * @param raw 用户输入或列表项里的原始代码
 * @param market 目标市场；缺省按代码形态推断（4-5 位纯数字 → 港股）
 */
export const normalizeSymbolForMarket = (
  raw: string,
  market?: MarketCode,
): string => {
  const s = (raw || '').trim();
  if (!s) return s;
  const mk = market ?? inferMarket(s);
  if (mk === 'HK') return normalizeHkCode(s);
  return normalizeStockCode(s);
};

/**
 * 由代码形态推断市场。判据与后端 `StockCodeUtil.detect_market` 对齐：
 * - 港股：带 `.HK` 后缀，或 **4-5 位纯数字**（A 股为 6 位，无歧义）
 * - 美股：含字母的 ticker（AAPL / BRK.B / BRK-B）
 * - 其余按 A 股处理
 */
export const inferMarket = (raw: string): MarketCode => {
  const s = (raw || '').trim().toUpperCase();
  if (!s) return 'CN';
  if (/^\d{4,5}\.HK$/.test(s)) return 'HK';
  if (/^\d{4,5}$/.test(s)) return 'HK';
  if (/^[A-Z][A-Z0-9.\-]{0,9}$/.test(s) && /[A-Z]/.test(s)) return 'US';
  return 'CN';
};

/**
 * 前端展示用的「同一标的」判定键：三市场写法差异抹平后比较。
 * 用于判断两次选择是否同一只股票（避免重复请求），不用于取数。
 */
export const symbolIdentityKey = (raw: string): string => {
  const s = (raw || '').trim().toUpperCase();
  if (!s) return '';
  const mk = inferMarket(s);
  if (mk === 'HK') return normalizeHkCode(s);
  if (mk === 'US') return s.replace(/[^A-Z0-9]/g, '');
  return s.replace(/[^0-9]/g, '');
};

// A 股口径从既有工具透传，保持全站一致（此处 re-export 只为让调用方单点引入）
import { normalizeStockCode } from './portfolioUtils';

export { normalizeStockCode };
