/**
 * NewsPanel — 资讯监控
 *
 * 三栏自适应布局：
 *   左：订阅源树（可多选、可折叠）
 *   中：文章流（弹性宽度）
 *   右：正文（弹性宽度）
 *
 * 筛选：单排工具栏 + 可展开高级筛选面板
 * 轮询：10s 文章列表 / 60s 来源和统计
 */

import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { useDebounce } from '../../../hooks/useDebounce';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import {
  Badge,
  Button,
  DatePicker,
  Empty,
  Input,
  List,
  Modal,
  Pagination,
  Segmented,
  Select,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Tree,
  message,
  Space,
  Collapse,
  Avatar,
} from 'antd';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  BellOutlined,
  DeleteOutlined,
  FilterOutlined,
  FireOutlined,
  GlobalOutlined,
  LinkOutlined,
  MinusOutlined,
  ReloadOutlined,
  RiseOutlined,
  SearchOutlined,
  SortAscendingOutlined,
  SortDescendingOutlined,
  StarFilled,
  StarOutlined,
  SyncOutlined,
  ThunderboltOutlined,
  ClearOutlined,
  ClockCircleOutlined,
} from '@ant-design/icons';
import type { DataNode } from 'antd/es/tree';
import dayjs, { type Dayjs } from 'dayjs';
import {
  NewsArticle,
  NewsArticleDetail,
  NewsEnrichmentStats,
  NewsFolder,
  NewsHealthInfo,
  NewsSource,
  newsService,
} from '../services/newsService';
import '../styles/news-panel.css';
import { sanitizeHtml } from '../../../utils/sanitizeHtml';

const { Text, Title, Paragraph } = Typography;
const { RangePicker } = DatePicker;

const POLL_ARTICLES_MS = 10_000;
const POLL_SOURCES_MS = 60_000;

type FeedMode = 'all' | 'events' | 'starred';
type SentimentFilter = 'any' | 'bullish' | 'bearish' | 'neutral';
type SortMode = 'time_desc' | 'time_asc' | 'sentiment_bullish' | 'sentiment_bearish';

const COLOR_BULLISH = '#dc2626';
const COLOR_BEARISH = '#16a34a';
const COLOR_NEUTRAL = '#64748b';

const LS_KEY = 'news_panel_filters';

interface FilterState {
  feedMode: FeedMode;
  sentiment: SentimentFilter;
  strongOnly: boolean;
  sort: SortMode;
  keyword: string;
  datePreset: string;
  dateRange: [string | null, string | null];
  selectedSourceIds: number[];
  selectedIndustries: string[];
  selectedTickers: string[];
  selectedEventTags: string[];
  selectedCountries: string[];
  selectedRegions: string[];
  selectedProvinces: string[];
  selectedCities: string[];
  selectedPoliticians: string[];
  selectedVisits: string[];
  selectedDepartments: string[];
  selectedKeyTerms: string[];
  selectedDateEnts: string[];
  advancedOpen: boolean;
}

const defaultFilters: FilterState = {
  feedMode: 'all',
  sentiment: 'any',
  strongOnly: false,
  sort: 'time_desc',
  keyword: '',
  datePreset: 'all',
  dateRange: [null, null],
  selectedSourceIds: [],
  selectedIndustries: [],
  selectedTickers: [],
  selectedEventTags: [],
  selectedCountries: [],
  selectedRegions: [],
  selectedProvinces: [],
  selectedCities: [],
  selectedPoliticians: [],
  selectedVisits: [],
  selectedDepartments: [],
  selectedKeyTerms: [],
  selectedDateEnts: [],
  advancedOpen: false,
};

function loadFilters(): FilterState {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) return { ...defaultFilters, ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return { ...defaultFilters };
}

function saveFilters(state: FilterState) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(state));
  } catch { /* ignore */ }
}

// 新闻大类快捷导航（对应 finance_lexicon 事件大类，点击按 event_tags 筛选）
const QUICK_EVENT_CHIPS = [
  { label: '全部', value: '' },
  { label: '政策', value: '政策' },
  { label: '宏观', value: '宏观' },
  { label: '产业', value: '产业' },
  { label: '财报', value: '财报' },
  { label: '市场', value: '市场' },
  { label: '监管', value: '监管' },
  { label: '地缘', value: '地缘' },
  { label: '加密', value: '加密' },
  { label: '期货', value: '期货' },
];

const formatRelative = (iso?: string | null) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 5) return '刚刚';
  if (diff < 60) return `${Math.floor(diff)}秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}天前`;
  return d.toLocaleDateString('zh-CN');
};

const SORT_OPTIONS: { label: React.ReactNode; value: SortMode }[] = [
  { label: <span><SortDescendingOutlined /> 最新</span>, value: 'time_desc' },
  { label: <span><SortAscendingOutlined /> 最早</span>, value: 'time_asc' },
  { label: <span style={{ color: COLOR_BULLISH }}><RiseOutlined /> 利好强度</span>, value: 'sentiment_bullish' },
  { label: <span style={{ color: COLOR_BEARISH }}><ArrowDownOutlined /> 利空强度</span>, value: 'sentiment_bearish' },
];

// —— helper to count active filters ——
function countActiveFilters(f: FilterState): number {
  let n = 0;
  if (f.sentiment !== 'any') n++;
  if (f.strongOnly) n++;
  if (f.keyword.trim()) n++;
  if (f.dateRange[0] || f.dateRange[1]) n++;
  if (f.selectedSourceIds.length) n++;
  if (f.selectedIndustries.length) n++;
  if (f.selectedTickers.length) n++;
  if (f.selectedEventTags.length) n++;
  if (f.selectedCountries.length) n++;
  if (f.selectedRegions.length) n++;
  if (f.selectedProvinces.length) n++;
  if (f.selectedCities.length) n++;
  if (f.selectedPoliticians.length) n++;
  if (f.selectedVisits.length) n++;
  if (f.selectedDepartments.length) n++;
  if (f.selectedKeyTerms.length) n++;
  if (f.selectedDateEnts.length) n++;
  return n;
}

