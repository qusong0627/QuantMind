# QuantMind Electron 编译方案

## 1. 项目概述

QuantMind 桌面端采用 **Electron + React + Vite** 技术栈，实现双部署模式：
- **Electron 桌面应用**：Windows/macOS/Linux 原生桌面体验
- **Web 浏览器应用**：通过 Nginx 代理访问

### 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 渲染进程 | React 18 + TypeScript | React 18.3.1, TS 5.4.5 |
| 构建工具 | Vite | 7.2.4 |
| Electron | Electron | 40.8.0 |
| 打包工具 | electron-builder | 24.13.3 |
| 状态管理 | Redux Toolkit + Recoil + Zustand | - |
| UI 框架 | Ant Design 5 + Tailwind CSS 3 | - |
| 测试 | Vitest + Playwright | - |

---

## 2. 目录结构

```
electron/
├── electron/                    # Electron 主进程代码
│   ├── main.ts                  # 主进程入口（窗口/IPC/自动更新）
│   ├── preload.ts               # 预加载脚本（contextBridge）
│   ├── ipc/                     # IPC 处理器
│   │   └── handlers/
│   │       └── auth.handler.ts
│   └── services/
│       └── config_service.ts
├── src/                         # React 渲染进程代码
│   ├── main.tsx                 # React 入口
│   ├── App.tsx                  # 根组件（路由）
│   ├── features/                # 功能模块
│   │   ├── auth/                # 认证模块
│   │   ├── quantbot/            # AI 对话模块
│   │   ├── strategy-wizard/     # 策略创建向导
│   │   ├── strategy-comparison/ # 策略对比
│   │   ├── user-center/         # 用户中心
│   │   └── admin/               # 管理后台
│   ├── services/                # API 客户端、WebSocket
│   ├── store/                   # Redux Toolkit store
│   └── config/                  # 服务地址配置
├── scripts/                     # 构建脚本
│   ├── package-win.js           # Windows 打包脚本
│   ├── electron-dev.js          # Electron 开发启动
│   ├── copy-dist-electron.js    # 复制 dist-electron
│   ├── clean.js                 # 清理构建产物
│   └── lint-*.js                # Lint 脚本
├── build/                       # 构建资源（图标等）
├── dist-react/                  # Vite 构建输出（React）
├── dist-electron/               # TypeScript 构建输出（Electron）
├── dist/                        # electron-builder 打包输出
├── package.json                 # 依赖 + electron-builder 配置
├── vite.config.ts               # Vite 配置
├── tsconfig.json                # React TypeScript 配置
├── tsconfig.electron.json       # Electron TypeScript 配置
├── .env.example                 # 环境变量示例
└── .env.local                   # 本地环境变量
```

---

## 3. 构建配置详解

### 3.1 Vite 配置 (`vite.config.ts`)

```typescript
// 关键配置
{
  base: mode === 'production' ? './' : '/',   // 生产使用相对路径
  build: {
    outDir: 'dist-react',                      // React 输出目录
    sourcemap: mode === 'development',
  },
  server: {
    port: 3000,
    strictPort: true,
    host: '0.0.0.0',
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws':  { target: 'ws://localhost:8000', ws: true },
      '/api/tencent': { target: 'https://qt.gtimg.cn', changeOrigin: true },
    }
  }
}
```

**代理说明**：
- `/api` → 后端 API 网关 (8000)
- `/ws` → WebSocket 市场数据
- `/api/tencent` → 腾讯股票行情接口（带 User-Agent 伪装）

### 3.2 TypeScript 配置

**React 渲染进程** (`tsconfig.json`)：
- `noEmit: true`（由 Vite 处理编译）
- 继承 `tsconfig.combined.json`

