/**
 * 告警标签金样对拍：前端 `copilotModel.ts` 的中文标签必须等于后端共读的那一份。
 *
 * 为什么两边不合成一份实现：推送文案在 Python 侧拼、面板在 TS 侧渲染，两套产物没法
 * 共用一份代码。所以口径落在**金样 JSON** 上，两侧各跑一条对拍
 * （后端 `backend/tests/test_alert_text.py::test_*_labels_match_golden`）。
 * 改一个标签只改一边 ⇒ 两侧各红一条，不会出现「推送写『关注』、面板写『警告』」。
 *
 * 金样放在**后端包内**（`backend/tests/fixtures/alertLabelsGolden.json`）：后端测试
 * 跑在容器里只挂了 `./backend`，放后端侧两边才都读得到（与 researchScoreGolden /
 * exportDisclaimerGolden 同一考虑）。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

import { alertTypeLabel, severityMeta } from '../copilotModel';

const GOLDEN_PATH = path.resolve(
  __dirname,
  '../../../../../..', // electron/src/features/desk/components/__tests__ → 仓库根
  'backend/tests/fixtures/alertLabelsGolden.json',
);

const GOLDEN: {
  severity: Record<string, string>;
  type: Record<string, string>;
  unknownPassthrough: string[];
} = JSON.parse(readFileSync(GOLDEN_PATH, 'utf-8'));

describe('金样对拍 —— 告警标签前端必须等于后端共读的那一份', () => {
  it('金样非空（空金样会让下面的用例变成假通过）', () => {
    expect(Object.keys(GOLDEN.severity).length).toBeGreaterThan(0);
    expect(Object.keys(GOLDEN.type).length).toBeGreaterThan(0);
    expect(GOLDEN.unknownPassthrough.length).toBeGreaterThan(0);
  });

  it('severityMeta 对每个级别给出金样里的中文', () => {
    for (const [severity, label] of Object.entries(GOLDEN.severity)) {
      expect([severity, severityMeta(severity).label]).toEqual([severity, label]);
    }
  });

  it('alertTypeLabel 对每个类型给出金样里的中文', () => {
    for (const [alertType, label] of Object.entries(GOLDEN.type)) {
      expect([alertType, alertTypeLabel(alertType)]).toEqual([alertType, label]);
    }
  });

  it('未登记的类型原样回显（不编标签）', () => {
    for (const alertType of GOLDEN.unknownPassthrough) {
      expect(alertTypeLabel(alertType)).toBe(alertType);
    }
  });
});
