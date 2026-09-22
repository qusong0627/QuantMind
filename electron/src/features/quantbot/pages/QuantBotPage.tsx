/**
 * QuantBot 主页面 — 完整嵌入 dsh（DeepSeek Harness）智能体 Web 界面
 *
 * dsh 是项目的大脑，提供完整 AI 智能体能力：
 * 执行命令、写代码、跑回测、跑因子挖掘、获取股票数据 AI 分析、获取新闻数据等。
 *
 * 加载策略（地址推导与状态机在 components/QuantBotFrame.tsx）：
 * - Web 浏览器端：按当前主机名直连 8088（dsh 容器宿主映射端口，同源 SSE/WebSocket）
 * - Electron 桌面端：通过用户配置的服务器地址推导 8088；未配置时回退本机 8088
 *
 * 实盘节点形态下本页不再是主入口（节点没有底部导航；QuantBot 是实盘交易页侧栏里
 * 「设置」下面的一栏，见 features/local-live/LiveTradingPage.tsx），但路由保留 ——
 * 直接敲 /quantbot 仍可用，且与本页共用同一个 iframe 与加载状态机。
 */

import React, { useState } from 'react';
import { BookMarked, FileText } from 'lucide-react';
import { Bot, RefreshCw, SquareTerminal, Wifi, WifiOff, ExternalLink, AlertTriangle } from 'lucide-react';
import { isElectronEnv } from '../../../config/services';
import { LIVE_NODE_ONLY } from '../../../config/liveNodeFlags';
import { QuantBotSurface, useQuantBotFrame } from '../components/QuantBotFrame';
import PromptLibraryModal from '../components/PromptLibraryModal';
import ReportsModal from '../components/ReportsModal';
import AiIdeModal from '../components/AiIdeModal';
import { PROMPT_LIBRARY_TOTAL } from '../components/promptLibraryModel';

const QuantBotPage: React.FC = () => {
  const frame = useQuantBotFrame();
  // 提示词库（示例 34 条 + 模板 24 条，合并为一个弹窗）、调研报告档案、AI-IDE 策略台，
  // 均为居中弹窗，不占 iframe 布局
  const [showPrompts, setShowPrompts] = useState(false);
  const [showReports, setShowReports] = useState(false);
  const [showAiIde, setShowAiIde] = useState(false);

  return (
    // Electron 下顶部有 h-12 的 TitleBar 覆盖层需让位；Web 无 TitleBar，直接贴顶（此前统一 pt-12 留出 48px 空白）。
    // 底部留白是给悬浮 Dock 让位的实体占位（padding 对 absolute 覆盖层无效）：节点形态没有 Dock，留白一并去掉。
    <div
      className={`w-full h-full flex flex-col overflow-hidden bg-[#f8fafc] ${
        LIVE_NODE_ONLY ? 'pb-0' : 'pb-[74px]'
      } px-3 sm:px-4 ${isElectronEnv() ? 'pt-12' : 'pt-4'}`}
    >
      {/* 顶部工具栏 — 清爽融合，规避 TitleBar 遮挡 */}
      <div className="h-12 flex-shrink-0 bg-white border border-slate-200/80 rounded-t-xl px-4 flex items-center justify-between shadow-xs">
        <div className="flex items-center gap-3">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-xs">
            <Bot className="w-4 h-4 text-white" />
          </div>
          <span className="text-base font-bold text-slate-800 tracking-tight">QuantBot · DSH</span>
          <button
            type="button"
            onClick={() => setShowPrompts(true)}
            className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-500 hover:text-blue-600 transition-colors"
            title="提示词库：示例 + 技能模板，复制后粘贴到下方对话框"
          >
            <BookMarked className="w-4 h-4 text-indigo-500" />
            提示词库
            <span className="text-[11px] font-bold text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded">{PROMPT_LIBRARY_TOTAL}</span>
          </button>
          <button
            type="button"
            onClick={() => setShowReports(true)}
            className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-500 hover:text-blue-600 transition-colors"
            title="调研报告：QuantBot 生成的 md + PDF 自动归档，点击文件直接预览"
          >
            <FileText className="w-4 h-4 text-indigo-500" />
            调研报告
          </button>
          <button
            type="button"
            onClick={() => setShowAiIde(true)}
            className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-500 hover:text-blue-600 transition-colors"
            title="AI-IDE：策略工作台（写策略 / 调试 / 一键回测），与 QuantBot 对话联动"
          >
            <SquareTerminal className="w-4 h-4 text-indigo-500" />
            AI-IDE
          </button>
          <span className="text-xs font-medium text-slate-400 bg-slate-100 px-2 py-1 rounded">AI 智能助理</span>
        </div>

        <div className="flex items-center gap-2">
          <div className={`flex items-center gap-1.5 text-sm font-medium px-2.5 py-1 rounded-full ${
            frame.connected
              ? 'bg-emerald-50 text-emerald-600 border border-emerald-200/60'
              : frame.timedOut
                ? 'bg-rose-50 text-rose-600 border border-rose-200/60'
                : frame.loading
                  ? 'bg-amber-50 text-amber-600 border border-amber-200/60'
                  : 'bg-slate-100 text-slate-500'
          }`}>
            {frame.connected ? (
              <Wifi className="w-3.5 h-3.5 text-emerald-500" />
            ) : frame.timedOut ? (
              <AlertTriangle className="w-3.5 h-3.5 text-rose-500" />
            ) : frame.loading ? (
              <div className="w-3 h-3 border-2 border-amber-500 border-t-transparent rounded-full animate-spin" />
            ) : (
              <WifiOff className="w-3.5 h-3.5 text-slate-400" />
            )}
            <span className="text-xs">{frame.connected ? '已连接' : frame.timedOut ? '连接超时' : frame.loading ? '连接中…' : '断开'}</span>
          </div>

          <button
            onClick={frame.openExternal}
            className="flex items-center gap-1 rounded-md px-2.5 py-1.5 text-sm font-medium text-slate-600 hover:text-blue-600 hover:bg-slate-100 transition-colors"
            title="在外部浏览器打开"
          >
            <ExternalLink className="w-4 h-4" />
          </button>
          <button
            onClick={frame.reload}
            className="flex items-center gap-1 rounded-md px-2.5 py-1.5 text-sm font-medium text-slate-600 hover:text-blue-600 hover:bg-slate-100 transition-colors"
            title="重新加载"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* iframe 内容区域（加载中 / 未响应两套遮罩在 QuantBotSurface 内） */}
      <QuantBotSurface frame={frame} />

      {/* 提示词库（示例 + 模板合并，居中弹窗） */}
      <PromptLibraryModal open={showPrompts} onClose={() => setShowPrompts(false)} />
      {/* 调研报告档案（md + PDF 自动归档，内嵌预览，居中弹窗） */}
      <ReportsModal open={showReports} onClose={() => setShowReports(false)} />
      {/* AI-IDE 策略台（懒加载，居中弹窗） */}
      <AiIdeModal open={showAiIde} onClose={() => setShowAiIde(false)} />
    </div>
  );
};

export default QuantBotPage;
