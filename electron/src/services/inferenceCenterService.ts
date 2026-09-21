import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';

export interface FeatureDriverItem {
  name: string;
  category?: string;
  value?: number;
  impact: number;
  direction: 'positive' | 'negative';
}

export interface ModelConsensusItem {
  model_id: string;
  model_name: string;
  model_type: string;
  score: number;
  expected_return: number;
  rating: 'STRONG_BUY' | 'BUY' | 'HOLD' | 'SELL';
  horizon: number;
}

export interface ForecastPoint {
  step: number;
  date: string;
  p10: number;
  p50: number;
  p90: number;
  predicted_price: number;
  upper_price: number;
  lower_price: number;
}

export interface SingleStockPredictionResponse {
  status: string;
  symbol: string;
  stock_name: string;
  model_id: string;
  model_name: string;
  model_type: string;
  as_of_date: string;
  current_price: number;
  /** 模型**实际**训练周期（分数语义周期），未必等于请求周期 */
  horizon: number;
  /** 调用方请求的周期；与 horizon 不一致时 horizon_warning 非空 */
  requested_horizon?: number;
  horizon_warning?: string | null;
  /** 该市场现存模型覆盖的周期分布（周期选择器据此渲染可用性） */
  available_horizons?: { horizon: number; model_count: number }[];
  predicted_score: number;
  expected_return: number;
  confidence: number;
  rating: 'STRONG_BUY' | 'BUY' | 'HOLD' | 'SELL';
  /**
   * 截面分位（0–1）：该标的在**同一模型、同一交易日**的截面内所处位置。
   * 这是对外展示的唯一分数刻度（`features/shared/researchScore.ts` 换算成 0–100）。
   * 取不到截面时后端返回 null —— UI 显示「—」，**不得显示成 0**。
   */
  rank_pct?: number | null;
  p10_return: number | null;
  p50_return: number;
  p90_return: number | null;
  forecast_curve: ForecastPoint[];
  forecast_warning?: string | null;
  /**
   * 区间口径：
   * - `model_quantile` = 模型自带分位头（真正的分位数预测）
   * - `realized_vol` = 由已实现波动率推算的波动率锥（**统计口径，非模型分位数**）
   * UI 必须按此换标题，不得把波动率锥渲染成「模型分位预测」。
   */
  forecast_basis?: 'model_quantile' | 'realized_vol' | null;
  /** 口径说明（后端生成，面向用户的一句话解释） */
  forecast_note?: string | null;
  /** 日波动率（小数），波动率锥的输入，用于展示与复核 */
  daily_vol_pct?: number;
  drivers: FeatureDriverItem[];
  /**
   * 归因取不到时的原因说明（drivers 非空时仍可能带说明：例如归因来自替代模型
   * 或元数据是从注册表恢复的）。UI 必须渲染它——空白归因面板与「功能坏了」
   * 在用户看来没有区别。
   */
  drivers_note?: string | null;
  /** 归因实际描述的模型；与 model_id 不同表示用了同批次的替代树模型。 */
  drivers_model_id?: string | null;
  drivers_model_name?: string | null;
  consensus: ModelConsensusItem[];
  consensus_score: number;
  /**
   * 共识覆盖度。`scored` 很小时「看多占比」是单模型观点而非共识，
   * UI 必须据此改措辞（不能只甩一个百分比）。
   */
  consensus_coverage?: {
    scored: number;
    total: number;
    trade_date?: string | null;
    is_thin?: boolean;
    /** 未参与共识的原因分布，运维排查用 */
    skip_reasons?: Record<string, number>;
    /** 点名后仍未算出分数的模型及失败步骤（error 为机器码，detail 为原始报错） */
    failed_models?: { model_id: string; error: string; detail?: string }[];
  } | null;
  /** 覆盖不足时后端生成的说明句；通常为空 */
  consensus_note?: string | null;
  /** 仅为真实持久化模型推理分数。 */
  data_source?: 'persisted';
  /** 仅在模型可提供时返回真实 SHAP。 */
  drivers_source?: 'shap';
  error?: string | null;
}

export interface SingleStockPredictionRequest {
  symbol: string;
  model_id?: string;
  date?: string;
  horizon?: number;
  market?: string;
  /** 共识矩阵成员（最多4个真实模型）；空=自动取当日全部有分数模型 */
  consensus_model_ids?: string[];
  /** 是否立即执行已注册模型；false 时仅读取已有真实结果。 */
  execute?: boolean;
}