**Electron 主进程** (`tsconfig.electron.json`)：
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "CommonJS",
    "outDir": "./dist-electron",
    "rootDir": "./",
    "strict": false,
    "declaration": true,
    "sourceMap": true
  },
  "include": ["electron/**/*"]
}
```

### 3.3 electron-builder 配置 (`package.json` → `build`)

```json
{
  "appId": "com.quantmind.dashboard",
  "productName": "QuantMind量化助手",
  "files": ["dist-react/**/*", "dist-electron/**/*"],
  "directories": {
    "buildResources": "build",
    "output": "dist"
  },
  "win": {
    "target": [
      { "target": "nsis", "arch": ["x64"] },
      { "target": "portable", "arch": ["x64"] }
    ],
    "icon": "logo.ico"
  },
  "mac": {
    "target": [
      { "target": "dmg", "arch": ["x64", "arm64"] },
      { "target": "zip", "arch": ["x64", "arm64"] }
    ],
    "identity": null,
    "hardenedRuntime": false
  },
  "linux": {
    "target": [
      { "target": "AppImage", "arch": ["x64"] },
      { "target": "deb", "arch": ["x64"] }
    ]
  },
  "nsis": {
    "oneClick": true,
    "perMachine": false,
    "allowToChangeInstallationDirectory": false,
    "createDesktopShortcut": true,
    "createStartMenuShortcut": true
  }
}
```

---

## 4. 主进程架构 (`electron/main.ts`)

### 4.1 窗口配置

```typescript
new BrowserWindow({
  width: 1440, height: 1000,
  minWidth: 1440, minHeight: 1000,
  maxWidth: 1440, maxHeight: 1000,
  resizable: false,
  maximizable: false,
  fullscreenable: false,
  frame: false,                          // 无边框窗口
  transparent: win32 && !isWin11,        // Win10 透明（CSS 圆角）
  titleBarStyle: darwin ? 'hiddenInset' : 'hidden',
  webPreferences: {
    preload: preloadPath,
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: false,
    webSecurity: true,
  }
})
```

### 4.2 开发/生产模式加载

| 模式 | 加载方式 | 说明 |
|------|----------|------|
| 开发 (`VITE_DEV=1`) | `loadURL('http://127.0.0.1:3000')` | 支持多端口重试（3000-3003, 5173） |
| 生产 | `loadFile('dist-react/index.html')` | 多路径候选查找，失败显示诊断页面 |

### 4.3 IPC 通道

| 通道 | 方向 | 功能 |
|------|------|------|
| `window:minimize` | R→M | 最小化窗口 |
| `window:maximize` | R→M | 切换最大化 |
| `window:close` | R→M | 关闭窗口 |
| `window:status` | R→M | 获取窗口状态 |
| `notification:show` | R→M | 显示系统通知 |
| `shell:open-path` | R→M | 打开文件/文件夹 |
| `shell:open-external` | R→M | 打开外部链接 |
| `export:save-file` | R→M | 保存文件对话框 |
| `app:check-for-update` | R→M | 手动检查更新 |
| `app:install-update` | R→M | 安装更新并重启 |
| `menu-export-data` | M→R | 菜单触发导出 |
| `update-*` | M→R | 更新进度事件 |

### 4.4 自动更新 (`electron-updater`)

- **OSS 版**：默认禁用（无更新服务器）
- **启用方式**：设置环境变量 `UPDATE_SERVER_URL`（或平台专用 `UPDATE_SERVER_URL_WIN/MAC/LINUX`）
- **更新策略**：
  - 启动 5 秒后首次检查
  - 每 4 小时后台定时检查
  - 静默下载，退出时自动安装
- **错误过滤**：网络错误/404 等良性错误不打扰用户

---

## 5. Preload 脚本 (`electron/preload.ts`)

通过 `contextBridge.exposeInMainWorld('electronAPI', {...})` 暴露安全 API：

```typescript
interface Window {
  electronAPI: {
    getAppVersion: () => string;
    getPlatform: () => string;
    getSystemVersion: () => string;
    getLocale: () => string;
    minimizeWindow: () => Promise<void>;
    maximizeWindow: () => Promise<void>;
    closeWindow: () => Promise<void>;
    showNotification: (title: string, body: string) => Promise<void>;
    exportSaveFile: (options: any) => Promise<any>;
    openPath: (path: string) => Promise<any>;
    openExternal: (url: string) => Promise<{ success: boolean; error?: string }>;
    onMenuExportData: (callback: () => void) => () => void;
    onUpdateAvailable: (callback: (info: any) => void) => () => void;
    onUpdateDownloadProgress: (callback: (progress: any) => void) => () => void;
    onUpdateDownloaded: (callback: (info: any) => void) => () => void;
    onUpdateError: (callback: (error: any) => void) => () => void;
    installUpdate: () => Promise<void>;
    checkForUpdates: () => Promise<{ checking: boolean }>;
  };
}
```

---

## 6. 环境变量配置

### 6.1 环境变量文件与优先级

| 文件 | 用途 |
|------|------|
| `.env.example` | 环境变量模板 |
| `.env.local` | 本地开发配置（不提交） |
| `.env.production` | 生产构建配置（`vite build` 生效） |
| `.env.template` | 推荐模板（复制后按环境修改） |
| `.env` | 通用环境变量 |

**Vite 优先级（高→低）**：
1. `.env.local`
2. `.env.[mode].local`
3. `.env.[mode]`
4. `.env`

> 说明：`npm run dev` 使用 `development` 模式，通常以 `.env.local` 为准。
> `npm run dashboard:build` / `vite build` 使用 `production` 模式，会读取 `.env.production`。

### 6.2 关键环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `VITE_API_BASE_URL` | 后端 API 地址 | `http://127.0.0.1:8000` |
| `VITE_WS_BASE_URL` | WebSocket 地址 | `ws://127.0.0.1:8003/api/v1/ws/market` |
| `VITE_USER_API_URL` | 用户认证 API 地址（可选） | `http://127.0.0.1:8000/api/v1` |
| `VITE_API_URL` | 开发代理目标 | `http://localhost:8000` |
| `VITE_PORT` | Vite 开发端口 | `3000` |
| `VITE_DEV` | 开发模式标志 | `1` |
| `UPDATE_SERVER_URL` | 自动更新服务器 | `https://updates.example.com` |
| `CSC_IDENTITY_AUTO_DISCOVERY` | 跳过代码签名 | `false` |

