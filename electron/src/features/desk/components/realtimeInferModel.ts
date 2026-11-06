/** 实时推理状态展示模型（纯函数，可单测）：admin 配置 + 引擎状态镜像 → 展示结构。
 *
 * 面板要回答的是运维四问：**活着吗**（心跳 vs 节拍）、**跑多慢**（本周期时延 + p95
 * 对节拍预算）、**在降级吗**（治理器档位与生效节拍）、**能复现吗**（台账条数/写错数 +
 * run_id + 基线取数面）。此前只摆了 4 个计数器，看不出上面任何一条。
 */

import { fmtBeijingClock } from '../../../utils/timeBeijing';

export interface InferConfigView {
  enabled?: string;
  model_dir?: string;
  cadence_s?: string;
  override_whitelist?: string;
  min_live_coverage?: string;
  /** T-RT-04 影子白名单（逗号分隔；空 = 未开启） */
  shadow_whitelist?: string;
  sample_interval_s?: string;
}

/** 引擎侧治理器快照（引擎进程内状态镜像）。字段缺失是常态，一律容错。 */
export interface InferGovernorView {
  level?: number;
  base_cadence_s?: number;
  effective_cadence_s?: number;
  degraded?: boolean;
  cycles?: number;
  degradations?: number;
  recoveries?: number;
  last_ms?: number | null;
  /** 近窗 p95 时延（ms）；样本不足为 null */
  p95_ms?: number | null;
  /** 单调时钟，**不是**墙钟——不要渲染成时间 */
  level_since?: number | null;
}

/** 影子摘要（T-RT-04）：扩展白名单重算 vs 生产分的运行面证据。 */
export interface InferShadowView {
  ts?: number;
  whitelist?: string[];
  n?: number;
  rank_corr?: number | null;
  mean_abs_diff?: number | null;
  top_overlap?: number | null;
  top_n?: number | null;
  overridden?: number;
}

export interface InferStatusView {
  updated_at?: string | null;
  counters?: Record<string, unknown>;
  governor?: InferGovernorView | null;
  shadow?: InferShadowView | null;
}

export type InferHeartbeat = 'live' | 'stale' | 'skewed' | 'missing';
export type InferDegradeTone = 'ok' | 'warn' | 'bad';

/** 当前模型 ONNX 产物状态（GET /infer/config 附带；ready=false 时首次运行会自动导出） */
export interface ModelOnnxStatus {
  ready: boolean;
  path?: string;
  size_bytes?: number | null;
  mtime?: string | null;
}

export interface InferViewState {
  available: boolean;
  /** 需管理员（403）——非管理员打开交易台时的诚实降级 */
  needAdmin: boolean;
  enabled: boolean;
  /** 配置里的完整模型目录（选择器的 value） */
  modelDir: string;
  /** 目录基名（mdl_cn_train_…，中文名缺失时的回落） */
  modelName: string;
  /** 中文显示名（qm_user_models 注册表；缺失回落 modelName） */
  modelDisplayName: string;
  cadenceS: number;
  minCoverage: number;
  coverageText: string;
  published: number;
  scores: number;
  skippedNoLive: number;
  lastSkip: string;
  lastError: string;
  lastCycleAt: string;
  staleMirror: boolean;
  /** ONNX 就绪三态：true/false/null（未取到状态） */
  onnxReady: boolean | null;
  onnxText: string;

  // ── 运行面（心跳 / 时延 / 治理 / 台账）────────────────────────────
  /** 心跳：live 按节拍在跳 / stale 停更 / skewed 镜像时间超前（时钟偏差）/ missing 从未上报 */
  heartbeat: InferHeartbeat;
  /** 距上次镜像的秒数；missing 与 skewed 为 null（负数没有意义） */
  heartbeatAgeS: number | null;
  heartbeatText: string;
  /** 镜像写入时刻（本地 hh:mm:ss）；无镜像为 '—' */
  mirrorUpdatedText: string;
  /** 判断心跳用的节拍（治理器生效值优先——降载后按新节拍判活，不冤枉慢下来的循环） */
  effectiveCadenceS: number;
  degradeLevel: number;
  degradeTone: InferDegradeTone;
  degradeText: string;
  /** 本周期耗时（ms 文本）；无数据 '—' */
  lastMsText: string;
  /** 近窗 p95 时延文本；有治理器但样本不足为「样本不足」，无治理器为 '—' */
  p95Text: string;
  /** p95 已逼近节拍预算（余量 < 20%）——串轮前兆 */
  p95OverBudget: boolean;
  cycles: number;
  lastScores: number;
  /** 「已发布/总周期」——发布率才是闸门有没有在拦的真相 */
  publishRateText: string;
  ledgerEntries: number;
  ledgerErrors: number;
  ledgerText: string;
  /** 本次运行标识（回放验收用同 run_id 对齐） */
  lastRunId: string;
  /** 基线取数面（模型 metadata.data_source 决定；回放复现的口径说明） */
  baselineSource: string;
  /** T-RT-02 行情前进校验：相比上一周期量价有变化的标的数 / 可比对总数 */
  quoteAdvanceText: string;
  /** 热集里服务启动/日切以来一次快照都没见过的只数（从未写入 vs 写过又过期） */
  neverSeenText: string;
  /** T-RT-04 影子白名单运行面文案 */
  shadowText: string;
}

