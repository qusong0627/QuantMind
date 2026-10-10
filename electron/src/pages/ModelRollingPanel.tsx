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
 * 视觉：样式见 ./model-rolling.css（.rp-board 作用域）；训练/验证/测试三段
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
import './model-rolling.css';

const STATUS_PILL: Record<string, { cls: string; label: string }> = {
  planned: { cls: 'rp-pill--warn', label: '已计划' },
  dispatched: { cls: 'rp-pill--info', label: '已提交' },
  registered: { cls: 'rp-pill--ok', label: '已注册' },
  failed: { cls: 'rp-pill--err', label: '失败' },
  skipped: { cls: 'rp-pill--muted', label: '已跳过' },
};

const DERIVE_PILL: Record<DeriveResult['status'], { cls: string; label: string }> = {
  preview: { cls: 'rp-pill--muted', label: '预览（未落盘）' },
  saved: { cls: 'rp-pill--ok', label: '已保存' },
  unchanged: { cls: 'rp-pill--warn', label: '内容未变' },
};

const DISPATCH_STATUS_TEXT: Record<string, string> = {
  dry_run: '预览',
  dispatched: '已派发',
  duplicate: '已存在（幂等命中）',
  skipped: '本轮跳过',
};

function dispatchPillCls(result: DispatchResult): string {
  if (result.status === 'dispatched') return 'rp-pill--info';
  if (result.status === 'dry_run') return result.ready === false ? 'rp-pill--warn' : 'rp-pill--ok';
  if (result.status === 'duplicate') return 'rp-pill--warn';
  return 'rp-pill--muted';
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

const BAND_SEGMENTS = [
  { key: 'train', label: '训练' },
  { key: 'valid', label: '验证' },
  { key: 'test', label: '测试' },
] as const;

/** 训练/验证/测试三段窗口色带：有派发计划时附真实日期区间 */
const WindowBand: React.FC<{ wp?: RollingWindowPolicy; plan?: DispatchPlan | null }> = ({ wp, plan }) => {
  if (!wp) return null;
  const days: Record<'train' | 'valid' | 'test', number> = {
    train: wp.train_days,
    valid: wp.valid_days,
    test: wp.test_days,
  };
  const ranges: Record<'train' | 'valid' | 'test', [string, string] | null> = {
    train: plan?.train ?? null,
    valid: plan?.valid ?? null,
    test: plan?.test ?? null,
  };
  return (
    <div className="rp-band">
      <div className="rp-band__track">
        {BAND_SEGMENTS.map(s => (
          <span
            key={s.key}
            className={`rp-band__seg rp-band__seg--${s.key}`}
            style={{ flex: `${days[s.key]} 0 0` }}
          />
        ))}
      </div>
      {plan ? (
        <div className="rp-band__caps">
          {BAND_SEGMENTS.map(s => {
            const r = ranges[s.key];
            return (
              <div key={s.key} className={`rp-band__cap rp-band__cap--${s.key}`}>
                <b>{s.label} {days[s.key]} 天</b>
                <span>{r ? `${r[0]} → ${r[1]}` : '—'}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="rp-band__note">
          {wp.mode === 'sliding' ? '滑动窗口' : '扩展窗口'} · purge {wp.purge_days ?? 0} 天 · 锚定日回推
        </div>
      )}
    </div>
  );
};

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
        {r.source === 'user' && <span className="rp-pill rp-pill--violet">派生</span>}
        <span className="text-slate-400 text-[11px]">{windowPolicyText(r.window_policy)}</span>
      </span>
    ),
  }));

  const campaignColumns: TableColumnsType<RollingCampaign> = [
    { title: '市场', dataIndex: 'market', width: 64, render: (v: string) => <span className="rp-mk">{v}</span> },
    {
      title: '配方', dataIndex: 'recipe_id', width: 200, ellipsis: true,
      render: (v: string) => <Tooltip title={v}><span className="font-mono text-xs">{v}</span></Tooltip>,
    },
    {
      title: '触发', dataIndex: 'trigger', width: 84,
      render: (v: string) => (
        <span className={`rp-pill ${v === 'manual' ? 'rp-pill--violet' : 'rp-pill--muted'}`}>
          {v === 'manual' ? '手动' : '调度'}
        </span>
      ),
    },
    { title: '锚定日', dataIndex: 'anchor_date', width: 102, render: (v: string) => <span className="font-mono text-xs">{v}</span> },
    {
      title: '状态', dataIndex: 'status', width: 92,
      render: (v: string) => {
        const cfg = STATUS_PILL[v] ?? { cls: 'rp-pill--muted', label: v };
        return <span className={`rp-pill ${cfg.cls}`}>{cfg.label}</span>;
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
  const dispatchHealthChip = (() => {
    if (!dispatchHealth) return { cls: 'rp-pill--muted', label: '派发器状态未知（心跳读取失败）' };
    switch (dispatchHealth.state) {
      case 'ok':
        return { cls: 'rp-pill--ok', label: `派发器正常 · ${dispatchHealth.age ?? 0}s 前心跳` };
      case 'stale':
        return { cls: 'rp-pill--err', label: `派发器心跳过期 ${dispatchHealth.age ?? '?'}s —— 调度不会触发，检查 celery-beat` };
      case 'off':
        return { cls: 'rp-pill--warn', label: '派发总闸未开（RETRAIN_SCHEDULER_ENABLED）——保存调度也不会自动派发' };
      default:
        return { cls: 'rp-pill--warn', label: '派发器无心跳记录 —— 尚未运行，到点不会自动派发' };
    }
  })();

  return (
    <div className="rp-board">
      {/* 头部：标题 + 实时统计 */}
      <div className="rp-head">
        <div>
          <div className="rp-head__title"><Activity size={18} strokeWidth={2.5} />滚动训练</div>
          <div className="rp-head__sub">配方驱动的月度重训 · 手动派发与调度同链路、同台账 · 窗口策略归配方所有</div>
        </div>
        <div className="rp-chips">
          <div className="rp-chip"><b>{validRecipeCount}</b><span>有效配方</span></div>
          <div className="rp-chip"><b>{derivedRecipeCount}</b><span>派生配方</span></div>
          <div className="rp-chip">
            <b>
              {scheduleEnabled
                ? <span className="rp-pill rp-pill--ok">已启用</span>
                : <span className="rp-pill rp-pill--muted">未启用</span>}
            </b>
            <span>{market} 调度</span>
          </div>
        </div>
      </div>

      {/* 坏配方必须可见（否则调度引用了它只会每天服务端告警） */}
      {invalidRecipes.length > 0 && (
        <div className="rp-notice">
          <AlertTriangle size={14} />
          <div className="rp-notice__body">
            有 {invalidRecipes.length} 个配方文件不可用（已从下拉中隐藏）：{' '}
            {invalidRecipes.map(r => `${r.recipe_id}（${r.error ?? '校验不过'}）`).join('；')}
          </div>
        </div>
      )}

      <div className="rp-grid">
        {/* ① 从模型派生配方（深色主卡：云端导入模型的入口） */}
        <section className="rp-cell rp-cell--hero">
          <div className="rp-step">
            <span className="rp-step__icon rp-accent--violet"><Wand2 size={14} /></span>
            <span className="rp-step__no">01</span>
            <span className="rp-step__title">派生滚动配方</span>
            <span className="rp-step__rule" />
          </div>
          <div className="rp-hero__model">来源模型 · {model.model_id}</div>
          <div className="rp-hero__desc">
            读取模型目录产物（metadata.json / config.yaml）复刻其训练语义为滚动配方；
            云端下载导入的模型同样可用。保存后进入用户配方目录，与内建配方在派发/调度上完全同权。
          </div>
          <div className="rp-hero__actions">
            <Button size="small" className="rp-btn-ghost" icon={<Wand2 size={13} />} loading={deriving === 'preview'} disabled={deriving === 'save'} onClick={() => handleDerive('preview')}>预览推导结果</Button>
            <Button size="small" className="rp-btn-violet" icon={<Save size={13} />} loading={deriving === 'save'} disabled={deriving === 'preview'} onClick={() => handleDerive('save')}>保存为配方</Button>
          </div>
          {deriveResult && (
            <div className="rp-readout">
              <div className="rp-readout__head">
                <span className={`rp-pill ${DERIVE_PILL[deriveResult.status].cls}`}>{DERIVE_PILL[deriveResult.status].label}</span>
                <span className="rp-readout__id">{deriveResult.recipe_id}</span>
              </div>
              <WindowBand wp={deriveResult.window_policy} />
              <div className="rp-readout__mono">
                市场 {deriveResult.market} · 因子面 {deriveResult.factor_market}（{deriveResult.factor_source}） · 特征 {deriveResult.feature_count} 维 · 标签 {deriveResult.target_horizon_days} 日
              </div>
              <div className="rp-readout__mono">来源文件 {deriveResult.source_files.join(' + ') || '—'}</div>
              <div className="rp-readout__mono">
                hash {deriveResult.recipe_hash}{deriveResult.path ? ` · ${deriveResult.path}` : ''}
              </div>
              {deriveResult.warnings.length > 0 && (
                <div className="rp-hero__warn">⚠ {deriveResult.warnings.join('；')}</div>
              )}
            </div>
          )}
        </section>

        {/* ② 立即执行 */}
        <section className="rp-cell">
          <div className="rp-step">
            <span className="rp-step__icon rp-accent--blue"><Rocket size={14} /></span>
            <span className="rp-step__no">02</span>
            <span className="rp-step__title">立即执行</span>
            <span className="rp-step__rule" />
          </div>
          <div className="rp-console">
            <div className="rp-console__row">
              <Select
                size="small"
                className="!w-[92px]"
                value={market}
                onChange={changeMarket}
                options={marketOptions.length ? marketOptions : [{ value: modelMarket, label: modelMarket }]}
              />
              <Select
                size="small"
                className="flex-1 !min-w-[220px]"
                placeholder="选择配方"
                value={dispatchRecipe || undefined}
                onChange={setDispatchRecipe}
                options={recipeOptions}
                notFoundContent={<span className="text-xs">该市场暂无有效配方（可先派生）</span>}
              />
              <DatePicker
                size="small"
                className="!w-[176px]"
                placeholder="锚定日（缺省=今天）"
                value={anchor}
                onChange={setAnchor}
                allowClear
              />
            </div>
            <div className="rp-inset">
              {selectedRecipe?.window_policy
                ? <WindowBand wp={selectedRecipe.window_policy} plan={dispatchResult?.plan ?? null} />
                : <div className="rp-band__note">选择配方后显示其滚动窗口（训练 / 验证 / 测试）</div>}
            </div>
            <div className="rp-console__actions">
              <Button size="small" icon={<Play size={13} />} loading={dispatching === 'preview'} disabled={dispatching === 'run'} onClick={() => runDispatch(true)}>预览窗口计划</Button>
              <Button size="small" className="rp-btn-dispatch" icon={<Rocket size={13} />} loading={dispatching === 'run'} disabled={dispatching === 'preview'} onClick={confirmDispatch}>执行派发</Button>
            </div>
            {dispatchResult && (
              <div className="rp-readout">
                <div className="rp-readout__head">
                  <span className={`rp-pill ${dispatchPillCls(dispatchResult)}`}>
                    {DISPATCH_STATUS_TEXT[dispatchResult.status] ?? dispatchResult.status}
                  </span>
                  <span>{dispatchSummary(dispatchResult)}</span>
                </div>
                {dispatchResult.plan && (
                  <div className="rp-readout__mono">
                    锚定 {dispatchResult.plan.anchor_date} · 窗口内序号 {dispatchResult.plan.window_index} · purge {dispatchResult.plan.purge_days}
                  </div>
                )}
                {dispatchResult.campaign_id && (
                  <div className="rp-readout__mono">campaign {dispatchResult.campaign_id}</div>
                )}
                {dispatchResult.detail && dispatchResult.status !== 'dispatched' && (
                  <div className="rp-readout__json">{JSON.stringify(dispatchResult.detail)}</div>
                )}
              </div>
            )}
          </div>
        </section>

        {/* ③ 月度调度 */}
        <section className="rp-cell rp-cell--schedule">
          <div className="rp-step">
            <span className="rp-step__icon rp-accent--emerald"><CalendarClock size={14} /></span>
            <span className="rp-step__no">03</span>
            <span className="rp-step__title">月度重训调度</span>
            <span className="rp-pill rp-pill--muted">{market}</span>
            <span className="rp-step__rule" />
          </div>
          <div className="rp-dispatch-health">
            <span className={`rp-pill ${dispatchHealthChip.cls}`}>{dispatchHealthChip.label}</span>
          </div>
          {draft ? (
            <div className="rp-form">
              <div className="rp-field rp-field--power">
                <span className="rp-field__label">启用</span>
                <div className={`rp-power ${draft.enabled ? 'rp-power--on' : ''}`}>
                  <b>{draft.enabled ? '已启用' : '未启用'}</b>
                  <Switch size="small" checked={draft.enabled} onChange={v => setDraft({ ...draft, enabled: v })} />
                </div>
              </div>
              <div className="rp-field rp-field--rule">
                <span className="rp-field__label">触发规则</span>
                <Select
                  size="small"
                  className="!w-full"
                  value={draft.day_rule}
                  onChange={v => setDraft({ ...draft, day_rule: v })}
                  options={[{ value: 'first_trading_day', label: '每月首个交易日' }]}
                />
              </div>
              <div className="rp-field rp-field--time">
                <span className="rp-field__label">触发时间</span>
                <Input size="small" value={draft.time} onChange={e => setDraft({ ...draft, time: e.target.value })} placeholder="15:30" />
              </div>
              <div className="rp-field rp-field--recipe">
                <span className="rp-field__label">配方</span>
                <Select
                  size="small"
                  className="!w-full"
                  placeholder="选择配方"
                  value={draft.recipe_id || undefined}
                  onChange={v => setDraft({ ...draft, recipe_id: v })}
                  options={recipeOptions}
                />
              </div>
              <div className="rp-field rp-field--obs">
                <span className="rp-field__label">观察天数</span>
                <InputNumber size="small" className="!w-full" min={1} max={250} value={draft.observation_days} onChange={v => setDraft({ ...draft, observation_days: v ?? 20 })} />
              </div>
              <div className="rp-field rp-field--max">
                <span className="rp-field__label">训练时限（分）</span>
                <InputNumber size="small" className="!w-full" min={10} max={1440} value={draft.max_time_minutes} onChange={v => setDraft({ ...draft, max_time_minutes: v ?? 240 })} />
              </div>
              <div className="rp-field rp-field--save">
                <Button className="rp-btn-dispatch" icon={<Save size={13} />} loading={saving} onClick={saveSchedule}>保存调度</Button>
              </div>
              <div className="rp-field rp-field--last">
                <span className="rp-field__label">上次运行</span>
                <div className="rp-lastrun">
                  <b>{lastRun ? dayjs(lastRun).format('YYYY-MM-DD HH:mm') : '—'}</b>
                </div>
              </div>
              <div className="rp-foot">
                窗口策略（训练/验证/测试天数）归配方所有，在配方里改；此处保存即对该市场生效。是否真的到点派发，以上方派发器心跳标记为准。
              </div>
            </div>
          ) : (
            <Empty description="暂无调度草稿" />
          )}
        </section>

        {/* ④ 台账 */}
        <section className="rp-cell rp-cell--ledger">
          <div className="rp-step">
            <span className="rp-step__icon rp-accent--amber"><History size={14} /></span>
            <span className="rp-step__no">04</span>
            <span className="rp-step__title">滚动台账</span>
            <span className="rp-step__rule" />
            <span className="rp-pill rp-pill--muted">{campaigns.length} 条</span>
            <Select
              size="small"
              allowClear
              className="!w-[120px]"
              placeholder="全部市场"
              value={campaignMarket}
              onChange={v => { setCampaignMarket(v); void loadCampaigns(v); }}
              options={marketOptions}
            />
            <Button size="small" icon={<RefreshCw size={13} />} loading={campaignsLoading} onClick={() => loadCampaigns(campaignMarket)}>刷新</Button>
          </div>
          <Table
            className="rp-table"
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
