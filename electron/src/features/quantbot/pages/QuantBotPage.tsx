/**
 * QuantBot 主页面 — 完整嵌入 QwenPaw 智能体 Web 界面
 *
 * QwenPaw 是项目的大脑，提供完整 AI 智能体能力：
 * 执行命令、写代码、跑回测、跑因子挖掘、获取股票数据 AI 分析、获取新闻数据等。
 *
 * 加载策略：
 * - Web 浏览器端：通过当前域名的 /api/v1/qwenpaw-ui/ 反向代理（局域网访问同样适用）
 * - Electron 桌面端：通过用户配置的服务器地址代理；未配置时回退本机 8088
 */

import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { Bot, RefreshCw, Wifi, WifiOff, ExternalLink, AlertTriangle } from 'lucide-react';
import { isElectronEnv, SERVICE_URLS } from '../../../config/services';

const QWENPAW_UI_PATH = '/api/v1/qwenpaw-ui/';
/** 无任何服务器配置时的兜底地址（QwenPaw 容器宿主映射端口） */
const QWENPAW_LOCAL_FALLBACK_URL = 'http://127.0.0.1:8088/';

/** iframe 加载超时时间（毫秒） */
const IFRAME_LOAD_TIMEOUT_MS = 15_000;

const QuantBotPage: React.FC = () => {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [iframeKey, setIframeKey] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(true);
  const [connected, setConnected] = useState<boolean>(false);
  const [timedOut, setTimedOut] = useState<boolean>(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const embedUrl = useMemo(() => {
    // Web 环境：页面由服务器（Nginx 等）托管，QwenPaw 一律走当前域名的
    // /api/v1/qwenpaw-ui/ 反向代理，避免按 hostname 拼 8088 端口导致拒绝连接。
    if (!isElectronEnv()) {
      return QWENPAW_UI_PATH;
    }

    // Electron 的页面宿主通常是 localhost，但 QwenPaw 部署在用户配置的
    // 远端服务器。因此桌面端不能根据 window.location.hostname 回退到
    // 127.0.0.1:8088；必须优先使用已配置的 API 网关代理。
    const gateway = SERVICE_URLS.API_GATEWAY;
    if (gateway) {
      return `${gateway}${QWENPAW_UI_PATH}`;
    }

    return QWENPAW_LOCAL_FALLBACK_URL;
  }, []);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const handleReload = useCallback(() => {
    setLoading(true);
    setConnected(false);
    setTimedOut(false);
    clearTimer();
    setIframeKey(iframeKey + 1);
  }, [clearTimer]);

  const handleOpenExternal = useCallback(() => {
    window.open(embedUrl, '_blank');
  }, [embedUrl]);

  const handleIframeLoad = useCallback(() => {
    clearTimer();
    setLoading(false);
    setConnected(true);
    setTimedOut(false);
  }, [clearTimer]);

  const handleIframeError = useCallback(() => {
    clearTimer();
    setLoading(false);
    setConnected(false);
    setTimedOut(true);
  }, [clearTimer]);

  useEffect(() => {
    setLoading(true);
    setConnected(false);
    setTimedOut(false);
    clearTimer();

    // 启动超时计时器：如果 iframe 在指定时间内未触发 onLoad，标记为超时
    timerRef.current = setTimeout(() => {
      setLoading(false);
      setTimedOut(true);
    }, IFRAME_LOAD_TIMEOUT_MS);

    return () => {
      clearTimer();
    };
  }, [iframeKey, clearTimer]);

  return (
    <div className="w-full h-full flex flex-col overflow-hidden bg-[#f8fafc] pt-12 pb-[74px] px-3 sm:px-4">
      {/* 顶部工具栏 — 清爽融合，规避 TitleBar 遮挡 */}
      <div className="h-10 flex-shrink-0 bg-white border border-slate-200/80 rounded-t-xl px-4 flex items-center justify-between shadow-xs">
        <div className="flex items-center gap-2.5">
          <div className="w-5 h-5 rounded-md bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-xs">
            <Bot className="w-3.5 h-3.5 text-white" />
          </div>
          <span className="text-xs font-bold text-slate-800 tracking-tight">QuantBot · QwenPaw</span>
          <span className="text-[10px] font-medium text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded">AI 智能助理</span>
        </div>

        <div className="flex items-center gap-2">
          <div className={`flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full ${
            connected 
              ? 'bg-emerald-50 text-emerald-600 border border-emerald-200/60' 
              : timedOut 
                ? 'bg-rose-50 text-rose-600 border border-rose-200/60' 
                : loading 
                  ? 'bg-amber-50 text-amber-600 border border-amber-200/60' 
                  : 'bg-slate-100 text-slate-500'
          }`}>
            {connected ? (
              <Wifi className="w-3 h-3 text-emerald-500" />
            ) : timedOut ? (
              <AlertTriangle className="w-3 h-3 text-rose-500" />
            ) : loading ? (
              <div className="w-3 h-3 border-2 border-amber-500 border-t-transparent rounded-full animate-spin" />
            ) : (
              <WifiOff className="w-3 h-3 text-slate-400" />
            )}
            <span className="text-[11px]">{connected ? '已连接' : timedOut ? '连接超时' : loading ? '连接中…' : '断开'}</span>
          </div>

          <button
            onClick={handleOpenExternal}
            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:text-blue-600 hover:bg-slate-100 transition-colors"
            title="在外部浏览器打开"
          >
            <ExternalLink className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={handleReload}
            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:text-blue-600 hover:bg-slate-100 transition-colors"
            title="重新加载"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* iframe 内容区域 — 避开底部 Dock 悬浮栏 */}
      <div className="flex-1 relative overflow-hidden bg-white border-x border-b border-slate-200/80 rounded-b-xl shadow-xs">
        {loading && !timedOut && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/90 backdrop-blur-xs">
            <div className="flex flex-col items-center gap-3">
              <div className="w-10 h-10 border-3 border-blue-500 border-t-transparent rounded-full animate-spin" />
              <div className="text-center">
                <p className="text-xs font-semibold text-slate-700">QwenPaw 智能体加载中…</p>
                <p className="text-[11px] text-slate-400 mt-0.5">AI Brain · Code · Backtest · Factor · Data</p>
              </div>
            </div>
          </div>
        )}

        {timedOut && !connected && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/95">
            <div className="flex flex-col items-center gap-3.5 max-w-md text-center px-4">
              <div className="w-12 h-12 rounded-full bg-rose-50 border border-rose-100 flex items-center justify-center">
                <AlertTriangle className="w-6 h-6 text-rose-500" />
              </div>
              <div>
                <p className="text-sm font-bold text-slate-800">QwenPaw 服务未响应</p>
                <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
                  请检查后端 QwenPaw 容器状态：
                </p>
                <code className="block mt-2 px-3 py-1.5 bg-slate-50 border border-slate-200 rounded-lg text-xs text-emerald-600 font-mono">
                  docker compose up -d qwenpaw
                </code>
              </div>
              <button
                onClick={handleReload}
                className="flex items-center gap-1.5 mt-1 px-4 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium transition-colors shadow-xs"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                重新连接
              </button>
            </div>
          </div>
        )}

        <iframe
          ref={iframeRef}
          key={iframeKey}
          src={embedUrl}
          className="w-full h-full border-0"
          title="QwenPaw Agent"
          allow="clipboard-read; clipboard-write; fullscreen; microphone; camera"
          sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox allow-modals allow-presentation"
          onLoad={handleIframeLoad}
          onError={handleIframeError}
        />
      </div>
    </div>
  );
};

export default QuantBotPage;
