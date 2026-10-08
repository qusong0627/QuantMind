/**
 * 组合实验室（P2）——因子池页第 5 个 tab。
 *
 * 行为契约：
 * - 只列「有面板」的因子：组合优化吃价值级面板（zscore + fret），无面板因子
 *   选了也必然 400 —— UI 从源头不给选，而不是让用户提交后吃错；
 * - 2..12 个因子（与后端 combo_optimizer.MIN/MAX_FACTORS 镜像），不足 2 个
 *   不发请求就地拦截；seed 留空 = 后端默认，非负整数才合法；
 * - 提交是「建行 + 起子进程」即时回包，随后轮询详情：pending/running 每
 *   pollMs 拉一次，done/failed 停轮询并刷新历史列表；
 * - 缺失指标一律「—」（绝不伪造 0）；权重正=正向暴露（绿）、负=反向暴露（红），
 *   Σ|w|=1 由后端保证，此处只展示。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';
import { Button } from './ui/Button';
import { Badge } from './ui/Badge';
import { MISSING_METRIC_TEXT } from '../services-v2/metricRegistry';
import {
  getCombo,
  getPoolFactors,
  listCombos,
  optimizeCombo,
  type ComboConfigEcho,
  type ComboDetail,
  type ComboListItem,
  type ComboWindowMetrics,
  type PoolFactorRow,
} from '../services-v2/api';
import { FlaskConical, RefreshCw, X } from 'lucide-react';

/** 与后端 combo_optimizer.MIN_FACTORS / MAX_FACTORS 镜像；改了后端必须同步。 */
const MIN_FACTORS = 2;
const MAX_FACTORS = 12;
const DEFAULT_POLL_MS = 4000;
const PICK_LIMIT = 500;
const HISTORY_LIMIT = 20;

interface ComboLabTabProps {
  market: string;
  universe: string;
  /** 轮询间隔（测试注入小值；生产用默认 4s） */
  pollMs?: number;
}

function fmtNum(value: number | null | undefined, digits = 4): string {
  return value == null || !Number.isFinite(value) ? MISSING_METRIC_TEXT : value.toFixed(digits);
}

function fmtPct(value: number | null | undefined, digits = 2): string {
  return value == null || !Number.isFinite(value)
    ? MISSING_METRIC_TEXT
    : `${(value * 100).toFixed(digits)}%`;
}

function fmtTime(value: string | null | undefined): string {
  if (!value) return MISSING_METRIC_TEXT;
  const dt = new Date(value);
  return Number.isNaN(dt.getTime()) ? String(value) : dt.toLocaleString('zh-CN');
}

function fmtWeight(w: number): string {
  return `${w >= 0 ? '+' : ''}${w.toFixed(4)}`;
}

type MetricKey =
  | 'meanRankIc'
  | 'rankIcir'
  | 'turnoverDaily'
  | 'annTurnover'
  | 'annReturnNet'
  | 'sharpeNet'
  | 'maxDrawdownNet'
  | 'nDays'
  | 'nObs';

const METRIC_ROWS: { key: MetricKey; label: string; hint?: string }[] = [
  { key: 'meanRankIc', label: '日均 rank-IC' },
  { key: 'rankIcir', label: 'rank-ICIR' },
  { key: 'turnoverDaily', label: '日换手' },
  { key: 'annTurnover', label: '年化换手（倍）', hint: '日均换手 × 252' },
  { key: 'annReturnNet', label: '年化收益（扣成本）', hint: '研究口径费率，非实盘' },
  { key: 'sharpeNet', label: 'Sharpe（扣成本）' },
  { key: 'maxDrawdownNet', label: '最大回撤（扣成本）' },
  { key: 'nDays', label: '有效交易日' },
  { key: 'nObs', label: '样本数' },
];

