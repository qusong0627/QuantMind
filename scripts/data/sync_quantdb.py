#!/usr/bin/env python3
"""
QuantDB 全量数据同步工具
使用 quantdb-sdk 的 sync_dataset 增量同步所有数据到本地 NAS。

数据布局:
  /media/zbox/nas-NSF/QUANTDB/
  ├── 1_kline_data/          # K线数据
  ├── 2_base_sector/         # 基础/板块数据
  ├── 3_financial_data/      # 财务数据
  ├── 5_technical_derived/   # 技术衍生数据
  └── 6_ml_datasets/         # ML数据集

用法:
  python3 scripts/data/sync_quantdb.py                    # 同步所有数据集
  python3 scripts/data/sync_quantdb.py --only v2          # 仅同步 V2 数据集
  python3 scripts/data/sync_quantdb.py --only v1          # 仅同步 V1 数据集
  python3 scripts/data/sync_quantdb.py --dataset valuation # 同步指定数据集
  python3 scripts/data/sync_quantdb.py --dry-run          # 仅检查，不下载
  python3 scripts/data/sync_quantdb.py --status           # 查看同步状态
"""

import argparse
import os
import sys
import time
import json
import logging
from pathlib import Path
from datetime import datetime

from quantdb_sdk import QuantDBClient

# ─── 配置 ────────────────────────────────────────────────────────────────

SAVE_DIR = os.environ.get("QUANTDB_SAVE_DIR", "/media/zbox/nas-NSF/QUANTDB")
API_KEY = os.environ.get("QUANTDB_API_KEY", "").strip()
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds

# V2 数据集: 使用 sync_dataset 增量同步 (按交易日分区)
V2_DATASETS = [
    {"category_id": "1", "sub_category": "daily_unadjusted",  "dir": "1_kline_data"},
    {"category_id": "1", "sub_category": "daily_forward",     "dir": "1_kline_data"},
    {"category_id": "1", "sub_category": "daily_backward",    "dir": "1_kline_data"},
    {"category_id": "1", "sub_category": "index_daily",       "dir": "1_kline_data"},
    {"category_id": "2", "sub_category": "margin_trading",    "dir": "2_base_sector"},
    {"category_id": "5", "sub_category": "valuation",         "dir": "5_technical_derived"},
    {"category_id": "5", "sub_category": "technical_indicators", "dir": "5_technical_derived"},
    {"category_id": "5", "sub_category": "market_sentiment",  "dir": "5_technical_derived"},
    {"category_id": "6", "sub_category": "features_daily",    "dir": "6_ml_datasets"},
    {"category_id": "6", "sub_category": "l1_factors",        "dir": "6_ml_datasets"},
    {"category_id": "6", "sub_category": "factor_dictionary", "dir": "6_ml_datasets"},
]

# V1 数据集: 使用 sync_dataset (V1 Manifest + ETag 增量)
V1_DATASETS = [
    {"category_id": "1", "sub_category": "min1_kline",        "dir": "1_kline_data"},
    {"category_id": "1", "sub_category": "min5_kline",        "dir": "1_kline_data"},
    {"category_id": "2", "sub_category": "sector_concept",    "dir": "2_base_sector"},
    {"category_id": "2", "sub_category": "instrument_detail", "dir": "2_base_sector"},
    {"category_id": "2", "sub_category": "index_weights",     "dir": "2_base_sector"},
    {"category_id": "2", "sub_category": "trading_calendar",  "dir": "2_base_sector"},
    {"category_id": "3", "sub_category": "balance",           "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "income",            "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "cashflow",          "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "capital",           "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "pershare_index",    "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "dividend_factors",  "dir": "3_financial_data"},
    {"category_id": "3", "sub_category": "holder_num",        "dir": "3_financial_data"},
]

# ─── 日志 ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(SAVE_DIR, "sync.log"), mode="a", encoding="utf-8"
        ),
    ],
)
log = logging.getLogger("quantdb-sync")


# ─── 工具函数 ────────────────────────────────────────────────────────────

def count_local_files(sub_category: str, parent_dir: str) -> int:
    local_path = Path(SAVE_DIR) / parent_dir / sub_category
    if not local_path.exists():
        return 0
    return sum(1 for _ in local_path.rglob("*.parquet"))


