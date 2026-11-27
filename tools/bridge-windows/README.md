# 通达信交易桥（Windows）· 首次安装与使用

> 给 QuantMind 提供行情与交易旁路：**TDX 免费客户端**（TQ 17709）+ **TdxAiData 付费通道**（可选）
> + **同花顺 worker**（可选）。桥跑在 Windows 交易机上，Linux 平台通过 HTTP 访问。

---

## 0. 组成与端口

| 组件 | 端口 | 监听 | 作用 |
|---|---|---|---|
| 桥 `main.py` | 8550 | `0.0.0.0`（局域网） | HTTP API、网页控制台、统一行情聚合 |
| TQ（通达信客户端策略） | 17709 | 本机 | 行情/交易指令通道，需 TdxW 已登录 |
| 采集器 `tdx_collector.py` | 8560 | 本机 | 热集行情落库/落盘 |
| 同花顺 worker（可选） | 8765 | 本机 | 盘口/资金流/板块/异动 |

计划任务：`TDXBridgeWatchdog`（桥看门狗）、`TdxCollector`（采集器）、`TdxThsWorker`（同花顺，可选）。

---

## 1. 环境要求（硬性）

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11 x64 | PowerShell 5.1 即可 |
| Python | **3.10 – 3.12**（推荐 3.12） | 安装时勾选 **Add Python to PATH**；代码使用 3.10+ 语法 |
| 通达信 | TdxW 已安装并**登录交易账号** | TQ 17709 由通达信内的常驻策略提供 |
| 代码位置 | 例如 `C:\new_tdx_mock\PYPlugins\bridge-windows` | 路径可换，命令相对本目录 |
| 网络 | 8550 对局域网放行 | 安装器会自动加防火墙规则 |
| 可选 | Git、同花顺账号、TdxAiData 付费 Key | 对应 `-WithThs` / `-WithAidata` |

> 不需要 Docker、不需要 Node、不需要 NAS；`vendor/` 与 `data/` 不入库，由安装器生成。

---

## 2. 快速开始（约 10 分钟）

### 第 1 步：启动通达信并登录

双击 `TdxW.exe`，登录交易账号（模拟/实盘都行），并确保 TQ 常驻策略在运行
（没有 TQ 时桥仍能启动，但 `tdx_connected=false`、行情与交易不可用）。

### 第 2 步：一键安装（管理员 PowerShell）

```powershell
cd C:\new_tdx_mock\PYPlugins\bridge-windows
.\scripts\install.ps1
```

安装器会依次：校验 Python → 创建 `.venv` → 装依赖（镜像 403 会自动回退官方 PyPI）
→ 生成访问 token → 放行 8550 → 注册三个计划任务 → 启动并验收。

**平台对接**（Linux 侧已有 QuantMind）：用与 `.env` 里 `TDX_BRIDGE_TOKEN` **相同**的 token：

```powershell
.\scripts\install.ps1 -Token <64位hex>
```

**需要同花顺 / 付费数据**：

```powershell
.\scripts\install.ps1 -WithThs -WithAidata
```

### 第 3 步：保存 token

独立安装时脚本会打印**一次** 64 位 token（同时写入 `config.yaml` 并收紧 ACL）。
网页登录、平台对接都用它；丢了可在管理员 PowerShell 里重置：

```powershell
.\scripts\install.ps1 -Token <新token> -NoStart
```

### 第 4 步：打开网页控制台

浏览器访问 `http://127.0.0.1:8550/ui`（局域网用 `http://<本机IP>:8550/ui`），
右上角填入 token。面板包含：行情测试台、同花顺（扫码/盘口/资金流/异动/对照）、
数据服务（TdxAiData Key/用量）、实时订阅、系统状态。

### 第 5 步：验证

```powershell
curl.exe http://127.0.0.1:8550/api/v1/health
# {"status":"ok","tdx_connected":true,...}
```

---

## 3. 安装器做了什么