export interface KlineItem {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface AvailableModelOption {
  modelId: string;
  modelName: string;
  modelType: string;
  description?: string;
  accuracy?: number;
  isEnsemble?: boolean;
  hasInference?: boolean;
  /** 训练周期 T+N（来自 metadata.target_horizon_days）；缺失表示老模型未记录 */
  horizon?: number | null;
  /** 目标口径（return / rank / …）；非 return 时区间数值不是收益率 */
  targetMode?: string;
}

class InferenceCenterService {
  private get client(): AxiosInstance {
    const baseURL = resolveWebSafeServiceBase(
      (import.meta as any).env?.VITE_USER_API_URL,
      SERVICE_ENDPOINTS.API_GATEWAY || SERVICE_ENDPOINTS.USER_SERVICE,
    );
    const client = axios.create({
      baseURL,
      // 实际模型执行会跑完整个推理批次，30 秒不足以覆盖生产模型冷启动与落库。
      // execute=true 且点名了共识模型时，后端要现场跑完主模型 + 最多 4 个共识模型
      // （实测 4 个树模型并发约 77s，含深度学习模型更久），120s 会把正常请求打成
      // 「超时」——而服务端仍在算，用户看到失败但分数其实已出，是更坏的结果。
      timeout: 300000,
    });
    client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        if (config.headers && typeof config.headers.set === 'function') {
          config.headers.set('Authorization', `Bearer ${token}`);
        } else if (config.headers) {
          config.headers['Authorization'] = `Bearer ${token}`;
        }
      }
      const tenantId = authService.getTenantId?.() || 'default';
      if (config.headers && typeof config.headers.set === 'function') {
        if (!config.headers.has('X-Tenant-Id') && !config.headers.has('x-tenant-id')) {
          config.headers.set('X-Tenant-Id', tenantId);
        }
      } else if (config.headers) {
        if (!config.headers['X-Tenant-Id'] && !config.headers['x-tenant-id']) {
          config.headers['X-Tenant-Id'] = tenantId;
        }
      }
      return config;
    });
    return client;
  }

  async getAvailableModels(market?: string): Promise<AvailableModelOption[]> {
    try {
      const params: Record<string, string> = {};
      if (market) params.market = market;
      const resp = await this.client.get('/research/models', { params });
      const body = (resp.data ?? {}) as any;
      const items =
        body?.data?.models ??
        body?.data?.items ??
        body?.models ??
        body?.items ??
        [];
      return items.map((m: any) => ({
        modelId: m.modelId ?? m.model_id,
        modelName: m.name || m.modelName || m.model_name || m.modelId,
        modelType: m.modelType || m.model_type || '',
        description: m.description,
        accuracy: m.ic ?? m.accuracy ?? m.ic_value,
        hasInference: m.hasInference ?? m.has_inference ?? false,
        // 周期与口径必须透传：周期选择器与「分数含义」标注都以它为准
        horizon: m.horizon ?? m.target_horizon_days ?? null,
        targetMode: m.targetMode || m.target_mode || '',
      }));
    } catch (e) {
      console.warn('获取可用模型列表失败:', e);
      return [];
    }
  }

  async getStockKline(symbol: string, days: number = 60, endDate?: string, startDate?: string): Promise<KlineItem[]> {
    try {
      const params = new URLSearchParams({ days: String(days) });
      // 指标口径用 endDate 按基准日截断（防前视泄露）；图表验证用 startDate
      // 拉取基准日之前窗口到最新的全量，展示基准日后实际走势对照预测
      if (endDate) params.set('end_date', endDate);
      if (startDate) params.set('start_date', startDate);
      const resp = await this.client.get<{ code: number; data: { items: KlineItem[] } }>(`/research/kline/${encodeURIComponent(symbol)}?${params.toString()}`);
      return resp.data?.data?.items || [];
    } catch (e) {
      console.warn('获取股票K线失败:', e);
      return [];
    }
  }

  async predictSingleStock(req: SingleStockPredictionRequest): Promise<SingleStockPredictionResponse> {
    const resp = await this.client.post<{ code?: number; data?: SingleStockPredictionResponse } | SingleStockPredictionResponse>('/research/predict-stock', req);
    if ((resp.data as any)?.data) {
      return (resp.data as any).data;
    }
    return resp.data as SingleStockPredictionResponse;
  }
}

export const inferenceCenterService = new InferenceCenterService();
