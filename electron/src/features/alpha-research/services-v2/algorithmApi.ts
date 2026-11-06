import { apiClient } from "../../../services/aiStrategyClients";
import type { ApiResponse, StatisticalMiningOptions } from "../types-v2";

export interface MiningAlgorithmOption {
  key: "rdagent" | "statistical";
  name: string;
  enabled: boolean;
  valid: boolean;
  markets: string[];
  freqs: string[];
  uses_llm: boolean;
  uses_datasets: boolean;
  requires_gpu: boolean;
  supports_direction_batches?: boolean;
  supports_sampled_direction_batches?: boolean;
  supports_direction_quota?: boolean;
  supports_experience_memory?: boolean;
  supports_variant_ledger?: boolean;
  statistical_defaults?: StatisticalMiningOptions;
}

export interface MiningDatasetOption {
  key: string;
  name: string;
  markets: string[];
  freqs: string[];
  available?: boolean;
  column_count?: number;
  availability_reason?: string;
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.every((item: unknown) => typeof item === "string")
  );
}

function isMiningAlgorithmOption(
  value: unknown,
): value is MiningAlgorithmOption {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    (item.key === "rdagent" || item.key === "statistical") &&
    typeof item.name === "string" &&
    item.valid === true &&
    item.enabled === true &&
    isStringArray(item.markets) &&
    item.markets.includes("a_share") &&
    isStringArray(item.freqs) &&
    item.freqs.includes("day") &&
    typeof item.uses_llm === "boolean" &&
    typeof item.uses_datasets === "boolean" &&
    typeof item.requires_gpu === "boolean"
  );
}

export async function getMiningAlgorithms(): Promise<
  ApiResponse<{ algorithms: MiningAlgorithmOption[]; available: boolean }>
> {
  try {
    const response = await apiClient.get<{ data?: { algorithms?: unknown } }>(
      "/alpha-agent/algorithms",
    );
    const raw = response.data?.data?.algorithms;
    if (!Array.isArray(raw)) {
      return { success: true, data: { algorithms: [], available: false } };
    }
    const algorithms = (raw as unknown[]).filter(isMiningAlgorithmOption);
    return {
      success: true,
      data: { algorithms, available: algorithms.length > 0 },
    };
  } catch {
    return { success: true, data: { algorithms: [], available: false } };
  }
}

export async function getMiningDatasets(): Promise<
  ApiResponse<{ datasets: MiningDatasetOption[]; available: boolean }>
> {
  try {
    const response = await apiClient.get<{ data?: { datasets?: unknown } }>(
      "/alpha-agent/datasets",
    );
    const raw = response.data?.data?.datasets;
    if (!Array.isArray(raw)) {
      return { success: true, data: { datasets: [], available: false } };
    }
    const datasets = raw.filter((value): value is MiningDatasetOption => {
      if (!value || typeof value !== "object") return false;
      const item = value as Record<string, unknown>;
      return typeof item.key === "string" && typeof item.name === "string" &&
        isStringArray(item.markets) && item.markets.includes("a_share") &&
        isStringArray(item.freqs) && item.freqs.includes("day");
    });
    return {
      success: true,
      data: { datasets, available: datasets.length > 0 },
    };
  } catch {
    return { success: true, data: { datasets: [], available: false } };
  }
}
