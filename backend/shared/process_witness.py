"""Linux process identity used to confirm execution ended, including PID reuse."""

import os
import socket
from pathlib import Path


def capture_process(pid=None):
    pid = int(pid or os.getpid())
    stat = Path(f"/proc/{pid}/stat").read_text()
    # comm can contain spaces and parentheses; fields after its last ')' are fixed.
    fields = stat[stat.rindex(")") + 2 :].split()
    return {
        "host": socket.gethostname(),
        "pid": pid,
        "start_ticks": fields[19],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid"),
    }


def process_has_stopped(witness):
    """Fail closed for another host/namespace or inaccessible process metadata."""
    if not isinstance(witness, dict):
        return False
    try:
        local = capture_process()
        for key in ("host", "boot_id", "pid_namespace"):
            if witness.get(key) != local[key]:
                return False
        pid = int(witness["pid"])
        if pid <= 0:
            return False
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return True
        fields = stat[stat.rindex(")") + 2 :].split()
        return fields[0] == "Z" or fields[19] != witness["start_ticks"]
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return False
