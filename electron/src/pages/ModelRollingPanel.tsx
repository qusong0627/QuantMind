/**
 * 模型管理 →「滚动训练」面板（P1 · 设计文档《滚动训练与模型生命周期》）。
 *
 * 四块能力（bento 里从 01 到 04 就是使用顺序）：
 * 1. 从当前模型派生滚动配方（云端导入模型同路径可用：读模型目录产物推导；
 *    可先预览再保存，保存后进入配方下拉，与内建配方同权）；
 * 2. 立即执行（dry_run 先看窗口计划，确认后真派发；低内存后端 409 拒绝）；
 * 3. 月度调度（按市场保存；窗口策略归配方所有，此处不提供覆写）；
 * 4. 滚动台账（qm_rolling_campaigns；市场/状态过滤 + 刷新）。
 *
 * 展示面不做执行面含糊化：这里的动作就是「派发训练」，按钮直说。
 * 视觉：Tailwind 卡片式布局（与推理中性化面板同一套设计语言）；训练/验证/测试三段
 * 窗口用色带做数据可视化——有派发计划时带真实日期区间，没有时给窗口档说明。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button, DatePicker, Empty, Input, InputNumber, Modal, Select,
  Spin, Switch, Table, Tooltip, message,
} from 'antd';
import type { TableColumnsType } from 'antd';
import {
  Activity, AlertTriangle, CalendarClock, History, Play, RefreshCw, Rocket, Save, Wand2,
} from 'lucide-react';
import dayjs, { Dayjs } from 'dayjs';
import type { UserModelRecord } from '../services/modelTrainingService';
import {
  modelRollingService,
  type DeriveResult,
  type DispatchPlan,
  type DispatchResult,
  type RetrainSchedule,
  type RollingCampaign,
  type RollingRecipeSummary,
  type RollingWindowPolicy,
  type SchedulerHeartbeat,
  type ScheduleUpdateBody,
  rollingErrorMessage,
} from '../services/modelRollingService';
import { clsx } from 'clsx';

type Tone = 'ok' | 'warn' | 'err' | 'info' | 'muted' | 'violet';

const PILL_CLS: Record<Tone, string> = {
  ok: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  warn: 'border-amber-200 bg-amber-50 text-amber-700',
  err: 'border-rose-200 bg-rose-50 text-rose-700',
  info: 'border-sky-200 bg-sky-50 text-sky-700',
  muted: 'border-slate-200 bg-slate-50 text-slate-500',
  violet: 'border-violet-200 bg-violet-50 text-violet-700',
};

function Pill({ tone = 'muted', children }: { tone?: Tone; children: React.ReactNode }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-black',
        PILL_CLS[tone],
      )}
    >
      {children}
    </span>
  );
}

const STATUS_PILL: Record<string, { tone: Tone; label: string }> = {
  planned: { tone: 'warn', label: '已计划' },
  dispatched: { tone: 'info', label: '已提交' },
  registered: { tone: 'ok', label: '已注册' },
  failed: { tone: 'err', label: '失败' },
  skipped: { tone: 'muted', label: '已跳过' },
};

const DERIVE_PILL: Record<DeriveResult['status'], { tone: Tone; label: string }> = {
  preview: { tone: 'muted', label: '预览（未落盘）' },
  saved: { tone: 'ok', label: '已保存' },
  unchanged: { tone: 'warn', label: '内容未变' },
};

const DISPATCH_STATUS_TEXT: Record<string, string> = {
  dry_run: '预览',
  dispatched: '已派发',
  duplicate: '已存在（幂等命中）',
  skipped: '本轮跳过',
};

function dispatchTone(result: DispatchResult): Tone {
  if (result.status === 'dispatched') return 'info';
  if (result.status === 'dry_run') return result.ready === false ? 'warn' : 'ok';
  if (result.status === 'duplicate') return 'warn';
  return 'muted';
}

function windowPolicyText(wp?: RollingRecipeSummary['window_policy']): string {
  if (!wp) return '—';
  const mode = wp.mode === 'sliding' ? '滑动' : '扩展';
  const purge = wp.purge_days ? ` · purge ${wp.purge_days}` : '';
  return `${wp.train_days}/${wp.valid_days}/${wp.test_days} 天 · ${mode}${purge}`;
}

function dispatchSummary(result: DispatchResult): string {
  if (result.status === 'dry_run') {
    return result.ready === false
      ? `当前不可派发：${result.reason ?? '数据未就绪'}`
      : '窗口计划就绪（未提交训练）';
  }
  if (result.status === 'dispatched') {
    return `已提交训练：run_id=${result.run_id ?? '—'}`;
  }
  return result.message ?? result.reason ?? '—';
}

/** 训练/验证/测试三段窗口色带：有派发计划时附真实日期区间 */
const WindowBand: React.FC<{ wp?: RollingWindowPolicy; plan?: DispatchPlan | null }> = ({
  wp,
  plan,
}) => {
  if (!wp) return null;
  const segments = [
    {
      key: 'train',
      label: '训练',
      days: wp.train_days,
      dot: 'bg-violet-500',
      bar: 'from-violet-400 to-violet-500',
      range: plan?.train ?? null,
    },
    {
      key: 'valid',
      label: '验证',
      days: wp.valid_days,
      dot: 'bg-emerald-500',
      bar: 'from-emerald-400 to-emerald-500',
      range: plan?.valid ?? null,
    },
    {
      key: 'test',
      label: '测试',
      days: wp.test_days,
      dot: 'bg-amber-500',
      bar: 'from-amber-400 to-amber-500',
      range: plan?.test ?? null,
    },
  ] as const;
  const total = Math.max(1, segments.reduce((sum, s) => sum + s.days, 0));
  return (
    <div>
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-slate-100">
        {segments.map((s) => (
          <div
            key={s.key}
            className={clsx('h-full bg-gradient-to-r', s.bar)}
            style={{ width: `${(s.days / total) * 100}%` }}
          />
        ))}
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2">
        {segments.map((s) => (
          <div
            key={s.key}
            className="rounded-lg border border-slate-200 bg-white px-2 py-1.5"
          >
            <div className="flex items-center gap-1.5 text-[10px] font-black text-slate-600">
              <span className={clsx('h-1.5 w-1.5 rounded-full', s.dot)} />
              {s.label} · {s.days} 天
            </div>
            <div className="truncate font-mono text-[10px] text-slate-400">
              {s.range ? `${s.range[0]} → ${s.range[1]}` : '锚定日回推'}
            </div>
          </div>
        ))}
      </div>
      {!plan && (
        <div className="mt-1.5 text-[10px] text-slate-400">
          {wp.mode === 'sliding' ? '滑动窗口' : '扩展窗口'} · purge {wp.purge_days ?? 0} 天
        </div>
      )}
    </div>
  );
};

