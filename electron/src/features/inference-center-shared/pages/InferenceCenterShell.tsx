/**
 * 模型推理中心（三市场共用）。
 *
 * 版面为「主从联动·同屏双栏」：
 *   左栏 = 市场截面推理工作台（预检 → 执行 → 排名/历史），
 *   右栏 = 个股预测推理（K 线扇形 + 指标 + 归因 + 共识）。
 * 两者不是两个 Tab，而是一条链路：点左栏排名榜任意一行，右栏直接出该股的个股预测。
 *
 * 窄屏（<1280，主要出现在 Web 而非 Electron：Electron 窗口 minWidth 1440）
 * 退化为「右栏变抽屉」：点排名后抽屉滑出承载同一份结果。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Select, Tag, Drawer, Tooltip, Typography } from 'antd';
import { clsx } from 'clsx';
import { Cpu, Database, Star, Layers, TrendingUp } from 'lucide-react';
import { useLocation } from 'react-router-dom';
import { useInferenceCenter } from '../adapter';
import { useCrossSectionInference } from '../hooks/useCrossSectionInference';
import { useIndividualPrediction } from '../hooks/useIndividualPrediction';
import { CrossSectionRail } from '../components/CrossSectionRail';
import { IndividualWorkbench } from '../components/IndividualWorkbench';
import { StockPoolPickerModal } from '../../../components/backtest/StockPoolPickerModal';
import type { StockPoolOption } from '../../../services/stockPoolOptionService';
import type { InferenceRankingItem } from '../../../services/modelTrainingService';
import { extractModelType, modelDisplayName } from '../../../pages/modelRegistryUtils';

const { Text } = Typography;

/** 低于此宽度不再并排，右栏改抽屉（Electron 窗口 minWidth 1440，正常不会触发） */
const NARROW_BREAKPOINT = 1280;

