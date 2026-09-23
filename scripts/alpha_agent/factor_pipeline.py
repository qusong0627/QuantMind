#!/usr/bin/env python3
"""RD-Agent 因子挖掘端到端管线（一键式）。

覆盖：preflight 环境检查 → 启动演化 → 轮询到完成 → 批量回测评估 →
     按 IC/Sharpe 排序 → explain 解读 topN → export 入库 → Markdown 报告。

依赖：仅标准库（urllib）。宿主机直接运行，服务在 http://127.0.0.1:8000。

示例：
  python scripts/alpha_agent/factor_pipeline.py --direction "连板高度递增与涨停回封率" --universe csi300 --loops 3
  python scripts/alpha_agent/factor_pipeline.py --check-env
  python scripts/alpha_agent/factor_pipeline.py --direction "..." --explain-top 5 --export --out /tmp/factor_report.md
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlencode

BASE = os.environ.get("QM_BASE", "http://127.0.0.1:8000")
API = "/api/v1"
LW_START = "2025-01-01"          # 轻量回测默认起点
POLL_INTERVAL = 25               # 演化/回测轮询间隔（秒）


# ---------- low-level api ----------
def api(method, path, token=None, body=None, params=None, timeout=120):
    url = BASE + API + path
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def login():
    d = api("POST", "/auth/login", body={
        "username": os.getenv("QM_USER", "admin"),
        "password": os.getenv("QM_PASSWORD", ""),  # 口令不入库，从 .env 读
        "tenant_id": "default"})
    return d["access_token"]


def backoff(fn, tries=4, wait=4):
    """短暂重试（engine 偶发 503 时有用）"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(wait * (i + 1))
    raise last


def task_detail(token, task_id):
    return backoff(lambda: api("GET", f"/alpha-agent/tasks/{task_id}", token=token))["data"]


def task_log(token, task_id, tail=3):
    try:
        lines = api("GET", f"/alpha-agent/tasks/{task_id}/log", token=token)["data"].get("lines", [])
        return lines[-tail:]
    except Exception:
        return []


def all_factors(token):
    return api("GET", "/alpha-agent/factors", token=token)["data"]["factors"]


def start_evolution(token, direction, universe, loops):
    form = {"market": "a_share", "universe": universe, "loop_n": int(loops), "direction": direction}
    return api("POST", "/alpha-agent/evolve", token=token, body=None, params=form)["data"]


def trigger_backtest(token, fid, start=LW_START, end=None, universe="csi300"):
    end = end or datetime.now().strftime("%Y-%m-%d")
    params = {"start_date": start, "end_date": end, "universe": universe, "data_source": "qlib_bin"}
    return backoff(lambda: api("POST", f"/alpha-agent/factors/{fid}/backtest", token=token, params=params))  # noqa: E501


def explain_factor(token, fid):
    return backoff(lambda: api("POST", f"/alpha-agent/factors/{fid}/explain", token=token), tries=3, wait=3)


def export_factor(token, fid):
    return backoff(lambda: api("POST", f"/alpha-agent/factors/{fid}/export", token=token), tries=3, wait=3)


# ---------- steps ----------
def check_env():
    """容器内 conda shim / litellm 补丁 / DeepSeek key 三项健康检查。"""
    print("[preflight] 容器内环境检查 ...")
    outs = {}
    checks = [
        ("conda_shim", 'docker exec quantmind bash -c \'conda env list | grep -q rdagent4qlib && echo OK\''),
        ("litellm_patch", 'docker exec quantmind bash -c \'python -c "import litellm.types.utils as U; U.Message.model_rebuild(); print(1)" | grep -q 1 && echo OK\''),
        ("deepseek_key", 'docker exec quantmind bash -c \'env | grep -q "^DEEPSEEK_API_KEY=" && echo OK\''),
    ]
    for name, cmd in checks:
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0 and "OK" in r.stdout
            outs[name] = ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        except Exception as e:
            outs[name] = False
            print(f"  FAIL  {name}: {e}")
    if not all(outs.values()):
        print("[preflight] 未全部通过：先修复环境再挖因子。参见 skill 环境前置小节。")
        return False
    return True


