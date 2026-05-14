import axios from 'axios';
import { SERVICE_ENDPOINTS } from '../../../config/services';
// 按需加载 wizardService 中的网络方法，避免静态/动态导入冲突
import { getWizardUserId } from '../utils/userId';

const client = axios.create({
  baseURL: SERVICE_ENDPOINTS.API_GATEWAY,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// Reuse existing interceptor logic for auth (ideally this should be in a shared client)
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token') || localStorage.getItem('auth_token');
  if (token) {
    if (config.headers && typeof config.headers.set === 'function') {
      config.headers.set('Authorization', `Bearer ${token}`);
    } else {
      (config.headers as any).Authorization = `Bearer ${token}`;
    }
  }
  return config;
});

export interface WorkingPoolItemV2 {
  symbol: string;
  name?: string;
  marketCap?: number;
  pe?: number;
  roe?: number;
  price?: number;
}

export interface SavedPoolVersionV2 {
  id: string;
  name: string;
  stockCount: number;
  createdAt: string;
  updatedAt?: string;
}

/**
 * 选股查询并同步到后端 WorkingPool
 */
export async function fetchWorkingPoolByDsl(dsl: string): Promise<WorkingPoolItemV2[]> {
  // 1. 获取查询结果
  const { queryPool } = await import('./wizardService');
  const res = await queryPool({ dsl });
  const items = Array.isArray(res?.items) ? res.items : [];
  const mapped = items.map((x: any) => ({
    symbol: String(x?.symbol || x?.code || '').trim(),
    name: String(x?.name || '').trim(),
    marketCap: Number(x?.metrics?.market_cap ?? x?.market_cap ?? 0) || 0,
    pe: Number(x?.metrics?.pe ?? x?.pe ?? 0) || 0,
    roe: Number(x?.metrics?.roe ?? x?.roe ?? 0) || 0,
    price: Number(x?.metrics?.close ?? x?.price ?? 0) || 0,
  })).filter((x: WorkingPoolItemV2) => x.symbol);

  // 2. 异步同步到后端 WorkingPool 缓存 (Fire and forget or wait?)
  // 计划书要求 Step 1 读写 workingPool
  await syncWorkingPoolToBackend(mapped);

  return mapped;
}

/**
 * 同步当前编辑池到后端 Redis 缓存
 */
export async function syncWorkingPoolToBackend(items: WorkingPoolItemV2[]) {
  try {
    await client.post('/strategy/pool/working', { items });
  } catch (err) {
    console.error('[WizardV2Service] Sync working pool failed:', err);
  }
}

/**
 * 从后端获取 WorkingPool
 */
export async function getWorkingPoolFromBackend(): Promise<WorkingPoolItemV2[]> {
  try {
    const res = await client.get('/strategy/pool/working');
    return res.data?.items || [];
  } catch (err) {
    console.error('[WizardV2Service] Get working pool failed:', err);
    return [];
  }
}

/**
 * 保存 WorkingPool 为持久化版本
 */
export async function saveWorkingPoolVersion(name: string, symbols: string[]): Promise<SavedPoolVersionV2 | null> {
  // 注意：此处优先通过后端 /pool/versions/save 接口，确保从后端缓存生成，保证一致性
  const res = await client.post(`/strategy/pool/versions/save?pool_name=${encodeURIComponent(name)}`);
  
  const ok = res?.data?.success === true;
  if (!ok) return null;
  const data = res?.data;
  return {
    id: data?.file_key || `v-${Date.now()}`,
    name,
    stockCount: symbols.length,
    createdAt: new Date().toISOString(),
  };
}

/**
 * 激活特定版本的股票池
 */
export async function activatePoolVersion(fileKey: string): Promise<boolean> {
  try {
    const res = await client.post(`/strategy/pool/versions/${fileKey}/activate`);
    return res.data?.success === true;
  } catch (err) {
    console.error('[WizardV2Service] Activate pool version failed:', err);
    return false;
  }
}

export async function listSavedPoolVersions(): Promise<SavedPoolVersionV2[]> {
  const res = await client.get('/strategy/pool/versions');
  if (!res?.data?.success || !Array.isArray(res?.data?.pools)) return [];
  return res.data.pools.map((p: any) => ({
    id: p.file_key,
    name: p.pool_name || '未命名股票池',
    stockCount: Number(p.stock_count || 0),
    createdAt: p.created_at || new Date().toISOString(),
    updatedAt: p.updated_at || p.created_at,
  }));
}

export async function loadSavedPoolSymbols(fileKey: string): Promise<string[]> {
  const userId = getWizardUserId();
  const { previewPoolFile } = await import('./wizardService');
  const res = await previewPoolFile({ user_id: userId, file_key: fileKey });
  if (!res?.success || !Array.isArray(res?.items)) return [];
  return res.items.map((x: any) => String(x?.symbol || '').trim()).filter(Boolean);
}

export async function deleteSavedPoolVersion(fileKey: string): Promise<boolean> {
  const userId = getWizardUserId();
  const { deletePoolFile } = await import('./wizardService');
  const res = await deletePoolFile({ user_id: userId, file_key: fileKey, file_url: '' });
  return res?.success === true;
}

