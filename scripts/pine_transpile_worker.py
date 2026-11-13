#!/usr/bin/env python3
"""Pine 转写 / 回测队列 worker（宿主侧）。

界面在「策略库」点「转写并回测」时，API 只往 ``data/pine_transpile/queue/<id>.json``
写一个请求——容器里没有 bwrap，也不该执行模型产出的代码。本脚本在宿主上消费队列：

  1. 转写段：调模型把 .pine 翻成 Pyne-Python（要联网，普通子进程）
  2. 校验段：静态闸 → bwrap 沙箱回测（只读根 + 断网 + 只把该策略目录挂成可写）；
     哪一段不过都把原因喂回模型重修，最多 MAX_REPAIRS 轮

沙箱不是可选项：候选是模型产物，静态检查只是速度闸不是沙箱。bwrap 缺失直接拒跑，
不允许静默降级成裸执行。

状态写在 ``data/pine_transpile/<id>/job.json``（界面轮询），回测结果写 report.json。

cron::

    * * * * * cd /home/zbox/projects/quantmind && /usr/bin/python3 scripts/pine_transpile_worker.py >> logs/pine_transpile_worker.log 2>&1

手动跑一次：``/usr/bin/python3 scripts/pine_transpile_worker.py``

移植说明（2026-10-08，自 quant-Trader 逐字节迁移）：本文件刻意**不 import 任何
backend 包**（常量全部模块级、路径走 ROOT/data 符号链接）——宿主 cron 直接用系统
python3 跑，容器内也能原样跑单测。队列由容器 API（root）写入、宿主 worker 摘牌；
队列目录由宿主预先建好（普通属主目录允许属主 unlink 别人的文件），若哪天 unlink
报权限错：``chown 1000:1000`` 该目录即可。
"""
from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data/pine_transpile"
QUEUE_DIR = OUT_DIR / "queue"
LOCK_PATH = OUT_DIR / "worker.lock"
CLI = ROOT / "scripts/pine_to_pyne.py"

