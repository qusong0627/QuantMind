import React, { startTransition, useCallback, useEffect, useState } from 'react';
import { Card, Tabs, Tag, Typography, Space, Spin, message, List, Button, Empty } from 'antd';
import {
    StockOutlined,
    BarChartOutlined,
    AppstoreOutlined,
    BankOutlined,
    StarFilled,
    ReloadOutlined,
    ExperimentOutlined,
} from '@ant-design/icons';
import { KlineChart } from '../components/KlineChart';
import { TradingViewChart } from '../components/TradingViewChart';
import { StockSearch } from '../components/StockSearch';
import { FieldBrowser } from '../components/FieldBrowser';
import { SectorExplorer } from '../components/SectorExplorer';
import { MarketOverview } from '../components/MarketOverview';
import StrategyLabSignalCard from '../components/StrategyLabSignalCard';
import { dataDashboardService, isRequestCancelled, KlineItem } from '../services/dataDashboardService';
import { listUserPoolSymbols, USER_POOL_FAVORITES } from '../../../services/userStockPoolService';
import { useAppDispatch, useAppSelector } from '../../../store';
import { selectCurrentMarket, setMarket, AppMarket } from '../../../store/slices/uiSlice';
import { notifySharedAdminMarket, type SharedAdminMarket } from '../../admin/hooks/useSharedAdminMarket';
import { isMarketEnabled } from '../../../config/marketFlags';
import { toSuffixCode } from '../../../utils/portfolioUtils';

const { Title, Text } = Typography;

const MARKET_TABS = [
    { key: 'CN' as AppMarket, label: 'A股', icon: <StockOutlined /> },
    { key: 'HK' as AppMarket, label: '港股', icon: <BankOutlined /> },
    { key: 'US' as AppMarket, label: '美股', icon: <BarChartOutlined /> },
    { key: 'CRYPTO' as AppMarket, label: '区块链', icon: <AppstoreOutlined /> },
    { key: 'FUTURES' as AppMarket, label: '期货', icon: <StockOutlined /> },
    { key: 'CUSTOM' as AppMarket, label: '自定义', icon: <ExperimentOutlined /> },
].filter((t) => isMarketEnabled(t.key));

const DEFAULT_SYMBOLS: Record<string, { symbol: string; name: string }> = {
    CN: { symbol: '600519.SH', name: '贵州茅台' },
    HK: { symbol: '00700.HK', name: '腾讯控股' },
    US: { symbol: 'AAPL', name: 'Apple' },
    CRYPTO: { symbol: 'BTCUSDT', name: '比特币' },
    FUTURES: { symbol: 'RB0.CN', name: '螺纹钢主力' },
    // 自定义市场无默认标的：切入后标的/K线/板块均显空，字段目录展示自传因子。
    CUSTOM: { symbol: '', name: '' },
};

interface WatchlistItem {
    symbol: string;
    stockName: string | null;
    tags: string[];
}

