/** 资讯中心 2.1：多市场 + 模型选择 + KPI + 五大视图 + 右侧阅读。 */

import { Suspense, lazy, useEffect, useState } from 'react';
import { Spin } from 'antd';
import { modelTrainingService } from '../../../services/modelTrainingService';
import { MarketRail } from '../components/MarketRail';
import { ModelPicker, type ModelOption } from '../components/ModelPicker';
import { KpiStrip } from '../components/KpiStrip';
import { DailyInferenceBoard } from '../components/DailyInferenceBoard';
import { SectorRadarBoard } from '../components/SectorRadarBoard';
import { TopicRadarBoard } from '../components/TopicRadarBoard';
import { StockDossier } from '../components/StockDossier';
import { SubscriptionsBoard } from '../components/SubscriptionsBoard';
import { MethodologyDrawer } from '../components/MethodologyDrawer';
import { ArticleDrawer } from '../components/ArticleDrawer';
import {
  newsIntelService,
  type MarketKey,
  type NewsIntelItem,
  type OverviewData,
} from '../services/newsIntelService';
import '../styles/news-intel.css';

const NewsPanel = lazy(() =>
  import('../../news/components/NewsPanel').then((m) => ({ default: m.NewsPanel })),
);

type View = 'daily' | 'sector' | 'topics' | 'stock' | 'subs' | 'library';

interface ViewDef {
  key: View;
  label: string;
  hint: string;
}

function viewsFor(market: MarketKey): ViewDef[] {
  if (market === 'FUTURES') {
    return [
      { key: 'topics', label: '商品雷达', hint: '品种新闻热度' },
      { key: 'library', label: '全库信息流', hint: '旧版三栏阅读器' },
    ];
  }
  return [
    { key: 'daily', label: '每日推理', hint: '模型当日榜单 × 新闻' },
    {
      key: market === 'CN' ? 'sector' : 'topics',
      label: market === 'CN' ? '板块雷达' : '主题雷达',
      hint: market === 'CN' ? '概念/行业板块' : 'key_terms 主题',
    },
    { key: 'stock', label: '个股档案', hint: '时间优先的全部新闻' },
    { key: 'subs', label: '我的订阅', hint: '实盘自动订阅 + 合并动态' },
    { key: 'library', label: '全库信息流', hint: '旧版三栏阅读器' },
  ];
}

