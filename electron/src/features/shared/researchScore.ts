/**
 * 研究评分——**对外唯一的分数刻度**（0–100 截面分位）。
 *
 * 为什么要有这一层：模型输出的原始分（`fusion_score`）量纲随模型变，跨模型不可比；
 * 而 `rank_pct`（当日截面百分位，见 `backend/shared/signal_contract.py`）是稳定的。
 * `glossary.ts` 早就写着「对外展示以 rank_pct 为准」，本模块把那条规则**兑现成一个函数**，
 * 免得每个页面各自 `* 100` 一次、各自决定保留几位、各自决定缺失显示什么。
 *
 * 口径（对外必须如实这么讲，见 `RESEARCH_SCORE_HINT`）：
 *   研究评分 = rank_pct × 100，保留一位小数。
 *   含义是「该标的在**同交易日、同市场**截面内的百分位」，**跨市场/跨模型不可直接比较**。
 *   它描述的是相对位置，**不含方向**。
 *
 * 与后端 `backend/shared/research_score.py` 是同一公式的两份实现，金样共用
 * `__tests__/fixtures/researchScoreGolden.json`；改一边必须改另一边，否则测试红。
 */

export interface ResearchScoreBand {
    /** 该档位的下界（含），0–100 */
    min: number;
    /** 档位名：纯描述截面位置，不含方向 */
    label: string;
}

/**
 * 档位刻度。上三档切线（85/70/60）**刻意与后端 `eval_scoring.py` 的评级刻度一致**，
 * 避免同一个分数在两处得到不同结论；40 只是把「居后」与「尾部」分开。
 */
export const RESEARCH_SCORE_BANDS: readonly ResearchScoreBand[] = [
    { min: 85, label: '头部' },
    { min: 70, label: '居前' },
    { min: 60, label: '居中' },
    { min: 40, label: '居后' },
    { min: 0, label: '尾部' },
];

/** 缺失占位符：**不是 0**。0 分是一个合法分数（全市场垫底）。 */
export const RESEARCH_SCORE_MISSING = '—';

/**
 * 口径声明（唯一一份）。UI 的 tooltip / 副标题 / 导出文件都引用这里。
 *
 * 「同一模型」四个字不能省：`rank_pct` 的分母是**同一次推理**（`PARTITION BY run_id`，
 * 见 `signal_contract.py:12`），不是全市场跨模型截面。含糊成「全市场分位」是拔高，
 * 与后端实际算的东西不符。字面量与 `backend/shared/research_score.py` 保持一致。
 */
export const RESEARCH_SCORE_HINT =
    '该标的在同一模型、同一交易日的截面内所处百分位（0–100，越高越靠前）。跨模型/跨市场不可直接比较，仅反映当日截面相对位置。';

/**
 * `rank_pct`（0–1）→ 研究评分（0.0–100.0，一位小数）。
 *
 * 输入缺失或**越界**一律返回 `null`（渲染成 `—`）：
 * - 越界（如 87.5）通常意味着调用方把百分数当分位传进来了。静默截断会把这个
 *   口径错误伪装成一个正常分数；返回 null 会让错误当场可见。
 * - 后端 `research_score.py` 用同样的判定，两侧行为一致。
 *
 * 取整用 `floor(x * 1000 + 0.5)`（四舍五入到一位小数）而不是 `toFixed` /
 * `Math.round` 的组合，是为了与 Python 侧逐位一致：两边的浮点运算同序，
 * 结果在位级别相同，金样才能真的锁住。
 */
export function researchScore(rankPct: number | null | undefined): number | null {
    if (rankPct === null || rankPct === undefined) return null;
    if (typeof rankPct !== 'number' || !Number.isFinite(rankPct)) return null;
    if (rankPct < 0 || rankPct > 1) return null;
    return Math.floor(rankPct * 1000 + 0.5) / 10;
}

/** 格式化：一位小数；缺失 → `—`。 */
export function formatResearchScore(score: number | null | undefined): string {
    if (score === null || score === undefined) return RESEARCH_SCORE_MISSING;
    if (typeof score !== 'number' || !Number.isFinite(score)) return RESEARCH_SCORE_MISSING;
    return score.toFixed(1);
}

/** 档位名：纯描述截面位置。缺失 → `—`（**不是「尾部」**，那是替模型说了它没说的话）。 */
export function researchScoreBand(score: number | null | undefined): string {
    if (score === null || score === undefined) return RESEARCH_SCORE_MISSING;
    if (typeof score !== 'number' || !Number.isFinite(score)) return RESEARCH_SCORE_MISSING;
    // 上界越界也照常归档（例如 120 归「头部」）：Band 只做展示分档，
    // 不承担校验职责——校验在 researchScore() 那一层
    for (const band of RESEARCH_SCORE_BANDS) {
        if (score >= band.min) return band.label;
    }
    return RESEARCH_SCORE_MISSING;
}

/** 组合展示：`87.2（头部）`；缺失 → `—`。 */
export function formatResearchScoreWithBand(score: number | null | undefined): string {
    if (score === null || score === undefined) return RESEARCH_SCORE_MISSING;
    if (typeof score !== 'number' || !Number.isFinite(score)) return RESEARCH_SCORE_MISSING;
    return `${formatResearchScore(score)}（${researchScoreBand(score)}）`;
}

/** 由 `rank_pct` 直接得到展示串（调用方最常见的一步到位用法）。 */
export function formatRankPctAsResearchScore(rankPct: number | null | undefined): string {
    return formatResearchScoreWithBand(researchScore(rankPct));
}