const DashboardPage: React.FC = () => {
    const market = useAppSelector(selectCurrentMarket);
    const dispatch = useAppDispatch();
    const marketLabel = MARKET_TABS.find((t) => t.key === market)?.label ?? 'A股';
    const [symbol, setSymbol] = useState(DEFAULT_SYMBOLS.CN.symbol);
    const [symbolName, setSymbolName] = useState(DEFAULT_SYMBOLS.CN.name);
    const [klineData, setKlineData] = useState<KlineItem[]>([]);
    const [klineLoading, setKlineLoading] = useState(false);
    const [klineSource, setKlineSource] = useState('');
    const [fieldCount, setFieldCount] = useState(0);
    const [activeTab, setActiveTab] = useState('fields');

    // Watchlist state
    const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
    const [watchlistLoading, setWatchlistLoading] = useState(false);

    // Load favorites user stock pool
    const loadWatchlist = useCallback(async () => {
        setWatchlistLoading(true);
        try {
            const symbols = await listUserPoolSymbols(USER_POOL_FAVORITES);
            setWatchlist(
                symbols.map((symbol) => ({
                    // 入库即转后缀式，与后端 StockCodeUtil.normalize 同口径
                    symbol: toSuffixCode(String(symbol)),
                    stockName: null,
                    tags: [],
                })),
            );
        } catch {
            setWatchlist([]);
        } finally {
            setWatchlistLoading(false);
        }
    }, []);

    useEffect(() => {
        loadWatchlist();
    }, [loadWatchlist]);

    // Load field count for current market（旧请求被 service 单飞取消，不覆盖新数据）
    useEffect(() => {
        let on = true;
        dataDashboardService
            .getFields(market)
            .then((f) => {
                if (on) setFieldCount(f.length);
            })
            .catch(() => {});
        return () => {
            on = false;
        };
    }, [market]);

    // Load K-line data（序号守卫：慢响应不覆盖快速切换后的新数据，取消不报错）
    const klineReq = React.useRef(0);
    const loadKline = useCallback(async (m: AppMarket, sym: string) => {
        const id = ++klineReq.current;
        setKlineLoading(true);
        try {
            const resp = await dataDashboardService.getKline(m, sym, 120);
            if (id !== klineReq.current) return;
            setKlineData(resp.items || []);
            setKlineSource(resp.source_used || '');
        } catch (e: any) {
            if (id !== klineReq.current || isRequestCancelled(e)) return;
            message.error(`K线数据加载失败: ${e?.message || e}`);
            setKlineData([]);
            setKlineSource('');
        } finally {
            if (id === klineReq.current) setKlineLoading(false);
        }
    }, []);

    // When market changes, reset to default symbol（自定义无默认标的：清空K线显空）
    useEffect(() => {
        const def = DEFAULT_SYMBOLS[market];
        if (!def) return;
        setSymbol(def.symbol);
        setSymbolName(def.name);
        if (!def.symbol) {
            klineReq.current += 1;
            setKlineData([]);
            setKlineSource('');
            setKlineLoading(false);
            return;
        }
        loadKline(market, def.symbol);
    }, [market, loadKline]);

    // Handle stock selection from search
    const handleStockSelect = useCallback(
        (sym: string, name: string) => {
            setSymbol(sym);
            setSymbolName(name);
            loadKline(market, sym);
        },
        [market, loadKline],
    );

    // Convert watchlist symbol to kline API format
    // SZ300258 → 300258.SZ, SH600519 → 600519.SH, 00700.HK stays, BTCUSDT → crypto
    const normalizeSymbol = (raw: string): { symbol: string; market: AppMarket } => {
        const s = raw.trim().toUpperCase();
        // SZ/SH/BJ prefix: SZ300258 → 300258.SZ
        const cnMatch = s.match(/^(SZ|SH|BJ)(\d{6})$/);
        if (cnMatch) return { symbol: `${cnMatch[2]}.${cnMatch[1]}`, market: 'CN' };
        // .HK suffix or 5-digit HK code
        if (s.includes('.HK')) return { symbol: s, market: 'HK' };
        if (/^0\d{4}$/.test(s.replace(/\.\w+$/, ''))) return { symbol: `${s.replace(/\.\w+$/, '')}.HK`, market: 'HK' };
        // .SH/.SZ/.BJ suffix already in correct format
        if (/\.(SH|SZ|BJ)$/.test(s)) return { symbol: s, market: 'CN' };
        // 6-digit A-stock code without suffix（号段口径以中枢 toSuffixCode 为准，
        // 60/68/90→SH、00/30/20→SZ、43/83/87/88/92→BJ；未知号段原样返回）
        if (/^\d{6}$/.test(s)) {
            const suffixed = toSuffixCode(s);
            if (/\.(SH|SZ|BJ)$/.test(suffixed)) return { symbol: suffixed, market: 'CN' };
            return { symbol: s, market: 'CN' };
        }
        // Crypto: USDT/USDC suffix or high-entropy alphanumeric ticker
        if (/USDT$|USDC$|^BTC|^ETH|^BNB|^SOL|^XRP|^DOGE/.test(s)) return { symbol: s, market: 'CRYPTO' };
        // Futures: .CN/.FUT/.CNF suffix
        if (/\.(CN|FUT|CNF)$/.test(s)) return { symbol: s, market: 'FUTURES' };
        // Otherwise US
        return { symbol: s, market: 'US' };
    };

    // Handle watchlist item click
    const handleWatchlistClick = useCallback(
        (item: WatchlistItem) => {
            const { symbol: sym, market: m } = normalizeSymbol(item.symbol);
            startTransition(() => { dispatch(setMarket(m)); notifySharedAdminMarket(m as SharedAdminMarket); });
            setSymbol(sym);
            setSymbolName(item.stockName || item.symbol);
            loadKline(m, sym);
        },
        [dispatch, loadKline],
    );

    return (
        <div style={{ padding: '16px 24px', background: '#f8fafc', minHeight: '100%' }}>
            {/* Header */}
            <div style={{ marginBottom: 16 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                    <Space>
                        <Title level={4} style={{ margin: 0 }}>{marketLabel}数据看板</Title>
                        <Tag color="blue">{fieldCount} 个字段</Tag>
                    </Space>
                    <StockSearch market={market} onSelect={handleStockSelect} />
                </div>

                {/* Market Tabs */}
                <Tabs
                    activeKey={market}
                    onChange={(k) => startTransition(() => { dispatch(setMarket(k as AppMarket)); notifySharedAdminMarket(k as SharedAdminMarket); })}
                    items={MARKET_TABS.map((t) => ({
                        key: t.key,
                        label: (
                            <span>
                                {t.icon}
                                <span style={{ marginLeft: 6 }}>{t.label}</span>
                            </span>
                        ),
                    }))}
                    style={{ marginBottom: 0 }}
                />
            </div>

            {/* Market Overview */}
            <div style={{ marginBottom: 16 }}>
                <MarketOverview market={market} />
            </div>

            {/* Strategy Lab daily scan signal card */}
            <div style={{ marginBottom: 16 }}>
                <StrategyLabSignalCard />
            </div>

            {/* Main content: Watchlist sidebar + K-line chart */}
            <div style={{ display: 'flex', gap: 16, marginBottom: 16 }}>
                {/* Watchlist sidebar */}
                <Card
                    size="small"
                    title={
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <Space>
                                <StarFilled style={{ color: '#f59e0b' }} />
                                <Text strong>自选股</Text>
                                <Tag>{watchlist.length}</Tag>
                            </Space>
                            <Button
                                type="text"
                                size="small"
                                icon={<ReloadOutlined />}
                                onClick={loadWatchlist}
                                loading={watchlistLoading}
                            />
                        </div>
                    }
                    style={{ width: 220, flexShrink: 0 }}
                    bodyStyle={{ padding: '4px 8px', maxHeight: 580, overflowY: 'auto' }}
                >
                    {watchlistLoading && watchlist.length === 0 ? (
                        <div style={{ textAlign: 'center', padding: 20 }}>
                            <Spin size="small" />
                        </div>
                    ) : watchlist.length === 0 ? (
                        <Empty
                            image={Empty.PRESENTED_IMAGE_SIMPLE}
                            description="暂无自选股"
                            style={{ padding: '20px 0' }}
                        >
                            <Text type="secondary" style={{ fontSize: 11 }}>
                                去股票终端或投研加入自选池
                            </Text>
                        </Empty>
                    ) : (
                        <List
                            dataSource={watchlist}
                            size="small"
                            renderItem={(item) => (
                                <div
                                    onClick={() => handleWatchlistClick(item)}
                                    style={{
                                        padding: '8px 10px',
                                        cursor: 'pointer',
                                        borderRadius: 6,
                                        marginBottom: 2,
                                        background: normalizeSymbol(item.symbol).symbol === normalizeSymbol(symbol).symbol ? '#e0f2fe' : 'transparent',
                                        border: normalizeSymbol(item.symbol).symbol === normalizeSymbol(symbol).symbol ? '1px solid #7dd3fc' : '1px solid transparent',
                                        transition: 'all 0.2s',
                                    }}
                                    onMouseEnter={(e) => {
                                        if (normalizeSymbol(symbol).symbol !== normalizeSymbol(item.symbol).symbol) {
                                            e.currentTarget.style.background = '#f8fafc';
                                        }
                                    }}
                                    onMouseLeave={(e) => {
                                        if (normalizeSymbol(symbol).symbol !== normalizeSymbol(item.symbol).symbol) {
                                            e.currentTarget.style.background = 'transparent';
                                        }
                                    }}
                                >
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                        <Text strong style={{ fontSize: 13 }}>{item.symbol.replace(/\.\w+$/, '')}</Text>
                                        {item.tags.length > 0 && (
                                            <Tag color="blue" style={{ fontSize: 10, margin: 0 }}>
                                                {item.tags[0]}
                                            </Tag>
                                        )}
                                    </div>
                                    <Text type="secondary" style={{ fontSize: 11 }}>
                                        {item.stockName || '—'}
                                    </Text>
                                </div>
                            )}
                        />
                    )}
                </Card>

                {/* K-line Chart */}
                <Card
                    size="small"
                    style={{ flex: 1 }}
                    bodyStyle={{ padding: '8px' }}
                    title={
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <Space>
                                <Text strong>{symbol}</Text>
                                <Text type="secondary">{symbolName}</Text>
                                {klineSource && (
                                    <Tag color="green" style={{ fontSize: 10 }}>
                                        {klineSource}
                                    </Tag>
                                )}
                            </Space>
                        </div>
                    }
                >
                    {klineData.length === 0 && klineLoading ? (
                        <div style={{ height: 550, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                            <Spin />
                        </div>
                    ) : (
                        <div style={{ position: 'relative' }}>
                            <TradingViewChart data={klineData} height={550} />
                            {klineLoading && (
                                <div
                                    style={{
                                        position: 'absolute',
                                        top: 8,
                                        right: 8,
                                        background: 'rgba(255,255,255,0.85)',
                                        borderRadius: 8,
                                        padding: '4px 10px',
                                    }}
                                >
                                    <Spin size="small" />
                                </div>
                            )}
                        </div>
                    )}
                </Card>
            </div>

            {/* Bottom tabs: Fields / Sectors */}
            <Card size="small" bodyStyle={{ padding: '8px 16px' }}>
                <Tabs
                    activeKey={activeTab}
                    onChange={setActiveTab}
                    items={[
                        {
                            key: 'fields',
                            label: (
                                <span>
                                    <AppstoreOutlined />
                                    <span style={{ marginLeft: 6 }}>数据字段浏览</span>
                                </span>
                            ),
                            children: <FieldBrowser market={market} symbol={symbol} />,
                        },
                        {
                            key: 'sectors',
                            label: (
                                <span>
                                    <BankOutlined />
                                    <span style={{ marginLeft: 6 }}>行业板块</span>
                                </span>
                            ),
                            children: <SectorExplorer market={market} symbol={symbol} />,
                        },
                    ]}
                />
            </Card>
        </div>
    );
};

export default DashboardPage;
