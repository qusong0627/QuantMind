/**
 * 「实盘交易」栏目**入仓**闸门（2026-10-08 用户决定：连同实盘栏目一起入仓）。
 *
 * 本条决定之前，本文件是一条**反向**机器闸门——「local-live 目录不得被跟踪」
 * （一次 `git add -A` 就够毁掉口头约定，所以要机器钉住）。现在约定翻过来了，
 * 闸门钉的是**新不变量**：
 *
 *   ① 目录**必须在仓**：`git ls-files` 非空且含入口文件。导航（FloatingNavBar）、
 *      路由（App.tsx）、部署脚本（deploy_frontend.sh）、垫片注释都以「目录恒在」为
 *      前提；把目录重新加回 .gitignore 或 git rm 掉，这几个前提会同时失义且分散在
 *      三处源码里——所以钉在这里，一处变红。
 *   ② `.gitignore` 里**没有**排除该目录的**生效行**。注释行允许提路径（本轮的新
 *      注释就提了），所以判据是剥掉注释与空行后的行。
 *   ③ 垫片仍是「缺目录也可构建」的形态：`import.meta.glob` 探测，而非静态动态导入。
 *      （防有人趁目录恒在就改成 `import('../local-live/xxx')`，把裁剪形态打爆。）
 *
 * 「开不开」不归本文件管：导航项开关断言在 `FloatingNavBar.test.tsx`
 * （`isLiveTradingEnabled`），页面兜底在 `LiveDisabledPage`。
 *
 * `git ls-files` 看的是**索引**——目录 add 完即绿，不必等提交，所以它能在
 * 提交之前就当闸门用。
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

/** .gitignore 剥掉注释与空行后的**生效行**。断言不许拿注释行当证据。 */
function activeIgnoreLines(): string[] {
    return readFileSync(GITIGNORE, 'utf-8')
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line.length > 0 && !line.startsWith('#'));
}

describe('「实盘交易」栏目必须随仓分发', () => {
    it('仓库根确实是个 git 仓（否则下面的断言全是假绿）', () => {
        expect(existsSync(path.join(REPO_ROOT, '.git'))).toBe(true);
        expect(existsSync(GITIGNORE)).toBe(true);
    });

    it('.gitignore 里没有排除 local-live 的生效行', () => {
        const offending = activeIgnoreLines().filter((line) => line.includes(LOCAL_LIVE_REL));
        expect(offending).toEqual([]);
    });

    it('local-live 下的文件被跟踪（含入口 LiveTradingPage.tsx）', () => {
        const tracked = trackedFilesUnder(LOCAL_LIVE_REL);
        expect(tracked.length).toBeGreaterThan(0);
        expect(tracked).toContain(`${LOCAL_LIVE_REL}/LiveTradingPage.tsx`);
    });

    it('闸门本身不是零项通过（能真的列出被跟踪的文件）', () => {
        // 反向自检：拿一个**确定被跟踪**的路径验证 trackedFilesUnder 有区分度——
        // 命令拼错/cwd 退化时，"空输出"既可能来自"没被跟踪"也可能来自"命令没跑成"，
        // 这条控制组把两者区分开。控制组刻意用 package.json 而不是本测试文件自身：
        // 后者在首次提交前是 untracked，拿它做控制组会假红（且掩盖真实回归）。
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
        // 构建期解析，缺这个目录的裁剪形态 → Rollup unresolved import → 构建失败。
        expect(code).toContain("import.meta.glob('../local-live/");
        expect(code).not.toMatch(/import\(\s*['"][^'"]*local-live/);
    });
});
