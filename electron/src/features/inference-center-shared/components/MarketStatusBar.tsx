/**
 * 顶栏状态带：市场 · 数据基准日与滞后 · 交易日 · 模型资产数 · 截面新鲜度。
 *
 * 机构环境里最贵的错误是「拿旧数据当真」——研判结论必须能一眼看到它是基于哪一天、
 * 滞后几个交易日的数据得出的。因此把数据新鲜度提到顶栏常驻，而不是藏在预检弹窗里。
 */

import React from 'react';
import { clsx } from 'clsx';
import { Tooltip } from 'antd';
import { AlertTriangle, CalendarCheck, CheckCircle2, Clock3, Layers } from 'lucide-react';

/** 数据滞后分级：超过该交易日数判为「偏旧」，超过「陈旧」阈值判为陈旧 */
const STALE_WARN_DAYS = 3;
const STALE_BAD_DAYS = 7;

interface StatusCellProps {
  icon: React.ComponentType<{ size?: number | string; className?: string }>;
  label: string;
  value: string;
  tone?: 'neutral' | 'good' | 'warn' | 'bad';
  title?: string;
}

const TONE_CLASS: Record<NonNullable<StatusCellProps['tone']>, string> = {
  neutral: 'text-slate-700',
  good: 'text-emerald-700',
  warn: 'text-amber-700',
  bad: 'text-rose-700',
};

const StatusCell: React.FC<StatusCellProps> = ({ icon: Icon, label, value, tone = 'neutral', title }) => (
  <Tooltip title={title}>
    <span className="flex items-center gap-1.5 whitespace-nowrap cursor-default">
      <Icon size={12} className="text-slate-400 shrink-0" />
      <span className="text-[10px] text-slate-400 font-semibold">{label}</span>
      <span className={clsx('text-[11px] font-mono font-black', TONE_CLASS[tone])}>{value}</span>
    </span>
  </Tooltip>
);

/** 自然日差（按 UTC 零点算，不做交易日剔除——仅用于「看起来多久没更新」的粗判） */
function daysBetween(fromIso: string, toIso: string): number | null {
  const a = Date.parse(`${fromIso}T00:00:00Z`);
  const b = Date.parse(`${toIso}T00:00:00Z`);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.round((b - a) / 86_400_000);
}

interface MarketStatusBarProps {
  marketLabel: string;
  calendar: string;
  /** 截面预检给出的数据基准日（YYYY-MM-DD） */
  dataTradeDate?: string | null;
  /** 预检预测交易日 */
  predictionTradeDate?: string | null;
  /** 预检是否通过（false = 当前模型在该市场不可执行） */
  precheckPassed?: boolean | null;
  precheckError?: string | null;
  /** 已注册模型资产数 */
  modelCount?: number;
  /** 当前截面榜行数（有榜时显示） */
  rankingCount?: number;
  /** 榜单是否为回退批次（非最近一批） */
  rankingFallbackFrom?: string | null;
}

export const MarketStatusBar: React.FC<MarketStatusBarProps> = ({
  marketLabel,
  calendar,
  dataTradeDate,
  predictionTradeDate,
  precheckPassed,
  precheckError,
  modelCount,
  rankingCount,
  rankingFallbackFrom,
}) => {
  const today = new Date().toISOString().slice(0, 10);
  const lag = dataTradeDate ? daysBetween(dataTradeDate, today) : null;

  const freshness: { value: string; tone: StatusCellProps['tone']; title: string } = (() => {
    if (!dataTradeDate) {
      return {
        value: '未知',
        tone: 'neutral',
        title: '尚未取到该市场的数据基准日；请选择模型后查看预检结果',
      };
    }
    if (lag == null) {
      return { value: dataTradeDate, tone: 'neutral', title: '数据基准日' };
    }
    if (lag > STALE_BAD_DAYS) {
      return {
        value: `${dataTradeDate} · 滞后${lag}天`,
        tone: 'bad',
        title: `行情/因子数据距今日已 ${lag} 个自然日，结论可能严重失真，请先同步数据`,
      };
    }
    if (lag > STALE_WARN_DAYS) {
      return {
        value: `${dataTradeDate} · 滞后${lag}天`,
        tone: 'warn',
        title: `行情/因子数据距今日 ${lag} 个自然日，建议核对数据同步链路`,
      };
    }
    return { value: dataTradeDate, tone: 'good', title: '数据基准日（新鲜）' };
  })();

  return (
    <div className="flex items-center gap-3 min-w-0 overflow-hidden">
      <span className="flex items-center gap-1.5 whitespace-nowrap">
        <Layers size={12} className="text-blue-500 shrink-0" />
        <span className="text-[11px] font-black text-slate-700">{marketLabel}</span>
        <span className="text-[10px] font-mono text-slate-400">{calendar}</span>
      </span>

      <span className="w-px h-4 bg-slate-200 shrink-0" />

      <StatusCell
        icon={Clock3}
        label="数据基准日"
        value={freshness.value}
        tone={freshness.tone}
        title={freshness.title}
      />

      {predictionTradeDate && (
        <StatusCell
          icon={CalendarCheck}
          label="预测交易日"
          value={predictionTradeDate}
          title="模型信号对应的目标交易日（T+N 的落点）"
        />
      )}

      {typeof modelCount === 'number' && (
        <StatusCell
          icon={CheckCircle2}
          label="模型资产"
          value={`${modelCount}`}
          tone={modelCount > 0 ? 'neutral' : 'bad'}
          title="该市场已注册且未归档的模型数"
        />
      )}

      {typeof rankingCount === 'number' && (
        <StatusCell
          icon={AlertTriangle}
          label="截面榜"
          value={`${rankingCount} 行`}
          tone={rankingCount > 0 ? 'neutral' : 'warn'}
          title="当前生效批次的排名明细行数"
        />
      )}

      {precheckPassed === false && (
        <span
          className="flex items-center gap-1 whitespace-nowrap text-[10px] font-bold text-rose-600 bg-rose-50 border border-rose-200 rounded px-1.5 py-0.5"
          title={precheckError || '当前模型在该市场未通过推理前置检查'}
        >
          <AlertTriangle size={10} />
          预检未通过
        </span>
      )}

      {rankingFallbackFrom && (
        <span
          className="flex items-center gap-1 whitespace-nowrap text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5"
          title={`最近一批的排名明细未落库，当前展示的是更早批次 ${rankingFallbackFrom}`}
        >
          <AlertTriangle size={10} />
          非最新批次
        </span>
      )}
    </div>
  );
};
