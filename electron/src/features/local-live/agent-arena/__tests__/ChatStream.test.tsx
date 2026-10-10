/**
 * 「模型对话」数据缺口横幅 + 合规尾注（审计 C5 / T3-2）的渲染闸门。
 *
 * 背景：桥不可达时分析端（scripts/live_model_analysis.py）会改写提示词（禁止编造
 * 持仓点评）并在日志条目上写 data_gaps=['account_unreachable']；前端必须据此**显式**
 * 挂横幅——若靠正文文本判读，LLM 幻觉出来的「持仓点评」就会被当事实读。
 *
 * 文案断言全部引用 compliance 单一来源，测试里不抄字面量——抄了就等于又开一个来源。
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import ChatStream from '../arena/components/ChatStream';
import type { LogLine } from '../arena/api/client';
import {
  COMPLIANCE_AI_GENERATED_TEXT,
  COMPLIANCE_HISTORY_TEXT,
  COMPLIANCE_TOOL_BOUNDARY_TEXT,
} from '../../../../components/shared/compliance/ComplianceChrome';

/** 一轮 A 股口径日志（单行含 user+assistant），缺口标记挂在同一行 */
const round = (gaps?: string[]): LogLine => ({
  timestamp: '2026-10-10T09:30:00',
  ...(gaps ? { data_gaps: gaps } : {}),
  new_messages: [
    { role: 'user', content: '【要求】……' },
    { role: 'assistant', content: '总结：新闻面偏多。' },
  ],
});

const agentsWith = (line: LogLine) => [
  { name: 'deepseek-v4-flash', id: 'deepseek-v4-flash', lines: [line] },
];

describe('数据缺口横幅（C5）', () => {
  it('account_unreachable 轮：卡片默认折叠态下横幅也可见', () => {
    render(<ChatStream agents={agentsWith(round(['account_unreachable']))} />);

    const banner = screen.getByRole('alert');
    expect(banner.textContent).toContain('本轮无实盘账户数据');
    expect(banner.textContent).toContain('未点评持仓');
  });

  it('无缺口轮不挂横幅', () => {
    render(<ChatStream agents={agentsWith(round())} />);

    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('未知缺口值不误触发（字段存在 ≠ 本轮桥挂）', () => {
    render(<ChatStream agents={agentsWith(round(['something_else']))} />);

    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('合规尾注（AI 生成免责，单一来源文案）', () => {
  it('有分析记录：列表尾部带 AI 生成提示 + 统一免责', () => {
    render(<ChatStream agents={agentsWith(round())} />);

    expect(screen.getByText(COMPLIANCE_AI_GENERATED_TEXT)).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_TOOL_BOUNDARY_TEXT)).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_HISTORY_TEXT)).toBeInTheDocument();
  });

  it('空态同样出尾注（列表空时也是 AI 展示面）', () => {
    render(<ChatStream agents={[]} />);

    expect(screen.getByText('暂无分析记录')).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_AI_GENERATED_TEXT)).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_TOOL_BOUNDARY_TEXT)).toBeInTheDocument();
  });
});
