# Argus

> 名字取自希腊神话里的百眼巨人 —— 永不闭眼的守望者。
>
> 企业微信自建应用的服务端：HTTP 探针监控告警 + 自建应用菜单 + 全功能 Web 配置面板。
> 一条 `docker compose up -d` 起服务，面板里填自己的企微参数就能用。

## 它能干什么

1. **监控告警** — 配置任意 HTTP 探针（URL / 方法 / 期望状态码 / 超时 / 间隔）。探针连续失败到阈值就往企业微信推一条告警；恢复了再推一条恢复通知。同一问题在静音窗口内不重复打扰。
2. **自定义菜单** — 在面板里编辑企业微信自建应用的菜单，一键推送到企微。支持 `view`（打开网页）和 `click`（触发服务端动作，例如「静音 1 小时」）两种类型。
3. **Web 配置面板** — 所有设置（企微凭据、通知策略、监控目标、菜单）都在浏览器里改，不用改环境变量、不用重启容器。手机企微里点开也能用。

## 快速开始

### 1. 准备一台能被公网访问的机器

企业微信的「接收消息」回调和菜单跳转都要求面板有一个公网可达的 URL。你可以用：

- 公网云服务器
- 内网穿透（frp / Lucky / Cloudflare Tunnel 等）把容器端口暴露出去

记住这个公网 URL，例如 `https://hub.example.com`，后面要填进面板。

### 2. 拉镜像启动

```bash
# 下载 docker-compose.yml（或从仓库自取）
mkdir -p argus && cd argus

cat > docker-compose.yml <<'EOF'
services:
  argus:
    image: ghcr.io/yisboss/argus:latest
    container_name: argus
    restart: unless-stopped
    ports:
      - "${HUB_PORT:-8080}:8080"
    environment:
      TZ: "${TZ:-Asia/Shanghai}"
      HUB_ADMIN_PASSWORD: "${HUB_ADMIN_PASSWORD:-admin}"
      HUB_DATA_DIR: "/data"
    volumes:
      - "${HUB_DATA_DIR:-./data}:/data"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
EOF

# 可选：改初始端口和初始密码
cat > .env <<EOF
HUB_PORT=8080
HUB_ADMIN_PASSWORD=改成你自己的强密码
EOF

docker compose up -d
```

> **拉不到镜像？** 如果这一步报 `denied` 或 `not found`（国内网络访问不了 ghcr.io，或包未设为公开），
> 改用从源码构建 —— 效果完全一样，且不依赖任何镜像仓库：
>
> ```bash
> git clone https://github.com/YisBoss/argus.git
> cd argus
> cp .env.example .env      # 改掉 HUB_ADMIN_PASSWORD
> docker compose -f docker-compose.build.yml up -d --build
> ```

启动后访问 `http://<宿主机IP>:<HUB_PORT>`，用初始密码登录（默认 `admin`，**请立刻在面板里改掉**）。

### 3. 在企业微信后台创建自建应用

1. 登录 [企业微信管理端](https://work.weixin.qq.com/) → 应用管理 → 应用 → 自建 → 创建应用
2. 记下 **企业 ID（corpid）**、**应用 AgentId**、**应用 Secret**
3. 在应用详情页找到「接收消息」→ 设置 API 接收：
   - **URL** 填 `https://<你的公网URL>/wecom/callback`
   - **Token** 和 **EncodingAESKey** 让系统随机生成，记下来
   - 点「保存」时企微会向你的 URL 发一个 GET 验证请求，面板会自动校验签名并返回 echostr
4. 把上面这些值填进面板「设置」页，点「测试」确认能取到 access_token

> 「接收消息」保存不报错 = 回调链路通了。如果报错，先检查公网 URL 是否可达、端口是否放行，详见 [部署与回调排障](docs/deploy.md)。

### 4. 开始用

- **加一个监控目标**：面板「目标」页 → 新建 → 填 URL、期望状态码、间隔。故意填错 URL 会立刻收到企微告警，改回正确会收到恢复通知。
- **生成菜单**：面板「菜单」页 → 一键生成默认模板 → 推送。企微应用里就能看到「📊 状态」「🧪 自检」「⚙️ 面板」「🔕 静音1小时」四个按钮。
- **配置动作**：每个目标可以绑一个 HTTP 动作（例如 `POST https://your-service/restart`），告警触发时可在面板里手动点确认执行。

## 面板功能一览

| 页面 | 路由 | 能做什么 |
|---|---|---|
| 状态 | `#/status` | 概览：目标总数 / 宕机数 / 最近告警 |
| 目标 | `#/targets` | 增删改 HTTP 探针、立即探测、执行动作、看历史 |
| 设置 | `#/settings` | 企微参数、通知策略、测试连通、回调 URL 展示 |
| 菜单 | `#/menu` | 编辑/一键生成/推送/删除企微菜单 |
| 告警 | `#/alerts` | 告警历史 |

## 配置项

所有配置项都在面板「设置」页改。环境变量只用于首次启动引导：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `HUB_PORT` | `8080` | 宿主机端口（容器内固定 8080） |
| `HUB_ADMIN_PASSWORD` | `admin` | 首次登录密码，登录后可在面板改 |
| `HUB_DATA_DIR` | `./data` | SQLite 数据库与日志目录 |
| `TZ` | `Asia/Shanghai` | 时区 |

面板里的设置项（企微凭据、通知策略、监控引擎等）存在 SQLite 里，改完即时生效，不需要重启。完整字段见 [`docs/deploy.md`](docs/deploy.md)。

## 企业微信侧需要做什么

详见 [企业微信后台配置指南](docs/wecom-setup.md)。简要：

1. 创建自建应用，拿 corpid / agentid / secret
2. 开启「接收消息」API，拿 callback_token / callback_aes_key，URL 填面板公网地址
3. （可选）配置「企业可信 IP」白名单 —— 若本机出口 IP 不在白名单，可填一个反代地址到面板的 `wecom.proxy_url`
4. 应用可见范围里加上要收告警的人

## 技术栈

- **后端**：Python 3.11+ / FastAPI / uvicorn，SQLite 存储
- **前端**：纯静态 HTML/CSS/JS（无 npm 构建步骤，离线可用）
- **依赖**：fastapi、uvicorn[standard]、httpx、pycryptodome、jinja2（可选），不引入需编译的重依赖
- **镜像**：多架构（linux/amd64 + linux/arm64），GHCR 分发

## 项目结构

```
argus/
├── app/
│   ├── settings.py        # 配置项 schema（契约文件）
│   ├── api/               # 后端路由
│   └── static/            # 面板 UI（单页应用）
├── docs/
│   ├── CONTRACT.md        # 冻结契约（API/表结构/文件所有权）
│   ├── deploy.md          # 部署与回调排障
│   ├── wecom-setup.md     # 企微后台配置指南
│   └── wecom-api-spec.md  # 企微 API 实测笔记
├── docker-compose.yml
├── Dockerfile
├── .env.example
└── README.md
```

## 许可证

[MIT](LICENSE)
