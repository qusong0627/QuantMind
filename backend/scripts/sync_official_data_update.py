#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import pandas as pd
from dotenv import load_dotenv


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read().decode("utf-8")
    payload = json.loads(data)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    if isinstance(payload, dict):
        return payload
    raise RuntimeError("Invalid JSON payload from official data update API")


def _download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=300) as resp, target.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _sync_dir(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    copied = 0
    for file in src.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, out)
        copied += 1
    return copied


def _to_python_value(v: Any) -> Any:
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            return v
    return v


async def _upsert_stock_daily_latest(parquet_path: Path) -> int:
    if not parquet_path.exists():
        return 0

    db_host = os.getenv("DB_HOST", "127.0.0.1")
    db_port = int(os.getenv("DB_PORT", "5432"))
    db_name = os.getenv("DB_NAME", "quantmind")
    db_user = os.getenv("DB_USER", "quantmind")
    db_password = os.getenv("DB_PASSWORD", "quantmind2026")

    df = pd.read_parquet(parquet_path)
    if df.empty:
        return 0
    if "trade_date" not in df.columns or "symbol" not in df.columns:
        raise RuntimeError("stock_daily_latest parquet 缺少主键字段 trade_date/symbol")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
    df = df.dropna(subset=["trade_date", "symbol"])
    if df.empty:
        return 0

    conn = await asyncpg.connect(
        host=db_host,
        port=db_port,
        database=db_name,
        user=db_user,
        password=db_password,
    )
    try:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='stock_daily_latest'
            ORDER BY ordinal_position
            """
        )
        table_cols = [r["column_name"] for r in rows]
        use_cols = [c for c in df.columns.tolist() if c in table_cols]
        if "trade_date" not in use_cols or "symbol" not in use_cols:
            raise RuntimeError("目标表缺少主键列 trade_date/symbol")

        records = []
        for row in df[use_cols].itertuples(index=False, name=None):
            records.append(tuple(_to_python_value(v) for v in row))

        temp_table = f"tmp_stock_daily_latest_{int(datetime.now().timestamp())}"
        async with conn.transaction():
            await conn.execute(
                f"CREATE TEMP TABLE {temp_table} AS SELECT * FROM stock_daily_latest WITH NO DATA"
            )
            await conn.copy_records_to_table(temp_table, records=records, columns=use_cols)

            insert_cols_sql = ", ".join(use_cols)
            select_cols_sql = ", ".join(use_cols)
            non_pk = [c for c in use_cols if c not in ("trade_date", "symbol")]
            if non_pk:
                update_set_sql = ", ".join([f"{c}=EXCLUDED.{c}" for c in non_pk])
                upsert_sql = (
                    f"INSERT INTO stock_daily_latest ({insert_cols_sql}) "
                    f"SELECT {select_cols_sql} FROM {temp_table} "
                    f"ON CONFLICT (trade_date, symbol) DO UPDATE SET {update_set_sql}"
                )
            else:
                upsert_sql = (
                    f"INSERT INTO stock_daily_latest ({insert_cols_sql}) "
                    f"SELECT {select_cols_sql} FROM {temp_table} "
                    "ON CONFLICT (trade_date, symbol) DO NOTHING"
                )
            await conn.execute(upsert_sql)
        return len(records)
    finally:
        await conn.close()


def _extract_bundle(bundle_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["tar", "--zstd", "-xf", str(bundle_path), "-C", str(target_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"解压失败: {proc.stderr or proc.stdout}")


def main() -> int:
    parser = argparse.ArgumentParser(description="拉取并应用官方增量数据包")
    parser.add_argument("--api-base-url", required=True)
    parser.add_argument("--access-key", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--version", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env")

    api_base = args.api_base_url.strip().rstrip("/")
    endpoint = (
        f"{api_base}/data-updates/{args.version.strip()}"
        if args.version and args.version.strip()
        else f"{api_base}/data-updates/latest"
    )
    headers = {
        "X-Access-Key": args.access_key.strip(),
        "X-Secret-Key": args.secret_key.strip(),
    }

    payload = _http_get_json(endpoint, headers=headers)
    version = str(payload.get("version") or args.version or "unknown")
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    bundle_entry = None
    manifest_entry = None
    for f in files:
        if not isinstance(f, dict):
            continue
        if f.get("kind") == "bundle" and f.get("url"):
            bundle_entry = f
        if f.get("kind") == "manifest" and f.get("url"):
            manifest_entry = f
    if bundle_entry is None:
        raise RuntimeError("官方响应中缺少 bundle 文件")

    download_dir = project_root / "tmp" / "official_data_updates" / version / "download"
    extract_dir = project_root / "tmp" / "official_data_updates" / version / "extract"
    bundle_name = str(bundle_entry.get("name") or f"update_{version}.tar.zst")
    bundle_path = download_dir / bundle_name

    _download_file(str(bundle_entry["url"]), bundle_path)
    expected_sha = str(bundle_entry.get("sha256") or "").strip().lower()
    if expected_sha:
        actual_sha = _sha256_file(bundle_path)
        if actual_sha != expected_sha:
            raise RuntimeError("bundle sha256 校验失败")

    if manifest_entry and manifest_entry.get("url"):
        manifest_path = download_dir / "manifest.parquet"
        _download_file(str(manifest_entry["url"]), manifest_path)

    result: dict[str, Any] = {
        "success": True,
        "version": version,
        "bundle_size": bundle_path.stat().st_size,
        "dry_run": bool(args.dry_run),
        "applied": {
            "feature_files": 0,
            "qlib_files": 0,
            "docs_files": 0,
            "stock_daily_latest_rows": 0,
        },
    }

    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    _extract_bundle(bundle_path, extract_dir)

    feature_src = extract_dir / "db" / "feature_snapshots"
    qlib_src = extract_dir / "db" / "qlib_data"
    docs_src = extract_dir / "docs"
    db_deltas = extract_dir / "db_deltas"

    result["applied"]["feature_files"] = _sync_dir(
        feature_src,
        project_root / "db" / "feature_snapshots",
    )
    result["applied"]["qlib_files"] = _sync_dir(
        qlib_src,
        project_root / "db" / "qlib_data",
    )
    result["applied"]["docs_files"] = _sync_dir(
        docs_src,
        project_root / "docs",
    )

    parquet_candidates = sorted(db_deltas.glob("stock_daily_latest*.parquet"))
    if parquet_candidates:
        updated_rows = asyncio.run(_upsert_stock_daily_latest(parquet_candidates[0]))
        result["applied"]["stock_daily_latest_rows"] = updated_rows

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTPError: {exc.code} {exc.reason}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
