"""Trusted execution controller: file input, bounded Docker, file output."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import math
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from backend.shared.owner_path import owner_segment, user_scoped_dir
from backend.shared.sandbox_stock_pools import visible_pool_catalog
from backend.shared.user_code_sandbox import (
    allowed_image,
    checked_input,
    host_bind,
    isolated_options,
    public_qlib_mounts,
    public_factor_panel_input,
    public_quantdb_mounts,
    prepare_source_bundle,
    validate_user_code_source,
    validate_image,
)


class SandboxExecutionCancelled(RuntimeError):
    """Raised after the sandbox container has stopped in response to cancel."""


@contextmanager
def _observe_lease_stop(lease, budget, cancel_event):
    """Translate durable lease stop requests into the runner's local cancel event."""
    if lease is None:
        yield cancel_event
        return
    if budget is None:
        raise ValueError("A durable factor lease requires its resource budget")
    event = cancel_event or threading.Event()
    finished = threading.Event()

    def observe() -> None:
        while not finished.wait(0.5):
            try:
                snapshot = budget.store.task_snapshot(
                    budget.pool, lease.task_id, lease.tenant_id, lease.user_id
                )
            except Exception:
                continue
            if (
                snapshot is None
                or snapshot.get("state") == "cancelled"
                or snapshot.get("stop_requested")
            ):
                event.set()
                return

    thread = threading.Thread(
        target=observe,
        daemon=True,
        name=f"factor-stop-observer-{lease.task_id[:12]}",
    )
    thread.start()
    try:
        yield event
    finally:
        finished.set()
        thread.join(timeout=2)


def read_outputs(client, image: str, volume_name: str) -> dict[str, bytes]:
    """Collect bounded outputs from the task's size-limited tmpfs volume."""
    script = """import base64, json
from pathlib import Path
files = {}
total = 0
for name in ('result.json', 'status.json', 'events.jsonl', 'complete.json', 'session_state.bin', 'factor_series.bin', 'factor_values.parquet', 'result.parquet', 'qlib_res.csv', 'ret.csv', 'execution.log'):
    path = Path('/output') / name
    if path.is_symlink():
        raise ValueError('Symlink output is forbidden')
    if not path.is_file():
        continue
    limit = 48 * 1024 * 1024 if name in ('factor_values.parquet', 'result.parquet', 'ret.csv') else (8 * 1024 * 1024 if name in ('session_state.bin', 'factor_series.bin') else 4 * 1024 * 1024)
    if path.stat().st_size > limit:
        raise ValueError('Output file exceeds limit')
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    total += len(content)
    if len(content) > limit or total > 64 * 1024 * 1024:
        raise ValueError('Output exceeds transfer limit')
    files[name] = base64.b64encode(content).decode()
print(json.dumps(files))
"""
    result = None
    # 具名读取容器：detach=False + remove=True 在 start 失败时不会自动删除，
    # 匿名会留下随机名 Created 残留；具名后失败可在 except 中兜底删除。
    read_name = f"qm-output-read-{uuid.uuid4().hex[:12]}"
    try:
        result = client.containers.run(
            image,
            command=["python", "-I", "-c", script],
            name=read_name,
            volumes={volume_name: {"bind": "/output", "mode": "ro"}},
            environment={"PYTHONUNBUFFERED": "1"},
            detach=False,
            remove=True,
            user="65534:65534",
            read_only=True,
            network_mode="none",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=16,
            mem_limit="128m",
            cpu_period=100000,
            cpu_quota=100000,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m,mode=1777"},
            working_dir="/tmp",
        )
    except Exception:
        try:
            stale = client.containers.get(read_name)
            stale.remove(force=True, v=True)
        except Exception:
            pass
        raise
    if not isinstance(result, bytes) or len(result) > 88 * 1024 * 1024:
        raise ValueError("Sandbox output collection failed")
    return {
        name: base64.b64decode(value, validate=True)
        for name, value in json.loads(result).items()
    }


