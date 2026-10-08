import type { CSSProperties } from 'react';
import { AgentTokenUsage, MarketId, marketMeta } from '../api/client';
import { displayAgentName } from '../utils/agents';
import { fmtMoney, fmtPct, pnlClass } from '../utils/format';

/** token 缩写: 11871 → 11.9k, 1234567 → 1.23M */
export const fmtTok = (n: number): string =>
  n >= 1_000_000 ? `${(n / 1e6).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n)}`;

/** 模型 emoji 标识（终端风色块映射） */
export const MODEL_LOGOS: Record<string, string> = {
  'deepseek-v4-flash': '🟠',
  'deepseek-v4-pro': '🔵',
  'glm-5.3-flash': '🟢',
};

export const logoOf = (name: string): string => MODEL_LOGOS[name] ?? '⚪';

/** 模型 → 标识色：图表线 / 模型对话 / 成交事件 统一配色（对话与图表颜色一致） */
export const MODEL_COLORS: Record<string, string> = {
  'deepseek-v4-flash': '#4d6bfe',
  'deepseek-v4-pro': '#8b5cf6',
  'glm-5.3-flash': '#2ecc71',
};

export const modelColor = (name: string): string => MODEL_COLORS[name] ?? '#5a5a5a';

/** 模型名 → 短标签（终端显示）。
 *  agent 展示名先过 displayAgentName（展示层唯一入口）：模型卡 / 配置面板 / 成交
 *  事件 feed 都走本函数，market-research 若直接 toUpperCase 会裸露 "MARKET-RESEARCH"
 *  （2026-09-12 审查 LOW）。模型名经该映射原样返回，行为不变。 */
export const shortName = (name: string): string =>
  displayAgentName(name)
    .replace('deepseek-v4', 'DS V4')
    .replace('deepseek', 'DS')
    .toUpperCase();

/** mini 模型卡：logo + 名称 + 余额 + 收益% —— 终端风 model-card-mini。
 *  点击触发选中（右栏 filter 联动）。 */
export default function ModelCard({
  market,
  agent,
  balance,
  ret,
  selected,
  onClick,
  tokens,
}: {
  market: MarketId;
  agent: string;
  balance: number | null;
  ret: number | null;
  selected: boolean;
  onClick: () => void;
  /** 实盘 LLM 分析累计 token 消耗（/api/token-usage） */
  tokens?: AgentTokenUsage | null;
}) {
  const meta = marketMeta(market);
  // 选中态：模型品牌色（边框/浅色底/名称着色），取代黑底白字
  const accent = modelColor(agent);
  return (
    <div
      className={`model-card-mini ${selected ? 'selected' : ''}`}
      onClick={onClick}
      style={
        {
          // 品牌色恒注入（原版只在选中时注入）：宿主打磨层要靠它给**每张**卡上品牌色
          // （左侧竖条 / logo 底色 / 选中角标），未选中时也得有值
          '--model-accent': accent,
          ...(selected
            ? {
                borderColor: accent,
                background: `${accent}12`,
                boxShadow: `3px 3px 0 ${accent}40`,
              }
            : {}),
        } as CSSProperties
      }
    >
      <div className="model-logo">{logoOf(agent)}</div>
      <div className="model-info">
        <div className="model-name">{shortName(agent)}</div>
        <div className="model-balance">{fmtMoney(balance, meta.currency)}</div>
        <div className={`model-pnl ${pnlClass(ret)}`}>{fmtPct(ret)}</div>
        {tokens != null && (
          <div
            className="model-tokens"
            title={`实盘分析 ${tokens.calls} 次 · 输入 ${fmtTok(tokens.prompt_tokens)} / 输出 ${fmtTok(tokens.completion_tokens)}${tokens.estimated ? '（部分为估算）' : ''}`}
          >
            ⚡ {fmtTok(tokens.total_tokens)} tok
          </div>
        )}
      </div>
    </div>
  );
}
