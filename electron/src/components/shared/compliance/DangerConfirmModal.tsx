/** 危险操作二次确认卡（T-FE-18）：统一后果文案 + 确认/取消；确认前绝不触发动作。 */

import React from 'react';
import { Modal } from 'antd';
import { AlertTriangle } from 'lucide-react';
import { recordComplianceEvent } from './complianceLog';

export interface DangerScenario {
  title: string;
  consequences: readonly string[];
  confirmText?: string;
  cancelText?: string;
}

interface DangerConfirmModalProps {
  open: boolean;
  scenario: DangerScenario | null;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

/** 后果文案里的 **强调** 以粗体渲染（设计口径：说清后果，重点加粗） */
function renderLine(line: string): React.ReactNode {
  const parts = String(line).split(/\*\*(.+?)\*\*/g);
  return parts.map((part, i) =>
    i % 2 === 1 ? (
      <b key={i} className="text-rose-700">
        {part}
      </b>
    ) : (
      <React.Fragment key={i}>{part}</React.Fragment>
    )
  );
}

export const DangerConfirmModal: React.FC<DangerConfirmModalProps> = ({
  open,
  scenario,
  onConfirm,
  onCancel,
  loading,
}) => {
  const title = scenario?.title || '危险操作确认';
  return (
  <Modal
    open={open && !!scenario}
    title={
      <span className="inline-flex items-center gap-2">
        <AlertTriangle className="w-4 h-4 text-rose-600" />
        {title}
      </span>
    }
    okText={scenario?.confirmText || '确认执行'}
    cancelText={scenario?.cancelText || '取消'}
    okButtonProps={{ danger: true }}
    confirmLoading={loading}
    onOk={() => {
      // 留痕（T-FE-17）：每次危险动作的确认/取消都记录一条
      recordComplianceEvent('danger_confirmed', title);
      onConfirm();
    }}
    onCancel={() => {
      recordComplianceEvent('danger_cancelled', title);
      onCancel();
    }}
    destroyOnHidden
  >
    <div className="space-y-1.5 text-xs text-slate-600 leading-5">
      {(scenario?.consequences || []).map((line, i) => (
        <div key={i}>· {renderLine(line)}</div>
      ))}
      <p className="text-[10px] text-slate-400 pt-1">确认前不会触发任何动作；取消立即回到原状态。</p>
    </div>
  </Modal>
  );
};
