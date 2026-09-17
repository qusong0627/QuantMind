/**
 * 策略模板库纯函数（T-FE-11）：后端模板 → 画廊视图模型（容错映射 + 过滤 + 三行说明）。
 *
 * 背景：`/api/v1/strategies/templates` 返回后端字段（id/name/description/category/
 * difficulty/markets/dir/params/live_config_tips），与 TS 侧 StrategyTemplate 接口
 * 历史字段不一致——本模块做**唯一容错边界**（两代字段都认），画廊只消费 GalleryTemplate。
 */

export interface GalleryTemplate {
  id: string;
  name: string;
  description: string;
  category: string;
  difficulty: string;
  markets: string[];
  isMinibt: boolean;
  paramCount: number;
  dir: string;
  tips: string[];
  code: string;
}

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  hong_kong: '港股',
  us_stock: '美股',
  crypto: '加密货币',
  futures: '期货',
};

export function marketLabel(key: string): string {
  return MARKET_LABELS[key] || key;
}

export function difficultyLabel(key: string): string {
  const map: Record<string, string> = { beginner: '入门', intermediate: '进阶', advanced: '高级' };
  return map[key] || key || '通用';
}

/** 容错映射（两代字段都认；缺字段给安全默认，不抛错） */
export function normalizeTemplate(raw: Record<string, unknown>): GalleryTemplate {
  const code = String(raw.code || '');
  const params = Array.isArray(raw.params) ? (raw.params as unknown[]) : [];
  const dir = String(raw.dir || '');
  const marketsRaw = raw.markets ?? raw.suitableMarkets ?? [];
  const markets = Array.isArray(marketsRaw) ? marketsRaw.map((m) => String(m)) : [];
  const tipsRaw = raw.live_config_tips ?? raw.liveConfigTips ?? raw.tips ?? [];
  const tips = Array.isArray(tipsRaw) ? tipsRaw.map((t) => String(t)) : [];
  return {
    id: String(raw.id || ''),
    name: String(raw.name || raw.id || '未命名模板'),
    description: String(raw.description || ''),
    category: String(raw.category || 'basic'),
    difficulty: String(raw.difficulty || ''),
    markets,
    // minibt 判定：代码导入 / 工作空间目录 / 参数含 SYMBOL
    isMinibt:
      /import\s+minibt|from\s+minibt/.test(code) ||
      dir.toLowerCase().includes('minibt') ||
      params.some((p) => String((p as Record<string, unknown>)?.name || '').toUpperCase() === 'SYMBOL'),
    paramCount: params.length,
    dir,
    tips,
    code,
  };
}

/** 模板三行说明（T-FE-11 验收）：描述 + 首条使用提示 + 规模信息 */
export function templateLines(t: GalleryTemplate): string[] {
  const lines: string[] = [];
  if (t.description) lines.push(t.description.split('\n')[0].slice(0, 120));
  if (t.tips.length > 0) lines.push(`使用提示：${t.tips[0].slice(0, 100)}`);
  lines.push(
    `参数 ${t.paramCount} 个 · ${difficultyLabel(t.difficulty)} · 适用 ${
      t.markets.length ? t.markets.map(marketLabel).join('/') : 'A股'
    }`
  );
  return lines.slice(0, 3);
}

export interface TemplateFilter {
  keyword?: string;
  difficulty?: string;
  market?: string;
}

/** 过滤（纯函数）：关键词（名/描述/目录）+ 难度 + 市场（空 markets 视为 A 股） */
export function filterTemplates(
  templates: GalleryTemplate[],
  filter: TemplateFilter = {}
): GalleryTemplate[] {
  const kw = String(filter.keyword || '').trim().toLowerCase();
  return templates.filter((t) => {
    if (filter.difficulty && t.difficulty !== filter.difficulty) return false;
    if (filter.market) {
      const markets = t.markets.length ? t.markets : ['a_share'];
      if (!markets.includes(filter.market)) return false;
    }
    if (kw) {
      const hay = `${t.name} ${t.description} ${t.dir}`.toLowerCase();
      if (!hay.includes(kw)) return false;
    }
    return true;
  });
}

/** 概览计数（页头徽章）：总数 / minibt 数 / 入门数 */
export function templateStats(templates: GalleryTemplate[]): {
  total: number;
  minibt: number;
  beginner: number;
} {
  return {
    total: templates.length,
    minibt: templates.filter((t) => t.isMinibt).length,
    beginner: templates.filter((t) => t.difficulty === 'beginner').length,
  };
}
