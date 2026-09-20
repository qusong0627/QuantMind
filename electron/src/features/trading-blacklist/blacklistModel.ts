/**
 * 交易黑名单表格的纯逻辑（与渲染分离，便于单测盯口径）。
 *
 * 三处「错了也不报错」的判断收在这里：
 *
 * 1. **行只能有一个身份**（机器/我排除的/我放行的）。三个身份对应三套互斥的操作按钮，
 *    判错就会给用户一个按下去没反应的按钮，或者一个方向完全相反的按钮。
 * 2. **放行到期要回落**。放行带了到期日时，过期后那只票**又**被排除了。界面上必须
 *    显示成「机器命中」，而不是继续挂着一枚「已放行」——后者会让用户以为可以买。
 * 3. **放行未命中要显式提示**。用户对一只本来就不在名单里的票点了放行，后端统计在
 *    `allow_miss` 里；不说出来，他会以为自己解除了一条不存在的限制。
 */

import type { BlacklistAction, BlacklistListMeta, BlacklistRow } from './types';

/** 行的身份：机器命中 / 本人手工排除 / 本人例外放行（放行已到期的算回机器命中）。 */
export type RowKind = 'machine' | 'manual' | 'allowed';

export function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

/** 本人改动是否仍然生效（无到期日 = 永久生效）。 */
export function manualActive(
  row: BlacklistRow,
  today: string = todayIso(),
): boolean {
  const expire = row.manual?.expire;
  if (!expire) return Boolean(row.manual);
  return expire >= today;
}

export function rowKind(row: BlacklistRow, today: string = todayIso()): RowKind {
  const action = row.manual?.action;
  if (!manualActive(row, today)) return 'machine';
  if (action === 'block') return 'manual';
  if (action === 'allow') return 'allowed';
  return 'machine';
}

/**
 * 放行已到期但本人改动还挂在行上 —— 界面要额外说一句「你放的行走到期了」，
 * 否则用户只会看到按钮变回「放行」，不知道自己那次操作去哪了。
 */
export function expiredManual(
  row: BlacklistRow,
  today: string = todayIso(),
): boolean {
  return Boolean(row.manual) && !manualActive(row, today);
}

export interface RowAction {
  /**
   * `delete` = 撤销本人改动；`allow` = 例外放行；`unallow` = 取消放行；
   * `edit` = 改我自己那条的理由/到期日。
   */
  kind: 'delete' | 'allow' | 'unallow' | 'edit';
  label: string;
}

/**
 * 该行可执行的操作。三条纪律：
 *
 * 1. **机器行没有「删除」**——机器名单由导入器整份覆盖写，删掉下一次导入就回来了，
 *    「删了又回来」比「根本删不掉」更让人困惑。
 * 2. **本人排除过的行只给「撤销排除」**，不给「放行」。放行的实现是把这条改成
 *    ``allow``；而对于一只**机器名单里没有**的票，allow 就是「放行未命中」，
 *    整行会从表里消失——用户点一下「放行」，那只票人间蒸发，且没有任何回执。
 *    要放行机器命中，先撤销自己的排除，再看那一行的「放行」，两步各自有明确回执。
 * 3. **「编辑」只出现在仍然生效的本人改动上**。机器行没有可编辑的理由——那 1800+ 条
 *    的理由是导入器写进来的，改了下次导入就被覆盖；给一个点了白点的按钮比不给更糟。
 *    放行**已到期**的行也回到机器动作（与 `rowKind` 的回落一致）：此时那只票已经又被
 *    拦下了，用户要的是「再放一次」而不是「改上一条已死的记录」，点「放行」重填
 *    理由与到期日即可。
 */
export function rowActions(row: BlacklistRow, today: string = todayIso()): RowAction[] {
  const mine = row.manual;
  if (manualActive(row, today)) {
    if (mine?.action === 'block') {
      return [
        { kind: 'edit', label: '改理由' },
        { kind: 'delete', label: '撤销排除' },
      ];
    }
    if (mine?.action === 'allow') {
      return [
        { kind: 'edit', label: '改理由' },
        { kind: 'unallow', label: '取消放行' },
      ];
    }
  }
  // 机器行的「放行」也要填理由（见 EntryModal）：一条没有理由的放行，
  // 三个月后连用户自己都想不起来当时为什么放它进来。
  return [{ kind: 'allow', label: '放行' }];
}

export interface ReasonView {
  /** 本人那条改动的理由（**可编辑**的那条）；没有本人改动时为 null。 */
  mine: {
    action: BlacklistAction;
    text: string;
    expire: string | null;
    expired: boolean;
  } | null;
  /** 机器名单侧的来源与理由（**只读**，下次导入会整份覆盖）。 */
  machine: string;
}

/**
 * 表格「理由」列的完整内容。
 *
 * 分成两层而不是合成一段，是因为两层的**可编辑性不同**：本人那条能改，机器那条不能。
 * 合成一段之后，用户会去改一段自己改不动、改了也会被下次导入覆盖的文字。
 */