def run_container(
    payload: dict,
    timeout_sec: int,
    *,
    cancel_event: threading.Event | None = None,
    lease=None,
    budget=None,
    lifecycle: dict | None = None,
) -> tuple[int, dict[str, bytes], str]:
    # OSS-ADAPT: OSS 单容器无 docker 沙箱/远端挖掘节点，保留 API 签名，
    # 执行入口直接 raise（降级为本地 subprocess 的 worker 链在 OSS 不存在）。
    try:
        import docker
    except ImportError as exc:
        raise RuntimeError("OSS 单容器不支持远端挖掘节点：docker 沙箱不可用") from exc

    sandbox_mode = payload.get("_sandbox_mode", "lab")
    if sandbox_mode not in {
        "lab",
        "factor",
        "replay",
        "simulation",
        "dsl",
        "rdagent",
    }:
        raise ValueError("Unknown sandbox operation")
    if sandbox_mode != "factor" and payload.get("code") is not None:
        validate_user_code_source(payload["code"])
    root = Path(os.getenv("STRATEGY_LAB_TEMP_DIR", "/app/db/lab_tmp"))
    owner = user_scoped_dir(root, payload["tenant_id"], payload["user_id"])
    job = owner / owner_segment(payload["run_id"])
    # The run ID is generated by the trusted controller. Never reuse a directory.
    job.mkdir(parents=True, exist_ok=False)
    checked_input(job, root)
    client = container = output_keeper = output_volume = None
    lifecycle = lifecycle if lifecycle is not None else {}
    lifecycle.setdefault("cleanup_confirmed", True)
    lifecycle.setdefault("intent_recorded", False)
    create_uncertain = False
    try:
        request_path = job / "request.json"
        volume_name = f"qm-output-{payload['run_id']}"
        factor_input_path = payload.pop("_factor_input_path", None)
        factor_data_path = payload.pop("_factor_data_path", None)
        rdagent_workspace_path = payload.pop("_rdagent_workspace_path", None)
        rdagent_data_path = payload.pop("_rdagent_data_path", None)
        rdagent_data_root = payload.pop("_rdagent_data_root", None)
        factor_result_mode = payload.pop("_factor_result_mode", "metrics")
        quantdb_datasets = payload.pop("_quantdb_datasets", None)
        request_payload = {
            key: value for key, value in payload.items() if not key.startswith("_")
        }
        use_quantdb = (
            sandbox_mode == "rdagent" and payload.get("operation") == "factor"
        ) or (
            sandbox_mode == "factor" and "quantdb_data" in str(payload.get("code", ""))
        )
        if use_quantdb:
            from backend.services.engine.rd_agent.mining_datasets import (
                resolve_datasets,
            )

            request_payload["mining_datasets"] = list(
                resolve_datasets(quantdb_datasets)
            )
        if sandbox_mode == "factor":
            request_payload.pop("tenant_id", None)
            request_payload.pop("user_id", None)
            request_payload["factor_input_path"] = "/input/factor_input.pkl"
            request_payload["factor_result_mode"] = factor_result_mode
            request_payload["factor_include_series"] = bool(
                payload.get("_factor_include_series")
            )
            request_payload["factor_series_max_points"] = payload.get(
                "_factor_series_max_points", 750
            )
            if payload.get("_factor_cost_rate") is not None:
                request_payload["factor_cost_rate"] = payload["_factor_cost_rate"]
            if payload.get("_factor_universe_symbols") is not None:
                request_payload["universe_symbols"] = payload[
                    "_factor_universe_symbols"
                ]
            if factor_data_path:
                request_payload["factor_data_path"] = "/input/daily_pv.parquet"
        elif sandbox_mode == "replay":
            request_payload.pop("tenant_id", None)
            request_payload.pop("user_id", None)
            if payload.get("_replay_state_path"):
                request_payload["replay_state_path"] = "/input/replay_state.bin"
        elif sandbox_mode == "simulation":
            request_payload.pop("tenant_id", None)
            request_payload.pop("user_id", None)
            request_payload.pop("strategy_id", None)
        elif sandbox_mode == "dsl":
            request_payload.pop("tenant_id", None)
            request_payload.pop("user_id", None)
        elif sandbox_mode == "rdagent":
            request_payload.pop("tenant_id", None)
            request_payload.pop("user_id", None)
            request_payload["workspace_archive_path"] = "/input/workspace.zip"
            if rdagent_data_path:
                request_payload["factor_data_path"] = "/input/daily_pv.parquet"
        elif sandbox_mode == "lab":
            request_payload["qlib_data_path"] = "/sandbox/qlib"
        request_bytes = json.dumps(request_payload).encode("utf-8")
        if sandbox_mode == "replay" and len(request_bytes) > 64 * 1024 * 1024:
            raise ValueError("Replay request exceeds the 64 MiB limit")
        if sandbox_mode == "simulation" and len(request_bytes) > 32 * 1024 * 1024:
            raise ValueError("Simulation tick request exceeds the 32 MiB limit")
        if sandbox_mode == "dsl" and len(request_bytes) > 20 * 1024 * 1024:
            raise ValueError("DSL execution request exceeds the 20 MiB limit")
        request_path.write_bytes(request_bytes)
        request_path.chmod(0o644)
        checked_input(request_path, root)
        source = prepare_source_bundle(job / "source")
        mounts = (
            public_qlib_mounts() if sandbox_mode in {"lab", "rdagent"} else {}
        )
        mounts[host_bind(request_path)] = {"bind": "/input/request.json", "mode": "ro"}
        if use_quantdb:
            mounts.update(public_quantdb_mounts(quantdb_datasets))
        if sandbox_mode == "factor":
            if factor_input_path:
                input_path = checked_input(factor_input_path, root)
                if input_path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("Factor input exceeds the 32 MiB limit")
                mounts[host_bind(input_path)] = {
                    "bind": "/input/factor_input.pkl",
                    "mode": "ro",
                }
            if factor_data_path:
                data_path = public_factor_panel_input(factor_data_path)
                mounts[host_bind(data_path)] = {
                    "bind": "/input/daily_pv.parquet",
                    "mode": "ro",
                }
            if not factor_input_path and not factor_data_path:
                raise ValueError("Factor input is missing")
        elif sandbox_mode == "replay":
            state_path = payload.get("_replay_state_path")
            if state_path:
                input_path = checked_input(state_path, root)
                if input_path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("Replay state exceeds the 8 MiB limit")
                mounts[host_bind(input_path)] = {
                    "bind": "/input/replay_state.bin",
                    "mode": "ro",
                }
        elif sandbox_mode == "rdagent":
            if not rdagent_workspace_path:
                raise ValueError("RD-Agent workspace archive is missing")
            workspace_path = checked_input(rdagent_workspace_path, root)
            if workspace_path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("RD-Agent workspace archive exceeds the 64 MiB limit")
            mounts[host_bind(workspace_path)] = {
                "bind": "/input/workspace.zip",
                "mode": "ro",
            }
            if rdagent_data_path:
                if rdagent_data_root:
                    data_path = checked_input(rdagent_data_path, rdagent_data_root)
                    if data_path.stat().st_size > 2 * 1024 * 1024 * 1024:
                        raise ValueError(
                            "RD-Agent task factor input exceeds the 2 GiB limit"
                        )
                else:
                    data_path = public_factor_panel_input(rdagent_data_path)
                mounts[host_bind(data_path)] = {
                    "bind": "/input/daily_pv.parquet",
                    "mode": "ro",
                }
        elif sandbox_mode == "lab":
            catalog_path = job / "pools.json"
            catalog_path.write_text(
                json.dumps(
                    visible_pool_catalog(payload["tenant_id"], payload["user_id"])
                ),
                encoding="utf-8",
            )
            catalog_path.chmod(0o644)
            mounts[host_bind(catalog_path)] = {
                "bind": "/input/pools.json",
                "mode": "ro",
            }
        mounts[host_bind(source)] = {"bind": "/app/backend", "mode": "ro"}
        image = allowed_image(
            None, os.getenv("STRATEGY_LAB_RUNNER_IMAGE", "quantmind-sandbox:latest")
        )
        client = docker.from_env()
        validate_image(client.images.get(image))
        lease_labels = (
            {
                "quantmind.job_id": lease.task_id,
                "quantmind.lease_token": lease.token,
                "quantmind.resource_pool": budget.pool,
            }
            if lease is not None
            else {}
        )
        labels = {
            "quantmind.run_id": payload["run_id"],
            "quantmind.tenant_id": payload["tenant_id"],
            "quantmind.user_id": payload["user_id"],
            **lease_labels,
            "quantmind.role": "sandbox",
        }
        keeper_name = f"qm-output-hold-{payload['run_id']}"
        sandbox_name = (
            f"qm-rdagent-run-{payload['run_id']}"
            if sandbox_mode == "rdagent"
            else f"qm-lab-run-{payload['run_id']}"
        )
        keeper_labels = {**labels, "quantmind.role": "output_keeper"}
        daemon_id = str(client.info().get("ID") or "") if lease is not None else ""
        related_containers = [{"name": keeper_name, "labels": keeper_labels}]
        if lease is not None:
            from backend.services.engine.routers.ai_ide.resource_budget import (
                record_container_intent,
            )

            if not daemon_id or not record_container_intent(
                lease,
                daemon_id=daemon_id,
                container_name=sandbox_name,
                labels=labels,
                related_containers=related_containers,
                volume_name=volume_name,
                volume_labels=keeper_labels,
                budget=budget,
            ):
                raise RuntimeError("Could not persist Strategy Lab container intent")
            lifecycle["intent_recorded"] = True
            lifecycle["cleanup_confirmed"] = False
            if cancel_event is not None and cancel_event.is_set():
                raise SandboxExecutionCancelled(
                    "Sandbox execution cancelled before Docker create"
                )
            # The following intent covers helper cleanup; user code starts only
            # after the sandbox container create call below.
        create_uncertain = True
        output_volume = client.volumes.create(
            name=volume_name,
            driver="local",
            driver_opts={
                "type": "tmpfs",
                "device": "tmpfs",
                "o": (
                    "size=64m,uid=65534,gid=65534,mode=0700"
                    if sandbox_mode == "rdagent"
                    or factor_result_mode == "functional_values"
                    else "size=16m,uid=65534,gid=65534,mode=0700"
                ),
            },
            labels=keeper_labels,
        )
        create_uncertain = False
        keeper_options = isolated_options()
        if sandbox_mode == "simulation":
            keeper_options["mem_limit"] = "64m"
            keeper_options["pids_limit"] = 16
        keeper_options["tmpfs"].pop("/output", None)
        create_uncertain = True
        output_keeper = client.containers.run(
            image,
            command=["python", "-I", "-c", "import time; time.sleep(3600)"],
            name=keeper_name,
            detach=True,
            volumes={volume_name: {"bind": "/output", "mode": "ro"}},
            environment={"PYTHONUNBUFFERED": "1", "HOME": "/tmp"},
            labels=keeper_labels,
            **keeper_options,
        )
        create_uncertain = False
        output_keeper.reload()
        if output_keeper.status != "running":
            raise RuntimeError("Sandbox output volume keeper failed to start")
        mounts[volume_name] = {"bind": "/output", "mode": "rw"}
        sandbox_options = isolated_options()
        if sandbox_mode == "rdagent":
            sandbox_options["tmpfs"]["/tmp"] = "rw,noexec,nosuid,size=512m,mode=1777"
            sandbox_options["tmpfs"]["/output"] = (
                "rw,noexec,nosuid,size=64m,mode=0700,uid=65534,gid=65534"
            )
        if sandbox_mode == "simulation":
            sandbox_options["mem_limit"] = os.getenv("SIMULATION_MEMORY_LIMIT", "512m")
            sandbox_options["cpu_quota"] = 50000
        # A size-limited local tmpfs volume survives task exit for collection.
        sandbox_options["tmpfs"].pop("/output", None)
        if lease is not None:
            if not daemon_id or not record_container_intent(
                lease,
                daemon_id=daemon_id,
                container_name=sandbox_name,
                labels=labels,
                related_containers=related_containers,
                volume_name=volume_name,
                volume_labels=keeper_labels,
                budget=budget,
            ):
                raise RuntimeError("Could not persist Strategy Lab container intent")
            lifecycle["intent_recorded"] = True
            lifecycle["cleanup_confirmed"] = False
            lifecycle["create_started"] = True
        create_uncertain = True
        container = client.containers.run(
            image,
            command=[
                "python",
                "-m",
                "backend.shared.sandbox_entrypoint",
                "rdagent" if sandbox_mode == "rdagent" else sandbox_mode,
            ],
            name=sandbox_name,
            detach=True,
            volumes=mounts,
            environment={
                "PYTHONPATH": (
                    "/app:/app/backend/services/engine/rd_agent"
                    if sandbox_mode in {"factor", "rdagent"}
                    else "/app"
                ),
                "PYTHONUNBUFFERED": "1",
                "USER_CODE_OUTPUT_DIR": "/output",
                "USER_CODE_POOL_CATALOG": "/input/pools.json",
                "QLIB_DATA_PATH": "/sandbox/qlib",
                "HOME": "/tmp",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            },
            labels=labels,
            log_config=docker.types.LogConfig(
                type="json-file", config={"max-size": "1m", "max-file": "1"}
            ),
            **sandbox_options,
        )
        create_uncertain = False
        if lease is not None:
            if not record_container_intent(
                lease,
                daemon_id=str(client.info().get("ID") or ""),
                container_name=sandbox_name,
                labels=labels,
                related_containers=related_containers,
                volume_name=volume_name,
                volume_labels=keeper_labels,
                container_id=str(container.id),
                budget=budget,
            ):
                _stop_container_confirmed(container)
                lifecycle["confirmed_stopped"] = container.status in {
                    "exited",
                    "dead",
                }
                raise RuntimeError("Strategy Lab execution lease was lost")
        max_runtime = 3600 if sandbox_mode == "rdagent" else 600
        deadline = time.monotonic() + min(max(int(timeout_sec), 1), max_runtime)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    container.kill()
                except Exception:
                    pass
                stop_deadline = time.monotonic() + 10
                while time.monotonic() < stop_deadline:
                    container.reload()
                    if container.status not in {"running", "created", "restarting"}:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Sandbox did not stop after cancellation")
                lifecycle["confirmed_stopped"] = True
                raise SandboxExecutionCancelled("Sandbox execution cancelled")
            container.reload()
            if container.status in {"running", "created"}:
                time.sleep(0.25)
                continue
            if container.status not in {"running", "created"}:
                lifecycle["confirmed_stopped"] = container.status in {
                    "exited",
                    "dead",
                }
                code = int(container.attrs["State"].get("ExitCode", 1))
                outputs = read_outputs(client, image, volume_name)
                logs = container.logs(tail=200).decode("utf-8", errors="replace")[
                    -65536:
                ]
                return code, outputs, logs
            time.sleep(0.25)
        _stop_container_confirmed(container)
        lifecycle["confirmed_stopped"] = container.status in {"exited", "dead"}
        return -9, {}, "Sandbox execution timeout"
    finally:
        cleanup_ok = True
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                cleanup_ok = False
        if output_keeper is not None:
            try:
                output_keeper.remove(force=True)
            except Exception:
                cleanup_ok = False
        if client is not None:
            if output_volume is not None:
                try:
                    output_volume.remove(force=True)
                except Exception:
                    cleanup_ok = False
            try:
                client.close()
            except Exception:
                cleanup_ok = False
        if job.exists() and checked_input(job, root) == job.resolve():
            try:
                shutil.rmtree(job)
            except OSError:
                cleanup_ok = False
        lifecycle["cleanup_confirmed"] = cleanup_ok and not create_uncertain


