import { apiClient } from '../../../services/aiStrategyClients';
import { requestResearchResult } from './researchRequestsApi';
import { generateId } from '../utils-v2';

export type ResearchDocumentStatus =
  | 'uploaded'
  | 'parsing'
  | 'parsed'
  | 'parse_failed'
  | 'organized'
  | 'expired'
  | 'deleted';

export interface ResearchDocument {
  doc_id: string;
  filename: string;
  ext: string;
  size_bytes: number;
  files_count: number;
  sha256: string;
  status: ResearchDocumentStatus;
  page_count: number | null;
  parse_job_id?: string | null;
  parse_state?: string | null;
  organized_text?: string | null;
  organize_kind: string | null;
  organize_job_id?: string | null;
  organize_state?: 'idle' | 'queued' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled' | null;
  organized_payload?: { direction?: string; summary?: string } | null;
  organize_prompt_version?: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  organized_at: string | null;
}

export interface ResearchDocumentPage {
  items: ResearchDocument[];
  has_more: boolean;
  limit: number;
  offset: number;
}

export type ResearchDocumentUpload = Pick<
  ResearchDocument,
  'doc_id' | 'filename' | 'size_bytes' | 'sha256' | 'files_count' | 'status'
>;

export interface DocumentParseJob {
  job_id: string;
  status: 'queued' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled';
}

export type DocumentOrganizeJob = DocumentParseJob;

export interface DocumentMiningTask {
  task_id: string;
  status: string;
}

export interface DirectionHypothesisCard {
  title: string;
  hypothesis: string;
  rationale?: string;
  categories: string[];
  evaluation_hint?: string;
  seed_factor_id?: string;
}

export interface DirectionDecomposeResult {
  prompt_version: string;
  cards: DirectionHypothesisCard[];
  dropped: number;
  max_cards: number;
}

export interface DocumentMiningBatchItem {
  index: number;
  direction: string;
  task_id: string | null;
  status: string;
  queued: boolean;
  queue_position: number | null;
  error: string | null;
  seed_factor_ids?: string[];
}

export interface DocumentMiningBatchReceipt {
  batch_id: string;
  items: DocumentMiningBatchItem[];
  started: number;
  queued: number;
  failed: number;
}

export interface ResearchMiningBatchRequest {
  batch_id: string;
  directions: string[];
  doc_id?: string;
  market: 'a_share';
  universe: string;
  loop_n: number;
  data_source?: 'qlib_bin' | 'parquet';
  freq?: 'day';
  datasets?: string[];
  mining_start?: string;
  mining_end?: string;
  valid_start?: string;
  valid_end?: string;
  seed_factor_ids?: Array<string | null>;
}

export interface OrganizedDraftRequest {
  organized_text: string;
}

export interface ResearchDocumentMarkdown {
  doc_id: string;
  content: string;
  truncated: boolean;
  max_bytes: number;
}

interface ApiEnvelope<T> {
  code: number;
  data: T;
}

const unwrap = <T>(response: { data: ApiEnvelope<T> }): T => response.data.data;

export async function listResearchDocuments(
  params: { limit: number; offset: number },
  signal?: AbortSignal,
): Promise<ResearchDocumentPage> {
  const response = await apiClient.get<ApiEnvelope<ResearchDocumentPage>>(
    '/alpha-agent/docs',
    { params, signal },
  );
  return unwrap(response);
}

export async function getResearchDocument(
  docId: string,
  signal?: AbortSignal,
): Promise<ResearchDocument> {
  const response = await apiClient.get<ApiEnvelope<ResearchDocument>>(
    `/alpha-agent/docs/${encodeURIComponent(docId)}`,
    { signal },
  );
  return unwrap(response);
}

export async function uploadResearchDocument(fileOrFiles: File | File[], signal?: AbortSignal): Promise<ResearchDocumentUpload> {
  const files = Array.isArray(fileOrFiles) ? fileOrFiles : [fileOrFiles];
  if (!files.length || files.length > 10) throw new Error('每批需选择 1 到 10 个 PDF 文件');
  const form = new FormData();
  files.forEach((file) => form.append('file', file, file.name));
  const response = await apiClient.post<ApiEnvelope<ResearchDocumentUpload>>(
    '/alpha-agent/docs',
    form,
    { signal },
  );
  return unwrap(response);
}

export async function submitDocumentParse(docId: string, signal?: AbortSignal): Promise<DocumentParseJob> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? generateId();
  const response = await apiClient.post<{ data?: DocumentParseJob }>(
    `/alpha-agent/docs/${encodeURIComponent(docId)}/parse`,
    undefined,
    { headers: { 'Idempotency-Key': idempotencyKey }, signal },
  );
  const job = response.data?.data;
  if (
    !job
    || typeof job.job_id !== 'string'
    || !['queued', 'running', 'stopping', 'completed', 'failed', 'cancelled'].includes(job.status)
  ) {
    throw new Error('文档解析任务格式无效');
  }
  return job;
}

export async function reconcileDocumentParse(
  docId: string,
  batchId: string,
  signal?: AbortSignal,
): Promise<DocumentParseJob> {
  const response = await apiClient.post<{ data?: DocumentParseJob }>(
    `/alpha-agent/docs/${encodeURIComponent(docId)}/parse/reconcile`,
    { batch_id: batchId },
    { signal },
  );
  const job = response.data?.data;
  if (
    !job
    || typeof job.job_id !== 'string'
    || !['queued', 'running', 'stopping'].includes(job.status)
  ) {
    throw new Error('文档解析恢复回执格式无效');
  }
  return job;
}