def wait_task(token, task_id, loops, show_log=False):
    deadline = time.time() + max(int(loops) * 15 * 60, 30 * 60)
    while time.time() < deadline:
        d = task_detail(token, task_id)
        st = d.get("status")
        print(f"  [{d.get('phase','?')}] loop {d.get('current_loop')}/{d.get('loop_n')} "
              f"err={d.get('error_message')!r}", flush=True)
        if st in ("completed", "failed"):
            return d
        if show_log:
            for line in task_log(token, task_id):
                print("    |", line[:200])
        time.sleep(POLL_INTERVAL)
    return None


def poll_backtests(token, factor_ids, universe):
    done, deadline = {}, time.time() + 60 * 60
    print(f"[backtest] 已触发 {len(factor_ids)} 个因子回测，轮询中 ...")
    while time.time() < deadline and len(done) < len(factor_ids):
        time.sleep(POLL_INTERVAL)
        for f in all_factors(token):
            if f["factor_id"] in factor_ids and f["status"] in ("completed", "failed"):
                done[f["factor_id"]] = f  # 注意：列表内 metrics 可能滞后，回测完成以 status 为准
        if len(done) >= len(factor_ids):
            break
        print(f"  waiting ... done {len(done)}/{len(factor_ids)}", flush=True)
    return done


