/**
 * 推理中性化面板（推理中心「模型治理」与模型管理共用）。
 *
 * 设计目标：把「中性化」当成一层可治理的推理基础设施来展示——
 * 先看状态与风险（覆盖率/风格解释度/暴露），再看配置，最后看逐日明细。
 * 口径见 `docs/推理中性化处理前端_生产级设计_20261030.md`。
 */

import { useEffect, useMemo, useState } from 'react';
import {
  Button,
  DatePicker,
  Drawer,
  InputNumber,
  message,
  Modal,
  Select,
  Table,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  Gauge,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  XCircle,
} from 'lucide-react';
import { clsx } from 'clsx';
import type { UserModelRecord } from '../../../services/modelTrainingService';
import { stockListService } from '../../../services/stockListService';
import { normalizeStockCode } from '../../../utils/portfolioUtils';
import { modelDisplayName } from '../../../pages/modelRegistryUtils';
import {
  neutralizationService,
  type NeutralizationDiagnostic,
  type NeutralizationMode,
  type NeutralizationOverviewRow,
  type NeutralizationScoreRow,
  type NeutralizationSettings,
} from '../../../services/neutralizationService';

/* ────────────────────────── 设计原语 ────────────────────────── */

const MODE_META: Record<
  NeutralizationMode,
  { label: string; hint: string; dot: string; pill: string }
> = {
  off: {
    label: '关闭',
    hint: '不计算中性化',
    dot: 'bg-slate-300',
    pill: 'bg-slate-100 text-slate-500 border-slate-200',
  },
  shadow: {
    label: '影子',
    hint: '只写影子表，不改排序',
    dot: 'bg-indigo-500',
    pill: 'bg-indigo-50 text-indigo-700 border-indigo-200',
  },
  enforce: {
    label: '启用',
    hint: '消费方可读取中性化分位',
    dot: 'bg-emerald-500',
    pill: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  },
};

const CONTROL_LABELS: Record<string, string> = {
  log_mv: '流通市值',
  log_total_mv: '总市值',
  beta20: '20日 Beta',
  beta60: '60日 Beta',
  mom20: '20日动量',
  mom60: '60日动量',
  vol20: '20日波动率',
  idio_vol20: '特质波动率',
  turn20: '20日换手',
  pb: 'PB',
  ep: 'EP',
};

const CONTROL_GROUPS: { label: string; keys: string[] }[] = [
  { label: '规模', keys: ['log_mv', 'log_total_mv'] },
  { label: '风险', keys: ['beta20', 'beta60', 'vol20', 'idio_vol20'] },
  { label: '动量', keys: ['mom20', 'mom60'] },
  { label: '流动性', keys: ['turn20'] },
  { label: '估值', keys: ['pb', 'ep'] },
];

const STATUS_META: Record<
  string,
  { label: string; icon: typeof CheckCircle2; pill: string; bar: string }
> = {
  ok: {
    label: '正常',
    icon: CheckCircle2,
    pill: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    bar: 'bg-emerald-500',
  },
  degraded: {
    label: '降级',
    icon: AlertTriangle,
    pill: 'bg-amber-50 text-amber-700 border-amber-200',
    bar: 'bg-amber-500',
  },
  failed: {
    label: '失败',
    icon: XCircle,
    pill: 'bg-rose-50 text-rose-700 border-rose-200',
    bar: 'bg-rose-500',
  },
};

