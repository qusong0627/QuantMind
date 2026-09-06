/**
 * StockScoreChart — K线(本地QuantDB) + 历史推理分数 + 模拟交易 + 时间回放
 * --------------------------------------------------
 * 基于 ECharts：
 *   - candlestick 画 K 线（红涨绿跌）
 *   - line 叠加推理分数折线（独立右侧轴）
 *   - scatter 标记推理日（买红 / 卖绿）
 *   - 时间回放：滑块控制"当前回放日期"，只显示该日之前的数据，逐步推进
 *   - 点击 K 线日期 → 模拟买卖（开盘买 / 收盘卖），统计收益
 */

import React, { useEffect, useMemo, useState, useRef } from 'react';
import * as echarts from 'echarts';
import ReactECharts from 'echarts-for-react';
import { Empty, Spin, Tag, Typography, Table, Button, InputNumber, Modal, Select, Input, Space, Switch } from 'antd';
import clsx from 'clsx';
import axios from 'axios';
import { modelTrainingService, StockScoreHistoryItem } from '../../services/modelTrainingService';
import { authService } from '../../features/auth/services/authService';
import { SERVICE_ENDPOINTS } from '../../config/services';

const { Text } = Typography;

interface KlineItem {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface Props {
  symbol: string;         // 纯数字，如 600365
  name?: string;
  /** 当前批次该股信息（排名/分数/板块/行业/市值） */
  stockInfo?: {
    rank?: number;
    score?: number;
    board?: string;
    industry?: string;
    market_cap_tier?: string;
    market_cap_yi?: number;
    negative_tag?: string;
  };
  market?: 'A' | 'HK' | 'US';
  days?: number;
  height?: number;
  /** 融合模型分数为 [-1,1] 时置 true，用自适应区间标注 */
  wideScale?: boolean;
  /** 当前推理的模型 ID：默认只加载该模型的分数，不显示全部模型 */
  modelId?: string;
}

interface Trade {
  id: string;
  date: string;        // 买入日
  side: 'buy' | 'sell';
  price: number;
  shares: number;
}

/** 自定义参考线：在分数轴上画虚线，标注分数含义（可买/热门/危险等） */
interface RefLine {
  id: string;
  value: number;        // 分数值（分数轴 yAxis 1）
  label: string;        // 显示名称，如 "可买"、"热门"
  color: string;        // 虚线颜色
  /** 是否显示（可单独开关） */
  visible?: boolean;
}

interface Position {
  openDate: string;
  buyPrice: number;
  shares: number;
}

const baseURL =
  (import.meta as any).env?.VITE_USER_API_URL || SERVICE_ENDPOINTS.USER_SERVICE;

/** 从 QuantDB 本地 parquet 取 K 线（/market/kline，A股优先 quantdb_parquet） */
async function fetchQuantdbKline(symbol: string, days: number): Promise<KlineItem[]> {
  const token = authService.getAccessToken();
  const resp = await axios.get(`${baseURL}/market/kline`, {
    params: { symbol, market: 'A', period: 'daily', days },
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    timeout: 30000,
  });
  return resp.data?.data?.items ?? [];
}

/** 上证指数 + MA20（/market/index-kline，QuantDB index_daily） */
async function fetchShanghaiIndex(days: number): Promise<{
  dates: string[];
  close: number[];
  ma20: (number | null)[];
  below_ma20: boolean | null;
  latest_close?: number | null;
  latest_ma20?: number | null;
} | null> {
  try {
    const token = authService.getAccessToken();
    // 后端接口 days 上限 500，clamp 避免 422
    const cappedDays = Math.min(500, Math.max(20, days));
    const resp = await axios.get(`${baseURL}/market/index-kline`, {
      params: { symbol: '000001.SH', days: cappedDays },
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      timeout: 15000,
    });
    const d = resp.data?.data;
    if (!d || !d.dates?.length) return null;
    return {
      dates: d.dates,
      close: d.close,
      ma20: d.ma20,
      below_ma20: d.below_ma20 ?? null,
      latest_close: d.latest_close ?? null,
      latest_ma20: d.latest_ma20 ?? null,
    };
  } catch (err: any) {
    console.error('[StockScoreChart] fetch index failed:', err);
    return null;
  }
}

/** 分数标注逻辑：按当前模型分数范围动态分档（融合模型高分如 2.7 也能分层） */
function annotateScore(score: number, wideScale = false, scoreMin?: number, scoreMax?: number): { label: string; color: string } | null {
  // 有动态范围时按分位数比例分档（更普适）
  if (typeof scoreMin === 'number' && typeof scoreMax === 'number' && scoreMax > scoreMin) {
    const range = scoreMax - scoreMin;
    const p = (score - scoreMin) / range; // 0~1
    if (p >= 0.80) return { label: '最高分', color: '#f43f5e' };
    if (p >= 0.60) return { label: '高分区', color: '#f97316' };
    if (p >= 0.40) return { label: '中分区', color: '#f59e0b' };
    if (p >= 0.20) return { label: '中低分', color: '#94a3b8' };
    return { label: '低分区', color: '#10b981' };
  }
  if (wideScale) {
    // 融合模型：高分/中高/中低/低分 四档（0.8 分位以上为最高分）
    if (score >= 0.50) return { label: '最高分', color: '#f43f5e' };
    if (score >= 0.20) return { label: '高分区', color: '#f97316' };
    if (score >= 0.05) return { label: '中高分', color: '#f59e0b' };
    if (score >= -0.05) return { label: '中低分', color: '#94a3b8' };
    if (score >= -0.30) return { label: '低分区', color: '#f97316' };
    return { label: '最低分', color: '#e11d48' };
  }
  if (score >= 0.20) return { label: '高分区', color: '#f43f5e' };
  if (score >= 0.15) return { label: '较高分区', color: '#f97316' };
  if (score >= 0.12) return { label: '中等偏高', color: '#f59e0b' };
  if (score >= 0.10) return { label: '中等分区', color: '#10b981' };
  if (score >= 0) return { label: '中低分区', color: '#94a3b8' };
  if (score <= -0.20) return { label: '低分区', color: '#e11d48' };
  if (score <= -0.15) return { label: '较低分区', color: '#f43f5e' };
  if (score <= -0.06) return { label: '低分区', color: '#f97316' };
  return { label: '中低分区', color: '#94a3b8' };
}

export const StockScoreChart: React.FC<Props> = ({ symbol, name, stockInfo, market = 'A', days = 3650, height = 420, wideScale = false, modelId }) => {
  const [loading, setLoading] = useState(true);
  const [klineItems, setKlineItems] = useState<KlineItem[]>([]);
  const [scoreItems, setScoreItems] = useState<StockScoreHistoryItem[]>([]);
  const [availableModels, setAvailableModels] = useState<Array<{ model_id: string; display_name?: string }>>([]);
  // 默认加载当前模型的分数（而非全部模型）；可手动切换其他模型查看
  const [selectedModel, setSelectedModel] = useState<string>(modelId ?? 'all');
  const [error, setError] = useState<string | null>(null);
  const chartRef = useRef<any>(null);
  // ── 上证指数 + MA20（大盘趋势叠加）──
  const [indexData, setIndexData] = useState<{
    dates: string[];
    close: number[];
    ma20: (number | null)[];
    below_ma20: boolean | null;
    latest_close?: number | null;
    latest_ma20?: number | null;
  } | null>(null);

  // ── 自定义参考线：按模型持久化（同一模型所有股票共用） ──
  // 每条线 = { id, value(分数), label(如"可买"), color }
  const [refLines, setRefLines] = useState<RefLine[]>([]);
  const [showRefLineModal, setShowRefLineModal] = useState(false);

  const refLineStorageKey = useMemo(() => {
    const m = selectedModel && selectedModel !== 'all' ? selectedModel : modelId;
    return `qm:ref-lines:${m || 'default'}`;
  }, [selectedModel, modelId]);

  // 加载参考线（localStorage，按模型隔离）
  useEffect(() => {
    try {
      const raw = localStorage.getItem(refLineStorageKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) setRefLines(parsed.filter((l: any) => l && typeof l.value === 'number'));
      } else if (selectedModel === 'all' && modelId) {
        // 未选择具体模型时也尝试加载当前模型配置
        const raw2 = localStorage.getItem(`qm:ref-lines:${modelId}`);
        if (raw2) {
          const parsed = JSON.parse(raw2);
          if (Array.isArray(parsed)) setRefLines(parsed.filter((l: any) => l && typeof l.value === 'number'));
        }
      }
    } catch { /* ignore */ }
  }, [refLineStorageKey, selectedModel, modelId]);