export const NewsPanel: React.FC = () => {
  // —— persisted filter state ——
  const [f, setF] = useState<FilterState>(loadFilters);
  const [debouncedKeyword] = useDebounce(f.keyword, 300);

  const updateF = useCallback((patch: Partial<FilterState>) => {
    const next = { ...f, ...patch };
    setF(next);
    saveFilters(next);
  }, [f]);

  // —— data state ——
  const [health, setHealth] = useState<NewsHealthInfo | null>(null);
  const [sources, setSources] = useState<NewsSource[]>([]);
  const [folders, setFolders] = useState<NewsFolder[]>([]);
  const [articles, setArticles] = useState<NewsArticle[]>([]);
  const [loading, setLoading] = useState(false);
  const [latestPublishedAt, setLatestPublishedAt] = useState<string | null>(null);
  const [lastSyncTick, setLastSyncTick] = useState<number>(Date.now());
  const [stats, setStats] = useState<NewsEnrichmentStats | null>(null);

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [totalArticles, setTotalArticles] = useState(0);

  const [selectedArticleId, setSelectedArticleId] = useState<number | null>(null);
  const [articleDetail, setArticleDetail] = useState<NewsArticleDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([]);
  const [checkedKeys, setCheckedKeys] = useState<React.Key[]>([]);
  const [_, forceTick] = useState(0);

  // —— rebuild / purge state ——
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildingAll, setRebuildingAll] = useState(false);
  const [purging, setPurging] = useState(false);
  const [rebuildProgress, setRebuildProgress] = useState<{
    running: boolean;
    total: number;
    processed: number;
    ok: number;
    failed: number;
    elapsed_seconds?: number;
    eta_seconds?: number | null;
  } | null>(null);
  const rebuildPollRef = useRef<number | null>(null);

  // —— layout drag ——
  const [leftWidth, setLeftWidth] = useState(18);
  const [midWidth, setMidWidth] = useState(42);
  const dragRef = useRef<{ side: 'left' | 'right'; startX: number; startLeft: number; startMid: number } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const pollTimer = useRef<number | null>(null);

  // —— data fetching ——
  const checkHealth = useCallback(async () => {
    try {
      setHealth(await newsService.health());
    } catch {
      setHealth({ huntly_status: 'unreachable', huntly_base_url: '?' });
    }
  }, []);

  const loadSources = useCallback(async () => {
    try {
      const { sources: srcs, folders: flds } = await newsService.listSources();
      setSources(srcs);
      setFolders(flds);
    } catch {
      setSources([]);
      setFolders([]);
    }
  }, []);

  const loadArticles = useCallback(async () => {
    setLoading(true);
    try {
      const params: any = {
        keyword: debouncedKeyword || undefined,
        only_financial_event: f.feedMode === 'events',
        page: currentPage,
        page_size: pageSize,
        sort: f.sort,
      };
      if (f.feedMode === 'starred') params.starred = true;
      if (f.selectedSourceIds.length === 1) {
        params.source_id = f.selectedSourceIds[0];
      } else if (f.selectedSourceIds.length > 1) {
        params.source_ids = f.selectedSourceIds.join(',');
      }
      if (f.sentiment !== 'any') params.sentiment = f.sentiment;
      if (f.selectedIndustries.length) params.industries = f.selectedIndustries.join(',');
      if (f.selectedTickers.length) params.tickers = f.selectedTickers.join(',');
      if (f.selectedEventTags.length) params.event_tags = f.selectedEventTags.join(',');
      if (f.selectedCountries.length) params.countries = f.selectedCountries.join(',');
      if (f.selectedRegions.length) params.regions = f.selectedRegions.join(',');
      if (f.selectedKeyTerms.length) params.key_terms = f.selectedKeyTerms.join(',');
      if (f.selectedDateEnts.length) params.date_entities = f.selectedDateEnts.join(',');
      if (f.selectedProvinces.length) params.provinces = f.selectedProvinces.join(',');
      if (f.selectedCities.length) params.cities = f.selectedCities.join(',');
      if (f.selectedPoliticians.length) params.politicians = f.selectedPoliticians.join(',');
      if (f.selectedVisits.length) params.visits = f.selectedVisits.join(',');
      if (f.selectedDepartments.length) params.departments = f.selectedDepartments.join(',');
      if (f.strongOnly) params.strong_only = true;
      if (f.dateRange[0]) params.since = dayjs(f.dateRange[0]).startOf('day').toISOString();
      if (f.dateRange[1]) params.until = dayjs(f.dateRange[1]).endOf('day').toISOString();

      const r = await newsService.listArticles(params);
      setArticles(r.articles ?? []);
      setTotalArticles(r.total ?? (r.articles?.length ?? 0));
      setLatestPublishedAt(r.latest_published_at ?? null);
      setLastSyncTick(Date.now());
    } catch {
      setArticles([]);
    } finally {
      setLoading(false);
    }
  }, [f.feedMode, f.sort, f.sentiment, f.strongOnly, f.selectedSourceIds, f.selectedIndustries, f.selectedTickers, f.selectedEventTags, f.selectedCountries, f.selectedRegions, f.selectedKeyTerms, f.selectedDateEnts, f.selectedProvinces, f.selectedCities, f.selectedPoliticians, f.selectedVisits, f.selectedDepartments, f.dateRange, debouncedKeyword, currentPage, pageSize]);

  const loadStats = useCallback(async () => {
    try {
      const params: any = {};
      if (f.sentiment !== 'any') params.sentiment = f.sentiment;
      if (f.selectedIndustries.length) params.industries = f.selectedIndustries.join(',');
      if (f.selectedTickers.length) params.tickers = f.selectedTickers.join(',');
      if (f.selectedEventTags.length) params.event_tags = f.selectedEventTags.join(',');
      if (f.selectedCountries.length) params.countries = f.selectedCountries.join(',');
      if (f.selectedRegions.length) params.regions = f.selectedRegions.join(',');
      if (f.selectedKeyTerms.length) params.key_terms = f.selectedKeyTerms.join(',');
      if (f.selectedDateEnts.length) params.date_entities = f.selectedDateEnts.join(',');
      if (f.selectedProvinces.length) params.provinces = f.selectedProvinces.join(',');
      if (f.selectedCities.length) params.cities = f.selectedCities.join(',');
      if (f.selectedPoliticians.length) params.politicians = f.selectedPoliticians.join(',');
      if (f.selectedVisits.length) params.visits = f.selectedVisits.join(',');
      if (f.selectedDepartments.length) params.departments = f.selectedDepartments.join(',');
      if (f.strongOnly) params.strong_only = true;
      if (f.keyword?.trim()) params.keyword = debouncedKeyword.trim();
      if (f.dateRange[0]) params.since = dayjs(f.dateRange[0]).startOf('day').toISOString();
      if (f.dateRange[1]) params.until = dayjs(f.dateRange[1]).endOf('day').toISOString();
      const s = await newsService.enrichmentStats(params);
      setStats(s);
    } catch {
      setStats(null);
    }
  }, [f.sentiment, f.strongOnly, f.selectedIndustries, f.selectedTickers, f.selectedEventTags, f.selectedCountries, f.selectedRegions, f.selectedKeyTerms, f.selectedDateEnts, f.selectedProvinces, f.selectedCities, f.selectedPoliticians, f.selectedVisits, f.selectedDepartments, debouncedKeyword, f.dateRange]);

  // —— rebuild handlers ——
  const stopRebuildPolling = useCallback(() => {
    if (rebuildPollRef.current) { window.clearInterval(rebuildPollRef.current); rebuildPollRef.current = null; }
  }, []);

  const startRebuildPolling = useCallback(() => {
    stopRebuildPolling();
    rebuildPollRef.current = window.setInterval(async () => {
      try {
        const p = await newsService.getRebuildProgress();
        setRebuildProgress(p);
        if (!p.running) {
          stopRebuildPolling();
          setRebuildingAll(false);
          message.success({
            content: `全量重建完成: ${(p.elapsed_seconds / 60).toFixed(1)} 分钟`,
            key: 'rebuild-all', duration: 6,
          });
          await loadArticles();
          await loadStats();
        }
      } catch { /* ignore */ }
    }, 3000);
  }, [stopRebuildPolling, loadArticles, loadStats]);

  const handleRebuildTags = useCallback(async () => {
    Modal.confirm({
      title: '重建标签',
      content: '对未 enrichment 的文章重新提取标签。确认继续？',
      okText: '开始重建', cancelText: '取消',
      onOk: async () => {
        setRebuilding(true);
        try {
          const r = await newsService.runEnrichmentNow(2000);
          message.success({ content: `完成: ${r.written} 篇`, key: 'rebuild-tags', duration: 3 });
          await loadArticles(); await loadStats();
        } catch { message.error({ content: '重建失败', key: 'rebuild-tags' }); }
        finally { setRebuilding(false); }
      },
    });
  }, [loadArticles, loadStats]);

  const handleRebuildAll = useCallback(() => {
    Modal.confirm({
      title: '一键重建全部标签',
      width: 480,
      content: (
        <div>
          <p>对 <b>全部历史文章</b> 强制重建标签 (后台异步)。</p>
          <p style={{ color: '#94a3b8', fontSize: 12 }}>预计 30-90 分钟，期间可继续浏览。</p>
        </div>
      ),
      okText: '开始强制重建', cancelText: '取消',
      okButtonProps: { type: 'primary', danger: true },
      onOk: async () => {
        setRebuildingAll(true);
        try {
          const r = await newsService.rebuildAllEnrichment(true);
          if (r.started) {
            message.success({ content: `已启动 (共 ${r.total || '?'} 篇)`, key: 'rebuild-all' });
          } else {
            message.info({ content: `已在运行 (${r.processed}/${r.total})`, key: 'rebuild-all' });
          }
          setRebuildProgress(r as any);
          startRebuildPolling();
        } catch { setRebuildingAll(false); message.error({ content: '启动失败', key: 'rebuild-all' }); }
      },
    });
  }, [startRebuildPolling]);

  const handlePurgeOld = useCallback(() => {
    Modal.confirm({
      title: '清理 24 小时前资讯',
      width: 440,
      content: (
        <div>
          <p>将删除 <b>超过 24 小时</b> 的资讯正文与标签（Huntly + enrichment）。</p>
          <p style={{ color: '#ef4444', fontSize: 12 }}>此操作不可恢复，近期（24h 内）文章不受影响。</p>
        </div>
      ),
      okText: '确认清理', cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setPurging(true);
        try {
          const r = await newsService.purgeOld(24);
          message.success({ content: `已清理 ${r.deleted_pages} 篇 / 标签 ${r.deleted_enrichments} 条`, key: 'purge-old', duration: 4 });
          await Promise.all([loadArticles(), loadStats(), loadSources()]);
        } catch (e: any) {
          message.error({ content: e?.response?.data?.detail || '清理失败', key: 'purge-old' });
        } finally { setPurging(false); }
      },
    });
  }, [loadArticles, loadStats, loadSources]);

  // —— effects ——
  useEffect(() => { checkHealth(); loadSources(); loadStats(); }, [checkHealth, loadSources, loadStats]);

  // auto-resume rebuild progress
  useEffect(() => {
    (async () => {
      try {
        const p = await newsService.getRebuildProgress();
        if (p.running) { setRebuildProgress(p); setRebuildingAll(true); startRebuildPolling(); }
        else if (p.total > 0) setRebuildProgress(p);
      } catch { /* ignore */ }
    })();
    return () => { if (rebuildPollRef.current) window.clearInterval(rebuildPollRef.current); };
  }, [startRebuildPolling]);

  // articles polling
  useEffect(() => {
    loadArticles();
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    pollTimer.current = window.setInterval(loadArticles, POLL_ARTICLES_MS);
    return () => {
      if (pollTimer.current) window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    };
  }, [loadArticles]);

  // sources polling
  useEffect(() => {
    loadSources();
    const srcTimer = window.setInterval(loadSources, POLL_SOURCES_MS);
    return () => window.clearInterval(srcTimer);
  }, [loadSources]);

  // stats debounced refresh on filter change
  useEffect(() => {
    const t = window.setTimeout(loadStats, 500);
    return () => window.clearTimeout(t);
  }, [loadStats]);

  // reset page on filter change
  useEffect(() => { setCurrentPage(1); }, [
    f.feedMode, f.sort, f.sentiment, f.strongOnly, f.keyword,
    f.selectedSourceIds, f.selectedIndustries, f.selectedTickers,
    f.selectedEventTags, f.selectedCountries, f.selectedRegions,
    f.selectedKeyTerms, f.selectedDateEnts, f.selectedProvinces,
    f.selectedCities, f.selectedPoliticians, f.selectedVisits,
    f.selectedDepartments, f.dateRange,
  ]);

  // drag
  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragRef.current || !containerRef.current) return;
      e.preventDefault();
      const rect = containerRef.current.getBoundingClientRect();
      const totalW = rect.width;
      const dx = e.clientX - dragRef.current.startX;
      const dPct = (dx / totalW) * 100;
      const minPct = 8;
      if (dragRef.current.side === 'left') {
        const newLeft = Math.max(minPct, Math.min(40, dragRef.current.startLeft + dPct));
        setLeftWidth(newLeft);
      } else {
        const newMid = Math.max(20, Math.min(65, dragRef.current.startMid + dPct));
        setMidWidth(newMid);
      }
    };
    const onMouseUp = () => {
      dragRef.current = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => { window.removeEventListener('mousemove', onMouseMove); window.removeEventListener('mouseup', onMouseUp); };
  }, []);

  // relative-time tick
  useEffect(() => {
    const t = window.setInterval(() => { forceTick(((x: number) => x + 1) as unknown as number); }, 1000);
    return () => window.clearInterval(t);
  }, []);

  // article detail
  useEffect(() => {
    if (selectedArticleId == null) { setArticleDetail(null); return; }
    setDetailLoading(true);
    newsService.getArticle(selectedArticleId)
      .then((d) => {
        setArticleDetail(d);
        if (!d.read) newsService.markRead(selectedArticleId, true).catch(() => undefined);
      })
      .catch(() => setArticleDetail(null))
      .finally(() => setDetailLoading(false));
  }, [selectedArticleId]);

  // —— star ——
  const handleStar = useCallback(async (article: NewsArticle, ev: React.MouseEvent) => {
    ev.stopPropagation();
    const next = !article.starred;
    try {
      await newsService.toggleStar(article.id, next);
      setArticles(((prev: NewsArticle[]) => prev.map((a: NewsArticle) => (a.id === article.id ? { ...a, starred: next } : a))) as unknown as NewsArticle[]);
      if (selectedArticleId === article.id && articleDetail) {
        setArticleDetail({ ...articleDetail, starred: next });
      }
    } catch { message.error('操作失败'); }
  }, [selectedArticleId, articleDetail]);

  // —— clear all filters ——
  const clearAllFilters = useCallback(() => {
    updateF({
      sentiment: 'any', strongOnly: false, keyword: '',
      datePreset: 'all', dateRange: [null, null],
      selectedSourceIds: [], selectedIndustries: [], selectedTickers: [],
      selectedEventTags: [], selectedCountries: [], selectedRegions: [],
      selectedProvinces: [], selectedCities: [],
      selectedPoliticians: [], selectedVisits: [], selectedDepartments: [],
      selectedKeyTerms: [], selectedDateEnts: [],
    });
  }, [updateF]);

  const handleRefresh = useCallback(async () => {
    message.loading({ content: '刷新中...', key: 'news-refresh', duration: 0 });
    try {
      await Promise.all([loadArticles(), loadSources()]);
      message.success({ content: '已刷新', key: 'news-refresh' });
    } catch { message.error({ content: '刷新失败', key: 'news-refresh' }); }
  }, [loadArticles, loadSources]);

  // —— derived ——
  const totalUnread = useMemo(() => sources.reduce((acc, s) => acc + (s.unread_count || 0), 0), [sources]);

  // Build source tree — grouped by backend folders only
  const treeData: DataNode[] = useMemo(() => {
    const folderMap = new Map<number, NewsSource[]>();
    const unassigned: NewsSource[] = [];
    sources.forEach((s) => {
      const fid = s.folder_id ?? 0;
      if (!fid) { unassigned.push(s); return; }
      if (!folderMap.has(fid)) folderMap.set(fid, []);
      folderMap.get(fid)!.push(s);
    });

    const nodes: DataNode[] = folders
      .filter((fld) => folderMap.has(fld.folder_id))
      .map((fld) => {
        const items = folderMap.get(fld.folder_id)!;
        return {
          key: `folder-${fld.folder_id}`,
          title: (
            <div className="news-tree-node">
              <span className="news-tree-label">{fld.folder_name || '未分组'}</span>
              <Text type="secondary" style={{ fontSize: 11 }}>{items.length}</Text>
              {fld.unread_count > 0 && <Badge count={fld.unread_count} size="small" />}
            </div>
          ),
          children: items.map((s) => ({
            key: `source-${s.source_id}`,
            title: (
              <div className="news-tree-node">
                <Avatar src={s.site_avatar_url} size={16} style={{ flexShrink: 0 }}>{(s.source_name || '?')[0]}</Avatar>
                <span className="news-tree-label">{s.source_name}</span>
                {(s.unread_count ?? 0) > 0 && <Badge count={s.unread_count} size="small" />}
              </div>
            ),
            isLeaf: true,
          })),
        };
      });

    // sources without a folder
    if (unassigned.length) {
      nodes.push({
        key: 'folder-0',
        title: (
          <div className="news-tree-node">
            <span className="news-tree-label">未分组</span>
            <Text type="secondary" style={{ fontSize: 11 }}>{unassigned.length}</Text>
          </div>
        ),
        children: unassigned.map((s) => ({
          key: `source-${s.source_id}`,
          title: (
            <div className="news-tree-node">
              <Avatar src={s.site_avatar_url} size={16} style={{ flexShrink: 0 }}>{(s.source_name || '?')[0]}</Avatar>
              <span className="news-tree-label">{s.source_name}</span>
              {(s.unread_count ?? 0) > 0 && <Badge count={s.unread_count} size="small" />}
            </div>
          ),
          isLeaf: true,
        })),
      });
    }

    return nodes;
  }, [sources, folders]);

  // Init expanded from treeData
  useEffect(() => {
    if (treeData.length > 0 && expandedKeys.length === 0) {
      setExpandedKeys(treeData.map((node) => node.key));
    }
  }, [treeData, expandedKeys.length]);

  // Sync tree checked keys from filter state
  useEffect(() => {
    setCheckedKeys(f.selectedSourceIds.map((id) => `source-${id}`));
  }, [f.selectedSourceIds]);

  const activeFilterCount = countActiveFilters(f);

  // —— helper: add/remove filter ——
  const addFilter = useCallback((key: keyof FilterState, value: string) => {
    updateF({
      [key]: (Array.isArray((f as any)[key])
        ? [...new Set([...(f as any)[key] as string[], value])]
        : value
      ) as any,
    } as any);
  }, [f, updateF]);

  const removeFilter = useCallback((key: keyof FilterState, value: string) => {
    const arr = (f as any)[key] as string[];
    updateF({ [key]: arr.filter((v: string) => v !== value) } as any);
  }, [f, updateF]);

  // —— render active filter chips ——
  const renderChips = () => {
    const chips: { label: string; key: keyof FilterState; value: string; color: string }[] = [];
    if (f.sentiment !== 'any') chips.push({ label: f.sentiment === 'bullish' ? '利好' : f.sentiment === 'bearish' ? '利空' : '中性', key: 'sentiment' as any, value: f.sentiment, color: f.sentiment === 'bullish' ? COLOR_BULLISH : f.sentiment === 'bearish' ? COLOR_BEARISH : COLOR_NEUTRAL });
    if (f.strongOnly) chips.push({ label: '强信号', key: 'strongOnly' as any, value: 'true', color: '#ef4444' });
    if (f.feedMode !== 'all') chips.push({ label: f.feedMode === 'events' ? '财务事件' : '收藏', key: 'feedMode' as any, value: f.feedMode, color: '#6366f1' });
    f.selectedSourceIds.forEach((id) => {
      const src = sources.find((s) => s.source_id === id);
      chips.push({ label: src?.source_name || `源#${id}`, key: 'selectedSourceIds', value: String(id), color: '#3b82f6' });
    });
    f.selectedIndustries.forEach((v) => chips.push({ label: v, key: 'selectedIndustries', value: v, color: '#6366f1' }));
    f.selectedTickers.forEach((v) => chips.push({ label: v, key: 'selectedTickers', value: v, color: '#8b5cf6' }));
    f.selectedEventTags.forEach((v) => chips.push({ label: v, key: 'selectedEventTags', value: v, color: '#f59e0b' }));
    f.selectedCountries.forEach((v) => chips.push({ label: v, key: 'selectedCountries', value: v, color: '#a855f7' }));
    f.selectedRegions.forEach((v) => chips.push({ label: v, key: 'selectedRegions', value: v, color: '#06b6d4' }));
    f.selectedKeyTerms.forEach((v) => chips.push({ label: v, key: 'selectedKeyTerms', value: v, color: '#ec4899' }));
    f.selectedProvinces.forEach((v) => chips.push({ label: v, key: 'selectedProvinces', value: v, color: '#f97316' }));
    f.selectedCities.forEach((v) => chips.push({ label: v, key: 'selectedCities', value: v, color: '#eab308' }));
    f.selectedPoliticians.forEach((v) => chips.push({ label: v, key: 'selectedPoliticians', value: v, color: '#ef4444' }));
    f.selectedVisits.forEach((v) => chips.push({ label: v, key: 'selectedVisits', value: v, color: '#84cc16' }));
    f.selectedDepartments.forEach((v) => chips.push({ label: v, key: 'selectedDepartments', value: v, color: '#3b82f6' }));
    f.selectedDateEnts.forEach((v) => chips.push({ label: v, key: 'selectedDateEnts', value: v, color: '#64748b' }));

    return (
      <div className="news-active-chips">
        {chips.slice(0, 12).map((c, i) => (
          <Tag
            key={`${c.key}-${c.value}-${i}`}
            closable
            color={c.color}
            onClose={() => {
              if (c.key === 'sentiment') updateF({ sentiment: 'any' });
              else if (c.key === 'strongOnly') updateF({ strongOnly: false });
              else if (c.key === 'feedMode') updateF({ feedMode: 'all' });
              else removeFilter(c.key, c.value);
            }}
            style={{ marginBottom: 4 }}
          >
            {c.label}
          </Tag>
        ))}
        {chips.length > 12 && <Text type="secondary" style={{ fontSize: 12 }}>+{chips.length - 12} 更多</Text>}
      </div>
    );
  };

  // —— render ——
  return (
    <div className="news-panel">
      <div className="news-frame">
        {/* ===== Toolbar (2-row aligned) ===== */}
        <div className="news-toolbar">
          {/* Row 1: header + actions */}
          <div className="news-toolbar-row news-toolbar-head">
            <div className="news-toolbar-left">
              <div className="w-9 h-9 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-2xl flex items-center justify-center shadow-md shrink-0">
                <BellOutlined style={{ color: '#ffffff', fontSize: 16 }} />
              </div>
              <Title level={5} style={{ margin: 0, fontSize: 16, fontWeight: 700, whiteSpace: 'nowrap' }}>RSS信息流</Title>
              <Tag color={health?.huntly_status === 'up' ? 'green' : 'red'} style={{ margin: 0 }}>{health?.huntly_status === 'up' ? '已连接' : '未连接'}</Tag>
              <Tooltip title={latestPublishedAt ? `最新发布于 ${new Date(latestPublishedAt).toLocaleString('zh-CN')}` : '暂无'}>
                <Tag icon={<SyncOutlined spin={loading} />} color="processing" style={{ margin: 0 }}>最新：{formatRelative(latestPublishedAt)}</Tag>
              </Tooltip>
              <Badge count={totalUnread} overflowCount={9999} style={{ backgroundColor: '#6366f1' }} className="news-toolbar-badge" />
            </div>
            <div className="news-toolbar-actions">
              <Tooltip title="立即刷新">
                <Button size="small" type="primary" ghost icon={<ReloadOutlined spin={loading} />} onClick={handleRefresh}>刷新</Button>
              </Tooltip>
              <Tooltip title="重建标签">
                <Button size="small" ghost danger icon={<SyncOutlined spin={rebuilding} />} loading={rebuilding} onClick={handleRebuildTags}>重建</Button>
              </Tooltip>
              <Tooltip title="一键重建全部">
                <Button size="small" type="primary" danger ghost icon={<SyncOutlined spin={rebuildingAll} />} loading={rebuildingAll} onClick={handleRebuildAll}>
                  {rebuildingAll && rebuildProgress && rebuildProgress.total > 0 ? `${rebuildProgress.processed}/${rebuildProgress.total}` : '全量'}
                </Button>
              </Tooltip>
              <Tooltip title="一键清理超过 24 小时的资讯（不可恢复）">
                <Button size="small" danger icon={<DeleteOutlined />} loading={purging} onClick={handlePurgeOld}>清理</Button>
              </Tooltip>
              {health?.huntly_base_url && (
                <Tooltip title="Huntly 后台">
                  <a href={`${SERVICE_ENDPOINTS.USER_SERVICE}/news/huntly-ui/`} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: '#6366f1', whiteSpace: 'nowrap' }}>
                    <LinkOutlined /> 后台
                  </a>
                </Tooltip>
              )}
            </div>
          </div>
          {/* Row 2: filters — always aligned */}
          <div className="news-toolbar-row news-toolbar-filters">
            <Segmented
              size="small"
              value={f.feedMode}
              onChange={(v) => updateF({ feedMode: v as FeedMode })}
              options={[
                { label: <span><GlobalOutlined /> 全部</span>, value: 'all' },
                { label: <span><ThunderboltOutlined /> 事件</span>, value: 'events' },
                { label: <span><StarFilled style={{ color: '#fbbf24', fontSize: 12 }} /> 收藏</span>, value: 'starred' },
              ]}
            />
            <Input
              allowClear
              size="small"
              prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
              placeholder="搜索标题/内容/股票/标签..."
              value={f.keyword}
              onChange={(e) => updateF({ keyword: e.target.value })}
              style={{ width: 220 }}
            />
            <Segmented
              size="small"
              value={f.sentiment}
              onChange={(v) => updateF({ sentiment: v as SentimentFilter })}
              options={[
                { label: '全部', value: 'any' },
                { label: <span style={{ color: COLOR_BULLISH }}>利好</span>, value: 'bullish' },
                { label: <span style={{ color: COLOR_BEARISH }}>利空</span>, value: 'bearish' },
                { label: <span style={{ color: COLOR_NEUTRAL }}>中性</span>, value: 'neutral' },
              ]}
            />
            <Tooltip title="仅显示强信号 (|情感分|>=0.5)">
              <Button size="small" type={f.strongOnly ? 'primary' : 'default'} danger={f.strongOnly}
                icon={<FireOutlined />} onClick={() => updateF({ strongOnly: !f.strongOnly })}>强信号</Button>
            </Tooltip>
            <div style={{ flex: 1 }} />
            <Segmented
              size="small"
              value={f.datePreset}
              onChange={(v) => {
                const today = dayjs().endOf('day');
                let range: [Dayjs | null, Dayjs | null] = null;
                switch (v) {
                  case 'all': range = null; break;
                  case '1d': range = [dayjs().startOf('day'), today]; break;
                  case '3d': range = [dayjs().subtract(2, 'day').startOf('day'), today]; break;
                  case '7d': range = [dayjs().subtract(6, 'day').startOf('day'), today]; break;
                  case '30d': range = [dayjs().subtract(29, 'day').startOf('day'), today]; break;
                }
                updateF({
                  datePreset: v as string,
                  dateRange: range ? [range[0]!.toISOString(), range[1]!.toISOString()] : [null, null],
                });
              }}
              options={[
                { label: '不限', value: 'all' },
                { label: '今日', value: '1d' },
                { label: '近3日', value: '3d' },
                { label: '近7日', value: '7d' },
                { label: '近30日', value: '30d' },
              ]}
            />
            <Select
              size="small"
              value={f.sort}
              onChange={(v) => updateF({ sort: v as SortMode })}
              options={SORT_OPTIONS.map((o) => ({ value: o.value, label: o.label as any }))}
              style={{ width: 130 }}
            />
            <Tooltip title={`高级筛选${activeFilterCount > 0 ? ` (${activeFilterCount} 项激活)` : ''}`}>
              <Button size="small" icon={<FilterOutlined />}
                type={activeFilterCount > 0 ? 'primary' : 'default'}
                ghost={activeFilterCount > 0}
                onClick={() => updateF({ advancedOpen: !f.advancedOpen })}>
                筛选{activeFilterCount > 0 ? `(${activeFilterCount})` : ''}
              </Button>
            </Tooltip>
          </div>
        </div>

      {/* ===== 分类导航：词典大类 + 全部高频事件标签（含文章数），点击按 event_tags 筛选 ===== */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 2, padding: '4px 16px', borderBottom: '1px solid rgba(226,232,240,0.8)', background: 'rgba(255,255,255,0.85)', flexWrap: 'wrap' }}>
        <Text type="secondary" style={{ fontSize: 12, marginRight: 6, whiteSpace: 'nowrap' }}>分类:</Text>
        {QUICK_EVENT_CHIPS.map((chip) => {
          const active = chip.value === '' ? f.selectedEventTags.length === 0 : f.selectedEventTags.includes(chip.value);
          const cnt = chip.value ? stats?.top_events.find((e) => e.name === chip.value)?.count : undefined;
          return (
            <Tag.CheckableTag
              key={chip.value || '__all'}
              checked={active}
              onChange={(c) => {
                if (chip.value === '') {
                  // 全部：清空所有事件筛选
                  updateF({ selectedEventTags: [] });
                } else if (c) {
                  addFilter('selectedEventTags', chip.value);
                } else {
                  removeFilter('selectedEventTags', chip.value);
                }
              }}
              style={{ fontSize: 12, padding: '2px 10px', borderRadius: 12, margin: 0, fontWeight: active ? 600 : 400 }}
            >
              {chip.label}
              {cnt != null && <span style={{ opacity: 0.5, fontWeight: 400 }}> ({cnt.toLocaleString()})</span>}
            </Tag.CheckableTag>
          );
        })}
        {/* 高频事件标签（词典大类之外，全部展示，含文章数） */}
        {(stats?.top_events ?? [])
          .filter((e) => !QUICK_EVENT_CHIPS.some((q) => q.value === e.name))
          .map((e) => {
            const active = f.selectedEventTags.includes(e.name);
            return (
              <Tag.CheckableTag
                key={`ev-${e.name}`}
                checked={active}
                onChange={(c) => {
                  if (c) addFilter('selectedEventTags', e.name);
                  else removeFilter('selectedEventTags', e.name);
                }}
                style={{ fontSize: 12, padding: '2px 10px', borderRadius: 12, margin: 0, fontWeight: active ? 600 : 400 }}
              >
                {e.name} <span style={{ opacity: 0.5, fontWeight: 400 }}>({e.count.toLocaleString()})</span>
              </Tag.CheckableTag>
            );
          })}
        {/* 当前筛选标签：紧跟分类 Tab 之后 */}
        {activeFilterCount > 0 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', marginLeft: 4 }}>
            <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>筛选:</Text>
            {renderChips()}
            <Button type="link" size="small" onClick={clearAllFilters} icon={<ClearOutlined />} style={{ padding: 0, fontSize: 12 }}>清除</Button>
          </div>
        )}
      </div>

      {/* ===== Advanced filter panel (collapsible) ===== */}
      {f.advancedOpen && (
        <div className="news-advanced-panel">
          <div className="news-advanced-row">
            <FilterSelect label="股票" value={f.selectedTickers} options={stats?.top_tickers ?? []} onChange={(v) => updateF({ selectedTickers: v })} showSearch />
            <FilterSelect label="行业" value={f.selectedIndustries} options={stats?.top_industries ?? []} onChange={(v) => updateF({ selectedIndustries: v })} showSearch />
            <FilterSelect label="国家" value={f.selectedCountries} options={stats?.top_countries ?? []} onChange={(v) => updateF({ selectedCountries: v })} showSearch />
            <FilterSelect label="地区" value={f.selectedRegions} options={stats?.top_regions ?? []} onChange={(v) => updateF({ selectedRegions: v })} showSearch />
            <FilterSelect label="关键词" value={f.selectedKeyTerms} options={stats?.top_key_terms ?? []} onChange={(v) => updateF({ selectedKeyTerms: v })} showSearch />
          </div>
          <div className="news-advanced-row">
            <FilterSelect label="省份" value={f.selectedProvinces} options={stats?.top_provinces ?? []} onChange={(v) => updateF({ selectedProvinces: v })} showSearch />
            <FilterSelect label="城市" value={f.selectedCities} options={stats?.top_cities ?? []} onChange={(v) => updateF({ selectedCities: v })} showSearch />
            <FilterSelect label="领导人" value={f.selectedPoliticians} options={stats?.top_politicians ?? []} onChange={(v) => updateF({ selectedPoliticians: v })} showSearch />
            <FilterSelect label="调研" value={f.selectedVisits} options={stats?.top_visits ?? []} onChange={(v) => updateF({ selectedVisits: v })} showSearch />
            <FilterSelect label="部门" value={f.selectedDepartments} options={stats?.top_departments ?? []} onChange={(v) => updateF({ selectedDepartments: v })} showSearch />
            <FilterSelect label="日期" value={f.selectedDateEnts} options={stats?.top_dates ?? []} onChange={(v) => updateF({ selectedDateEnts: v })} showSearch />
            <RangePicker size="small" value={f.dateRange[0] ? [dayjs(f.dateRange[0]), dayjs(f.dateRange[1])] as any : null}
              onChange={(v) => updateF({ dateRange: v ? [v[0]!.toISOString(), v[1]!.toISOString()] : [null, null] })}
              allowClear style={{ width: 230 }} />
          </div>
        </div>
      )}

      {/* ===== Main 3-panel container card ===== */}
      <div ref={containerRef} className="news-content-card">
        {/* Left: source tree */}
        <div className="news-left-panel" style={{ flex: `0 0 ${leftWidth}%` }}>
          {sources.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span style={{ fontSize: 12 }}>无订阅源</span>} style={{ marginTop: 60 }} />
          ) : (
            <Tree
              checkable
              blockNode
              treeData={treeData}
              checkedKeys={checkedKeys}
              expandedKeys={expandedKeys}
              onExpand={(keys) => setExpandedKeys(keys)}
              onCheck={(checked) => {
                const keys = (checked as React.Key[]).filter((k) => String(k).startsWith('source-'));
                const ids = keys.map((k) => Number(String(k).replace('source-', ''))).filter((n) => !Number.isNaN(n));
                updateF({ selectedSourceIds: ids });
              }}
              style={{ background: 'transparent', fontSize: 13 }}
            />
          )}
        </div>

        {/* Drag handle L-M */}
        <div className="news-drag-handle" onMouseDown={(e) => {
          e.preventDefault();
          dragRef.current = { side: 'left', startX: e.clientX, startLeft: leftWidth, startMid: midWidth };
          document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
        }}><div className="news-drag-bar" /></div>

        {/* Center: article list */}
        <div className="news-center-panel" style={{ flex: `0 0 ${midWidth}%` }}>
          {/* Sentiment stats bar */}
          {stats?.sentiment_counts && (() => {
            const total = (stats.sentiment_counts.bullish || 0) + (stats.sentiment_counts.bearish || 0) + (stats.sentiment_counts.neutral || 0);
            if (total === 0) return null;
            const pct = (n: number) => `${(n / total * 100).toFixed(0)}%`;
            return (
              <div style={{ padding: '6px 16px', borderBottom: '1px solid rgba(226, 232, 240, 0.8)', background: 'rgba(255, 255, 255, 0.85)', fontSize: 12, display: 'flex', gap: 14, color: '#64748b', alignItems: 'center', flexWrap: 'wrap' }}>
                {activeFilterCount > 0 && <Tag color="processing" style={{ fontSize: 11, margin: 0 }}>已筛选</Tag>}
                <span>共 <b style={{ color: '#6366f1' }}>{total.toLocaleString()}</b> 篇</span>
                <span style={{ color: COLOR_BULLISH }}>利好 {stats.sentiment_counts.bullish || 0}</span>
                <span style={{ color: COLOR_BEARISH }}>利空 {stats.sentiment_counts.bearish || 0}</span>
                <span style={{ color: COLOR_NEUTRAL }}>中性 {stats.sentiment_counts.neutral || 0}</span>
                {/* 情绪分布横条 */}
                <span style={{ flex: 1, maxWidth: 160, display: 'inline-flex', height: 6, borderRadius: 3, overflow: 'hidden', background: '#e2e8f0', minWidth: 90 }}>
                  <span style={{ width: pct(stats.sentiment_counts.bullish || 0), background: COLOR_BULLISH, height: '100%' }} />
                  <span style={{ width: pct(stats.sentiment_counts.bearish || 0), background: COLOR_BEARISH, height: '100%' }} />
                  <span style={{ flex: 1, background: '#cbd5e1', height: '100%' }} />
                </span>
              </div>
            );
          })()}
          {loading && articles.length === 0 ? (
            <div style={{ padding: 80, textAlign: 'center' }}><Spin /></div>
          ) : articles.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<span style={{ fontSize: 13 }}>{health?.huntly_status === 'up' ? '当前筛选无文章' : '资讯服务未连接'}</span>}
              style={{ marginTop: 80 }} />
          ) : (
            <>
              <div style={{ flex: 1, overflowY: 'auto' }}>
                <List size="small" dataSource={articles} renderItem={(a) => {
                  const active = selectedArticleId === a.id;
                  return (
                    <List.Item className={`news-article-item ${active ? 'news-article-active' : ''} ${a.read ? 'news-article-read' : ''}`}
                      onClick={() => setSelectedArticleId(a.id)}>
                      <div style={{ width: '100%' }}>
                        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
                          <Avatar src={a.thumbnail} size={28} style={{ flexShrink: 0, marginTop: 2 }}>{(a.source_name || '?')[0]}</Avatar>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 3, flexWrap: 'wrap' }}>
                              {a.is_financial_event && <Tag color="gold" style={{ margin: 0, fontSize: 10, padding: '0 4px', lineHeight: '16px' }}><ThunderboltOutlined /></Tag>}
                              {a.enrichment?.sentiment_label === 'bullish' && (
                                <Tag color="red" style={{ margin: 0, fontSize: 10, padding: '0 4px', lineHeight: '16px', fontWeight: (a.enrichment.sentiment_score ?? 0) >= 0.5 ? 700 : 500 }}>
                                  {(a.enrichment.sentiment_score ?? 0) >= 0.5 ? <FireOutlined /> : <RiseOutlined />} 利好
                                </Tag>
                              )}
                              {a.enrichment?.sentiment_label === 'bearish' && (
                                <Tag color="green" style={{ margin: 0, fontSize: 10, padding: '0 4px', lineHeight: '16px', fontWeight: (a.enrichment.sentiment_score ?? 0) <= -0.5 ? 700 : 500 }}>
                                  {(a.enrichment.sentiment_score ?? 0) <= -0.5 ? <FireOutlined /> : <ArrowDownOutlined />} 利空
                                </Tag>
                              )}
                              <Text className="news-article-title" style={{ fontSize: 14, fontWeight: a.read ? 400 : 600, lineHeight: 1.5, flex: 1 }}>
                                {a.title}
                              </Text>
                            </div>
                            {a.summary && (
                              <Text style={{ color: '#64748b', fontSize: 12, display: 'block', lineHeight: 1.5, marginBottom: 2 }}>
                                {a.summary.length > 100 ? `${a.summary.slice(0, 100)}...` : a.summary}
                              </Text>
                            )}
                            {/* Enrichment tags */}
                            {(a.enrichment && (a.enrichment.tickers.length > 0 || a.enrichment.industries.length > 0 || a.enrichment.event_tags.length > 0)) && (
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 2, marginTop: 2 }}>
                                {a.enrichment.tickers.slice(0, 3).map((t) => (
                                  <Tag key={`tk-${t}`} color="blue" className="news-clickable-tag" onClick={(e) => { e.stopPropagation(); addFilter('selectedTickers', t); }}>{t}</Tag>
                                ))}
                                {a.enrichment.industries.slice(0, 2).map((ind) => (
                                  <Tag key={`ind-${ind}`} color="geekblue" className="news-clickable-tag" onClick={(e) => { e.stopPropagation(); addFilter('selectedIndustries', ind); }}>{ind}</Tag>
                                ))}
                                {a.enrichment.event_tags.slice(0, 2).map((ev) => (
                                  <Tag key={`ev-${ev}`} color="orange" className="news-clickable-tag" onClick={(e) => { e.stopPropagation(); addFilter('selectedEventTags', ev); }}>{ev}</Tag>
                                ))}
                              </div>
                            )}
                            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2, fontSize: 11, color: '#94a3b8' }}>
                              <span>{a.source_name || '未知'}</span>
                              <span>·</span>
                              <ClockCircleOutlined style={{ fontSize: 10 }} />
                              <span>{formatRelative(a.published_at)}</span>
                            </div>
                          </div>
                          <Button type="text" size="small"
                            icon={a.starred ? <StarFilled style={{ color: '#fbbf24' }} /> : <StarOutlined style={{ color: '#cbd5e1' }} />}
                            onClick={(e) => handleStar(a, e)} />
                        </div>
                      </div>
                    </List.Item>
                  );
                }} />
              </div>
              <div className="news-pagination-bar news-pagination-bar--centered">
                <Pagination size="small" current={currentPage} pageSize={pageSize} total={totalArticles}
                  showSizeChanger showQuickJumper showLessItems
                  pageSizeOptions={['20', '50', '100', '200']}
                  onChange={(page, size) => { setCurrentPage(page); setPageSize(size); }}
                  onShowSizeChange={(_current, size) => { setCurrentPage(1); setPageSize(size); }} />
              </div>
            </>
          )}
        </div>

        {/* Drag handle M-R */}
        <div className="news-drag-handle" onMouseDown={(e) => {
          e.preventDefault();
          dragRef.current = { side: 'right', startX: e.clientX, startLeft: leftWidth, startMid: midWidth };
          document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
        }}><div className="news-drag-bar" /></div>

        {/* Right: article detail */}
        <div className="news-right-panel">
          {detailLoading ? (
            <div style={{ padding: 80, textAlign: 'center' }}><Spin /></div>
          ) : !articleDetail ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span style={{ fontSize: 12 }}>选择文章查看正文</span>} style={{ marginTop: 80 }} />
          ) : (
            <div>
              <Title level={4} style={{ marginTop: 0, lineHeight: 1.4, fontSize: 18 }}>{articleDetail.title}</Title>
              <div style={{ marginBottom: 14, fontSize: 13, color: '#64748b' }}>
                {articleDetail.source_name} · {formatRelative(articleDetail.published_at)}
                {articleDetail.url && <a href={articleDetail.url} target="_blank" rel="noreferrer" style={{ marginLeft: 10, color: '#6366f1' }}><LinkOutlined /> 原文</a>}
              </div>
              {articleDetail.is_financial_event && <Tag color="gold" icon={<ThunderboltOutlined />} style={{ marginBottom: 14 }}>财务事件</Tag>}

              {/* Enrichment block */}
              {articleDetail.enrichment && (articleDetail.enrichment.tickers.length > 0 || articleDetail.enrichment.industries.length > 0 || articleDetail.enrichment.sentiment_label) && (
                <div style={{ marginBottom: 16, padding: 12, background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 6, fontSize: 12 }}>
                  {articleDetail.enrichment.sentiment_label && (
                    <div style={{ marginBottom: 8 }}>
                      <Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>情感:</Text>
                      <Tag color={articleDetail.enrichment.sentiment_label === 'bullish' ? 'red' : articleDetail.enrichment.sentiment_label === 'bearish' ? 'green' : 'default'}
                        style={{ margin: 0, fontWeight: Math.abs(articleDetail.enrichment.sentiment_score ?? 0) >= 0.5 ? 700 : 500 }}>
                        {articleDetail.enrichment.sentiment_label === 'bullish' && <><RiseOutlined /> 利好</>}
                        {articleDetail.enrichment.sentiment_label === 'bearish' && <><ArrowDownOutlined /> 利空</>}
                        {articleDetail.enrichment.sentiment_label === 'neutral' && <><MinusOutlined /> 中性</>}
                        {articleDetail.enrichment.sentiment_score != null && ` ${articleDetail.enrichment.sentiment_score.toFixed(3)}`}
                      </Tag>
                    </div>
                  )}
                  {articleDetail.enrichment.tickers.length > 0 && <TagRow label="股票" items={articleDetail.enrichment.tickers} color="blue" onClick={(t) => { addFilter('selectedTickers', t); }} />}
                  {articleDetail.enrichment.industries.length > 0 && <TagRow label="行业" items={articleDetail.enrichment.industries} color="geekblue" onClick={(i) => { addFilter('selectedIndustries', i); }} />}
                  {articleDetail.enrichment.event_tags.length > 0 && <TagRow label="事件" items={articleDetail.enrichment.event_tags} color="orange" onClick={(e) => { addFilter('selectedEventTags', e); }} />}
                  {(articleDetail.enrichment.countries?.length ?? 0) > 0 && <TagRow label="国家" items={articleDetail.enrichment.countries!} color="purple" onClick={(c) => { addFilter('selectedCountries', c); }} />}
                  {(articleDetail.enrichment.regions?.length ?? 0) > 0 && <TagRow label="地区" items={articleDetail.enrichment.regions!} color="cyan" onClick={(r) => { addFilter('selectedRegions', r); }} />}
                  {(articleDetail.enrichment.key_terms?.length ?? 0) > 0 && <TagRow label="关键词" items={articleDetail.enrichment.key_terms!} color="magenta" onClick={(k) => { updateF({ keyword: k }); }} />}
                  {(articleDetail.enrichment.provinces?.length ?? 0) > 0 && <TagRow label="省份" items={articleDetail.enrichment.provinces!} color="volcano" onClick={(p) => { addFilter('selectedProvinces', p); }} />}
                  {(articleDetail.enrichment.cities?.length ?? 0) > 0 && <TagRow label="城市" items={articleDetail.enrichment.cities!} color="gold" onClick={(c) => { addFilter('selectedCities', c); }} />}
                  {(articleDetail.enrichment.politicians?.length ?? 0) > 0 && <TagRow label="领导人" items={articleDetail.enrichment.politicians!} color="red" onClick={(p) => { addFilter('selectedPoliticians', p); }} />}
                  {(articleDetail.enrichment.visits?.length ?? 0) > 0 && <TagRow label="调研" items={articleDetail.enrichment.visits!} color="lime" onClick={(v) => { addFilter('selectedVisits', v); }} />}
                  {(articleDetail.enrichment.departments?.length ?? 0) > 0 && <TagRow label="部门" items={articleDetail.enrichment.departments!} color="geekblue" onClick={(d) => { addFilter('selectedDepartments', d); }} />}
                  {/* Entity sentiments */}
                  {articleDetail.enrichment.entity_sentiments && Object.keys(articleDetail.enrichment.entity_sentiments).length > 0 && (
                    <div style={{ marginTop: 8 }}>
                      <Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>实体级情感:</Text>
                      {Object.entries(articleDetail.enrichment.entity_sentiments).sort(([, a], [, b]) => Math.abs(b) - Math.abs(a)).slice(0, 8).map(([key, score]) => {
                        const [, name] = key.split(':', 2);
                        const isPos = score > 0;
                        return <Tag key={`es-${key}`} color={isPos ? 'red' : 'green'} style={{ marginBottom: 4, fontSize: 11 }}>{name} {score > 0 ? '+' : ''}{score.toFixed(2)}</Tag>;
                      })}
                    </div>
                  )}
                </div>
              )}
              {/* 正文：对纯文本做关键词高亮（股票名/事件词标黄） */}
              {articleDetail.content_html ? (
                <div className="news-detail-content" style={{ fontSize: 14, lineHeight: 1.8 }} dangerouslySetInnerHTML={{ __html: sanitizeHtml(articleDetail.content_html) }} />
              ) : (
                <Paragraph style={{ fontSize: 14, lineHeight: 1.8, whiteSpace: 'pre-wrap' }}>
                  {highlightText(articleDetail.content || articleDetail.summary || '(正文为空)', articleDetail.enrichment)}
                </Paragraph>
              )}

              {/* 相关推荐：同股票/同事件/同行业 */}
              {(() => {
                const enr = articleDetail.enrichment;
                if (!enr) return null;
                const myTickers = new Set(enr.tickers || []);
                const myEvents = new Set(enr.event_tags || []);
                const myInd = new Set(enr.industries || []);
                const related = (articles || []).filter((a) => a.id !== articleDetail.id).filter((a) => {
                  const ae = a.enrichment;
                  if (!ae) return false;
                  if ((ae.tickers || []).some((t) => myTickers.has(t))) return true;
                  if ((ae.event_tags || []).some((e) => myEvents.has(e))) return true;
                  if ((ae.industries || []).some((i) => myInd.has(i))) return true;
                  return false;
                }).slice(0, 8);
                if (related.length === 0) return null;
                return (
                  <div style={{ marginTop: 24, paddingTop: 16, borderTop: '1px solid #e2e8f0' }}>
                    <Text type="secondary" style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, display: 'block' }}>相关推荐</Text>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      {related.map((a) => (
                        <div
                          key={a.id}
                          onClick={() => setSelectedArticleId(a.id)}
                          style={{ cursor: 'pointer', padding: '6px 8px', borderRadius: 4, fontSize: 13, color: '#334155', lineHeight: 1.4 }}
                          onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = '#f1f5f9'; }}
                          onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent'; }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                            {a.enrichment?.sentiment_label === 'bullish' && <Tag color="red" style={{ margin: 0, fontSize: 10 }}>利好</Tag>}
                            {a.enrichment?.sentiment_label === 'bearish' && <Tag color="green" style={{ margin: 0, fontSize: 10 }}>利空</Tag>}
                            <span style={{ flex: 1 }}>{a.title}</span>
                            <span style={{ fontSize: 11, color: '#94a3b8', whiteSpace: 'nowrap' }}>{formatRelative(a.published_at)}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })()}
            </div>
          )}
        </div>
      </div>
    </div>
  </div>
  );
};