def main():
    p = argparse.ArgumentParser(description="RD-Agent 因子挖掘一键管线")
    p.add_argument("--direction", type=str, help="挖掘方向/假设表述，如\"连板高度递增与涨停回封率\"")
    p.add_argument("--universe", default="csi300",
                   help="股票池: csi300/csi500/csi1000/sse50/gem/star/csi800/all_a（后端对部分池归一）")
    p.add_argument("--loops", type=int, default=3, help="演化轮数（1-20，实际以任务详情为准）")
    p.add_argument("--check-env", action="store_true", help="只跑容器环境健康检查")
    p.add_argument("--no-backtest", action="store_true", help="演化完成后不触发批量回测")
    p.add_argument("--backtest-start", default=LW_START, help="回测起始日")
    p.add_argument("--backtest-universe", default=None, help="回测股票池（默认同 universe）")
    p.add_argument("--explain-top", type=int, default=0, help="对 IC 排名前 N 因子调用 explain")
    p.add_argument("--export", action="store_true", help="对 IC 最高因子 export 进生产特征库")
    p.add_argument("--min-ic", type=float, default=0.0, help="export 的最低 |IC| 门槛")
    p.add_argument("--out", type=str, default="/tmp/rd_agent_factor_report.md", help="报告输出路径")
    p.add_argument("--show-log", action="store_true", help="轮询时打印任务日志")
    args = p.parse_args()

    if args.check_env:
        sys.exit(0 if check_env() else 1)

    if not args.direction:
        print("缺少 --direction，退出。方向建议见 skill 的\"方向建议库\"。")
        sys.exit(2)

    token = login()
    bt_univ = args.backtest_universe or args.universe
    report = []

    # 1) 环境 preflight
    env_ok = check_env()
    report.append(f"# RD-Agent 因子挖掘报告\n\n- 方向: {args.direction}\n- 股票池: {args.universe}"
                  f"\n- 演化轮数: {args.loops}\n- 环境 preflight: {'PASS' if env_ok else 'FAIL'}\n- 时间: "
                  f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 2) 启动演化
    d = start_evolution(token, args.direction, args.universe, args.loops)
    task_id = d.get("task_id")
    print(f"[evolve] task_id={task_id}  status={d.get('status')}  message={d.get('message')}")
    report.append(f"## 1. 演化任务\n\n- task_id: `{task_id}`\n\n")

    # 3) 轮询完成
    print("[evolve] 等待演化完成（轮询中，可 Ctrl+C 后用 /tasks 续看）...")
    fin = wait_task(token, task_id, args.loops, show_log=args.show_log)
    if fin is None:
        print("[evolve] 轮询超时退出（任务仍在后台运行）")
        sys.exit(3)
    if fin.get("status") != "completed":
        print(f"[evolve] 任务失败: {fin.get('error_message')}")
        sys.exit(1)
    print("[evolve] 完成")
    report.append(f"## 2. 演化完成（{fin.get('loop_n')} 轮）\n")

    # 4) 收集本次任务因子
    factors = [f for f in all_factors(token) if f.get("metadata", {}).get("task_id") == task_id]
    print(f"[factor] 本次任务入库因子: {len(factors)} 个")
    if args.no_backtest or not factors:
        if factors:
            for f in factors:
                print(f"  - {f['factor_name']}  ({f['factor_id']})")
        print("完成（未触发批量回测或此次无因子）。")
        return

    # 5) 批量回测
    ids = [f["factor_id"] for f in factors]
    for fid in ids:
        try:
            trigger_backtest(token, fid, args.backtest_start, universe=bt_univ)
        except Exception as e:
            print(f"  trigger fail {fid[:8]}: {e}")
    poll_backtests(token, ids, bt_univ)

    # 6) 排序（回测完成后重新拉取精确指标）
    facts = all_factors(token)
    byid = {f["factor_id"]: f for f in facts}
    scored = [byid[i] for i in ids if byid.get(i, {}).get("ic_value") is not None]
    scored.sort(key=lambda f: -abs(f["ic_value"]))
    print(f"\n[score] 回测完成，有 IC 的因子 {len(scored)}/{len(ids)} 个，按 |IC| 排序：")
    for f in scored[:15]:
        print(f"  {f['factor_name']:<30} ic={f['ic_value']:+.4f} icir={f.get('rank_ic')} "
              f"sharpe={f.get('sharpe_ratio') if f.get('sharpe_ratio') is not None else 0:+.2f} "
              f"mdd={f.get('max_drawdown')}")
    report.append(f"## 3. 回测评估（{arena_scope(args.backtest_start)} ~ 今, {bt_univ}）\n\n| 因子 | IC | ICIR | Sharpe | MDD |\n|---|---|---|---|---|\n")
    for f in scored[:20]:
        report.append(f"| {f['factor_name']} | {f['ic_value']:+.4f} | {f.get('rank_ic')} | "
                      f"{f.get('sharpe_ratio') if f.get('sharpe_ratio') is not None else ''} | {f.get('max_drawdown')} |\n")
    report.append("\n")

    # 7) explain topN
    explained = []
    if args.explain_top > 0 and scored:
        print(f"\n[explain] 对 top {args.explain_top} 因子调用 LLM 解读 ...")
        report.append(f"## 4. 因子解读（top {args.explain_top}）\n")
        for f in scored[:args.explain_top]:
            try:
                r = explain_factor(token, f["factor_id"])
                txt = r.get("data") or r.get("explanation") or r.get("message") or str(r)[:500]
                explained.append((f, txt))
                print(f"  - {f['factor_name']}: {str(txt)[:100]}")
                report.append(f"### {f['factor_name']}（IC={f['ic_value']:+.4f}）\n\n{txt}\n")
            except Exception as e:
                print(f"  explain fail {f['factor_name']}: {e}")
        report.append("\n")

    # 8) export
    exported = []
    if args.export and scored and abs(scored[0]["ic_value"]) >= args.min_ic:
        top = scored[0]
        try:
            r = export_factor(token, top["factor_id"])
            exported.append(top)
            print(f"[export] 已导出 {top['factor_name']} -> {r.get('data') or r.get('message') or 'ok'}")
            report.append(f"## 5. 导出\n\n- `{top['factor_name']}` 已加入生产特征库\n")
        except Exception as e:
            print(f"[export] fail: {e}")

    with open(args.out, "w") as fh:
        fh.write("".join(report))
    print(f"\n[done] 报告已写入 {args.out}")


def arena_scope(s):
    return s


if __name__ == "__main__":
    main()
