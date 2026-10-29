/** 因子概率板（市场分析 Tab）——细案 §六 P1 展示面。
 *
 * 数据：GET /api/v1/market-analysis/factor-board（决策板：精选因子合成分位 +
 * 板块三层共振（点击行 → 右侧卡钻取板块成分）+ 个股名单 + 入选因子 +
 * 持有期画像/个股四象限（末行并列）+ meta 质检条）。
 *
 * 展示纪律（CLAUDE.md「展示面/执行面用词边界」）：只说位置（靠前/靠后/居中），
 * 判定枚举经 VERDICT_META 映射，不出现买入/卖出字样。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Gauge,
  Grid3x3,
  History,
  Layers,
  ListOrdered,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { DateBadge, EmptyHint, NumText, PeriodChips, RankRow, SectionCard, fmtInt } from '../../market-analysis-shared/ui';
import { signalPositionLabel } from '../../shared/signalVocabulary';
import type { VerdictMeta } from '../factorBoardVocabulary';
import { decayMeta, quadChipCls, regimeNoteMeta, verdictMeta } from '../factorBoardVocabulary';
import { FactorICBar, FactorICIRScatter, SectorResonanceScatter } from './FactorBoardCharts';
import {
  FactorBoardError,
  FactorBoardMaskInfo,
  FactorBoardResponse,
  FactorBoardSectorItem,
  FactorBoardSectorMember,
  getFactorBoard,
  getFactorBoardMembers,
} from '../services/factorBoardService';

const SECTOR_TYPE_OPTIONS = [
  { id: 'industry_l2', label: '行业二级' },
  { id: 'industry_l1', label: '行业一级' },
  { id: 'concept', label: '概念板块' },
];

const STOCK_LIST_OPTIONS = [
  { id: 'top', label: '合成分位靠前 Top50' },
  { id: 'bottom', label: '合成分位靠后 Top50' },
  { id: 'resonant_long', label: '共振名单 · 靠前' },
  { id: 'resonant_short', label: '共振名单 · 靠后' },
];

const HORIZON_KEYS = ['1', '2', '5', '10', '20'];

/** 入选因子清单默认折叠预览行数（清单长时卡片过高；点展开看全量，按次会话状态）。 */
const FACTOR_PREVIEW_ROWS = 4;

/** 象限键序 = 因子分位在前、模型分位在后（sectors.quadrant_frame: fq+mq）。 */
const QUAD_LEVELS = [
  { id: 'H', label: '高' },
  { id: 'M', label: '中' },
  { id: 'L', label: '低' },
] as const;

function VerdictChip({ verdict }: { verdict: string }) {
  const meta = verdictMeta(verdict);
  return (
    <span
      title={meta.hint}
      className={`inline-flex items-center px-1.5 py-0.5 rounded-md border text-[10px] font-extrabold whitespace-nowrap ${meta.cls}`}
    >
      {meta.label}
    </span>
  );
}

/** 表格内小号状态 chip（衰减/制度注记等，词表见 factorBoardVocabulary）。 */
function StateChip({ meta }: { meta: VerdictMeta }) {
  return (
    <span
      title={meta.hint}
      className={`inline-flex items-center px-1 py-0.5 rounded-md border text-[9px] font-extrabold whitespace-nowrap ${meta.cls}`}
    >
      {meta.label}
    </span>
  );
}

/** 掩码 chip 三态（命中当日 / 缺当日行 / 陈旧超限）；数据缺失 → null 不渲染。 */
function maskChip(mask: FactorBoardMaskInfo | null | undefined): { cls: string; label: string; hint: string } | null {
  if (!mask) return null;
  if (!mask.missing && mask.mask_date !== null) {
    return {
      cls: 'bg-emerald-50 border-emerald-200/80 text-emerald-700',
      label: `掩码 ${mask.mask_date}（当日命中）`,
      hint: '可交易掩码命中决策日：涨跌停挡多头、跌停挡空头、停牌两腿都挡（名单层过滤口径）',
    };
  }
  if (mask.stale_days !== null && mask.stale_days > 7) {
    return {
      cls: 'bg-red-50 border-red-200/80 text-red-700',
      label: `掩码陈旧：截至 ${mask.mask_max_dt ?? '—'}（${mask.stale_days} 天）`,
      hint: '掩码停更超 7 天：涨跌停/停牌是当日状态，无当日行一律不过滤（绝不沿用旧日行），此处显形以便排查停更',
    };
  }
  return {
    cls: 'bg-amber-50 border-amber-200/80 text-amber-700',
    label: `掩码：缺当日行（最新 ${mask.mask_max_dt ?? '—'}）`,
    hint: '掩码无决策日当天的行：名单层不过滤（涨跌停/停牌是当日状态，不沿用旧日行）',
  };
}

