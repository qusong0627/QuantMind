/**
 * 组合排名工作区状态（单一状态源）。
 *
 * 约定：
 * - 子组件**不各自请求**；一切都从这里拿，避免「榜单刷新了、计划还是旧的」；
 * - 数据按 Tab 懒加载（滚动/计划/证据第一次进入才发请求），榜单随选中组合+日期刷新；
 * - 未保存的模板只能看「当日榜单」（`/preview`）；滚动与计划要求先保存
 *   ——这是后端契约（它们要写纸面台账，必须有 blend_id），不是前端偷懒；
 * - 首次进入若「我的组合」为空，自动把基线模板（C0 全 13·T+10）建成默认组合，
 *   让页面开门就有榜单；只尝试一次，失败只提示不阻塞。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { message } from 'antd';
import { blendService, blendErrorMessage } from '../../../services/blendService';
import type {
  BlendBounds,
  BlendDatesResponse,
  BlendEvidenceResponse,
  BlendPeriodQuery,
  BlendPeriodResponse,
  BlendPlan,
  BlendPlanRequest,
  BlendRankingResponse,
  BlendRecord,
  BlendRollingResponse,
  ComboTemplate,
} from '../../../services/blendTypes';
import { templateToCreateRequest } from '../components/combo/comboRankingModel';

export type ComboTab = 'ranking' | 'rolling' | 'period' | 'plan' | 'evidence';

/** 滚动建仓默认批次本金（元） */
export const DEFAULT_ROLLING_CAPITAL = 10_000_000;

export interface ComboSelection {
  kind: 'blend' | 'template';
  id: string;
}

export interface ComboWorkspaceState {
  market: string;
  blends: BlendRecord[];
  templates: ComboTemplate[];
  baselineTemplateId: string | null;
  snapshotLoading: boolean;
  snapshotError: string | null;

  selection: ComboSelection | null;
  selectedBlend: BlendRecord | null;
  selectedTemplate: ComboTemplate | null;
  select: (selection: ComboSelection | null) => void;

  dates: string[];
  datesLoading: boolean;
  date: string | null;
  setDate: (date: string | null) => void;

  /** 组合历史可用区间（pred 测试集 / 推理台账），周期收益的快捷区间锚点 */
  bounds: BlendBounds | null;
  boundsLoading: boolean;
  boundsError: string | null;

  /** 滚动建仓批次本金（元）：改变后重算批次与机构指标 */
  rollingCapital: number;
  setRollingCapital: (capital: number) => void;

  tab: ComboTab;
  setTab: (tab: ComboTab) => void;

  ranking: BlendRankingResponse | null;
  rankingLoading: boolean;
  rankingError: string | null;
  reloadRanking: () => void;

  rolling: BlendRollingResponse | null;
  rollingLoading: boolean;
  rollingError: string | null;
  reloadRolling: () => void;

  period: BlendPeriodResponse | null;
  periodLoading: boolean;
  periodError: string | null;
  /** 周期收益按参数显式触发（建仓日/截止日/资金/持仓上限） */
  loadPeriod: (params?: BlendPeriodQuery) => Promise<void>;
  reloadPeriod: () => void;

  plan: BlendPlan | null;
  planLoading: boolean;
  planError: string | null;
  reloadPlan: () => void;
  generatePlan: (overrides?: BlendPlanRequest) => Promise<void>;
  confirmPlan: () => Promise<void>;

  evidence: BlendEvidenceResponse | null;
  evidenceLoading: boolean;
  evidenceError: string | null;
  reloadEvidence: () => void;

  refreshBlends: () => Promise<void>;
  /** 重载组合清单 + 模板（失败重试入口）；同时复位默认组合的「只建一次」守卫 */
  reloadSnapshot: () => Promise<void>;
}

