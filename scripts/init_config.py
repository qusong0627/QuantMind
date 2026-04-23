#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QuantMind 配置初始化脚本
用于初始化 Qlib 配置和系统必要的配置文件
"""

import os
import sys
from pathlib import Path


def init_qlib_config():
    """初始化 Qlib 配置"""
    try:
        import qlib
        from qlib.config import QlibConfig
    except ImportError:
        print("❌ Qlib 未安装，请先运行: pip install pyqlib")
        return False

    # 确定 qlib 数据目录
    project_root = Path(__file__).parent.parent
    qlib_data_dir = project_root / "db"

    if not qlib_data_dir.exists():
        print(f"⚠️  Qlib 数据目录不存在: {qlib_data_dir}")
        print("请先下载数据包并解压到 db 目录")
        return False

    # 初始化 Qlib
    print(f"正在初始化 Qlib，数据目录: {qlib_data_dir}")
    try:
        qlib.init(str(qlib_data_dir), region="cn")
        print("✅ Qlib 初始化成功")
        return True
    except Exception as e:
        print(f"❌ Qlib 初始化失败: {e}")
        return False


def init_env_file():
    """检查并提示 .env 文件配置"""
    project_root = Path(__file__).parent.parent
    env_file = project_root / ".env"

    if env_file.exists():
        print(f"✅ .env 文件已存在: {env_file}")
        return True

    print(f"⚠️  .env 文件不存在，请参考 .env.example 创建配置文件")
    return False


def main():
    """主函数"""
    print("=" * 50)
    print("QuantMind 配置初始化")
    print("=" * 50)
    print()

    results = []

    # 1. 检查 .env 文件
    print("[1/2] 检查环境配置...")
    results.append((".env 配置", init_env_file()))
    print()

    # 2. 初始化 Qlib
    print("[2/2] 初始化 Qlib...")
    results.append(("Qlib 配置", init_qlib_config()))
    print()

    # 输出结果汇总
    print("=" * 50)
    print("初始化结果汇总:")
    print("=" * 50)
    all_success = True
    for name, success in results:
        status = "✅" if success else "❌"
        print(f"  {status} {name}")
        if not success:
            all_success = False

    print()
    if all_success:
        print("🎉 所有配置初始化成功！")
        print("现在可以运行: python scripts/run_backtest.py")
    else:
        print("⚠️  部分配置初始化失败，请检查上述错误信息")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
