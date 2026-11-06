import { apiClient } from '../../../services/aiStrategyClients';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { generateId } from '../utils-v2';

export interface ResearchRequest {
  request_id: string; kind: 'explain' | 'decompose';
  status: 'queued' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled';
  label: string; factor_id: string | null; universe: string | null;
  created_at: string; completed_at: string | null; error_message: string | null;
  call_count: number; source_revision_no?: number | null;
  result?: Record<string, unknown> | null;
}
const intentKeys = new Map<string, string>();
const controllers = new Set<AbortController>();
registerSessionReset(() => { intentKeys.clear(); controllers.forEach((controller) => controller.abort()); controllers.clear(); });
const stopped = () => new DOMException('研究请求已中止等待', 'AbortError');
const unwrap = (value: unknown): ResearchRequest => {
  const row = value as ResearchRequest | null;
  if (!row || typeof row.request_id !== 'string' || !['explain', 'decompose'].includes(row.kind)) throw new Error('研究请求响应无效');
  return row;
};
export async function getResearchRequest(id: string, signal?: AbortSignal): Promise<ResearchRequest> {
  const response = await apiClient.get(`/alpha-agent/research/requests/${encodeURIComponent(id)}`, { signal });
  return unwrap(response.data?.data);
}
export async function listResearchRequests(offset: number, signal: AbortSignal): Promise<{ items: ResearchRequest[]; has_more: boolean }> {
  const response = await apiClient.get('/alpha-agent/research/requests', { params: { offset }, signal });
  if (!Array.isArray(response.data?.data?.items)) throw new Error('研究请求历史格式无效');
  return { items: response.data.data.items.map(unwrap), has_more: response.data.data.has_more === true };
}
export async function cancelResearchRequest(id: string, signal: AbortSignal): Promise<ResearchRequest> {
  const response = await apiClient.post(`/alpha-agent/research/requests/${encodeURIComponent(id)}/cancel`, undefined, { signal });
  return unwrap(response.data?.data);
}
class FinishedResearchError extends Error {}
function pause(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(stopped()); return; }
    const abort = () => { window.clearTimeout(timer); reject(stopped()); };
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 1200);
    signal.addEventListener('abort', abort, { once: true });
  });
}

export async function requestResearchResult<T>(path: string, body?: unknown, callerSignal?: AbortSignal): Promise<T> {
  const generation = getSessionGeneration();
  const intent = `${generation}\u0000${path}\u0000${JSON.stringify(body)}`;
  let key = intentKeys.get(intent);
  if (!key) {
    key = globalThis.crypto?.randomUUID?.() ?? generateId(); intentKeys.set(intent, key);
    if (intentKeys.size > 32) intentKeys.delete(intentKeys.keys().next().value as string);
  }
  const controller = new AbortController(); controllers.add(controller);
  const abort = () => controller.abort();
  if (callerSignal?.aborted) controller.abort(); else callerSignal?.addEventListener('abort', abort, { once: true });
  const valid = () => { if (controller.signal.aborted || generation !== getSessionGeneration()) throw stopped(); };
  const wait = async (initial: ResearchRequest): Promise<T> => {
    let row = initial;
    while (true) {
      valid();
      if (row.status === 'completed' && row.result && typeof row.result === 'object') return { ...row.result, source_revision_no: row.source_revision_no } as T;
      if (row.status === 'failed' || row.status === 'cancelled') throw new FinishedResearchError(row.error_message || '研究请求已结束，可重新发起');
      await pause(controller.signal); valid(); row = await getResearchRequest(row.request_id, controller.signal);
    }
  };
  try {
    valid();
    const response = await apiClient.post(path, body, { signal: controller.signal, headers: { 'Idempotency-Key': key }, timeout: 270000 });
    valid();
    const data = response.data?.data;
    const result = data?.research_request ? await wait(unwrap(data.research_request)) : data as T;
    valid(); intentKeys.delete(intent); return result;
  } catch (failure) {
    valid();
    if (failure instanceof FinishedResearchError) { intentKeys.delete(intent); throw failure; }
    // Recover the original result through a read, never by dispatching a second model call.
    let saved: ResearchRequest | null = null;
    try {
      const response = await apiClient.get(`/alpha-agent/research/requests/by-key/${encodeURIComponent(key)}`, { signal: controller.signal, timeout: 12000 });
      valid(); saved = unwrap(response.data?.data);
    } catch { valid(); }
    if (saved) {
      if (saved.status === 'failed' || saved.status === 'cancelled') intentKeys.delete(intent);
      else {
        try { const result = await wait(saved); valid(); intentKeys.delete(intent); return result; }
        catch (resumeFailure) { if (resumeFailure instanceof FinishedResearchError) intentKeys.delete(intent); throw resumeFailure; }
      }
    }
    throw failure;
  } finally {
    callerSignal?.removeEventListener('abort', abort); controllers.delete(controller);
  }
}