| 步骤 | 内容 | 失败时 |
|---|---|---|
| 1 | 找 Python（拒绝 WindowsApps 别名），校验 ≥3.10，创建 `.venv` | 明确报错与下载链接 |
| 2 | 装 `aiohttp/pyyaml`；pip 镜像 403 自动改用 `https://pypi.org/simple` | `-IndexUrl` 可指定镜像 |
| 3 | 生成/写入 `auth.token`，`icacls` 收紧到当前用户/SYSTEM/Admins | 已有 token 不轮换 |
| 4 | `-WithThs`：克隆 `thspypc` 到 `vendor\ths` + 依赖到 `vendor\ths_deps` | 需要 git + 网络 |
| 5 | `-WithAidata`：`pip install --target vendor tdxaidata` | 无公共镜像时按发布包放置 |
| 6 | 防火墙放行 TCP 8550 | 非管理员会跳过 |
| 7 | 注册 `TDXBridgeWatchdog` / `TdxCollector` / `TdxThsWorker` | 非管理员会跳过 |
| 8 | 调 `scripts\launcher.ps1 -Restart` 启动并打印状态 | 看 `data\logs\` |

常用参数：`-DryRun`（只打印计划）、`-Check`（只体检）、`-NoStart`、`-NoTasks`、
`-NoFirewall`、`-NoVenv`、`-PythonPath`、`-IndexUrl`。

> 非管理员也能装（`.venv` + 依赖 + token），但**计划任务与防火墙会跳过**；
> 之后用管理员重跑一次 `.\scripts\install.ps1 -NoStart` 即可补齐。

---

## 4. 两种使用形态

### A. 独立运行（只用桥的网页/接口）

`install.ps1` 默认模式。适合先验证数据（测试台、三源行情、板块、异动），
不依赖 Linux 平台。token 随机生成即可。

### B. 对接 QuantMind 平台

Linux `.env` 增加（或核对）：

```ini
TDX_BRIDGE_URL=http://<本机IP>:8550
TDX_BRIDGE_TOKEN=<与安装时 -Token 相同>
```

平台侧调用 `/api/v1/market/quote`、`/api/v1/tdx/call`、`/api/v1/aidata/call` 等；
行情聚合默认 `ths → tdx`，付费 `aidata` 默认不参与（按次计费，需要时显式 `prefer`）。

---

## 5. 可选组件

### 同花顺 worker（`-WithThs`）

1. 安装器克隆上游到 `vendor\ths`、依赖到 `vendor\ths_deps`，并注册 `TdxThsWorker`；
2. 打开网页「同花顺」面板 → **扫码登录**（推荐），或把账号写进 `data\ths.env`；
3. 登录后状态面显示「已登录 · 账号」；普通账号只有 5 档，10 档需 L2。

### TdxAiData 付费通道（`-WithAidata`）

1. 安装器把包放到 `vendor\tdxaidata`；
2. 网页「数据服务」填商城 Key（写入 `TdxAiData.ini`）；
3. 预算：200 行/词元、最低 1 词元/次，每日硬预算默认 500（`config.yaml` 可调）。

### 实时订阅

网页「实时订阅」维护标的：免费 `tq` 通道 ≤100 只（硬上限），付费 `aidata` 推送默认关闭。
勾选 **「自动跟随实盘持仓」**（可设 30–3600 秒）后，桥会周期性读取账户持仓并自动并入订阅
（`merge` 保留手动代码；超出 100 只截断并提示）。同花顺面板的 **「跟随持仓」** 按钮会把
同一份实盘代码载入实时关注（5s 轮询）。

---

## 6. 环境自检与验收

```powershell
.\scripts\install.ps1 -Check
```

会逐项检查：Python/venv/依赖、token、`vendor`、三个计划任务、防火墙、17709/8550/8560/8765、
桥健康。任何 `[FAIL]` 按提示处理后重跑。

手工验收：

```powershell
curl.exe http://127.0.0.1:8550/api/v1/health          # 桥 + TQ
curl.exe -H "Authorization: Bearer <token>" http://127.0.0.1:8550/api/v1/ths/status
curl.exe -H "Authorization: Bearer <token>" "http://127.0.0.1:8550/api/v1/market/quote?code=600519.SH"
```

---

## 7. 目录与日志

```
bridge-windows\
├─ main.py                 # 桥本体
├─ config.yaml             # 配置（token / 端口 / 限流 / 预算）——部署时不要整文件覆盖
├─ .venv\                  # 安装器创建的虚拟环境
├─ vendor\                 # 第三方依赖（不入库）：ths / ths_deps / tdxaidata
├─ data\                   # 运行状态（不入库）
│  ├─ logs\bridge.log collector.log watchdog.log
│  ├─ ths.env              # 同花顺账号（可选）
│  ├─ ths_qr.json          # 扫码凭证缓存
│  ├─ subscription.json    # 订阅状态
│  └─ tdx_cache.db         # 行情缓存（自愈 + 日清）
├─ scripts\                # install / launcher / 任务注册 / worker
└─ src\                    # 桥源码
```

---

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 平台全 401 / 网页 token 无效 | 两侧 token 不一致 | 用 `-Token` 重装或改为 `.env` 相同值 |
| `tdx_connected:false` | TdxW 未登录 / TQ 未运行 / 17709 未监听 | 登录通达信，重启 TQ 策略；`Test-NetConnection 127.0.0.1 -Port 17709` |
| `.venv` 依赖装不上（HTTP 403） | pip 镜像不可用 | 安装器已自动回退官方 PyPI；仍失败用 `-IndexUrl https://pypi.org/simple` |
| 同花顺面板「未安装」 | 未装 `vendor\ths` | `.\scripts\install.ps1 -WithThs` |
| 扫码后刷新显示未登录 | 旧版 UI 用 `credentials` 判断 | 升级到本版（状态面已带 `qr.account`，显示「已登录 · 账号」） |
| 同花顺 503 `THS_BUSY` | worker 饱和（并发闸门） | 等 0.3–1s 重试；统一行情会自动回落 TDX |
| 端口 8550 被占用 | 旧桥/僵尸进程 | `.\scripts\launcher.ps1 -Restart`（会先停看门狗再杀端口） |
| 计划任务没起来 | 安装时不是管理员 | 管理员重跑 `.\scripts\install.ps1 -NoStart` |
| 网页乱码 | 控制台编码 | 用 `启动交易台.cmd`（已设 UTF-8）；脚本需 UTF-8 BOM |
| 防火墙弹窗 | 首次监听 8550 | 点允许；安装器也会加 `TDXBridge-8550` 规则 |

