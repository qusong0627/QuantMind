"""container_guard 单测：边界去重与重启记录判定（纯函数，无 docker 依赖）。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'container_guard.py'
_spec = importlib.util.spec_from_file_location('container_guard', _MODULE_PATH)
guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)


def test_dedup_boundary_drops_identical_first_line():
    text = '2026-11-06T03:30:00Z line-a\n2026-11-06T03:30:01Z line-b\n'
    out = guard._dedup_boundary(text, '2026-11-06T03:30:00Z line-a')
    assert 'line-a' not in out and 'line-b' in out


def test_dedup_boundary_keeps_new_first_line():
    text = '2026-11-06T03:30:01Z line-b\n'
    out = guard._dedup_boundary(text, '2026-11-06T03:30:00Z line-a')
    assert 'line-b' in out


def test_dedup_boundary_without_previous():
    text = 'x\ny\n'
    assert guard._dedup_boundary(text, '') == text


def test_restart_record_baseline_and_change(monkeypatch, tmp_path):
    log_dir = tmp_path / 'containers'
    keeper = tmp_path / 'keepers'
    monkeypatch.setattr(guard, 'LOG_DIR', str(log_dir))
    monkeypatch.setattr(guard, 'KEEPER_DIR', str(keeper))
    monkeypatch.setattr(guard, 'RESTART_LOG', str(keeper / 'container_restarts.log'))

    info = {'started_at': '2026-11-06T03:30:38Z', 'finished_at': 'x',
            'exit_code': '0', 'oom': 'false', 'restart_count': '0'}
    # 首次运行只建基线
    assert guard.record_restart_if_any('quantmind', info, {}) is False
    # 未变化
    assert guard.record_restart_if_any('quantmind', info,
                                       {'started_at': info['started_at']}) is False
    # 变化 → 记录
    newer = {**info, 'started_at': '2026-11-06T05:30:00Z'}
    assert guard.record_restart_if_any('quantmind', newer, info) is True
    text = (keeper / 'container_restarts.log').read_text(encoding='utf-8')
    assert '容器重启' in text
    assert 'ExitCode    : 0  OOMKilled=false' in text


def test_state_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, 'KEEPER_DIR', str(tmp_path))
    monkeypatch.setattr(guard, 'STATE_PATH', str(tmp_path / '.state.json'))
    guard._save_state({'quantmind': {'last_log_ts': '2026-11-06T03:30:00Z'}})
    loaded = guard._load_state()
    assert loaded['quantmind']['last_log_ts'] == '2026-11-06T03:30:00Z'
    assert json.loads((tmp_path / '.state.json').read_text())['quantmind']