export function NewsIntelPage() {
  const [market, setMarket] = useState<MarketKey>('CN');
  const [view, setView] = useState<View>('daily');
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelId, setModelId] = useState<string | null>(null);
  const [modelName, setModelName] = useState('');
  const [dossierCode, setDossierCode] = useState<string | null>(null);
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [article, setArticle] = useState<{ item: NewsIntelItem; code: string | null } | null>(null);
  const [methodOpen, setMethodOpen] = useState(false);

  useEffect(() => {
    const defs = viewsFor(market);
    setView((prev) => (defs.some((v) => v.key === prev) ? prev : defs[0].key));
    setDossierCode(null);
  }, [market]);

  useEffect(() => {
    if (market === 'FUTURES') {
      setModels([]);
      setModelId(null);
      setModelName('');
      return;
    }
    let cancelled = false;
    setModelsLoading(true);
    Promise.all([
      modelTrainingService.listUserModels(false, market).catch(() => ({ items: [], total: 0 })),
      modelTrainingService.listSystemModels(market).catch(() => []),
    ])
      .then(([user, system]) => {
        if (cancelled) return;
        const options: ModelOption[] = [];
        const seen = new Set<string>();
        for (const m of system) {
          if (!seen.has(m.model_id)) {
            seen.add(m.model_id);
            options.push({
              id: m.model_id,
              name: m.display_name || m.model_id,
              market,
              source: 'system',
            });
          }
        }
        let userDefault: string | null = null;
        for (const m of user.items || []) {
          if (m.is_default) userDefault = m.model_id;
          if (seen.has(m.model_id)) continue;
          seen.add(m.model_id);
          const meta = (m.metadata_json || {}) as Record<string, unknown>;
          const display = String(meta.display_name || meta.name || m.model_id);
          options.push({ id: m.model_id, name: display, market, source: 'user' });
        }
        setModels(options);
        const preferred =
          userDefault && options.some((x) => x.id === userDefault)
            ? userDefault
            : options[0]?.id || null;
        setModelId(preferred);
        setModelName(options.find((x) => x.id === preferred)?.name || '');
      })
      .finally(() => {
        if (!cancelled) setModelsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  useEffect(() => {
    let cancelled = false;
    setOverviewLoading(true);
    newsIntelService
      .overview(market, 1)
      .then((r) => {
        if (!cancelled) setOverview(r);
      })
      .catch(() => {
        if (!cancelled) setOverview(null);
      })
      .finally(() => {
        if (!cancelled) setOverviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  const openStock = (code: string) => {
    setDossierCode(code);
    setView('stock');
  };
  const openArticle = (item: NewsIntelItem, code?: string | null) => {
    setArticle({ item, code: code ?? null });
  };
  const changeModel = (id: string) => {
    setModelId(id);
    setModelName(models.find((x) => x.id === id)?.name || id);
  };

  const views = viewsFor(market);
  const tabsNode = (
    <nav className="ni-tabs" aria-label="资讯视图">
      {views.map((item) => (
        <button
          type="button"
          key={item.key}
          className={`ni-tab ${view === item.key ? 'active' : ''}`}
          onClick={() => setView(item.key)}
          title={item.hint}
          aria-current={view === item.key ? 'page' : undefined}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );

  return (
    <div className={`ni-page ni-market-${market.toLowerCase()}`}>
      <header className="ni-header">
        <div className="ni-brand">
          <div className="ni-title">
            资讯中心 <em>NEWS DESK</em>
          </div>
          <div className="ni-subtitle">研究口径 2026-11-18 · 权重排序 · 右侧阅读</div>
        </div>
        <MarketRail value={market} onChange={setMarket} />
        {view !== 'library' && (
          <KpiStrip market={market} data={overview} loading={overviewLoading} />
        )}
        <div className="ni-header-controls">
          <button type="button" className="ni-ghost-btn" onClick={() => setMethodOpen(true)}>
            研究口径
          </button>
          <ModelPicker
            options={models}
            value={modelId}
            onChange={changeModel}
            loading={modelsLoading}
            disabled={market === 'FUTURES' || !models.length}
            disabledHint={market === 'FUTURES' ? '该市场暂不接推理' : '加载模型…'}
          />
        </div>
      </header>

      {view !== 'library' && tabsNode}

      <main className="ni-body">
        {view === 'daily' && (
          <DailyInferenceBoard
            market={market}
            modelId={modelId}
            modelName={modelName}
            onOpenStock={openStock}
            onOpenArticle={openArticle}
            onOpenMethod={() => setMethodOpen(true)}
          />
        )}
        {view === 'sector' && market === 'CN' && (
          <SectorRadarBoard onOpenStock={openStock} onOpenArticle={openArticle} />
        )}
        {view === 'topics' && (
          <TopicRadarBoard
            market={market}
            onOpenStock={openStock}
            onOpenArticle={openArticle}
          />
        )}
        {view === 'stock' && (
          <StockDossier
            initialCode={dossierCode}
            market={market}
            modelId={modelId}
            modelName={modelName}
            onOpenArticle={openArticle}
            onOpenMethod={() => setMethodOpen(true)}
          />
        )}
        {view === 'subs' && (
          <SubscriptionsBoard market={market} onOpenStock={openStock} onOpenArticle={openArticle} />
        )}
        {view === 'library' && (
          <Suspense
            fallback={
              <div className="ni-loading">
                <Spin />
              </div>
            }
          >
            <NewsPanel embedded tabsSlot={tabsNode} />
          </Suspense>
        )}
      </main>

      <ArticleDrawer
        open={!!article}
        item={article?.item || null}
        market={market}
        code={article?.code || null}
        onClose={() => setArticle(null)}
      />

      <MethodologyDrawer open={methodOpen} onClose={() => setMethodOpen(false)} />

      <footer className="ni-disclaimer">
        资讯标签与权重由规则/模型自动生成，仅用于研究与风险提示，不构成投资建议。
        新闻层不产生任何买入信号。
      </footer>
    </div>
  );
}

export default NewsIntelPage;
