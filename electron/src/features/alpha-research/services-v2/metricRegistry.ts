/**
 * 因子挖掘指标描述符注册表（前端侧唯一词汇表）。
 *
 * 双层来源：
 * - 本地默认表 = 后端金样 `backend/tests/fixtures/miningMetricsGolden.json` 的
 *   registry 段（双端读同一金样，改标签/精度必须两边同步过测试）；
 * - 运行时 `GET /alpha-agent/metrics/registry` 拉取后端表合并（按 key 覆盖、
 *   新增键追加）；后端不可用时回落本地表——**离线可渲染**。
 *
 * 数值纪律：缺失一律显示 `—`（MISSING_METRIC_TEXT），绝不伪造 0——
 * `turnover=0` 的语义是「名单完全没换」，与「没算过」是两回事。
 */

import { apiClient } from '../../../services/aiStrategyClients';

export interface MetricDescriptor {
  key: string;
  label: string;
  /** 分组：prediction / robustness / trading / return（后端 MetricDescriptor.to_dict 契约） */
  group: string;
  /** ratio=倍数 / score=分数 / days=天数 / pct=小数比例（展示时 ×100 加 %） */
  unit: string;
  /** higher=越大越好 / lower=越小越好 / none=无方向（按方向着色的消费方读这个） */
  better: string;
  precision: number;
  description: string;
}

/** 缺失值占位符。UI 一律用它，禁止用 0 代替。 */
export const MISSING_METRIC_TEXT = '—';

/** 本地默认表：与后端金样 registry 段逐字段一致（dual-read，双端测试锁定）。 */
export const DEFAULT_METRIC_DESCRIPTORS: MetricDescriptor[] = [
  { key: 'ic', label: 'IC（日均）', group: 'prediction', unit: 'ratio', better: 'higher', precision: 4, description: '因子值与次日收益的日度截面相关系数均值' },
  { key: 'rank_ic', label: 'Rank IC（日中位）', group: 'prediction', unit: 'ratio', better: 'higher', precision: 4, description: '日度截面 Spearman 秩相关的中位数' },
  { key: 'icir', label: 'ICIR', group: 'prediction', unit: 'ratio', better: 'higher', precision: 3, description: '日均 IC ÷ 日度 IC 标准差' },
  { key: 'rank_icir', label: 'Rank ICIR', group: 'prediction', unit: 'ratio', better: 'higher', precision: 3, description: '日均 Rank IC ÷ 其标准差' },
  { key: 'n_obs', label: '有效天数', group: 'prediction', unit: 'days', better: 'none', precision: 0, description: '参与 IC 计算的交易日数' },
  { key: 'rre', label: 'RRE（排序可靠度）', group: 'robustness', unit: 'score', better: 'higher', precision: 4, description: '相邻日排名分布 KL 散度贴合度，1=分布完全不变（AlphaEval 口径）' },
  { key: 'quality.pfs', label: 'PFS（扰动保真度）', group: 'robustness', unit: 'score', better: 'higher', precision: 4, description: '截面加噪后排序保持率，<0.9 预警' },
  { key: 'quality.pfs_gauss', label: 'PFS-Gauss', group: 'robustness', unit: 'score', better: 'higher', precision: 4, description: '高斯噪声扰动下的 PFS' },
  { key: 'quality.pfs_t', label: 'PFS-T', group: 'robustness', unit: 'score', better: 'higher', precision: 4, description: 't 分布噪声扰动下的 PFS' },
  { key: 'turnover_daily', label: '日均换手', group: 'trading', unit: 'ratio', better: 'lower', precision: 4, description: '多头组合（截面 rank 前 30%）名单日均变动比' },
  { key: 'ann_turnover', label: '年化换手', group: 'trading', unit: 'ratio', better: 'lower', precision: 2, description: '日均换手 × 252' },
  { key: 'ann_return_net', label: '扣费年化收益', group: 'trading', unit: 'pct', better: 'higher', precision: 2, description: '按换手比例扣除双边成本后的年化收益（研究口径 0.2%）' },
  { key: 'sharpe_net', label: '扣费夏普', group: 'trading', unit: 'ratio', better: 'higher', precision: 2, description: '扣费日收益的年化夏普' },
  { key: 'max_drawdown_net', label: '扣费最大回撤', group: 'trading', unit: 'pct', better: 'lower', precision: 2, description: '扣费净值最大回撤' },
  { key: 'annual_return', label: '年化收益（毛）', group: 'return', unit: 'pct', better: 'higher', precision: 2, description: '多头组合年化收益（未扣成本）' },
  { key: 'sharpe_ratio', label: '夏普（毛）', group: 'return', unit: 'ratio', better: 'higher', precision: 3, description: '毛收益年化夏普' },
  { key: 'max_drawdown', label: '最大回撤（毛）', group: 'return', unit: 'pct', better: 'lower', precision: 2, description: '毛净值最大回撤' },
];

