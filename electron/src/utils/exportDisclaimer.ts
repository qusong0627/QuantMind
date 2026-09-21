/**
 * 导出件统一免责段 —— **文案与行的唯一实现**。
 *
 * 为什么免责段必须进文件本体
 * --------------------------
 * 屏幕上的免责条（`components/shared/compliance/ComplianceChrome`）只在应用内
 * 可见。导出件一旦落盘或转发就脱离了那个上下文：收件人看到的是一张「AI 选出来的
 * 股票表」，却看不到「这不构成投资建议」。所以免责段是**文件的一部分**，
 * 不是可选的装饰 —— CSV / Excel 两侧都从这里取词，只此一处。
 *
 * 生成时间与数据区间是必要成分，不是装饰
 * --------------------------------------
 * - **生成时间**：说明这是某个时点的快照，避免被当成实时结论；
 * - **数据区间**：说明结论覆盖哪段数据，避免跨期误读（回测口径尤其）。
 *
 * 两者都**不编造**：区间拿不到就整行不出现，绝不留一个空格子或写个假区间。
 * 与 `signalVocabulary` / `researchScore` 同一条纪律 —— 缺失就是缺失，
 * 不显示成 0，也不显示成「—」以外的任何东西（这里连「—」都不写，直接省略该行）。
 *
 * 本模块**零依赖**（不 import 项目内任何东西）：CSV 与 Excel 两个渲染器都要用它，
 * 让它依赖任一渲染器都会绕出循环。渲染各归各家，措辞归一。
 *
 * 与后端的关系
 * ------------
 * 后端另有一份实现（`backend/shared/export_disclaimer.py`）—— 不同语言、不同产物，
 * 无法共享代码。两侧「不许漂」由**金样**机器钉住：
 *
 *     backend/tests/fixtures/exportDisclaimerGolden.json
 *
 * 前后端测试都读它并逐字对拍（本文件的 `__tests__` 与后端的
 * `test_export_disclaimer.py`），任一侧改了措辞而另一侧没跟，两侧测试都红。
 * 措辞本身沿用项目根 `CLAUDE.md` 免责声明段的说法，不另创一套。
 */

/** 免责语句本体。改这里 = 改所有导出件的措辞；**必须同步金样与后端**。 */
export const DISCLAIMER_SENTENCE =
  '本文件由 QuantMind 自动生成，仅供学习研究与技术演示，不构成任何投资建议。';

/** 免责段的标签。CSV 与 Excel 共用，避免两侧叫法漂移。 */
export const DISCLAIMER_LABELS = {
  exportedAt: '生成时间',
  dataRange: '数据区间',
  sentence: '免责声明',
} as const;

export interface ExportDisclaimerMeta {
  /** 数据覆盖区间（人读文本）。拿不到就别传 —— 不编造。 */
  dataRange?: string;
  /** 覆盖「生成时间」，仅供测试固定时钟用。 */
  now?: Date;
}

/**
 * 时间格式化：`YYYY-MM-DD HH:mm:ss`（本地时区，人读且可排序）。
 *
 * 用本地时区而非 UTC：导出件是给人看的，收件人对照自己手表即可；
 * 库里存的瞬时时间（TIMESTAMPTZ）另有 `utc_now()` 口径，两回事。
 */
export function formatExportTime(date: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} ` +
    `${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`
  );
}

/**
 * 由起止构一个区间文本。**缺一端就退化成单端**，不写「~ 2026-09-18」这种半截；
 * 两端都缺返回 `undefined`（交由调用方省略整行）。
 */
export function dataRangeRow(
  start?: string | null,
  end?: string | null,
): string | undefined {
  const s = start?.trim();
  const e = end?.trim();
  if (s && e) return s === e ? s : `${s} ~ ${e}`;
  if (s) return `${s} 起`;
  if (e) return `截至 ${e}`;
  return undefined;
}

/**
 * 免责段的行：`[标签, 值]` 的有序列表。
 *
 * 返回**标签化的对**而不是一整句，是为了两侧渲染器都能各自正确地成列：
 * CSV 逐格转义后并列，Excel 逐格落格。一整句直接塞进去，遇到值里有逗号就会破列。
 *
 * 顺序固定：生成时间 → 数据区间（有才出现）→ 免责语句。语句压尾。
 */
export function disclaimerRows(
  meta: ExportDisclaimerMeta = {},
): Array<[string, string]> {
  const rows: Array<[string, string]> = [
    [DISCLAIMER_LABELS.exportedAt, formatExportTime(meta.now ?? new Date())],
  ];

  const range = meta.dataRange?.trim();
  if (range) {
    rows.push([DISCLAIMER_LABELS.dataRange, range]);
  }

  rows.push([DISCLAIMER_LABELS.sentence, DISCLAIMER_SENTENCE]);
  return rows;
}