### 6.3 Vite Define 注入

以下变量在构建时被替换（OSS 版置空）：
- `COS_SECRET_ID`, `COS_SECRET_KEY`, `COS_BUCKET`, `COS_REGION`
- `TENCENT_SECRET_ID`, `TENCENT_SECRET_KEY`, `TENCENT_BUCKET`, `TENCENT_REGION`

### 6.4 Electron 配置模板（推荐）

```bash
# 开发模式
cp electron/.env.template electron/.env.local

# 生产构建
cp electron/.env.template electron/.env.production
```

模板核心项（示例）：

```bash
VITE_SERVICE_HOST=127.0.0.1
VITE_HTTP_PROTOCOL=http
VITE_API_BASE_URL=http://127.0.0.1:8000
VITE_WS_BASE_URL=ws://127.0.0.1:8003/api/v1/ws/market
```

**建议**：不设置 `VITE_USER_API_URL`，让认证模块自动走 `VITE_API_BASE_URL + /api/v1`。
如果必须设置，请写完整前缀：

```bash
VITE_USER_API_URL=http://127.0.0.1:8000/api/v1
```

本地另一个常见方案（Nginx 同域反向代理）：

```bash
VITE_API_BASE_URL=http://127.0.0.1
VITE_WS_BASE_URL=ws://127.0.0.1/ws/api/v1/ws/market
```

---

## 7. NPM Scripts 详解

### 7.1 开发命令

| 命令 | 说明 |
|------|------|
| `npm run dev` | 并发启动 Vite (3000) + Electron |
| `npm run dev:react` | 仅启动 Vite 开发服务器 |
| `npm run dev:electron` | 构建 Electron TS + 等待 3000 端口 + 启动 Electron |
| `npm run dev:simple` | 简化版 Electron 开发启动 |

### 7.2 构建命令

| 命令 | 说明 |
|------|------|
| `npm run build` | 构建 React + Electron |
| `npm run build:react` | 仅构建 React（复制 Monaco 资源 + Vite build） |
| `npm run build:electron` | 仅构建 Electron（tsc + 复制图标） |
| `npm run build:package` | 完整构建 + electron-builder（不发布） |

### 7.3 打包命令

| 命令 | 说明 |
|------|------|
| `npm run build:package:win` | Windows 打包（自定义脚本） |
| `npm run build:package:win:dir` | Windows 仅打包目录（不生成安装程序） |
| `npm run build:package:win:builder` | Windows NSIS + Portable |
| `npm run build:package:win:publish` | Windows 打包并发布 |

### 7.4 其他命令

| 命令 | 说明 |
|------|------|
| `npm run clean` | 清理构建产物 |
| `npm run typecheck` | TypeScript 类型检查 |
| `npm run lint` | ESLint 检查 |
| `npm run test` | Vitest 单元测试 |
| `npm run test:e2e` | Playwright E2E 测试 |
| `npm run preview` | Vite 生产预览 |
| `npm run start` | 直接启动 Electron（需已构建） |

---

## 8. 完整编译流程

### 8.1 环境准备（仓库根目录）

```bash
# 1. 安装 Node.js（推荐 v20+）
node -v  # >= 20.0.0

# 2. 在仓库根目录安装依赖（workspace）
npm install

# 3. 配置 Electron 开发环境变量
cp electron/.env.example electron/.env.local
# 编辑 electron/.env.local 设置后端地址
```

### 8.2 开发模式

