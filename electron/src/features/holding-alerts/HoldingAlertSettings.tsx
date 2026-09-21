/**
 * 个人中心 · 持仓预警设置（两列对称：左「持仓监控与提醒」/ 右「提醒通道」）
 *
 * 绑定 `GET|PUT /api/v1/trading/holding-alerts/config`（Redis `qm:holding:alert:config:{tenant}:{user}`）。
 * 哨兵 60s 一轮读同一份配置，所以这里一改下一轮就生效，不需要重启任何服务。
 *
 * 开关是**立即保存**（部分更新，后端与已存值合并后整体校验）：漏一次保存 = 用户以为
 * 自己关了提醒。阈值是数值输入，改成「输入框 + 保存」避免每敲一个字符打一次接口。
 *
 * 拆两列是因为这两组设置回答的是不同问题——「什么值得盯」（监控）与「怎么通知我」
 * （通道）。此前竖排堆在一张卡里，用户要一路读到底才知道自己到底会被怎么打扰。
 * 两列各自以一条**状态带**开头（左=哨兵是否在跑，右=通道实际会响几条），
 * 一眼看到的是结论，而不是又一组开关。
 *
 * 另有两处如实呈现：哨兵是否在跑、浏览器桌面通知权限是否被拒——
 * 开关开着但权限被拒时不说清楚，用户会以为提醒坏了。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Bell, BellRing, Monitor, Volume2, ShieldAlert, Save, SlidersHorizontal } from 'lucide-react';
import { InputNumber, Select, Switch, message } from 'antd';
import {
  DEFAULT_ALERT_CONFIG,
  holdingAlertService,
  type HoldingAlertConfig,
  type HoldingSentinelStatus,
} from '../../services/holdingAlertService';
import { previewAlertSound } from '../../services/alertDelivery';
import { channelSummary, sentinelHeadline } from './alertModel';

const TONE_CLS = {
  rose: 'bg-rose-50 text-rose-600',
  sky: 'bg-sky-50 text-sky-600',
} as const;

const CARD_CLS = 'bg-white rounded-2xl border border-gray-200 p-4 shadow-sm flex flex-col';

/** 与 PersonalCenter 的四张卡同款卡壳（那套 CardHead 是页面私有，不跨文件导出） */
function CardHead({
  icon,
  title,
  tone,
}: {
  icon: React.ReactNode;
  title: string;
  tone: keyof typeof TONE_CLS;
}): React.ReactElement {
  return (
    <header className="flex items-center gap-2 mb-3">
      <span className={`flex h-6 w-6 items-center justify-center rounded-lg shrink-0 ${TONE_CLS[tone]}`}>
        {icon}
      </span>
      <h4 className="text-[13px] font-bold text-gray-800">{title}</h4>
    </header>
  );
}

/** 两列共用的状态带：结论先行，两列因此有同一条视觉起跑线 */
function StatusStrip({ text, warn }: { text: string; warn: boolean }): React.ReactElement {
  return (
    <div
      className={`mb-3 rounded-lg border px-2 py-1.5 text-[11px] ${
        warn ? 'border-amber-200 bg-amber-50 text-amber-700' : 'border-emerald-100 bg-emerald-50 text-emerald-700'
      }`}
    >
      {text}
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }): React.ReactElement {
  return <div className="mt-3 text-[11px] font-bold text-gray-600">{children}</div>;
}

function ToggleRow({
  icon,
  label,
  hint,
  checked,
  disabled,
  onChange,
}: {
  icon: React.ReactNode;
  label: string;
  hint?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}): React.ReactElement {
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg bg-gray-50 p-1.5">
      <div className="flex min-w-0 items-center gap-2">
        {icon}
        <span className="truncate text-[13px] text-gray-700">{label}</span>
        {hint && <span className="shrink-0 text-[10px] text-gray-400">{hint}</span>}
      </div>
      {/* aria-label 不是装饰：这一排开关没有它就没有可访问名，读屏软件只会念「开关」 */}
      <Switch size="small" aria-label={label} checked={checked} disabled={disabled} onChange={onChange} />
    </div>
  );
}

type DesktopPermission = 'granted' | 'denied' | 'default' | 'unsupported';

