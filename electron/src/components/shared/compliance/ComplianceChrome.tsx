/** 合规展示组件（T-FE-17）：收益展示规范包装 + 免责页脚 + 首启风险问卷留痕展示。 */

import React from 'react';
import { ShieldAlert } from 'lucide-react';
import { loadRiskProfile, RISK_LEVEL_LABEL } from './riskProfile';

/**
 * 收益展示规范（产品化 §四）：历史收益必须附 **样本区间 + 口径 + 过往不代表未来**。
 * 任何对外展示的收益数字都应经本组件渲染（禁止裸数字）。
 */
export const ComplianceReturn: React.FC<{
  value: number | null | undefined;
  windowText?: string;
  calibNote?: string;
  className?: string;
  percent?: boolean;
}> = ({ value, windowText, calibNote, className, percent = true }) => {
  const text =
    value === null || value === undefined || Number.isNaN(Number(value))
      ? '—'
      : percent
        ? `${(Number(value) * 100).toFixed(2)}%`
        : Number(value).toLocaleString('zh-CN');
  return (
    <span className={className} title="历史收益按样本区间与既定口径计算；过往表现不代表未来收益。">
      {text}
      {windowText && <span className="text-[10px] text-slate-400 ml-1">（{windowText}）</span>}
      {calibNote && <span className="text-[10px] text-slate-400 ml-1">· {calibNote}</span>}
      <span className="text-[9px] text-slate-300 ml-1">过往不代表未来</span>
    </span>
  );
};

/** 标准免责页脚（资质边界：量化研究工具，不构成投资建议/不代客理财） */
export const ComplianceFooter: React.FC<{ extra?: string }> = ({ extra }) => {
  const profile = loadRiskProfile();
  return (
    <footer className="text-[10px] text-slate-400 leading-4 flex flex-wrap items-center gap-x-2 gap-y-0.5">
      <ShieldAlert className="w-3 h-3 shrink-0" />
      <span>
        本页为量化研究工具，展示研究结果与信号，不构成投资建议、不代客理财；股市有风险，投资需谨慎。
      </span>
      <span>历史数据与回测结果不代表未来收益。</span>
      {extra && <span>{extra}</span>}
      {profile && (
        <span className="text-slate-400">
          风险等级：{RISK_LEVEL_LABEL[profile.level]}（{profile.takenAt.slice(0, 10)} 评估）
        </span>
      )}
    </footer>
  );
};