// —— Tiny helper components ——

/** 正文关键词高亮：把 enrichment 命中的股票/事件/关键词标黄（返回 JSX，安全转义） */
function highlightText(text: string, enrichment: any): React.ReactNode {
  if (!text || !enrichment) return text;
  const words: string[] = [];
  (enrichment.tickers || []).forEach((t: string) => words.push(t.split('.')[0]));  // 600519.SH → 600519
  (enrichment.event_tags || []).forEach((t: string) => words.push(t));
  (enrichment.key_terms || []).forEach((t: string) => words.push(t));
  (enrichment.industries || []).forEach((t: string) => words.push(t));
  const uniq = Array.from(new Set(words.filter((w) => w && w.length >= 2))).sort((a, b) => b.length - a.length);
  if (uniq.length === 0) return text;
  // 按长度降序替换，避免短词被长词覆盖；用文本节点拼接避免 XSS
  const parts: React.ReactNode[] = [];
  let remaining = text;
  let key = 0;
  while (remaining.length > 0) {
    let best = -1;
    let bestWord = '';
    for (const w of uniq) {
      const idx = remaining.indexOf(w);
      if (idx !== -1 && (best === -1 || idx < best)) {
        best = idx;
        bestWord = w;
      }
    }
    if (best === -1) { parts.push(remaining); break; }
    if (best > 0) parts.push(remaining.slice(0, best));
    parts.push(<mark key={key++} style={{ background: '#fef08a', padding: '0 1px', borderRadius: 2 }}>{bestWord}</mark>);
    remaining = remaining.slice(best + bestWord.length);
  }
  return parts;
}

