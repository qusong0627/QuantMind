/**
 * 推理中性化面板（推理中心「模型治理」与模型管理共用）。
 *
 * 设计口径见 `docs/推理中性化处理前端_生产级设计_20261030.md`：
 * - 影子优先：默认 shadow，只写影子表，不改原始分数与下单链路；
 * - 降级不静默：status=degraded/failed 必须显式标出原因；
 * - enforce 需要管理员权限（后端已强制，前端提前提示）。
 */

import { useEffect, useMemo, useState } from 'react';
import {
  Button,
  DatePicker,
  Drawer,
  InputNumber,
  message,
  Select,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import { AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react';
import type { UserModelRecord } from '../../../services/modelTrainingService';
import { modelDisplayName } from '../../../pages/modelRegistryUtils';
import {
  neutralizationService,
  type NeutralizationDiagnostic,
  type NeutralizationMode,
  type NeutralizationOverviewRow,
  type NeutralizationScoreRow,
  type NeutralizationSettings,
} from '../../../services/neutralizationService';

const MODE_META: Record<NeutralizationMode, { label: string; color: string }> = {
  off: { label: '关闭', color: 'default' },
  shadow: { label: '影子', color: 'blue' },
  enforce: { label: '启用', color: 'green' },
};

const CONTROL_LABELS: Record<string, string> = {
  log_mv: '流通市值',
  beta60: '60日 Beta',
  mom20: '20日动量',
};

const STATUS_META: Record<string, { label: string; color: string }> = {
  ok: { label: '正常', color: 'green' },
  degraded: { label: '降级', color: 'orange' },
  failed: { label: '失败', color: 'red' },
};

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

function fmtNum(value: number | null | undefined, digits = 4): string {
  if (value == null || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

interface ModeTagProps {
  mode: NeutralizationMode;
}

function ModeTag({ mode }: ModeTagProps) {
  const meta = MODE_META[mode] ?? MODE_META.off;
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

interface StatusTagProps {
  status: string | null;
}

function StatusTag({ status }: StatusTagProps) {
  if (!status) return <span className="text-slate-300 text-xs">未计算</span>;
  const meta = STATUS_META[status] ?? { label: status, color: 'default' };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

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
  const [recomputeDate, setRecomputeDate] = useState<Dayjs>(dayjs());
  const [scores, setScores] = useState<NeutralizationScoreRow[]>([]);
  const [scoresOpen, setScoresOpen] = useState(false);
  const [scoresDate, setScoresDate] = useState<string>('');
  const [scoresLoading, setScoresLoading] = useState(false);

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

  const save = async () => {
    if (!controls.length) {
      message.warning('至少保留一个控制变量');
      return;
    }
    if (mode === 'enforce') {
      message.warning('启用（enforce）需要管理员权限，保存后若权限不足会被拒绝');
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
      message.success('中性化配置已保存');
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

  const openScores = async (dateText: string) => {
    setScoresDate(dateText);
    setScoresOpen(true);
    setScoresLoading(true);
    try {
      const data = await neutralizationService.getScores(modelId, dateText, 200, 0);
      setScores(data.rows);
    } catch (err) {
      message.error(`加载明细失败: ${err instanceof Error ? err.message : String(err)}`);
      setScores([]);
    } finally {
      setScoresLoading(false);
    }
  };

  const diagColumns: ColumnsType<NeutralizationDiagnostic> = useMemo(
    () => [
      {
        title: '数据日',
        dataIndex: 'data_trade_date',
        width: 116,
        render: (value: string) => <span className="font-mono text-xs">{value}</span>,
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 88,
        render: (value: string) => <StatusTag status={value} />,
      },
      {
        title: '口径',
        dataIndex: 'universe_scope',
        width: 92,
        render: (value: string) =>
          value === 'hot_set' ? (
            <Tooltip title="热集口径只用于影子展示，enforce 不使用">
              <Tag color="purple">热集</Tag>
            </Tooltip>
          ) : (
            <Tag>全市场</Tag>
          ),
      },
      {
        title: '覆盖',
        dataIndex: 'coverage',
        width: 84,
        align: 'right',
        render: (value: number) => <span className="font-mono text-xs">{fmtPct(value)}</span>,
      },
      {
        title: 'R²',
        dataIndex: 'r2_pass1',
        width: 84,
        align: 'right',
        render: (value: number | null) => <span className="font-mono text-xs">{fmtNum(value, 3)}</span>,
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
        title: '明细',
        key: 'detail',
        width: 72,
        render: (_, row) => (
          <Button
            size="small"
            type="link"
            className="px-0 text-xs"
            onClick={() => void openScores(row.data_trade_date)}
          >
            查看
          </Button>
        ),
      },
    ],
    // openScores 每次都重新创建，依赖 modelId 即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [modelId],
  );

  const scoreColumns: ColumnsType<NeutralizationScoreRow> = [
    { title: '标的', dataIndex: 'symbol', width: 110, render: (v: string) => <span className="font-mono text-xs">{v}</span> },
    {
      title: '原始分',
      dataIndex: 'raw_score',
      align: 'right',
      render: (v: number) => <span className="font-mono text-xs">{fmtNum(v, 5)}</span>,
    },
    {
      title: '中性化分位',
      dataIndex: 'neutral_score',
      align: 'right',
      render: (v: number | null) =>
        v == null ? (
          <span className="text-slate-300 text-xs">—</span>
        ) : (
          <span className="font-mono text-xs font-bold text-slate-700">{fmtPct(v, 1)}</span>
        ),
    },
    {
      title: '标记',
      dataIndex: 'flag',
      width: 110,
      render: (v: string | null) =>
        v && v !== 'ok' ? <Tag color="orange">{v}</Tag> : <span className="text-slate-300 text-xs">正常</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <ShieldCheck size={16} className="text-slate-500" />
          <span className="text-xs font-black uppercase tracking-widest text-slate-600">
            中性化 · {modelName || modelId}
          </span>
          {settings && <ModeTag mode={settings.mode} />}
          {settings?.exists && (
            <Tooltip title={`口径版本 ${settings.config_version}`}>
              <span className="text-[11px] text-slate-400 font-mono">{settings.config_version}</span>
            </Tooltip>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button size="small" icon={<RefreshCw size={12} />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        </div>
      </div>

      {mode === 'enforce' && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            启用模式会改变信号读取层看到的分位（影子表 → 消费方）。仅管理员可保存；热集口径结果不允许用于启用。
          </span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-bold text-slate-500">模式</span>
          <Select
            size="small"
            value={mode}
            onChange={(value) => setMode(value as NeutralizationMode)}
            options={[
              { value: 'off', label: '关闭' },
              { value: 'shadow', label: '影子（只读观察）' },
              { value: 'enforce', label: '启用（需管理员）' },
            ]}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-bold text-slate-500">控制变量</span>
          <Select
            size="small"
            mode="multiple"
            value={controls}
            onChange={(value: string[]) => setControls(value)}
            options={Object.entries(CONTROL_LABELS).map(([value, label]) => ({ value, label }))}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-bold text-slate-500">最小样本</span>
          <InputNumber size="small" min={50} max={5000} value={minN} onChange={(v) => setMinN(Number(v ?? 300))} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-bold text-slate-500">截尾比例</span>
          <InputNumber
            size="small"
            min={0}
            max={0.2}
            step={0.005}
            value={winsor}
            onChange={(v) => setWinsor(Number(v ?? 0.01))}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-bold text-slate-500">控制变量滞后</span>
          <InputNumber size="small" min={0} max={5} value={lag} onChange={(v) => setLag(Number(v ?? 0))} />
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="primary" size="small" loading={saving} onClick={() => void save()}>
          保存配置
        </Button>
        <span className="mx-1 h-4 w-px bg-slate-200" />
        <DatePicker
          size="small"
          value={recomputeDate}
          onChange={(value) => value && setRecomputeDate(value)}
          allowClear={false}
        />
        <Button size="small" loading={recomputing} onClick={() => void recompute()}>
          重算当日
        </Button>
        <span className="ml-auto text-[11px] text-slate-400">
          仅写影子表，不改原始分数与下单链路
        </span>
      </div>

      <Table<NeutralizationDiagnostic>
        size="small"
        rowKey="neutral_run_id"
        loading={loading}
        columns={diagColumns}
        dataSource={diagnostics}
        pagination={{ pageSize: 10, size: 'small', hideOnSinglePage: true }}
        locale={{ emptyText: '暂无中性化结果（影子模式首次推理后生成）' }}
      />

      <Drawer
        title={`中性化明细 · ${scoresDate}`}
        open={scoresOpen}
        onClose={() => setScoresOpen(false)}
        width={720}
        destroyOnClose
      >
        <Table<NeutralizationScoreRow>
          size="small"
          rowKey="symbol"
          loading={scoresLoading}
          columns={scoreColumns}
          dataSource={scores}
          pagination={{ pageSize: 50, size: 'small' }}
        />
      </Drawer>
    </div>
  );
}

interface NeutralizationOverviewPanelProps {
  models: UserModelRecord[];
  marketLabel?: string;
}

export function NeutralizationOverviewPanel({ models, marketLabel }: NeutralizationOverviewPanelProps) {
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

  const columns: ColumnsType<NeutralizationOverviewRow> = [
    {
      title: '模型',
      dataIndex: 'model_id',
      ellipsis: true,
      render: (value: string) => {
        const model = models.find((m) => m.model_id === value);
        return (
          <span className="text-xs font-bold text-slate-700">
            {model ? modelDisplayName(model) : value}
          </span>
        );
      },
    },
    {
      title: '模式',
      dataIndex: 'mode',
      width: 84,
      render: (value: NeutralizationMode) => <ModeTag mode={value} />,
    },
    {
      title: '最近状态',
      dataIndex: 'last_status',
      width: 96,
      render: (value: string | null) => <StatusTag status={value} />,
    },
    {
      title: '数据日',
      dataIndex: 'data_trade_date',
      width: 106,
      render: (value: string | null) => <span className="font-mono text-xs">{value ?? '—'}</span>,
    },
    {
      title: '覆盖',
      dataIndex: 'coverage',
      width: 78,
      align: 'right',
      render: (value: number | null) => <span className="font-mono text-xs">{fmtPct(value)}</span>,
    },
    {
      title: 'R²',
      dataIndex: 'r2',
      width: 78,
      align: 'right',
      render: (value: number | null) => <span className="font-mono text-xs">{fmtNum(value, 3)}</span>,
    },
    {
      title: '原因',
      dataIndex: 'reason',
      ellipsis: true,
      render: (value: string | null) =>
        value ? <span className="text-[11px] text-amber-600">{value}</span> : <span className="text-slate-300 text-xs">—</span>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 96,
      render: (_, row) => (
        <Button
          size="small"
          type="link"
          className="px-0 text-xs"
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
    <div className="flex flex-1 min-h-0 flex-col gap-3 p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <ShieldCheck size={16} className="text-slate-500" />
          <span className="text-xs font-black uppercase tracking-widest text-slate-600">
            推理中性化 {marketLabel ? `· ${marketLabel}` : ''}
          </span>
          <Tag color="blue">影子优先</Tag>
        </div>
        <div className="flex items-center gap-2">
          <Select
            size="small"
            showSearch
            placeholder="选择模型开启中性化"
            value={newModelId || undefined}
            onChange={(value) => setNewModelId(value)}
            style={{ width: 300 }}
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
          >
            配置
          </Button>
          <Button size="small" icon={<RefreshCw size={12} />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        </div>
      </div>

      <Table<NeutralizationOverviewRow>
        size="small"
        rowKey="model_id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 12, size: 'small', hideOnSinglePage: true }}
        locale={{ emptyText: '还没有模型开启中性化。先从右上角选择模型并保存为「影子」模式。' }}
      />

      <Drawer
        title="中性化配置与诊断"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={860}
        destroyOnClose
      >
        {selected && (
          <NeutralizationModelPanel
            modelId={selected}
            modelName={models.find((m) => m.model_id === selected) ? modelDisplayName(models.find((m) => m.model_id === selected)!) : selected}
          />
        )}
      </Drawer>
    </div>
  );
}
