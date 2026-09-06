# 一键启动包远程训练教程(本地=便携包,远程=AutoDL 节点包)

> 场景:本地主节点跑的是 **QuantMind 一键启动包**(免 Docker,Linux/WSL2),
> 远程 AutoDL GPU 实例跑 **训练节点包**——配置与 Docker 部署**完全一致**,
> 本文只写一键包视角的差异点与步骤。

```
[本地] 一键启动包(训练中心)          [远程] AutoDL 实例(节点包)
config/training_nodes.yaml  ⇄ SSH ⇄  /root/qm-train-node(runtime+backend+sync)
训练中心:直读因子训练                 自动:sync_factors.sh CN / runtime python train.py
   └─ 产物 rsync 回传 → 注册进模型管理(与本机训练同一链路)
```

## 〇、前提

- 本地一键包为 **Linux/WSL2 版**,且已同步最新代码(2026-09-06 后,含
  `executor=process` 支持):包根 `sync_from_git.sh` 拉一次 next 并重启
- **Windows 一键包暂不支持远程节点**(编排器依赖系统 rsync/sshpass,Windows 缺;
  待适配版发布前请用 Linux/WSL2 包或 Docker 部署做主节点)
- 远程机器已按《远程训练节点部署教程》装好节点包并自检通过

## 一、远程机器 SSH 公钥(本机包视角)

远程节点装包时把本机公钥装好(一次):
```bash
# 在跑一键包的这台机器上执行
ssh-copy-id -p <端口> root@<远程host>
```
之后全程免密。**私钥路径写一键包所在机器的绝对路径**(无容器隔离,不需 docker cp):
```
ssh_key: "/home/<用户>/.ssh/id_ed25519"
```

## 二、本地一键包配置远程节点

编辑**一键包根目录**的 `config/training_nodes.yaml`(不存在就新建,内容照抄改字段):

```yaml
nodes:
  - id: autodl-gpu-1
    name: "AutoDL 2080Ti"
    host: "region-42.seetacloud.com"     # 实例控制台给的 SSH 域名
    port: 49470
    user: "root"
    ssh_key: "/home/<用户>/.ssh/id_ed25519"
    executor: "process"
    pack_root: "/root/qm-train-node"          # 远程节点包根(系统盘)
    runtime_python: "/root/qm-train-node/runtime/python/bin/python3"
    env_file: "train_env.sh"
    work_dir: "/root/qm-train-node/workspace"
    quantdb_dir: "/root/autodl-tmp/qm-data/quantdb"   # 远程数据盘
```

保存后**重启一键包**(或重启其 api 子进程),使节点配置生效。

## 三、验证与训练(操作与本地训练零差异)

1. 网页进「训练中心」→ 节点区出现 **AutoDL 2080Ti**
2. 节点应显示「已就绪」与 GPU 型号(RTX 2080 Ti 11GB)
   - 若显示「未连接」:实例是否开机/私钥路径对不对/是否同步了最新代码
3. 选节点 → 数据源「直读因子」→ 数据集 → 模型/参数照常 → 开始训练
4. 日志实时回流;完成后产物自动回传本机 `data/training_jobs/<run_id>/`
   并注册进模型管理(可回测/推理/做策略信号)

## 四、一键包 vs Docker 部署差异速查

| 项 | Docker 部署 | 一键包(Linux) | 说明 |
|---|---|---|---|
| 节点配置文件 | 仓库 `config/training_nodes.yaml` | **包根 `config/training_nodes.yaml`** | 字段一致 |
| SSH 私钥 | 需 docker cp 进容器,写容器内路径 | 直接写本机路径 | 免 Docker 无隔离 |
| 训练中心/注册 | 同 | **同** | 同一套代码 |
| 产物落盘 | `/data/training_jobs/` | `data/training_jobs/`(包内) | 同结构 |
| 重启恢复 | api 子进程重启 | 重启包 | 训练任务调度恢复逻辑相同 |
| 前置命令 | 容器内齐备 | 需系统有 ssh/rsync(密钥模式免 sshpass) | Linux 桌面一般自带 |

## 五、常见问题(一键包视角)

- **节点一直「未连接」** → ① 确认拉到 2026-09-06 后代码(`sync_from_git.sh`);
  ② 私钥路径是本机绝对路径;③ 实例密码换了就用 `ssh-copy-id` 重装一次公钥
- **测试连接报 sshpass/rsync 缺失** → 系统装一下:`sudo apt install -y rsync sshpass`
  (或坚持用密钥模式,则只需 rsync)
- **训练中关掉了一键包** → 远端 setsid 任务继续跑;重开包后可手动从实例
  `workspace` 拉产物补注册(见远程教程 FAQ)
- **换远程机器** → 只改 `training_nodes.yaml` 的 host/port/ssh_key + 新机装包即可

## 六、关联文档

- 远程机器侧完整部署:《远程训练节点部署教程》`docs/remote-training-node-tutorial.md`
- AutoDL 实例管理:`autodl` skill(`scripts/autodl.sh`:开关机/状态/镜像)
- Windows 主节点远程适配:待支持(传输层需补 rsync 或纯 Python 实现)
