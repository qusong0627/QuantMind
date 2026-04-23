# QuantMind 服务器部署指南

## 快速部署（推荐）

在服务器上执行以下命令：

```bash
# 一键部署
curl -fsSL https://gitee.com/qusong0627/quantmind/raw/master/deploy/quick-deploy.sh | sudo bash
```

## 指定服务器IP

脚本会自动检测服务器IP，优先级如下：
1. 命令行参数：`sudo ./deploy.sh 192.168.1.100`
2. 环境变量：`export QUANTMIND_SERVER_IP=192.168.1.100 && sudo ./deploy.sh`
3. 自动检测：公网IP → 局域网IP → localhost

### 本机部署（无公网IP）

```bash
# 方式1：使用localhost
sudo ./deploy.sh localhost

# 方式2：使用局域网IP
sudo ./deploy.sh 192.168.1.100

# 方式3：让脚本自动检测
sudo ./deploy.sh
```

## 手动部署

### 1. 克隆代码

```bash
sudo git clone https://gitee.com/qusong0627/quantmind.git /opt/quantmind
cd /opt/quantmind
```

### 2. 执行部署脚本

```bash
sudo chmod +x deploy/deploy.sh
sudo ./deploy/deploy.sh
```

## 部署步骤说明

### 第一阶段：系统准备
- 更新系统依赖
- 安装 Docker & Docker Compose
- 安装 Node.js 20.x LTS
- 安装 Nginx

### 第二阶段：代码部署
- 从 Gitee 克隆代码
- 配置环境变量
- 创建数据目录

### 第三阶段：后端部署
- 构建 Docker 镜像
- 启动 PostgreSQL、Redis、QuantMind 服务
- 初始化数据库

### 第四阶段：前端部署
- 安装 npm 依赖
- 构建生产版本
- PM2 启动服务

### 第五阶段：Nginx 配置
- 配置反向代理
- 启动 Nginx

### 第六阶段：验证
- 健康检查
- 防火墙配置

## 访问地址

部署完成后，根据部署方式访问：

| 服务 | 地址 |
|-----|------|
| 前端 | http://服务器IP 或 http://localhost |
| 后端 API | http://服务器IP:8000 |
| Engine | http://服务器IP:8001 |
| Trade | http://服务器IP:8002 |
| Stream | http://服务器IP:8003 |

> 本机部署时，使用 `http://localhost` 访问前端

## 默认账号

- 用户名：`admin`
- 密码：需要通过 API 重置

## 常用命令

```bash
# 查看后端日志
docker compose -f /opt/quantmind/docker-compose.yml logs -f

# 查看前端日志
pm2 logs quantmind-web

# 重启后端
docker compose -f /opt/quantmind/docker-compose.yml restart

# 重启前端
pm2 restart quantmind-web

# 重启 Nginx
systemctl restart nginx

# 查看服务状态
docker compose -f /opt/quantmind/docker-compose.yml ps
pm2 status
```

## 目录结构

```
/opt/quantmind/
├── backend/            # 后端代码
├── electron/           # 前端代码
├── docker-compose.yml
├── .env                # 环境配置
└── data/               # 数据目录
    ├── postgres/       # 数据库数据
    ├── redis/          # Redis 数据
    ├── logs/           # 日志
    ├── models/         # 模型文件
    └── qlib_data/      # Qlib 数据
```

## 端口说明

| 端口 | 服务 | 说明 |
|-----|------|------|
| 80 | Nginx | HTTP 入口 |
| 443 | Nginx | HTTPS 入口 |
| 3000 | Frontend | 前端服务 |
| 8000 | API | 后端 API |
| 8001 | Engine | 回测引擎 |
| 8002 | Trade | 交易服务 |
| 8003 | Stream | 实时行情 |
| 5432 | PostgreSQL | 数据库 |
| 6379 | Redis | 缓存 |

## 故障排查

### 后端无法启动

```bash
# 查看日志
docker compose logs quantmind

# 检查数据库连接
docker exec quantmind-db pg_isready -U quantmind

# 检查 Redis 连接
docker exec quantmind-redis redis-cli ping
```

### 前端无法访问

```bash
# 检查 PM2 状态
pm2 status

# 重启前端
pm2 restart quantmind-web
```

### Nginx 错误

```bash
# 测试配置
nginx -t

# 查看日志
tail -f /var/log/nginx/error.log
```

## 更新部署

```bash
cd /opt/quantmind

# 拉取最新代码
git pull origin master

# 重新构建后端
docker compose build
docker compose up -d

# 重新构建前端
npm install
npm run dashboard:build
pm2 restart quantmind-web
```

## 卸载

```bash
# 停止服务
docker compose -f /opt/quantmind/docker-compose.yml down
pm2 delete quantmind-web
systemctl stop nginx

# 删除数据（谨慎操作）
rm -rf /opt/quantmind
```
