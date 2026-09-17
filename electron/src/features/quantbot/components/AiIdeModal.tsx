/**
 * AI-IDE 策略台弹窗（QuantBot 顶栏入口）— 同「调研报告」居中弹窗模式。
 *
 * 只做入口搬运，不改 AI-IDE 内部：Monaco 编辑器 + 策略文件树 + minibt 回测执行，
 * 与 QuantBot 对话联动（对话里让 agent 写策略 → 弹窗里直接调/跑）。
 * 懒加载（首次打开才载入 chunk）+ destroyOnHidden（关闭即卸载，Monaco 不驻留）。
 */
import React, { Suspense } from 'react';
import { Modal, Spin } from 'antd';
import { SquareTerminal, X } from 'lucide-react';

const AIIDEPage = React.lazy(() => import('../../../pages/AIIDEPage'));

interface AiIdeModalProps {
  open: boolean;
  onClose: () => void;
}

const AiIdeModal: React.FC<AiIdeModalProps> = ({ open, onClose }) => (
  <Modal
    open={open}
    onCancel={onClose}
    centered
    footer={null}
    closable={false}
    width="min(1500px, 96vw)"
    zIndex={1200}
    destroyOnHidden
    classNames={{ wrapper: 'qb-aiide-modal' }}
    styles={{
      content: {
        padding: 0,
        borderRadius: 24,
        overflow: 'hidden',
        boxShadow: '0 32px 80px -20px rgba(15, 23, 42, 0.4)',
      },
      body: { padding: 0 },
      mask: { background: 'rgba(15, 23, 42, 0.45)', backdropFilter: 'blur(4px)' },
    }}
  >
    <div className="flex flex-col" style={{ height: 'min(88vh, 940px)' }}>
      {/* 弹窗头部 */}
      <div className="flex items-center gap-3 px-5 h-14 shrink-0 border-b border-slate-100 bg-gradient-to-r from-slate-50/80 via-white to-white">
        <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500 to-blue-500 flex items-center justify-center shadow-md shrink-0">
          <SquareTerminal className="w-4 h-4 text-white" />
        </div>
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="text-[15px] font-bold text-slate-800 tracking-tight">AI-IDE 策略台</span>
          <span className="hidden sm:block text-[11px] text-slate-400 truncate">
            写策略 / 调试 / 一键回测 —— 与 QuantBot 对话联动
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto flex w-7 h-7 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors shrink-0"
          title="关闭"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* 主体：AI-IDE 页面（懒加载；根节点 h-full，由本容器给定高度） */}
      <div className="flex-1 min-h-0 overflow-hidden bg-[#f8fafc]">
        <Suspense
          fallback={
            <div className="h-full flex items-center justify-center">
              <Spin size="large" />
            </div>
          }
        >
          <AIIDEPage />
        </Suspense>
      </div>
    </div>
  </Modal>
);

export default AiIdeModal;