  const saveRefLines = (lines: RefLine[]) => {
    setRefLines(lines);
    try { localStorage.setItem(refLineStorageKey, JSON.stringify(lines)); } catch { /* ignore */ }
  };

  // 回放：默认关闭(全量)。开启后从开始日期显示到当前推进日
  const [replayEnabled, setReplayEnabled] = useState(false);
  const [startIdx, setStartIdx] = useState(0);     // 回放开始日期(索引)
  const [replayIdx, setReplayIdx] = useState(0);   // 当前推进到第几天(索引)

  // 模拟交易
  const [trades, setTrades] = useState<Trade[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [tradeModal, setTradeModal] = useState<{ date: string; open: number; close: number; idx: number } | null>(null);
  const [tradeShares, setTradeShares] = useState(100);

  // 归一化 symbol → suffix
  const suffixSymbol = useMemo(() => {
    if (symbol.includes('.')) return symbol;
    const code = symbol.replace(/^(SH|SZ|BJ)/, '');
    if (code.startsWith('688')) return `${code}.SH`;
    if (code.startsWith('30')) return `${code}.SZ`;
    if (code.startsWith('00') || code.startsWith('002') || code.startsWith('003')) return `${code}.SZ`;
    if (code.startsWith('60')) return `${code}.SH`;
    if (code.startsWith('4') || code.startsWith('8') || code.startsWith('9')) return `${code}.BJ`;
    return `${code}.SH`;
  }, [symbol]);

  /* ---- 拉数据 ---- */
  // K 线固定加载一次；分数默认加载当前模型（modelId），不带则全部模型
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setError(null);
      setLoading(true);
      try {
        const [kresp, sresp, idxresp] = await Promise.all([
          fetchQuantdbKline(suffixSymbol, days),
          modelTrainingService.getStockInferenceHistory(symbol, days, modelId),
          fetchShanghaiIndex(days),
        ]);
        if (cancelled) return;
        setKlineItems(kresp ?? []);
        setScoreItems(sresp?.items ?? []);
        setAvailableModels(sresp?.models ?? []);
        setIndexData(idxresp);
      } catch (e: any) {
        if (!cancelled) setError(e?.response?.data?.detail || e?.message || '加载失败');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [suffixSymbol, symbol, market, days, modelId]);

  // 切换模型时重新加载该模型的分数
  useEffect(() => {
    let cancelled = false;
    async function loadModelScores() {
      setLoading(true);
      try {
        const sresp = await modelTrainingService.getStockInferenceHistory(
          symbol, days, selectedModel === 'all' ? undefined : selectedModel,
        );
        if (cancelled) return;
        setScoreItems(sresp?.items ?? []);
      } catch (e: any) {
        if (!cancelled) setError(e?.response?.data?.detail || e?.message || '加载失败');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    if (availableModels.length > 0) void loadModelScores();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedModel, symbol, days]);

  // 外部传入的 modelId 变化时同步选中（例如切换到另一个模型的推理详情）
  useEffect(() => {
    if (modelId) setSelectedModel(modelId);
  }, [modelId]);

  // 回放索引默认到最新
  useEffect(() => {
    if (klineItems.length) {
      setReplayIdx(klineItems.length - 1);
      setStartIdx(Math.max(0, klineItems.length - 30)); // 默认从最近30个交易日起
    }
  }, [klineItems.length]);

  // 回放窗口：K线显示到 replayIdx-1（前一天收盘），信号显示到 replayIdx（当天信号，基于前一天数据）
  // 这样模拟交易时：先看到当天信号（开盘前决策），K线后出现（收盘后才知道）
  const visibleKline = useMemo(() => {
    if (!replayEnabled) return klineItems;
    const s = Math.min(startIdx, replayIdx);
    // K线只显示到 replayIdx-1，当前日K线未出现
    const e = Math.max(startIdx, Math.min(replayIdx, replayIdx - 1));
    if (e < s) return [];
    return klineItems.slice(s, e + 1);
  }, [klineItems, replayEnabled, startIdx, replayIdx]);
  const visibleScores = useMemo(() => {
    if (!replayEnabled) return scoreItems;
    const sDate = klineItems[Math.min(startIdx, klineItems.length - 1)]?.date || '';
    // 信号显示到 replayIdx（当天信号可见）
    const eDate = klineItems[Math.min(replayIdx, klineItems.length - 1)]?.date || '';
    return scoreItems.filter(x => x.trade_date >= sDate && x.trade_date <= eDate);
  }, [scoreItems, replayEnabled, startIdx, replayIdx, klineItems]);


  // 策略汇总统计：N 天中黄金/危险/买点各多少天
  const strategySummary = useMemo(() => {
    const scored = visibleScores.filter(s => s.fusion_score !== null && s.fusion_score !== undefined);
    const board = stockInfo?.board || '';
    const tier = stockInfo?.market_cap_tier || '';
    const negTag = stockInfo?.negative_tag || '';
    const staticWarnings: string[] = [];
    if (board.includes('科创')) staticWarnings.push('科创板');
    if (board.includes('北交')) staticWarnings.push('北交所');
    if (tier === '微盘') staticWarnings.push('微盘');
    if (negTag) staticWarnings.push(negTag);
    return { total: scored.length, staticWarnings };
  }, [visibleScores, stockInfo]);

  /* ---- 交易操作 ---- */
  const doBuy = () => {
    if (!tradeModal) return;
    const { date, open, idx } = tradeModal;
    const shares = tradeShares;
    const newTrade: Trade = { id: `buy-${date}-${Date.now()}`, date, side: 'buy', price: open, shares };
    setTrades([...trades, newTrade]);
    setPositions([...positions, { openDate: date, buyPrice: open, shares }]);
    setTradeModal(null);
  };
  const doSell = () => {
    if (!tradeModal) return;
    const { date, close } = tradeModal;
    const shares = tradeShares;
    const held = positions.reduce((s, p) => s + p.shares, 0);
    if (shares > held) return; // 不能超卖
    const newTrade: Trade = { id: `sell-${date}-${Date.now()}`, date, side: 'sell', price: close, shares };
    setTrades([...trades, newTrade]);
    // 从最早持仓扣减（用当前 positions 快照计算）
    let remaining = shares;
    const nextPositions: Position[] = [];
    for (const p of positions) {
      if (remaining <= 0) { nextPositions.push(p); continue; }
      const take = Math.min(p.shares, remaining);
      remaining -= take;
      if (p.shares - take > 0) nextPositions.push({ ...p, shares: p.shares - take });
    }
    setPositions(nextPositions);
    setTradeModal(null);
  };

  const clickableDates = useMemo(() => {
    if (!replayEnabled) return klineItems;
    // 回放：返回完整区间含当前信号日（当天 K 线空但可点击模拟买卖）
    const s = Math.min(startIdx, replayIdx);
    const e = Math.min(replayIdx, klineItems.length - 1);
    return klineItems.slice(s, e + 1);
  }, [replayEnabled, klineItems, startIdx, replayIdx]);

  // 默认 dataZoom 窗口：显示最近 100 根 K 线（总根数不足 100 则全量）
  const defaultZoom = useMemo(() => {
    const total = klineItems.length;
    const WINDOW = 100;
    if (total <= WINDOW) return { start: 0, end: 100 };
    const start = ((total - WINDOW) / total) * 100;
    return { start, end: 100 };
  }, [klineItems.length]);

  /* ---- ECharts 点击处理 ---- */
  const onChartClick = (params: any) => {
    const idx = params?.dataIndex;
    if (idx === undefined || idx === null) return;
    const k = clickableDates[idx];
    if (!k) return;
    setTradeModal({ date: k.date, open: k.open, close: k.close, idx });
  };

  // 点击日历日期 → 聚焦 K 线到该日前后 20 根
  const handleCalendarDateSelect = (date: string) => {
    const idx = klineItems.findIndex(k => k.date === date);
    if (idx < 0) return;
    if (replayEnabled) {
      // 回放：把推进位置设到该日附近（含之前20根）
      setStartIdx(Math.max(0, idx - 20));
      setReplayIdx(Math.min(idx + 20, klineItems.length - 1));
      return;
    }
    // 非回放：用 dataZoom 聚焦窗口
    const total = klineItems.length;
    if (total === 0) return;
    const half = 20;
    const startIdx0 = Math.max(0, idx - half);
    const endIdx0 = Math.min(total - 1, idx + half);
    const start = (startIdx0 / total) * 100;
    const end = ((endIdx0 + 1) / total) * 100;
    const chart = chartRef.current?.getEchartsInstance?.();
    if (chart) {
      chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 0, start, end });
      chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 1, start, end });
    }
  };

  /* ---- 构建 ECharts option ---- */
  const option = useMemo(() => {
    // x 轴日期：回放时含当前信号日（当天 K 线空，分数可见）；非回放全量
    const chartDates = replayEnabled
      ? klineItems.slice(Math.min(startIdx, replayIdx), Math.min(replayIdx, klineItems.length - 1) + 1).map(k => k.date)
      : klineItems.map(k => k.date);
    // 回放：当天 K 线为空数组（未出现），非回放全量。
    // 注意不能用 null/undefined：echarts 6.0.0 candlestick 遇到 null 数据项会抛
    // "Cannot read properties of null (reading 'value')"，导致整页 ErrorBoundary。
    // 空数组能保留 x 轴槽位（蜡烛位置不偏移）且不渲染该日。
    const chartKline = replayEnabled
      ? klineItems.slice(Math.min(startIdx, replayIdx), Math.min(replayIdx, klineItems.length - 1) + 1).map((k, i) => {
          // i 对应回放区间内；最后一天(当前信号日)K线不显示
          const isToday = Math.min(startIdx, replayIdx) + i === Math.min(replayIdx, klineItems.length - 1);
          return isToday ? [] : [k.open, k.close, k.low, k.high];
        })
      : klineItems.map(k => [k.open, k.close, k.low, k.high]);
    const dates = chartDates;
    const kdata = chartKline;

    // 分数按信号生效日对齐 K 线（trade_date 就是 K 线日）
    const scoreMap = new Map(visibleScores.filter(s => s.fusion_score !== null).map(s => [s.trade_date, s]));
    // 纯数字数组：ECharts line 数据项用数值，null 表示该日无分数(折线断开)
    const lineData = dates.map(d => {
      const s = scoreMap.get(d);
      return s ? Number(s.fusion_score) : null;
    });

    // 模拟交易的买卖点标记
    // 模拟交易的买卖点标记（放在 K 线价格轴上：买价上方/卖价下方）
    const tradeScatter: any[] = [];
    for (const t of trades) {
      const idx = dates.indexOf(t.date);
      if (idx < 0) continue;
      const close = kdata[idx]?.[1] ?? 0;
      tradeScatter.push({
        value: [idx, t.side === 'buy' ? close * 1.015 : close * 0.985],
        side: t.side, date: t.date, price: t.price,
      });
    }

    // 分数轴范围：始终按实际分数动态收紧（留 15% 边距），
    // 让分数波动占满纵向空间；无数据时回退到固定范围
    const allScores = visibleScores
      .map(s => Number(s.fusion_score))
      .filter(v => !Number.isNaN(v) && v !== null && v !== undefined);
    let scoreMin = wideScale ? -1.0 : -0.3;
    let scoreMax = wideScale ? 1.0 : 0.3;
    if (allScores.length > 0) {
      const dataMin = Math.min(...allScores);
      const dataMax = Math.max(...allScores);
      const pad = Math.max((dataMax - dataMin) * 0.15, 0.005);
      scoreMin = Math.floor((dataMin - pad) * 100) / 100;
      scoreMax = Math.ceil((dataMax + pad) * 100) / 100;
      if (scoreMax - scoreMin < 0.01) {
        // 全相等兜底：以该值为中心展开
        const c = (scoreMax + scoreMin) / 2;
        scoreMin = Math.floor((c - 0.005) * 100) / 100;
        scoreMax = Math.ceil((c + 0.005) * 100) / 100;
      }
    }
    return {
      animation: false,
      backgroundColor: '#ffffff',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params: any[]) => {
          if (!params?.length) return '';
          const idx = params[0]?.dataIndex;
          const d = dates[idx];
          const k = kdata[idx];
          const s = scoreMap.get(d);   // 按信号日取信号
          const score = s?.fusion_score;
          let html = `<div style="font-size:11px;color:#475569;font-weight:bold">${d}</div>`;
          if (k && k.length >= 4) html += `<div>开 ${k[0]} · 收 ${k[1]} · 低 ${k[2]} · 高 ${k[3]}</div>`;
          if (score !== null && score !== undefined) {
            const a = annotateScore(Number(score), wideScale, scoreMin, scoreMax);
            html += `<div>推理分数 <b style="color:${a?.color}">${Number(score).toFixed(4)}</b> <span style="color:${a?.color}">${a?.label || ''}</span></div>`;
            if (s?.score_rank !== null && s?.score_rank !== undefined) {
              html += `<div>当日排名 #${s.score_rank}</div>`;
            }
          }
          return html;
        },
      },
      legend: { data: ['K线', '推理分数', '模拟交易', ...(indexData ? ['上证指数', '上证MA20'] : [])], textStyle: { fontSize: 10 }, top: 0 },
      grid: { left: 8, right: 8, top: 28, bottom: 20, containLabel: true },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisLabel: { fontSize: 10, color: '#64748b' },
        splitLine: { show: false },
      },
      yAxis: [
        { type: 'value', scale: true, axisLabel: { fontSize: 10, color: '#64748b' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
        {
          type: 'value', position: 'right',
          // 分数轴范围：按实际分数动态扩展（融合模型高分如 2.7 不被截断）
          min: scoreMin,
          max: scoreMax,
          axisLabel: {
            fontSize: 9, color: '#94a3b8',
            // 轴收紧后刻度自适应小数位，避免 -0.0/0.0 这种无效刻度
            formatter: (v: number) => {
              const span = scoreMax - scoreMin;
              if (span < 0.1) return v.toFixed(3);
              if (span < 1) return v.toFixed(2);
              return v.toFixed(1);
            },
          },
          splitLine: { show: false },
        },
        // 第三轴：上证指数（右外侧，青色），用于大盘趋势叠加
        indexData ? {
          type: 'value', position: 'right', offset: 40,
          scale: true,
          axisLabel: { fontSize: 8, color: '#0ea5e9' },
          splitLine: { show: false },
          name: '上证指数',
          nameTextStyle: { fontSize: 8, color: '#0ea5e9' },
        } : null,
      ].filter(Boolean),
      dataZoom: replayEnabled
        ? []
        : [
            { type: 'inside', id: 'dzInside', start: defaultZoom.start, end: defaultZoom.end },
            { type: 'slider', id: 'dzSlider', start: defaultZoom.start, end: defaultZoom.end, height: 14, bottom: 0, textStyle: { fontSize: 9 } },
          ],
      series: [
        {
          name: 'K线', type: 'candlestick', data: kdata,
          itemStyle: { color: '#ef4444', color0: '#10b981', borderColor: '#ef4444', borderColor0: '#10b981' },
        },
        {
          name: '推理分数', type: 'line',
          data: lineData, yAxisIndex: 1,
          connectNulls: false,
          symbol: 'circle', symbolSize: 9,
          lineStyle: { width: 2.5, color: '#6366f1', type: 'solid', shadowBlur: 6, shadowColor: 'rgba(99,102,241,0.5)' },
          itemStyle: { color: '#6366f1', borderColor: '#ffffff', borderWidth: 2 },
          label: {
            show: true, position: 'top', fontSize: 9, fontWeight: 'bold',
            formatter: (p: any) => {
              if (p?.value === null || p?.value === undefined) return '';
              return Number(p.value).toFixed(3);
            },
            color: '#6366f1',
          },
          emphasis: { scale: 1.5 },
          markLine: {
            silent: true, symbol: 'none',
            // 自定义参考线优先；无配置时回退到默认线（只显示开启的线）
            data: refLines.length > 0
              ? refLines.filter(l => l.visible !== false).map(l => ({
                  yAxis: l.value,
                  lineStyle: { color: l.color, type: 'dashed', width: 1.5 },
                  label: { formatter: `${l.label} ${l.value >= 0 ? '+' : ''}${l.value.toFixed(2)}`, fontSize: 9, position: 'insideEndTop', color: l.color },
                }))
              : wideScale
                ? [
                    { yAxis: 0.50, lineStyle: { color: '#f43f5e', type: 'dashed', width: 1.5 }, label: { formatter: '高分线 0.50', fontSize: 9, position: 'insideEndTop' } },
                    { yAxis: -0.50, lineStyle: { color: '#10b981', type: 'dashed', width: 1.5 }, label: { formatter: '低分线 -0.50', fontSize: 9, position: 'insideEndBottom' } },
                  ].filter(l => l.yAxis >= scoreMin && l.yAxis <= scoreMax)
                : [
                    { yAxis: 0.10, lineStyle: { color: '#10b981', type: 'dashed', width: 1.5 }, label: { formatter: '参考 0.10', fontSize: 9, position: 'insideEndTop' } },
                    { yAxis: -0.15, lineStyle: { color: '#f43f5e', type: 'dashed', width: 1.5 }, label: { formatter: '参考 -0.15', fontSize: 9, position: 'insideEndBottom' } },
                  ].filter(l => l.yAxis >= scoreMin && l.yAxis <= scoreMax),
          },
        },
        {
          name: '模拟交易', type: 'scatter',
          data: tradeScatter,
          symbolSize: 14,
          symbol: (value: any, params: any) => params?.data?.side === 'sell' ? 'arrowDown' : 'arrowUp',
          itemStyle: { color: (p: any) => p.data?.side === 'sell' ? '#f59e0b' : '#8b5cf6', borderColor: '#ffffff', borderWidth: 1.5 },
          label: { show: true, position: 'top', fontSize: 9, formatter: (p: any) => `${p.data?.side === 'sell' ? '卖' : '买'}` },
          tooltip: {
            formatter: (p: any) => `<div>${p.data?.date} <b>${p.data?.side === 'sell' ? '卖出' : '买入'}</b> @${p.data?.price?.toFixed(2)}</div>`,
          },
        },
        // 上证指数 + MA20 叠加（第三轴 yAxisIndex=2）
        ...(indexData ? [
          {
            name: '上证指数', type: 'line',
            data: dates.map(d => {
              const idx = indexData.dates.indexOf(d);
              return idx >= 0 ? indexData.close[idx] : null;
            }),
            yAxisIndex: 2,
            connectNulls: true,
            symbol: 'none',
            lineStyle: { width: 1.2, color: '#0ea5e9', opacity: 0.7 },
            itemStyle: { color: '#0ea5e9' },
            zlevel: 2,
            tooltip: { formatter: (p: any) => `<div><b>${dates[p.dataIndex]}</b><br/><span style="color:#0ea5e9;font-weight:bold">上证指数 ${p.value !== null && p.value !== undefined ? Number(p.value).toFixed(2) : '-'}</span></div>` },
          },
          {
            name: '上证MA20', type: 'line',
            data: dates.map(d => {
              const idx = indexData.dates.indexOf(d);
              return idx >= 0 && indexData.ma20[idx] != null ? indexData.ma20[idx] : null;
            }),
            yAxisIndex: 2,
            connectNulls: true,
            symbol: 'none',
            lineStyle: { width: 1.2, color: '#f97316', type: 'dashed', opacity: 0.8 },
            itemStyle: { color: '#f97316' },
            zlevel: 2,
            tooltip: { formatter: (p: any) => `<div><b>${dates[p.dataIndex]}</b><br/><span style="color:#f97316;font-weight:bold">上证MA20 ${p.value !== null && p.value !== undefined ? Number(p.value).toFixed(2) : '-'}</span></div>` },
          },
        ] : []),
      ],
    };
  }, [visibleKline, visibleScores, trades, replayEnabled, defaultZoom, refLines, indexData]);

  // 打开回放时：点击逻辑绑定到 visibleKline 的索引
  const onEvents = useMemo(() => ({ click: onChartClick }), [clickableDates]);

  return (
    <div className="space-y-3">
      {/* 股票信息卡 */}
      <div className="rounded-2xl border border-slate-100 bg-slate-50/60 px-4 py-3 grid grid-cols-2 sm:grid-cols-6 gap-3">
        <div className="text-center">
          <Text className="block text-[11px] text-slate-400 font-black uppercase">股票</Text>
          <Text className="block text-xs font-black text-slate-800">{name || symbol}</Text>
        </div>
        <div className="text-center">
          <Text className="block text-[11px] text-slate-400 font-black uppercase">板块</Text>
          <Text className="block text-xs font-black text-slate-700">{stockInfo?.board || '—'}</Text>
        </div>
        <div className="text-center">
          <Text className="block text-[11px] text-slate-400 font-black uppercase">行业</Text>
          <Text className="block text-xs font-black text-slate-700">{stockInfo?.industry || '—'}</Text>
        </div>
        <div className="text-center">
          <Text className="block text-[11px] text-slate-400 font-black uppercase">市值</Text>
          <Text className="block text-xs font-black text-slate-700">
            {stockInfo?.market_cap_tier ? `${stockInfo.market_cap_tier}${stockInfo.market_cap_yi ? ` ${stockInfo.market_cap_yi}亿` : ''}` : '—'}
          </Text>
        </div>
        <div className="text-center">
          <Text className="block text-[11px] text-slate-400 font-black uppercase">当前排名</Text>
          <Text className="block text-xs font-black text-slate-700">#{stockInfo?.rank ?? '—'}</Text>
        </div>
        <div className="text-center">
          <Text className="block text-[11px] text-slate-400 font-black uppercase">当前分数</Text>
          <Text className={clsx('block text-xs font-black', (stockInfo?.score ?? 0) >= 0 ? 'text-rose-600' : 'text-emerald-600')}>
            {stockInfo?.score?.toFixed(4) ?? '—'}
          </Text>
        </div>
      </div>

      {/* 策略汇总条 + 模型选择器（合并一行：策略靠左，模型选择器靠右） */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-100 bg-white px-3 py-2">
        <Text className="text-[11px] text-slate-400 font-black uppercase flex-shrink-0">策略</Text>
        {strategySummary.total > 0 ? (
          <>
            <Tag color="geekblue" className="m-0 rounded-full text-[11px] font-bold px-2">共 {strategySummary.total} 推理日</Tag>
          </>
        ) : (
          <Text className="text-xs text-slate-400">无历史推理数据</Text>
        )}
        {strategySummary.staticWarnings.length > 0 && (
          <Tag color="gold" className="m-0 rounded-full text-[11px] font-bold px-2">
            ⚠ {strategySummary.staticWarnings.join(' · ')}
          </Tag>
        )}

        {/* 大盘状态：上证指数 vs MA20 */}
        {indexData && indexData.latest_close != null && (
          <span className={clsx('m-0 rounded-full text-[11px] font-bold px-2', indexData.below_ma20 ? 'bg-rose-50 text-rose-600 border-rose-200' : 'bg-emerald-50 text-emerald-600 border-emerald-200')}>
            上证{indexData.latest_close}{indexData.latest_ma20 != null ? ` / MA20 ${indexData.latest_ma20}` : ''} · 指数{indexData.below_ma20 ? '低于' : '高于'}MA20
          </span>
        )}

        {/* 模型选择器：靠右，切换不同模型推理分数 */}
        {availableModels.length > 1 && (
          <div className="flex items-center gap-1.5 ml-auto flex-shrink-0">
            <Text className="text-[11px] font-black text-slate-400 uppercase tracking-wide">模型</Text>
            <Select
              value={selectedModel}
              onChange={setSelectedModel}
              size="small"
              variant="borderless"
              className="w-[150px]"
              popupMatchSelectWidth={false}
              options={[
                { value: 'all', label: '全部模型' },
                ...availableModels.map((m) => ({
                  value: m.model_id,
                  label: m.display_name || m.model_id,
                })),
              ]}
            />
            {selectedModel !== 'all' && (
              <Tag className="m-0 border-0 bg-indigo-50 text-indigo-600 text-[8px] font-black rounded px-1.5">已过滤</Tag>
            )}
          </div>
        )}
      </div>

      {/* K 线图 */}
      <div className="relative" style={{ height }}>
        {!loading && !error && klineItems.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center z-10 bg-white/70">
            <Empty description={<span className="text-xs text-slate-400">无 K 线数据</span>} />
          </div>
        )}
        {!loading && error && (
          <div className="absolute inset-0 flex items-center justify-center z-10 bg-white/70">
            <Empty description={<span className="text-xs text-slate-400">{error}</span>} />
          </div>
        )}
        {loading ? (
          <div className="absolute inset-0 flex items-center justify-center z-10 bg-white/60"><Spin /></div>
        ) : (
          <ReactECharts
            ref={chartRef}
            echarts={echarts}
            option={option}
            onEvents={onEvents}
            style={{ height, width: '100%' }}
            notMerge
            lazyUpdate
          />
        )}
      </div>

      {/* 分数区间图例 */}
      <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
        <span className="rounded-md bg-emerald-50 text-emerald-600 px-1.5 py-0.5 font-bold">高分区 0.10-0.12</span>
        <span className="rounded-md bg-amber-50 text-amber-600 px-1.5 py-0.5 font-bold">中高分 0.12-0.15</span>
        <span className="rounded-md bg-orange-50 text-orange-600 px-1.5 py-0.5 font-bold">中分区 0.15-0.20</span>
        <span className="rounded-md bg-rose-50 text-rose-600 px-1.5 py-0.5 font-bold">低分区 ≤-0.15</span>
        <Button
          size="small"
          className="rounded-lg text-[11px] font-bold h-6 px-2 ml-auto"
          onClick={() => setShowRefLineModal(true)}
        >
          参考线 {refLines.length > 0 ? `(${refLines.length})` : ''}
        </Button>
      </div>

      {/* 历史分数日历视图 */}
      <ScoreCalendar items={visibleScores} onSelectDate={handleCalendarDateSelect} wideScale={wideScale} />

      {/* 模拟交易弹窗 */}
      <Modal
        open={!!tradeModal}
        onCancel={() => setTradeModal(null)}
        footer={null}
        width={420}
        centered
        title={<span className="text-sm font-black text-slate-800">模拟交易 · {tradeModal?.date}</span>}
      >
        {tradeModal && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 text-center">
              <div className="rounded-xl bg-emerald-50 border border-emerald-100 py-2">
                <Text className="block text-xs text-emerald-600 font-black">开盘价（买入价）</Text>
                <Text className="block font-black font-mono text-lg text-emerald-700">{tradeModal.open.toFixed(2)}</Text>
              </div>
              <div className="rounded-xl bg-rose-50 border border-rose-100 py-2">
                <Text className="block text-xs text-rose-600 font-black">收盘价（卖出价）</Text>
                <Text className="block font-black font-mono text-lg text-rose-700">{tradeModal.close.toFixed(2)}</Text>
              </div>
            </div>
            <div>
              <Text className="block text-xs text-slate-500 font-bold mb-1">数量（股）</Text>
              <InputNumber min={100} step={100} value={tradeShares} onChange={(v) => setTradeShares(Number(v) || 100)} className="w-full" />
            </div>
            <div className="flex gap-2">
              <Button type="primary" block className="rounded-lg font-bold" onClick={doBuy} danger>买入（开盘价 {tradeModal.open.toFixed(2)}）</Button>
              <Button block className="rounded-lg font-bold" onClick={doSell} style={{ background: '#10b981', borderColor: '#10b981', color: '#fff' }}>
                卖出（收盘价 {tradeModal.close.toFixed(2)}）
              </Button>
            </div>
            <Text className="block text-[11px] text-slate-400">买入按当日开盘价成交，卖出按当日收盘价成交。交易需按时间顺序进行。</Text>
          </div>
        )}
      </Modal>

      {/* 自定义参考线编辑弹窗 */}
      <Modal
        open={showRefLineModal}
        onCancel={() => setShowRefLineModal(false)}
        footer={null}
        width={520}
        centered
        title={<span className="text-sm font-black text-slate-800">自定义参考线 · {selectedModel !== 'all' ? '当前模型' : '全部'}</span>}
      >
        <div className="space-y-3">
          <Text className="block text-xs text-slate-400">
            在分数轴上画虚线，标注不同分数对应的分档名称。同一模型的股票共用此配置。每行左侧开关可单独控制该线的显示/隐藏。
          </Text>
          {refLines.length === 0 && (
            <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-6 text-center">
              <Text className="text-xs text-slate-400">暂无参考线，点击下方按钮添加</Text>
            </div>
          )}
          {refLines.map((l, idx) => (
            <div key={l.id} className="flex items-center gap-2 rounded-xl border border-slate-100 bg-white px-3 py-2">
              <Switch
                size="small"
                checked={l.visible !== false}
                onChange={(checked) => {
                  const next = refLines.map((x, i) => i === idx ? { ...x, visible: checked } : x);
                  saveRefLines(next);
                }}
              />
              <span className="w-3 h-0.5 rounded-full flex-shrink-0" style={{ background: l.color }} />
              <Input
                size="small"
                value={l.label}
                placeholder="参考线名称"
                className="w-28 text-xs"
                onChange={e => {
                  const next = refLines.map((x, i) => i === idx ? { ...x, label: e.target.value } : x);
                  saveRefLines(next);
                }}
              />
              <InputNumber
                size="small"
                value={l.value}
                step={0.05}
                className="w-24 text-xs"
                onChange={v => {
                  if (v === null) return;
                  const next = refLines.map((x, i) => i === idx ? { ...x, value: Number(v) } : x);
                  saveRefLines(next);
                }}
              />
              <Select
                size="small"
                value={l.color}
                className="w-20"
                onChange={c => {
                  const next = refLines.map((x, i) => i === idx ? { ...x, color: c } : x);
                  saveRefLines(next);
                }}
                options={[
                  { value: '#ef4444', label: '红' },
                  { value: '#10b981', label: '绿' },
                  { value: '#6366f1', label: '紫' },
                  { value: '#f59e0b', label: '橙' },
                  { value: '#f43f5e', label: '玫红' },
                  { value: '#0ea5e9', label: '蓝' },
                ]}
              />
              <Button
                size="small"
                type="text"
                danger
                className="ml-auto flex-shrink-0"
                onClick={() => saveRefLines(refLines.filter(x => x.id !== l.id))}
              >
                删除
              </Button>
            </div>
          ))}
          <div className="flex items-center gap-2 pt-1">
            <Button
              size="small"
              type="dashed"
              className="rounded-lg text-xs font-bold"
              onClick={() => saveRefLines([...refLines, { id: `rl_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`, value: 0.1, label: '', color: '#6366f1' }])}
            >
              + 添加参考线
            </Button>
            <Button
              size="small"
              type="text"
              className="ml-auto rounded-lg text-xs font-bold"
              onClick={() => saveRefLines([])}
            >
              清空
            </Button>
            <Button
              size="small"
              type="primary"
              className="rounded-lg text-xs font-bold bg-blue-600"
              onClick={() => setShowRefLineModal(false)}
            >
              完成
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
};

