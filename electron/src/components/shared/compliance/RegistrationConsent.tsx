/**
 * 注册页合规确认项（开源发行合规）：必须勾选才能提交注册。
 *
 * 文案复用 ComplianceChrome 的资质边界原句（COMPLIANCE_CONSENT_TEXT），前端内嵌、
 * 不依赖外链；勾选即写一条 terms_consent 留痕，事后可回溯「用户同意过什么」。
 *
 * 受控组件：勾选状态由注册页持有（提交按钮的 disabled 依赖它），本组件只负责
 * 展示 + 回调 + 留痕。
 */

import React from 'react';
import { Checkbox } from 'antd';
import { COMPLIANCE_CONSENT_TEXT } from './ComplianceChrome';
import { recordComplianceEvent } from './complianceLog';

export const RegistrationConsent: React.FC<{
  checked: boolean;
  onChange: (checked: boolean) => void;
}> = ({ checked, onChange }) => {
  const handleChange = (e: { target: { checked: boolean } }) => {
    const next = e.target.checked;
    if (next) {
      recordComplianceEvent('terms_consent', COMPLIANCE_CONSENT_TEXT);
    }
    onChange(next);
  };

  return (
    <Checkbox checked={checked} onChange={handleChange} style={{ alignItems: 'flex-start' }}>
      <span style={{ fontSize: '12px', lineHeight: 1.6, color: 'rgba(0,0,0,0.65)' }}>
        {COMPLIANCE_CONSENT_TEXT}
      </span>
    </Checkbox>
  );
};
