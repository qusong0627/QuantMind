---
name: qwenpaw-migrate
description: QwenPaw（千问，旧版 QuantBot）→ DSH 迁移向导：把旧容器里的用户技能与 MCP 配置导入当前 DSH 环境（导入完停用并清理旧 QwenPaw 与数据卷）。用户说「迁移」「QwenPaw」「千问」「旧数据导入」「升级后数据没了」「旧技能/旧 MCP 导入」「停掉千问」时使用。触发词：迁移、QwenPaw、千问、旧数据、技能导入、MCP导入、升级清理、停掉千问
---

# QwenPaw → DSH 迁移

把旧 QwenPaw（QuantBot 上一代后端）里的**用户资产**迁移到 DSH：**技能**（用户自定义 SKILL.md）与 **MCP 配置**（mcp.clients）。聊天记录不迁移（旧卷在清理前会整卷打包备份，可人工取证）。迁移完成后停用并清理旧 QwenPaw 容器与数据卷。

## ⚙️ 运行环境契约（在 dsh 容器内执行）

1. **执行位置**：本容器（QuantBot / dsh）。脚本纯标准库，`python3` 直接跑：
   `python3 /quantmind/skills/qwenpaw-migrate/scripts/qwenpaw_migrate.py <子命令>`
2. **docker CLI**：本容器已挂宿主 docker.sock；`docker run -v <路径>` 的路径是**宿主路径**（经宿主 daemon）。
3. **旧数据位置**：命名卷（带 compose 项目前缀，如 `quantmind_qwenpaw-data`），挂 `/app/working`——`skill_pool/`、`workspaces/default/skills/`、`config.json` 都在里面。**动态发现，不要写死卷名**。
4. **仓库技能库**：`/root/.dsh/skills`（只读挂载 == 宿主仓库 `skills/`）。往仓库写技能要经宿主路径：脚本已用 `docker inspect` 自动定位宿主仓库。
5. **重启副作用**：`docker restart quantmind-dsh` 会中断**当前对话**——凡需要重启生效的步骤，先明确告知用户再执行。

## 迁移流程（四步，按顺序）

### 第 0 步：盘点

```bash
python3 /quantmind/skills/qwenpaw-migrate/scripts/qwenpaw_migrate.py inventory
```

输出 JSON：发现的旧卷/旧容器、旧技能分类（`custom_to_import` = 要导入的用户技能；QwenPaw 内置与已在仓库的不动）、旧 MCP 客户端清单、当前 dsh 连接器已有连接、宿主仓库路径。

若 `volumes` 为空 → 无旧数据可迁移，直接跳到第 3 步清理确认；若发现了多个数据卷候选，让用户确认用哪个（`--volume <卷名>`）。

### 第 1 步：导入用户技能

```bash
python3 /quantmind/skills/qwenpaw-migrate/scripts/qwenpaw_migrate.py import-skills
```

- 把 `custom_to_import` 里的技能从旧卷复制到**宿主仓库 `skills/`**（仓库即 dsh 插件库，挂载即生效）；
- QwenPaw 运行时内置（browser_cdp / cron / dingtalk_channel / himalaya / make_plan 等）**不迁移**——它们是渠道/浏览器基础设施，与技能体系无关；
- 同名技能保留仓库版（平台维护）；若用户明确说旧版更新，让用户人工比对后再手工覆盖。

**核验 quantmind 本地技能库**（第 3 件导入物）：inventory 的 `repo_skill_count` 应 ≥ 90（完整平台技能库），并与 `/root/.dsh/skills/` 目录数一致；缺了就说明仓库 `skills/` 不完整——从 `/quantmind/skills` 对照补齐或提示用户升级部署包。

### 第 2 步：导入 MCP 配置

```bash
python3 /quantmind/skills/qwenpaw-migrate/scripts/qwenpaw_migrate.py import-mcp
```

- 读旧 `config.json → mcp.clients`，按 dsh-mcp-connector 的 ConnectionRecord 结构写入 `/root/.dsh/storages/mcp_connector.json`（**写入前自动备份**为 `mcp_connector.json.bak-<时间戳>`）；
- stdio（command/args/env）与 url 型（streamable-http/sse，headers 原样带上）都支持；无 command/url 的畸形条目跳过并列出；
- 幂等：已存在的 key（`qwenpaw-<slug>-main`）跳过，可安全重复执行；
- **env/headers 里的密钥原样保留在本机存储**（本地迁移，不做脱敏）——提示用户迁移后去「设置 → MCP 连接器」核对并按需禁用。

生效需要重启 dsh（`docker restart quantmind-dsh`，**先告知用户当前对话会中断**；重启后用「设置 → MCP 连接器」核对连接列表，或直接让用户发一条需要该工具的消息验证）。

### 第 3 步：清理旧 QwenPaw

**先向用户确认**："技能与 MCP 已导入并核验，接下来停用并删除旧 QwenPaw（数据卷会先整卷打包到 `/data/backups/` 再删除）——确认？"

```bash
# ① 先保留卷（安全档）
python3 /quantmind/skills/qwenpaw-migrate/scripts/qwenpaw_migrate.py cleanup
# ② 用户确认后，备份 + 删除卷（不可逆；备份 tar 留在 /data/backups/）
python3 /quantmind/skills/qwenpaw-migrate/scripts/qwenpaw_migrate.py cleanup --remove-volumes
```

- 停止并删除所有 `qwenpaw*` 容器（含旧版 8088 占用——dsh 与 qwenpaw 端口冲突，只能留一个）；
- `--remove-volumes`：四个卷（data/secrets/backups/shared）逐一 `tar.gz` 到 `/data/backups/`（宿主 ./data 卷，随平台数据长期保留）→ 再 `docker volume rm`。
- `qwenpaw-shared` 若仍被 quantmind 主容器挂载（compose 引用未清）会删不掉——属零头卷，脚本会保留并提示；主容器下次重建后即可删（或在 compose 里清掉该挂载）。
- 若部署走 compose legacy profile：`docker compose --profile legacy` 相关服务已随容器删除停用，无需再动 compose 文件（回滚能力在 dsh 侧由 compose 注释保留）。

## 边界与已知限制

- **聊天记录不迁移**（产品决定）：旧会话内容只存在于备份 tar 里；用户若需要，可解包 `data.tar.gz` 里的 `workspaces/default/sessions/**/*.json` 人工查阅。
- **渠道配置不迁移**：微信/钉钉等渠道属于 QwenPaw 运行时，DSH 侧用 @xmanrui/dsh-im（QQ）等插件替代，账号与白名单需重新配置。
- **技能正文里的旧路径**：用户自定义技能若引用 `/app/working/...`（QwenPaw 工作区路径），迁移后需按 `docker/dsh/AGENTS.md` 的映射改用 `/quantmind/skills/<name>/scripts/`。
- 迁移不改 `docker-compose.yml`；部署包自身的 QwenPaw→DSH 切换由 update.sh 负责，本技能只处理**数据面**。
