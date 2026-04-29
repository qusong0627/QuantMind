/**
 * QuantBot API 服务 - 最终加固版
 */

import { apiClient } from '../../../services/api-client';
import { authService } from '../../auth/services/authService';
import { SERVICE_URLS } from '../../../config/services';

const API_BASE_URL = '/api/v1/openclaw'; 

export interface Session {
  id: string;
  name: string;
  user_id: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp?: string;
}

export interface SessionAttachment {
  file_id: string;
  original_name: string;
  file_name: string;
  file_size: number;
  content_type: string;
  copaw_path: string;
  uploaded_at?: string;
}

export interface OpenClawHealthResponse {
  status: 'healthy' | 'degraded' | 'unhealthy';
  service: string;
  timestamp?: string;
  components: {
    api: {
      status: 'healthy' | 'degraded' | 'unhealthy';
      latency_ms?: number;
    };
    copaw: {
      status: 'healthy' | 'degraded' | 'unhealthy' | 'unreachable';
      latency_ms?: number;
      error?: string | null;
    };
  };
}

interface BackendSession {
  session_id?: string;
  title?: string;
  user_id?: string;
  created_at?: string;
  updated_at?: string;
  id?: string;
  name?: string;
}

interface StreamUpstreamError extends Error {
  code?: string;
  status?: number;
  userMessage?: string;
  details?: string;
  recoverable?: boolean;
}

class QuantBotApiService {
  private isSessionServiceUnavailable(error: any): boolean {
    const status = error?.response?.status || error?.status;
    return [502, 503, 504].includes(Number(status));
  }

  private createAuthError(message: string): Error {
    const error = new Error(message) as Error & { response?: { status: number } };
    error.response = { status: 401 };
    return error;
  }