const GATE_HINT = (min: number): string =>
  min <= 0
    ? '闸门关闭（接受 T-1 基线口径）'
    : `覆盖率 < ${Math.round(min * 100)}% 不发布（防伪实时）`;

/** 心跳阈值系数：镜像超过 N 个「节拍 + 计算」周期未更新 = 没按节拍在跑。 */
export const HEARTBEAT_STALE_FACTOR = 2;
/** 面板轮询间隔（毫秒）：必须明显小于判活阈值，否则数据显示滞后本身会被误判成停更。 */
export const HEARTBEAT_POLL_INTERVAL_MS = 10000;
const HEARTBEAT_POLL_SLACK_S = HEARTBEAT_POLL_INTERVAL_MS / 1000;
/** 未来时间容差：几秒抖动属正常，超过即判时钟偏差（那会冤枉一个健康的服务）。 */
const CLOCK_SKEW_TOLERANCE_S = 5;
/** 时延预算余量：p95 超过节拍预算的 80% 即报警——留不出余量，下一轮就会串在一起。 */
const LATENCY_BUDGET_RATIO = 0.8;

/**
 * 心跳判活阈值（秒）：`2 × (节拍 + 本周期耗时) + 轮询间隔`。
 *
 * 为什么不是 `2 × 节拍`：镜像每轮在 `tick_once` 末尾写，两轮间隔 = 节拍（等待）+
 * 本周期耗时（实测 ~7s）。只按节拍判活时，正常运行时显示年龄就会周期性越过 30s；
 * 再叠加面板 30s 的轮询滞后，面板会定期误报「心跳停更」。
 * 本周期耗时按节拍封顶——一次异常长周期不该把停更判据永久放宽。
 */
export function heartbeatStaleAfterS(cadenceS: number, lastCycleS: number): number {
  const cadence = Math.max(1, cadenceS);
  const cycle = Math.min(Math.max(0, lastCycleS), cadence);
  return HEARTBEAT_STALE_FACTOR * (cadence + cycle) + HEARTBEAT_POLL_SLACK_S;
}

function _num(v: unknown, d = 0): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

