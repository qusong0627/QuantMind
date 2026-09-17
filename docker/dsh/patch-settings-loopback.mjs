#!/usr/bin/env node
/**
 * dsh 客户端补丁：让「可信前门」注入的 __DSH_TRUSTED_LOOPBACK__ 也走 isLoopback 分支。
 *
 * 背景：dsh（预览版）只在 loopback 页面启用设置持久化——dsh-client-ui-settings 里
 *   const persistence = ctx.remote.$host.isLoopback ? "host" : "memory"
 * 非 loopback（局域网 / 远程浏览器）时 settings 镜像直接进入终态 unavailable，
 * 模型提供方目录、MCP 连接器、欢迎声明、打开配置文件等所有走 settings 的界面
 * 都报 "settings are unavailable in this browser"。$host.isLoopback 的唯一数据源
 * 是 dsh-client-connection 里下面这一行计算，补它一处即可全修（2026-09-17 实测）。
 *
 * 我们的 nginx 前门（docker/dsh/nginx.conf）给每个页面注入
 *   globalThis.__DSH_TRUSTED_LOOPBACK__ = true
 * 使经前门访问的浏览器与 localhost 同权。真正的安全边界不在客户端：dsh 服务端
 * /api browser-trust 围栏（DSH_TRUSTED_HOSTS，entrypoint 传 --trusted-host）仍按
 * Host 精确匹配，非可信来源的 /api 一律 403——该标志给不了他们任何额外能力。
 *
 * 幂等：已打过补丁直接通过；锚点找不到（dsh 升级改了实现）时报错退出，让镜像构建
 * 失败提醒人工复核——静默失配会让局域网设置页再次损坏且无任何日志。
 */
import { readFileSync, writeFileSync } from 'node:fs';

const TARGET =
  '/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-connection/lib/client.js';
const FLAG = '__DSH_TRUSTED_LOOPBACK__';
const ANCHOR =
  'isLoopback: transport?.ownsHost === true || pageLocation === void 0 || isLoopbackHostname(pageLocation.hostname),';

const src = readFileSync(TARGET, 'utf8');
if (src.includes(FLAG)) {
  console.log('[patch-settings-loopback] 已打过补丁，跳过');
  process.exit(0);
}
if (!src.includes(ANCHOR)) {
  console.error('[patch-settings-loopback] 锚点未找到——dsh 版本可能已改动实现，请人工复核补丁点后再构建');
  process.exit(1);
}
const replacement = ANCHOR.replace(/,$/, ` || globalThis.${FLAG} === true,`);
writeFileSync(TARGET, src.replace(ANCHOR, replacement));
console.log('[patch-settings-loopback] 补丁完成：isLoopback 已支持 globalThis.__DSH_TRUSTED_LOOPBACK__ 通道');
