# QuantMind 远程训练节点部署教程(AutoDL / 云服务器)

> 适用:本机(主节点)训练中心 → 远程 GPU 机器(AutoDL 容器实例 / 自备 Linux 服务器)跑训练,
> 产物自动回传本机注册。全程**免 Docker**(服务器只需 Python runtime 节点包)。
> 验证环境:AutoDL RTX 2080 Ti + `qm-train-node` 节点包(2026-09-06)。

```
本机(主节点 QuantMind)                 远程 GPU 机器
训练中心 → AutoDL 节点(填 SSH)  ⇄ SSH ⇄  节点包(内嵌 runtime + 训练代码 + 因子同步)
  ├─ 直读因子训练提交                      ├─ sync_factors.sh <市场> 自同步 6_ml_datasets 因子
  ├─ 轮询日志(TrainingRunLogStream)        └─ runtime python 跑 train.py(--gpus 无关,直吃驱动)
  └─ 完成后 rsync 拉产物 → 注册进模型管理
```

---

## 一、远程机器准备(AutoDL 容器实例 Pro)

1. **租实例**:AutoDL 控制台 → 容器实例 → 新建。规格按需(GPU 附录见 `autodl` skill);
   基础镜像选任一 Miniconda 公共镜像即可(训练不依赖镜像自带框架)
2. **开机**后,控制台实例页复制 **SSH 登录命令与 root 密码**(形如
   `ssh -p 49470 root@region-42.seetacloud.com`;**密码每次开机可能轮换**)
3. 数据盘:`/root/autodl-tmp`(50G,存数据/产物/待传文件,关机保留、释放删除);
   系统盘 `/`(30G,装程序);文件存储 `/root/autodl-fs`(200G,网页上传中转)

## 二、部署节点包(一次性,约 5 分钟)

### 1. 上传包到实例

- **CPU 版**(每台都通用,GPU 需另装):`qm-train-node-<date>.tar.gz`(本机
  `deploy/portable/dist/`)
- **GPU 版**(已含 cu128 runtime,**新实例推荐直接用这个**):在实例数据盘或网盘,
  如 `qm-train-node-gpu-20260906.tar.gz`(4.7G,由已验证 GPU 实例打包)

放到 `/root/autodl-tmp/` 后:

```bash
# 建议装到系统盘 /root(autodl-tmp 属数据盘,实例释放会丢)
mv /root/autodl-tmp/qm-train-node-gpu-20260906.tar.gz /root/   # 若在 tmp
cd /root && tar xzf qm-train-node-gpu-20260906.tar.gz
cd qm-train-node && bash start_node.sh     # 自检:runtime 版本 / 依赖 / train.py / GPU
```

自检出现 `torch ... cuda: True`(GPU 版)即就绪。
**CPU 版想变 GPU**:两种方式——
① `pip install`(需联网):`runtime/python/bin/python3 -m pip install --force-reinstall --index-url https://download.pytorch.org/whl/cu128 "torch==2.9.1"`
② 离线 GPU 增补包:把 `QuantMind-Portable-gpu-addon-linux-x64.tar.gz` 解到包根 → `bash install_gpu.sh`

### 2. 配置数据与环境

```bash
cd /root/qm-train-node
vim train_env.sh
# 填/改: QUANTDB_API_KEY=<平台key>          (构建时若带 key 已内置)
#        QM_QUANTDB_DATA_DIR=/root/autodl-tmp/qm-data/quantdb   (数据盘,大文件放这)
```

### 3. 装主节点 SSH 公钥(一次,之后免密)

主节点本机:`ssh-copy-id -p <port> root@<host>`(输一次实例密码)。
之后本机/容器用密钥即可,节点配置不用存密码。

### 4. 数据就绪(二选一)

- **自同步**:`bash sync_factors.sh CN`(自动下载 l1/l2/l1_l2 因子到数据根;
  训练前编排器也会自动触发;HK/US 分支待扩展)
- **离线导入**:把 `6_ml_datasets/{l1_factors,l2_factors,l1_l2_factors}`(按 `dt=YYYYMMDD`
  分区)放到 `$QM_QUANTDB_DATA_DIR/6_ml_datasets/` 对应目录(注意层级)

## 三、主节点注册远程节点

编辑主节点 `config/training_nodes.yaml`(该文件已 gitignore,勿提交):

