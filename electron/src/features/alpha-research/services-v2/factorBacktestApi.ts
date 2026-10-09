/**
 * 回测中心服务层（T-FB-12..15）——`/api/v1/factor-backtest/*`（engine 服务）。
 *
 * 与 `api.ts` 的老回测面（alpha-agent 快速回测）**刻意分开**：老面是
 * 「一个因子库过一遍模型回测」，本面是「因子 × 市场截面 IC 求值」的矩阵
 * 数据面。两边共用同一张台账表，但读写契约不同，混在一起会让矩阵语义
 * 被旧口径污染。
 *
 * 错误约定：沿用 alpha-research 服务层惯例——不抛异常，返回
 * `{ success, data?, error? }`；错误原文透传（归属 404 的 detail、
 * 后端 message），绝不静默空表。
 */
import { apiClient } from '../../../services/aiStrategyClients';
import type { ApiResponse } from '../types-v2';
import type {
  BatchLaunchResult,
  BatchListItem,
  BatchSkippedItem,
  BatchStatus,
  BatchUnit,
  LedgerRun,
  MarketInfo,
  MatrixCell,
  MatrixFactorRow,
  MatrixMarketCol,
  MatrixResult,
  RunReport,
  RunReportResult,
  RunSeries,
  RunSeriesResult,
} from '../types-v2/backtestCenter';

const BASE = '/factor-backtest';

function ok<T>(data: T): ApiResponse<T> {
  return { success: true, data };
}

function fail<T>(error: string): ApiResponse<T> {
  return { success: false, error };
}

/** axios 错误 → 可展示原文（detail 优先，其次 message） */
function errorText(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: unknown };
  const detail = e?.response?.data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (typeof e?.message === 'string' && e.message) return e.message;
  return fallback;
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

// ── 市场档案 ─────────────────────────────────────────────────────────

export async function listBacktestMarkets(): Promise<ApiResponse<MarketInfo[]>> {
  try {
    const res = await apiClient.get(`${BASE}/markets`);
    const rows: any[] = res.data?.data?.markets ?? [];
    const markets: MarketInfo[] = rows.map((r) => ({
      market: r.market ?? '',
      qlibMarket: r.qlib_market ?? '',
      label: r.label ?? r.market ?? '',
      inSample: !!r.in_sample,
      experimental: !!r.experimental,
      note: str(r.note),
      ready: !!r.ready,
      calendarStart: str(r.calendar_start),
      calendarEnd: str(r.calendar_end),
      instruments: num(r.instruments),
      columns: Array.isArray(r.columns) ? r.columns : [],
      universeMode: r.universe_mode ?? '',
      defaultUniverse: str(r.default_universe),
      universeTopN: num(r.universe_top_n),
      windowYears: num(r.window_years) ?? 0,
      costBps: num(r.cost_bps) ?? 0,
      benchmark: str(r.benchmark),
      minDays: num(r.min_days) ?? 0,
    }));
    return ok(markets);
  } catch (err) {
    return fail(errorText(err, '查询市场档案失败'));
  }
}

// ── 适配矩阵 ─────────────────────────────────────────────────────────

function mapCell(raw: any): MatrixCell {
  const metrics: Record<string, number | null> = {};
  const rawMetrics =
    raw && typeof raw.metrics === 'object' && raw.metrics !== null ? raw.metrics : {};
  for (const [k, v] of Object.entries(rawMetrics)) {
    if (typeof v === 'number' && Number.isFinite(v)) metrics[k] = v;
    else if (v === null) metrics[k] = null;
  }
  // 排序便利键合并进同一字典（后端已做「metrics 主源→台账专列回落」，这里
  // 只把顶层六键摊平，UI 侧读一个地方）
  for (const key of ['ic', 'rank_ic', 'icir', 'sharpe', 'ann_return_net', 'max_drawdown']) {
    const v = raw?.[key];
    if (metrics[key] == null && typeof v === 'number' && Number.isFinite(v)) {
      metrics[key] = v;
    }
  }
  return {
    status: raw?.status ?? 'not_run',
    runId: str(raw?.run_id),
    compat: raw?.compat ?? 'unknown',
    missing: Array.isArray(raw?.missing) ? raw.missing : [],
    dynamic: !!raw?.dynamic,
    error: str(raw?.error),
    universe: str(raw?.universe),
    dateRange: str(raw?.date_range),
    finishedAt: str(raw?.finished_at),
    inSample: !!raw?.in_sample,
    metrics,
  };
}