function metricValue(metrics: ComboWindowMetrics | null, key: MetricKey): string {
  const value = metrics?.[key] ?? null;
  if (key === 'annReturnNet' || key === 'maxDrawdownNet') return fmtPct(value);
  if (key === 'annTurnover') {
    return value == null ? MISSING_METRIC_TEXT : `×${value.toFixed(1)}`;
  }
  if (key === 'nDays' || key === 'nObs') {
    return value == null ? MISSING_METRIC_TEXT : String(Math.round(value));
  }
  return fmtNum(value, key === 'meanRankIc' || key === 'turnoverDaily' ? 4 : 3);
}

const STATUS_BADGE: Record<string, { label: string; variant: 'default' | 'success' | 'warning' | 'destructive' }> = {
  pending: { label: '排队中', variant: 'warning' },
  running: { label: '优化中', variant: 'warning' },
  done: { label: '完成', variant: 'success' },
  failed: { label: '失败', variant: 'destructive' },
};

function StatusBadge({ status, pulse }: { status: string; pulse?: boolean }) {
  const meta = STATUS_BADGE[status] ?? { label: status || '未知', variant: 'default' as const };
  return (
    <Badge variant={meta.variant} className={pulse ? 'animate-pulse' : undefined}>
      {meta.label}
    </Badge>
  );
}

