/**
 * 文档挖掘链 API 桥（T-FM-11）：/api/v1/alpha-agent/docs/*
 *
 * 后端契约（backend/services/engine/routers/alpha_agent_docs.py）：
 * - 整组端点挂在 ENABLE_DOC_MINING 闸门后，未开时 403 detail=doc_mining_disabled；
 * - 所有响应三层信封 {code, data}；错误细节在 FastAPI 的 detail 字段。
 *
 * 错误处理约定：本模块统一 throw Error（message=后端 detail 原文），
 * 调用方 catch 后用 `extractDetail` 拿用户可读文案——与 TaskContext 对
 * evolve 失败的处理方式一致（后端的拒绝理由原样带给用户，不吞成「请求失败」）。
 */

import { apiClient } from '../../../services/aiStrategyClients';

// ========================== 常量 ==========================

/** 与后端 `ALLOWED_EXTENSIONS`（alpha_agent_docs.py）同词表，改一处必须改两处。 */
export const DOC_UPLOAD_ACCEPT = '.pdf,.png,.jpg,.jpeg,.docx,.pptx,.doc,.ppt';

/** 与后端 `MAX_SUBMIT_DIRECTION_CHARS`（routers/alpha_agent.py）同字面量。 */
export const DOC_MAX_DIRECTION_CHARS = 8000;

/** 与后端 `RD_AGENT_DOC_MAX_MB` 默认值（200MB）对齐——前端早拒只是省一次白传。 */
export const DOC_MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

export const ORGANIZE_KIND_LABELS: Record<string, string> = {
  free: '主题研究简报',
  paper: '论文复现解读',
};

export const DOC_STATUS_LABELS: Record<string, string> = {
  uploaded: '排队中',
  parsing: '解析中',
  parsed: '已解析',
  organized: '已整理',
  parse_failed: '解析失败',
  expired: '已过期',
  deleted: '已删除',
};

// ========================== 类型 ==========================

export interface DocRow {
  doc_id: string;
  filename: string;
  ext: string | null;
  size_bytes: number | null;
  parse_state: string | null;
  page_count: number | null;
  status: string;
  /** 仅详情端点返回；列表端点为省体积不带 */
  organized_text?: string | null;
  organize_kind: string | null;
  organize_prompt_version: string | null;
  organized_at: string | null;
  task_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocsListPage {
  items: DocRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface DocQuotaStatus {
  day: string;
  user_id: string;
  user_used: number;
  user_limit: number;
  platform_used: number;
  platform_budget: number;
  user_remaining: number;
  platform_remaining: number;
  exhausted: boolean;
  warning: boolean;
  /** MinerU token 是否已配置：false 时上传会 503，入口应先提示 */
  token_configured: boolean;
}

export interface OrganizeResult {
  kind: string;
  prompt_version: string;
  payload: Record<string, unknown>;
  markdown: string;
  truncated: boolean;
  chunks_used: number;
  doc: DocRow;
}

// ========================== 工具 ==========================

/** axios 错误 → 用户可读文案（后端 detail 原文优先，其次 Error.message）。 */
export function extractDetail(err: unknown): string {
  const e = err as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };
  const detail = e?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  return e?.message || '请求失败';
}

// ========================== 端点 ==========================

/** 上传文档（multipart）。进度回调仅在浏览器能算出 total 时触发。 */
export async function uploadDoc(
  file: File,
  onProgress?: (pct: number) => void,
): Promise<{ doc: DocRow; reused: boolean }> {
  const form = new FormData();
  form.append('file', file);
  // axios 1.x 的 XHR 适配器对 FormData 会自动让浏览器设置 multipart boundary
  //（quantbot agentApi 同款路径），不要手写 Content-Type。
  const res = await apiClient.post('/alpha-agent/docs/upload', form, {
    onUploadProgress: (evt) => {
      if (onProgress && evt.total) {
        onProgress(Math.round((evt.loaded * 100) / evt.total));
      }
    },
  });
  const data = res.data?.data ?? {};
  if (!data.doc) throw new Error('上传响应缺少文档信息');
  return { doc: data.doc as DocRow, reused: Boolean(data.reused) };
}

export async function listDocs(params?: {
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<DocsListPage> {
  const res = await apiClient.get('/alpha-agent/docs', { params });
  const data = res.data?.data ?? {};
  return {
    items: (data.items ?? []) as DocRow[],
    total: data.total ?? 0,
    limit: data.limit ?? 0,
    offset: data.offset ?? 0,
  };
}

export async function getDoc(docId: string): Promise<DocRow> {
  const res = await apiClient.get(`/alpha-agent/docs/${docId}`);
  const doc = res.data?.data?.doc;
  if (!doc) throw new Error('文档不存在或已删除');
  return doc as DocRow;
}

export async function getDocQuota(): Promise<DocQuotaStatus> {
  const res = await apiClient.get('/alpha-agent/docs/quota');
  return res.data?.data as DocQuotaStatus;
}

/** 读取解析产物文本（默认 full.md）。返回原始文本，不做 markdown 渲染。 */
export async function getDocFileText(
  docId: string,
  path = 'full.md',
): Promise<string> {
  const res = await apiClient.get(`/alpha-agent/docs/${docId}/file`, {
    params: { path },
    responseType: 'text',
    transformResponse: [(d: unknown) => d],
  });
  return typeof res.data === 'string' ? res.data : String(res.data ?? '');
}

/** 触发整理（同步等待 LLM，长文可达分钟级；apiClient 默认超时 300s 已覆盖）。 */
export async function organizeDoc(
  docId: string,
  opts: { kind: string; extra?: string },
): Promise<OrganizeResult> {
  const res = await apiClient.post(`/alpha-agent/docs/${docId}/organize`, {
    kind: opts.kind,
    extra: opts.extra || undefined,
  });
  const data = res.data?.data;
  if (!data || typeof data.markdown !== 'string') {
    throw new Error('整理响应缺少结果');
  }
  return data as OrganizeResult;
}

export async function deleteDoc(docId: string): Promise<void> {
  await apiClient.delete(`/alpha-agent/docs/${docId}`);
}