```bash
# 在仓库根目录启动（Vite + Electron 并发）
npm run dev

# 访问地址：
# - Vite 开发服务器: http://localhost:3000
# - Electron 窗口: 自动打开
```

### 8.3 生产构建

```bash
# 方式一：仅构建（不打包）
npm run build
# 输出：
# - dist-react/    (React 静态文件)
# - dist-electron/ (Electron 主进程 JS)

# 方式二：完整打包
npm run build:package
# 输出：
# - dist/          (安装包/可执行文件)
```

### 8.4 Windows 打包

```bash
# 完整打包（NSIS 安装程序 + Portable）
npm run build:package:win

# 仅打包目录（用于调试）
npm run build:package:win:dir

# 使用 electron-builder 直接打包
npm run build:package:win:builder
```

**打包产物**：
```
dist/
├── win-unpacked/                           # 解压后的应用目录
├── QuantMind量化助手 Setup 1.0.0.exe       # NSIS 安装程序
└── QuantMind量化助手 1.0.0 Portable.exe    # 便携版
```

### 8.5 macOS 打包

```bash
# 构建 + 打包
npm run build:package

# 产物
dist/
├── mac/                                    # .app 应用
├── QuantMind量化助手-1.0.0.dmg             # DMG 安装包
└── QuantMind量化助手-1.0.0-mac.zip         # ZIP 压缩包
```

**注意**：macOS 代码签名需要配置证书，当前配置 `identity: null` 跳过签名。

### 8.6 Linux 打包

```bash
# 构建 + 打包
npm run build:package

# 产物
dist/
├── linux-unpacked/                         # 解压后的应用目录
├── QuantMind量化助手-1.0.0.AppImage        # AppImage
└── quantmind-quantmind_1.0.0_amd64.deb     # DEB 包
```

---

## 9. 跨平台编译

### 9.1 本地编译（推荐）

在目标平台上直接编译：

| 平台 | 要求 | 产物 |
|------|------|------|
| Windows | Windows 10/11, Node.js | NSIS + Portable |
| macOS | macOS 12+, Node.js | DMG + ZIP |
| Linux | Ubuntu 20.04+, Node.js | AppImage + DEB |

### 9.2 CI/CD 自动化

```yaml
# GitHub Actions 示例
jobs:
  build:
    strategy:
      matrix:
        os: [windows-latest, macos-latest, ubuntu-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: cd electron && npm ci
      - run: cd electron && npm run build:package
      - uses: actions/upload-artifact@v4
        with:
          name: quantmind-${{ matrix.os }}
          path: electron/dist/
```

---

## 10. 后端通信配置

### 10.1 API 客户端 (`src/services/api-client.ts`)

```typescript
// Axios 配置
- 请求拦截器: 自动附加 Bearer Token + X-Tenant-Id + X-Request-ID
- 响应拦截器: 401 自动刷新 Token
- 重试逻辑: 指数退避，最多 3 次，仅 GET/HEAD/OPTIONS 的 5xx 错误
```

### 10.2 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| API 网关 | 8000 | 用户认证、策略管理 |
| 引擎服务 | 8001 | Qlib 回测、AI 策略 |
| 交易服务 | 8002 | 订单、持仓、风控 |
| 流媒体服务 | 8003 | 实时行情、WebSocket |

### 10.3 Web 部署代理配置

```nginx
# Nginx 配置示例
server {
    listen 80;
    server_name your-domain.com;

    # React 静态文件
    location / {
        root /var/www/html;
        try_files $uri $uri/ /index.html;
    }

    # API 代理
    location /api/ {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket 代理
    location /ws/ {
        proxy_pass http://localhost:8003;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## 11. 常见问题排查

### 11.1 构建失败

**问题**: `npm run build:electron` 报错
```bash
# 清理后重新构建
npm run clean
npm install
npm run build:electron
```

**问题**: `dist-react/index.html` 找不到
```bash
# 确保先构建 React
npm run build:react
# 检查输出目录
ls dist-react/
```

### 11.2 Electron 启动失败

**问题**: 开发模式无法连接 Vite
```bash
# 检查 Vite 是否正常运行
curl http://localhost:3000

