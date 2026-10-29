/**
 * 回测参数栏（策略编辑右侧 320px）。
 *
 * 关键改动：
 * - 按回测引擎分派字段：minibt 只显示 SYMBOL/START/END/SIZE/COMMISSION/fast/slow；
 *   STRATEGY_CONFIG 只显示 topk/n_drop/rebalance_days，避免把无关参数摆在用户面前；
 * - 参数「应用后才写回代码」，栏头明确显示已同步/未应用，并在无可写回字段时
 *   直接说明「应用不会改动代码」（旧实现会静默无操作）；
 * - 日期区间给预设，fast/slow 与日期做前置校验，主按钮只在可应用时点亮。
 */
import React, { useMemo, useState } from 'react';
import { Input, InputNumber, Select, Tooltip } from 'antd';
import {
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Coins,
  Landmark,
  RotateCcw,
  SlidersHorizontal,
  Timer,
} from 'lucide-react';
import {
  DEFAULT_STRATEGY_EDITOR_PARAMS,
  isMinibtStrategyCode,
  type StrategyEditorBacktestParams,
} from '../../../utils/strategyEditorParams';
import { validateBacktestParams } from './resultMetrics';
import type { ParamsState } from './types';

interface Props {
  params: ParamsState;
  code: string;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
}

const UNIVERSE_OPTIONS_CN = [
  { value: 'all', label: '全部 A 股' },
  { value: 'csi300', label: '沪深 300' },
  { value: 'csi500', label: '中证 500' },
  { value: 'csi800', label: '中证 800' },
  { value: 'csi1000', label: '中证 1000' },
];

const Group: React.FC<{ title: string; icon: React.ReactNode; hint?: string; children: React.ReactNode }> = ({
  title,
  icon,
  hint,
  children,
}) => (
  <section>
    <div className="mb-2 flex items-center gap-1.5">
      <span className="text-slate-300">{icon}</span>
      <span className="se-section-label">{title}</span>
      <span className="h-px flex-1 bg-slate-100" />
    </div>
    {hint && <div className="mb-2 text-[9.5px] leading-relaxed text-slate-400">{hint}</div>}
    <div className="space-y-2">{children}</div>
  </section>
);

const Field: React.FC<{ label: string; htmlFor?: string; children: React.ReactNode; hint?: string }> = ({
  label,
  htmlFor,
  children,
  hint,
}) => (
  <div className="flex items-center gap-2">
    <label htmlFor={htmlFor} className="w-[58px] shrink-0 cursor-default text-[10.5px] text-slate-500">
      {label}
    </label>
    <div className="min-w-0 flex-1">{children}</div>
    {hint && <span className="w-[46px] shrink-0 whitespace-nowrap text-right font-mono text-[9px] text-slate-300">{hint}</span>}
  </div>
);

const TwoUp: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="grid grid-cols-2 gap-2">{children}</div>
);

const toLocalIsoDate = (date: Date): string => {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};

const addYears = (iso: string, years: number): string => {
  if (!iso) return iso;
  const date = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(date.getTime())) return iso;
  date.setFullYear(date.getFullYear() + years);
  // 不能用 toISOString：东八区/东九区的本地零点会退到前一天
  return toLocalIsoDate(date);
};

type EngineMode = 'minibt' | 'config' | 'unknown';

const ENGINE_META: Record<EngineMode, { label: string; tone: string; hint: string }> = {
  minibt: {
    label: 'minibt',
    tone: 'border-emerald-100 bg-emerald-50 text-emerald-700',
    hint: 'minibt 运行时写回：SYMBOL / START / END / SIZE / COMMISSION / params.fast / params.slow',
  },
  config: {
    label: 'Qlib 配置',
    tone: 'border-blue-100 bg-blue-50 text-blue-700',
    hint: '写回 STRATEGY_CONFIG.topk / n_drop / rebalance_days；区间/资金/股票池随回测请求下发。',
  },
  unknown: {
    label: '未识别',
    tone: 'border-amber-100 bg-amber-50 text-amber-700',
    hint: '当前代码没有可识别的回测参数：点「应用」不会改动代码，参数也不会随请求下发。',
  },
};