/** 缺失与 0 必须分开：面板上的「—」是「不知道」，不是「零」。 */
function _numOrNull(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** 毫秒 → 人读时延：< 1s 用 ms，≥ 1s 用 s（一位小数） */
function _msText(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/** 秒 → 人读时长（心跳年龄用） */
function _ageText(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

function _hms(ms: number): string {
  // 北京口径（`timeBeijing`）：该值来自后端 epoch 毫秒，设备时区无关地换算北京墙钟。
  return fmtBeijingClock(ms, { withSeconds: true });
}

function _sizeText(bytes: number | null | undefined): string {
  if (!Number.isFinite(Number(bytes)) || Number(bytes) <= 0) return '';
  const kb = Number(bytes) / 1024;
  return kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.round(kb)} KB`;
}

export function inferViewState(
  config: InferConfigView | null,
  status: InferStatusView | null,
  nowMs: number = Date.now(),
  needAdmin = false,
  modelOnnx: ModelOnnxStatus | null = null,
  modelDisplayName = '',
  governor: InferGovernorView | null = null,
  baselineSource = '',
): InferViewState {
  const counters = (status?.counters || {}) as Record<string, unknown>;
  const minCoverage = _num(config?.min_live_coverage, 0.5);
  const coverage = counters['last_live_coverage'];
  const coverageText =
    coverage === null || coverage === undefined
      ? '—'
      : `${Math.round(_num(coverage) * 100)}%`;
  const modelDir = String(config?.model_dir || '');
  const modelName = modelDir ? modelDir.split('/').filter(Boolean).pop() || modelDir : '未配置';
  const displayName = String(modelDisplayName || '').trim();
  const updatedRaw = status?.updated_at ? new Date(String(status.updated_at)).getTime() : 0;
  const updatedAt = Number.isFinite(updatedRaw) ? updatedRaw : 0;
  const enabled = String(config?.enabled || '').toLowerCase() === 'true';
  const onnxReady = modelOnnx && modelDir ? !!modelOnnx.ready : null;
  const onnxText =
    onnxReady === null
      ? '—'
      : onnxReady
        ? `已就绪${_sizeText(modelOnnx?.size_bytes) ? `（${_sizeText(modelOnnx?.size_bytes)}）` : ''}`
        : '缺失（首次运行自动导出）';

  // ── 治理器（引擎进程内状态的镜像；缺字段一律容错，绝不让面板少一格变成整卡 500）──
  const hasGovernor = !!governor && typeof governor === 'object';
  const govCadence = Number(governor?.effective_cadence_s);
  const cadenceS = _num(config?.cadence_s, 15);
  const effectiveCadenceS =
    hasGovernor && Number.isFinite(govCadence) && govCadence > 0 ? govCadence : cadenceS;
  const govLevel = Number(governor?.level);
  const degradeLevel = Number.isFinite(govLevel) ? govLevel : _num(counters['degrade_level']);
  const degradeText =
    degradeLevel <= 0 ? '正常' : degradeLevel === 1 ? '轻度降载' : '重度降载（节拍已放慢）';
  const degradeTone: InferDegradeTone =
    degradeLevel <= 0 ? 'ok' : degradeLevel === 1 ? 'warn' : 'bad';

  // 本周期耗时先算：心跳阈值要用它（镜像两轮之间的真实间隔 = 节拍 + 计算）。
  const lastMsRaw = counters['last_ms'];
  const lastMsNum = Number(lastMsRaw);
  const lastCycleS =
    lastMsRaw === null || lastMsRaw === undefined || !Number.isFinite(lastMsNum) || lastMsNum <= 0
      ? 0
      : lastMsNum / 1000;

  // 心跳：阈值跟随**生效**节拍——降载是设计内的放慢，按原节拍判活会把好循环判死。
  // 再叠加本周期耗时与轮询滞后：只按 2×节拍会在健康运行时周期性误报「停更」。
  const heartbeatFactorS = Math.max(1, effectiveCadenceS);
  const staleAfterS = heartbeatStaleAfterS(effectiveCadenceS, lastCycleS);
  const ageS = updatedAt > 0 ? (nowMs - updatedAt) / 1000 : null;
  const heartbeat: InferHeartbeat =
    ageS === null
      ? 'missing'
      : ageS < -CLOCK_SKEW_TOLERANCE_S
        ? 'skewed'
        : ageS > staleAfterS
          ? 'stale'
          : 'live';
  const heartbeatText =
    heartbeat === 'missing'
      ? '无状态镜像（引擎从未上报或已被 TTL 清掉）'
      : heartbeat === 'skewed'
        ? `镜像时间超前 ${_ageText(Math.abs(ageS ?? 0))} · 两侧时钟不同步（时效判断暂不可信）`
        : heartbeat === 'stale'
          ? `镜像 ${_ageText(ageS ?? 0)} 未更新（节拍 ${heartbeatFactorS}s + 本周期 ${lastCycleS.toFixed(1)}s，阈值 ${Math.round(staleAfterS)}s）· 循环可能已停，或两侧时钟不同步`
          : `心跳 ${_ageText(ageS ?? 0)}前 · 节拍 ${heartbeatFactorS}s`;

  // 时延：本周期（counters.last_ms）与近窗 p95（治理器）。p95 无样本时如实说、
  // 不写 0 —— 0 与「样本不足」在运维眼里是两件完全相反的事。
  const lastMsText =
    lastMsRaw === null || lastMsRaw === undefined || !Number.isFinite(lastMsNum)
      ? '—'
      : _msText(lastMsNum);
  const p95Raw = governor?.p95_ms;
  const p95Num = Number(p95Raw);
  const hasP95 = p95Raw !== null && p95Raw !== undefined && Number.isFinite(p95Num) && p95Num > 0;
  const p95Text = hasP95 ? _msText(p95Num) : hasGovernor ? '样本不足' : '—';
  const p95OverBudget = hasP95 && p95Num > effectiveCadenceS * 1000 * LATENCY_BUDGET_RATIO;

  const cycles = _num(counters['cycles']);
  const publishedCount = _num(counters['published']);
  const ledgerEntries = _num(counters['ledger_entries']);
  const ledgerErrors = _num(counters['ledger_errors']);

  // T-RT-02：量价三元组与上一轮都相同 = 冻结（覆盖率只证"喂数进程活着"，
  // 这里补"价格有没有在动"）。只显示、不参与任何发布判断。
  const quoteCompared = _numOrNull(counters['quote_compared']);
  const quoteAdvanced = _numOrNull(counters['quote_advanced']);
  const quoteFrozen = _numOrNull(counters['quote_frozen']);
  const quoteAdvanceText =
    quoteCompared === null || quoteCompared === 0
      ? '—'
      : `前进 ${quoteAdvanced ?? 0}/${quoteCompared} · 冻结 ${quoteFrozen ?? 0}`;
  const hotSymbols = _numOrNull(counters['hot_symbols']);
  const neverSeen = _numOrNull(counters['never_seen']);
  // 统计窗口必须写明：服务重启/日切后集合清零，不写「自几点起」会把「刚重启」误读成
  // 「全热集都没行情」——2026-11-06 收盘后重启即出现 597/597。
  const neverSeenSince = String(counters['never_seen_since'] || '');
  const neverSeenSinceText = neverSeenSince
    ? `自 ${fmtBeijingClock(Date.parse(neverSeenSince), { withSeconds: false })} 起 `
    : '';
  const neverSeenText =
    hotSymbols === null
      ? '—'
      : `${neverSeenSinceText}${neverSeen ?? 0}/${hotSymbols} 只从未见快照`;

  // T-RT-04：影子白名单（只重算不发布）。未开启 / 已开启未到首轮 / 有摘要 三态。
  const shadowEnabled = String(config?.shadow_whitelist || '').trim() !== '';
  const shadow = status?.shadow ?? null;
  const shadowText = !shadowEnabled
    ? '未开启'
    : !shadow
      ? '已开启 · 等待首轮'
      : `秩相关 ${shadow.rank_corr ?? '—'} · Top${shadow.top_n ?? 20} 重合 ${shadow.top_overlap ?? '—'} · Δ${shadow.mean_abs_diff ?? '—'}`;

  return {
    available: !!config,
    needAdmin,
    enabled,
    modelDir,
    modelName,
    modelDisplayName: displayName || modelName,
    cadenceS,
    minCoverage,
    coverageText,
    published: publishedCount,
    scores: _num(counters['scores']),
    skippedNoLive: _num(counters['skipped_no_live']),
    lastSkip: String(counters['last_skip'] || ''),
    lastError: String(counters['last_error'] || ''),
    lastCycleAt: String(counters['last_cycle_at'] || ''),
    // 与心跳同源（旧的「超 5 分钟」是另一套阈值，会与心跳打架 → 只留一个判据）
    staleMirror: heartbeat === 'stale',
    onnxReady,
    onnxText,

    heartbeat,
    heartbeatAgeS: heartbeat === 'live' || heartbeat === 'stale' ? Math.round(ageS ?? 0) : null,
    heartbeatText,
    mirrorUpdatedText: updatedAt > 0 ? _hms(updatedAt) : '—',
    effectiveCadenceS,
    degradeLevel,
    degradeTone,
    degradeText,
    lastMsText,
    p95Text,
    p95OverBudget,
    cycles,
    lastScores: _num(counters['last_scores']),
    publishRateText: cycles > 0 ? `${publishedCount}/${cycles} 周期` : '—',
    ledgerEntries,
    ledgerErrors,
    ledgerText: `${ledgerEntries} 条${ledgerErrors > 0 ? ` · ${ledgerErrors} 错` : ''}`,
    lastRunId: String(counters['last_run_id'] || ''),
    baselineSource: String(baselineSource || '').trim(),
    quoteAdvanceText,
    neverSeenText,
    shadowText,
  };
}

export function gateHint(minCoverage: number): string {
  return GATE_HINT(minCoverage);
}
