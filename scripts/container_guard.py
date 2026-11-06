#!/usr/bin/env python3
"""容器日志增量归档 + 重启现场记录（无需 root、不重建容器）。

背景（2026-11-06）：`/etc/docker/daemon.json` 的 `max-size=100m / max-file=3`
让单容器日志只保留约 90 分钟——11:30 的容器重启现场因此取证失败。改 daemon
配置要重建容器（会丢容器内直装依赖），所以改用用户态轮询：

1. **增量归档**：每次运行把 `docker logs --timestamps --since <上一条时间戳>`
   追加到 `logs/containers/<容器>-<YYYYMMDD>.log`；边界行去重。
2. **重启记录**：记录 `StartedAt/RestartCount/OOMKilled`；一旦变化，把
   前后现场（退出码、OOM、内存、归档日志尾部）写进
   `logs/keepers/container_restarts.log`。

由用户 crontab 每分钟调用一次；任何异常只记日志、绝不抛到 cron 邮件。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE, 'logs', 'containers')
KEEPER_DIR = os.path.join(BASE, 'logs', 'keepers')
STATE_PATH = os.path.join(KEEPER_DIR, '.container_guard_state.json')
RESTART_LOG = os.path.join(KEEPER_DIR, 'container_restarts.log')

DEFAULT_CONTAINERS = ('quantmind',)


def _run(cmd: str, timeout: int = 120) -> str:
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout)
        return (out.stdout or '') + (out.stderr or '')
    except Exception as exc:  # noqa: BLE001
        return f'[guard] command failed: {exc}\n'


def _inspect(container: str) -> dict:
    raw = _run(
        f"docker inspect {container} --format "
        "'{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.State.ExitCode}}|"
        "{{.State.OOMKilled}}|{{.RestartCount}}'", timeout=20).strip()
    parts = raw.split('|')
    if len(parts) < 5:
        return {}
    return {
        'started_at': parts[0],
        'finished_at': parts[1],
        'exit_code': parts[2],
        'oom': parts[3],
        'restart_count': parts[4],
    }


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(KEEPER_DIR, exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def _dedup_boundary(text: str, last_line: str) -> str:
    """`--since` 是包含边界的：去掉与上一条完全相同的第一行，避免重复。"""
    if not last_line:
        return text
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip('\n') == last_line:
        return ''.join(lines[1:])
    return text


def archive_logs(container: str, state: dict) -> dict:
    os.makedirs(LOG_DIR, exist_ok=True)
    # 首次运行只回捞 30 分钟：daemon 只保留 ~90 分钟，别一次灌 300MB
    since = state.get('last_log_ts') or '30m'
    text = _run(f'docker logs --timestamps --since {since} {container} 2>&1')
    text = _dedup_boundary(text, state.get('last_line') or '')
    if text.strip():
        path = os.path.join(LOG_DIR, f'{container}-{time.strftime("%Y%m%d")}.log')
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(text)
        last = text.rstrip('\n').splitlines()[-1]
        state['last_line'] = last
        # docker 行首是 RFC3339Nano 时间戳，`--since` 接受它
        state['last_log_ts'] = last.split(' ', 1)[0] if ' ' in last else since
    return state


def record_restart_if_any(container: str, info: dict, prev: dict) -> bool:
    """StartedAt 变化 = 容器重启；把现场写进 keeper 日志。返回是否变化。"""
    if not info:
        return False
    if prev.get('started_at') == info['started_at']:
        return False
    if not prev.get('started_at'):
        # 第一次运行只建立基线
        return False
    block = [
        f'==== {time.strftime("%Y-%m-%d %H:%M:%S")} 容器重启: {container} ====',
        f'  StartedAt   : {prev.get("started_at")} -> {info["started_at"]}',
        f'  FinishedAt  : {info["finished_at"]}',
        f'  ExitCode    : {info["exit_code"]}  OOMKilled={info["oom"]}  '
        f'RestartCount={info["restart_count"]}',
        f'  宿主内存    : {_run("free -m | head -2")}'.rstrip(),
    ]
    try:
        path = os.path.join(LOG_DIR, f'{container}-{time.strftime("%Y%m%d")}.log')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                tail = fh.readlines()[-200:]
            block.append('  --- 归档日志尾部（重启前）---')
            block.extend('  ' + line.rstrip('\n') for line in tail)
    except OSError:
        pass
    os.makedirs(KEEPER_DIR, exist_ok=True)
    with open(RESTART_LOG, 'a', encoding='utf-8') as fh:
        fh.write('\n'.join(block) + '\n')
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='容器日志归档与重启记录')
    parser.add_argument('--containers', default=os.environ.get(
        'QM_GUARD_CONTAINERS', ','.join(DEFAULT_CONTAINERS)))
    parser.add_argument('--no-archive', action='store_true')
    args = parser.parse_args(argv)

    state = _load_state()
    for container in [c.strip() for c in args.containers.split(',') if c.strip()]:
        entry = state.get(container) or {}
        try:
            info = _inspect(container)
            changed = record_restart_if_any(container, info, entry)
            if changed:
                print(f'{time.strftime("%H:%M:%S")} restart recorded {container}')
            entry['started_at'] = info.get('started_at') or entry.get('started_at')
            if not args.no_archive:
                entry = archive_logs(container, entry)
        except Exception as exc:  # noqa: BLE001
            print(f'{time.strftime("%H:%M:%S")} guard failed {container}: {exc}',
                  file=sys.stderr)
        state[container] = entry
    _save_state(state)
    return 0


if __name__ == '__main__':
    sys.exit(main())
