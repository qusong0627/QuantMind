/**
 * Reference mining directions list and default direction parsing (consistent with "Mining Direction" in settings)
 * Each direction can attach up to 3 factors' "short name", "expression", "meaning", displayed on hover
 */

import type { FactorLibrary } from '../types-v2';

export interface FactorHint {
  shortName: string;
  expression: string;
  meaning: string;
}

export interface MiningDirectionItem {
  label: string;
  /** Up to 3 factors, displayed when hovering over the direction */
  factors?: FactorHint[];
}

/** Reference mining directions (can be added/deleted/modified as needed; factors can be filled from original_direction.json) */
export const REFERENCE_MINING_DIRECTIONS: MiningDirectionItem[] = [
  {
    label: '价量关系与开盘收益率',
    factors: [
      { shortName: 'KMID', expression: '(close-open)/open', meaning: '开盘收益率' },
      { shortName: 'KUP', expression: '(high-max(open,close))/open', meaning: '上影线相对开盘' },
      { shortName: 'KLOW', expression: '(min(open,close)-low)/open', meaning: '下影线相对开盘' },
    ],
  },
  { label: '短期动量与收益率', factors: [] },
  { label: '成交量比率与放量确认', factors: [] },
  { label: '波动率与价格稳定性', factors: [] },
  { label: '振幅与高低价区间', factors: [] },
  { label: 'RSV 与超买超卖', factors: [] },
  { label: '均线比率与趋势', factors: [] },
  { label: '影线比例与 K 线形态', factors: [] },
  { label: '实体比例与多空力量', factors: [] },
  { label: '收益率波动与风险', factors: [] },
  { label: '高低价相对位置', factors: [] },
  { label: '量价背离与确认', factors: [] },
  { label: '多周期动量组合', factors: [] },
  { label: '成交量标准化特征', factors: [] },
  { label: '价格相对均线位置', factors: [] },
];

/** Get direction label (compatible with object or string) */
export function getDirectionLabel(item: MiningDirectionItem): string {
  return typeof item === 'string' ? item : item.label;
}

interface StoredMiningDirectionConfig {
  miningDirectionMode?: 'selected' | 'random';
  /** 选中的挖掘方向（存 label，避免与动态 L1 类别列表下标错位） */
  selectedMiningDirections?: string[];
  /** 入库闸门开关（T-MV-05，设置页写） */
  qualityGateEnabled?: boolean;
}

/** 读取本地保存的挖掘方向选择（label 列表 + 模式） */
export function getStoredDirectionConfig(): {
  labels: string[];
  mode: 'selected' | 'random';
} {
  try {
    const raw = localStorage.getItem('quantaalpha_config');
    if (!raw) return { labels: [], mode: 'selected' };
    const config = JSON.parse(raw) as StoredMiningDirectionConfig;
    return {
      labels: Array.isArray(config?.selectedMiningDirections)
        ? config.selectedMiningDirections.filter((l) => typeof l === 'string' && l.trim())
        : [],
      mode: config?.miningDirectionMode === 'random' ? 'random' : 'selected',
    };
  } catch {
    return { labels: [], mode: 'selected' };
  }
}

/** Get a default mining direction from saved config (one of the selected list, or a random one) */
export function getDefaultMiningDirection(
  list?: MiningDirectionItem[],
): string {
  const { labels, mode } = getStoredDirectionConfig();
  if (!labels.length) return '';
  let usable = labels;
  if (list && list.length) {
    const valid = new Set(list.map(getDirectionLabel));
    usable = labels.filter((l) => valid.has(l));
  }
  if (!usable.length) return '';
  return mode === 'random'
    ? usable[Math.floor(Math.random() * usable.length)]
    : usable[0];
}

/**
 * 读取设置页的入库闸门开关（T-MV-05）。默认开（true）；设置页存储损坏 /
 * 未读过 → 同默认。语义只表达「关」：true → 调用方不下发 quality_gate_mode
 * （任务行 NULL，生效模式由后端 env ALPHA_GATE_MODE / 逐门禁配置兜底）。
 */
export function getStoredQualityGateEnabled(): boolean {
  try {
    const raw = localStorage.getItem('quantaalpha_config');
    if (!raw) return true;
    const config = JSON.parse(raw) as StoredMiningDirectionConfig;
    return config?.qualityGateEnabled !== false;
  } catch {
    return true;
  }
}