function SectorRow({
  item,
  rank,
  q67,
  q33,
  scale,
  active,
  onPick,
}: {
  item: FactorBoardSectorItem;
  rank: number;
  q67: number | null;
  q33: number | null;
  /** 双向热力条比例尺（当日全部板块 |均分| 与 |q67|/|q33| 的最大值 ×1.12 留边）。 */
  scale: number;
  active: boolean;
  onPick: (name: string) => void;
}) {
  const barPct = item.fac === null ? 0 : (Math.abs(item.fac) / scale) * 50;
  const tick = (v: number | null) =>
    v === null
      ? null
      : Math.min(Math.max(50 + (v / scale) * 50, 2), 98);
  const q67Pos = tick(q67);
  const q33Pos = tick(q33);
  return (
    <div
      onClick={() => onPick(item.name)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onPick(item.name);
        }
      }}
      title={`${item.name}：成分 ${item.n} 只 · 有评分 ${item.n_scored ?? '—'} 只；点击查看成分股`}
      className={`group flex items-center gap-2 px-1 py-1.5 border-b border-slate-50 last:border-0 cursor-pointer transition-colors ${
        active ? 'bg-purple-50/70' : 'hover:bg-slate-50/80'
      }`}
    >
      {/* 名称列吸收余宽；指标列定宽右簇——行间对齐 */}
      <span
        className={`w-5 h-5 rounded-md grid place-items-center text-[10px] font-extrabold flex-shrink-0 ${
          rank <= 3 ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-500'
        }`}
      >
        {rank}
      </span>
      <div className="flex-1 min-w-0 flex items-center gap-1.5">
        <span className={`text-xs font-bold truncate ${active ? 'text-purple-700' : 'text-slate-800'}`}>
          {item.name}
        </span>
        <span className="text-[10px] font-mono text-slate-400 flex-shrink-0">{item.n}只</span>
      </div>
      {/* 双向热力条：中轴=0，右红（正）/左绿（负）；细刻度=三层判定入选线 q67/q33 */}
      <div className="w-36 flex-shrink-0 flex items-center gap-1.5">
        <div
          className="relative flex-1 h-1.5 rounded-full bg-slate-100"
          title={`板块均分 ${item.fac ?? '—'}（中轴=0）；刻度线=入选线 q67=${q67 ?? '—'} / q33=${q33 ?? '—'}`}
        >
          <div className="absolute left-1/2 -top-0.5 -bottom-0.5 w-px bg-slate-300/80 z-10" />
          {q67Pos !== null && (
            <div className="absolute -top-1 -bottom-1 w-px bg-red-300/90 z-10" style={{ left: `${q67Pos.toFixed(1)}%` }} />
          )}
          {q33Pos !== null && (
            <div className="absolute -top-1 -bottom-1 w-px bg-green-400/80 z-10" style={{ left: `${q33Pos.toFixed(1)}%` }} />
          )}
          {item.fac !== null && (
            <div
              className={`absolute top-0 bottom-0 ${
                item.fac >= 0 ? 'left-1/2 rounded-r-full bg-red-400' : 'right-1/2 rounded-l-full bg-green-500'
              }`}
              style={{ width: `${Math.max(barPct, 1.5)}%` }}
            />
          )}
        </div>
        <span className="w-11 text-right flex-shrink-0">
          <NumText value={item.fac} digits={3} />
        </span>
      </div>
      <span
        className="hidden 2xl:inline-flex w-[86px] flex-shrink-0 justify-end text-[10px] font-mono text-slate-400 whitespace-nowrap"
        title="个股广度（成分内合成分>0 占比）/ 模型广度（成分内模型分位≥0.5 占比）"
      >
        广 <span className="text-slate-600 font-bold">{item.fbreadth === null ? '—' : item.fbreadth.toFixed(2)}</span>
        /<span className="text-slate-600 font-bold">{item.mbreadth === null ? '—' : item.mbreadth.toFixed(2)}</span>
      </span>
      <span className="flex-shrink-0">
        <VerdictChip verdict={item.verdict} />
      </span>
      <ChevronRight
        className={`w-3.5 h-3.5 flex-shrink-0 transition-colors ${active ? 'text-purple-400' : 'text-slate-200 group-hover:text-purple-400'}`}
      />
    </div>
  );
}

/** 行内标的（名单行与板块成分行共用；成分行无 sector_l2）。 */
type StockRowItem = {
  symbol: string;
  name: string | null;
  composite: number | null;
  model_rank: number | null;
  quadrant: string | null;
  sector_l2?: string | null;
};

function StockRow({ rank, item }: { rank: number; item: StockRowItem }) {
  return (
    <RankRow
      rank={rank}
      name={item.name || item.symbol}
      nameSub={item.symbol}
      main={
        item.sector_l2 ? (
          <span className="text-[10px] text-slate-400 truncate flex-shrink-0" title="所属行业（二级）">
            {item.sector_l2}
          </span>
        ) : undefined
      }
      right={
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-[10px] font-mono text-slate-400" title="模型分位（同批推理截面 rank_pct）">
            模型 {item.model_rank === null ? '—' : item.model_rank.toFixed(2)}
          </span>
          <span className="w-14 text-right">
            <NumText value={item.composite} digits={3} />
          </span>
          <span
            className={`w-7 text-center text-[10px] font-extrabold font-mono rounded border px-0.5 ${quadChipCls(item.quadrant)}`}
            title="象限=因子分位+模型分位（高/中/低）；HH 红 / LL 绿 / 背离琥珀；缺失显示 —"
          >
            {item.quadrant ?? '—'}
          </span>
        </div>
      }
    />
  );
}

