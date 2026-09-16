/**
 * KlineChart — 跨市场（A / HK / US）通用 K 线组件
 * --------------------------------------------------
 * 数据源：GET /api/v1/market/kline
 * 渲染：lightweight-charts CandlestickSeries + HistogramSeries（成交量）
 *
 * Props:
 *   - symbol: 600519.SH / 00700.HK / AAPL
 *   - market: A | HK | US
 *   - days: 默认 120
 *   - height: 默认 420
 *   - showVolume: 默认 true
 *
 * 用法：
 *   <KlineChart symbol="600519.SH" market="A" />
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    CandlestickSeries,
    CrosshairMode,
    HistogramSeries,
    createChart,
    type IChartApi,
    type ISeriesApi,
    type Time,
} from 'lightweight-charts';
import { Card, Empty, Segmented, Space, Spin, Tag, Typography } from 'antd';
import axios from 'axios';
import { authService } from '../features/auth/services/authService';
import { SERVICE_ENDPOINTS } from '../config/services';

const { Text } = Typography;

export type Market = 'A' | 'HK' | 'US';

export interface KlineChartProps {
    symbol: string;
    market?: Market;
    days?: number;
    height?: number;
    showVolume?: boolean;
    title?: string;
    /** 容器外部样式 */
    className?: string;
}

interface KlineItem {
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    amount?: number;
}

interface KlineResponse {
    success: boolean;
    data: {
        market: string;
        symbol: string;
        period: string;
        source_used: string;
        items: KlineItem[];
        fallbacks_tried?: string[];
        cleaning_report?: any;
    };
}

const baseURL =
    (import.meta as any).env?.VITE_USER_API_URL || SERVICE_ENDPOINTS.USER_SERVICE;

async function fetchKline(symbol: string, market: Market, days: number): Promise<KlineResponse['data']> {
    const token = authService.getAccessToken();
    const resp = await axios.get<KlineResponse>(`${baseURL}/market/kline`, {
        params: { symbol, market, period: 'daily', days },
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        timeout: 30000,
    });
    return resp.data.data;
}