---

## 9. 升级与运维

- **升级代码**：只更新 `src/`、`scripts/`、`main.py`、`watchdog.ps1`、`ui.html`；
  **不要整文件覆盖 `config.yaml`**（桥在 token 缺失时会自动生成新 token，平台会立刻 401）。
- **一键启动**：`启动交易台.cmd`（或 ASCII 别名 `start_trading_desk.cmd`）现在会先跑 TDX 交易台
  （TQ 17709 / 桥 8550 / 看门狗 / 采集器 8560 / 同花顺 8765），再启动 QMT 伴生桥 17720，
  最后跑 `qmt-tools\cache_quality_check.py` 质检缓存与关键参数；QMT 已在健康运行时不重启（`NOOP`），
  `-Restart` 会同时强制重启 TDX 与 QMT；自动化用 `-NoPause -NoUi`，跳过质检用 `-NoCacheCheck`。
- **重启**：`.\scripts\launcher.ps1 -Restart`（缺谁起谁；`-NoThs` 跳过同花顺）。
- **日志**：`data\logs\`；缓存库损坏会自愈（隔离重建 + 日清）。
- **卸载**：停止并删除三个计划任务，删除本目录即可（`data/` 一并删除）。

---

## 10. 安全

- token 是唯一钥匙：不要发到群里/提交到仓库；`config.yaml` 已被 ACL 收紧。
- 桥对局域网明文 HTTP：只在内网使用，勿做公网端口映射；需要公网请套反向代理 + TLS。
- `/api/v1/tdx/call` 只放行**只读**数据方法；交易与写操作一律 403。
- 未鉴权 600/分、鉴权 6000/分；5 次鉴权失败封 IP 30s；请求体上限 512KB。
- 同花顺 worker 只绑 `127.0.0.1`，不对外；付费 Key 存在 `vendor\tdxaidata\lib\TdxAiData.ini`。

> 本项目仅供学习研究与技术演示，不构成投资建议。实盘有风险，下单前请自行复核。
