/**
 * 调研报告抽屉（QuantBot 顶栏入口）— QuantBot 技能生成的 md + PDF 自动归档目录。
 * 复用 trading-agents 的报告档案组件（inline 模式：左列表 + 右 PDF 内嵌预览，点文件即看）。
 */
import React from 'react';
import { Drawer } from 'antd';
import { FileText } from 'lucide-react';
import ReportManagerPage from '../../trading-agents/pages/ReportManagerPage';

interface ReportsDrawerProps {
  open: boolean;
  onClose: () => void;
}

const ReportsDrawer: React.FC<ReportsDrawerProps> = ({ open, onClose }) => (
  <Drawer
    open={open}
    onClose={onClose}
    placement="right"
    width="min(1280px, 96vw)"
    zIndex={1200}
    destroyOnHidden
    title={
      <div className="flex items-center gap-2 min-w-0">
        <FileText className="w-4 h-4 text-indigo-600 shrink-0" />
        <span className="text-sm font-bold text-slate-800">调研报告</span>
        <span className="text-[11px] text-slate-400 font-normal truncate hidden sm:inline">
          QuantBot 生成的 md + PDF 自动归档到这里，点击文件直接预览
        </span>
      </div>
    }
    styles={{ body: { padding: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' } }}
  >
    <div className="flex-1 min-h-0">
      <ReportManagerPage embedded previewMode="inline" />
    </div>
  </Drawer>
);

export default ReportsDrawer;
