#!/usr/bin/env python3
"""生成 Windows 侧「大 QMT 桥」开箱包（拷贝即用，无需在 QMT 里 pip 安装）。

产出 ``dist/qmt-bridge-kit/`` + ``dist/qmt-bridge-kit.zip``：

  bigqmt_signal_trader/                      RPC 服务端包（整个目录）
  bigqmt_signal_trader_strategy.py           策略模块
  bigqmt_signal_trader_redis_rpc_runtime.py  运行时
  BIGQMT_REDIS_DRYRUN.py                     QMT 策略编辑器里的入口
  BIGQMT_ZMQ_DRYRUN.py                       ZMQ 兜底入口（Redis 被沙箱拦截时）
  bigqmt_signal_trader_local_config.py       账号/Redis 配置模板（改这一个文件）
  probe_qmt_env.py                           环境探测（只读）
  部署步骤.txt                                照做即可（UTF-8 BOM + CRLF）
  kit_manifest.json                          版本与文件清单

用法（容器内）::

    docker exec -w /app quantmind python backend/scripts/export_qmt_bridge_kit.py
    docker cp quantmind:/app/dist/qmt-bridge-kit.zip .

为什么不是 pip 安装：QMT 内置 Python 是 **3.6**，而 ``xtquant-big-convert``
声明 ``Requires-Python >=3.8``，在 QMT 的 ``python.exe`` 里必装失败；服务端
只需要 QMT 自带的 ``redis`` 包，代码一律文件拷贝。
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

# QMT python 目录下必需的文件（缺一不可）
_REQUIRED = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy.py",
    "bigqmt_signal_trader_redis_rpc_runtime.py",
    "BIGQMT_REDIS_DRYRUN.py",
)
# 可选：Redis 被券商沙箱拦截时改用 ZMQ 传输的入口
_OPTIONAL = ("BIGQMT_ZMQ_DRYRUN.py",)

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".git", "*.egg-info")

_CONFIG_TEMPLATE = '''# coding: utf-8
"""QMT 端私有配置（只改这一个文件，不要提交 git）。

放在 QMT 的 python 目录下，与 BIGQMT_REDIS_DRYRUN.py 同级。
"""

# QMT 资金账号 —— 必须与 QuantMind 页面里的 account_id 完全一致
BIGQMT_ACCOUNT_ID = "在这里填资金账号"

# 账号类型 —— 服务端只认这里的值（页面上填的 account_type 传不过去）。
# 普通账户 STOCK；两融/信用账户必须改 CREDIT，否则服务端按 STOCK 查询，
# 信用账户会返回「资产全 0」而不是报错。
BIGQMT_ACCOUNT_TYPE = "STOCK"

BIGQMT_REDIS_CONFIG = {
    # 桥 Redis（建议独立实例，只监听局域网 + 密码）
    "host": "在这里填 Redis 地址",
    "port": 6380,
    "db": 0,
    "password": "在这里填 Redis 密码",

    # ★ 下单开关：先保持 False 验通只读链路，确认风控后再改 True
    "rpc_allow_order_methods": False,

    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    # redis 传输保持 True；换 zmq/pipe 必须改 False（上游实测 zmq 差 37 倍）
    "rpc_background_threads": True,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
}
'''

_STEPS = """\
QuantMind · 大 QMT 桥 — Windows 侧开箱包
生成时间：{generated_at}
big-convert 版本：{version}

【这个包是什么】
  QMT 内置 Python 里常驻的 RPC 服务端。QMT 端**不需要 pip 安装任何东西**
  （QMT 自带 Python 3.6，xtquant-big-convert 要求 >=3.8，装不上；且 QMT 的 pip
  用旧 OpenSSL，连 HTTPS 镜像常报 SSL 错）。全部走文件拷贝，只需 QMT 自带的
  redis 包。

【第 1 步 · 放文件】
  1) 先确认 QMT 已下载「Python 组件」：<QMT安装目录>\\bin.x64\\ 下应有
     python.exe 与 Lib\\（全新安装没有，需在 QMT 界面里下载，不要手动建）
  2) 把本包所有文件拷到 QMT 的 python 目录，例如：
     D:\\国金证券QMT交易端\\python\\
     必需：bigqmt_signal_trader\\、bigqmt_signal_trader_strategy.py、
           bigqmt_signal_trader_redis_rpc_runtime.py、BIGQMT_REDIS_DRYRUN.py

【第 2 步 · 改配置】
  编辑 bigqmt_signal_trader_local_config.py：
    BIGQMT_ACCOUNT_ID   = QMT 资金账号（与 QuantMind 页面 account_id 一致）
    BIGQMT_ACCOUNT_TYPE = STOCK 普通 / CREDIT 信用（★ 服务端只认这里；
                          填错信用账户会返回「资产全 0」而不是报错）
    BIGQMT_REDIS_CONFIG = Redis 地址 / 端口 / 密码
    rpc_allow_order_methods = False → 先只验只读链路；
                              确认风控后改 True 才会真正下单

【第 3 步 · 在 QMT 里启动】
  QMT 策略编辑器 → 加载运行 BIGQMT_REDIS_DRYRUN.py（只加载这一个文件，它会
  自己 import 其余模块）。QMT 需处于实盘模式。
  ★ 必须用「策略编辑器」方式运行：用 python.exe 当普通脚本跑、或用独立进程跑，
    QMT 不会注入 passorder/get_trade_detail_data，日志会以 finished 结尾，
    看起来跑起来了其实什么都没监听。
  成功标志（QMT 输出面板）：
    [bigqmt_shell] local rpc config loaded transport=redis keys=[...]
    [bigqmt_shell] local account config loaded=True
    [bigqmt_rpc] started channel=bigqmt:rpc:req:<账号>
    [bigqmt_signal_trader] init ok
  QMT 重启后需重新运行该策略。