/* ==================== 历史分数日历视图 ==================== */
const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];

/** 按月日历：一次显示一个月（周一到周日），支持月份切换 */
const ScoreCalendar: React.FC<{
  items: StockScoreHistoryItem[];
  onSelectDate?: (date: string) => void;
  wideScale?: boolean;
}> = ({ items, onSelectDate, wideScale = false }) => {
  // 按信号生效日 trade_date 建立映射（与 K 线日一致）
  const byDate = useMemo(() => {
    const m = new Map<string, StockScoreHistoryItem>();
    for (const it of items) {
      const key = it.trade_date;
      if (!m.has(key)) m.set(key, it);
    }
    return m;
  }, [items]);

  // 可选月份列表（倒序）
  const months = useMemo(() => {
    const set = new Set<string>();
    for (const it of items) {
      set.add(it.trade_date.slice(0, 7));
    }
    return [...set].sort((a, b) => b.localeCompare(a));
  }, [items]);

  const [ym, setYm] = useState<string>(months[0] || '');

  // 月份变化时重置
  useEffect(() => {
    if (months.length && !months.includes(ym)) setYm(months[0]);
  }, [months, ym]);

  if (items.length === 0) {
    return (
      <div className="rounded-2xl border border-slate-100 bg-slate-50/60 p-4">
        <Text className="text-xs text-slate-400">暂无历史推理分数</Text>
      </div>
    );
  }

  // 计算当前月的日历网格：首列为周日（JS getDay(): 0=周日 直接对齐首列）
  const [year, month] = ym.split('-').map(Number);
  const daysInMonth = new Date(year, month, 0).getDate();
  const firstDow = new Date(year, month - 1, 1).getDay(); // 0=周日
  const cells: (string | null)[] = [
    ...Array.from({ length: firstDow }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => `${ym}-${String(i + 1).padStart(2, '0')}`),
  ];

  const prevMonth = () => {
    const idx = months.indexOf(ym);
    if (idx < months.length - 1) setYm(months[idx + 1]);
  };
  const nextMonth = () => {
    const idx = months.indexOf(ym);
    if (idx > 0) setYm(months[idx - 1]);
  };

  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/60 p-3">
      {/* 月份切换 */}
      <div className="flex items-center justify-between mb-3">
        <Button size="small" disabled={!months.includes(ym) || ym === months[months.length - 1]} onClick={prevMonth}
          className="rounded-lg text-xs font-bold h-7 px-3">‹ 上个月</Button>
        <div className="flex items-center gap-2">
          <Text className="text-sm font-black text-slate-800">{ym}</Text>
          <Text className="text-[11px] text-slate-400">该月 {byDate.size} 个推理日</Text>
        </div>
        <Button size="small" disabled={ym === months[0]} onClick={nextMonth}
          className="rounded-lg text-xs font-bold h-7 px-3">下个月 ›</Button>
      </div>

      {/* 星期表头 */}
      <div className="grid grid-cols-7 gap-1 mb-1.5">
        {WEEKDAYS.map(w => (
          <div key={w} className="text-center text-[11px] font-black text-slate-400 py-1">周{w}</div>
        ))}
      </div>

      {/* 日历网格 */}
      <div className="grid grid-cols-7 gap-1">
        {cells.map((date, i) => {
          if (!date) return <div key={`empty-${i}`} className="aspect-auto" />;
          const it = byDate.get(date);
          const day = Number(date.slice(8));
          return (
            <div
              key={date}
              onClick={() => { if (it && onSelectDate) onSelectDate(date); }}
              title={it ? (onSelectDate ? '点击跳转到该日K线' : undefined) : undefined}
              className={clsx(
                'rounded-lg border px-1.5 py-1.5 min-h-[52px]',
                it && onSelectDate ? 'cursor-pointer hover:ring-2 hover:ring-indigo-300 transition-shadow' : '',
                it
                  ? Number(it.fusion_score) >= 0
                    ? 'border-rose-200 bg-rose-50/70'
                    : 'border-emerald-200 bg-emerald-50/70'
                  : 'border-slate-100 bg-white/50',
              )}
            >
              <div className="flex items-center justify-between">
                <Text className={clsx('text-[11px] font-mono', it ? 'text-slate-600 font-bold' : 'text-slate-300')}>{day}</Text>
              </div>
              {it ? (
                <>
                  <Text className={clsx('block font-black font-mono text-xs mt-0.5', Number(it.fusion_score) >= 0 ? 'text-rose-600' : 'text-emerald-600')}>
                    {Number(it.fusion_score).toFixed(3)}
                  </Text>
                  <Text className="block text-[7px] font-bold truncate" style={{ color: annotateScore(Number(it.fusion_score), wideScale)?.color }}>
                    {annotateScore(Number(it.fusion_score), wideScale)?.label}
                  </Text>
                </>
              ) : (
                <Text className="block text-[8px] text-slate-200 mt-0.5">·</Text>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