def _stop_container_confirmed(container, *, timeout_sec: float = 10.0) -> bool:
    """Stop a sandbox and return true only after Docker confirms it is terminal."""
    try:
        container.kill()
    except Exception:
        pass
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            container.reload()
            if container.status in {"exited", "dead"}:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def budgeted_run_container(
    payload: dict,
    timeout_sec: int,
    *,
    budget=None,
    cancel_event: threading.Event | None = None,
    on_started=None,
    queue_wait_sec: int | None = None,
) -> tuple[int, dict[str, bytes], str]:
    """Run a non-factor user-code sandbox under the durable shared pool."""
    if payload.get("_sandbox_mode", "lab") == "factor":
        raise ValueError("Factor mining is excluded from the user-code pool")
    if not payload.get("tenant_id") or not payload.get("user_id"):
        raise ValueError("Strategy Lab execution requires an authenticated owner")
    from backend.services.engine.routers.ai_ide.resource_budget import (
        ExecutionCapacityFull,
        cancel_queued,
        get_budget,
    )
    from backend.shared.resource_lease import LeaseUnavailable

    run_id = str(payload["run_id"])
    tenant_id, user_id = str(payload["tenant_id"]), str(payload["user_id"])
    budget = budget or get_budget()
    pool = budget.pool
    if queue_wait_sec is None:
        queue_wait_sec = max(
            1, int(os.getenv("STRATEGY_LAB_QUEUE_WAIT_SECONDS", "1800"))
        )
    deadline = time.monotonic() + queue_wait_sec
    lifecycle = {"create_started": False, "confirmed_stopped": False}
    terminal = {"state": "failed"}
    heartbeat_stop = threading.Event()
    stop_event = threading.Event()

    def observe_stop_request():
        while not heartbeat_stop.wait(1):
            if cancel_event is not None and cancel_event.is_set():
                stop_event.set()
                return
            try:
                snapshot = budget.store.task_snapshot(pool, run_id, tenant_id, user_id)
            except Exception:
                continue
            if snapshot is None or snapshot["state"] == "cancelled":
                stop_event.set()
                return
            if snapshot["stop_requested"]:
                stop_event.set()
                return

    def heartbeat_controller():
        while not heartbeat_stop.wait(10):
            try:
                _touch_run_owner(run_id)
            except Exception:
                # The durable lease heartbeat remains authoritative once running.
                pass

    heartbeat_thread = threading.Thread(
        target=heartbeat_controller,
        daemon=True,
        name=f"lab-controller-{run_id[:12]}",
    )
    heartbeat_thread.start()
    stop_thread = threading.Thread(
        target=observe_stop_request,
        daemon=True,
        name=f"lab-stop-observer-{run_id[:12]}",
    )
    stop_thread.start()

    def resolve_terminal(_lease, snapshot):
        if not lifecycle["create_started"] or lifecycle["confirmed_stopped"]:
            if snapshot.get("stop_requested"):
                return "cancelled"
            return terminal["state"]
        return None

    try:
        while True:
            try:
                queue_state = budget.store.task_state(pool, run_id, tenant_id, user_id)
                if queue_state == "cancelled":
                    terminal["state"] = "cancelled"
                    raise SandboxExecutionCancelled(
                        "Strategy Lab execution cancelled while queued"
                    )
                if queue_state != "queued":
                    raise LeaseUnavailable(
                        "Strategy Lab queued reservation is no longer available"
                    )
                with budget.running(
                    run_id,
                    tenant_id,
                    user_id,
                    executor={"kind": "strategy_lab", "run_id": run_id},
                    terminal_resolver=resolve_terminal,
                ) as lease:
                    if on_started is not None:
                        on_started()
                    result = run_container(
                        payload,
                        timeout_sec,
                        cancel_event=stop_event,
                        lease=lease,
                        budget=budget,
                        lifecycle=lifecycle,
                    )
                    code, outputs, _logs = result
                    terminal["state"] = (
                        "completed"
                        if code == 0 and "result.json" in outputs
                        else "failed"
                    )
                    return result
            except ExecutionCapacityFull as exc:
                if time.monotonic() >= deadline:
                    try:
                        cancel_queued(run_id, tenant_id, user_id, budget=budget)
                    except Exception:
                        pass
                    raise ExecutionCapacityFull(
                        "Strategy Lab execution queue wait expired"
                    ) from exc
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        cancel_queued(run_id, tenant_id, user_id, budget=budget)
                    except Exception:
                        pass
                    raise SandboxExecutionCancelled(
                        "Strategy Lab queue wait cancelled"
                    ) from exc
                time.sleep(0.25)
            except LeaseUnavailable:
                raise
            except SandboxExecutionCancelled:
                terminal["state"] = "cancelled"
                raise
    finally:
        heartbeat_stop.set()
        stop_event.set()
        heartbeat_thread.join(timeout=2)
        stop_thread.join(timeout=2)
        try:
            _finish_run_owner(run_id)
        except Exception:
            pass