/** Feature catalog category structure */
interface FeatureCatalogCategory {
  id: string;
  name: string;
  features: Array<{ key: string; description: string; formula?: string }>;
}

interface FeatureCatalog {
  categories?: FeatureCatalogCategory[];
}

/**
 * Convert feature catalog categories into mining directions.
 * Each category becomes a direction, with its features as FactorHints.
 */
export function importFeatureCatalogDirections(catalog: FeatureCatalog): MiningDirectionItem[] {
  if (!catalog?.categories) return [];
  return catalog.categories.map((cat) => ({
    label: `${cat.name}类因子变体`,
    factors: cat.features.slice(0, 3).map((f) => ({
      shortName: f.key,
      expression: f.formula || f.key,
      meaning: f.description,
    })),
  }));
}

// ── 因子值库目录（T-MV-06）────────────────────────────────────────────

export interface LibraryDirectionItem extends MiningDirectionItem {
  library: FactorLibrary;
}

export interface MiningDirectionGroups {
  /** 训练特征目录组（feature catalog 类别；后端不可达时为内置参考） */
  catalog: MiningDirectionItem[];
  catalogSource: 'feature_catalog' | 'reference';
  /** 因子值库组（可勾选方向，label 稳定不带磁盘数字） */
  libraries: LibraryDirectionItem[];
  /** 标签/泄露库——目录里显式展示但不可选（治理透明） */
  excludedLibraries: FactorLibrary[];
  /** 目录策展核对日期（后端 checked_at，可空） */
  checkedAt: string;
}

/**
 * 方向标签：只含策展文本（name），**绝不带列数/日期**——label 会存进
 * localStorage 并在派发时作为 direction 下发；塞磁盘数字，盘面一变
 * （219→221 列）已存 label 就与列表失配被静默丢弃（日切白名单旧坑）。
 * 磁盘事实走 libraryFactSummary 的副行展示，每次拉取即刷新。
 */
export function libraryDirectionLabel(lib: FactorLibrary): string {
  return `${lib.name} · 因子值库`;
}

/**
 * 库的磁盘事实摘要（展示副行，非存储）：市场按 CN > HK > US > 其余 的
 * 优先级取第一个有数据的；全无数据 → 诚实说明「本机无数据目录」。
 */
export function libraryFactSummary(lib: FactorLibrary): string {
  const priority = ['CN', 'HK', 'US', ...Object.keys(lib.markets)];
  const market = priority.find((m) => lib.markets[m]);
  if (!market) return '本机无数据目录';
  const facts = lib.markets[market];
  if (!facts) return '本机无数据目录';
  const parts = [market];
  if (facts.columns != null) parts.push(`${facts.columns} 列`);
  if (facts.start && facts.end) parts.push(`${facts.start} ~ ${facts.end}`);
  else if (facts.start) parts.push(`${facts.start} 起`);
  return parts.join(' · ');
}

/**
 * 一次拉齐设置页「挖掘方向」两组：训练特征目录（L1 类别）+ 因子值库。
 * 后端不可达时回落到内置参考列表且如实标注 source='reference'（组标题
 * 据此改文案，不把内置参考冒充成 feature catalog）。
 */
export async function fetchMiningDirectionGroups(): Promise<MiningDirectionGroups> {
  const fallback: MiningDirectionGroups = {
    catalog: REFERENCE_MINING_DIRECTIONS,
    catalogSource: 'reference',
    libraries: [],
    excludedLibraries: [],
    checkedAt: '',
  };
  try {
    const { getFactorCategories } = await import('../services-v2/api');
    const res = await getFactorCategories();
    const categories = res.data?.categories ?? [];
    const libraries = res.data?.libraries ?? [];
    return {
      catalog: categories.length
        ? categories.map((cat) => ({
            label: `${cat.name}类因子 (${cat.featureCount})`,
            factors: cat.sampleFeatures.slice(0, 3).map((key) => ({
              shortName: key,
              expression: key,
              meaning: `${cat.name}类特征`,
            })),
          }))
        : REFERENCE_MINING_DIRECTIONS,
      catalogSource: categories.length ? 'feature_catalog' : 'reference',
      libraries: libraries
        .filter((lib) => !lib.excluded)
        .map((lib) => ({ label: libraryDirectionLabel(lib), library: lib })),
      excludedLibraries: libraries.filter((lib) => lib.excluded),
      checkedAt: res.data?.librariesCheckedAt ?? '',
    };
  } catch {
    return fallback;
  }
}
