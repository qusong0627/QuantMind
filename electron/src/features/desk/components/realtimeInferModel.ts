/** 实时推理状态展示模型（纯函数，可单测）：admin 配置 + 引擎状态镜像 → 展示结构。 */

export interface InferConfigView {
  enabled?: string;
  model_dir?: string;
  cadence_s?: string;
  override_whitelist?: string;
  min_live_coverage?: string;
}

export interface InferStatusView {
  updated_at?: string | null;
  counters?: Record<string, unknown>;
}

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
}

const GATE_HINT = (min: number): string =>
  min <= 0
    ? '闸门关闭（接受 T-1 基线口径）'
    : `覆盖率 < ${Math.round(min * 100)}% 不发布（防伪实时）`;

function _num(v: unknown, d = 0): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
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
  const updatedAt = status?.updated_at ? new Date(String(status.updated_at)).getTime() : 0;
  const enabled = String(config?.enabled || '').toLowerCase() === 'true';
  const onnxReady = modelOnnx && modelDir ? !!modelOnnx.ready : null;
  const onnxText =
    onnxReady === null
      ? '—'
      : onnxReady
        ? `已就绪${_sizeText(modelOnnx?.size_bytes) ? `（${_sizeText(modelOnnx?.size_bytes)}）` : ''}`
        : '缺失（首次运行自动导出）';
  return {
    available: !!config,
    needAdmin,
    enabled,
    modelDir,
    modelName,
    modelDisplayName: displayName || modelName,
    cadenceS: _num(config?.cadence_s, 15),
    minCoverage,
    coverageText,
    published: _num(counters['published']),
    scores: _num(counters['scores']),
    skippedNoLive: _num(counters['skipped_no_live']),
    lastSkip: String(counters['last_skip'] || ''),
    lastError: String(counters['last_error'] || ''),
    lastCycleAt: String(counters['last_cycle_at'] || ''),
    // 镜像超过 5 分钟未更新 = 引擎循环可能不在跑（如实提示，不猜）
    staleMirror: updatedAt > 0 ? nowMs - updatedAt > 5 * 60 * 1000 : false,
    onnxReady,
    onnxText,
  };
}

export function gateHint(minCoverage: number): string {
  return GATE_HINT(minCoverage);
}
