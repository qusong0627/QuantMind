/**
 * 策略编辑模块（AI-IDE 内嵌）——回测中心左侧导航「策略编辑」（2026-09-17 加入，置于快速回测之前）。
 *
 * 复用 AI-IDE 全屏页（Monaco 编辑器 + 策略文件树 + minibt 回测执行），
 * 懒加载（进入本模块才载入 AIIDE chunk），外壳套回测中心的圆角卡片样式保持排版一致。
 */
import React, { Suspense } from 'react';
import { Spin } from 'antd';

const AIIDEPage = React.lazy(() => import('../../pages/AIIDEPage'));

export const StrategyEditorModule: React.FC = () => (
  <div className="h-full rounded-2xl border border-gray-200 bg-white shadow-sm overflow-hidden">
    <Suspense
      fallback={
        <div className="h-full flex flex-col items-center justify-center gap-3">
          <Spin size="large" />
          <span className="text-xs text-slate-400">策略工作台加载中…</span>
        </div>
      }
    >
      <AIIDEPage />
    </Suspense>
  </div>
);

export default StrategyEditorModule;
