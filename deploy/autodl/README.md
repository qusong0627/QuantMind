# AutoDL 免 Docker 训练节点部署

在 AutoDL 的 Python 容器上部署免 docker 直跑训练（`RemoteSSHOrchestrator` 的 `native_python` 执行模式）。

## 背景

AutoDL 显卡实例默认是 Python 容器（有 torch + GPU，但**没有 docker**）。项目训练链路支持两种执行模式：

| 模式 | 节点形态 | 脚本 |
|------|----------|------|
| `ssh_docker`（默认） | 已装 docker + nvidia-container-toolkit + 训练镜像 | `scripts/setup/build-autodl-remote.sh` |
| `native_python`（免 docker） | 纯 AutoDL Python 容器 | **本目录 `setup-autodl-native.sh`** |

> AutoDL 自定义镜像**不能跨账户共享**，因此不采用「固化镜像」交付，改用「节点现场安装依赖」。
> 代码（train.py / training 包 / backend 直读子树）不入镜像，由编排器每次 rsync 流式推送。

## 快速开始

开通带 GPU 的 AutoDL Python 实例，SSH 登录后在 **AutoDL 本机**（不是主节点）执行。

### 方式一：一键下载执行（推荐）

无需先 `scp` 脚本，直接 curl 拉取即跑（保留 stdin，可交互）：

```bash
bash -c "$(curl -fsSL https://quantmindai.cn/gitea/qusong0627/QuantMind/raw/branch/master/deploy/autodl/quick-setup.sh)"
```

非交互（CI / 管道执行，需带齐环境变量）：

```bash
QUANTDB_API_KEY=qdb_xxx AUTO_DL=yes AUTODL_SINCE=2024-01-01 AUTODL_DATASETS=l1_factors \
  bash -c "$(curl -fsSL https://quantmindai.cn/gitea/qusong0627/QuantMind/raw/branch/master/deploy/autodl/quick-setup.sh)"
```

> `quick-setup.sh` 是下载器，内部自动拉取并执行 `setup-autodl-native.sh`。
> 可用 `QUANTMIND_REF`（分支/tag）、`QUANTMIND_RAW_BASE`（raw 根地址）、
> `QUANTMIND_SETUP_SHA256`（脚本校验）覆盖下载行为。

### 方式二：先上传再执行

```bash
scp -P <端口> deploy/autodl/setup-autodl-native.sh root@connect.xxx.seetacloud.com:/root/
ssh -p <端口> root@connect.xxx.seetacloud.com
bash /root/setup-autodl-native.sh
```

非交互：

```bash
QUANTDB_API_KEY=qdb_xxx AUTO_DL=yes AUTODL_SINCE=2024-01-01 AUTODL_DATASETS=l1_factors \
  bash setup-autodl-native.sh
```

已有数据仍做增量：

```bash
AUTODL_RESYNC=1 QUANTDB_API_KEY=qdb_xxx bash setup-autodl-native.sh
```

脚本会：检测 Python / GPU；安装训练依赖（缺啥装啥，pandas 钉在 2.x）；创建 `/root/workspace` 与 `/root/autodl-fs/quantdb`；（可选）写入 API Key；按选择自动同步 / 增量 / 跳过并提示手动上传。

## 数据集

训练直读 QuantDB 因子 parquet，放 **数据盘** `/root/autodl-fs/quantdb`（重启不丢；系统盘 `/` 会清）。

| 方式 | 何时 | 行为 |
|------|------|------|
| 自动同步 | 空盘默认，或 `AUTO_DL=yes` | `quantdb-sdk` 增量拉取，默认 **近 3 年**、仅 **`l1_factors`**（可用 `AUTODL_SINCE` / `AUTODL_DATASETS` 改） |
| 增量 | 已有数据后选 Y，或 `AUTODL_RESYNC=1` | 同样走 SDK，已下载分区会匹配跳过 |
| 手动 | 选 N / `AUTO_DL=no` | 自行把 `6_ml_datasets/` 传到 `/root/autodl-fs/quantdb/6_ml_datasets/` |
| 跳过 | 已有数据默认，或 `AUTO_DL=skip` | 不下载 |

日常开训时，主节点编排器还会再跑 `quantdb_daily_sync.py --parquet-only`（与本脚本独立）。

环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `QUANTDB_API_KEY` | 无 | **可选**；自动/增量同步时必填，不填则跳过数据源配置（手动上传） |
| `AUTO_DL` | 空盘 yes / 有数据 skip | `yes` / `no` / `skip` |
| `AUTODL_RESYNC` | 0 | `1` 时已有数据仍增量 |
| `AUTODL_SINCE` | `3-year` | `YYYY-MM-DD` 或 `full` |
| `AUTODL_DATASETS` | `l1_factors` | 逗号分隔，如 `l1_factors,l2_factors` |
| `PIP_INDEX` | 清华 PyPI | 国内源，可用 `PIP_INDEX` 改阿里云等 |

## 环境变量持久化

Key 写入（格式统一为 `export QUANTDB_API_KEY=...`）：

- `/etc/profile.d/quantmind_sh.sh`
- `~/.bashrc`
- `/root/workspace/.env`（编排器非登录 SSH 不一定 source profile.d，开训同步由主节点注入 Key）

验证：

```bash
set -a; . /root/workspace/.env; set +a
/root/miniconda3/bin/python -c "import lightgbm, duckdb, quantdb_sdk; print('deps OK')"
nvidia-smi
du -sh /root/autodl-fs/quantdb/6_ml_datasets
```

## 在项目侧注册节点

编辑主节点 `config/training_nodes.yaml`（gitignore，含凭证不入库）。**推荐 `native_python`**，不要用 AutoDL 再套一层 Docker。

```yaml
nodes:
  - id: autodl-rtx4090
    name: "AutoDL RTX4090 (免Docker)"
    host: "connect.xxx.seetacloud.com"
    port: <控制台 SSH 端口，重启实例会变>
    user: "root"
    ssh_key: "/root/.ssh/id_ed25519"   # 或 ssh_password
    work_dir: "/root/workspace"
    exec_mode: "native_python"
    gpus: "all"
    quantdb_dir: "/root/autodl-fs/quantdb"
```

主节点 `.env`：

```bash
TRAINING_MASTER_HOST=<协调机公网IP>   # AutoDL 回调 API，勿填 127.0.0.1
```

改完后 `docker compose restart quantmind`。密码 SSH 需要容器内有 `sshpass`。

端到端（架构、开训、排障）见 **[docs/部署指南.md 第十一节](../../docs/部署指南.md)**。

## 验证

节点侧见上方命令。主节点提交 `node_id=该节点id` 的小训练后应看到：原生进程启动、产物回传到 `/data/training_jobs/{run_id}/`、模型写入 `qm_user_models`。

## 文件

- `quick-setup.sh` — 一键下载器（curl 拉取并执行 `setup-autodl-native.sh`）
- `setup-autodl-native.sh` — 节点初始化（交互 + 非交互）