def _budgeted_call(payload: dict, timeout_sec: int, **kwargs):
    from backend.services.engine.routers.ai_ide.resource_budget import (
        cancel_queued,
        get_simulation_budget,
        submit_budget,
    )

    admission = (
        {"budget": get_simulation_budget()}
        if payload.get("_sandbox_mode") == "simulation"
        else {}
    )
    budget = submit_budget(
        payload["run_id"], payload["tenant_id"], payload["user_id"], **admission
    )
    try:
        _write_run_owner(payload["run_id"], payload["tenant_id"], payload["user_id"])
        return budgeted_run_container(payload, timeout_sec, budget=budget, **kwargs)
    except Exception:
        try:
            cancel_queued(
                payload["run_id"],
                payload["tenant_id"],
                payload["user_id"],
                budget=budget,
            )
        except Exception:
            pass
        raise


def run_operation(
    code: str, *, tenant_id: str, user_id: str, mode: str, **fields
) -> dict:
    """Overfit and scheduled scans use the same boundary as interactive runs."""
    run_id = uuid.uuid4().hex
    payload = {
        **fields,
        "code": code,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "run_id": run_id,
        "mode": mode,
    }
    rc, outputs, logs = _budgeted_call(payload, timeout_sec=600)
    if rc != 0 or "result.json" not in outputs:
        raise RuntimeError(f"Isolated {mode} failed: {logs[-1024:]}")
    result = json.loads(outputs["result.json"])
    if result.get("run_id") != run_id:
        raise ValueError("Sandbox result run ID mismatch")
    return result


