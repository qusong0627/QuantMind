/**
 * 研究评分（0–100 截面分位）——对外唯一的分数刻度。
 *
 * 这一层的纪律与 `lookbackModel.ts` 同款：**缺失一律 `—`，绝不显示成 0**。
 * 0 分是一个合法分数（全市场垫底），把它与「没有数据」混为一谈，
 * 用户会把「没算出来」读成「模型说这票最差」。
 *
 * 金样用例与后端 `backend/tests/test_research_score.py` 共用同一个 JSON，
 * 存在**后端包内**（`backend/tests/fixtures/researchScoreGolden.json`）：
 * 后端测试跑在容器里，只挂了 `./backend`，金样放在后端侧两边才都读得到。
 * 任一侧改了公式，另一侧的测试立刻红。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
    RESEARCH_SCORE_BANDS,
    RESEARCH_SCORE_HINT,
    formatResearchScore,
    formatResearchScoreWithBand,
    researchScore,
    researchScoreBand,
} from '../researchScore';

/**
 * 直接读文件而不是 `import`：金样在 `electron/` 之外，走模块解析会碰到 vite 的
 * root 限制。用 `__dirname` 定位（与 `glossary.test.ts` 同款），与 cwd 无关。
 */
const GOLDEN_PATH = path.resolve(
    __dirname,
    '../../../../..',
    'backend/tests/fixtures/researchScoreGolden.json',
);
const golden: { cases: Array<{ rankPct: number; score: number }>; bands: Array<{ min: number; label: string }> } =
    JSON.parse(readFileSync(GOLDEN_PATH, 'utf-8'));

describe('researchScore 换算', () => {
    it.each(golden.cases)('rank_pct=$rankPct → $score', ({ rankPct, score }) => {
        expect(researchScore(rankPct)).toBe(score);
    });

    it('保留一位小数，不做整数取整', () => {
        // 两个分数之间只差 0.1 分，取整会让它们看起来一样，分位信息就丢了
        expect(researchScore(0.871)).toBe(87.1);
        expect(researchScore(0.879)).toBe(87.9);
    });

    it('边界 0 与 1 都是合法分数，不是缺失', () => {
        expect(researchScore(0)).toBe(0);
        expect(researchScore(1)).toBe(100);
    });

    it.each([
        ['null', null],
        ['undefined', undefined],
        ['NaN', Number.NaN],
        ['Infinity', Number.POSITIVE_INFINITY],
    ])('%s → null（缺失）', (_label, input) => {
        expect(researchScore(input as number | null | undefined)).toBeNull();
    });

    it.each([
        ['越界的高值（像是把百分数当分位传进来了）', 87.5],
        ['越界的负值', -0.1],
    ])('%s → null，不猜、不截断', (_label, input) => {
        // 静默截断会把「口径接错了」伪装成一个正常分数；返回 null 会让它
        // 在界面上显示成 `—`，错误立刻可见
        expect(researchScore(input)).toBeNull();
    });
});

describe('formatResearchScore 格式化', () => {
    it('缺失 → —', () => {
        expect(formatResearchScore(null)).toBe('—');
        expect(formatResearchScore(undefined)).toBe('—');
    });

    it('固定一位小数', () => {
        expect(formatResearchScore(87)).toBe('87.0');
        expect(formatResearchScore(87.15)).toBe('87.2');
    });

    it('0 分显示为 0.0 而不是 —', () => {
        // 「全市场垫底」与「没有数据」是两件事，这条是二者的分界线
        expect(formatResearchScore(0)).toBe('0.0');
    });
});

describe('researchScoreBand 档位', () => {
    it.each([
        [100, '头部'],
        [85, '头部'],
        [84.9, '居前'],
        [70, '居前'],
        [69.9, '居中'],
        [60, '居中'],
        [59.9, '居后'],
        [40, '居后'],
        [39.9, '尾部'],
        [0, '尾部'],
    ])('%s → %s', (score, band) => {
        expect(researchScoreBand(score)).toBe(band);
    });

    it('档位边界与后端 eval_scoring 的既有刻度对齐（85/70/60）', () => {
        // 不新造第二套阈值：A/B/C 的切线必须与评级体系同源，否则同一个分数
        // 在两处会得到不同结论
        const cuts = RESEARCH_SCORE_BANDS.map((b) => b.min);
        expect(cuts).toContain(85);
        expect(cuts).toContain(70);
        expect(cuts).toContain(60);
    });

    it('缺失 → —（不是尾部）', () => {
        // 把「没数据」显示成「尾部」，等于替模型说了一句它没说过的话
        expect(researchScoreBand(null)).toBe('—');
    });

    it('组合展示形如「87.2（头部）」', () => {
        expect(formatResearchScoreWithBand(87.2)).toBe('87.2（头部）');
        expect(formatResearchScoreWithBand(null)).toBe('—');
    });
});

describe('口径声明', () => {
    it('必须如实说明是同市场同交易日截面，且跨市场不可比', () => {
        expect(RESEARCH_SCORE_HINT).toContain('截面');
        expect(RESEARCH_SCORE_HINT).toContain('跨市场');
        expect(RESEARCH_SCORE_HINT).toContain('不可直接比较');
    });

    it('不出现方向性措辞', () => {
        for (const word of ['买入', '卖出', '看多', '看空', '建议']) {
            expect(RESEARCH_SCORE_HINT).not.toContain(word);
        }
    });

    it('与后端 research_score.py 的口径声明是同一句话', () => {
        // 两处口径文案一旦分叉，前后端就会对用户讲两种不同的话。Python 里是
        // 隐式字符串拼接，所以抹掉引号与空白后做包含判断。
        const py = readFileSync(
            path.resolve(__dirname, '../../../../..', 'backend/shared/research_score.py'),
            'utf-8',
        );
        const squeeze = (s: string) => s.replace(/["\s]/g, '');
        expect(squeeze(py)).toContain(squeeze(RESEARCH_SCORE_HINT));
    });
});
