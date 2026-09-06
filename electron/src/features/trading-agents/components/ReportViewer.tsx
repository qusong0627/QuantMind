/** Analysis report viewer with tabbed analyst sections */

import React, { useState } from 'react';
import { ANALYST_SECTIONS, type AnalysisProgress } from '../types';

interface ReportViewerProps {
  progress: AnalysisProgress;
}

export const ReportViewer: React.FC<ReportViewerProps> = ({ progress }) => {
  const [activeTab, setActiveTab] = useState(0);

  const finalState = progress.final_state || {};
  const stageReports = progress.stage_reports || {};

  const investmentDebate = finalState.investment_debate_state;
  const riskDebate = finalState.risk_debate_state;
  const finalDecision = stageReports.pm || finalState.final_trade_decision || '';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Stats */}
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        gap: 20,
        padding: '10px 16px',
        background: 'rgba(255,255,255,0.8)',
        borderRadius: 10,
        border: '1px solid #e2e8f0',
        fontSize: 12,
        color: '#64748b',
      }}>
        <span>LLM: {progress.stats.llm_calls}</span>
        <span>工具: {progress.stats.tool_calls}</span>
        <span>输入: {(progress.stats.tokens_in / 1000).toFixed(0)}K tokens</span>
        <span>输出: {(progress.stats.tokens_out / 1000).toFixed(0)}K tokens</span>
      </div>

      {/* Analyst Reports Tabs */}
      <div style={{
        background: 'rgba(255,255,255,0.9)',
        borderRadius: 12,
        border: '1px solid #e2e8f0',
        overflow: 'hidden',
        backdropFilter: 'blur(8px)',
      }}>
        {/* Tab Bar */}
        <div style={{
          display: 'flex',
          overflowX: 'auto',
          borderBottom: '1px solid #e2e8f0',
          gap: 0,
        }}>
          {ANALYST_SECTIONS.map((section, i) => {
            const stageId = section.key.replace('_report', '');
            const hasReport = !!stageReports[stageId] || !!finalState[section.key];
            return (
              <button
                key={section.key}
                onClick={() => setActiveTab(i)}
                style={{
                  flex: '0 0 auto',
                  padding: '10px 16px',
                  background: activeTab === i ? '#f8fafc' : 'transparent',
                  border: 'none',
                  borderBottom: activeTab === i ? '2px solid #ff5a1f' : '2px solid transparent',
                  color: activeTab === i ? '#ff5a1f' : hasReport ? '#1e293b' : '#cbd5e1',
                  fontSize: 13,
                  cursor: 'pointer',
                  whiteSpace: 'nowrap',
                  transition: 'all 0.2s',
                }}
              >
                {section.label}
              </button>
            );
          })}
        </div>

        {/* Tab Content */}
        <div style={{ padding: 20, minHeight: 200 }}>
          {(() => {
            const section = ANALYST_SECTIONS[activeTab];
            const stageId = section.key.replace('_report', '');
            const content = stageReports[stageId] || finalState[section.key] || '';

            if (!content) {
              return (
                <div style={{ color: '#94a3b8', textAlign: 'center', padding: 40 }}>
                  暂无数据
                </div>
              );
            }

            return (
              <div style={{
                color: '#334155',
                fontSize: 14,
                lineHeight: 1.8,
                whiteSpace: 'pre-wrap',
                maxHeight: 500,
                overflowY: 'auto',
              }}>
                {content}
              </div>
            );
          })()}
        </div>
      </div>

      {/* Debate Section */}
      {investmentDebate && (
        <CollapsibleSection title="多空辩论" defaultOpen={false}>
          <DebateContent debate={investmentDebate} />
        </CollapsibleSection>
      )}

      {/* Risk Section */}
      {riskDebate && (
        <CollapsibleSection title="风控评估" defaultOpen={false}>
          <DebateContent debate={riskDebate} />
        </CollapsibleSection>
      )}

      {/* Final Decision */}
      {finalDecision && (
        <CollapsibleSection title="最终决策" defaultOpen={true}>
          <div style={{ color: '#334155', fontSize: 14, lineHeight: 1.8, whiteSpace: 'pre-wrap' }}>
            {finalDecision}
          </div>
        </CollapsibleSection>
      )}
    </div>
  );
};

const CollapsibleSection: React.FC<{
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}> = ({ title, defaultOpen = false, children }) => {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div style={{
      background: 'rgba(255,255,255,0.9)',
      borderRadius: 12,
      border: '1px solid #e2e8f0',
      overflow: 'hidden',
      backdropFilter: 'blur(8px)',
    }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%',
          padding: '14px 20px',
          background: 'transparent',
          border: 'none',
          color: '#1e293b',
          fontSize: 15,
          fontWeight: 600,
          cursor: 'pointer',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <span>{title}</span>
        <span style={{ color: '#94a3b8', fontSize: 12 }}>{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div style={{ padding: '0 20px 20px' }}>
          {children}
        </div>
      )}
    </div>
  );
};

const DebateContent: React.FC<{ debate: Record<string, any> }> = ({ debate }) => {
  const judge = debate.judge_decision || '';
  const bull = debate.bull_history || debate.aggressive_history || '';
  const bear = debate.bear_history || debate.conservative_history || '';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {judge && (
        <div style={{
          padding: 14,
          background: '#f0fdf4',
          borderRadius: 8,
          border: '1px solid #bbf7d0',
        }}>
          <div style={{ fontSize: 12, color: '#16a34a', marginBottom: 6, fontWeight: 600 }}>
            评判结论
          </div>
          <div style={{ color: '#334155', fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
            {judge}
          </div>
        </div>
      )}
      {bull && (
        <div style={{
          padding: 14,
          background: '#eff6ff',
          borderRadius: 8,
          border: '1px solid #bfdbfe',
        }}>
          <div style={{ fontSize: 12, color: '#2563eb', marginBottom: 6, fontWeight: 600 }}>
            多头 / 激进观点
          </div>
          <div style={{ color: '#475569', fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap', maxHeight: 200, overflowY: 'auto' }}>
            {bull}
          </div>
        </div>
      )}
      {bear && (
        <div style={{
          padding: 14,
          background: '#fef2f2',
          borderRadius: 8,
          border: '1px solid #fecaca',
        }}>
          <div style={{ fontSize: 12, color: '#dc2626', marginBottom: 6, fontWeight: 600 }}>
            空头 / 保守观点
          </div>
          <div style={{ color: '#475569', fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap', maxHeight: 200, overflowY: 'auto' }}>
            {bear}
          </div>
        </div>
      )}
    </div>
  );
};
