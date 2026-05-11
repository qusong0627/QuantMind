#!/usr/bin/env python3
"""删除 db/ 下所有 ._ 开头的 macOS 资源分支文件"""

import argparse
import logging
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent / "db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("CleanupDotfiles")


def collect_dotfiles() -> list[Path]:
    files = []
    for f in ROOT_DIR.rglob("._*"):
        if f.is_file():
            files.append(f)
    return sorted(files)


def cmd_dry_run() -> int:
    files = collect_dotfiles()
    logger.info("[DRY-RUN] 将删除 %d 个 ._* 文件", len(files))
    for f in files[:20]:
        logger.info("  %s", f)
    if len(files) > 20:
        logger.info("  ... 还有 %d 个", len(files) - 20)
    return 0


def cmd_run() -> int:
    files = collect_dotfiles()
    if not files:
        logger.info("未找到 ._* 文件，无需清理")
        return 0

    logger.info("找到 %d 个 ._* 文件", len(files))
    resp = input(f"确认删除这 {len(files)} 个文件? [y/N]: ")
    if resp.lower() != "y":
        logger.info("已取消")
        return 0

    deleted = 0
    errors = 0
    for f in files:
        try:
            f.unlink()
            deleted += 1
            if deleted % 2000 == 0:
                logger.info("已删除 %d 个文件...", deleted)
        except OSError as e:
            errors += 1
            logger.warning("删除失败: %s (%s)", f, e)

    logger.info("完成: 删除 %d 个文件, %d 个失败", deleted, errors)
    return 0 if errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 db/ 目录下的 macOS 资源分支文件 (._*)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅列出将要删除的文件，不实际删除",
    )
    args = parser.parse_args()

    if args.dry_run:
        return cmd_dry_run()
    return cmd_run()


if __name__ == "__main__":
    raise SystemExit(main())