export const BacktestSettingsPanel: React.FC<Props> = ({
  params,
  code,
  collapsed,
  onCollapsedChange,
}) => {
  const [edited, setEdited] = useState(false);
  const value = params.value;
  const mode: EngineMode = useMemo(() => {
    if (isMinibtStrategyCode(code)) return 'minibt';
    if (String(code || '').includes('STRATEGY_CONFIG')) return 'config';
    return 'unknown';
  }, [code]);

  const validation = validateBacktestParams({
    symbol: value.symbol,
    startDate: value.startDate,
    endDate: value.endDate,
    fast: value.fast,
    slow: value.slow,
    isMinibt: mode === 'minibt',
  });

  const patch = (next: Partial<StrategyEditorBacktestParams>) => {
    params.onChange(next);
    setEdited(true);
  };

  const apply = (run: boolean) => {
    setEdited(false);
    if (run) params.onApplyAndRun();
    else params.onApply();
  };

  if (collapsed) {
    return (
      <aside className="se-card flex w-[38px] shrink-0 flex-col items-center py-2">
        <button
          type="button"
          onClick={() => onCollapsedChange(false)}
          aria-label="展开回测设置"
          className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-50 hover:text-slate-700"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <div className="mt-3 flex flex-1 flex-col items-center gap-2">
          <SlidersHorizontal className="h-3.5 w-3.5 text-slate-300" />
          <span
            className="text-[10px] font-semibold tracking-[0.18em] text-slate-400"
            style={{ writingMode: 'vertical-rl' }}
          >
            回测设置
          </span>
        </div>
        <button
          type="button"
          onClick={() => onCollapsedChange(false)}
          aria-label="展开回测设置"
          className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-300 transition-colors hover:bg-slate-50 hover:text-slate-700"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </aside>
    );
  }

  return (
    <aside className="se-card se-params flex w-[318px] shrink-0 flex-col overflow-hidden">
      {/* 栏头 */}
      <div className="flex h-[46px] shrink-0 items-center gap-2 border-b border-slate-100 px-3">
        <SlidersHorizontal className="h-3.5 w-3.5 shrink-0 text-slate-400" />
        <span className="shrink-0 whitespace-nowrap text-[12px] font-bold tracking-tight text-slate-800">回测设置</span>
        <span className={`shrink-0 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[9px] font-semibold ${ENGINE_META[mode].tone}`}>
          {ENGINE_META[mode].label}
        </span>
        <span className="flex-1" />
        <span
          className={`flex shrink-0 items-center gap-1 whitespace-nowrap text-[9.5px] font-medium ${
            edited ? 'text-amber-600' : 'text-slate-300'
          }`}
          title={edited ? '参数改了还没写回代码' : '参数没有待应用的修改'}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${edited ? 'bg-amber-400' : 'bg-emerald-400'}`}
          />
          {edited ? '待应用' : '无改动'}
        </span>
        <Tooltip title="恢复默认参数">
          <button
            type="button"
            onClick={() => {
              params.onChange({ ...DEFAULT_STRATEGY_EDITOR_PARAMS });
              setEdited(true);
            }}
            aria-label="恢复默认参数"
            className="flex h-6 w-6 items-center justify-center rounded-md text-slate-300 transition-colors hover:bg-slate-50 hover:text-slate-600"
          >
            <RotateCcw className="h-3 w-3" />
          </button>
        </Tooltip>
        <Tooltip title="收起参数栏">
          <button
            type="button"
            onClick={() => onCollapsedChange(true)}
            aria-label="收起参数栏"
            className="flex h-6 w-6 items-center justify-center rounded-md text-slate-300 transition-colors hover:bg-slate-50 hover:text-slate-600"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </Tooltip>
      </div>

      {/* 参数体 */}
      <div className="se-scroll flex-1 space-y-4 overflow-y-auto px-3 py-3">
        <Group title="标的与区间" icon={<Landmark className="h-3 w-3" />}>
          {mode === 'minibt' ? (
            <Field label="标的代码" htmlFor="se-param-symbol">
              <Input
                id="se-param-symbol"
                size="small"
                value={value.symbol}
                placeholder="600036"
                onChange={(event) => patch({ symbol: event.target.value.trim() })}
                status={validation.symbolMissing ? 'error' : undefined}
              />
            </Field>
          ) : (
            <div className="rounded-lg border border-slate-100 bg-slate-50/60 px-2.5 py-2 text-[9.5px] leading-relaxed text-slate-400">
              {mode === 'config'
                ? '截面策略：标的由「股票池 + topk」在回测日选出，单标的代码不参与本次回测。'
                : '当前代码未识别回测引擎：单标的代码不会写回代码，也不随请求下发。'}
            </div>
          )}
          <Field label="股票池" htmlFor="se-param-universe">
            <Select
              id="se-param-universe"
              size="small"
              value={value.universe}
              onChange={(next) => patch({ universe: next })}
              options={params.market === 'CN' ? UNIVERSE_OPTIONS_CN : [{ value: 'all', label: '全部标的' }]}
            />
          </Field>
          <div>
            <div className="mb-1 text-[10.5px] text-slate-500">回测区间</div>
            <div className="grid grid-cols-2 gap-1.5">
              <Input
                size="small"
                type="date"
                aria-label="开始日期"
                value={value.startDate}
                onChange={(event) => patch({ startDate: event.target.value })}
                status={validation.dateInvalid ? 'error' : undefined}
              />
              <Input
                size="small"
                type="date"
                aria-label="结束日期"
                value={value.endDate}
                onChange={(event) => patch({ endDate: event.target.value })}
                status={validation.dateInvalid ? 'error' : undefined}
              />
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              {[
                { label: '近 1 年', run: () => patch({ startDate: addYears(value.endDate, -1) }) },
                { label: '近 3 年', run: () => patch({ startDate: addYears(value.endDate, -3) }) },
                { label: '今年', run: () => patch({ startDate: `${(value.endDate || '2026').slice(0, 4)}-01-01` }) },
                { label: '2016 起', run: () => patch({ startDate: '2016-01-01' }) },
              ].map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  onClick={preset.run}
                  className="rounded-md border border-slate-200/80 bg-white px-1.5 py-0.5 text-[9px] text-slate-500 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-600"
                >
                  {preset.label}
                </button>
              ))}
            </div>
          </div>
        </Group>

        {mode !== 'minibt' && (
          <Group title="组合构建" icon={<SlidersHorizontal className="h-3 w-3" />} hint="topk / n_drop / 调仓周期写回 STRATEGY_CONFIG。">
            <TwoUp>
              <Field label="topk" htmlFor="se-param-topk">
                <InputNumber
                  id="se-param-topk"
                  size="small"
                  min={1}
                  max={200}
                  controls={false}
                  value={value.topk}
                  onChange={(next) => patch({ topk: Number(next ?? 1) })}
                />
              </Field>
              <Field label="n_drop" htmlFor="se-param-ndrop">
                <InputNumber
                  id="se-param-ndrop"
                  size="small"
                  min={0}
                  max={200}
                  controls={false}
                  value={value.nDrop}
                  onChange={(next) => patch({ nDrop: Number(next ?? 0) })}
                />
              </Field>
            </TwoUp>
            <Field label="调仓周期" htmlFor="se-param-rebalance" hint="交易日">
              <InputNumber
                id="se-param-rebalance"
                size="small"
                min={1}
                max={60}
                controls={false}
                value={value.rebalanceDays}
                onChange={(next) => patch({ rebalanceDays: Number(next ?? 1) })}
              />
            </Field>
          </Group>
        )}

        {mode === 'minibt' && (
          <Group title="均线信号" icon={<Timer className="h-3 w-3" />} hint="快慢均线写回 params.fast / params.slow。">
            <TwoUp>
              <Field label="快线 fast" htmlFor="se-param-fast">
                <InputNumber
                  id="se-param-fast"
                  size="small"
                  min={2}
                  max={120}
                  controls={false}
                  value={value.fast}
                  onChange={(next) => patch({ fast: Number(next ?? 2) })}
                  status={validation.fastSlowInvalid ? 'error' : undefined}
                />
              </Field>
              <Field label="慢线 slow" htmlFor="se-param-slow">
                <InputNumber
                  id="se-param-slow"
                  size="small"
                  min={3}
                  max={250}
                  controls={false}
                  value={value.slow}
                  onChange={(next) => patch({ slow: Number(next ?? 3) })}
                  status={validation.fastSlowInvalid ? 'error' : undefined}
                />
              </Field>
            </TwoUp>
            <Field label="每次股数" htmlFor="se-param-size" hint="股">
              <InputNumber
                id="se-param-size"
                size="small"
                min={100}
                step={100}
                controls={false}
                value={value.size}
                onChange={(next) => patch({ size: Number(next ?? 100) })}
              />
            </Field>
          </Group>
        )}

        <Group title="资金与成本" icon={<Coins className="h-3 w-3" />}>
          <Field label="初始资金" htmlFor="se-param-capital" hint="元">
            <InputNumber
              id="se-param-capital"
              size="small"
              min={1000}
              step={100000}
              controls={false}
              value={value.initialCapital}
              onChange={(next) => patch({ initialCapital: Number(next ?? 0) })}
              formatter={(raw) => `${raw ?? ''}`.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}
              parser={(text) => Number(String(text ?? '').replace(/,/g, ''))}
            />
          </Field>
          <Field label="佣金率" htmlFor="se-param-commission" hint={`万 ${(Number(value.commission) * 10000).toFixed(2)}`}>
            <InputNumber
              id="se-param-commission"
              size="small"
              min={0}
              max={0.003}
              step={0.00005}
              controls={false}
              value={value.commission}
              onChange={(next) => patch({ commission: Number(next ?? 0) })}
            />
          </Field>
        </Group>

        {validation.messages.length > 0 && (
          <div className="rounded-lg border border-amber-200/70 bg-amber-50/70 px-2.5 py-2">
            {validation.messages.map((message) => (
              <div key={message} className="flex items-start gap-1.5 text-[10px] leading-relaxed text-amber-700">
                <CircleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                <span>{message}</span>
              </div>
            ))}
          </div>
        )}

        <div className="rounded-lg border border-slate-100 bg-slate-50/60 px-2.5 py-2 text-[9.5px] leading-relaxed text-slate-400">
          {ENGINE_META[mode].hint}
        </div>
      </div>

      {/* 底部操作 */}
      <div className="shrink-0 border-t border-slate-100 bg-white px-3 py-2.5">
        <div className="flex gap-1.5">
          <button
            type="button"
            disabled={!validation.canApply}
            onClick={() => apply(false)}
            className="h-8 flex-1 rounded-lg border border-slate-200 bg-white text-[11px] font-semibold text-slate-600 transition-colors hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-45"
          >
            应用参数
          </button>
          <button
            type="button"
            disabled={!validation.canApply}
            onClick={() => apply(true)}
            className="h-8 flex-[1.35] rounded-lg bg-blue-600 text-[11px] font-semibold text-white shadow-sm transition-all hover:bg-blue-700 active:scale-[0.99] disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            应用并运行
          </button>
        </div>
        <div className="mt-1.5 text-center text-[9px] text-slate-300">
          快捷键 Ctrl/⌘+S 保存 · F5 运行
        </div>
      </div>
    </aside>
  );
};

export default BacktestSettingsPanel;