export async function fetchMatrix(params: {
  factorIds: string[];
  markets?: string[] | null;
}): Promise<ApiResponse<MatrixResult>> {
  try {
    const body: Record<string, unknown> = { factor_ids: params.factorIds };
    if (params.markets && params.markets.length > 0) body.markets = params.markets;
    const res = await apiClient.post(`${BASE}/matrix`, body);
    const data = res.data?.data ?? {};
    const markets: MatrixMarketCol[] = (data.markets ?? []).map((m: any) => ({
      market: m.market ?? '',
      label: m.label ?? m.market ?? '',
      inSample: !!m.in_sample,
      experimental: !!m.experimental,
      benchmark: str(m.benchmark),
      costBps: num(m.cost_bps) ?? 0,
    }));
    const factors: MatrixFactorRow[] = (data.factors ?? []).map((f: any) => {
      const cells: Record<string, MatrixCell> = {};
      for (const [m, c] of Object.entries(f.cells ?? {})) {
        cells[m] = mapCell(c);
      }
      return {
        factorId: f.factor_id ?? '',
        factorName: str(f.factor_name),
        found: !!f.found,
        owned: !!f.owned,
        cnIc: num(f.cn_ic),
        cells,
      };
    });
    return ok({ markets, factors, counts: data.counts ?? {} });
  } catch (err) {
    return fail(errorText(err, '查询适配矩阵失败'));
  }
}

// ── 批量派发 / 进度 / 取消 ──────────────────────────────────────────

function mapSkipped(raw: any): BatchSkippedItem[] {
  return Array.isArray(raw)
    ? raw.map((s: any) => ({
        factorId: s?.factor_id ?? '',
        market: str(s?.market),
        reason: s?.reason ?? '',
      }))
    : [];
}

export async function launchBatch(params: {
  factorIds: string[];
  markets?: string[] | null;
  start?: string | null;
  end?: string | null;
  costBps?: number | null;
}): Promise<ApiResponse<BatchLaunchResult>> {
  try {
    const body: Record<string, unknown> = { factor_ids: params.factorIds };
    if (params.markets && params.markets.length > 0) body.markets = params.markets;
    if (params.start) body.start = params.start;
    if (params.end) body.end = params.end;
    if (params.costBps != null) body.cost_bps = params.costBps;
    const res = await apiClient.post(`${BASE}/batch`, body);
    const d = res.data?.data ?? {};
    return ok({
      batchId: str(d.batch_id),
      total: num(d.total) ?? 0,
      queued: num(d.queued) ?? 0,
      skipped: mapSkipped(d.skipped),
      status: d.status ?? '',
      message: d.message ?? '',
    });
  } catch (err) {
    return fail(errorText(err, '批量派发失败'));
  }
}

function mapUnit(raw: any): BatchUnit {
  return {
    factorId: raw?.factor_id ?? '',
    market: raw?.market ?? '',
    status: raw?.status ?? 'pending',
    runId: str(raw?.run_id),
    attempts: num(raw?.attempts) ?? 0,
    error: str(raw?.error),
    finishedAt: str(raw?.finished_at),
    ic: num(raw?.ic),
    rankIc: num(raw?.rank_ic),
    icir: num(raw?.icir),
    sharpe: num(raw?.sharpe),
    maxDrawdown: num(raw?.max_drawdown),
    nDays: num(raw?.n_days),
  };
}

export async function getBatchStatus(
  batchId: string,
): Promise<ApiResponse<BatchStatus>> {
  try {
    const res = await apiClient.get(`${BASE}/batch/status`, {
      params: { batch_id: batchId },
    });
    const d = res.data?.data ?? {};
    const p = d.progress ?? {};
    return ok({
      batch: {
        batchId: d.batch?.batch_id ?? batchId,
        userId: str(d.batch?.user_id),
        status: d.batch?.status ?? 'running',
        error: str(d.batch?.error),
        createdAt: str(d.batch?.created_at),
        finishedAt: str(d.batch?.finished_at),
      },
      spec: {
        factorIds: d.spec?.factor_ids ?? [],
        markets: d.spec?.markets ?? [],
        start: str(d.spec?.start),
        end: str(d.spec?.end),
        costBps: num(d.spec?.cost_bps),
        skipped: mapSkipped(d.spec?.skipped),
      },
      progress: {
        pending: num(p.pending) ?? 0,
        running: num(p.running) ?? 0,
        completed: num(p.completed) ?? 0,
        failed: num(p.failed) ?? 0,
        cancelled: num(p.cancelled) ?? 0,
        dataUnsupported: num(p.data_unsupported) ?? 0,
        insufficient: num(p.insufficient) ?? 0,
        unavailable: num(p.unavailable) ?? 0,
        total: num(p.total) ?? 0,
        done: num(p.done) ?? 0,
        consecFails: num(p.consec_fails) ?? 0,
        maxConsecFails: num(p.max_consec_fails) ?? 0,
        draining: !!p.draining,
        current: Array.isArray(p.current)
          ? p.current.map((c: any) => ({
              factorId: c?.factor_id ?? '',
              market: c?.market ?? '',
              runId: c?.run_id ?? '',
            }))
          : [],
      },
      units: Array.isArray(d.units) ? d.units.map(mapUnit) : [],
      failures: Array.isArray(d.failures) ? d.failures.map(mapUnit) : [],
    });
  } catch (err) {
    return fail(errorText(err, '查询批次进度失败'));
  }
}

