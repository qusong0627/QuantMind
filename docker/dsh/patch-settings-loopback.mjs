#!/usr/bin/env node
/**
 * dsh 客户端补丁：让「可信前门」注入的 __DSH_TRUSTED_LOOPBACK__ 也走 isLoopback 分支。
 *
 * 背景：dsh（预览版）只在 loopback 页面启用设置持久化——dsh-client-ui-settings 里
 *   const persistence = ctx.remote.$host.isLoopback ? "host" : "memory"
 * 非 loopback（局域网 / 远程浏览器）时 settings 镜像直接进入终态 unavailable，
 * 模型提供方目录、MCP 连接器、欢迎声明、打开配置文件等所有走 settings 的界面
 * 都报 "settings are unavailable in this browser"。$host.isLoopback 的唯一数据源
 * 是 dsh-client-connection 里下面这一行计算，补它即可全修（2026-09-17 实测）。
 *
 * 我们的 nginx 前门（docker/dsh/nginx.conf）给每个页面注入
 *   globalThis.__DSH_TRUSTED_LOOPBACK__ = true
 * 使经前门访问的浏览器与 localhost 同权。真正的安全边界不在客户端：dsh 服务端
 * /api browser-trust 围栏（DSH_TRUSTED_HOSTS，entrypoint 传 --trusted-host）仍按
 * Host 精确匹配，非可信来源的 /api 一律 403——该标志给不了他们任何额外能力。
 *
 * 版本适配：
 *   0.2.0-rc.2 及更早：dsh-client-connection/lib/client.js（单文件）
 *   0.2.2-rc.3 起：拆成 client.browser.js（压缩产物）与 client.node.js（带空格）
 *   npm 在「全新安装」与「覆盖升级」下的依赖提升位置不同（嵌套或顶层 hoist），
 *   所以这里同时探测两种路径；页面实际执行的是 browser 产线，缺失即构建失败。
 *
 * 幂等：已打过补丁直接通过；锚点找不到（dsh 升级改了实现）时报错退出，让镜像构建
 * 失败提醒人工复核——静默失配会让局域网设置页再次损坏且无任何日志。
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const FLAG = '__DSH_TRUSTED_LOOPBACK__';
const PATCH_SUFFIX = ` || globalThis.${FLAG} === true,`;

const ROOTS = [
  // 覆盖升级：依赖嵌在 dsh 自身的 node_modules 下
  '/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-connection',
  // 全新安装：npm 可能把共享依赖提升到顶层
  '/usr/local/lib/node_modules/@deepseek-ai/dsh-client-connection',
];

const SPACED_ANCHOR =
  'isLoopback: transport?.ownsHost === true || pageLocation === void 0 || isLoopbackHostname(pageLocation.hostname),';
const MINIFIED_ANCHOR =
  'isLoopback:transport?.ownsHost===true||pageLocation===void 0||isLoopbackHostname(pageLocation.hostname),';

// 允许的文件：browser 是 0.2.2 起的网页端产物；client.js 是 0.2.0 及更早的单文件产物。
const BROWSER_FILES = ['client.browser.js', 'client.js'];
const NODE_FILES = ['client.node.js'];

function collectTargets() {
  const targets = [];
  const seen = new Set();
  for (const root of ROOTS) {
    const libDir = join(root, 'lib');
    if (!existsSync(libDir)) continue;
    for (const name of [...BROWSER_FILES, ...NODE_FILES]) {
      const path = join(libDir, name);
      if (existsSync(path) && !seen.has(path)) {
        seen.add(path);
        targets.push({
          name: `${root.split('/').pop()}/lib/${name}`,
          path,
          required: BROWSER_FILES.includes(name),
          browser: BROWSER_FILES.includes(name),
        });
      }
    }
  }
  return targets;
}

const targets = collectTargets();
if (!targets.length) {
  console.error(
    '[patch-settings-loopback] 未找到 dsh-client-connection 客户端文件——请人工复核 dsh 构建布局：',
  );
  for (const root of ROOTS) console.error(`  ${root}/lib`);
  process.exit(1);
}

let patched = 0;
let browserPatched = false;

for (const target of targets) {
  const source = readFileSync(target.path, 'utf8');
  if (source.includes(FLAG)) {
    console.log(`[patch-settings-loopback] ${target.name} 已打过补丁，跳过`);
    patched += 1;
    if (target.browser) browserPatched = true;
    continue;
  }

  const anchor = [MINIFIED_ANCHOR, SPACED_ANCHOR].find((candidate) =>
    source.includes(candidate),
  );
  if (!anchor) {
    const message = `[patch-settings-loopback] ${target.name} 锚点未找到——dsh 版本可能已改动实现，请人工复核补丁点后再构建`;
    if (target.required) {
      console.error(message);
      process.exit(1);
    }
    console.warn(message);
    continue;
  }

  writeFileSync(target.path, source.replace(anchor, anchor.replace(/,$/, PATCH_SUFFIX)));
  console.log(`[patch-settings-loopback] ${target.name} 补丁完成`);
  patched += 1;
  if (target.browser) browserPatched = true;
}

if (!patched) {
  console.error('[patch-settings-loopback] 未匹配到任何补丁目标——请人工复核 dsh 构建布局');
  process.exit(1);
}
if (!browserPatched) {
  console.error('[patch-settings-loopback] 浏览器客户端未打上补丁——局域网设置持久化会失效，构建失败');
  process.exit(1);
}
console.log('[patch-settings-loopback] 全部完成：isLoopback 已支持 globalThis.__DSH_TRUSTED_LOOPBACK__ 通道');
