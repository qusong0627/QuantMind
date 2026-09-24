import React, { useState, useEffect } from 'react';
import {
  Search,
  Tag as TagIcon,
  Sparkles,
  ArrowUpRight,
  TrendingUp,
  TrendingDown,
  Layers,
  Hash,
  BarChart3,
} from 'lucide-react';
import { Input, Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { SERVICE_ENDPOINTS } from '../../../config/services';

const MARKET_ANALYSIS_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis`;

interface HotTagItem {
  name: string;
  type: string;
  count: number;
}

interface TagStats {
  total_sectors: number;
  total_stocks: number;
  avg_tags_per_stock: number;
  max_tags_per_stock: number;
  total_relations: number;
  hot_tags: HotTagItem[];
}

interface TagStockItem {
  symbol: string;
  name: string;
  close_price?: number | null;
  pct_change?: number | null;
  net_inflow?: number | null;
}

const MISSING = <span className="font-mono text-xs text-slate-300">—</span>;

export const TagLookupPanel: React.FC = () => {
  const [perspective, setPerspective] = useState<'stock' | 'sector'>('stock');
  const [sectorFilter, setSectorFilter] = useState<string>('全部');
  const [searchQuery, setSearchQuery] = useState('SH600000');
  const [loading, setLoading] = useState(false);
  const [statsLoading, setStatsLoading] = useState(false);

  const [stats, setStats] = useState<TagStats | null>(null);
  const [tagToStocksData, setTagToStocksData] = useState<TagStockItem[]>([]);
  const [activeTag, setActiveTag] = useState<string>('');
  const [stockToTagsData, setStockToTagsData] = useState<Record<string, string[]>>({});
  const [activeSymbol, setActiveSymbol] = useState<string>('');

  const sectorSubCategories = ['全部', '地区板块', '概念板块', '行业板块(一级)', '行业板块(二级)', '风格板块'];

  const fetchStats = async () => {
    setStatsLoading(true);
    try {
      const token = localStorage.getItem('access_token') || '';
      const res = await fetch(`${MARKET_ANALYSIS_API}/tags/stats?limit=30`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        if (data) setStats(data);
      }
    } catch (e) {
      console.warn('标签统计接口请求异常', e);
    }
    setStatsLoading(false);
  };

  const handleStockSearch = async (symbol: string) => {
    const q = symbol.trim();
    if (!q) return;
    setSearchQuery(q);
    setActiveSymbol(q);
    setLoading(true);
    try {
      const token = localStorage.getItem('access_token') || '';
      const res = await fetch(`${MARKET_ANALYSIS_API}/tags/by-stock?symbol=${encodeURIComponent(q)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        setStockToTagsData(data.tags || {});
      } else {
        setStockToTagsData({});
      }
    } catch (e) {
      console.warn('个股标签接口请求异常', e);
      setStockToTagsData({});
    }
    setLoading(false);
  };

  const handleTagSearch = async (tag: string) => {
    const q = tag.trim();
    if (!q) return;
    setSearchQuery(q);
    setActiveTag(q);
    setLoading(true);
    try {
      const token = localStorage.getItem('access_token') || '';
      const res = await fetch(`${MARKET_ANALYSIS_API}/tags/by-tag?tag=${encodeURIComponent(q)}&limit=50`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        setTagToStocksData(data.items || []);
      } else {
        setTagToStocksData([]);
      }
    } catch (e) {
      console.warn('板块成分接口请求异常', e);
      setTagToStocksData([]);
    }
    setLoading(false);
  };

  const handlePerspectiveChange = (mode: 'stock' | 'sector') => {
    setPerspective(mode);
    if (mode === 'sector') {
      const first = stats?.hot_tags?.[0]?.name;
      if (first) handleTagSearch(first);
      else setSearchQuery('');
    } else {
      handleStockSearch('SH600000');
    }
  };

  useEffect(() => {
    fetchStats();
    handleStockSearch('SH600000');
  }, []);

  const hotTagsList: HotTagItem[] = stats?.hot_tags || [];
  const filteredHotTags = hotTagsList.filter(
    (t) => sectorFilter === '全部' || t.type === sectorFilter
  );

  const columns: ColumnsType<TagStockItem> = [
    {
      title: '序号',
      key: 'idx',
      width: 50,
      align: 'center',
      render: (_, __, i) => <span className="font-mono text-xs font-bold text-purple-600">{i + 1}</span>,
    },
    {
      title: '股票代码',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 170,
      render: (symbol, r) => (
        <div className="flex items-center gap-2 whitespace-nowrap">
          <div className="w-7 h-7 rounded-xl bg-purple-50 text-purple-700 font-extrabold text-xs flex items-center justify-center border border-purple-100 shadow-inner">
            {r.name?.substring(0, 1) || '?'}
          </div>
          <div>
            <div className="font-bold text-slate-800 text-xs flex items-center gap-1">
              <span>{r.name}</span>
              <ArrowUpRight className="w-3 h-3 text-slate-400" />
            </div>
            <div className="text-[10px] text-slate-400 font-mono">{symbol}</div>
          </div>
        </div>
      ),
    },
    {
      title: '最新价',
      dataIndex: 'close_price',
      key: 'close_price',
      align: 'right',
      width: 90,
      render: (v: number | null | undefined) =>
        v == null ? (
          MISSING
        ) : (
          <span className="font-mono text-xs font-semibold text-slate-800">¥{v.toFixed(2)}</span>
        ),
    },
    {
      title: '涨跌幅',
      dataIndex: 'pct_change',
      key: 'pct_change',
      align: 'right',
      width: 100,
      render: (v: number | null | undefined) => {
        if (v == null) return MISSING;
        const isPos = v >= 0;
        return (
          <span className={`font-mono text-xs font-bold flex items-center justify-end gap-0.5 ${isPos ? 'text-red-500' : 'text-emerald-500'}`}>
            {isPos ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
            {isPos ? '+' : ''}{v.toFixed(2)}%
          </span>
        );
      },
    },
    {
      title: '主力资金净流入',
      dataIndex: 'net_inflow',
      key: 'net_inflow',
      align: 'right',
      width: 140,
      render: (v: number | null | undefined) => {
        if (v == null) return MISSING;
        const isPos = v >= 0;
        return (
          <span className={`font-mono text-xs font-extrabold ${isPos ? 'text-red-500' : 'text-emerald-500'}`}>
            {isPos ? '+' : ''}{(v / 1e8).toFixed(2)} 亿
          </span>
        );
      },
    },
  ];

  const statCards = [
    {
      label: '总板块数',
      value: stats ? stats.total_sectors.toLocaleString() : '—',
      sub: '涵盖行业/概念/风格',
      icon: <Layers className="w-6 h-6" />,
      cardClass: 'from-white to-blue-50/40 border-blue-100/80',
      iconClass: 'bg-blue-100/70 text-blue-600 border border-blue-200/60',
    },
    {
      label: '覆盖股票',
      value: stats ? stats.total_stocks.toLocaleString() : '—',
      sub: '沪深京 A 股全量',
      icon: <Hash className="w-6 h-6" />,
      cardClass: 'from-white to-emerald-50/40 border-emerald-100/80',
      iconClass: 'bg-emerald-100/70 text-emerald-600 border border-emerald-200/60',
    },
    {
      label: '平均标签数',
      value: stats ? String(stats.avg_tags_per_stock) : '—',
      sub: `最高单股 ${stats?.max_tags_per_stock ?? '—'} 个`,
      icon: <BarChart3 className="w-6 h-6" />,
      cardClass: 'from-white to-amber-50/40 border-amber-100/80',
      iconClass: 'bg-amber-100/70 text-amber-600 border border-amber-200/60',
    },
    {
      label: '总记录数',
      value: stats ? stats.total_relations.toLocaleString() : '—',
      sub: '股票-板块映射关系',
      icon: <TagIcon className="w-6 h-6" />,
      cardClass: 'from-white to-purple-50/40 border-purple-100/80',
      iconClass: 'bg-purple-100/70 text-purple-600 border border-purple-200/60',
    },
  ];

  return (
    <div className="w-full h-full flex flex-col gap-5 bg-white/95 backdrop-blur-md rounded-3xl p-6 border border-purple-100/80 shadow-lg shadow-purple-500/5 overflow-hidden">
      {/* 🌟 1. 顶部 4 大统计数据卡片 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 flex-shrink-0">
        {statCards.map((c) => (
          <div key={c.label} className={`bg-gradient-to-br ${c.cardClass} rounded-2xl p-4 shadow-sm flex items-center justify-between`}>
            <div>
              <div className="text-xs text-slate-500 font-bold mb-1">{c.label}</div>
              <div className="text-2xl font-black font-mono text-slate-900 tracking-tight">
                {statsLoading && !stats ? '...' : c.value}
              </div>
              <div className="text-[10px] text-slate-400 font-medium mt-1">{c.sub}</div>
            </div>
            <div className={`p-3 rounded-2xl ${c.iconClass} shadow-inner`}>{c.icon}</div>
          </div>
        ))}
      </div>

      {/* 🌟 2. 视角切换与板块类别过滤 */}
      <div className="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-4 pt-1 flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="flex items-center p-1 rounded-2xl bg-slate-100 border border-slate-200/60 shadow-inner">
            <button
              onClick={() => handlePerspectiveChange('stock')}
              className={`px-5 py-2 rounded-xl text-xs font-extrabold transition-all duration-200 ${
                perspective === 'stock'
                  ? 'bg-blue-600 text-white shadow-md shadow-blue-600/30'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              股票视角
            </button>
            <button
              onClick={() => handlePerspectiveChange('sector')}
              className={`px-5 py-2 rounded-xl text-xs font-extrabold transition-all duration-200 ${
                perspective === 'sector'
                  ? 'bg-blue-600 text-white shadow-md shadow-blue-600/30'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              板块视角
            </button>
          </div>

          {perspective === 'sector' && (
            <div className="flex items-center gap-1.5 overflow-x-auto">
              {sectorSubCategories.map((sc) => (
                <button
                  key={sc}
                  onClick={() => setSectorFilter(sc)}
                  className={`px-3 py-1.5 rounded-full text-xs font-bold transition-all whitespace-nowrap ${
                    sectorFilter === sc
                      ? 'bg-blue-600 text-white shadow-sm'
                      : 'bg-white text-slate-600 hover:bg-slate-100 border border-slate-200/80'
                  }`}
                >
                  {sc}
                </button>
              ))}
            </div>
          )}
        </div>

        {stats && (
          <span className="text-xs text-slate-400 font-mono hidden lg:inline-block">
            探索 {stats.total_sectors.toLocaleString()} 个板块与 {stats.total_stocks.toLocaleString()} 只股票的多维关联
          </span>
        )}
      </div>

      {/* 🌟 3. 统一搜索输入框与“立即检索”按钮 */}
      <div className="flex items-center gap-3 flex-shrink-0">
        <div className="relative flex-1">
          <Input
            prefix={<Search className="w-4 h-4 text-slate-400 mr-2" />}
            placeholder={perspective === 'stock' ? '输入股票代码或名称，如 SH600000 / 招商银行' : '输入板块名称，如 机器人概念 / 低空经济'}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) =>
              e.key === 'Enter' && (perspective === 'stock' ? handleStockSearch(searchQuery) : handleTagSearch(searchQuery))
            }
            className="rounded-2xl border border-slate-200 bg-white text-xs text-slate-800 placeholder-slate-400 py-3 px-4 shadow-xs hover:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-100 transition-all"
          />
        </div>
        <button
          onClick={() => (perspective === 'stock' ? handleStockSearch(searchQuery) : handleTagSearch(searchQuery))}
          className="px-6 py-3 rounded-2xl bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white text-xs font-extrabold shadow-md shadow-blue-600/20 hover:shadow-lg transition-all flex items-center gap-2 flex-shrink-0"
        >
          <Search className="w-4 h-4" />
          <span>立即检索</span>
        </button>
      </div>

      {/* 下方内容区：顶部(统计/筛选/搜索)固定，此处单独滚动 */}
      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-5 pr-1">
      {/* 🌟 4. 主内容渲染区 */}
      {perspective === 'stock' ? (
        <div className="bg-white rounded-2xl p-5 border border-slate-200/80 shadow-xs flex flex-col gap-3">
          <div className="flex items-center justify-between border-b border-slate-100 pb-3">
            <div className="flex items-center gap-2">
              <span className="text-sm font-extrabold text-blue-600 font-mono">{activeSymbol}</span>
              <span className="text-xs text-slate-400">
                包含 {Object.values(stockToTagsData).flat().length} 个板块标签
              </span>
            </div>
          </div>

          <div className="flex flex-col gap-3 pt-1">
            {loading ? (
              <span className="text-xs text-slate-400">检索中...</span>
            ) : Object.keys(stockToTagsData).length > 0 ? (
              Object.entries(stockToTagsData).map(([groupTitle, tagList]) => (
                <div key={groupTitle} className="flex flex-col gap-2">
                  <div className="text-xs font-bold text-slate-500">{groupTitle}:</div>
                  <div className="flex items-center gap-2 flex-wrap">
                    {tagList.map((t) => (
                      <span
                        key={t}
                        onClick={() => {
                          handlePerspectiveChange('sector');
                          handleTagSearch(t);
                        }}
                        className="px-3.5 py-1.5 rounded-full bg-emerald-50 text-emerald-700 font-extrabold text-xs border border-emerald-200/80 flex items-center gap-1.5 cursor-pointer hover:bg-emerald-600 hover:text-white transition-all shadow-2xs"
                      >
                        <TagIcon className="w-3 h-3 text-emerald-500" />
                        <span>{t}</span>
                      </span>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <span className="text-xs text-slate-400">未查询到该股票的标签信息</span>
            )}
          </div>
        </div>
      ) : (
        /* 🌟 板块视角：成分股行情列表（真实数据） */
        <div className="bg-white rounded-2xl p-5 border border-slate-200/80 shadow-xs flex flex-col gap-3">
          <div className="flex items-center justify-between border-b border-slate-100 pb-3">
            <div className="flex items-center gap-3">
              <span className="text-sm font-extrabold text-blue-600">{activeTag || searchQuery}</span>
              <span className="px-2.5 py-0.5 rounded-full bg-blue-50 text-blue-600 text-xs font-bold border border-blue-100">
                {tagToStocksData.length} 只成分股
              </span>
            </div>
            <span className="text-[11px] text-slate-400">按主力净流入排序 · 点击股票可切换到股票视角</span>
          </div>

          <Table
            columns={columns}
            dataSource={tagToStocksData.map((d, i) => ({ ...d, key: i }))}
            loading={loading}
            pagination={{ pageSize: 8 }}
            size="small"
            locale={{ emptyText: '未查询到该板块的成分股数据' }}
            onRow={(record) => ({
              onClick: () => {
                handlePerspectiveChange('stock');
                handleStockSearch(record.symbol);
              },
              className: 'cursor-pointer',
            })}
          />
        </div>
      )}

      {/* 🌟 5. 底部“热门板块 (按成分股数量排序)” (可折叠) */}
      <div className="bg-slate-50/80 rounded-2xl p-5 border border-slate-200/80 shadow-xs flex flex-col gap-3.5 transition-all">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-blue-600" />
            <h3 className="text-xs font-extrabold text-slate-900 tracking-tight">热门板块 (按成分股数量排序)</h3>
          </div>
        </div>

        <div className="flex items-center gap-2.5 flex-wrap pt-1 animate-in fade-in duration-200">
          {filteredHotTags.length > 0 ? (
            filteredHotTags.map((tagItem) => (
              <div
                key={tagItem.name}
                onClick={() => {
                  handlePerspectiveChange('sector');
                  handleTagSearch(tagItem.name);
                }}
                className="group flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-white hover:bg-blue-600 text-slate-700 hover:text-white border border-slate-200/80 hover:border-blue-600 cursor-pointer transition-all duration-200 shadow-2xs hover:shadow-sm"
              >
                <span className="text-xs font-bold">{tagItem.name}</span>
                <span className="px-2 py-0.5 rounded-full bg-slate-100 group-hover:bg-blue-500 text-slate-500 group-hover:text-white text-[10px] font-medium border border-slate-200/60 group-hover:border-blue-400">
                  {tagItem.type}
                </span>
                <span className="px-2 py-0.5 rounded-full bg-blue-50 group-hover:bg-blue-700 text-blue-600 group-hover:text-white text-[10px] font-mono font-extrabold">
                  {tagItem.count}
                </span>
              </div>
            ))
          ) : (
            <span className="text-xs text-slate-400">{statsLoading ? '加载中...' : '暂无热门板块数据'}</span>
          )}
        </div>
      </div>
      </div>
    </div>
  );
};
