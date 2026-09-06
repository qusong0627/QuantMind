---
name: autodl
description: AutoDL 容器实例 Pro API 管理 — 创建/开关机/释放 GPU 实例、查状态与 SSH 信息、保存镜像;与 QuantMind 免 Docker 训练节点包配合做远程 GPU 训练
---

# AutoDL 容器实例 Pro API

管理 AutoDL「容器实例 Pro」GPU 实例的官方 REST API(host: `https://api.autodl.com`)。
本技能用于:开机一台 GPU 实例 → 拿到 SSH 信息 → 部署 QuantMind 训练节点包
(`deploy/portable/dist/qm-train-node-*.tar.gz`,免 Docker)→ 在主节点训练中心
注册为 `executor=process` 节点 → 远程 GPU 训练。

## ⚠️ 凭据安全(先读)

- Token 是 JWT,等于账号控制权。**绝不写进代码/技能/提交**,一律从环境读:
  `AUTODL_API_TOKEN`(或仓库 `.env` 的 `AUTODL_API_TOKEN=`,`.env` 已 gitignore)
- 辅助脚本 `scripts/autodl.sh` 自动读上述两处;token 一旦疑似泄露,去
  AutoDL 控制台→账号→开发者 Token 重置
- 实例按量计费(示例 pro6000 ≈ ¥1.97/时):用完即 `power_off`,长期不用 `release`

## 快速命令(scripts/autodl.sh)

```bash
bash .claude/skills/autodl/scripts/autodl.sh list            # 实例列表(page1,20条)
bash .claude/skills/autodl/scripts/autodl.sh snapshot <uuid> # 实例详情:含 ssh_command/root_password/proxy_host/ssh_port
bash .claude/skills/autodl/scripts/autodl.sh status  <uuid>  # running/stopped/…
bash .claude/skills/autodl/scripts/autodl.sh on      <uuid>  # 开机(有卡 gpu)
bash .claude/skills/autodl/scripts/autodl.sh off     <uuid>  # 关机
bash .claude/skills/autodl/scripts/autodl.sh release <uuid>  # 释放(先关机)
bash .claude/skills/autodl/scripts/autodl.sh images         # 私有镜像列表
bash .claude/skills/autodl/scripts/autodl.sh save    <uuid> "<镜像名>"   # 保存镜像→返回 image_uuid
bash .claude/skills/autodl/scripts/autodl.sh create <gpu_spec_uuid> [data_center] [cuda_v_from] [image_uuid] # 创建
```

不带 jq 时自动退化 python -m json.tool。

## 实例生命周期与关键字段

- **创建** body:`gpu_spec_uuid`(必填)、`req_gpu_amount` 1-4、`expand_system_disk_by_gb`、
  `image_uuid`(默认公共 miniconda 镜像)、`cuda_v_from`(驱动 CUDA 下限,语义 `11.3→113`;
  torch cu128 建议 ≥ 128)、`data_center_list` 选填、`start_command` 选填
- **snapshot 返回** `ssh_command`(如 `ssh -p 34222 root@connect.xxx.autodl.com`)、
  `root_password`、`proxy_host`、`ssh_port` —— 主节点配节点直接用这组信息
- **status** `running / stopped`;开关机异步,轮询 status 至目标态
- 释放前必须 `power_off`,否则可能释放失败

## GPU 规格与镜像附录(常用)

| GPU | 规格 ID |
|---|---|
| H800-80G 通用 | `h800` |
| 4090-48G 通用 | `v-48g` |
| 5090-32G 性能 | `5090-p` |
| PRO6000-96G 性能 | `pro6000-p` |
| 4080(S)-32G 性能 | `v-32g-p` |

公共基础镜像:`base-image-mbr2n4urrc`(miniconda cuda11.6-py38)等;更推荐
**先建实例装好训练节点包/环境后 `save` 私有镜像**,下次秒开同环境。

## 与 QuantMind 训练节点包的标准流程

> 完整图文步骤见仓库教程:`docs/remote-training-node-tutorial.md`
> (节点包部署/密钥/节点注册/训练中心操作/运维/FAQ)

1. `list` 找目标实例 / `create 4090-p`(或所需规格)→ 等 `status=running`
2. `snapshot <uuid>` 取 ssh_command/root_password/ssh_port
3. `scp qm-train-node-<date>.tar.gz` 到实例并解压,`bash start_node.sh` 自检
4. 主节点 `config/training_nodes.yaml` 加节点:`executor: process`、
   `pack_root`、`work_dir`、ssh 信息;训练中心测试连接 → 直读因子训练
   (实例上 `sync_factors.sh CN|HK|US` 自动同步该市场因子)
5. 训练完 `off`,长期不用 `release`;同环境再开用私有镜像秒级恢复
