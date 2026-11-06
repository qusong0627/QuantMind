import { apiClient } from '../../../services/aiStrategyClients';

export interface PersonalPaper {
  paper_id: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
}

export interface PersonalPaperUpload {
  paper_id: string;
  filename: string;
  size_bytes: number;
  sha256: string;
}

function unwrap<T>(response: { data?: { code?: number; data?: T } }): T {
  const data = response.data?.data;
  if (data === undefined || data === null) throw new Error('论文库响应格式无效');
  return data;
}

export async function listPersonalPapers(
  params: { limit: number; offset: number },
  signal?: AbortSignal,
): Promise<PersonalPaper[]> {
  const response = await apiClient.get('/alpha-agent/papers', { params, signal });
  return unwrap<PersonalPaper[]>(response);
}

export async function uploadPersonalPaper(file: File, signal?: AbortSignal): Promise<PersonalPaperUpload> {
  if (!/\.pdf$/i.test(file.name)) throw new Error('仅支持 PDF 文件');
  const form = new FormData();
  form.append('file', file, file.name);
  const response = await apiClient.post('/alpha-agent/papers', form, { signal });
  return unwrap<PersonalPaperUpload>(response);
}

export async function deletePersonalPaper(paperId: string, signal?: AbortSignal): Promise<void> {
  await apiClient.delete(`/alpha-agent/papers/${encodeURIComponent(paperId)}`, { signal });
}

export async function downloadPersonalPaper(paperId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/alpha-agent/papers/${encodeURIComponent(paperId)}/download`,
    { responseType: 'blob', signal },
  );
  return response.data;
}
