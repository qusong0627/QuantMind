/**
 * QuantBot 主页面
 */

import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { useDispatch } from 'react-redux';
import { useSelector } from 'react-redux';
import { Bot, HelpCircle, Radio } from 'lucide-react';
import ChatContainer from '../components/ChatArea/ChatContainer';
import TaskPanelContainer from '../components/TaskPanel/TaskPanelContainer';
import agentApi from '../services/agentApi';
import { useSessionStore } from '../store/sessionStore';
import { addMessage, clearMessages } from '../store/chatSlice';
import { RootState } from '../../../store';

const HEALTH_CHECK_INTERVAL_MS = 30000;

const QuantBotPage: React.FC = () => {
  const dispatch = useDispatch();
  const currentSessionId = useSessionStore(state => state.currentSessionId);
  const isAuthReady = useSelector((state: RootState) => state.auth.isInitialized && state.auth.isAuthenticated);
  const [apiOnline, setApiOnline] = useState<boolean>(false);

  useEffect(() => {
    if (!isAuthReady) {
      setApiOnline(false);
      return;
    }

    let disposed = false;

    const syncHealth = async () => {
      try {
        await agentApi.healthCheck();
        if (!disposed) {
          setApiOnline(true);
        }
      } catch {
        if (!disposed) {
          setApiOnline(false);
        }
      }
    };

    void syncHealth();
    const timer = window.setInterval(() => {
      void syncHealth();
    }, HEALTH_CHECK_INTERVAL_MS);

    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [isAuthReady]);

  useEffect(() => {
    if (!isAuthReady) {
      dispatch(clearMessages());
      return;
    }

    let disposed = false;

    const loadSessionMessages = async () => {
      if (!currentSessionId) {
        dispatch(clearMessages());
        return;
      }

      try {
        const messages = await agentApi.getSessionMessages(currentSessionId);
        if (disposed) return;
        
        dispatch(clearMessages());
        messages.forEach((message, index) => {
          dispatch(addMessage({
            id: message.id || `${currentSessionId}-${index}-${Date.now()}`,
            type: message.role === 'user' ? 'user' : 'ai',
            content: message.content,
            timestamp: message.timestamp || new Date().toISOString(),
            status: 'sent',
          }));
        });
      } catch (error: any) {
        if (!disposed) {
          if (error?.response?.status !== 401) {
            console.error('[QuantBot] Load messages failed:', error);
          }
          // 如果是 404，说明该 ID 在后端已失效
          if (error.response?.status === 404) {
             useSessionStore.getState().setCurrentSessionId(null);
             useSessionStore.getState().fetchSessions();
          }
          dispatch(clearMessages());
        }
      }
    };

    void loadSessionMessages();

    return () => {
      disposed = true;
    };
  }, [currentSessionId, dispatch, isAuthReady]);

  return (
    <div className="w-full h-full bg-[#f8fafc] p-6 flex flex-col items-center justify-center overflow-auto custom-scrollbar">
      <div
        className="w-full h-full rounded-[32px] bg-white border border-gray-200 shadow-sm flex flex-col flex-shrink-0 overflow-hidden"
      >
        {/* 顶部标题栏 - 对齐回测中心 */}
        <div className="h-[60px] flex-shrink-0 bg-white border-b border-gray-200 px-6 flex items-center justify-between z-10">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center shadow-md">
              <Bot className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-lg font-bold text-gray-800 leading-tight">QuantBot</h1>
              <p className="text-[10px] text-gray-400 uppercase tracking-wider font-semibold">Intelligent Financial Assistant</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <div className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-all ${
              apiOnline
                ? 'border-blue-200 bg-blue-50 text-blue-700'
                : 'border-slate-200 bg-slate-50 text-slate-500'
            }`}>
              <Radio className={`h-3.5 w-3.5 ${apiOnline ? 'animate-pulse' : ''}`} />
              <span>{apiOnline ? '网关在线' : '网关不可达'}</span>
            </div>

          </div>
        </div>

        {/* 主要内容区域 - 三栏布局 */}
        <div className="flex-1 flex flex-col overflow-hidden bg-gray-50/30">
          <div className="mx-4 mt-3 mb-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-700">
            当前为 QuantBot 测试接入版本，已启用聊天与会话管理；任务系统与高级管理能力暂不开放。
          </div>
          <div className="flex-1 flex overflow-hidden">
            <div className="w-[300px] bg-white border-r border-gray-200 flex flex-col overflow-hidden">
              <div className="flex-1 overflow-hidden">
                <TaskPanelContainer />
              </div>

              {/* 底部帮助中心链接 */}
              <div className="border-t border-gray-200 p-2">
                <a
                  href="https://www.quantmindai.cn/help"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="w-full flex items-center gap-3 px-4 py-3 rounded-2xl text-gray-600 hover:text-blue-600 hover:bg-blue-50 transition-colors"
                >
                  <HelpCircle className="w-5 h-5" />
                  <span className="text-sm">帮助中心</span>
                </a>
              </div>
            </div>

            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.3 }}
              className="flex-1 flex flex-col overflow-hidden bg-white"
            >
              <ChatContainer />
            </motion.div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default QuantBotPage;
