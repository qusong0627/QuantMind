# QuantMind Web 部署指南

## 概述

QuantMind 支持两种前端部署模式：

1. **Electron 桌面应用** - 原生桌面体验
2. **Web 应用** - 浏览器访问，便于服务器部署

## Web 模式部署

### 方式一：开发模式（推荐用于测试）

```bash
# 1. 安装依赖
npm install

# 2. 启动后端服务
docker-compose up -d

# 3. 启动前端开发服务器
npm run dev:web

# 或使用脚本
./scripts/start-web.sh
```

访问：`http://localhost:3000`

### 方式二：生产模式（推荐用于部署）

```bash
# 1. 构建生产版本
npm run dashboard:build

# 2. 启动预览服务器
npm run dashboard:preview

# 或使用脚本
./scripts/start-web-prod.sh
```

访问：`http://localhost:3000`

### 环境变量配置

```bash
# API 地址（默认：http://localhost:8000）
export VITE_API_URL=http://your-server:8000

# WebSocket 地址（默认：ws://localhost:8000）
export VITE_WS_URL=ws://your-server:8000

# 监听端口（默认：3000）
export VITE_PORT=3000

# 监听地址（默认：0.0.0.0）
export VITE_HOST=0.0.0.0
```

### 使用 PM2 部署（推荐）

```bash
# 安装 PM2
npm install -g pm2

# 启动服务
pm2 start npm --name "quantmind-web" -- run dashboard:preview

# 查看状态
pm2 status

# 查看日志
pm2 logs quantmind-web

# 开机自启
pm2 startup
pm2 save
```

### 使用 Nginx 反向代理

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # 前端
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }

    # 后端 API
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8000/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## Electron 模式部署

```bash
# 开发
npm run dev

# 构建 Windows 安装包
npm run dashboard:package:win

# 构建 Windows 便携版
npm run dashboard:package:win:dir
```

## 功能差异

| 功能 | Electron | Web |
|-----|----------|-----|
| 策略回测 | ✅ | ✅ |
| 模拟交易 | ✅ | ✅ |
| 实盘交易 | ✅ (QMT) | ❌ |
| AI 助手 | ✅ | ✅ |
| 自动更新 | ✅ | ❌ |
| 本地文件 | ✅ | ⚠️ (下载) |
| 系统通知 | ✅ | ⚠️ (需授权) |

## 注意事项

1. **Electron API 兼容**：Web 模式自动降级 Electron 特有功能
2. **文件导出**：Web 模式使用浏览器下载替代本地文件保存
3. **实盘交易**：Web 模式不支持 QMT 实盘交易
4. **跨域配置**：确保后端 API 配置了正确的 CORS

## 快速启动（完整流程）

```bash
# 1. 克隆项目
git clone https://gitee.com/qusong0627/quantmind.git
cd quantmind

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 配置数据库等

# 3. 启动后端
docker-compose up -d

# 4. 启动前端
npm install
npm run dev:web

# 5. 访问
# 前端：http://localhost:3000
# 后端：http://localhost:8000
```
