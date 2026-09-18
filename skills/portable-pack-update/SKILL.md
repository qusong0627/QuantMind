---
name: portable-pack-update
description: "QuantMind 一键启动包（便携包 / 免 Docker 版）的升级维护——判断更新类型（后端 .py / 前端 web / 依赖模型）、生成与套用补丁包（make_update_patch / make_web_update_patch）、覆盖后端与前端产物、stop→start 重启与验证口径。用户说「升级便携包」「更新一键启动包」「覆盖前端」「web 更新」「重启便携包」「补丁包」时使用。触发词：便携包、一键启动包、免Docker、覆盖web、前端更新、补丁包、升级包、重启便携包"
---

> ## ⚙️ 运行环境契约（最高优先级）
>
> 1. **便携包跑在用户自己的机器上**（Linux/WSL2 或 Windows）。本技能的动作分「打包机（维护者，本仓库）」与「用户机（客户）」两侧，**别混**：打包机产出补丁，用户机套用并重启。
> 2. **QuantBot（dsh）的角色 = 判断更新类型 → 产出补丁包/给出精确命令 → 指导用户覆盖与重启**。QuantBot 容器只读挂载本仓库 `skills/`，**无法直接写用户机文件系统**——不要假装能替用户执行；给命令、给补丁、给验证口径。
> 3. **Docker 服务器版**（quantmind 容器栈）的更新走 `skills/quantmind-deploy`（update.sh）；本技能只管**便携包（免 Docker）**。
> 4. Linux/WSL2 用 bash 命令；Windows 用 bat（包内均已提供，双击或 cmd 运行）。Windows 中文系统铁律：bat 为 ASCII+CRLF（已固化，勿手改）。

# 便携包更新（一键启动包）

## 1. 包结构速览（找东西先看这里）

| 路径 | 说明 |
|---|---|
| `start.sh` / `stop.sh` / `start.bat` / `stop.bat` | 启动/停止（Win 双击 bat，窗口驻留；Linux `bash start.sh --bg` 后台） |
| `backend/` | 后端源码**直跑**（改 .py 即生效，但需重启进程） |
| `web/` | 前端静态产物，**由后端同端口伺服**（`QM_WEB_DIST_DIR`）；覆盖后无需重启，浏览器强刷 |
| `config/`、`strategy_templates/` | 配置与策略模板（补丁会覆盖） |
| `data/`、`models/`、`runtime/` | 数据（不打包、界面同步）/ 模型 / 内嵌运行时 |
| `pack.env` | **用户私有配置（端口、路径）——补丁永不覆盖** |
| `VERSION` | 版本号；前端也可查「设置→关于」或 `GET /api/v1/system/version` |
| `logs/` | `startup.log`（启动过程）/ `backend.log`（服务日志） |

## 2. 更新类型判断（先判断，再动手）

| 改动内容 | 更新方式 | 需要重启？ |
|---|---|---|
| 后端 `.py`（bug 修复 / 新功能） | 后端补丁 zip，或直接覆盖 `backend/` | ✅ `stop` → `start` |
| **前端 UI**（改了 `electron/src`） | **web 更新包**（见 §3.2） | ❌ 不用重启，浏览器 `Ctrl+Shift+R` 强刷 |
| start/stop 脚本、依赖、模型文件 | 定向包或全量包 | ✅ |
| 数据库结构 | `data/upgrade_*.sql` 幂等补丁（随补丁包自动执行） | ✅（在重启前套用） |

## 3. 打包机侧（维护者，本仓库）

**前置**：① 改动已 `git commit`；② 改过 `electron/src` 必须先 `cd electron && npm run build:react`（产物 = `electron/dist-react/`，是打 web 包的输入）。

### 3.1 后端 + 配置增量补丁

```bash
bash deploy/portable/make_update_patch.sh          # 基线默认 HEAD~1，可传 tag/commit
```

产出 `deploy/portable/dist/QuantMind-Update-<日期>.zip`：含 `backend/ config/ strategy_templates/`
+ 根级启动脚本 + `data/upgrade_*.sql` + `apply_update.bat`（用户解压覆盖到包根后双击套用）。
**不覆盖 `pack.env`**；前端产物不在其中。

### 3.2 纯前端（web）更新包

```bash
bash deploy/portable/make_web_update_patch.sh      # 使用 electron/dist-react 现成产物
```

产出 `dist/QuantMind-WebUpdate-<日期>.zip`：含 `newweb/`（完整前端）+ `apply_update_web.bat`
（自动清旧 `web/assets` → 拷 `newweb`）。**补丁包不含 web/ 时用它单独升级老用户 UI**。

**分发**：拷 zip 给用户（或 SMB 共享 `deploy/portable/dist`）。

## 4. 用户机侧（客户）套用

三步：**① 停服 → ② 覆盖 → ③ 起服验证**（前端覆盖可不停服）。

**Windows**
1. 后端补丁：zip 解压到包根（覆盖同名文件）→ 双击 `apply_update.bat`
2. 前端补丁：zip 解压到包根（不覆盖任何已有文件）→ 双击 `apply_update_web.bat`
3. 重启：`stop.bat` → `start.bat`（等待窗口出现 `Ready:`）

**Linux / WSL2**

```bash
# 后端补丁
unzip QuantMind-Update-<日期>.zip -o -d <包根>/    # 覆盖
bash stop.sh && bash start.sh --bg

# 前端补丁（无需重启）
unzip QuantMind-WebUpdate-<日期>.zip -d <包根>/    # 得到 newweb/
cd <包根> && rm -rf web/assets && cp -r newweb/. web/ && rm -rf newweb
# 浏览器 Ctrl+Shift+R 强刷
```

## 5. 重启与验证口径（每次更新后必做）

- **起服成功信号**：日志/窗口出现 `Ready: http://127.0.0.1:<API端口>/`；4 个服务端口
  `/health` 均 200（端口见 `pack.env`，默认 8000–8003）。
- **三个红旗**（任一出现 = 有根因没修完，不是"再等等"）：
  `crashed too many times` / `'gbk' codec can't decode` / 成片 `UndefinedTable`。
- 前端抽查：登录（默认 admin / admin123，需带 tenant_id=default）→ 关键页面可用性；
  必要时参数化推理 E2E（`POST /api/v1/models/inference/run`，`sys-` 系模型 + 近交易日，
  期望 `success=true` 且 `signals_count>0`）。
- **全量重发时**：先在 **Windows 真机**完整跑一轮（Win 包是 Linux 交叉组装，
  `start.bat` 的真机验证不可省略）。

## 6. 已验证基线（2026-09-18，Linux 便携包实测）

当日 backend + web 同步进便携实例后全链通过：4 端口健康；后端四服务 import 冒烟 OK；
建议卡生成器/兑现回填 worker 在包内正常运行（生成器实跑建 3 张卡）；登录 OK；
推理 E2E `success=true, signals_count=5192`（09-17 口径）。**Windows `start.bat` 真机验证仍待做**。

> 维护者本机调试实例（test7 配方）与排坑史见仓库外维护笔记；出全量包用
> `deploy/portable/build_linux_pack.sh` / `build_windows_pack.sh`（详见部署 README）。
