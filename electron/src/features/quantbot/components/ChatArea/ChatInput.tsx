/**
 * QuantBot 聊天输入框组件 (重构版)
 * 路径: electron/src/features/quantbot/components/ChatArea/ChatInput.tsx
 */

import React, { useRef, KeyboardEvent } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { Send, X } from 'lucide-react';
import { motion } from 'framer-motion';
import { RootState } from '../../../../store';
import { addMessage, clearInput, setInputValue, setLoading, setTyping, updateMessage } from '../../store/chatSlice';
import agentApi from '../../services/agentApi';
import { useSessionStore } from '../../store/sessionStore';

const getFriendlyQuantBotErrorMessage = (error: any): string => {
  const code = String(error?.code || '').toUpperCase();
  const message = String(error?.message || '');
  const userMessage = String(error?.userMessage || '').trim();
  if (userMessage) return userMessage;
  if (code === 'PROVIDER_ERROR' && /no active model configured/i.test(message)) {
    return '当前未配置可用模型，请先在 QuantBot 管理端启用一个模型后再试。';
  }
  return message ? `对话中断: ${message}` : '对话中断: 上游服务暂时不可用';
};

const ChatInput: React.FC = () => {
  const dispatch = useDispatch();
  const inputValue = useSelector((state: RootState) => state.quantbotChat?.inputValue || '');
  const isLoading = useSelector((state: RootState) => state.quantbotChat?.isLoading || false);
  const isAuthReady = useSelector((state: RootState) => state.auth.isInitialized && state.auth.isAuthenticated);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    dispatch(setInputValue(e.target.value));

    // 自动调整高度
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 150)}px`;
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = async () => {
    if (!inputValue.trim() || isLoading) return;
    if (!isAuthReady) {
      dispatch(addMessage({
        id: `error-auth-${Date.now()}`,
        type: 'system' as const,
        content: '❌ 当前未登录或登录状态未就绪，请重新登录后再发送消息。',
        timestamp: new Date().toISOString(),
      }));
      return;
    }

    const messageContent = inputValue.trim();
    dispatch(clearInput());

    // 重置高度
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
    dispatch(setLoading(true));
    dispatch(setTyping(true));

    try {
      const sessionStore = useSessionStore.getState();
      let chatId = sessionStore.currentSessionId;
      
      if (!chatId) {
        const session = await sessionStore.createSession('新对话');
        chatId = session.id || (session as any).session_id;
      }

      dispatch(addMessage({
        id: `user-${Date.now()}`,
        type: 'user' as const,
        content: messageContent,
        timestamp: new Date().toISOString(),
      }));

      const assistantId = `ai-${Date.now()}`;
      dispatch(addMessage({
        id: assistantId,
        type: 'ai' as const,
        content: '',
        timestamp: new Date().toISOString(),
        status: 'sending',
      }));

      let accumulated = '';

      // 调用流式接口
      await agentApi.sendMessageStream(messageContent, {
        chatId: chatId || undefined,
        onChunk: (chunk) => {
          accumulated += chunk;
          dispatch(updateMessage({
            id: assistantId,
            updates: { content: accumulated, status: 'sending' },
          }));
        },
        onComplete: async (fullText) => {
          dispatch(updateMessage({
            id: assistantId,
            updates: { content: fullText, status: 'sent' },
          }));
          
          // 如果是首条消息（名称为新对话），则更新标题
          const currentSession = useSessionStore.getState().sessions.find(s => 
            (s.id === chatId || (s as any).session_id === chatId)
          );
          
          if (currentSession && (currentSession.name === '新对话' || !currentSession.name)) {
             const nextTitle = messageContent.length > 20 ? `${messageContent.slice(0, 20)}...` : messageContent;
             await useSessionStore.getState().updateSessionTitle(chatId!, nextTitle);
          }
          
          // 延迟刷新列表，避免后端写入延迟导致的重复/丢失
          setTimeout(() => {
            useSessionStore.getState().fetchSessions();
          }, 1000);
        },
        onError: (err) => {
           const friendlyMessage = getFriendlyQuantBotErrorMessage(err);
           if (String(err?.code || '').toUpperCase() === 'PROVIDER_ERROR') {
             console.warn('[QuantBot] Stream provider error:', {
               code: err?.code,
               message: err?.message,
               userMessage: err?.userMessage,
             });
           } else {
             console.error('Stream error:', err);
           }
           dispatch(updateMessage({
             id: assistantId,
             updates: { content: accumulated + `\n\n⚠️ ${friendlyMessage}`, status: 'error' },
           }));
        }
      });

    } catch (error: any) {
      console.error('Send message error:', error);
      dispatch(addMessage({
        id: `error-${Date.now()}`,
        type: 'system' as const,
        content: `❌ 发送失败: ${error.message}`,
        timestamp: new Date().toISOString(),
      }));
    } finally {
      dispatch(setLoading(false));
      dispatch(setTyping(false));
    }
  };

  const handleClear = () => {
    dispatch(clearInput());
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.focus();
    }
  };


  return (
    <div className="space-y-3">

      {/* 输入框 */}
      <div className="relative">
        <textarea
          ref={textareaRef}
          value={inputValue}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          placeholder="输入您的问题或指令... (Enter发送, Shift+Enter换行)"
          className="w-full px-4 py-3.5 pr-12 border-2 border-slate-100 rounded-xl resize-none focus:border-blue-400 focus:ring-4 focus:ring-blue-500/10 transition-all outline-none bg-slate-50/50 hover:bg-white focus:bg-white text-slate-700"
          rows={2}
          disabled={isLoading}
        />

        {inputValue && (
          <motion.button
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            onClick={handleClear}
            className="absolute right-3.5 top-3.5 p-1.5 hover:bg-rose-50 hover:text-rose-500 text-slate-400 rounded-lg transition-all"
            title="清空"
          >
            <X className="w-4 h-4" />
          </motion.button>
        )}
      </div>


      {/* 操作按钮 */}
      <div className="flex items-center justify-between px-1">
        <div className="flex items-center gap-3">
          <div className="text-[11px] text-slate-400 font-medium flex items-center gap-1.5 ml-1">
            <div className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse" />
            <span>QuantBot 已就绪，为您提供智能量化辅助</span>
          </div>
        </div>

        <motion.button
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
          onClick={handleSend}
          disabled={!inputValue.trim() || isLoading}
          className={`flex items-center gap-2 px-6 py-2 rounded-xl font-bold text-sm transition-all ${inputValue.trim() && !isLoading
            ? 'bg-gradient-to-r from-blue-600 to-indigo-600 text-white shadow-lg shadow-blue-500/20 hover:shadow-blue-500/30'
            : 'bg-slate-100 text-slate-300 cursor-not-allowed'
            }`}
        >
          {isLoading ? (
            <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
          ) : (
            <Send className="w-4 h-4" />
          )}
          <span>{isLoading ? '处理中...' : '发送'}</span>
        </motion.button>
      </div>
    </div>
  );
};

export default ChatInput;
