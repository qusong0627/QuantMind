#!/usr/bin/env python3
"""大 QMT 环境探测脚本（只读，绝不下单）。

用途：回答三个问题，决定 QuantMind 的 QMT 执行端走哪条分支。
  1) `xtquant.xttrader` 是否可用（miniQMT/极速版外接权限）→ 分支 A
  2) `xtquant-big-convert` 服务端能否在 QMT 内置 Python 启动 → 分支 B
  3) 资金账号 / 账号类型 / 下单开关 / 传输依赖现状

在 **大 QMT 那台 Windows 机器**上跑，建议跑两次：
  - 用 QMT 内置 Python 跑（关键，因为服务端就跑在这个解释器里）
  - 用系统 Python 跑（对比，确认差异）

    python probe_qmt_env.py                      # 只探测导入与文件，不连账户
    python probe_qmt_env.py --account 1234567    # 额外检查本地配置里的账号是否匹配
    python probe_qmt_env.py --connect --qmt-path "D:\\国金QMT交易端\\userdata_mini"

产物：同目录 `qmt_probe_report.json`（脱敏，密码/账号只报"是否设置"），
把该文件发回即可。脚本只读：不做任何配置写入、不下单、不建连接（除非显式 --connect）。

兼容性：本文件刻意保持 Python 3.6 语法（QMT 内置解释器为 3.6），
        不使用 f-string 以外的 3.7+ 特性、不使用 dataclasses / 类型注解。
"""

import argparse
import glob
import inspect
import json
import os
import platform
import sys
import traceback
from datetime import datetime

# 服务端需要在 QMT python 目录就位的文件
SERVER_FILES = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy.py",
    "bigqmt_signal_trader_redis_rpc_runtime.py",
    "BIGQMT_REDIS_DRYRUN.py",
)
LOCAL_CONFIG_NAME = "bigqmt_signal_trader_local_config.py"
MASKED_KEYS = ("password", "passwd", "token", "secret", "login_password")


def _safe(callable_, default=None):
    """执行探测片段并吞掉异常，返回 (值, 错误字符串)。"""
    try:
        return callable_(), None
    except Exception:  # noqa: BLE001 - 探测脚本必须继续跑完
        return default, traceback.format_exc(limit=3).strip().splitlines()[-1]


def probe_env():
    return {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "script_dir": os.path.dirname(os.path.abspath(__file__)),
        "sys_path_head": sys.path[:8],
    }


def probe_qmt_dirs():
    """猜测 QMT 安装目录 / python 目录 / 关键文件。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    for pattern in (
        "D:\\*QMT*",
        "C:\\*QMT*",
        "D:\\*\\*QMT*",
        "D:\\*国金*",
        "D:\\*迅投*",
    ):
        candidates.extend(glob.glob(pattern))
    python_dirs = [p for p in candidates if os.path.isdir(os.path.join(p, "python"))]
    return {
        "install_candidates": sorted(set(candidates))[:20],
        "python_dir_candidates": sorted(set(python_dirs))[:10],
        "python36_dll_found": [
            p
            for p in python_dirs
            if os.path.exists(os.path.join(p, "bin.x64", "python36.dll"))
            or os.path.exists(os.path.join(p, "python36.dll"))
        ][:10],
        "server_files_in_script_dir": {
            name: os.path.exists(os.path.join(script_dir, name))
            for name in SERVER_FILES
        },
    }


def probe_xtquant(qmt_path=None, account=None, do_connect=False):
    """分支 A 判定：xtquant.xttrader 是否可用。"""
    out = {"importable": False, "file": None, "is_bigconvert_shim": False}
    try:
        import xtquant  # noqa: F401

        out["importable"] = True
        out["file"] = getattr(xtquant, "__file__", None)
        blob = (
            (out["file"] or "")
            + " "
            + " ".join(str(p) for p in getattr(xtquant, "__path__", []) or [])
        )
        out["is_bigconvert_shim"] = "xtquant_big_convert" in blob
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        return out

    try:
        from xtquant import xttrader  # noqa: F401

        out["xttrader_importable"] = True
        out["xttrader_file"] = getattr(xttrader, "__file__", None)
    except Exception as exc:  # noqa: BLE001
        out["xttrader_importable"] = False
        out["xttrader_error"] = str(exc)
        return out

    out["has_XtQuantTrader"] = hasattr(xttrader, "XtQuantTrader")
    out["has_StockAccount"] = hasattr(xttrader, "StockAccount")

    if not do_connect:
        out["connect"] = {
            "attempted": False,
            "reason": "未指定 --connect（默认不建连接）",
        }
        return out

    if not qmt_path:
        out["connect"] = {"attempted": False, "reason": "缺少 --qmt-path"}
        return out
    if not account:
        out["connect"] = {"attempted": False, "reason": "缺少 --account"}
        return out

    result = {"attempted": True}

    def _connect():
        trader = xttrader.XtQuantTrader(qmt_path, int(datetime.now().strftime("%M%S")))
        start_ret = trader.start()
        conn_ret = trader.connect()
        acc = trader.query_stock_asset(xttrader.StockAccount(str(account)))
        return {
            "start_return": start_ret,
            "connect_return": conn_ret,
            "asset_query_ok": acc is not None,
            "total_asset": getattr(acc, "total_asset", None),
            "cash": getattr(acc, "cash", None),
        }

    value, error = _safe(_connect)
    if error:
        result["error"] = error
    if value:
        result.update(value)
    out["connect"] = result
    return out


def _find_bigconvert_class(module):
    """在包里找带 submit_order 的客户端类。"""
    for name in dir(module):
        obj = getattr(module, name, None)
        if inspect.isclass(obj) and hasattr(obj, "submit_order"):
            return name, obj
    return None, None


def probe_bigconvert():
    out = {"importable": False}
    try:
        import bigqmt_signal_trader as pkg

        out["importable"] = True
        out["version"] = getattr(pkg, "__version__", None)
        out["file"] = getattr(pkg, "__file__", None)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)

    out["redis_importable"] = _safe(lambda: (__import__("redis"), True)[1], False)[0]
    out["zmq_importable"] = _safe(lambda: (__import__("zmq"), True)[1], False)[0]

    if out["importable"]:
        import bigqmt_signal_trader as pkg

        cls_name, cls = _find_bigconvert_class(pkg)
        out["client_class"] = cls_name
        if cls is not None:
            try:
                out["submit_order_signature"] = str(inspect.signature(cls.submit_order))
            except Exception as exc:  # noqa: BLE001
                out["submit_order_signature_error"] = str(exc)
            try:
                out["cancel_order_signature"] = str(inspect.signature(cls.cancel_order))
            except Exception as exc:  # noqa: BLE001
                out["cancel_order_signature_error"] = str(exc)

    out["local_config"] = probe_local_config()
    return out


def probe_local_config():
    """读取 QMT python 目录的私有配置，只报关键键的存在与取值（脱敏）。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, LOCAL_CONFIG_NAME)
    if not os.path.exists(path):
        # 也找 QMT python 目录
        for d in probe_qmt_dirs()["python_dir_candidates"]:
            cand = os.path.join(d, "python", LOCAL_CONFIG_NAME)
            if os.path.exists(cand):
                path = cand
                break
    if not os.path.exists(path):
        return {"found": False}

    result = {"found": True, "path": path, "keys": {}}

    def _load():
        namespace = {}
        with open(path, "rb") as fh:
            source = fh.read()
        # QMT 端配置为 UTF-8/GBK 混合，逐个尝试
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                code = source.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise RuntimeError("无法解码配置文件")
        exec(compile(code, path, "exec"), namespace)  # noqa: S102 - 本机私有配置文件
        return namespace

    namespace, error = _safe(_load, {})
    if error:
        result["error"] = error
        return result

    for key, value in sorted(namespace.items()):
        if key.startswith("__"):
            continue
        if isinstance(value, dict):
            result["keys"][key] = {
                k: ("***" if any(m in k.lower() for m in MASKED_KEYS) and v else v)
                for k, v in value.items()
            }
        elif any(m in key.lower() for m in MASKED_KEYS):
            result["keys"][key] = "***" if value else ""
        else:
            result["keys"][key] = value
    return result


