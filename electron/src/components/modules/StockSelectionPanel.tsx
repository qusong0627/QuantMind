import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
    Search, Loader2, CheckCircle, AlertCircle, Download, TrendingUp, DollarSign,
    Layers, Shield, Target, PieChart, TrendingDown, Star, Building, Zap, Activity,
    Award, BarChart3, Cpu, Flame, Gauge, GitBranch, Heart, Rocket, Sparkles,
    Crown, Briefcase, Globe, Anchor, Coins, Diamond, ChevronsUp, ChevronsDown,
} from 'lucide-react';
import axios from 'axios';
import { SERVICE_ENDPOINTS } from '../../config/services';
import { buildCsvText, downloadCsvFile } from '../../utils/csvExport';

interface StockResult {
    symbol: string;
    name: string;
    pe_ratio?: number;
    pb_ratio?: number;
    market_cap?: number;
    roe?: number;
    turnover?: number;
    close?: number;
}

interface StockPoolTemplate {
    id: string;
    name: string;
    description: string;
    query: string;
    icon: React.ReactNode;
    color: string;
    category: '宽基指数' | '市值规模' | '行业板块' | '价值成长' | '技术形态' | '资金趋势' | '主题热点';
}

const STOCK_POOL_TEMPLATES: StockPoolTemplate[] = [
    // ===== 宽基指数 =====
    {
        id: 'all_stocks', name: '全部股票', description: '全市场 A 股，剔除 ST、*ST、退市股',
        query: '全市场A股，排除ST股、*ST股、退市股、新股(上市不足60天)',
        icon: <Layers size={16} />, color: 'from-blue-500 to-indigo-500', category: '宽基指数',
    },
    {
        id: 'no_st', name: '排除ST', description: '全市场排除所有 ST、*ST、退市风险股',
        query: '全市场A股，排除ST、*ST、退市风险股',
        icon: <Shield size={16} />, color: 'from-green-500 to-emerald-500', category: '宽基指数',
    },
    {
        id: 'csi300', name: '沪深300', description: '沪深300成分股，大盘蓝筹',
        query: '沪深300指数成分股',
        icon: <Star size={16} />, color: 'from-red-500 to-rose-500', category: '宽基指数',
    },
    {
        id: 'csi500', name: '中证500', description: '中证500成分股，中盘核心',
        query: '中证500指数成分股',
        icon: <Award size={16} />, color: 'from-orange-500 to-red-500', category: '宽基指数',
    },
    {
        id: 'csi1000', name: '中证1000', description: '中证1000成分股，小盘活跃',
        query: '中证1000指数成分股',
        icon: <Target size={16} />, color: 'from-purple-500 to-violet-500', category: '宽基指数',
    },
    {
        id: 'csi800', name: '中证800', description: '沪深300+中证500，A股核心代表',
        query: '中证800指数成分股',
        icon: <Crown size={16} />, color: 'from-fuchsia-500 to-pink-500', category: '宽基指数',
    },
    {
        id: 'sse50', name: '上证50', description: '沪市最具代表性的50家大盘蓝筹',
        query: '上证50指数成分股',
        icon: <Diamond size={16} />, color: 'from-rose-500 to-red-600', category: '宽基指数',
    },
    {
        id: 'star50', name: '科创50', description: '科创板50成分股，硬科技核心',
        query: '科创50指数成分股',
        icon: <Cpu size={16} />, color: 'from-cyan-500 to-blue-500', category: '宽基指数',
    },
    {
        id: 'chinext', name: '创业板指', description: '创业板成长龙头集合',
        query: '创业板指数成分股',
        icon: <Rocket size={16} />, color: 'from-violet-500 to-purple-600', category: '宽基指数',
    },
    {
        id: 'bse_a', name: '北交所', description: '北京证券交易所全部上市股票',
        query: '北交所全部上市股票',
        icon: <Building size={16} />, color: 'from-amber-600 to-orange-600', category: '宽基指数',
    },

    // ===== 市值规模 =====
    {
        id: 'small_cap', name: '小市值', description: '总市值小于50亿的小盘股',
        query: '总市值小于50亿的非ST股票',
        icon: <TrendingDown size={16} />, color: 'from-cyan-500 to-teal-500', category: '市值规模',
    },
    {
        id: 'mid_cap', name: '中市值', description: '总市值50-300亿的中盘股',
        query: '总市值在50亿到300亿之间的非ST股票',
        icon: <Activity size={16} />, color: 'from-teal-500 to-emerald-500', category: '市值规模',
    },
    {
        id: 'large_cap', name: '大市值', description: '总市值大于500亿的大盘股',
        query: '总市值大于500亿的非ST股票',
        icon: <TrendingUp size={16} />, color: 'from-blue-600 to-blue-800', category: '市值规模',
    },
    {
        id: 'mega_cap', name: '巨型蓝筹', description: '总市值大于2000亿的核心资产',
        query: '总市值大于2000亿的非ST股票',
        icon: <Crown size={16} />, color: 'from-indigo-700 to-blue-900', category: '市值规模',
    },

    // ===== 行业板块 =====
    {
        id: 'financial', name: '金融股', description: '银行、券商、保险等金融板块',
        query: '银行、券商、保险、多元金融板块的非ST股票',
        icon: <Building size={16} />, color: 'from-yellow-600 to-orange-600', category: '行业板块',
    },
    {
        id: 'tech', name: '科技股', description: '半导体、软件、人工智能、计算机',
        query: '半导体、软件开发、人工智能、计算机设备、电子板块的非ST股票',
        icon: <Cpu size={16} />, color: 'from-blue-500 to-cyan-500', category: '行业板块',
    },
    {
        id: 'medical', name: '医药生物', description: '医药、生物制品、医疗器械',
        query: '医药制造、生物制品、医疗器械、医疗服务板块的非ST股票',
        icon: <Heart size={16} />, color: 'from-pink-500 to-red-500', category: '行业板块',
    },
    {
        id: 'new_energy', name: '新能源', description: '锂电、光伏、风电、储能',
        query: '锂电池、光伏设备、风电设备、储能、新能源汽车板块的非ST股票',
        icon: <Zap size={16} />, color: 'from-green-500 to-lime-500', category: '行业板块',
    },
    {
        id: 'consumer', name: '消费白马', description: '白酒、食品饮料、家电、医药消费',
        query: '白酒、食品饮料、家电、医药消费板块的非ST股票',
        icon: <Coins size={16} />, color: 'from-amber-500 to-yellow-500', category: '行业板块',
    },
    {
        id: 'military', name: '军工', description: '航空、航天、船舶、兵器装备',
        query: '航空装备、航天装备、船舶制造、兵器装备板块的非ST股票',
        icon: <Anchor size={16} />, color: 'from-slate-600 to-slate-800', category: '行业板块',
    },
    {
        id: 'real_estate', name: '地产链', description: '房地产、建材、家居家电',
        query: '房地产开发、建材、家居家电板块的非ST股票',
        icon: <Briefcase size={16} />, color: 'from-stone-500 to-amber-700', category: '行业板块',
    },

    // ===== 价值/成长 =====
    {
        id: 'value', name: '低估值', description: '市盈率<15、市净率<1.5的价值股',
        query: '市盈率大于0且小于15，市净率小于1.5，ROE大于8%的非ST股票',
        icon: <DollarSign size={16} />, color: 'from-emerald-600 to-green-700', category: '价值成长',
    },
    {
        id: 'growth', name: '高成长', description: '营收增速>20%、ROE>15%的成长股',
        query: '营收同比增长率大于20%，ROE大于15%，净利润同比增长大于20%的非ST股票',
        icon: <TrendingUp size={16} />, color: 'from-pink-500 to-rose-500', category: '价值成长',
    },
    {
        id: 'high_dividend', name: '高股息', description: '股息率>4%、连续3年分红的高股息股',
        query: '股息率大于4%，市盈率大于0的非ST股票',
        icon: <PieChart size={16} />, color: 'from-amber-500 to-yellow-600', category: '价值成长',
    },
    {
        id: 'high_roe', name: '高ROE', description: 'ROE>20%的高质量白马股',
        query: 'ROE大于20%，市盈率大于0且小于30的非ST股票',
        icon: <Zap size={16} />, color: 'from-indigo-500 to-blue-600', category: '价值成长',
    },
    {
        id: 'broken_pb', name: '破净股', description: '市净率小于1的破净价值股',
        query: '市净率小于1，ROE大于0的非ST股票',
        icon: <ChevronsDown size={16} />, color: 'from-slate-500 to-slate-700', category: '价值成长',
    },
    {
        id: 'gem_quality', name: '白马蓝筹', description: 'ROE>15%、净利润正增长、大市值',
        query: '总市值大于200亿，ROE大于15%，净利润同比增长大于0的非ST股票',
        icon: <Crown size={16} />, color: 'from-amber-600 to-yellow-700', category: '价值成长',
    },
    {
        id: 'peg', name: 'PEG合理', description: 'PEG<1的价值成长平衡股',
        query: '市盈率大于0且小于25，净利润同比增长大于市盈率的非ST股票',
        icon: <Gauge size={16} />, color: 'from-violet-500 to-fuchsia-500', category: '价值成长',
    },

    // ===== 技术形态 =====
    {
        id: 'ma_bullish', name: '均线多头', description: '5/10/20/60日均线多头排列',
        query: '5日均线>10日均线>20日均线>60日均线，且收盘价站上5日均线的非ST股票',
        icon: <ChevronsUp size={16} />, color: 'from-red-500 to-orange-500', category: '技术形态',
    },
    {
        id: 'oversold_rebound', name: '超跌反弹', description: '20日跌幅大、RSI<30',
        query: '近20日跌幅大于15%，RSI小于30的非ST股票',
        icon: <TrendingDown size={16} />, color: 'from-teal-500 to-cyan-500', category: '技术形态',
    },
    {
        id: 'limit_up_combo', name: '连板妖股', description: '近5日涨停≥2次的活跃股',
        query: '近5个交易日有2次及以上涨停的非ST股票',
        icon: <Flame size={16} />, color: 'from-red-600 to-orange-700', category: '技术形态',
    },
    {
        id: 'breakthrough', name: '突破新高', description: '突破60日新高、量能放大',
        query: '收盘价创60日新高，且当日成交额是5日均值1.5倍以上的非ST股票',
        icon: <Sparkles size={16} />, color: 'from-yellow-500 to-amber-600', category: '技术形态',
    },
    {
        id: 'high_turnover', name: '换手活跃', description: '换手率>8%的活跃股',
        query: '换手率大于8%，成交额大于5亿的非ST股票',
        icon: <Activity size={16} />, color: 'from-purple-500 to-pink-500', category: '技术形态',
    },
    {
        id: 'new_listing', name: '次新股', description: '上市60-365天的次新股',
        query: '上市时间在60天到365天之间，非ST股票',
        icon: <Sparkles size={16} />, color: 'from-cyan-400 to-blue-500', category: '技术形态',
    },

    // ===== 资金趋势 =====
    {
        id: 'north_inflow', name: '北向重仓', description: '北向资金持仓占比>5%',
        query: '北向资金持仓占流通股比例大于5%的非ST股票',
        icon: <Globe size={16} />, color: 'from-blue-500 to-indigo-600', category: '资金趋势',
    },
    {
        id: 'main_inflow', name: '主力净流入', description: '近5日主力资金净流入',
        query: '近5个交易日主力资金净流入超过5000万的非ST股票',
        icon: <ChevronsUp size={16} />, color: 'from-red-500 to-pink-600', category: '资金趋势',
    },
    {
        id: 'fund_heavy', name: '机构重仓', description: '基金持仓占比>30%',
        query: '基金持仓占流通股比例大于30%的非ST股票',
        icon: <BarChart3 size={16} />, color: 'from-emerald-500 to-teal-600', category: '资金趋势',
    },
    {
        id: 'buyback', name: '回购增持', description: '近期有股份回购或大股东增持',
        query: '近3个月发布股份回购或大股东增持公告的非ST股票',
        icon: <GitBranch size={16} />, color: 'from-green-500 to-emerald-600', category: '资金趋势',
    },

    // ===== 主题热点 =====
    {
        id: 'ai_concept', name: 'AI人工智能', description: 'AI大模型、算力、机器人',
        query: '人工智能、AI大模型、算力服务器、机器人、AIGC概念的非ST股票',
        icon: <Cpu size={16} />, color: 'from-violet-500 to-purple-600', category: '主题热点',
    },
    {
        id: 'chip', name: '半导体芯片', description: '芯片设计、制造、设备、材料',
        query: '芯片设计、半导体制造、半导体设备、半导体材料、封装测试板块',
        icon: <Cpu size={16} />, color: 'from-blue-600 to-cyan-700', category: '主题热点',
    },
    {
        id: 'state_owned', name: '中字头央企', description: '中字头央企国企改革标的',
        query: '股票简称含中字头的央企国企非ST股票',
        icon: <Building size={16} />, color: 'from-red-700 to-amber-700', category: '主题热点',
    },
];