```yaml
nodes:
  - id: autodl-gpu-1            # 节点唯一标识
    name: "AutoDL 2080Ti"       # 训练中心显示名
    host: "region-42.seetacloud.com"
    port: 49470
    user: "root"
    ssh_key: "/root/.ssh/id_ed25519"   # 主节点容器内的私钥路径(宿主机 ~/.ssh 需 docker cp 进容器)
    executor: "process"                # 免 Docker:ssh + runtime python 直跑
    pack_root: "/root/qm-train-node"   # 实例上节点包根
    runtime_python: "/root/qm-train-node/runtime/python/bin/python3"
    env_file: "train_env.sh"           # 包内环境文件(自动 source)
    work_dir: "/root/qm-train-node/workspace"
    quantdb_dir: "/root/autodl-tmp/qm-data/quantdb"
```

> 备注:若私钥在主节点宿主机 `~/.ssh`,容器内编排器看不到宿主路径——把私钥
> `docker cp` 进容器 `/root/.ssh/` 并在配置里写容器内路径。

验证(管理端):训练中心/后台 → 节点应显示 **AutoDL 2080Ti | 已就绪 | RTX 2080 Ti(11GB)**。

## 四、发起远程训练(与本地完全同操作)

1. 进「训练中心」→ 节点选 **AutoDL 2080Ti**
2. 数据源选「直读因子」+ 数据集(l1_factors / l2_factors / l1_l2_factors + 新增自动识别)
3. 模型/参数/标签/周期照常(13 模型全支持;GPU 机 DL 模型走 CUDA)
4. 点开始训练 → 日志实时回流 → 完成后**产物自动 rsync 回本机并注册进模型管理**
   (回传位置 `/data/training_jobs/<run_id>/`,注册与本地训练同一链路)

支持:多周期(逐周期跑)、直读 CN/HK/US(process 节点放开市场守卫,数据由包侧同步)、
Optuna 寻优(镜像/包 runtime 已含)、断点续跑由主节点 DB 状态驱动(远端进程 setsid 不中断)。

## 五、日常运维

- **看状态**:`bash .claude/skills/autodl/scripts/autodl.sh status <uuid>`
- **关机**:训练完 `autodl.sh off <uuid>`(按量计费,别白跑);长期不用先关再 `release`
- **同环境复用**:在实例上配好环境后 `autodl.sh save <uuid> "qm-train-node-gpu"`,
  下次用私有镜像开新实例秒级同环境;或直接复用 GPU 版节点包 tar
- **数据增量**:`sync_factors.sh CN` 幂等,只补缺失 `dt` 分区;本地新数据不影响远端
- **迁移/备份**:实例释放前把 `qm-train-node-gpu-*.tar.gz` 传网盘/AutoDL 文件存储留存

## 六、常见问题

| 现象 | 处理 |
|---|---|
| 节点显示「未连接/offline」 | 实例是否开机?SSH 端口/密码是否变了(每次开机可能轮换);主节点私钥是否在容器内路径 |
| 节点显示 CPU(明明有 GPU) | 早期版本 bug,已修(2026-09-06 后拉最新);或 CUDA torch 未装(见第二节) |
| 同步报 `can't open file /root/backend/...` | 旧包 bug,用 r2/新版包(已固化 cd 包根);或手动 `cd /root/qm-train-node && bash sync_factors.sh CN` |
| torch 装成 CPU 版 | pip 源把 `2.9.1+cpu` 判为满足 `==2.9.1`:加 `--force-reinstall --index-url .../whl/cu128` |
| 训练卡 provisioning 很久 | 首次因子同步(几十 GB)在跑;之后只有增量 |
| 训练中主节点重启 | 远端 setsid 任务继续跑,产物留在实例 workspace;恢复后可手动 rsync + 补注册 |
| 端口/地址 404 | 走主节点管理 API 前缀 `/api/v1/admin/models/training-nodes` |

## 七、相关文件

> 本地主节点为一键启动包?见《一键启动包远程训练教程》`docs/remote-training-from-portable-pack.md`


- 节点包构建:本机 `scripts/setup/build_node_pack.sh`(输出 `deploy/portable/dist/qm-train-node-*.tar.gz`)
- 编排代码:`backend/services/engine/training/remote_ssh_orchestrator.py`(executor=process 分支)
- 节点探测:`backend/services/engine/training/node_manager.py`
- AutoDL 实例 API:`~/.claude/skills/autodl/`(SKILL.md + scripts/autodl.sh)
