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
