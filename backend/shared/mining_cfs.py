"""Prove that mining reads the same NFS export as CPU training, read only."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _unescape(value: str) -> str:
    for escaped, literal in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
        value = value.replace(escaped, literal)
    return value


def shared_data_facts(
    *, required: bool = False, mount_root: str = "/data"
) -> dict | None:
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点，/mining-cfs 网关挂载不可用。
    if mount_root == "/mining-cfs":
        raise RuntimeError("OSS 单容器不支持远端挖掘节点")
    if mount_root not in {"/data", "/mining-cfs"}:
        raise ValueError("Unknown shared mining mount root")
    gateway = mount_root == "/mining-cfs"
    if not gateway and os.getenv("QM_MINING_SHARED_CFS") != "1":
        if required:
            raise ValueError("Dedicated mining requires shared CFS data")
        return None
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/cfs_mounts.json").read_text()
    )
    export = next(m["source"] for m in config["mounts"] if m["name"] == "data")
    if not gateway and os.getenv("QM_QUANTDB_DATA_DIR") != "/data/quantdb":
        raise ValueError("Shared mining QuantDB path must be /data/quantdb")
    if not gateway and os.getenv("QLIB_PROVIDER_URI") != "/data/qlib/cn_data":
        raise ValueError("Shared mining Qlib path must be /data/qlib/cn_data")
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, filesystem = left.split(), right.split()
        mounts.append(
            (_unescape(fields[4]), _unescape(fields[3]), fields[5], filesystem)
        )
    result = {"kind": "cfs"}
    for name in ("quantdb", "qlib"):
        target = mount_root + "/" + name
        candidates = [m for m in mounts if m[0] == target]
        if len(candidates) != 1:
            raise ValueError(f"Missing dedicated CFS bind: {target}")
        _, root, options, filesystem = candidates[0]
        source = _unescape(filesystem[1]).rstrip("/") + root.rstrip("/")
        expected = export.rstrip("/") + "/" + name
        if (
            filesystem[0] not in {"nfs", "nfs4"}
            or "ro" not in options.split(",")
            or source != expected
        ):
            raise ValueError(f"CFS source/read-only verification failed: {target}")
        result[name] = {"source": source, "read_only": True}
    return result