【第 4 步 · 放行防火墙】
  Redis 端口只对 QuantMind 主机 IP 开放。

【第 5 步 · 回 QuantMind 验证】
  在 QuantMind 主机执行（只读，不下单）：
    docker exec -w /app/backend -e PYTHONPATH=/app quantmind \\
        python scripts/qmt_bridge_selftest.py
  5 层全 OK 即链路通；哪一层 FAIL，按提示修。

【排错】
  QMT 的 python 目录下 logs\\bigqmt_*.log（保留 7 天）
  详细手册：docs/大QMT真单镜像_部署与上线手册.md
  Redis 被券商沙箱拦截 → 可换 BIGQMT_ZMQ_DRYRUN.py 入口（QMT 侧需 pyzmq；
  注意 QuantMind 客户端侧目前只走 redis 传输，换 ZMQ 还需客户端侧接线，
  属未验证路径，见手册 §三.4）
"""


def _site_packages() -> Path:
    try:
        import bigqmt_signal_trader  # noqa: PLC0415 - 延迟到运行时才要求依赖
    except ImportError:
        print(
            '缺少 xtquant-big-convert：pip install "xtquant-big-convert[redis]"',
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return Path(bigqmt_signal_trader.__file__).resolve().parent.parent


def _write_text(path: Path, text: str) -> None:
    """Windows 记事本友好：UTF-8 BOM + CRLF（否则中文可能显示为乱码）。"""
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8-sig"))


def _copy_server_files(source: Path, out: Path) -> list[str]:
    copied: list[str] = []
    missing: list[str] = []
    for name in _REQUIRED + _OPTIONAL:
        src = source / name
        if not src.exists():
            missing.append(name)
            continue
        dst = out / name
        if dst.exists():
            # 重复生成（更新 QuantMind 后重跑）时先清掉旧副本，避免上游删掉的
            # 模块残留在包里；用户自己的 *_local_config.py 不在此列，不受影响。
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_IGNORE)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    if missing:
        print(f"警告：源目录缺少 {missing}（版本不匹配？）", file=sys.stderr)
    return copied


def _write_kit_config(out: Path, force: bool) -> bool:
    """写配置模板；已存在则不覆盖（保留用户填过的账号密码）。"""
    target = out / "bigqmt_signal_trader_local_config.py"
    if target.exists() and not force:
        print(f"保留已有配置：{target}（--force 可覆盖）")
        return False
    _write_text(target, _CONFIG_TEMPLATE)
    return True


def _zip_kit(out: Path) -> Path:
    archive = out.parent / f"{out.name}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out.parent))
    config = out / "bigqmt_signal_trader_local_config.py"
    if config.exists():
        # 落盘是 BOM + CRLF，比较前先归一化
        text = config.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        if text != _CONFIG_TEMPLATE:
            print(
                "  ⚠ zip 内含已填写的 bigqmt_signal_trader_local_config.py"
                "（资金账号/Redis 密码），别外传"
            )
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="生成大 QMT 桥 Windows 开箱包")
    parser.add_argument(
        "--out",
        default="dist/qmt-bridge-kit",
        help="输出目录（相对仓库根，默认 dist/qmt-bridge-kit）",
    )
    parser.add_argument("--force", action="store_true", help="覆盖已存在的本地配置")
    parser.add_argument("--no-zip", action="store_true", help="只出目录，不打包 zip")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    out = (
        (root / args.out).resolve()
        if not Path(args.out).is_absolute()
        else Path(args.out)
    )
    source = _site_packages()
    version = metadata.version("xtquant-big-convert")
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")

    out.mkdir(parents=True, exist_ok=True)
    copied = _copy_server_files(source, out)

    probe = root / "backend" / "scripts" / "probe_qmt_env.py"
    if probe.exists():
        shutil.copy2(probe, out / probe.name)
        copied.append(probe.name)

    wrote_config = _write_kit_config(out, args.force)
    _write_text(
        out / "部署步骤.txt", _STEPS.format(generated_at=generated_at, version=version)
    )

    manifest = {
        "kit": "qmt-bridge-kit",
        "generated_at": generated_at,
        "big_convert_version": version,
        "server_files": copied,
        "config_template_written": wrote_config,
    }
    manifest_path = out / "kit_manifest.json"
    # 先落盘再统计，让 file_count/total_bytes 把 manifest 自身也算进去
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files = sorted(p for p in out.rglob("*") if p.is_file())
    manifest["file_count"] = len(files)
    manifest["total_bytes"] = sum(p.stat().st_size for p in files)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"开箱包已生成：{out}")
    print(
        f"  big-convert {version}，服务端文件 {len(copied)} 项，"
        f"共 {manifest['file_count']} 个文件 / {manifest['total_bytes'] / 1024:.0f} KB"
    )
    if not args.no_zip:
        archive = _zip_kit(out)
        print(f"  压缩包：{archive}（{archive.stat().st_size / 1024:.0f} KB）")
    print("下一步：把目录/zip 拷到 Windows 的 QMT python 目录，按「部署步骤.txt」操作")
    return 0


if __name__ == "__main__":
    sys.exit(main())