export async function cancelBatch(
  batchId: string,
): Promise<ApiResponse<{ killed: number; closed: boolean }>> {
  try {
    const res = await apiClient.post(`${BASE}/batch/cancel`, { batch_id: batchId });
    const d = res.data?.data ?? {};
    return ok({ killed: num(d.killed) ?? 0, closed: !!d.closed });
  } catch (err) {
    return fail(errorText(err, '取消批次失败'));
  }
}

export async function listBatches(limit = 20): Promise<ApiResponse<BatchListItem[]>> {
  try {
    const res = await apiClient.get(`${BASE}/batches`, { params: { limit } });
    const rows: any[] = res.data?.data?.batches ?? [];
    const batches: BatchListItem[] = rows.map((b) => ({
      batchId: b.batch_id ?? '',
      userId: str(b.user_id),
      status: b.status ?? 'running',
      error: str(b.error),
      createdAt: str(b.created_at),
      finishedAt: str(b.finished_at),
      spec: {
        factorIds: b.spec?.factor_ids ?? [],
        markets: b.spec?.markets ?? [],
        start: str(b.spec?.start),
        end: str(b.spec?.end),
        costBps: num(b.spec?.cost_bps),
        skipped: mapSkipped(b.spec?.skipped),
      },
    }));
    return ok(batches);
  } catch (err) {
    return fail(errorText(err, '查询批次列表失败'));
  }
}

// ── 运行台账 / 曲线 ─────────────────────────────────────────────────

function mapRun(raw: any): LedgerRun {
  return {
    runId: raw?.run_id ?? '',
    factorId: raw?.factor_id ?? '',
    factorName: str(raw?.factor_name),
    status: raw?.status ?? '',
    kind: str(raw?.kind),
    market: str(raw?.market),
    universe: str(raw?.universe),
    dataSource: str(raw?.data_source),
    dateRange: str(raw?.date_range),
    error: str(raw?.error),
    metrics:
      raw && typeof raw.metrics === 'object' && raw.metrics !== null ? raw.metrics : {},
    hasSeries: !!raw?.has_series,
    createdAt: str(raw?.created_at),
    finishedAt: str(raw?.finished_at),
  };
}

export async function listRuns(params: {
  factorId: string;
  market?: string | null;
  status?: string | null;
  limit?: number;
  offset?: number;
}): Promise<ApiResponse<LedgerRun[]>> {
  try {
    const qs: Record<string, string | number> = {
      factor_id: params.factorId,
      limit: Math.min(Math.max(params.limit ?? 50, 1), 200),
      offset: Math.max(params.offset ?? 0, 0),
    };
    if (params.market) qs.market = params.market;
    if (params.status) qs.status = params.status;
    const res = await apiClient.get(`${BASE}/runs`, { params: qs });
    const rows: any[] = res.data?.data?.runs ?? [];
    return ok(rows.map(mapRun));
  } catch (err) {
    return fail(errorText(err, '查询回测台账失败'));
  }
}

function mapSeries(raw: any): RunSeries {
  const arr = (v: unknown): (number | null)[] =>
    Array.isArray(v)
      ? v.map((x) => (typeof x === 'number' && Number.isFinite(x) ? x : null))
      : [];
  const qCurves: Record<string, (number | null)[]> = {};
  if (raw?.q_curves && typeof raw.q_curves === 'object') {
    for (const [k, v] of Object.entries(raw.q_curves)) qCurves[k] = arr(v);
  }
  return {
    dates: Array.isArray(raw?.dates) ? raw.dates.map((d: unknown) => String(d)) : [],
    ic: arr(raw?.ic),
    icCum: arr(raw?.ic_cum),
    navLong: arr(raw?.nav_long),
    navLs: arr(raw?.nav_ls),
    navBench: arr(raw?.nav_bench),
    qCurves,
    turnover: arr(raw?.turnover),
    coverage: Array.isArray(raw?.coverage)
      ? raw.coverage.map((v: unknown) => (typeof v === 'number' ? v : 0))
      : [],
    bench: raw?.bench ?? '',
    meta: {
      costBps: num(raw?.meta?.cost_bps) ?? 0,
      topPct: num(raw?.meta?.top_pct) ?? 0,
      nBuckets: num(raw?.meta?.n_buckets) ?? 0,
      turnoverConvention: raw?.meta?.turnover_convention ?? '',
    },
  };
}

