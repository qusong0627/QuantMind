#coding:gbk
"""QMT editor entry for the same-host ZeroMQ bridge.

The original editor entry keeps its historical Redis-oriented filename for
backward compatibility even though it supports every transport.  This wrapper
gives ZMQ deployments an unambiguous entry, forces the ZMQ transport, and
persists bootstrap exceptions that occur before the normal bridge logger is
available.
"""
import datetime
import os
import sys
import traceback


def _find_bridge_entry():
    candidates = []
    entry_file = globals().get("__file__")
    if entry_file:
        candidates.append(os.path.dirname(os.path.abspath(entry_file)))
    cwd = os.getcwd()
    if cwd and cwd not in candidates:
        candidates.append(cwd)
    for path in sys.path:
        if path and path not in candidates:
            candidates.append(path)
    for directory in candidates:
        entry = os.path.join(directory, "BIGQMT_REDIS_DRYRUN.py")
        if os.path.isfile(entry):
            return entry
    raise ImportError("BIGQMT_REDIS_DRYRUN.py was not found beside the ZMQ entry or on sys.path")


def _write_bootstrap_error(entry_path):
    try:
        log_dir = os.path.join(os.path.dirname(entry_path), "logs")
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir)
        log_path = os.path.join(log_dir, "bigqmt-bootstrap-error.log")
        with open(log_path, "a") as log_file:
            log_file.write("\n%s BIGQMT_ZMQ_DRYRUN bootstrap failed\n" % datetime.datetime.now().isoformat())
            traceback.print_exc(file=log_file)
        print("[bigqmt_shell] bootstrap traceback written to %s" % log_path)
    except Exception as log_error:
        print("[bigqmt_shell] bootstrap traceback could not be written: %s" % log_error)


BIGQMT_FORCE_TRANSPORT = "zmq"
try:
    _BRIDGE_ENTRY = _find_bridge_entry()
    with open(_BRIDGE_ENTRY, "rb") as source_file:
        _BRIDGE_SOURCE = source_file.read()
    exec(compile(_BRIDGE_SOURCE, _BRIDGE_ENTRY, "exec"), globals(), globals())
except Exception:
    _bootstrap_log_anchor = globals().get("_BRIDGE_ENTRY") or globals().get("__file__") or os.path.join(os.getcwd(), "BIGQMT_ZMQ_DRYRUN.py")
    _write_bootstrap_error(_bootstrap_log_anchor)
    raise