interface StepHeaderProps {
  no: string;
  title: string;
  icon: typeof Activity;
  tone: 'violet' | 'sky' | 'emerald' | 'amber';
  right?: React.ReactNode;
}

function StepHeader({ no, title, icon: Icon, tone, right }: StepHeaderProps) {
  const toneMap = {
    violet: 'bg-violet-50 text-violet-600',
    sky: 'bg-sky-50 text-sky-600',
    emerald: 'bg-emerald-50 text-emerald-600',
    amber: 'bg-amber-50 text-amber-600',
  } as const;
  return (
    <div className="flex items-center gap-2">
      <span className={clsx('rounded-lg p-1.5', toneMap[tone])}>
        <Icon size={14} />
      </span>
      <span className="font-mono text-[10px] font-black text-slate-300">{no}</span>
      <span className="text-xs font-black uppercase tracking-[0.12em] text-slate-700">
        {title}
      </span>
      <span className="h-px flex-1 bg-slate-100" />
      {right}
    </div>
  );
}

interface StatProps {
  label: string;
  value: string;
  hint?: string;
  tone?: 'slate' | 'violet' | 'emerald' | 'amber' | 'rose';
}

function Stat({ label, value, hint, tone = 'slate' }: StatProps) {
  const toneMap = {
    slate: 'text-slate-800',
    violet: 'text-violet-700',
    emerald: 'text-emerald-700',
    amber: 'text-amber-700',
    rose: 'text-rose-700',
  } as const;
  return (
    <div className="rounded-2xl border border-slate-200 bg-white px-3.5 py-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      <div className="text-[10px] font-black uppercase tracking-[0.16em] text-slate-400">
        {label}
      </div>
      <div className={clsx('mt-1.5 font-mono text-lg font-black leading-none', toneMap[tone])}>
        {value}
      </div>
      {hint && <div className="mt-1 text-[10px] text-slate-400">{hint}</div>}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-bold text-slate-400">{label}</span>
      {children}
    </label>
  );
}