export function useComboWorkspace(
  market: string,
  excludeBlacklist = false,
): ComboWorkspaceState {
  const [blends, setBlends] = useState<BlendRecord[]>([]);
  const [templates, setTemplates] = useState<ComboTemplate[]>([]);
  const [baselineTemplateId, setBaselineTemplateId] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(true);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);

  const [selection, setSelection] = useState<ComboSelection | null>(null);
  const [dates, setDates] = useState<string[]>([]);
  const [datesLoading, setDatesLoading] = useState(false);
  /** 日期请求已结束（成功或失败）：榜单/滚动/计划都要等它，避免先按 null 日发一次再重发 */
  const [datesReady, setDatesReady] = useState(false);
  const [date, setDate] = useState<string | null>(null);

  const [bounds, setBounds] = useState<BlendBounds | null>(null);
  const [boundsLoading, setBoundsLoading] = useState(false);
  const [boundsError, setBoundsError] = useState<string | null>(null);
  const [rollingCapital, setRollingCapital] = useState(DEFAULT_ROLLING_CAPITAL);

  const [tab, setTab] = useState<ComboTab>('ranking');

  const [ranking, setRanking] = useState<BlendRankingResponse | null>(null);
  const [rankingLoading, setRankingLoading] = useState(false);
  const [rankingError, setRankingError] = useState<string | null>(null);
  const [rankingNonce, setRankingNonce] = useState(0);

  const [rolling, setRolling] = useState<BlendRollingResponse | null>(null);
  const [rollingLoading, setRollingLoading] = useState(false);
  const [rollingError, setRollingError] = useState<string | null>(null);
  const [rollingNonce, setRollingNonce] = useState(0);

  const [period, setPeriod] = useState<BlendPeriodResponse | null>(null);
  const [periodLoading, setPeriodLoading] = useState(false);
  const [periodError, setPeriodError] = useState<string | null>(null);

  const [plan, setPlan] = useState<BlendPlan | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [planNonce, setPlanNonce] = useState(0);

  const [evidence, setEvidence] = useState<BlendEvidenceResponse | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [evidenceNonce, setEvidenceNonce] = useState(0);

  const autoCreateRef = useRef(false);
  const evidenceRequestedRef = useRef(false);
  /** 周期收益首次进入 Tab 自动按默认参数算一次；之后的参数由面板显式触发 */
  const periodRequestedRef = useRef(false);
  /** 面板「重新计算」用最近一次参数（默认参数也算） */
  const lastPeriodQueryRef = useRef<BlendPeriodQuery>({});
  const periodSeqRef = useRef(0);

  const selectedBlend = useMemo(
    () => (selection?.kind === 'blend' ? blends.find((b) => b.blend_id === selection.id) ?? null : null),
    [selection, blends],
  );
  const selectedTemplate = useMemo(
    () => (selection?.kind === 'template' ? templates.find((t) => t.template_id === selection.id) ?? null : null),
    [selection, templates],
  );
  const selectionKey = selection ? `${selection.kind}:${selection.id}` : null;

  // ── 周期收益（显式参数触发，禁止并发响应互相覆盖）────────────────────
  const loadPeriod = useCallback(
    async (params: BlendPeriodQuery = {}) => {
      if (!selectedBlend) return;
      lastPeriodQueryRef.current = params;
      const seq = periodSeqRef.current + 1;
      periodSeqRef.current = seq;
      setPeriodLoading(true);
      setPeriodError(null);
      try {
        const resp = await blendService.getPeriod(selectedBlend.blend_id, params);
        if (seq === periodSeqRef.current) setPeriod(resp);
      } catch (err) {
        if (seq === periodSeqRef.current) setPeriodError(blendErrorMessage(err));
      } finally {
        if (seq === periodSeqRef.current) setPeriodLoading(false);
      }
    },
    [selectedBlend],
  );

  const reloadPeriod = useCallback(() => {
    void loadPeriod(lastPeriodQueryRef.current);
  }, [loadPeriod]);

  // 组合切换：周期结果属于旧组合，必须清空并允许新组合首次自动计算
  useEffect(() => {
    periodRequestedRef.current = false;
    lastPeriodQueryRef.current = {};
    periodSeqRef.current += 1;
    setPeriod(null);
    setPeriodError(null);
    setPeriodLoading(false);
  }, [selectionKey]);

  // 首次进入「周期收益」Tab：按模板默认参数算一次（用户改参数后由面板触发）
  useEffect(() => {
    if (selection?.kind !== 'blend' || tab !== 'period' || !datesReady) return;
    if (periodRequestedRef.current) return;
    periodRequestedRef.current = true;
    void loadPeriod({});
  }, [selection, tab, datesReady, loadPeriod]);

  // ── 组合列表 + 模板 ────────────────────────────────────────────────
  const loadSnapshot = useCallback(async () => {
    setSnapshotLoading(true);
    setSnapshotError(null);
    try {
      const [saved, auto] = await Promise.all([
        blendService.listBlends(),
        blendService.getAutoCandidates(market).catch(() => null),
      ]);
      setBlends(saved);
      setTemplates(auto?.items ?? []);
      setBaselineTemplateId(auto?.baseline_template_id ?? null);

      // 首次进入：空列表 + 有基线模板 → 建默认组合（只尝试一次）
      const baseline = (auto?.items ?? []).find((item) => item.baseline);
      if (saved.length === 0 && baseline && !autoCreateRef.current) {
        autoCreateRef.current = true;
        try {
          const created = await blendService.createBlend(templateToCreateRequest(baseline));
          setBlends((prev) => [created, ...prev]);
          message.success('已创建默认组合：13 个 T+10 模型等权');
        } catch (err) {
          // 并发下可能已被别处创建：重拉一次即可，不当作失败
          const retry = await blendService.listBlends().catch(() => []);
          if (retry.length > 0) setBlends(retry);
          else message.warning(`默认组合创建失败：${blendErrorMessage(err)}`);
        }
      }
    } catch (err) {
      setSnapshotError(blendErrorMessage(err));
    } finally {
      setSnapshotLoading(false);
    }
  }, [market]);

  useEffect(() => {
    setSelection(null);
    setDates([]);
    setDate(null);
    setRanking(null);
    setRolling(null);
    setPlan(null);
    setEvidence(null);
    evidenceRequestedRef.current = false;
    autoCreateRef.current = false;
    void loadSnapshot();
  }, [loadSnapshot]);

  // 默认选中：已保存组合优先，其次基线模板
  useEffect(() => {
    if (selection || snapshotLoading) return;
    if (blends.length > 0) {
      setSelection({ kind: 'blend', id: blends[0].blend_id });
      return;
    }
    if (baselineTemplateId) {
      setSelection({ kind: 'template', id: baselineTemplateId });
      return;
    }
    if (templates.length > 0) setSelection({ kind: 'template', id: templates[0].template_id });
  }, [selection, snapshotLoading, blends, templates, baselineTemplateId]);

  // ── 可用区间（pred 测试集 / 推理台账）──────────────────────────────
  // 周期收益的快捷区间以它为准（pred 可回看到 2025）；失败不阻塞页面，
  // 面板退化为按 `/dates`（仅推理台账）锚定。
  useEffect(() => {
    if (selection?.kind !== 'blend') {
      setBounds(null);
      setBoundsError(null);
      setBoundsLoading(false);
      return;
    }
    let alive = true;
    setBoundsLoading(true);
    setBoundsError(null);
    blendService
      .getBounds(selection.id)
      .then((resp) => alive && setBounds(resp))
      .catch((err) => alive && setBoundsError(blendErrorMessage(err)))
      .finally(() => alive && setBoundsLoading(false));
    return () => {
      alive = false;
    };
  }, [selection]);

  // ── 可用日期（仅已保存组合）────────────────────────────────────────
  useEffect(() => {
    if (selection?.kind !== 'blend') {
      setDates([]);
      setDate(null);
      setDatesReady(false);
      return;
    }
    let alive = true;
    setDatesReady(false);
    setDatesLoading(true);
    blendService
      // 周期收益可回看半年甚至更长，日期下拉要覆盖到后端上限附近
      .getDates(selection.id, 365)
      .then((resp: BlendDatesResponse) => {
        if (!alive) return;
        setDates(resp.dates ?? []);
        // 已选日期仍可用就保留；否则回到最新一日
        setDate((prev) => (prev && resp.dates?.includes(prev) ? prev : resp.dates?.[0] ?? null));
      })
      .catch((err) => {
        if (!alive) return;
        setDates([]);
        setDate(null);
        message.warning(blendErrorMessage(err));
      })
      .finally(() => {
        if (!alive) return;
        setDatesLoading(false);
        setDatesReady(true);
      });
    return () => {
      alive = false;
    };
  }, [selection]);

  // ── 当日榜单：已保存走 /ranking（带历史标注），模板走 /preview ──────
  // 依赖按「选取类型」折算：模板预览不使用日期，切到模板时 /dates 复位引发的
  // date/datesReady 变化不应再触发一次同参请求（否则每次切模板都打两次）。
  const isBlendSelection = selection?.kind === 'blend';
  const blendDate = isBlendSelection ? date : null;
  const blendDatesReady = isBlendSelection ? datesReady : true;
  const hasBlendDates = isBlendSelection ? dates.length > 0 : true;

  useEffect(() => {
    if (!selection) return;
    if (!isBlendSelection) {
      // 模板列表还没回来时先等（空 model_ids 会 422）
      if (!selectedTemplate) return;
    } else if (!blendDatesReady) {
      // 已保存组合：等 /dates 落定，避免「先按最新日请求一次、再按选中日重发」
      return;
    } else if (!hasBlendDates) {
      setRanking(null);
      setRankingError('该组合暂无完整评分的交易日');
      setRankingLoading(false);
      return;
    }

    let alive = true;
    setRankingLoading(true);
    setRankingError(null);
    const request = isBlendSelection
      ? blendService.getRanking(selection.id, {
          date: blendDate ?? undefined,
          excludeBlacklist,
        })
      : blendService.preview({
          market,
          model_ids: (selectedTemplate?.members ?? []).map((m) => m.model_id),
          method: selectedTemplate?.method ?? 'mean',
          trim_count: selectedTemplate?.trim_count ?? 2,
          top_k: 500,
          top: 500,
          min_coverage: selectedTemplate?.min_coverage ?? 2,
          consensus_min_count: selectedTemplate?.consensus_min_count ?? 0,
          exclude_blacklist: excludeBlacklist,
          // 模板未保存，没有可用日期列表：恒按最新交易日预览
        });
    request
      .then((resp) => {
        if (!alive) return;
        setRanking(resp);
        if (isBlendSelection && !blendDate && resp.data_trade_date) {
          setDate(resp.data_trade_date);
        }
      })
      .catch((err) => alive && setRankingError(blendErrorMessage(err)))
      .finally(() => alive && setRankingLoading(false));
    return () => {
      alive = false;
    };
  }, [selection, blendDate, blendDatesReady, hasBlendDates, market, selectedTemplate, rankingNonce, excludeBlacklist]);

  // ── 滚动建仓（仅已保存组合，Tab 首次进入才拉）──────────────────────
  useEffect(() => {
    if (selection?.kind !== 'blend' || tab !== 'rolling' || !datesReady) return;
    if (dates.length === 0) {
      setRolling(null);
      setRollingError('该组合暂无完整评分的交易日');
      setRollingLoading(false); // 上一轮请求的 finally 已随 cleanup 失效，不补就永远转圈
      return;
    }
    let alive = true;
    setRollingLoading(true);
    setRollingError(null);
    blendService
      .getRolling(selection.id, {
        date: date ?? undefined,
        capital: rollingCapital,
      })
      .then((resp) => alive && setRolling(resp))
      .catch((err) => alive && setRollingError(blendErrorMessage(err)))
      .finally(() => alive && setRollingLoading(false));
    return () => {
      alive = false;
    };
  }, [selection, tab, date, datesReady, dates.length, rollingNonce, rollingCapital]);

  // ── 纸面计划 ───────────────────────────────────────────────────────
  useEffect(() => {
    if (selection?.kind !== 'blend' || tab !== 'plan' || !datesReady) return;
    if (dates.length === 0) {
      setPlan(null);
      setPlanError('该组合暂无完整评分的交易日');
      setPlanLoading(false); // 同 rolling：cleanup 后旧请求的 finally 不会落地
      return;
    }
    let alive = true;
    setPlanLoading(true);
    setPlanError(null);
    blendService
      .listPlans(selection.id, { date: date ?? undefined, limit: 1 })
      .then((resp) => alive && setPlan(resp.items?.[0] ?? null))
      .catch((err) => alive && setPlanError(blendErrorMessage(err)))
      .finally(() => alive && setPlanLoading(false));
    return () => {
      alive = false;
    };
  }, [selection, tab, date, datesReady, dates.length, planNonce]);

  // ── 证据包（只读一次，缺失给空态）──────────────────────────────────
  useEffect(() => {
    if (tab !== 'evidence' || evidenceRequestedRef.current) return;
    evidenceRequestedRef.current = true;
    let alive = true;
    setEvidenceLoading(true);
    setEvidenceError(null);
    blendService
      .getEvidence()
      .then((resp) => alive && setEvidence(resp))
      .catch((err) => {
        if (!alive) return;
        setEvidenceError(blendErrorMessage(err));
        evidenceRequestedRef.current = false;
      })
      .finally(() => alive && setEvidenceLoading(false));
    return () => {
      alive = false;
    };
  }, [tab, evidenceNonce]);

  // ── 动作 ───────────────────────────────────────────────────────────
  const generatePlan = useCallback(
    async (overrides: BlendPlanRequest = {}) => {
      if (!selectedBlend) return;
      setPlanLoading(true);
      setPlanError(null);
      try {
        const created = await blendService.createPlan(selectedBlend.blend_id, {
          data_trade_date: date ?? undefined,
          persist: true,
          ...overrides,
        });
        setPlan(created);
        message.success('纸面计划已生成（仅本地台账，不产生委托）');
      } catch (err) {
        setPlanError(blendErrorMessage(err));
      } finally {
        setPlanLoading(false);
      }
    },
    [selectedBlend, date],
  );

  const confirmPlan = useCallback(async () => {
    if (!selectedBlend || !plan?.plan_id) return;
    setPlanLoading(true);
    setPlanError(null);
    try {
      const confirmed = await blendService.confirmPlan(selectedBlend.blend_id, plan.plan_id);
      setPlan(confirmed);
      message.success('已确认纸面调仓（仅本地台账）');
    } catch (err) {
      setPlanError(blendErrorMessage(err));
    } finally {
      setPlanLoading(false);
    }
  }, [selectedBlend, plan]);

  const reloadSnapshot = useCallback(async () => {
    // 手动重试视为一次新的首启：后端更新/网络恢复后仍应能补建默认组合，
    // 否则一次失败会让 autoCreateRef 永久锁死，直到整页刷新。
    autoCreateRef.current = false;
    await loadSnapshot();
  }, [loadSnapshot]);

  const refreshBlends = useCallback(async () => {
    setSnapshotLoading(true);
    try {
      setBlends(await blendService.listBlends());
    } catch (err) {
      setSnapshotError(blendErrorMessage(err));
    } finally {
      setSnapshotLoading(false);
    }
  }, []);

  return {
    market,
    blends,
    templates,
    baselineTemplateId,
    snapshotLoading,
    snapshotError,
    selection,
    selectedBlend,
    selectedTemplate,
    select: setSelection,
    dates,
    datesLoading,
    date,
    setDate,
    bounds,
    boundsLoading,
    boundsError,
    rollingCapital,
    setRollingCapital,
    tab,
    setTab,
    ranking,
    rankingLoading,
    rankingError,
    reloadRanking: () => setRankingNonce((n) => n + 1),
    rolling,
    rollingLoading,
    rollingError,
    reloadRolling: () => setRollingNonce((n) => n + 1),
    period,
    periodLoading,
    periodError,
    loadPeriod,
    reloadPeriod,
    plan,
    planLoading,
    planError,
    reloadPlan: () => setPlanNonce((n) => n + 1),
    generatePlan,
    confirmPlan,
    evidence,
    evidenceLoading,
    evidenceError,
    reloadEvidence: () => {
      evidenceRequestedRef.current = false;
      setEvidenceNonce((n) => n + 1);
    },
    refreshBlends,
    reloadSnapshot,
  };
}
