"""AutoDL 训练节点配置加载与状态采集。

提供两件事：
1. load_training_nodes() —— 读取多节点配置（config/training_nodes.yaml），
   无 YAML 时回退单节点环境变量（向后兼容）。
2. NodeStatus.collect() —— SSH 到节点采集实时状态（CPU/GPU/内存/训练容器），
   供后台「AutoDL 节点」状态面板展示。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

NODES_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "training_nodes.yaml"
# 容器内路径（docker-compose 挂载 ./config:/app/config）
NODES_CONFIG_CONTAINER = Path("/app/config/training_nodes.yaml")


def _resolve_config_path() -> Path | None:
    for p in (NODES_CONFIG_PATH, NODES_CONFIG_CONTAINER):
        if p.exists():
            return p
    return None


def _env_or(key: str, default: str) -> str:
    return (os.getenv(key) or default).strip()


# 写操作相关常量
_PASSWORD_FIELD = "ssh_password"
_KEY_FIELD = "ssh_key"
_PUBLIC_FIELDS = (
    "id", "name", "host", "port", "user", "work_dir", "docker_image", "gpus",
)


def _load_yaml() -> dict[str, Any]:
    """读取节点配置文件原始内容（不存在时返回空结构）。"""
    cfg_path = _resolve_config_path()
    if cfg_path and cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取训练节点配置失败 %s: %s", cfg_path, exc)
    return {}


def _write_yaml(data: dict[str, Any]) -> Path:
    """原子写回节点配置文件（临时文件 + rename），返回写入路径。"""
    cfg_path = _resolve_config_path()
    if cfg_path is None:
        raise RuntimeError("未找到训练节点配置文件（training_nodes.yaml），无法保存节点配置")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cfg_path.with_suffix(".yaml.tmp")
    tmp_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp_path.replace(cfg_path)
    return cfg_path


def _sanitize_node_for_output(node: dict[str, Any]) -> dict[str, Any]:
    """输出给前端的节点信息：剔除明文密码，仅保留是否已配置标记。"""
    out = {k: node.get(k) for k in _PUBLIC_FIELDS}
    out["id"] = str(node.get("id") or "")
    out["has_password"] = bool(node.get(_PASSWORD_FIELD))
    out["has_key"] = bool(node.get(_KEY_FIELD))
    return out


def save_training_node(node: dict[str, Any]) -> dict[str, Any]:
    """新增或更新一个训练节点。

    - 按 node["id"] 定位；已存在则更新，否则追加。
    - ssh_password / ssh_key 为空字符串时表示"保持不变"（不回显明文）。
    - 校验：id/host/user/port 必填，密码与 key 至少其一（首次创建时）。
    """
    node_id = str(node.get("id") or "").strip()
    if not node_id:
        raise ValueError("节点 id 不能为空")
    host = str(node.get("host") or "").strip()
    if not host:
        raise ValueError("节点 host 不能为空")

    data = _load_yaml()
    nodes = data.get("nodes") or []
    existing = next((n for n in nodes if str(n.get("id") or "") == node_id), None)
    is_new = existing is None

    if is_new:
        user = str(node.get("user") or "root").strip()
        pwd = str(node.get(_PASSWORD_FIELD) or "").strip()
        key = str(node.get(_KEY_FIELD) or "").strip()
        if not pwd and not key:
            raise ValueError("新增节点必须提供 ssh_password 或 ssh_key 之一")
        nodes.append({
            "id": node_id,
            "name": str(node.get("name") or node_id).strip(),
            "host": host,
            "port": int(node.get("port") or 22),
            "user": user,
            _PASSWORD_FIELD: pwd,
            _KEY_FIELD: key,
            "work_dir": str(node.get("work_dir") or "/workspace").strip(),
            "docker_image": str(node.get("docker_image") or "quantmind-oss:latest").strip(),
            "gpus": str(node.get("gpus") or "all").strip(),
        })
    else:
        existing["name"] = str(node.get("name") or existing.get("name") or node_id).strip()
        existing["host"] = host
        existing["port"] = int(node.get("port") or existing.get("port") or 22)
        existing["user"] = str(node.get("user") or existing.get("user") or "root").strip()
        existing["work_dir"] = str(node.get("work_dir") or existing.get("work_dir") or "/workspace").strip()
        existing["docker_image"] = str(
            node.get("docker_image") or existing.get("docker_image") or "quantmind-oss:latest"
        ).strip()
        existing["gpus"] = str(node.get("gpus") or existing.get("gpus") or "all").strip()
        # 密码/密钥留空 = 保持不变
        if node.get(_PASSWORD_FIELD):
            existing[_PASSWORD_FIELD] = str(node[_PASSWORD_FIELD]).strip()
        if node.get(_KEY_FIELD):
            existing[_KEY_FIELD] = str(node[_KEY_FIELD]).strip()

    data["nodes"] = nodes
    _write_yaml(data)
    logger.info("已保存训练节点 %s (is_new=%s)", node_id, is_new)
    return _sanitize_node_for_output(existing or nodes[-1])


def delete_training_node(node_id: str) -> bool:
    """按 id 删除训练节点，返回是否实际删除。"""
    node_id = str(node_id or "").strip()
    if not node_id:
        return False
    data = _load_yaml()
    nodes = data.get("nodes") or []
    remaining = [n for n in nodes if str(n.get("id") or "") != node_id]
    if len(remaining) == len(nodes):
        return False
    data["nodes"] = remaining
    _write_yaml(data)
    logger.info("已删除训练节点 %s", node_id)
    return True


def get_training_node_detail(node_id: str) -> dict[str, Any] | None:
    """获取单个节点的详情（剔除明文密码，返回 has_password/has_key 标记）。"""
    node = get_node_config(node_id)
    if node is None:
        return None
    return _sanitize_node_for_output(node)


def load_training_nodes() -> list[dict[str, Any]]:
    """读取所有 AutoDL 远程节点配置。

    优先 config/training_nodes.yaml；不存在时回退旧的单节点环境变量
    （TRAINING_AUTODL_HOST），保证老部署无缝升级。
    """
    cfg_path = _resolve_config_path()
    if cfg_path:
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            nodes = data.get("nodes") or []
            result = []
            for n in nodes:
                if not n.get("id") or not n.get("host"):
                    continue
                # 认证信息缺省时回退单节点环境变量（docker-compose 传入）
                if not n.get("ssh_password") and not n.get("ssh_key"):
                    n["ssh_password"] = _env_or("TRAINING_AUTODL_SSH_PASSWORD", "")
                    n["ssh_key"] = _env_or("TRAINING_AUTODL_SSH_KEY", "")
                result.append(n)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取训练节点配置失败 %s: %s", cfg_path, exc)

    # 回退：单节点环境变量
    host = _env_or("TRAINING_AUTODL_HOST", "")
    if not host:
        return []
    return [{
        "id": "autodl-1",
        "name": _env_or("TRAINING_AUTODL_NODE_NAME", "AutoDL GPU"),
        "host": host,
        "port": _env_or("TRAINING_AUTODL_SSH_PORT", "22"),
        "user": _env_or("TRAINING_AUTODL_USER", "root"),
        "ssh_password": _env_or("TRAINING_AUTODL_SSH_PASSWORD", ""),
        "ssh_key": _env_or("TRAINING_AUTODL_SSH_KEY", ""),
        "work_dir": _env_or("TRAINING_AUTODL_WORK_DIR", "/workspace"),
        "docker_image": _env_or("TRAINING_AUTODL_DOCKER_IMAGE", "quantmind-oss:latest"),
        "gpus": _env_or("TRAINING_AUTODL_GPUS", "all"),
    }]


def get_node_config(node_id: str) -> dict[str, Any] | None:
    """按 node_id 查节点配置。"""
    for n in load_training_nodes():
        if n["id"] == node_id:
            return n
    return None


class NodeStatus:
    """从 AutoDL 节点采集实时状态（SSH）。"""

    _SSH_TIMEOUT = 15
    # 非交互 SSH 的 PATH 极简(可能无 /usr/bin),前置标准 PATH 防 nvidia-smi/docker 误判
    _COLLECT_CMD = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
set -e
echo "===SYS==="
nproc
uptime
echo "mem:$(free -m | grep -iE 'mem|内存' | awk '{print $2, $3}')"
echo "disk:$(df -P / | awk 'NR==2{print $2, $3}')"
echo "net:$(cat /proc/net/dev | awk '/eth0|ens|enp/{gsub(/:/,\"\"); rx+=$2; tx+=$10} END{print rx, tx}')"
echo "rx1:$(cat /sys/class/net/*/statistics/rx_bytes 2>/dev/null | awk '{s+=$1} END{print s+0}')"
echo "tx1:$(cat /sys/class/net/*/statistics/tx_bytes 2>/dev/null | awk '{s+=$1} END{print s+0}')"
echo "===GPU==="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name --format=csv,noheader,nounits 2>&1 || echo "gpu-error"
else
  echo "no-gpu"
fi
echo "===DOCKER==="
docker ps --filter name=qm-train- --format '{{.Names}}|{{.Status}}' 2>/dev/null || echo "no-docker"
echo "===NET==="
cat /proc/loadavg 2>/dev/null | awk '{print $1}'
"""

    @staticmethod
    def _build_ssh(node: dict[str, Any]) -> list[str]:
        args = []
        if node.get("ssh_password"):
            args += ["sshpass", "-p", node["ssh_password"]]
        args += ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        args += ["-p", str(node.get("port") or 22)]
        if node.get("ssh_key"):
            args += ["-i", node["ssh_key"]]
        args.append(f"{node.get('user') or 'root'}@{node['host']}")
        return args

    @classmethod
    async def collect(cls, node: dict[str, Any]) -> dict[str, Any]:
        """SSH 采集节点状态。失败时返回 offline 标记，不抛错。"""
        result: dict[str, Any] = {
            "id": node.get("id"),
            "name": node.get("name") or node.get("id"),
            "host": node.get("host"),
            "online": False,
        }
        # 免 Docker 节点(executor=process):探测包内 runtime/GPU/活跃训练任务
        if str(node.get("executor") or "").lower() == "process":
            pack_root = str(node.get("pack_root") or "")
            runtime = str(node.get("runtime_python") or "").strip() or (
                f"{pack_root}/runtime/bin/python3" if pack_root else ""
            )
            work_dir = str(node.get("work_dir") or "")
            if not runtime:
                result["error"] = "executor=process 节点缺少 pack_root/runtime_python"
                return cls.assess_readiness(result)
            # GPU 探测与容器版同款(PATH 前置防极简 PATH 漏 nvidia-smi);
            # 活跃任务 = 该节点 work_dir 下正在跑的 train.py(进程级,非 docker ps)
            probe = (
                "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH; "
                "echo ===SYS===; "
                f"{shlex.quote(runtime)} --version 2>&1 | head -1; nproc; "
                "echo ===GPU===; "
                "if command -v nvidia-smi >/dev/null 2>&1; then "
                "  nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name "
                "    --format=csv,noheader,nounits 2>&1 || echo gpu-error; "
                "else echo no-gpu; fi; "
                "echo ===DOCKER===; "
            )
            if work_dir:
                probe += (
                    f"if ps -ef | grep -v grep | grep -q 'train.py --config {shlex.quote(work_dir)}/'; "
                    "then echo 'local-run|running'; else echo no-docker; fi; "
                )
            else:
                probe += "echo no-docker; "
            probe += "echo ===NET===; cat /proc/loadavg 2>/dev/null | awk '{print $1}'"
            proc = await asyncio.create_subprocess_exec(
                *cls._build_ssh(node),
                probe,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cls._SSH_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                result["error"] = "SSH 连接超时"
                return cls.assess_readiness(result)
            if proc.returncode not in (0, None):
                result["error"] = (stderr or stdout).decode(errors="replace")[:200]
                return cls.assess_readiness(result)
            out = stdout.decode(errors="replace")
            if "Python" not in out:
                result["error"] = "包内 runtime python 探测失败"
                return cls.assess_readiness(result)
            # 与容器版同走 _parse:GPU 列表/活跃任务/readiness 标签统一解析
            return cls._parse(out, result)
        proc = await asyncio.create_subprocess_exec(
            *cls._build_ssh(node),
            cls._COLLECT_CMD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cls._SSH_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            result["error"] = "SSH 连接超时"
            return cls.assess_readiness(result)

        if proc.returncode not in (0, None):
            err_msg = (stderr.decode(errors="replace") if stderr else "").strip()
            if "Permission denied" in err_msg:
                result["error"] = "SSH 密码/密钥认证失败"
            elif "Connection refused" in err_msg:
                result["error"] = "连接被拒绝 (主机未开机或端口错误)"
            elif "Could not resolve hostname" in err_msg or "Name or service not known" in err_msg:
                result["error"] = "主机名无法解析"
            elif "No route to host" in err_msg or "Host is down" in err_msg:
                result["error"] = "主机不可达 (已关机)"
            else:
                result["error"] = err_msg or f"SSH 连接失败 (code={proc.returncode})"
            return cls.assess_readiness(result)

        out = stdout.decode(errors="replace")
        return cls._parse(out, result)

    @staticmethod
    def _parse(out: str, result: dict[str, Any]) -> dict[str, Any]:
        result["online"] = True
        sections: dict[str, str] = {}
        current = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("===") and line.endswith("==="):
                current = line.strip("=")
                sections[current] = ""
            elif current is not None:
                sections[current] = (sections[current] + "\n" + line).strip()

        # CPU 核数 + loadavg
        sys_text = sections.get("SYS", "")
        sys_lines = [l for l in sys_text.splitlines() if l]
        if sys_lines:
            result["cpu_cores"] = int(sys_lines[0]) if sys_lines[0].isdigit() else None
        # loadavg（NET 段取了第 1 个值）
        load_txt = sections.get("NET", "").strip()
        result["cpu_load"] = float(load_txt) if load_txt.replace(".", "", 1).isdigit() else None

        # 内存 mem:total used（free -m，MB）
        mem_line = next((l for l in sys_lines if l.startswith("mem:")), None)
        if mem_line:
            parts = mem_line.split(":")
            if len(parts) >= 2:
                vals = parts[1].split()
                if len(vals) >= 2:
                    result["mem_total_mb"] = int(vals[0]) if vals[0].isdigit() else None
                    result["mem_used_mb"] = int(vals[1]) if vals[1].isdigit() else None

        # 硬盘 disk:total used（df -P /，KB）
        disk_line = next((l for l in sys_lines if l.startswith("disk:")), None)
        if disk_line:
            parts = disk_line.split(":")
            if len(parts) >= 2:
                vals = parts[1].split()
                if len(vals) >= 2:
                    result["disk_total_kb"] = int(vals[0]) if vals[0].isdigit() else None
                    result["disk_used_kb"] = int(vals[1]) if vals[1].isdigit() else None

        # 网络累计收发（字节）——用于前端计算速率
        rx_line = next((l for l in sys_lines if l.startswith("rx1:")), None)
        tx_line = next((l for l in sys_lines if l.startswith("tx1:")), None)
        result["net_rx_bytes"] = int(rx_line.split(":")[1]) if rx_line and rx_line.split(":")[1].strip().isdigit() else None
        result["net_tx_bytes"] = int(tx_line.split(":")[1]) if tx_line and tx_line.split(":")[1].strip().isdigit() else None

        # GPU nvidia-smi（若驱动异常则记录原因）
        gpu_lines = [l for l in sections.get("GPU", "").splitlines() if l]
        gpu_list = []
        if gpu_lines and gpu_lines[0] not in ("no-gpu", "gpu-error"):
            for l in gpu_lines:
                parts = [p.strip() for p in l.split(",")]
                if len(parts) >= 4:
                    gpu_list.append({
                        "util": int(parts[0]) if parts[0].isdigit() else 0,
                        "mem_used_mb": int(parts[1]) if parts[1].isdigit() else 0,
                        "mem_total_mb": int(parts[2]) if parts[2].isdigit() else 0,
                        "temp_c": int(parts[3]) if parts[3].isdigit() else 0,
                        "name": parts[4] if len(parts) > 4 else "",
                    })
        result["gpus"] = gpu_list
        if not gpu_list:
            # 记录 GPU 不可用原因
            if gpu_lines and gpu_lines[0] == "gpu-error":
                err = " ".join(gpu_lines[1:]).strip()
                result["gpu_error"] = err or "nvidia-smi 驱动异常"
            elif gpu_lines and gpu_lines[0] == "no-gpu":
                result["gpu_error"] = "未安装 nvidia-smi"
            else:
                result["gpu_error"] = "未检测到 GPU"

        # Docker 训练容器
        docker_lines = [l for l in sections.get("DOCKER", "").splitlines() if l and l != "no-docker"]
        containers = []
        for l in docker_lines:
            if "|" in l:
                name, status = l.split("|", 1)
                containers.append({"name": name.strip(), "status": status.strip()})
        result["containers"] = containers
        result["training_active"] = bool(containers)

        # 网络延迟：ping 一次网关（尽力而为）
        result["ping_ms"] = None
        # 注意：_parse 是 @staticmethod，不能用 cls（曾致 NameError → 远程节点永远 offline）
        result = NodeStatus.assess_readiness(result)
        return result

    @classmethod
    async def collect_local(cls) -> dict[str, Any]:
        """采集本地节点（Local Docker / 宿主机）实时状态。"""
        import shutil
        import subprocess

        result: dict[str, Any] = {
            "id": "local",
            "name": "本地 Docker",
            "type": "local",
            "host": "localhost",
            "online": True,
            "is_local": True,
            "docker_available": False,
            "containers": [],
            "gpus": [],
        }

        # 1. 探测 CPU 与内存
        try:
            import psutil
            result["cpu_cores"] = psutil.cpu_count(logical=True)
            mem = psutil.virtual_memory()
            result["mem_total_mb"] = int(mem.total / (1024 * 1024))
            result["mem_used_mb"] = int(mem.used / (1024 * 1024))
            result["cpu_load"] = round(psutil.cpu_percent(interval=None) / 100.0, 2)
        except Exception:
            result["cpu_cores"] = os.cpu_count() or 1

        # 2. 探测磁盘空间
        try:
            disk_path = "/data" if Path("/data").exists() else "."
            usage = shutil.disk_usage(disk_path)
            result["disk_total_kb"] = int(usage.total / 1024)
            result["disk_used_kb"] = int(usage.used / 1024)
        except Exception:
            pass

        # 3. 探测本地执行环境：Docker daemon（训练镜像 + qm-train-* 容器），
        #    或便携包等免 Docker 部署的本机 python 直跑条件
        # 与 local_docker_orchestrator 的 _TRAINING_IMAGE 保持一致（TRAINING_IMAGE 环境变量，
        # .env 可配置）；只检查硬编码的 quantmind-trainer:latest 会在自定义训练镜像
        # （如 quantmind-oss-gpu:latest）时误报"未安装"，导致前端禁止开始训练。
        training_image = (os.getenv("TRAINING_IMAGE") or "quantmind-trainer:latest").strip()
        result["training_image"] = training_image
        result["image_installed"] = False
        result["executor"] = "docker"

        from backend.shared.training_runtime import resolve_training_executor

        probe = await asyncio.to_thread(resolve_training_executor)
        if probe.get("executor") == "process":
            # 免 Docker 部署：本机 python 直跑 train.py（镜像/容器探测无意义）
            result["executor"] = "process"
            result["docker_available"] = False
            result["process_ready"] = bool(probe.get("script")) and not bool(probe.get("missing"))
            result["process_script"] = probe.get("script")
            result["node_name"] = "本地直跑(免Docker)"
            result["node_description"] = "本机 python 进程直跑训练（无需 Docker）"
            if not result["process_ready"]:
                result["docker_error"] = (
                    "Docker daemon 未运行，且本机直跑条件不齐"
                    f"（train.py: {'存在' if probe.get('script') else '缺失'}；"
                    f"依赖缺失: {', '.join(probe.get('missing') or []) or '无'}）"
                )
        else:
            try:
                from docker import DockerClient
                client = await asyncio.to_thread(DockerClient.from_env)
                await asyncio.to_thread(client.ping)
                result["docker_available"] = True
                result["docker_error"] = None

                # 校验是否已安装实际配置的训练容器镜像（TRAINING_IMAGE）
                try:
                    await asyncio.to_thread(client.images.get, training_image)
                    result["image_installed"] = True
                except Exception:
                    result["image_installed"] = False

                # 检查是否有 qm-train-* 容器
                all_containers = await asyncio.to_thread(client.containers.list, all=True)
                train_containers = []
                for c in all_containers:
                    if c.name and c.name.startswith("qm-train-"):
                        train_containers.append({"name": c.name, "status": c.status})
                result["containers"] = train_containers
                result["training_active"] = any(c["status"] == "running" for c in train_containers)
            except Exception as docker_err:
                result["docker_available"] = False
                result["docker_error"] = str(docker_err)

        # 4. 探测本地 GPU (nvidia-smi / torch)
        gpus: list[dict[str, Any]] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0 and stdout:
                for line in stdout.decode(errors="replace").splitlines():
                    parts = [p.strip() for p in line.split(",") if p.strip()]
                    if len(parts) >= 5:
                        gpus.append({
                            "util": int(parts[0]) if parts[0].isdigit() else 0,
                            "mem_used_mb": int(parts[1]) if parts[1].isdigit() else 0,
                            "mem_total_mb": int(parts[2]) if parts[2].isdigit() else 0,
                            "temp_c": int(parts[3]) if parts[3].isdigit() else 0,
                            "name": parts[4],
                        })
        except Exception:
            pass

        if not gpus:
            try:
                import torch
                if torch.cuda.is_available():
                    for i in range(torch.cuda.device_count()):
                        props = torch.cuda.get_device_properties(i)
                        gpus.append({
                            "util": 0,
                            "mem_used_mb": int(torch.cuda.memory_allocated(i) / (1024 * 1024)),
                            "mem_total_mb": int(props.total_memory / (1024 * 1024)),
                            "temp_c": 0,
                            "name": props.name,
                        })
            except Exception:
                pass

        # Docker-in-Docker：api 容器本身未挂载 GPU（无 nvidia-smi / 驱动），
        # 但宿主机 daemon 可能已配置 nvidia runtime（训练容器以 --gpus all 启动）。
        # 用训练镜像临时起一个容器实测 GPU，避免把真实 GPU 机器误报成 CPU 训练模式。
        if not gpus and result.get("docker_available") and result.get("image_installed"):
            gpus = await cls._probe_gpu_via_docker(client, training_image)

        result["gpus"] = gpus
        if not gpus:
            result["gpu_error"] = "未检测到独立 GPU (将使用 CPU 训练)"

        result = cls.assess_readiness(result)
        return result

    # GPU 容器探测 TTL 缓存（秒）：避免前端轮询时频繁起探针容器
    _gpu_probe_cache: tuple[float, list[dict[str, Any]]] | None = None
    _GPU_PROBE_TTL_SEC = 60

    @classmethod
    async def _probe_gpu_via_docker(
        cls, client: Any, image: str
    ) -> list[dict[str, Any]]:
        """通过宿主机 daemon 起一个带 --gpus all 的一次性容器执行 nvidia-smi。

        返回与本地探测相同结构的 GPU 列表；探测失败或宿主机不支持 GPU 时返回空列表。
        """
        import time

        now = time.monotonic()
        if cls._gpu_probe_cache and now - cls._gpu_probe_cache[0] < cls._GPU_PROBE_TTL_SEC:
            return cls._gpu_probe_cache[1]

        gpus: list[dict[str, Any]] = []
        try:
            import docker as docker_sdk

            out = await asyncio.to_thread(
                client.containers.run,
                image,
                # 覆盖镜像 ENTRYPOINT（默认训练镜像为 python /app/train.py）：
                # 否则 nvidia-smi 命令会被 train.py 当作 CLI 参数吞掉，
                # config 缺失秒退，探针永远返回空 → GPU 机器被误判为 CPU。
                entrypoint=["nvidia-smi"],
                command=[
                    "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                    "--format=csv,noheader,nounits",
                ],
                device_requests=[
                    docker_sdk.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                ],
                remove=True,
            )
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            for line in str(out).splitlines():
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if len(parts) >= 5:
                    gpus.append({
                        "util": int(parts[0]) if parts[0].isdigit() else 0,
                        "mem_used_mb": int(parts[1]) if parts[1].isdigit() else 0,
                        "mem_total_mb": int(parts[2]) if parts[2].isdigit() else 0,
                        "temp_c": int(parts[3]) if parts[3].isdigit() else 0,
                        "name": parts[4],
                    })
        except Exception as exc:
            logger.warning("docker GPU probe failed: %s", exc)
        cls._gpu_probe_cache = (now, gpus)
        return gpus

    @staticmethod
    def assess_readiness(status: dict[str, Any]) -> dict[str, Any]:
        """根据采集的状态综合评估节点就绪度并生成摘要。"""
        if not status.get("online", False):
            status["readiness"] = "offline"
            status["readiness_label"] = "离线 / 未连接"
            status["status_desc"] = status.get("error") or "无法建立通信连接"
            return status

        # 本地模式：先检查执行环境（Docker 引擎+训练镜像，或免 Docker 本机直跑）
        if status.get("is_local"):
            if status.get("executor") == "process":
                if not status.get("process_ready"):
                    # 用 offline 而非 warning：前端 warning 分支展示的是
                    # "docker build 训练镜像" 提示，与本机直跑场景不符；
                    # offline 分支直接展示下方的详细原因文案
                    status["readiness"] = "offline"
                    status["readiness_label"] = "直跑环境未就绪"
                    status["status_desc"] = (
                        status.get("docker_error")
                        or "本机直跑条件不齐（训练脚本或运行时依赖缺失）"
                    )
                    return status
                # 直跑模式不要求 Docker：跳过镜像检查，直接评估 GPU / 资源
            else:
                if not status.get("docker_available"):
                    status["readiness"] = "offline"
                    status["readiness_label"] = "Docker 未运行"
                    status["status_desc"] = f"未连接到 Docker: {status.get('docker_error') or '服务未运行'}"
                    return status
                if not status.get("image_installed"):
                    status["readiness"] = "warning"
                    status["readiness_label"] = "待安装训练镜像"
                    status["status_desc"] = f"未安装独立训练镜像 {status.get('training_image', 'quantmind-trainer:latest')} (需先构建/拉取)"
                    return status

        # 检查是否训练中
        if status.get("training_active"):
            running_cnt = sum(1 for c in status.get("containers", []) if c.get("status") == "running")
            status["readiness"] = "busy"
            status["readiness_label"] = "训练中"
            status["status_desc"] = f"正在执行 {running_cnt or 1} 个训练任务"
            return status

        # 检查 GPU 与系统资源
        gpus = status.get("gpus") or []
        disk_total_kb = status.get("disk_total_kb") or 0
        disk_used_kb = status.get("disk_used_kb") or 0
        disk_free_gb = round((disk_total_kb - disk_used_kb) / (1024 * 1024), 1) if disk_total_kb > 0 else 0
        status["disk_free_gb"] = disk_free_gb

        if gpus:
            first_gpu = gpus[0]
            gpu_name = first_gpu.get("name") or "GPU"
            total_vram_gb = round(first_gpu.get("mem_total_mb", 0) / 1024, 1)
            used_vram_gb = round(first_gpu.get("mem_used_mb", 0) / 1024, 1)
            free_vram_gb = max(0.0, round(total_vram_gb - used_vram_gb, 1))

            status["gpu_summary"] = f"{gpu_name} ({total_vram_gb}GB)"
            status["readiness"] = "ready"
            status["readiness_label"] = "已就绪"
            status["status_desc"] = f"{gpu_name} · 显存余 {free_vram_gb}GB · 磁盘可用 {disk_free_gb}GB"
        else:
            status["gpu_summary"] = "CPU (无独立 GPU)"
            status["readiness"] = "ready"
            status["readiness_label"] = "已就绪 (CPU)"
            mem_mb = status.get("mem_total_mb") or 0
            mem_str = f" · 内存 {round(mem_mb / 1024, 1)}GB" if mem_mb > 0 else ""
            status["status_desc"] = f"CPU 训练模式{mem_str} · 磁盘可用 {disk_free_gb}GB"

        # 直跑模式补充标注（与 Docker 容器训练区分，前端按状态文案展示）
        if status.get("is_local") and status.get("executor") == "process":
            desc = str(status.get("status_desc") or "").strip()
            if desc and "本机直跑" not in desc:
                status["status_desc"] = desc + " · 本机直跑"

        # 如果磁盘不足 5GB 则 warning
        if disk_total_kb > 0 and disk_free_gb < 5.0:
            status["readiness"] = "warning"
            status["readiness_label"] = "磁盘空间不足"
            status["status_desc"] = f"磁盘剩余不足 5GB (仅剩 {disk_free_gb}GB)"

        return status

    @classmethod
    async def collect_all(cls) -> list[dict[str, Any]]:
        """并发采集所有节点（本地 + AutoDL 远程节点）状态。"""
        nodes = load_training_nodes()
        tasks = [cls.collect_local()]
        for n in nodes:
            tasks.append(cls.collect(n))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, dict):
                out.append(r)
            elif isinstance(r, Exception):
                logger.warning("采集节点状态异常: %s", r)
        return out

