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

/** 与后端 `MAX_DOC_FILES`（alpha_agent_docs.py）同字面量。 */
export const DOC_MAX_FILES = 20;

/**
 * 与后端 `RD_AGENT_DOC_MAX_TOTAL_MB` 默认值（200MB）对齐——多文件合计早拒。
 * 抬后端合计上限要求 nginx / api→engine 代理体一起抬（部署链同卡一个请求），
 * 届时本常量与面板文案也要同步改（见 docs/文档挖掘_启用与通道指南.md 第四节）。
 */
export const DOC_MAX_TOTAL_UPLOAD_BYTES = 200 * 1024 * 1024;

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
  /** 单次上传的文件件数（多文件合并解析：正文+附录/多图） */
  files_count?: number | null;
  parse_state: string | null;
  page_count: number | null;
  status: string;
  /** 仅详情端点返回；列表端点为省体积不带 */
  organized_text?: string | null;
  organize_kind: string | null;
  organize_prompt_version: string | null;
  organized_at: string | null;
  task_id: string | null;
  /**
   * 该文档关联的挖掘任务数（一文档多方向）。列表/详情端点批量带出；
   * **undefined = 后端未返回（查询失败），0 = 确认没挖过**——两者绝不混。
   */
  task_count?: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

/** 文档关联的挖掘任务摘要（GET /docs/{id} 的 tasks 项，最近优先，≤20 条）。 */
export interface DocTaskSummary {
  task_id: string;
  status: string;
  /** 挖掘方向原文（可能上万字，展示面自行截断） */
  direction: string;
  created_at: string;
}

export interface DocDetail {
  doc: DocRow;
  /** 明细（≤20 条）；undefined = 后端未返回明细（查询失败），[] = 确认没有 */
  tasks?: DocTaskSummary[];
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

/**
 * 上传文档（multipart，单/多文件）。进度回调仅在浏览器能算出 total 时触发。
 *
 * 多文件：同一字段 `file` 重复提交，**数组顺序 = 合并顺序**（正文在前、
 * 附录/附图在后由调用方决定）；后端一次 MinerU 批次解析后合并为一份
 * full.md（合并处插入「第 i/N 部分」章节标记）。
 */
export async function uploadDocs(
  files: File[],
  onProgress?: (pct: number) => void,
): Promise<{ doc: DocRow; reused: boolean }> {
  const form = new FormData();
  for (const file of files) form.append('file', file);
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

/** 单文件便捷包装（保持旧签名：老调用方零改动）。 */
export async function uploadDoc(
  file: File,
  onProgress?: (pct: number) => void,
): Promise<{ doc: DocRow; reused: boolean }> {
  return uploadDocs([file], onProgress);
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

/** 文档详情 + 关联挖掘任务（一文档多方向回看）。缺 doc → 抛错，不许编空壳。 */
export async function getDocDetail(docId: string): Promise<DocDetail> {
  const res = await apiClient.get(`/alpha-agent/docs/${docId}`);
  const data = res.data?.data ?? {};
  if (!data.doc) throw new Error('文档不存在或已删除');
  return {
    doc: data.doc as DocRow,
    tasks: Array.isArray(data.tasks) ? (data.tasks as DocTaskSummary[]) : undefined,
  };
}

export async function getDoc(docId: string): Promise<DocRow> {
  return (await getDocDetail(docId)).doc;
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