export const ComboLabTab: React.FC<ComboLabTabProps> = ({
  market,
  universe,
  pollMs = DEFAULT_POLL_MS,
}) => {
  const [poolRows, setPoolRows] = useState<PoolFactorRow[] | null>(null);
  const [pickError, setPickError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);

  const [nameText, setNameText] = useState('');
  const [seedText, setSeedText] = useState('');
  const [submitBusy, setSubmitBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [activeId, setActiveId] = useState<string | null>(null);
  /** 同 id 重开时自增，强制轮询 effect 重跑（state 相同值 React 会跳过）。 */
  const [activeSeq, setActiveSeq] = useState(0);
  const [active, setActive] = useState<ComboDetail | null>(null);
  const [activeError, setActiveError] = useState<string | null>(null);

  const [history, setHistory] = useState<ComboListItem[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const nameByFactorId = useMemo(
    () => new Map((poolRows ?? []).map((row) => [row.factorId, row.factorName])),
    [poolRows],
  );

  // ── 候选因子（仅有面板）：市场/股票池切换即重拉，选中清空 ──
  const factorSeq = useRef(0);

  const loadFactors = useCallback(async () => {
    const seq = ++factorSeq.current;
    const res = await getPoolFactors({
      market,
      universe,
      limit: PICK_LIMIT,
      offset: 0,
      sort: 'pool_score',
    });
    // 过期响应丢弃：市场/股票池切走后旧请求才回来，不许覆盖新作用域的候选
    if (seq !== factorSeq.current) return;
    if (res.success && res.data) {
      setPoolRows(res.data.items.filter((row) => row.hasPanel));
      setPickError(null);
    } else {
      setPoolRows([]);
      setPickError(res.error ?? '池因子加载失败');
    }
  }, [market, universe]);

  useEffect(() => {
    loadFactors();
  }, [loadFactors]);

  useEffect(() => {
    setSelected([]);
  }, [market, universe]);

  // ── 历史组合 ──
  const loadHistory = useCallback(async () => {
    const res = await listCombos({ market, limit: HISTORY_LIMIT });
    if (res.success && res.data) {
      setHistory(res.data.items);
      setHistoryError(null);
    } else {
      setHistoryError(res.error ?? '组合列表加载失败');
    }
  }, [market]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // ── 当前组合轮询：pending/running 每 pollMs 拉一次，终态/失败停 ──
  const applyDetail = useCallback(
    (res: { success: boolean; data?: ComboDetail; error?: string }): string | null => {
      if (res.success && res.data) {
        setActive(res.data);
        setActiveError(null);
        return res.data.status;
      }
      setActiveError(res.error ?? '组合详情获取失败');
      return null;
    },
    [],
  );

  useEffect(() => {
    if (!activeId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    const stopPolling = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };

    const fetchOnce = async () => {
      const res = await getCombo(activeId);
      if (cancelled) return;
      const status = applyDetail(res);
      if (status == null) {
        // 拉取失败不再假设「下一轮会自愈」：停表，错误留在页面上，避免无效请求永久打下去
        stopPolling();
        return;
      }
      if (status !== 'pending' && status !== 'running') {
        stopPolling();
        if (status === 'done' || status === 'failed') loadHistory();
      }
    };

    fetchOnce();
    timer = setInterval(fetchOnce, pollMs);
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [activeId, activeSeq, pollMs, loadHistory, applyDetail]);

  const toggle = useCallback((factorId: string) => {
    setSubmitError(null);
    setNotice(null);
    setSelected((prev) => {
      if (prev.includes(factorId)) return prev.filter((fid) => fid !== factorId);
      if (prev.length >= MAX_FACTORS) return prev;
      return [...prev, factorId];
    });
  }, []);

  const handleSubmit = useCallback(async () => {
    setSubmitError(null);
    setNotice(null);
    if (selected.length < MIN_FACTORS) {
      setSubmitError(`至少选 ${MIN_FACTORS} 个因子才能组合（当前 ${selected.length} 个）`);
      return;
    }
    let seed: number | null = null;
    const rawSeed = seedText.trim();
    if (rawSeed) {
      const parsedSeed = Number(rawSeed);
      // 上限与后端 _MAX_SEED（2^32−1，scipy DE 的 seed 范围）镜像：超界就地拦，
      // 不让它变成一次注定 400 的建行
      if (
        !/^\d+$/.test(rawSeed) ||
        !Number.isSafeInteger(parsedSeed) ||
        parsedSeed > 0xffffffff
      ) {
        setSubmitError('随机种子须为非负整数且不超过 4294967295（留空 = 后端默认，落库可复现）');
        return;
      }
      seed = parsedSeed;
    }
    setSubmitBusy(true);
    try {
      const res = await optimizeCombo({
        market,
        universe,
        factorIds: selected,
        name: nameText.trim(),
        seed,
      });
      if (res.success && res.data) {
        setNotice(`已提交（${res.data.comboId.slice(0, 12)}…），优化子进程运行中`);
        setActive(null);
        setActiveError(null);
        setActiveId(res.data.comboId);
      } else {
        setSubmitError(res.error ?? '提交失败');
      }
    } finally {
      setSubmitBusy(false);
    }
  }, [selected, seedText, nameText, market, universe]);

  const openDetail = useCallback(
    (comboId: string) => {
      if (comboId === activeId) {
        // 重开同一行：setActiveId 相同值不触发 effect，若先清空就会永远停在
        // 「加载中…」——bump seq 让 effect 重跑（保留旧数据直到新数据到达）
        setActiveSeq((n) => n + 1);
        return;
      }
      setActive(null);
      setActiveError(null);
      setActiveId(comboId);
    },
    [activeId],
  );

  // ── 展示派生 ──
  const weightRows = useMemo(() => {
    if (!active) return [];
    return Object.entries(active.weights)
      .map(([fid, weight]) => ({
        fid,
        weight,
        name: nameByFactorId.get(fid) ?? `${fid.slice(0, 12)}…`,
      }))
      .sort((a, b) => Math.abs(b.weight) - Math.abs(a.weight));
  }, [active, nameByFactorId]);

  const curveOption = useMemo(() => {
    const curve = active?.validMetrics?.curve ?? null;
    if (!curve || curve.dates.length === 0) return null;
    return {
      grid: { left: 52, right: 16, top: 22, bottom: 34 },
      tooltip: {
        trigger: 'axis',
        confine: true,
        valueFormatter: (value: number) =>
          typeof value === 'number' ? `${value.toFixed(4)}（${fmtPct(value - 1)}）` : '—',
      },
      xAxis: {
        type: 'category',
        data: curve.dates,
        axisLabel: { fontSize: 10, hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { fontSize: 10, formatter: (v: number) => `${((v - 1) * 100).toFixed(0)}%` },
      },
      series: [
        {
          type: 'line',
          data: curve.values,
          showSymbol: false,
          lineStyle: { width: 2, color: '#8b5cf6' },
          itemStyle: { color: '#8b5cf6' },
          areaStyle: { color: 'rgba(139, 92, 246, 0.12)' },
        },
      ],
    };
  }, [active]);

  const configEcho: ComboConfigEcho | null = active?.trainMetrics?.config ?? null;
  const atCap = selected.length >= MAX_FACTORS;

  return (
    <div className="space-y-4">
      {/* 投研纪律条 */}
      <Card className="glass">
        <CardContent className="p-3 text-xs text-muted-foreground leading-relaxed">
          选 {MIN_FACTORS}–{MAX_FACTORS} 个<b>有面板</b>的因子 → 差分进化在 train 窗（按时间 70/30
          拆分的前 70%）最大化组合 rank-IC，权重 L1 归一（Σ|w|=1，允许负权=反向暴露），
          valid 窗独立出样；净值曲线扣研究口径成本。
          <span className="text-foreground">结果仅为研究展示，不自动进生产链路</span>；同 seed 重跑权重可复现。
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* ── 因子选择 ── */}
        <Card className="glass">
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm">
                候选因子（有面板 {poolRows ? `· ${poolRows.length}` : ''}）
              </CardTitle>
              <Button variant="ghost" size="sm" onClick={loadFactors} title="重新拉取池因子">
                <RefreshCw className="h-3.5 w-3.5" />
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {pickError ? (
              <div className="p-4 text-sm text-destructive">{pickError}</div>
            ) : poolRows === null ? (
              <div className="p-4 text-sm text-muted-foreground">加载中…</div>
            ) : poolRows.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted-foreground">
                池里还没有带面板的因子。新回测的因子会自动带面板登记进池；历史因子用页头「执行刷新」补面板。
              </div>
            ) : (
              <div data-testid="combo-picker" className="max-h-[360px] overflow-y-auto space-y-1 pr-1">
                {poolRows.map((row) => {
                  const picked = selected.includes(row.factorId);
                  const order = selected.indexOf(row.factorId);
                  return (
                    <button
                      key={row.factorId}
                      type="button"
                      onClick={() => toggle(row.factorId)}
                      className={`w-full flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-left text-xs transition-colors cursor-pointer ${
                        picked
                          ? 'border-violet-500/60 bg-violet-500/10'
                          : 'border-border/50 hover:bg-muted/40'
                      }`}
                    >
                      <span
                        className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-[4px] border text-[10px] font-bold ${
                          picked
                            ? 'border-violet-500 bg-violet-600 text-white'
                            : 'border-border text-transparent'
                        }`}
                      >
                        {picked ? order + 1 : '·'}
                      </span>
                      <span className="truncate font-medium max-w-[40%]" title={row.factorName}>
                        {row.factorName}
                      </span>
                      <span className="ml-auto font-mono text-[10px] text-muted-foreground shrink-0">
                        IC {fmtNum(row.ic)} · 池分 {fmtNum(row.poolScore)} · 新颖 {fmtNum(row.novelty, 2)}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        {/* ── 组合配置 + 提交 ── */}
        <Card className="glass">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">
              组合配置（{selected.length}/{MAX_FACTORS}）
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {selected.length === 0 ? (
              <p className="text-sm text-muted-foreground">从左侧勾选因子（顺序即权重表顺序的底稿）。</p>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {selected.map((fid, index) => (
                  <span
                    key={fid}
                    className="inline-flex items-center gap-1 rounded-md border border-violet-500/40 bg-violet-500/10 px-2 py-0.5 text-xs"
                  >
                    <span className="font-mono text-[10px] text-violet-700">{index + 1}</span>
                    <span className="max-w-[140px] truncate" title={nameByFactorId.get(fid) ?? fid}>
                      {nameByFactorId.get(fid) ?? `${fid.slice(0, 12)}…`}
                    </span>
                    <button
                      type="button"
                      aria-label={`移除 ${nameByFactorId.get(fid) ?? fid}`}
                      onClick={() => toggle(fid)}
                      className="text-muted-foreground hover:text-foreground cursor-pointer"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}

            <div className="grid grid-cols-2 gap-2">
              <label className="space-y-1 block">
                <span className="text-[11px] text-muted-foreground">组合名称（可选）</span>
                <input
                  value={nameText}
                  onChange={(e) => setNameText(e.target.value)}
                  placeholder="如：动量×反转 试验"
                  maxLength={60}
                  className="h-8 w-full rounded-md border border-input bg-background px-2 text-xs"
                />
              </label>
              <label className="space-y-1 block">
                <span className="text-[11px] text-muted-foreground">随机种子（留空=默认）</span>
                <input
                  value={seedText}
                  onChange={(e) => setSeedText(e.target.value)}
                  placeholder="如 42"
                  inputMode="numeric"
                  className="h-8 w-full rounded-md border border-input bg-background px-2 text-xs font-mono"
                />
              </label>
            </div>

            <Button
              variant="primary"
              size="sm"
              disabled={submitBusy || selected.length < MIN_FACTORS}
              onClick={handleSubmit}
            >
              <FlaskConical className="h-3.5 w-3.5 mr-1" />
              {submitBusy ? '提交中…' : '开始优化'}
            </Button>

            {atCap && (
              <p className="text-[11px] text-amber-600">已达上限 {MAX_FACTORS} 个（后端硬上限）。</p>
            )}
            {submitError && <p className="text-xs text-destructive">{submitError}</p>}
            {notice && <p className="text-xs text-primary">{notice}</p>}
          </CardContent>
        </Card>
      </div>

      {/* ── 当前组合详情 ── */}
      {activeId && (
        <Card className="glass">
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="text-sm">组合详情</CardTitle>
              {active && <StatusBadge status={active.status} pulse={active.status === 'pending' || active.status === 'running'} />}
              {(active?.status === 'pending' || active?.status === 'running' || active === null) &&
                !activeError && (
                  <span className="text-[11px] text-muted-foreground">
                    差分进化可能耗时 1–3 分钟，页面每 {Math.round(pollMs / 1000)} 秒自动刷新
                  </span>
                )}
              <span className="ml-auto font-mono text-[10px] text-muted-foreground">{activeId}</span>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {activeError && <div className="text-sm text-destructive">{activeError}</div>}
            {!active && !activeError && (
              <div className="text-sm text-muted-foreground">加载中…</div>
            )}
            {active && (
              <>
                {active.name && <div className="text-sm font-medium">{active.name}</div>}
                {active.trainWindow && (
                  <div className="font-mono text-[11px] text-muted-foreground">
                    {active.trainWindow}
                  </div>
                )}
                {active.status === 'failed' && (
                  <div className="rounded-lg bg-red-500/10 border border-red-500/30 p-3 text-xs text-red-600">
                    {active.error ?? '作业失败（详见服务日志）'}
                  </div>
                )}
                {active.status === 'done' && (
                  <>
                    {/* 权重表 */}
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="text-xs font-bold text-foreground">权重（Σ|w|=1）</span>
                        <span className="text-[10px] text-muted-foreground">
                          正权=正向暴露（绿） · 负权=反向暴露（红） · 按 |权重| 排序
                        </span>
                      </div>
                      <div className="space-y-1">
                        {weightRows.map(({ fid, weight, name }) => (
                          <div key={fid} className="flex items-center gap-2 text-xs">
                            <span className="w-[36%] truncate" title={`${name}（${fid}）`}>
                              {name}
                            </span>
                            <span
                              className={`w-16 text-right font-mono font-bold ${
                                weight >= 0 ? 'text-emerald-600' : 'text-red-600'
                              }`}
                            >
                              {fmtWeight(weight)}
                            </span>
                            <span className="flex-1 h-2 rounded-full bg-secondary/60 overflow-hidden">
                              <span
                                className={`block h-full rounded-full ${
                                  weight >= 0 ? 'bg-emerald-500/80' : 'bg-red-500/80'
                                }`}
                                style={{ width: `${Math.min(100, Math.abs(weight) * 100).toFixed(1)}%` }}
                              />
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* train / valid 指标对比 */}
                    <div>
                      <div className="text-xs font-bold text-foreground mb-1.5">
                        指标对比（train 拟合窗 / valid 样本外）
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="border-b border-border/50 text-muted-foreground">
                              <th className="py-1.5 px-2 text-left font-medium">指标</th>
                              <th className="py-1.5 px-2 text-right font-medium">train</th>
                              <th className="py-1.5 px-2 text-right font-medium">valid</th>
                            </tr>
                          </thead>
                          <tbody>
                            {METRIC_ROWS.map((row) => (
                              <tr key={row.key} className="border-b border-border/30 last:border-0">
                                <td className="py-1.5 px-2">
                                  {row.label}
                                  {row.hint && (
                                    <span className="ml-1 text-[10px] text-muted-foreground">{row.hint}</span>
                                  )}
                                </td>
                                <td className="py-1.5 px-2 text-right font-mono">
                                  {metricValue(active.trainMetrics, row.key)}
                                </td>
                                <td className="py-1.5 px-2 text-right font-mono font-semibold">
                                  {metricValue(active.validMetrics, row.key)}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>

                    {/* valid 扣成本净值曲线 */}
                    {curveOption ? (
                      <div>
                        <div className="text-xs font-bold text-foreground mb-1">valid 净值（扣成本）</div>
                        <ReactECharts option={curveOption} style={{ height: 220 }} notMerge />
                      </div>
                    ) : (
                      <div className="text-xs text-muted-foreground">净值曲线缺失（valid 窗无有效交易日）</div>
                    )}

                    {/* 参数回执 */}
                    {configEcho && (
                      <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-muted-foreground">
                        <span>seed={configEcho.seed ?? MISSING_METRIC_TEXT}</span>
                        <span>
                          收敛={configEcho.converged == null ? MISSING_METRIC_TEXT : configEcho.converged ? '是' : '否'}
                        </span>
                        <span>评估次数={configEcho.nEvaluations ?? MISSING_METRIC_TEXT}</span>
                        <span>train 比例={configEcho.trainRatio == null ? MISSING_METRIC_TEXT : configEcho.trainRatio.toFixed(2)}</span>
                        <span>成本率={configEcho.costRate == null ? MISSING_METRIC_TEXT : fmtPct(configEcho.costRate, 3)}</span>
                        <span>时间预算={configEcho.timeBudgetS == null ? MISSING_METRIC_TEXT : `${configEcho.timeBudgetS.toFixed(0)}s`}</span>
                      </div>
                    )}
                  </>
                )}
              </>
            )}
          </CardContent>
        </Card>
      )}

      {/* ── 历史组合 ── */}
      <Card className="glass">
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm">历史组合（当前市场）</CardTitle>
            <Button variant="ghost" size="sm" onClick={loadHistory} title="刷新历史组合">
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {historyError ? (
            <div className="p-4 text-sm text-destructive">{historyError}</div>
          ) : history.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">还没有组合记录</div>
          ) : (
            <div className="space-y-1.5">
              {history.map((item) => (
                <button
                  key={item.comboId}
                  type="button"
                  onClick={() => openDetail(item.comboId)}
                  className={`w-full flex items-center gap-2 rounded-lg border px-3 py-2 text-left text-xs transition-colors cursor-pointer ${
                    activeId === item.comboId
                      ? 'border-violet-500/60 bg-violet-500/10'
                      : 'border-border/50 hover:bg-muted/40'
                  }`}
                >
                  <span className="truncate font-medium max-w-[30%]">
                    {item.name || `${item.nFactors} 因子组合`}
                  </span>
                  <StatusBadge status={item.status} />
                  <span className="font-mono text-[10px] text-muted-foreground">
                    valid IC {fmtNum(item.validMeanRankIc)}
                  </span>
                  <span className="ml-auto text-[10px] text-muted-foreground">{fmtTime(item.createdAt)}</span>
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
};

export default ComboLabTab;