def _write_run_owner(run_id: str, tenant_id: str, user_id: str) -> None:
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client
    from .progress import _TTL_SECONDS, meta_key

    client = get_redis_sentinel_client()
    client.hset(
        meta_key(run_id),
        mapping={
            "kind": "strategy_lab",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "controller_heartbeat": str(time.time()),
        },
    )
    client.expire(meta_key(run_id), _TTL_SECONDS)


def _touch_run_owner(run_id: str) -> None:
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client
    from .progress import meta_key

    get_redis_sentinel_client().hset(
        meta_key(run_id), mapping={"controller_heartbeat": str(time.time())}
    )


def _finish_run_owner(run_id: str) -> None:
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client
    from .progress import meta_key

    get_redis_sentinel_client().hset(
        meta_key(run_id), mapping={"controller_finished": str(time.time())}
    )


def run_simulation_tick(
    *,
    code: str,
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    run_id: str,
    state: dict,
    account: dict,
    exec_config: dict,
    live_trade_config: dict,
    current_time: float,
    cancel_event: threading.Event | None = None,
    sdk_snapshot: dict | None = None,
) -> dict:
    """Execute one hosted simulation tick in the constrained code container."""
    task_id = uuid.uuid4().hex
    rc, outputs, logs = _budgeted_call(
        {
            "_sandbox_mode": "simulation",
            "mode": "simulation",
            "run_id": task_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "strategy_run_id": run_id,
            "code": code,
            "state": state,
            "account": account,
            "exec_config": exec_config,
            "live_trade_config": live_trade_config,
            "current_time": current_time,
            "sdk_snapshot": sdk_snapshot,
        },
        timeout_sec=30,
        cancel_event=cancel_event,
        queue_wait_sec=15,
    )
    raw = outputs.get("result.json")
    if raw is None:
        raise RuntimeError(
            f"Isolated simulation tick failed (exit={rc}): "
            f"{logs[-1024:] or 'sandbox did not produce result.json'}"
        )
    result = json.loads(raw)
    if result.get("run_id") != task_id:
        raise ValueError("Simulation sandbox result run ID mismatch")
    if rc != 0:
        reason = str(result.get("error") or logs[-1024:] or "unknown sandbox error")
        raise RuntimeError(f"Isolated simulation tick failed (exit={rc}): {reason}")
    return result