function desktopPermission(): DesktopPermission {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported';
  // Electron 里是主进程原生通知，不存在浏览器权限概念
  if ((window as { process?: { type?: string } }).process?.type) return 'granted';
  return Notification.permission as DesktopPermission;
}

interface ColumnProps {
  config: HoldingAlertConfig;
  /** 整卡禁用（加载中/保存中）或总开关关闭——两者对用户是同一件事：现在改不了 */
  disabled: boolean;
  patch: (partial: Partial<HoldingAlertConfig>) => void;
}

/** 左列：什么值得盯（总开关 / 监控范围 / 分数阈值） */
const MonitorColumn: React.FC<
  ColumnProps & {
    headline: { text: string; warn: boolean };
    thresholdDraft: number;
    onThresholdDraft: (value: number) => void;
    onSaveThreshold: () => void;
  }
> = ({ config, disabled, patch, headline, thresholdDraft, onThresholdDraft, onSaveThreshold }) => (
  <section data-testid="holding-monitor-card" className={CARD_CLS}>
    <CardHead icon={<ShieldAlert size={14} />} title="持仓监控与提醒" tone="rose" />

    <StatusStrip text={headline.text} warn={headline.warn} />

    <ToggleRow
      icon={<ShieldAlert size={13} className="text-rose-500" />}
      label="持仓监控总开关"
      checked={config.enabled}
      disabled={disabled}
      onChange={(next) => patch({ enabled: next })}
    />

    <SectionLabel>监控范围</SectionLabel>
    <div className="mt-1 space-y-1">
      <ToggleRow
        icon={<Bell size={13} className="text-slate-400" />}
        label="模拟盘持仓"
        checked={config.watch_sim}
        disabled={disabled || !config.enabled}
        onChange={(next) => patch({ watch_sim: next })}
      />
      <ToggleRow
        icon={<Bell size={13} className="text-slate-400" />}
        label="实盘持仓"
        checked={config.watch_real}
        disabled={disabled || !config.enabled}
        onChange={(next) => patch({ watch_real: next })}
      />
      <ToggleRow
        icon={<Bell size={13} className="text-slate-400" />}
        label="手工自选"
        checked={config.watch_manual}
        disabled={disabled || !config.enabled}
        onChange={(next) => patch({ watch_manual: next })}
      />
    </div>

    {/* mt-auto：把阈值块压到卡底，与右列的试听行齐平——两列的底边因此对齐 */}
    <div className="mt-auto pt-3">
      <div className="text-[11px] font-bold text-gray-600">分数阈值</div>
      <div className="mt-1 flex items-center gap-2">
        <InputNumber
          value={thresholdDraft}
          step={0.05}
          disabled={disabled || !config.enabled}
          onChange={(v) => onThresholdDraft(Number(v ?? 0))}
          className="w-28"
          size="small"
        />
        <button
          type="button"
          onClick={onSaveThreshold}
          disabled={disabled || !config.enabled}
          className="flex items-center gap-1 rounded-lg bg-blue-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-blue-700 disabled:bg-gray-300"
        >
          <Save size={12} />
          保存
        </button>
      </div>
      <p className="mt-1 text-[10px] leading-relaxed text-gray-400">
        分数下穿 0 始终提醒；此处是额外的预警线，填 0 = 只报下穿 0
      </p>
    </div>
  </section>
);

/** 右列：怎么通知我（三条通道 + 最低级别 + 试听） */
const ChannelColumn: React.FC<
  ColumnProps & {
    perm: DesktopPermission;
    summary: { text: string; warn: boolean };
    onPreview: () => void;
  }
