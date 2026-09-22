/**
 * QuantBot 嵌入面 — dsh（DeepSeek Harness）Web 界面的可复用载体。
 *
 * 从 QuantBotPage 里提出来的：**整页形态**（/quantbot 路由）与实盘交易页里的
 * **QuantBot 栏**（`features/local-live/LiveTradingPage.tsx`，挂在侧栏「设置」下面）
 * 用的是同一个 iframe、同一套加载/超时遮罩。两处各写一遍必然漂移 —— 改了这边的
 * 超时提示，那边还是旧的（这个坑刚在打包脚本上踩过）。
 *
 * 只负责「面」：地址推导、加载状态、遮罩、iframe。顶栏与弹窗由调用方自己排。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { isElectronEnv, SERVICE_URLS } from '../../../config/services';
import { LIVE_NODE_ONLY } from '../../../config/liveNodeFlags';

/** 无任何服务器配置时的兜底地址（dsh 容器宿主映射端口） */
const QWENPAW_LOCAL_FALLBACK_URL = 'http://127.0.0.1:8088/';

/** dsh 直连 Web UI 端口（quantmind-dsh 容器的宿主映射端口） */
const QWENPAW_DIRECT_PORT = 8088;

/** iframe 加载超时时间（毫秒） */
const IFRAME_LOAD_TIMEOUT_MS = 15_000;

/**
 * 推导 dsh 直连 Web UI 地址（供“在外部浏览器打开”使用）。
 * 基于已配置的 API 网关地址（如 http://1.2.3.4:8000）取同名主机、换到 8088 端口，
 * 直接打开 dsh 容器自身托管的界面；未配置网关时回退本机。
 */
export function getQwenPawDirectUrl(): string {
  const gateway = SERVICE_URLS.API_GATEWAY;
  if (!gateway) return QWENPAW_LOCAL_FALLBACK_URL;
  try {
    const u = new URL(gateway);
    u.port = String(QWENPAW_DIRECT_PORT);
    u.pathname = '/';
    u.search = '';
    u.hash = '';
    return u.toString();
  } catch {
    return `${gateway.replace(/\/+$/, '')}:${QWENPAW_DIRECT_PORT}/`;
  }
}

export interface QuantBotFrameState {
  embedUrl: string;
  iframeKey: number;
  loading: boolean;
  connected: boolean;
  timedOut: boolean;
  reload: () => void;
  openExternal: () => void;
  handleIframeLoad: () => void;
  handleIframeError: () => void;
}

/** iframe 的地址推导与加载状态机；顶栏（状态徽标/刷新按钮）从这里取状态。 */
export function useQuantBotFrame(): QuantBotFrameState {
  const [iframeKey, setIframeKey] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(true);
  const [connected, setConnected] = useState<boolean>(false);
  const [timedOut, setTimedOut] = useState<boolean>(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const embedUrl = useMemo(() => {
    // Electron：dsh 部署在用户配置的远端服务器，直连其 8088。
    // 直连时 dsh SPA 自身的 /api 与 WebSocket 都走 ip:8088 同源，
    // 实时推送可原生工作，无需网关代理及其路径重写。
    if (isElectronEnv()) {
      return getQwenPawDirectUrl();
    }

    // Web：dsh 部署在提供页面的同一台服务器，按当前主机名直连 8088，
    // 避免走网关代理导致实时（WebSocket）链路不稳定。
    if (typeof window !== 'undefined' && window.location?.hostname) {
      return `http://${window.location.hostname}:8088/`;
    }

    return QWENPAW_LOCAL_FALLBACK_URL;
  }, []);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const reload = useCallback(() => {
    setLoading(true);
    setConnected(false);
    setTimedOut(false);
    clearTimer();
    // 不用函数式 setter（本仓 tsc 下必报错），也不必读旧值：key 只要「每次刷新都不同」。
    setIframeKey(Date.now());
  }, [clearTimer]);

  const openExternal = useCallback(() => {
    window.open(getQwenPawDirectUrl(), '_blank');
  }, []);

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

  return {
    embedUrl,
    iframeKey,
    loading,
    connected,
    timedOut,
    reload,
    openExternal,
    handleIframeLoad,
    handleIframeError,
  };
}

const SURFACE_DEFAULT_CLASS =
  'flex-1 relative overflow-hidden bg-white border-x border-b border-slate-200/80 rounded-b-xl shadow-xs';

/**
 * iframe 本体 + 加载中 / 未响应两套遮罩。
 * 超时那屏同时承担「dsh 没起来」的排障指引，所以文案跟着部署形态走。
 */
export const QuantBotSurface: React.FC<{ frame: QuantBotFrameState; className?: string }> = ({
  frame,
  className,
}) => (
  <div className={className ?? SURFACE_DEFAULT_CLASS}>
    {frame.loading && !frame.timedOut && (
      <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/90 backdrop-blur-xs">
        <div className="flex flex-col items-center gap-3">
          <div className="w-10 h-10 border-3 border-blue-500 border-t-transparent rounded-full animate-spin" />
          <div className="text-center">
            <p className="text-xs font-semibold text-slate-700">QuantBot 智能体加载中…</p>
            <p className="text-[11px] text-slate-400 mt-0.5">AI Brain · Code · Backtest · Factor · Data</p>
          </div>
        </div>
      </div>
    )}

    {frame.timedOut && !frame.connected && (
      <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/95">
        <div className="flex flex-col items-center gap-3.5 max-w-md text-center px-4">
          <div className="w-12 h-12 rounded-full bg-rose-50 border border-rose-100 flex items-center justify-center">
            <AlertTriangle className="w-6 h-6 text-rose-500" />
          </div>
          <div>
            <p className="text-sm font-bold text-slate-800">QuantBot 服务未响应</p>
            {/* 两种部署形态的启动方式不同，提示跟着形态走：容器栈里 dsh 是 compose
                服务；实盘 Win 节点没有 Docker，dsh 是包内 node 载荷，由回环前门
                quantbot_front.py 拉起。提示一条本形态不存在的命令只会让人白折腾。 */}
            <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
              请确认 dsh 已经启动：
            </p>
            <code className="block mt-2 px-3 py-1.5 bg-slate-50 border border-slate-200 rounded-lg text-xs text-emerald-600 font-mono">
              {LIVE_NODE_ONLY ? '双击包根目录的 start-quantbot.bat' : 'docker compose up -d dsh'}
            </code>
          </div>
          <button
            onClick={frame.reload}
            className="flex items-center gap-1.5 mt-1 px-4 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium transition-colors shadow-xs"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            重新连接
          </button>
        </div>
      </div>
    )}

    <iframe
      key={frame.iframeKey}
      src={frame.embedUrl}
      className="w-full h-full border-0"
      title="QuantBot Agent"
      allow="clipboard-read; clipboard-write; fullscreen; microphone; camera"
      sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox allow-modals allow-presentation"
      onLoad={frame.handleIframeLoad}
      onError={frame.handleIframeError}
    />
  </div>
);
