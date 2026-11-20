# Windows 实盘桥（源码，随包附带）

本目录是 Windows 端实盘桥的源码与部署脚本，供把 Linux 主机接到 Windows
通达信/QMT 客户端时使用。**包内不含任何 token、账户或运行态数据**，这些都在
Windows 端首次配置时填写。

| 目录 | 内容 | 入口 |
|---|---|---|
| `windows/` | 通达信实盘桥（行情/交易 HTTP + 桥守护） | `setup.bat` / `bootstrap.bat`，说明见 `Windows操作说明.md`、`通达信实盘桥_完整部署指南.md` |
| `qmt-pipe/` | 大 QMT 管道桥（Redis 队列 → Windows 命名管道） | `start_pipe_bridge.cmd` / `register_pipe_watchdog.ps1`，说明见 `README.md` |

## 最短部署路径

1. 在 Windows 机器上安装通达信/QMT 客户端，并把本目录整体拷过去。
2. `windows\`：按 `Windows操作说明.md` 配置 `config.yaml`（TDX 桥 token、账户），
   运行 `setup.bat`，再 `start_bridge_hidden.cmd`。
3. `qmt-pipe\`：按 `README.md` 的 `bridge_config.example.json` 配置 Redis/账户，
   运行 `start_pipe_bridge.cmd`；需要开机自启时运行对应的 `register_*.ps1`。

Linux 主机侧只需要在平台的「数据源设置 / 实盘设置」里填写 Windows 机器地址
和 token；端口与 token 以 Windows 端 `config.yaml` 为准。