export function reasonView(row: BlacklistRow, today: string = todayIso()): ReasonView {
  const mine = row.manual;
  return {
    mine: mine
      ? {
          action: mine.action,
          // 没填理由时**不显示空串**：空白的「我的理由」看起来像界面坏了
          text: mine.reason?.trim() || '（未填理由）',
          expire: mine.expire ?? null,
          expired: !manualActive(row, today),
        }
      : null,
    machine: sourceBreakdown(row),
  };
}

export interface RowBadge {
  label: string;
  cls: string;
  title: string;
}

/** 状态徽章。颜色是**风险语义**（红=会被拦下、绿=已放行），与行情涨跌无关。 */
export function rowBadge(row: BlacklistRow, today: string = todayIso()): RowBadge {
  const kind = rowKind(row, today);
  if (kind === 'allowed') {
    return {
      label: '已放行',
      cls: 'bg-emerald-50 text-emerald-600 border-emerald-200',
      title: `本人放行${row.manual?.expire ? `，到期日 ${row.manual.expire}` : '（永久）'}：${row.manual?.reason || '未填理由'}`,
    };
  }
  if (row.expired) {
    return {
      label: '已失效',
      cls: 'bg-slate-50 text-slate-400 border-slate-200',
      title: row.expire ? `窗口已于 ${row.expire} 结束，不再拦买` : '窗口已结束',
    };
  }
  if (kind === 'manual') {
    return {
      label: '我排除',
      cls: 'bg-rose-50 text-rose-600 border-rose-200',
      title: `本人手工排除：${row.manual?.reason || '未填理由'}`,
    };
  }
  return {
    label: '拦买',
    cls: 'bg-rose-50 text-rose-600 border-rose-200',
    title: (row.source_labels?.length ? row.source_labels.join('、') : row.sources.join('、')) +
      (row.reason ? `：${row.reason}` : ''),
  };
}

export interface BlacklistSummary {
  imported: boolean;
  reason?: string;
  /** 名单条目总数（机器 + 手工） */
  total: number;
  /** 当前真正拦买的只数 */
  blocking: number;
  asof: string;
  stale: boolean;
  staleDays: number | null;
  manual: number;
  allow: number;
  allowMiss: number;
}

/** 表头统计。名单未导入是一等状态（`imported=false` + reason），不是「0 条」。 */
export function summarize(
  meta: BlacklistListMeta | null | undefined,
  imported: boolean,
  reason?: string,
): BlacklistSummary {
  const counts = meta?.counts ?? {};
  const overlay = meta?.overlay ?? {};
  return {
    imported,
    reason,
    total: Number(counts.total ?? 0),
    blocking: Number(meta?.blocking_now ?? counts.blocking ?? 0),
    asof: String(meta?.asof ?? ''),
    stale: Boolean(meta?.stale),
    staleDays: meta?.stale_days ?? null,
    manual: Number(overlay.manual ?? 0),
    allow: Number(overlay.allow ?? 0),
    allowMiss: Number(overlay.allow_miss ?? 0),
  };
}

/** 基准日陈旧提示（隔壁每日刷新，本仓导入是手动一步，必须显式提醒）。 */
export function staleNote(summary: BlacklistSummary): string | null {
  if (!summary.imported) return null;
  if (!summary.stale) return null;
  const days = summary.staleDays == null ? '?' : String(summary.staleDays);
  return `名单基准日 ${summary.asof || '未知'}，已 ${days} 天未刷新`;
}

/** 放行未命中提示：用户以为解除了限制、实际那只票本来就不在名单里。 */
export function allowMissNote(summary: BlacklistSummary): string | null {
  if (summary.allowMiss <= 0) return null;
  return `有 ${summary.allowMiss} 条放行没有对应的名单命中（那只票本来就不在名单里），放行不产生效果`;
}

export interface SourceLine {
  /** 来源显示名（缺显示名时回落到来源键） */
  label: string;
  /** 该来源的**原话**（合并理由去过重，这里不去了——详情页要能对得上源文件） */
  reason: string;
}

/**
 * 逐源明细（行详情弹窗用）。
 *
 * 与 `sourceBreakdown` 的区别：那个把「来源（理由）」拼成一段给表格用，这个保留结构，
 * 让弹窗能一行一个来源地排版。理由可能为空（有些来源只给标签不给话）——空就留空，
 * 不编造一句「无理由」，那会让人以为系统查过了。
 */
export function sourceLines(row: BlacklistRow): SourceLine[] {
  const by = row.by_source ?? {};
  return Object.entries(by).map(([source, detail]) => ({
    label: detail?.label || source,
    reason: String(detail?.reason ?? '').trim(),
  }));
}

/** 逐源条数（表格底部/悬停的「这一行是被哪些名单命中的」）。 */
export function sourceBreakdown(row: BlacklistRow): string {
  const by = row.by_source ?? {};
  const parts = Object.entries(by).map(([source, detail]) => {
    const label = detail?.label || source;
    return detail?.reason ? `${label}（${detail.reason}）` : label;
  });
  if (parts.length) return parts.join('；');
  return row.reason || '—';
}