def get_remote_count(client: QuantDBClient, category_id: str, sub_category: str) -> int:
    try:
        manifest = client.query_manifest(category_id=category_id, sub_category=sub_category)
        return len(manifest)
    except Exception as e:
        log.warning(f"获取 manifest 失败 {category_id}/{sub_category}: {e}")
        return -1


def format_size(gb: float) -> str:
    if gb < 1:
        return f"{gb * 1024:.1f} MB"
    return f"{gb:.2f} GB"


# ─── 同步逻辑 ────────────────────────────────────────────────────────────

def make_client() -> QuantDBClient:
    """创建配置了更长超时和更多重试的 SDK 客户端"""
    if not API_KEY:
        raise RuntimeError("未设置 QUANTDB_API_KEY 环境变量")
    return QuantDBClient(
        api_key=API_KEY,
        timeout=(15, 300),  # 连接超时15s，读取超时300s
        max_retries=5,      # 更多重试
    )


def sync_single_dataset(
    client: QuantDBClient,
    dataset: dict,
    dry_run: bool = False,
) -> dict:
    """同步单个数据集，带重试"""
    cat_id = dataset["category_id"]
    sub = dataset["sub_category"]
    parent = dataset["dir"]
    save_path = SAVE_DIR

    local_count = count_local_files(sub, parent)
    remote_count = get_remote_count(client, cat_id, sub)

    result = {
        "dataset": f"{cat_id}/{sub}",
        "local": local_count,
        "remote": remote_count,
        "status": "skip",
        "downloaded": 0,
        "error": None,
    }

    if remote_count <= 0:
        result["status"] = "error"
        result["error"] = "无法获取远端文件数"
        return result

    if local_count >= remote_count:
        result["status"] = "up_to_date"
        log.info(f"[OK] {sub}: {local_count}/{remote_count} 已是最新")
        return result

    missing = remote_count - local_count
    log.info(f"[SYNC] {sub}: {local_count}/{remote_count} (缺 {missing})")

    if dry_run:
        result["status"] = "dry_run"
        return result

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sync_result = client.sync_dataset(dataset=sub, save_dir=save_path)
            downloaded = len(sync_result.get("downloaded", []))
            layout = sync_result.get("layout", "unknown")
            result["status"] = "synced"
            result["downloaded"] = downloaded
            result["layout"] = layout
            log.info(f"[DONE] {sub}: 下载 {downloaded} 个文件 (layout={layout})")
            return result
        except Exception as e:
            if attempt < MAX_RETRIES:
                log.warning(f"[RETRY] {sub}: 第 {attempt} 次失败 ({e}), {RETRY_DELAY}s 后重试...")
                time.sleep(RETRY_DELAY)
                # 重新创建 client 避免连接问题
                client = make_client()
            else:
                result["status"] = "error"
                result["error"] = str(e)
                log.error(f"[FAIL] {sub}: {MAX_RETRIES} 次重试后仍失败: {e}")

    return result


def show_status(client: QuantDBClient) -> None:
    all_datasets = V2_DATASETS + V1_DATASETS

    usage = client.get_usage()
    log.info("=" * 70)
    log.info("QuantDB 同步状态报告")
    log.info("=" * 70)
    log.info(f"账户: {usage.get('used_gb', 0):.2f} GB / {usage.get('limit_gb', 0):.1f} GB "
             f"(剩余 {usage.get('remaining_gb', 0):.2f} GB)")
    log.info(f"订阅: {usage.get('subscription', {}).get('plan_id', 'N/A')} "
             f"({usage.get('subscription', {}).get('status', 'N/A')})")
    log.info("-" * 70)

    total_local = 0
    total_remote = 0
    missing_datasets = []

    for ds in all_datasets:
        cat_id = ds["category_id"]
        sub = ds["sub_category"]
        parent = ds["dir"]
        local = count_local_files(sub, parent)
        remote = get_remote_count(client, cat_id, sub)
        total_local += local
        total_remote += max(remote, 0)

        if remote < 0:
            status_str = "ERROR"
        elif local >= remote:
            status_str = "OK"
        else:
            status_str = f"缺 {remote - local}"
            missing_datasets.append(f"{parent}/{sub}")

        log.info(f"  {parent}/{sub:25s}  {local:>5} / {remote:>5}  [{status_str}]")

    log.info("-" * 70)
    log.info(f"总计: {total_local} / {total_remote} 文件")
    if missing_datasets:
        log.info(f"需要同步的数据集: {', '.join(missing_datasets)}")
    else:
        log.info("所有数据集已是最新!")
    log.info("=" * 70)


