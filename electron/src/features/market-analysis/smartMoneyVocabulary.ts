/** A 股聪明钱 · 口径文案与展示映射（唯一映射处；细案 §6.2 用词纪律）。
 *
 * 纪律：
 * - 展示面只说**事实语义与位置**（机构净买额/融资净买入/分位 0–100），
 *   不出现「买入信号/推荐/建议」，不写「前 N%」话术；
 * - 缺失一律「—」，**绝不显示成 0**（`position_score=0` 类语义不存在于此，
 *   但 0 分位与缺失是两码事，这里只处理缺失）；
 * - 柱名 → 中文 label 与后端 `signals.PILLAR_META` 同步（后端为唯一数据源，
 *   本表为兜底/缩写展示；运行时优先用接口返回的 label）。
 */

/** 面板口径横幅 */
export const SMART_MONEY_TITLE = '聪明钱 · 席位与资金行为统计评分';
export const SMART_MONEY_DISCLAIMER = '研究评分卡（非模型预测、非交易指令）';

/** 缺失展示符（全站统一「—」） */
export const MISSING = '—';

/** 事件类型 → 展示词（后端 latest_event.type 枚举） */
export const EVENT_TYPE_LABELS: Record<string, string> = {
  lhb: '龙虎榜',
  inst: '机构席位',
  block_trade: '大宗交易',
  survey: '机构调研',
};

export function eventTypeLabel(t: string | null | undefined): string {
  if (!t) return MISSING;
  return EVENT_TYPE_LABELS[t] ?? '事件';
}

/** 柱名 → 中文 label（与后端 signals.PILLAR_META 同步；兜底展示） */
export const PILLAR_LABELS: Record<string, string> = {
  inst_net_ratio_5d: '机构净买占比·5日',
  inst_net_buy_5d_z: '机构净买额·5日z20',
  lhb_net_ratio_5d: '龙虎榜净买占比·5日',
  margin_net_5d_z: '融资净买入·5日z20',
  margin_bal_chg_20d: '融资余额变化·20日',
  dzjy_amount_ratio_5d: '大宗成交占比·5日',
  dzjy_premium_5d: '大宗折溢率·5日',
  survey_inst_90d: '机构调研家数·90日',
};

/** 柱名 → 榜单表头缩写（窄列用；tooltip 里给全称） */
export const PILLAR_SHORT: Record<string, string> = {
  inst_net_ratio_5d: '机构比',
  inst_net_buy_5d_z: '机构z',
  lhb_net_ratio_5d: '龙虎榜',
  margin_net_5d_z: '两融z',
  margin_bal_chg_20d: '两融额',
  dzjy_amount_ratio_5d: '大宗量',
  dzjy_premium_5d: '大宗价',
  dzjy_inst_amount_ratio_5d: '机构大宗',
  dzjy_inst_premium_5d: '大宗价',
  survey_inst_90d: '调研',
};

export function pillarLabel(name: string): string {
  return PILLAR_LABELS[name] ?? name;
}

export function pillarShort(name: string): string {
  return PILLAR_SHORT[name] ?? name;
}

/** 入柱方向（+1/−1；来自 evidence 的 h10 实测 IC 符号）→ 展示词 */
export function directionLabel(d: number | null | undefined): string {
  if (d === 1) return '正向';
  if (d === -1) return '负向';
  return MISSING;
}

/** 方向 tooltip（解释负向柱分位已取反，柱分高 = 研究口径下历史有利方向） */
export function directionHint(d: number | null | undefined): string {
  if (d === 1) return '实测 h10 IC 为正：分位原值计分（柱分高 = 数值靠前）';
  if (d === -1) return '实测 h10 IC 为负：分位取反（100−分位）计分，柱分高 = 历史有利方向';
  return '无方向信息（未裁定或旧产物）';
}

/** 缺失安全数值格式化：null/NaN → 「—」，绝不显示 0 */
export function fmtNum(
  v: number | null | undefined,
  digits = 2,
): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return MISSING;
  return Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** 分数（0–100 分位合成）→ 1 位小数；缺失「—」 */
export function fmtScore(v: number | null | undefined): string {
  return fmtNum(v, 1);
}

/** 元 → 亿元（带符号，2 位）；缺失「—」 */
export function fmtYiFromYuan(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return MISSING;
  return fmtNum(Number(v) / 1e8, 2);
}

/** 万元 → 亿元（带符号，2 位）；缺失「—」（两融原始单位万元） */
export function fmtYiFromWan(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return MISSING;
  return fmtNum(Number(v) / 1e4, 2);
}

/** 分数（0–1）→ 百分比（1 位，不带 % 符号的数值）；缺失「—」 */
export function fmtPct01(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return MISSING;
  return fmtNum(Number(v) * 100, 1);
}

/** 分数（0–1）→ 百分比带 % 符号；缺失「—」 */
export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return MISSING;
  return `${fmtNum(Number(v) * 100, digits)}%`;
}

/** 带符号数值（B7 唯一格式器：正数统一带「+」，0 不带号）；缺失「—」 */
export function fmtSigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return MISSING;
  const n = Number(v);
  return `${n > 0 ? '+' : ''}${fmtNum(n, digits)}`;
}

/** bp → 带符号 1 位小数字符串（研究口径统一；不加单位后缀）；缺失「—」 */
export function fmtBp(v: number | null | undefined, digits = 1): string {
  return fmtSigned(v, digits);
}

/** 席位类型 chip 顺序（与后端 SEAT_CLASS_LABELS 键同集） */
export const SEAT_CLASS_ORDER = ['inst', 'north', 'branch', 'other'] as const;

/** 席位类型兜底标签（接口已随行返回 class_label，本表仅兜底） */
export const SEAT_CLASS_FALLBACK: Record<string, string> = {
  inst: '机构专用',
  north: '陆股通',
  branch: '券商席位',
  other: '其他',
};

export function seatClassLabel(cls: string, fromApi?: string | null): string {
  return fromApi || SEAT_CLASS_FALLBACK[cls] || cls;
}

/** 评分桶色阶（分桶不是涨跌，用紫阶强度；越靠后越深） */
export const BUCKET_TONES = [
  'bg-slate-100 text-slate-500 border-slate-200',
  'bg-purple-50 text-purple-600 border-purple-100',
  'bg-purple-100 text-purple-700 border-purple-200',
  'bg-purple-600 text-white border-purple-600',
] as const;

/** 事件研究强/弱组双色（组=身份不是涨跌 → 中性紫/蓝，不占红绿语义） */
export const CAR_STRONG_COLOR = '#7c3aed';
export const CAR_WEAK_COLOR = '#2563eb';

/** 金额/涨跌符号着色（A 股口径：红涨绿跌，仅用于数值正负）；0 与缺失走中性 */
export function signCls(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v)) || Number(v) === 0) {
    return 'text-slate-500';
  }
  return Number(v) > 0 ? 'text-red-600' : 'text-green-600';
}

/** 分数色阶（中性，紫系）：分不是涨跌，绝不套红绿 */
export function scoreCls(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return 'text-slate-400';
  if (v >= 70) return 'text-purple-700';
  if (v >= 40) return 'text-slate-700';
  return 'text-slate-500';
}
