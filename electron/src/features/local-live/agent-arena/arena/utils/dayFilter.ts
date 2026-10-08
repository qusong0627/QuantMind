/** 「按日期筛选」工具（已完成 / 成交 / 持仓 三 tab 共用）。
 *
 *  口径与 datetime.fmtDay 输出一致：按时间戳原文截取日历日（记录本就是 UTC+8），
 *  不做时区换算——筛出来的日期与卡片上显示的日期保证同源一致。
 *  无日期字段的行（富途持仓无买入时刻、模拟盘快照持仓、桥日志窗口外的持仓等）
 *  只在「全部日期」下显示，选中具体日期时不参与（各列表会明示被排除的条数）。
 */

/** 月/日做范围校验（2026-13-45 这类脏值不当作日期；闰日等历法细节不深究） */
const RE_DAY = /^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])/;

/** 提取日历日（YYYY-MM-DD）；空值 / 非法值 → ''（不参与日期筛选） */
export function dayOf(ts: string | null | undefined): string {
  const s = String(ts ?? '');
  return RE_DAY.test(s) ? s.slice(0, 10) : '';
}

/** 值集合内出现过的日期：去重、降序（最新在前）、剔除空日期 */
export function availableDays(values: (string | null | undefined)[]): string[] {
  const set = new Set<string>();
  for (const v of values) {
    const d = dayOf(v);
    if (d) set.add(d);
  }
  return [...set].sort().reverse();
}

/** 该行是否命中选中日期（'all' = 不筛；无日期行仅 'all' 下可见） */
export function dayHit(ts: string | null | undefined, day: string): boolean {
  return day === 'all' || dayOf(ts) === day;
}

/** 下拉候选 = 数据中出现过的日期 + 当前选中值（切 tab / 切市场后筛选不丢、选项不悬空） */
export function dayOptions(values: (string | null | undefined)[], active: string): string[] {
  const days = availableDays(values);
  if (active !== 'all' && !days.includes(active)) days.push(active);
  return days.sort().reverse();
}