# 手动指定端口
VITE_PORT=3000 npm run dev
```

**问题**: 生产模式白屏
```bash
# 检查 index.html 路径
# 查看控制台日志，会输出候选路径
# 确保 dist-react/ 目录存在且包含 index.html
```

### 11.3 打包失败

**问题**: electron-builder 下载 Electron 二进制慢
```bash
# 使用国内镜像
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ npm run build:package
```

**问题**: Windows 打包缺少图标
```bash
# 确保 build/ 目录包含正确图标
ls build/icon.ico build/logo.ico build/logo.png
```

**问题**: macOS 签名失败
```bash
# 跳过签名（开发/测试）
CSC_IDENTITY_AUTO_DISCOVERY=false npm run build:package
```

### 11.4 运行时问题

**问题**: API 请求 404
```bash
# 检查环境变量
cat electron/.env.local
# 确认 VITE_API_BASE_URL 指向正确的后端地址
```

**问题**: 前端无法登录（`/auth/login` 404）
```bash
# 典型误配：VITE_USER_API_URL 缺少 /api/v1
# 错误示例
VITE_USER_API_URL=http://YOUR_SERVER_HOST

# 正确示例
VITE_USER_API_URL=http://YOUR_SERVER_HOST/api/v1
# 或者直接不设置该变量（推荐）
```

认证服务会优先读取 `VITE_USER_API_URL`；若该变量存在但缺少 `/api/v1`，登录请求可能被发到错误路径导致失败。

本地默认可直接使用：

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000
# 可选：
VITE_USER_API_URL=http://127.0.0.1:8000/api/v1
```

**问题**: WebSocket 连接失败
```bash
# 检查 WebSocket 地址格式
# 正确格式: ws://host:8003/api/v1/ws/market
# 错误格式: http://host:8003
```

---

## 12. 性能优化建议

### 12.1 构建优化

```typescript
// vite.config.ts
{
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
          antd: ['antd'],
          echarts: ['echarts'],
          monaco: ['monaco-editor'],
        }
      }
    }
  }
}
```

### 12.2 运行时优化

- **懒加载**: 主要页面使用 `React.lazy()` + `Suspense`
- **GPU 加速**: 已启用 `enable-gpu-rasterization` + `enable-zero-copy`
- **内存限制**: `force-gpu-mem-available-mb=2048`

### 12.3 依赖优化

```bash
# 使用 npm ci 确保可重复安装
npm ci

# 检查依赖更新
npm outdated

# 清理未使用依赖
npx depcheck
```

---

## 13. 发布流程

### 13.1 版本更新

```bash
# 1. 更新版本号
npm version patch  # 1.0.0 -> 1.0.1
# 或
npm version minor  # 1.0.0 -> 1.1.0
# 或
npm version major  # 1.0.0 -> 2.0.0

# 2. 构建打包
npm run build:package

# 3. 测试安装包
# 安装并验证功能

# 4. 发布（如配置了自动更新服务器）
npm run build:package:win:publish
```

### 13.2 自动更新服务器配置

```bash
# 设置更新服务器 URL
export UPDATE_SERVER_URL=https://your-update-server.com/releases

# 或使用平台专用 URL
export UPDATE_SERVER_URL_WIN=https://your-server.com/win
export UPDATE_SERVER_URL_MAC=https://your-server.com/mac
```

更新服务器需提供 `latest.yml`（Windows）或 `latest-mac.yml`（macOS）文件，包含版本信息和下载链接。

---

## 14. 附录

### 14.1 关键依赖版本

| 依赖 | 版本 | 用途 |
|------|------|------|
| electron | 40.8.0 | 桌面应用框架 |
| electron-builder | 24.13.3 | 打包工具 |
| electron-updater | 6.6.2 | 自动更新 |
| electron-log | 5.4.3 | 日志记录 |
| vite | 7.2.4 | 构建工具 |
| react | 18.3.1 | UI 框架 |
| typescript | 5.4.5 | 类型系统 |
| axios | 1.7.2 | HTTP 客户端 |
| antd | 5.18.0 | UI 组件库 |
| echarts | 6.0.0 | 图表库 |
| monaco-editor | 0.55.1 | 代码编辑器 |

### 14.2 安全配置

| 配置项 | 值 | 说明 |
|--------|-----|------|
| contextIsolation | true | 上下文隔离 |
| nodeIntegration | false | 禁用 Node.js 集成 |
| sandbox | false | 沙箱模式（关闭以兼容） |
| webSecurity | true | Web 安全策略 |
| webviewTag | false | 禁用 webview |

### 14.3 快捷键

| 快捷键 | 功能 |
|--------|------|
| Ctrl/Cmd + R | 重新加载 |
| Ctrl/Cmd + Shift + R | 强制重新加载（忽略缓存） |
| Ctrl/Cmd + Shift + I | 开发者工具 |
| Ctrl/Cmd + Shift + E | 导出数据 |
| Ctrl/Cmd + M | 最小化 |
| Ctrl/Cmd + W | 关闭窗口 |