function fmtPct(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

function fmtNum(value: number | null | undefined, digits = 3): string {
  if (value == null || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

function maxExposure(exposure: Record<string, number | null> | null | undefined): number | null {
  if (!exposure) return null;
  const values = Object.values(exposure).filter(
    (v): v is number => v != null && Number.isFinite(v),
  );
  if (!values.length) return null;
  return Math.max(...values.map((v) => Math.abs(v)));
}

/* ────────────────────────── 小组件 ────────────────────────── */

interface StatTileProps {
  label: string;
  value: string;
  hint?: string;
  tone?: 'slate' | 'indigo' | 'emerald' | 'amber' | 'rose';
  icon?: typeof Gauge;
}

function StatTile({ label, value, hint, tone = 'slate', icon: Icon }: StatTileProps) {
  const toneMap = {
    slate: 'text-slate-800 bg-slate-50 border-slate-200',
    indigo: 'text-indigo-700 bg-indigo-50 border-indigo-200',
    emerald: 'text-emerald-700 bg-emerald-50 border-emerald-200',
    amber: 'text-amber-700 bg-amber-50 border-amber-200',
    rose: 'text-rose-700 bg-rose-50 border-rose-200',
  } as const;
  return (
    <div className="rounded-2xl border border-slate-200 bg-white px-3.5 py-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-black uppercase tracking-[0.16em] text-slate-400">
          {label}
        </span>
        {Icon && (
          <span className={clsx('rounded-lg border p-1', toneMap[tone])}>
            <Icon size={12} />
          </span>
        )}
      </div>
      <div className="mt-1.5 font-mono text-lg font-black leading-none text-slate-800">
        {value}
      </div>
      {hint && <div className="mt-1 text-[10px] text-slate-400">{hint}</div>}
    </div>
  );
}

interface SparklineProps {
  values: number[];
  color: string;
  height?: number;
}

function Sparkline({ values, color, height = 34 }: SparklineProps) {
  if (values.length < 2) {
    return <div className="h-[34px] rounded-md bg-slate-50" />;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * 100;
      const y = height - ((v - min) / range) * (height - 4) - 2;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');
  return (
    <svg
      viewBox={`0 0 100 ${height}`}
      preserveAspectRatio="none"
      className="h-[34px] w-full"
    >
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth="1.6"
        vectorEffect="non-scaling-stroke"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

interface StatusPillProps {
  status: string | null;
}

function StatusPill({ status }: StatusPillProps) {
  if (!status) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-bold text-slate-400">
        未计算
      </span>
    );
  }
  const meta = STATUS_META[status] ?? {
    label: status,
    icon: AlertTriangle,
    pill: 'bg-slate-50 text-slate-500 border-slate-200',
    bar: 'bg-slate-400',
  };
  const Icon = meta.icon;
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-black',
        meta.pill,
      )}
    >
      <Icon size={11} />
      {meta.label}
    </span>
  );
}

function CoverageBar({ value }: { value: number | null }) {
  const pct = Math.max(0, Math.min(1, value ?? 0));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-14 overflow-hidden rounded-full bg-slate-100">
        <div
          className={clsx(
            'h-full rounded-full',
            pct >= 0.95 ? 'bg-emerald-500' : pct >= 0.9 ? 'bg-amber-500' : 'bg-rose-500',
          )}
          style={{ width: `${pct * 100}%` }}
        />
      </div>
      <span className="font-mono text-[11px] text-slate-500">{fmtPct(value)}</span>
    </div>
  );
}

/* ────────────────────────── 单模型面板 ────────────────────────── */

interface NeutralizationModelPanelProps {
  modelId: string;
  modelName?: string;
}

