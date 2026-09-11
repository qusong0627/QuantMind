#!/usr/bin/env node
const { spawnSync } = require('child_process');
const path = require('path');

function run(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    stdio: 'inherit',
    shell: process.platform === 'win32',
    env: process.env,
  });
  if (result.error) {
    console.error(`[package-win] Failed to start: ${command}`, result.error);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

const electronRoot = path.resolve(__dirname, '..');

if (process.platform !== 'win32') {
  console.error('[package-win] 该脚本仅支持在 Windows 环境执行。');
  process.exit(1);
}

run('npm', ['run', 'build:react'], electronRoot);
run('npm', ['run', 'build:electron'], electronRoot);

const args = [
  'electron-builder',
  '--win',
  '--x64',
  '--publish=never',
  '--config.npmRebuild=false',
  '--config.nodeGypRebuild=false',
  '--config.buildDependenciesFromSource=false',
];

if (process.env.QUANTMIND_WIN_DIR_ONLY === '1') {
  args.splice(1, 0, '--dir');
}

run('npx', args, electronRoot);
