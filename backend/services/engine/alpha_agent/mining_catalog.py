"""Public CFS metadata on the gateway; never starts an execution machine."""

from pathlib import Path
import os

from backend.shared.mining_cfs import shared_data_facts


def dataset_catalog(root: Path | None) -> dict:
    import pyarrow.parquet as pq
    from backend.services.engine.rd_agent.mining_datasets import (
        DATASET_LABELS,
        DATASET_REL_PATHS,
        SUPPORTED_DATASETS,
    )
    from backend.shared.user_code_sandbox import checked_input

    states = {}
    for key in SUPPORTED_DATASETS:
        try:
            if root is None:
                raise ValueError("Public dataset root is unavailable")
            directory = checked_input(root / DATASET_REL_PATHS[key], root)
            names = sorted(
                name
                for name in os.listdir(directory)
                if name.startswith("dt=") and len(name) == 11 and name[3:].isdigit()
            )
            parent = checked_input(directory / names[-1], root) if names else directory
            file = next(iter(parent.glob("*.parquet")), None)
            if file is None:
                raise ValueError("No Parquet data")
            schema = pq.read_schema(checked_input(file, root))
            states[key] = {"available": True, "column_count": len(schema)}
        except Exception:
            states[key] = {
                "available": False,
                "availability_reason": "数据未同步、不可读或无可用字段",
            }
    return {
        "code": 200,
        "data": {
            "datasets": [
                {
                    "key": key,
                    "name": DATASET_LABELS[key],
                    "markets": ["a_share"],
                    "freqs": ["day"],
                    **states[key],
                }
                for key in SUPPORTED_DATASETS
            ]
        },
    }


def gateway_snapshot(*, data_source, datasets, universe, tenant_id, user_id):
    # OSS-ADAPT: OSS 单容器不支持远端挖掘节点(/mining-cfs 共享快照)，网关快照不可用。
    raise RuntimeError("OSS 单容器不支持远端挖掘节点")


def gateway_catalog(name: str) -> dict:
    shared_data_facts(required=True, mount_root="/mining-cfs")
    root = Path("/mining-cfs/quantdb")
    if name == "datasets":
        return dataset_catalog(root)
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    # 覆盖率/因子分类一律取本地主库（QM_QUANTDB_DATA_DIR）。
    # /mining-cfs/quantdb 只是给挖掘机器共享的副本，同步进度独立且常滞后于主库；
    # 过去用它算 data-summary 再把 data_dir 改写成 "/data/quantdb"，等于让副本
    # 数据冒充主库口径，下游（推理基准日、数据新鲜度判断）会把可用数据日期
    # 误判成副本最新日（2026-10-30 实测：副本 10-23 vs 主库 10-29）。
    # 副本自身的覆盖单独挂 mining_shared 块，供挖掘场景判读，不再覆盖主口径。
    hub = QuantDBDataHub.get_instance()
    if name == "data-summary":
        data = hub.get_data_summary()
        data["data_dir"] = str(hub.data_dir)
        try:
            shared = QuantDBDataHub(data_dir=root).get_data_summary()
            data["mining_shared"] = {
                "data_dir": str(root),
                "date_range": shared.get("date_range") or {},
                "stock_count": shared.get("stock_count"),
            }
        except Exception as exc:  # noqa: BLE001
            data["mining_shared"] = {"data_dir": str(root), "error": str(exc)[:200]}
    elif name == "factor-categories":
        data = hub.fetch_l1_factor_categories()
    else:
        raise ValueError("Unknown mining catalog")
    return {"code": 200, "data": data}
