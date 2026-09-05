import { describe, expect, it } from 'vitest';
import {
  QLIB_STRATEGY_TEMPLATES,
  StrategyTemplate,
  filterTemplatesByMarket,
} from '../qlibStrategyTemplates';

function makeTemplate(id: string, markets?: string[]): StrategyTemplate {
  return {
    id,
    name: id,
    description: '',
    category: 'basic',
    difficulty: 'beginner',
    code: 'STRATEGY_CONFIG = {}',
    params: [],
    markets,
  };
}

describe('filterTemplatesByMarket', () => {
  const cn = makeTemplate('standard_topk'); // 无标记 = 历史 A 股模板
  const cnTagged = makeTemplate('cn_momentum', ['a_share']);
  const hk = makeTemplate('hk_topk', ['hong_kong']);
  const us = makeTemplate('us_topk', ['us_stock']);
  const crypto = makeTemplate('btc', ['crypto']);
  const all = [cn, cnTagged, hk, us, crypto];

  it('CN 视图包含无标记历史模板与 a_share 模板，排除港股等', () => {
    const ids = filterTemplatesByMarket(all, 'CN').map((t) => t.id);
    expect(ids).toEqual(['standard_topk', 'cn_momentum']);
  });

  it('HK 视图只含 hong_kong 标记模板', () => {
    const ids = filterTemplatesByMarket(all, 'HK').map((t) => t.id);
    expect(ids).toEqual(['hk_topk']);
  });

  it('US / CRYPTO 视图按各自标记过滤', () => {
    expect(filterTemplatesByMarket(all, 'US').map((t) => t.id)).toEqual(['us_topk']);
    expect(filterTemplatesByMarket(all, 'CRYPTO').map((t) => t.id)).toEqual(['btc']);
  });

  it('大小写不敏感', () => {
    expect(filterTemplatesByMarket(all, 'hk').map((t) => t.id)).toEqual(['hk_topk']);
  });

  it('market 缺省或未知时不过滤（向后兼容全量）', () => {
    expect(filterTemplatesByMarket(all, undefined)).toHaveLength(all.length);
    expect(filterTemplatesByMarket(all, '')).toHaveLength(all.length);
    expect(filterTemplatesByMarket(all, 'FUTURES')).toHaveLength(all.length);
  });

  it('静态 fallback 列表（无 markets 字段）在 CN 视图全部保留', () => {
    const ids = filterTemplatesByMarket(QLIB_STRATEGY_TEMPLATES, 'CN');
    expect(ids).toHaveLength(QLIB_STRATEGY_TEMPLATES.length);
  });
});
