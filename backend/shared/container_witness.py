"""Read-only Docker evidence that the exact old PID namespace has ended."""

import json
import re

from backend.shared.process_witness import capture_process

_SERVICES = {
    "engine",
    "celery-worker",
    "celery-worker-backtest",
    "celery-worker-heavy",
    "celery-worker-legacy",
    "celery-worker-rdagent",
}
_READ_NAMESPACE = [
    "python",
    "-I",
    "-S",
    "-c",
    "import json,os,pathlib; s=pathlib.Path('/proc/1/stat').read_text(); "
    "print(json.dumps({'pid_namespace':os.readlink('/proc/self/ns/pid'),"
    "'init_start_ticks':s[s.rindex(')')+2:].split()[19],"
    "'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
]


def container_namespace_has_stopped(client, witness):
    """Unknown/deleted containers or foreign hosts are not proof of termination.

    A live restarted container has a different PID namespace. The trusted fixed
    command inspects only namespace/boot IDs, never environment or user files.
    """
    if not isinstance(witness, dict):
        return False
    try:
        host = witness.get("host", "")
        if not isinstance(host, str) or not re.fullmatch(r"[0-9a-f]{12,64}", host):
            return False
        if not re.fullmatch(r"pid:\[\d+\]", str(witness.get("pid_namespace", ""))):
            return False
        if witness.get("boot_id") != capture_process()["boot_id"]:
            return False
        container = client.containers.get(host)
        container.reload()
        config = container.attrs.get("Config", {})
        if not container.id.startswith(host) or config.get("Hostname") != host:
            return False
        if config.get("Labels", {}).get("com.docker.compose.service") not in _SERVICES:
            return False
        state = container.attrs.get("State", {})
        if state.get("Status") in {"exited", "dead"} and not state.get("Running"):
            return True
        if not state.get("Running"):
            return False
        result = container.exec_run(_READ_NAMESPACE)
        if result.exit_code != 0 or len(result.output) > 4096:
            return False
        observed = json.loads(result.output)
        return (
            observed.get("boot_id") == witness["boot_id"]
            and isinstance(observed.get("pid_namespace"), str)
            and observed["pid_namespace"].startswith("pid:[")
            and (
                observed["pid_namespace"] != witness["pid_namespace"]
                # Linux may reuse a namespace inode immediately after restart.
                # A new init started after the recorded process proves that
                # the recorded execution belonged to the previous incarnation.
                or int(observed["init_start_ticks"]) > int(witness["start_ticks"])
            )
        )
    except Exception:
        return False
