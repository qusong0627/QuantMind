import pytest

from backend.services.engine.alpha_agent.hw_lock import (
    HardwareLockError,
    assert_factor_mining_hardware,
    probe_hardware,
)


def test_probe_fails_when_below_cpu_or_memory(monkeypatch):
    monkeypatch.delenv("ALPHA_AGENT_SKIP_HW_LOCK", raising=False)
    monkeypatch.setenv("ALPHA_AGENT_MIN_CPU_CORES", "8")
    monkeypatch.setenv("ALPHA_AGENT_MIN_MEMORY_GB", "32")
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_cpu_cores", lambda: 4
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_memory_bytes",
        lambda: 16_000_000_000,
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_cpu_cores", lambda: None
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_memory_bytes", lambda: None
    )

    probe = probe_hardware()
    assert probe.ok is False
    assert "4 核" in probe.message
    assert "16.0 GB" in probe.message
    with pytest.raises(HardwareLockError, match="低于最低门槛"):
        assert_factor_mining_hardware()


def test_probe_passes_8_core_32gb_machine(monkeypatch):
    monkeypatch.delenv("ALPHA_AGENT_SKIP_HW_LOCK", raising=False)
    monkeypatch.setenv("ALPHA_AGENT_MIN_CPU_CORES", "8")
    monkeypatch.setenv("ALPHA_AGENT_MIN_MEMORY_GB", "32")
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_cpu_cores", lambda: 8
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_memory_bytes",
        lambda: 32_000_000_000,
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_cpu_cores", lambda: None
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_memory_bytes", lambda: None
    )

    probe = assert_factor_mining_hardware()
    assert probe.ok is True
    assert probe.cpu_cores == 8


def test_cgroup_limit_is_stricter_than_host(monkeypatch):
    monkeypatch.delenv("ALPHA_AGENT_SKIP_HW_LOCK", raising=False)
    monkeypatch.setenv("ALPHA_AGENT_MIN_CPU_CORES", "8")
    monkeypatch.setenv("ALPHA_AGENT_MIN_MEMORY_GB", "32")
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_cpu_cores", lambda: 16
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_memory_bytes",
        lambda: 64_000_000_000,
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_cpu_cores", lambda: 4.0
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_memory_bytes",
        lambda: 8_000_000_000,
    )

    probe = probe_hardware()
    assert probe.cpu_cores == 4
    assert probe.memory_bytes == 8_000_000_000
    assert probe.ok is False


def test_skip_env_bypasses_lock(monkeypatch):
    monkeypatch.setenv("ALPHA_AGENT_SKIP_HW_LOCK", "1")
    monkeypatch.setenv("ALPHA_AGENT_MIN_CPU_CORES", "8")
    monkeypatch.setenv("ALPHA_AGENT_MIN_MEMORY_GB", "32")
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_cpu_cores", lambda: 2
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._host_memory_bytes",
        lambda: 4_000_000_000,
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_cpu_cores", lambda: None
    )
    monkeypatch.setattr(
        "backend.services.engine.alpha_agent.hw_lock._cgroup_memory_bytes", lambda: None
    )

    probe = assert_factor_mining_hardware()
    assert probe.ok is True
    assert "已跳过" in probe.message