TRANSPILE_TIMEOUT = 900      # 推理模型 + 32k 上限，长策略一次转写可能好几分钟
BACKTEST_TIMEOUT = 180
MAX_REPAIRS = 2             # 报错喂回模型重修的轮数上限（静态闸与回测都算）
LOG_TAIL = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_job_at(path: Path, **fields) -> dict:
    """写任意 job.json（原子替换）。界面只读这个文件判断进度。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    job = {}
    if path.exists():
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            job = {}
    job.update(fields)
    job["updated"] = _now()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return job


def _write_job(item_id: str, **fields) -> dict:
    return _write_job_at(OUT_DIR / item_id / "job.json", **fields)


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-LOG_TAIL:] if len(text) > LOG_TAIL else text


def _last_line(text: str) -> str:
    """最后一行非空输出——CLI 失败时把原因打成一行，界面直接显示它（别只给 exit code）。"""
    for ln in reversed((text or "").strip().splitlines()):
        if ln.strip():
            return ln.strip()[:200]
    return ""


def _read_meta(item_dir: Path) -> dict:
    p = item_dir / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def sandbox_cmd(item_dir: Path, argv: list[str]) -> list[str]:
    """把命令包进 bwrap：只读根、断网、仅 item_dir 可写、/tmp 用 tmpfs。

    ``--unshare-net`` 是重点——回测不需要网络，断掉之后候选就算绕过了静态检查
    也发不出数据。写路径只放行该策略目录（PyneCore 会在脚本旁删/写 .toml）。

    ``item_dir.resolve()``：QM 的 ``data/`` 是指向 /media 的**符号链接**，而 bwrap
    为 --bind 目标建挂载点时**不穿符号链接**（报 Can't mkdir parents: No such
    file or directory）——必须绑真实路径；沙箱内 CLI 走符号链接访问同一目录不受影响。
    """
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("没有 bwrap，拒绝在沙箱外执行模型产出的代码（apt install bubblewrap）")
    real_dir = item_dir.resolve()
    return [bwrap, "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", "--unshare-net",
            "--chdir", str(ROOT),
            "--bind", str(real_dir), str(real_dir),
            sys.executable, str(CLI), *argv]


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout, check=False)


def process(req: dict, item_dir: Path | None = None,
            job_path: Path | None = None, success_status: str = "done") -> dict:
    """处理一条队列请求。返回 job 字段。

    ``item_dir`` / ``job_path`` 给对话改策略的候选改道用：候选跑在
    ``data/pine_library/chat/<id>/<job_id>/`` 里，进度直接写对话 job（合并写，
    不动 reply 等字段），所以 ``job_path=None`` 表示不写 job 文件。
    正式队列由 ``main()`` 显式传 ``data/pine_transpile/<id>/job.json``。

    ``success_status``：成功时写什么状态。对话候选写 ``staged``——它还没跑完
    （对话 worker 还要落前后对比），界面不能把它当终态停止轮询。
    """
    item_id = str(req.get("id") or "").strip()
    symbol = str(req.get("symbol") or "").strip()
    adj = str(req.get("adj") or "backward")
    model = str(req.get("model") or "")
    source = str(req.get("source") or "").strip()
    if not item_id or not symbol:
        raise ValueError("请求缺 id / symbol")
    item_dir = item_dir or OUT_DIR / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    def write_job(**fields) -> dict:
        if job_path is None:
            return dict(fields, id=item_id, updated=_now())
        return _write_job_at(job_path, id=item_id, **fields)

    write_job(status="running", stage="transpile", symbol=symbol, adj=adj,
              error="", started=_now())

    # 候选与正式产物走同一套 CLI、同一道静态闸、同一个沙箱，只有路径改道。
    where = ["--out", str(item_dir)]
    if source:
        where += ["--source", source]

    # ---- 1. 转写（联网；只生成文本，不执行候选）----
    argv = ["--id", item_id, *where]
    if model:
        argv += ["--model", model]
    if req.get("force"):
        argv.append("--force")
    try:
        got = _run([sys.executable, str(CLI), *argv], TRANSPILE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return write_job(status="failed", stage="transpile",
                         error=f"转写超时（>{TRANSPILE_TIMEOUT}s）")
    if got.returncode not in (0, 2):
        detail = _last_line(got.stdout + "\n" + got.stderr)
        return write_job(status="failed", stage="transpile",
                         error=detail or f"转写失败（exit {got.returncode}）",
                         log=_tail(got.stdout + got.stderr))

    # ---- 2. 静态闸 + 沙箱回测：哪一段不过都把原因喂回模型重修，最多 MAX_REPAIRS 轮 ----
    # 首次转写的语法错误也走这个循环：实测 30 条抽样里 8 条卡在 Pine 残留语法，
    # 直接判死太浪费，模型看一眼报错基本能改对。
    for attempt in range(MAX_REPAIRS + 1):
        problems = _read_meta(item_dir).get("problems") or []
        got = None
        if problems:
            stage = "static"
            detail = "静态检查未通过：" + "；".join(problems)
            feedback = detail
        else:
            write_job(status="running", stage="backtest", attempt=attempt + 1)
            try:
                argv = sandbox_cmd(item_dir, ["--id", item_id, *where,
                                              "--no-transpile",
                                              "--validate", symbol, "--adj", adj])
            except RuntimeError as exc:
                return write_job(status="failed", stage="backtest", error=str(exc))
            try:
                got = _run(argv, BACKTEST_TIMEOUT)
            except subprocess.TimeoutExpired:
                return write_job(status="failed", stage="backtest",
                                 error=f"回测超时（>{BACKTEST_TIMEOUT}s）")
            if got.returncode == 0:
                return write_job(status=success_status, stage="done", error="", problems=[],
                                 log=_tail(got.stdout))
            # 沙箱里 --no-transpile 会复读 meta.problems，所以这里再读一次
            problems = _read_meta(item_dir).get("problems") or []
            if problems:
                stage = "static"
                detail = "静态检查未通过：" + "；".join(problems)
                feedback = detail
            else:
                stage = "backtest"
                detail = _last_line(got.stdout + "\n" + got.stderr)
                feedback = _tail(got.stdout + "\n" + got.stderr)

        if attempt >= MAX_REPAIRS:
            return write_job(status="failed", stage=stage,
                             error=detail or "失败", problems=problems,
                             log=_tail((got.stdout + "\n" + got.stderr) if got else ""))

        # 模型只改代码；执行永远只发生在下一轮沙箱里
        write_job(status="running", stage="repair", error=detail)
        try:
            fix = _run([sys.executable, str(CLI), "--id", item_id, *where,
                        "--repair", feedback], TRANSPILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return write_job(status="failed", stage="repair",
                             error=f"修复超时（>{TRANSPILE_TIMEOUT}s）")
        if fix.returncode not in (0, 2):     # 2 = 修完仍有静态问题，留给下一轮继续修
            return write_job(status="failed", stage="repair",
                             error=_last_line(fix.stdout + "\n" + fix.stderr)
                             or f"修复失败（exit {fix.returncode}）",
                             log=_tail(fix.stdout + "\n" + fix.stderr))


def _claim_lock():
    """独占锁：cron 每分钟起一次，上一轮没跑完就安静退出。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main() -> int:
    lock = _claim_lock()
    if lock is None:
        print(f"{_now()} 上一轮还在跑，跳过")
        return 0
    if not QUEUE_DIR.is_dir():
        return 0

    reqs = sorted(QUEUE_DIR.glob("*.json"))
    if not reqs:
        return 0
    print(f"{_now()} 待处理 {len(reqs)} 条")
    for path in reqs:
        t0 = time.time()
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{_now()} 请求文件损坏 {path.name}: {exc}")
            path.unlink(missing_ok=True)
            continue
        # 先摘牌再处理：处理中重复点按钮不会排两遍（API 也有一道查重）。
        path.unlink(missing_ok=True)
        try:
            job = process(req, job_path=OUT_DIR / str(req.get("id") or "unknown") / "job.json")
            print(f"{_now()} {req.get('id')} → {job.get('status')}/{job.get('stage')} "
                  f"（{time.time() - t0:.1f}s）{job.get('error') or ''}")
        except Exception as exc:  # noqa: BLE001
            print(f"{_now()} {req.get('id')} 处理异常: {exc}")
            _write_job(str(req.get("id") or "unknown"), status="failed", stage="worker",
                       error=f"worker 异常: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
