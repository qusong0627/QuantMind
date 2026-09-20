/** 评估中心类型（FE-E：对齐后端 /api/v1/eval/* 契约） */

export interface EvalDimension {
  label?: string;
  score?: number | null;
  weight?: number;
  red_line_failed?: boolean;
  detail?: Record<string, unknown>;
}

export interface EvalScoreRow {
  object_type: string;
  object_id: string;
  /** 人话名（后端尽力解析：因子词典/模型元数据/回测配置/账户用户名；无 → null 由前端回退 object_id） */
  display_name?: string | null;
  snapshot_date: string | null;
  score: number | null;
  grade: string | null;
  low_confidence: boolean;
  red_line_failed: string[];
  dimensions: Record<string, EvalDimension>;
  inputs_version: Record<string, unknown>;
  created_at: string | null;
}

export interface EvalListResponse {
  success: boolean;
  data: EvalScoreRow[];
  meta: { count: number; object_type: string };
}

export interface EvalHistoryResponse {
  success: boolean;
  data: EvalScoreRow[];
  meta: { count: number; object_type: string; object_id: string };
}

export interface EvalObjectType {
  object_type: string;
  label: string;
}

/** 长序列侧车的一个点（各序列的键不同：date/label/bucket/horizon/pair/name） */
export interface EvalSeriesPoint {
  date?: string;
  label?: string;
  name?: string;
  bucket?: number;
  horizon?: number;
  pair?: number;
  value: number;
  n_days?: number;
  is_min_segment?: boolean;
}

/** `/eval/series` 的 data（后端 factor_series / model_series 摊平产物，前端不重算） */
export interface EvalSeriesData {
  series?: Record<string, EvalSeriesPoint[]>;
  scalars?: Record<string, number | null>;
  /** 序列缺省原因（键与 series 同名）；缺省时这里是唯一解释来源，必须展示 */
  notes?: Record<string, string>;
}

export interface EvalSeriesMeta {
  object_type: string;
  object_id: string;
  /** false = 该对象尚未产出序列（正常返回，不是错误） */
  available: boolean;
  reason: string | null;
  note: string | null;
  generated_at: string | null;
  version: number | null;
}

export interface EvalSeriesResponse {
  success: boolean;
  data: EvalSeriesData;
  meta: EvalSeriesMeta;
}

export interface HealthSnapshot {
  verdict: string | null;
  verdict_label?: string | null;
  confidence: number | null;
  reasons: string[];
  suggestions: string[];
  items: Record<string, unknown>;
  backtest_id?: string | null;
  evidence_source?: string | null;
  snapshot_date: string | null;
}

export interface HealthHistoryPoint {
  snapshot_date: string | null;
  verdict: string | null;
  confidence: number | null;
  evidence_source?: string | null;
}

export interface StrategyHealthArchive {
  strategy_id: string;
  latest: HealthSnapshot | null;
  history: HealthHistoryPoint[];
  gate: { mode: string; allowed: boolean; note: string };
}

export interface StrategyHealthResponse {
  success: boolean;
  data: StrategyHealthArchive;
  meta: { count: number };
}
