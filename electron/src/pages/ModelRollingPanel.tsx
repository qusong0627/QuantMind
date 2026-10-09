/**
 * 模型管理 →「滚动训练」面板（P1 · 设计文档《滚动训练与模型生命周期》）。
 *
 * 四块能力（自上而下就是使用顺序）：
 * 1. 从当前模型派生滚动配方（云端导入模型同路径可用：读模型目录产物推导；
 *    可先预览再保存，保存后进入配方下拉，与内建配方同权）；
 * 2. 立即执行（dry_run 先看窗口计划，确认后真派发；低内存后端 409 拒绝）；
 * 3. 月度调度（按市场保存；窗口策略归配方所有，此处不提供覆写）；
 * 4. 滚动台账（qm_rolling_campaigns；市场/状态过滤 + 刷新）。
 *
 * 展示面不做执行面含糊化：这里的动作就是「派发训练」，按钮直说。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Card, DatePicker, Empty, Input, InputNumber, Modal, Select,
  Space, Spin, Switch, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import type { TableColumnsType } from 'antd';
import {
  CalendarClock, History, Play, RefreshCw, Rocket, Save, Wand2,
} from 'lucide-react';
import dayjs, { Dayjs } from 'dayjs';
import type { UserModelRecord } from '../services/modelTrainingService';
import {
  modelRollingService,
  type DeriveResult,
  type DispatchResult,
  type RetrainSchedule,
  type RollingCampaign,
  type RollingRecipeSummary,
  type ScheduleUpdateBody,
  rollingErrorMessage,
} from '../services/modelRollingService';

const { Text } = Typography;

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  planned: { color: 'gold', label: '已计划' },
  dispatched: { color: 'blue', label: '已提交' },
  registered: { color: 'green', label: '已注册' },
  failed: { color: 'red', label: '失败' },
  skipped: { color: 'default', label: '已跳过' },
};

const DISPATCH_STATUS_TEXT: Record<string, string> = {
  dry_run: '预览',
  dispatched: '已派发',
  duplicate: '已存在（幂等命中）',
  skipped: '本轮跳过',
};

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

export const ModelRollingPanel: React.FC<{ model: UserModelRecord }> = ({ model }) => {
  const modelMarket = (model.market || 'CN').toUpperCase();
  const [market, setMarket] = useState(modelMarket);

  const [recipes, setRecipes] = useState<RollingRecipeSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const [schedules, setSchedules] = useState<Record<string, RetrainSchedule>>({});
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

  const loadRecipes = useCallback(async (): Promise<RollingRecipeSummary[]> => {
    const list = await modelRollingService.listRecipes();
    setRecipes(list);
    return list;
  }, []);

  const loadSchedules = useCallback(async (): Promise<Record<string, RetrainSchedule>> => {
    const data = await modelRollingService.getSchedules();
    setSchedules(data.schedules ?? {});
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
        {r.source === 'user' && <Tag color="purple" className="!m-0 !text-[10px]">派生</Tag>}
        <span className="text-slate-400 text-[11px]">{windowPolicyText(r.window_policy)}</span>
      </span>
    ),
  }));

  const campaignColumns: TableColumnsType<RollingCampaign> = [
    { title: '市场', dataIndex: 'market', width: 64, render: (v: string) => <Tag className="font-mono !m-0">{v}</Tag> },
    {
      title: '配方', dataIndex: 'recipe_id', width: 200, ellipsis: true,
      render: (v: string) => <Tooltip title={v}><span className="font-mono text-xs">{v}</span></Tooltip>,
    },
    { title: '触发', dataIndex: 'trigger', width: 84, render: (v: string) => (v === 'manual' ? '手动' : '调度') },
    { title: '锚定日', dataIndex: 'anchor_date', width: 102, render: (v: string) => <span className="font-mono text-xs">{v}</span> },
    {
      title: '状态', dataIndex: 'status', width: 84,
      render: (v: string) => {
        const cfg = STATUS_TAG[v] ?? { color: 'default', label: v };
        return <Tag color={cfg.color} className="!m-0">{cfg.label}</Tag>;
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

  return (
    <div className="space-y-4">
      {/* ① 从模型派生配方 */}
      <Card
        size="small"
        className="rounded-2xl"
        title={<span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><Wand2 size={13} className="text-violet-500" />从当前模型派生滚动配方</span>}
      >
        <div className="text-xs text-slate-500 leading-relaxed mb-3">
          读取模型目录产物（metadata.json / config.yaml）复刻其训练语义为滚动配方；
          云端下载导入的模型同样可用。派生配方保存进用户配方目录，与内建配方在派发/调度上完全同权。
        </div>
        <Space wrap>
          <Button size="small" icon={<Wand2 size={13} />} loading={deriving === 'preview'} disabled={deriving === 'save'} onClick={() => handleDerive('preview')}>预览推导结果</Button>
          <Button size="small" type="primary" className="bg-slate-900" icon={<Save size={13} />} loading={deriving === 'save'} disabled={deriving === 'preview'} onClick={() => handleDerive('save')}>保存为配方</Button>
          {deriveResult && (
            <Text type="secondary" className="text-xs">
              来源文件：{deriveResult.source_files.join(' + ') || '—'}
            </Text>
          )}
        </Space>
        {invalidRecipes.length > 0 && (
          <Alert
            type="warning"
            showIcon
            className="!py-1 mt-3"
            message={
              <span className="text-xs">
                有 {invalidRecipes.length} 个配方文件不可用（已从下拉中隐藏）：{' '}
                {invalidRecipes
                  .map(r => `${r.recipe_id}（${r.error ?? '校验不过'}）`)
                  .join('；')}
              </span>
            }
          />
        )}
        {deriveResult && (
          <div className="mt-3 rounded-xl bg-slate-50 border border-slate-100 px-4 py-3 space-y-1.5">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Tag color={deriveResult.status === 'preview' ? 'default' : deriveResult.status === 'unchanged' ? 'gold' : 'green'} className="!m-0">
                {deriveResult.status === 'preview' ? '预览（未落盘）' : deriveResult.status === 'unchanged' ? '内容未变' : '已保存'}
              </Tag>
              <span className="font-mono font-bold text-slate-700">{deriveResult.recipe_id}</span>
              <Tag className="!m-0 font-mono">{deriveResult.market}</Tag>
              <span className="text-slate-500">因子面 {deriveResult.factor_market} · {deriveResult.factor_source}</span>
              <span className="text-slate-500">特征 {deriveResult.feature_count} 维</span>
              <span className="text-slate-500">窗口 {windowPolicyText(deriveResult.window_policy)}</span>
            </div>
            <div className="text-[11px] text-slate-400 font-mono truncate">
              hash {deriveResult.recipe_hash}{deriveResult.path ? ` · ${deriveResult.path}` : ''}
            </div>
            {deriveResult.warnings.length > 0 && (
              <Alert
                type="warning"
                showIcon
                className="!py-1"
                message={<span className="text-xs">{deriveResult.warnings.join('；')}</span>}
              />
            )}
          </div>
        )}
      </Card>

      {/* ② 立即执行 */}
      <Card
        size="small"
        className="rounded-2xl"
        title={<span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><Rocket size={13} className="text-blue-500" />立即执行（手动派发一次）</span>}
      >
        <Space wrap className="mb-3">
          <Select
            size="small"
            className="min-w-[92px]"
            value={market}
            onChange={changeMarket}
            options={marketOptions.length ? marketOptions : [{ value: modelMarket, label: modelMarket }]}
          />
          <Select
            size="small"
            className="min-w-[320px]"
            placeholder="选择配方"
            value={dispatchRecipe || undefined}
            onChange={setDispatchRecipe}
            options={recipeOptions}
            notFoundContent={<span className="text-xs">该市场暂无有效配方（可先派生）</span>}
          />
          <DatePicker
            size="small"
            placeholder="锚定日（缺省=今天）"
            value={anchor}
            onChange={setAnchor}
            allowClear
          />
          <Button size="small" icon={<Play size={13} />} loading={dispatching === 'preview'} disabled={dispatching === 'run'} onClick={() => runDispatch(true)}>预览窗口计划</Button>
          <Button size="small" type="primary" danger icon={<Rocket size={13} />} loading={dispatching === 'run'} disabled={dispatching === 'preview'} onClick={confirmDispatch}>执行派发</Button>
        </Space>
        {dispatchResult && (
          <div className="rounded-xl bg-slate-50 border border-slate-100 px-4 py-3 space-y-1.5">
            <div className="flex items-center gap-2 text-xs">
              <Tag color={dispatchResult.status === 'dispatched' ? 'blue' : dispatchResult.status === 'dry_run' && dispatchResult.ready !== false ? 'green' : 'default'} className="!m-0">
                {DISPATCH_STATUS_TEXT[dispatchResult.status] ?? dispatchResult.status}
              </Tag>
              <span className="text-slate-600">{dispatchSummary(dispatchResult)}</span>
              {dispatchResult.campaign_id && <span className="font-mono text-slate-400 text-[11px]">campaign {dispatchResult.campaign_id}</span>}
            </div>
            {dispatchResult.plan && (
              <div className="text-[11px] text-slate-500 font-mono leading-relaxed">
                <div>锚定 {dispatchResult.plan.anchor_date} · 窗口内序号 {dispatchResult.plan.window_index} · purge {dispatchResult.plan.purge_days}</div>
                <div>训练 {dispatchResult.plan.train[0]} ~ {dispatchResult.plan.train[1]}</div>
                <div>验证 {dispatchResult.plan.valid[0]} ~ {dispatchResult.plan.valid[1]}</div>
                <div>测试 {dispatchResult.plan.test[0]} ~ {dispatchResult.plan.test[1]}</div>
              </div>
            )}
            {dispatchResult.detail && dispatchResult.status !== 'dispatched' && (
              <div className="text-[11px] text-slate-400 break-all">{JSON.stringify(dispatchResult.detail)}</div>
            )}
          </div>
        )}
      </Card>

      {/* ③ 月度调度 */}
      <Card
        size="small"
        className="rounded-2xl"
        title={<span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><CalendarClock size={13} className="text-emerald-500" />月度重训调度（{market}）</span>}
      >
        {draft ? (
          <div className="flex flex-wrap items-end gap-4">
            <div>
              <div className="text-[11px] text-slate-400 font-bold mb-1">启用</div>
              <Switch size="small" checked={draft.enabled} onChange={v => setDraft({ ...draft, enabled: v })} />
            </div>
            <div>
              <div className="text-[11px] text-slate-400 font-bold mb-1">触发规则</div>
              <Select
                size="small"
                className="min-w-[150px]"
                value={draft.day_rule}
                onChange={v => setDraft({ ...draft, day_rule: v })}
                options={[{ value: 'first_trading_day', label: '每月首个交易日' }]}
              />
            </div>
            <div>
              <div className="text-[11px] text-slate-400 font-bold mb-1">触发时间</div>
              <Input size="small" className="!w-20" value={draft.time} onChange={e => setDraft({ ...draft, time: e.target.value })} placeholder="15:30" />
            </div>
            <div>
              <div className="text-[11px] text-slate-400 font-bold mb-1">配方</div>
              <Select
                size="small"
                className="min-w-[300px]"
                placeholder="选择配方"
                value={draft.recipe_id || undefined}
                onChange={v => setDraft({ ...draft, recipe_id: v })}
                options={recipeOptions}
              />
            </div>
            <div>
              <div className="text-[11px] text-slate-400 font-bold mb-1">观察天数</div>
              <InputNumber size="small" className="!w-20" min={1} max={250} value={draft.observation_days} onChange={v => setDraft({ ...draft, observation_days: v ?? 20 })} />
            </div>
            <div>
              <div className="text-[11px] text-slate-400 font-bold mb-1">训练时限(分)</div>
              <InputNumber size="small" className="!w-24" min={10} max={1440} value={draft.max_time_minutes} onChange={v => setDraft({ ...draft, max_time_minutes: v ?? 240 })} />
            </div>
            <Button size="small" type="primary" className="bg-slate-900" icon={<Save size={13} />} loading={saving} onClick={saveSchedule}>保存调度</Button>
            <div className="text-[11px] text-slate-400 leading-relaxed basis-full">
              窗口策略（训练/验证/测试天数）归配方所有，在配方里改；此处保存即对该市场生效（调度总闸 RETRAIN_SCHEDULER_ENABLED 见部署配置）。
            </div>
          </div>
        ) : (
          <Empty description="暂无调度草稿" />
        )}
      </Card>

      {/* ④ 台账 */}
      <Card
        size="small"
        className="rounded-2xl"
        title={<span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><History size={13} className="text-amber-500" />滚动台账</span>}
        extra={
          <Space size={6}>
            <Select
              size="small"
              allowClear
              className="min-w-[100px]"
              placeholder="全部市场"
              value={campaignMarket}
              onChange={v => { setCampaignMarket(v); void loadCampaigns(v); }}
              options={marketOptions}
            />
            <Button size="small" icon={<RefreshCw size={13} />} loading={campaignsLoading} onClick={() => loadCampaigns(campaignMarket)}>刷新</Button>
          </Space>
        }
      >
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
      </Card>
    </div>
  );
};
