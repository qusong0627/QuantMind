#!/usr/bin/env python3
"""dsh headless agent 封装：把盘中分析/研究任务交给 DeepSeek Harness agent 跑。

【2026-09-29 迁入 QuantMind】大脑侧未搬迁：dsh CLI 与其 MCP（127.0.0.1:8100-8104）、
ROOT/dsh/*.cordis.yml 补丁均属已停机的 BayMax 栈，本文件**暂不可执行**；night_pool
两条 cron 已注释（`# [待改造 2026-09-29]`），待改接 QuantMind dsh 平台后启用。

为什么：单次提示词调用（call_llm）没有工具、不能写代码、无会话记忆。
dsh agent 有 5 个 MCP 工具（行情/搜索/交易/数学/记忆）+ 可写代码算特征。

用法:
  python scripts/dsh_agent.py "<任务文本>"            # 单次（cron 整点分析用）
  python scripts/dsh_agent.py --status                 # dsh 环境自检（MCP 可达性）
  echo "<任务>" | python scripts/dsh_agent.py

输出: agent 完整回复（stdout）；决策 JSON 由调用方 parse_intraday_decision 解析。
关键点:
  - cwd 必须避开仓库目录——dsh 会读 CWD 的 .env，仓库 .env 有 DSH_BIND_IP
    （仅启动环境可设），会直接报错
  - DEEPSEEK_API_KEY 从 .service.env 补进环境（dsh 需要）
  - MCP 走宿主 127.0.0.1:8100-8104（mcp-us 容器端口已发布）
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "dsh" / "baymax.trading.cordis.yml"
GLM_PATCH = ROOT / "dsh" / "baymax.glm.cordis.yml"  # GLM 端点 + 默认模型覆写
NEUTRAL_CWD = Path.home()          # 无 .env 的安全目录

# cron 环境 PATH 不含 ~/.local/bin → 用绝对路径找 dsh（今天 v4-flash 全降级根因）
def _dsh_bin() -> str:
    import shutil

    found = shutil.which("dsh")
    if found:
        return found
    home_bin = Path.home() / ".local" / "bin" / "dsh"
    if home_bin.is_file():
        return str(home_bin)
    return "dsh"

DSH_BIN = _dsh_bin()
MODEL_ENV_KEYS = ("DEEPSEEK_API_KEY", "GLM_API_KEY", "GLM_API_BASE",
                  "OPENAI_API_KEY", "OPENAI_API_BASE", "DASHSCOPE_API_KEY")


def build_env(model: str = "deepseek") -> dict:
    env = os.environ.copy()
    # GLM 走 GLM_API_KEY；其余走 DEEPSEEK_API_KEY（dsh 默认 provider 是 deepseek）
    keys = ("GLM_API_KEY", "GLM_API_BASE") if model == "glm" else MODEL_ENV_KEYS
    for key in keys:
        if env.get(key):
            continue
        for p in (ROOT / ".service.env", ROOT / ".env"):
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith(f"{key}="):
                        env[key] = line.split("=", 1)[1].strip().strip('"')
                        break
            except OSError:
                continue
    return env


def _is_quota_error(msg: str) -> bool:
    """LLM 配额类错误（Token 用尽/限流/余量不足）→ 触发降级重试。"""
    return any(k in msg for k in (
        "Token Insufficient", "Insufficient", "错误码 13", "insufficient",
        "quota", "rate limit", "429", "余量不足", "No Enough Quota",
    ))


def _run_once(task: str, timeout_s: int, model: str,
              extra_patch: str | None) -> str:
    env = build_env(model)
    patches = [str(PATCH)]
    if extra_patch:
        patches.append(extra_patch)
    if model == "glm":
        patches.append(str(GLM_PATCH))
    cmd = [DSH_BIN, "--profile", "headless"]
    for p in patches:
        cmd += ["--patch", p]
    cmd.append(task)
    try:
        r = subprocess.run(cmd, cwd=NEUTRAL_CWD, env=env,
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"dsh agent 超时（>{timeout_s}s）") from None
    if r.returncode != 0:
        raise RuntimeError(f"dsh 失败 rc={r.returncode}: {(r.stderr or r.stdout)[-800:]}")
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError("dsh 无输出")
    return out


def run_agent(task: str, timeout_s: int = 300, model: str = "deepseek",
             extra_patch: str | None = None) -> str:
    """跑一个 dsh headless agent 任务，返回完整回复。失败抛 RuntimeError。
    model: deepseek（默认）/ glm（智谱端点，baymax.glm.cordis.yml 覆写）。
    extra_patch: 追加市场专属 persona（如 baymax.us.cordis.yml 美股规则）。

    2026-09 复盘：闪 speak 配额耗尽（Token Insufficient/错误码 13 ×23）会让
    整轮分析/调仓静默失败——主模型配额类错误时自动降级到备用模型重试一次。
    """
    if not task.strip():
        return ""
    try:
        return _run_once(task, timeout_s, model, extra_patch)
    except RuntimeError as exc:
        if not _is_quota_error(str(exc)):
            raise
        fallback = os.getenv("FALLBACK_LLM_MODEL", "").strip() or (
            "glm" if model == "deepseek" else "deepseek")
        need_key = "GLM_API_KEY" if fallback == "glm" else "DEEPSEEK_API_KEY"
        if fallback == model or not os.getenv(need_key):
            raise  # 无备用端点，原样抛给上层告警
        print(f"[dsh_agent] {model} 配额不足（Token 用尽），降级 {fallback} 重试一次",
              file=sys.stderr)
        return _run_once(task, min(timeout_s, 240), fallback, extra_patch)


def self_check() -> str:
    """自检：dsh 版本 + MCP 五个端点连通性。"""
    lines = []
    r = subprocess.run(["dsh", "--version"], capture_output=True, text=True)
    lines.append(f"dsh: {r.stdout.strip() or r.stderr.strip()}")
    import socket

    for port, name in ((8100, "math"), (8101, "search"), (8102, "trade"),
                       (8103, "price"), (8104, "memory")):
        s = socket.socket()
        s.settimeout(1.5)
        ok = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        lines.append(f"  mcp-{name} 810{port % 10}: {'✓' if ok else '✗ 不可达'}")
    return "\n".join(lines)


def main() -> int:
    if "--status" in sys.argv:
        print(self_check())
        return 0
    task = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    try:
        print(run_agent(task))
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
