/**
 * 策略编辑（回测中心）右栏：参数调整 + 专业回测图表 + 本地存档 + QuantBot 跳转。
 *
 * 数据来源：
 * - minibt 结果：AI-IDE `/execute/result/{job_id}` → StrategyLabRunResult；
 * - qlib 模块回测结果：`[RESULT] backtest_id` → backtestService.getResult。
 * 两套结果分别复用已有的专业图表组件，避免在 500px 右栏里重画一套。
 */
import React, { useMemo, useState } from 'react';
import { Button, Empty, Input, InputNumber, Select, Tag, Tooltip, Typography } from 'antd';
import {
  Activity,
  Bot,
  ChevronDown,
  ChevronUp,
  History,
  Play,
  Save,
  SlidersHorizontal,
  Trash2,
} from 'lucide-react';
import type { StrategyLabRunResult } from '../../features/strategy-lab/types';
import { StrategyLabResultPanel } from '../../features/strategy-lab/components/StrategyLabResultPanel';
import type { BacktestResult } from '../../services/backtestService';
import type { QlibBacktestResult } from '../../types/backtest/qlib';
import { QlibResultDisplay } from '../backtest/QlibResultComponents';
import type { SavedBacktestRun } from '../../utils/strategyEditorSavedRuns';
import type { StrategyEditorBacktestParams } from '../../utils/strategyEditorParams';

const { Text } = Typography;

interface Props {
  params: StrategyEditorBacktestParams;
  onParamsChange: (patch: Partial<StrategyEditorBacktestParams>) => void;
  onApplyParams: () => void;
  onRun: () => void;
  onApplyAndRun: () => void;
  running: boolean;
  minibtResult: StrategyLabRunResult | null;
  qlibResult: BacktestResult | QlibBacktestResult | null;
  code: string;
  strategyName?: string | null;
  savedRuns: SavedBacktestRun[];
  onSaveResult: () => void;
  onLoadSaved: (run: SavedBacktestRun) => void;
  onDeleteSaved: (id: string) => void;
  onOpenQuantBot: () => void;
}

const SectionTitle: React.FC<{ icon: React.ReactNode; children: React.ReactNode; extra?: React.ReactNode }> = ({
  icon,
  children,
  extra,
}) => (
  <div className="flex items-center justify-between gap-2">
    <div className="flex items-center gap-1.5 text-[11px] font-bold text-gray-700 tracking-wide">
      {icon}
      {children}
    </div>
    {extra}
  </div>
);

const Field: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <label className="flex flex-col gap-1 text-[10px] text-gray-500">
    <span>{label}</span>
    {children}
  </label>
);

