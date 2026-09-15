import React, { useState, useEffect } from 'react';
import { useSelector } from 'react-redux';

import { useRealtimeData } from '../../hooks/useRealtimeData';
import { Wifi, ShieldCheck } from 'lucide-react';
import { MarketSelector } from './MarketSelector';
import { motion } from 'framer-motion';
import { selectCurrentTab } from '../../store/slices/aiStrategySlice';
import { useAppDispatch, useAppSelector } from '../../store';
import { setTradingMode } from '../../store/slices/uiSlice';

const TRADING_MODE_PREF_KEY = 'qm:trading_mode_pref';
import { SERVICE_URLS } from '../../config/services';

export const HeaderBar: React.FC = () => {
  const [currentTime, setCurrentTime] = useState(new Date());
  const [apiStatus, setApiStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');
  const [networkLatency, setNetworkLatency] = useState<number>(0);
  const currentTab = useSelector(selectCurrentTab);
  const dispatch = useAppDispatch();
  const tradingMode = useAppSelector((state) => state.ui.tradingMode);

  const { isConnected: realtimeConnected } = useRealtimeData({
    enabled: false,
    interval: 15000,
  });

  useEffect(() => {
    const timer = setInterval(() => setCurrentTime(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const checkLatency = async () => {
      const start = Date.now();
      try {
        const healthUrl = `${SERVICE_URLS.API_GATEWAY}/health`;

        await fetch(healthUrl, { mode: 'no-cors', cache: 'no-cache' });
        const end = Date.now();
        setNetworkLatency(end - start);
        setApiStatus('connected');
      } catch (error) {
        console.error('延时检测失败:', error);
        setApiStatus('disconnected');
      }
    };

    checkLatency();
    const latencyTimer = setInterval(checkLatency, 10000);

    return () => clearInterval(latencyTimer);
  }, []);

  const handleModeSwitch = (mode: 'real' | 'simulation'): void => {
    localStorage.setItem(TRADING_MODE_PREF_KEY, mode);
    dispatch(setTradingMode(mode));
  };

  return (
    <div className="relative px-8 pt-6 pb-2 grid grid-cols-3 items-center bg-transparent">
      <div className="flex items-center gap-4 justify-start flex-nowrap overflow-hidden">
        <motion.div
          initial={{ opacity: 0, x: -10 }}
          animate={{ opacity: 1, x: 0 }}
          className={`flex items-center gap-2.5 px-3.5 py-1.5 rounded-full border shadow-sm backdrop-blur-md transition-all min-w-0 shrink ${
            apiStatus === 'connected'
              ? 'bg-white/72 border-emerald-100/80 text-emerald-700 shadow-[0_8px_22px_rgba(16,185,129,0.08)]'
              : 'bg-white/72 border-red-100/80 text-red-700 shadow-[0_8px_22px_rgba(239,68,68,0.06)]'
          }`}
        >
          <div className="relative">
            <Wifi className={`w-4 h-4 ${apiStatus === 'connected' ? 'text-green-500' : 'text-red-500'}`} />
            {apiStatus === 'connected' && (
              <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-green-400 rounded-full animate-ping" />
            )}
          </div>
          {apiStatus === 'connected' && (
            <span className="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-white/60 text-slate-500 border border-slate-100">
              {networkLatency}MS
            </span>
          )}
        </motion.div>

        {realtimeConnected && (
          <motion.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white/68 border border-slate-200/80 text-slate-600 shadow-[0_8px_22px_rgba(15,23,42,0.05)]"
          >
            <ShieldCheck className="w-3.5 h-3.5 text-indigo-500" />
            <span className="text-[11px] font-semibold uppercase tracking-[0.18em]">Secure</span>
          </motion.div>
        )}

        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="flex items-center ml-2 shrink-0"
        >
          <MarketSelector />
        </motion.div>

        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="flex items-center ml-2 shrink-0"
        >
          <button
            type="button"
            role="switch"
            aria-checked={tradingMode === 'simulation'}
            aria-label={`当前交易模式：${tradingMode === 'real' ? '实盘' : '模拟盘'}，点击切换`}
            onClick={() => handleModeSwitch(tradingMode === 'real' ? 'simulation' : 'real')}
            className={`relative flex h-8 w-[72px] shrink-0 items-center rounded-full border px-1 transition-all focus:outline-none shadow-sm ${tradingMode === 'simulation'
              ? 'border-emerald-200 bg-emerald-50/50'
              : 'border-blue-200 bg-blue-50/50'
              }`}
            title={tradingMode === 'real' ? '切换仪表盘数据源：模拟盘' : '切换仪表盘数据源：实盘'}
          >
            <span
              className={`absolute top-0.5 bottom-0.5 w-[32px] rounded-full shadow-sm transition-transform duration-200 ${tradingMode === 'simulation'
                ? 'translate-x-[32px] bg-emerald-500'
                : 'translate-x-0 bg-blue-500'
                }`}
            />
            <span className="relative z-10 flex w-full items-center justify-between px-1.5 text-[9px] font-black tracking-tighter">
              <span className={tradingMode === 'real' ? 'text-white' : 'text-slate-400'}>REAL</span>
              <span className={tradingMode === 'simulation' ? 'text-white' : 'text-slate-400'}>SIM</span>
            </span>
          </button>
        </motion.div>
      </div>

      <div className="flex flex-col items-center justify-center text-center">
        <h1
          className="text-[32px] font-black tracking-[-0.045em] text-slate-800 mb-1 leading-none"
          style={{ fontFamily: 'Outfit, sans-serif' }}
        >
          QuantMind
        </h1>
        <div className="flex items-center gap-2 text-slate-400 text-[10px] font-semibold uppercase tracking-[0.22em]">
          <span>{currentTab === 'dashboard' ? 'Real-time' : 'Workspace'}</span>
          <span className="w-1 h-1 rounded-full bg-slate-300/90" />
          <span>Intelligence</span>
          <span className="w-1 h-1 rounded-full bg-slate-300/90" />
          <span>Precision</span>
        </div>
      </div>

      <div className="flex flex-col items-end justify-center">
        <div className="rounded-[20px] border border-white/70 bg-white/54 backdrop-blur-md px-5 py-2.5 shadow-[0_10px_28px_rgba(15,23,42,0.05)]">
          <div className="flex items-baseline gap-1 font-mono text-slate-800 justify-center">
            <span className="text-[30px] font-black tracking-[-0.05em] leading-none">
              {currentTime.getHours().toString().padStart(2, '0')}:
              {currentTime.getMinutes().toString().padStart(2, '0')}
            </span>
            <span className="text-xs font-bold opacity-55">
              {currentTime.getSeconds().toString().padStart(2, '0')}
            </span>
          </div>
          <div className="text-[10px] font-bold text-slate-400 uppercase tracking-[0.22em] mt-1 text-center">
            {currentTime.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
          </div>
        </div>
      </div>
    </div>
  );
};