const FALLBACK_DESCRIPTOR: MetricDescriptor = {
  key: '',
  label: '',
  group: 'prediction',
  unit: 'ratio',
  better: 'none',
  precision: 4,
  description: '',
};

let registryCache: MetricDescriptor[] = DEFAULT_METRIC_DESCRIPTORS;
let inflight: Promise<MetricDescriptor[]> | null = null;

function coerceDescriptor(raw: any): MetricDescriptor | null {
  if (!raw || typeof raw.key !== 'string' || !raw.key) return null;
  return {
    key: raw.key,
    label: typeof raw.label === 'string' ? raw.label : raw.key,
    group: typeof raw.group === 'string' ? raw.group : 'prediction',
    unit: typeof raw.unit === 'string' ? raw.unit : 'ratio',
    better: typeof raw.better === 'string' ? raw.better : 'none',
    precision: Number.isInteger(raw.precision) ? raw.precision : 4,
    description: typeof raw.description === 'string' ? raw.description : '',
  };
}

/** base 顺序保留；incoming 按 key 覆盖，新键追加在后（后端扩了指标，前端不用发版也有描述符）。 */
export function mergeMetricDescriptors(
  base: MetricDescriptor[],
  incoming: MetricDescriptor[],
): MetricDescriptor[] {
  const incomingByKey = new Map(incoming.map((d) => [d.key, d]));
  const merged = base.map((d) => incomingByKey.get(d.key) ?? d);
  const baseKeys = new Set(base.map((d) => d.key));
  for (const d of incoming) {
    if (!baseKeys.has(d.key)) merged.push(d);
  }
  return merged;
}

/**
 * 拉取后端注册表并与本地默认表合并；后端失败返回本地表（不抛）。
 * 并发调用共享同一个在途请求；成功后结果驻留缓存。
 */
export async function fetchMetricRegistry(): Promise<MetricDescriptor[]> {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const res = await apiClient.get('/alpha-agent/metrics/registry');
      const rawMetrics = res.data?.data?.metrics;
      if (!Array.isArray(rawMetrics) || rawMetrics.length === 0) {
        throw new Error('empty metric registry');
      }
      const incoming = rawMetrics
        .map(coerceDescriptor)
        .filter((d): d is MetricDescriptor => d !== null);
      registryCache = mergeMetricDescriptors(DEFAULT_METRIC_DESCRIPTORS, incoming);
    } catch {
      registryCache = DEFAULT_METRIC_DESCRIPTORS; // 离线/后端未升级：本地金样表照常渲染
    }
    return registryCache;
  })();
  try {
    return await inflight;
  } finally {
    inflight = null;
  }
}

/** 当前生效的注册表（未 fetch 过 = 本地默认表）。 */
export function getMetricRegistry(): MetricDescriptor[] {
  return registryCache;
}

export function getMetricDescriptor(key: string): MetricDescriptor | undefined {
  return registryCache.find((d) => d.key === key);
}

/**
 * 展示格式化：null/undefined/NaN/非数字一律 `—`；pct 单位的小数值 ×100 加 %。
 * 接受 key（查注册表）或描述符对象（调用方已有）。
 */
export function formatMetricValue(
  keyOrDescriptor: string | MetricDescriptor,
  value: number | null | undefined,
): string {
  const descriptor =
    typeof keyOrDescriptor === 'string'
      ? getMetricDescriptor(keyOrDescriptor) ?? { ...FALLBACK_DESCRIPTOR, key: keyOrDescriptor }
      : keyOrDescriptor;
  if (value == null || typeof value !== 'number' || !Number.isFinite(value)) {
    return MISSING_METRIC_TEXT;
  }
  if (descriptor.unit === 'pct') {
    return `${(value * 100).toFixed(descriptor.precision)}%`;
  }
  return value.toFixed(descriptor.precision);
}

/** 供测试重置模块级缓存（生产代码不要调用）。 */
export function __resetMetricRegistryForTests(): void {
  registryCache = DEFAULT_METRIC_DESCRIPTORS;
  inflight = null;
}
