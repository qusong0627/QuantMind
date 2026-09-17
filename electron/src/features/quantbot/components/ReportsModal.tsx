/**
 * 调研报告弹窗（QuantBot 顶栏入口）— QuantBot 技能生成的 md + PDF 自动归档目录。
 * 居中模态：24px 圆角 + 毛玻璃遮罩；复用 trading-agents 报告档案组件
 * （inline 模式：左列表 + 右 PDF 内嵌预览，点文件即看）。
 */
import React from 'react';
import { Modal } from 'antd';
import { FileText, X } from 'lucide-react';
import ReportManagerPage from '../../trading-agents/pages/ReportManagerPage';

interface ReportsModalProps {
  open: boolean;
  onClose: () => void;
}

const ReportsModal: React.FC<ReportsModalProps> = ({ open, onClose }) => (
  <Modal
    open={open}
    onCancel={onClose}
    centered
    footer={null}
    closable={false}
    width="min(1280px, 94vw)"
    zIndex={1200}
    destroyOnHidden
    classNames={{ wrapper: 'qb-reports-modal' }}
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
    <div className="flex flex-col" style={{ height: 'min(80vh, 860px)' }}>
      {/* 弹窗头部 */}
      <div className="flex items-center gap-3 px-5 h-14 shrink-0 border-b border-slate-100 bg-gradient-to-r from-slate-50/80 via-white to-white">
        <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500 to-blue-500 flex items-center justify-center shadow-md shrink-0">
          <FileText className="w-4 h-4 text-white" />
        </div>
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="text-[15px] font-bold text-slate-800 tracking-tight">调研报告</span>
          <span className="hidden sm:block text-[11px] text-slate-400 truncate">
            QuantBot 生成的 md + PDF 自动归档到这里，点击文件直接预览
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

      {/* 主体：报告档案（左列表 + 右内嵌 PDF 预览） */}
      <div className="flex-1 min-h-0 bg-white">
        <ReportManagerPage embedded previewMode="inline" />
      </div>
    </div>
  </Modal>
);

export default ReportsModal;