def run_dsl_strategy(
    *,
    code: str,
    strategy_name: str,
    data_json: str,
    tenant_id: str,
    user_id: str,
    run_id: str,
) -> dict:
    """Execute compiler output against serialized public data in the sandbox."""
    task_id = uuid.uuid4().hex
    rc, outputs, logs = _budgeted_call(
        {
            "_sandbox_mode": "dsl",
            "mode": "dsl",
            "run_id": task_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "strategy_run_id": run_id,
            "strategy_name": strategy_name,
            "code": code,
            "data_json": data_json,
        },
        timeout_sec=60,
    )
    raw = outputs.get("result.json")
    if rc != 0 or raw is None:
        raise RuntimeError(f"Isolated DSL execution failed: {logs[-1024:]}")
    result = json.loads(raw)
    if result.get("run_id") != task_id:
        raise ValueError("DSL sandbox result run ID mismatch")
    return result


def run_factor_metrics(
    code: str, frame, *, tenant_id: str, user_id: str
) -> dict[str, float | int | None]:
    """Compute RD-Agent factor metrics on one public Qlib frame in Docker."""
    import uuid

    root = Path(os.getenv("STRATEGY_LAB_TEMP_DIR", "/app/db/lab_tmp"))
    owner = user_scoped_dir(root, tenant_id, user_id)
    owner.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    staged = owner / f"factor-input-{run_id}.pkl"
    try:
        if not isinstance(code, str) or len(code.encode("utf-8")) > 256 * 1024:
            raise ValueError("Factor source exceeds the 256 KiB limit")
        # Pickle is only used for this trusted-side-generated frame; the file is
        # mounted read-only and never accepted from a client upload.
        frame.to_pickle(staged, protocol=4)
        if staged.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Factor input exceeds the 32 MiB limit")
        rc, outputs, logs = run_container(
            {
                "mode": "factor",
                "_sandbox_mode": "factor",
                "run_id": run_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "code": code,
                "_factor_input_path": str(staged),
            },
            timeout_sec=300,
        )
        raw = outputs.get("result.json")
        if rc != 0 or raw is None:
            raise RuntimeError(f"Isolated factor evaluation failed: {logs[-1024:]}")
        result = json.loads(raw)
        if result.get("run_id") != run_id:
            raise ValueError("Sandbox factor result run ID mismatch")
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("Sandbox factor metrics are invalid")
        return metrics
    finally:
        staged.unlink(missing_ok=True)
        try:
            owner.rmdir()
        except OSError:
            # Other simultaneous tasks may still own files in this directory.
            pass


def run_factor_series(
    code: str,
    frame,
    *,
    tenant_id: str,
    user_id: str,
    datasets: list[str] | tuple[str, ...] | None = None,
    cancel_event: threading.Event | None = None,
    lease=None,
    budget=None,
    lifecycle: dict | None = None,
):
    """Evaluate a user Factor class in Docker and return aligned float64 values."""
    import numpy as np

    root = Path(os.getenv("STRATEGY_LAB_TEMP_DIR", "/app/db/lab_tmp"))
    owner = user_scoped_dir(root, tenant_id, user_id)
    owner.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    staged = owner / f"factor-input-{run_id}.pkl"
    try:
        if not isinstance(code, str) or len(code.encode("utf-8")) > 256 * 1024:
            raise ValueError("Factor source exceeds the 256 KiB limit")
        frame.to_pickle(staged, protocol=4)
        if staged.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Factor input exceeds the 32 MiB limit")
        payload = {
            "mode": "factor",
            "_sandbox_mode": "factor",
            "_factor_result_mode": "series",
            "_quantdb_datasets": datasets,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "code": code,
            "_factor_input_path": str(staged),
        }
        run_options = {}
        if lease is not None:
            run_options.update(lease=lease, budget=budget, lifecycle=lifecycle)
        with _observe_lease_stop(lease, budget, cancel_event) as observed_cancel:
            if observed_cancel is not None:
                run_options["cancel_event"] = observed_cancel
            rc, outputs, logs = run_container(payload, timeout_sec=300, **run_options)
        raw = outputs.get("factor_series.bin")
        if rc != 0 or raw is None:
            raise RuntimeError(f"Isolated factor evaluation failed: {logs[-1024:]}")
        if len(raw) != len(frame) * 8:
            raise ValueError("Sandbox factor series shape does not match input")
        values = np.frombuffer(raw, dtype="<f8")
        return values.astype(np.float64, copy=False)
    finally:
        staged.unlink(missing_ok=True)
        try:
            owner.rmdir()
        except OSError:
            pass