const StrategyEditorBacktestPanel: React.FC<Props> = ({
  params,
  onParamsChange,
  onApplyParams,
  onRun,
  onApplyAndRun,
  running,
  minibtResult,
  qlibResult,
  code,
  strategyName,
  savedRuns,
  onSaveResult,
  onLoadSaved,
  onDeleteSaved,
  onOpenQuantBot,
}) => {
  const [paramsOpen, setParamsOpen] = useState(true);
  const hasResult = Boolean(minibtResult || qlibResult);
  const isMinibt = useMemo(
    () => /^\s*(?:import|from)\s+minibt\b/m.test(code || ''),
    [code],
  );

  return (
    <div className="h-full flex flex-col bg-white rounded-[24px] border border-gray-200 shadow-sm overflow-hidden">
      <header className="px-4 py-3 border-b border-gray-100 bg-gradient-to-r from-slate-900 to-slate-800 text-white shrink-0">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-emerald-300" />
            <div className="leading-tight">
              <div className="text-sm font-bold tracking-tight">回测工作台</div>
              <div className="text-[10px] text-slate-300">参数 · 图表 · 存档</div>
            </div>
          </div>
          <Tooltip title="AI 写策略请到 QuantBot（策略编辑只做回测）">
            <Button
              size="small"
              type="primary"
              icon={<Bot className="h-3.5 w-3.5" />}
              onClick={onOpenQuantBot}
              className="!bg-emerald-500 !border-emerald-500 hover:!bg-emerald-400"
            >
              AI 写策略
            </Button>
          </Tooltip>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto custom-scrollbar">
        <section className="p-3 border-b border-gray-100">
          <SectionTitle
            icon={<SlidersHorizontal className="h-3.5 w-3.5 text-blue-600" />}
            extra={
              <button
                type="button"
                onClick={() => setParamsOpen((v) => !v)}
                className="text-[10px] text-gray-400 hover:text-gray-600 flex items-center gap-0.5"
              >
                {paramsOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                {paramsOpen ? '收起' : '展开'}
              </button>
            }
          >
            参数调整
          </SectionTitle>

          {paramsOpen && (
            <>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <Field label="标的代码">
                  <Input
                    size="small"
                    value={params.symbol}
                    onChange={(e) => onParamsChange({ symbol: e.target.value })}
                    placeholder="600036"
                  />
                </Field>
                <Field label="股票池">
                  <Select
                    size="small"
                    value={params.universe}
                    onChange={(value) => onParamsChange({ universe: value })}
                    options={[
                      { value: 'all', label: '全部' },
                      { value: 'csi300', label: '沪深300' },
                      { value: 'csi500', label: '中证500' },
                      { value: 'csi800', label: '中证800' },
                      { value: 'csi1000', label: '中证1000' },
                    ]}
                  />
                </Field>
                <Field label="开始日期">
                  <Input
                    size="small"
                    type="date"
                    value={params.startDate}
                    onChange={(e) => onParamsChange({ startDate: e.target.value })}
                  />
                </Field>
                <Field label="结束日期">
                  <Input
                    size="small"
                    type="date"
                    value={params.endDate}
                    onChange={(e) => onParamsChange({ endDate: e.target.value })}
                  />
                </Field>
                <Field label="初始资金">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={1000}
                    step={100000}
                    value={params.initialCapital}
                    onChange={(value) =>
                      onParamsChange({ initialCapital: Number(value ?? 0) })
                    }
                  />
                </Field>
                <Field label="佣金率">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={0}
                    max={0.003}
                    step={0.00001}
                    value={params.commission}
                    onChange={(value) => onParamsChange({ commission: Number(value ?? 0) })}
                  />
                </Field>
                <Field label="topk">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={1}
                    max={200}
                    value={params.topk}
                    onChange={(value) => onParamsChange({ topk: Number(value ?? 1) })}
                  />
                </Field>
                <Field label="n_drop">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={0}
                    max={200}
                    value={params.nDrop}
                    onChange={(value) => onParamsChange({ nDrop: Number(value ?? 0) })}
                  />
                </Field>
                <Field label="调仓周期（回测）">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={1}
                    max={60}
                    value={params.rebalanceDays}
                    onChange={(value) => onParamsChange({ rebalanceDays: Number(value ?? 1) })}
                  />
                </Field>
                <Field label="每次股数 SIZE">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={100}
                    step={100}
                    value={params.size}
                    onChange={(value) => onParamsChange({ size: Number(value ?? 100) })}
                  />
                </Field>
                <Field label="快线 fast">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={2}
                    max={60}
                    value={params.fast}
                    onChange={(value) => onParamsChange({ fast: Number(value ?? 2) })}
                  />
                </Field>
                <Field label="慢线 slow">
                  <InputNumber
                    size="small"
                    className="w-full"
                    min={5}
                    max={250}
                    value={params.slow}
                    onChange={(value) => onParamsChange({ slow: Number(value ?? 5) })}
                  />
                </Field>
              </div>
              <div className="mt-1 text-[10px] text-gray-400 leading-relaxed">
                {isMinibt
                  ? 'minibt：SYMBOL/START/END/SIZE/COMMISSION/fast/slow 会写回代码；日期按 YYYYMMDD。'
                  : 'STRATEGY_CONFIG：topk/n_drop/rebalance_days 会写回代码；日期/资金/股票池随回测请求。'}
              </div>
              <div className="mt-2 flex gap-2">
                <Button size="small" onClick={onApplyParams} disabled={running}>
                  仅应用参数
                </Button>
                <Button
                  size="small"
                  type="primary"
                  icon={<Play className="h-3 w-3" />}
                  loading={running}
                  onClick={onApplyAndRun}
                >
                  应用并运行
                </Button>
                <Button size="small" onClick={onRun} disabled={running}>
                  直接运行
                </Button>
              </div>
            </>
          )}
        </section>

        <section className="p-3 border-b border-gray-100">
          <SectionTitle
            icon={<Activity className="h-3.5 w-3.5 text-emerald-600" />}
            extra={
              <Button
                size="small"
                icon={<Save className="h-3.5 w-3.5" />}
                onClick={onSaveResult}
                disabled={!hasResult}
              >
                保存本次
              </Button>
            }
          >
            本次结果
          </SectionTitle>
          <div className="mt-2">
            {minibtResult ? (
              <div className="rounded-xl border border-blue-100 overflow-hidden">
                <StrategyLabResultPanel
                  result={minibtResult}
                  loading={running}
                  code={code}
                  strategyName={strategyName || undefined}
                />
              </div>
            ) : qlibResult ? (
              <div className="rounded-xl border border-blue-100 overflow-hidden p-2">
                <QlibResultDisplay result={qlibResult} />
              </div>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span className="text-[11px] text-gray-400">
                    运行一次回测，图表会显示在这里
                  </span>
                }
              />
            )}
          </div>
        </section>

        <section className="p-3">
          <SectionTitle
            icon={<History className="h-3.5 w-3.5 text-purple-600" />}
            extra={<Tag className="!text-[10px]">{savedRuns.length}</Tag>}
          >
            已保存回测
          </SectionTitle>
          {savedRuns.length === 0 ? (
            <div className="mt-2 text-[11px] text-gray-400">
              暂无存档。回测完成后点「保存本次」。
            </div>
          ) : (
            <div className="mt-2 space-y-1.5">
              {savedRuns.map((run) => (
                <div
                  key={run.id}
                  className="group flex items-center gap-2 rounded-xl border border-gray-100 bg-gray-50/60 px-2.5 py-2 hover:border-blue-200 hover:bg-blue-50/40 transition-colors"
                >
                  <button
                    type="button"
                    className="flex-1 min-w-0 text-left"
                    onClick={() => onLoadSaved(run)}
                    title="加载这次回测"
                  >
                    <div className="flex items-center gap-1.5">
                      <Tag
                        color={run.kind === 'minibt' ? 'green' : 'blue'}
                        className="!m-0 !text-[9px] !px-1 !leading-4"
                      >
                        {run.kind === 'minibt' ? 'minibt' : 'qlib'}
                      </Tag>
                      <span className="truncate text-[11px] font-medium text-gray-700">
                        {run.name}
                      </span>
                    </div>
                    <div className="mt-0.5 text-[10px] text-gray-400">
                      {new Date(run.savedAt).toLocaleString('zh-CN')}
                    </div>
                  </button>
                  <button
                    type="button"
                    className="p-1 text-gray-300 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-all"
                    title="删除存档"
                    onClick={() => onDeleteSaved(run.id)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="mt-2 text-[10px] text-gray-400">
            <Text type="secondary" className="!text-[10px]">
              存档在本机浏览器；正式台账仍以回测历史为准。
            </Text>
          </div>
        </section>
      </div>
      <div className="px-3 py-2 border-t border-gray-100 bg-gray-50/60 text-[10px] text-gray-400 shrink-0">
        {strategyName ? `策略：${strategyName}` : '未选择策略'} · 回测不是收益承诺
      </div>
    </div>
  );
};

export default StrategyEditorBacktestPanel;
