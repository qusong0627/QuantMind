#!/usr/bin/env python3
"""删除 db/ 下所有 ._ 开头的 macOS 资源分支文件"""
from pathlib import Path

root = Path(__file__).resolve().parent.parent / "db"
deleted = 0
errors = 0

for f in root.rglob("._*"):
    if not f.is_file():
        continue
    try:
        f.unlink()
        deleted += 1
        if deleted % 2000 == 0:
            print(f"  已删除 {deleted} 个文件...")
    except OSError as e:
        errors += 1
        print(f"  删除失败: {f} ({e})")

print(f"完成: 删除 {deleted} 个文件, {errors} 个失败")
