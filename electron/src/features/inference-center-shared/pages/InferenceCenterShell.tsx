/**
 * 模型推理中心（三市场共用）。
 *
 * 版面为「顶栏状态带 + 三工作区」：
 *   顶栏   = 页面身份 + 工作区切换 + 截面模型选型
 *   状态带 = 市场 · 数据基准日与陈旧度 · 预测交易日 · 模型/榜行数 · 预检结论
 *   工作区 = 单票研判（主从双栏）/ 截面选股（全宽榜）/ 模型治理（资产体检）
 *
 * 三个工作区共享同一份 hook 状态（模型选型、基准日、已选标的、预检结论），
 * 切换不丢上下文：在截面榜点一行 → 切到单票研判并直接出该股预测。
 *
 * 窄屏（<1280，主要出现在 Web 而非 Electron：Electron 窗口 minWidth 1440）
 * 只有「单票研判」需要退化：右侧工作台改由抽屉承载；另两个工作区本就是单列。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Select, Tag, Drawer, Tooltip, Typography } from 'antd';
import { clsx } from 'clsx';
import { Cpu, Database, Star, Layers, TrendingUp } from 'lucide-react';
import { useLocation } from 'react-router-dom';
import { useInferenceCenter } from '../adapter';
import { ComplianceStrip } from '../../../components/shared/compliance/ComplianceChrome';
import { useCrossSectionInference } from '../hooks/useCrossSectionInference';
import {
  RAIL_WIDTH_KEY,
  nextRailWidth,
  parseSavedRailWidth,
  railWidthStyle,
} from '../railWidth';
import { useIndividualPrediction } from '../hooks/useIndividualPrediction';
import { CrossSectionRail } from '../components/CrossSectionRail';
import { IndividualWorkbench } from '../components/IndividualWorkbench';
import { CrossSectionTable } from '../components/CrossSectionTable';
import { MarketStatusBar } from '../components/MarketStatusBar';
import { ModelGovernancePanel } from '../components/ModelGovernancePanel';
import { WorkspaceTabs, type WorkspaceKey } from '../components/WorkspaceTabs';
import { StockPoolPickerModal } from '../../../components/backtest/StockPoolPickerModal';
import type { StockPoolOption } from '../../../services/stockPoolOptionService';
import type { InferenceRankingItem, UserModelRecord } from '../../../services/modelTrainingService';
import { extractModelType, modelDisplayName } from '../../../pages/modelRegistryUtils';

const { Text } = Typography;

/** 低于此宽度不再并排，右侧工作台改抽屉（Electron 窗口 minWidth 1440，正常不会触发） */
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
  const [workspace, setWorkspace] = useState<WorkspaceKey>('single');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [poolPickerOpen, setPoolPickerOpen] = useState(false);

  // ── 排名榜宽度（可拖，落 localStorage）───────────────────────
  // null = 用默认的 clamp；一旦拖过就固定成像素值（拖动中不做 clamp 免得跟手起来一跳一跳）
  const railRef = useRef<HTMLDivElement | null>(null);
  const [railWidth, setRailWidth] = useState<number | null>(() =>
    typeof localStorage === 'undefined'
      ? null
      : parseSavedRailWidth(localStorage.getItem(RAIL_WIDTH_KEY)),
  );
  /**
   * 拖动起点以「按下那一刻的实测宽度」为基准，而不是 state —— state 可能还是 null
   * （走默认 clamp），拿它当基准会从 0 开始跳。`last` 记最后一帧的宽度，抬手时落盘；
   * `moved` 用来区分「真拖过」与「只是点了一下」——后者不该把 clamp 默认值固化成像素。
   */
  const railDrag = useRef<{ x: number; w: number; last: number; moved: boolean } | null>(null);

  const startRailDrag = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = railRef.current;
    if (!el) return;
    const w = el.getBoundingClientRect().width;
    railDrag.current = { x: e.clientX, w, last: w, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);

  const moveRailDrag = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const d = railDrag.current;
    if (!d) return;
    const next = nextRailWidth(d.w, e.clientX - d.x);
    if (next == null) return;   // 越界：这一帧不动
    d.moved = true;
    d.last = next;
    setRailWidth(next);
  }, []);

  const endRailDrag = useCallback(() => {
    const d = railDrag.current;
    railDrag.current = null;
    if (d?.moved && typeof localStorage !== 'undefined') {
      localStorage.setItem(RAIL_WIDTH_KEY, String(d.last));
    }
  }, []);

  const resetRailWidth = useCallback(() => {
    railDrag.current = null;
    setRailWidth(null);
    if (typeof localStorage !== 'undefined') localStorage.removeItem(RAIL_WIDTH_KEY);
  }, []);

  // 挂载时预加载联想数据源（CN 本地静态表 / HK 名称接口 / US 标的池；失败静默降级为直接输入）
  useEffect(() => {
    adapter.preload().catch((err) => {
      console.warn('[InferenceCenter] 联想数据源加载失败，降级为直接输入:', err);
    });
  }, [adapter]);

  // ── 主从联动：排名榜 / 治理表 → 个股研判 ────────────────────
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
      setWorkspace('single');
      if (isNarrow) setDrawerOpen(true);
    },
    [predictFor, crossModelId, availableSingleModels, isNarrow],
  );

  /** 治理表点行 → 切到单票研判；模型在该市场个股端点可用时预选，否则交给默认模型 */
  const handleInspectModel = useCallback(
    (model: UserModelRecord) => {
      const usable = availableSingleModels.some((m) => m.modelId === model.model_id);
      if (usable) ip.setModelId(model.model_id);
      setWorkspace('single');
    },
    [availableSingleModels, ip],
  );

  // ── 顶栏状态带数据 ──────────────────────────────────────────
  const precheckFailDetail = useMemo(() => {
    const failed = cs.precheck?.items?.find((i) => !i.passed && i.severity !== 'soft');
    return failed ? `${failed.label}：${failed.detail}` : null;
  }, [cs.precheck]);

  const rankingCount = cs.ranking?.rankings?.length ?? 0;

  const workbench = (
    <IndividualWorkbench
      ip={ip}
      currencySymbol={currencySymbol}
      searchPlaceholder={adapter.searchPlaceholder}
      suggestionLabel={adapter.suggestionLabel}
      toSuffixSymbol={adapter.toSuffixSymbol}
    />
  );

  const crossSectionRail = cs.selectedModel ? (
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
  );

  return (
    <div
      className="w-full h-full bg-[#f8fafc] p-4 flex flex-col overflow-hidden box-border select-none"
      style={{ fontFamily: "'Microsoft YaHei', '微软雅黑', 'PingFang SC', 'Hiragino Sans GB', sans-serif" }}
    >
      {/* ── 顶栏：页面身份 + 工作区切换 + 截面模型选型 ── */}
      <div className="flex items-center justify-between gap-4 bg-white border border-slate-200 rounded-xl px-4 h-12 mb-2 shrink-0">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center text-white shrink-0">
            <Cpu className="w-4 h-4" />
          </div>
          <h1 className="text-sm font-black text-slate-800 m-0 tracking-tight whitespace-nowrap">模型推理中心</h1>
          <Tag color="blue" className="rounded text-[11px] font-bold border-0 px-1.5 py-0 m-0 shrink-0">
            {marketLabel}
          </Tag>
          <WorkspaceTabs
            active={workspace}
            onChange={setWorkspace}
            badges={{
              cross: rankingCount > 0 ? String(rankingCount) : undefined,
              governance: cs.registeredModels.length > 0 ? String(cs.registeredModels.length) : undefined,
            }}
          />
        </div>

        {/* 截面模型选型只对「单票研判 / 截面选股」有意义：两个工作区共用这一份上下文 */}
        {cs.selectedModel && workspace !== 'governance' && (
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
            <div className="hidden 2xl:flex items-center gap-2 shrink-0 pl-2 border-l border-slate-200">
              <span className="text-[11px] text-slate-500 whitespace-nowrap">
                架构 <strong className="font-mono text-slate-800">{extractModelType(crossSelectedModel!)}</strong>
              </span>
              <span className="text-[11px] text-slate-500 whitespace-nowrap">
                目标 <strong className="font-mono text-blue-700">T+{cs.horizonDays}</strong>
              </span>
              <span
                className={clsx(
                  'text-[10px] font-bold px-1.5 py-0.5 rounded whitespace-nowrap',
                  crossSelectedModel!.is_default
                    ? 'bg-amber-50 border border-amber-200 text-amber-600'
                    : 'bg-slate-50 border border-slate-200 text-slate-500',
                )}
              >
                {crossSelectedModel!.is_default ? (
                  <span className="flex items-center gap-1"><Star size={10} fill="currentColor" /> 默认生效</span>
                ) : '非默认模型'}
              </span>
            </div>
          </div>
        )}

        {workspace === 'governance' && (
          <Tooltip title="盘点该市场已注册模型的周期口径、区间能力、归因可用性与产物健康度">
            <span className="text-[11px] text-slate-400 whitespace-nowrap cursor-help hidden xl:inline">
              模型资产盘点 · 不依赖截面选型
            </span>
          </Tooltip>
        )}
      </div>

      {/* ── 状态带：任何工作区都可见的市场/数据/模型上下文 ── */}
      <div className="shrink-0 mb-3">
        <MarketStatusBar
          marketLabel={marketLabel}
          calendar={calendar}
          dataTradeDate={cs.precheck?.data_trade_date ?? null}
          predictionTradeDate={cs.precheck?.prediction_trade_date ?? null}
          precheckPassed={cs.precheck ? cs.precheck.passed : null}
          precheckError={precheckFailDetail}
          modelCount={cs.registeredModels.length}
          rankingCount={rankingCount}
          rankingFallbackFrom={cs.rankingFallbackFrom}
        />
      </div>

      {/* ── 工作区主体 ─────────────────────────────────────── */}
      {workspace === 'single' && (
        <div className="flex-1 min-h-0 flex">
          {/* 排名榜宽度：默认随视口走，但**可拖**。
              两侧对宽度都有真实诉求 —— 榜单那行「手动推理执行 + 日期 + T+N + 立即执行 + 设默认」
              实测最少要 ~500px（给 460 会溢出 20px），而右栏的 K 线图越宽越好。
              固定分配必然有一侧受挤，所以交给用户：拖完记住，双击复位。 */}
          <div
            ref={railRef}
            className="shrink-0 min-h-0"
            style={{ width: railWidthStyle(railWidth) }}
          >
            {crossSectionRail}
          </div>
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="调整排名榜宽度"
            title="拖动调整宽度 · 双击复位"
            onPointerDown={startRailDrag}
            onPointerMove={moveRailDrag}
            onPointerUp={endRailDrag}
            onPointerCancel={endRailDrag}
            onDoubleClick={resetRailWidth}
            className={clsx(
              'shrink-0 w-3 cursor-col-resize rounded-md transition-colors',
              'hover:bg-blue-300/60 active:bg-blue-400/70',
            )}
          />

          {isNarrow ? (
            <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-3 bg-white border border-dashed border-slate-200 rounded-xl text-slate-500">
              <TrendingUp size={26} className="opacity-30" />
              <span className="text-xs font-semibold">点左侧排名任意一行，个股预测从右侧滑出</span>
            </div>
          ) : (
            workbench
          )}
        </div>
      )}

      {workspace === 'cross' && (
        <div className="flex-1 min-h-0 flex flex-col">
          <CrossSectionTable
            ranking={cs.ranking}
            loading={cs.rankingLoading}
            fallbackFrom={cs.rankingFallbackFrom}
            onSelect={handleSelectRanking}
            activeCode={ip.symbol}
          />
        </div>
      )}

      {workspace === 'governance' && (
        <div className="flex-1 min-h-0 flex flex-col">
          <ModelGovernancePanel
            models={cs.registeredModels}
            loading={cs.modelsLoading}
            marketLabel={marketLabel}
            onInspect={handleInspectModel}
          />
        </div>
      )}

      {/* 窄屏：单票研判右侧工作台以抽屉承载同一份结果（hook 状态在 Shell，关掉再开结果不丢） */}
      {isNarrow && workspace === 'single' && (
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
      {/* 合规免责横条（A股/港股/美股三市场共用本页壳，改一处三处生效） */}
      <ComplianceStrip className="shrink-0 pt-1" />
      {/* 给悬浮 Dock 让位：root 只有 p-4（16px），不足 Dock 胶囊的 64px，
          不留实体占位块的话免责横条正好被 .bottom-dock 盖住（padding 无效，
          实测见 SignalsExplorerPage 同名注释）。无 Dock 页面 --dock-height 为 0，
          max() 兜住 calc(0px-8px) 负值。 */}
      <div aria-hidden className="h-[max(0px,calc(var(--dock-height)-8px))] shrink-0" />
    </div>
  );
};

export default InferenceCenterShell;
