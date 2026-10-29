/**
 * 策略编辑模块（回测中心内嵌）——回测中心左侧导航「策略编辑」（2026-09-17 加入，置于快速回测之前）。
 *
 * 复用 AI-IDE 的编辑/执行数据层，但内嵌形态使用独立工作台布局
 * （`strategy-editor/StrategyEditorWorkspace`）：整块 Monaco + 右侧参数栏 +
 * 底部结果坞。不再套 AI-IDE 的 280px 文件树与 500px 聊天栏，避免在
 * 回测中心的 1150px 内容区里把编辑器挤成 222px。
 */
import React, { Suspense } from 'react';
import { Spin } from 'antd';

const AIIDEPage = React.lazy(() => import('../../pages/AIIDEPage'));

export const StrategyEditorModule: React.FC = () => (
  <div className="h-full min-h-0 overflow-hidden">
    <Suspense
      fallback={
        <div className="flex h-full flex-col items-center justify-center gap-3">
          <Spin size="large" />
          <span className="text-xs text-slate-400">策略工作台加载中…</span>
        </div>
      }
    >
      <AIIDEPage strategyEditorMode />
    </Suspense>
  </div>
);

export default StrategyEditorModule;