const FilterSelect: React.FC<{
  label: string;
  value: string[];
  options: { name: string; count: number }[];
  onChange: (v: string[]) => void;
  showSearch?: boolean;
}> = ({ label, value, options, onChange, showSearch }) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
    <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{label}:</Text>
    <Select mode="multiple" allowClear size="small" placeholder={label}
      value={value} onChange={onChange}
      options={options.map((o) => ({ value: o.name, label: `${o.name} (${o.count})` }))}
      style={{ minWidth: 120, maxWidth: 200 }} maxTagCount="responsive"
      showSearch={showSearch}
      filterOption={showSearch ? (input, opt) => String(opt?.label || '').toLowerCase().includes(input.toLowerCase()) : undefined}
    />
  </div>
);

const TagRow: React.FC<{
  label: string;
  items: string[];
  color: string;
  onClick?: (item: string) => void;
}> = ({ label, items, color, onClick }) => (
  <div style={{ marginBottom: 6 }}>
    <Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>{label}:</Text>
    {items.map((it) => (
      <Tag
        key={it}
        color={color}
        style={{ marginBottom: 4, fontSize: 11, cursor: onClick ? 'pointer' : 'default' }}
        onClick={onClick ? (e) => { e.stopPropagation(); onClick(it); } : undefined}
      >
        {it}
      </Tag>
    ))}
  </div>
);

export default NewsPanel;
