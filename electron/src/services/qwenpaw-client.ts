/**
 * QuantBot AI 助手客户端 (对接 QuantMind 原生 LLM + RD-Agent)
 * 路径: electron/src/services/qwenpaw-client.ts
 */

import { authService } from '../features/auth/services/authService';

export interface ChatMessage {
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  created_at?: string;
}

export interface SendMessageOptions {
  onChunk?: (text: string) => void;
  onComplete?: (fullText: string) => void;
  onError?: (error: any) => void;
  onTaskStarted?: (taskId: string, answer: string) => void;
  chatId?: string;
}

export interface ChatSession {
  id: string;
  name: string;
  user_id: string;
  created_at: string;
}

export interface QuantBotTask {
  task_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  progress?: string;
  result?: {
    factors?: Array<Record<string, unknown>>;
    total_factors?: number;
    summary?: Record<string, unknown>;
  };
  error_message?: string;
  factor_ids?: string[];
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
}

export class QwenPawClient {
  private apiBase: string;
  private userId: string;
  private channel: string;

  constructor(userId: string, channel: string = 'quantbot') {
    this.apiBase = '/api/v1/quantbot';
    this.userId = userId;
    this.channel = channel;
  }

  private getHeaders() {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-User-Id': this.userId,
      'X-Channel': this.channel,
    };

    const token = authService.getAccessToken();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    return headers;
  }

  /**
   * 获取会话列表（兼容旧接口，QuantBot 不维护会话列表）
   */
  async listChats(): Promise<ChatSession[]> {
    return [];
  }

  /**
   * 创建新会话（兼容旧接口）
   */
  async createChat(name: string = '新对话'): Promise<ChatSession> {
    return {
      id: 'default',
      name,
      user_id: this.userId,
      created_at: new Date().toISOString(),
    };
  }

  /**
   * 发送消息 (SSE 流式)
   *
   * 后端可能返回两种响应：
   * 1. SSE 流式文本（一般对话）
   * 2. JSON { intent, task_id, answer }（因子挖掘任务）
   */
  async sendMessage(content: string, options: SendMessageOptions) {
    let fullText = '';

    try {
      const response = await fetch(`${this.apiBase}/chat`, {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({
          message: content,
          history: [],
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
      }

      // 判断响应类型
      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('text/event-stream')) {
        // SSE 流式文本
        const reader = response.body!.getReader();
        const decoder = new TextDecoder();

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          const chunk = decoder.decode(value);
          const lines = chunk.split('\n');

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || !trimmed.startsWith('data: ')) continue;

            const data = trimmed.slice(6);
            if (data === '[DONE]') break;

            try {
              const parsed = JSON.parse(data);
              if (parsed.delta) {
                fullText += parsed.delta;
                options.onChunk?.(parsed.delta);
              }
              if (parsed.error) {
                options.onError?.(new Error(parsed.error));
                return;
              }
            } catch {
              // 忽略非 JSON 数据
            }
          }
        }

        options.onComplete?.(fullText);
      } else {
        // JSON 响应（因子挖掘任务启动）
        const json = await response.json();
        if (json.task_id) {
          options.onTaskStarted?.(json.task_id, json.answer || '任务已启动');
        } else if (json.answer) {
          options.onComplete?.(json.answer);
        }
      }
    } catch (error) {
      console.error('QuantBot sendMessage error:', error);
      options.onError?.(error);
    }
  }

  /**
   * 获取任务状态
   */
  async getTask(taskId: string): Promise<QuantBotTask | null> {
    try {
      const res = await fetch(`${this.apiBase}/task/${taskId}`, {
        headers: this.getHeaders(),
      });
      if (!res.ok) return null;
      return res.json();
    } catch (error) {
      console.error('QuantBot getTask error:', error);
      return null;
    }
  }

  /**
   * 列出所有任务
   */
  async listTasks(): Promise<QuantBotTask[]> {
    try {
      const res = await fetch(`${this.apiBase}/tasks`, {
        headers: this.getHeaders(),
      });
      if (!res.ok) return [];
      const data = await res.json();
      return data.tasks || [];
    } catch (error) {
      console.error('QuantBot listTasks error:', error);
      return [];
    }
  }

  /**
   * 获取聊天历史（QuantBot 暂不维护持久化历史）
   */
  async getChatHistory(chatId: string): Promise<ChatMessage[]> {
    return [];
  }

  /**
   * 删除会话（兼容旧接口）
   */
  async deleteChat(chatId: string): Promise<boolean> {
    return true;
  }
}