export const FactorBoardPanel: React.FC = () => {
  const [board, setBoard] = useState<FactorBoardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; status?: number } | null>(null);
  const [selectedDate, setSelectedDate] = useState<string | undefined>();
  const [sectorType, setSectorType] = useState<string>('industry_l2');
  const [stockList, setStockList] = useState<string>('top');
  const [sectorView, setSectorView] = useState<'chart' | 'table'>('chart');
  const [factorView, setFactorView] = useState<'chart' | 'table'>('chart');
  // 板块成分钻取（master-detail：点左侧板块行 → 右侧卡切成分视图）
  const [sectorPick, setSectorPick] = useState<{ type: string; name: string } | null>(null);
  const [members, setMembers] = useState<FactorBoardSectorMember[] | null>(null);
  /** `members` 当前归属的取数键（切板块时先失配 → 渲染走加载态，绝不闪现上一板块的成分）。 */
  const [membersKey, setMembersKey] = useState<string | null>(null);
  const [membersLoading, setMembersLoading] = useState(false);
  const [membersError, setMembersError] = useState<string | null>(null);
  const [factorsExpanded, setFactorsExpanded] = useState(false);
  const membersCache = useRef<Map<string, FactorBoardSectorMember[]>>(new Map());

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getFactorBoard(selectedDate)
      .then((d) => {
        if (!cancelled) setBoard(d);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setBoard(null);
        setError({
          message: e instanceof Error ? e.message : '因子概率板加载失败',
          status: e instanceof FactorBoardError ? e.status : undefined,
        });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedDate]);

  const dateOptions = useMemo(() => {
    const dates = board?.available_dates ?? [];
    return [{ id: '', label: '最新' }, ...dates.slice(0, 8).map((d) => ({ id: d, label: d.slice(5) }))];
  }, [board]);

  // 板块成分取数（按 决策日|分类|板块名 缓存；再点同板块 = 收起，回名单视图）
  useEffect(() => {
    if (!sectorPick) return;
    const key = `${selectedDate ?? ''}|${sectorPick.type}|${sectorPick.name}`;
    const cached = membersCache.current.get(key);
    if (cached) {
      setMembers(cached);
      setMembersKey(key);
      setMembersError(null);
      setMembersLoading(false);
      return;
    }
    let cancelled = false;
    setMembers(null);
    setMembersKey(null);
    setMembersError(null);
    setMembersLoading(true);
    getFactorBoardMembers(sectorPick.type, sectorPick.name, selectedDate)
      .then((d) => {
        if (cancelled) return;
        membersCache.current.set(key, d.items);
        setMembers(d.items);
        setMembersKey(key);
      })
      .catch((e: unknown) => {
        if (!cancelled) setMembersError(e instanceof Error ? e.message : '板块成分加载失败');
      })
      .finally(() => {
        if (!cancelled) setMembersLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sectorPick, selectedDate]);

  // 双向热力条比例尺：当日板块 |均分| 与 |q67|/|q33| 的全局最大 ×1.12 留边（同日内部可比）
  const sectorScale = useMemo(() => {
    if (!board) return 0.01;
    const group = board.sectors[sectorType as keyof typeof board.sectors];
    let m = 0;
    for (const it of group?.items ?? []) {
      if (it.fac !== null) m = Math.max(m, Math.abs(it.fac));
    }
    for (const q of [group?.q67, group?.q33]) {
      if (q !== null && q !== undefined) m = Math.max(m, Math.abs(q));
    }
    return m > 0 ? m * 1.12 : 0.01;
  }, [board, sectorType]);

  const pickSector = (name: string) => {
    setSectorPick((prev) =>
      prev && prev.type === sectorType && prev.name === name ? null : { type: sectorType, name },
    );
  };

  // 当前钻取键与其归属数据（membersKey 失配 → 视为未就绪，走加载态）
  const drillKey = sectorPick ? `${selectedDate ?? ''}|${sectorPick.type}|${sectorPick.name}` : null;
  const drillMembers = drillKey !== null && membersKey === drillKey ? members : null;

  if (loading && !board) {
    return (
      <SectionCard title="因子概率板" icon={<Sparkles className="w-3.5 h-3.5 text-purple-600" />}>
        <EmptyHint loading />
      </SectionCard>
    );
  }

  if (!board) {
    const notReady = error?.status === 404;
    return (
      <SectionCard title="因子概率板" icon={<Sparkles className="w-3.5 h-3.5 text-purple-600" />}>
        <EmptyHint
          text={
            notReady
              ? '因子概率板尚未生成（每交易日 08:40 自动构建一版；也可在服务器执行 python backend/scripts/schedule_ctl.py run factor_board 手动重跑）'
              : error?.message || '因子概率板加载失败'
          }
        />
      </SectionCard>
    );
  }

  const { meta, resonance, validation } = board;
  const sectorGroup = board.sectors[sectorType as keyof typeof board.sectors];
  const stocks = board.stocks[stockList as keyof typeof board.stocks] ?? [];
  const bySide = meta.model.by_side ?? {};
  const sideEntries = Object.entries(bySide);
  const regime = board.regime;
  const dedup = board.dedup;
  const mask = maskChip(meta.coverage.mask);
  const rollingOos = validation.rolling_oos;
  const dedupCut = dedup ? dedup.dropped.length + dedup.pruned.length : 0;
  // 「二次去重：…」类 meta.warnings（builder.py 产出）与 dedup 块重复 → 并入下方去重 chip；
  // 无 dedup 块（异常回退）时保留原文，绝不吞告警。
  const nonDedupWarnings = dedup ? meta.warnings.filter((w) => !w.startsWith('二次去重：')) : meta.warnings;
  // 1.1 因子列（近 20 日 / 净 IC）：按行键探测——1.0 历史产物无键 → 整列隐藏
  const hasNetCols = board.factors.some((f) => 'decay' in f || 'net_ic60' in f);
  const factorsFoldable = board.factors.length > FACTOR_PREVIEW_ROWS;
  const shownFactors = factorsFoldable && !factorsExpanded
    ? board.factors.slice(0, FACTOR_PREVIEW_ROWS)
    : board.factors;

  return (
    <div className="flex flex-col gap-3.5">
      {/* ── 质检与口径条 ─────────────────────────────────────── */}
      <SectionCard
        title="质检与口径"
        icon={<ShieldCheck className="w-3.5 h-3.5 text-purple-600" />}
        extra={
          <div className="flex items-center gap-2">
            <PeriodChips
              options={dateOptions}
              value={selectedDate ?? ''}
              onChange={(id) => {
                setSelectedDate(id || undefined);
                setSectorPick(null);
              }}
            />
          </div>
        }
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] text-slate-500">
          <span className="flex items-center gap-1.5">
            <CalendarDays className="w-3.5 h-3.5 text-purple-500" />
            <span>决策日</span>
            <DateBadge date={meta.decision_date} />
            <span>数据截面</span>
            <DateBadge date={meta.trade_date} />
          </span>
          <span className="font-mono text-slate-400" title="产物生成时间（UTC，浏览器按本地时区显示）">
            生成于 {new Date(meta.generated_at).toLocaleString('zh-CN', { hour12: false })}
          </span>
          <span className="font-mono">
            候选 {fmtInt(meta.coverage.pool)} → 评估 {fmtInt(meta.coverage.evaluated)} → 过闸{' '}
            {fmtInt(meta.coverage.gated)} → <b className="text-purple-700">入选 {fmtInt(meta.coverage.selected)}</b>（{meta.coverage.families} 族）
          </span>
          <span className="font-mono">
            合成覆盖 {fmtInt(meta.coverage.composite_n)} / 模型覆盖 {fmtInt(meta.coverage.model_cov_n)}
          </span>
          <span className="font-mono">
            因子↔模型 <b className="text-purple-700">ρ={resonance.spearman === null ? '—' : resonance.spearman.toFixed(4)}</b>（n={fmtInt(resonance.n)}）
          </span>
          {sideEntries.length > 0 && (
            <span className="font-mono" title="模型信号在当日截面的位置分布（同一批推理）">
              信号位置：
              {sideEntries.map(([side, n]) => `${signalPositionLabel(side)} ${fmtInt(n)}`).join(' / ')}
            </span>
          )}
          <span className="font-mono" title={`模型信号批 run_id=${meta.model.run_id}（source=${meta.model.source}）`}>
            信号批 {meta.model.run_id.slice(0, 24)}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {meta.model.stale ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-50 border border-red-200/80 text-[10px] font-extrabold text-red-700">
              <AlertTriangle className="w-3 h-3" /> 模型信号非当日（已回退 ≤7 天）
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-200/80 text-[10px] font-extrabold text-emerald-700">
              <ShieldCheck className="w-3 h-3" /> 模型信号为当日批
            </span>
          )}
          {meta.warnings.length === 0 ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-200/80 text-[10px] font-extrabold text-emerald-700">
              <ShieldCheck className="w-3 h-3" /> 质检无告警
            </span>
          ) : (
            nonDedupWarnings.map((w, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200/80 text-[10px] font-extrabold text-amber-700"
              >
                <AlertTriangle className="w-3 h-3" /> {w}
              </span>
            ))
          )}
          {validation.in_sample && (
            <span
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-sky-50 border border-sky-200/80 text-[10px] font-extrabold text-sky-700"
              title={validation.note}
            >
              样本内自洽（入选窗=评估窗）
            </span>
          )}
          {dedup && (
            <span
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-extrabold ${
                dedup.warnings.length > 0
                  ? 'bg-amber-50 border-amber-200/80 text-amber-700'
                  : 'bg-slate-50 border-slate-200/80 text-slate-600'
              }`}
              title={`入选内二次去重：族头替补池 ${dedup.n_reserves}；|ρ|≥${dedup.reject} 判重、[${dedup.warn},${dedup.reject}) 记警 ${dedup.warnings.length} 对。本版：席位置换 ${dedup.dropped.length}、族修剪 ${dedup.pruned.length}。ρ=快照窗内合成序列的逐日 Spearman（近重因子按 |IC60| 降序占席，被拒者由替补递补）`}
            >
              去重：池 {fmtInt(dedup.n_pool)} → 入选 {fmtInt(dedup.n_kept)}（剔 {fmtInt(dedupCut)}
              {dedup.warnings.length > 0 ? ` · 警 ${dedup.warnings.length} 对` : ''}）
            </span>
          )}
          {regime && (
            <span
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-extrabold font-mono ${
                regime.weak_count > 0
                  ? 'bg-amber-50 border-amber-200/80 text-amber-700'
                  : 'bg-slate-50 border-slate-200/80 text-slate-600'
              }`}
              title={`制度状态 ${regime.state}@${regime.state_asof}${regime.state_lag_days > 0 ? `（滞后 ${regime.state_lag_days} 天取最近行）` : ''}；长窗 ${regime.lookback_days} 日 / 评估 ${regime.eval_days} 日。弱桶谓词=桶内方向对齐 IC 均值≤0 且 ≥15 日 → 权重 ×${regime.weak_mult}（只降不翻）`}
            >
              制度：{regime.state}（弱桶 {regime.weak_count} / 调整 {regime.adjusted_count}）
            </span>
          )}
          {mask && (
            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-extrabold font-mono ${mask.cls}`} title={mask.hint}>
              {mask.label}
            </span>
          )}
        </div>
        {rollingOos && (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] font-mono text-slate-500 pt-0.5">
            <History className="w-3.5 h-3.5 text-purple-500" />
            <span>
              上月滚动验证（{rollingOos.month}）：OOS RankIC{' '}
              <b className={rollingOos.rank_ic === null ? 'text-slate-400' : rollingOos.rank_ic >= 0 ? 'text-red-600' : 'text-green-600'}>
                {rollingOos.rank_ic === null ? '—' : rollingOos.rank_ic.toFixed(4)}
              </b>{' '}
              / 胜率 {rollingOos.win === null ? '—' : `${(rollingOos.win * 100).toFixed(0)}%`} / {fmtInt(rollingOos.days)} 日
            </span>
            <span className="text-slate-400" title={rollingOos.note ?? '前窗选、后窗验：以当时快照的冻结 sgn/w 合成，后窗逐日对 fwd5 求 RankIC'}>
              前窗选·后窗验
              {rollingOos.excluded_no_fwd ? `（后窗排除 ${rollingOos.excluded_no_fwd} 日无前向）` : ''}
            </span>
          </div>
        )}
      </SectionCard>

      {/* ── 板块三层共振（左） + 个股名单/板块成分（右，master-detail） ── */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3.5 items-start">
        <SectionCard
          title={`板块共振（三层判定 · q67=${sectorGroup?.q67 ?? '—'} / q33=${sectorGroup?.q33 ?? '—'}）`}
          icon={<Layers className="w-3.5 h-3.5 text-purple-600" />}
          extra={
            <div className="flex items-center gap-1.5 flex-wrap justify-end">
              <PeriodChips
                options={SECTOR_TYPE_OPTIONS}
                value={sectorType}
                onChange={(id) => {
                  setSectorType(id);
                  setSectorPick(null);
                }}
              />
              <div className="flex rounded-full bg-slate-100 p-0.5">
                {(['chart', 'table'] as const).map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => setSectorView(v)}
                    aria-pressed={sectorView === v}
                    className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                      sectorView === v ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500'
                    }`}
                  >
                    {v === 'chart' ? '图表' : '明细'}
                  </button>
                ))}
              </div>
            </div>
          }
        >
          {sectorGroup && sectorGroup.items.length > 0 ? (
            sectorView === 'chart' ? (
              <SectorResonanceScatter
                group={sectorGroup}
                activeName={sectorPick?.type === sectorType ? sectorPick.name : null}
                onPick={pickSector}
              />
            ) : (
              <div className="max-h-[400px] overflow-y-auto custom-scrollbar -mx-1 px-1">
                {sectorGroup.items.map((it, i) => (
                  <SectorRow
                    key={it.name}
                    item={it}
                    rank={i + 1}
                    q67={sectorGroup.q67}
                    q33={sectorGroup.q33}
                    scale={sectorScale}
                    active={sectorPick?.type === sectorType && sectorPick?.name === it.name}
                    onPick={pickSector}
                  />
                ))}
              </div>
            )
          ) : (
            <EmptyHint text="该分类无板块数据" />
          )}
        </SectionCard>

        <SectionCard
          title={sectorPick ? `板块成分 · ${sectorPick.name}` : '个股名单'}
          icon={<ListOrdered className="w-3.5 h-3.5 text-purple-600" />}
          extra={
            sectorPick ? (
              <div className="flex items-center gap-2">
                <span className="text-[10px] font-mono text-slate-400">
                  {drillMembers ? `${fmtInt(drillMembers.length)} 只` : membersLoading ? '…' : ''}
                </span>
                <button
                  onClick={() => setSectorPick(null)}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-slate-100 text-[11px] font-extrabold text-slate-600 hover:bg-slate-200 transition-colors"
                >
                  <ArrowLeft className="w-3 h-3" /> 返回名单
                </button>
              </div>
            ) : (
              <PeriodChips options={STOCK_LIST_OPTIONS} value={stockList} onChange={setStockList} />
            )
          }
        >
          {sectorPick ? (
            membersLoading || (!drillMembers && !membersError) ? (
              <EmptyHint loading />
            ) : membersError ? (
              <EmptyHint text={membersError} />
            ) : drillMembers && drillMembers.length > 0 ? (
              <>
                <p className="text-[10px] text-slate-400 -mt-1">
                  按合成分降序；含 ST 与无模型覆盖标的（象限/模型缺失显示 —）
                </p>
                <div className="max-h-[380px] overflow-y-auto custom-scrollbar">
                  {drillMembers.map((s, i) => (
                    <StockRow key={`${s.symbol}-${i}`} rank={i + 1} item={s} />
                  ))}
                </div>
              </>
            ) : (
              <EmptyHint text="该板块无成分数据" />
            )
          ) : stocks.length > 0 ? (
            <div className="flex flex-col gap-1">
              <p className="text-[10px] text-slate-400 -mt-1">
                按合成分排序；无模型覆盖的票保留在名单中，模型/象限显示「—」（不补 0）。
              </p>
              <div className="max-h-[400px] overflow-y-auto custom-scrollbar">
                {stocks.map((s, i) => (
                  <StockRow key={`${s.symbol}-${i}`} rank={i + 1} item={s} />
                ))}
              </div>
            </div>
          ) : (
            <EmptyHint text="该名单为空" />
          )}
        </SectionCard>
      </div>

      {/* ── 入选因子清单 ─────────────────────────────────────── */}
      <SectionCard
        title={`入选因子（${board.factors.length}）`}
        icon={<Sparkles className="w-3.5 h-3.5 text-purple-600" />}
        extra={
          <div className="flex items-center gap-2 flex-wrap justify-end">
            <span className="text-[10px] text-slate-400">近 60 评估日自算 IC 过闸（|IC60|≥0.02 ∧ 同号 ∧ 胜率≥0.5）× 族封顶（每族≤3，rd≤4）</span>
            <div className="flex rounded-full bg-slate-100 p-0.5">
              {(['chart', 'table'] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setFactorView(v)}
                  aria-pressed={factorView === v}
                  className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold transition-colors ${
                    factorView === v ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500'
                  }`}
                >
                  {v === 'chart' ? '图表' : '明细'}
                </button>
              ))}
            </div>
          </div>
        }
      >
        {factorView === 'chart' ? (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-3 items-start">
              <div className="rounded-xl border border-slate-100 bg-white/60 p-1.5">
                <FactorICBar factors={board.factors} />
              </div>
              <div className="rounded-xl border border-slate-100 bg-white/60 p-1.5">
                <FactorICIRScatter factors={board.factors} />
              </div>
            </div>
            <div className="text-[9px] text-slate-400 px-1">
              图表取 |IC60| 前 18；完整 {board.factors.length} 条与逐列数字切「明细」。
              ICIR=IC60/IC 标准差，t=ICIR×√n；净 IC 只显形、不进闸门。
            </div>
          </div>
        ) : (
          <>
        <div className="overflow-x-auto custom-scrollbar">
          {/* table-fixed + 列宽配额：数值列定宽右对齐、名称列吸收余宽——不再整行拉散 */}
          <table className="w-full table-fixed text-[11px]">
            <colgroup>
              <col className="w-[25%]" />
              <col className="w-[10.5%]" />
              <col className="w-[6.5%]" />
              <col className="w-[6.5%]" />
              {hasNetCols && <col className="w-[9.5%]" />}
              {hasNetCols && <col className="w-[9.5%]" />}
              <col className="w-[5.5%]" />
              <col className="w-[5.5%]" />
              <col className="w-[7.5%]" />
              <col className="w-[14%]" />
            </colgroup>
            <thead>
              <tr className="text-[10px] text-slate-400 border-b border-slate-100">
                <th className="text-left font-extrabold py-1.5 px-1">因子</th>
                <th className="text-left font-extrabold py-1.5 px-1">族</th>
                <th className="text-center font-extrabold py-1.5 px-1">方向</th>
                <th className="text-right font-extrabold py-1.5 px-1">IC60</th>
                {hasNetCols && (
                  <th
                    className="text-right font-extrabold py-1.5 px-1"
                    title="尾窗 20 个有效日的方向对齐 IC 均值（sgn×RankIC，可负）。衰减谓词：均值<0→近期反向；|均值|<0.02→走弱；有效日<15→样本不足；否则同向"
                  >
                    近 20 日
                  </th>
                )}
                {hasNetCols && (
                  <th
                    className="text-right font-extrabold py-1.5 px-1"
                    title="净 IC：扣当日新进多头腿调仓成本后的 60 评估日 RankIC（只显形，不进闸门）。净与毛反号 → 侵蚀"
                  >
                    净 IC
                  </th>
                )}
                <th className="text-right font-extrabold py-1.5 px-1" title="方向胜率（60 评估日 IC 同号占比）">胜率</th>
                <th className="text-right font-extrabold py-1.5 px-1">ICIR</th>
                <th className="text-right font-extrabold py-1.5 px-1" title="合成分权重（|IC60| clip [0.02, 0.15] 归一 × regime 乘子）">权重</th>
                <th className="text-right font-extrabold py-1.5 px-1" title="持有期画像（方向对齐 IC；评估窗 45 日，每 h 需 t+h 前向数据）">持有期 1/2/5/10/20</th>
              </tr>
            </thead>
            <tbody>
              {shownFactors.map((f) => (
                <tr key={`${f.lib}/${f.name}`} className="border-b border-slate-50 last:border-0 hover:bg-slate-50/60">
                  <td className="py-1.5 px-1">
                    <div className="font-bold text-slate-800 truncate" title={f.display}>{f.display}</div>
                    <div className="text-[9px] font-mono text-slate-400 truncate" title={f.name}>{f.name}</div>
                  </td>
                  <td className="py-1.5 px-1 text-[10px] font-mono text-slate-500">
                    <div className="truncate" title={f.group}>{f.group}</div>
                  </td>
                  <td className="py-1.5 px-1 text-center">
                    <span className={`px-1.5 py-0.5 rounded-md text-[10px] font-extrabold ${f.sign > 0 ? 'bg-indigo-50 text-indigo-600' : 'bg-slate-100 text-slate-500'}`}>
                      {f.sign > 0 ? '正向' : '反向'}
                    </span>
                  </td>
                  <td className="py-1.5 px-1 text-right"><NumText value={f.ic60} digits={3} /></td>
                  {hasNetCols && (
                    <td className="py-1.5 px-1 text-right whitespace-nowrap">
                      <span className="inline-flex items-center gap-1 justify-end">
                        <span className={`font-mono ${f.ic20 == null ? 'text-slate-300' : f.ic20 >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                          {f.ic20 == null ? '—' : f.ic20.toFixed(3)}
                        </span>
                        <StateChip meta={decayMeta(f.decay)} />
                      </span>
                    </td>
                  )}
                  {hasNetCols && (
                    <td className="py-1.5 px-1 text-right whitespace-nowrap">
                      <span className="inline-flex items-center gap-1 justify-end">
                        <span className={`font-mono ${f.net_ic60 == null ? 'text-slate-300' : f.net_ic60 >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                          {f.net_ic60 == null ? '—' : f.net_ic60.toFixed(3)}
                        </span>
                        {f.eroded === true && (
                          <StateChip
                            meta={{
                              label: '侵蚀',
                              cls: 'bg-red-50 text-red-700 border-red-200/80',
                              hint: '净 IC 与毛 IC 反号：当日新进多头腿的调仓成本已把净效应翻负（净轨只显形，不进闸门）',
                            }}
                          />
                        )}
                      </span>
                    </td>
                  )}
                  <td className="py-1.5 px-1 text-right font-mono text-slate-600">
                    {f.win60 === null ? '—' : `${(f.win60 * 100).toFixed(0)}%`}
                  </td>
                  <td
                    className="py-1.5 px-1 text-right font-mono text-slate-600"
                    title={`ICIR=IC60/IC标准差；t=${f.t60 === null || f.t60 === undefined ? '—' : f.t60.toFixed(1)}（t=ICIR×√n）`}
                  >
                    {f.icir60 === null ? '—' : f.icir60.toFixed(2)}
                  </td>
                  <td className="py-1.5 px-1 text-right font-mono text-slate-600 whitespace-nowrap">
                    {f.weight === null ? '—' : `${(f.weight * 100).toFixed(1)}%`}
                    {f.regime_mult != null && f.regime_mult !== 1 && (
                      <span className="ml-1">
                        <StateChip
                          meta={{ ...regimeNoteMeta(f.regime_note), label: `×${f.regime_mult}` }}
                        />
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 px-1 text-right font-mono whitespace-nowrap">
                    {HORIZON_KEYS.map((h) => {
                      const v = f.horizon?.[h];
                      return (
                        <span key={h} className={`inline-block w-10 ${v == null ? 'text-slate-300' : v >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                          {v == null ? '—' : v.toFixed(2)}
                        </span>
                      );
                    })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {factorsFoldable && (
          <button
            onClick={() => setFactorsExpanded((v) => !v)}
            aria-expanded={factorsExpanded}
            className="self-center inline-flex items-center gap-1 px-3 py-1 rounded-full text-[11px] font-extrabold bg-slate-100 text-slate-600 hover:bg-purple-50 hover:text-purple-700 transition-colors cursor-pointer"
          >
            {factorsExpanded ? (
              <>
                <ChevronUp className="w-3 h-3" /> 收起
              </>
            ) : (
              <>
                <ChevronDown className="w-3 h-3" /> 展开全部 {board.factors.length} 条
              </>
            )}
          </button>
        )}
          </>
        )}
      </SectionCard>

      {/* ── 持有期画像（左） + 个股四象限（右）· 页面末行 ────── */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3.5 items-start">
        <SectionCard title="持有期画像（方向对齐 IC 均值）" icon={<Gauge className="w-3.5 h-3.5 text-purple-600" />}>
          <div className="flex flex-col gap-3">
            {/* 全局条形图：卡内定宽居中 */}
            <div className="flex items-end gap-2 h-24 w-full max-w-[420px] mx-auto px-1">
              {HORIZON_KEYS.map((h) => {
                const v = board.horizon.global?.[h];
                const abs = v === null || v === undefined ? 0 : Math.min(Math.abs(v) * 4, 1);
                return (
                  <div key={h} className="flex flex-col items-center gap-1 flex-1 h-full justify-end" title={`h=${h} 日 方向对齐 IC=${v == null ? '—' : v.toFixed(4)}（sgn×RankIC 均值；负=跨日翻转占优）`}>
                    <span className={`text-[10px] font-mono font-bold ${v == null ? 'text-slate-300' : v >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                      {v == null ? '—' : v.toFixed(3)}
                    </span>
                    <div
                      className={`w-full max-w-[46px] rounded-t-md ${v == null ? 'bg-slate-100' : v >= 0 ? 'bg-red-400/80' : 'bg-green-500/80'}`}
                      style={{ height: `${Math.max(abs * 64, 4)}px` }}
                    />
                    <span className="text-[10px] font-extrabold text-slate-400">{h}日</span>
                  </div>
                );
              })}
            </div>
            {/* 分组明细：卡内自适应多列 */}
            <div className="flex flex-wrap gap-x-6 gap-y-0.5 border-t border-slate-50 pt-2">
              {Object.entries(board.horizon.by_group ?? {}).map(([grp, vals]) => (
                <div key={grp} className="flex items-center justify-between gap-2 text-[10px] font-mono py-0.5 flex-1 min-w-[250px]">
                  <span className="text-slate-500 truncate" title={grp}>{grp}</span>
                  <span className="whitespace-nowrap">
                    {HORIZON_KEYS.map((h) => {
                      const v = vals?.[h];
                      return (
                        <span key={h} className={`inline-block w-10 text-right ${v == null ? 'text-slate-300' : v >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                          {v == null ? '—' : v.toFixed(2)}
                        </span>
                      );
                    })}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </SectionCard>

        <SectionCard
          title="个股四象限（因子分位 × 模型分位）"
          icon={<Grid3x3 className="w-3.5 h-3.5 text-purple-600" />}
          extra={<span className="text-[10px] font-mono text-slate-400">n={fmtInt(resonance.n)}</span>}
        >
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-[auto_repeat(3,minmax(84px,1fr))] gap-1.5">
              <span />
              {QUAD_LEVELS.map((m) => (
                <span key={m.id} className="text-center text-[10px] font-extrabold text-slate-400">
                  模型·{m.label}（{m.id}）
                </span>
              ))}
              {QUAD_LEVELS.map((f) => (
                <React.Fragment key={f.id}>
                  <span className="text-[10px] font-extrabold text-slate-400 flex items-center">
                    因子·{f.label}（{f.id}）
                  </span>
                  {QUAD_LEVELS.map((m) => {
                    const key = `${f.id}${m.id}`;
                    const count = resonance.quadrants[key] ?? 0;
                    const isResoLong = key === 'HH';
                    const isResoShort = key === 'LL';
                    const isDiverge = (f.id === 'H' && m.id !== 'H') || (f.id === 'L' && m.id !== 'L');
                    return (
                      <div
                        key={key}
                        title={`因子分位${f.label} × 模型分位${m.label}：${fmtInt(count)} 只${isResoLong ? '（共振·靠前名单）' : isResoShort ? '（共振·靠后名单）' : isDiverge ? '（背离区）' : ''}`}
                        className={`rounded-lg border px-2 py-1.5 text-center ${
                          isResoLong
                            ? 'bg-red-50/80 border-red-200/70'
                            : isResoShort
                              ? 'bg-green-50/80 border-green-200/70'
                              : isDiverge
                                ? 'bg-amber-50/50 border-amber-100'
                                : 'bg-slate-50/70 border-slate-200/60'
                        }`}
                      >
                        <div className={`text-sm font-extrabold font-mono ${isResoLong ? 'text-red-700' : isResoShort ? 'text-green-700' : 'text-slate-700'}`}>
                          {fmtInt(count)}
                        </div>
                        <div className="text-[9px] font-mono text-slate-400">{key}</div>
                      </div>
                    );
                  })}
                </React.Fragment>
              ))}
            </div>
            <p className="text-[10px] text-slate-400 leading-relaxed border-t border-slate-50 pt-2">
              因子分位=合成分的截面分位（≥70% 高 / ≤30% 低）；模型分位=同批推理 rank_pct（≥0.7 高 / ≤0.3 低）。
              HH 入「共振名单·靠前」、LL 入「共振名单·靠后」（剔 ST）。ρ 为两轴秩相关。
            </p>
          </div>
        </SectionCard>
      </div>
    </div>
  );
};
