/** 首启风险问卷弹窗（T-FE-17）：答完 → 等级留痕；关闭=稍后再答（不阻断浏览，但高风险动作恒走确认卡）。 */

import React, { useState } from 'react';
import { Modal, Radio, message } from 'antd';
import {
  RISK_LEVEL_LABEL,
  RISK_QUESTIONS,
  evaluateRiskProfile,
  markRiskProfileSkipped,
  saveRiskProfile,
} from './riskProfile';
import { recordComplianceEvent } from './complianceLog';

interface RiskProfileModalProps {
  open: boolean;
  onDone: () => void;
  onSkip: () => void;
}

export const RiskProfileModal: React.FC<RiskProfileModalProps> = ({ open, onDone, onSkip }) => {
  const [answers, setAnswers] = useState<Array<number | null>>(
    RISK_QUESTIONS.map(() => null)
  );
  const answeredAll = answers.every((a) => a !== null);

  const submit = () => {
    const { level, score } = evaluateRiskProfile(answers);
    saveRiskProfile(level, score);
    recordComplianceEvent('risk_profile_taken', `${RISK_LEVEL_LABEL[level]}（score=${score}）`);
    message.success(`风险评测完成：${RISK_LEVEL_LABEL[level]}（已留痕）`);
    onDone();
  };

  const skip = () => {
    markRiskProfileSkipped();
    recordComplianceEvent('risk_profile_skipped', '稍后再答（7 天内不再询问）');
    onSkip();
  };

  return (
    <Modal
      open={open}
      title="投资适当性评估（首启 1 次）"
      okText="提交评估"
      cancelText="稍后再答"
      okButtonProps={{ disabled: !answeredAll }}
      onOk={submit}
      onCancel={skip}
      width={560}
      destroyOnHidden
    >
      <div className="space-y-3 text-xs text-slate-600">
        <p className="text-slate-500">
          用于风险匹配提示（不收集个人身份信息，仅本地留痕）。完成评估后，收益展示与免责页脚会标注你的风险等级。
        </p>
        {RISK_QUESTIONS.map((q, qi) => (
          <div key={q.key} className="rounded-xl border border-gray-100 p-3">
            <div className="font-medium text-slate-700 mb-2">
              {qi + 1}. {q.text}
            </div>
            <Radio.Group
              value={answers[qi]}
              onChange={(e) => {
                const next = [...answers];
                next[qi] = Number(e.target.value);
                setAnswers(next);
              }}
              size="small"
            >
              {q.options.map((opt) => (
                <Radio key={opt.label} value={opt.score} className="block text-xs">
                  {opt.label}
                </Radio>
              ))}
            </Radio.Group>
          </div>
        ))}
      </div>
    </Modal>
  );
};
