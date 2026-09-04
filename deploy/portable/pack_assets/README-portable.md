# QuantMind 便携版

> 免安装、免 Docker：解压后一键启动，浏览器打开即用。
> 仅供学习研究与技术演示，不构成任何投资建议。

## 支持平台

| 包 | 适用环境 | 启动方式 |
|---|---|---|
| linux-x64 | Ubuntu 20.04+ / Debian 11+ / Rocky 8+ 等 x86_64 glibc 系统，以及 Windows 上的 WSL2 | `bash start.sh` |
| win-x64 | Windows 10/11 x64 | 双击 `start.bat` |

不支持的架构：ARM/M 苹果芯片的 Linux、Alpine/musl 系发行版。

## 启动步骤

1. 把整个文件夹解压到任意**纯英文、不含空格**的路径（如 `D:\QuantMind` 或 `~/quantmind`）。
2. Linux/WSL：`bash start.sh`（前台运行，Ctrl+C 停止；`bash start.sh --bg` 后台运行，`bash stop.sh` 停止）。
   Windows：双击 `start.bat`，停止运行 `stop.bat`。
3. 等待控制台提示「QuantMind 已启动」，浏览器会自动打开 `http://127.0.0.1:8000/`。

首次启动会自动完成：初始化 PostgreSQL 数据目录（约 30 秒）→ 创建数据库 → 建表 → 启动 4 个后端服务 + Celery。

**默认登录账号：`admin / admin123`（首次登录后请在设置里改密码）。**

## 首次使用：下载数据

本包**不含行情数据**。启动后进入网页端「实时数据流 → 数据管理」，在对应市场 tab 点击同步：

- A 股（QuantDB）：约 60GB，首次全量同步耗时较长
- 美股（QuantUS）/ 港股（QuantHK）/ 期货（QuantFutures）：按需
- 若拿到了离线数据包，直接把内容放进 `data/quantdb`、`data/quanthk` 等目录即可，无需联网同步

## 常见问题

- **端口被占用**：编辑 `pack.env`（或 start.bat 顶部）修改端口。
- **AI 功能不可用**：把 DEEPSEEK_API_KEY 等密钥写进 `pack.env`（参考 pack.env.example）。
- **Windows 双击 start.bat 一闪而过**：右键"以管理员身份运行"一次，或检查是否被杀毒软件拦截。
- **PostgreSQL 拒绝启动（Linux）**：不要用 root 运行 start.sh。
- **日志**：全部在 `logs/` 目录（backend.log / postgres.log / redis.log / celery-*.log）。

## 目录说明

```
runtime/   内嵌 Python 3.10 + 全部依赖（勿动）
pgsql/     便携 PostgreSQL 15（勿动）
redis/     便携 Redis（勿动）
backend/   后端源码
web/       前端构建产物
data/      全部运行数据（行情 parquet、模型、报告、备份等）
pgdata/    PostgreSQL 数据库文件（自动生成）
logs/ run/ 日志与运行时文件
```
