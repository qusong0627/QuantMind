#!/usr/bin/env python3
"""QwenPaw → DSH 迁移助手（纯标准库；在 dsh 容器内运行，经 docker CLI 走宿主 daemon）。

子命令：
  inventory                     盘点：旧卷/旧容器/旧技能分类/MCP 客户端/仓库技能核验
  import-skills [--volume V]    用户自定义技能 → 宿主机仓库 skills/（QwenPaw 内置不迁移）
  import-mcp    [--volume V]    旧 MCP 客户端 → dsh mcp_connector 存储（先自动备份）
  cleanup [--remove-volumes]    停止/删除旧 qwenpaw 容器；--remove-volumes 时先打 tar 包再删卷

用法（QuantBot / dsh 容器内）：
  python3 /quantmind/skills/qwenpaw-migrate/scripts/qwenpaw_migrate.py inventory
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

# QwenPaw 运行时内置技能（渠道/浏览器/定时等基础设施，非用户资产，不迁移）
QWENPAW_BUILTIN_SKILLS = {
    "QA_source_index", "browser_cdp", "browser_visible", "channel_message",
    "chat_with_agent", "cron", "dingtalk_channel", "file_reader", "guidance",
    "himalaya", "imessage_channel", "make-skill", "make_plan",
    "multi_agent_collaboration", "news", "skill.json", "wechat_channel",
}

MCP_STORAGE = Path("/root/.dsh/storages/mcp_connector.json")
DSH_SKILLS = Path("/root/.dsh/skills")
BACKUP_DIR = Path("/data/backups")


def sh(cmd: list[str], check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} → {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def find_qwenpaw_volumes() -> list[str]:
    out = sh(["docker", "volume", "ls", "--format", "{{.Name}}"], check=False)
    return [v for v in out.splitlines() if "qwenpaw" in v.lower()]


def find_data_volume(volumes: list[str], explicit: str | None) -> str:
    if explicit:
        return explicit
    data = [v for v in volumes if v.endswith("_qwenpaw-data") or v.endswith("qwenpaw-data")]
    if len(data) == 1:
        return data[0]
    raise SystemExit(f"无法唯一确定 QwenPaw 数据卷（候选 {data}）——请用 --volume 指定")


def find_host_repo() -> str:
    """dsh 容器自身 /quantmind 挂载的宿主路径（写入宿主仓库用）。"""
    out = sh([
        "docker", "inspect", "quantmind-dsh", "--format",
        '{{range .Mounts}}{{if eq .Destination "/quantmind"}}{{.Source}}{{end}}{{end}}',
    ], check=False)
    if not out:
        raise SystemExit("未能定位 /quantmind 宿主路径（容器名非 quantmind-dsh？）")
    return out


def host_cmd(args: str, volume: str | None = None, host_paths: dict[str, str] | None = None) -> str:
    """经宿主 daemon 执行 shell（-v 路径一律为宿主路径）。"""
    cmd = ["docker", "run", "--rm"]
    if volume:
        cmd += ["-v", f"{volume}:/src", "-v", f"{volume}:/w"]
    for host, dest in (host_paths or {}).items():
        cmd += ["-v", f"{host}:{dest}"]
    cmd += ["alpine", "sh", "-c", args]
    return sh(cmd)


def read_json_from_volume(volume: str, path: str) -> dict:
    out = host_cmd(f"cat {path}", volume)
    return json.loads(out)


def list_dir_from_volume(volume: str, path: str) -> list[str]:
    """只返回目录项（ls -p 给目录加尾 /）——技能目录必须，过滤 skill.json 等文件。"""
    out = host_cmd(f"ls -p {path} 2>/dev/null", volume)
    return [x[:-1] for x in out.splitlines() if x.endswith("/")]


# ── inventory ────────────────────────────────────────────────────────────────

def cmd_inventory(args: list[str]) -> None:
    volumes = find_qwenpaw_volumes()
    containers = sh(["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=qwenpaw"], check=False)
    report: dict = {"volumes": volumes, "qwenpaw_containers": containers.splitlines()}
    if volumes:
        data_vol = find_data_volume(volumes, None)
        report["data_volume"] = data_vol
        # 旧技能分类
        pool = set(list_dir_from_volume(data_vol, "/w/skill_pool"))
        ws = set(list_dir_from_volume(data_vol, "/w/workspaces/default/skills"))
        repo = {p.name for p in DSH_SKILLS.iterdir() if p.is_dir()} if DSH_SKILLS.exists() else set()
        old_all = (pool | ws) - QWENPAW_BUILTIN_SKILLS
        report["skills"] = {
            "old_total": len(pool | ws),
            "builtin_skipped": sorted((pool | ws) & QWENPAW_BUILTIN_SKILLS),
            "already_in_repo": sorted(old_all & repo),
            "custom_to_import": sorted(old_all - repo),
            "repo_skill_count": len(repo),
        }
        # 旧 MCP
        try:
            cfg = read_json_from_volume(data_vol, "/w/config.json")
            clients = (cfg.get("mcp") or {}).get("clients") or {}
            report["mcp_clients"] = {
                name: {k: c.get(k) for k in ("name", "enabled", "transport", "command", "args", "url")}
                for name, c in clients.items()
            }
        except Exception as e:  # noqa: BLE001
            report["mcp_clients"] = f"读取失败: {e}"
        # 本机连接器现状
        if MCP_STORAGE.exists():
            st = json.loads(MCP_STORAGE.read_text())
            report["dsh_connections_existing"] = list((st.get("tables") or {}).get("connections") or {})
        report["host_repo"] = find_host_repo()
    print(json.dumps(report, ensure_ascii=False, indent=2))


# ── import-skills ────────────────────────────────────────────────────────────

def cmd_import_skills(args: list[str]) -> None:
    explicit = _opt(args, "--volume")
    volumes = find_qwenpaw_volumes()
    data_vol = find_data_volume(volumes, explicit)
    host_repo = find_host_repo()
    # 比较集用容器内可见的挂载目录（宿主路径只用于 docker -v 挂载）
    repo = {p.name for p in DSH_SKILLS.iterdir() if p.is_dir()} if DSH_SKILLS.exists() else set()
    old = set(list_dir_from_volume(data_vol, "/w/skill_pool")) | set(
        list_dir_from_volume(data_vol, "/w/workspaces/default/skills")
    )
    customs = sorted((old - QWENPAW_BUILTIN_SKILLS) - repo)
    if not customs:
        print("[ok] 无用户自定义技能需要导入（旧库技能均已在仓库或属 QwenPaw 内置）")
        return
    for name in customs:
        src = f"/w/workspaces/default/skills/{name}"
        # 工作区没有则回退 skill_pool
        exists = host_cmd(f"[ -d {src} ] && echo yes || echo no", data_vol)
        if exists != "yes":
            src = f"/w/skill_pool/{name}"
        host_cmd(f"cp -r {src} /dest/ && echo copied", data_vol, {f"{host_repo}/skills": "/dest"})
        print(f"[ok] 技能导入：{name} → {host_repo}/skills/{name}")
    print(f"[完成] 共导入 {len(customs)} 个技能；重启 dsh 容器后技能列表生效")


# ── import-mcp ───────────────────────────────────────────────────────────────

def build_connection_record(slug: str, client: dict) -> dict:
    """QwenPaw mcp.clients 条目 → dsh-mcp-connector ConnectionRecord（本地迁移，保留值）。"""
    now = int(time.time() * 1000)
    transport = str(client.get("transport") or "stdio")
    if transport not in ("stdio", "streamable-http", "sse"):
        transport = "streamable-http" if client.get("url") else "stdio"
    name = str(client.get("name") or slug)
    rec: dict = {
        "key": f"qwenpaw-{slug}-main",
        "connectorId": f"qwenpaw-{slug}",
        "kind": "manual",
        "name": name,
        "serverKey": "main",
        "transport": transport,
        "serverName": name,
        "enabled": bool(client.get("enabled", True)),
        "createdAt": now,
        "updatedAt": now,
    }
    if transport == "stdio":
        rec["command"] = str(client.get("command") or "")
        rec["args"] = [str(a) for a in (client.get("args") or [])]
        env = {k: str(v) for k, v in (client.get("env") or {}).items()}
        if env:
            rec["env"] = env
        if client.get("cwd"):
            rec["cwd"] = str(client["cwd"])
    else:
        rec["url"] = str(client.get("url") or "")
        headers = {k: str(v) for k, v in (client.get("headers") or {}).items() if v}
        if headers:
            rec["headers"] = headers
        rec["allowInsecurePrivateNetwork"] = True  # 旧客户端多为内网/自建服务
    return rec


def cmd_import_mcp(args: list[str]) -> None:
    explicit = _opt(args, "--volume")
    data_vol = find_data_volume(find_qwenpaw_volumes(), explicit)
    cfg = read_json_from_volume(data_vol, "/w/config.json")
    clients = (cfg.get("mcp") or {}).get("clients") or {}
    if not clients:
        print("[ok] 旧配置无 MCP 客户端（mcp.clients 为空）")
        return
    if not MCP_STORAGE.exists():
        raise SystemExit(f"未找到 {MCP_STORAGE}（dsh-mcp-connector 未安装？）")

    backup = MCP_STORAGE.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(MCP_STORAGE, backup)
    st = json.loads(MCP_STORAGE.read_text())
    connections = st.setdefault("tables", {}).setdefault("connections", {})
    added, skipped = [], []
    for slug, client in clients.items():
        rec = build_connection_record(slug, client)
        if rec["transport"] == "stdio" and not rec.get("command"):
            skipped.append(f"{slug}（stdio 无 command，无法迁移）")
            continue
        if rec["transport"] != "stdio" and not rec.get("url"):
            skipped.append(f"{slug}（无 url，无法迁移）")
            continue
        if rec["key"] in connections:
            skipped.append(f"{slug}（已存在 {rec['key']}，跳过）")
            continue
        connections[rec["key"]] = rec
        added.append(rec["key"])
    MCP_STORAGE.write_text(json.dumps(st, ensure_ascii=False))
    print(json.dumps({"backup": str(backup), "added": added, "skipped": skipped}, ensure_ascii=False, indent=1))
    print("[完成] 重启 dsh 容器后连接生效（重启会中断当前会话，先告知用户）")


# ── cleanup ──────────────────────────────────────────────────────────────────

def cmd_cleanup(args: list[str]) -> None:
    remove_volumes = "--remove-volumes" in args
    containers = sh(["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=qwenpaw"], check=False)
    for name in [c for c in containers.splitlines() if c.strip()]:
        sh(["docker", "stop", name], check=False)
        sh(["docker", "rm", name], check=False)
        print(f"[ok] 已停止并删除容器 {name}")

    volumes = find_qwenpaw_volumes()
    if not remove_volumes:
        print(f"[保留] 数据卷未删除（{volumes}）；确认备份后可加 --remove-volumes")
        return
    if not volumes:
        print("[ok] 无遗留卷")
        return
    # 先备份到 /data/backups/（宿主 ./data 卷，长期保留）
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for vol in volumes:
        tar_path = BACKUP_DIR / f"{vol}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        # 用 docker run 读卷 + tar 输出，本地落盘（路径为宿主 /data → 本容器 /data）
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{vol}:/w", "alpine", "tar", "czf", "-", "-C", "/w", "."],
            capture_output=True,
        )
        if r.returncode != 0:
            raise SystemExit(f"卷 {vol} 备份失败：{r.stderr.decode()[:200]}")
        tar_path.write_bytes(r.stdout)
        print(f"[ok] 已备份 {vol} → {tar_path}（{tar_path.stat().st_size // 1024} KB）")
        r2 = subprocess.run(["docker", "volume", "rm", vol], capture_output=True, text=True)
        if r2.returncode != 0:
            # 如 quantmind 主容器仍挂 qwenpaw-shared（compose 引用未清）——零头卷，保留即可
            print(f"[保留] {vol} 正在被容器引用，未删除（{r2.stderr.strip()[:120]}）")
        else:
            print(f"[ok] 已删除卷 {vol}")


def _opt(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("inventory", "import-skills", "import-mcp", "cleanup"):
        print(__doc__)
        sys.exit(2)
    cmd, rest = sys.argv[1], sys.argv[2:]
    {"inventory": cmd_inventory, "import-skills": cmd_import_skills,
     "import-mcp": cmd_import_mcp, "cleanup": cmd_cleanup}[cmd](rest)


if __name__ == "__main__":
    main()
