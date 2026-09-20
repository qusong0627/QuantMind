/**
 * 持仓预警面板的纯展示逻辑（无 React，可单测）
 *
 * 口径全部来自后端 `holding_alert_contract`：这里只做「怎么显示」，**不重新判定**。
 */

import type {
  HoldingAlertItem,
  HoldingAlertKind,
  HoldingAlertSeverity,
  HoldingSentinelStatus,
} from '../../services/holdingAlertService';

export interface ToneMeta {
  tone: 'red' | 'amber' | 'slate' | 'blue';
  label: string;
}

const KIND_LABEL: Record<HoldingAlertKind, string> = {
  score_cross_zero: '分数转负',
  score_below_threshold: '跌破阈值',
  risk_news: '盘中利空',
  risk_anomaly: '盘中异动',
  risk_list: '名单/利空命中',
};

const SEVERITY_META: Record<HoldingAlertSeverity, ToneMeta> = {
  critical: { tone: 'red', label: '危急' },
  warning: { tone: 'amber', label: '警告' },
  info: { tone: 'blue', label: '提示' },
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind as HoldingAlertKind] || kind || '预警';
}

export function severityMeta(severity: string): ToneMeta {
  return SEVERITY_META[severity as HoldingAlertSeverity] || SEVERITY_META.info;
}

/** 分数一律 3 位小数带符号：`+0.401` / `-0.153`（和列表页同款观感） */
export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const n = Number(value);
  return `${n >= 0 ? '+' : ''}${n.toFixed(3)}`;
}

/** 分数迁移展示：`+0.401 → -0.153`；缺一侧就只显示有的那侧 */
export function scoreTransition(item: Pick<HoldingAlertItem, 'scorePrev' | 'scoreNow'>): string {
  const prev = item.scorePrev;
  const now = item.scoreNow;
  if (prev === null || prev === undefined) {
    return now === null || now === undefined ? '—' : `现 ${formatScore(now)}`;
  }
  if (now === null || now === undefined) return `曾 ${formatScore(prev)}`;
  return `${formatScore(prev)} → ${formatScore(now)}`;
}

/**
 * 时间解析：后端 `TIMESTAMPTZ.isoformat()` 带 `+00:00`，但历史行/其它序列化路径可能给出
 * 不带时区的字符串——那种一律按 UTC 解释（项目瞬时列口径），不能按浏览器本地时区。
 */
export function parseAlertTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value);
  const date = new Date(hasZone ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 相对时间（面板用；无值返回 '—'） */
export function alertAgeText(value: string | null | undefined, nowMs: number = Date.now()): string {
  const date = parseAlertTime(value);
  if (!date) return '—';
  const seconds = Math.max(0, Math.floor((nowMs - date.getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** 哨兵扫描多久算「不新鲜」：周期 60s，给 5 个周期余量 */
export const SENTINEL_STALE_SECONDS = 300;

export interface SentinelHeadline {
  tone: 'red' | 'amber' | 'green' | 'slate';
  text: string;
  /** 面板是否该显示「不会有预警」的警示条 */
  warn: boolean;
}

/**
 * 哨兵状态文案。**如实**：没在跑就说没在跑，扫到了 0 只就说 0 只，
 * 不能因为「列表为空」让用户以为「我的持仓都很安全」。
 */
export function sentinelHeadline(
  status: HoldingSentinelStatus | null | undefined,
  nowMs: number = Date.now(),
): SentinelHeadline {
  if (!status || !status.running) {
    return {
      tone: 'red',
      text: `持仓哨兵未运行${status?.reason ? `（${status.reason}）` : ''}，当前不会有新预警`,
      warn: true,
    };
  }
  const last = status.lastScanEpoch
    ? status.lastScanEpoch * 1000
    : parseAlertTime(status.lastScanAt)?.getTime() ?? null;
  const ageSeconds = last === null ? null : Math.max(0, Math.floor((nowMs - last) / 1000));
  const stale = ageSeconds !== null && ageSeconds > SENTINEL_STALE_SECONDS;

  const mine = status.mine?.monitored;
  const scope = mine === undefined || mine === null
    ? `全局监控 ${status.monitored ?? '—'} 只`
    : `监控你的 ${mine} 只`;

  const when = ageSeconds === null
    ? ''
    : ageSeconds < 60
      ? `${ageSeconds} 秒前扫描`
      : `${Math.floor(ageSeconds / 60)} 分钟前扫描`;

  if (stale) {
    return {
      tone: 'amber',
      text: `哨兵心跳过期（${when || `>${SENTINEL_STALE_SECONDS}s`}），预警可能滞后 · ${scope}`,
      warn: true,
    };
  }
  if (mine === 0) {
    return { tone: 'amber', text: `哨兵运行中，但没扫到你的持仓/自选（${when}）`, warn: true };
  }
  return { tone: 'green', text: `哨兵运行中 · ${scope}${when ? ` · ${when}` : ''}`, warn: false };
}

/** 推送回执的最小结构（结构化入参，避免 alertModel 依赖终端模块的具体类型） */
export interface PushOutcomeLike {
  dry_run?: boolean;
  status?: string;
  summary?: { succeeded?: number; failed?: number; skipped?: number };
}

/**
 * 卖出回执是否够格把预警标成「已卖出」。
 *
 * 只有**真的提交出去了**才算：被拦（blocked）、失败（failed）、预演（dry_run）、零成功
 * 都不标——否则用户会看到「已卖出」而仓位还在，这比不标更危险。
 */
export function shouldMarkExecuted(outcome: PushOutcomeLike | null | undefined): boolean {
  if (!outcome || outcome.dry_run) return false;
  const succeeded = Number(outcome.summary?.succeeded ?? 0);
  if (succeeded <= 0) return false;
  return outcome.status === 'executed' || outcome.status === 'partial' || outcome.status === undefined;
}

/** 预警深链：后端给的 action_url 是站内路径；异常的（外链/空）一律退回交易台 */
export function alertActionTarget(actionUrl: string | null | undefined): string {
  const raw = String(actionUrl || '').trim();
  return raw.startsWith('/') ? raw : '/trading';
}

/**
 * 面板计数。
 *
 * `counts` 是后端按 **status** 聚合的（active/dismissed/executed/expired/total），**没有** severity 维度，
 * 所以「待处理」取后端全量真值，而「危急」只能在已加载的这批里数——因此调用方必须把
 * 危急那块标成「本页」口径，别让它看起来像全量。
 */
export function panelCounts(
  counts: Record<string, number> | null | undefined,
  items: HoldingAlertItem[],
): { active: number; criticalInPage: number } {
  const safe = counts || {};
  return {
    active: Number(safe['active'] ?? items.filter((i) => i.status === 'active').length) || 0,
    criticalInPage: items.filter((i) => i.severity === 'critical' && i.status === 'active').length,
  };
}