def build_verdict(xtq, bigconv, qmt_dirs):
    notes = []
    branch = "blocked"

    shim = xtq.get("is_bigconvert_shim")
    xtq_ok = xtq.get("importable") and xtq.get("xttrader_importable")
    connect = xtq.get("connect") or {}
    connect_ok = connect.get("attempted") and not connect.get("error")

    if xtq_ok and not shim:
        if connect_ok:
            branch = "A"
            notes.append(
                "xtquant.xttrader 可用且 connect 成功 → 可复用 tools/qmt_agent"
            )
        else:
            notes.append(
                "xtquant.xttrader 可导入，但未验证 connect（加 --connect --qmt-path 复测）"
            )
            branch = "A?"
    if shim:
        notes.append("当前 xtquant 是 big-convert 的 shim（非官方包），分支 A 判定无效")

    if bigconv.get("importable"):
        if all(qmt_dirs.get("server_files_in_script_dir", {}).values()):
            notes.append("big-convert 已装且服务端 4 项文件齐备 → 分支 B 可部署")
        else:
            notes.append(
                "big-convert 已装，但服务端文件不全（见 server_files_in_script_dir）"
            )
        if branch == "blocked":
            branch = "B"
    else:
        notes.append(
            "big-convert 未安装：QMT 内置 Python 里执行 pip install xtquant-big-convert"
        )

    if not bigconv.get("redis_importable") and not bigconv.get("zmq_importable"):
        notes.append("redis 与 zmq 都不可用 → 需要无 redis 版（ZMQ/管道）或放开沙箱")
    return {"branch": branch, "notes": notes}


def main():
    parser = argparse.ArgumentParser(description="大 QMT 环境探测（只读）")
    parser.add_argument("--account", help="资金账号（仅用于比对本地配置，不写盘）")
    parser.add_argument(
        "--connect", action="store_true", help="尝试 xtquant 建连（需 --qmt-path）"
    )
    parser.add_argument(
        "--qmt-path", help="userdata_mini 路径，例如 D:\\国金QMT交易端\\userdata_mini"
    )
    parser.add_argument("--out", default="qmt_probe_report.json", help="报告输出路径")
    args = parser.parse_args()

    qmt_dirs = probe_qmt_dirs()
    xtq = probe_xtquant(
        qmt_path=args.qmt_path, account=args.account, do_connect=args.connect
    )
    bigconv = probe_bigconvert()

    if args.account:
        cfg = bigconv.get("local_config") or {}
        configured = (cfg.get("keys") or {}).get("BIGQMT_ACCOUNT_ID")
        if configured:
            xtq["account_matches_local_config"] = str(configured) == str(args.account)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": probe_env(),
        "qmt_dirs": qmt_dirs,
        "xtquant": xtq,
        "bigconvert": bigconv,
    }
    report["verdict"] = build_verdict(xtq, bigconv, qmt_dirs)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)

    print("=" * 60)
    print("大 QMT 环境探测完成（只读，未下单、未改配置）")
    print("Python : {}".format(report["env"]["python_version"]))
    print("判定   : {}".format(report["verdict"]["branch"]))
    for note in report["verdict"]["notes"]:
        print(f"  - {note}")
    print(f"报告   : {out_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