  private async sleep(ms: number): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, ms));
  }

  private shouldRetry(error: any): boolean {
    const status = error?.response?.status || error?.status;
    const code = error?.code;
    return status === 503 || code === 'ECONNABORTED' || code === 'ERR_NETWORK';
  }

  private async withRetry<T>(fn: () => Promise<T>, retries = 1): Promise<T> {
    let lastError: any = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        return await fn();
      } catch (error: any) {
        lastError = error;
        if (attempt >= retries || !this.shouldRetry(error)) {
          throw error;
        }
        await this.sleep(300 * (attempt + 1));
      }
    }
    throw lastError;
  }

  private async resolveAccessToken(): Promise<string> {
    const accessToken = authService.getAccessToken();
    if (!accessToken) {
      throw this.createAuthError('登录状态失效，请重新登录后再使用 QuantBot');
    }

    if (!authService.isTokenExpired()) {
      return accessToken;
    }

    const refreshToken = authService.getRefreshToken();
    if (!refreshToken) {
      authService.clearTokens();
      throw this.createAuthError('登录状态已过期，请重新登录');
    }

    try {
      const newToken = await authService.getRefreshedToken(refreshToken);
      localStorage.setItem('access_token', newToken);
      return newToken;
    } catch {
      authService.clearTokens();
      throw this.createAuthError('登录状态已过期，请重新登录');
    }
  }

  private resolveUserId(): string {
    const user = authService.getStoredUser() as any;
    return String(user?.user_id || user?.id || 'quantbot-user').trim();
  }

  private get userId(): string {
    return this.resolveUserId();
  }

  /**
   * 获取基础请求配置，包含 Bot 特有 Header
   */
  private getBotRequestConfig(config: any = {}) {
    return {
      ...config,
      headers: {
        ...(config.headers || {}),
        'X-User-Id': this.userId,
        'X-Channel': 'quantbot',
      },
    };
  }

  private getOpenClawRequestConfig(config: any = {}) {
    return this.getBotRequestConfig({
      ...config,
      _suppressServiceUnavailableRetry: true,
    });
  }

  private getLocalSessionsStorageKey(): string {
    return `quantbot:local-sessions:${this.userId}`;
  }

  private createLocalSessionId(): string {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    const bytes = Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = bytes.map((b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  private readLocalSessions(): Session[] {
    try {
      const raw = window.localStorage.getItem(this.getLocalSessionsStorageKey());
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed
        .map((session) => this.mapSession(session as BackendSession))
        .filter((session) => this.isUUID(session.id));
    } catch {
      return [];
    }
  }

  private writeLocalSessions(sessions: Session[]): void {
    try {
      window.localStorage.setItem(this.getLocalSessionsStorageKey(), JSON.stringify(sessions));
    } catch {
      // localStorage 不可用时静默降级
    }
  }

  private upsertLocalSession(session: Session): void {
    const sessions = this.readLocalSessions();
    const next = [session, ...sessions.filter((item) => item.id !== session.id)];
    this.writeLocalSessions(next);
  }

  private removeLocalSession(sessionId: string): void {
    const sessions = this.readLocalSessions().filter((session) => session.id !== sessionId);
    this.writeLocalSessions(sessions);
  }

  private mapSession(session: BackendSession): Session {
    return {
      id: String(session.session_id || session.id || ''),
      name: String(session.title || session.name || '新对话'),
      user_id: String(session.user_id || this.userId),
      created_at: String(session.created_at || new Date().toISOString()),
      updated_at: String(session.updated_at || session.created_at || new Date().toISOString()),
    };
  }

  /**
   * 校验是否为合法的 UUID 格式
   */
  private isUUID(id: string | null): boolean {
    if (!id) return false;
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
  }

  private extractTextContent(payload: unknown): string {
    if (payload == null) return '';
    if (typeof payload === 'string') return payload;
    if (Array.isArray(payload)) {
      return payload.map(item => this.extractTextContent(item)).join('');
    }
    if (typeof payload === 'object') {
      const record = payload as Record<string, unknown>;
      if (typeof record.text === 'string') return record.text;
      if (typeof record.delta === 'string') return record.delta;
      if (typeof record.output_text === 'string') return record.output_text;
      if (record.delta === true && typeof record.text === 'string') return record.text;
      if (record.type === 'text' && typeof record.text === 'string') return record.text;
      if (record.content != null) return this.extractTextContent(record.content);
      if (record.output != null) return this.extractTextContent(record.output);
      if (record.message != null) return this.extractTextContent(record.message);
    }
    return '';
  }

  private normalizeSseBuffer(buffer: string): string {
    return buffer.replace(/\r\n/g, '\n');
  }

  private parseSseEventBlock(block: string): string {
    const dataLines = block
      .split('\n')
      .map((line) => line.trimEnd())
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim());
    return dataLines.join('\n').trim();
  }

  private buildUpstreamError(payload: Record<string, unknown>): StreamUpstreamError | null {
    const status = String(payload.status || '');
    const errorLike = payload.error as Record<string, unknown> | null | undefined;
    const hasError = !!errorLike && typeof errorLike === 'object';
    if (status !== 'failed' && !hasError) return null;
    const code = String(errorLike?.code || payload.code || 'UPSTREAM_STREAM_FAILED');
    const message = String(errorLike?.message || payload.message || '上游流式响应失败');
    const error = new Error(`${code}: ${message}`) as StreamUpstreamError & {
      response?: { status: number };
    };
    const normalizedMessage = message.toLowerCase();
    if (code === 'PROVIDER_ERROR' && normalizedMessage.includes('no active model configured')) {
      error.userMessage = '当前未配置可用模型，请先在 QuantBot 管理端启用一个模型后再试。';
      error.recoverable = false;
    }
    error.code = code;
    error.details = message;
    error.response = { status: 502 };
    return error;
  }

  async sendMessageStream(messageText: string, options: { chatId?: string, attachments?: SessionAttachment[], onChunk?: (t: string) => void, onComplete?: (t: string) => void, onError?: (e: any) => void }) {
    let attempt = 0;
    const maxRetries = 1;

    const performRequest = async (): Promise<void> => {
      try {
        const chatId = options.chatId;
        if (!chatId || !this.isUUID(chatId)) {
          throw new Error('缺少合法的会话 ID');
        }
        const accessToken = await this.resolveAccessToken();
        const fullUrl = `${SERVICE_URLS.API_GATEWAY}${API_BASE_URL}/chat`;

        const response = await this.withRetry(() => fetch(fullUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-User-Id': this.userId,
            'X-Channel': 'quantbot',
            'Authorization': `Bearer ${accessToken}`,
          },
          body: JSON.stringify({
            message: messageText,
            session_id: chatId,
            user_id: this.userId,
            attachments: options.attachments || [],
          }),
        }).then(async (resp) => {
          if (!resp.ok && resp.status === 503) {
            throw Object.assign(new Error(`HTTP ${resp.status}`), { response: { status: resp.status } });
          }
          return resp;
        }));

        if (!response.ok) {
          // 如果遇到 401 且还有重试次数，尝试刷新 Token
          if (response.status === 401 && attempt < maxRetries) {
            attempt += 1;
            const refreshToken = authService.getRefreshToken();
            if (refreshToken) {
              await authService.getRefreshedToken(refreshToken);
              return performRequest(); // 重试
            }
          }
          throw new Error(`HTTP ${response.status}`);
        }

        if (!response.body) {
          throw new Error('上游未返回流式响应');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let streamBuffer = '';
        let accumulatedText = '';
        let upstreamError: Error | null = null;

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          streamBuffer += decoder.decode(value, { stream: true });
          const normalized = this.normalizeSseBuffer(streamBuffer);
          const blocks = normalized.split('\n\n');
          streamBuffer = blocks.pop() || '';

          for (const block of blocks) {
            const rawData = this.parseSseEventBlock(block);
            if (!rawData || rawData === '[DONE]') continue;

            try {
              const parsed = JSON.parse(rawData);
              const maybeError = this.buildUpstreamError(parsed as Record<string, unknown>);
              if (maybeError) {
                upstreamError = maybeError;
                continue;
              }
              const chunkText = this.extractTextContent(parsed);
              if (!chunkText) continue;

              if (chunkText.length > accumulatedText.length && chunkText.startsWith(accumulatedText)) {
                const delta = chunkText.slice(accumulatedText.length);
                accumulatedText = chunkText;
                if (delta) options.onChunk?.(delta);
                continue;
              }

              accumulatedText += chunkText;
              options.onChunk?.(chunkText);
            } catch (err) {
              if (rawData) {
                options.onChunk?.(rawData);
                accumulatedText += rawData;
              }
            }
          }
        }

        const tailData = this.parseSseEventBlock(this.normalizeSseBuffer(streamBuffer));
        if (tailData && tailData !== '[DONE]') {
          try {
            const parsed = JSON.parse(tailData);
            const maybeError = this.buildUpstreamError(parsed as Record<string, unknown>);
            if (maybeError) {
              upstreamError = maybeError;
            } else {
              const chunkText = this.extractTextContent(parsed);
              if (chunkText) {
                if (chunkText.length > accumulatedText.length && chunkText.startsWith(accumulatedText)) {
                  const delta = chunkText.slice(accumulatedText.length);
                  accumulatedText = chunkText;
                  if (delta) options.onChunk?.(delta);
                } else {
                  accumulatedText += chunkText;
                  options.onChunk?.(chunkText);
                }
              }
            }
          } catch (err) {
            options.onChunk?.(tailData);
            accumulatedText += tailData;
          }
        }

        if (upstreamError) {
          throw upstreamError;
        }
        options.onComplete?.(accumulatedText);
      } catch (error: any) {
        options.onError?.(error);
      }
    };

    return performRequest();
  }

  async getSessions(_limit?: number): Promise<Session[]> {
    try {
      const config = this.getOpenClawRequestConfig();
      const data = await apiClient.get<BackendSession[]>(`${API_BASE_URL}/sessions`, {}, config);
      const sessionList = Array.isArray(data) ? data : [];
      const sessions = sessionList
        .map((session: BackendSession) => this.mapSession(session))
        .filter(session => this.isUUID(session.id));
      const localSessions = this.readLocalSessions();
      const deduped = new Map<string, Session>();
      for (const session of localSessions) {
        deduped.set(session.id, session);
      }
      for (const session of sessions) {
        const previous = deduped.get(session.id);
        if (!previous) {
          deduped.set(session.id, session);
          continue;
        }
        const prevTime = new Date(previous.updated_at || previous.created_at || 0).getTime();
        const nextTime = new Date(session.updated_at || session.created_at || 0).getTime();
        if (nextTime >= prevTime) {
          deduped.set(session.id, session);
        }
      }
      return Array.from(deduped.values());
    } catch (error: any) {
      if (error?.status === 401) {
        throw error;
      }
      if (this.isSessionServiceUnavailable(error)) {
        return this.readLocalSessions();
      }
      return this.readLocalSessions();
    }
  }

  async getSessionMessages(chatId: string): Promise<ChatMessage[]> {
    if (!this.isUUID(chatId)) throw { status: 404 };
    try {
      const config = this.getOpenClawRequestConfig();
      const data = await apiClient.get<any>(`${API_BASE_URL}/sessions/${chatId}/messages`, {}, config);
      return (data.messages || []).map((m: any) => ({
        role: m.role,
        content: Array.isArray(m.content) ? m.content.map((c: any) => c.text || '').join('') : String(m.content),
        timestamp: m.timestamp || m.created_at
      }));
    } catch (error: any) {
      if (this.isSessionServiceUnavailable(error)) {
        return [];
      }
      throw error;
    }
  }

  async createNewSession(name: string = '新对话'): Promise<Session> {
    try {
      const config = this.getOpenClawRequestConfig();
      const data = await apiClient.post<any>(`${API_BASE_URL}/sessions`, {
        title: name,
        user_id: this.userId,
      }, config);

      const session = this.mapSession(data || {});
      if (!this.isUUID(session.id)) throw new Error('后端未返回合法 session_id');
      this.setChatId(session.id);
      this.upsertLocalSession(session);
      return session;
    } catch (error: any) {
      if (!this.isSessionServiceUnavailable(error)) {
        throw error;
      }

      const session: Session = {
        id: this.createLocalSessionId(),
        name,
        user_id: this.userId,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      this.setChatId(session.id);
      this.upsertLocalSession(session);
      return session;
    }
  }

  async uploadSessionFile(file: File, chatId: string): Promise<SessionAttachment> {
    if (!this.isUUID(chatId)) {
      throw new Error('缺少合法的会话 ID');
    }

    const formData = new FormData();
    formData.append('file', file);
    formData.append('session_id', chatId);
    formData.append('user_id', this.userId);
    
    // apiClient 的 POST 会自动携带 Token 和多租户 Header
    // 对于 FormData，不设置 Content-Type，让 axios 自动处理 multipart/form-data 边界
    const config = this.getBotRequestConfig();
    // 明确删除 Content-Type，让浏览器自动设置正确的 multipart/form-data 和 boundary
    if (config.headers) {
      delete config.headers['Content-Type'];
    }
    const data = await apiClient.post<SessionAttachment>(`${API_BASE_URL}/files/upload`, formData as any, config);
    return data;
  }

  async deleteSession(chatId: string): Promise<boolean> {
    try {
      const config = this.getOpenClawRequestConfig();
      await apiClient.delete(`${API_BASE_URL}/sessions/${chatId}`, config);
      this.removeLocalSession(chatId);
      return true;
    } catch (error: any) {
      if (error?.status === 404) return true;
      if (this.isSessionServiceUnavailable(error)) {
        this.removeLocalSession(chatId);
        return true;
      }
      return false;
    }
  }

  async updateSessionTitle(chatId: string, title: string): Promise<boolean> {
    try {
      const config = this.getOpenClawRequestConfig();
      await apiClient.put(`${API_BASE_URL}/sessions/${chatId}/title`, {
        title,
        user_id: this.userId,
      }, config);
      const sessions = this.readLocalSessions().map((session) =>
        session.id === chatId ? { ...session, name: title, updated_at: new Date().toISOString() } : session
      );
      this.writeLocalSessions(sessions);
      return true;
    } catch (error) {
      if (this.isSessionServiceUnavailable(error)) {
        const sessions = this.readLocalSessions().map((session) =>
          session.id === chatId ? { ...session, name: title, updated_at: new Date().toISOString() } : session
        );
        this.writeLocalSessions(sessions);
        return true;
      }
      return false;
    }
  }

  async healthCheck(): Promise<OpenClawHealthResponse> {
    try {
      const config = this.getOpenClawRequestConfig();
      const data = await apiClient.get<OpenClawHealthResponse>(`${API_BASE_URL}/health`, {}, config);
      return data;
    } catch (error: any) {
      if (!this.isSessionServiceUnavailable(error)) {
        console.error('[QuantBot] healthCheck failed:', {
          message: error?.message,
          status: error?.status,
          data: error?.data,
          baseURL: API_BASE_URL,
        });
      }
      return {
        status: 'unhealthy',
        service: 'openclaw-gateway',
        components: {
          api: { status: 'unhealthy' },
          copaw: { status: 'unreachable', error: 'health request failed' },
        },
      };
    }
  }

  getChatId(): string | null {
    const id = window.localStorage.getItem(`quantbot:chat:${this.userId}`);
    return this.isUUID(id) ? id : null;
  }

  setChatId(id: string): void {
    if (this.isUUID(id)) {
      window.localStorage.setItem(`quantbot:chat:${this.userId}`, id);
    }
  }

  resetSession(): void {
    window.localStorage.removeItem(`quantbot:chat:${this.userId}`);
  }

  async getActiveTasks(): Promise<any[]> {
    return [];
  }
}

export const agentApi = new QuantBotApiService();
export default agentApi;