const CATEGORY_LIST: Array<StockPoolTemplate['category'] | '全部'> = [
    '全部', '宽基指数', '市值规模', '行业板块', '价值成长', '技术形态', '资金趋势', '主题热点',
];

interface StockSelectionPanelProps {
    onStockPoolSelected?: (symbols: string[]) => void;
}

export const StockSelectionPanel: React.FC<StockSelectionPanelProps> = ({
    onStockPoolSelected
}) => {
    const [query, setQuery] = useState('');
    const [isSelecting, setIsSelecting] = useState(false);
    const [results, setResults] = useState<StockResult[]>([]);
    const [selectedStocks, setSelectedStocks] = useState<Set<string>>(new Set());
    const [error, setError] = useState<string | null>(null);
    const [activeCategory, setActiveCategory] = useState<StockPoolTemplate['category'] | '全部'>('全部');

    const visibleTemplates = activeCategory === '全部'
        ? STOCK_POOL_TEMPLATES
        : STOCK_POOL_TEMPLATES.filter(t => t.category === activeCategory);

    const applyTemplate = (template: StockPoolTemplate) => {
        setQuery(template.query);
        // 直接传 template.query 触发，避免 React state 异步更新导致读到空值
        setTimeout(() => runSelect(template.query), 0);
    };

    const runSelect = async (queryText: string) => {
        if (!queryText.trim()) {
            setError('请输入选股条件');
            return;
        }

        setIsSelecting(true);
        setError(null);
        setResults([]);
        setSelectedStocks(new Set());

        try {
            const response = await axios.post(
                `${SERVICE_ENDPOINTS.AI_STRATEGY}/stocks/select`,
                {
                    query: queryText.trim(),
                    limit: 200
                }
            );

            const data = response.data?.data || response.data;
            const stockList = data?.data || data?.stocks || [];

            if (Array.isArray(stockList) && stockList.length > 0) {
                setResults(stockList);
            } else {
                setError('未找到符合条件的股票');
            }
        } catch (err) {
            console.error('Stock selection error:', err);
            if (axios.isAxiosError(err)) {
                const message = err.response?.data?.message || err.response?.data?.detail || err.message;
                setError(`选股失败: ${message}`);
            } else {
                setError('选股失败，请稍后重试');
            }
        } finally {
            setIsSelecting(false);
        }
    };

    const handleSelect = async () => {
        await runSelect(query);
    };

    const toggleStock = (symbol: string) => {
        const newSelected = new Set(selectedStocks);
        if (newSelected.has(symbol)) {
            newSelected.delete(symbol);
        } else {
            newSelected.add(symbol);
        }
        setSelectedStocks(newSelected);
    };

    const toggleAll = () => {
        if (selectedStocks.size === results.length) {
            setSelectedStocks(new Set());
        } else {
            setSelectedStocks(new Set(results.map(s => s.symbol)));
        }
    };

    const handleApply = () => {
        if (onStockPoolSelected && selectedStocks.size > 0) {
            onStockPoolSelected(Array.from(selectedStocks));
        }
    };

    const handleExport = () => {
        const selectedData = results.filter(s => selectedStocks.has(s.symbol));
        const csv = buildCsvText(
            ['股票代码', '股票名称', '市盈率', '市净率', '市值(万)', 'ROE(%)', '成交额(万)', '收盘价'],
            selectedData.map(s => [
                s.symbol,
                s.name,
                s.pe_ratio?.toFixed(2) || '-',
                s.pb_ratio?.toFixed(2) || '-',
                s.market_cap?.toFixed(0) || '-',
                s.roe?.toFixed(2) || '-',
                s.turnover?.toFixed(0) || '-',
                s.close?.toFixed(2) || '-',
            ]),
            { textColumns: [0], filename: '' },
        );
        downloadCsvFile(csv, `选股结果_${new Date().toISOString().split('T')[0]}.csv`);
    };

    const formatNumber = (num: number | undefined, decimals = 2): string => {
        if (num === undefined || num === null) return '-';
        return num.toFixed(decimals);
    };

    const formatLargeNumber = (num: number | undefined): string => {
        if (num === undefined || num === null) return '-';
        if (num >= 10000) {
            return `${(num / 10000).toFixed(2)}亿`;
        }
        return `${num.toFixed(0)}万`;
    };

    return (
        <div className="h-full flex flex-col gap-4 p-6 bg-gray-50">
            {/* 头部 */}
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-green-500 to-teal-500 flex items-center justify-center">
                        <TrendingUp className="w-5 h-5 text-white" />
                    </div>
                    <div>
                        <h2 className="text-xl font-bold text-gray-800">智能选股</h2>
                        <p className="text-sm text-gray-500">使用自然语言描述选股条件</p>
                    </div>
                </div>
            </div>

            <div className="flex-1 overflow-y-auto">
                <div className="max-w-6xl mx-auto space-y-6">
                    {/* 选股模板 */}
                    <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
                        <div className="flex items-center justify-between mb-3">
                            <h3 className="text-sm font-semibold text-gray-700">常用股票池模板</h3>
                            <span className="text-xs text-gray-400">{visibleTemplates.length} / {STOCK_POOL_TEMPLATES.length} 个模板</span>
                        </div>
                        {/* 分类筛选条 */}
                        <div className="flex flex-wrap gap-2 mb-4 pb-3 border-b border-gray-100">
                            {CATEGORY_LIST.map((cat) => (
                                <button
                                    key={cat}
                                    onClick={() => setActiveCategory(cat)}
                                    className={`px-3 py-1 rounded-full text-xs font-semibold transition-all ${
                                        activeCategory === cat
                                            ? 'bg-gradient-to-r from-green-500 to-teal-500 text-white shadow-sm'
                                            : 'bg-gray-50 text-gray-600 hover:bg-gray-100'
                                    }`}
                                >
                                    {cat}
                                </button>
                            ))}
                        </div>
                        <div className="grid grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-3">
                            {visibleTemplates.map((t) => (
                                <button
                                    key={t.id}
                                    onClick={() => applyTemplate(t)}
                                    disabled={isSelecting}
                                    className="group flex items-start gap-3 p-3 rounded-xl border border-gray-100 hover:border-gray-200 hover:shadow-sm transition-all text-left disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    <div className={`w-8 h-8 rounded-lg bg-gradient-to-br ${t.color} flex items-center justify-center text-white flex-shrink-0 mt-0.5 group-hover:scale-110 transition-transform`}>
                                        {t.icon}
                                    </div>
                                    <div className="min-w-0">
                                        <div className="text-sm font-semibold text-gray-800">{t.name}</div>
                                        <div className="text-[10px] text-gray-500 mt-0.5 leading-tight line-clamp-2">{t.description}</div>
                                    </div>
                                </button>
                            ))}
                        </div>
                    </div>

                    {/* 搜索区域 */}
                    <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
                        <label className="block text-sm font-medium text-gray-700 mb-3">
                            选股条件 <span className="text-red-500">*</span>
                        </label>
                        <div className="flex gap-3">
                            <input
                                type="text"
                                value={query}
                                onChange={(e) => setQuery(e.target.value)}
                                onKeyPress={(e) => e.key === 'Enter' && handleSelect()}
                                placeholder="例如: 低市盈率的白马股、ROE大于15%的成长股、市值超过100亿的蓝筹股..."
                                className="flex-1 px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent text-gray-800 placeholder-gray-400"
                                disabled={isSelecting}
                            />
                            <button
                                onClick={handleSelect}
                                disabled={isSelecting || !query.trim()}
                                className="px-6 py-3 bg-gradient-to-r from-green-600 to-teal-600 text-white rounded-lg font-medium hover:from-green-700 hover:to-teal-700 disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-lg shadow-green-200 flex items-center gap-2"
                            >
                                {isSelecting ? (
                                    <>
                                        <Loader2 className="w-5 h-5 animate-spin" />
                                        选股中...
                                    </>
                                ) : (
                                    <>
                                        <Search className="w-5 h-5" />
                                        开始选股
                                    </>
                                )}
                            </button>
                        </div>

                        {/* 自定义输入提示 */}
                        <div className="mt-3 p-3 bg-blue-50 border border-blue-100 rounded-lg">
                            <p className="text-xs text-blue-700">
                                <span className="font-medium">提示:</span> 点击上方模板快速选股，也可手动输入自定义条件。支持多种条件组合，如"市盈率小于20且ROE大于15%的股票"、"沪深300成分股中市值前50名"等
                            </p>
                        </div>
                    </div>

                    {/* 错误提示 */}
                    {error && (
                        <motion.div
                            initial={{ opacity: 0, y: -10 }}
                            animate={{ opacity: 1, y: 0 }}
                            className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3"
                        >
                            <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
                            <div>
                                <p className="text-sm font-medium text-red-800">选股失败</p>
                                <p className="text-sm text-red-600 mt-1">{error}</p>
                            </div>
                        </motion.div>
                    )}

                    {/* 结果区域 */}
                    {results.length > 0 && (
                        <motion.div
                            initial={{ opacity: 0, y: 20 }}
                            animate={{ opacity: 1, y: 0 }}
                            className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden"
                        >
                            {/* 结果头部 */}
                            <div className="bg-gradient-to-r from-green-50 to-teal-50 border-b border-gray-200 px-6 py-4">
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-3">
                                        <CheckCircle className="w-6 h-6 text-green-500" />
                                        <div>
                                            <h3 className="font-bold text-gray-800">选股结果</h3>
                                            <p className="text-sm text-gray-600 mt-1">
                                                共找到 {results.length} 只股票，已选择 {selectedStocks.size} 只
                                            </p>
                                        </div>
                                    </div>

                                    <div className="flex gap-2">
                                        <button
                                            onClick={toggleAll}
                                            className="px-4 py-2 bg-white border border-gray-300 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-50 transition-colors"
                                        >
                                            {selectedStocks.size === results.length ? '取消全选' : '全选'}
                                        </button>
                                        <button
                                            onClick={handleExport}
                                            disabled={selectedStocks.size === 0}
                                            className="px-4 py-2 bg-white border border-gray-300 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                                        >
                                            <Download className="w-4 h-4" />
                                            导出CSV
                                        </button>
                                        {onStockPoolSelected && (
                                            <button
                                                onClick={handleApply}
                                                disabled={selectedStocks.size === 0}
                                                className="px-4 py-2 bg-gradient-to-r from-green-600 to-teal-600 text-white rounded-lg text-sm font-medium hover:from-green-700 hover:to-teal-700 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                                            >
                                                应用到策略 ({selectedStocks.size})
                                            </button>
                                        )}
                                    </div>
                                </div>
                            </div>

                            {/* 结果表格 */}
                            <div className="overflow-x-auto">
                                <table className="w-full">
                                    <thead className="bg-gray-50 border-b border-gray-200">
                                        <tr>
                                            <th className="px-4 py-3 text-left">
                                                <input
                                                    type="checkbox"
                                                    checked={selectedStocks.size === results.length && results.length > 0}
                                                    onChange={toggleAll}
                                                    className="rounded border-gray-300 text-green-600 focus:ring-green-500"
                                                />
                                            </th>
                                            <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">股票代码</th>
                                            <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">股票名称</th>
                                            <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase">市盈率</th>
                                            <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase">市净率</th>
                                            <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase">市值</th>
                                            <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase">ROE(%)</th>
                                            <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase">成交额</th>
                                            <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase">收盘价</th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gray-200">
                                        {results.map((stock, idx) => (
                                            <tr
                                                key={stock.symbol}
                                                className={`hover:bg-gray-50 transition-colors ${selectedStocks.has(stock.symbol) ? 'bg-green-50' : ''
                                                    }`}
                                            >
                                                <td className="px-4 py-3">
                                                    <input
                                                        type="checkbox"
                                                        checked={selectedStocks.has(stock.symbol)}
                                                        onChange={() => toggleStock(stock.symbol)}
                                                        className="rounded border-gray-300 text-green-600 focus:ring-green-500"
                                                    />
                                                </td>
                                                <td className="px-4 py-3 text-sm font-mono text-gray-800">{stock.symbol}</td>
                                                <td className="px-4 py-3 text-sm font-medium text-gray-800">{stock.name}</td>
                                                <td className="px-4 py-3 text-sm text-right text-gray-600">{formatNumber(stock.pe_ratio)}</td>
                                                <td className="px-4 py-3 text-sm text-right text-gray-600">{formatNumber(stock.pb_ratio)}</td>
                                                <td className="px-4 py-3 text-sm text-right text-gray-600">{formatLargeNumber(stock.market_cap)}</td>
                                                <td className="px-4 py-3 text-sm text-right text-gray-600">{formatNumber(stock.roe)}</td>
                                                <td className="px-4 py-3 text-sm text-right text-gray-600">{formatLargeNumber(stock.turnover)}</td>
                                                <td className="px-4 py-3 text-sm text-right font-medium text-gray-800">
                                                    {stock.close ? `¥${formatNumber(stock.close)}` : '-'}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </motion.div>
                    )}
                </div>
            </div>
        </div>
    );
};