def run_rdagent_workspace(
    *,
    workspace_archive_path: str | Path,
    operation: str,
    timeout_seconds: int,
    config_name: str = "conf.yaml",
    run_env: dict[str, str] | None = None,
    tenant_id: str,
    user_id: str,
    task_id: str,
    run_id: str | None = None,
    debug_data: bool = False,
    datasets: list[str] | tuple[str, ...] | None = None,
    universe_symbols: list[str] | None = None,
    cancel_event: threading.Event | None = None,
    lease=None,
    budget=None,
    lifecycle: dict | None = None,
) -> tuple[int, dict[str, bytes], str]:
    """Run one fixed RD-Agent operation over a private, engine-validated archive."""
    if operation not in {"factor", "qlib"}:
        raise ValueError("Unsupported RD-Agent sandbox operation")
    if operation == "qlib" and config_name not in {
        "conf.yaml",
        "conf_baseline.yaml",
        "conf_combined_factors.yaml",
        "conf_combined_factors_sota_model.yaml",
        "conf_baseline_factors_model.yaml",
        "conf_sota_factors_model.yaml",
    }:
        raise ValueError("Qlib config name is not allowlisted")
    if operation != "qlib" and run_env:
        raise ValueError("Qlib environment overrides are only valid for Qlib")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError("RD-Agent sandbox timeout is invalid")
    root = Path(os.getenv("STRATEGY_LAB_TEMP_DIR", "/app/db/lab_tmp"))
    archive = checked_input(workspace_archive_path, root)
    if archive.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("RD-Agent workspace archive exceeds the 64 MiB limit")
    run_id = owner_segment(run_id or uuid.uuid4().hex)
    payload = {
        "_sandbox_mode": "rdagent",
        "run_id": run_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "task_id": task_id,
        "operation": operation,
        "timeout_seconds": timeout_seconds,
        "debug_data": bool(debug_data),
        "universe_symbols": universe_symbols,
        "config_name": config_name,
        "run_env": run_env or {},
        "_rdagent_workspace_path": str(archive),
        "_quantdb_datasets": datasets,
    }
    if operation == "factor":
        from backend.services.engine.alpha_agent.launcher import get_launcher

        launcher = get_launcher()
        task_root = launcher.task_dir(task_id, tenant_id, user_id)
        owner_root = user_scoped_dir(launcher._log_dir, tenant_id, user_id)
        # The RD-Agent wrapper prepares the selected public dataset under this
        # task's private workspace. Resolve that exact snapshot; never silently
        # substitute the shared 1.5 GiB all-market cache when it is missing.
        checked_input(task_root, owner_root)
        # RD-Agent's debug folder keeps the canonical daily_pv.parquet name
        # (rd_loop_wrapper writes ..._debug/daily_pv.parquet). The sandbox
        # worker always mounts it at /input/daily_pv.parquet, so the source
        # filename must not carry a "_debug" suffix.
        filename = "daily_pv.parquet"
        data_path = (
            task_root
            / "git_ignore_folder"
            / (
                "factor_implementation_source_data_debug"
                if debug_data
                else "factor_implementation_source_data"
            )
            / filename
        )
        data_root: str | None = str(task_root)
        if data_path.is_symlink():
            # _link_panel may fall back to a symlink when hard links are
            # unavailable. Such a link is allowed only when it resolves to a
            # known public factor panel input.
            data_path = public_factor_panel_input(data_path.resolve(strict=True))
            data_root = None
        else:
            data_path = checked_input(data_path, task_root)
            if data_path.stat().st_size > 2 * 1024 * 1024 * 1024:
                raise ValueError("RD-Agent task factor input exceeds the 2 GiB limit")
        payload["_rdagent_data_path"] = str(data_path)
        if data_root is not None:
            payload["_rdagent_data_root"] = data_root
    run_options = {}
    if lease is not None:
        run_options.update(lease=lease, budget=budget, lifecycle=lifecycle)
    with _observe_lease_stop(lease, budget, cancel_event) as observed_cancel:
        if observed_cancel is not None:
            run_options["cancel_event"] = observed_cancel
        return run_container(payload, timeout_sec=timeout_seconds, **run_options)


def run_functional_factor_metrics(
    code: str,
    data_path: str,
    *,
    tenant_id: str,
    user_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    universe_symbols: list[str] | None = None,
    datasets: list[str] | tuple[str, ...] | None = None,
    cancel_event: threading.Event | None = None,
    lease=None,
    budget=None,
    lifecycle: dict | None = None,
    include_series: bool = False,
    cost_rate: float | None = None,
    series_max_points: int = 750,
) -> dict[str, Any]:
    """Evaluate a calculate_* factor against an allowlisted public parquet panel."""
    if not isinstance(code, str) or len(code.encode("utf-8")) > 256 * 1024:
        raise ValueError("Factor source exceeds the 256 KiB limit")
    public_factor_panel_input(data_path)
    root = Path(os.getenv("STRATEGY_LAB_TEMP_DIR", "/app/db/lab_tmp"))
    owner = user_scoped_dir(root, tenant_id, user_id)
    owner.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    try:
        payload = {
            "mode": "factor",
            "_sandbox_mode": "factor",
            "_factor_result_mode": "functional_metrics",
            "_quantdb_datasets": datasets,
            "_factor_include_series": bool(include_series),
            "_factor_data_path": data_path,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "code": code,
            "start_date": start_date,
            "end_date": end_date,
        }
        if cost_rate is not None:
            if (
                not isinstance(cost_rate, (int, float))
                or not math.isfinite(float(cost_rate))
                or cost_rate < 0
            ):
                raise ValueError("Factor cost rate is invalid")
            payload["_factor_cost_rate"] = float(cost_rate)
        if not isinstance(series_max_points, int) or not 2 <= series_max_points <= 750:
            raise ValueError("Factor series point limit is invalid")
        payload["_factor_series_max_points"] = series_max_points
        if universe_symbols is not None:
            if (
                not isinstance(universe_symbols, list)
                or not universe_symbols
                or len(universe_symbols) > 20_000
                or any(not isinstance(symbol, str) for symbol in universe_symbols)
            ):
                raise ValueError("Factor universe snapshot is invalid")
            payload["_factor_universe_symbols"] = universe_symbols
        run_options = {}
        if lease is not None:
            run_options.update(lease=lease, budget=budget, lifecycle=lifecycle)
        with _observe_lease_stop(lease, budget, cancel_event) as observed_cancel:
            if observed_cancel is not None:
                run_options["cancel_event"] = observed_cancel
            rc, outputs, logs = run_container(payload, timeout_sec=600, **run_options)
        raw = outputs.get("result.json")
        if rc != 0 or raw is None:
            raise RuntimeError(f"Isolated factor evaluation failed: {logs[-1024:]}")
        result = json.loads(raw)
        if result.get("run_id") != run_id:
            raise ValueError("Sandbox factor result run ID mismatch")
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("Sandbox factor metrics are invalid")
        if include_series:
            series = result.get("series")
            if not isinstance(series, dict):
                raise ValueError("Sandbox factor curves are invalid")
            return {"metrics": metrics, "series": series}
        return metrics
    finally:
        try:
            owner.rmdir()
        except OSError:
            pass