> = ({ config, disabled, patch, perm, summary, onPreview }) => (
  <section data-testid="holding-alert-channels" className={CARD_CLS}>
    <CardHead icon={<BellRing size={14} />} title="提醒通道" tone="sky" />

    <StatusStrip text={summary.text} warn={summary.warn} />

    <div className="space-y-1">
      <ToggleRow
        icon={<BellRing size={13} className="text-slate-400" />}
        label="站内面板"
        checked={config.notify_inapp}
        disabled={disabled || !config.enabled}
        onChange={(next) => patch({ notify_inapp: next })}
      />
      <ToggleRow
        icon={<Monitor size={13} className="text-slate-400" />}
        label="桌面系统通知"
        hint={perm === 'denied' ? '浏览器已拒绝，需在站点设置里放开' : perm === 'unsupported' ? '当前环境不支持' : undefined}
        checked={config.notify_desktop}
        disabled={disabled || !config.enabled}
        onChange={(next) => patch({ notify_desktop: next })}
      />
      <ToggleRow
        icon={<Volume2 size={13} className="text-slate-400" />}
        label="声音提示"
        checked={config.notify_sound}
        disabled={disabled || !config.enabled}
        onChange={(next) => patch({ notify_sound: next })}
      />
      <div className="flex items-center justify-between gap-2 rounded-lg bg-gray-50 p-1.5">
        <div className="flex items-center gap-2">
          {/* 别和上一行的「声音提示」同图标：相邻两行同字形，扫读时就分不出谁是谁 */}
          <SlidersHorizontal size={13} className="text-slate-400" />
          <span className="text-[13px] text-gray-700">最低提醒级别</span>
        </div>
        <Select
          size="small"
          aria-label="最低提醒级别"
          value={config.min_severity}
          disabled={disabled || !config.enabled}
          style={{ width: 96 }}
          onChange={(v) => patch({ min_severity: v })}
          options={[
            { value: 'info', label: '提示' },
            { value: 'warning', label: '警告' },
            { value: 'critical', label: '危急' },
          ]}
        />
      </div>
    </div>

    <div className="mt-auto flex items-center gap-2 pt-3">
      <button
        type="button"
        onClick={onPreview}
        className="rounded-lg border border-gray-200 px-2.5 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
      >
        试听提示音
      </button>
      <span className="text-[10px] leading-relaxed text-gray-400">
        试听同时会解锁浏览器音频（不点一次，之后的提示音可能不响）
      </span>
    </div>
  </section>
);

export const HoldingAlertSettings: React.FC = () => {
  const [config, setConfig] = useState<HoldingAlertConfig>(DEFAULT_ALERT_CONFIG);
  const [sentinel, setSentinel] = useState<HoldingSentinelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [thresholdDraft, setThresholdDraft] = useState<number>(0);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      const [cfg, status] = await Promise.all([
        holdingAlertService.getConfig(),
        holdingAlertService.getSentinelStatus().catch(() => null),
      ]);
      setConfig(cfg);
      setThresholdDraft(cfg.score_threshold);
      setSentinel(status);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** 乐观更新 + 失败回滚：开关没落库必须让用户看见 */
  const patch = useCallback(async (partial: Partial<HoldingAlertConfig>) => {
    const before = config;
    setConfig({ ...config, ...partial });
    setSaving(true);
    try {
      const merged = await holdingAlertService.updateConfig(partial);
      setConfig(merged);
      setThresholdDraft(merged.score_threshold);
      message.success('预警设置已更新');
    } catch (e) {
      setConfig(before);
      message.error(e instanceof Error ? e.message : '预警设置保存失败');
    } finally {
      setSaving(false);
    }
  }, [config]);

  const saveThreshold = useCallback(async () => {
    const value = Number(thresholdDraft);
    if (!Number.isFinite(value)) {
      message.warning('阈值必须是数字');
      return;
    }
    await patch({ score_threshold: value });
  }, [thresholdDraft, patch]);

  const headline = sentinelHeadline(sentinel);
  const perm = desktopPermission();
  const disabled = loading || saving;

  const summary = channelSummary(config, perm === 'denied');

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-stretch">
      {/* 错误是整份配置的（加载/保存失败），不属于任何一列，故横跨两列 */}
      {error && (
        <div className="lg:col-span-2 rounded-lg border border-red-100 bg-red-50 px-3 py-1.5 text-[11px] text-red-600">
          {error}
        </div>
      )}

      <MonitorColumn
        config={config}
        disabled={disabled}
        patch={(p) => void patch(p)}
        headline={headline}
        thresholdDraft={thresholdDraft}
        onThresholdDraft={setThresholdDraft}
        onSaveThreshold={() => void saveThreshold()}
      />

      <ChannelColumn
        config={config}
        disabled={disabled}
        patch={(p) => void patch(p)}
        perm={perm}
        summary={summary}
        onPreview={() => { previewAlertSound('critical'); }}
      />
    </div>
  );
};

export default HoldingAlertSettings;