export const KlineChart: React.FC<KlineChartProps> = ({
    symbol,
    market = 'A',
    days = 120,
    height = 420,
    showVolume = true,
    title,
    className,
}) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const chartRef = useRef<IChartApi | null>(null);
    const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
    const volRef = useRef<ISeriesApi<'Histogram'> | null>(null);

    const [range, setRange] = useState<number>(days);
    const [loading, setLoading] = useState(false);
    const [meta, setMeta] = useState<{ source_used: string; fallbacks_tried: string[] } | null>(
        null,
    );
    const [items, setItems] = useState<KlineItem[]>([]);
    const [error, setError] = useState<string | null>(null);

    /* ---- 初始化 chart 实例 ---- */
    useEffect(() => {
        if (!containerRef.current) return;
        const chart = createChart(containerRef.current, {
            width: containerRef.current.clientWidth,
            height,
            layout: {
                background: { color: '#ffffff' },
                textColor: '#475569',
            },
            grid: {
                vertLines: { color: '#f1f5f9' },
                horzLines: { color: '#f1f5f9' },
            },
            crosshair: { mode: CrosshairMode.Normal },
            rightPriceScale: { borderColor: '#e2e8f0' },
            timeScale: { borderColor: '#e2e8f0', timeVisible: false },
        });
        chartRef.current = chart;
        // 中国市场红涨绿跌；港 / 美沿用国际惯例 - 简化版默认中式
        const isChineseStyle = market === 'A' || market === 'HK';
        // v5 API：addCandlestickSeries 已移除（v4 写法在升级后静默失败）→ addSeries(CandlestickSeries)
        candleRef.current = chart.addSeries(CandlestickSeries, {
            upColor: isChineseStyle ? '#ef4444' : '#10b981',
            downColor: isChineseStyle ? '#10b981' : '#ef4444',
            borderUpColor: isChineseStyle ? '#ef4444' : '#10b981',
            borderDownColor: isChineseStyle ? '#10b981' : '#ef4444',
            wickUpColor: isChineseStyle ? '#ef4444' : '#10b981',
            wickDownColor: isChineseStyle ? '#10b981' : '#ef4444',
        });
        if (showVolume) {
            volRef.current = chart.addSeries(HistogramSeries, {
                color: '#94a3b8',
                priceFormat: { type: 'volume' },
                priceScaleId: '',
            });
            chart.priceScale('').applyOptions({
                scaleMargins: { top: 0.8, bottom: 0 },
            });
        }
        const onResize = () => {
            if (containerRef.current) {
                chart.applyOptions({ width: containerRef.current.clientWidth });
            }
        };
        window.addEventListener('resize', onResize);
        return () => {
            window.removeEventListener('resize', onResize);
            chart.remove();
            chartRef.current = null;
            candleRef.current = null;
            volRef.current = null;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [height, market, showVolume]);

    /* ---- 拉数据 ---- */
    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError(null);
        fetchKline(symbol, market, range)
            .then((d) => {
                if (cancelled) return;
                setItems(d.items || []);
                setMeta({
                    source_used: d.source_used,
                    fallbacks_tried: d.fallbacks_tried || [],
                });
            })
            .catch((e: any) => {
                if (cancelled) return;
                setError(e?.response?.data?.detail || e?.message || 'load failed');
                setItems([]);
            })
            .finally(() => !cancelled && setLoading(false));
        return () => {
            cancelled = true;
        };
    }, [symbol, market, range]);

    /* ---- 灌数据 ---- */
    useEffect(() => {
        if (!candleRef.current || items.length === 0) return;
        const candles = items.map((it) => ({
            time: it.date as Time,
            open: it.open,
            high: it.high,
            low: it.low,
            close: it.close,
        }));
        candleRef.current.setData(candles);
        if (volRef.current) {
            const isChineseStyle = market === 'A' || market === 'HK';
            const vols = items.map((it) => ({
                time: it.date as Time,
                value: it.volume,
                color:
                    it.close >= it.open
                        ? isChineseStyle
                            ? 'rgba(239,68,68,0.45)'
                            : 'rgba(16,185,129,0.45)'
                        : isChineseStyle
                          ? 'rgba(16,185,129,0.45)'
                          : 'rgba(239,68,68,0.45)',
            }));
            volRef.current.setData(vols);
        }
        chartRef.current?.timeScale().fitContent();
    }, [items, market]);

    const last = useMemo(() => (items.length ? items[items.length - 1] : null), [items]);
    const first = useMemo(() => (items.length ? items[0] : null), [items]);
    const chg = last && first ? ((last.close - first.close) / first.close) * 100 : 0;

    return (
        <Card
            bordered={false}
            className={`rounded-2xl shadow-sm ${className || ''}`}
            bodyStyle={{ padding: 16 }}
            title={
                <div className="flex items-center justify-between flex-wrap gap-2">
                    <div className="flex items-center gap-2">
                        <Text strong className="text-base">{title || symbol}</Text>
                        <Tag color="blue">{market}</Tag>
                        {meta?.source_used && (
                            <Tag color="default" className="font-mono text-[10px]">
                                {meta.source_used}
                            </Tag>
                        )}
                        {last && (
                            <Space className="ml-2">
                                <Text className="text-lg font-bold">{last.close.toFixed(2)}</Text>
                                <Text
                                    className={chg >= 0 ? 'text-red-500' : 'text-emerald-500'}
                                    style={{ fontWeight: 600 }}
                                >
                                    {chg >= 0 ? '+' : ''}
                                    {chg.toFixed(2)}%
                                </Text>
                            </Space>
                        )}
                    </div>
                    <Segmented
                        value={range}
                        onChange={(v) => setRange(Number(v))}
                        options={[
                            { label: '30D', value: 30 },
                            { label: '60D', value: 60 },
                            { label: '120D', value: 120 },
                            { label: '1Y', value: 250 },
                            { label: '2Y', value: 500 },
                        ]}
                        size="small"
                    />
                </div>
            }
        >
            <Spin spinning={loading}>
                <div style={{ position: 'relative', minHeight: height }}>
                    <div ref={containerRef} style={{ width: '100%', height }} />
                    {!loading && items.length === 0 && (
                        <div className="absolute inset-0 flex items-center justify-center">
                            <Empty description={error || '暂无数据'} />
                        </div>
                    )}
                </div>
                {meta?.fallbacks_tried && meta.fallbacks_tried.length > 0 && (
                    <div className="mt-2 text-[11px] text-slate-400">
                        fallback chain: {meta.fallbacks_tried.join(' → ')}
                    </div>
                )}
            </Spin>
        </Card>
    );
};

export default KlineChart;
