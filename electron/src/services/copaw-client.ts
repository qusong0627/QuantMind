/**
 * QuantBot AI 助手客户端 (适配 QuantMind 网关)
 * 路径: electron/src/services/copaw-client.ts
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
  chatId?: string;
}

export interface ChatSession {
  id: string;
  name: string;
  user_id: string;
  created_at: string;
}

export class CopawClient {
  private apiBase: string;
  private userId: string;
  private channel: string;

  constructor(userId: string, channel: string = 'quantbot') {
    // 统一通过 Nginx 网关访问 (已由网关重写为 http://copaw:8088/api/)
    this.apiBase = '/api/v1/copaw'; 
    this.userId = userId;
    this.channel = channel;
  }

  private getHeaders() {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-User-Id': this.userId,
      'X-Channel': this.channel,
    };
    
    // 仅接受经过统一校验的访问令牌
    const token = authService.getAccessToken();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    
    return headers;
  }

  /**
   * 获取会话列表
   */
  async listChats(): Promise<ChatSession[]> {
    try {
      const res = await fetch(`${this.apiBase}/chats?user_id=${this.userId}`, {
        headers: this.getHeaders(),
      });
      if (!res.ok) throw new Error('Failed to list chats');
      return res.json();
    } catch (error) {
      console.error('QuantBot listChats error:', error);
      return [];
    }
  }

  /**
   * 创建新会话
   */
  async createChat(name: string = '新对话'): Promise<ChatSession> {
    const res = await fetch(`${this.apiBase}/chats`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({
        name,
        user_id: this.userId,
        channel: this.channel,
      }),
    });
    if (!res.ok) throw new Error('Failed to create chat');
    return res.json();
  }

  /**
   * 发送消息 (SSE 流式)
   */
  async sendMessage(content: string, options: SendMessageOptions) {
    let fullText = '';
    
    try {
      const response = await fetch(`${this.apiBase}/process`, {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({
          message: content,
          chat_id: options.chatId,
          user_id: this.userId,
          stream: true,
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
      }

      if (!response.body) throw new Error('No response body');

      const reader = response.body.getReader();
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
            if (parsed.text) {
              fullText += parsed.text;
              options.onChunk?.(parsed.text);
            }
          } catch (e) {
            // 忽略非 JSON 数据
          }
        }
      }

      options.onComplete?.(fullText);
    } catch (error) {
      console.error('QuantBot sendMessage error:', error);
      options.onError?.(error);
    }
  }

  /**
   * 获取聊天历史
   */
  async getChatHistory(chatId: string): Promise<ChatMessage[]> {
    try {
      const res = await fetch(`${this.apiBase}/chats/${chatId}`, {
        headers: this.getHeaders(),
      });
      if (!res.ok) throw new Error('Failed to get history');
      const data = await res.json();
      return data.messages || [];
    } catch (error) {
      console.error('QuantBot getChatHistory error:', error);
      return [];
    }
  }

  /**
   * 删除会话
   */
  async deleteChat(chatId: string): Promise<boolean> {
    try {
      const res = await fetch(`${this.apiBase}/chats/${chatId}`, {
        method: 'DELETE',
        headers: this.getHeaders(),
      });
      return res.ok;
    } catch (error) {
      return false;
    }
  }
}
