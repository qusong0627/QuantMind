/**
 * 持仓预警（持仓哨兵）前端服务
 *
 * 后端：`backend/services/api/routers/holding_alerts.py`（读侧）+ `trade/holding_sentinel.py`（写侧）。
 * 信封是 `{success, data}`（**不是**通知中心的 `{code, message, data}`），`apiClient` 已
 * 把 `response.data` 拆出来，这里拿到的是整个 `{success, data}` 对象。
 *
 * 字段名保持后端 snake_case：配置要原样 PUT 回去，中间加一层驼峰映射只会多一个会漂移的地方。
 */

import { apiClient } from './api-client';
import { API_ENDPOINTS } from './config';

export type HoldingAlertKind =
  | 'score_cross_zero'
  | 'score_below_threshold'
  | 'risk_news'
  | 'risk_anomaly'
  | 'risk_list';

export type HoldingAlertSeverity = 'info' | 'warning' | 'critical';
export type HoldingAlertStatus = 'active' | 'dismissed' | 'executed' | 'expired';

/** 严重度排序（与后端 `SEVERITY_ORDER` 同口径；前端只做通道取舍，不重新判定） */
export const SEVERITY_ORDER: Record<HoldingAlertSeverity, number> = {
  info: 0,
  warning: 1,
  critical: 2,
};

export interface HoldingAlertItem {
  id: number;
  symbol: string;
  stockName: string;
  kind: HoldingAlertKind;
  severity: HoldingAlertSeverity;
  title: string;
  content: string;
  detail: Record<string, unknown>;
  scorePrev: number | null;
  scoreNow: number | null;
  scoreAsOf: string | null;
  status: HoldingAlertStatus;
  actionUrl: string;
  createdAt: string | null;
  resolvedAt: string | null;
}

export interface HoldingAlertConfig {
  enabled: boolean;
  /** 跌破该值告警；0 = 关闭该规则（由正转负仍然报） */
  score_threshold: number;
  watch_sim: boolean;
  watch_real: boolean;
  watch_manual: boolean;
  notify_inapp: boolean;
  notify_desktop: boolean;
  notify_sound: boolean;
  min_severity: HoldingAlertSeverity;
}

export interface HoldingSentinelStatus {
  running: boolean;
  lastScanAt?: string | null;
  lastScanEpoch?: number | null;
  users?: number | null;
  monitored?: number | null;
  orphanAccounts?: number;
  /** 该用户的监控面（哨兵按用户写的；没扫到过就是 null） */
  mine?: { monitored?: number; sources?: Record<string, number> } | null;
  reason?: string;
}

export interface HoldingAlertListResult {
  items: HoldingAlertItem[];
  counts: Record<string, number>;
  config: HoldingAlertConfig;
  sentinel: HoldingSentinelStatus;
}

export const DEFAULT_ALERT_CONFIG: HoldingAlertConfig = {
  enabled: true,
  score_threshold: 0,
  watch_sim: true,
  watch_real: true,
  watch_manual: true,
  notify_inapp: true,
  notify_desktop: true,
  notify_sound: true,
  min_severity: 'warning',
};

interface Envelope<T> {
  success?: boolean;
  data?: T;
  detail?: string;
}

/** 统一拆信封：失败必须抛，不能返回空数组让界面显示「暂无预警」（假绿）。 */
function unwrap<T>(raw: unknown, fallbackMessage: string): T {
  const envelope = raw as Envelope<T> | null;
  if (!envelope || envelope.success !== true || envelope.data === undefined) {
    throw new Error(envelope?.detail || fallbackMessage);
  }
  return envelope.data;
}

export const holdingAlertService = {
  async listAlerts(params?: {
    status?: HoldingAlertStatus | 'all';
    limit?: number;
    symbol?: string;
  }): Promise<HoldingAlertListResult> {
    const query: Record<string, unknown> = {};
    if (params?.status) query['status'] = params.status;
    if (params?.limit) query['limit'] = params.limit;
    if (params?.symbol) query['symbol'] = params.symbol;

    const raw = await apiClient.get<unknown>(API_ENDPOINTS.HOLDING_ALERTS, query);
    const data = unwrap<HoldingAlertListResult>(raw, '获取持仓预警失败');
    return {
      items: Array.isArray(data.items) ? data.items : [],
      counts: data.counts || {},
      config: { ...DEFAULT_ALERT_CONFIG, ...(data.config || {}) },
      sentinel: data.sentinel || { running: false },
    };
  },

  /** 忽略：留痕不删，不再出现在默认列表 */
  async dismissAlert(id: number): Promise<boolean> {
    const raw = await apiClient.post<unknown>(API_ENDPOINTS.HOLDING_ALERT_DISMISS(id));
    return unwrap<{ changed: boolean }>(raw, '忽略预警失败').changed;
  },

  /** 已卖出：一键卖出推送成功后的回写 */
  async markExecuted(id: number): Promise<boolean> {
    const raw = await apiClient.post<unknown>(API_ENDPOINTS.HOLDING_ALERT_EXECUTED(id));
    return unwrap<{ changed: boolean }>(raw, '回写预警状态失败').changed;
  },

  async getConfig(): Promise<HoldingAlertConfig> {
    const raw = await apiClient.get<unknown>(API_ENDPOINTS.HOLDING_ALERT_CONFIG);
    return { ...DEFAULT_ALERT_CONFIG, ...unwrap<Partial<HoldingAlertConfig>>(raw, '读取预警配置失败') };
  },

  /** 部分更新：只传要改的字段（后端与已存值合并后整体校验） */
  async updateConfig(patch: Partial<HoldingAlertConfig>): Promise<HoldingAlertConfig> {
    const raw = await apiClient.put<unknown>(API_ENDPOINTS.HOLDING_ALERT_CONFIG, patch as Record<string, unknown>);
    return { ...DEFAULT_ALERT_CONFIG, ...unwrap<Partial<HoldingAlertConfig>>(raw, '保存预警配置失败') };
  },

  async getSentinelStatus(): Promise<HoldingSentinelStatus> {
    const raw = await apiClient.get<unknown>(API_ENDPOINTS.HOLDING_ALERT_STATUS);
    return unwrap<HoldingSentinelStatus>(raw, '读取哨兵状态失败');
  },
};

export default holdingAlertService;