# ─── 主入口 ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="QuantDB 全量数据同步工具")
    parser.add_argument("--only", choices=["v1", "v2"], help="仅同步 V1 或 V2 数据集")
    parser.add_argument("--dataset", type=str, help="仅同步指定 sub_category")
    parser.add_argument("--dry-run", action="store_true", help="仅检查，不下载")
    parser.add_argument("--status", action="store_true", help="查看同步状态")
    args = parser.parse_args()

    client = make_client()

    if args.status:
        show_status(client)
        return

    # 确定要同步的数据集
    if args.dataset:
        all_ds = V2_DATASETS + V1_DATASETS
        datasets = [ds for ds in all_ds if ds["sub_category"] == args.dataset]
        if not datasets:
            log.error(f"未找到数据集: {args.dataset}")
            sys.exit(1)
    elif args.only == "v2":
        datasets = V2_DATASETS
    elif args.only == "v1":
        datasets = V1_DATASETS
    else:
        datasets = V2_DATASETS + V1_DATASETS

    # 过滤掉已最新的数据集
    need_sync = []
    for ds in datasets:
        local = count_local_files(ds["sub_category"], ds["dir"])
        remote = get_remote_count(client, ds["category_id"], ds["sub_category"])
        if remote > 0 and local < remote:
            need_sync.append(ds)
        else:
            log.info(f"[SKIP] {ds['sub_category']}: 已是最新 ({local}/{remote})")

    if not need_sync:
        log.info("所有数据集已是最新，无需同步!")
        return

    # 按优先级排序: 小数据集先同步，大数据集后同步
    need_sync.sort(key=lambda ds: get_remote_count(client, ds["category_id"], ds["sub_category"]))

    log.info(f"需要同步 {len(need_sync)} 个数据集 (dry_run={args.dry_run})")

    usage = client.get_usage()
    log.info(f"当前流量: {format_size(usage['used_gb'])} / {format_size(usage['limit_gb'])} "
             f"(剩余 {format_size(usage['remaining_gb'])})")

    start_time = time.time()
    results = []

    # 串行同步: 避免同一 save_dir 下 SQLite 锁冲突 (NFS 不支持文件锁)
    for i, ds in enumerate(need_sync, 1):
        log.info(f"--- [{i}/{len(need_sync)}] 同步 {ds['sub_category']} ---")
        r = sync_single_dataset(client, ds, args.dry_run)
        results.append(r)
        # 每个数据集完成后重新创建 client，释放连接
        client = make_client()

    elapsed = time.time() - start_time

    # 汇总
    log.info("=" * 70)
    log.info("同步完成汇总")
    log.info("=" * 70)

    synced = sum(1 for r in results if r["status"] == "synced")
    up_to_date = sum(1 for r in results if r["status"] == "up_to_date")
    errors = sum(1 for r in results if r["status"] == "error")
    dry_runs = sum(1 for r in results if r["status"] == "dry_run")
    total_downloaded = sum(r.get("downloaded", 0) for r in results)

    log.info(f"已同步: {synced}  已最新: {up_to_date}  错误: {errors}  "
             f"仅检查: {dry_runs}  下载文件: {total_downloaded}")
    log.info(f"耗时: {elapsed:.1f}s")

    if errors:
        log.error("失败的数据集:")
        for r in results:
            if r["status"] == "error":
                log.error(f"  {r['dataset']}: {r['error']}")

    usage = client.get_usage()
    log.info(f"剩余流量: {format_size(usage['remaining_gb'])}")
    log.info("=" * 70)

    sync_record = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "results": results,
        "remaining_gb": round(usage["remaining_gb"], 2),
    }
    record_path = os.path.join(SAVE_DIR, "sync_history.jsonl")
    with open(record_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(sync_record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
