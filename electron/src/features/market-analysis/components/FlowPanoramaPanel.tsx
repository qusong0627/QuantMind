/**
 * 资金流全景面板（细案 v2 §5 状态中枢；v4/v5 版式改版）。
 *
 * 布局（v5）：工具栏（周期/体系/方向/个股 TopK/数据日期）→ 证据 meta 披露条 →
 * 板块标签融合条（统一搜索 + 热门板块）→ 4 层桑基 v2（全宽）→ 工作台左右栏：
 * 左列「一窗两签」（延续命中榜 ⇄ 板块榜；有连N证据时默认命中榜，两签常挂载、
 * hidden 切换，切签不重取）＋右列板块明细（选中个股时再挂个股详情卡）→
 * 底部档案（信号档案 + 压缩后的否决/口径行，可展开审计明细）。
 *
 * 状态纪律：
 * - 切体系 → 清板块选中（不同体系下名称不可混用）；切周期/方向/日期只重取；
 * - 外部 focusSymbol（顶栏搜索/其他面板）→ 打开个股卡；关闭时回报 onClearFocus
 *   以便同一代码可再次触发；
 * - v7 双向定位：板块入口跳转（榜行/桑基板块节点/热门 chip/搜索候选/命中榜）→
 *   明细到位自动下钻首位成分（开个股卡 + 高亮该行）；检索/聚焦个股 → 按
 *   attribution 切到其所属体系板块并高亮其行（已在当前板块榜内则只高亮不切板）；
 *   个股卡内「归因回板块」传 autoPick=false（保持当前个股锚点）；
 * - 证据 meta 缺失/陈旧/不适用一律显式披露，绝不假装有 streak 数据；
 * - 命中榜档位（hitLevel）由本面板持有：命中榜签切换 → 底部信号档案对应行高亮联动；
 * - 左列签（boardTab）null=按证据产物自动（有连N证据默认命中榜），用户点选后固定。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Spin } from 'antd';
import { Clock, Info, AlertCircle, Network, RefreshCw, Waves } from 'lucide-react';
import { FlowSankeyV2 } from './FlowSankeyV2';
import { SectorBoardList, SectorDetailPanel } from './SectorFlowWorkbench';
import { EvidenceHitList } from './EvidenceHitList';
import { StockFlowDetailCard } from './StockFlowDetailCard';
import { FlowEvidencePanel } from './FlowEvidencePanel';
import { FlowPredictiveCard } from './FlowPredictiveCard';
import { FlowProbabilityCard } from './FlowProbabilityCard';
import { TagBrowserStrip } from './TagBrowserStrip';
import { normalizeStockCode } from '../../../utils/portfolioUtils';
import {
  getFlowPanorama,
  getFlowProbability,
  getFlowSector,
  MoneyFlowError,
  panoramaEmptyHint,
  type FlowDirection,
  type FlowEvidenceMeta,
  type FlowPanoramaNode,
  type FlowPanoramaResponse,
  type FlowPeriod,
  type FlowProbabilityResponse,
  type FlowSectorDetailResponse,
  type FlowSectorSet,
  type FlowStockDetailResponse,
} from '../services/moneyFlowService';

const PERIODS: Array<{ id: FlowPeriod; label: string }> = [
  { id: '1d', label: '1日' },
  { id: '3d', label: '3日' },
  { id: '5d', label: '5日' },
  { id: '10d', label: '10日' },
  { id: '20d', label: '20日' },
];
const SECTOR_SETS: Array<{ id: FlowSectorSet; label: string }> = [
  { id: 'industry2', label: '行业二级' },
  { id: 'industry1', label: '行业一级' },
  { id: 'concept', label: '概念' },
];
const DIRECTIONS: Array<{ id: FlowDirection; label: string }> = [
  { id: 'all', label: '全部' },
  { id: 'inflow', label: '净流入' },
  { id: 'outflow', label: '净流出' },
];
const TOP_KS = [3, 5, 10] as const;
const TOP_SECTORS = 10; // 6 入 + 4 出（direction=all 时后端 6:4 拆分）

/** 工作台左列两签（v5）：命中榜在前（有证据时的默认签），板块榜次之。 */
const WORKBENCH_TABS = [
  { id: 'streak' as const, label: '延续命中榜' },
  { id: 'board' as const, label: '板块榜' },
];