/** 视口宽度是否小于断点（监听 resize，SSR 缺失时按宽屏处理） */
function useIsNarrow(breakpoint: number): boolean {
  const [isNarrow, setIsNarrow] = useState(
    () => typeof window !== 'undefined' && window.innerWidth < breakpoint,
  );
  useEffect(() => {
    const onResize = () => setIsNarrow(window.innerWidth < breakpoint);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [breakpoint]);
  return isNarrow;
}

export const InferenceCenterShell: React.FC = () => {
  const location = useLocation();
  const adapter = useInferenceCenter();
  const { market, marketLabel, calendar, currencySymbol } = adapter;

  // 深链：外部可带 state.modelId 预选截面模型（旧版还有一个永远取不到 individual 的 tab 分支，已移除）
  const initialModelId = (location.state as any)?.modelId || '';

  const cs = useCrossSectionInference(market, calendar, initialModelId);
  const ip = useIndividualPrediction(adapter);

  const isNarrow = useIsNarrow(NARROW_BREAKPOINT);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [poolPickerOpen, setPoolPickerOpen] = useState(false);

  // 挂载时预加载联想数据源（CN 本地静态表 / HK 名称接口 / US 标的池；失败静默降级为直接输入）
  useEffect(() => {
    adapter.preload().catch((err) => {
      console.warn('[InferenceCenter] 联想数据源加载失败，降级为直接输入:', err);
    });
  }, [adapter]);

  // ── 主从联动：排名榜 → 个股预测 ────────────────────────────
  const { predictFor } = ip;
  const { selectedModel: crossSelectedModel } = cs;
  const availableSingleModels = ip.models;
  const crossModelId = crossSelectedModel?.model_id;

  const handleSelectRanking = useCallback(
    (item: InferenceRankingItem) => {
      // 排名是当前截面模型跑出来的，个股预测优先复用同一个模型；但个股端点只认
      // /research/models 的 id（系统模型 sys-* 不在该表里，传了会被后端静默替换成
      // 第一个模型），所以只在能对上号时才带上，对不上就让后端取默认。
      const reusable =
        crossModelId && availableSingleModels.some((m) => m.modelId === crossModelId)
          ? crossModelId
          : undefined;
      predictFor(item.code, { modelId: reusable });
      if (isNarrow) setDrawerOpen(true);
    },
    [predictFor, crossModelId, availableSingleModels, isNarrow],
  );

  const workbench = (
    <IndividualWorkbench
      ip={ip}
      currencySymbol={currencySymbol}
      searchPlaceholder={adapter.searchPlaceholder}
      suggestionLabel={adapter.suggestionLabel}
      toSuffixSymbol={adapter.toSuffixSymbol}
    />
  );

  return (
    <div
      className="w-full h-full bg-[#f8fafc] p-4 flex flex-col overflow-hidden box-border select-none"
      style={{ fontFamily: "'Microsoft YaHei', '微软雅黑', 'PingFang SC', 'Hiragino Sans GB', sans-serif" }}
    >
      {/* ── 顶栏：页面身份 + 截面推理模型选型（压到一条，纵向空间让给数据区）── */}
      <div className="flex items-center justify-between gap-4 bg-white border border-slate-200 rounded-xl px-4 h-12 mb-3 shrink-0">
        <div className="flex items-center gap-2.5 shrink-0">
          <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center text-white">
            <Cpu className="w-4 h-4" />
          </div>
          <h1 className="text-sm font-black text-slate-800 m-0 tracking-tight whitespace-nowrap">模型推理中心</h1>
          <Tag color="blue" className="rounded text-[11px] font-bold border-0 px-1.5 py-0 m-0">
            {marketLabel}
          </Tag>
          <Tooltip title="左侧跑全市场截面打分 · 点排名即出右侧个股预测与因子归因">
            <span className="text-[11px] text-slate-400 whitespace-nowrap cursor-help hidden xl:inline">
              截面打分 · 点排名出个股预测
            </span>
          </Tooltip>
        </div>

        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[11px] font-bold text-slate-500 flex items-center gap-1.5 whitespace-nowrap">
            <Database size={13} className="text-slate-400" />
            截面模型
          </span>
          <Select
            value={cs.selectedModelId}
            onChange={cs.setSelectedModelId}
            loading={cs.modelsLoading}
            size="small"
            showSearch
            // 可选模型常有几十个（CN 当前 34+），必须能按名称搜；
            // label 是 ReactNode 不能直接参与过滤，回到注册表按展示名匹配。
            filterOption={(input, option) => {
              const hit = cs.registeredModels.find((m) => m.model_id === option?.value);
              return Boolean(hit) && modelDisplayName(hit!).toLowerCase().includes(input.trim().toLowerCase());
            }}
            className="!w-64 [&_.ant-select-selection-item]:text-xs [&_.ant-select-selection-item]:font-bold [&_.ant-select-selection-item]:text-slate-800"
            options={cs.registeredModels.map((m) => ({
              value: m.model_id,
              label: (
                <div className="flex items-center justify-between text-xs">
                  <span className="font-semibold truncate">{modelDisplayName(m)}</span>
                  {m.is_default && <Tag color="gold" className="!mr-0 text-[10px] leading-tight">默认</Tag>}
                </div>
              ),
            }))}
          />
          {crossSelectedModel && (
            <div className="flex items-center gap-2 shrink-0 pl-2 border-l border-slate-200">
              <span className="text-[11px] text-slate-500 whitespace-nowrap">
                架构 <strong className="font-mono text-slate-800">{extractModelType(crossSelectedModel)}</strong>
              </span>
              <span className="text-[11px] text-slate-500 whitespace-nowrap">
                目标 <strong className="font-mono text-blue-700">T+{cs.horizonDays}</strong>
              </span>
              <span
                className={clsx(
                  'text-[10px] font-bold px-1.5 py-0.5 rounded whitespace-nowrap',
                  crossSelectedModel.is_default
                    ? 'bg-amber-50 border border-amber-200 text-amber-600'
                    : 'bg-slate-50 border border-slate-200 text-slate-500',
                )}
              >
                {crossSelectedModel.is_default ? (
                  <span className="flex items-center gap-1"><Star size={10} fill="currentColor" /> 默认生效</span>
                ) : '非默认模型'}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ── 主体：左栏截面 / 右栏个股 ─────────────────────────── */}
      <div className="flex-1 min-h-0 flex gap-3">
        <div className="w-[520px] shrink-0 min-h-0">
          {cs.selectedModel ? (
            <CrossSectionRail
              model={cs.selectedModel}
              cs={cs}
              activeCode={ip.symbol}
              onSelectRanking={handleSelectRanking}
              onDeleteHistory={cs.deleteHistory}
              poolSlot={
                market === 'CN' ? (
                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => {
                        if (!cs.pool) setPoolPickerOpen(true);
                        else cs.setPool(null);
                      }}
                      title={cs.pool ? `股票池: ${cs.pool.name}（点击恢复全市场）` : '全市场（点击选择股票池）'}
                      className={clsx(
                        'border rounded-lg px-2 py-1 flex items-center gap-1 whitespace-nowrap transition-colors',
                        cs.pool ? 'bg-blue-50 border-blue-300' : 'bg-white border-slate-200 hover:border-blue-200',
                      )}
                    >
                      <Layers size={11} className={cs.pool ? 'text-blue-600' : 'text-slate-400'} />
                      <span className="text-[11px] font-bold text-slate-700">股票池</span>
                      <span className={clsx('text-[11px] font-black max-w-[110px] truncate', cs.pool ? 'text-blue-700' : 'text-slate-500')}>
                        {cs.pool ? cs.pool.name : '全市场'}
                      </span>
                    </button>
                    {cs.pool && (
                      <button
                        type="button"
                        onClick={() => setPoolPickerOpen(true)}
                        title="更换股票池"
                        className="border border-blue-200 bg-white rounded-lg px-2 py-1 whitespace-nowrap text-[11px] font-bold text-blue-600 hover:bg-blue-50 transition-colors"
                      >
                        更换
                      </button>
                    )}
                  </div>
                ) : undefined
              }
            />
          ) : (
            <div className="h-full flex items-center justify-center bg-white border border-slate-200 rounded-xl">
              <Text className="text-xs text-slate-500">当前市场无可用模型</Text>
            </div>
          )}
        </div>

        {isNarrow ? (
          <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-3 bg-white border border-dashed border-slate-200 rounded-xl text-slate-500">
            <TrendingUp size={26} className="opacity-30" />
            <span className="text-xs font-semibold">点左侧排名任意一行，个股预测从右侧滑出</span>
          </div>
        ) : (
          workbench
        )}
      </div>

      {/* 窄屏：右栏以抽屉承载同一份结果（hook 状态在 Shell，关掉再开结果不丢） */}
      {isNarrow && (
        <Drawer
          placement="right"
          width="92%"
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          closable={false}
          styles={{ body: { padding: 0, background: '#f8fafc' } }}
        >
          <div className="h-full p-3 flex flex-col">{workbench}</div>
        </Drawer>
      )}

      {market === 'CN' && (
        <StockPoolPickerModal
          open={poolPickerOpen}
          onClose={() => setPoolPickerOpen(false)}
          selectedPoolId={cs.pool?.id ?? null}
          market="CN"
          title="推理股票池"
          onSelect={(pool: StockPoolOption) => {
            cs.setPool({ ref: `pool:${pool.code}`, name: pool.name, id: pool.pool_id });
            setPoolPickerOpen(false);
          }}
        />
      )}
    </div>
  );
};

export default InferenceCenterShell;
