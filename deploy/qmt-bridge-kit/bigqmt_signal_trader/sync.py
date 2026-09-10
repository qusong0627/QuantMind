# coding: utf-8
"""Push this client's package into the QMT python directory.

Deploying is a file copy, and until now it was a manual one -- which is how a
fix gets confirmed working locally and then debugged for an hour because it was
never actually on the terminal.

The copy runs *here*, on the client, not inside QMT. The bridge only answers
where it lives (``get_deployment_info``). A trading process that rewrites its
own code while running would put whatever is in the source tree -- including a
half-finished edit -- straight into the live terminal.

Two things this deliberately does not do:

* **Config files are never written.** ``bigqmt_signal_trader_local_config.py``
  and ``bigqmt_signal_trader_client_config.py`` hold the account id and
  credentials. Their ``.example.py`` counterparts are documentation and do get
  synced.
* **No new top-level files.** Only modules the deployment already has are
  refreshed, plus the package tree. Pushing files a deployment never had is how
  you end up with a QMT directory nobody can reason about.

A copy alone changes nothing until the strategy is restarted: QMT keeps modules
in sys.modules across re-runs. Every result says so.
"""

import os
import shutil
import time


# Real config files: account id and credentials live here.
NEVER_OVERWRITE = (
    "bigqmt_signal_trader_local_config.py",
    "bigqmt_signal_trader_client_config.py",
)

PACKAGE_DIRS = ("bigqmt_signal_trader", "xtquant")

STRATEGY_ENTRY = "bigqmt_signal_trader_strategy.py"


def _source_root():
    """Directory holding the package, i.e. what would be on sys.path."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _same_bytes(source, target):
    try:
        with open(source, "rb") as left, open(target, "rb") as right:
            return left.read() == right.read()
    except Exception:
        return False


def _planned_pairs(source_root, target_root):
    """(source, target) for everything eligible to be refreshed."""
    pairs = []
    for name in sorted(os.listdir(source_root)):
        if not name.endswith(".py") or name in NEVER_OVERWRITE:
            continue
        target = os.path.join(target_root, name)
        # Only top-level modules the deployment already has, plus the entry.
        if os.path.exists(target) or name == STRATEGY_ENTRY:
            pairs.append((os.path.join(source_root, name), target))
    for package in PACKAGE_DIRS:
        package_dir = os.path.join(source_root, package)
        if not os.path.isdir(package_dir):
            continue
        for directory, subdirs, files in os.walk(package_dir):
            subdirs[:] = [d for d in subdirs if d != "__pycache__"]
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                source = os.path.join(directory, name)
                relative = os.path.relpath(source, source_root)
                pairs.append((source, os.path.join(target_root, relative)))
    return pairs


def _copy(source, target, stamp):
    """Back up, then replace atomically."""
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    if os.path.exists(target):
        shutil.copy2(target, "%s.bak_%s" % (target, stamp))
    temporary = "%s.tmp_%s" % (target, stamp)
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def sync_deployment(qmt_python_dir, source_root=None, dry_run=False):
    """Refresh the QMT deployment from this client's package.

    Returns ``{"updated": [...], "skipped_config": [...], "identical": n,
    "target": ..., "source": ..., "restart_required": bool}``.
    """
    source_root = source_root or _source_root()
    target_root = str(qmt_python_dir or "")
    result = {
        "source": source_root,
        "target": target_root,
        "updated": [],
        "skipped_config": [c for c in NEVER_OVERWRITE
                           if os.path.exists(os.path.join(target_root, c))],
        "identical": 0,
        "restart_required": False,
    }
    if not target_root or not os.path.isdir(target_root):
        result["error"] = "target directory not found: %s" % target_root
        return result

    stamp = time.strftime("%Y%m%d_%H%M%S")
    for source, target in _planned_pairs(source_root, target_root):
        if os.path.exists(target) and _same_bytes(source, target):
            result["identical"] += 1
            continue
        relative = os.path.relpath(target, target_root)
        if not dry_run:
            try:
                _copy(source, target, stamp)
            except Exception as exc:
                result.setdefault("failed", []).append(
                    "%s: %s" % (relative, exc))
                continue
        result["updated"].append(relative)

    result["restart_required"] = bool(result["updated"])
    if not dry_run and result["updated"]:
        result["backup_suffix"] = ".bak_%s" % stamp
    return result