/** 后端产物里的裸日期（20261010）→ 展示用 ISO；已是 ISO 则原样。 */
function fmtRawDate(s: string | null | undefined): string {
  if (!s) return '—';
  return s.length === 8 && /^\d{8}$/.test(s)
    ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`
    : s;
}

interface EvidenceStrip {
  tone: 'warn' | 'muted';
  text: string;
}

/**
 * 证据产物降级披露条（契约见后端 flow_panorama._evidence_context）：正常态
 * reason=null 且 available=true（不渲染提示条，产物日/悬停提示已就近在各 pane 头部）；
 * 降级态 reason ∈ not_applicable | not_built | stale。后端从不下发 'ok'——
 * 判可用性一律用 meta.evidence.available，不要拿 reason 比 'ok'。
 */
function evidenceStrip(meta: FlowEvidenceMeta | undefined): EvidenceStrip | null {
  if (!meta) return null;
  switch (meta.reason) {
    case 'stale':
      return {
        tone: 'warn',
        text: `延续证据产物停留在 ${meta.trade_date ?? '—'}，落后于最新行情——本次不展示连续净流入与条件超额`,
      };
    case 'not_built':
      return { tone: 'muted', text: '延续证据产物尚未生成（离线任务未运行）——本次不展示连续净流入与条件超额' };
    case 'not_applicable':
      return { tone: 'muted', text: '连续净流入证据当前只覆盖「行业二级」分类，该体系下不展示条件超额徽章' };
    default:
      return null;
  }
}

const Chip: React.FC<{ active: boolean; onClick: () => void; children: React.ReactNode }> = ({
  active,
  onClick,
  children,
}) => (
  <button
    onClick={onClick}
    className={`px-3.5 py-1 rounded-full text-xs font-extrabold transition-all duration-200 cursor-pointer whitespace-nowrap ${
      active
        ? 'bg-purple-600 text-white shadow-md shadow-purple-600/20 scale-[1.02]'
        : 'text-slate-600 hover:text-slate-900 hover:bg-slate-200/60'
    }`}
  >
    {children}
  </button>
);

const Group: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex items-center gap-2">
    <span className="text-xs font-extrabold text-slate-700 whitespace-nowrap">{label}</span>
    <div className="flex items-center bg-slate-100/90 border border-slate-200/60 p-1 rounded-full gap-1 shadow-2xs">
      {children}
    </div>
  </div>
);

export interface FlowPanoramaPanelProps {
  /** 页面快照日期（ISO yyyy-mm-dd）；undefined=最新 */
  date?: string;
  /** 外部聚焦个股（顶栏搜索/其他面板入口），非空时打开个股卡 */
  focusSymbol?: string | null;
  onClearFocus?: () => void;
}

export const FlowPanoramaPanel: React.FC<FlowPanoramaPanelProps> = ({ date, focusSymbol, onClearFocus }) => {
  const [period, setPeriod] = useState<FlowPeriod>('1d');
  const [sectorSet, setSectorSet] = useState<FlowSectorSet>('industry2');
  const [direction, setDirection] = useState<FlowDirection>('all');
  const [stocksPerSector, setStocksPerSector] = useState<number>(5);
  const [refreshTick, setRefreshTick] = useState(0);

  const [data, setData] = useState<FlowPanoramaResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selectedSector, setSelectedSector] = useState<string | null>(null);
  /** 板块入口点击数（含同名再点）——递增即触发明细重取与高亮反馈，不依赖状态是否变化。 */
  const [detailTick, setDetailTick] = useState(0);
  const [detail, setDetail] = useState<FlowSectorDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [selectedStock, setSelectedStock] = useState<{ symbol: string; name?: string } | null>(null);
  /** 成分贡献榜选中行（=当前个股卡个股）。面板级持有：检索个股打开卡片后，榜单据此高亮其所属板块内的该行。 */
  const [highlightSymbol, setHighlightSymbol] = useState<string | null>(null);
  /** 板块入口跳转后是否自动下钻首位成分（明细加载完成时消费一次；初始默认选板不消费）。 */
  const autoPickFirstRef = useRef(false);
  /** 延续命中榜档位（命中榜签内切换；底部信号档案行高亮联动）。null=按产物默认档。 */
  const [hitLevel, setHitLevel] = useState<number | null>(null);
  /** 左列签（v5）：null=按证据产物自动（有连N证据默认命中榜，否则板块榜），用户点选后固定。 */
  const [boardTab, setBoardTab] = useState<'streak' | 'board' | null>(null);
  const [colorMode, setColorMode] = useState<'flow' | 'prob'>('flow');
  const [probData, setProbData] = useState<FlowProbabilityResponse | null>(null);

  const panoSeq = useRef(0);
  const detailSeq = useRef(0);

  // 概率模型产物（着色/列表用；与全景独立取数，失败不影响主图）
  useEffect(() => {
    let cancelled = false;
    getFlowProbability(date)
      .then((d) => {
        if (!cancelled) setProbData(d);
      })
      .catch(() => {
        if (!cancelled) setProbData(null);
      });
    return () => {
      cancelled = true;
    };
  }, [date]);

  // ── 全景取数（周期/体系/方向/TopK/快照日期/手动刷新 全量重取） ──
  useEffect(() => {
    const seq = ++panoSeq.current;
    setLoading(true);
    setError(null);
    getFlowPanorama({
      period,
      sectorSet,
      topSectors: TOP_SECTORS,
      stocksPerSector,
      direction,
      date,
    })
      .then((d) => {
        if (seq !== panoSeq.current) return;
        setData(d);
        // 默认选中排名第一的板块（延续状态优先榜第一行）：进入即有明细可看；
        // 用户已选项（含体系切换后的复位）优先——只补空，不覆盖
        setSelectedSector((prev) => prev ?? (d?.available ? d.sectors?.[0]?.name ?? null : null));
      })
      .catch((e: unknown) => {
        if (seq !== panoSeq.current) return;
        setData(null);
        setError(e instanceof MoneyFlowError ? `资金流全景取数失败：${e.message}` : '资金流全景取数失败，请稍后重试');
      })
      .finally(() => {
        if (seq === panoSeq.current) setLoading(false);
      });
  }, [period, sectorSet, direction, stocksPerSector, date, refreshTick]);

  // ── 板块明细取数（detailTick：同名再点也重取——旧实现同名 setState 被 React 去重，
  //     表现为「点了一次就不能点第二次」，现每次点击都可见加载/高亮反馈） ──
  useEffect(() => {
    if (!selectedSector) {
      setDetail(null);
      setDetailError(null);
      autoPickFirstRef.current = false;
      return;
    }
    const seq = ++detailSeq.current;
    const autoPick = autoPickFirstRef.current; // 本次加载生效一次；期间新加载各自取值（seq 守卫丢弃晚到者）
    autoPickFirstRef.current = false;
    setDetailLoading(true);
    setDetailError(null);
    getFlowSector(selectedSector, { sectorSet, period, top: 30, date })
      .then((d) => {
        if (seq !== detailSeq.current) return;
        setDetail(d);
        // 板块入口跳转（v7）：明细到位即自动下钻首位成分（members 已按 |净额| 降序），
        // 个股卡现于最右、该行选中——「点板块要能看到其中的第一只个股」。
        if (autoPick && d.members[0]) handleSelectStock(d.members[0].symbol, d.members[0].name);
      })
      .catch((e: unknown) => {
        if (seq !== detailSeq.current) return;
        setDetail(null);
        setDetailError(
          e instanceof MoneyFlowError && e.status === 404
            ? '该板块在当前体系与日期下查无数据'
            : '板块明细取数失败，请稍后重试',
        );
      })
      .finally(() => {
        if (seq === detailSeq.current) setDetailLoading(false);
      });
  }, [selectedSector, sectorSet, period, date, detailTick]);

  // ── 外部聚焦个股 → 打开个股卡（v7：统一走 handleSelectStock，同步选中行） ──
  useEffect(() => {
    if (focusSymbol) handleSelectStock(focusSymbol);
  }, [focusSymbol]);

  const handleSectorSetChange = (s: FlowSectorSet) => {
    if (s === sectorSet) return;
    setSectorSet(s);
    setSelectedSector(null); // 不同体系的板块名称不可跨用
  };

  const handleSelectStock = (symbol: string, name?: string) => {
    // 入口统一归一化为前缀式（SH600519）：融合条个股 chip 来自 /stock-terminal/list
    // （后缀式 600519.SH）、桑基/成分榜为前缀式——不归一化则选中行/定位比较全部错配
    const norm = normalizeStockCode(symbol);
    setSelectedStock({ symbol: norm, name });
    setHighlightSymbol(norm);
  };

  const handleCloseStock = () => {
    setSelectedStock(null);
    setHighlightSymbol(null);
    onClearFocus?.();
  };

  /** 板块入口统一选板（榜行/桑基板块节点/热门 chip/搜索板块候选/命中榜）：同名再点=重取明细。
   *  autoPick（默认开）：明细加载完成后自动下钻首位成分并开个股卡——板块跳转即见其中最强个股；
   *  个股卡内「归因回板块」与其检索个股定位传 false（保持当前个股锚点，仅高亮其行）。 */
  const selectSector = (name: string, opts?: { autoPick?: boolean }) => {
    setSelectedSector(name);
    setDetailTick((t) => t + 1);
    autoPickFirstRef.current = opts?.autoPick !== false;
  };

  const handleNavigateSector = (name: string, set: string | null) => {
    if (set && set !== sectorSet) setSectorSet(set as FlowSectorSet);
    selectSector(name);
  };

  /** 个股卡内「归因回板块」：只切板块明细，不自动换股（当前个股卡保持并高亮）。 */
  const handleCardNavigateSector = (name: string, set: string | null) => {
    if (set && set !== sectorSet) setSectorSet(set as FlowSectorSet);
    selectSector(name, { autoPick: false });
  };

  /** 检索/聚焦个股取数到位 → 成分贡献榜定位并选中其所属板块（v7）。
   *  规则：该股已在当前板块成分榜内（含点行开卡）→ 只需行高亮，不切板块；
   *  否则按 attribution 匹配当前体系板块（回退 行业二级 → 行业一级 → 任一有体系项）切过去，
   *  明细加载后行高亮即出现——「检索个股跳转时也知道它属于哪个板块」。 */
  const resolveStockSectors = (d: FlowStockDetailResponse) => {
    if (normalizeStockCode(d.symbol ?? '') !== normalizeStockCode(selectedStock?.symbol ?? '')) return; // 陈旧响应（已换股；两侧归一化比较）
    if (selectedSector && detail?.members.some((m) => m.symbol === d.symbol)) return;
    const atts = d.attribution ?? [];
    const pick =
      atts.find((a) => a.sector_set === sectorSet) ??
      atts.find((a) => a.sector_set === 'industry2') ??
      atts.find((a) => a.sector_set === 'industry1') ??
      atts.find((a) => a.sector_set != null);
    if (!pick) return;
    if (pick.name === selectedSector) return; // 同板块（该股在 top30 外时行不可见，重取无益）
    if (pick.sector_set && pick.sector_set !== sectorSet) setSectorSet(pick.sector_set);
    selectSector(pick.name, { autoPick: false });
  };

  // 证据可用性（契约：正常态 reason=null 且 available=true；降级态 reason ∈
  // not_applicable | not_built | stale 且 available=false）——可用则默认「延续命中榜」签。
  const evidenceOk = data?.meta.evidence?.available === true;
  const flowTab: 'streak' | 'board' = boardTab ?? (evidenceOk ? 'streak' : 'board');

  const probReady = Boolean(probData?.available && probData.as_of_latest && probData.artifact);
  const probByKey = useMemo(() => {
    if (!probReady) return undefined;
    const map: Record<string, number | null> = {};
    const stocks = probData?.artifact?.latest?.stocks?.prob_map ?? {};
    for (const [sym, vals] of Object.entries(stocks)) map[sym] = vals['5'] ?? null;
    for (const sec of probData?.artifact?.latest?.sectors ?? []) {
      map[sec.name] = (sec.p5 as number | null | undefined) ?? null;
    }
    return map;
  }, [probReady, probData]);
  const probHi = probData?.artifact?.thresholds?.['5']?.hi ?? 0.55;
  const probLo = probData?.artifact?.thresholds?.['5']?.lo ?? 0.45;
  const sectorThresholds = useMemo(() => {
    const vals = (probData?.artifact?.latest?.sectors ?? [])
      .map((s) => (s.p5 as number | null | undefined))
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
    if (vals.length < 10) return { hi: 0.5, lo: 0.42 };
    vals.sort((a, b) => a - b);
    const q = (p: number) => vals[Math.min(vals.length - 1, Math.floor(p * vals.length))];
    return { hi: q(0.8), lo: q(0.2) };
  }, [probData]);

  const strip = evidenceStrip(data?.meta.evidence);
  const stripClass =
    strip?.tone === 'warn'
      ? 'bg-amber-50/90 text-amber-700 border-amber-200/70'
      : 'bg-slate-50 text-slate-500 border-slate-200/70';

  return (
    <div className="flex flex-col gap-3.5 min-w-0">
      {/* ── 工具栏 ── */}
      <div className="bg-white/95 backdrop-blur-md rounded-2xl p-3.5 border border-purple-100/80 shadow-xs flex flex-col gap-2.5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-4">
            <Group label="窗口">
              {PERIODS.map((p) => (
                <Chip key={p.id} active={period === p.id} onClick={() => setPeriod(p.id)}>
                  {p.label}
                </Chip>
              ))}
            </Group>
            <Group label="体系">
              {SECTOR_SETS.map((s) => (
                <Chip key={s.id} active={sectorSet === s.id} onClick={() => handleSectorSetChange(s.id)}>
                  {s.label}
                </Chip>
              ))}
            </Group>
            <Group label="方向">
              {DIRECTIONS.map((d) => (
                <Chip key={d.id} active={direction === d.id} onClick={() => setDirection(d.id)}>
                  {d.label}
                </Chip>
              ))}
            </Group>
            <Group label="个股数">
              {TOP_KS.map((k) => (
                <Chip key={k} active={stocksPerSector === k} onClick={() => setStocksPerSector(k)}>
                  Top {k}
                </Chip>
              ))}
            </Group>
            <Group label="着色">
              <Chip active={colorMode === 'flow'} onClick={() => setColorMode('flow')}>
                净额
              </Chip>
              <Chip
                active={colorMode === 'prob'}
                onClick={() => probReady && setColorMode('prob')}
              >
                概率{probReady ? '' : '（未就绪）'}
              </Chip>
            </Group>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {data?.trade_date && (
              <span className="flex items-center gap-1.5 px-3 py-1 rounded-full bg-slate-50 text-slate-600 border border-slate-200/70 text-[11px] font-bold font-mono whitespace-nowrap">
                <Clock className="w-3 h-3 text-purple-500" />
                <span>数据日期</span>
                <span className="text-purple-700 font-extrabold">{data.trade_date}</span>
                {data.meta.window_days && (
                  <span className="text-slate-400">
                    （窗口 {fmtRawDate(data.meta.window_days[0])}~{fmtRawDate(data.meta.window_days[1])}）
                  </span>
                )}
              </span>
            )}
            {data?.meta.coverage && (
              <span
                title="窗口内有 L2 资金流数据、且能映射到当前板块体系的个股覆盖率"
                className="px-3 py-1 rounded-full bg-purple-50 text-purple-700 border border-purple-200/70 text-[11px] font-bold font-mono whitespace-nowrap"
              >
                覆盖 {data.meta.coverage.covered_symbols}/{data.meta.coverage.flow_symbols}
                {data.meta.coverage.covered_pct !== null ? `（${data.meta.coverage.covered_pct}%）` : ''}
              </span>
            )}
            <button
              onClick={() => setRefreshTick((t) => t + 1)}
              disabled={loading}
              className="flex items-center gap-1 px-3 py-1 rounded-full bg-white text-slate-600 border border-slate-200/80 hover:bg-slate-50 text-[11px] font-extrabold transition-colors cursor-pointer disabled:opacity-50"
              title="重新取数（服务端缓存 5 分钟）"
            >
              <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
              刷新
            </button>
          </div>
        </div>

        {strip && (
          <div className={`flex items-start gap-1.5 px-3 py-1.5 rounded-xl border text-[11px] font-bold leading-snug ${stripClass}`}>
            <Info className="w-3.5 h-3.5 shrink-0 mt-px" />
            <span>{strip.text}</span>
          </div>
        )}
      </div>

      {/* ── 板块标签融合条（统一搜索 + 热门板块；数据面 /tags/*，不依赖全景取数） ── */}
      <TagBrowserStrip
        currentSectors={data?.sectors?.map((s) => s.name) ?? []}
        sectorSet={sectorSet}
        onSectorSetChange={handleSectorSetChange}
        onNavigateSector={handleNavigateSector}
        onSelectStock={handleSelectStock}
      />

      {/* ── 取数失败 ── */}
      {!loading && error && (
        <div className="bg-white/95 rounded-2xl border border-rose-100 p-6 flex flex-col items-center gap-2 text-center">
          <AlertCircle className="w-5 h-5 text-rose-400" />
          <span className="text-xs text-slate-600 font-bold">{error}</span>
          <button
            onClick={() => setRefreshTick((t) => t + 1)}
            className="mt-1 px-4 py-1.5 rounded-full bg-purple-600 text-white text-[11px] font-extrabold hover:bg-purple-500 cursor-pointer"
          >
            重试
          </button>
        </div>
      )}

      {/* ── 数据不可用（available=false，含原因） ── */}
      {!loading && !error && data && !data.available && (
        <div className="bg-white/95 rounded-2xl border border-purple-100/80 p-8 flex flex-col items-center gap-2.5 text-center">
          <Waves className="w-6 h-6 text-slate-300" />
          <span className="text-sm font-extrabold text-slate-700">资金流全景暂不可用</span>
          <span className="text-xs text-slate-400 max-w-md leading-relaxed">
            {panoramaEmptyHint(data.meta.reason)}
          </span>
        </div>
      )}

      {/* ── 桑基 v2 ── */}
      {!error && (!data || data.available) && (
        <div className="bg-white/95 backdrop-blur-md rounded-2xl p-3.5 border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col gap-2 min-w-0">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
              <Network className="w-3.5 h-3.5 text-purple-600" />
              <span>主力与散户资金流全景 · 四层桑基（主体 → 单型 → 板块 → 个股）</span>
            </h3>
            <span className="text-[10px] text-slate-400 font-mono">
              {colorMode === 'prob'
                ? '节点红=模型分高（未来强）/绿=低 · 边宽=|当前净额| · 个股层=每板块流入/流出双侧'
                : '红=净流入 · 绿=净流出 · 边宽=|净额| · 个股层=每板块流入/流出双侧 · 点节点联动左列榜单与右侧明细'}
            </span>
          </div>
          <FlowSankeyV2
            nodes={data?.nodes ?? []}
            links={data?.links ?? []}
            loading={loading}
            height={560}
            selectedSector={selectedSector}
            onSelectSector={selectSector}
            onSelectStock={(n: FlowPanoramaNode) => handleSelectStock(n.key, n.name)}
            colorMode={colorMode}
            probByKey={probByKey}
            probHi={probHi}
            probLo={probLo}
            sectorProbHi={sectorThresholds.hi}
            sectorProbLo={sectorThresholds.lo}
          />
        </div>
      )}

      {/* ── 未来强度概率（模型）：排序分 / 逆流候选 / 板块成分聚合 ── */}
      <FlowProbabilityCard date={date} onSelectStock={handleSelectStock} />

      {/* ── 资金流预测力（研究证据）：净流入金额对照 / 质量分分位 / 5×5 热力图 / 形态 ── */}
      <FlowPredictiveCard date={date} onSelectStock={handleSelectStock} />

      {/* ── 工作台 v5：左列一窗两签（延续命中榜 ⇄ 板块榜）+ 右列板块明细（选中个股再加挂个股卡）。
          任一行点击联动桑基高亮与右侧明细；两签常挂载（hidden 切换，避免切签重取证据）。 ── */}
      {!error && (!data || data.available) && (
        <div className="flex flex-col xl:flex-row gap-3.5 min-w-0 items-start">
          {/* 左列：一窗两签 */}
          <div className="w-full xl:w-[440px] xl:shrink-0 flex flex-col bg-white/95 backdrop-blur-md rounded-2xl border border-purple-100/80 shadow-md shadow-purple-500/5 min-w-0">
            <div className="flex items-center px-3 pt-2.5 pb-2 border-b border-slate-100">
              <div className="flex items-center bg-slate-100/90 border border-slate-200/60 p-0.5 rounded-full gap-0.5 w-fit">
                {WORKBENCH_TABS.map((t) => (
                  <button
                    key={t.id}
                    onClick={() => setBoardTab(t.id)}
                    className={`px-3.5 py-1 rounded-full text-[11px] font-extrabold transition-all duration-200 cursor-pointer whitespace-nowrap ${
                      flowTab === t.id
                        ? 'bg-purple-600 text-white shadow-sm shadow-purple-600/20'
                        : 'text-slate-600 hover:text-slate-900 hover:bg-slate-200/60'
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex flex-col p-3 min-w-0">
              <div className={flowTab === 'streak' ? 'flex min-w-0 flex-col' : 'hidden'}>
                <EvidenceHitList
                  date={date}
                  sectorSet={sectorSet}
                  level={hitLevel}
                  onLevelChange={setHitLevel}
                  onSelectSector={(name) => handleNavigateSector(name, 'industry2')}
                />
              </div>
              <div className={flowTab === 'board' ? 'flex min-w-0 flex-col' : 'hidden'}>
                <SectorBoardList
                  rows={data?.sectors ?? []}
                  selectedSector={selectedSector}
                  onSelectSector={selectSector}
                />
              </div>
            </div>
          </div>

          {/* 右列：板块明细（+ 条件个股卡） */}
          <div className="flex-1 min-w-0 flex flex-col xl:flex-row gap-3.5 items-stretch">
            <div className="flex-1 min-w-0">
              <SectorDetailPanel
                selectedSector={selectedSector}
                detail={selectedSector ? detail : null}
                detailLoading={detailLoading}
                detailError={detailError}
                refreshTick={detailTick}
                onSelectStock={handleSelectStock}
                highlightSymbol={highlightSymbol}
              />
            </div>
            {selectedStock && (
              <div className="w-full xl:w-[380px] shrink-0">
                <StockFlowDetailCard
                  symbol={selectedStock.symbol}
                  date={date}
                  onClose={handleCloseStock}
                  onNavigateSector={handleCardNavigateSector}
                  onDetailLoaded={resolveStockSectors}
                />
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── 延续证据台（底部档案：信号档案 + 否决清单 + 口径与披露） ── */}
      <FlowEvidencePanel date={date} activeLevel={hitLevel} />

      {/* ── 常驻披露（一行制：截断显示 + 悬停看全文，把纵向空间留给主体） ── */}
      {!error && data?.meta.disclosure && data.meta.disclosure.length > 0 && (
        <p
          className="px-1 text-[10px] text-slate-400 leading-snug truncate"
          title={data.meta.disclosure.join('\n')}
        >
          {data.meta.disclosure.join(' ')}
        </p>
      )}
    </div>
  );
};
