/**
 * 适当性风险问卷（T-FE-17 合规四件之一）：首启评估 → 风险等级，留痕本地并随免责页脚展示。
 *
 * v1 边界（如实）：等级暂存前端 localStorage（产品化要求"按等级解锁高风险功能"需要后端
 * 用户档案接口配合，v1 先覆盖"问卷 + 留痕 + 展示"三件）；高风险动作当前一律走二次确认卡
 * （T-FE-18），风险等级作为额外提示层。
 *
 * 纯函数（计分/分档）与持久化分离，计分可单测。
 */

export type RiskLevel = 'conservative' | 'balanced' | 'aggressive';

export interface RiskOption {
  label: string;
  score: number;
}

export interface RiskQuestion {
  key: string;
  text: string;
  options: RiskOption[];
}

export const RISK_QUESTIONS: RiskQuestion[] = [
  {
    key: 'horizon',
    text: '你计划这笔资金投入多长时间？',
    options: [
      { label: '1 年以内（随时可能要用）', score: 0 },
      { label: '1-3 年', score: 1 },
      { label: '3 年以上（长期不用）', score: 2 },
    ],
  },
  {
    key: 'drawdown',
    text: '账户短期浮亏多少你会考虑止损离场？',
    options: [
      { label: '5% 以内就受不了', score: 0 },
      { label: '10%-20% 可以接受', score: 1 },
      { label: '30% 以上也能拿得住', score: 2 },
    ],
  },
  {
    key: 'experience',
    text: '你过去参与股票/基金投资的年限？',
    options: [
      { label: '没有经验', score: 0 },
      { label: '1-3 年', score: 1 },
      { label: '3 年以上', score: 2 },
    ],
  },
  {
    key: 'return_expectation',
    text: '你对年化收益的预期是？',
    options: [
      { label: '跑赢存款即可（≤5%）', score: 0 },
      { label: '10%-20%，能接受波动', score: 1 },
      { label: '越高越好，波动大没关系', score: 2 },
    ],
  },
  {
    key: 'source',
    text: '投入资金的来源属性？',
    options: [
      { label: '生活必需资金（不可损失）', score: 0 },
      { label: '一部分闲置资金', score: 1 },
      { label: '长期闲钱，可承担较高风险', score: 2 },
    ],
  },
];

/** 计分分档（纯函数）：总分 ≤2 稳健 / 3-6 平衡 / ≥7 进取 */
export function evaluateRiskProfile(answers: Array<number | null>): {
  level: RiskLevel;
  score: number;
} {
  const score = answers.reduce<number>((acc, a) => acc + (typeof a === 'number' ? a : 0), 0);
  if (score <= 2) return { level: 'conservative', score };
  if (score <= 6) return { level: 'balanced', score };
  return { level: 'aggressive', score };
}

export const RISK_LEVEL_LABEL: Record<RiskLevel, string> = {
  conservative: '稳健型',
  balanced: '平衡型',
  aggressive: '进取型',
};

export interface RiskProfileRecord {
  level: RiskLevel;
  score: number;
  takenAt: string;
}

const STORAGE_KEY = 'qm:risk_profile_v1';

export function loadRiskProfile(): RiskProfileRecord | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.level === 'string' && parsed.takenAt) {
      return parsed as RiskProfileRecord;
    }
    return null;
  } catch {
    return null;
  }
}

export function saveRiskProfile(level: RiskLevel, score: number): RiskProfileRecord {
  const record: RiskProfileRecord = { level, score, takenAt: new Date().toISOString() };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(record));
  } catch {
    // 存储不可用：本次会话内仍以内存态展示（不阻断）
  }
  return record;
}

// ---------------------------------------------------------------------------
// 首启询问策略（T-FE-17）：无档案 → 询问；"稍后再答" → 7 天内不再打扰
// ---------------------------------------------------------------------------

const SKIP_KEY = 'qm:risk_profile_skipped_at';
const REASK_AFTER_DAYS = 7;

export function markRiskProfileSkipped(): void {
  try {
    window.localStorage.setItem(SKIP_KEY, new Date().toISOString());
  } catch {
    // 存储不可用：本次会话不再询问（内存态由调用方控制）
  }
}

export function shouldAskRiskProfile(): boolean {
  if (loadRiskProfile()) return false;
  try {
    const raw = window.localStorage.getItem(SKIP_KEY);
    if (!raw) return true;
    const ts = Date.parse(raw);
    if (Number.isNaN(ts)) return true;
    return Date.now() - ts > REASK_AFTER_DAYS * 24 * 60 * 60 * 1000;
  } catch {
    return true;
  }
}