export async function getRunSeries(
  runId: string,
): Promise<ApiResponse<RunSeriesResult>> {
  try {
    const res = await apiClient.get(`${BASE}/runs/${encodeURIComponent(runId)}/series`);
    const d = res.data?.data ?? {};
    return ok({ run: mapRun(d.run ?? {}), series: mapSeries(d.series ?? {}) });
  } catch (err) {
    return fail(errorText(err, '查询回测曲线失败'));
  }
}

// ── 机构报告标量块（GET /report/{run_id}，T-FB-16） ─────────────────

function mapReport(raw: any): RunReport {
  const d = raw ?? {};
  if (!d.available) {
    // 降级出口：除 status/reason/note 文本外无任何数字
    return {
      available: false,
      status: d.status ?? '',
      reason: str(d.reason),
      note: str(d.note),
    };
  }
  const h = d.headline ?? {};
  const s = d.significance ?? {};
  const cg = d.cost_grid ?? {};
  const ex = d.excess ?? {};
  const meta = d.meta ?? {};
  const boot = s.bootstrap;
  const crowd = s.crowding;
  return {
    available: true,
    status: d.status ?? '',
    runId: str(d.run_id),
    nDays: num(d.n_days),
    headline: {
      nDays: num(h.n_days),
      muDaily: num(h.mu_daily),
      sigmaDaily: num(h.sigma_daily),
      annVol: num(h.ann_vol),
      returns: num(h.returns),
      cumReturn: num(h.cum_return),
      ir: num(h.ir),
      turnover: num(h.turnover),
      fitness: num(h.fitness),
      margin: num(h.margin),
    },
    significance: {
      plainT: num(s.plain_t),
      nwT: num(s.nw_t),
      pValue: num(s.p_value),
      qValueBhy: num(s.q_value_bhy),
      familyN: num(s.family_n) ?? 0,
      familyNote: str(s.family_note),
      dsr: num(s.dsr),
      nTrials: num(s.n_trials) ?? 0,
      nTrialsSource: s.n_trials_source ?? '',
      dsrNote: str(s.dsr_note),
      bootstrap:
        boot && typeof boot === 'object'
          ? {
              lo: num(boot.lo),
              hi: num(boot.hi),
              point: num(boot.point),
              level: num(boot.level),
              nBoot: num(boot.n_boot),
              stat: str(boot.stat),
            }
          : null,
      crowding:
        crowd && typeof crowd === 'object'
          ? {
              score: num(crowd.score),
              turnoverPct: num(crowd.turnover_pct),
              icAutocorrLag1: num(crowd.ic_autocorr_lag1),
              nDays: num(crowd.n_days),
              note: str(crowd.note),
            }
          : null,
    },
    costGrid: {
      rows: Array.isArray(cg.rows)
        ? cg.rows.map((r: any) => ({
            bps: num(r?.bps) ?? 0,
            netReturn: num(r?.net_return),
            netIr: num(r?.net_ir),
            netFitness: num(r?.net_fitness),
          }))
        : [],
      breakEvenBps: num(cg.break_even_bps),
      breakEvenNote: str(cg.break_even_note),
      defaultBps: num(cg.default_bps),
    },
    excess: {
      kind: ex.kind ?? '',
      benchmarkRef: str(ex.benchmark_ref),
      label: ex.label ?? '',
      note: ex.note ?? '',
    },
    unavailable: Array.isArray(d.unavailable)
      ? d.unavailable.map((b: any) => ({
          block: b?.block ?? '',
          reason: b?.reason ?? '',
        }))
      : [],
    meta: {
      costBps: num(meta.cost_bps),
      topPct: num(meta.top_pct),
      turnoverConvention: str(meta.turnover_convention),
      source: meta.source ?? '',
    },
  };
}

/**
 * 机构报告标量块。`nTrials` 覆盖 DSR 试次数（缺省=后端批内完成单元数）。
 * 报告端点自身对非完成终态也回 200 + `available:false`（原因在 reason），
 * 所以这里不按状态做前置拦截，按 `available` 渲染。
 */
export async function getRunReport(
  runId: string,
  nTrials?: number | null,
): Promise<ApiResponse<RunReportResult>> {
  try {
    const res = await apiClient.get(`${BASE}/report/${encodeURIComponent(runId)}`, {
      params: nTrials != null ? { n_trials: nTrials } : undefined,
    });
    const d = res.data?.data ?? {};
    return ok({ run: mapRun(d.run ?? {}), report: mapReport(d.report ?? {}) });
  } catch (err) {
    return fail(errorText(err, '查询机构报告失败'));
  }
}
