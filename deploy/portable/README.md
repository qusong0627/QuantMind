# QuantMind 便携版（免 Docker 一键启动包）

目标：**解压即用、零安装**。用户不需要 Docker、Python、PostgreSQL、Redis、Node —— 全部随包分发。

```
deploy/portable/
├── build_linux_pack.sh     # 在 Linux 上构建 Linux 便携包（本机可直接完整验证）
├── build_windows_pack.sh   # 在 Linux 上交叉组装 Windows 便携包（需 Win 真机验证后分发）
├── pack_assets/            # 放进包里的启动脚本与用户文档
│   ├── start.sh / stop.sh      # Linux/WSL 启停
│   ├── start.bat / stop.bat    # Windows 启停
│   ├── pack.env.example        # 用户可选配置（端口/密钥）
│   └── README-portable.md      # 包内用户说明
├── build/                  # 组装暂存区与下载缓存（gitignore）
└── dist/                   # 成品包（gitignore）
```

## 包内结构

```
QuantMind-Portable-xxx/
├── runtime/python/   内嵌 Python 3.10（python-build-standalone，可搬迁、无绝对路径依赖）
├── pgsql/            便携 PostgreSQL 15（zonky 官方二进制，initdb 到包内 pgdata/）
├── redis/            便携 Redis（Linux 版源码编译 7.2；Windows 版 tporadowski 5.0）
├── backend/ config/ strategy_templates/   源码
├── web/              前端构建产物（electron/dist-react 拷贝）
├── data/             运行数据（空，用户启动后在线同步或拷入离线数据包）
├── start.sh|bat      一键启动：initdb → PG → Redis → main_oss(4服务) → celery → 浏览器
└── README.md         用户说明
```

## 与 Docker 版的差异（有意为之）

| 项 | Docker 版 | 便携版 |
|---|---|---|
| 前端伺服 | quantmind-web 容器 (nginx) | api 服务直接伺服（`QM_WEB_DIST_DIR` 环境变量，见 api/main.py 末尾 SPA 兜底） |
| Huntly/RSSHub 新闻 | 容器 | 不含，新闻功能降级 |
| qwenpaw / ib-gateway / futu-opend | 容器 | 不含 |
| GPU 训练镜像 | quantmind-oss-gpu | 不含（训练走远程 GPU 节点链路） |
| rd-agent 因子挖掘 | 镜像内 | 不含 |
| FinBERT 权重 | 构建时预下载 | 首次使用新闻情绪时在线下载（HF_HOME=data/hf） |
| qlib features 链接 | os.symlink | Windows 上自动改用目录联接 mklink /J（main_oss.py 已兼容） |

## 构建流程

```bash
# Linux 包（约 30-60 分钟，主要耗在 pip 下载 4-6GB 依赖）
bash deploy/portable/build_linux_pack.sh
# 成品: deploy/portable/dist/QuantMind-Portable-linux-x64.tar.gz

# Windows 包（交叉组装，成品必须在真机 Windows 验证）
bash deploy/portable/build_windows_pack.sh
# 成品: deploy/portable/dist/QuantMind-Portable-win-x64.zip
```

构建前置：`npm run dashboard:build`（生成 electron/dist-react）、curl、gcc/make（Linux 包编译 Redis 用）、磁盘 ≥ 23GB。

增量构建：脚本有缓存（build/cache 下载缓存、runtime 已装依赖自动跳过），改代码后重跑只需重新复制源码 + 重压缩；改依赖后删 build/QuantMind-Portable-*/runtime 重装。

## 发布检查单

- [ ] Linux 包：本机解压到新路径跑 `bash start.sh`，/health 200、前端页面可打开、数据管理页可发起同步
- [ ] Windows 包：真机解压 `start.bat` 同上（重点盯 Redis 5.0 命令兼容、qlib features junction）
- [ ] 包内 VERSION 文件记录 git 提交号，与发布说明对应
- [ ] 网盘分发建议：环境包与离线数据包分开上传（数据包按市场拆分）

## macOS

pyqlib 0.9.7 有 macosx universal2 wheel、PG 有 zonky darwin 二进制、启动脚本可直接复用 start.sh，
理论可行；但没有 mac 构建机验证，未提供打包脚本。需要时参照 build_linux_pack.sh 换
`-apple-darwin` 运行时与 darwin PG/Redis 二进制即可。