export async function submitDocumentOrganization(
  docId: string,
  payload: { kind: 'free' | 'paper'; extra?: string },
  signal?: AbortSignal,
): Promise<DocumentOrganizeJob> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? generateId();
  const response = await apiClient.post<{ data?: DocumentOrganizeJob }>(
    `/alpha-agent/docs/${encodeURIComponent(docId)}/organize`,
    payload,
    { headers: { 'Idempotency-Key': idempotencyKey }, signal },
  );
  const job = response.data?.data;
  if (
    !job
    || typeof job.job_id !== 'string'
    || !['queued', 'running', 'stopping', 'completed', 'failed', 'cancelled'].includes(job.status)
  ) {
    throw new Error('文档整理任务格式无效');
  }
  return job;
}

export async function saveOrganizedDraft(
  docId: string,
  organizedText: string,
  signal?: AbortSignal,
): Promise<void> {
  await apiClient.put(
    `/alpha-agent/docs/${encodeURIComponent(docId)}/organized-text`,
    { organized_text: organizedText },
    { signal },
  );
}

export async function dispatchDocumentMining(
  docId: string,
  direction: string,
  signal?: AbortSignal,
): Promise<DocumentMiningTask> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? generateId();
  const response = await apiClient.post<{ data?: DocumentMiningTask }>(
    '/alpha-agent/evolve',
    {
      algorithm: 'rdagent',
      market: 'a_share',
      universe: 'csi300',
      loop_n: 3,
      direction,
      doc_id: docId,
    },
    { headers: { 'Idempotency-Key': idempotencyKey }, signal },
  );
  const task = response.data?.data;
  if (!task || typeof task.task_id !== 'string' || typeof task.status !== 'string') {
    throw new Error('文档挖掘任务响应格式无效');
  }
  return task;
}

export async function decomposeResearchDirection(
  direction: string,
  signal?: AbortSignal,
  seedFactorIds: string[] = [],
  options: { universe?: string; maxCards?: number } = {},
): Promise<DirectionDecomposeResult> {
  const result = await requestResearchResult<DirectionDecomposeResult>(
    '/alpha-agent/directions/decompose',
    {
      direction, market: 'a_share', universe: options.universe ?? 'csi300',
      max_cards: options.maxCards ?? 6, seed_factor_ids: seedFactorIds,
    },
    signal,
  );
  if (!result || !Array.isArray(result.cards) || result.cards.some(
    (card) => typeof card?.title !== 'string' || typeof card?.hypothesis !== 'string',
  )) {
    throw new Error('方向拆解结果格式无效');
  }
  return result;
}

export function createResearchMiningBatchId(): string {
  return globalThis.crypto?.randomUUID?.() ?? generateId();
}

export const createDocumentMiningBatchId = createResearchMiningBatchId;

export async function dispatchDocumentMiningBatch(
  docId: string,
  directions: string[],
  batchId: string,
  signal?: AbortSignal,
  seedFactorIds?: Array<string | null>,
): Promise<DocumentMiningBatchReceipt> {
  return dispatchResearchMiningBatch({
    batch_id: batchId,
    doc_id: docId,
    directions,
    market: 'a_share',
    universe: 'csi300',
    loop_n: 3,
    ...(seedFactorIds ? { seed_factor_ids: seedFactorIds } : {}),
  }, signal);
}

export async function dispatchResearchMiningBatch(
  payload: ResearchMiningBatchRequest,
  signal?: AbortSignal,
): Promise<DocumentMiningBatchReceipt> {
  const response = await apiClient.post<{ data?: DocumentMiningBatchReceipt }>(
    '/alpha-agent/directions/dispatch',
    payload,
    { signal },
  );
  const receipt = response.data?.data;
  if (
    !receipt
    || receipt.batch_id !== payload.batch_id
    || !Array.isArray(receipt.items)
    || receipt.items.length !== payload.directions.length
    || receipt.items.some((item, index) => (
      item.index !== index
      || typeof item.status !== 'string'
      || (item.task_id !== null && typeof item.task_id !== 'string')
    ))
  ) {
    throw new Error('批量挖掘回执格式无效');
  }
  return receipt;
}

export async function deleteResearchDocument(docId: string, signal?: AbortSignal): Promise<void> {
  await apiClient.delete(`/alpha-agent/docs/${encodeURIComponent(docId)}`, { signal });
}

export async function downloadResearchDocument(docId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/alpha-agent/docs/${encodeURIComponent(docId)}/original`,
    { responseType: 'blob', signal },
  );
  return response.data;
}

export async function getResearchDocumentMarkdown(
  docId: string,
  signal?: AbortSignal,
): Promise<ResearchDocumentMarkdown> {
  const response = await apiClient.get<{ data?: ResearchDocumentMarkdown }>(
    `/alpha-agent/docs/${encodeURIComponent(docId)}/markdown`,
    { signal },
  );
  const data = response.data?.data;
  if (!data || data.doc_id !== docId || typeof data.content !== 'string') {
    throw new Error('文档解析内容格式无效');
  }
  return data;
}
