#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
补丁脚本：从 Qlib 官方成分股数据中批量回填数据库中的中证500标记 (idx_zz500)
"""

import os
import re
import sys
import argparse
import logging
from pathlib import Path
from urllib.parse import quote
from sqlalchemy import create_engine, text

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("patch_idx_zz500")

def project_root() -> Path:
    return Path(__file__).resolve().parents[3]

ROOT = project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def normalize_db_url(raw: str) -> str:
    url = raw.strip()
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    auth, host = rest.rsplit("@", 1)
    if ":" not in auth:
        return url
    user, password = auth.split(":", 1)
    return f"{scheme}://{user}:{quote(password, safe='')}@{host}"

def get_database_url(explicit_url: str | None = None) -> str:
    url = (explicit_url or os.getenv("DATABASE_URL", "")).strip()
    if url:
        return normalize_db_url(url)
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    dbname = os.getenv("DB_NAME", "quantmind")
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{dbname}"

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Patch idx_zz500 from csi500.txt")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
    parser.add_argument(
        "--csi500-path",
        default=str(project_root() / "db" / "qlib_data" / "instruments" / "csi500.txt"),
        help="Path to csi500.txt",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    return parser.parse_args()

def main():
    args = parse_args()
    db_url = get_database_url(args.database_url)
    csi500_file = Path(args.csi500_path)

    if not csi500_file.exists():
        logger.error(f"CSI500 file not found at: {csi500_file}")
        sys.exit(1)

    logger.info(f"Connecting to database...")
    engine = create_engine(db_url)

    # 1. 检查 stock_daily_latest 表中是否存在 idx_zz500 字段，若不存在则创建它
    logger.info("Verifying database schema for idx_zz500 column...")
    with engine.begin() as conn:
        res = conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'stock_daily_latest' AND column_name = 'idx_zz500'
        """)).fetchone()
        
        if not res:
            logger.info("Adding missing column 'idx_zz500' to stock_daily_latest...")
            conn.execute(text("ALTER TABLE stock_daily_latest ADD COLUMN idx_zz500 INTEGER DEFAULT 0"))
        else:
            logger.info("Column 'idx_zz500' already exists.")

    # 2. 如果是 dry-run，仅做结构和文件检查
    if args.dry_run:
        logger.info("Dry-run requested. Exiting without modifying data.")
        return

    # 3. 首先重置所有数据中的 idx_zz500 为 0
    logger.info("Resetting idx_zz500 to 0 for all stocks to ensure clean slate...")
    with engine.begin() as conn:
        reset_res = conn.execute(text("UPDATE stock_daily_latest SET idx_zz500 = 0"))
        logger.info(f"Reset {reset_res.rowcount} records successfully.")

    # 4. 解析 csi500.txt
    logger.info(f"Reading index constituents from: {csi500_file}")
    constituents = []
    with open(csi500_file, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 3:
                symbol = parts[0].strip()
                start_date = parts[1].strip()
                end_date = parts[2].strip()
                constituents.append({
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date
                })

    logger.info(f"Loaded {len(constituents)} membership intervals from file.")

    # 5. 批量更新
    logger.info("Starting batch update for CSI500 index members...")
    total_updated = 0
    batch_size = 50
    
    # 按照 symbol、日期段分批在事务中更新
    with engine.begin() as conn:
        for i in range(0, len(constituents), batch_size):
            chunk = constituents[i:i+batch_size]
            for item in chunk:
                up_res = conn.execute(
                    text("""
                        UPDATE stock_daily_latest 
                        SET idx_zz500 = 1 
                        WHERE symbol = :symbol 
                          AND trade_date >= :start_date 
                          AND trade_date <= :end_date
                    """),
                    item
                )
                total_updated += up_res.rowcount
            
            logger.info(f"Progress: Processed {min(i + batch_size, len(constituents))}/{len(constituents)} constituent intervals...")

    logger.info(f"✅ Successfully completed! Patched {total_updated} rows in stock_daily_latest table.")

if __name__ == "__main__":
    main()