export const ModelRollingPanel: React.FC<{ model: UserModelRecord }> = ({ model }) => {
  const modelMarket = (model.market || 'CN').toUpperCase();
  const [market, setMarket] = useState(modelMarket);

  const [recipes, setRecipes] = useState<RollingRecipeSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const [schedules, setSchedules] = useState<Record<string, RetrainSchedule>>({});
  // 派发器心跳（后端口径）：调度存了 enabled≠到点真会跑——M5 复盘可见性
  const [dispatchHealth, setDispatchHealth] = useState<SchedulerHeartbeat | null>(null);
  const [draft, setDraft] = useState<ScheduleUpdateBody | null>(null);
  const [saving, setSaving] = useState(false);

  const [dispatchRecipe, setDispatchRecipe] = useState('');
  const [anchor, setAnchor] = useState<Dayjs | null>(null);
  const [dispatchResult, setDispatchResult] = useState<DispatchResult | null>(null);
  const [dispatching, setDispatching] = useState<'preview' | 'run' | null>(null);

  const [deriveResult, setDeriveResult] = useState<DeriveResult | null>(null);
  const [deriving, setDeriving] = useState<'preview' | 'save' | null>(null);

  const [campaigns, setCampaigns] = useState<RollingCampaign[]>([]);
  const [campaignsLoading, setCampaignsLoading] = useState(false);
  const [campaignMarket, setCampaignMarket] = useState<string | undefined>(modelMarket);

  const marketRecipes = useMemo(
    () => recipes.filter(r => r.valid && (r.market || '').toUpperCase() === market),
    [recipes, market],
  );
  const marketOptions = useMemo(
    () => Array.from(new Set(recipes.filter(r => r.valid && r.market).map(r => (r.market || '').toUpperCase())))
      .sort()
      .map(m => ({ value: m, label: m })),
    [recipes],
  );
  // 坏配方文件不上任何下拉，但必须可见——否则调度引用了它只会每天服务端告警
  const invalidRecipes = useMemo(() => recipes.filter(r => !r.valid), [recipes]);
  const validRecipeCount = useMemo(() => recipes.filter(r => r.valid).length, [recipes]);
  const derivedRecipeCount = useMemo(
    () => recipes.filter(r => r.valid && r.source === 'user').length,
    [recipes],
  );
  const selectedRecipe = useMemo(
    () => marketRecipes.find(r => r.recipe_id === dispatchRecipe),
    [marketRecipes, dispatchRecipe],
  );
  const scheduleEnabled = draft ? draft.enabled : (schedules[market]?.enabled ?? false);

  const loadRecipes = useCallback(async (): Promise<RollingRecipeSummary[]> => {
    const list = await modelRollingService.listRecipes();
    setRecipes(list);
    return list;
  }, []);

  const loadSchedules = useCallback(async (): Promise<Record<string, RetrainSchedule>> => {
    const data = await modelRollingService.getSchedules();
    setSchedules(data.schedules ?? {});
    setDispatchHealth(data.dispatch ?? null);
    return data.schedules ?? {};
  }, []);

  const loadCampaigns = useCallback(async (mk?: string) => {
    setCampaignsLoading(true);
    try {
      const rows = await modelRollingService.listCampaigns({ market: mk, limit: 50 });
      setCampaigns(rows);
    } catch (error) {
      message.error(rollingErrorMessage(error));
      setCampaigns([]);
    } finally {
      setCampaignsLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const [list, scheduleMap] = await Promise.all([loadRecipes(), loadSchedules()]);
        if (cancelled) return;
        syncDraft(modelMarket, scheduleMap, list);
      } catch (error) {
        if (!cancelled) message.error(rollingErrorMessage(error));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    void loadCampaigns(modelMarket);
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const syncDraft = useCallback(
    (mk: string, scheduleMap: Record<string, RetrainSchedule>, list: RollingRecipeSummary[]) => {
      const existing = scheduleMap[mk];
      if (existing) {
        const { window_policy: _wp, purge_days: _pd, last_run: _lr, ...rest } = existing;
        setDraft({ ...rest, window_policy: null, purge_days: null });
        return;
      }
      const fallbackRecipe = list.find(r => r.valid && (r.market || '').toUpperCase() === mk)?.recipe_id ?? '';
      setDraft({
        enabled: false,
        day_rule: 'first_trading_day',
        time: '15:30',
        recipe_id: fallbackRecipe,
        observation_days: 20,
        max_time_minutes: 240,
        executor: 'local',
        window_policy: null,
        purge_days: null,
      });
    },
    [],
  );

  const changeMarket = (mk: string) => {
    setMarket(mk);
    setDispatchResult(null);
    setDispatchRecipe('');
    syncDraft(mk, schedules, recipes);
    setCampaignMarket(mk);
    void loadCampaigns(mk);
  };

  // 派发配方默认选中：本模型派生过的配方 > 第一个可用配方
  useEffect(() => {
    if (dispatchRecipe && marketRecipes.some(r => r.recipe_id === dispatchRecipe)) return;
    const derived = marketRecipes.find(r => r.source_model_id === model.model_id);
    setDispatchRecipe(derived?.recipe_id ?? marketRecipes[0]?.recipe_id ?? '');
  }, [marketRecipes, dispatchRecipe, model.model_id]);

  const handleDerive = async (mode: 'preview' | 'save') => {
    setDeriving(mode);
    try {
      const result = await modelRollingService.deriveRecipe({
        model_id: model.model_id,
        dry_run: mode === 'preview',
      });
      setDeriveResult(result);
      if (mode === 'save') {
        message.success(result.status === 'unchanged'
          ? `配方未变化（内容一致，未重写）：${result.recipe_id}`
          : `配方已保存：${result.recipe_id}`);
        await loadRecipes();
        setDispatchRecipe(result.recipe_id);
      }
    } catch (error) {
      message.error(rollingErrorMessage(error));
    } finally {
      setDeriving(null);
    }
  };

  const runDispatch = async (dryRun: boolean) => {
    if (!dispatchRecipe) {
      message.warning('请先选择配方（可先从上一步派生）');
      return;
    }
    setDispatching(dryRun ? 'preview' : 'run');
    try {
      const result = await modelRollingService.dispatch({
        market,
        recipe_id: dispatchRecipe,
        dry_run: dryRun,
        anchor_date: anchor ? anchor.format('YYYY-MM-DD') : null,
      });
      setDispatchResult(result);
      if (!dryRun) {
        message.success(dispatchSummary(result));
        await loadCampaigns(campaignMarket);
      }
    } catch (error) {
      message.error(rollingErrorMessage(error));
    } finally {
      setDispatching(null);
    }
  };

  const confirmDispatch = () => {
    Modal.confirm({
      title: '确认立即派发滚动训练？',
      content: `市场 ${market} · 配方 ${dispatchRecipe}${anchor ? ` · 锚定日 ${anchor.format('YYYY-MM-DD')}` : ''}。将走与调度完全相同的提交链路并写入台账。`,
      okText: '派发',
      cancelText: '取消',
      onOk: () => runDispatch(false),
    });
  };

  const saveSchedule = async () => {
    if (!draft) return;
    // 与后端 Field(pattern) 同口径的 24h HH:MM——不一致会让「前端过、后端 422」
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.time || '')) {
      message.warning('触发时间格式应为 24 小时制 HH:mm（如 15:30）');
      return;
    }
    if (!draft.recipe_id) {
      message.warning('请选择配方');
      return;
    }
    setSaving(true);
    try {
      const data = await modelRollingService.saveSchedule(market, draft);
      setSchedules(prev => ({ ...prev, [market]: data.schedule }));
      // 以服务端归一化后的配置回写草稿——否则「前端显示 25:99、后端存了别的」不可见
      const { window_policy: _wp, purge_days: _pd, last_run: _lr, ...rest } = data.schedule;
      setDraft({ ...rest, window_policy: null, purge_days: null });
      message.success(`${market} 重训调度已保存${draft.enabled ? '（已开启）' : '（未开启）'}`);
    } catch (error) {
      message.error(rollingErrorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const recipeOptions = marketRecipes.map(r => ({
    value: r.recipe_id,
    label: (
      <span className="flex items-center gap-1.5">
        <span className="font-mono text-xs">{r.recipe_id}</span>
        {r.source === 'user' && <Pill tone="violet">派生</Pill>}
        <span className="text-slate-400 text-[11px]">{windowPolicyText(r.window_policy)}</span>
      </span>
    ),
  }));

  const campaignColumns: TableColumnsType<RollingCampaign> = [
    { title: '市场', dataIndex: 'market', width: 64, render: (v: string) => <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-black text-slate-600">{v}</span> },
    {
      title: '配方', dataIndex: 'recipe_id', width: 200, ellipsis: true,
      render: (v: string) => <Tooltip title={v}><span className="font-mono text-xs">{v}</span></Tooltip>,
    },
    {
      title: '触发', dataIndex: 'trigger', width: 84,
      render: (v: string) => (
        <Pill tone={v === 'manual' ? 'violet' : 'muted'}>
          {v === 'manual' ? '手动' : '调度'}
        </Pill>
      ),
    },
    { title: '锚定日', dataIndex: 'anchor_date', width: 102, render: (v: string) => <span className="font-mono text-xs">{v}</span> },
    {
      title: '状态', dataIndex: 'status', width: 92,
      render: (v: string) => {
        const cfg = STATUS_PILL[v] ?? { tone: 'muted' as Tone, label: v };
        return <Pill tone={cfg.tone}>{cfg.label}</Pill>;
      },
    },
    { title: '尝试', dataIndex: 'attempts', width: 56 },
    {
      title: 'run_id', dataIndex: 'run_id', width: 160, ellipsis: true,
      render: (v: string | null) => v
        ? <Tooltip title={v}><span className="font-mono text-xs text-slate-500">{v}</span></Tooltip>
        : <span className="text-slate-300">—</span>,
    },
    {
      title: '更新时间', dataIndex: 'updated_at', width: 150, ellipsis: true,
      render: (v?: string) => <span className="text-xs text-slate-400">{v ? dayjs(v).format('MM-DD HH:mm:ss') : '—'}</span>,
    },
  ];

  if (loading) return <div className="flex justify-center py-12"><Spin /></div>;

  const lastRun = schedules[market]?.last_run;

  // 派发器心跳 → 状态标记（口径 = 后端 read_heartbeats/体检 C07，前端不做二次判定）。
  // 「调度存了 enabled 但没人派发」曾经整月不可见（M5 复盘），此标记即真相出口。
  const dispatchHealthChip: { tone: Tone; label: string } = (() => {
    if (!dispatchHealth) return { tone: 'muted', label: '派发器状态未知（心跳读取失败）' };
    switch (dispatchHealth.state) {
      case 'ok':
        return { tone: 'ok', label: `派发器正常 · ${dispatchHealth.age ?? 0}s 前心跳` };
      case 'stale':
        return { tone: 'err', label: `派发器心跳过期 ${dispatchHealth.age ?? '?'}s —— 调度不会触发，检查 celery-beat` };
      case 'off':
        return { tone: 'warn', label: '派发总闸未开（RETRAIN_SCHEDULER_ENABLED）——保存调度也不会自动派发' };
      default:
        return { tone: 'warn', label: '派发器无心跳记录 —— 尚未运行，到点不会自动派发' };
    }
  })();

  const healthValue =
    dispatchHealth?.state === 'ok'
      ? '正常'
      : dispatchHealth?.state === 'stale'
        ? '心跳过期'
        : dispatchHealth?.state === 'off'
          ? '总闸关闭'
          : '未知';

  return (
    <div className="flex flex-col gap-4">
      {/* ── 顶部身份条 ── */}
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.06)]">
        <div className="h-1 w-full bg-gradient-to-r from-violet-500 via-indigo-500 to-sky-400" />
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex items-center gap-2.5">
            <span className="rounded-xl bg-violet-50 p-2 text-violet-600">
              <Activity size={18} />
            </span>
            <div>
              <div className="text-sm font-black text-slate-800">滚动训练</div>
              <div className="text-[10px] font-bold uppercase tracking-[0.14em] text-slate-400">
                配方驱动的月度重训 · 手动派发与调度同链路、同台账
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Select
              size="small"
              className="!w-[92px]"
              value={market}
              onChange={changeMarket}
              options={marketOptions.length ? marketOptions : [{ value: modelMarket, label: modelMarket }]}
            />
            <Pill tone={scheduleEnabled ? 'ok' : 'muted'}>
              {market} 调度{scheduleEnabled ? '已启用' : '未启用'}
            </Pill>
            <Button
              size="small"
              icon={<RefreshCw size={12} />}
              className="!rounded-lg"
              onClick={() => {
                void (async () => {
                  const list = await loadRecipes();
                  const scheduleMap = await loadSchedules();
                  syncDraft(market, scheduleMap, list);
                  await loadCampaigns(campaignMarket);
                })();
              }}
            >
              刷新
            </Button>
          </div>
        </div>
      </div>

      {/* ── KPI ── */}
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
        <Stat
          label="有效配方"
          value={String(validRecipeCount)}
          hint={`派生 ${derivedRecipeCount} 个`}
          tone="violet"
        />
        <Stat
          label="当前市场"
          value={market}
          hint={`${marketRecipes.length} 个可用配方`}
        />
        <Stat
          label="调度状态"
          value={scheduleEnabled ? '已启用' : '未启用'}
          hint={`每月首个交易日 ${draft?.time ?? '—'}`}
          tone={scheduleEnabled ? 'emerald' : 'slate'}
        />
        <Stat
          label="派发器"
          value={healthValue}
          hint={
            dispatchHealth?.age != null
              ? `${dispatchHealth.age}s 前心跳`
              : '检查 celery-beat'
          }
          tone={
            dispatchHealth?.state === 'ok'
              ? 'emerald'
              : dispatchHealth?.state === 'stale'
                ? 'rose'
                : 'amber'
          }
        />
      </div>

      {invalidRecipes.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <div>
            有 {invalidRecipes.length} 个配方文件不可用（已从下拉中隐藏）：
            {invalidRecipes.map(r => `${r.recipe_id}（${r.error ?? '校验不过'}）`).join('；')}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {/* ── 01 派生配方 ── */}
        <section className="rounded-2xl border border-violet-200/70 bg-gradient-to-br from-violet-50/70 via-white to-sky-50/50 p-4 shadow-[0_1px_3px_rgba(15,23,42,0.05)] lg:col-span-2">
          <StepHeader
            no="01"
            title="派生滚动配方"
            icon={Wand2}
            tone="violet"
            right={<Pill tone="violet">云端导入模型可用</Pill>}
          />
          <div className="mt-3 grid gap-4 lg:grid-cols-5">
            <div className="lg:col-span-3">
              <div className="font-mono text-[11px] text-slate-400">
                来源模型 · {model.model_id}
              </div>
              <p className="mt-1.5 text-xs leading-relaxed text-slate-600">
                读取模型目录产物（metadata.json / config.yaml）复刻其训练语义为滚动配方；
                云端下载导入的模型同样可用。保存后进入用户配方目录，与内建配方在派发、调度上完全同权。
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button
                  size="small"
                  icon={<Wand2 size={13} />}
                  loading={deriving === 'preview'}
                  disabled={deriving === 'save'}
                  onClick={() => handleDerive('preview')}
                  className="!rounded-lg text-[11px] font-bold"
                >
                  预览推导结果
                </Button>
                <Button
                  size="small"
                  type="primary"
                  icon={<Save size={13} />}
                  loading={deriving === 'save'}
                  disabled={deriving === 'preview'}
                  onClick={() => handleDerive('save')}
                  className="!rounded-lg !border-0 !bg-violet-600 text-[11px] font-bold"
                >
                  保存为配方
                </Button>
              </div>
            </div>
            <div className="rounded-xl border border-slate-200 bg-white/90 p-3 lg:col-span-2">
              {deriveResult ? (
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Pill tone={DERIVE_PILL[deriveResult.status].tone}>
                      {DERIVE_PILL[deriveResult.status].label}
                    </Pill>
                    <span className="font-mono text-xs font-bold text-slate-700">
                      {deriveResult.recipe_id}
                    </span>
                  </div>
                  <div className="mt-2.5">
                    <WindowBand wp={deriveResult.window_policy} />
                  </div>
                  <div className="mt-2.5 space-y-1 font-mono text-[10px] text-slate-400">
                    <div>
                      市场 {deriveResult.market} · 因子面 {deriveResult.factor_market}（{deriveResult.factor_source}）
                    </div>
                    <div>
                      特征 {deriveResult.feature_count} 维 · 标签 {deriveResult.target_horizon_days} 日 · hash {deriveResult.recipe_hash}
                    </div>
                    <div className="truncate">
                      来源文件 {deriveResult.source_files.join(' + ') || '—'}
                    </div>
                  </div>
                  {deriveResult.warnings.length > 0 && (
                    <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1.5 text-[10px] text-amber-700">
                      ⚠ {deriveResult.warnings.join('；')}
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex h-full min-h-[130px] flex-col items-center justify-center gap-2 text-center">
                  <span className="rounded-xl bg-violet-50 p-2 text-violet-500">
                    <Wand2 size={18} />
                  </span>
                  <div className="text-[11px] font-bold text-slate-500">尚未派生</div>
                  <div className="text-[10px] text-slate-400">
                    先预览窗口与特征口径，确认无误再保存为配方
                  </div>
                </div>
              )}
            </div>
          </div>
        </section>

        {/* ── 02 立即执行 ── */}
        <section className="rounded-2xl border border-slate-200 bg-white p-4">
          <StepHeader no="02" title="立即执行" icon={Rocket} tone="sky" />
          <div className="mt-3 space-y-3">
            <div className="grid grid-cols-12 gap-2">
              <div className="col-span-3">
                <Field label="市场">
                  <Select
                    size="small"
                    value={market}
                    onChange={changeMarket}
                    options={marketOptions.length ? marketOptions : [{ value: modelMarket, label: modelMarket }]}
                  />
                </Field>
              </div>
              <div className="col-span-5">
                <Field label="配方">
                  <Select
                    size="small"
                    placeholder="选择配方"
                    value={dispatchRecipe || undefined}
                    onChange={setDispatchRecipe}
                    options={recipeOptions}
                    notFoundContent={<span className="text-xs">该市场暂无有效配方（可先派生）</span>}
                  />
                </Field>
              </div>
              <div className="col-span-4">
                <Field label="锚定日">
                  <DatePicker
                    size="small"
                    className="!w-full"
                    placeholder="缺省=今天"
                    value={anchor}
                    onChange={setAnchor}
                    allowClear
                  />
                </Field>
              </div>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-50/60 p-2.5">
              {selectedRecipe?.window_policy ? (
                <WindowBand wp={selectedRecipe.window_policy} plan={dispatchResult?.plan ?? null} />
              ) : (
                <div className="py-3 text-center text-[11px] text-slate-400">
                  选择配方后显示其滚动窗口（训练 / 验证 / 测试）
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="small"
                icon={<Play size={13} />}
                loading={dispatching === 'preview'}
                disabled={dispatching === 'run'}
                onClick={() => runDispatch(true)}
                className="!rounded-lg text-[11px] font-bold"
              >
                预览窗口计划
              </Button>
              <Button
                size="small"
                type="primary"
                className="!rounded-lg !border-0 !bg-slate-900 text-[11px] font-bold"
                icon={<Rocket size={13} />}
                loading={dispatching === 'run'}
                disabled={dispatching === 'preview'}
                onClick={confirmDispatch}
              >
                执行派发
              </Button>
            </div>
            {dispatchResult && (
              <div className="rounded-xl border border-slate-200 bg-white p-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone={dispatchTone(dispatchResult)}>
                    {DISPATCH_STATUS_TEXT[dispatchResult.status] ?? dispatchResult.status}
                  </Pill>
                  <span className="text-[11px] text-slate-600">{dispatchSummary(dispatchResult)}</span>
                </div>
                {dispatchResult.plan && (
                  <div className="mt-1.5 font-mono text-[10px] text-slate-400">
                    锚定 {dispatchResult.plan.anchor_date} · 窗口内序号 {dispatchResult.plan.window_index} · purge {dispatchResult.plan.purge_days}
                  </div>
                )}
                {dispatchResult.campaign_id && (
                  <div className="mt-0.5 font-mono text-[10px] text-slate-400">
                    campaign {dispatchResult.campaign_id}
                  </div>
                )}
                {dispatchResult.detail && dispatchResult.status !== 'dispatched' && (
                  <div className="mt-1 max-h-24 overflow-auto rounded-lg bg-slate-50 p-2 font-mono text-[10px] text-slate-500">
                    {JSON.stringify(dispatchResult.detail)}
                  </div>
                )}
              </div>
            )}
          </div>
        </section>

        {/* ── 03 月度调度 ── */}
        <section className="rounded-2xl border border-slate-200 bg-white p-4">
          <StepHeader
            no="03"
            title="月度重训调度"
            icon={CalendarClock}
            tone="emerald"
            right={<Pill tone={dispatchHealthChip.tone}>{dispatchHealthChip.label}</Pill>}
          />
          {draft ? (
            <div className="mt-3 space-y-3">
              <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2">
                <div>
                  <div className="text-xs font-black text-slate-700">启用月度重训</div>
                  <div className="text-[10px] text-slate-400">
                    每月首个交易日 {draft.time} 自动派发 · 窗口策略归配方所有
                  </div>
                </div>
                <Switch
                  size="small"
                  checked={draft.enabled}
                  onChange={v => setDraft({ ...draft, enabled: v })}
                />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label="触发规则">
                  <Select
                    size="small"
                    value={draft.day_rule}
                    onChange={v => setDraft({ ...draft, day_rule: v })}
                    options={[{ value: 'first_trading_day', label: '每月首个交易日' }]}
                  />
                </Field>
                <Field label="触发时间">
                  <Input
                    size="small"
                    value={draft.time}
                    onChange={e => setDraft({ ...draft, time: e.target.value })}
                    placeholder="15:30"
                  />
                </Field>
                <div className="col-span-2">
                  <Field label="配方">
                    <Select
                      size="small"
                      placeholder="选择配方"
                      value={draft.recipe_id || undefined}
                      onChange={v => setDraft({ ...draft, recipe_id: v })}
                      options={recipeOptions}
                    />
                  </Field>
                </div>
                <Field label="观察天数">
                  <InputNumber
                    size="small"
                    className="!w-full"
                    min={1}
                    max={250}
                    value={draft.observation_days}
                    onChange={v => setDraft({ ...draft, observation_days: v ?? 20 })}
                  />
                </Field>
                <Field label="训练时限（分）">
                  <InputNumber
                    size="small"
                    className="!w-full"
                    min={10}
                    max={1440}
                    value={draft.max_time_minutes}
                    onChange={v => setDraft({ ...draft, max_time_minutes: v ?? 240 })}
                  />
                </Field>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Button
                  type="primary"
                  size="small"
                  className="!rounded-lg !border-0 !bg-emerald-600 text-[11px] font-bold"
                  icon={<Save size={13} />}
                  loading={saving}
                  onClick={saveSchedule}
                >
                  保存调度
                </Button>
                <span className="font-mono text-[10px] text-slate-400">
                  上次运行 {lastRun ? dayjs(lastRun).format('YYYY-MM-DD HH:mm:ss') : '—'}
                </span>
              </div>
              <div className="text-[10px] leading-relaxed text-slate-400">
                窗口策略（训练/验证/测试天数）在配方里改；此处保存即对该市场生效。
                是否真的到点派发，以上方派发器心跳为准。
              </div>
            </div>
          ) : (
            <Empty description="暂无调度草稿" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}
        </section>

        {/* ── 04 台账 ── */}
        <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white lg:col-span-2">
          <div className="border-b border-slate-100 px-4 py-3">
            <StepHeader
              no="04"
              title="滚动台账"
              icon={History}
              tone="amber"
              right={
                <div className="flex items-center gap-2">
                  <Pill>{campaigns.length} 条</Pill>
                  <Select
                    size="small"
                    allowClear
                    className="!w-[120px]"
                    placeholder="全部市场"
                    value={campaignMarket}
                    onChange={v => {
                      setCampaignMarket(v);
                      void loadCampaigns(v);
                    }}
                    options={marketOptions}
                  />
                  <Button
                    size="small"
                    icon={<RefreshCw size={13} />}
                    loading={campaignsLoading}
                    onClick={() => loadCampaigns(campaignMarket)}
                    className="!rounded-lg"
                  >
                    刷新
                  </Button>
                </div>
              }
            />
          </div>
          <Table
            size="small"
            rowKey="campaign_id"
            loading={campaignsLoading}
            columns={campaignColumns}
            dataSource={campaigns}
            pagination={{ pageSize: 10, size: 'small', hideOnSinglePage: true }}
            scroll={{ x: 860 }}
            locale={{ emptyText: <Empty description="暂无派发记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          />
        </section>
      </div>
    </div>
  );
};
