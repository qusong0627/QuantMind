/**
 * 移植一致性闸门：**镜像 arena/** == 重跑移植脚本的产物**（字节级）。
 * 跑法：`cd electron && npx vitest run port-parity`
 *
 * 为什么要有这道闸：arena/** 是生成物，「不许手改、要改就登记 PATCHES」这条规矩
 * 只有重跑时才会抓现行——一次「手改生成物、忘了登记」要等到下次重新同步才爆雷
 * （E1 成交标记就这么被冲掉过一次，用户二次报障）。本测试用与 port-from-arena.mjs
 * **同一渲染实现**（renderFile，见其注释）把上游重渲一遍，逐字节比对：任何未登记的
 * 手改、或登记了但 find 已失配的补丁（applyPatches 会抛），在这里当场红。
 *
 * 依赖上游仓库（默认 /home/zbox/quant-Trader/arena，ARENA_SRC 可覆盖）：非开发机
 * 没有上游时整测跳过——这条闸门只在能重跑移植的地方有意义。
 */
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { DEST, SRC, buildClosure, collectKeyframes, renderFile } from './port-from-arena.mjs';

const upstreamMissing = !fs.existsSync(SRC);

/** 找第一处不同行，报错信息要能直接指到行（排 byte 级排查的时间） */
function firstDifference(mirror, regenerated) {
  const a = mirror.split('\n');
  const b = regenerated.split('\n');
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (a[i] !== b[i]) {
      return (
        `第 ${i + 1} 行不同\n` +
        `      镜像: ${JSON.stringify(a[i] ?? '(EOF)')}\n` +
        `      重跑: ${JSON.stringify(b[i] ?? '(EOF)')}`
      );
    }
  }
  return '逐行相同但整体不等（行尾符差异？）';
}

test.skipIf(upstreamMissing)('镜像与重跑产物逐字节一致（闭包全量）', async () => {
  const { files, missing } = buildClosure();
  assert.deepEqual(missing, [], '相对 import 解析失败，拒绝在半闭包上比对');

  // 防假绿：闭包不能是空集，且三个关键文件必须在——入口表/解析规则被改坏要报出来
  for (const must of ['components/ChatStream.tsx', 'api/client.ts', 'styles/globals.css']) {
    assert.ok(files.includes(must), `闭包缺少 ${must}（入口表或解析规则被改坏了？）`);
  }

  const kfNames = collectKeyframes(files.filter((f) => f.endsWith('.css')).map((rel) => ({ rel })));
  const kfMap = new Map([...kfNames].map((n) => [n, `qm-arena-${n}`]));

  const diffs = [];
  for (const rel of files) {
    const mirrorAbs = path.join(DEST, rel);
    if (!fs.existsSync(mirrorAbs)) {
      diffs.push(`${rel}：镜像缺失`);
      continue;
    }
    const mirror = fs.readFileSync(mirrorAbs, 'utf8');
    const { content } = await renderFile(rel, kfMap);
    if (mirror !== content) {
      diffs.push(`${rel}：${firstDifference(mirror, content)}`);
    }
  }

  assert.deepEqual(
    diffs,
    [],
    `镜像与重跑产物不一致（手改生成物未登记 PATCHES？补丁 find 失配？）：\n  ${diffs.join('\n  ')}`,
  );
});