export function NeutralizationModelPanel({ modelId, modelName }: NeutralizationModelPanelProps) {
  const [settings, setSettings] = useState<NeutralizationSettings | null>(null);
  const [diagnostics, setDiagnostics] = useState<NeutralizationDiagnostic[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [mode, setMode] = useState<NeutralizationMode>('off');
  const [controls, setControls] = useState<string[]>(['log_mv', 'beta60', 'mom20']);
  const [minN, setMinN] = useState(300);
  const [winsor, setWinsor] = useState(0.01);
  const [lag, setLag] = useState(0);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [recomputeDate, setRecomputeDate] = useState<Dayjs>(dayjs());
  const [scores, setScores] = useState<NeutralizationScoreRow[]>([]);
  const [scoresOpen, setScoresOpen] = useState(false);
  const [scoresDate, setScoresDate] = useState<string>('');
  const [scoresLoading, setScoresLoading] = useState(false);
  const [nameMap, setNameMap] = useState<Record<string, string>>({});

  // 后端 list_scores 尚未重启时也能显示名称：用本地股票列表兜底
  useEffect(() => {
    let cancelled = false;
    void stockListService
      .load()
      .then(() => {
        if (cancelled) return;
        const map: Record<string, string> = {};
        for (const stock of stockListService.getAll()) {
          map[normalizeStockCode(stock.symbol)] = stock.name ?? '';
        }
        setNameMap(map);
      })
      .catch(() => {
        if (!cancelled) setNameMap({});
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const displayName = (symbol: string, name?: string | null): string =>
    name || nameMap[normalizeStockCode(symbol)] || '—';

  const applySettings = (next: NeutralizationSettings) => {
    setSettings(next);
    setMode(next.mode);
    setControls(next.controls ?? ['log_mv', 'beta60', 'mom20']);
    setMinN(next.min_n ?? 300);
    setWinsor(next.winsor_pct ?? 0.01);
    setLag(next.control_lag ?? 0);
  };

  const load = async () => {
    setLoading(true);
    try {
      const bundle = await neutralizationService.getSettings(modelId);
      applySettings(bundle.settings);
      setDiagnostics(bundle.diagnostics ?? []);
    } catch (err) {
      message.error(`加载中性化配置失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelId]);

  const latest = diagnostics[0] ?? null;
  const trend = useMemo(() => [...diagnostics].reverse().slice(-20), [diagnostics]);
  const latestExposure = maxExposure(latest?.exposure);

  const save = async () => {
    if (!controls.length) {
      message.warning('至少保留一个控制变量');
      return;
    }
    setSaving(true);
    try {
      const next = await neutralizationService.putSettings(modelId, {
        mode,
        controls,
        min_n: minN,
        winsor_pct: winsor,
        control_lag: lag,
      });
      applySettings(next);
      message.success(mode === 'enforce' ? '已保存为启用模式' : '中性化配置已保存');
      await load();
    } catch (err) {
      message.error(`保存失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const recompute = async () => {
    setRecomputing(true);
    try {
      await neutralizationService.recompute(modelId, recomputeDate.format('YYYY-MM-DD'));
      message.success('已重算该日中性化影子结果');
      await load();
    } catch (err) {
      message.error(`重算失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setRecomputing(false);
    }
  };

  const loadAllScores = async (dateText: string) => {
    const pageSize = 1000;
    const all: NeutralizationScoreRow[] = [];
    let offset = 0;
    let total = 1;
    while (offset < total && all.length < 10000) {
      const page = await neutralizationService.getScores(modelId, dateText, pageSize, offset);
      total = page.total;
      all.push(...page.rows);
      if (!page.rows.length) break;
      offset += page.rows.length;
    }
    return all;
  };

  const openScores = async (dateText: string) => {
    setScoresDate(dateText);
    setScoresOpen(true);
    setScoresLoading(true);
    try {
      setScores(await loadAllScores(dateText));
    } catch (err) {
      message.error(`加载明细失败: ${err instanceof Error ? err.message : String(err)}`);
      setScores([]);
    } finally {
      setScoresLoading(false);
    }
  };

  const moverRows = useMemo(() => {
    const valid = scores.filter(
      (row): row is NeutralizationScoreRow & { neutral_score: number } =>
        row.neutral_score != null,
    );
    if (!valid.length) return [];
    const rawSorted = [...scores].sort((a, b) => b.raw_score - a.raw_score);
    const rawRank = new Map(rawSorted.map((row, idx) => [row.symbol, idx + 1]));
    const neutralSorted = [...valid].sort((a, b) => b.neutral_score - a.neutral_score);
    return neutralSorted.slice(0, 10).map((row, idx) => {
      const before = rawRank.get(row.symbol) ?? 0;
      return {
        symbol: row.symbol,
        name: row.name ?? '',
        raw_rank: before,
        neutral_rank: idx + 1,
        delta: before - (idx + 1),
        neutral_score: row.neutral_score,
      };
    });
  }, [scores]);

  const rankMaps = useMemo(() => {
    const rawSorted = [...scores].sort((a, b) => b.raw_score - a.raw_score);
    const rawRank = new Map(rawSorted.map((row, idx) => [row.symbol, idx + 1]));
    const validNeutral = scores
      .filter((row) => row.neutral_score != null)
      .sort((a, b) => (b.neutral_score ?? 0) - (a.neutral_score ?? 0));
    const neutralRank = new Map(validNeutral.map((row, idx) => [row.symbol, idx + 1]));
    return { rawRank, neutralRank };
  }, [scores]);

  const upCount = useMemo(
    () => moverRows.filter((row) => row.delta > 0).length,
    [moverRows],
  );

  const diagColumns: ColumnsType<NeutralizationDiagnostic> = useMemo(
    () => [
      {
        title: '数据日',
        dataIndex: 'data_trade_date',
        width: 112,
        render: (value: string) => (
          <span className="font-mono text-xs font-bold text-slate-700">{value}</span>
        ),
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 92,
        render: (value: string) => <StatusPill status={value} />,
      },
      {
        title: '口径',
        dataIndex: 'universe_scope',
        width: 96,
        render: (value: string) =>
          value === 'hot_set' ? (
            <Tooltip title="热集口径只用于影子展示，enforce 不使用">
              <span className="rounded border border-violet-200 bg-violet-50 px-1.5 py-0.5 text-[10px] font-bold text-violet-600">
                热集
              </span>
            </Tooltip>
          ) : value === 'custom_universe' ? (
            <Tooltip title="自定义/池宇宙：分位只在该宇宙内可比">
              <span className="rounded border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] font-bold text-sky-600">
                自定义
              </span>
            </Tooltip>
          ) : (
            <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] font-bold text-slate-500">
              全市场
            </span>
          ),
      },
      {
        title: '覆盖',
        dataIndex: 'coverage',
        width: 118,
        render: (value: number) => <CoverageBar value={value} />,
      },
      {
        title: 'R²',
        dataIndex: 'r2_pass1',
        width: 80,
        align: 'right',
        render: (value: number | null) => (
          <span className="font-mono text-xs text-slate-600">{fmtNum(value, 2)}</span>
        ),
      },
      {
        title: '降级原因',
        dataIndex: 'reason',
        ellipsis: true,
        render: (value: string | null) =>
          value ? (
            <span className="text-[11px] text-amber-600">{value}</span>
          ) : (
            <span className="text-slate-300 text-xs">—</span>
          ),
      },
      {
        title: '',
        key: 'detail',
        width: 64,
        render: (_, row) => (
          <Button
            size="small"
            type="text"
            className="!px-1 text-[11px] font-bold text-indigo-600"
            onClick={() => void openScores(row.data_trade_date)}
          >
            明细
          </Button>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [modelId],
  );

  const scoreColumns: ColumnsType<NeutralizationScoreRow> = [
    {
      title: '中性排名',
      key: 'neutral_rank',
      width: 82,
      align: 'right',
      render: (_, row) => {
        const rank = rankMaps.neutralRank.get(row.symbol);
        return rank ? (
          <span className="rounded-md bg-indigo-50 px-1.5 py-0.5 font-mono text-[11px] font-black text-indigo-700">
            #{rank}
          </span>
        ) : (
          <span className="text-[11px] text-slate-300">—</span>
        );
      },
    },
    {
      title: '标的',
      dataIndex: 'symbol',
      width: 100,
      render: (v: string) => (
        <span className="font-mono text-[11px] font-bold text-slate-700">{v}</span>
      ),
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 108,
      render: (v: string | null | undefined, row) => (
        <span className="text-xs font-bold text-slate-800">
          {displayName(row.symbol, v)}
        </span>
      ),
    },
    {
      title: '原始分',
      dataIndex: 'raw_score',
      align: 'right',
      width: 96,
      render: (v: number) => (
        <span className="font-mono text-[11px] text-slate-500">{v.toFixed(5)}</span>
      ),
    },
    {
      title: '中性化分位',
      dataIndex: 'neutral_score',
      align: 'right',
      width: 126,
      render: (v: number | null) =>
        v == null ? (
          <span className="text-slate-300">—</span>
        ) : (
          <div className="flex items-center justify-end gap-2">
            <div className="h-1.5 w-12 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-indigo-500"
                style={{ width: `${v * 100}%` }}
              />
            </div>
            <span className="font-mono text-[11px] font-bold text-indigo-700">
              {(v * 100).toFixed(1)}
            </span>
          </div>
        ),
    },
    {
      title: '原始排名',
      key: 'raw_rank',
      width: 82,
      align: 'right',
      render: (_, row) => {
        const rank = rankMaps.rawRank.get(row.symbol);
        return rank ? (
          <span className="font-mono text-[11px] text-slate-500">#{rank}</span>
        ) : (
          <span className="text-[11px] text-slate-300">—</span>
        );
      },
    },
    {
      title: '名次变化',
      key: 'delta',
      width: 84,
      align: 'right',
      render: (_, row) => {
        const rawRank = rankMaps.rawRank.get(row.symbol);
        const neutralRank = rankMaps.neutralRank.get(row.symbol);
        if (!rawRank || !neutralRank) {
          return <span className="text-[11px] text-slate-300">—</span>;
        }
        const delta = rawRank - neutralRank;
        if (delta > 0) {
          return (
            <span className="font-mono text-[11px] font-black text-emerald-600">
              ↑{delta}
            </span>
          );
        }
        if (delta < 0) {
          return (
            <span className="font-mono text-[11px] font-bold text-slate-400">
              ↓{Math.abs(delta)}
            </span>
          );
        }
        return <span className="text-[11px] text-slate-300">—</span>;
      },
    },
    {
      title: '标记',
      dataIndex: 'flag',
      width: 96,
      render: (v: string | null) =>
        v && v !== 'ok' ? (
          <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-bold text-amber-700">
            {v}
          </span>
        ) : (
          <span className="text-[10px] text-slate-300">正常</span>
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      {/* ── 顶部：身份 + 模式 + 动作 ── */}
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.06)]">
        <div className="h-1 w-full bg-gradient-to-r from-indigo-500 via-violet-500 to-sky-400" />
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="rounded-lg bg-indigo-50 p-1.5 text-indigo-600">
                <ShieldCheck size={16} />
              </span>
              <div className="min-w-0">
                <div className="truncate text-sm font-black text-slate-800">
                  {modelName || modelId}
                </div>
                <div className="text-[10px] font-bold uppercase tracking-[0.14em] text-slate-400">
                  推理中性化 · Neutralization Layer
                </div>
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex rounded-xl border border-slate-200 bg-slate-50 p-0.5">
              {(Object.keys(MODE_META) as NeutralizationMode[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  title={MODE_META[key].hint}
                  onClick={() => setMode(key)}
                  className={clsx(
                    'rounded-lg px-2.5 py-1 text-[11px] font-black transition-colors',
                    mode === key
                      ? key === 'enforce'
                        ? 'bg-emerald-600 text-white shadow-sm'
                        : key === 'shadow'
                          ? 'bg-indigo-600 text-white shadow-sm'
                          : 'bg-slate-700 text-white shadow-sm'
                      : 'text-slate-500 hover:text-slate-700',
                  )}
                >
                  {MODE_META[key].label}
                </button>
              ))}
            </div>
            <Button
              type="primary"
              size="small"
              loading={saving}
              onClick={() => void save()}
              className="!rounded-lg !border-0 !bg-slate-900 !px-3 text-[11px] font-bold"
            >
              保存
            </Button>
            <Button
              size="small"
              icon={<RefreshCw size={12} />}
              loading={loading}
              onClick={() => void load()}
              className="!rounded-lg"
            >
              刷新
            </Button>
          </div>
        </div>
        {settings?.exists && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-slate-100 bg-slate-50/60 px-4 py-1.5 text-[10px] text-slate-400">
            <span>
              口径版本 <span className="font-mono text-slate-500">{settings.config_version}</span>
            </span>
            <span>控制变量 {settings.controls.map((c) => CONTROL_LABELS[c] ?? c).join(' · ')}</span>
            <span>更新人 {settings.updated_by || '—'}</span>
            {settings.updated_at && <span>更新于 {dayjs(settings.updated_at).format('MM-DD HH:mm')}</span>}
          </div>
        )}
      </div>

      {mode === 'enforce' && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] font-medium text-amber-700">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            启用模式会让信号读取层看到中性化分位。仅管理员可保存；热集/自定义宇宙结果不允许用于启用。
          </span>
        </div>
      )}

      {/* ── KPI ── */}
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
        <StatTile
          label="当前模式"
          value={MODE_META[mode]?.label ?? '—'}
          hint={MODE_META[mode]?.hint}
          tone={mode === 'enforce' ? 'emerald' : mode === 'shadow' ? 'indigo' : 'slate'}
          icon={ShieldCheck}
        />
        <StatTile
          label="最近状态"
          value={latest ? (STATUS_META[latest.status]?.label ?? latest.status) : '未计算'}
          hint={latest?.data_trade_date ?? '等待首次推理'}
          tone={
            latest?.status === 'ok'
              ? 'emerald'
              : latest?.status === 'degraded'
                ? 'amber'
                : latest?.status === 'failed'
                  ? 'rose'
                  : 'slate'
          }
          icon={Activity}
        />
        <StatTile
          label="覆盖率"
          value={fmtPct(latest?.coverage ?? null)}
          hint={latest ? `${latest.n_symbols} / ${latest.n_total}` : '—'}
          tone={(latest?.coverage ?? 0) >= 0.95 ? 'emerald' : 'amber'}
          icon={Gauge}
        />
        <StatTile
          label="风格解释度 R²"
          value={fmtNum(latest?.r2_pass1 ?? null, 2)}
          hint="控制变量对原始分的解释比例"
          tone="indigo"
          icon={Sparkles}
        />
        <StatTile
          label="事后暴露"
          value={latestExposure == null ? '—' : latestExposure.toFixed(3)}
          hint="中性化后与控制变量的最大 |ρ|"
          tone={latestExposure != null && latestExposure <= 0.05 ? 'emerald' : 'amber'}
          icon={SlidersHorizontal}
        />
      </div>

      {/* ── 趋势 + 配置 ── */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-5">
        <div className="rounded-2xl border border-slate-200 bg-white p-3.5 lg:col-span-3">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-black uppercase tracking-[0.16em] text-slate-400">
              近 {trend.length || 0} 次计算 · 覆盖率 / R²
            </span>
            <span className="text-[10px] text-slate-400">左：覆盖率 · 右：R²</span>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-4">
            <div>
              <div className="mb-1 flex items-baseline justify-between">
                <span className="text-[10px] font-bold text-slate-500">覆盖率</span>
                <span className="font-mono text-xs font-black text-emerald-600">
                  {fmtPct(trend.length ? trend[trend.length - 1].coverage : null)}
                </span>
              </div>
              <Sparkline values={trend.map((d) => d.coverage)} color="#10b981" />
            </div>
            <div>
              <div className="mb-1 flex items-baseline justify-between">
                <span className="text-[10px] font-bold text-slate-500">R²（style）</span>
                <span className="font-mono text-xs font-black text-indigo-600">
                  {fmtNum(trend.length ? trend[trend.length - 1].r2_pass1 : null, 2)}
                </span>
              </div>
              <Sparkline
                values={trend.map((d) => d.r2_pass1 ?? 0)}
                color="#6366f1"
              />
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-3.5 lg:col-span-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-black uppercase tracking-[0.16em] text-slate-400">
              口径配置
            </span>
            <button
              type="button"
              className="text-[10px] font-bold text-indigo-600 hover:text-indigo-700"
              onClick={() => setShowAdvanced((v) => !v)}
            >
              {showAdvanced ? '收起高级' : '高级设置'}
            </button>
          </div>
          <div className="mt-2 space-y-1.5">
            {CONTROL_GROUPS.map((group) => (
              <div key={group.label} className="flex items-center gap-2">
                <span className="w-9 shrink-0 text-[9px] font-black uppercase tracking-wide text-slate-400">
                  {group.label}
                </span>
                <div className="flex flex-wrap gap-1">
                  {group.keys.map((key) => {
                    const active = controls.includes(key);
                    return (
                      <button
                        key={key}
                        type="button"
                        onClick={() =>
                          setControls((prev) =>
                            prev.includes(key)
                              ? prev.filter((c) => c !== key)
                              : [...prev, key],
                          )
                        }
                        className={clsx(
                          'rounded-md border px-1.5 py-0.5 text-[10px] font-bold transition-colors',
                          active
                            ? 'border-indigo-200 bg-indigo-50 text-indigo-700'
                            : 'border-slate-200 bg-white text-slate-400 hover:border-slate-300',
                        )}
                      >
                        {active ? '✓ ' : ''}
                        {CONTROL_LABELS[key] ?? key}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
          {controls.length > 4 && (
            <div className="mt-1.5 text-[10px] text-amber-600">
              同族变量高度共线，建议每族最多选 1 个；默认三因子已验证
            </div>
          )}
          <div className="mt-3 flex items-center gap-2">
            <DatePicker
              size="small"
              value={recomputeDate}
              onChange={(value) => value && setRecomputeDate(value)}
              allowClear={false}
              className="!rounded-lg"
            />
            <Button
              size="small"
              loading={recomputing}
              onClick={() => void recompute()}
              className="!rounded-lg text-[11px] font-bold"
            >
              重算当日
            </Button>
          </div>
          {showAdvanced && (
            <div className="mt-3 grid grid-cols-3 gap-2 border-t border-slate-100 pt-3">
              <label className="flex flex-col gap-1">
                <span className="text-[10px] font-bold text-slate-400">最小样本</span>
                <InputNumber
                  size="small"
                  min={50}
                  max={5000}
                  value={minN}
                  onChange={(v) => setMinN(Number(v ?? 300))}
                  className="!w-full"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] font-bold text-slate-400">截尾比例</span>
                <InputNumber
                  size="small"
                  min={0}
                  max={0.2}
                  step={0.005}
                  value={winsor}
                  onChange={(v) => setWinsor(Number(v ?? 0.01))}
                  className="!w-full"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] font-bold text-slate-400">滞后天数</span>
                <InputNumber
                  size="small"
                  min={0}
                  max={5}
                  value={lag}
                  onChange={(v) => setLag(Number(v ?? 0))}
                  className="!w-full"
                />
              </label>
            </div>
          )}
        </div>
      </div>

      {/* ── 诊断表 ── */}
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5">
          <span className="text-[10px] font-black uppercase tracking-[0.16em] text-slate-400">
            逐日诊断
          </span>
          <span className="text-[10px] text-slate-400">
            status=ok 才可被消费方使用；degraded 只落库不参与启用
          </span>
        </div>
        <Table<NeutralizationDiagnostic>
          size="small"
          rowKey="neutral_run_id"
          loading={loading}
          columns={diagColumns}
          dataSource={diagnostics}
          pagination={{ pageSize: 8, size: 'small', hideOnSinglePage: true }}
          locale={{ emptyText: '暂无中性化结果（影子模式首次推理后生成，或点「重算当日」）' }}
        />
      </div>

      {/* ── 明细弹窗 ── */}
      <Modal
        title={
          <div className="flex items-center gap-2">
            <span className="rounded-lg bg-indigo-50 p-1.5 text-indigo-600">
              <Sparkles size={14} />
            </span>
            <div>
              <div className="text-sm font-black text-slate-800">中性化明细 · {scoresDate}</div>
              <div className="text-[10px] text-slate-400">
                原始分 → 中性化分位（已剥离所选风格因子），名次按中性化分位重排
              </div>
            </div>
          </div>
        }
        open={scoresOpen}
        onCancel={() => setScoresOpen(false)}
        width={1120}
        footer={null}
        destroyOnClose
        styles={{ body: { maxHeight: '72vh', overflowY: 'auto', paddingTop: 8 } }}
      >
        <div className="mb-3 grid grid-cols-3 gap-2">
          <StatTile label="有效标的" value={String(scores.filter((r) => r.neutral_score != null).length)} hint={`共 ${scores.length} 行`} />
          <StatTile label="前 10 上升" value={String(upCount)} hint="按中性化分位重排后的名次提升" tone="indigo" />
          <StatTile
            label="最大变化"
            value={
              moverRows.length
                ? `${moverRows[0].delta > 0 ? '+' : ''}${moverRows[0].delta}`
                : '—'
            }
            hint={moverRows[0]?.symbol ?? ''}
            tone="slate"
          />
        </div>
        <div className="mb-4 rounded-xl border border-slate-200 bg-slate-50/60 p-2.5">
          <div className="mb-1.5 text-[10px] font-black uppercase tracking-[0.14em] text-slate-400">
            中性化后进入前 10 的标的（相对原始名次）
          </div>
          <div className="flex flex-wrap gap-1.5">
            {moverRows.map((row) => (
              <span
                key={row.symbol}
                className={clsx(
                  'inline-flex items-center gap-1 rounded-lg border px-2 py-1 font-mono text-[10px] font-bold',
                  row.delta > 0
                    ? 'border-indigo-200 bg-white text-indigo-700'
                    : 'border-slate-200 bg-white text-slate-500',
                )}
              >
                {row.symbol}
                <span className="text-slate-500">{displayName(row.symbol, row.name)}</span>
                <span className={row.delta > 0 ? 'text-emerald-600' : 'text-slate-400'}>
                  {row.delta > 0 ? (
                    <ArrowUpRight size={11} className="inline" />
                  ) : (
                    <ArrowDownRight size={11} className="inline" />
                  )}
                  {Math.abs(row.delta)}
                </span>
              </span>
            ))}
            {!moverRows.length && <span className="text-[11px] text-slate-400">暂无有效分位</span>}
          </div>
        </div>
        <Table<NeutralizationScoreRow>
          size="small"
          rowKey="symbol"
          loading={scoresLoading}
          columns={scoreColumns}
          dataSource={scores}
          pagination={{ pageSize: 50, size: 'small' }}
        />
      </Modal>
    </div>
  );
}

/* ────────────────────────── 治理总览 ────────────────────────── */

interface NeutralizationOverviewPanelProps {
  models: UserModelRecord[];
  marketLabel?: string;
}

export function NeutralizationOverviewPanel({
  models,
  marketLabel,
}: NeutralizationOverviewPanelProps) {
  const [rows, setRows] = useState<NeutralizationOverviewRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string>('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [newModelId, setNewModelId] = useState<string>('');

  const load = async () => {
    setLoading(true);
    try {
      setRows(await neutralizationService.getOverview());
    } catch (err) {
      message.error(`加载中性化总览失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const configured = new Set(rows.map((r) => r.model_id));
  const candidates = models.filter((m) => !configured.has(m.model_id));
  const okCount = rows.filter((r) => r.last_status === 'ok').length;
  const degradedCount = rows.filter((r) => r.last_status === 'degraded' || r.last_status === 'failed').length;
  const avgCoverage =
    rows.filter((r) => r.coverage != null).length > 0
      ? rows.reduce((sum, r) => sum + (r.coverage ?? 0), 0) /
        rows.filter((r) => r.coverage != null).length
      : null;

  const columns: ColumnsType<NeutralizationOverviewRow> = [
    {
      title: '模型',
      dataIndex: 'model_id',
      ellipsis: true,
      render: (value: string) => {
        const model = models.find((m) => m.model_id === value);
        return (
          <div className="flex flex-col">
            <span className="truncate text-xs font-black text-slate-700">
              {model ? modelDisplayName(model) : value}
            </span>
            <span className="truncate font-mono text-[10px] text-slate-400">{value}</span>
          </div>
        );
      },
    },
    {
      title: '模式',
      dataIndex: 'mode',
      width: 84,
      render: (value: NeutralizationMode) => (
        <span
          className={clsx(
            'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-black',
            MODE_META[value]?.pill ?? MODE_META.off.pill,
          )}
        >
          <span className={clsx('h-1.5 w-1.5 rounded-full', MODE_META[value]?.dot ?? 'bg-slate-300')} />
          {MODE_META[value]?.label ?? value}
        </span>
      ),
    },
    {
      title: '最近状态',
      dataIndex: 'last_status',
      width: 96,
      render: (value: string | null) => <StatusPill status={value} />,
    },
    {
      title: '数据日',
      dataIndex: 'data_trade_date',
      width: 104,
      render: (value: string | null) => (
        <span className="font-mono text-xs text-slate-600">{value ?? '—'}</span>
      ),
    },
    {
      title: '覆盖',
      dataIndex: 'coverage',
      width: 118,
      render: (value: number | null) => <CoverageBar value={value} />,
    },
    {
      title: 'R²',
      dataIndex: 'r2',
      width: 72,
      align: 'right',
      render: (value: number | null) => (
        <span className="font-mono text-xs text-slate-600">{fmtNum(value, 2)}</span>
      ),
    },
    {
      title: '原因',
      dataIndex: 'reason',
      ellipsis: true,
      render: (value: string | null) =>
        value ? (
          <span className="text-[11px] text-amber-600">{value}</span>
        ) : (
          <span className="text-slate-300 text-xs">—</span>
        ),
    },
    {
      title: '',
      key: 'actions',
      width: 84,
      render: (_, row) => (
        <Button
          size="small"
          type="text"
          className="!px-1 text-[11px] font-bold text-indigo-600"
          onClick={() => {
            setSelected(row.model_id);
            setDrawerOpen(true);
          }}
        >
          配置 / 诊断
        </Button>
      ),
    },
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 p-4">
      {/* 头部 */}
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.06)]">
        <div className="h-1 w-full bg-gradient-to-r from-indigo-500 via-violet-500 to-sky-400" />
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex items-center gap-2.5">
            <span className="rounded-xl bg-indigo-50 p-2 text-indigo-600">
              <ShieldCheck size={18} />
            </span>
            <div>
              <div className="text-sm font-black text-slate-800">
                推理中性化 {marketLabel ? `· ${marketLabel}` : ''}
              </div>
              <div className="text-[10px] font-bold uppercase tracking-[0.14em] text-slate-400">
                影子优先 · 不改原始分数与下单链路
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Select
              size="small"
              showSearch
              placeholder="选择模型开启中性化"
              value={newModelId || undefined}
              onChange={(value) => setNewModelId(value)}
              style={{ width: 260 }}
              optionFilterProp="label"
              options={candidates.map((m) => ({
                value: m.model_id,
                label: `${modelDisplayName(m)} · ${m.model_id}`,
              }))}
            />
            <Button
              size="small"
              disabled={!newModelId}
              onClick={() => {
                setSelected(newModelId);
                setDrawerOpen(true);
              }}
              className="!rounded-lg text-[11px] font-bold"
            >
              配置
            </Button>
            <Button
              size="small"
              icon={<RefreshCw size={12} />}
              loading={loading}
              onClick={() => void load()}
              className="!rounded-lg"
            >
              刷新
            </Button>
          </div>
        </div>
      </div>

      {/* 汇总 */}
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
        <StatTile label="已配置模型" value={String(rows.length)} hint="含关闭/影子/启用" icon={ShieldCheck} />
        <StatTile
          label="最近正常"
          value={String(okCount)}
          hint={`${degradedCount} 个降级/失败`}
          tone={degradedCount === 0 ? 'emerald' : 'amber'}
          icon={CheckCircle2}
        />
        <StatTile
          label="平均覆盖率"
          value={fmtPct(avgCoverage)}
          hint="最近一次计算"
          tone={(avgCoverage ?? 0) >= 0.95 ? 'emerald' : 'amber'}
          icon={Gauge}
        />
        <StatTile
          label="默认模式"
          value="影子"
          hint="新配置默认只写影子表"
          tone="indigo"
          icon={Sparkles}
        />
      </div>

      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <Table<NeutralizationOverviewRow>
          size="small"
          rowKey="model_id"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 10, size: 'small', hideOnSinglePage: true }}
          locale={{ emptyText: '还没有模型开启中性化。先从右上角选择模型并保存为「影子」模式。' }}
        />
      </div>

      <Drawer
        title="中性化配置与诊断"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={920}
        destroyOnClose
      >
        {selected && (
          <NeutralizationModelPanel
            modelId={selected}
            modelName={
              models.find((m) => m.model_id === selected)
                ? modelDisplayName(models.find((m) => m.model_id === selected)!)
                : selected
            }
          />
        )}
      </Drawer>
    </div>
  );
}