def run_functional_factor_values(
    code: str,
    data_path: str,
    *,
    tenant_id: str,
    user_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    universe_symbols: list[str] | None = None,
    datasets: list[str] | tuple[str, ...] | None = None,
    cancel_event: threading.Event | None = None,
    lease=None,
    budget=None,
    lifecycle: dict | None = None,
):
    """Materialize calculate_* factor rows inside the isolated factor sandbox."""
    import io

    import pandas as pd

    if not isinstance(code, str) or len(code.encode("utf-8")) > 256 * 1024:
        raise ValueError("Factor source exceeds the 256 KiB limit")
    public_factor_panel_input(data_path)
    if universe_symbols is not None and (
        not isinstance(universe_symbols, list)
        or not universe_symbols
        or len(universe_symbols) > 20_000
        or any(not isinstance(symbol, str) for symbol in universe_symbols)
    ):
        raise ValueError("Factor universe snapshot is invalid")
    root = Path(os.getenv("STRATEGY_LAB_TEMP_DIR", "/app/db/lab_tmp"))
    owner = user_scoped_dir(root, tenant_id, user_id)
    owner.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    try:
        payload = {
            "mode": "factor",
            "_sandbox_mode": "factor",
            "_factor_result_mode": "functional_values",
            "_quantdb_datasets": datasets,
            "_factor_data_path": data_path,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "code": code,
            "start_date": start_date,
            "end_date": end_date,
        }
        if universe_symbols is not None:
            payload["_factor_universe_symbols"] = universe_symbols
        run_options = {}
        if lease is not None:
            run_options.update(lease=lease, budget=budget, lifecycle=lifecycle)
        with _observe_lease_stop(lease, budget, cancel_event) as observed_cancel:
            if observed_cancel is not None:
                run_options["cancel_event"] = observed_cancel
            rc, outputs, logs = run_container(payload, timeout_sec=600, **run_options)
        raw = outputs.get("result.json")
        parquet = outputs.get("factor_values.parquet")
        if rc == 137:
            raise RuntimeError(
                "沙箱因内存超限被终止（OOMKilled）：面板过大或窗口过长，"
                "请缩小 universe/日期区间后重试"
            )
        if rc != 0 or raw is None or parquet is None:
            raise RuntimeError(
                f"Isolated factor materialization failed: {logs[-1024:]}"
            )
        result = json.loads(raw)
        if result.get("run_id") != run_id:
            raise ValueError("Sandbox factor result run ID mismatch")
        row_count = result.get("row_count")
        if type(row_count) is not int or not 1 <= row_count <= 1_000_000:
            raise ValueError("Sandbox factor row count is invalid")
        if len(parquet) > 48 * 1024 * 1024:
            raise ValueError("Sandbox factor output exceeds the 48 MiB limit")
        frame = pd.read_parquet(io.BytesIO(parquet))
        if (
            list(frame.columns) != ["symbol", "trade_date", "value"]
            or len(frame) != row_count
            or frame.empty
            or frame.duplicated(["symbol", "trade_date"]).any()
        ):
            raise ValueError("Sandbox factor rows are invalid")
        from backend.shared.stock_utils import StockCodeUtil

        symbols = frame["symbol"]
        if symbols.isna().any() or any(
            StockCodeUtil.to_suffix(value) != value for value in symbols
        ):
            raise ValueError("Sandbox factor symbols are not canonical")
        dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        values = pd.to_numeric(frame["value"], errors="coerce")
        if (
            dates.isna().any()
            or values.isna().any()
            or not values.map(math.isfinite).all()
        ):
            raise ValueError("Sandbox factor values are invalid")
        return frame
    finally:
        try:
            owner.rmdir()
        except OSError:
            pass


def run_replay_operation(
    operation: str,
    *,
    tenant_id: str,
    user_id: str,
    window: dict,
    code: str | None = None,
    state: bytes | None = None,
    data: dict | None = None,
    symbols: list[str] | None = None,
    trade_date: str | None = None,
    cash: float | None = None,
    timeout_sec: int = 60,
) -> tuple[dict, bytes]:
    """Execute replay hooks in a fresh isolated container and return opaque state."""
    if operation not in {"prepare", "step"}:
        raise ValueError("Unknown replay operation")
    root = Path(os.getenv("STRATEGY_LAB_TEMP_DIR", "/app/db/lab_tmp"))
    owner = user_scoped_dir(root, tenant_id, user_id)
    owner.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    state_input = owner / f"replay-state-{run_id}.bin"
    if state is not None and len(state) > 8 * 1024 * 1024:
        raise ValueError("Replay session state exceeds 8 MiB")
    try:
        if state is not None:
            state_input.write_bytes(state)
            # Sandbox containers run as uid 65534; the input is transient and mounted
            # only into this one networkless, read-only operation.
            state_input.chmod(0o644)
        payload = {
            "_sandbox_mode": "replay",
            "run_id": run_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "operation": operation,
            "window": window,
            "code": code,
            "data": data,
            "symbols": symbols or [],
            "trade_date": trade_date,
            "cash": cash,
            "_replay_state_path": str(state_input) if state is not None else None,
        }
        try:
            rc, outputs, logs = _budgeted_call(payload, timeout_sec)
        except (ValueError, RuntimeError):
            raise
        except Exception as exc:
            raise RuntimeError("Replay sandbox is unavailable") from exc
        raw = outputs.get("result.json")
        if raw is None:
            raise RuntimeError(f"Isolated replay {operation} failed: {logs[-1024:]}")
        envelope = json.loads(raw)
        if envelope.get("run_id") != run_id:
            raise RuntimeError("Isolated replay result is invalid")
        if "error" in envelope:
            raise ValueError(str(envelope["error"])[:1000])
        next_state = outputs.get("session_state.bin")
        if (
            rc != 0
            or next_state is None
            or not isinstance(envelope.get("result"), dict)
        ):
            raise RuntimeError(f"Isolated replay {operation} failed: {logs[-1024:]}")
        return envelope["result"], next_state
    finally:
        state_input.unlink(missing_ok=True)
        try:
            owner.rmdir()
        except OSError:
            pass
