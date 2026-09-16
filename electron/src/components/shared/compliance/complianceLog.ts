/**
 * 合规留痕（T-FE-17 第四件）：风险问卷与危险操作确认的本地事件记录。
 *
 * v1 边界（如实）：记录暂存 localStorage 环形缓冲（上限 200 条），服务于本机审计与
 * 问题回溯；跨设备/服务端留档需要后端用户档案接口配合，属后续任务。
 */

export type ComplianceEventKind =
  | 'risk_profile_taken'
  | 'risk_profile_skipped'
  | 'danger_confirmed'
  | 'danger_cancelled';

export interface ComplianceEvent {
  kind: ComplianceEventKind;
  detail: string;
  at: string;
}

const STORAGE_KEY = 'qm:compliance_events_v1';
const MAX_EVENTS = 200;

export function recordComplianceEvent(kind: ComplianceEventKind, detail: string): void {
  try {
    const events = listComplianceEvents();
    events.push({ kind, detail: String(detail || '').slice(0, 200), at: new Date().toISOString() });
    const trimmed = events.slice(-MAX_EVENTS);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
  } catch {
    // 存储不可用：留痕降级为无操作，不阻断用户流程
  }
}

export function listComplianceEvents(): ComplianceEvent[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (e) => e && typeof e.kind === 'string' && typeof e.at === 'string'
    ) as ComplianceEvent[];
  } catch {
    return [];
  }
}
