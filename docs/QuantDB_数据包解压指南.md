# QuantDB 数据包解压指南

> **已下线**：`quant_data.7z`（百度网盘）已彻底下线，请勿再使用。现请通过 **ModelScope** 数据集 <https://www.modelscope.cn/datasets/qusong0627/LightGBM_Alpha300> 获取，或直接在部署后到【管理后台】→【数据管理】点击【初始化数据】自动拉取。下文保留历史目录结构说明，仅作参考。

本文档说明 **QuantDB 本地数据目录**的标准布局与验证方法。

> 该数据包是 **QuantDB 本地数据仓库**（按数据集目录组织），与 `docs/数据包安装指南.md` 中的 `tar.gz` 备份包不是同一类数据，两者独立安装。注：旧文档中的 `feature_snapshots` 目录已废弃，请忽略。

## 1. 数据包内容

历史 `quant_data.7z` 解压后包含以下按数据集组织的顶层目录（约 **56 GB / 7.4 万文件**，现以 ModelScope 为准）：

| 目录 | 说明 | 解压大小 |
|------|------|----------|
| `1_kline_data/` | K 线数据（日线 forward/backward/unadjusted、指数、1/5 分钟、tick） | ~7.4 GB |
| `2_base_sector/` | 基础板块（申万行业、概念、交易日历、指数权重、融资融券） | ~0.4 GB |
| `3_financial_data/` | 财务数据（资产负债表、利润表、现金流、股本、分红因子等） | ~0.85 GB |
| `4_bond_etf/` | 债券 / ETF | 小 |
| `5_technical_derived/` | 技术衍生（估值、技术指标、市场情绪） | ~9.3 GB |
| `6_ml_datasets/` | 机器学习因子（features_daily、l1_factors、l1_l2_factors 等） | ~39 GB |

## 2. 项目标准数据目录

QuantDB 数据的标准目录为 `data/quantdb`，在宿主机与容器内的路径对应关系如下：

| 位置 | 路径 |
|------|------|
| 宿主机项目目录 | `/opt/quantmind/data/quantdb` |
| 容器内（挂载 `/data`） | `/data/quantdb` |
| Windows 本地开发 | 参照 `backend/services/engine/data_platform/quantdb_hub.py` 中的默认目录 |

容器通过 `./data:/data` 挂载，解压到宿主机 `data/quantdb` 后即可被容器读取。

## 3. 磁盘空间检查（重要）

解压前务必确认磁盘空间充足：

- 数据包解压后约需 **56 GB**，压缩包本身约 **36 GB**
- **解压期间压缩包与解压数据会同时占用磁盘**，高峰需求约 **92 GB**
- 若磁盘不足，先清理可释放的空间（如已加载进 Docker 的离线镜像包 `data/quantmind-downloads/quantmind-offline/`），或解压成功后删除压缩包以回收空间

使用 `df -h` 检查数据目录所在磁盘：

```bash
df -h /opt/quantmind/data
```

## 4. 解压步骤

### 4.1 确认压缩包与目标目录

```bash
# 已下线：不再提供 quant_data.7z，改用管理后台初始化数据
# 数据集：https://www.modelscope.cn/datasets/qusong0627/LightGBM_Alpha300

# 创建目标目录
mkdir -p /opt/quantmind/data/quantdb
```

### 4.2 执行解压

进入目标目录，用 `7z` 解压（与已有少量本地数据自动合并）：

```bash
# 已下线：不再提供 7z 解压，请到管理后台点击初始化数据
# 如需离线导入，请从 ModelScope 下载后按目录结构放置到 /opt/quantmind/data/quantdb
```

- 查看后台解压进度：

```bash
du -sh /opt/quantmind/data/quantdb
ps aux | grep "7z x" | grep -v grep | wc -l
```

### 4.3 解压完成后回收空间

解压并核验无误后，可删除压缩包释放约 36 GB 空间：

```bash
# 已下线，无需清理 quant_data.7z
df -h /opt/quantmind/data
```

## 5. 验证解压结果

解压日志末尾会出现 `Everything is Ok`，并给出目录/文件统计：

```bash
tail -8 /tmp/quantdb_extract.log
```

预期输出：

```
Everything is Ok

Folders: 27678
Files: 131272
Size:       59606711644
```

进一步核验目录结构：

```bash
ls /opt/quantmind/data/quantdb
du -sh /opt/quantmind/data/quantdb/
ls /opt/quantmind/data/quantdb/1_kline_data/
```

应包含 `1_kline_data`~`6_ml_datasets` 等顶层目录，且 `Size` 与压缩包清单一致。

## 6. 服务读取（无需重启容器式重建）

QuantDB 数据通过 bind mount 挂载进容器（`./data:/data`），解压到宿主机 `data/quantdb` 后，容器内 `/data/quantdb` 即可直接读取，无需重新打包镜像。

若数据更新后相关服务未能即时感知，可重启后端服务使其重新加载：

```bash
cd /opt/quantmind
docker compose restart quantmind celery-worker
```

> 注：`data/quantdb/quantdb_sync.sqlite` 为本地同步状态库，解压时若目标已有则保留，若由数据包带入则按需保留。

## 7. 常见问题

### Q: 解压后数据集目录为空？

确保是在 `data/quantdb` 目录内解压（`7z x` 会把顶层 `1_kline_data` 等目录解出）：

```bash
# 已下线：该路径不再提供 7z 包
ls /opt/quantmind/data/quantdb
```

### Q: 磁盘空间不足？

数据解压 + 压缩包高峰需求约 92 GB。清理可释放空间后重试（如已加载进 Docker 的离线镜像包）：

```bash
du -sh /mnt/data/quantmind-downloads/*
```

### Q: 容器读不到数据？

确认容器内数据目录可见：

```bash
docker exec quantmind ls -la /data/quantdb
```

必要时按第 6 节重启后端服务。

---

本文档按项目标准目录（宿主机 `/opt/quantmind/data/quantdb`、容器内 `/data/quantdb`）编写，未涉及具体部署机器的软链接等环境细节。