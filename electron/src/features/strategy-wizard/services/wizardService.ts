import axios from 'axios';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import type { Condition, BuyRule, SellRule, RiskConfig } from '../types';

const client = axios.create({
  baseURL: SERVICE_ENDPOINTS.API_GATEWAY,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// 添加认证 Token 拦截器
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token') || localStorage.getItem('auth_token');
  const origin = window.location.origin;

  if (token) {
    // Axios 1.7.2 推荐使用 .set() 方法
    if (config.headers && typeof config.headers.set === 'function') {
      config.headers.set('Authorization', `Bearer ${token}`);
    } else {
      config.headers.Authorization = `Bearer ${token}`;
    }
    console.log(`[WizardService] ${config.method?.toUpperCase()} ${config.url} | Header: Authorization set (Origin: ${origin})`);
  } else {
    console.warn(`[WizardService] Warning: access_token not found in localStorage (Origin: ${origin})`);
  }

  // 打印最终将要发送的 Header (部分敏感信息脱敏)
  const finalHeaders = (config.headers as any);
  console.log(`[WizardService] Final Request Headers:`, {
    ...finalHeaders,
    Authorization: finalHeaders.Authorization ? 'Bearer ******' : 'MISSING'
  });

  return config;
});

export async function parseConditions(payload: { conditions: Condition }) {
  const res = await client.post('/strategy/parse-conditions', payload);
  return res.data;
}

export async function queryPool(payload: { dsl: string }) {
  const res = await client.post('/strategy/query-pool', payload);
  return res.data;
}


export async function parseText(text: string) {
  const res = await client.post(
    '/strategy/parse-text',
    { text },
    { timeout: 120000 } // 首次解析可能触发向量预热，给足时间
  );
  return res.data;
}


export async function searchStocks(query: string) {
  try {
    const keyword = String(query || '').trim();
    if (!keyword) return [];

    // 统一走后端网关，避免浏览器直连第三方接口的 CORS 限制
    const res = await client.get('/stocks/search', {
      params: { q: keyword, limit: 20 },
    });
    const payload = res?.data || {};
    const rawList = Array.isArray(payload.results)
      ? payload.results
      : (Array.isArray(payload.data) ? payload.data : []);

    return rawList
      .map((item: any) => ({
        symbol: String(item?.code || item?.symbol || '').trim(),
        name: String(item?.name || '').trim(),
        price: undefined as number | undefined,
      }))
      .filter((s: any) => s.symbol && s.name);
  } catch (e) {
    console.error('Stock search failed', e);
    return [];
  }
}

// ========== Phase 2: New APIs ==========


/**
 * 验证Qlib策略代码
 */
export async function validateQlibCode(payload: {
  code: string;
  context?: {
    start_date?: string;
    end_date?: string;
    universe_size?: number;
  };
  mode?: 'full' | 'syntax_only';
}) {
  const res = await client.post('/strategy/validate-qlib', payload);
  return res.data;
}

/**
 * AI 修复 Qlib 策略代码（主要用于语法/结构修复）
 */
export async function repairQlibCode(payload: {
  code: string;
  error?: string;
  max_rounds?: number;
}) {
  const res = await client.post(
    '/strategy/repair-qlib',
    payload,
    { timeout: 120000 } // 修复通常也需要调用大模型
  );
  return res.data;
}


/**
 * 保存策略到云端（个人中心）
 */
export async function saveToCloud(payload: {
  user_id: string;
  strategy_name: string;
  code: string;
  metadata: Record<string, any>;
}) {
  const res = await client.post('/strategy/save-to-cloud', payload);
  return res.data;
}

/**
 * 保存股票池文件
 */
export async function savePoolFile(payload: {
  user_id: string;
  format: 'json' | 'txt' | 'csv';
  pool: Array<{ symbol: string; name?: string }>;
  pool_name: string; // Add pool_name support
}) {
  const res = await client.post('/strategy/save-pool-file', payload);
  return res.data;
}

/**
 * 删除股票池文件
 */
export async function deletePoolFile(payload: { user_id?: string; file_url?: string; file_key?: string }) {
  const res = await client.post('/strategy/delete-pool-file', payload);
  return res.data;
}

/**
 * 获取用户当前活跃的股票池文件
 */
export async function getActivePoolFile(payload: { user_id: string }) {
  const res = await client.post('/strategy/get-active-pool-file', payload);
  return res.data;
}

/**
 * 设置某个股票池为活跃状态
 */
export async function setActivePoolFile(payload: { user_id: string; file_key: string }) {
  const res = await client.post('/strategy/set-active-pool-file', payload);
  return res.data;
}

/**
 * 列出用户历史股票池（用于第二步复用）
 */
export async function listPoolFiles(payload: { user_id: string; limit?: number }) {
  const res = await client.post('/strategy/list-pool-files', payload);
  return res.data;
}

/**
 * 预览某个历史股票池（返回列表+summary）
 */
export async function previewPoolFile(payload: { user_id: string; file_key: string }) {
  const res = await client.post('/strategy/preview-pool-file', payload, { timeout: 60000 });
  return res.data;
}

/**
 * 生成Qlib策略
 */
export async function generateQlib(payload: {
  user_id: string;
  conditions: Record<string, any>;
  pool_file_key: string;
  pool_file_url?: string;
  qlib_params: {
    strategy_type: 'TopkDropout' | 'TopkWeight';
    topk: number;
    n_drop?: number;
    rebalance_days?: 1 | 3 | 5;
    rebalance_period?: 'daily' | 'weekly' | 'monthly';
  };
  custom_notes?: string;
}) {
  const timeoutMs = Number((import.meta as any)?.env?.VITE_STRATEGY_GENERATE_TIMEOUT_MS || 600000);
  const pollIntervalMs = Number((import.meta as any)?.env?.VITE_STRATEGY_GENERATE_POLL_MS || 2000);
  const startedAt = Date.now();

  const submit = await client.post('/strategy/generate-qlib/async', payload, { timeout: 30000 });
  const taskId = submit?.data?.task_id as string | undefined;
  if (!taskId) {
    throw new Error('策略生成任务提交失败：未返回 task_id');
  }

  while (Date.now() - startedAt < timeoutMs) {
    const statusResp = await client.get(`/strategy/generate-qlib/tasks/${encodeURIComponent(taskId)}`, {
      timeout: 30000,
    });
    const data = statusResp.data || {};
    const status = String(data.status || '').toLowerCase();

    if (status === 'completed') {
      return data.result || { success: false, error: '任务已完成但无结果' };
    }
    if (status === 'failed') {
      const reason = data.error || data?.result?.error || '策略生成任务失败';
      return { success: false, error: reason };
    }
    if (status === 'not_found') {
      return { success: false, error: data.error || '任务不存在或已过期' };
    }

    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }

  return { success: false, error: `策略生成超时（>${Math.floor(timeoutMs / 1000)}秒）` };
}
