/**
 * 「实盘交易」栏目是**本机独有**的：源码在 `electron/src/features/local-live/`，
 * 被 `.gitignore` 排除，不开源、不提交。
 *
 * 这条约束靠自觉必然失守（一次 `git add -A` 就够了），所以在此钉成机器闸门。
 * `git ls-files` 是唯一能真正回答「这个目录到底提交没提交」的手段 —— 断言
 * 文件系统上存不存在是没用的，它在本机当然存在。
 *
 * 公开仓形态下 `local-live/` 整个目录不存在，本文件仍然必须通过：它断言的是
 * **不被跟踪**，不是**存在**。
 */

import { execSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

/** `__dirname` = <root>/electron/src/features/shared/__tests__ → 上溯 5 层到仓库根 */
const REPO_ROOT = path.resolve(__dirname, '../../../../..');
const LOCAL_LIVE_REL = 'electron/src/features/local-live';
const GITIGNORE = path.join(REPO_ROOT, '.gitignore');
const SHIM = path.join(REPO_ROOT, 'electron/src/features/shared/localLive.ts');

function trackedFilesUnder(rel: string): string[] {
    const out = execSync(`git ls-files -- "${rel}"`, {
        cwd: REPO_ROOT,
        encoding: 'utf-8',
    });
    return out.split('\n').filter((line) => line.trim().length > 0);
}

describe('「实盘交易」栏目不得进入公开仓', () => {
    it('仓库根确实是个 git 仓（否则下面的断言全是假绿）', () => {
        expect(existsSync(path.join(REPO_ROOT, '.git'))).toBe(true);
        expect(existsSync(GITIGNORE)).toBe(true);
    });

    it('.gitignore 排除了整个 local-live 目录', () => {
        const ignore = readFileSync(GITIGNORE, 'utf-8');
        expect(ignore).toContain(LOCAL_LIVE_REL);
    });

    it('local-live 下没有任何文件被跟踪', () => {
        // 这是本文件存在的全部理由：`git add -f` 或 .gitignore 被误删时当场变红。
        const tracked = trackedFilesUnder(LOCAL_LIVE_REL);
        expect(tracked).toEqual([]);
    });

    it('闸门本身不是零项通过（能真的列出被跟踪的文件）', () => {
        // 反向自检：拿一个**确定被跟踪**的路径验证 trackedFilesUnder 有区分度。
        // 少了这条，`git ls-files` 拼错参数返回空串也能让上面的用例全绿。
        //
        // 控制组刻意用 package.json 而不是本测试文件自身：后者在首次提交前是
        // untracked，拿它做控制组会让这条自检在提交前假红（且掩盖真实回归）。
        expect(trackedFilesUnder('electron/package.json').length).toBe(1);
    });
});

/**
 * 剥掉块注释与行注释，只留可执行代码。
 *
 * 必需：垫片的文档注释里**引用**了 `import('../local-live/xxx')` 作为反例，
 * 不剥注释的负向断言会被自己写的说明文字判红。正向断言同样只在剥完的源码上
 * 才有效——否则注释里出现 `import.meta.glob` 也能骗过它。
 *
 * **不能用正则剥**：glob 模式写作 `'../local-live/*.tsx'`，字符串里就带星号斜杠，
 * 正则版的块注释匹配会从那里一直啃到下一个块注释结束标记（实测把整段代码吃掉）。
 * 所以逐字符扫描，遇到字符串/模板字面量原样跳过。
 *
 * 不含正则字面量处理——垫片里没有，写进来只会是猜。
 */
function stripComments(src: string): string {
    let out = '';
    let i = 0;
    while (i < src.length) {
        const ch = src[i];
        const next = src[i + 1];
        if (ch === '"' || ch === "'" || ch === '`') {
            out += ch;
            i += 1;
            while (i < src.length) {
                const c = src[i];
                if (c === '\\') {
                    out += c + (src[i + 1] ?? '');
                    i += 2;
                    continue;
                }
                out += c;
                i += 1;
                if (c === ch) break;
            }
            continue;
        }
        if (ch === '/' && next === '*') {
            const end = src.indexOf('*/', i + 2);
            i = end === -1 ? src.length : end + 2;
            continue;
        }
        if (ch === '/' && next === '/') {
            const end = src.indexOf('\n', i);
            i = end === -1 ? src.length : end;
            continue;
        }
        out += ch;
        i += 1;
    }
    return out;
}

describe('探测垫片必须能在缺目录时构建通过', () => {
    it('垫片存在', () => {
        expect(existsSync(SHIM)).toBe(true);
    });

    it('用静态 import.meta.glob 探测，而非静态动态导入', () => {
        const code = stripComments(readFileSync(SHIM, 'utf-8'));
        // 静态模式的 glob 缺目录时返回 {}；而 `import('../local-live/...')` 是
        // 构建期解析，公开仓没这个目录 → Rollup unresolved import → 构建失败。
        expect(code).toContain("import.meta.glob('../local-live/");
        expect(code).not.toMatch(/import\(\s*['"][^'"]*local-live/);
    });
});
