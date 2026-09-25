# wecom-ops-hub

> An enterprise-WeChat (WeCom) self-built application server: HTTP probe monitoring & alerting, custom app menu, and a full web configuration panel.
> Run `docker compose up -d`, fill in your WeCom credentials in the panel, and you're done.

## What it does

1. **Monitoring & alerting** — Configure any HTTP probe (URL / method / expected status / timeout / interval). When a probe fails consecutively past the threshold, an alert is pushed to WeCom; when it recovers, a recovery notice is sent. The same issue won't repeat within the silence window.
2. **Custom menu** — Edit your WeCom self-built app menu in the panel and push it with one click. Supports `view` (open a web page) and `click` (trigger a server-side action, e.g. "silence 1 hour").
3. **Web configuration panel** — All settings (WeCom credentials, notification policy, monitor targets, menu) are editable in the browser — no env-var tinkering, no restarts. Works in WeCom's in-app browser on mobile.

## Quick start

### 1. Prepare a machine reachable from the public internet

WeCom's "receive message" callback and menu links require the panel to have a public URL. Use a cloud server or a tunnel (frp / Cloudflare Tunnel / etc.) to expose the container port. Remember this URL, e.g. `https://hub.example.com`.

### 2. Pull the image and start

```bash
mkdir -p wecom-ops-hub && cd wecom-ops-hub

cat > docker-compose.yml <<'EOF'
services:
  wecom-ops-hub:
    image: ghcr.io/yisboss/wecom-ops-hub:latest
    container_name: wecom-ops-hub
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

cat > .env <<EOF
HUB_PORT=8080
HUB_ADMIN_PASSWORD=change-me-to-a-strong-password
EOF

docker compose up -d
```

Open `http://<host-IP>:<HUB_PORT>` and log in with the initial password (default `admin` — **change it immediately in the panel**).

### 3. Create a self-built app in WeCom

1. Log in to the [WeCom admin console](https://work.weixin.qq.com/) → App Management → App → Self-built → Create
2. Note down the **Corp ID**, **Agent ID**, and **Secret**
3. In the app detail page, find "Receive Messages" → set up API receive:
   - **URL**: `https://<your-public-url>/wecom/callback`
   - Let the system generate **Token** and **EncodingAESKey**, note them down
   - Clicking "Save" makes WeCom send a GET verification request; the panel verifies the signature and returns echostr automatically
4. Fill these values into the panel's **Settings** page and click "Test" to confirm you can get an access_token

> If "Save" in the WeCom console doesn't error, the callback link is working. If it errors, check that the public URL is reachable and the port is open. See [Deployment & callback troubleshooting](docs/deploy.md).

### 4. Start using it

- **Add a monitor target**: Panel → Targets → New → fill in URL, expected status, interval. An intentionally wrong URL triggers an immediate WeCom alert; fixing it triggers a recovery notice.
- **Generate a menu**: Panel → Menu → one-click default template → push. The WeCom app will show four buttons: status / self-test / panel / silence-1h.
- **Configure an action**: Each target can bind an HTTP action (e.g. `POST https://your-service/restart`); when an alert fires you can manually confirm execution in the panel.

## Panel pages

| Page | Route | What you can do |
|---|---|---|
| Status | `#/status` | Overview: total targets / down count / recent alerts |
| Targets | `#/targets` | CRUD probes, probe now, run actions, view history |
| Settings | `#/settings` | WeCom params, notification policy, test connectivity, callback URL |
| Menu | `#/menu` | Edit / generate / push / delete WeCom menu |
| Alerts | `#/alerts` | Alert history |

## Configuration

All config is done in the panel's Settings page. Environment variables are only for first-boot bootstrap:

| Env var | Default | Description |
|---|---|---|
| `HUB_PORT` | `8080` | Host port (container always 8080) |
| `HUB_ADMIN_PASSWORD` | `admin` | Initial login password, changeable in panel |
| `HUB_DATA_DIR` | `./data` | SQLite DB + logs directory |
| `TZ` | `Asia/Shanghai` | Timezone |

Panel settings (WeCom credentials, notification policy, monitor engine, etc.) are stored in SQLite and take effect immediately — no restart needed. Full field reference: [`docs/deploy.md`](docs/deploy.md).

## What to do on the WeCom side

See [WeCom backend setup guide](docs/wecom-setup.md). Brief:

1. Create a self-built app, get corpid / agentid / secret
2. Enable "Receive Messages" API, get callback_token / callback_aes_key, set URL to the panel's public address
3. (Optional) Configure "Trusted IP" allowlist — if your outbound IP isn't on it, fill a reverse-proxy URL into the panel's `wecom.proxy_url`
4. Add the people who should receive alerts to the app's visible range

## Tech stack

- **Backend**: Python 3.11+ / FastAPI / uvicorn, SQLite storage
- **Frontend**: plain static HTML/CSS/JS (no npm build step, works offline)
- **Dependencies**: fastapi, uvicorn[standard], httpx, pycryptodome, jinja2 (optional) — no heavy compile-required deps
- **Image**: multi-arch (linux/amd64 + linux/arm64), distributed via GHCR

## License

[MIT](LICENSE)
